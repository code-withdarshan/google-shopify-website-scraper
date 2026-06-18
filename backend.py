"""
Shopify Vertical Scraper - Backend
Discovers Shopify-powered stores via Google Search using local Chrome cookies (CDP),
then verifies each domain against Jewelry / Fashion / Retail using NVIDIA Build API.
"""
import os
import re
import sys
import json
import time
import socket
import threading
import webbrowser
import subprocess
from urllib.parse import urlparse, quote_plus, parse_qs, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# Optional imports (degrade gracefully when missing at runtime)
try:
    import browser_cookie3
except Exception:
    browser_cookie3 = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

import db as _db

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except Exception:
    webdriver = None


# ----------------------------------------------------------------------------
# Paths (PyInstaller-aware)
# ----------------------------------------------------------------------------
def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "nvidia_api_key": "",
    "nvidia_base_url": "https://integrate.api.nvidia.com/v1",
    "nvidia_model": "deepseek-ai/deepseek-r1",
    "nvidia_reasoning": True,
    "chrome_remote_port": 9222,
    "proxy_url": "",                # e.g. "socks5://127.0.0.1:1080" or "http://user:pass@host:port"
    "min_delay_sec": 1.5,           # min delay between queries
    "max_delay_sec": 4.0,           # max delay between queries (random pick)
    "verticals": ["Jewelry", "Fashion", "Retail"],
}

# Curated fallback list of NVIDIA Build models (used only when /api/models can't
# fetch the live catalog). Ordered by speed:quality balance for niche
# classification — fastest acceptable model first.
NVIDIA_REASONING_MODELS = [
    # ── Fast small models (≤8B params, ~1s responses) ────────────────────
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "meta/llama-3.1-8b-instruct",
    "microsoft/phi-3.5-mini-instruct",
    "mistralai/mistral-7b-instruct-v0.3",
    "qwen/qwen2.5-7b-instruct",
    # ── Mid-size models (~30B, fast + accurate) ──────────────────────────
    "qwen/qwq-32b",
    "deepseek-ai/deepseek-r1-distill-qwen-32b",
    "mistralai/mistral-nemotron",
    # ── Strong reasoning + general (49B–70B) ─────────────────────────────
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.1-nemotron-51b-instruct",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "deepseek-ai/deepseek-r1-distill-llama-70b",
    # ── DeepSeek family (R1 reasoning + V3 fast/chat) ────────────────────
    "deepseek-ai/deepseek-r1",
    "deepseek-ai/deepseek-r1-0528",
    "deepseek-ai/deepseek-v3.1",
    "deepseek-ai/deepseek-v3-0324",
    "deepseek-ai/deepseek-v3",
    # ── Heavy hitters (slow but best) ────────────────────────────────────
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "meta/llama-3.1-405b-instruct",
]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # env override
    if os.environ.get("NVIDIA_API_KEY"):
        cfg["nvidia_api_key"] = os.environ["NVIDIA_API_KEY"]
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ----------------------------------------------------------------------------
# Chrome cookies via local browser (CDP / direct cookie store)
# ----------------------------------------------------------------------------
def get_chrome_cookies_for(domain: str) -> requests.cookies.RequestsCookieJar:
    """Pull cookies for `domain` from the locally-installed Chrome profile."""
    jar = requests.cookies.RequestsCookieJar()
    if not browser_cookie3:
        return jar
    try:
        cj = browser_cookie3.chrome(domain_name=domain)
        for c in cj:
            jar.set(c.name, c.value, domain=c.domain, path=c.path)
    except Exception as e:
        print(f"[cookies] {domain}: {e}", file=sys.stderr)
    return jar


def cdp_attach_cookies(port: int, domain: str) -> requests.cookies.RequestsCookieJar:
    """If Chrome is running with --remote-debugging-port, pull cookies via CDP."""
    jar = requests.cookies.RequestsCookieJar()
    try:
        tabs = requests.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
        if not tabs:
            return jar
        ws_url = tabs[0].get("webSocketDebuggerUrl")
        if not ws_url:
            return jar
        # Use websocket-client if present; otherwise skip silently.
        try:
            import websocket  # type: ignore
        except Exception:
            return jar
        ws = websocket.create_connection(ws_url, timeout=3, origin="http://localhost")
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        resp = json.loads(ws.recv())
        ws.close()
        for c in resp.get("result", {}).get("cookies", []):
            if domain in (c.get("domain") or ""):
                jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    except Exception:
        pass
    return jar


# ----------------------------------------------------------------------------
# Google Search → candidate Shopify stores
# ----------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------------
# Local-Chrome (debugger) driven search
# ----------------------------------------------------------------------------
def _find_chrome_exe() -> str | None:
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def _debugger_alive(port: int) -> bool:
    try:
        requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        return True
    except Exception:
        return False


def _ws_origin_allowed(port: int) -> bool:
    """Try a real websocket handshake — Chrome 111+ rejects with 403 if
    --remote-allow-origins wasn't set at launch time."""
    try:
        import websocket  # type: ignore
    except Exception:
        return True  # can't test → assume yes
    try:
        tabs = requests.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
    except Exception:
        return False
    if not tabs:
        return True
    ws_url = tabs[0].get("webSocketDebuggerUrl")
    if not ws_url:
        return False
    try:
        w = websocket.create_connection(ws_url, timeout=3, origin="http://localhost")
        w.close()
        return True
    except Exception:
        return False


def _kill_debug_chrome(port: int, push=None) -> None:
    """Close all Chrome processes connected to our debug profile."""
    profile = os.path.join(APP_DIR, ".chrome-debug-profile")
    try:
        # tasklist + filter by command line — use wmic for the cmdline match
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='chrome.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
            stderr=subprocess.DEVNULL, text=True, timeout=8,
        )
        killed = 0
        for line in out.splitlines():
            if "remote-debugging-port" in line and (profile in line or f"port={port}" in line):
                pid = line.strip().split(",")[-1]
                if pid.isdigit():
                    subprocess.call(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    killed += 1
        if push: push(f"[chrome] killed {killed} stale debug-Chrome process(es)")
    except Exception as e:
        if push: push(f"[chrome] could not auto-kill: {e}")


def ensure_chrome_debugger(port: int = 9222, push=None) -> bool:
    """Make sure Chrome is running with --remote-debugging-port=<port>
    AND --remote-allow-origins (Chrome 111+). If a stale debugger is running
    without the origin flag, kill and relaunch it."""
    if _debugger_alive(port):
        if _ws_origin_allowed(port):
            if push: push(f"[chrome] debugger already running on port {port} (origin OK)")
            return True
        if push: push("[chrome] existing debugger rejects websocket origin — restarting…")
        _kill_debug_chrome(port, push=push)
        time.sleep(1.0)
        _CDP_TABS.clear()
    exe = _find_chrome_exe()
    if not exe:
        if push: push("[chrome] chrome.exe not found")
        return False
    profile = os.path.join(APP_DIR, ".chrome-debug-profile")
    os.makedirs(profile, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",           # Chrome 111+ requires this
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    proxy = (load_config().get("proxy_url") or "").strip()
    if proxy:
        args.append(f"--proxy-server={proxy}")
        if push: push(f"[chrome] using proxy: {proxy}")
    try:
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as e:
        if push: push(f"[chrome] launch failed: {e}")
        return False
    # wait up to 8s
    for _ in range(40):
        if _debugger_alive(port):
            if push: push(f"[chrome] debugger up on port {port}")
            return True
        time.sleep(0.2)
    if push: push("[chrome] debugger did not come up")
    return False


_DRIVER_CACHE: dict[int, object] = {}
_TAB_HANDLES: dict[str, str] = {}  # engine -> Chrome window handle (legacy)
_CDP_TABS: dict[str, dict] = {}    # engine -> {"id": targetId, "ws": webSocketDebuggerUrl}


def get_attached_driver(port: int = 9222):
    """Attach Selenium to the running Chrome debugger."""
    if not webdriver:
        return None
    if port in _DRIVER_CACHE:
        return _DRIVER_CACHE[port]
    opts = ChromeOptions()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    try:
        drv = webdriver.Chrome(options=opts)
        _DRIVER_CACHE[port] = drv
        return drv
    except Exception as e:
        print(f"[selenium] attach failed: {e}", file=sys.stderr)
        return None


ENGINE_HOME = {
    "google": "https://www.google.com",
}


# ----------------------------------------------------------------------------
# Raw CDP — one persistent tab per engine, parallel queries
# ----------------------------------------------------------------------------
def _cdp_list_tabs(port: int) -> list[dict]:
    try:
        return requests.get(f"http://127.0.0.1:{port}/json", timeout=2).json()
    except Exception:
        return []


def _cdp_open_tab(port: int, url: str) -> dict | None:
    """Open a new tab via Chrome's CDP HTTP endpoint."""
    try:
        # Modern Chrome uses PUT; older uses GET. PUT first, fall back to GET.
        r = requests.put(f"http://127.0.0.1:{port}/json/new?{quote_plus(url)}", timeout=5)
        if r.status_code >= 400:
            r = requests.get(f"http://127.0.0.1:{port}/json/new?{quote_plus(url)}", timeout=5)
        return r.json()
    except Exception as e:
        print(f"[cdp] open_tab failed: {e}", file=sys.stderr)
        return None


def ensure_cdp_engine_tabs(port: int = 9222, push=None) -> dict[str, dict]:
    """Open (or reuse) one tab per engine via CDP. Returns {engine: tab_info}."""
    if not _debugger_alive(port):
        return {}

    tabs = _cdp_list_tabs(port)
    # Match existing tabs by URL host to engines so we reuse what's already open
    for engine, home in ENGINE_HOME.items():
        if engine in _CDP_TABS:
            # Verify it's still alive
            alive_ids = {t.get("id") for t in tabs}
            if _CDP_TABS[engine].get("id") in alive_ids:
                continue
            _CDP_TABS.pop(engine, None)

        # Try to match an already-open tab
        matched = None
        host_key = urlparse(home).netloc.replace("www.", "")
        for t in tabs:
            tu = t.get("url", "")
            if host_key in tu and t.get("type") == "page":
                matched = t
                break
        if matched:
            _CDP_TABS[engine] = {"id": matched["id"], "ws": matched["webSocketDebuggerUrl"], "url": matched["url"]}
            if push: push(f"[cdp] reusing {engine} tab")
            continue

        info = _cdp_open_tab(port, home)
        if info and info.get("webSocketDebuggerUrl"):
            _CDP_TABS[engine] = {"id": info["id"], "ws": info["webSocketDebuggerUrl"], "url": home}
            if push: push(f"[cdp] opened {engine} tab")
        else:
            if push: push(f"[cdp] failed to open {engine}")

    return dict(_CDP_TABS)


def _cdp_run(ws_url: str, url: str, push=None, engine: str = "") -> dict:
    """Navigate tab → poll until results appear → return {links, no_results}."""
    try:
        import websocket  # type: ignore
    except Exception:
        msg = "[cdp] websocket-client NOT installed — run: pip install websocket-client"
        print(msg, file=sys.stderr)
        if push: push("    " + msg)
        return {"links": [], "no_results": False}
    try:
        ws = websocket.create_connection(ws_url, timeout=10, origin="http://localhost")
    except Exception as e:
        if push: push(f"    [{engine}] ws connect failed: {e}")
        return {"links": [], "no_results": False}

    msg_id = 0
    def call(method, params=None, wait=8):
        nonlocal msg_id
        msg_id += 1
        try:
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        except Exception as e:
            if push: push(f"    [{engine}] ws send error: {e}")
            return None
        end = time.time() + wait
        while time.time() < end:
            try:
                ws.settimeout(max(0.5, end - time.time()))
                resp = json.loads(ws.recv())
            except Exception:
                return None
            if resp.get("id") == msg_id:
                if "error" in resp:
                    if push: push(f"    [{engine}] cdp error: {resp['error']}")
                    return None
                return resp.get("result")
        return None

    links: list[str] = []
    no_results = False
    captcha = False
    try:
        call("Page.enable")
        call("Runtime.enable")
        if push: push(f"    [{engine}] → {url[:80]}…")
        nav = call("Page.navigate", {"url": url})
        if nav is None:
            if push: push(f"    [{engine}] navigate returned nothing")
        elif "errorText" in (nav or {}):
            if push: push(f"    [{engine}] navigate error: {nav['errorText']}")

        time.sleep(0.8)
        cur = call("Runtime.evaluate", {"expression": "location.href", "returnByValue": True}, wait=4)
        cur_url = (cur or {}).get("result", {}).get("value", "") if cur else ""
        if cur_url and "search" not in cur_url and "q=" not in cur_url:
            if push: push(f"    [{engine}] WARNING tab is at {cur_url[:80]} — search URL didn't load")

        deadline = time.time() + 15
        stable_since = None
        while time.time() < deadline:
            time.sleep(0.6)
            res = call("Runtime.evaluate", {
                "expression": (
                    "(function(){"
                    "  var t = (document.body && document.body.innerText) || '';"
                    "  var h = location.href || '';"
                    "  return JSON.stringify({"
                    "    ready: document.readyState,"
                    "    href: h,"
                    "    captcha: /\\/sorry\\/|unusual traffic|I'm not a robot|recaptcha|"
                    "ReCAPTCHA|select all images|select images|verify you are human|"
                    "429 Too Many Requests|Rate limit exceeded/i.test(h+' '+t),"
                    "    noResults: /did not match any documents|No results found|"
                    "your search.*did not match|did not match any/i.test(t),"
                    "    links: Array.from(document.querySelectorAll('a'))"
                    "      .map(a=>a.href).filter(h=>h && h.startsWith('http'))"
                    "  });"
                    "})()"
                ),
                "returnByValue": True,
            }, wait=5)
            if not res or not res.get("result"):
                continue
            payload_str = res["result"].get("value") or "{}"
            try:
                payload = json.loads(payload_str)
            except Exception:
                continue
            links = payload.get("links") or []
            ready = payload.get("ready")
            no_results = bool(payload.get("noResults"))
            captcha = bool(payload.get("captcha"))
            if captcha:
                break  # Google CAPTCHA — caller will pause the job
            if no_results:
                break  # "did not match any documents"
            if ready == "complete":
                if len(links) >= 15:
                    break
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > 2:
                    break
    finally:
        try: ws.close()
        except Exception: pass

    if push:
        suffix = ""
        if captcha: suffix = " · CAPTCHA"
        elif no_results: suffix = " · NO RESULTS"
        push(f"    [{engine}] {len(links)} raw anchors{suffix}")
    return {"links": links, "no_results": no_results, "captcha": captcha}


def _engine_url(engine: str, query: str, page: int = 0) -> str:
    """page 0 = first page (start=0). page 1 = start=10. etc."""
    if engine == "google":
        start = max(0, page) * 10
        return f"https://www.google.com/search?q={quote_plus(query)}&num=10&start={start}&hl=en"
    return ""


def _captcha_present_now(port: int) -> bool:
    """Quick check: does the Google tab currently show a CAPTCHA / /sorry/ page?"""
    tab = _CDP_TABS.get("google")
    if not tab:
        return False
    try:
        import websocket  # type: ignore
        ws = websocket.create_connection(tab["ws"], timeout=4, origin="http://localhost")
    except Exception:
        return False
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": (
                "JSON.stringify({"
                "href: location.href,"
                "captcha: /\\/sorry\\/|unusual traffic|recaptcha|select all images|"
                "verify you are human/i.test(location.href+' '+(document.body && document.body.innerText || ''))"
                "})"
            ),
            "returnByValue": True,
        }}))
        ws.settimeout(4)
        resp = json.loads(ws.recv())
        val = resp.get("result", {}).get("result", {}).get("value", "{}")
        return bool(json.loads(val).get("captcha"))
    except Exception:
        return False
    finally:
        try: ws.close()
        except Exception: pass


