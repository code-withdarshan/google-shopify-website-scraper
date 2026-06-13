"""SQLite persistence — every URL collected and every verification is written
immediately, so a crash / Stop / closed Chrome tab never loses data."""
import os
import json
import sqlite3
import threading
import time

DB_LOCK = threading.RLock()
_DB_PATH: str = ""


def init_db(app_dir: str) -> str:
    global _DB_PATH
    _DB_PATH = os.path.join(app_dir, "scraper.db")
    with DB_LOCK, sqlite3.connect(_DB_PATH) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,
                created_at   REAL NOT NULL,
                status       TEXT NOT NULL,
                params_json  TEXT NOT NULL,
                ended_at     REAL
            );
            CREATE TABLE IF NOT EXISTS urls (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL,
                url          TEXT NOT NULL,
                host         TEXT NOT NULL,
                query        TEXT,
                found_at     REAL NOT NULL,
                UNIQUE(job_id, host)
            );
            CREATE TABLE IF NOT EXISTS verifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL,
                url          TEXT NOT NULL,
                domain       TEXT NOT NULL,
                platforms    TEXT,
                vertical     TEXT,
                confidence   REAL,
                reason       TEXT,
                accepted     INTEGER,
                verified_at  REAL NOT NULL,
                UNIQUE(job_id, domain)
            );
            CREATE INDEX IF NOT EXISTS idx_urls_job ON urls(job_id);
            CREATE INDEX IF NOT EXISTS idx_ver_job ON verifications(job_id);
            """
        )
    return _DB_PATH


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def record_job(job_id: str, params: dict) -> None:
    with DB_LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, status, params_json) "
            "VALUES (?, ?, ?, ?)",
            (job_id, time.time(), "running", json.dumps(params)),
        )


def update_job_status(job_id: str, status: str, ended: bool = False) -> None:
    with DB_LOCK, _conn() as c:
        if ended:
            c.execute("UPDATE jobs SET status=?, ended_at=? WHERE id=?",
                      (status, time.time(), job_id))
        else:
            c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


def record_url(job_id: str, url: str, host: str, query: str) -> None:
    with DB_LOCK, _conn() as c:
        try:
            c.execute(
                "INSERT INTO urls (job_id, url, host, query, found_at) VALUES (?,?,?,?,?)",
                (job_id, url, host, query, time.time()),
            )
        except sqlite3.IntegrityError:
            pass  # dup host for this job


def record_verification(job_id: str, row: dict) -> None:
    with DB_LOCK, _conn() as c:
        try:
            c.execute(
                "INSERT OR REPLACE INTO verifications "
                "(job_id, url, domain, platforms, vertical, confidence, reason, accepted, verified_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id, row.get("url", ""), row.get("domain", ""),
                    ",".join(row.get("platforms") or []),
                    row.get("vertical", ""), float(row.get("confidence") or 0),
                    (row.get("reason") or "")[:500],
                    1 if row.get("accepted") else 0,
                    time.time(),
                ),
            )
        except Exception:
            pass


def list_jobs(limit: int = 30) -> list[dict]:
    with DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT j.id, j.created_at, j.status, j.params_json, j.ended_at, "
            "  (SELECT COUNT(*) FROM urls u WHERE u.job_id=j.id) AS collected, "
            "  (SELECT COUNT(*) FROM verifications v WHERE v.job_id=j.id) AS verified, "
            "  (SELECT COUNT(*) FROM verifications v WHERE v.job_id=j.id AND v.accepted=1) AS matched, "
            "  (SELECT COUNT(*) FROM verifications v WHERE v.job_id=j.id "
            "     AND (v.vertical IS NULL OR v.vertical='' OR v.vertical='Unknown')) AS unknown_niche "
            "FROM jobs j ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try: d["params"] = json.loads(d.pop("params_json"))
            except Exception: d["params"] = {}
            out.append(d)
        return out


def get_job_urls(job_id: str) -> list[dict]:
    with DB_LOCK, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT url, host, query, found_at FROM urls WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()]


def get_job_verifications(job_id: str, accepted_only: bool = False) -> list[dict]:
    q = "SELECT * FROM verifications WHERE job_id=?"
    if accepted_only:
        q += " AND accepted=1"
    q += " ORDER BY id"
    with DB_LOCK, _conn() as c:
        out = []
        for r in c.execute(q, (job_id,)).fetchall():
            d = dict(r)
            d["platforms"] = [p for p in (d.get("platforms") or "").split(",") if p]
            d["accepted"] = bool(d.get("accepted"))
            out.append(d)
        return out


def recover_orphans() -> int:
    """Mark any job left as 'running' (from a killed process) as 'interrupted'
    so it shows up as resumable. Returns the number of jobs recovered."""
    with DB_LOCK, _conn() as c:
        rows = c.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
        for r in rows:
            c.execute("UPDATE jobs SET status='interrupted', ended_at=? WHERE id=?",
                      (time.time(), r["id"]))
        return len(rows)


def force_mark_status(job_id: str, status: str) -> None:
    with DB_LOCK, _conn() as c:
        c.execute("UPDATE jobs SET status=?, ended_at=? WHERE id=?",
                  (status, time.time(), job_id))


def find_previous_similar_job(params: dict, exclude_job_id: str = "") -> str | None:
    """Find the most recent finished job with the same keywords + tech stacks +
    custom niche, so we can compute a 'what's new' diff."""
    key_fields = ("keywords", "custom_niche", "tech_stacks", "city", "state", "country", "area")
    target = {k: params.get(k, "") for k in key_fields}
    # Normalise lists/strings
    if isinstance(target["tech_stacks"], list):
        target["tech_stacks"] = sorted(target["tech_stacks"])
    target_json = json.dumps(target, sort_keys=True)
    with DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT id, params_json FROM jobs WHERE id != ? AND status IN ('done','stopped') "
            "ORDER BY created_at DESC LIMIT 50",
            (exclude_job_id,),
        ).fetchall()
    for r in rows:
        try:
            p = json.loads(r["params_json"])
        except Exception:
            continue
        candidate = {k: p.get(k, "") for k in key_fields}
        if isinstance(candidate["tech_stacks"], list):
            candidate["tech_stacks"] = sorted(candidate["tech_stacks"])
        if json.dumps(candidate, sort_keys=True) == target_json:
            return r["id"]
    return None


def get_job_domains(job_id: str) -> set:
    with DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT host FROM urls WHERE job_id=?", (job_id,)
        ).fetchall()
    return {r["host"] for r in rows}


def delete_job(job_id: str) -> None:
    with DB_LOCK, _conn() as c:
        c.execute("DELETE FROM urls WHERE job_id=?", (job_id,))
        c.execute("DELETE FROM verifications WHERE job_id=?", (job_id,))
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
