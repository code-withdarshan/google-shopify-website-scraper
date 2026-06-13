# Google → Shopify / Tech-Stack Vertical Scraper

A local Windows tool that discovers e-commerce stores by **niche keyword + location**, fingerprints their **tech stack**, and uses **NVIDIA Build's reasoning LLMs** to verify each site against a free-text niche description. Results are persisted to SQLite and exportable as CSV or pasted directly into Google Sheets.

It drives your real local **Google Chrome** browser over the Chrome DevTools Protocol — so Google search happens in a logged-in tab with full JavaScript rendering, bypassing the captcha walls that hit plain `requests` scrapers.

---

## Features

**Discovery**
- Google search through a real Chrome tab (DevTools Protocol, persistent session)
- Niche keyword input (comma-separated, e.g. `engagement rings, gold jewelry`)
- Location filter (country / state / city / area)
- Configurable Google **pages per query** (1–10)
- Auto-pause on **CAPTCHA** / **429** with a Resume button — you solve in the tab, the scraper waits and continues
- Random inter-query delays + exponential backoff
- Optional Chrome proxy (SOCKS5 / HTTP)

**Tech-stack fingerprinting**
20 platforms detected via HTML markers + response headers:
Shopify · WordPress · WooCommerce · Wix · Webflow · Squarespace · GoHighLevel · BigCommerce · Magento · Drupal · Joomla · Ghost · Framer · Duda · Weebly · ClickFunnels · Kajabi · Cargo · Next.js · React

**Niche verification with NVIDIA Build**
- Free-text custom niche ("vegan skincare", "dental clinic", "jewelry store") OR built-in Jewelry / Fashion / Retail taxonomy
- Reasoning models supported (DeepSeek R1, Nemotron Ultra, Nemotron Super, QwQ, etc.)
- Per-URL retry on transient failures (429, timeout, 5xx)
- Auto-sweep at end-of-run for any rows still classified `Unknown`
- Single-row **↻ Re-classify** button for spot fixes
- Live model probing (**Test ALL models** button) to find which models your free tier actually allows

**Data persistence (SQLite)**
- Every URL is written to `scraper.db` the instant it's collected
- Verifications stored as they happen — crashes, Stop clicks, closed Chrome tabs never lose data
- **Past runs** card lists every job with timestamp, status, full filter params (keywords / niche / stacks / location / pages)
- **▶ Continue / Recover** button on any unfinished or zombie job
- **↻ Re-classify N Unknown** for cleaning up runs where NVIDIA was down
- One-click **All CSV** / **Matched CSV** download from any historical run

**URL filtering**
- Strict noise blocklist (social, listing sites, marketplaces, CDNs, file hosts — 50+ domains)
- File extension blocking (PDF, DOC, ZIP, images, video, etc.)
- `.gov` / `.edu` / `.org` / `.mil` / `.ac` domains auto-skipped (incl. country variants like `.gov.in`, `.edu.au`)
- URL canonicalization (lowercase host, `www.` stripped, tracking params removed, one row per root domain)

**Workflow extras**
- **Bulk verify** — paste a list of URLs, skip Google search, run Phase 2 + 3 only (with its own niche field)
- **Diff vs previous run** — automatically detects the most recent run with the same params and shows "N new domains since last time"
- **Copy as TSV** — one-click clipboard copy that pastes directly into Google Sheets / Excel
- **CSV filename auto-generated** from your search params (e.g. `jewelry-engagement-rings_mumbai_shopify_2026-06-11.csv`)

---

## Requirements

- Windows 10 / 11 (64-bit)
- Python 3.11 + (only for running from source — the `.exe` build needs nothing)
- Google Chrome installed at the standard path
- A free **NVIDIA Build** API key from <https://build.nvidia.com>

---

## Quick start (from source)

```powershell
cd C:\path\to\google-shopify-website-scraper
py -m pip install -r requirements.txt
py backend.py
```

The browser opens automatically to `http://127.0.0.1:5000`.

### First-time setup in the UI

1. **NVIDIA Build API** card → paste your API key → click **Save**
2. Click **Test ALL models** → in the result table, click **Use** next to any green-pilled working model (recommended: `nvidia/llama-3.1-nemotron-51b-instruct`)
3. **Open Google search tab** button → a separate Chrome window opens with `google.com` — complete any consent prompt once
4. Fill the form:
   - Niche keywords (comma-separated)
   - Niche to verify (free text)
   - Country / State / City / Area
   - Tech stacks to keep (Shopify pre-checked)
   - Google pages per query (1–10)
5. Click **Start scraping**

Watch the three-phase pipeline live: **collect → verify → filter**.

---

## Build a single `.exe` for distribution

```powershell
.\build.bat
```

Output: `release\ShopifyVerticalScraper.exe` + `ShopifyVerticalScraper.zip` ready to send to other Windows users. They double-click the exe — no Python, no dependencies, no installer.

End-user instructions live in `README_END_USER.md` (bundled into the release zip).

---

## How it works

```
                         ┌─────────────────────────────────────┐
                         │  Flask backend  (backend.py)        │
                         │  http://127.0.0.1:5000              │
                         └─────────────────────────────────────┘
                                       │
        ┌──────────────┬───────────────┼──────────────┬──────────────┐
        ▼              ▼               ▼              ▼              ▼
  index.html       SQLite DB     Chrome DevTools   NVIDIA Build    requests
  (single-page    (scraper.db)    Protocol        OpenAI-compat.   (HTTP)
   UI, no build)                  (CDP over WS)    chat completion
                                       │
                                       ▼
                                 Local Chrome
                                 (3-tab debugger profile)
                                 google.com / bing.com / duckduckgo.com
                                 (Google only by default)
```