def _wait_for_captcha_clear(port: int, push=None, job=None, max_wait: int = 1200) -> bool:
    """Block until the Google tab is no longer on a CAPTCHA, or the user clicks Resume.
    Returns True if cleared naturally, False if user pressed Resume."""
    deadline = time.time() + max_wait
    last_log = 0
    while time.time() < deadline:
        # User clicked Resume in UI?
        if job and job.get("captcha_resume"):
            job["captcha_resume"] = False
            return False
        time.sleep(2.5)
        if not _captcha_present_now(port):
            if push: push("    ✅ CAPTCHA cleared — resuming.")
            return True
        if time.time() - last_log > 15:
            secs_left = int(deadline - time.time())
            if push: push(f"    …still waiting for CAPTCHA (auto-resume when solved, {secs_left}s left).")
            last_log = time.time()
    if push: push("    ⏱ CAPTCHA wait timed out — giving up on this query.")
    return False


def cdp_parallel_search(query: str, push=None, pages: int = 1) -> dict:
    """Run the query across `pages` Google result pages.
    Returns {urls: [...], captcha: bool}."""
    if not _CDP_TABS:
        return {"urls": [], "captcha": False}
    import concurrent.futures as cf
    captcha_hit = {"v": False}
    out_urls: list[str] = []

    def work(engine: str):
        tab = _CDP_TABS.get(engine)
        if not tab:
            return engine, []
        raw: list[str] = []
        for p in range(max(1, pages)):
            result = _cdp_run(
                tab["ws"], _engine_url(engine, query, p), push=push,
                engine=f"{engine} p{p+1}" if pages > 1 else engine,
            )
            page_links = result.get("links", [])
            if result.get("captcha"):
                captcha_hit["v"] = True
                break
            if result.get("no_results"):
                if push: push(f"    [{engine}] Google: no results — skipping to next query")
                break
            if not page_links:
                break
            raw.extend(page_links)
        # decode redirector links
        cleaned = []
        for h in raw:
            if "duckduckgo.com/l/" in h:
                qs = parse_qs(urlparse(h).query)
                real = (qs.get("uddg") or [""])[0]
                if real: cleaned.append(unquote(real)); continue
            if h.startswith("https://www.google.com/url?"):
                qs = parse_qs(urlparse(h).query)
                real = (qs.get("q") or qs.get("url") or [""])[0]
                if real.startswith("http"): cleaned.append(real); continue
            cleaned.append(h)
        return engine, _clean(cleaned)

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        for engine, urls in ex.map(work, list(ENGINE_HOME.keys())):
            out_urls.extend(urls)
            if push: push(f"    [{engine}] {len(urls)} results")
    return {"urls": out_urls, "captcha": captcha_hit["v"]}


def ensure_engine_tabs(driver, push=None) -> dict[str, str]:
    """Open (or reuse) one persistent tab per engine. Returns {engine: handle}."""
    global _TAB_HANDLES
    if not driver:
        return {}

    # Drop handles that no longer exist
    live = set(driver.window_handles)
    _TAB_HANDLES = {e: h for e, h in _TAB_HANDLES.items() if h in live}

    for engine, home in ENGINE_HOME.items():
        if engine in _TAB_HANDLES:
            continue
        try:
            # Open a new tab
            driver.switch_to.new_window("tab")
            handle = driver.current_window_handle
            driver.get(home)
            _TAB_HANDLES[engine] = handle
            if push: push(f"[chrome] opened tab for {engine}")
        except Exception as e:
            if push: push(f"[chrome] could not open {engine} tab: {e}")

    # Close any extra "about:blank" tabs that aren't ours
    try:
        ours = set(_TAB_HANDLES.values())
        for h in list(driver.window_handles):
            if h not in ours:
                driver.switch_to.window(h)
                if driver.current_url in ("about:blank", "chrome://newtab/", "data:,"):
                    driver.close()
        if _TAB_HANDLES:
            driver.switch_to.window(next(iter(_TAB_HANDLES.values())))
    except Exception:
        pass

    return dict(_TAB_HANDLES)


def _extract_links_from_page(driver) -> list[str]:
    try:
        anchors = driver.find_elements(By.CSS_SELECTOR, "a")
    except Exception:
        return []
    out = []
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if href.startswith("http"):
            out.append(href)
    return out


def browser_search(query: str, engine: str, driver, push=None) -> list[str]:
    """Switch to the engine's persistent tab and run the query there."""
    if not driver:
        return []
    if engine == "google":
        url = f"https://www.google.com/search?q={quote_plus(query)}&num=20&hl=en"
    elif engine == "bing":
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count=20"
    elif engine == "duckduckgo":
        url = f"https://duckduckgo.com/?q={quote_plus(query)}"
    else:
        return []
    try:
        handle = _TAB_HANDLES.get(engine)
        if handle and handle in driver.window_handles:
            driver.switch_to.window(handle)
        driver.get(url)
        time.sleep(1.5)
    except Exception as e:
        if push: push(f"    [{engine}] nav error: {e}")
        return []
    raw = _extract_links_from_page(driver)
    # decode redirector URLs (ddg /l/?uddg=, google /url?q=)
    cleaned = []
    for h in raw:
        if "/l/?" in h or "duckduckgo.com/l/" in h:
            q = parse_qs(urlparse(h).query)
            real = (q.get("uddg") or [""])[0]
            if real: cleaned.append(unquote(real)); continue
        if h.startswith("https://www.google.com/url?"):
            q = parse_qs(urlparse(h).query)
            real = (q.get("q") or q.get("url") or [""])[0]
            if real.startswith("http"): cleaned.append(real); continue
        if h.startswith("http"):
            cleaned.append(h)
    return _clean(cleaned)


def browser_multi_search(query: str, driver, push=None) -> dict:
    """Search all three engines via the attached browser, return per-engine results."""
    return {
        "google": browser_search(query, "google", driver, push),
        "bing": browser_search(query, "bing", driver, push),
        "duckduckgo": browser_search(query, "duckduckgo", driver, push),
    }


# ----------------------------------------------------------------------------
# URL normalisation + dedupe
# ----------------------------------------------------------------------------
_TRACK_PARAMS = re.compile(
    r"^(utm_|gclid$|fbclid$|mc_eid$|mc_cid$|ref$|ref_src$|igshid$)", re.I
)


def normalize_url(u: str) -> str:
    """Canonicalise so duplicates collapse cleanly."""
    try:
        p = urlparse(u if "://" in u else "https://" + u)
    except Exception:
        return u.strip().lower()
    host = (p.netloc or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    scheme = "https"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    # strip tracking params
    keep = [kv for kv in (p.query or "").split("&")
            if kv and not _TRACK_PARAMS.match(kv.split("=", 1)[0])]
    q = "&".join(keep)
    return f"{scheme}://{host}{path}" + (f"?{q}" if q else "")


NOISE_HOSTS = (
    # Search / portals
    "google.", "bing.", "duckduckgo.", "duck.com", "ask.com", "yandex.",
    "baidu.", "naver.", "search.yahoo.", "ecosia.", "startpage.",
    # Google sub-services
    "maps.google.", "photos.google.", "books.google.", "scholar.google.",
    "translate.google.", "support.google.", "policies.google.", "accounts.google.",
    "play.google.", "store.google.", "g.co", "goo.gl", "youtu.be",
    # Social / video
    "youtube.", "facebook.", "fb.com", "fb.me", "instagram.", "instagr.am",
    "pinterest.", "pin.it", "reddit.", "redd.it", "tiktok.",
    "twitter.", "x.com", "t.co", "linkedin.", "lnkd.in",
    "snapchat.", "threads.net", "tumblr.", "vimeo.", "twitch.",
    "whatsapp.", "wa.me", "telegram.", "t.me", "discord.",
    # Listings / reviews
    "yelp.", "yellowpages.", "tripadvisor.", "trustpilot.", "bbb.org",
    "glassdoor.", "indeed.", "manta.com", "foursquare.",
    "justdial.", "sulekha.", "indiamart.", "tradeindia.",
    # Marketplaces (we want the store's OWN domain, not their listing page)
    "amazon.", "ebay.", "etsy.com", "alibaba.", "aliexpress.",
    "walmart.", "target.com", "flipkart.", "myntra.", "ajio.",
    # Knowledge bases / news / Q&A
    "wikipedia.", "wiktionary.", "medium.com", "quora.", "blogspot.",
    "wordpress.com", "stackoverflow.", "github.", "gitlab.",
    "apps.apple.", "microsoft.com", "apple.com/maps",
    # Generic CDN / image / file hosts
    "imgur.", "flickr.", "500px.", "behance.", "dribbble.",
    "drive.google.", "docs.google.", "sites.google.",
    "dropbox.", "onedrive.", "sharepoint.", "icloud.",
    # Web archive
    "web.archive.org", "archive.org",
)

# Extensions that aren't websites
BAD_EXTS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".mp3", ".mp4", ".mov", ".avi", ".webm", ".m4a", ".wav",
    ".exe", ".dmg", ".apk", ".msi", ".iso",
    ".json", ".xml", ".rss", ".atom", ".txt",
)

# Path fragments that signal a Google/utility link, not a real site
BAD_PATH_FRAGMENTS = (
    "/maps/", "/maps?", "/search?", "/imgres?", "/imghp",
    "/url?", "/sorry/", "/preferences", "/policies/",
    "/intl/", "/gws_rd", "/setprefs",
)

# Government / educational / non-profit TLDs we never want commercial leads from.
# Matches both root TLDs (example.gov) and country variants (example.gov.in, foo.ac.uk).
EXCLUDED_TLD_LABELS = {"gov", "edu", "mil", "ac", "org", "nic"}


def _is_excluded_tld(host: str) -> bool:
    parts = host.lower().strip(".").split(".")
    if len(parts) < 2:
        return False
    if parts[-1] in EXCLUDED_TLD_LABELS:
        return True  # foo.gov, foo.edu, foo.org
    if len(parts) >= 3 and parts[-2] in EXCLUDED_TLD_LABELS:
        return True  # foo.gov.in, foo.edu.au, foo.ac.uk, foo.org.in
    return False


def _clean(urls: list[str]) -> list[str]:
    """Dedupe + drop social/maps/images/PDFs/file links and other noise."""
    seen_norm, seen_host, out = set(), set(), []
    for u in urls:
        if not isinstance(u, str):
            continue
        u = u.strip()
        # Drop non-http(s) schemes (mailto:, tel:, javascript:, etc.)
        if not (u.startswith("http://") or u.startswith("https://")):
            continue
        try:
            n = normalize_url(u)
            p = urlparse(n)
            host = p.netloc
            path = p.path.lower()
        except Exception:
            continue
        if not host or n in seen_norm or host in seen_host:
            continue
        # Drop file downloads
        if any(path.endswith(ext) for ext in BAD_EXTS):
            continue
        # Drop noisy paths
        if any(frag in (path + "?") for frag in BAD_PATH_FRAGMENTS):
            continue
        # Drop noise hosts
        if any(b in host for b in NOISE_HOSTS):
            continue
        # Drop .gov / .edu / .org / .mil / .ac (and country variants)
        if _is_excluded_tld(host):
            continue
        # Drop IP addresses and localhost
        if host.startswith(("127.", "0.0.0.0", "localhost", "192.168.", "10.")):
            continue
        seen_norm.add(n)
        seen_host.add(host)
        out.append(n)
    return out


def duckduckgo_search(query: str, num: int = 20) -> list[str]:
    """DuckDuckGo HTML endpoint — no JS, scraping-friendly."""
    urls: list[str] = []
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=headers,
            timeout=15,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.result__a, a.result__url"):
            href = a.get("href", "")
            if href.startswith("//duckduckgo.com/l/"):
                href = "https:" + href
            if "duckduckgo.com/l/" in href:
                qs = parse_qs(urlparse(href).query)
                real = (qs.get("uddg") or [""])[0]
                if real:
                    urls.append(unquote(real))
            elif href.startswith("http"):
                urls.append(href)
    except Exception as e:
        print(f"[ddg] {e}", file=sys.stderr)
    return _clean(urls)[:num]


