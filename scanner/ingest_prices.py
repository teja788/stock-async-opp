"""Daily close/volume ingester from the free BSE bhavcopy CSV.

One CSV per trading day (CM common format, verified live 2026-07-02). Gives the
scanner its only market-data signal: "since the catalyst, has the price already
moved / has volume spiked?" — the deterministic evidence behind the rubric's
'under-appreciated / not yet priced in' gate, and the data the `review` command
uses to score past leads.

Design:
- Fetch only MISSING trade dates within `prices.lookback_days` (a 404 means
  holiday/weekend — expected, skipped, and remembered for this run only).
- Keep only universe ISINs (~1.7k of ~5k rows/day) to bound DB size.
- Prune rows older than `prices.prune_after_days`.
First run backfills ~60 files (~2 min at 1 req/s); daily runs fetch 1-3 files.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner import store
from scanner.config import load_settings, load_sources
from scanner.http import PoliteSession

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _cfg() -> dict[str, Any]:
    return load_settings().get("prices", {}) or {}


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _parse_bhavcopy(text: str, universe_isins: set[str]) -> list[tuple[str, str, float, float]]:
    """CSV -> (isin, date, close, volume) rows for universe scrips only."""
    rows: list[tuple[str, str, float, float]] = []
    for r in csv.DictReader(io.StringIO(text)):
        isin = (r.get("ISIN") or "").strip()
        if isin not in universe_isins:
            continue
        try:
            close = float(r.get("ClsPric") or 0)
            volume = float(r.get("TtlTradgVol") or 0)
        except ValueError:
            continue
        date = (r.get("TradDt") or "").strip()[:10]
        if isin and date and close > 0:
            rows.append((isin, date, close, volume))
    return rows


def ingest(session: PoliteSession | None = None,
           stats: dict[str, int] | None = None) -> int:
    """Fetch missing bhavcopy days, store closes, prune old rows.

    Returns the number of NEW price rows inserted. `stats` (optional) gets
    {"days_fetched", "days_failed"} — failed here means a non-404 error (a 404
    is a holiday, not a failure).
    """
    from scanner.universe import load_map

    session = session or PoliteSession()
    cfg = _cfg()
    lookback = int(cfg.get("lookback_days", 90))
    prune_after = int(cfg.get("prune_after_days", 200))
    url_tmpl = load_sources().get("bse", {}).get(
        "bhavcopy",
        "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{date}_F_0000.CSV")

    universe_isins = {c["isin"] for c in load_map() if c.get("isin")}
    have = store.price_dates()
    today = datetime.now(IST).date()

    # Candidate trade dates: weekdays in the lookback window we don't have yet.
    candidates = []
    for i in range(lookback):
        d = today - timedelta(days=i)
        if d.weekday() < 5 and d.isoformat() not in have:
            candidates.append(d)

    new_rows = 0
    fetched = failed = 0
    for d in sorted(candidates):
        url = url_tmpl.format(date=d.strftime("%Y%m%d"))
        try:
            resp = session.get(url, timeout=40)
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                continue  # market holiday — expected
            failed += 1
            log.warning("Bhavcopy fetch failed for %s: %s", d, exc)
            continue
        rows = _parse_bhavcopy(resp.text, universe_isins)
        new_rows += store.upsert_prices(rows)
        fetched += 1

    pruned = store.prune_prices((today - timedelta(days=prune_after)).isoformat())
    if stats is not None:
        stats["days_fetched"] = fetched
        stats["days_failed"] = failed
    log.info("Prices ingest: %d files fetched (%d failed), %d new rows, %d pruned",
             fetched, failed, new_rows, pruned)
    return new_rows
