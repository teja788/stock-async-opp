"""Credit-rating-action ingester (ICRA / CARE / CRISIL).

A rating UPGRADE/DOWNGRADE/outlook change is a classic re-rating catalyst. None
of the three CRA feeds expose ISIN or ticker, so we match the rated entity to our
universe by NORMALISED company name (precision-first: a suffix-stripped exact /
alias match) and KEEP ONLY universe-matched actions -- most rated entities are
unlisted and irrelevant here. Direction is parsed from the action text (and, for
CARE, from the rationale PDF, only for universe matches, to stay polite).

Sources (verified live 2026-06-06, plain requests + Chrome UA, no bot-wall):
  - ICRA : GET /Rating/AllRatingRationales -> server-rendered HTML, ~10 newest.
  - CARE : GET /rrcompany?companyName=%&fdate=&tdate= -> JSON {data:[...]}; PR PDFs.
  - CRISIL: monthly 'rating-actions' newsletter HTML (4-8 wk lag -> backfill).
"""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse as up
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from scanner import pdf_extract
from scanner.config import load_settings, load_sources
from scanner.http import PoliteSession
from scanner.universe import load_map

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_SUFFIX = {"ltd", "limited", "pvt", "private", "corporation", "corp", "company",
           "co", "plc", "inc", "llp", "the"}
_PUNCT = re.compile(r"[^\w\s&]")
_CRISIL_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
                  "august", "september", "october", "november", "december"]

# Direction keywords, most-important first.
_DIRECTION = [
    ("upgrade", re.compile(r"upgrad", re.I)),
    ("downgrade", re.compile(r"downgrad", re.I)),
    ("outlook", re.compile(r"outlook|revised to|revision", re.I)),
    ("watch", re.compile(r"watch", re.I)),
    ("reaffirm", re.compile(r"reaffirm|re-affirm|affirm", re.I)),
    ("assigned", re.compile(r"assign", re.I)),
    ("withdrawn", re.compile(r"withdraw", re.I)),
]
_POLARITY = {"upgrade": "positive", "downgrade": "negative", "watch": "negative"}


def _direction(text: str) -> str:
    for name, pat in _DIRECTION:
        if pat.search(text or ""):
            return name
    return "other"


def _norm_core(name: str) -> str:
    """Lower-case, strip punctuation + trailing corporate-suffix tokens."""
    cleaned = _PUNCT.sub(" ", (name or "").lower())
    tokens = re.sub(r"\s+", " ", cleaned).strip().split()
    while tokens and tokens[-1] in _SUFFIX:
        tokens.pop()
    return " ".join(tokens)


def _parse_dt(value: str | None) -> datetime | None:
    """All three feeds use month-named or ISO dates (no DD/MM/YYYY ambiguity)."""
    if not value:
        return None
    try:
        dt = dtparser.parse(str(value).strip())
    except (ValueError, TypeError, OverflowError):
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


class CompanyMatcher:
    """Precision-first name -> universe matcher (suffix-stripped exact/alias)."""

    def __init__(self, universe: list[dict[str, Any]]):
        self.by_alias: dict[str, dict[str, Any]] = {}
        for c in universe:
            for a in c.get("aliases", []) or []:
                if len(a) >= 4:
                    self.by_alias.setdefault(a, c)
            core = _norm_core(c.get("name", ""))
            if len(core) >= 4:
                self.by_alias.setdefault(core, c)

    def match(self, name: str) -> dict[str, Any] | None:
        core = _norm_core(name)
        return self.by_alias.get(core) if len(core) >= 4 else None


