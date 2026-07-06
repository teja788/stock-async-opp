"""Turn a quarterly-results filing's PDF-extracted text into structured figures.

The rubric's two hardest gates — "material relative to size" and "earnings
surprise" — need NUMBERS, not vibes. This module reads the body text of a SEBI
"Statement of Standalone / Consolidated Financial Results" (already pulled from
the attached PDF by ``pdf_extract`` and cached in the store) and returns the
headline figures: revenue, PAT, their year-on-year deltas, and the prior full
year's revenue. It is a *labelled heuristic feeding a research pack* — the
reasoning layer treats every number as an estimate to verify — so the guiding
principle throughout is: **a WRONG number is much worse than a missing one.**
Every parse gates hard on confidence and returns ``None`` (whole result or a
single field) the moment a figure is ambiguous or fails a sanity check.

Design choices (why it looks like this)
----------------------------------------
* **Line-based label matching, never positional table parsing.** The text comes
  from pymupdf / pypdf text-layer extraction; table COLUMNS routinely arrive
  scrambled, interleaved, or wrapped onto the next line. So we locate a known
  SEBI statement label (e.g. "Revenue from operations") and read the numeric
  tokens that follow it *on that line*, rather than trusting any column grid.
* **Unit is read, never guessed.** Indian statements declare their unit once —
  "(Rs. in Lakhs)", "(₹ in Crores)", "Amount in Rs. Millions". Without an
  explicit declaration we cannot convert to ₹ crore, so we return ``None``
  outright rather than assume a unit and emit a figure off by 100x.
* **YoY only when the columns disambiguate themselves.** The SEBI quarterly
  format carries the year-ago quarter as another column in the same statement,
  but the column *order* varies by filer and the text layer may reorder it.
  We compute a YoY only when the period-header row parses into a clean set of
  dated columns AND the data line carries exactly that many numeric tokens (so
  positional mapping is safe on a line the extractor did NOT scramble). Any
  doubt → the YoY field is ``None``. We never cross units or standalone/
  consolidated sections when differencing.
* **Consolidated preferred.** When a filing carries both statements we slice to
  the consolidated section and parse only within it, so figures and their YoY
  never mix the two bases.

Known corpus limitation
-----------------------
On the *current* cached corpus (``data/catalyst.db``) this module extracts
almost nothing, by design and correctly: that snapshot holds catalyst-oriented
filings — board-meeting *intimations* ("results will be considered on <date>"),
voting results, and acquisition / credit-rating disclosures — none of which are
actual results *statements* with a revenue/PAT table. ``extract_results``
returns ``None`` on all of them (no false positives), which is the intended
behaviour. Additionally, some older filings are so badly mangled by pypdf's
text layer (headers/footers shredded into gibberish, digits split across lines)
that any results table inside them is unparseable; those also return ``None``
rather than risk a wrong number. The label/number machinery below is validated
against realistic SEBI-format fixtures in the throwaway test harness.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# --- ₹-crore multiplier per declared statement unit -------------------------
# 1 crore = 100 lakh = 10 million = 1e5 thousand.
_UNIT_TO_CR = {"lakh": 0.01, "crore": 1.0, "million": 0.1, "thousand": 0.0001}

# Converted-to-crore sanity window (see module docstring): below ₹0.01 cr is
# noise / a fragment, above ₹10 lakh cr is a parse artefact (no Indian listco
# posts a quarter that large).
_CR_MIN, _CR_MAX = 0.01, 10_00_000.0

# --- unit declaration -------------------------------------------------------
# Only ever fired by an EXPLICIT statement-level declaration. Covers the common
# spellings: "(Rs. in Lakhs)", "₹ in Crore", "Rs. In Lacs", "INR in Lakhs",
# "(Amount in Rs. Millions)", "Rupees in Thousands", "Rs. in '000".
_UNIT_WORD = {
    "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "lacs": "lakh",
    "crore": "crore", "crores": "crore", "cr": "crore",
    "million": "million", "millions": "million", "mn": "million",
    "thousand": "thousand", "thousands": "thousand", "'000": "thousand",
    "000": "thousand",
}
_UNIT_DECL_RE = re.compile(
    r"(?:₹|rs\.?|inr|rupees|amount(?:\s+in)?)\s*"       # currency / "Amount"
    r"(?:in\s+)?"                                        # optional "in"
    r"(lakhs?|lacs?|crores?|millions?|thousands?|mn|cr|'?000)\b",
    re.IGNORECASE,
)

# --- section / period labels ------------------------------------------------
_CONSOLIDATED_RE = re.compile(r"\bconsolidated\b", re.IGNORECASE)
_STANDALONE_RE = re.compile(r"\bstandalone\b", re.IGNORECASE)
# A statement header that opens a section, e.g.
# "Statement of Consolidated Unaudited Financial Results for the Quarter ...".
_STMT_HEADER_RE = re.compile(
    r"(consolidated|standalone)[^\n]{0,60}?"
    r"(?:un)?audited[^\n]{0,40}?financial\s+results",
    re.IGNORECASE,
)
# DOCUMENT-TYPE self-gate: proceed only when the text is an actual results
# *statement*, not a rating rationale / acquisition disclosure that merely
# quotes a "Total income" summary table (those lead to wrong, stale figures —
# their tables run oldest->newest). A genuine statement carries one of these
# headers. This also lets forward-looking board-meeting *intimations* through
# the header (they say "financial results for the quarter ended"), but they
# then fail for want of a unit declaration + value lines, so they still -> None.
_RESULTS_HEADER_RE = re.compile(
    r"(?:consolidated|standalone)[^\n]{0,60}?(?:un)?audited[^\n]{0,40}?financial\s+results"
    r"|(?:un)?audited\s+financial\s+results\s+for\s+the\s+(?:quarter|period|year|half)"
    r"|financial\s+results\s+for\s+the\s+(?:quarter|period|year|half)"
    r"|results\s+for\s+the\s+(?:quarter|period|year)\s+(?:and\s+\w+\s+)?ended",
    re.IGNORECASE,
)
_PERIOD_RE = re.compile(
    r"(?:quarter|period|(?:three|nine|six)\s+months?)\s+ended[^\n]{0,40}?"
    r"(\d{1,2}[.\-/\s][A-Za-z0-9.\-/\s]{2,15}\d{2,4})",
    re.IGNORECASE,
)

# --- results-statement value labels (matched on a single physical line) -----
# Revenue: prefer "revenue from operations", fall back to "total income".
_REVENUE_RE = re.compile(r"revenue\s+from\s+operations", re.IGNORECASE)
_TOTAL_INCOME_RE = re.compile(r"total\s+income", re.IGNORECASE)
# PAT: the period profit AFTER tax. Must exclude the pre-tax line, the
# comprehensive-income line, per-share lines, and the continuing/discontinued
# and attributable split lines (which are components, not the headline PAT).
_PAT_RE = re.compile(
    r"(?:net\s+)?profit"
    r"(?:\s*/?\s*\(?\s*loss\s*\)?)?\s*"       # optional "/(Loss)"
    r"(?:for\s+the\s+period|after\s+tax)",
    re.IGNORECASE,
)
_PAT_EXCLUDE_RE = re.compile(
    r"before\s+tax|before\s+exceptional|comprehensive|per\s+(?:equity\s+)?share"
    r"|continuing\s+operation|discontinued\s+operation|attributable|exceptional",
    re.IGNORECASE,
)

# --- numeric token extraction ----------------------------------------------
# Financial figure: Indian digit grouping (12,34,567.89), optional decimals,
# parenthesised or signed negatives — as they appear in a statement cell.
_NUM_RE = re.compile(r"\(\s*-?\d[\d,]*(?:\.\d+)?\s*\)|-?\d[\d,]*(?:\.\d+)?")
# Things that look numeric but are NOT figures — stripped from a line before we
# read its cells: dd.mm.yyyy dates, FY ranges (2025-26), bare years, face
# values ("Rs. 10/-"), percentages, and share/CIN-ish counts followed by "/-".
_DATE_RE = re.compile(r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\b")
_MONTHNAME_DATE_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?[\s.\-]*"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"[\s.,\-]*\d{2,4}\b",
    re.IGNORECASE,
)
_FY_RE = re.compile(r"\b(?:19|20)\d{2}\s*-\s*\d{2,4}\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b(?!\.\d)")
_FACEVAL_RE = re.compile(r"\d[\d,]*\s*/-")
_PCT_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*%")


def is_results_filing(headline: str, subcategory: str) -> bool:
    """Cheap gate: does this filing look like a financial-results *statement*?

    The caller uses this before spending a parse on ``extract_results``. It is
    deliberately loose (the real work — and the None-on-doubt discipline —
    lives in ``extract_results``) but rules out the obvious non-statements the
    BSE feed labels with the word "results": e-voting / poll results, and the
    forward-looking board-meeting *intimations / notices* that merely announce
    results are coming.
    """
    blob = f"{headline or ''} {subcategory or ''}".lower()
    if not blob.strip():
        return False
    # Obvious non-statements even though they contain "result(s)".
    if re.search(r"voting\s+result|poll\s+result|e-?voting|scrutin", blob):
        return False
    # Forward-looking pre-announcements, not the statement itself.
    if re.search(r"intimation|notice of board|schedule of|to be held|"
                 r"will be held|analyst|investor\s+meet|con(?:ference)?\s*call",
                 blob):
        return False
    return bool(
        re.search(r"financial\s+result", blob)
        or re.search(r"(?:un)?audited\s+.*result", blob)
        or re.search(r"\bresults?\b", blob)
    )


def _to_cr(raw: str, unit: str) -> float | None:
    """Parse one numeric token and convert it to ₹ crore under ``unit``."""
    mult = _UNIT_TO_CR.get(unit)
    if mult is None:
        return None
    neg = raw.strip().startswith("(") or raw.strip().startswith("-")
    digits = raw.replace("(", "").replace(")", "").replace(",", "").replace("-", "").strip()
    if not digits:
        return None
    try:
        val = float(digits)
    except ValueError:
        return None
    if neg:
        val = -val
    return round(val * mult, 3)


def _clean_line_for_numbers(segment: str) -> str:
    """Strip date/FY/year/face-value/percentage artefacts before reading cells."""
    for pat in (_DATE_RE, _MONTHNAME_DATE_RE, _FY_RE, _FACEVAL_RE, _PCT_RE, _YEAR_RE):
        segment = pat.sub(" ", segment)
    return segment


def _numbers_in(segment: str) -> list[str]:
    """Ordered raw numeric tokens in a text segment (dates etc. removed)."""
    cleaned = _clean_line_for_numbers(segment)
    return [m.group(0) for m in _NUM_RE.finditer(cleaned)]


def _detect_unit(text: str) -> tuple[str | None, bool]:
    """Return (unit, ambiguous). ambiguous=True if declarations disagree."""
    units = set()
    first: str | None = None
    for m in _UNIT_DECL_RE.finditer(text):
        token = m.group(1).lower().lstrip(".").replace(" ", "")
        unit = _UNIT_WORD.get(token)
        if unit:
            if first is None:
                first = unit
            units.add(unit)
    if not units:
        return None, False
    return first, len(units) > 1


def _detect_consolidated(text: str) -> bool | None:
    """True if consolidated statement present, False if only standalone, else None."""
    has_con = bool(_CONSOLIDATED_RE.search(text))
    has_std = bool(_STANDALONE_RE.search(text))
    if has_con:
        return True
    if has_std:
        return False
    return None


def _section_slice(text: str, consolidated: bool | None) -> str:
    """When both statements are present, return only the consolidated slice.

    A results filing often prints standalone then consolidated (or vice versa).
    We isolate the consolidated statement so figures and their YoY never mix
    bases. If we can't cleanly isolate it, return the whole text unchanged.
    """
    if not consolidated:
        return text
    headers = list(_STMT_HEADER_RE.finditer(text))
    if len(headers) < 2:
        return text
    for i, h in enumerate(headers):
        if h.group(1).lower() == "consolidated":
            start = h.start()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            return text[start:end]
    return text


def _period_label(text: str) -> str | None:
    m = _PERIOD_RE.search(text)
    if not m:
        return None
    label = re.sub(r"\s+", " ", m.group(0)).strip()
    return label[:60] if label else None


# --- column-role mapping (for YoY / FY only) --------------------------------
_HDR_DATE_RE = re.compile(
    r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b"
    r"|\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[,\s]+(\d{4})\b",
    re.IGNORECASE,
)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _parse_header_dates(header_line: str) -> list[tuple[int, int, int]]:
    """Ordered (year, month, day) tuples for each date column in a header line."""
    out: list[tuple[int, int, int]] = []
    for m in _HDR_DATE_RE.finditer(header_line):
        if m.group(1):
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            d, mo, y = int(m.group(4)), _MONTHS[m.group(5)[:3].lower()], int(m.group(6))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2099:
            out.append((y, mo, d))
    return out


def _find_period_header(lines: list[str]) -> tuple[list[tuple[int, int, int]], bool]:
    """Best period-header row: the line with the most parseable column dates.

    Returns (dates, has_year_ended) where has_year_ended flags whether the
    header text mentions a full-year column ("year ended" / "12 months").
    """
    best: list[tuple[int, int, int]] = []
    has_ye = False
    for ln in lines:
        dates = _parse_header_dates(ln)
        if len(dates) > len(best):
            best = dates
            has_ye = bool(re.search(r"year\s+ended|12\s*months|year\s+to\s+date",
                                    ln, re.IGNORECASE))
    return best, has_ye


def _classify_columns(dates: list[tuple[int, int, int]], has_ye: bool
                      ) -> tuple[int | None, int | None, int | None]:
    """Map dated columns to (current_q_idx, year_ago_q_idx, year_ended_idx).

    Only returns a role when it is UNAMBIGUOUS; otherwise that role stays None
    (wrong is worse than missing). The current quarter is the most recent date
    (and must be unique). The year-ago quarter is the column exactly one year
    earlier with the same month/day (unique). A full-year column is only mapped
    when the header says "year ended" AND there is exactly one fiscal-year-end
    date that does NOT collide with any other column — the common Q1 case, where
    the preceding-quarter column and the year-ended column both read 31 March,
    is genuinely undecidable from dates alone, so we return None there.
    """
    if not dates:
        return None, None, None
    latest = max(dates)
    if dates.count(latest) != 1:
        return None, None, None             # duplicate 'latest' -> untrustworthy
    cur_idx = dates.index(latest)
    cur = dates[cur_idx]
    ya_target = (cur[0] - 1, cur[1], cur[2])
    ya_hits = [i for i, d in enumerate(dates) if d == ya_target]
    year_ago_idx = ya_hits[0] if len(ya_hits) == 1 else None
    ye_idx = None
    if has_ye:
        # Fiscal-year-end candidates (31 March), excluding the current quarter.
        fye = [i for i, d in enumerate(dates)
               if d[1] == 3 and d[2] == 31 and i != cur_idx]
        # Must be a single column with a date value that occurs exactly once.
        if len(fye) == 1 and dates.count(dates[fye[0]]) == 1:
            ye_idx = fye[0]
    return cur_idx, year_ago_idx, ye_idx


def _line_values(lines: list[str], label_re: re.Pattern, unit: str,
                 exclude_re: re.Pattern | None = None
                 ) -> list[list[float | None]]:
    """For every line matching ``label_re`` (post-exclusion), the ordered ₹-cr
    cell values that follow the label on that line (or the next line if empty).
    """
    results: list[list[float | None]] = []
    for i, ln in enumerate(lines):
        m = label_re.search(ln)
        if not m:
            continue
        if exclude_re and exclude_re.search(ln):
            continue
        tail = ln[m.end():]
        raws = _numbers_in(tail)
        if not raws and i + 1 < len(lines):
            # Label wrapped; the cells sit on the next physical line.
            nxt = lines[i + 1]
            if not label_re.search(nxt):
                raws = _numbers_in(nxt)
        if raws:
            results.append([_to_cr(r, unit) for r in raws])
    return results


def _pick_current(rows: list[list[float | None]]) -> tuple[float | None, bool]:
    """Current-period value = first cell of the matched line. Returns
    (value, ambiguous) where ambiguous means several matched lines disagree.
    """
    firsts = [r[0] for r in rows if r and r[0] is not None]
    if not firsts:
        return None, False
    distinct = {round(v, 2) for v in firsts}
    return firsts[0], len(distinct) > 1


def _yoy(rows: list[list[float | None]], cur_idx: int | None, prev_idx: int | None,
         ncols: int) -> tuple[float | None, float | None]:
    """(current, year_ago) taken positionally — ONLY when a single matched line
    carries exactly ``ncols`` cells and both column indices are known. Any other
    shape → (None, None): we refuse to guess which cell is the comparative.
    """
    if cur_idx is None or prev_idx is None or ncols <= 0:
        return None, None
    full = [r for r in rows if len(r) == ncols]
    if len(full) != 1:
        return None, None
    row = full[0]
    cur, prev = row[cur_idx], row[prev_idx]
    if cur is None or prev is None:
        return None, None
    return cur, prev


def _pct_change(cur: float | None, prev: float | None) -> float | None:
    """YoY %; None if not computable or beyond the ±1000% sanity band."""
    if cur is None or prev is None or prev == 0:
        return None
    pct = (cur - prev) / abs(prev) * 100.0
    if abs(pct) > 1000.0:
        return None
    return round(pct, 1)


def _sane_value(v: float | None) -> bool:
    return v is not None and _CR_MIN <= abs(v) <= _CR_MAX


def extract_results(text: str) -> dict | None:
    """Extract headline figures from a results-statement's text, or ``None``.

    See the module docstring for the guarantees. Returns ``None`` unless a unit
    is declared AND at least a sane revenue or PAT figure is found; individual
    fields are ``None`` whenever they are absent or fail a sanity gate.
    """
    if not text or len(text) < 40:
        return None
    if not _RESULTS_HEADER_RE.search(text):  # not a results *statement* -> skip
        return None

    unit, unit_ambiguous = _detect_unit(text)
    if unit is None:                        # no declared unit -> never guess
        log.debug("results_extract: statement header but no declared unit; skipping")
        return None

    consolidated = _detect_consolidated(text)
    scope = _section_slice(text, consolidated)
    lines = scope.splitlines()

    # --- current-period revenue (prefer 'from operations', else total income)
    rev_rows = _line_values(lines, _REVENUE_RE, unit)
    used_income_fallback = False
    if not any(r and r[0] is not None for r in rev_rows):
        rev_rows = _line_values(lines, _TOTAL_INCOME_RE, unit)
        used_income_fallback = bool(rev_rows)
    revenue_cr, rev_ambiguous = _pick_current(rev_rows)
    if revenue_cr is not None and not (revenue_cr > 0 and _sane_value(revenue_cr)):
        revenue_cr, rev_rows = None, []      # revenue must be > 0 and in-band

    # --- current-period PAT (after tax; may be negative)
    pat_rows = _line_values(lines, _PAT_RE, unit, exclude_re=_PAT_EXCLUDE_RE)
    pat_cr, pat_ambiguous = _pick_current(pat_rows)
    if pat_cr is not None:
        if not _sane_value(pat_cr):
            pat_cr, pat_rows = None, []
        elif revenue_cr is not None and abs(pat_cr) > 3.0 * revenue_cr:
            pat_cr, pat_rows = None, []      # |PAT| <= 3x revenue

    if revenue_cr is None and pat_cr is None:
        return None                          # nothing trustworthy to report

    # --- YoY + FY, from column-role mapping (hard-gated) --------------------
    dates, has_ye = _find_period_header(lines)
    cur_idx, prev_idx, ye_idx = _classify_columns(dates, has_ye)
    ncols = len(dates)

    rev_yoy_pct = pat_yoy_pct = fy_revenue_cr = None
    if revenue_cr is not None:
        rc, rp = _yoy(rev_rows, cur_idx, prev_idx, ncols)
        rev_yoy_pct = _pct_change(rc, rp)
        if ye_idx is not None:
            full = [r for r in rev_rows if len(r) == ncols]
            if len(full) == 1:
                fy = full[0][ye_idx]
                fy_revenue_cr = fy if _sane_value(fy) else None
    if pat_cr is not None:
        pc, pp = _yoy(pat_rows, cur_idx, prev_idx, ncols)
        pat_yoy_pct = _pct_change(pc, pp)

    # --- confidence + notes ------------------------------------------------
    clean_single = (
        not unit_ambiguous
        and revenue_cr is not None and not rev_ambiguous and not used_income_fallback
        and pat_cr is not None and not pat_ambiguous
    )
    confidence = "high" if clean_single else "medium"

    matched: list[str] = []
    if revenue_cr is not None:
        matched.append("total income" if used_income_fallback else "revenue")
    if pat_cr is not None:
        matched.append("PAT")
    caveats: list[str] = []
    if unit_ambiguous:
        caveats.append("multiple unit declarations")
    if used_income_fallback:
        caveats.append("revenue via total-income fallback")
    if rev_ambiguous or pat_ambiguous:
        caveats.append("multiple matching lines")
    notes = f"unit={unit}; matched {', '.join(matched) or 'none'}"
    if caveats:
        notes += "; " + "; ".join(caveats)

    return {
        "unit": unit,
        "period_label": _period_label(scope),
        "revenue_cr": revenue_cr,
        "revenue_yoy_pct": rev_yoy_pct,
        "pat_cr": pat_cr,
        "pat_yoy_pct": pat_yoy_pct,
        "fy_revenue_cr": fy_revenue_cr,
        "consolidated": consolidated,
        "confidence": confidence,
        "notes": notes,
    }
