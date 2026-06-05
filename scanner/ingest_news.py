"""News RSS ingester + company tagging (Milestone 4).

Pulls each configured RSS feed, normalises entries, and tags each article to one
or more Nifty-500 companies using the alias table built in Milestone 2.

Tagging precision strategy (see Tagger):
- Distinctive NAME aliases (>=5 chars or multi-word, e.g. "wipro", "tata motors")
  match case-insensitively. These are unambiguous proper nouns.
- Short TICKER aliases (e.g. "OIL", "ITC", "TCS") match ONLY when written in
  uppercase, which is how tickers appear in headlines. This avoids tagging
  "oil prices" -> Oil India or "sail through" -> SAIL.

Articles that match no company are kept as market-wide context (empty isins).
Per-feed failures are isolated so one dead feed never aborts the run.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import feedparser

from scanner.config import load_sources
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Company tagging
# --------------------------------------------------------------------------- #
class Tagger:
    """Maps free text (headlines/summaries) to Nifty-500 companies via aliases.

    Three precision tiers, in increasing strictness:
      1. MULTI-WORD names ("tata motors", "bank of india") -> case-insensitive.
         Multi-token proper nouns are unambiguous.
      2. SINGLE-WORD names ("wipro", "persistent") -> must be CAPITALISED in the
         text. Proper nouns are capitalised ("Persistent Systems"); the
         common-word usage is lower-case ("persistent inflation").
      3. TICKERS ("OIL", "ITC", "TCS") -> uppercase-only token match.
    """

    _WORD_RE = re.compile(r"[A-Za-z][A-Za-z&]+")

    # Suppress a multi-word alias when it is really a fragment of a longer,
    # unrelated phrase. Key = alias, value = preceding words that signal the
    # longer phrase. "Reserve Bank of India" (RBI) is not "Bank of India" Ltd.
    _NEG_PRECEDING = {
        "bank of india": {"reserve"},
    }

    def __init__(self, universe: list[dict[str, Any]]):
        self._isin_meta = {c["isin"]: c for c in universe}
        multi_map: dict[str, str] = {}    # "tata motors" -> isin
        single_map: dict[str, str] = {}   # "wipro" -> isin (match only if Capitalised)
        ticker_map: dict[str, str] = {}   # "WIPRO" -> isin (uppercase only)

        for c in universe:
            isin = c["isin"]
            symbol = (c.get("symbol") or "").strip()
            if symbol and len(symbol) >= 2:
                ticker_map.setdefault(symbol, isin)
            for alias in c.get("aliases", []):
                if " " in alias:
                    multi_map.setdefault(alias, isin)
                elif len(alias) >= 5:
                    # Single-word distinctive name (e.g. "wipro"). If it also
                    # equals the ticker, the uppercase-ticker tier still applies.
                    single_map.setdefault(alias, isin)

        self._multi_map = multi_map
        self._single_map = single_map
        self._ticker_map = ticker_map
        self._multi_re = self._compile(multi_map.keys(), boundary_amp=False)
        self._ticker_re = self._compile(ticker_map.keys(), boundary_amp=True)

    @staticmethod
    def _compile(aliases, *, boundary_amp: bool) -> re.Pattern | None:
        items = sorted((a for a in aliases if a), key=len, reverse=True)
        if not items:
            return None
        alt = "|".join(re.escape(a) for a in items)
        lhs = r"(?<![A-Za-z0-9&])" if boundary_amp else r"(?<![A-Za-z0-9])"
        rhs = r"(?![A-Za-z0-9&])" if boundary_amp else r"(?![A-Za-z0-9])"
        return re.compile(f"{lhs}({alt}){rhs}")

    def tag(self, text: str) -> list[str]:
        """Return the list of matched company ISINs (deduped, order-stable)."""
        if not text:
            return []
        found: dict[str, None] = {}
        # 1. Multi-word names, case-insensitive.
        if self._multi_re:
            low = text.lower()
            for m in self._multi_re.finditer(low):
                alias = m.group(1)
                blockers = self._NEG_PRECEDING.get(alias)
                if blockers:
                    preceding = low[:m.start()].split()
                    if preceding and preceding[-1] in blockers:
                        continue  # e.g. "reserve bank of india" -> not the bank
                isin = self._multi_map.get(alias)
                if isin:
                    found.setdefault(isin, None)
        # 2. Single-word names, only when capitalised in the original text.
        for m in self._WORD_RE.finditer(text):
            tok = m.group(0)
            if tok[:1].isupper():
                isin = self._single_map.get(tok.lower())
                if isin:
                    found.setdefault(isin, None)
        # 3. Uppercase-only tickers.
        if self._ticker_re:
            for m in self._ticker_re.finditer(text):
                isin = self._ticker_map.get(m.group(1))
                if isin:
                    found.setdefault(isin, None)
        return list(found.keys())

    def symbol_for(self, isin: str) -> str:
        return self._isin_meta.get(isin, {}).get("symbol", "")


# --------------------------------------------------------------------------- #
# Feed parsing
# --------------------------------------------------------------------------- #
def _entry_datetime(entry: Any) -> datetime:
    """Best-effort publish time in IST; falls back to 'now' if absent."""
    for attr in ("published_parsed", "updated_parsed"):
        tm = getattr(entry, attr, None)
        if tm:
            return datetime(*tm[:6], tzinfo=timezone.utc).astimezone(IST)
    return datetime.now(IST)


def _dedupe_hash(source: str, link: str, title: str) -> str:
    """Dedupe by the article's stable identity: its URL.

    Keying on URL (not source|URL) collapses the same article carried by two
    feeds of the same publisher (e.g. ET 'Markets' and ET 'Stocks' share URLs).
    Falls back to source|title only when there is no URL.
    """
    key = (link or "").strip().lower() or f"{source}|{title}".lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def fetch_feed(session: PoliteSession, feed: dict[str, Any],
               tagger: Tagger) -> list[dict[str, Any]]:
    """Fetch + parse one feed into normalised, company-tagged news items."""
    name = feed.get("name", "?")
    url = feed.get("url")
    trust = feed.get("trust", "reputed_news")

    resp = session.get(url, timeout=30)
    parsed = feedparser.parse(resp.content)
    items: list[dict[str, Any]] = []
    for e in parsed.entries:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        summary = (getattr(e, "summary", "") or "").strip()
        published = _entry_datetime(e)
        isins = tagger.tag(f"{title}. {summary}")
        items.append({
            "source": name,
            "trust": trust,
            "headline": title,
            "url": link,
            "summary": summary,
            "published_at": published.isoformat(),
            "company_isins": isins,
            "dedupe_hash": _dedupe_hash(name, link, title),
        })
    return items


def ingest(session: PoliteSession | None = None) -> list[dict[str, Any]]:
    """Pull all configured feeds. Per-feed failures are logged and skipped."""
    session = session or PoliteSession()
    tagger = Tagger(load_map())
    feeds = load_sources().get("news_feeds", [])

    results: list[dict[str, Any]] = []
    for feed in feeds:
        try:
            items = fetch_feed(session, feed, tagger)
            tagged = sum(1 for i in items if i["company_isins"])
            log.info("News feed %-28s -> %3d items (%d tagged)",
                     feed.get("name"), len(items), tagged)
            results.extend(items)
        except Exception as exc:  # noqa: BLE001 - isolate per-feed failures
            log.warning("News feed failed (%s): %s", feed.get("name"), exc)
    return results
