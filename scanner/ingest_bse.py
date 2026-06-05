"""BSE corporate-announcements ingester (Milestone 3).

For each Nifty-500 BSE scrip code, fetch recent corporate announcements from the
BSE public API, normalise them into a stable shape, and return them. Storage +
dedupe persistence is wired in Milestone 6; this module computes the dedupe hash
so the store can rely on it.

Key facts learned from live probing:
- The all-companies feed is disabled ("No Record Found!"), so we query per scrip.
- The API is DATE-granular (YYYYMMDD); we filter to the exact lookback window in
  code using the per-row timestamp.
- Attachment PDFs live at AttachLive/<ATTACHMENTNAME>.
- BSE timestamps are naive IST.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from scanner.config import load_settings
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
ANN_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_ist(value: str | None) -> datetime | None:
    """Parse a BSE naive-IST timestamp into an aware IST datetime."""
    if not value:
        return None
    try:
        dt = dtparser.parse(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _dedupe_hash(bse_code: str, newsid: str, headline: str) -> str:
    """Stable content hash. NEWSID is BSE's own unique id, so this rarely collides."""
    raw = f"{bse_code}|{newsid}|{headline}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _normalize(row: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """Map a raw BSE announcement row to our internal announcement shape."""
    newsid = str(row.get("NEWSID") or "").strip()
    headline = (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip()
    attachment = (row.get("ATTACHMENTNAME") or "").strip()
    published = _parse_ist(row.get("NEWS_DT") or row.get("DT_TM"))
    return {
        "bse_code": str(meta["bse_code"]),
        "isin": meta["isin"],
        "company": meta["name"],
        "symbol": meta["symbol"],
        "category": (row.get("CATEGORYNAME") or "").strip(),
        "subcategory": (row.get("SUBCATNAME") or "").strip(),
        "headline": headline,
        "body_text": (row.get("MORE") or "").strip(),
        "pdf_url": (ATTACH_BASE + attachment) if attachment else "",
        "nsurl": (row.get("NSURL") or "").strip(),
        "is_critical": bool(row.get("CRITICALNEWS")),
        "published_at": published.isoformat() if published else None,
        "newsid": newsid,
        "dedupe_hash": _dedupe_hash(str(meta["bse_code"]), newsid, headline),
        "source": "BSE",
    }


def fetch_for_scrip(session: PoliteSession, scrip_code: str,
                    frm: str, to: str) -> list[dict[str, Any]]:
    """Return raw announcement rows for one scrip over [frm, to] (YYYYMMDD)."""
    params = {
        "pageno": 1, "strCat": "-1", "strPrevDate": frm, "strToDate": to,
        "strSearch": "P", "strscrip": str(scrip_code), "strType": "C",
    }
    resp = session.bse_get(ANN_URL, params=params, timeout=45)
    data = resp.json()
    if isinstance(data, str):  # BSE sometimes double-encodes or returns a message
        return []
    return data.get("Table", []) if isinstance(data, dict) else []


def ingest(session: PoliteSession | None = None,
           since: datetime | None = None,
           scrip_codes: Iterable[str] | None = None,
           progress_cb: Callable[[int, int], None] | None = None
           ) -> list[dict[str, Any]]:
    """Fetch + normalise announcements for the universe within the lookback window.

    Args:
        since: only keep announcements at/after this instant (catch-up). Defaults
               to now - settings.lookback_hours.
        scrip_codes: explicit subset (for testing); defaults to the full universe.
        progress_cb: optional callback(done, total) for a CLI progress bar.

    Per-scrip failures are logged and skipped so one bad scrip never aborts the run.
    """
    session = session or PoliteSession()
    settings = load_settings()
    lookback = int(settings.get("lookback_hours", 24))
    since = since or (_now_ist() - timedelta(hours=lookback))

    universe = load_map()
    meta_by_code = {str(c["bse_code"]): c for c in universe if c.get("bse_code")}
    codes = [str(c) for c in scrip_codes] if scrip_codes else list(meta_by_code.keys())

    # API date window: pad one day on the early side so timezone/edge filings aren't missed.
    frm = (since - timedelta(days=1)).strftime("%Y%m%d")
    to = _now_ist().strftime("%Y%m%d")

    results: list[dict[str, Any]] = []
    total = len(codes)
    failures = 0
    for i, code in enumerate(codes, 1):
        meta = meta_by_code.get(code)
        if not meta:
            continue
        try:
            rows = fetch_for_scrip(session, code, frm, to)
            for row in rows:
                norm = _normalize(row, meta)
                pub = _parse_ist(row.get("NEWS_DT") or row.get("DT_TM"))
                if pub is None or pub >= since:
                    results.append(norm)
        except Exception as exc:  # noqa: BLE001 - isolate per-scrip failures
            failures += 1
            log.warning("BSE fetch failed for scrip %s (%s): %s",
                        code, meta.get("symbol"), exc)
        if progress_cb:
            progress_cb(i, total)

    log.info("BSE ingest done: %d announcements from %d scrips (%d failures), since %s",
             len(results), total, failures, since.isoformat())
    return results
