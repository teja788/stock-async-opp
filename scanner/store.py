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

CREATE INDEX IF NOT EXISTS idx_ann_isin   ON announcements(isin);
CREATE INDEX IF NOT EXISTS idx_ann_pub    ON announcements(published_at);
CREATE INDEX IF NOT EXISTS idx_news_pub   ON news(published_at);
CREATE INDEX IF NOT EXISTS idx_deals_isin ON deals(isin);
CREATE INDEX IF NOT EXISTS idx_deals_date ON deals(date);
"""


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def get_conn() -> sqlite3.Connection:
    """Open the DB (creating the file/dir on first use) with Row access."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create all tables/indexes if absent."""
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.executescript(_SCHEMA)
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
        cols = ["date", "deal_type", "company", "bse_code", "isin", "symbol",
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
        for t in ("companies", "announcements", "news", "deals"):
            out[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        return out
    finally:
        if own:
            conn.close()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


def get_watchlist(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn, "SELECT * FROM watchlist ORDER BY added_at DESC", ())
    finally:
        if own:
            conn.close()


def get_recent_announcements(since_iso: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or get_conn()
    try:
        return _rows(conn,
            "SELECT * FROM announcements WHERE published_at >= ? ORDER BY published_at DESC",
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
            "SELECT * FROM deals WHERE date >= ? ORDER BY date DESC",
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
