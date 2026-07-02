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
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner import materiality, pdf_extract, store
from scanner.config import load_settings, resolve_path
from scanner.ingest_ratings import parse_notch
from scanner.prefilter import run_prefilter, tag_catalysts
from scanner.universe import load_map

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

MAX_NOTE = 220          # truncate filing body text for token economy
MAX_MARKET_NEWS = 25    # cap untagged market-wide headlines
MAX_FILINGS = 120       # cap filings in the pack (catalyst-tagged sort first, so
                        # truncation drops the least interesting tail); wide
                        # --days windows would otherwise blow up the pack
MAX_COMPANY_NEWS = 60   # cap company-tagged news items


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
        # Value-bearing tags (orders/capex/approvals/overhangs) are enriched
        # FIRST — they feed the materiality line; coverage extends across scans
        # via the cache, so a slow/interrupted run self-heals.
        if enrich_pdf and pdf_extract.is_enabled():
            value_tags = {"order_win", "capacity_capex", "approval_patent",
                          "overhang_resolution"}
            tagged_anns = sorted(
                (a for a in anns_sorted if a.get("candidate_tags")),
                key=lambda a: 0 if value_tags & set(a.get("candidate_tags") or []) else 1)
            if pdf_extract.enrich_filings(tagged_anns, conn=conn):
                _retag_enriched(tagged_anns, conn=conn)

        flagged_deals = [d for d in deals if d.get("is_marquee") or d.get("is_promoter_buy")]
        news = _dedupe_news_by_url(news)
        tagged_news = [n for n in news if _news_isins(n)]
        market_news = [n for n in news if not _news_isins(n)]

        ratings = store.get_recent_ratings(summary["window_since"], conn=conn)

        # Enrich tagged filings with the two materiality signals: the rupee
        # value mentioned (vs mcap) and the price/volume move since publication.
        for a in anns_sorted[:MAX_FILINGS]:
            if not a.get("candidate_tags"):
                continue
            a["value_cr"] = materiality.headline_value_cr(
                a.get("headline", ""), a.get("body_text", ""), a.get("pdf_text", ""))
            if a.get("isin") and a.get("published_at"):
                a["px_ctx"] = store.price_context(a["isin"], a["published_at"], conn=conn)

        # Rating notch info (multi-notch moves / IG crossover are the re-raters).
        for r in ratings:
            r["notch"] = parse_notch(f"{r.get('rating','')} {r.get('summary','')}")

        # Confluence: companies with >=2 INDEPENDENT signal kinds in-window.
        confluence: dict[str, set[str]] = {}
        def _mark(isin: str | None, kind: str) -> None:
            if isin:
                confluence.setdefault(isin, set()).add(kind)
        for a in anns_sorted:
            if a.get("candidate_tags"):
                _mark(a.get("isin"), "tagged filing")
        for d in flagged_deals:
            _mark(d.get("isin"), "investor deal")
        for r in ratings:
            _mark(r.get("isin"), f"rating {r.get('action','')}".strip())
        for n in tagged_news:
            for i in _news_isins(n):
                _mark(i, "news")
        confluence = {i: k for i, k in confluence.items() if len(k) >= 2}

        # Insider accumulation over a trailing 90d (independent of the window).
        accum_since = (datetime.now(IST) - timedelta(days=90)).isoformat()
        accumulation = store.insider_accumulation(accum_since, conn=conn)

        watch = {w["isin"] for w in store.get_watchlist(conn=conn)}
    finally:
        conn.close()

    md = _render_md(summary, anns_sorted, flagged_deals, tagged_news, market_news,
                    ratings, idx, confluence, accumulation, watch)
    pack_json = _render_json(summary, anns_sorted, flagged_deals, tagged_news,
                             ratings, idx, confluence, accumulation)

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


def _retag_enriched(anns: list[dict[str, Any]], conn=None) -> None:
    """Re-run catalyst tagging including the freshly-extracted PDF body text.

    Persists any newly-found tags back to the store so follow-up queries
    (`ask`, announcements_by_tag) see the same tags as the pack.
    """
    for a in anns:
        pdf = a.get("pdf_text")
        if not pdf:
            continue
        tags = tag_catalysts(a.get("category", ""), a.get("headline", ""),
                             a.get("body_text", ""), pdf)
        merged = list(dict.fromkeys((a.get("candidate_tags") or []) + tags))
        if merged != (a.get("candidate_tags") or []) and a.get("id") is not None:
            store.set_announcement_tags(a["id"], merged, conn=conn)
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


def _co_label(rec: dict[str, Any], idx: dict[str, dict],
              watch: set[str] | None = None) -> str:
    """SYMBOL (Company, BSE:code, ₹mcap[, F&O]) label. Market cap feeds the
    materiality judgement; F&O marks institutional coverage (its absence on a
    smallcap = likely under-followed); ★ = user watchlist."""
    symbol = rec.get("symbol") or "?"
    company = rec.get("company") or ""
    bse = rec.get("bse_code") or ""
    isin = rec.get("isin") or ""
    tail = f", BSE:{bse}" if bse else ""
    fno = ", F&O" if idx.get(isin, {}).get("in_fno") else ""
    star = "★ " if (watch and isin in watch) else ""
    return f"{star}{symbol} ({company}{tail}{_mcap_str(rec.get('isin'), idx)}{fno})"