### Pipeline phases

**Phase 1 — Collect**
- For each keyword × stack hint × location anchor, builds a Google query
- Each query is fired through the persistent Chrome tab over CDP
- Anchor hrefs are extracted via `Runtime.evaluate`
- URLs canonicalized + deduplicated by root host
- Noise / blocklist filters applied
- Every survivor is `INSERT`ed into `scraper.db` immediately
- Stops paginating a query early on Google "did not match any documents"
- Auto-pauses on CAPTCHA / 429 (auto-resume when solved or manual button)

**Phase 2 — Verify**
- Each URL is fetched with 3 progressively friendlier UA / header sets (Chrome with Sec-CH-UA → Safari → Googlebot)
- HTML is fingerprinted against 20 tech-stack signatures
- Page text (with `<script>`, `<style>`, etc. stripped) + page title + URL tokens are sent to NVIDIA Build
- Reasoning models: chain-of-thought enabled where supported
- Per-URL retry on transient errors (429, timeout, 5xx, empty response)
- End-of-phase auto-sweep retries any rows still classified `Unknown`
- Each verification is `INSERT OR REPLACE`d into the DB as it happens

**Phase 3 — Filter**
- Tech-stack filter: keep rows where any detected platform is in your selected list (or any detected stack if none selected)
- Niche filter: keep rows where `vertical == your custom niche` (case-insensitive)
- Smart fallback: if NVIDIA produced 0 usable classifications, the niche filter is auto-skipped so you still get a tech-stack-filtered list

---

## Configuration

`config.json` (next to the exe / `backend.py`):

| Key | Purpose | Default |
|---|---|---|
| `nvidia_api_key` | Bearer token for NVIDIA Build | empty |
| `nvidia_base_url` | OpenAI-compat. base URL | `https://integrate.api.nvidia.com/v1` |
| `nvidia_model` | Active reasoning model id | `deepseek-ai/deepseek-r1` |
| `nvidia_reasoning` | Enable `chat_template_kwargs.thinking` for Nemotron models | `true` |
| `chrome_remote_port` | CDP port for the scraper's Chrome | `9222` |
| `proxy_url` | Chrome proxy (`socks5://…` or `http://user:pass@host:port`) | empty |
| `min_delay_sec` / `max_delay_sec` | Jittered delay between Google queries | `1.5` / `4.0` |
| `verticals` | Built-in taxonomy when no custom niche set | `Jewelry, Fashion, Retail` |

Everything except `verticals` is editable from the UI.

---

## SQLite schema (`scraper.db`)

| Table | Columns | Purpose |
|---|---|---|
| `jobs` | `id, created_at, status, params_json, ended_at` | One row per scrape / bulk-verify / continue |
| `urls` | `id, job_id, url, host, query, found_at` | Every URL collected in Phase 1 |
| `verifications` | `id, job_id, url, domain, platforms, vertical, confidence, reason, accepted, verified_at` | Every Phase 2 result |

Indexed by `job_id` for fast Past-Run queries. WAL journal mode is enabled for concurrent reads while writes happen.

---

## Project structure

```
google-shopify-website-scraper/
├── backend.py             # Flask server + scraper + CDP driver + NVIDIA client
├── db.py                  # SQLite layer
├── index.html             # Single-page UI (vanilla JS, no build step)
├── config.json            # User config (API key, model, proxy, delays)
├── requirements.txt       # Python deps
├── build.bat              # PyInstaller --onefile build script
├── README.md              # This file
├── README_END_USER.md     # Bundled into the release zip
├── scraper.db             # Auto-created on first run (persistent storage)
├── .chrome-debug-profile/ # Isolated Chrome profile (never touches your real one)
├── dist/                  # PyInstaller raw exe
├── build/                 # PyInstaller intermediate
└── release/               # Final exe + readme + zip
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pyinstaller : not recognized` | Wrong shell or not installed | `py -m pip install pyinstaller` then `py -m PyInstaller …` |
| `python : not recognized` (Windows Store stub appears) | Microsoft Store alias intercepting | Settings → Apps → Advanced app settings → App execution aliases → turn OFF `python.exe` and `python3.exe`, OR use `py` instead of `python` |
| Status pill stuck on "Checking…" | Backend hasn't been restarted since route was added | Ctrl+C and re-run `py backend.py` |
| `model returned 404` | That model id is no longer on your NVIDIA tier | Click **Test ALL models**, then **Use** next to any working green-pilled model |
| All niches come back `Unknown` | NVIDIA model deprecated / wrong id / cold-starting | Run **Test ALL models** and switch to one that responds |
| Google search returns 0 results | CAPTCHA / login wall in the scraper's Chrome tab | Switch to that Chrome window, solve the challenge, click **Resume** |
| Past-Run row stuck on `running` after backend crash | Zombie job — process died before flipping status | Restart backend (auto-marks orphans as `interrupted`) → row gets **▶ Recover** button |
| Browser doesn't auto-open | Pop-up blocker / wrong default | Manually visit `http://127.0.0.1:5000` |
| `wmic` not found during Chrome auto-kill | Newer Windows 11 builds removed it | Close the stale Chrome window manually once |

---

## Privacy & security

- The NVIDIA API key is stored locally in `config.json` next to the exe. **Don't commit it.**
- Page text + URL of every Phase 2 candidate is sent to NVIDIA's API for classification. Don't run against private / internal sites.
- Google sees your search queries — they come from your real Chrome session.
- The scraper's Chrome runs in an isolated profile (`.chrome-debug-profile/`) so it never reads from or writes to your normal browsing data.
- Delete `.chrome-debug-profile/` to factory-reset the scraper's Chrome state (log out of Google etc.).

---

## License

For internal use. No license attached.