def bing_search(query: str, num: int = 20) -> list[str]:
    urls: list[str] = []
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    first = 1
    while len(urls) < num and first < 50:
        try:
            r = requests.get(
                "https://www.bing.com/search",
                params={"q": query, "first": str(first), "count": "20"},
                headers=headers,
                timeout=15,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            page = []
            for h2 in soup.select("li.b_algo h2 a"):
                href = h2.get("href", "")
                if href.startswith("http"):
                    page.append(href)
            if not page:
                break
            urls.extend(page)
            first += 20
            time.sleep(0.8)
        except Exception as e:
            print(f"[bing] {e}", file=sys.stderr)
            break
    return _clean(urls)[:num]


def google_search(query: str, num: int = 20, cookies=None) -> list[str]:
    """Google HTML scrape. Often blocked → callers should fall back."""
    urls: list[str] = []
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    start = 0
    while len(urls) < num and start < 50:
        params = {"q": query, "num": "10", "start": str(start), "hl": "en", "gbv": "1"}
        try:
            r = requests.get(
                "https://www.google.com/search",
                params=params,
                headers=headers,
                cookies=cookies,
                timeout=15,
            )
            if r.status_code != 200 or "Our systems have detected" in r.text or "/sorry/" in r.url:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            found_any = False
            for a in soup.select("a"):
                href = a.get("href", "")
                if href.startswith("/url?"):
                    qs = parse_qs(urlparse(href).query)
                    real = (qs.get("q") or qs.get("url") or [""])[0]
                    if real.startswith("http"):
                        urls.append(real); found_any = True
                elif href.startswith("http") and "google." not in urlparse(href).netloc:
                    urls.append(href); found_any = True
            if not found_any:
                break
            start += 10
            time.sleep(1.2)
        except Exception as e:
            print(f"[google] {e}", file=sys.stderr)
            break
    return _clean(urls)[:num]


def multi_search(query: str, num: int = 20, cookies=None, push=None) -> list[str]:
    """Try Google → Bing → DuckDuckGo. First engine to return results wins."""
    for name, fn in (
        ("google", lambda: google_search(query, num, cookies)),
        ("bing", lambda: bing_search(query, num)),
        ("ddg", lambda: duckduckgo_search(query, num)),
    ):
        try:
            res = fn()
            if push:
                push(f"    [{name}] {len(res)} results")
            if res:
                return res
        except Exception as e:
            if push:
                push(f"    [{name}] error: {e}")
    return []


# ----------------------------------------------------------------------------
# Shopify fingerprint
# ----------------------------------------------------------------------------
TECH_STACK_FINGERPRINTS = {
    # ── E-commerce platforms ──────────────────────────────────────────────
    "Shopify": ["cdn.shopify.com", "myshopify.com", "shopify.theme",
                "shopify-section", "/cdn/shop/", "x-shopify-stage", "x-shopid",
                "x-shardid", "shopify-features", "window.shopify"],
    "Salesforce Commerce": ["demandware", "dwstatic", "/on/demandware.store/",
                "/dw/image/", "dwac_", "x-dw-", "salesforce commerce", "sfcc",
                "demandware.edgesuite.net", "/dw/shop/", "dwfrm_"],
    "BigCommerce": ["bigcommerce.com", "stencilbootstrap", "x-bc-", "mybigcommerce.com",
                "cdn11.bigcommerce.com", "bigcommerce-labs"],
    "WooCommerce": ["woocommerce", "wc-blocks", "wc_add_to_cart", "wp-content/plugins/woocommerce",
                "woocommerce-page", "wc-ajax"],
    "Magento": ["mage.cookies", "/skin/frontend/", "mage/cookies", "magento",
                "/static/version", "mage-translation", "x-magento-", "/pub/static/",
                "magento_", "mage-init"],
    "Salla": ["salla.sa", "cdn.salla.network", "salla-"],
    "Zid": ["zid.store", "cdn.zid.sa"],
    "PrestaShop": ["prestashop", "/img/p/", "presta-", "id_product"],
    "Ecwid": ["ecwid", "app.ecwid.com"],
    "Squarespace Commerce": ["squarespace-commerce"],
    # ── CMS / website builders ────────────────────────────────────────────
    "WordPress": ["wp-content", "wp-includes", "wp-json", "/wp-admin",
                'name="generator" content="wordpress', "wp-block-", "wp-emoji"],
    "Wix": ["static.wixstatic.com", "wix-code", "wixsite.com", "x-wix-", "_wixcss",
                "x-wix-request-id", "wixstatic", "wix.com", "_wixapps"],
    "Squarespace": ["squarespace.com", "static1.squarespace.com", "static.squarespace.com",
                   "squarespace_context", "sqs-block", "squarespace-cdn", "this.squarespace"],
    "Webflow": ["webflow.com", "data-wf-page", ".webflow.io", "webflow.js", "wf-page", "w-mod-"],
    "GoHighLevel": ["msgsndr.com", "gohighlevel", "hl-builder", "leadconnectorhq.com", "highlevelpages.com"],
    "Drupal": ["drupal.settings", "/sites/default/files", "x-generator: drupal",
               'content="drupal', "drupal-", "/core/misc/drupal"],
    "Joomla": ["/components/com_", "joomla!", "joomla.javascript", "/media/jui/", "option=com_"],
    "Ghost": ["ghost.io", "/ghost/", 'content="ghost', "ghost-sdk", "casper"],
    "Framer": ["framer.com", "framerusercontent.com", "data-framer-", "framer-"],
    "Duda": ["dudaone.com", "multiscreensite.com", "d_ssr", "irp.cdn-website.com"],
    "Weebly": ["weebly.com", "weeblycloud", "weebly-footer", "editmysite.com"],
    "ClickFunnels": ["clickfunnels.com", "etison.com", "cfgenerator", "myclickfunnels"],
    "Kajabi": ["kajabi.com", "kajabi-cdn", "kajabi-storefronts"],
    "Cargo": ["cargocollective.com", "cargo.site"],
    "HubSpot": ["hs-scripts.com", "hubspot", "hsforms.com", "hs-sites.com", "hubspotusercontent"],
    "Shopline": ["shoplineapp.com", "shopline"],
    # ── JS frameworks (rendering tech) ────────────────────────────────────
    "Next.js": ["__next_data__", "_next/static", "/_next/data/", "next/dist", "__next"],
    "Nuxt.js": ["__nuxt__", "/_nuxt/", "nuxt-link", "data-nuxt", "window.__nuxt",
                "nuxt.js", "_nuxt/entry"],
    "Vue.js": ["vue.js", "data-v-", "data-server-rendered", "__vue__", "vue-router"],
    "React": ["react-dom", "data-reactroot", "_reactrootcontainer", "__reactcontainer",
              "react.production.min"],
    "Angular": ["ng-version", "angular.js", "ng-app", "_nghost", "_ngcontent"],
    "Gatsby": ["___gatsby", "gatsby-", "/page-data/"],
    "Svelte": ["svelte-", "__svelte"],
}

# Some platforms IMPLY others (e.g. WooCommerce runs on WordPress).
TECH_IMPLIES = {
    "WooCommerce": "WordPress",
}

# Platforms that are "real" e-commerce/CMS — distinguish from pure JS frameworks
# so a site detected as ONLY a framework is flagged for deeper inspection.
JS_FRAMEWORK_ONLY = {"Next.js", "Nuxt.js", "Vue.js", "React", "Angular", "Gatsby", "Svelte"}

ALL_TECH_STACKS = list(TECH_STACK_FINGERPRINTS.keys())


def _scan_tech(blob: str) -> tuple[list, dict]:
    """Run all fingerprints against a lowercased html+headers blob."""
    platforms, evidence = [], {}
    for plat, markers in TECH_STACK_FINGERPRINTS.items():
        hits = [m for m in markers if m.lower() in blob]
        if hits:
            platforms.append(plat)
            evidence[plat] = hits[:3]
    for child, parent in TECH_IMPLIES.items():
        if child in platforms and parent not in platforms:
            platforms.append(parent)
    return platforms, evidence


def detect_tech_stack(url: str, push=None) -> dict:
    """Fingerprint the site's tech stack. Strategy:
       1. Fetch with 3 UA fallbacks (Chrome → Safari → Googlebot)
       2. Scan HTML + response headers against all fingerprints
       3. If nothing found OR only a JS framework, render via Chrome CDP and re-scan
          (catches Nuxt/Next/Vue/SF-Commerce SPAs whose markers load client-side)
    """
    final_url = url
    blob = ""
    ok = False
    # ----- 1. Multi-UA static fetch -----
    for hdrs in _CONTACT_UA_FALLBACKS:
        try:
            r = requests.get(url, headers=hdrs, timeout=12, allow_redirects=True)
            if r.status_code < 400 and r.text:
                final_url = r.url
                hdr_str = " ".join(f"{k}:{v}" for k, v in r.headers.items()).lower()
                blob = (r.text[:400_000].lower()) + " " + hdr_str
                ok = True
                break
        except Exception:
            continue

    platforms, evidence = _scan_tech(blob) if blob else ([], {})

    # ----- 2. SPA render fallback -----
    # Trigger when: no platform found, OR only JS frameworks (markers often
    # inject after hydration), OR fetch was blocked entirely.
    only_framework = bool(platforms) and all(p in JS_FRAMEWORK_ONLY for p in platforms)
    if (not platforms or only_framework or not ok):
        cfg_now = load_config()
        port = cfg_now.get("chrome_remote_port", 9222)
        if _debugger_alive(port):
            if push: push(f"    [tech] {root_domain(url)} → rendering in Chrome for deeper detection…")
            rendered = _render_via_cdp(url, port=port, push=push)
            if rendered:
                rblob = rendered.lower()
                rp, re_ = _scan_tech(rblob)
                # Merge — keep evidence from both passes
                for p in rp:
                    if p not in platforms:
                        platforms.append(p)
                        evidence[p] = re_.get(p, [])
                ok = True
                final_url = url if final_url == url else final_url

    return {"platforms": platforms, "evidence": evidence, "final_url": final_url, "ok": ok or bool(platforms)}


def root_domain(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    host = p.netloc or p.path
    return host.lower().lstrip("www.")


# ----------------------------------------------------------------------------
# NVIDIA Build API classification (Jewelry / Fashion / Retail / Other)
# ----------------------------------------------------------------------------
def nvidia_classify_with_retry(url: str, cfg: dict, custom_niche: str = "",
                                attempts: int = 3) -> dict:
    """Call nvidia_classify up to `attempts` times on transient failures
    (429, 5xx, timeout, empty vertical). Backs off between tries."""
    last = {"vertical": "Unknown", "confidence": 0.0, "reason": ""}
    for attempt in range(attempts):
        v = nvidia_classify(url, cfg, custom_niche=custom_niche)
        vert = (v.get("vertical") or "").strip()
        reason = (v.get("reason") or "").lower()
        transient = (
            vert in ("", "Unknown")
            and any(t in reason for t in ("429", "timeout", "rate", "5", "connect", "network"))
        )
        if vert and vert != "Unknown":
            return v
        last = v
        if not transient:
            return v  # genuine "Other" or model rejection — no retry
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))  # 1.5s, 3s
    return last


def _nvidia_reachable(cfg: dict) -> bool:
    key = cfg.get("nvidia_api_key", "")
    if not key:
        return False
    try:
        r = requests.get(
            f"{cfg.get('nvidia_base_url','https://integrate.api.nvidia.com/v1').rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"}, timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?:(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?)?\d{3,4}[\s.\-]?\d{3,4}(?:[\s.\-]?\d{2,4})?)"
)
SOCIAL_PATTERNS = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?", re.I),
    "facebook":  re.compile(r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9.\-]{3,60})/?", re.I),
    "twitter":   re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{2,30})/?", re.I),
    "linkedin":  re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/([A-Za-z0-9\-_.]+)/?", re.I),
    "youtube":   re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|user/|@)([A-Za-z0-9_\-]+)/?", re.I),
    "tiktok":    re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)/?", re.I),
    "whatsapp":  re.compile(r"https?://(?:wa\.me|api\.whatsapp\.com/send)\??(?:phone=)?([+\d\s\-]{6,20})", re.I),
    "pinterest": re.compile(r"https?://(?:www\.)?pinterest\.[a-z.]+/([A-Za-z0-9_\-]+)/?", re.I),
}

# Junk emails we should never report (image-name false positives, tracking, etc.)
EMAIL_JUNK_PATTERNS = (
    "wixpress", "sentry", "@2x.", "@3x.", "example.com", "domain.com",
    "yourcompany", "your-email", "noreply@", "no-reply@", "donotreply@",
    "u003e", "u003c", ".png", ".jpg", ".svg", ".webp",
)


def _is_real_email(e: str) -> bool:
    el = e.lower()
    if len(el) > 80:
        return False
    if any(j in el for j in EMAIL_JUNK_PATTERNS):
        return False
    if el.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
        return False
    return True


def _normalise_phone(p: str) -> str:
    digits = re.sub(r"[^\d+]", "", p)
    if len(digits) < 8 or len(digits) > 16:
        return ""
    return digits


_CONTACT_UA_FALLBACKS = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml;q=0.9",
     "Accept-Language": "en-US,en;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
     "Accept-Language": "en-US,en;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
]


def _fetch_for_contacts(url: str) -> str:
    """Try multiple UAs, return HTML body (empty string on total failure)."""
    for hdrs in _CONTACT_UA_FALLBACKS:
        try:
            r = requests.get(url, headers=hdrs, timeout=12, allow_redirects=True)
            if r.status_code < 400 and r.text:
                return r.text[:500_000]
        except Exception:
            continue
    return ""


# Signals that a page is a client-rendered SPA whose static HTML doesn't
# contain the footer / contact info we want.
SPA_MARKERS = (
    "__NEXT_DATA__",
    "_next/static",
    "/_nuxt/",
    "data-reactroot",
    "react-dom",
    "id=\"root\"></div>",
    "id='root'></div>",
    "data-server-rendered",
    "data-react-helmet",
    "_app-",
)


def _looks_like_spa(html: str) -> bool:
    return any(m in html for m in SPA_MARKERS)


