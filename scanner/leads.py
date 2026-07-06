"""Parse flagged leads out of the research log and score them against prices.

Shared by the `review` CLI command and the static-site generator (publish.py).
A "lead" is any numbered `1. **TICKER — ...**` line inside a research-log
entry; scoring compares the close on the log date with the latest stored close
(bhavcopy history). Research calibration only — never presented as a
performance record.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner import store
from scanner.config import resolve_path

IST = ZoneInfo("Asia/Kolkata")
LOG_PATH = resolve_path("digests/research_log.md")

_ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}) \d{2}:\d{2} IST — (.+)$")
_LEAD_RE = re.compile(r"^\s*\d+\.\s+\*\*([A-Z0-9&._\-]+)\s*[—–-]")
# "Watch, not act" heading inside an entry: anything below it (until the next
# entry) is NOT a lead — numbered watch items must not pollute calibration.
_WATCH_RE = re.compile(r"^\s*(?:#{1,6}\s*)?\*{0,2}watch\b", re.IGNORECASE)


def collect_leads() -> list[dict[str, Any]]:
    """[{date, ticker, entry_title}] for every numbered lead in the log."""
    if not LOG_PATH.exists():
        return []
    leads: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    current_date = current_title = None
    in_watch = False
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            current_date, current_title = m.group(1), m.group(2)
            in_watch = False
            continue
        if _WATCH_RE.match(line):
            in_watch = True
            continue
        m = _LEAD_RE.match(line)
        if (m and current_date and not in_watch
                and (current_date, m.group(1)) not in seen):
            seen.add((current_date, m.group(1)))
            leads.append({"date": current_date, "ticker": m.group(1),
                          "entry_title": current_title})
    return leads


def score_leads(universe: list[dict[str, Any]],
                min_age_days: int = 0) -> list[dict[str, Any]]:
    """Attach then/now closes, pct move, benchmark-adjusted alpha, and catalyst
    tags to each lead (None where unpriceable).

    `alpha` = the lead's move minus the universe-median move over the same
    span — a bull tape shouldn't flatter every lead. `tags` are attributed
    from the company's catalyst-tagged filings in the 35 days before the log
    date (approximate: the log doesn't record which filing drove the call)."""
    sym_to_isin = {c["symbol"].upper(): c["isin"] for c in universe if c.get("symbol")}
    today = datetime.now(IST).strftime("%Y-%m-%d")
    out = []
    conn = store.get_conn()
    bench_cache: dict[str, dict[str, Any] | None] = {}
    try:
        for lead in collect_leads():
            age = (datetime.strptime(today, "%Y-%m-%d")
                   - datetime.strptime(lead["date"], "%Y-%m-%d")).days
            if age < min_age_days:
                continue
            isin = sym_to_isin.get(lead["ticker"].upper())
            then = store.price_on(isin, lead["date"], conn=conn) if isin else None
            now = store.price_on(isin, today, conn=conn) if isin else None
            pct = bench = alpha = None
            if then and now and then["close"]:
                pct = (now["close"] - then["close"]) / then["close"] * 100
                if lead["date"] not in bench_cache:
                    bench_cache[lead["date"]] = store.benchmark_move(
                        lead["date"], today, conn=conn)
                b = bench_cache[lead["date"]]
                if b:
                    bench = b["median_pct"]
                    alpha = pct - bench
            tags: list[str] = []
            if isin:
                since = (datetime.strptime(lead["date"], "%Y-%m-%d")
                         - timedelta(days=35)).strftime("%Y-%m-%d")
                tags = store.catalyst_tags_between(
                    isin, since, lead["date"] + "T23:59:59", conn=conn)
            out.append({**lead, "age_days": age, "isin": isin,
                        "then_close": then["close"] if then else None,
                        "now_close": now["close"] if now else None,
                        "pct_move": pct, "bench_pct": bench, "alpha": alpha,
                        "tags": tags})
    finally:
        conn.close()
    return out


def _median(values: list[float]) -> float:
    s, n = sorted(values), len(values)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def breakdown_by_tag(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-catalyst-tag calibration over benchmark-adjusted alpha.

    This is the loop that teaches the gating which catalyst types actually
    pay: a tag with a persistently negative median alpha should be gated
    harder, a strong one can be trusted more. Leads with no attributable
    tagged filing land in '(untagged)'."""
    buckets: dict[str, list[float]] = {}
    for s in scored:
        if s.get("alpha") is None:
            continue
        for t in (s.get("tags") or ["(untagged)"]):
            buckets.setdefault(t, []).append(s["alpha"])
    out = [{"tag": tag, "n": len(al),
            "positive": sum(1 for a in al if a > 0),
            "median_alpha": _median(al)}
           for tag, al in buckets.items()]
    out.sort(key=lambda r: -r["n"])
    return out


def summarize(scored: list[dict[str, Any]]) -> dict[str, Any] | None:
    moves = [s["pct_move"] for s in scored if s["pct_move"] is not None]
    if not moves:
        return None
    res = {
        "scored": len(moves),
        "total": len(scored),
        "positive": sum(1 for m in moves if m > 0),
        "median": _median(moves),
        "average": sum(moves) / len(moves),
    }
    alphas = [s["alpha"] for s in scored if s.get("alpha") is not None]
    if alphas:
        res["alpha_scored"] = len(alphas)
        res["alpha_positive"] = sum(1 for a in alphas if a > 0)
        res["median_alpha"] = _median(alphas)
        res["avg_alpha"] = sum(alphas) / len(alphas)
    return res