def _px_line(px: dict[str, Any] | None) -> str:
    """'Px since: +4.2% · vol 3.1x prior 20d' — the priced-in check."""
    if not px:
        return ""
    s = f"Px since: {px['pct_change']:+.1f}%"
    if px.get("vol_ratio"):
        s += f" · vol {px['vol_ratio']:.1f}x prior 20d"
    return s


def _render_md(summary, anns, deals, tagged_news, market_news, ratings, idx,
               confluence=None, accumulation=None, watch=None) -> str:
    confluence = confluence or {}
    accumulation = accumulation or []
    watch = watch or set()
    out: list[str] = []
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    out.append(f"# Context Pack — {now}")
    out.append(f"Window since: {_ist_short(summary['window_since'])}  |  Universe: Nifty 500 + smallcap expansion")
    out.append(
        f"Counts: filings {len(anns)} (tagged {sum(1 for a in anns if a.get('candidate_tags'))}), "
        f"investor deals {len(deals)}, rating actions {len(ratings)}, "
        f"company news {len(tagged_news)}, market news {len(market_news)}")
    out.append("")
    out.append("> Trust order: HARD FILING > INVESTOR DEAL (disclosed) > RATING ACTION > NEWS. "
               "Research leads only — not advice. 'Value ≈ % of mcap' is a regex estimate — "
               "verify in the filing. 'Px since' answers 'already priced in?'. "
               "F&O in a label = institutionally covered; its absence on a smallcap = likely under-followed.")
    out.append("")

    # --- WATCHLIST (user-pinned names with any in-window items) ---
    if watch:
        hits: dict[str, list[str]] = {}
        for a in anns:
            if a.get("isin") in watch:
                hits.setdefault(a["isin"], []).append(f"filing: {_short(a.get('headline',''), 90)}")
        for d in deals:
            if d.get("isin") in watch:
                hits.setdefault(d["isin"], []).append(f"deal: {d.get('matched_investor') or d.get('client_name','')} {d.get('side','')}")
        for r in ratings:
            if r.get("isin") in watch:
                hits.setdefault(r["isin"], []).append(f"rating: {r.get('agency','')} {r.get('action','')}")
        for n in tagged_news:
            for i in _news_isins(n):
                if i in watch:
                    hits.setdefault(i, []).append(f"news: {_short(n.get('headline',''), 90)}")
        if hits:
            out.append("## ★ WATCHLIST ACTIVITY (user-pinned)")
            for isin, items in hits.items():
                c = idx.get(isin, {})
                out.append(f"[WATCH] {c.get('symbol','?')} ({c.get('name','')}{_mcap_str(isin, idx)})")
                for it in items[:6]:
                    out.append(f"  - {it}")
                out.append("")

    # --- CONFLUENCE (>=2 independent signal kinds — the classic asymmetric setup) ---
    if confluence:
        out.append("## CONFLUENCE (multiple independent signals in-window — inspect first)")
        for isin, kinds in sorted(confluence.items(), key=lambda kv: -len(kv[1])):
            c = idx.get(isin, {})
            label = f"{c.get('symbol','?')} ({c.get('name','')}{_mcap_str(isin, idx)})"
            out.append(f"[CONFLUENCE] {label} — {' + '.join(sorted(kinds))}")
        out.append("")

    # --- INSIDER ACCUMULATION (trailing 90d aggregate; single rows are weak) ---
    if accumulation:
        out.append("## INSIDER ACCUMULATION (trailing 90d — promoter/insider BUYs aggregated)")
        for row in accumulation[:15]:
            c = idx.get(row.get("isin") or "", {})
            sym = row.get("symbol") or c.get("symbol") or "?"
            cum = row.get("cum_pct") or 0
            cross = "  [CROSSED 5% — new substantial shareholder]" if row.get("crossed_5pct") else ""
            out.append(f"[ACCUM] {sym} ({row.get('company','')}{_mcap_str(row.get('isin'), idx)}) — "
                       f"{row['n_buys']} buys, stake +{cum:.2f}pp "
                       f"({(row.get('first_buy') or '')[:10]} → {(row.get('last_buy') or '')[:10]}){cross}")
        out.append("")

    # --- HARD FILINGS ---
    shown_anns = anns[:MAX_FILINGS]
    trunc = (f" — showing {len(shown_anns)} of {len(anns)} (catalyst-tagged first)"
             if len(anns) > len(shown_anns) else "")
    out.append(f"## HARD FILINGS (high trust — BSE){trunc}")
    if not anns:
        out.append("_None in window._")
    for a in shown_anns:
        tags = a.get("candidate_tags") or []
        tagstr = f"  | Candidate tags: [{', '.join(tags)}]" if tags else ""
        out.append(f"[HARD FILING] {_co_label(a, idx, watch)} — {_ist_short(a.get('published_at'))}")
        out.append(f"  Category: {a.get('category','')}{tagstr}")
        out.append(f"  Headline: {_short(a.get('headline',''))}")
        note = _short(a.get("pdf_text") or a.get("body_text", ""))
        if note:
            out.append(f"  Note: {note}")
        mat = materiality.materiality_line(
            a.get("value_cr"), idx.get(a.get("isin") or "", {}).get("market_cap_cr"))
        px = _px_line(a.get("px_ctx"))
        if mat or px:
            out.append(f"  {'  ·  '.join(x for x in (f'Value: {mat}' if mat else '', px) if x)}")
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
        out.append(f"[INVESTOR] {_co_label(d, idx, watch)} — {exch} {d.get('deal_type','')} [{flag}]")
        out.append(f"  {who} {d.get('side','')} {qtystr}{pricestr}{pct}  ({_ist_short(d.get('date'))})")
        if d.get("url"):
            out.append(f"  Source: {d['url']}")
        out.append("")

    # --- RATING ACTIONS (CRA upgrades/downgrades/outlook for universe names) ---
    if ratings:
        out.append("## RATING ACTIONS (credit-rating agencies — universe-matched)")
        for r in ratings:
            notch = r.get("notch")
            notchstr = ""
            if notch:
                notchstr = f"  | {notch['from']}→{notch['to']} ({notch['notches']:+d} notch{'es' if abs(notch['notches']) != 1 else ''})"
                if notch.get("ig_crossover"):
                    notchstr += "  [CROSSES INTO INVESTMENT GRADE]"
            out.append(f"[RATING] {r.get('symbol') or r.get('company','?')} — {r.get('agency','')} "
                       f"{(r.get('action') or '').upper()} ({r.get('direction','')})  "
                       f"({(r.get('date') or '')[:10]}){notchstr}")
            if r.get("summary"):
                out.append(f"  {_short(r.get('summary',''), 180)}")
            if r.get("url"):
                out.append(f"  Source: {r['url']}")
            out.append("")

    # --- COMPANY NEWS ---
    shown_news = tagged_news[:MAX_COMPANY_NEWS]
    trunc = (f" — showing {len(shown_news)} of {len(tagged_news)}"
             if len(tagged_news) > len(shown_news) else "")
    out.append(f"## NEWS (lower trust — reputed outlets, company-tagged){trunc}")
    if not tagged_news:
        out.append("_None in window._")
    for n in shown_news:
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


