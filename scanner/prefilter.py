"""Deterministic pre-filter (Milestone 7).

Two jobs, both cheap and generous (recall over precision -- the reasoning layer
makes the final call):
  1. Drop / down-rank routine "noise" announcements (per config/noise_filters.yaml).
  2. Tag candidate catalyst categories on announcements + news by keyword heuristic.

It also surfaces already-flagged deals (marquee/promoter) as investor catalysts.
The surviving, tagged candidate set is what the context-pack assembler (M8) reads.

IMPORTANT: this layer does NOT judge what is asymmetric. It only buckets and
tags so the agent reads a small, well-organised packet.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from scanner import store
from scanner.config import load_noise_filters, load_settings

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Catalyst taxonomy (Section 10). Keyword substrings, matched case-insensitively
# against category + headline + body. Deliberately broad: a false tag is cheap,
# a missed catalyst is not. Edit to taste.
CATALYST_RULES: dict[str, list[str]] = {
    "capacity_capex": [
        "capacity expansion", "capacity addition", "new plant", "new facility",
        "capex", "commission", "commissioned", "commissioning", "greenfield",
        "brownfield", "mtpa", "expansion plan",
        "debottleneck", "scale up", "ramp-up", "ramp up", "new line", "incremental capacity",
    ],
    "order_win": [
        "order win", "bags order", "bagged", "secures order", "secured order",
        "work order", "purchase order", "letter of intent", "loi", "awarded",
        "contract win", "wins contract", "bags contract", "order book", "l1 bidder",
        "lowest bidder", "emerges as", "project awarded", "order worth", "order valued",
    ],
    "overhang_resolution": [
        "settlement", "settled", "nclt", "insolvency", "resolution plan",
        "debt reduction", "deleverag", "pledge release", "release of pledge",
        "revocation of pledge", "favourable order", "favorable order", "stay vacated",
        "arbitration award", "litigation", "writ petition disposed", "matter resolved",
        "one-time settlement", "debt restructuring",
    ],
    "approval_patent": [
        "usfda", "us fda", "fda", "anda", "dmf", "eu-gmp", "eugmp", "who-gmp",
        "establishment inspection report", "eir", "510(k)", "ce mark", "patent grant",
        "granted patent", "marketing authorisation", "marketing authorization",
        "drug approval", "tentative approval", "product approval", "regulatory approval",
        "cdsco", "type ii dmf",
    ],
    "rating_action": [
        "rating upgrade", "upgrades rating", "rating upgraded", "ratings upgrade",
        "credit rating", "outlook revised to positive", "rating revised upward",
        "icra upgrade", "crisil upgrade", "care upgrade", "india ratings upgrade",
    ],
    "capital_action": [
        "buyback", "buy-back", "buy back", "bonus issue", "bonus share", "stock split",
        "sub-division of shares", "demerger", "spin-off", "spin off", "value unlock",
        "qip", "qualified institution", "preferential issue", "preferential allotment",
        "fund raise", "fundraise", "rights issue", "capital raise", "raising of funds",
        "issue of warrants", "convertible warrants",
    ],
    "mgmt_change": [
        "appointed as managing director", "appointed as ceo", "new ceo", "new md",
        "appointment of chief executive", "appointment of managing director",
        "appointment of cfo", "chief executive officer", "key managerial personnel",
        "elevated to", "steps down", "resignation of",
    ],
    "jv_ma": [
        "joint venture", "jv", "partnership", "strategic alliance", "acquisition",
        "to acquire", "acquires", "acquired", "merger", "amalgamation", "stake acquisition",
        "collaboration", "mou", "memorandum of understanding", "definitive agreement",
        "share purchase agreement",
    ],
    "index_inclusion": [
        "index inclusion", "included in the index", "msci", "ftse", "index rejig",
        "added to nifty", "added to sensex", "inclusion in",
    ],
    "govt_pli": [
        "pli", "production linked incentive", "production-linked", "incentive scheme",
        "government order", "subsidy", "policy support", "tariff", "anti-dumping",
        "import duty", "budget allocation",
    ],
    "earnings_surprise": [
        "record revenue", "record profit", "highest ever", "highest-ever", "multi-fold",
        "multifold", "profit surges", "profit jumps", "revenue surges", "guidance raised",
        "raises guidance", "strong order book", "all-time high", "margin expansion",
    ],
    "investor_purchase": [  # rare in announcements; deals path is the main source
        "promoter buying", "promoter acquires", "open market purchase", "increase in stake",
        "acquisition of shares by promoter",
    ],
}


def _compile_rules(rules: dict[str, list[str]]) -> dict[str, re.Pattern]:
    """One boundary-guarded alternation regex per category.

    The (?<![a-z0-9])...(?![a-z0-9]) guards stop short keywords from matching
    inside longer words ("pli" in "compliance", "mou" in "amount").
    """
    compiled = {}
    for cat, keywords in rules.items():
        alt = "|".join(re.escape(k) for k in sorted(set(keywords), key=len, reverse=True))
        compiled[cat] = re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")
    return compiled


_COMPILED_RULES = _compile_rules(CATALYST_RULES)


def _now_ist() -> datetime:
    return datetime.now(IST)


def tag_catalysts(*texts: str) -> list[str]:
    """Return catalyst category keys whose keywords appear as whole tokens."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return []
    return [cat for cat, pat in _COMPILED_RULES.items() if pat.search(blob)]


