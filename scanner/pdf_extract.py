"""Extract the BODY TEXT of filing / rationale PDFs (#8). Lazy + best-effort.

BSE filings only expose a short headline/subject in the API; the real numbers
(order value, capacity MTPA, JV terms) live inside the attached PDF. This fetches
the PDF via the shared polite session and pulls its text layer, caching the
result by the announcement's dedupe_hash so each PDF is parsed at most once.

Extractor preference: PyMuPDF (fitz) if installed (best quality), else pypdf
(already in the venv) -- so this works with ZERO new dependencies and improves if
PyMuPDF is added. Scanned/image-only PDFs (no text layer) return '' with
method='empty'; OCR is intentionally not bundled (heavy Tesseract dep).
"""
from __future__ import annotations

import logging
from typing import Any

from scanner import store
from scanner.config import load_settings
from scanner.http import PoliteSession

log = logging.getLogger(__name__)

MAX_CHARS = 20000       # cap stored text per filing
MIN_TEXT = 10           # below this => treat as no text layer (scanned)


def _cfg() -> dict[str, Any]:
    return load_settings().get("pdf_extract", {}) or {}


def is_available() -> bool:
    """True if either PyMuPDF or pypdf can be imported."""
    for mod in ("fitz", "pypdf"):
        try:
            __import__(mod)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True)) and is_available()


def _extract_bytes(data: bytes) -> tuple[str, str]:
    """Return (text, method). method: pymupdf | pypdf | empty | error:<reason>."""
    # 1) PyMuPDF — best text extraction, self-contained Windows wheel.
    try:
        import fitz  # PyMuPDF
        try:
            parts = []
            with fitz.open(stream=data, filetype="pdf") as doc:
                for page in doc:
                    parts.append(page.get_text())
            text = "\n".join(parts).strip()
            return (text[:MAX_CHARS], "pymupdf") if len(text) >= MIN_TEXT else ("", "empty")
        except Exception as exc:  # noqa: BLE001
            return "", f"error:pymupdf:{type(exc).__name__}"
    except Exception:  # noqa: BLE001 - PyMuPDF not installed; fall through
        pass
    # 2) pypdf — pure-python fallback (already installed).
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = [(p.extract_text() or "") for p in reader.pages]
        text = "\n".join(parts).strip()
        return (text[:MAX_CHARS], "pypdf") if len(text) >= MIN_TEXT else ("", "empty")
    except Exception as exc:  # noqa: BLE001
        return "", f"error:pypdf:{type(exc).__name__}"


def _fetch(url: str, session: PoliteSession):
    """GET a PDF, priming BSE cookies when it's a bseindia attachment."""
    if "bseindia.com" in url:
        session.prime_bse()
    return session.get(url, timeout=45)


def extract_for(ann: dict[str, Any], session: PoliteSession, conn=None) -> str:
    """Cached-or-fresh body text for one filing (by dedupe_hash). '' if none."""
    ref, url = ann.get("dedupe_hash"), ann.get("pdf_url")
    if not ref or not url:
        return ""
    cached = store.get_filing_text(ref, conn=conn)
    if cached is not None:
        return cached.get("text") or ""
    try:
        text, method = _extract_bytes(_fetch(url, session).content)
    except Exception as exc:  # noqa: BLE001
        text, method = "", f"error:fetch:{type(exc).__name__}"
    # Cache successes and genuine 'empty' (scanned PDFs), but NOT transient errors
    # — otherwise one flaky fetch sticks at '' forever; leave it to retry next scan.
    if not method.startswith("error:"):
        store.save_filing_text(ref, url, text, method, conn=conn)
    return text


def extract_url(url: str, session: PoliteSession) -> str:
    """Extract text from an arbitrary PDF URL (no caching). For CRA rationale PDFs."""
    if not url:
        return ""
    try:
        text, _ = _extract_bytes(_fetch(url, session).content)
        return text
    except Exception as exc:  # noqa: BLE001
        log.info("PDF extract failed for %s: %s", url, exc)
        return ""


def enrich_filings(anns: list[dict[str, Any]], session: PoliteSession | None = None,
                   limit: int | None = None, conn=None) -> int:
    """Best-effort: pull PDF body text for filings and attach as ann['pdf_text'].

    Self-continuing by design: extraction is cached per filing and transient
    fetch errors are NOT cached, so each scan retries failures and extends
    coverage until every filing in scope has text — an interrupted/timed-out
    run loses nothing. The `limit` caps NEW fetches per run (cached hits are
    free and don't count), so backlogs drain across scans. Default from
    settings pdf_extract.max_per_scan. Returns count enriched.
    """
    if not is_enabled():
        return 0
    if limit is None:
        limit = int(_cfg().get("max_per_scan", 40))
    session = session or PoliteSession()
    done = fetched = 0
    for a in anns:
        ref = a.get("dedupe_hash")
        cached = store.get_filing_text(ref, conn=conn) if ref else None
        if cached is None:
            if fetched >= limit:
                continue  # budget spent — next scan continues from cache
            fetched += 1
        text = extract_for(a, session, conn=conn)
        if text:
            a["pdf_text"] = text
            done += 1
    if done or fetched:
        log.info("PDF-enriched %d filings (%d fresh fetches, %d in scope)",
                 done, fetched, len(anns))
    return done
