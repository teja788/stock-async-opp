"""Parse flagged leads out of the research log and score them against prices.

Shared by the `review` CLI command and the static-site generator (publish.py).
A "lead" is any numbered `1. **TICKER — ...**` line inside a research-log
entry; scoring compares the close on the log date with the latest stored close
(bhavcopy history). Research calibration only — never presented as a
performance record.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scanner import store
from scanner.config import resolve_path

IST = ZoneInfo("Asia/Kolkata")
LOG_PATH = resolve_path("digests/research_log.md")

_ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}) \d{2}:\d{2} IST — (.+)$")
_LEAD_RE = re.compile(r"^\s*\d+\.\s+\*\*([A-Z0-9&._\-]+)\s*[—–-]")


def collect_leads() -> list[dict[str, Any]]:
    """[{date, ticker, entry_title}] for every numbered lead in the log."""
    if not LOG_PATH.exists():
        return []
    leads: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    current_date = current_title = None
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            current_date, current_title = m.group(1), m.group(2)
            continue
        m = _LEAD_RE.match(line)
        if m and current_date and (current_date, m.group(1)) not in seen:
            seen.add((current_date, m.group(1)))
            leads.append({"date": current_date, "ticker": m.group(1),
                          "entry_title": current_title})
    return leads


def score_leads(universe: list[dict[str, Any]],
                min_age_days: int = 0) -> list[dict[str, Any]]:
    """Attach then/now closes + pct move to each lead (None when unpriceable)."""
    sym_to_isin = {c["symbol"].upper(): c["isin"] for c in universe if c.get("symbol")}
    today = datetime.now(IST).strftime("%Y-%m-%d")
    out = []
    conn = store.get_conn()
    try:
        for lead in collect_leads():
            age = (datetime.strptime(today, "%Y-%m-%d")
                   - datetime.strptime(lead["date"], "%Y-%m-%d")).days
            if age < min_age_days:
                continue
            isin = sym_to_isin.get(lead["ticker"].upper())
            then = store.price_on(isin, lead["date"], conn=conn) if isin else None
            now = store.price_on(isin, today, conn=conn) if isin else None
            pct = None
            if then and now and then["close"]:
                pct = (now["close"] - then["close"]) / then["close"] * 100
            out.append({**lead, "age_days": age, "isin": isin,
                        "then_close": then["close"] if then else None,
                        "now_close": now["close"] if now else None,
                        "pct_move": pct})
    finally:
        conn.close()
    return out


def summarize(scored: list[dict[str, Any]]) -> dict[str, Any] | None:
    moves = [s["pct_move"] for s in scored if s["pct_move"] is not None]
    if not moves:
        return None
    return {
        "scored": len(moves),
        "total": len(scored),
        "positive": sum(1 for m in moves if m > 0),
        "median": sorted(moves)[len(moves) // 2],
        "average": sum(moves) / len(moves),
    }
