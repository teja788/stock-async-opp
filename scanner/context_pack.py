"""Context-pack assembler (Milestone 8).

Turns the pre-filtered candidate set into a compact, token-efficient packet the
reasoning agent reads: runtime/context_pack.md (human/agent-readable) plus a
.json mirror (for programmatic use / the future LLM scorer).

Hard separation of trust is structural here:
  - HARD FILINGS  -> straight from BSE (high trust)
  - INVESTOR DEALS-> disclosed bulk/block/insider (marquee/promoter flagged)
  - RATING ACTIONS-> credit-rating agencies (universe-matched)
  - NEWS          -> reputed outlets (lower trust)
Every item keeps its source link.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scanner import pdf_extract, store
from scanner.config import load_settings, resolve_path
from scanner.prefilter import run_prefilter, tag_catalysts
from scanner.universe import load_map

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

MAX_NOTE = 220          # truncate filing body text for token economy
MAX_MARKET_NEWS = 25    # cap untagged market-wide headlines


def _ist_short(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    except ValueError:
        return iso[:16]


def _isin_index() -> dict[str, dict[str, Any]]:
    return {c["isin"]: c for c in load_map()}


def _short(text: str, n: int = MAX_NOTE) -> str:
    text = (text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def build_context_pack(summary: dict[str, Any] | None = None,
                       since: datetime | None = None,
                       enrich_pdf: bool = True) -> dict[str, Any]:
    """Assemble + write the context pack. Returns paths and headline stats.

    `since` overrides the prefilter window (else settings.lookback_hours).
    `enrich_pdf` pulls PDF body text for catalyst-tagged filings (cached, bounded).
    """
    summary = summary or run_prefilter(since=since)
    cand = summary["candidates"]
    idx = _isin_index()
    settings = load_settings()

    # TODO(future hook, Section 17): prioritise/segregate watchlisted tickers.
    # The watchlist table + store.get_watchlist() exist; when wiring the UX, pull
    # store.get_watchlist() here and float those companies into a dedicated
    # "WATCHLIST" section (or boost their sort order) in the pack below.

    anns = cand["announcements"]
    news = cand["news"]
    deals = cand["deals"]

    # Order filings: catalyst-tagged first, then by recency (stable sorts).
    anns_sorted = list(anns)
    anns_sorted.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    anns_sorted.sort(key=lambda a: 0 if a.get("candidate_tags") else 1)

    conn = store.get_conn()
    try:
        # Pull PDF body text for catalyst-tagged filings, then re-tag on the
        # richer text (catches catalysts only visible inside the attachment).
        if enrich_pdf and pdf_extract.is_enabled():
            tagged_anns = [a for a in anns_sorted if a.get("candidate_tags")]
            if pdf_extract.enrich_filings(tagged_anns, conn=conn):
                _retag_enriched(tagged_anns)

        flagged_deals = [d for d in deals if d.get("is_marquee") or d.get("is_promoter_buy")]
        news = _dedupe_news_by_url(news)
        tagged_news = [n for n in news if _news_isins(n)]
        market_news = [n for n in news if not _news_isins(n)]

        ratings = store.get_recent_ratings(summary["window_since"], conn=conn)
    finally:
        conn.close()

    md = _render_md(summary, anns_sorted, flagged_deals, tagged_news, market_news, ratings, idx)
    pack_json = _render_json(summary, anns_sorted, flagged_deals, tagged_news, ratings, idx)

    md_path = resolve_path(settings.get("output", {}).get("context_pack", "runtime/context_pack.md"))
    json_path = md_path.with_suffix(".json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(pack_json, indent=2, ensure_ascii=False), encoding="utf-8")

    stats = {
        "md_path": str(md_path),
        "json_path": str(json_path),
        "filings": len(anns_sorted),
        "filings_tagged": sum(1 for a in anns_sorted if a.get("candidate_tags")),
        "investor_deals": len(flagged_deals),
        "rating_actions": len(ratings),
        "company_news": len(tagged_news),
        "market_news": len(market_news),
    }
    log.info("Context pack written: %s", stats)
    return stats


def _retag_enriched(anns: list[dict[str, Any]]) -> None:
    """Re-run catalyst tagging including the freshly-extracted PDF body text."""
    for a in anns:
        pdf = a.get("pdf_text")
        if not pdf:
            continue
        tags = tag_catalysts(a.get("category", ""), a.get("headline", ""),
                             a.get("body_text", ""), pdf)
        merged = list(dict.fromkeys((a.get("candidate_tags") or []) + tags))
        a["candidate_tags"] = merged


def _dedupe_news_by_url(news: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same article appearing under multiple feeds (same URL)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for n in news:
        url = (n.get("url") or "").strip().lower()
        key = url or f"{n.get('source','')}|{n.get('headline','')}".lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _news_isins(n: dict[str, Any]) -> list[str]:
    raw = n.get("company_isins")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw not in ("", "[]"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return []


def _mcap_str(isin: str | None, idx: dict[str, dict]) -> str:
    mcap = idx.get(isin or "", {}).get("market_cap_cr")
    if not mcap:
        return ""
    if mcap >= 1_00_000:
        return f", ₹{mcap/1_00_000:.1f}L cr"   # lakh crore
    return f", ₹{mcap:,.0f} cr"


def _co_label(rec: dict[str, Any], idx: dict[str, dict]) -> str:
    """SYMBOL (Company, BSE:code, ₹mcap) label. Market cap feeds the
    materiality-relative-to-size judgement in the rubric."""
    symbol = rec.get("symbol") or "?"
    company = rec.get("company") or ""
    bse = rec.get("bse_code") or ""
    tail = f", BSE:{bse}" if bse else ""
    return f"{symbol} ({company}{tail}{_mcap_str(rec.get('isin'), idx)})"


def _render_md(summary, anns, deals, tagged_news, market_news, ratings, idx) -> str:
    out: list[str] = []
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    out.append(f"# Context Pack — {now}")
    out.append(f"Window since: {_ist_short(summary['window_since'])}  |  Universe: Nifty 500")
    out.append(
        f"Counts: filings {len(anns)} (tagged {sum(1 for a in anns if a.get('candidate_tags'))}), "
        f"investor deals {len(deals)}, rating actions {len(ratings)}, "
        f"company news {len(tagged_news)}, market news {len(market_news)}")
    out.append("")
    out.append("> Trust order: HARD FILING > INVESTOR DEAL (disclosed) > RATING ACTION > NEWS. "
               "Research leads only — not advice.")
    out.append("")

    # --- HARD FILINGS ---
    out.append("## HARD FILINGS (high trust — BSE)")
    if not anns:
        out.append("_None in window._")
    for a in anns:
        tags = a.get("candidate_tags") or []
        tagstr = f"  | Candidate tags: [{', '.join(tags)}]" if tags else ""
        out.append(f"[HARD FILING] {_co_label(a, idx)} — {_ist_short(a.get('published_at'))}")
        out.append(f"  Category: {a.get('category','')}{tagstr}")
        out.append(f"  Headline: {_short(a.get('headline',''))}")
        note = _short(a.get("pdf_text") or a.get("body_text", ""))
        if note:
            out.append(f"  Note: {note}")
        if a.get("pdf_url"):
            out.append(f"  Source: {a['pdf_url']}")
        out.append("")

    # --- INVESTOR DEALS ---
    out.append("## INVESTOR / DEALS (disclosed bulk/block/insider — marquee & promoter)")
    if not deals:
        out.append("_None flagged in window._")
    for d in deals:
        who = d.get("matched_investor") or d.get("client_name") or "?"
        flag = "MARQUEE" if d.get("is_marquee") else ("PROMOTER" if d.get("is_promoter_buy") else "")
        qty = d.get("qty")
        qtystr = f"{qty:,.0f}" if isinstance(qty, (int, float)) else "?"
        price = d.get("price")
        pricestr = f" @ {price}" if price else ""
        pct = ""
        if d.get("pct_pre") is not None and d.get("pct_post") is not None:
            pct = f" (stake {d['pct_pre']}%→{d['pct_post']}%)"
        exch = d.get("exchange", "")
        out.append(f"[INVESTOR] {_co_label(d, idx)} — {exch} {d.get('deal_type','')} [{flag}]")
        out.append(f"  {who} {d.get('side','')} {qtystr}{pricestr}{pct}  ({_ist_short(d.get('date'))})")
        if d.get("url"):
            out.append(f"  Source: {d['url']}")
        out.append("")

    # --- RATING ACTIONS (CRA upgrades/downgrades/outlook for universe names) ---
    if ratings:
        out.append("## RATING ACTIONS (credit-rating agencies — universe-matched)")
        for r in ratings:
            out.append(f"[RATING] {r.get('symbol') or r.get('company','?')} — {r.get('agency','')} "
                       f"{(r.get('action') or '').upper()} ({r.get('direction','')})  "
                       f"({(r.get('date') or '')[:10]})")
            if r.get("summary"):
                out.append(f"  {_short(r.get('summary',''), 180)}")
            if r.get("url"):
                out.append(f"  Source: {r['url']}")
            out.append("")

    # --- COMPANY NEWS ---
    out.append("## NEWS (lower trust — reputed outlets, company-tagged)")
    if not tagged_news:
        out.append("_None in window._")
    for n in tagged_news:
        isins = _news_isins(n)
        syms = ", ".join(idx.get(i, {}).get("symbol", "?") for i in isins) or "?"
        tags = n.get("candidate_tags") or []
        tagstr = f"  | Tags: [{', '.join(tags)}]" if tags else ""
        out.append(f"[NEWS] {syms} — {_ist_short(n.get('published_at'))}  | Source: {n.get('source','')}{tagstr}")
        out.append(f"  Headline: {_short(n.get('headline',''))}")
        if n.get("url"):
            out.append(f"  Source: {n['url']}")
        out.append("")

    # --- MARKET-WIDE NEWS (titles only, capped) ---
    if market_news:
        out.append(f"## MARKET-WIDE NEWS (untagged context — showing {min(len(market_news), MAX_MARKET_NEWS)} of {len(market_news)})")
        for n in market_news[:MAX_MARKET_NEWS]:
            out.append(f"- [{n.get('source','')}] {_short(n.get('headline',''), 110)}")
        out.append("")

    return "\n".join(out)


def _render_json(summary, anns, deals, tagged_news, ratings, idx) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(IST).isoformat(),
        "window_since": summary["window_since"],
        "stats": {
            "filings": len(anns),
            "investor_deals": len(deals),
            "rating_actions": len(ratings),
            "company_news": len(tagged_news),
        },
        "hard_filings": [{
            "symbol": a.get("symbol"), "company": a.get("company"), "bse_code": a.get("bse_code"),
            "isin": a.get("isin"), "market_cap_cr": idx.get(a.get("isin") or "", {}).get("market_cap_cr"),
            "published_at": a.get("published_at"),
            "category": a.get("category"), "candidate_tags": a.get("candidate_tags") or [],
            "headline": a.get("headline"), "note": _short(a.get("pdf_text") or a.get("body_text", "")),
            "source": a.get("pdf_url"),
        } for a in anns],
        "investor_deals": [{
            "symbol": d.get("symbol"), "company": d.get("company"),
            "exchange": d.get("exchange"), "deal_type": d.get("deal_type"),
            "investor": d.get("matched_investor") or d.get("client_name"),
            "is_marquee": bool(d.get("is_marquee")), "is_promoter_buy": bool(d.get("is_promoter_buy")),
            "side": d.get("side"), "qty": d.get("qty"), "price": d.get("price"),
            "pct_pre": d.get("pct_pre"), "pct_post": d.get("pct_post"),
            "date": d.get("date"), "source": d.get("url"),
        } for d in deals],
        "rating_actions": [{
            "symbol": r.get("symbol"), "company": r.get("company"), "isin": r.get("isin"),
            "agency": r.get("agency"), "action": r.get("action"), "direction": r.get("direction"),
            "date": r.get("date"), "summary": r.get("summary"), "source": r.get("url"),
        } for r in ratings],
        "company_news": [{
            "symbols": [idx.get(i, {}).get("symbol", "?") for i in _news_isins(n)],
            "source": n.get("source"), "trust": n.get("trust"),
            "published_at": n.get("published_at"), "candidate_tags": n.get("candidate_tags") or [],
            "headline": n.get("headline"), "url": n.get("url"),
        } for n in tagged_news],
    }