def _scan_next_data(html: str, found_emails: set, found_phones: set, socials: dict) -> bool:
    """Some Next/Nuxt sites bake email/phone/socials right into the SSR payload.
    Returns True if a payload was found and scanned."""
    found = False
    # __NEXT_DATA__ (Next.js pages router)
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.S)
    if m:
        try:
            payload = m.group(1)
            _scan_for_contacts(payload, found_emails, found_phones, socials)
            found = True
        except Exception:
            pass
    # Generic JSON-LD (often has email/phone/sameAs socials in Organization schema)
    for sm in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            blob = sm.group(1)
            # Inline scan handles emails / phones; sameAs URLs → socials
            _scan_for_contacts(blob, found_emails, found_phones, socials)
            for sameas in re.findall(r'"sameAs"\s*:\s*\[([^\]]+)\]', blob):
                for url_m in re.findall(r'"(https?://[^"]+)"', sameas):
                    _scan_for_contacts(url_m, found_emails, found_phones, socials)
            found = True
        except Exception:
            pass
    # Nuxt window.__NUXT__ payload (sometimes serialised inline)
    nm = re.search(r'window\.__NUXT__\s*=\s*(\{.*?\});', html, re.S)
    if nm:
        try:
            _scan_for_contacts(nm.group(1), found_emails, found_phones, socials)
            found = True
        except Exception:
            pass
    return found


def _render_via_cdp(url: str, port: int = 9222, wait_seconds: float = 4.0,
                    push=None) -> str:
    """Use Chrome's CDP to open a temporary tab, navigate, wait for client-side
    hydration, return rendered outerHTML. Closes the tab afterwards.
    Returns empty string if anything fails."""
    if not _debugger_alive(port):
        return ""
    # Create a new tab
    try:
        info = _cdp_open_tab(port, "about:blank")
        if not info or not info.get("webSocketDebuggerUrl"):
            return ""
        target_id = info["id"]
        ws_url = info["webSocketDebuggerUrl"]
    except Exception as e:
        if push: push(f"    [contacts/render] tab open failed: {e}")
        return ""

    rendered_html = ""
    try:
        import websocket  # type: ignore
        ws = websocket.create_connection(ws_url, timeout=10, origin="http://localhost")
        msg_id = 0
        def call(method, params=None, wait=8):
            nonlocal msg_id
            msg_id += 1
            ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            end = time.time() + wait
            while time.time() < end:
                ws.settimeout(max(0.5, end - time.time()))
                try:
                    resp = json.loads(ws.recv())
                except Exception:
                    return None
                if resp.get("id") == msg_id:
                    if "error" in resp: return None
                    return resp.get("result")
            return None
        call("Page.enable")
        call("Runtime.enable")
        call("Page.navigate", {"url": url})
        # Wait for hydration — listen briefly, then poll for stable readyState
        deadline = time.time() + wait_seconds + 8
        stable_since = None
        while time.time() < deadline:
            time.sleep(0.5)
            r = call("Runtime.evaluate", {
                "expression": "JSON.stringify({ready:document.readyState,len:(document.body?document.body.innerText.length:0)})",
                "returnByValue": True,
            }, wait=4)
            if not r or not r.get("result"): continue
            try:
                payload = json.loads(r["result"].get("value") or "{}")
            except Exception:
                continue
            if payload.get("ready") == "complete" and payload.get("len", 0) > 200:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > wait_seconds:
                    break
        # Pull final rendered HTML
        r = call("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        }, wait=6)
        if r and r.get("result") and r["result"].get("value"):
            rendered_html = r["result"]["value"][:800_000]
        try: ws.close()
        except Exception: pass
    except Exception as e:
        if push: push(f"    [contacts/render] cdp error: {e}")
    finally:
        # Always close the temporary tab
        try:
            requests.get(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=3)
        except Exception:
            pass
    return rendered_html


def _scan_for_contacts(html: str, found_emails: set, found_phones: set, socials: dict) -> None:
    """Pull emails/phones/socials out of a single HTML blob — mutates the sets."""
    if not html:
        return

    # Emails from raw HTML
    for m in EMAIL_RE.findall(html):
        if _is_real_email(m):
            found_emails.add(m.strip(".,;:()[]{}"))
    # Emails from mailto: anchors
    for m in re.findall(r'mailto:([^"\'\s>?]+)', html, re.I):
        addr = m.split("?")[0].strip()
        if _is_real_email(addr):
            found_emails.add(addr)
    # Obfuscated: name [at] domain [dot] com
    for m in re.findall(r"([A-Za-z0-9._%+\-]+)\s*[\[(]\s*at\s*[\])]\s*([A-Za-z0-9.\-]+)\s*[\[(]\s*dot\s*[\])]\s*([A-Za-z]{2,})", html, re.I):
        candidate = f"{m[0]}@{m[1]}.{m[2]}"
        if _is_real_email(candidate):
            found_emails.add(candidate)
    # Cloudflare email protection: data-cfemail="..."
    for cfhex in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html):
        try:
            key = int(cfhex[:2], 16)
            decoded = "".join(chr(int(cfhex[i:i+2], 16) ^ key) for i in range(2, len(cfhex), 2))
            if _is_real_email(decoded):
                found_emails.add(decoded)
        except Exception:
            pass

    # Socials
    for name, rx in SOCIAL_PATTERNS.items():
        for handle in rx.findall(html):
            h = (handle or "").strip()
            if h and h.lower() not in ("sharer", "share", "intent", "tr", "p", "pin", "create", "developers"):
                socials[name].add(h if name != "whatsapp" else _normalise_phone(h))

    # Phones — tel: links first (most reliable)
    for m in re.findall(r'tel:([^"\'\s>?]+)', html, re.I):
        n = _normalise_phone(m)
        if n: found_phones.add(n)
    # Plain-text phones inside phone-context chunks
    try:
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.extract()
        text = soup.get_text(" ", strip=True)
        for chunk in re.findall(r"[^.]*?(?:phone|call|tel|mobile|whatsapp|contact|reach us|customer care)[^.]{0,120}", text, re.I):
            for m in PHONE_RE.findall(chunk):
                n = _normalise_phone(m)
                if n and len(n) >= 9:
                    found_phones.add(n)
    except Exception:
        pass


