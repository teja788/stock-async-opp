"""SQLite storage: schema, dedupe-safe upserts, catch-up tracking, read queries.

Design choices:
- Dedupe is enforced by a UNIQUE index on each table's `dedupe_hash` plus
  `INSERT OR IGNORE`, so re-running an ingester never creates duplicates -- the
  database is the single source of truth, not the caller.
- Catch-up is driven by the `runs` table: each source records its
  `last_success_at`; the next refresh fetches only since then. First run falls
  back to now - lookback_hours (handled by the ingesters).
- All timestamps are stored as ISO-8601 strings in IST.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from scanner.config import resolve_path

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DB_PATH = resolve_path("data/catalyst.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    isin          TEXT PRIMARY KEY,
    symbol        TEXT,
    bse_code      TEXT,
    name          TEXT,
    aliases       TEXT,           -- json array
    market_cap_cr REAL,
    sector        TEXT
);

CREATE TABLE IF NOT EXISTS announcements (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    isin           TEXT,
    bse_code       TEXT,
    company        TEXT,
    symbol         TEXT,
    category       TEXT,
    subcategory    TEXT,
    headline       TEXT,
    body_text      TEXT,
    pdf_url        TEXT,
    published_at   TEXT,
    ingested_at    TEXT,
    dedupe_hash    TEXT UNIQUE,
    candidate_tags TEXT            -- json array, filled by the prefilter (M7)
);

CREATE TABLE IF NOT EXISTS news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_isins TEXT,            -- json array
    source        TEXT,
    trust         TEXT,
    headline      TEXT,
    url           TEXT,
    summary       TEXT,
    published_at  TEXT,
    ingested_at   TEXT,
    dedupe_hash   TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT,
    deal_type       TEXT,          -- bulk | block | insider | sast
    exchange        TEXT,          -- BSE | NSE
    company         TEXT,
    bse_code        TEXT,
    isin            TEXT,
    symbol          TEXT,
    in_universe     INTEGER,
    client_name     TEXT,
    side            TEXT,          -- BUY | SELL | PLEDGE | OTHER
    qty             REAL,
    price           REAL,
    person_category TEXT,
    pct_pre         REAL,
    pct_post        REAL,
    is_marquee      INTEGER,
    matched_investor TEXT,
    is_promoter_buy INTEGER,
    url             TEXT,
    ingested_at     TEXT,
    dedupe_hash     TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS runs (
    source          TEXT PRIMARY KEY,
    last_success_at TEXT,
    items_fetched   INTEGER,
    status          TEXT,
    note            TEXT,
    updated_at      TEXT
);

-- Stub for Milestone 12 (UX wired later): prioritise/segregate tagged tickers.
CREATE TABLE IF NOT EXISTS watchlist (
    isin     TEXT PRIMARY KEY,
    symbol   TEXT,
    added_at TEXT,
    note     TEXT
);

-- Cached PDF body text for filings (extracted on demand for candidate filings).
-- Keyed by the announcement's dedupe_hash so extraction is done once and reused.
CREATE TABLE IF NOT EXISTS filing_text (
    ref_hash    TEXT PRIMARY KEY,   -- announcements.dedupe_hash
    url         TEXT,
    text        TEXT,
    n_chars     INTEGER,
    method      TEXT,               -- pymupdf | empty | error:<reason>
    created_at  TEXT
);

-- Numbers extracted from quarterly-results filings (results_extract.py):
-- the quantified basis for "earnings surprise" and revenue-based materiality.
CREATE TABLE IF NOT EXISTS results_extract (
    ref_hash        TEXT PRIMARY KEY,   -- announcements.dedupe_hash
    isin            TEXT,
    period_label    TEXT,
    revenue_cr      REAL,
    revenue_yoy_pct REAL,
    pat_cr          REAL,
    pat_yoy_pct     REAL,
    fy_revenue_cr   REAL,
    consolidated    INTEGER,            -- 1/0/NULL
    confidence      TEXT,
    created_at      TEXT
);

-- Credit-rating actions scraped from CRA media pages (ICRA/CRISIL/CARE).
CREATE TABLE IF NOT EXISTS ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agency      TEXT,               -- ICRA | CRISIL | CARE
    company     TEXT,
    isin        TEXT,
    bse_code    TEXT,
    symbol      TEXT,
    in_universe INTEGER,
    action      TEXT,               -- upgrade | downgrade | reaffirm | assign | outlook | unknown
    direction   TEXT,
    instrument  TEXT,
    rating      TEXT,
    date        TEXT,
    url         TEXT,
    summary     TEXT,
    ingested_at TEXT,
    dedupe_hash TEXT UNIQUE
);

-- Daily close/volume per universe scrip from the free BSE bhavcopy.
-- Powers the "already priced in?" context lines and the `review` command.
CREATE TABLE IF NOT EXISTS prices (
    isin   TEXT,
    date   TEXT,               -- YYYY-MM-DD (trade date)
    close  REAL,
    volume REAL,
    PRIMARY KEY (isin, date)
);

CREATE INDEX IF NOT EXISTS idx_ann_isin   ON announcements(isin);
CREATE INDEX IF NOT EXISTS idx_ann_pub    ON announcements(published_at);
CREATE INDEX IF NOT EXISTS idx_news_pub   ON news(published_at);
CREATE INDEX IF NOT EXISTS idx_deals_isin ON deals(isin);
CREATE INDEX IF NOT EXISTS idx_deals_date ON deals(date);
CREATE INDEX IF NOT EXISTS idx_ratings_dt ON ratings(date);
"""


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def get_conn() -> sqlite3.Connection:
    """Open the DB (creating the file/dir on first use) with Row access."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Generous busy timeout: the scheduled 45-min refresh and an interactive
    # scan/dashboard can write concurrently; WAL + 30s wait avoids "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create all tables/indexes if absent."""
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executescript(_SCHEMA)
        # Lightweight migration: add `exchange` to deals tables created before it
        # existed. SQLite has no "ADD COLUMN IF NOT EXISTS", so we try/ignore.
        try:
            conn.execute("ALTER TABLE deals ADD COLUMN exchange TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Companies (from the universe map)
# --------------------------------------------------------------------------- #
def sync_companies(universe: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = [(
            c["isin"], c.get("symbol"), str(c.get("bse_code") or ""), c.get("name"),
            json.dumps(c.get("aliases", [])), c.get("market_cap_cr"), c.get("sector"),
        ) for c in universe if c.get("isin")]
        conn.executemany(
            """INSERT INTO companies (isin, symbol, bse_code, name, aliases, market_cap_cr, sector)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(isin) DO UPDATE SET
                 symbol=excluded.symbol, bse_code=excluded.bse_code, name=excluded.name,
                 aliases=excluded.aliases, market_cap_cr=excluded.market_cap_cr,
                 sector=excluded.sector""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Dedupe-safe upserts. Each returns the number of NEW rows inserted.
# --------------------------------------------------------------------------- #
def _insert_ignore(conn: sqlite3.Connection, table: str, cols: list[str],
                   records: Iterable[dict[str, Any]]) -> int:
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    before = conn.total_changes
    now = _now_iso()
    payload = []
    for r in records:
        row = dict(r)
        row["ingested_at"] = now
        payload.append([row.get(c) for c in cols])
    conn.executemany(sql, payload)
    conn.commit()
    return conn.total_changes - before


def upsert_announcements(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["isin", "bse_code", "company", "symbol", "category", "subcategory",
                "headline", "body_text", "pdf_url", "published_at", "ingested_at",
                "dedupe_hash", "candidate_tags"]
        # candidate_tags is filled by the prefilter; default to empty json array.
        for it in items:
            it.setdefault("candidate_tags", "[]")
        return _insert_ignore(conn, "announcements", cols, items)
    finally:
        if own:
            conn.close()


def upsert_news(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["company_isins", "source", "trust", "headline", "url", "summary",
                "published_at", "ingested_at", "dedupe_hash"]
        prepared = []
        for it in items:
            r = dict(it)
            r["company_isins"] = json.dumps(r.get("company_isins", []))  # list -> json text
            prepared.append(r)
        return _insert_ignore(conn, "news", cols, prepared)
    finally:
        if own:
            conn.close()


def upsert_deals(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["date", "deal_type", "exchange", "company", "bse_code", "isin", "symbol",
                "in_universe", "client_name", "side", "qty", "price",
                "person_category", "pct_pre", "pct_post", "is_marquee",
                "matched_investor", "is_promoter_buy", "url", "ingested_at",
                "dedupe_hash"]
        prepared = []
        for it in items:
            r = dict(it)
            r["in_universe"] = int(bool(r.get("in_universe")))
            r["is_marquee"] = int(bool(r.get("is_marquee")))
            r["is_promoter_buy"] = int(bool(r.get("is_promoter_buy")))
            prepared.append(r)
        return _insert_ignore(conn, "deals", cols, prepared)
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Run / catch-up tracking
# --------------------------------------------------------------------------- #
def get_last_success(source: str, conn: sqlite3.Connection | None = None) -> datetime | None:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute(
            "SELECT last_success_at FROM runs WHERE source=?", (source,)).fetchone()
        if row and row["last_success_at"]:
            try:
                return datetime.fromisoformat(row["last_success_at"])
            except ValueError:
                return None
        return None
    finally:
        if own:
            conn.close()


def mark_run(source: str, items_fetched: int, status: str, note: str = "",
             success_at: datetime | None = None, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        now = _now_iso()
        last = (success_at or datetime.now(IST)).isoformat() if status == "ok" else None
        # Preserve the prior last_success_at on failure (don't advance the cursor).
        if last is None:
            prior = conn.execute(
                "SELECT last_success_at FROM runs WHERE source=?", (source,)).fetchone()
            last = prior["last_success_at"] if prior else None
        conn.execute(
            """INSERT INTO runs (source, last_success_at, items_fetched, status, note, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 last_success_at=excluded.last_success_at, items_fetched=excluded.items_fetched,
                 status=excluded.status, note=excluded.note, updated_at=excluded.updated_at""",
            (source, last, items_fetched, status, note, now),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Read queries (used by `ask` in Milestone 10; minimal set for now)
# --------------------------------------------------------------------------- #
def counts(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    own = conn is None
    conn = conn or get_conn()
    try:
        out = {}
        for t in ("companies", "announcements", "news", "deals", "ratings",
                  "prices", "filing_text"):
            out[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        return out
    finally:
        if own:
            conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def coverage(conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
    """Stored date range + count per source, so the UI knows what it already has.

    Lets 'days back' stay display-only: if the requested window starts before
    a source's earliest stored row, the UI can offer a gap-only backfill instead
    of re-downloading everything.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        out: dict[str, dict[str, Any]] = {}
        specs = [("announcements", "published_at"), ("news", "published_at"),
                 ("deals", "date"), ("ratings", "date"), ("prices", "date")]
        for table, col in specs:
            row = conn.execute(
                f"SELECT COUNT(*) n, MIN({col}) lo, MAX({col}) hi FROM {table}").fetchone()
            out[table] = {"count": row["n"], "earliest": row["lo"], "latest": row["hi"]}
        return out
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Watchlist (Section 17 future hook). The TABLE + these basic helpers exist now;
# the UX (prioritising/segregating watchlisted tickers in the context pack) is
# wired later -- see the TODO in context_pack.py.
# --------------------------------------------------------------------------- #
def add_to_watchlist(isin: str, symbol: str = "", note: str = "",
                     conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            "INSERT INTO watchlist (isin, symbol, added_at, note) VALUES (?,?,?,?) "
            "ON CONFLICT(isin) DO UPDATE SET symbol=excluded.symbol, note=excluded.note",
            (isin, symbol, _now_iso(), note))
        conn.commit()
    finally:
        if own:
            conn.close()


def remove_from_watchlist(isin: str, conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        before = conn.total_changes
        conn.execute("DELETE FROM watchlist WHERE isin=?", (isin,))
        conn.commit()
        return conn.total_changes - before
    finally:
        if own:
            conn.close()


def get_watchlist(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM watchlist ORDER BY added_at DESC", ())
    finally:
        if own:
            conn.close()


def filings_with_text_before(isin: str, before_iso: str, limit: int = 12,
                             conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Recent filings for `isin` published before `before_iso` that have cached
    PDF text — e.g. the previous investor deck for guidance diffing."""
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, """
            SELECT a.dedupe_hash, a.headline, a.subcategory, a.published_at, f.text
            FROM announcements a JOIN filing_text f ON f.ref_hash = a.dedupe_hash
            WHERE a.isin=? AND a.published_at < ? AND f.n_chars > 200
            ORDER BY a.published_at DESC LIMIT ?""", (isin, before_iso, limit))
    finally:
        if own:
            conn.close()


def get_recent_announcements(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        # COALESCE keeps recall-first rows whose publish date failed to parse
        # (stored as NULL) visible in the window via their ingestion time.
        return _rows(conn,
            "SELECT * FROM announcements WHERE COALESCE(published_at, ingested_at) >= ? "
            "ORDER BY COALESCE(published_at, ingested_at) DESC",
            (since_iso,))
    finally:
        if own:
            conn.close()


def get_recent_news(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn,
            "SELECT * FROM news WHERE published_at >= ? ORDER BY published_at DESC",
            (since_iso,))
    finally:
        if own:
            conn.close()


def get_recent_deals(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn,
            "SELECT * FROM deals WHERE COALESCE(date, ingested_at) >= ? "
            "ORDER BY COALESCE(date, ingested_at) DESC",
            (since_iso,))
    finally:
        if own:
            conn.close()


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def announcements_for_isins(isins: list[str], limit: int = 50, since_iso: str | None = None,
                            conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if not isins:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        where = f"isin IN ({_placeholders(len(isins))})"
        params: list[Any] = list(isins)
        if since_iso:
            where += " AND published_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn,
            f"SELECT * FROM announcements WHERE {where} ORDER BY published_at DESC LIMIT ?",
            tuple(params))
    finally:
        if own:
            conn.close()


def deals_for_isins(isins: list[str], limit: int = 50, since_iso: str | None = None,
                    conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if not isins:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        where = f"isin IN ({_placeholders(len(isins))})"
        params: list[Any] = list(isins)
        if since_iso:
            where += " AND date >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn,
            f"SELECT * FROM deals WHERE {where} ORDER BY date DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()


def news_for_isins(isins: list[str], limit: int = 50, since_iso: str | None = None,
                   conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """News rows whose company_isins json array contains any of the given isins."""
    if not isins:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        clause = " OR ".join("company_isins LIKE ?" for _ in isins)
        where = f"({clause})"
        params: list[Any] = [f'%"{i}"%' for i in isins]
        if since_iso:
            where += " AND published_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn,
            f"SELECT * FROM news WHERE {where} ORDER BY published_at DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()


def announcements_by_tag(tag: str, limit: int = 50, since_iso: str | None = None,
                         conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        where = "candidate_tags LIKE ?"
        params: list[Any] = [f'%"{tag}"%']
        if since_iso:
            where += " AND published_at >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn,
            f"SELECT * FROM announcements WHERE {where} ORDER BY published_at DESC LIMIT ?",
            tuple(params))
    finally:
        if own:
            conn.close()


def set_announcement_tags(ann_id: int, tags: list[str], conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute("UPDATE announcements SET candidate_tags=? WHERE id=?",
                     (json.dumps(tags), ann_id))
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Filing PDF text cache (#8). Extracted on demand for candidate filings, keyed
# by the announcement's dedupe_hash so each PDF is parsed at most once.
# --------------------------------------------------------------------------- #
def get_filing_text(ref_hash: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT * FROM filing_text WHERE ref_hash=?", (ref_hash,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def save_filing_text(ref_hash: str, url: str, text: str, method: str,
                     conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            "INSERT INTO filing_text (ref_hash, url, text, n_chars, method, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(ref_hash) DO UPDATE SET "
            "url=excluded.url, text=excluded.text, n_chars=excluded.n_chars, "
            "method=excluded.method, created_at=excluded.created_at",
            (ref_hash, url, text, len(text or ""), method, _now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Extracted results numbers (cache for results_extract.py).
# --------------------------------------------------------------------------- #
def get_results_extract(ref_hash: str,
                        conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute("SELECT * FROM results_extract WHERE ref_hash=?",
                           (ref_hash,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["consolidated"] = None if d["consolidated"] is None else bool(d["consolidated"])
        return d
    finally:
        if own:
            conn.close()


def save_results_extract(ref_hash: str, isin: str | None, res: dict[str, Any],
                         conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        cons = res.get("consolidated")
        conn.execute(
            """INSERT OR REPLACE INTO results_extract
               (ref_hash, isin, period_label, revenue_cr, revenue_yoy_pct,
                pat_cr, pat_yoy_pct, fy_revenue_cr, consolidated, confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ref_hash, isin, res.get("period_label"), res.get("revenue_cr"),
             res.get("revenue_yoy_pct"), res.get("pat_cr"), res.get("pat_yoy_pct"),
             res.get("fy_revenue_cr"),
             None if cons is None else int(bool(cons)),
             res.get("confidence"), _now_iso()))
        conn.commit()
    finally:
        if own:
            conn.close()


def latest_fy_revenues(conn: sqlite3.Connection | None = None) -> dict[str, float]:
    """{isin: latest known previous-full-year revenue (₹ cr)} — the denominator
    for revenue-based materiality ('a ₹500 cr order ≈ 2x FY revenue')."""
    own = conn is None
    conn = conn or get_conn()
    try:
        out: dict[str, float] = {}
        for r in conn.execute(
                "SELECT isin, fy_revenue_cr FROM results_extract "
                "WHERE isin IS NOT NULL AND fy_revenue_cr IS NOT NULL "
                "ORDER BY created_at ASC"):
            out[r["isin"]] = r["fy_revenue_cr"]  # later rows win
        return out
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Prices (daily bhavcopy closes) + derived context.
# --------------------------------------------------------------------------- #
def upsert_prices(rows: list[tuple[str, str, float, float]],
                  conn: sqlite3.Connection | None = None) -> int:
    """Insert (isin, date, close, volume) rows; returns NEW rows inserted."""
    own = conn is None
    conn = conn or get_conn()
    try:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO prices (isin, date, close, volume) VALUES (?,?,?,?)",
            rows)
        conn.commit()
        return conn.total_changes - before
    finally:
        if own:
            conn.close()


def price_dates(conn: sqlite3.Connection | None = None) -> set[str]:
    """Distinct trade dates already stored (so the ingester fetches only gaps)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        return {r["date"] for r in conn.execute("SELECT DISTINCT date FROM prices")}
    finally:
        if own:
            conn.close()


def prune_prices(keep_after_iso: str, conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        before = conn.total_changes
        conn.execute("DELETE FROM prices WHERE date < ?", (keep_after_iso,))
        conn.commit()
        return conn.total_changes - before
    finally:
        if own:
            conn.close()


def price_context(isin: str, since_date: str,
                  conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Price/volume move since a catalyst date — the "already priced in?" check.

    Returns {pct_change, vol_ratio, ref_date, last_date, last_close} or None if
    there isn't enough stored history. `vol_ratio` compares average daily volume
    AFTER the catalyst to the 20 sessions BEFORE it.
    """
    if not isin or not since_date:
        return None
    own = conn is None
    conn = conn or get_conn()
    try:
        day = since_date[:10]
        # Baseline strictly BEFORE the catalyst day — that day's close (and
        # volume) may already contain the reaction we're trying to measure.
        ref = conn.execute(
            "SELECT date, close FROM prices WHERE isin=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (isin, day)).fetchone()
        last = conn.execute(
            "SELECT date, close FROM prices WHERE isin=? "
            "ORDER BY date DESC LIMIT 1", (isin,)).fetchone()
        if not ref or not last or not ref["close"]:
            return None
        base_vol = conn.execute(
            "SELECT AVG(volume) v FROM (SELECT volume FROM prices "
            "WHERE isin=? AND date<? ORDER BY date DESC LIMIT 20)",
            (isin, day)).fetchone()["v"]
        after_vol = conn.execute(
            "SELECT AVG(volume) v FROM prices WHERE isin=? AND date>=?",
            (isin, day)).fetchone()["v"]
        return {
            "pct_change": (last["close"] - ref["close"]) / ref["close"] * 100,
            "vol_ratio": (after_vol / base_vol) if (after_vol and base_vol) else None,
            "ref_date": ref["date"], "last_date": last["date"],
            "last_close": last["close"],
        }
    finally:
        if own:
            conn.close()


def benchmark_move(start_day: str, end_day: str,
                   conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Equal-weight universe move between two dates — the alpha baseline.

    Median % change across every stored ISIN with a close on/before BOTH dates
    (same as-of rule as price_on). A smallcap-heavy universe median is a fairer
    "did this lead beat the tape?" baseline than a large-cap index, and it
    needs no extra data source. Returns {median_pct, n} or None.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            """SELECT (p2.close - p1.close) * 100.0 / p1.close AS pct
               FROM (SELECT p.isin, p.close FROM prices p
                     JOIN (SELECT isin, MAX(date) d FROM prices
                           WHERE date <= ? GROUP BY isin) m
                       ON p.isin = m.isin AND p.date = m.d) p1
               JOIN (SELECT p.isin, p.close FROM prices p
                     JOIN (SELECT isin, MAX(date) d FROM prices
                           WHERE date <= ? GROUP BY isin) m
                       ON p.isin = m.isin AND p.date = m.d) p2
                 ON p1.isin = p2.isin
               WHERE p1.close > 0""",
            (start_day[:10], end_day[:10])).fetchall()
        moves = sorted(r["pct"] for r in rows)
        if not moves:
            return None
        n = len(moves)
        median = moves[n // 2] if n % 2 else (moves[n // 2 - 1] + moves[n // 2]) / 2
        return {"median_pct": median, "n": n}
    finally:
        if own:
            conn.close()


def catalyst_tags_between(isin: str, since_iso: str, until_iso: str,
                          conn: sqlite3.Connection | None = None) -> list[str]:
    """Union of candidate_tags on a company's filings in [since, until].

    Used by the review loop to attribute a logged lead to the catalyst tags
    that were live shortly before it was flagged (approximate by design)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        tags: list[str] = []
        for r in conn.execute(
                "SELECT candidate_tags FROM announcements WHERE isin=? "
                "AND published_at >= ? AND published_at <= ? "
                "AND candidate_tags NOT IN ('', '[]')", (isin, since_iso, until_iso)):
            try:
                for t in json.loads(r["candidate_tags"] or "[]"):
                    if t not in tags:
                        tags.append(t)
            except json.JSONDecodeError:
                continue
        return tags
    finally:
        if own:
            conn.close()


def price_on(isin: str, on_or_before: str,
             conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Close on/most-recently-before a date (for the `review` command)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute(
            "SELECT date, close FROM prices WHERE isin=? AND date<=? "
            "ORDER BY date DESC LIMIT 1", (isin, on_or_before[:10])).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Insider accumulation (trailing aggregation over insider/SAST buys).
# --------------------------------------------------------------------------- #
def insider_accumulation(since_iso: str,
                         conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Per-company aggregate of promoter/insider BUYs since `since_iso`.

    A promoter buying five times in 60 days is a far stronger signal than any
    single row. Also flags a SAST 5% threshold crossing (a NEW substantial
    shareholder appearing) and counts DISTINCT buyers (cluster buying — several
    different insiders buying beats one promoter's total). Sorted by cumulative
    stake added, desc. Significance gating (₹ floor + % of mcap) happens in the
    context pack, where market caps live.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, """
            SELECT isin, symbol, company,
                   COUNT(*)                                  AS n_buys,
                   COUNT(DISTINCT LOWER(TRIM(client_name)))  AS n_buyers,
                   SUM(COALESCE(pct_post,0) - COALESCE(pct_pre,0)) AS cum_pct,
                   SUM(COALESCE(qty,0))                      AS cum_qty,
                   MIN(date)                                 AS first_buy,
                   MAX(date)                                 AS last_buy,
                   MAX(CASE WHEN pct_pre IS NOT NULL AND pct_post IS NOT NULL
                            AND pct_pre < 5 AND pct_post >= 5 THEN 1 ELSE 0 END)
                                                             AS crossed_5pct
            FROM deals
            WHERE deal_type IN ('insider','sast') AND side='BUY'
              AND isin IS NOT NULL AND date >= ?
            GROUP BY isin
            HAVING n_buys >= 2 OR cum_pct >= 0.5 OR crossed_5pct = 1
            ORDER BY cum_pct DESC, n_buys DESC""", (since_iso,))
    finally:
        if own:
            conn.close()


def marquee_accumulation(since_iso: str,
                         conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Per-(investor, company) aggregate of marquee BUYs since `since_iso`.

    A star investor printing bulk deals on four separate days is a much
    stronger signal than one print — the in-window deals section shows them as
    disconnected rows; this rolls them up. `value_cr` sums qty*price where both
    are disclosed (bulk/block deals); insider/SAST rows carry no price and
    contribute qty only. Grouped on COALESCE(isin, company) because marquee
    deals are kept even for out-of-universe companies (isin unresolved).
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, """
            SELECT matched_investor, isin, symbol, company,
                   COUNT(*)                                  AS n_buys,
                   SUM(COALESCE(qty,0))                      AS cum_qty,
                   SUM(CASE WHEN qty IS NOT NULL AND price IS NOT NULL
                            THEN qty*price ELSE 0 END) / 1e7 AS value_cr,
                   MIN(date)                                 AS first_buy,
                   MAX(date)                                 AS last_buy
            FROM deals
            WHERE is_marquee=1 AND side='BUY'
              AND matched_investor IS NOT NULL AND date >= ?
            GROUP BY matched_investor, COALESCE(isin, company)
            ORDER BY n_buys DESC, value_cr DESC""", (since_iso,))
    finally:
        if own:
            conn.close()


def news_pickup_count(isin: str, since_iso: str,
                      conn: sqlite3.Connection | None = None) -> int:
    """Stories tagging `isin` published at/after `since_iso`.

    The attention check behind rubric gate 3: a catalyst filing that is a day
    old with ZERO news pickup is the literal definition of "not yet widely
    noticed"."""
    own = conn is None
    conn = conn or get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM news WHERE company_isins LIKE ? "
            "AND published_at >= ?", (f'%"{isin}"%', since_iso)).fetchone()
        return row["n"]
    finally:
        if own:
            conn.close()


def pledge_activity(since_iso: str,
                    conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Per-company pledge/release/invocation aggregate since `since_iso`.

    A large pledge RELEASE after deleveraging is a classic re-rating tell (the
    overhang is gone); fresh pledging is a caution and an INVOCATION (lender
    seizing shares) is a red flag. Sides come from the insider/SAST feed and
    vary in wording (PLEDGE / REVOKE / RELEASE / INVOKE...), hence LIKE."""
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, """
            SELECT isin, symbol, company,
                   SUM(CASE WHEN side LIKE 'PLEDGE%' THEN 1 ELSE 0 END)  AS n_pledges,
                   SUM(CASE WHEN side LIKE 'REVO%' OR side LIKE 'RELEAS%'
                            THEN 1 ELSE 0 END)                           AS n_releases,
                   SUM(CASE WHEN side LIKE 'INVO%' THEN 1 ELSE 0 END)    AS n_invocations,
                   SUM(CASE WHEN side LIKE 'REVO%' OR side LIKE 'RELEAS%'
                            THEN COALESCE(qty,0) ELSE 0 END)             AS released_qty,
                   SUM(CASE WHEN side LIKE 'PLEDGE%'
                            THEN COALESCE(qty,0) ELSE 0 END)             AS pledged_qty,
                   MAX(date)                                             AS last_event
            FROM deals
            WHERE date >= ? AND (side LIKE 'PLEDGE%' OR side LIKE 'REVO%'
                                 OR side LIKE 'RELEAS%' OR side LIKE 'INVO%')
            GROUP BY COALESCE(isin, company)
            ORDER BY n_releases DESC, n_pledges DESC""", (since_iso,))
    finally:
        if own:
            conn.close()


def insider_selling(since_iso: str,
                    conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Per-company aggregate of marquee/promoter SELLs since `since_iso`.

    The caution overlay: sells never generate leads, but a lead on a name the
    insiders are exiting needs that selling explained. `cum_pct_sold` sums
    disclosed stake reductions (insider/SAST rows); `value_cr` sums qty*price
    where both are disclosed (bulk/block rows).
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, """
            SELECT isin, symbol, company,
                   COUNT(*)                                  AS n_sells,
                   COUNT(DISTINCT LOWER(TRIM(client_name)))  AS n_sellers,
                   SUM(COALESCE(qty,0))                      AS cum_qty,
                   SUM(CASE WHEN qty IS NOT NULL AND price IS NOT NULL
                            THEN qty*price ELSE 0 END) / 1e7 AS value_cr,
                   SUM(CASE WHEN pct_pre IS NOT NULL AND pct_post IS NOT NULL
                            THEN pct_pre - pct_post ELSE 0 END) AS cum_pct_sold,
                   MAX(is_marquee)                           AS any_marquee,
                   GROUP_CONCAT(DISTINCT COALESCE(matched_investor, client_name))
                                                             AS sellers
            FROM deals
            WHERE side='SELL' AND date >= ?
              AND (is_marquee=1 OR LOWER(COALESCE(person_category,'')) LIKE '%promoter%')
            GROUP BY COALESCE(isin, company)
            ORDER BY any_marquee DESC, cum_pct_sold DESC, value_cr DESC""", (since_iso,))
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# Credit-rating actions (CRA scraping).
# --------------------------------------------------------------------------- #
def upsert_ratings(items: list[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or get_conn()
    try:
        cols = ["agency", "company", "isin", "bse_code", "symbol", "in_universe",
                "action", "direction", "instrument", "rating", "date", "url",
                "summary", "ingested_at", "dedupe_hash"]
        prepared = []
        for it in items:
            r = dict(it)
            r["in_universe"] = int(bool(r.get("in_universe")))
            prepared.append(r)
        return _insert_ignore(conn, "ratings", cols, prepared)
    finally:
        if own:
            conn.close()


def update_rating_directions(items: list[dict[str, Any]],
                             conn: sqlite3.Connection | None = None) -> int:
    """Persist PDF-derived direction onto rows previously stored as 'other'.

    The dedupe hash is computed BEFORE the CARE rationale-PDF enrichment, so a
    row stored as action='other' on an earlier run matches this run's enriched
    copy by hash — INSERT OR IGNORE alone would discard the improvement and the
    stored direction would stay 'other' forever. Returns rows updated.
    """
    rows = [(r.get("action"), r.get("direction"), r.get("summary"), r.get("dedupe_hash"))
            for r in items
            if r.get("dedupe_hash") and r.get("action") not in (None, "", "other")]
    if not rows:
        return 0
    own = conn is None
    conn = conn or get_conn()
    try:
        before = conn.total_changes
        conn.executemany(
            "UPDATE ratings SET action=?, direction=?, summary=? "
            "WHERE dedupe_hash=? AND action='other'", rows)
        conn.commit()
        return conn.total_changes - before
    finally:
        if own:
            conn.close()


def get_recent_ratings(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        # `ingested_at >= ?` surfaces newly-DISCOVERED actions once even when the
        # nominal action date is older than the window (CRISIL's monthly
        # newsletter lags 4-8 weeks; an upgrade found today is new information).
        return _rows(conn,
            "SELECT * FROM ratings WHERE date >= ? OR ingested_at >= ? "
            "ORDER BY COALESCE(date, ingested_at) DESC", (since_iso, since_iso))
    finally:
        if own:
            conn.close()


def ratings_for_isins(isins: list[str], limit: int = 50, since_iso: str | None = None,
                      conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    if not isins:
        return []
    own = conn is None
    conn = conn or get_conn()
    try:
        where = f"isin IN ({_placeholders(len(isins))})"
        params: list[Any] = list(isins)
        if since_iso:
            where += " AND date >= ?"
            params.append(since_iso)
        params.append(limit)
        return _rows(conn,
            f"SELECT * FROM ratings WHERE {where} ORDER BY date DESC LIMIT ?", tuple(params))
    finally:
        if own:
            conn.close()