def _render_json(summary, anns, deals, tagged_news, ratings, idx,
                 confluence=None, accumulation=None) -> dict[str, Any]:
    confluence = confluence or {}
    accumulation = accumulation or []
    return {
        "generated_at": datetime.now(IST).isoformat(),
        "window_since": summary["window_since"],
        "stats": {
            "filings": len(anns),
            "investor_deals": len(deals),
            "rating_actions": len(ratings),
            "company_news": len(tagged_news),
        },
        "confluence": [{
            "isin": i, "symbol": idx.get(i, {}).get("symbol"),
            "company": idx.get(i, {}).get("name"),
            "market_cap_cr": idx.get(i, {}).get("market_cap_cr"),
            "signals": sorted(kinds),
        } for i, kinds in sorted(confluence.items(), key=lambda kv: -len(kv[1]))],
        "insider_accumulation": [{
            "symbol": r.get("symbol"), "company": r.get("company"), "isin": r.get("isin"),
            "n_buys": r.get("n_buys"), "cum_pct": r.get("cum_pct"),
            "first_buy": r.get("first_buy"), "last_buy": r.get("last_buy"),
            "crossed_5pct": bool(r.get("crossed_5pct")),
        } for r in accumulation[:15]],
        "hard_filings": [{
            "symbol": a.get("symbol"), "company": a.get("company"), "bse_code": a.get("bse_code"),
            "isin": a.get("isin"), "market_cap_cr": idx.get(a.get("isin") or "", {}).get("market_cap_cr"),
            "in_fno": bool(idx.get(a.get("isin") or "", {}).get("in_fno")),
            "published_at": a.get("published_at"),
            "category": a.get("category"), "candidate_tags": a.get("candidate_tags") or [],
            "headline": a.get("headline"), "note": _short(a.get("pdf_text") or a.get("body_text", "")),
            "value_cr": a.get("value_cr"),
            "px_since": ({"pct_change": a["px_ctx"]["pct_change"],
                          "vol_ratio": a["px_ctx"].get("vol_ratio")}
                         if a.get("px_ctx") else None),
            "source": a.get("pdf_url"),
        } for a in anns[:MAX_FILINGS]],
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
            "notch": r.get("notch"),
            "date": r.get("date"), "summary": r.get("summary"), "source": r.get("url"),
        } for r in ratings],
        "company_news": [{
            "symbols": [idx.get(i, {}).get("symbol", "?") for i in _news_isins(n)],
            "source": n.get("source"), "trust": n.get("trust"),
            "published_at": n.get("published_at"), "candidate_tags": n.get("candidate_tags") or [],
            "headline": n.get("headline"), "url": n.get("url"),
        } for n in tagged_news[:MAX_COMPANY_NEWS]],
    }