def _extract_footer_html(html: str) -> str:
    """Return the HTML of the page's footer region, or empty string if none."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Strict footer first
        f = soup.find("footer")
        if f:
            return str(f)
        # Heuristic: divs/sections whose class/id contains footer/bottom/site-info
        for elem in soup.find_all(["div", "section", "aside"]):
            attrs = " ".join(
                (elem.get(k, "") if isinstance(elem.get(k, ""), str)
                 else " ".join(elem.get(k, [])))
                for k in ("class", "id", "role")
            ).lower()
            if any(kw in attrs for kw in ("footer", "site-info", "site-bottom", "page-footer", "contact-info")):
                return str(elem)
    except Exception:
        pass
    return ""


def _has_enough_contacts(emails: set, phones: set, socials: dict) -> bool:
    """We have 'enough' when we found at least: 1 email OR (1 phone + 1 social)."""
    n_social = sum(len(v) for v in socials.values())
    return len(emails) >= 1 or (len(phones) >= 1 and n_social >= 1)


def extract_contacts(base_url: str, push=None) -> dict:
    """Footer-first strategy:
       1. Fetch homepage → scan the FOOTER hard (where most sites put contacts)
       2. If footer didn't yield enough → scan the rest of the homepage
       3. If still not enough → crawl /contact, /about, etc.
       4. Stop as soon as we have ≥1 email or (≥1 phone + ≥1 social).
    """
    found_emails: set[str] = set()
    found_phones: set[str] = set()
    socials: dict[str, set[str]] = {k: set() for k in SOCIAL_PATTERNS}

    base = base_url.rstrip("/")
    fetched_any = False
    pages_tried: list[str] = []
    pages_skipped: int = 0

    # ----- 1. Homepage → footer first, then whole page -----
    home_html = _fetch_for_contacts(base_url)
    if home_html:
        fetched_any = True
        pages_tried.append("homepage")

        # 1a. JSON-LD + __NEXT_DATA__ payloads (great for SPAs that hardcode org info)
        if _scan_next_data(home_html, found_emails, found_phones, socials):
            pages_tried.append("__NEXT_DATA__/JSON-LD")

        # 1b. Footer first
        footer_html = _extract_footer_html(home_html)
        if footer_html:
            _scan_for_contacts(footer_html, found_emails, found_phones, socials)
            if push and (found_emails or found_phones or any(socials.values())):
                push(f"    [contacts] footer hit on {base_url}: "
                     f"📧{len(found_emails)} 📞{len(found_phones)} "
                     f"🔗{sum(len(v) for v in socials.values())}")

        # 1c. If footer was empty / weak, scan the whole homepage HTML
        if not _has_enough_contacts(found_emails, found_phones, socials):
            _scan_for_contacts(home_html, found_emails, found_phones, socials)

        # 1d. SPA fallback — if STILL empty AND page looks like a client-rendered
        # React/Next/Nuxt shell, render in Chrome and scan the rendered DOM.
        if (not _has_enough_contacts(found_emails, found_phones, socials)
                and _looks_like_spa(home_html)):
            cfg_now = load_config()
            port = cfg_now.get("chrome_remote_port", 9222)
            if push: push(f"    [contacts] {base_url} looks like a SPA — rendering in Chrome…")
            rendered = _render_via_cdp(base_url, port=port, push=push)
            if rendered:
                pages_tried.append("rendered-spa")
                # Re-scan footer of the rendered DOM
                rfooter = _extract_footer_html(rendered)
                if rfooter:
                    _scan_for_contacts(rfooter, found_emails, found_phones, socials)
                if not _has_enough_contacts(found_emails, found_phones, socials):
                    _scan_for_contacts(rendered, found_emails, found_phones, socials)
                if push:
                    push(f"    [contacts] post-render on {base_url}: "
                         f"📧{len(found_emails)} 📞{len(found_phones)} "
                         f"🔗{sum(len(v) for v in socials.values())}")
            elif push:
                push(f"    [contacts] SPA render failed (Chrome debugger unavailable?)")

    # ----- 2. If still not enough, crawl extra pages -----
    extra_pages = [
        f"{base}/contact",
        f"{base}/contact-us",
        f"{base}/contacts",
        f"{base}/pages/contact",
        f"{base}/pages/contact-us",
        f"{base}/about",
        f"{base}/about-us",
        f"{base}/pages/about",
        f"{base}/imprint",
        f"{base}/impressum",
        f"{base}/legal",
    ]
    seen = set([base_url])
    for page_url in extra_pages:
        if _has_enough_contacts(found_emails, found_phones, socials):
            pages_skipped = len(extra_pages) - extra_pages.index(page_url)
            break
        if page_url in seen:
            continue
        seen.add(page_url)
        html = _fetch_for_contacts(page_url)
        if not html:
            continue
        fetched_any = True
        pages_tried.append(page_url.rsplit("/", 1)[-1])
        # On these pages, scan the whole HTML (they ARE contact pages)
        _scan_for_contacts(html, found_emails, found_phones, socials)

    # ----- 3. Last-resort: follow footer links on homepage labelled contact/about -----
    if home_html and not _has_enough_contacts(found_emails, found_phones, socials):
        try:
            soup = BeautifulSoup(home_html, "html.parser")
            followed = 0
            for a in (soup.find("footer") or soup).find_all("a", href=True):
                href = a["href"].strip()
                label = a.get_text(" ", strip=True).lower()
                if not href.startswith(("http", "/")):
                    continue
                if not any(k in (href + " " + label).lower() for k in
                           ("contact", "about", "impressum", "imprint", "legal", "team", "get in touch")):
                    continue
                full = href if href.startswith("http") else (base + href if href.startswith("/") else f"{base}/{href}")
                if urlparse(full).netloc != urlparse(base_url).netloc:
                    continue
                if full in seen:
                    continue
                seen.add(full)
                sub = _fetch_for_contacts(full)
                if sub:
                    fetched_any = True
                    pages_tried.append(f"footer-link:{full.rsplit('/',1)[-1]}")
                    _scan_for_contacts(sub, found_emails, found_phones, socials)
                    followed += 1
                if followed >= 3 or _has_enough_contacts(found_emails, found_phones, socials):
                    break
        except Exception:
            pass

    if push and not fetched_any:
        push(f"    [contacts] all page fetches blocked for {base_url}")
    elif push and pages_skipped:
        push(f"    [contacts] {base_url} → fetched {len(pages_tried)} page(s), "
             f"skipped {pages_skipped} (already had enough info)")

    return {
        "emails": sorted(found_emails)[:10],
        "phones": sorted(found_phones)[:8],
        "socials": {k: sorted(v)[:5] for k, v in socials.items() if v},
        "fetched_any": fetched_any,
        "pages_tried": pages_tried,
    }


def _fetch_page_text(url: str) -> tuple[str, str]:
    """Fetch page → (visible_text, title). Tries 3 headers/UAs before giving up.
    Returns ("", "") if every attempt fails."""
    attempts = [
        {  # Modern Chrome on Windows
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Google Chrome";v="130", "Chromium";v="130"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        },
        {  # Safari on macOS
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
                          "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Accept-Language": "en-US,en;q=0.9",
        },
        {  # Googlebot (some sites whitelist it)
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        },
    ]
    for hdrs in attempts:
        try:
            r = requests.get(url, headers=hdrs, timeout=12, allow_redirects=True)
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            # Strip non-content tags before extracting text
            for t in soup(["script", "style", "noscript", "svg", "iframe"]):
                t.extract()
            txt = soup.get_text(" ", strip=True)
            if len(txt) < 100:
                continue  # likely a challenge / empty page → try next UA
            title = (soup.title.string.strip() if soup.title and soup.title.string else "")
            return txt[:3000], title
        except Exception:
            continue
    return "", ""


def _url_hint(url: str) -> str:
    """Decompose a URL into readable tokens so the model has hints even when
    the page text is empty (bot protection, 403, etc.)."""
    p = urlparse(url if "://" in url else "https://" + url)
    host = (p.netloc or "").lower().lstrip(".")
    if host.startswith("www."): host = host[4:]
    tld_strip = re.sub(r"\.(com|net|co|org|io|shop|store|app|au|uk|in|us|ca|nz|me|biz|info)(\.[a-z]{2})?$",
                       "", host)
    words = re.findall(r"[a-z]+", tld_strip)
    path_words = re.findall(r"[a-z]+", (p.path or "").lower())
    return f"domain words: {' '.join(words)} | path words: {' '.join(path_words[:8])}"


def nvidia_classify(url: str, cfg: dict, custom_niche: str = "") -> dict:
    """
    If `custom_niche` is provided, NVIDIA decides whether the site MATCHES that niche
    description (returns vertical = custom_niche or "Other"). Otherwise classifies
    into the configured Jewelry/Fashion/Retail/Other taxonomy.
    """
    if not OpenAI or not cfg.get("nvidia_api_key"):
        return {"vertical": "Unknown", "confidence": 0.0, "reason": "NVIDIA API not configured"}
    try:
        snippet, page_title = _fetch_page_text(url)
        url_hint = _url_hint(url)

        client = OpenAI(api_key=cfg["nvidia_api_key"], base_url=cfg["nvidia_base_url"])
        model = cfg.get("nvidia_model", "deepseek-ai/deepseek-r1")
        # Only Nemotron actually consumes chat_template_kwargs.thinking — other
        # reasoning models (R1, QwQ) emit reasoning automatically and may
        # reject the extension with a 404/400.
        reasoning_on = (
            bool(cfg.get("nvidia_reasoning", True))
            and "nemotron" in model.lower()
        )

        system_msg = (
            "You are an expert website classifier. "
            "Reason step by step internally, then output ONLY a strict JSON object "
            "on the FINAL line — no prose, no markdown fences."
        )

        page_block = (
            f"PAGE TITLE: {page_title}\n"
            f"URL HINT: {url_hint}\n"
            f"PAGE TEXT ({len(snippet)} chars):\n{snippet if snippet else '[empty — site blocked the fetch or returned no body]'}"
        )

        if custom_niche.strip():
            label = custom_niche.strip()
            user_msg = (
                f"Decide whether this website offers / sells / serves the niche:\n"
                f'  "{label}"\n\n'
                f"INCLUSIVE matching rules:\n"
                f"  • MATCH if the site genuinely carries this niche, EVEN IF it also "
                f"    sells other product categories alongside it (mixed-product stores "
                f"    and multi-category retailers ARE a match).\n"
                f"  • MATCH if it is a sub-category or specialty within this niche.\n"
                f"  • If PAGE TEXT is empty/blocked, infer from PAGE TITLE + URL HINT + "
                f"    domain words — make your best determination, do not refuse.\n"
                f"  • Tangential mentions alone do NOT count — there must be real "
                f"    product/service evidence (or strong URL/title evidence).\n\n"
                f"URL: {url}\n{page_block}\n\n"
                f'Final line MUST be JSON: '
                f'{{"vertical":"{label}|Other","confidence":0..1,"reason":"short justification"}}'
            )
        else:
            verticals = cfg["verticals"]
            user_msg = (
                f"Pick the BEST category for this website. Multi-category stores that "
                f"carry jewelry OR fashion as part of their range count as Jewelry or "
                f"Fashion — do NOT downgrade them to Retail just because they also sell "
                f"other things.\n\n"
                f"Categories: {', '.join(verticals)}, or Other.\n"
                f"  - Jewelry: site sells jewelry (rings, necklaces, watches, gems) — "
                f"alone or as part of a wider product mix.\n"
                f"  - Fashion: site sells apparel, shoes, or accessories — alone or "
                f"alongside other items.\n"
                f"  - Retail: general consumer-goods store with NO jewelry and NO fashion.\n"
                f"  - Other: not a consumer storefront (SaaS, food, services, B2B, etc.).\n"
                f"If PAGE TEXT is empty, infer from PAGE TITLE + URL HINT.\n\n"
                f"URL: {url}\n{page_block}\n\n"
                'Final line MUST be JSON: {"vertical":"Jewelry|Fashion|Retail|Other","confidence":0..1,"reason":"short justification"}'
            )

        kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2 if reasoning_on else 0.1,
            top_p=0.7,
            max_tokens=1500 if reasoning_on else 300,
        )
        # NVIDIA reasoning models accept an extra body param to expose chain-of-thought.
        if reasoning_on:
            kwargs["extra_body"] = {"chat_template_kwargs": {"thinking": True}}

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as api_e:
            err = str(api_e)
            # NVIDIA returns 404 for unknown extension params; retry without extra_body
            if "404" in err and "extra_body" in kwargs:
                print(f"[nvidia] 404 with extra_body — retrying without it", file=sys.stderr)
                kwargs.pop("extra_body", None)
                try:
                    resp = client.chat.completions.create(**kwargs)
                except Exception as api_e2:
                    print(f"[nvidia] retry also failed for {url}: {api_e2}", file=sys.stderr)
                    return {"vertical": "Unknown", "confidence": 0.0,
                            "reason": f"API error after retry: {str(api_e2)[:200]}", "model": model}
            else:
                print(f"[nvidia] API call failed for {url}: {api_e}", file=sys.stderr)
                return {"vertical": "Unknown", "confidence": 0.0,
                        "reason": f"API error: {str(api_e)[:200]}", "model": model}
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        if not content:
            reasoning_trace = getattr(msg, "reasoning_content", None) or ""
            print(f"[nvidia] empty content for {url}; reasoning len={len(reasoning_trace)}", file=sys.stderr)
            if not reasoning_trace:
                return {"vertical": "Unknown", "confidence": 0.0,
                        "reason": "model returned empty response", "model": model}

        # Some reasoning models stream <think>...</think> blocks — strip them.
        content_clean = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        # Capture the LAST JSON object on the response (post-reasoning).
        matches = re.findall(r"\{[^{}]*\"vertical\"[^{}]*\}", content_clean, re.S)
        if not matches:
            matches = re.findall(r"\{.*?\}", content_clean, re.S)
        reasoning_trace = getattr(msg, "reasoning_content", None) or ""
        if matches:
            try:
                data = json.loads(matches[-1])
                return {
                    "vertical": data.get("vertical", "Other"),
                    "confidence": float(data.get("confidence", 0)),
                    "reason": data.get("reason", "") or (reasoning_trace[:240] if reasoning_trace else ""),
                    "model": model,
                }
            except Exception:
                pass
        # Couldn't extract JSON — log so we can see what the model actually said
        print(f"[nvidia] could not parse JSON from response for {url}", file=sys.stderr)
        print(f"[nvidia] raw content: {content_clean[:400]}", file=sys.stderr)
        return {"vertical": "Other", "confidence": 0.0,
                "reason": "could not parse JSON · raw: " + content_clean[:200], "model": model}
    except Exception as e:
        import traceback as _tb
        print(f"[nvidia] unexpected exception for {url}: {e}", file=sys.stderr)
        print(_tb.format_exc(), file=sys.stderr)
        return {"vertical": "Unknown", "confidence": 0.0, "reason": f"NVIDIA error: {e}"}


# ----------------------------------------------------------------------------
# Job runner
# ----------------------------------------------------------------------------
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def run_continue_job(job_id: str, cfg: dict) -> None:
    """Resume an interrupted job — skips Phase 1, only verifies URLs that were
    saved to scraper.db but never made it through tech-stack/NVIDIA verification."""
    try:
        rows = _db.list_jobs(1000)
        job = next((j for j in rows if j["id"] == job_id), None)
        if not job:
            return
        params = job.get("params", {}) or {}

        # Load what's already on disk
        saved_urls = _db.get_job_urls(job_id)
        prior_verifs = _db.get_job_verifications(job_id)
        verified_hosts = {root_domain(v["url"]) for v in prior_verifs if v.get("url")}

        # Init job state in memory
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "running",
                "phase": "verify",
                "progress": 30,
                "log": [f"▶ Continuing job — {len(saved_urls)} URLs on disk, "
                        f"{len(prior_verifs)} already verified."],
                "results": [],
                "all_verified": list(prior_verifs),
                "rejected": [],
                "collected": [u["url"] for u in saved_urls],
                "stop_requested": False,
            }
        _db.update_job_status(job_id, "running")

        def push(m: str):
            print(m); JOBS[job_id]["log"].append(m)

        classify_niche = bool(params.get("classify_niche", True)) and bool(cfg.get("nvidia_api_key"))
        custom_niche = (params.get("custom_niche") or "").strip()
        do_extract_contacts = bool(params.get("extract_contacts", False))

        # Pick up the URLs that have NOT been verified yet
        remaining = [u["url"] for u in saved_urls
                     if root_domain(u["url"]) not in verified_hosts]

        # Also pick up URLs whose niche came back "Unknown" — re-classify them
        # when NVIDIA is now available.
        reclassify: list[dict] = []
        if classify_niche:
            for v in prior_verifs:
                vert = (v.get("vertical") or "").strip()
                if vert in ("", "Unknown") and v.get("url"):
                    reclassify.append(v)

        # Backfill contacts for prior rows that have none (when requested).
        contact_backfill: list[dict] = []
        if do_extract_contacts:
            for v in prior_verifs:
                if v.get("url") and not (v.get("emails") or v.get("phones") or v.get("socials")):
                    contact_backfill.append(v)

        push(f"PHASE 2 (continued) — {len(remaining)} new URLs to verify, "
             f"{len(reclassify)} unknown niches to re-classify, "
             f"{len(contact_backfill)} rows to backfill contacts.")
        all_verified: list[dict] = list(prior_verifs)

        for i, u in enumerate(remaining):
            if JOBS[job_id].get("stop_requested"):
                push("⏹ Stop requested — moving to filter step.")
                break
            JOBS[job_id]["progress"] = 30 + int(60 * i / max(1, len(remaining)))
            det = detect_tech_stack(u, push=push)
            final_url = det["final_url"]
            platforms = det["platforms"]
            platform_str = ", ".join(platforms) if platforms else "Unknown"
            verdict = {"vertical": "Unknown", "confidence": 0.0, "reason": ""}
            if classify_niche:
                # Attempt classification even when page fetch failed — nvidia_classify
                # does its own snippet fetch and can also classify from URL alone.
                verdict = nvidia_classify_with_retry(final_url, cfg, custom_niche=custom_niche)
            contacts = {"emails": [], "phones": [], "socials": {}}
            if do_extract_contacts:
                try:
                    contacts = extract_contacts(final_url, push=push)
                except Exception as ce:
                    push(f"    [contacts] {root_domain(final_url)} extraction failed: {ce}")
            row = {
                "domain": root_domain(final_url),
                "url": final_url,
                "platforms": platforms,
                "platform_str": platform_str,
                "evidence": det.get("evidence", {}),
                "vertical": verdict["vertical"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "emails": contacts.get("emails") or [],
                "phones": contacts.get("phones") or [],
                "socials": contacts.get("socials") or {},
            }
            all_verified.append(row)
            _db.record_verification(job_id, row)
            JOBS[job_id]["all_verified"] = list(all_verified)
            contact_str = ""
            if do_extract_contacts:
                n_email = len(row["emails"])
                n_phone = len(row["phones"])
                n_social = sum(len(v) for v in row["socials"].values())
                contact_str = f" · 📧{n_email} 📞{n_phone} 🔗{n_social}"
            push(f"  [{i+1}/{len(remaining)}] {row['domain']} → stack: {platform_str}"
                 + (f" · niche: {verdict['vertical']} ({verdict['confidence']:.2f})" if classify_niche else "")
                 + contact_str)

        # Re-classify previously-Unknown rows
        if reclassify:
            push(f"Re-classifying {len(reclassify)} prior 'Unknown' rows with NVIDIA…")
            for i, v in enumerate(reclassify):
                if JOBS[job_id].get("stop_requested"):
                    break
                JOBS[job_id]["progress"] = 75 + int(15 * i / max(1, len(reclassify)))
                verdict = nvidia_classify_with_retry(v["url"], cfg, custom_niche=custom_niche)
                # Update the in-memory row AND DB
                for r in all_verified:
                    if r.get("url") == v.get("url"):
                        r["vertical"] = verdict["vertical"]
                        r["confidence"] = verdict["confidence"]
                        r["reason"] = verdict["reason"]
                        _db.record_verification(job_id, r)
                        break
                push(f"  [reclass {i+1}/{len(reclassify)}] {v.get('domain','')} → "
                     f"{verdict['vertical']} ({verdict['confidence']:.2f})")
            JOBS[job_id]["all_verified"] = list(all_verified)

        # Backfill contacts for prior rows that had none
        if contact_backfill:
            push(f"📇 Backfilling contacts on {len(contact_backfill)} rows…")
            for ci, v in enumerate(contact_backfill):
                if JOBS[job_id].get("stop_requested"):
                    break
                try:
                    contacts = extract_contacts(v["url"], push=push)
                except Exception as ce:
                    push(f"    [contacts] {v.get('domain','')} extraction failed: {ce}")
                    contacts = {"emails": [], "phones": [], "socials": {}}
                for r in all_verified:
                    if r.get("url") == v.get("url"):
                        r["emails"] = contacts.get("emails") or []
                        r["phones"] = contacts.get("phones") or []
                        r["socials"] = contacts.get("socials") or {}
                        _db.record_verification(job_id, r)
                        break
                n_e, n_p = len(contacts.get("emails") or []), len(contacts.get("phones") or [])
                n_s = sum(len(x) for x in (contacts.get("socials") or {}).values())
                push(f"  [contacts {ci+1}/{len(contact_backfill)}] {v.get('domain','')} → 📧{n_e} 📞{n_p} 🔗{n_s}")
            JOBS[job_id]["all_verified"] = list(all_verified)

        # Final auto-sweep — any rows still Unknown after retries get one more pass
        if classify_niche:
            unknowns = [r for r in all_verified
                        if (r.get("vertical") or "Unknown") in ("", "Unknown")]
            if unknowns and _nvidia_reachable(cfg):
                push(f"🔁 Auto-sweep: re-trying {len(unknowns)} remaining Unknown niches…")
                for r in unknowns:
                    if JOBS[job_id].get("stop_requested"):
                        break
                    v = nvidia_classify_with_retry(r["url"], cfg, custom_niche=custom_niche)
                    r["vertical"] = v["vertical"]
                    r["confidence"] = v["confidence"]
                    r["reason"] = v["reason"]
                    _db.record_verification(job_id, r)
                JOBS[job_id]["all_verified"] = list(all_verified)

        # PHASE 3 — filter using the original params
        JOBS[job_id]["phase"] = "filter"
        selected_stacks = params.get("tech_stacks") or []
        vertical = (params.get("vertical") or "all").strip()
        niche_filter = custom_niche if custom_niche else (vertical if vertical != "all" else "")

        def keep(r: dict) -> bool:
            if selected_stacks:
                if not any(p in selected_stacks for p in r.get("platforms", [])):
                    return False
            else:
                if not r.get("platforms"):
                    return False
            if classify_niche and niche_filter:
                if (r.get("vertical") or "").strip().lower() != niche_filter.strip().lower():
                    return False
            return True

        results = [r for r in all_verified if keep(r)]
        rejected = [r for r in all_verified if not keep(r)]
        for r in results:
            r["accepted"] = True
            _db.record_verification(job_id, r)
        for r in rejected:
            r["accepted"] = False
            _db.record_verification(job_id, r)

        final_status = "stopped" if JOBS[job_id].get("stop_requested") else "done"
        _db.update_job_status(job_id, final_status, ended=True)
        with JOBS_LOCK:
            JOBS[job_id].update(
                results=results, rejected=rejected,
                progress=100, status=final_status, phase=final_status,
            )
        push(f"Continuation {'stopped' if final_status=='stopped' else 'done'}. "
             f"Total analysed {len(all_verified)} · matched {len(results)} · rejected {len(rejected)}.")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id].setdefault("log", []).append(f"❌ Continue crashed: {e}\n{tb}")
        try: _db.update_job_status(job_id, "error", ended=True)
        except Exception: pass


def run_scrape_job(job_id: str, payload: dict, cfg: dict) -> None:
    try:
        return _run_scrape_job_inner(job_id, payload, cfg)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        with JOBS_LOCK:
            j = JOBS.get(job_id, {})
            j["status"] = "error"
            j.setdefault("log", []).append(f"❌ Crash: {e}\n{tb}")
        try: _db.update_job_status(job_id, "error", ended=True)
        except Exception: pass


def _run_scrape_job_inner(job_id: str, payload: dict, cfg: dict) -> None:
    vertical = (payload.get("vertical") or "all").strip()
    custom_niche = (payload.get("custom_niche") or "").strip()
    country = (payload.get("country") or "").strip()
    state = (payload.get("state") or "").strip()
    city = (payload.get("city") or "").strip()
    area = (payload.get("area") or "").strip()
    pages_per_query = max(1, min(10, int(payload.get("pages_per_query") or 1)))
    do_extract_contacts = bool(payload.get("extract_contacts", False))
    skip_tech_filter = bool(payload.get("skip_tech_filter", False))

    # Comma-separated keywords from UI ("salon, spa, beauty" → ["salon","spa","beauty"])
    raw_kw = payload.get("keywords") or ""
    keywords = [k.strip() for k in re.split(r"[,\n]", raw_kw) if k.strip()]

    use_browser = bool(payload.get("use_browser", True))

    # Selected tech stacks to KEEP (if empty → keep all detected stacks)
    selected_stacks = payload.get("tech_stacks") or []
    if isinstance(selected_stacks, str):
        selected_stacks = [s.strip() for s in selected_stacks.split(",") if s.strip()]

    verticals_q = (
        [vertical] if vertical in cfg["verticals"] else cfg["verticals"]
    )

    def update(**kw):
        with JOBS_LOCK:
            JOBS[job_id].update(kw)

    # Persist job intent
    _db.record_job(job_id, payload)

    update(
        status="running",
        phase="collect",
        progress=0,
        log=[],
        results=[],
        all_verified=[],
        rejected=[],
        collected=[],
        stop_requested=False,
    )
    log = JOBS[job_id]["log"]

    def push(msg: str):
        print(msg)
        log.append(msg)

    # ---------- cookies from local Chrome ----------
    push("Loading Google cookies from local Chrome profile…")
    g_cookies = get_chrome_cookies_for(".google.com")
    push(f"Loaded {len(list(g_cookies))} cookies from Chrome.")

    cdp_jar = cdp_attach_cookies(cfg["chrome_remote_port"], "google.com")
    if list(cdp_jar):
        push(f"CDP attached: {len(list(cdp_jar))} extra cookies via port {cfg['chrome_remote_port']}.")
        for c in cdp_jar:
            g_cookies.set(c.name, c.value, domain=c.domain, path=c.path)

    # ---------- PHASE 1: collect every URL ----------
    # Location tokens (don't slam them together — use them as flexible anchors)
    loc_parts = [x for x in (area, city, state, country) if x]
    # Prefer the most specific name for proximity phrases ("based in <city>")
    primary_loc = loc_parts[0] if loc_parts else ""
    broad_loc = " ".join(loc_parts).strip()

    # Build platform footprints — phrases that actually appear on those sites.
    stack_hints = {
        "Shopify":     ['site:myshopify.com', '"powered by shopify"', 'inurl:/products/', 'inurl:/collections/'],
        "WordPress":   ['"powered by wordpress"', 'inurl:wp-content', 'inurl:/?p='],
        "WooCommerce": ['"powered by woocommerce"', '"proudly powered by woocommerce"', 'inurl:/product/'],
        "Wix":         ['site:wixsite.com', '"site created with wix"'],
        "Squarespace": ['site:squarespace.com', '"powered by squarespace"'],
        "Webflow":     ['site:webflow.io', '"made in webflow"'],
        "GoHighLevel": ['site:msgsndr.com', 'site:leadconnectorhq.com', '"powered by gohighlevel"'],
        "BigCommerce": ['site:mybigcommerce.com', '"powered by bigcommerce"'],
        "Magento":     ['"powered by magento"'],
        "Ghost":       ['"powered by ghost"'],
        "ClickFunnels":['"powered by clickfunnels"'],
        "Kajabi":      ['"powered by kajabi"'],
        "Framer":      ['site:framer.website'],
    }
    hint_phrases: list[str] = []
    # When skip_tech_filter is on, IGNORE selected stacks for query-building
    # too — generate plain niche+location queries with no platform footprints.
    if not skip_tech_filter:
        for s in (selected_stacks or []):
            hint_phrases.extend(stack_hints.get(s, []))
    if not hint_phrases:
        hint_phrases = [""]

    queries: list[str] = []
    seen_q: set[str] = set()
    def add(q: str):
        q = " ".join(q.split())
        if q and q not in seen_q:
            seen_q.add(q); queries.append(q)

    # Junk-site exclusions used when skip_tech_filter is ON to push Google
    # away from social / marketplace / listing / wiki results.
    NOISE_EXCLUDE = (
        "-site:facebook.com -site:instagram.com -site:linkedin.com "
        "-site:twitter.com -site:x.com -site:youtube.com -site:tiktok.com "
        "-site:wikipedia.org -site:reddit.com -site:pinterest.com -site:quora.com -site:medium.com "
        "-site:amazon.com -site:amazon.in -site:flipkart.com -site:myntra.com "
        "-site:ajio.com -site:meesho.com -site:etsy.com -site:ebay.com "
        "-site:justdial.com -site:sulekha.com -site:indiamart.com -site:tradeindia.com "
        "-site:tripadvisor.com -site:yelp.com -site:trustpilot.com -site:bbb.org"
    )

    bases = keywords if keywords else verticals_q

    if skip_tech_filter:
        # ---------- Consolidated commerce queries — match the format:
        # ("kw1" OR "kw2" OR "kw3") "City" "State" -site:wiki -site:fb …
        # One Google fetch covers all keywords; far fewer queries needed.

        # Build the OR-block once: ("kw1" OR "kw2" OR "kw3")
        kw_quoted = [f'"{kw}"' for kw in bases]
        kw_or_block = "(" + " OR ".join(kw_quoted) + ")" if len(kw_quoted) > 1 else (kw_quoted[0] if kw_quoted else "")

        # Build the location block: "City" "State" (each quoted, space-separated)
        loc_tokens = []
        for x in (area, city, state, country):
            x = (x or "").strip()
            if x:
                loc_tokens.append(f'"{x}"')
        loc_block = " ".join(loc_tokens)

        # 1. PRIMARY — your exact target format
        add(f'{kw_or_block} {loc_block} {NOISE_EXCLUDE}')

        # 2. Commerce-page bias — adds intitle:/inurl: nudges toward shops
        if loc_block:
            add(f'{kw_or_block} {loc_block} (intitle:shop OR intitle:store OR intitle:brand) {NOISE_EXCLUDE}')
            add(f'{kw_or_block} {loc_block} ("shop now" OR "add to cart" OR "buy now") {NOISE_EXCLUDE}')
            add(f'{kw_or_block} {loc_block} "contact us" {NOISE_EXCLUDE}')

        # 3. Local-brand patterns — catches About / Footer "based in …" copy
        if primary_loc:
            add(f'{kw_or_block} "based in {primary_loc}" {NOISE_EXCLUDE}')
            add(f'{kw_or_block} "made in {primary_loc}" {NOISE_EXCLUDE}')
    else:
        # ---------- Tech-stack-aware queries (existing behavior) ----------
        # 1) keyword × stack-hint × location  (the core query)
        for kw in bases:
            for h in hint_phrases:
                if primary_loc:
                    add(f'{kw} "{primary_loc}" {h}')        # "Mumbai" anchored
                    add(f'{kw} {broad_loc} {h}')            # loose match
                else:
                    add(f'{kw} {h}')

        # 2) location-anchored phrases that real stores use
        if primary_loc:
            for h in hint_phrases:
                add(f'"based in {primary_loc}" {h}')
                add(f'"made in {primary_loc}" {h}')
                add(f'"ships from {primary_loc}" {h}')
                for kw in bases:
                    add(f'"{kw}" "based in {primary_loc}" {h}')

        # 3) raw site: search with location word (great for Shopify)
        if "Shopify" in (selected_stacks or []) and primary_loc:
            add(f'site:myshopify.com "{primary_loc}"')
            if broad_loc != primary_loc:
                add(f'site:myshopify.com "{broad_loc}"')

    # Boot local Chrome debugger if user opted in
    cdp_ready = False
    port = cfg.get("chrome_remote_port", 9222)
    if use_browser:
        push("Starting local Chrome with debugger…")
        if ensure_chrome_debugger(port, push=push):
            tabs = ensure_cdp_engine_tabs(port, push=push)
            cdp_ready = "google" in tabs
            if cdp_ready:
                push(f"[cdp] Google tab ready — searches will run through your local Chrome.")
                push("→ If Google shows a login/consent wall, complete it in that tab.")
            else:
                push(f"[cdp] could not open Google tab — falling back to HTTP")
        else:
            push("[chrome] debugger unavailable — falling back to HTTP")

    push(f"PHASE 1 — collecting URLs from {len(queries)} queries "
         f"({'BROWSER-CDP' if cdp_ready else 'HTTP'} · Google only)…")
    collected: list[str] = []
    seen_norm: set[str] = set()
    seen_hosts: set[str] = set()

    import random as _rng
    # Use slower delays when scraping contacts (politeness; fewer captchas)
    min_delay = float(cfg.get("min_delay_sec", 1.5)) * (2 if do_extract_contacts else 1)
    max_delay = float(cfg.get("max_delay_sec", 4.0)) * (2 if do_extract_contacts else 1)
    backoff = 0.0  # grows after each CAPTCHA / 429

    for qi, q in enumerate(queries):
        if JOBS[job_id].get("stop_requested"):
            push("⏹ Stop requested — ending Phase 1 early, jumping to filter step.")
            break

        # Jittered delay between queries to look human
        if qi > 0:
            delay = _rng.uniform(min_delay, max_delay) + backoff
            if delay > 0.1:
                push(f"  ⏳ waiting {delay:.1f}s before next query")
                # Sleep in 0.5s chunks so Stop reacts fast
                t_end = time.time() + delay
                while time.time() < t_end:
                    if JOBS[job_id].get("stop_requested"):
                        break
                    time.sleep(0.5)

        push(f"  query: {q}")
        all_urls: list[str] = []
        if cdp_ready:
            # Retry loop: if a CAPTCHA is detected, pause until user solves it,
            # then re-run THIS query from the beginning.
            while True:
                res = cdp_parallel_search(q, push=push, pages=pages_per_query)
                if res.get("captcha"):
                    push("    🛑 CAPTCHA / 429 — pausing for solve + adding backoff.")
                    backoff = min(30.0, (backoff or 2.0) * 2)  # exponential
                    update(status="paused-captcha", captcha=True)
                    if not _wait_for_captcha_clear(port, push=push, job=JOBS[job_id]):
                        push("    Resume signal received — retrying query.")
                    update(status="running", captcha=False)
                    continue  # retry the same query
                else:
                    backoff = max(0.0, backoff - 0.5)  # decay backoff on success
                all_urls.extend(res.get("urls", []))
                break
        else:
            # HTTP fallback — Google only, multi-page
            try:
                res = google_search(q, num=pages_per_query * 10, cookies=g_cookies)
                push(f"    [google-http] {len(res)} results across {pages_per_query} page(s)")
                all_urls.extend(res)
            except Exception as e:
                push(f"    [google-http] error: {e}")

        # dedupe (URL-normalised AND root-host) + PERSIST every new URL
        added = 0
        for u in _clean(all_urls):
            host = urlparse(u).netloc
            if u in seen_norm or host in seen_hosts:
                continue
            seen_norm.add(u)
            seen_hosts.add(host)
            collected.append(u)
            _db.record_url(job_id, u, host, q)        # ← disk-backed
            added += 1
        push(f"    + {added} new unique (total {len(collected)})")
        update(progress=int(30 * (qi + 1) / len(queries)), collected=list(collected))
    push(f"PHASE 1 done — {len(collected)} unique URLs collected.")

    # ---------- DIFF against previous similar run ----------
    prev_id = _db.find_previous_similar_job(payload, exclude_job_id=job_id)
    if prev_id:
        prev_hosts = _db.get_job_domains(prev_id)
        cur_hosts = {urlparse(u).netloc for u in collected}
        new_hosts = cur_hosts - prev_hosts
        push(f"📊 Diff vs previous run {prev_id[:14]}…: {len(new_hosts)} NEW domains "
             f"({len(prev_hosts)} were in the old run)")
        update(diff={"prev_job_id": prev_id, "new_domains": sorted(new_hosts),
                     "new_count": len(new_hosts), "prev_total": len(prev_hosts)})

    # ---------- PHASE 2: detect tech stack on EVERY collected URL ----------
    update(phase="verify")
    push(f"PHASE 2 — detecting tech stack on all {len(collected)} URLs…")
    all_verified: list[dict] = []
    classify_niche = bool(payload.get("classify_niche", True)) and bool(cfg.get("nvidia_api_key"))
    if payload.get("classify_niche") and not cfg.get("nvidia_api_key"):
        push("⚠ NVIDIA niche classification requested but no API key is set — "
             "skipping niche check, will filter by tech stack only.")
    nvidia_ok_count = 0
    nvidia_fail_count = 0

    for i, u in enumerate(collected):
        if JOBS[job_id].get("stop_requested"):
            push("⏹ Stop requested — ending Phase 2 early, filtering what we have.")
            break
        update(progress=30 + int(60 * i / max(1, len(collected))))
        det = detect_tech_stack(u, push=push)
        final_url = det["final_url"]
        platforms = det["platforms"]
        platform_str = ", ".join(platforms) if platforms else "Unknown"

        # NVIDIA niche classification (run whenever requested — works even if
        # the local fetch failed, since nvidia_classify fetches its own snippet)
        verdict = {"vertical": "Unknown", "confidence": 0.0, "reason": ""}
        if classify_niche:
            verdict = nvidia_classify_with_retry(final_url, cfg, custom_niche=custom_niche)
            if verdict.get("vertical") and verdict["vertical"] not in ("Unknown", ""):
                nvidia_ok_count += 1
            else:
                nvidia_fail_count += 1

        # Contact extraction (homepage + /contact + /about)
        contacts = {"emails": [], "phones": [], "socials": {}}
        if do_extract_contacts:
            try:
                contacts = extract_contacts(final_url)
            except Exception as ce:
                push(f"    [contacts] {root_domain(final_url)} extraction failed: {ce}")
            # Politeness pause between heavy fetches
            time.sleep(_rng.uniform(min_delay * 0.5, min_delay))

        row = {
            "domain": root_domain(final_url),
            "url": final_url,
            "platforms": platforms,
            "platform_str": platform_str,
            "evidence": det.get("evidence", {}),
            "vertical": verdict["vertical"],
            "confidence": verdict["confidence"],
            "reason": verdict["reason"],
            "emails": contacts.get("emails") or [],
            "phones": contacts.get("phones") or [],
            "socials": contacts.get("socials") or {},
        }
        all_verified.append(row)
        _db.record_verification(job_id, row)          # ← disk-backed
        update(all_verified=list(all_verified))
        contact_str = ""
        if do_extract_contacts:
            n_email = len(row["emails"])
            n_phone = len(row["phones"])
            n_social = sum(len(v) for v in row["socials"].values())
            contact_str = f" · 📧{n_email} 📞{n_phone} 🔗{n_social}"
        push(f"  [{i+1}/{len(collected)}] {row['domain']} → "
             f"stack: {platform_str}"
             + (f" · niche: {verdict['vertical']} ({verdict['confidence']:.2f})" if classify_niche else "")
             + contact_str)
    push(f"PHASE 2 done — {len(all_verified)} URLs analysed.")

    # ---------- AUTO-SWEEP: re-classify Unknowns if NVIDIA is now reachable ----------
    if classify_niche:
        unknowns = [r for r in all_verified if (r.get("vertical") or "Unknown") in ("", "Unknown")]
        if unknowns and _nvidia_reachable(cfg):
            push(f"🔁 Auto-sweep: {len(unknowns)} Unknown niches — retrying with NVIDIA…")
            for ui, r in enumerate(unknowns):
                if JOBS[job_id].get("stop_requested"):
                    break
                v = nvidia_classify_with_retry(r["url"], cfg, custom_niche=custom_niche)
                r["vertical"] = v["vertical"]
                r["confidence"] = v["confidence"]
                r["reason"] = v["reason"]
                _db.record_verification(job_id, r)
                if v.get("vertical") and v["vertical"] not in ("Unknown", ""):
                    nvidia_ok_count += 1
                    nvidia_fail_count = max(0, nvidia_fail_count - 1)
            JOBS[job_id]["all_verified"] = list(all_verified)
            still_unknown = sum(1 for r in all_verified if (r.get("vertical") or "Unknown") in ("", "Unknown"))
            push(f"🔁 Auto-sweep done · {len(unknowns) - still_unknown} resolved · "
                 f"{still_unknown} still Unknown.")

    # ---------- PHASE 3: filter ----------
    update(phase="filter")
    if selected_stacks:
        push(f"PHASE 3 — filtering to tech stacks: {', '.join(selected_stacks)}")
    else:
        push("PHASE 3 — no tech-stack filter set; keeping any detected platform")
    niche_filter = custom_niche if custom_niche else (vertical if vertical != "all" else "")

    # Smart fallback: if NVIDIA failed for everything (or no successes), drop the niche
    # filter so the user still gets the tech-stack matches instead of an empty list.
    apply_niche = bool(classify_niche and niche_filter)
    if apply_niche:
        if nvidia_ok_count == 0 and (nvidia_fail_count > 0 or len(all_verified) > 0):
            push(f"⚠ NVIDIA produced 0 usable classifications ({nvidia_fail_count} failed) — "
                 f"applying tech-stack-only filter so you still get results.")
            apply_niche = False
        elif nvidia_fail_count > nvidia_ok_count * 3 and nvidia_fail_count > 5:
            push(f"⚠ NVIDIA failed on {nvidia_fail_count}/{nvidia_ok_count + nvidia_fail_count} URLs — "
                 f"niche filter will still be applied to the {nvidia_ok_count} successful ones.")
    if apply_niche:
        push(f"            and niche match: {niche_filter}")
    elif classify_niche and niche_filter:
        # filter was supposed to apply but we're skipping it — mark in job state
        update(niche_fallback=True)

    def keep(r: dict) -> bool:
        if not skip_tech_filter:
            if selected_stacks:
                if not any(p in selected_stacks for p in r["platforms"]):
                    return False
            else:
                if not r["platforms"]:
                    return False
        if apply_niche:
            if r["vertical"].strip().lower() != niche_filter.strip().lower():
                return False
        return True

    results = [r for r in all_verified if keep(r)]
    rejected = [r for r in all_verified if not keep(r)]
    for r in results:
        r["accepted"] = True
        _db.record_verification(job_id, r)
    for r in rejected:
        r["accepted"] = False
        _db.record_verification(job_id, r)
    final_status = "stopped" if JOBS[job_id].get("stop_requested") else "done"
    _db.update_job_status(job_id, final_status, ended=True)
    update(results=results, rejected=rejected, progress=100, status=final_status, phase=final_status)
    push(f"{'Stopped' if final_status=='stopped' else 'Done'}. "
         f"Collected {len(collected)} · analysed {len(all_verified)} · "
         f"matched {len(results)} · rejected {len(rejected)}. Saved to scraper.db.")


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
CORS(app)


@app.route("/")
def index():
    return send_from_directory(resource_path("."), "index.html")


@app.route("/api/models")
def api_models():
    """Try to fetch the LIVE model list from NVIDIA Build so the dropdown is
    never out of date. Falls back to the curated list if no key / API down."""
    cfg = load_config()
    key = cfg.get("nvidia_api_key", "")
    live: list[str] = []
    if key:
        try:
            r = requests.get(
                f"{cfg.get('nvidia_base_url','https://integrate.api.nvidia.com/v1').rstrip('/')}/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=6,
            )
            if r.status_code == 200:
                data = r.json().get("data") or []
                # Rank: fast small models first (they classify niches in ~1s),
                # then mid-size, then large reasoning, then heavy.
                priority = (
                    "nano-8b", "phi-3", "mistral-7b", "qwen2.5-7b",   # fastest tier
                    "8b-instruct",
                    "nemotron-super-49b", "nemotron-51b", "mistral-nemotron",
                    "qwq-32b", "distill-qwen-32b",
                    "70b-instruct", "distill-llama-70b",
                    "deepseek-v3", "deepseek-r1",
                    "nemotron-ultra", "405b",
                )
                def rank(m):
                    mid = m.get("id", "").lower()
                    for i, p in enumerate(priority):
                        if p in mid:
                            return i
                    return 99
                models = sorted(data, key=rank)
                live = [m["id"] for m in models if m.get("id")]
        except Exception as e:
            print(f"[models] live fetch failed: {e}", file=sys.stderr)
    if not live:
        live = list(NVIDIA_REASONING_MODELS)
    return jsonify({"models": live, "live": bool(live and key)})


@app.route("/api/tech-stacks")
def api_tech_stacks():
    return jsonify({"stacks": ALL_TECH_STACKS})


_NVIDIA_STATUS_CACHE = {"ts": 0.0, "result": None}


@app.route("/api/nvidia-status")
def api_nvidia_status():
    """End-to-end NVIDIA Build check:
       1. Auth via GET /models  → must be 200
       2. Real 1-token chat completion using the CONFIGURED model
       'Connected' is only true if step 2 succeeds.  Cached 30s to limit traffic."""
    cfg = load_config()
    key = cfg.get("nvidia_api_key", "")
    if not key:
        return jsonify({"connected": False, "reason": "no API key", "step": "key"})

    cache_key = f"{key[:8]}|{cfg.get('nvidia_model','')}"
    now = time.time()
    # Successful probes stay valid for 5 minutes; failures re-probe after 30s
    cached = _NVIDIA_STATUS_CACHE.get("result")
    ttl = 300 if (cached and cached.get("connected")) else 30
    if _NVIDIA_STATUS_CACHE.get("key") == cache_key and now - _NVIDIA_STATUS_CACHE["ts"] < ttl:
        return jsonify(_NVIDIA_STATUS_CACHE["result"])

    base = cfg.get("nvidia_base_url", "https://integrate.api.nvidia.com/v1").rstrip("/")
    model = cfg.get("nvidia_model", "")

    # Fast auth-only check (no chat completion — that endpoint cold-starts slowly)
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=6)
        if r.status_code in (401, 403):
            out = {"connected": False, "reason": f"invalid API key ({r.status_code})", "step": "auth"}
        elif r.status_code != 200:
            out = {"connected": False, "reason": f"/models HTTP {r.status_code}", "step": "auth"}
        else:
            out = {"connected": True, "model": model, "step": "auth"}
    except requests.exceptions.Timeout:
        out = {"connected": False, "reason": "timeout reaching /models", "step": "auth"}
    except Exception as e:
        out = {"connected": False, "reason": f"network: {str(e)[:120]}", "step": "auth"}

    _NVIDIA_STATUS_CACHE.update({"ts": now, "key": cache_key, "result": out})
    return jsonify(out)


@app.route("/api/nvidia-test-all", methods=["POST"])
def api_nvidia_test_all():
    """Probe every model in the live catalog with a 1-token completion.
    Returns a list of {model, ok, elapsed_ms, reason}."""
    cfg = load_config()
    key = cfg.get("nvidia_api_key", "")
    if not key or not OpenAI:
        return jsonify({"ok": False, "error": "no API key or openai SDK missing"})
    base = cfg.get("nvidia_base_url", "https://integrate.api.nvidia.com/v1").rstrip("/")
    # Pull the live catalog
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except Exception as e:
        return jsonify({"ok": False, "error": f"could not list models: {e}"})

    client = OpenAI(api_key=key, base_url=base, timeout=20.0)
    results = []
    for mid in ids:
        t0 = time.time()
        try:
            stream = client.chat.completions.create(
                model=mid,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1, temperature=0, stream=True, timeout=20.0,
            )
            for _ in stream: break
            try: stream.response.close()
            except Exception: pass
            results.append({
                "model": mid, "ok": True,
                "elapsed_ms": int((time.time() - t0) * 1000),
            })
        except Exception as e:
            msg = str(e)
            if "404" in msg: short = "404 not on this account"
            elif "401" in msg or "403" in msg: short = "auth denied"
            elif "429" in msg: short = "429 rate-limited"
            elif "timeout" in msg.lower(): short = ">20s timeout"
            else: short = msg[:80]
            results.append({
                "model": mid, "ok": False, "reason": short,
                "elapsed_ms": int((time.time() - t0) * 1000),
            })
    return jsonify({"ok": True, "results": results})


@app.route("/api/nvidia-test-model", methods=["POST"])
def api_nvidia_test_model():
    """Slow but thorough: actually run a 1-token chat completion with the
    configured model. Use this when the user clicks 'Test model'."""
    cfg = load_config()
    key = cfg.get("nvidia_api_key", "")
    if not key:
        return jsonify({"ok": False, "reason": "no API key"})
    if not OpenAI:
        return jsonify({"ok": False, "reason": "openai library missing"})
    base = cfg.get("nvidia_base_url", "https://integrate.api.nvidia.com/v1").rstrip("/")
    model = cfg.get("nvidia_model", "")
    t0 = time.time()
    try:
        # No upper timeout — let cold-starts take as long as they take.
        client = OpenAI(api_key=key, base_url=base, timeout=None)
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
            temperature=0,
            stream=True,
        )
        first_chunk_ms = None
        for _chunk in stream:
            first_chunk_ms = int((time.time() - t0) * 1000)
            break
        try: stream.response.close()
        except Exception: pass
        return jsonify({
            "ok": True, "model": model,
            "elapsed_ms": first_chunk_ms or int((time.time() - t0) * 1000),
            "streamed": True,
        })
    except Exception as e:
        msg = str(e)
        if "404" in msg:
            short = f"model '{model}' returned 404 — pick a different model"
        elif "401" in msg or "403" in msg:
            short = "invalid API key for chat endpoint"
        elif "429" in msg:
            short = "rate-limited on chat endpoint (429)"
        elif "timeout" in msg.lower():
            short = f"timeout after {int(time.time() - t0)}s — model is cold or slow"
        else:
            short = msg[:200]
        return jsonify({"ok": False, "reason": short, "model": model,
                        "elapsed_ms": int((time.time() - t0) * 1000)})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    cfg = load_config()
    if request.method == "POST":
        data = request.get_json(force=True)
        for k in ("nvidia_api_key", "nvidia_base_url", "nvidia_model", "chrome_remote_port"):
            if k in data and data[k] != "":
                cfg[k] = data[k]
        if "nvidia_reasoning" in data:
            cfg["nvidia_reasoning"] = bool(data["nvidia_reasoning"])
        save_config(cfg)
        _NVIDIA_STATUS_CACHE.clear()  # force fresh probe next status request


    safe = dict(cfg)
    if safe.get("nvidia_api_key"):
        safe["nvidia_api_key"] = safe["nvidia_api_key"][:6] + "…"
    return jsonify(safe)


@app.route("/api/disconnect-nvidia", methods=["POST"])
def api_disconnect_nvidia():
    cfg = load_config()
    cfg["nvidia_api_key"] = ""
    save_config(cfg)
    _NVIDIA_STATUS_CACHE.clear()
    return jsonify({"ok": True})


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    cfg = load_config()
    data = request.get_json(force=True)
    job_id = f"job_{int(time.time()*1000)}"
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0, "log": [], "results": []}
    threading.Thread(target=run_scrape_job, args=(job_id, data, cfg), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        return jsonify(JOBS.get(job_id, {"status": "unknown"}))


@app.route("/api/job/<job_id>/resume-captcha", methods=["POST"])
def api_resume_captcha(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"ok": False, "error": "no such job"}), 404
        j["captcha_resume"] = True
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/stop", methods=["POST"])
def api_stop_job(job_id):
    """Politely ask the worker to stop. It will still run the filter step
    on whatever it has and persist results."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"ok": False, "error": "no such job"}), 404
        j["stop_requested"] = True
        j.setdefault("log", []).append("⏹ Stop requested — finishing up.")
    return jsonify({"ok": True})


