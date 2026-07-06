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
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scanner import issue_price, materiality, pdf_extract, store
from scanner.config import load_investors, load_settings, resolve_path
from scanner.ingest_deals import InvestorMatcher
from scanner.ingest_ratings import parse_notch

# Optional enrichment modules (same graceful-degrade policy as the pdf dep):
# the pack builds fine without them, just without those lines.
try:
    from scanner import results_extract
except ImportError:  # pragma: no cover
    results_extract = None
try:
    from scanner import presentation_diff
except ImportError:  # pragma: no cover
    presentation_diff = None
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
MAX_CONFLUENCE = 25     # cap the confluence shortlist (it must stay a shortlist)


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


def _collapse_filings(anns: list[dict[str, Any]], per_company: int = 3) -> list[dict[str, Any]]:
    """Collapse repeat filings so they don't eat display slots.

    1) Exact repeats (same company + category + normalised headline — e.g. the
       same disclosure filed twice) keep one copy carrying a `collapsed_n` count.
    2) At most `per_company` TAGGED filings per company are kept for display,
       preferring ones with a parsed ₹ value, then recency; one kept row gets
       `more_in_window` so the agent knows to `ask` for the rest.
    """
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()[:80]

    deduped: list[dict[str, Any]] = []
    seen: dict[tuple, dict[str, Any]] = {}
    for a in anns:
        key = (a.get("isin") or a.get("company"), a.get("category"),
               norm(a.get("headline", "")))
        if key in seen:
            seen[key]["collapsed_n"] = seen[key].get("collapsed_n", 0) + 1
            continue
        seen[key] = a
        deduped.append(a)

    groups: dict[Any, list[dict[str, Any]]] = {}
    for a in deduped:
        if a.get("candidate_tags"):
            groups.setdefault(a.get("isin") or a.get("company"), []).append(a)
    drop: set[int] = set()
    for items in groups.values():
        if len(items) <= per_company:
            continue
        ranked = sorted(items, key=lambda a: ((a.get("value_cr") or 0),
                                              a.get("published_at") or ""), reverse=True)
        ranked[0]["more_in_window"] = len(items) - per_company
        drop.update(id(a) for a in ranked[per_company:])
    return [a for a in deduped if id(a) not in drop]