@lru_cache(maxsize=8)
def _noise_pattern(patterns: tuple[str, ...]) -> re.Pattern | None:
    """Boundary-guarded alternation over the noise phrases.

    Bare substring matching is too greedy — "AGM" would match inside
    "diaphragm" and drop a real filing. Same guards as the catalyst rules.
    """
    items = [p.lower() for p in patterns if p]
    if not items:
        return None
    alt = "|".join(re.escape(p) for p in sorted(items, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?![a-z0-9])")


def is_noise(category: str, headline: str) -> bool:
    """True if the announcement matches a routine/administrative noise pattern."""
    pat = _noise_pattern(tuple(load_noise_filters().get("drop_or_downrank", [])))
    return bool(pat and pat.search(f"{category} {headline}".lower()))


def run_prefilter(since: datetime | None = None) -> dict[str, Any]:
    """Read the recent window from the store, tag/triage, return the candidate set.

    Side effect: persists candidate_tags back onto each announcement row.
    """
    settings = load_settings()
    lookback = int(settings.get("lookback_hours", 24))
    since = since or (_now_ist() - timedelta(hours=lookback))
    since_iso = since.isoformat()

    conn = store.get_conn()
    try:
        anns = store.get_recent_announcements(since_iso, conn=conn)
        news = store.get_recent_news(since_iso, conn=conn)
        deals = store.get_recent_deals(since_iso, conn=conn)

        # --- Announcements: tag + triage ---
        ann_candidates: list[dict[str, Any]] = []
        noise_dropped = 0
        for a in anns:
            # Include cached PDF body text (if previously extracted) so tags
            # found only inside the attachment survive re-scans — otherwise a
            # headline-only recompute would silently wipe them.
            pdf_text = ""
            if a.get("dedupe_hash"):
                cached = store.get_filing_text(a["dedupe_hash"], conn=conn)
                pdf_text = (cached or {}).get("text") or ""
            tags = tag_catalysts(a.get("category", ""), a.get("headline", ""),
                                 a.get("body_text", ""), pdf_text)
            noise = is_noise(a.get("category", ""), a.get("headline", ""))
            try:
                old_tags = json.loads(a.get("candidate_tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                old_tags = []
            a["candidate_tags"] = tags
            a["is_noise"] = noise
            if set(tags) != set(old_tags):  # skip no-op writes (commit churn)
                store.set_announcement_tags(a["id"], tags, conn=conn)
            # Keep if it carries a catalyst tag, or is not routine noise.
            if tags or not noise:
                ann_candidates.append(a)
            else:
                noise_dropped += 1

        # --- News: tag (company-tagged news is most useful, but keep all tagged) ---
        news_candidates: list[dict[str, Any]] = []
        for n in news:
            tags = tag_catalysts(n.get("headline", ""), n.get("summary", ""))
            n["candidate_tags"] = tags
            # A news item is a candidate if it maps to a company OR carries a catalyst.
            has_company = n.get("company_isins") not in (None, "", "[]")
            if has_company or tags:
                news_candidates.append(n)

        # --- Deals: the flagged ones (marquee/promoter) are investor catalysts ---
        flagged_deals = [d for d in deals if d.get("is_marquee") or d.get("is_promoter_buy")]

        summary = {
            "window_since": since_iso,
            "announcements_total": len(anns),
            "announcements_noise_dropped": noise_dropped,
            "announcements_candidates": len(ann_candidates),
            "news_total": len(news),
            "news_candidates": len(news_candidates),
            "deals_total": len(deals),
            "deals_flagged": len(flagged_deals),
            "candidates": {
                "announcements": ann_candidates,
                "news": news_candidates,
                "deals": deals,            # all kept deals (already pre-filtered in ingest)
                "flagged_deals": flagged_deals,
            },
        }
        log.info("Prefilter: ann %d->%d (noise -%d) | news %d->%d | deals %d (flagged %d)",
                 len(anns), len(ann_candidates), noise_dropped,
                 len(news), len(news_candidates), len(deals), len(flagged_deals))
        return summary
    finally:
        conn.close()
