"""Bulk/block deals + insider/SAST ingester (Milestone 5).

Pulls three BSE datasets, normalises them to a common 'deal' shape, resolves each
to our universe by BSE scrip code, and flags:
  - marquee purchases: CLIENT_NAME / promoter-name matches config/investors.yaml
  - promoter buys: insider rows whose person-category is a Promoter, side = BUY

Endpoints + quirks (verified live 2026-06-05):
  - Bulk:  BulkDealData_ng/w  DealType=1  FDate/TDate = DD/MM/YYYY  side P/S
  - Block: BulkDealData_ng/w  DealType=2  (same shape); BlockDeal_Beta/w is a
           latest-day-only fallback that ignores params.
  - Insider+SAST: getCorp_Regulation_ng/w  fromDT/ToDate = YYYYMMDD  side Buy/Sell/...

Investor name matching uses TOKEN-SUBSET matching (every significant token of a
watchlist name must appear in the disclosed name), which handles middle names and
suffixes: "Rekha Jhunjhunwala" matches "REKHA RAKESH JHUNJHUNWALA".
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from scanner.config import load_investors, load_settings, load_sources
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_TOKEN = re.compile(r"[a-z0-9]+")
_BUY_CODES = {"P", "B", "BUY", "PURCHASE", "ACQUISITION"}
_SELL_CODES = {"S", "SELL", "SALE", "DISPOSAL"}


# --------------------------------------------------------------------------- #
# Marquee-investor matching
# --------------------------------------------------------------------------- #
class InvestorMatcher:
    """Token-subset matcher of disclosed client names against the watchlist."""

    def __init__(self, investors_cfg: dict[str, Any]):
        names = investors_cfg.get("marquee_investors", [])
        # (canonical name to report, token pattern to match). Aliases map an
        # investment vehicle / family account onto the canonical investor so
        # entity-routed buys aggregate under one person.
        pairs = [(n, n) for n in names]
        for alias, canonical in (investors_cfg.get("aliases") or {}).items():
            pairs.append((canonical, alias))
        self._marquee = [(canon, self._tokens(pattern)) for canon, pattern in pairs]

    @staticmethod
    def _tokens(s: str) -> set[str]:
        return {t for t in _TOKEN.findall((s or "").lower()) if len(t) >= 3}

    def match(self, client_name: str) -> str | None:
        """Return the watchlist name if all its tokens appear in client_name."""
        cset = self._tokens(client_name)
        if not cset:
            return None
        for name, mset in self._marquee:
            if mset and mset <= cset:
                return name
        return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse BSE dates tolerantly as IST.

    BSE mixes formats across endpoints: bulk/block deals use DD/MM/YYYY
    (day-first), while insider/SAST + some deal feeds use ISO YYYY-MM-DD
    (month-first). dayfirst MUST match the format: applying dayfirst=True to an
    ISO date swaps month/day whenever both are <=12 (e.g. 2026-06-05 -> May 6).
    Slashes => DD/MM/YYYY (day-first); dashes/ISO => month-first.
    """
    if not value:
        return None
    s = str(value).strip()
    dayfirst = "/" in s
    try:
        dt = dtparser.parse(s, dayfirst=dayfirst)
    except (ValueError, TypeError, OverflowError):
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _side(code: str | None) -> str:
    c = (code or "").strip().upper()
    if c in _BUY_CODES:
        return "BUY"
    if c in _SELL_CODES:
        return "SELL"
    if "PLEDGE" in c:
        return "PLEDGE"
    return c or "OTHER"