# --------------------------------------------------------------------------- #
# Fetchers (each returns raw {company, text, date, url} dicts)
# --------------------------------------------------------------------------- #
def fetch_icra(session: PoliteSession, src: dict[str, Any]) -> list[dict[str, Any]]:
    url = src.get("icra_rationales", "https://www.icra.in/Rating/AllRatingRationales")
    resp = session.get(url, timeout=30, headers={"Referer": "https://www.icra.in/"})
    soup = BeautifulSoup(resp.text, "lxml")
    out = []
    for row in soup.select("div.row.cpr_info.rationales_flex"):
        date_el = row.select_one(".col-2.date")
        a = row.select_one("a[href*='ShowRationaleReport']")
        text_el = row.select_one("p.tootip_con") or (a.find("p") if a else None)
        lender = row.select_one("a[href*='CompanyName=']")
        pdf = row.select_one("a[href*='GetRationalReportFilePdf']")
        company = None
        if lender and lender.get("href"):
            qs = up.parse_qs(up.urlparse(lender["href"]).query)
            company = (qs.get("CompanyName") or [None])[0]
        text = (text_el.get_text(strip=True) if text_el
                else (a.get_text(strip=True) if a else ""))
        link = ("https://www.icra.in" + a["href"]) if a and a.get("href") else None
        out.append({"company": company, "text": text,
                    "date": date_el.get_text(strip=True) if date_el else None,
                    "url": link})
    return out


def fetch_care(session: PoliteSession, src: dict[str, Any],
               since: datetime) -> list[dict[str, Any]]:
    base = src.get("care_rrcompany", "https://www.careratings.com/rrcompany")
    pdf_base = src.get("care_pdf_base", "https://www.careratings.com/upload/CompanyFiles/PR/")
    try:  # optional warm-up (sets a cookie; not required)
        session.get("https://www.careratings.com/find-ratings", timeout=20)
    except Exception:  # noqa: BLE001
        pass
    now = datetime.now(IST)
    resp = session.get(base, timeout=60, params={
        "companyName": "%", "YearID": now.year,
        "fdate": since.strftime("%Y-%m-%d"), "tdate": now.strftime("%Y-%m-%d")},
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": "https://www.careratings.com/find-ratings",
                 "Accept": "application/json"})
    parsed = resp.json()
    if not isinstance(parsed, dict):   # endpoint returns a bare int 0 under some conditions
        return []
    data = parsed.get("data", [])
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        fileurl = (d.get("FileURL") or "").strip()
        out.append({"company": d.get("CompanyName"),
                    "text": d.get("FileTitle") or "",
                    "date": (d.get("PublishedDate") or "")[:10],
                    "url": (pdf_base + fileurl) if fileurl else None})
    return out


def fetch_crisil(session: PoliteSession, src: dict[str, Any]) -> list[dict[str, Any]]:
    """Monthly newsletter: probe current + prior month (404 = not yet published).
    Tables in fixed order: Upgrades, Downgrades, Outlook Revision, Reaffirmations.
    Keep the first three (drop reaffirmation noise)."""
    tmpl = src.get("crisil_newsletter",
                   "https://www.crisilratings.com/en/home/our-business/ratings/"
                   "newsletters/{year}/{month}/rating-actions.html")
    actions = ["upgrade", "downgrade", "outlook"]   # tables[0..2]
    out: list[dict[str, Any]] = []
    now = datetime.now(IST)
    months = [(now.year, now.month), ((now - timedelta(days=31)).year, (now - timedelta(days=31)).month)]
    for year, mon in months:
        url = tmpl.format(year=year, month=_CRISIL_MONTHS[mon - 1])
        try:
            resp = session.get(url, timeout=40, headers={"Accept": "text/html"})
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status != 404:   # 404 = month not yet published (expected); else surface it
                log.warning("CRISIL fetch %s: HTTP %s", url, status)
            continue
        except Exception as exc:  # noqa: BLE001 - timeout/DNS: isolate but log, don't hide
            log.warning("CRISIL fetch %s: %s", url, exc)
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        for action, tbl in zip(actions, soup.find_all("table")):
            for tr in tbl.find_all("tr"):
                a = tr.find("a", href=True)
                if not a:
                    continue
                link = a["href"]
                m = re.search(r"_([A-Za-z]+)%20(\d{2})_%20(\d{4})_RR_(\d+)", link)
                # Fall back to the newsletter's own month so a parse miss can't
                # null the date and slip past the lookback filter downstream.
                date = (f"{m.group(1)} {int(m.group(2))}, {m.group(3)}" if m
                        else f"1 {_CRISIL_MONTHS[mon - 1]} {year}")
                out.append({"company": a.get_text(strip=True),
                            "text": f"{action} (CRISIL rating action)",
                            "date": date, "url": link, "_action": action})
    return out