def _select_filings(anns: list[dict[str, Any]], idx: dict[str, dict],
                    cap: int = MAX_FILINGS) -> list[dict[str, Any]]:
    """Choose which filings the pack displays when the window overflows `cap`.

    Most slots go to recency (the list arrives catalyst-tagged-first, newest-
    first); a third is RESERVED for the highest value≈%-of-mcap tagged filings
    across the WHOLE window, flagged `materiality_pick` — so a wide --days
    window cannot bury a big older catalyst behind hundreds of newer routine
    filings (seen live: AMBER's 47%-of-mcap capex filing, 547 filings deep).
    """
    if len(anns) <= cap:
        return list(anns)
    reserve = cap // 3
    head = anns[:cap - reserve]
    chosen = {id(a) for a in head}

    def pct(a: dict[str, Any]) -> float:
        v = a.get("value_cr")
        m = idx.get(a.get("isin") or "", {}).get("market_cap_cr")
        return (v / m) if (v and m) else 0.0

    rest = sorted((a for a in anns[cap - reserve:]
                   if a.get("candidate_tags") and pct(a) > 0),
                  key=pct, reverse=True)
    picks = rest[:reserve]
    for a in picks:
        a["materiality_pick"] = True
        chosen.add(id(a))
    out = head + picks
    if len(out) < cap:  # few valued older filings — top back up with recency
        for a in anns[cap - reserve:]:
            if len(out) >= cap:
                break
            if id(a) not in chosen:
                out.append(a)
                chosen.add(id(a))
    out.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    out.sort(key=lambda a: 0 if a.get("candidate_tags") else 1)
    return out


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

        # Size every flagged deal: qty*price where disclosed (bulk/block);
        # insider/SAST rows carry no price, so estimate from the stored close
        # (marked `price_est` — the pack labels it an estimate).
        today = datetime.now(IST).date().isoformat()
        for d in flagged_deals:
            qty, price = d.get("qty"), d.get("price")
            if qty and not price and d.get("isin"):
                px = store.price_on(d["isin"], (d.get("date") or today)[:10], conn=conn)
                if px and px.get("close"):
                    price = px["close"]
                    d["price_est"] = True
            d["value_cr"] = (qty * price / 1e7) if (qty and price) else None

        # Only actual BUYs render as positive signals: SELLs feed the
        # trailing-30d caution overlay, and PLEDGE/REVOKE/OTHER sides must not
        # masquerade as accumulation in a section titled "BUYs".
        buy_deals = [d for d in flagged_deals if d.get("side") == "BUY"]

        news = _dedupe_news_by_url(news)
        tagged_news = [n for n in news if _news_isins(n)]
        market_news = [n for n in news if not _news_isins(n)]

        # Rating dates are date-granular (midnight IST); query with a date-only
        # prefix so boundary-day actions survive the string compare against a
        # time-of-day window start.
        ratings = store.get_recent_ratings(summary["window_since"][:10], conn=conn)

        # Rupee value (regex, cheap) for ALL tagged filings — the display
        # selection below ranks the whole window by value vs mcap, so this
        # cannot be limited to the first MAX_FILINGS.
        for a in anns_sorted:
            if a.get("candidate_tags"):
                a["value_cr"] = materiality.headline_value_cr(
                    a.get("headline", ""), a.get("body_text", ""), a.get("pdf_text", ""))

        # Collapse repeat filings, then choose what the pack displays:
        # recency fills most slots, a reserved share goes to the window's
        # highest value≈%-of-mcap tagged filings.
        collapsed = _collapse_filings(anns_sorted)
        shown_anns = _select_filings(collapsed, idx)

        # Price/volume move + attention + issue-price context — only for
        # displayed filings (a few store queries per filing).
        now_dt = datetime.now(IST)
        inv_matcher = InvestorMatcher(load_investors())
        for a in shown_anns:
            if not (a.get("candidate_tags") and a.get("isin") and a.get("published_at")):
                continue
            a["px_ctx"] = store.price_context(a["isin"], a["published_at"], conn=conn)
            # Attention check (rubric gate 3): zero news pickup on a day-old
            # catalyst filing = likely not yet widely noticed.
            a["news_pickup"] = store.news_pickup_count(a["isin"], a["published_at"], conn=conn)
            try:
                a["age_hours"] = (now_dt - datetime.fromisoformat(a["published_at"])
                                  ).total_seconds() / 3600
            except ValueError:
                a["age_hours"] = None
            # Issue-price vs market on capital raises: a premium to close is
            # smart-money validation; promoter warrants at a discount are
            # dilution — the same tag, opposite signals.
            if "capital_action" in (a.get("candidate_tags") or []):
                info = issue_price.parse_issue(
                    a.get("pdf_text") or a.get("body_text") or "", inv_matcher)
                if info:
                    px = store.price_on(a["isin"], a["published_at"][:10], conn=conn)
                    if px and px.get("close"):
                        prem = (info["price"] / px["close"] - 1) * 100
                        # Implausible premium = parse artefact (face value,
                        # ratio), not a real issue price — drop it. Fallback
                        # parses get a tighter band: SEBI ICDR floor pricing
                        # makes a >60% discount to market essentially
                        # impossible for a genuine issue.
                        lo = -95 if info["explicit"] else -60
                        if lo <= prem <= 300:
                            info["close"] = px["close"]
                            info["premium_pct"] = prem
                            a["issue_info"] = info
                    elif info["explicit"]:
                        a["issue_info"] = info  # price parsed, close unknown
            # Quantified earnings on results filings: numbers, not vibes.
            if (results_extract is not None and a.get("pdf_text")
                    and results_extract.is_results_filing(
                        a.get("headline", ""), a.get("subcategory", ""))):
                res = store.get_results_extract(a["dedupe_hash"], conn=conn)
                if res is None:
                    res = results_extract.extract_results(a["pdf_text"])
                    if res:
                        store.save_results_extract(a["dedupe_hash"], a.get("isin"),
                                                   res, conn=conn)
                if res and res.get("revenue_cr") is not None:
                    a["results"] = res
            # Guidance delta on investor presentations (best-effort).
            if (presentation_diff is not None and a.get("pdf_text")
                    and presentation_diff.is_presentation(
                        a.get("headline", ""), a.get("subcategory", ""))):
                curr = presentation_diff.guidance_lines(a["pdf_text"])
                if curr:
                    prev_text = None
                    for cand in store.filings_with_text_before(
                            a["isin"], a["published_at"], conn=conn):
                        if presentation_diff.is_presentation(
                                cand.get("headline", ""), cand.get("subcategory", "")):
                            prev_text = cand.get("text")
                            break
                    if prev_text:
                        delta = presentation_diff.diff_guidance(
                            presentation_diff.guidance_lines(prev_text), curr)
                        if delta.get("changed") or delta.get("added"):
                            a["guidance_diff"] = delta

        # Revenue-based materiality denominator (from extracted results):
        # attach the latest known FY revenue per company to displayed filings.
        fy_rev = store.latest_fy_revenues(conn=conn)
        for a in shown_anns:
            if a.get("isin") in fy_rev:
                a["fy_revenue_cr"] = fy_rev[a["isin"]]

        # Rating notch info (multi-notch moves / IG crossover are the re-raters).
        for r in ratings:
            r["notch"] = parse_notch(f"{r.get('rating','')} {r.get('summary','')}")

        # Confluence: companies with >=2 INDEPENDENT hard-signal kinds in-window.
        # News is deliberately excluded — an outlet story is usually the same
        # event as the filing (an echo, not independent confirmation), and the
        # lowest-trust source must not gate the pack's top section. Rating rows
        # count once per company, and only real moves (upgrade/downgrade);
        # outlook/reaffirm/other are rubric noise.
        confluence: dict[str, set[str]] = {}
        def _mark(isin: str | None, kind: str) -> None:
            if isin:
                confluence.setdefault(isin, set()).add(kind)
        for a in anns_sorted:
            if a.get("candidate_tags"):
                _mark(a.get("isin"), "tagged filing")
        for d in buy_deals:  # sells are a caution overlay, never confluence
            _mark(d.get("isin"), "investor deal")
        for r in ratings:
            if r.get("action") in ("upgrade", "downgrade"):
                _mark(r.get("isin"), f"rating {r['action']}")

        def _n_independent(kinds: set[str]) -> int:
            # An upgrade + a downgrade on one name is still ONE source (CRAs).
            return len({"rating" if k.startswith("rating ") else k for k in kinds})

        confluence = {i: k for i, k in confluence.items() if _n_independent(k) >= 2}

        # Insider accumulation over a trailing 90d (independent of the window),
        # gated by the hybrid significance rule (config/investors.yaml): the
        # aggregate must clear BOTH the ₹ floor AND the % of mcap ratio; a SAST
        # 5% crossing always qualifies. Value is estimated from disclosed stake
        # change x mcap, else qty x last close.
        sig = load_investors().get("significance", {}) or {}
        floor_cr = float(sig.get("floor_cr", 1.0))
        min_pct_mcap = float(sig.get("min_pct_mcap", 0.25))
        accum_since = (datetime.now(IST) - timedelta(days=90)).isoformat()
        accumulation = store.insider_accumulation(accum_since, conn=conn)
        for row in accumulation:
            isin = row.get("isin") or ""
            mcap = idx.get(isin, {}).get("market_cap_cr")
            cum_pct = row.get("cum_pct") or 0
            est = pctm = None
            if cum_pct > 0 and mcap:
                est, pctm = cum_pct / 100 * mcap, cum_pct
            elif row.get("cum_qty") and isin:
                px = store.price_on(isin, today, conn=conn)
                if px and px.get("close"):
                    est = row["cum_qty"] * px["close"] / 1e7
                    pctm = (est / mcap * 100) if mcap else None
            row["est_value_cr"] = est
            row["pct_mcap"] = pctm
            # Qualifies via: a SAST 5% crossing; the full hybrid gate; or a
            # CLUSTER (>=3 distinct insiders buying) that clears the ₹ floor —
            # many different buyers is a signal in itself, ratio waived.
            clears_floor = est is not None and est >= floor_cr
            row["significant"] = (
                bool(row.get("crossed_5pct"))
                or (clears_floor and pctm is not None and pctm >= min_pct_mcap)
                or (clears_floor and (row.get("n_buyers") or 1) >= 3))
        accum_sub_n = sum(1 for r in accumulation if not r["significant"])
        accumulation = [r for r in accumulation if r["significant"]]

        # Marquee repeat-buying over the same trailing 90d: one star investor
        # printing several deals is far stronger than the disconnected in-window
        # rows. Keep repeats, or single buys that clear the ₹ floor.
        marquee_accum = [m for m in store.marquee_accumulation(accum_since, conn=conn)
                         if (m.get("n_buys") or 0) >= 2 or (m.get("value_cr") or 0) >= floor_cr]

        # Marquee/promoter SELLs over a trailing 30d — the caution overlay.
        sell_since = (datetime.now(IST) - timedelta(days=30)).isoformat()
        selling = store.insider_selling(sell_since, conn=conn)

        # Pledge/release/invocation activity over a trailing 180d (rows are
        # date-granular, so pass a date-only floor).
        pledge_since = (datetime.now(IST) - timedelta(days=180)).date().isoformat()
        pledges = store.pledge_activity(pledge_since, conn=conn)

        watch = {w["isin"] for w in store.get_watchlist(conn=conn)}
    finally:
        conn.close()

    gate = (floor_cr, min_pct_mcap, accum_sub_n)
    md = _render_md(summary, anns_sorted, buy_deals, tagged_news, market_news,
                    ratings, idx, confluence, accumulation, watch,
                    marquee=marquee_accum, selling=selling, gate=gate,
                    shown=shown_anns, pledges=pledges)
    pack_json = _render_json(summary, anns_sorted, buy_deals, tagged_news,
                             ratings, idx, confluence, accumulation,
                             marquee=marquee_accum, selling=selling,
                             shown=shown_anns, pledges=pledges)

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
        "marquee_90d": len(marquee_accum),
        "insider_selling_30d": len(selling),
        "rating_actions": len(ratings),
        "company_news": len(tagged_news),
        "market_news": len(market_news),
        "pledge_activity_180d": len(pledges),
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
               confluence=None, accumulation=None, watch=None,
               marquee=None, selling=None, gate=None, shown=None,
               pledges=None) -> str:
    confluence = confluence or {}
    accumulation = accumulation or []
    watch = watch or set()
    marquee = marquee or []
    selling = selling or []
    pledges = pledges or []
    floor_cr, min_pct_mcap, accum_sub_n = gate or (1.0, 0.25, 0)
    shown = shown if shown is not None else anns[:MAX_FILINGS]
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
               "F&O in a label = institutionally covered; its absence on a smallcap = likely under-followed. "
               "A marquee/insider BUY is corroboration, NOT a catalyst by itself — alone it is a "
               "Watch at most; paired with a hard catalyst it is the classic setup. The SELLING "
               "section is a caution overlay: down-rank other signals on those names. "
               "'Pickup: no news coverage' on a tagged filing = not yet in the news cycle "
               "(gate-3 evidence). 'Issue px vs close': a premium placement to outside investors "
               "validates; discounted promoter warrants dilute. PLEDGE releases lean positive; "
               "new pledges/invocations are cautions.")
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
        for s in selling:
            if s.get("isin") in watch:
                hits.setdefault(s["isin"], []).append(
                    f"⚠ selling (30d): {_short(s.get('sellers') or '?', 60)} — {s.get('n_sells')} sell(s)")
        for p in pledges:
            if p.get("isin") in watch:
                hits.setdefault(p["isin"], []).append(
                    f"pledge activity (180d): {p.get('n_releases') or 0} release(s), "
                    f"{p.get('n_pledges') or 0} pledge(s), {p.get('n_invocations') or 0} invocation(s)")
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

    # --- CONFLUENCE (>=2 independent HARD signals — the classic asymmetric setup) ---
    if confluence:
        shown_conf = sorted(confluence.items(), key=lambda kv: -len(kv[1]))[:MAX_CONFLUENCE]
        out.append("## CONFLUENCE (≥2 independent HARD signals in-window — "
                   "tagged filing / investor deal / rating move; inspect first)")
        for isin, kinds in shown_conf:
            c = idx.get(isin, {})
            label = f"{c.get('symbol','?')} ({c.get('name','')}{_mcap_str(isin, idx)})"
            out.append(f"[CONFLUENCE] {label} — {' + '.join(sorted(kinds))}")
        if len(confluence) > len(shown_conf):
            out.append(f"(+{len(confluence) - len(shown_conf)} more confluence names — `ask` to list)")
        out.append("")

    # --- INSIDER ACCUMULATION (trailing 90d aggregate; single rows are weak) ---
    if accumulation or accum_sub_n:
        out.append(f"## INSIDER ACCUMULATION (trailing 90d — promoter/insider BUYs aggregated; "
                   f"gate: ≥₹{floor_cr:g} cr AND ≥{min_pct_mcap:g}% of mcap — or a 5% crossing, "
                   f"or a ≥3-insider cluster over the ₹ floor)")
        for row in accumulation[:15]:
            c = idx.get(row.get("isin") or "", {})
            sym = row.get("symbol") or c.get("symbol") or "?"
            cum = row.get("cum_pct") or 0
            n_buyers = row.get("n_buyers") or 1
            val = ""
            if row.get("est_value_cr"):
                val = f", ~₹{row['est_value_cr']:,.1f} cr"
                if row.get("pct_mcap"):
                    val += f" ≈ {row['pct_mcap']:.2f}% of mcap"
            cross = "  [CROSSED 5% — new substantial shareholder]" if row.get("crossed_5pct") else ""
            cluster = f"  [CLUSTER — {n_buyers} distinct insiders]" if n_buyers >= 3 else ""
            out.append(f"[ACCUM] {sym} ({row.get('company','')}{_mcap_str(row.get('isin'), idx)}) — "
                       f"{row['n_buys']} buys by {n_buyers} insider(s), stake +{cum:.2f}pp{val} "
                       f"({(row.get('first_buy') or '')[:10]} → {(row.get('last_buy') or '')[:10]}){cross}{cluster}")
        if accum_sub_n:
            out.append(f"(+{accum_sub_n} more compan{'ies' if accum_sub_n != 1 else 'y'} with "
                       f"sub-threshold insider buying — below the significance gate, not listed)")
        out.append("")

    # --- MARQUEE ACTIVITY (trailing 90d — star-investor repeat buying) ---
    if marquee:
        out.append("## MARQUEE ACTIVITY (trailing 90d — star-investor BUYs aggregated across days)")
        for m in marquee[:15]:
            val = f", ~₹{m['value_cr']:,.1f} cr" if m.get("value_cr") else ""
            out.append(f"[MARQUEE-90D] {m.get('matched_investor','?')} → {_co_label(m, idx, watch)} — "
                       f"{m['n_buys']} buy(s){val} "
                       f"({(m.get('first_buy') or '')[:10]} → {(m.get('last_buy') or '')[:10]})")
        out.append("")

    # --- HARD FILINGS ---
    shown_anns = shown
    trunc = (f" — showing {len(shown_anns)} of {len(anns)} (recent catalyst-tagged first "
             f"+ high value-vs-mcap picks from the full window)"
             if len(anns) > len(shown_anns) else "")
    out.append(f"## HARD FILINGS (high trust — BSE){trunc}")
    if not anns:
        out.append("_None in window._")
    for a in shown_anns:
        tags = a.get("candidate_tags") or []
        tagstr = f"  | Candidate tags: [{', '.join(tags)}]" if tags else ""
        pick = ("  [MATERIALITY PICK — older filing, high value vs mcap]"
                if a.get("materiality_pick") else "")
        out.append(f"[HARD FILING] {_co_label(a, idx, watch)} — {_ist_short(a.get('published_at'))}{pick}")
        out.append(f"  Category: {a.get('category','')}{tagstr}")
        extra = []
        if a.get("collapsed_n"):
            extra.append(f"+{a['collapsed_n']} identical re-filing(s) collapsed")
        if a.get("more_in_window"):
            extra.append(f"+{a['more_in_window']} more tagged filings from this company "
                         f"in-window — `ask` to list")
        if extra:
            out.append(f"  ({'; '.join(extra)})")
        out.append(f"  Headline: {_short(a.get('headline',''))}")
        note = _short(a.get("pdf_text") or a.get("body_text", ""))
        if note:
            out.append(f"  Note: {note}")
        mat = materiality.materiality_line(
            a.get("value_cr"), idx.get(a.get("isin") or "", {}).get("market_cap_cr"),
            a.get("fy_revenue_cr"))
        px = _px_line(a.get("px_ctx"))
        if mat or px:
            out.append(f"  {'  ·  '.join(x for x in (f'Value: {mat}' if mat else '', px) if x)}")
        pickup = a.get("news_pickup")
        if pickup is not None:
            # Zero coverage is the NORM for smallcap filings, so the marker is
            # only informative on filings that carry a real headline value —
            # there, "still uncovered" is gate-3 evidence, not background noise.
            if pickup == 0 and (a.get("age_hours") or 0) >= 24 and a.get("value_cr"):
                out.append("  Pickup: no news coverage since filing — likely under-the-radar")
            elif pickup:
                out.append(f"  Pickup: {pickup} news stor{'y' if pickup == 1 else 'ies'} since filing")
        ii = a.get("issue_info")
        if ii:
            line = f"  Issue px: ₹{ii['price']:,.2f}"
            if ii.get("kind") == "warrant":
                line += " (warrants)"
            if ii.get("premium_pct") is not None:
                word = "premium" if ii["premium_pct"] >= 0 else "discount"
                line += (f" vs close ₹{ii['close']:,.2f} → "
                         f"{abs(ii['premium_pct']):.1f}% {word}")
            if ii.get("promoter_allottee"):
                line += " · promoter mentioned in allotment context — check dilution"
            if ii.get("marquee"):
                line += f" · text mentions marquee: {', '.join(ii['marquee'][:3])}"
            if not ii.get("explicit"):
                line += " (regex estimate — verify in filing)"
            out.append(line)
        res = a.get("results")
        if res:
            bits = []
            if res.get("revenue_cr") is not None:
                b = f"Rev ₹{res['revenue_cr']:,.0f} cr"
                if res.get("revenue_yoy_pct") is not None:
                    b += f" ({res['revenue_yoy_pct']:+.0f}% YoY)"
                bits.append(b)
            if res.get("pat_cr") is not None:
                b = f"PAT ₹{res['pat_cr']:,.1f} cr"
                if res.get("pat_yoy_pct") is not None:
                    b += f" ({res['pat_yoy_pct']:+.0f}% YoY)"
                bits.append(b)
            if bits:
                scope = {True: " (consol)", False: " (standalone)"}.get(
                    res.get("consolidated"), "")
                surprise = ""
                if (res.get("pat_yoy_pct") is not None
                        and res.get("revenue_yoy_pct") is not None
                        and res["pat_yoy_pct"] >= 40 and res["revenue_yoy_pct"] >= 15):
                    surprise = "  [EARNINGS SURPRISE candidate]"
                out.append(f"  Results{scope}: {' · '.join(bits)} "
                           f"[extracted — verify]{surprise}")
        gd = a.get("guidance_diff")
        if gd:
            out.append("  Guidance delta vs previous deck (extracted — verify):")
            for prev, curr in (gd.get("changed") or [])[:3]:
                out.append(f"    ~ was: {_short(prev, 100)}")
                out.append(f"      now: {_short(curr, 100)}")
            for added in (gd.get("added") or [])[:2]:
                out.append(f"    + new: {_short(added, 100)}")
        if a.get("pdf_url"):
            out.append(f"  Source: {a['pdf_url']}")
        out.append("")

    # --- INVESTOR DEALS (buys; sells live in the caution overlay below) ---
    out.append("## INVESTOR / DEALS (disclosed bulk/block/insider BUYs — marquee & promoter)")
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
        val = d.get("value_cr")
        if val:
            mcap = idx.get(d.get("isin") or "", {}).get("market_cap_cr")
            mat = (materiality.materiality_line(val, mcap) if val >= 1
                   else f"~₹{val:.2f} cr")  # materiality_line rounds sub-crore to ₹0
            est = " (est @ last close — price not disclosed)" if d.get("price_est") else ""
            out.append(f"  Value: {mat}{est}")
        if d.get("url"):
            out.append(f"  Source: {d['url']}")
        out.append("")

    # --- SELLING (trailing 30d — caution overlay, never leads) ---
    if selling:
        out.append("## ⚠ MARQUEE / PROMOTER SELLING (trailing 30d — caution overlay, NOT leads)")
        out.append("> Down-rank other signals on these names; a lead here needs the selling explained.")
        for s in selling[:15]:
            tag = "MARQUEE" if s.get("any_marquee") else "PROMOTER"
            who = _short(s.get("sellers") or "?", 90)
            bits = [f"{s['n_sells']} sell(s) by {s.get('n_sellers') or 1} seller(s)"]
            if s.get("value_cr"):
                bits.append(f"~₹{s['value_cr']:,.1f} cr")
            if (s.get("cum_pct_sold") or 0) > 0:
                bits.append(f"stake -{s['cum_pct_sold']:.2f}pp")
            out.append(f"[SELLING] {_co_label(s, idx, watch)} — [{tag}] {who} — {', '.join(bits)}")
        out.append("")

    # --- PLEDGE ACTIVITY (trailing 180d) ---
    if pledges:
        out.append("## PLEDGE ACTIVITY (trailing 180d — a big RELEASE leans positive; "
                   "new pledges / invocations are cautions)")
        for p in pledges[:12]:
            bits = []
            if p.get("n_releases"):
                q = f" ({p['released_qty']:,.0f} sh)" if p.get("released_qty") else ""
                bits.append(f"{p['n_releases']} release(s){q}")
            if p.get("n_pledges"):
                q = f" ({p['pledged_qty']:,.0f} sh)" if p.get("pledged_qty") else ""
                bits.append(f"{p['n_pledges']} new pledge(s){q}")
            if p.get("n_invocations"):
                bits.append(f"{p['n_invocations']} INVOCATION(s) — lender-selling risk")
            tag = ("[PLEDGE-RELEASE]"
                   if p.get("n_releases") and not p.get("n_invocations")
                   and p["n_releases"] >= (p.get("n_pledges") or 0) else "[PLEDGE]")
            out.append(f"{tag} {_co_label(p, idx, watch)} — {', '.join(bits)} "
                       f"(last {(p.get('last_event') or '')[:10]})")
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
                 confluence=None, accumulation=None,
                 marquee=None, selling=None, shown=None,
                 pledges=None) -> dict[str, Any]:
    confluence = confluence or {}
    accumulation = accumulation or []
    marquee = marquee or []
    selling = selling or []
    pledges = pledges or []
    shown = shown if shown is not None else anns[:MAX_FILINGS]
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
        } for i, kinds in sorted(confluence.items(),
                                 key=lambda kv: -len(kv[1]))[:MAX_CONFLUENCE]],
        "insider_accumulation": [{
            "symbol": r.get("symbol"), "company": r.get("company"), "isin": r.get("isin"),
            "n_buys": r.get("n_buys"), "n_buyers": r.get("n_buyers"),
            "cum_pct": r.get("cum_pct"),
            "est_value_cr": r.get("est_value_cr"), "pct_mcap": r.get("pct_mcap"),
            "first_buy": r.get("first_buy"), "last_buy": r.get("last_buy"),
            "crossed_5pct": bool(r.get("crossed_5pct")),
        } for r in accumulation[:15]],
        "marquee_activity_90d": [{
            "investor": m.get("matched_investor"),
            "symbol": m.get("symbol"), "company": m.get("company"), "isin": m.get("isin"),
            "n_buys": m.get("n_buys"), "cum_qty": m.get("cum_qty"),
            "value_cr": m.get("value_cr"),
            "first_buy": m.get("first_buy"), "last_buy": m.get("last_buy"),
        } for m in marquee[:15]],
        "insider_selling_30d": [{
            "symbol": s.get("symbol"), "company": s.get("company"), "isin": s.get("isin"),
            "sellers": s.get("sellers"), "n_sells": s.get("n_sells"),
            "n_sellers": s.get("n_sellers"), "value_cr": s.get("value_cr"),
            "cum_pct_sold": s.get("cum_pct_sold"),
            "any_marquee": bool(s.get("any_marquee")),
        } for s in selling[:15]],
        "pledge_activity_180d": [{
            "symbol": p.get("symbol"), "company": p.get("company"), "isin": p.get("isin"),
            "n_pledges": p.get("n_pledges"), "n_releases": p.get("n_releases"),
            "n_invocations": p.get("n_invocations"),
            "released_qty": p.get("released_qty"), "pledged_qty": p.get("pledged_qty"),
            "last_event": p.get("last_event"),
        } for p in pledges[:12]],
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
            "materiality_pick": bool(a.get("materiality_pick")),
            "collapsed_n": a.get("collapsed_n"),
            "more_in_window": a.get("more_in_window"),
            "news_pickup": a.get("news_pickup"),
            "fy_revenue_cr": a.get("fy_revenue_cr"),
            "results": ({"period_label": a["results"].get("period_label"),
                         "revenue_cr": a["results"].get("revenue_cr"),
                         "revenue_yoy_pct": a["results"].get("revenue_yoy_pct"),
                         "pat_cr": a["results"].get("pat_cr"),
                         "pat_yoy_pct": a["results"].get("pat_yoy_pct"),
                         "fy_revenue_cr": a["results"].get("fy_revenue_cr"),
                         "consolidated": a["results"].get("consolidated"),
                         "confidence": a["results"].get("confidence")}
                        if a.get("results") else None),
            "guidance_diff": a.get("guidance_diff"),
            "issue": ({"price": a["issue_info"]["price"],
                       "kind": a["issue_info"].get("kind"),
                       "close": a["issue_info"].get("close"),
                       "premium_pct": a["issue_info"].get("premium_pct"),
                       "promoter_allottee": a["issue_info"].get("promoter_allottee"),
                       "marquee": a["issue_info"].get("marquee"),
                       "explicit": a["issue_info"].get("explicit")}
                      if a.get("issue_info") else None),
            "source": a.get("pdf_url"),
        } for a in shown],
        "investor_deals": [{
            "symbol": d.get("symbol"), "company": d.get("company"),
            "exchange": d.get("exchange"), "deal_type": d.get("deal_type"),
            "investor": d.get("matched_investor") or d.get("client_name"),
            "is_marquee": bool(d.get("is_marquee")), "is_promoter_buy": bool(d.get("is_promoter_buy")),
            "side": d.get("side"), "qty": d.get("qty"), "price": d.get("price"),
            "value_cr": d.get("value_cr"), "price_est": bool(d.get("price_est")),
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