def _float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _dedupe_hash(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _table(resp_json: Any) -> list[dict[str, Any]]:
    """Extract the 'Table' list, tolerating double-encoding and message strings."""
    import json
    data = resp_json
    if isinstance(data, str):
        if data.strip().startswith(("{", "[")):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return []
        else:
            return []  # e.g. "No Record Found!"
    if isinstance(data, dict):
        return data.get("Table", []) or []
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #
def fetch_bulk_or_block(session: PoliteSession, deal_type: str,
                        since: datetime) -> list[dict[str, Any]]:
    """Fetch bulk (deal_type='bulk') or block ('block') deals over the window."""
    src = load_sources().get("bse", {})
    url = src["bulk_block_deals"]
    code = "1" if deal_type == "bulk" else "2"
    frm = (since - timedelta(days=1)).strftime("%d/%m/%Y")
    to = _now_ist().strftime("%d/%m/%Y")
    resp = session.bse_get(url, params={
        "DealType": code, "sc_code": "", "FDate": frm, "TDate": to}, timeout=45)
    return _table(resp.json())


def fetch_block_live(session: PoliteSession) -> list[dict[str, Any]]:
    """Fallback block-deals feed (latest trading day only, ignores params)."""
    src = load_sources().get("bse", {})
    resp = session.bse_get(src["block_deals_live"], params={}, timeout=45)
    return _table(resp.json())


def fetch_insider_sast(session: PoliteSession, since: datetime) -> list[dict[str, Any]]:
    """Fetch insider (PIT) + SAST disclosures over the window."""
    src = load_sources().get("bse", {})
    url = src["insider_sast"]
    frm = (since - timedelta(days=1)).strftime("%Y%m%d")
    to = _now_ist().strftime("%Y%m%d")
    resp = session.bse_get(url, params={
        "scripCode": "", "Regulation": "", "fromDT": frm,
        "ToDate": to, "Isdefault": "0"}, timeout=45)
    return _table(resp.json())


def fetch_nse_largedeals(session: PoliteSession) -> tuple[list[dict], list[dict]]:
    """Fetch NSE's current bulk + block deals snapshot. Returns (bulk, block).

    The snapshot covers the latest trading day(s) only (NSE's historical
    date-range endpoint is bot-blocked), so run it daily.
    """
    src = load_sources().get("nse", {})
    url = src.get("largedeals")
    if not url:
        return [], []
    resp = session.nse_get(url, timeout=30)
    data = resp.json()
    if not isinstance(data, dict):
        return [], []
    return (data.get("BULK_DEALS_DATA") or [], data.get("BLOCK_DEALS_DATA") or [])


def _normalize_nse_deal(row: dict[str, Any], deal_type: str,
                        sym_to_meta: dict[str, dict], matcher: InvestorMatcher,
                        src: dict[str, Any]) -> dict[str, Any]:
    """Map an NSE largedeal row (resolved by NSE symbol) to our deal shape."""
    symbol = (row.get("symbol") or "").strip()
    meta = sym_to_meta.get(symbol, {})
    client = (row.get("clientName") or "").strip()
    side = _side(row.get("buySell"))
    dt = _parse_dt(row.get("date"))  # "05-Jun-2026" -> month-named, parses fine
    matched = matcher.match(client)
    return {
        "deal_type": deal_type,
        "exchange": "NSE",
        "date": dt.isoformat() if dt else None,
        "bse_code": meta.get("bse_code"),
        "isin": meta.get("isin"),
        "company": meta.get("name") or row.get("name") or "",
        "symbol": symbol,
        "in_universe": bool(meta),
        "client_name": client,
        "side": side,
        "qty": _float(row.get("qty")),
        "price": _float(row.get("watp")),
        "person_category": None,
        "pct_pre": None,
        "pct_post": None,
        "is_marquee": matched is not None,
        "matched_investor": matched,
        "is_promoter_buy": False,
        "url": src.get("deals_referer", "https://www.nseindia.com/"),
        "dedupe_hash": _dedupe_hash("NSE", deal_type, symbol, client, side,
                                    row.get("qty"), row.get("date")),
        "source": "NSE",
    }


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _normalize_deal(row: dict[str, Any], deal_type: str,
                    code_to_meta: dict[str, dict], matcher: InvestorMatcher,
                    src: dict[str, Any]) -> dict[str, Any]:
    bse_code = str(row.get("SCRIP_CODE") or "").strip()
    meta = code_to_meta.get(bse_code, {})
    client = (row.get("CLIENT_NAME") or "").strip()
    side = _side(row.get("TRANSACTION_TYPE"))
    dt = _parse_dt(row.get("DEAL_DATE"))
    matched = matcher.match(client)
    page = src.get("bulk_deals_page" if deal_type == "bulk" else "block_deals_page", "")
    return {
        "deal_type": deal_type,
        "exchange": "BSE",
        "date": dt.isoformat() if dt else None,
        "bse_code": bse_code,
        "isin": meta.get("isin"),
        "company": meta.get("name") or row.get("scripname") or row.get("ScripName") or "",
        "symbol": meta.get("symbol", ""),
        "in_universe": bool(meta),
        "client_name": client,
        "side": side,
        "qty": _float(row.get("QUANTITY")),
        "price": _float(row.get("PRICE")),
        "person_category": None,
        "pct_pre": None,
        "pct_post": None,
        "is_marquee": matched is not None,
        "matched_investor": matched,
        "is_promoter_buy": False,
        "url": page,
        "dedupe_hash": _dedupe_hash(deal_type, bse_code, client, side,
                                    row.get("QUANTITY"), row.get("DEAL_DATE")),
        "source": "BSE",
    }


def _normalize_insider(row: dict[str, Any], code_to_meta: dict[str, dict],
                       matcher: InvestorMatcher, src: dict[str, Any]) -> dict[str, Any]:
    bse_code = str(row.get("Fld_ScripCode") or "").strip()
    meta = code_to_meta.get(bse_code, {})
    client = (row.get("Fld_PromoterName") or "").strip()
    category = (row.get("Fld_PersonCatgName") or "").strip()
    side = _side(row.get("Fld_TransactionType"))
    dt = _parse_dt(row.get("Fld_DateIntimation") or row.get("Fld_StampDate"))
    matched = matcher.match(client)
    is_promoter_buy = ("promoter" in category.lower()) and side == "BUY"
    # SAST disclosures use Acquisition/Disposal; tag those as deal_type 'sast'.
    raw_txn = (row.get("Fld_TransactionType") or "").strip().lower()
    deal_type = "sast" if raw_txn in ("acquisition", "disposal") else "insider"
    qty = _float(row.get("Fld_SecurityNo"))
    pct_pre = _float(row.get("Fld_PercentofShareholdingPre"))
    pct_post = _float(row.get("Fld_PercentofShareholdingPost"))
    # Dedupe on transaction CONTENT, not BSE's file id (Fld_ID): the exchange
    # sometimes reposts the same disclosure under a new XBRL file, which would
    # double-count the buy (seen live: Shah Metacorp +28pp counted twice).
    dedupe = _dedupe_hash("insider", deal_type, bse_code, client, side,
                          qty, pct_pre, pct_post,
                          dt.date().isoformat() if dt else "")
    return {
        "deal_type": deal_type,
        "exchange": "BSE",
        "date": dt.isoformat() if dt else None,
        "bse_code": bse_code,
        "isin": meta.get("isin"),
        "company": meta.get("name") or row.get("Companyname") or "",
        "symbol": meta.get("symbol", ""),
        "in_universe": bool(meta),
        "client_name": client,
        "side": side,
        "qty": qty,
        "price": None,  # not disclosed in this feed
        "person_category": category,
        "pct_pre": pct_pre,
        "pct_post": pct_post,
        "is_marquee": matched is not None,
        "matched_investor": matched,
        "is_promoter_buy": is_promoter_buy,
        "url": (row.get("xbrlurl") or "").strip() or src.get("insider_page", ""),
        "dedupe_hash": dedupe,
        "source": "BSE",
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def ingest(session: PoliteSession | None = None,
           since: datetime | None = None,
           stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Fetch all deal datasets, normalise, flag, and keep the relevant ones.

    A deal is KEPT if it is marquee-matched, a promoter buy, OR involves a
    Nifty-500 (in-universe) company. Marquee/promoter signals are kept even for
    companies outside the universe, since marquee investors often buy small/mid
    caps -- exactly the under-the-radar setups this tool is meant to surface.
    Per-source failures are isolated; `stats` (optional) is populated with
    {"total_sources", "failed_sources"} so the caller can hold back its
    catch-up cursor when a sub-feed failed (else that window is lost).
    """
    session = session or PoliteSession()
    settings = load_settings()
    lookback = int(settings.get("lookback_hours", 24))
    since = since or (_now_ist() - timedelta(hours=lookback))

    sources = load_sources()
    src = sources.get("bse", {})
    nse_src = sources.get("nse", {})
    matcher = InvestorMatcher(load_investors())
    universe = load_map()
    code_to_meta = {str(c["bse_code"]): c for c in universe if c.get("bse_code")}
    sym_to_meta = {c["symbol"]: c for c in universe if c.get("symbol")}

    out: list[dict[str, Any]] = []
    failed = 0

    for deal_type in ("bulk", "block"):
        try:
            rows = fetch_bulk_or_block(session, deal_type, since)
            for r in rows:
                out.append(_normalize_deal(r, deal_type, code_to_meta, matcher, src))
            log.info("Deals %-5s -> %d raw rows", deal_type, len(rows))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("Deals fetch failed (%s): %s", deal_type, exc)

    try:
        rows = fetch_insider_sast(session, since)
        for r in rows:
            out.append(_normalize_insider(r, code_to_meta, matcher, src))
        log.info("Insider/SAST -> %d raw rows", len(rows))
    except Exception as exc:  # noqa: BLE001
        failed += 1
        log.warning("Insider/SAST fetch failed: %s", exc)

    # NSE bulk/block (snapshot feed; resolved by NSE symbol). Isolated like the rest.
    try:
        nse_bulk, nse_block = fetch_nse_largedeals(session)
        for r in nse_bulk:
            out.append(_normalize_nse_deal(r, "bulk", sym_to_meta, matcher, nse_src))
        for r in nse_block:
            out.append(_normalize_nse_deal(r, "block", sym_to_meta, matcher, nse_src))
        log.info("NSE deals -> %d bulk + %d block raw rows", len(nse_bulk), len(nse_block))
    except Exception as exc:  # noqa: BLE001
        failed += 1
        log.warning("NSE deals fetch failed: %s", exc)

    if stats is not None:
        stats["total_sources"] = 4  # bulk, block, insider/SAST, NSE snapshot
        stats["failed_sources"] = failed

    # Keep only relevant deals (and only within the lookback window).
    # Deal dates are DATE-granular (stored as midnight IST) while the catch-up
    # cursor is a time-of-day instant, so compare at DAY level: `when < since`
    # would drop every row trade-dated today once the cursor passes midnight —
    # under the 45-min scheduled refresh that silently loses ALL deals. Keeping
    # the whole boundary day is free (dedupe hashes absorb the re-ingest).
    kept: list[dict[str, Any]] = []
    for d in out:
        if d["date"]:
            when = _parse_dt(d["date"])
            if when and when.date() < since.date():
                continue
        if d["is_marquee"] or d["is_promoter_buy"] or d["in_universe"]:
            kept.append(d)

    flagged = sum(1 for d in kept if d["is_marquee"] or d["is_promoter_buy"])
    log.info("Deals ingest: %d kept (%d flagged marquee/promoter) since %s",
             len(kept), flagged, since.isoformat())
    return kept
