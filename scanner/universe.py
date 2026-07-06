"""Build the Nifty 500 <-> BSE map, joined on ISIN.

Pipeline:
  1. Download the official NiftyIndices Nifty 500 CSV (has ISIN already).
  2. Download the BSE securities master (scrip_code + ISIN + names).
  3. Inner-join on ISIN to attach a BSE scrip code to each Nifty 500 company.
  4. Generate a small alias table per company for news tagging (Milestone 4).
  5. Persist raw caches + the merged map under data/universe/.

The merged map is the universe of everything downstream ingests/filters.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from scanner.config import load_settings, load_sources, resolve_path
from scanner.http import PoliteSession

log = logging.getLogger(__name__)

UNIVERSE_DIR = resolve_path("data/universe")

# Corporate suffix TOKENS stripped only from the END of a company name to make a
# short alias. We deliberately do NOT strip interior words like "india" — doing so
# turns "Bank of India" into "bank of", which then matches almost any headline.
_CORP_SUFFIX_TOKENS = {
    "ltd", "limited", "corporation", "corp", "company", "co", "plc", "inc",
}
_TRAILING_INDIA_STOP = {"of", "and", "the", "for", "new", "&"}
_PUNCT = re.compile(r"[^\w\s&]")


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #
def fetch_nifty500(session: PoliteSession) -> list[dict[str, str]]:
    """Return Nifty 500 rows: company, industry, symbol, series, isin."""
    src = load_sources().get("nse", {})
    url = src.get("nifty500_csv")
    # NiftyIndices is primary; the NSE archives mirror is an automatic fallback.
    candidates = [url, "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"]
    text = None
    for cand in [c for c in candidates if c]:
        try:
            text = session.get(cand, timeout=30).text
            log.info("Fetched Nifty 500 list from %s", cand)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("Nifty 500 source failed (%s): %s", cand, exc)
    if text is None:
        raise RuntimeError("Could not fetch the Nifty 500 constituent list from any source.")

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        rows.append({
            "company": (r.get("Company Name") or "").strip(),
            "industry": (r.get("Industry") or "").strip(),
            "symbol": (r.get("Symbol") or "").strip(),
            "series": (r.get("Series") or "").strip(),
            "isin": (r.get("ISIN Code") or "").strip(),
        })
    return rows


def fetch_fno_symbols(session: PoliteSession) -> set[str]:
    """NSE symbols with F&O contracts (under-coverage proxy: F&O = well covered).

    Best-effort: an empty set (fetch failed) simply omits the F&O flag.
    """
    src = load_sources().get("nse", {})
    url = src.get("fo_mktlots")
    if not url:
        return set()
    try:
        text = session.get(url, timeout=30).text
    except Exception as exc:  # noqa: BLE001 - flag is optional enrichment
        log.warning("F&O list fetch failed (flag omitted): %s", exc)
        return set()
    symbols: set[str] = set()
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2:
            sym = row[1].strip().upper()
            if sym and sym != "SYMBOL":
                symbols.add(sym)
    return symbols


def fetch_bse_master(session: PoliteSession) -> list[dict[str, Any]]:
    """Return active BSE equity securities (scrip_code, isin, names, market cap)."""
    src = load_sources().get("bse", {})
    url = src.get("scrip_master")
    params = {"Group": "", "Scripcode": "", "industry": "",
              "segment": "Equity", "status": "Active"}
    resp = session.bse_get(url, params=params, timeout=60)
    return resp.json()


# --------------------------------------------------------------------------- #
# Alias generation (for tagging news headlines to companies in Milestone 4)
# --------------------------------------------------------------------------- #
def make_aliases(company: str, symbol: str) -> list[str]:
    """Produce a few distinct lower-cased aliases for fuzzy news matching.

    Strategy (precision-first):
    - core: name with trailing corporate suffixes removed
      ("Tata Motors Ltd." -> "tata motors"; "Bank of India" stays whole).
    - india-trimmed variant: only when "<Brand> India" reduces to a single
      distinctive token ("Castrol India" -> "castrol"), never "Bank of India".
    - the ticker symbol (lower-cased).
    """
    aliases: set[str] = set()
    if company:
        cleaned = _PUNCT.sub(" ", company.lower())
        tokens = re.sub(r"\s+", " ", cleaned).strip().split()
        # Strip trailing corporate-suffix tokens iteratively.
        while tokens and tokens[-1] in _CORP_SUFFIX_TOKENS:
            tokens.pop()
        core = " ".join(tokens)
        if len(core) >= 3:
            aliases.add(core)
        # "Castrol India" -> "castrol", but NOT "Bank of India" -> "bank of".
        if core.endswith(" india"):
            head = core[:-len(" india")].strip()
            head_tokens = head.split()
            if (head and len(head) >= 5
                    and head_tokens[-1] not in _TRAILING_INDIA_STOP):
                aliases.add(head)
    if symbol:
        aliases.add(symbol.lower())
    return sorted(a for a in aliases if len(a) >= 3)


# --------------------------------------------------------------------------- #
# Join
# --------------------------------------------------------------------------- #
def _parse_market_cap(row: dict[str, Any]) -> float | None:
    """BSE rows sometimes carry a market-cap field under varying keys."""
    for key in ("Mktcap_crore", "Mktcap", "MktCap", "MARKET_CAP", "Mktcap_cr"):
        val = row.get(key)
        if val in (None, "", "0", "0.00"):
            continue
        try:
            return float(str(val).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _bse_company_record(row: dict[str, Any], cap: float | None) -> dict[str, Any]:
    """Build a universe record for a non-index company straight from BSE data.

    These names aren't in the Nifty 500 CSV, so we have no NSE symbol/sector from
    there — we use the BSE master's own fields (scrip_id as symbol, Issuer_Name,
    INDUSTRY) instead.
    """
    name = (row.get("Issuer_Name") or row.get("Scrip_Name") or "").strip()
    symbol = (row.get("scrip_id") or "").strip() or str(row.get("SCRIP_CD") or "").strip()
    return {
        "symbol": symbol,
        "bse_code": str(row.get("SCRIP_CD") or "").strip(),
        "isin": (row.get("ISIN_NUMBER") or "").strip(),
        "name": name,
        "bse_name": name,
        "sector": (row.get("INDUSTRY") or "").strip(),
        "market_cap_cr": cap,
        "aliases": make_aliases(name, symbol),
    }


def _expand_universe(bse: list[dict[str, Any]], have_isins: set[str]) -> list[dict[str, Any]]:
    """Add liquid BSE companies above the configured market-cap floor (deduped).

    Rule (config.settings.universe_expansion): keep an active equity if its market
    cap > mcap_floor_cr AND it's in `groups` (e.g. A), OR it's in `group_caps` and
    above that group's higher cap (e.g. Group B above ₹3,000 cr).
    """
    cfg = load_settings().get("universe_expansion", {}) or {}
    if not cfg.get("enabled"):
        return []
    floor = float(cfg.get("mcap_floor_cr", 500))
    groups = set(cfg.get("groups", []) or [])
    group_caps = {k: float(v) for k, v in (cfg.get("group_caps", {}) or {}).items()}

    added: list[dict[str, Any]] = []
    for row in bse:
        isin = (row.get("ISIN_NUMBER") or "").strip()
        # Equity ISINs start with INE; INF = mutual-fund/ETF schemes, which BSE
        # lists under Segment=Equity too (seen live: SENSEXIETF, JUNIORBEES) —
        # not companies, so they don't belong in a catalyst universe.
        if (not isin or not isin.startswith("INE") or isin in have_isins
                or row.get("Status") != "Active" or row.get("Segment") != "Equity"):
            continue
        cap = _parse_market_cap(row)
        if not cap or cap <= floor:
            continue
        grp = (row.get("GROUP") or "").strip()
        if grp in groups or (grp in group_caps and cap > group_caps[grp]):
            added.append(_bse_company_record(row, cap))
            have_isins.add(isin)
    return added


def build_map(session: PoliteSession | None = None) -> dict[str, Any]:
    """Fetch both sources, join on ISIN, write outputs, return summary stats."""
    session = session or PoliteSession()
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)

    nifty = fetch_nifty500(session)
    bse = fetch_bse_master(session)
    fno = fetch_fno_symbols(session)

    # Cache raw pulls so we can inspect/debug without re-fetching.
    (UNIVERSE_DIR / "nifty500_raw.json").write_text(
        json.dumps(nifty, indent=2, ensure_ascii=False), encoding="utf-8")
    (UNIVERSE_DIR / "bse_scrip_master.json").write_text(
        json.dumps(bse, ensure_ascii=False), encoding="utf-8")

    # Index BSE by ISIN for an O(1) join.
    bse_by_isin: dict[str, dict[str, Any]] = {}
    for row in bse:
        isin = (row.get("ISIN_NUMBER") or "").strip()
        if isin:
            bse_by_isin.setdefault(isin, row)  # first active wins

    merged: list[dict[str, Any]] = []
    unmatched: list[dict[str, str]] = []
    for c in nifty:
        isin = c["isin"]
        b = bse_by_isin.get(isin)
        if not b:
            unmatched.append(c)
            continue
        merged.append({
            "symbol": c["symbol"],
            "bse_code": str(b.get("SCRIP_CD") or "").strip(),
            "isin": isin,
            "name": c["company"],
            "bse_name": (b.get("Issuer_Name") or b.get("Scrip_Name") or "").strip(),
            "sector": c["industry"],
            "market_cap_cr": _parse_market_cap(b),
            "aliases": make_aliases(c["company"], c["symbol"]),
            "in_fno": c["symbol"].upper() in fno,
        })

    index_count = len(merged)
    # Expand beyond the index to liquid BSE companies above the market-cap floor.
    expansion = _expand_universe(bse, {c["isin"] for c in merged})
    for rec in expansion:  # expansion symbols are BSE scrip_ids; still match when they align
        rec["in_fno"] = (rec.get("symbol") or "").upper() in fno
    merged.extend(expansion)

    # Persist the merged map (JSON for code, CSV for eyeballing in Excel).
    (UNIVERSE_DIR / "nifty500_bse_map.json").write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(UNIVERSE_DIR / "nifty500_bse_map.csv", merged)

    stats = {
        "nifty_count": len(nifty),
        "bse_master_count": len(bse),
        "index_matched": index_count,
        "expansion_added": len(expansion),
        "universe_size": len(merged),
        "unmatched": len(unmatched),
        "unmatched_samples": [u["company"] for u in unmatched[:10]],
        "out_dir": str(UNIVERSE_DIR),
    }
    log.info("Universe built: %s", stats)
    return stats


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = ["symbol", "bse_code", "isin", "name", "bse_name", "sector", "market_cap_cr"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


def load_map() -> list[dict[str, Any]]:
    """Read the previously-built merged map (used by downstream ingesters)."""
    path = UNIVERSE_DIR / "nifty500_bse_map.json"
    if not path.exists():
        raise FileNotFoundError(
            "Universe map not found. Run `setup-universe` first.")
    return json.loads(path.read_text(encoding="utf-8"))
