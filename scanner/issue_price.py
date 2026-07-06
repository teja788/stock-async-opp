"""Parse the issue/allotment price out of capital-raise filings (#4).

The same `capital_action` tag currently covers two opposite signals: a
preferential allotment PRICED AT/ABOVE market to outside investors
(smart-money validation) and deep-discount promoter warrants (dilution).
The filing text carries the number; comparing it to the stored close makes
the two distinguishable deterministically.

Labelled heuristic, precision-first:
- Only trust explicit constructs ("issue price of Rs. X", "at a price of ₹X
  per share/warrant"); a bare rupee figure is never taken as the issue price.
- The classic trap is the FACE VALUE ("equity shares of face value of Rs. 10
  each") — any candidate preceded by face-value language is discarded.
- The caller sanity-gates the premium vs the close before displaying (a 5x
  "premium" is a parse artefact, e.g. a split ratio, not a real price).
"""
from __future__ import annotations

import re
from typing import Any

# Explicit issue-price constructs. Group 1 = the rupee figure.
_EXPLICIT_RE = re.compile(
    r"(?:issue\s+price|exercise\s+price|conversion\s+price|floor\s+price)"
    r"\s*(?:of|:|at|being|fixed\s+at)?\s*"
    r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE)

# "at a price of Rs. X ... per equity share / per warrant" fallback.
_PER_SHARE_RE = re.compile(
    r"(?:at\s+(?:a\s+)?price\s+of\s+)?(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)"
    r"\s*(?:/-)?\s*(?:each|per\s+(?:equity\s+share|share|warrant|unit))",
    re.IGNORECASE)

_FACE_VALUE_NEAR = re.compile(r"face\s+value|nominal\s+value", re.IGNORECASE)
_WARRANT_NEAR = re.compile(r"warrant", re.IGNORECASE)

# "promoter" within ~200 chars of an allotment verb. A plain proximity window
# (not a sentence-bounded one): filing text is PDF-extracted, so it is full of
# arbitrary newlines and abbreviation dots ("Rs.", "Ltd.") that would break
# any clause-boundary regex. Proximity is looser — the display wording says
# "mentioned in allotment context", not "is the allottee".
_PROMOTER_RE = re.compile(
    r"(?:allot|issue|subscri)[\s\S]{0,200}?promoter"
    r"|promoter[\s\S]{0,200}?(?:allot|warrant|subscri)",
    re.IGNORECASE)

_TOKEN = re.compile(r"[a-z0-9]+")


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _candidates(pattern: re.Pattern, text: str, *, guard: bool) -> list[float]:
    """Matches of `pattern`; with `guard`, drop any preceded by face-value talk.

    The guard applies only to the bare per-share fallback: "face value of
    Rs. 10 each" must not parse as a price. An explicit label ("issue price
    of Rs. X") disambiguates by itself — the standard sentence "shares of
    face value Rs. 10 each at an issue price of Rs. 390" has face-value
    language legitimately in front of it."""
    out: list[float] = []
    for m in pattern.finditer(text):
        if guard and _FACE_VALUE_NEAR.search(text[max(0, m.start() - 60):m.start()]):
            continue
        v = _num(m.group(1))
        if v is not None and 0.5 <= v <= 1_00_000:  # ₹0.50 .. ₹1 lakh/share
            out.append(v)
    return out


def parse_issue(text: str, marquee_matcher: Any | None = None) -> dict[str, Any] | None:
    """Extract {price, kind, explicit, promoter_allottee, marquee} or None.

    `marquee_matcher` is an ingest_deals.InvestorMatcher; when given, the
    watchlist names whose tokens ALL appear in the filing text are reported
    as `marquee` (mention-level evidence, weaker than a deal print)."""
    if not text:
        return None
    explicit = _candidates(_EXPLICIT_RE, text, guard=False)
    fallback = _candidates(_PER_SHARE_RE, text, guard=True)
    if explicit:
        # Multiple explicit figures (e.g. floor + issue price): highest wins —
        # the actual issue price is at/above the SEBI floor by construction.
        price, is_explicit = max(explicit), True
    elif fallback:
        price, is_explicit = max(fallback), False
    else:
        return None

    marquee: list[str] = []
    if marquee_matcher is not None:
        text_tokens = {t for t in _TOKEN.findall(text.lower()) if len(t) >= 3}
        for name, toks in getattr(marquee_matcher, "_marquee", []):
            if toks and toks <= text_tokens and name not in marquee:
                marquee.append(name)

    return {
        "price": price,
        "kind": "warrant" if _WARRANT_NEAR.search(text) else "share",
        "explicit": is_explicit,
        "promoter_allottee": bool(_PROMOTER_RE.search(text)),
        "marquee": marquee,
    }