@app.route("/api/jobs")
def api_jobs_list():
    return jsonify({"jobs": _db.list_jobs(limit=50)})


@app.route("/api/jobs/<job_id>/results")
def api_jobs_results(job_id):
    return jsonify({
        "urls": _db.get_job_urls(job_id),
        "verifications": _db.get_job_verifications(job_id),
    })


@app.route("/api/jobs/<job_id>/csv")
def api_jobs_csv(job_id):
    rows = _db.get_job_verifications(job_id, accepted_only=request.args.get("accepted") == "1")
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["domain", "url", "tech_stack", "niche", "confidence", "accepted", "reason"])
    for r in rows:
        w.writerow([
            r.get("domain", ""), r.get("url", ""),
            "|".join(r.get("platforms") or []),
            r.get("vertical", ""), r.get("confidence", ""),
            "yes" if r.get("accepted") else "no",
            (r.get("reason") or "")[:300],
        ])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="job_{job_id}.csv"'})


@app.route("/api/jobs/<job_id>/outreach-csv")
def api_jobs_outreach_csv(job_id):
    """CSV optimized for outreach: domain, niche, emails, phones, social handles."""
    rows = _db.get_job_verifications(job_id, accepted_only=request.args.get("accepted") == "1")
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "domain", "url", "niche", "confidence", "tech_stack",
        "emails", "phones",
        "instagram", "facebook", "twitter", "linkedin",
        "youtube", "tiktok", "whatsapp", "pinterest",
    ])
    for r in rows:
        s = r.get("socials") or {}
        w.writerow([
            r.get("domain", ""), r.get("url", ""),
            r.get("vertical", ""), r.get("confidence", ""),
            "|".join(r.get("platforms") or []),
            "; ".join(r.get("emails") or []),
            "; ".join(r.get("phones") or []),
            "; ".join(s.get("instagram") or []),
            "; ".join(s.get("facebook") or []),
            "; ".join(s.get("twitter") or []),
            "; ".join(s.get("linkedin") or []),
            "; ".join(s.get("youtube") or []),
            "; ".join(s.get("tiktok") or []),
            "; ".join(s.get("whatsapp") or []),
            "; ".join(s.get("pinterest") or []),
        ])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="outreach_{job_id}.csv"'})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_jobs_delete(job_id):
    _db.delete_job(job_id)
    return jsonify({"ok": True})


