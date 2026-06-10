"""stock-async-opp — Streamlit dashboard.

A thin presentation layer over the existing `scanner` functions:
  - "Days back" / "Hours back" only DISPLAY stored data (zero downloading).
  - "Update" does an INCREMENTAL catch-up (only new data since the last fetch).
  - "Backfill" fetches ONLY the missing older gap, never re-downloading.
  - The AI rank/chat panels are OFF until you provide a Claude or OpenAI key
    (key held in session memory only, never written to disk).

Run:  dashboard.bat   (or  .venv\\Scripts\\streamlit run dashboard.py)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from scanner import store
from scanner.cli import _refresh_all, _resolve_companies
from scanner.config import load_settings
from scanner.context_pack import build_context_pack
from scanner.prefilter import tag_catalysts
from scanner.universe import load_map

IST = ZoneInfo("Asia/Kolkata")
st.set_page_config(page_title="stock-async-opp", page_icon="📡", layout="wide")


def _now() -> datetime:
    return datetime.now(IST)


@st.cache_data(ttl=600, show_spinner=False)
def _universe():
    return load_map()


@st.cache_data(show_spinner=False)
def _build_pack(since_iso: str, version: int):
    """Assemble the pack for a window. Cached by (window, data-version) so we don't
    rebuild on every widget interaction; `version` bumps when data changes."""
    # enrich_pdf=False keeps dashboard loads snappy (no inline PDF fetches); the
    # CLI `scan` does PDF enrichment, and extracted text is cached + reused here.
    stats = build_context_pack(since=datetime.fromisoformat(since_iso), enrich_pdf=False)
    pack = json.loads(open(stats["json_path"], encoding="utf-8").read())
    md = open(stats["md_path"], encoding="utf-8").read()
    return stats, pack, md


def _bump():
    st.session_state["data_version"] = st.session_state.get("data_version", 0) + 1
    _build_pack.clear()


store.init_db()
universe = _universe()
idx = {c["isin"]: c for c in universe}
st.session_state.setdefault("data_version", 0)
st.session_state.setdefault("chat", [])

# --------------------------------------------------------------------------- #
# Sidebar — parameters
# --------------------------------------------------------------------------- #
with st.sidebar:
    # --- AI first (most-used control), so it never needs scrolling ---
    st.header("🤖 AI engine")
    provider_label = st.radio(
        "Engine", ["Claude (Anthropic)", "OpenAI / Codex (GPT)"],
        horizontal=True, label_visibility="collapsed")
    provider = "claude" if "Claude" in provider_label else "openai"
    default_model = "claude-opus-4-8" if provider == "claude" else "gpt-5.5"
    model = st.text_input("Model", value=default_model, key=f"model_{provider}")
    api_key = st.text_input("API key", type="password",
                            help="Held in session memory only; never written to disk.")
    ai_on = bool(api_key.strip())
    st.caption("AI rank + chat enabled." if ai_on else "Add a key to enable AI rank + chat.")

    st.divider()
    st.header("⏱ Window")
    cwin = st.columns(2)
    days = cwin[0].number_input("Days back", 0, 90, 1)
    hours = cwin[1].number_input("Hours back", 0, 23, 0)
    total_h = days * 24 + hours
    if total_h == 0:
        total_h = int(load_settings().get("lookback_hours", 24))
        st.caption(f"Using settings default: {total_h}h")
    # Minute precision keeps the _build_pack cache key stable across reruns —
    # a to-the-microsecond timestamp would defeat the cache on every interaction.
    since = (_now() - timedelta(hours=total_h)).replace(second=0, microsecond=0)
    st.caption("Window only **displays** stored data — no downloading.")

    st.divider()
    st.header("🔄 Data")
    cov = store.coverage()
    a = cov["announcements"]
    st.caption(f"Filings: {a['count']} stored · oldest {(a['earliest'] or '—')[:10]}")
    st.caption(f"Deals: {cov['deals']['count']} · News: {cov['news']['count']}")

    c1, c2 = st.columns(2)
    if c1.button("Update news+deals", help="Incremental — only new data since last fetch (~30s)"):
        with st.spinner("Fetching news + deals + ratings (catch-up)..."):
            res = _refresh_all(sources={"news", "deals", "ratings"})
        _bump()
        st.success(f"news {res.get('news',{}).get('new',0)} new · deals {res.get('deals',{}).get('new',0)} new "
                   f"· ratings {res.get('ratings',{}).get('new',0)} new")
    if c2.button("+ BSE filings", help="Incremental BSE filings poll (~8 min)"):
        with st.spinner("Polling BSE filings (slow, ~8 min)..."):
            res = _refresh_all()
        _bump()
        st.success(f"filings {res.get('bse_announcements',{}).get('new',0)} new")

    # Gap-only backfill when the window reaches before stored filings.
    if a["earliest"] and since.isoformat() < a["earliest"]:
        if st.button("⤓ Backfill filings to window", help="Fetches ONLY the missing older gap"):
            from scanner import ingest_bse
            from scanner.http import PoliteSession
            gap_until = datetime.fromisoformat(a["earliest"])
            with st.spinner(f"Backfilling filings {since.date()} → {gap_until.date()} (~8 min)..."):
                workers = int(load_settings().get("bse_fetch_workers", 1))
                items = ingest_bse.ingest(session=PoliteSession(), since=since,
                                          until=gap_until, workers=workers)
                n = store.upsert_announcements(items)
            _bump()
            st.success(f"Backfilled {n} older filings.")

# --------------------------------------------------------------------------- #
# Header + pack
# --------------------------------------------------------------------------- #
st.title("📡 stock-async-opp")
st.caption("Asymmetric-opportunity scanner for Nifty-500 (BSE filings + BSE/NSE deals + news). "
           "Research leads only — **not investment advice**.")

stats, pack, md = _build_pack(since.isoformat(), st.session_state["data_version"])

m = st.columns(5)
m[0].metric("Window", f"{total_h}h")
m[1].metric("Hard filings", f"{stats['filings']}", f"{stats['filings_tagged']} tagged")
m[2].metric("Investor deals", stats["investor_deals"])
m[3].metric("Company news", stats["company_news"])
m[4].metric("Market news", stats["market_news"])

tab_sig, tab_fil, tab_deal, tab_chat = st.tabs(
    ["⚡ Signals", "📄 Filings", "💰 Deals", "💬 Chat"])

# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
with tab_sig:
    if ai_on:
        if st.button(f"🤖 Rank with AI ({provider})", type="primary"):
            from scanner.scoring import llm_scorer
            with st.spinner(f"Ranking with {model}..."):
                try:
                    ranked = llm_scorer.score(md, provider=provider, model=model, api_key=api_key)
                    st.session_state["ranked"] = ranked
                    from scanner import research_log
                    status = research_log.save(
                        ranked, title=f"Dashboard AI rank ({provider}, {total_h}h)",
                        key=f"{since.date()}|{total_h}h|{provider}|ai-rank")
                    st.caption(f"📝 Research log: {status} → digests/research_log.md")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"AI ranking failed: {exc}")
        if st.session_state.get("ranked"):
            st.markdown(st.session_state["ranked"])
            st.divider()
    else:
        st.info("Add an API key in the sidebar to get an AI-ranked signal list. "
                "Below is the deterministic candidate set (sourced).")

    st.subheader("Catalyst-tagged hard filings")
    tagged = [f for f in pack["hard_filings"] if f.get("candidate_tags")]
    if tagged:
        st.dataframe([{
            "Symbol": f["symbol"], "Mcap (₹cr)": f.get("market_cap_cr"),
            "Catalyst": ", ".join(f["candidate_tags"]), "Headline": f["headline"],
            "Source": f["source"],
        } for f in tagged], use_container_width=True, hide_index=True,
            column_config={"Source": st.column_config.LinkColumn("Source", display_text="PDF")})
    else:
        st.caption("No catalyst-tagged filings in this window.")

    st.subheader("Flagged investor / promoter deals")
    if pack["investor_deals"]:
        st.dataframe([{
            "Symbol": d["symbol"], "Exch": d.get("exchange"), "Type": d["deal_type"],
            "Flag": "MARQUEE" if d["is_marquee"] else ("PROMOTER" if d["is_promoter_buy"] else ""),
            "Investor": d["investor"], "Side": d["side"], "Qty": d["qty"],
            "Stake %": (f"{d['pct_pre']}→{d['pct_post']}" if d.get("pct_post") is not None else ""),
        } for d in pack["investor_deals"]], use_container_width=True, hide_index=True)
    else:
        st.caption("No flagged marquee/promoter deals in this window.")

# --------------------------------------------------------------------------- #
# Filings (all, filterable)
# --------------------------------------------------------------------------- #
with tab_fil:
    all_tags = sorted({t for f in pack["hard_filings"] for t in (f.get("candidate_tags") or [])})
    pick = st.multiselect("Filter by catalyst tag", all_tags)
    rows = pack["hard_filings"]
    if pick:
        rows = [f for f in rows if set(f.get("candidate_tags") or []) & set(pick)]
    st.caption(f"{len(rows)} filings")
    st.dataframe([{
        "When": (f.get("published_at") or "")[:16], "Symbol": f["symbol"],
        "Mcap (₹cr)": f.get("market_cap_cr"), "Category": f.get("category"),
        "Tags": ", ".join(f.get("candidate_tags") or []), "Headline": f["headline"],
        "Source": f["source"],
    } for f in rows], use_container_width=True, hide_index=True,
        column_config={"Source": st.column_config.LinkColumn("Source", display_text="PDF")})

# --------------------------------------------------------------------------- #
# Deals (all stored in window, BSE+NSE)
# --------------------------------------------------------------------------- #
with tab_deal:
    deals = store.get_recent_deals(since.isoformat())
    st.caption(f"{len(deals)} deals in window (BSE + NSE)")
    st.dataframe([{
        "Date": (d.get("date") or "")[:10], "Symbol": d.get("symbol") or d.get("company"),
        "Exch": d.get("exchange"), "Type": d.get("deal_type"), "Side": d.get("side"),
        "Client": d.get("client_name"), "Qty": d.get("qty"), "Price": d.get("price"),
        "Marquee": bool(d.get("is_marquee")), "PromoterBuy": bool(d.get("is_promoter_buy")),
        "Matched": d.get("matched_investor"),
    } for d in deals], use_container_width=True, hide_index=True)

# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def _link(text: str, url: str | None) -> str:
    return f"{text} [↗]({url})" if url else text


def _retrieve(q: str) -> str:
    """Deterministic retrieval (the `ask` behaviour) — relevant stored data with
    source links, rendered as Markdown. Works with no API key."""
    companies = _resolve_companies(q, universe)
    isins = [c["isin"] for c in companies]
    tags = tag_catalysts(q)
    syms = ", ".join(c["symbol"] for c in companies) or "—"
    out = [f"**Matched companies:** {syms}  ·  **Catalyst tags:** {', '.join(tags) or '—'}"]
    if isins:
        anns = store.announcements_for_isins(isins, limit=15)
        deals = store.deals_for_isins(isins, limit=15)
        news = store.news_for_isins(isins, limit=15)
        if anns:
            out.append("\n**Filings (hard, high trust):**")
            out += [f"- {a_['symbol']} · {a_.get('category')} — "
                    f"{_link(a_['headline'], a_.get('pdf_url'))}" for a_ in anns]
        if deals:
            out.append("\n**Deals:**")
            out += [f"- {d_.get('symbol')} {d_.get('exchange')} {d_.get('deal_type')} — "
                    f"{d_.get('client_name')} {d_.get('side')} {d_.get('qty')}"
                    f"{' [MARQUEE]' if d_.get('is_marquee') else ''}" for d_ in deals]
        if news:
            out.append("\n**News (lower trust):**")
            out += [f"- [{n_.get('source')}] {_link(n_['headline'], n_.get('url'))}" for n_ in news]
        if not (anns or deals or news):
            out.append("\n_No stored data for this company in the DB. Use the sidebar to Update/Backfill._")
    elif tags:
        for t in tags:
            anns = store.announcements_by_tag(t, limit=20)
            out.append(f"\n**Filings tagged `{t}`:**")
            out += [f"- {a_['symbol']} — {_link(a_['headline'], a_.get('pdf_url'))}" for a_ in anns] or ["- _none_"]
    else:
        out.append("\n_No company or catalyst recognised. Try a ticker (e.g. `GPIL`) "
                   "or a theme (e.g. `credit rating`, `acquisition`)._")
    return "\n".join(out)


with tab_chat:
    st.caption("Ask about a company or catalyst. **No key needed** — you'll get the matching "
               "stored data with sources. Add a key for an AI-written answer on top.")
    for role, text in st.session_state["chat"]:
        with st.chat_message(role):
            st.markdown(text)
    q = st.chat_input("Ask about a company or catalyst…")
    if q:
        st.session_state["chat"].append(("user", q))
        with st.chat_message("user"):
            st.markdown(q)
        retrieved = _retrieve(q)
        with st.chat_message("assistant"):
            if ai_on:
                from scanner.scoring import llm_scorer
                with st.spinner(f"Thinking with {model}…"):
                    try:
                        ans = llm_scorer.chat(q, retrieved, provider=provider, model=model, api_key=api_key)
                    except Exception as exc:  # noqa: BLE001
                        ans = f"_AI answer failed ({exc}). Showing retrieved data below._"
                final = f"{ans}\n\n---\n**Sources / retrieved data**\n\n{retrieved}"
            else:
                final = (f"{retrieved}\n\n---\n_Add an API key in the sidebar for an AI-written "
                         f"answer; or paste the above into Claude Code / Codex._")
            st.markdown(final)
        st.session_state["chat"].append(("assistant", final))
