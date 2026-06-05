"""Shared HTTP layer: one polite, browser-like session for every fetcher.

Design goals (Section 16 of the spec):
- Be a polite citizen: ~1 req/sec, exponential backoff, capped retries.
- Browser-like headers so public endpoints don't reject us outright.
- BSE quirk: api.bseindia.com only responds after the SAME session has first
  visited www.bseindia.com (TLS/session warming). We expose prime_bse() for that.
- Per-call failures raise, so each ingester can catch and continue (one dead
  source must not kill the whole run).
"""
from __future__ import annotations

import logging
import time

import requests

from scanner.config import load_settings, load_sources

log = logging.getLogger(__name__)

# A realistic desktop-Chrome UA. Endpoints we use are public; this just avoids
# naive bot-blocks. We do not bypass auth or paywalls.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


class PoliteSession:
    """A requests.Session wrapper that rate-limits and retries with backoff.

    Use one instance per logical run. Reusing the session keeps cookies (needed
    for BSE) and TCP connections warm.
    """

    def __init__(self) -> None:
        settings = load_settings()
        self.delay = float(settings.get("request_delay_sec", 1.0))
        self.max_retries = int(settings.get("max_retries", 3))
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)
        self._last_request_ts = 0.0
        self._bse_primed = False
        self._nse_primed = False

    # -- internal: enforce ~1 req/sec across ALL calls on this session ---------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def get(self, url: str, *, timeout: int = 30, **kwargs) -> requests.Response:
        """Throttled GET with exponential backoff. Raises on final failure."""
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=timeout, **kwargs)
                # Retry on transient server/rate errors; raise on hard client errors.
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{resp.status_code} from {url}")
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001 - we deliberately retry any fetch error
                last_exc = exc
                backoff = self.delay * (2 ** (attempt - 1))
                log.warning("GET %s failed (attempt %d/%d): %s — backing off %.1fs",
                            url, attempt, self.max_retries, exc, backoff)
                if attempt < self.max_retries:
                    time.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    # -- BSE session warming ---------------------------------------------------
    def prime_bse(self) -> None:
        """Visit www.bseindia.com once so api.bseindia.com will answer.

        Idempotent: only the first call does the round-trip.
        """
        if self._bse_primed:
            return
        src = load_sources().get("bse", {})
        referer = src.get("referer", "https://www.bseindia.com/")
        try:
            self.get(referer, timeout=30)
            self._bse_primed = True
            log.info("BSE session primed via %s", referer)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to prime BSE session: %s", exc)
            raise

    def bse_get(self, url: str, *, params: dict | None = None, timeout: int = 40) -> requests.Response:
        """GET a BSE API endpoint with the required Referer/Origin + primed session."""
        self.prime_bse()
        src = load_sources().get("bse", {})
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": src.get("referer", "https://www.bseindia.com/"),
            "Origin": src.get("origin", "https://www.bseindia.com"),
        }
        return self.get(url, params=params, headers=headers, timeout=timeout)

    # -- NSE session warming --------------------------------------------------
    def prime_nse(self) -> None:
        """Seed cookies by visiting nseindia.com once.

        NSE's root often returns 403 to non-browser clients, but the visit still
        seeds the session enough for the (less-protected) snapshot APIs to answer.
        We do a single direct GET (no retry) and swallow any failure.
        """
        if self._nse_primed:
            return
        src = load_sources().get("nse", {})
        root = src.get("root", "https://www.nseindia.com/")
        try:
            self._throttle()
            self.session.get(root, timeout=20)
        except Exception as exc:  # noqa: BLE001 - 403/timeout here is expected
            log.info("NSE prime returned %s (expected; continuing)", exc)
        self._nse_primed = True

    def nse_get(self, url: str, *, params: dict | None = None, timeout: int = 30) -> requests.Response:
        """GET an NSE API endpoint with the required Referer + primed session."""
        self.prime_nse()
        src = load_sources().get("nse", {})
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": src.get("deals_referer", "https://www.nseindia.com/"),
        }
        return self.get(url, params=params, headers=headers, timeout=timeout)