@app.route("/api/bulk-verify", methods=["POST"])
def api_bulk_verify():
    """Accept a list of URLs and run Phase 2 + 3 on them (no Google search)."""
    data = request.get_json(force=True)
    urls = data.get("urls") or []
    if isinstance(urls, str):
        urls = [u.strip() for u in re.split(r"[\s,]+", urls) if u.strip()]
    urls = [u if "://" in u else "https://" + u for u in urls]
    if not urls:
        return jsonify({"ok": False, "error": "no urls provided"}), 400
    # Filter out .gov / .edu / .org / .mil / .ac hosts before they touch the pipeline
    filtered_urls = []
    skipped_tld = []
    for u in urls:
        host = urlparse(u).netloc.lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if _is_excluded_tld(host):
            skipped_tld.append(host)
        else:
            filtered_urls.append(u)
    urls = filtered_urls

    job_id = f"bulk_{int(time.time()*1000)}"
    cfg_now = load_config()
    # Bulk verify: ALWAYS classify with NVIDIA when a key is set — the user's
    # whole intent is to learn the niche of these URLs.
    classify_for_bulk = bool(cfg_now.get("nvidia_api_key"))
    # Bulk verify defaults to extracting contacts (its main use-case is outreach).
    extract_for_bulk = bool(data.get("extract_contacts", True))
    params = {
        "keywords": "",
        "custom_niche": (data.get("custom_niche") or "").strip(),
        "tech_stacks": data.get("tech_stacks") or [],
        "classify_niche": classify_for_bulk,
        "extract_contacts": extract_for_bulk,
        "vertical": "all",
        "country": "", "state": "", "city": "", "area": "",
        "_bulk": True,
    }
    _db.record_job(job_id, params)
    for u in urls:
        n = normalize_url(u)
        host = urlparse(n).netloc
        if host:
            _db.record_url(job_id, n, host, "bulk-verify")
    cfg = load_config()
    threading.Thread(target=run_continue_job, args=(job_id, cfg), daemon=True).start()
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "queued": len(urls),
        "skipped_tld_count": len(skipped_tld),
        "skipped_tld_sample": skipped_tld[:5],
    })