# --------------------------------------------------------------------------- #
# Normalisation + orchestration
# --------------------------------------------------------------------------- #
def _normalize(raw: dict[str, Any], agency: str, matcher: CompanyMatcher) -> dict[str, Any] | None:
    company = (raw.get("company") or "").strip()
    if not company:
        return None
    meta = matcher.match(company)
    if not meta:
        return None  # keep only universe-matched rating actions
    text = raw.get("text") or ""
    action = raw.get("_action") or _direction(text)
    dt = _parse_dt(raw.get("date"))
    return {
        "agency": agency,
        "company": meta.get("name") or company,
        "isin": meta.get("isin"),
        "bse_code": meta.get("bse_code"),
        "symbol": meta.get("symbol"),
        "in_universe": True,
        "action": action,
        "direction": _POLARITY.get(action, "neutral"),
        "instrument": None,
        "rating": text[:200],
        "date": dt.isoformat() if dt else None,
        "url": raw.get("url"),
        "summary": text[:400],
        "dedupe_hash": _hash(agency, meta.get("isin"), action, raw.get("url") or text),
    }


def _maybe_pdf_direction(rows: list[dict[str, Any]], session: PoliteSession, limit: int = 8) -> None:
    """CARE listing lacks direction; pull it from the rationale PDF for the few
    universe matches (bounded + polite). Mutates rows in place."""
    if not pdf_extract.is_enabled():
        return
    done = 0
    for r in rows:
        if done >= limit:
            break
        if r["agency"] != "CARE" or r["action"] != "other" or not r.get("url"):
            continue
        text = pdf_extract.extract_url(r["url"], session)
        if text:
            r["action"] = _direction(text)
            r["direction"] = _POLARITY.get(r["action"], "neutral")
            r["summary"] = (text[:400]).strip()
            done += 1


def ingest(session: PoliteSession | None = None,
           since: datetime | None = None) -> list[dict[str, Any]]:
    """Fetch ICRA + CARE + CRISIL rating actions, keep universe-matched ones."""
    session = session or PoliteSession()
    settings = load_settings()
    lookback = int(settings.get("lookback_hours", 24))
    since = since or (datetime.now(IST) - timedelta(hours=lookback))
    src = load_sources().get("ratings", {})
    matcher = CompanyMatcher(load_map())

    raw_by_agency: list[tuple[str, list[dict[str, Any]]]] = []
    for agency, fetch in (("ICRA", lambda: fetch_icra(session, src)),
                          ("CARE", lambda: fetch_care(session, src, since)),
                          ("CRISIL", lambda: fetch_crisil(session, src))):
        try:
            rows = fetch()
            raw_by_agency.append((agency, rows))
            log.info("Ratings %-6s -> %d raw rows", agency, len(rows))
        except Exception as exc:  # noqa: BLE001 - per-source isolation
            log.warning("Ratings fetch failed (%s): %s", agency, exc)

    kept: list[dict[str, Any]] = []
    for agency, rows in raw_by_agency:
        for raw in rows:
            norm = _normalize(raw, agency, matcher)
            if not norm:
                continue
            if norm["date"]:
                when = _parse_dt(norm["date"])
                if when and when < since:
                    continue
            kept.append(norm)

    _maybe_pdf_direction(kept, session)
    log.info("Ratings ingest: %d universe-matched actions since %s", len(kept), since.isoformat())
    return kept
