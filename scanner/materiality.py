"""Extract rupee values from filing text and size them against market cap.

The rubric's hardest gate — "material relative to size" — needs a number, not a
vibe. This module finds money mentions (₹/Rs/INR + crore/lakh/million/billion,
plus bare "X crore" which in Indian filings is nearly always money) and reports
the LARGEST as the headline value, on the theory that an order-win/capex filing
leads with its biggest figure. It is a labelled heuristic: the pack prints it as
"~₹X cr" so the reasoning layer treats it as an estimate to verify, not a fact.

All values are normalised to ₹ crore. USD amounts are ignored (no live FX here;
a wrong conversion is worse than a missing one).
"""
from __future__ import annotations

import re

# ₹-crore multiplier per unit token.
_UNIT_CR = {
    "crore": 1.0, "crores": 1.0, "cr": 1.0, "cr.": 1.0,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
    "million": 0.1, "mn": 0.1, "mio": 0.1,
    "billion": 100.0, "bn": 100.0,
}

# Currency-prefixed amounts: "Rs. 520 crore", "₹520cr", "INR 1,200 million".
_CURRENCY_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*"
    r"(crores?|cr\.?|lakhs?|lacs?|million|mn|mio|billion|bn)\b",
    re.IGNORECASE)

# Bare "X crore(s)/lakh(s)" without a currency prefix — money in practice,
# unless it counts shares/units/warrants ("12 lakh equity shares").
_BARE_RE = re.compile(
    r"\b([\d,]+(?:\.\d+)?)\s*(crores?|lakhs?|lacs?)"
    r"(?!\s*(?:equity|shares?|units?|warrants?|sq\.?\s*ft))\b",
    re.IGNORECASE)

# Ignore USD/EUR amounts outright (see module docstring).
_FOREIGN_RE = re.compile(r"(?:usd|us\$|\$|eur|€|gbp|£)\s*[\d,]+(?:\.\d+)?", re.IGNORECASE)


def _to_cr(num: str, unit: str) -> float | None:
    mult = _UNIT_CR.get(unit.lower().rstrip("."))
    if mult is None and unit.lower().startswith("crore"):
        mult = 1.0
    if mult is None:
        return None
    try:
        return float(num.replace(",", "")) * mult
    except ValueError:
        return None


def extract_values_cr(*texts: str) -> list[float]:
    """All rupee amounts found across `texts`, in ₹ crore (deduped, desc)."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return []
    blob = _FOREIGN_RE.sub(" ", blob)
    found: set[float] = set()
    for pat in (_CURRENCY_RE, _BARE_RE):
        for m in pat.finditer(blob):
            v = _to_cr(m.group(1), m.group(2))
            # Sanity bounds: sub-₹1cr mentions are noise for materiality;
            # >₹10L cr is a parse artefact (no single order is that big).
            if v is not None and 1 <= v <= 1_000_000:
                found.add(round(v, 2))
    return sorted(found, reverse=True)


def headline_value_cr(*texts: str) -> float | None:
    """Largest rupee amount mentioned — the filing's headline figure (heuristic)."""
    values = extract_values_cr(*texts)
    return values[0] if values else None


def materiality_line(value_cr: float | None, mcap_cr: float | None) -> str:
    """Render '~₹X cr ≈ Y% of mcap' (or partial forms) for a pack line."""
    if not value_cr:
        return ""
    s = f"~₹{value_cr:,.0f} cr"
    if mcap_cr:
        pct = value_cr / mcap_cr * 100
        s += f" ≈ {pct:.0f}% of mcap" if pct >= 1 else f" ≈ {pct:.1f}% of mcap"
    return s