@app.route("/api/jobs/<job_id>/continue", methods=["POST"])
def api_jobs_continue(job_id):
    """Resume a stopped/error/interrupted job: skip Phase 1, run Phase 2+3
    on unverified URLs (and reclassify Unknown niches)."""
    # If the row is stuck on 'running' but we have no live worker for it,
    # it's a zombie — force it to 'interrupted' before resuming.
    with JOBS_LOCK:
        active = job_id in JOBS and JOBS[job_id].get("status") == "running"
    if not active:
        try: _db.force_mark_status(job_id, "interrupted")
        except Exception: pass
    cfg = load_config()
    threading.Thread(target=run_continue_job, args=(job_id, cfg), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/jobs/<job_id>/extract-contacts", methods=["POST"])
def api_jobs_extract_contacts(job_id):
    """Run contact extraction on every URL in a job that doesn't yet have contacts.
    Returns immediately; the heavy work runs in a background thread."""
    rows = _db.list_jobs(1000)
    job = next((j for j in rows if j["id"] == job_id), None)
    if not job:
        return jsonify({"ok": False, "error": "no such job"}), 404

    # Mark intent on the job so future Continue calls also extract
    cfg = load_config()

    def worker():
        verifs = _db.get_job_verifications(job_id)
        targets = [v for v in verifs
                   if v.get("url") and not (v.get("emails") or v.get("phones") or v.get("socials"))]
        if not targets:
            with JOBS_LOCK:
                JOBS.setdefault(job_id, {}).setdefault("log", []).append(
                    "📇 Contact extraction: nothing to backfill — every row already has contacts.")
            return
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "running", "phase": "extract", "progress": 0,
                "log": [f"📇 Extracting contacts on {len(targets)} rows…"],
                "results": [], "all_verified": list(verifs), "rejected": [],
                "collected": [v["url"] for v in verifs],
                "stop_requested": False,
            }
        _db.update_job_status(job_id, "running")
        for i, v in enumerate(targets):
            if JOBS[job_id].get("stop_requested"):
                break
            try:
                contacts = extract_contacts(v["url"])
            except Exception as ce:
                contacts = {"emails": [], "phones": [], "socials": {}}
                JOBS[job_id]["log"].append(f"    [contacts] {v.get('domain','')} failed: {ce}")
            for r in verifs:
                if r.get("url") == v.get("url"):
                    r["emails"] = contacts.get("emails") or []
                    r["phones"] = contacts.get("phones") or []
                    r["socials"] = contacts.get("socials") or {}
                    _db.record_verification(job_id, r)
                    break
            JOBS[job_id]["all_verified"] = list(verifs)
            JOBS[job_id]["progress"] = int(100 * (i + 1) / len(targets))
            n_e, n_p = len(contacts.get("emails") or []), len(contacts.get("phones") or [])
            n_s = sum(len(x) for x in (contacts.get("socials") or {}).values())
            JOBS[job_id]["log"].append(
                f"  [{i+1}/{len(targets)}] {v.get('domain','')} → 📧{n_e} 📞{n_p} 🔗{n_s}")
        final_status = "stopped" if JOBS[job_id].get("stop_requested") else "done"
        _db.update_job_status(job_id, final_status, ended=True)
        JOBS[job_id]["status"] = final_status
        JOBS[job_id]["phase"] = final_status
        JOBS[job_id]["progress"] = 100
        JOBS[job_id]["log"].append(
            f"📇 Contact extraction {final_status}.")

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/jobs/<job_id>/extract-one", methods=["POST"])
def api_jobs_extract_one(job_id):
    """Run contact extraction for a SINGLE url. Returns the result inline."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url required"}), 400
    try:
        contacts = extract_contacts(url)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    # Update the existing row, if any
    existing = _db.get_job_verifications(job_id)
    base = next((r for r in existing if r.get("url") == url), None)
    if base:
        base["emails"] = contacts.get("emails") or []
        base["phones"] = contacts.get("phones") or []
        base["socials"] = contacts.get("socials") or {}
        _db.record_verification(job_id, base)
    return jsonify({"ok": True, "contacts": contacts})


@app.route("/api/jobs/<job_id>/reclassify-one", methods=["POST"])
def api_jobs_reclassify_one(job_id):
    """Re-classify a single URL within a job. Returns the new verdict."""
    cfg = load_config()
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url required"}), 400

    # Find original niche/params for this job
    rows = _db.list_jobs(1000)
    job = next((j for j in rows if j["id"] == job_id), None)
    params = (job or {}).get("params", {}) or {}
    custom_niche = (params.get("custom_niche") or "").strip()

    verdict = nvidia_classify_with_retry(url, cfg, custom_niche=custom_niche)
    # Find the existing row to preserve platforms/etc., then update
    existing = _db.get_job_verifications(job_id)
    base = next((r for r in existing if r.get("url") == url), None)
    row = {
        "url": url,
        "domain": (base or {}).get("domain") or root_domain(url),
        "platforms": (base or {}).get("platforms") or [],
        "vertical": verdict["vertical"],
        "confidence": verdict["confidence"],
        "reason": verdict["reason"],
        "accepted": (base or {}).get("accepted", False),
    }
    _db.record_verification(job_id, row)
    return jsonify({"ok": True, "row": row})


@app.route("/api/jobs/<job_id>/force-stop", methods=["POST"])
def api_jobs_force_stop(job_id):
    """Mark a zombie job as 'interrupted' so the Continue button becomes available."""
    _db.force_mark_status(job_id, "interrupted")
    return jsonify({"ok": True})


@app.route("/api/open-engine-tabs", methods=["POST"])
def api_open_engine_tabs():
    cfg = load_config()
    port = cfg.get("chrome_remote_port", 9222)
    if not ensure_chrome_debugger(port):
        return jsonify({"ok": False, "error": "could not start Chrome debugger"}), 500
    tabs = ensure_cdp_engine_tabs(port)
    return jsonify({"ok": True, "tabs": {e: {"id": t["id"], "url": t.get("url")} for e, t in tabs.items()}})


@app.route("/api/browser-status")
def api_browser_status():
    cfg = load_config()
    port = cfg.get("chrome_remote_port", 9222)
    return jsonify({
        "port": port,
        "alive": _debugger_alive(port),
        "chrome_exe": _find_chrome_exe(),
        "selenium_available": webdriver is not None,
    })


@app.route("/api/verify", methods=["POST"])
def api_verify():
    """Verify a single user-supplied domain via NVIDIA."""
    cfg = load_config()
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    if "://" not in url:
        url = "https://" + url
    det = detect_tech_stack(url)
    custom_niche = (data.get("custom_niche") or "").strip()
    verdict = nvidia_classify(det["final_url"], cfg, custom_niche=custom_niche) if det["ok"] else {
        "vertical": "Unknown", "confidence": 0.0, "reason": "fetch failed"
    }
    return jsonify(
        {
            "url": det["final_url"],
            "domain": root_domain(det["final_url"]),
            "platforms": det["platforms"],
            "platform_str": ", ".join(det["platforms"]) or "Unknown",
            "evidence": det["evidence"],
            "vertical": verdict["vertical"],
            "confidence": verdict["confidence"],
            "reason": verdict["reason"],
        }
    )


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def free_port(default: int = 5000) -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", default))
        s.close()
        return default
    except OSError:
        s.close()
        s2 = socket.socket()
        s2.bind(("127.0.0.1", 0))
        p = s2.getsockname()[1]
        s2.close()
        return p


def main():
    _db.init_db(APP_DIR)
    orphans = _db.recover_orphans()
    if orphans:
        print(f"[recovery] marked {orphans} orphaned 'running' job(s) as 'interrupted'")
    port = free_port(5000)
    url = f"http://127.0.0.1:{port}"
    print(f"Shopify Vertical Scraper running at {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
