"""FUTURE HOOK — Zerodha Kite adapter. NOT IMPLEMENTED. Do not wire in now.

Section 17 of the build spec: a placeholder data-source module so you can later
add Kite for richer/cleaner data (live quotes, holdings, instruments master,
historical candles) without disturbing the free-source ingesters.

Design intent when built:
- Read credentials from env vars (KITE_API_KEY / KITE_ACCESS_TOKEN) ONLY inside
  the functions, never at import time — so importing this module never requires
  a key and the core tool keeps working without one.
- Return data normalised to the same shapes the existing store expects, so the
  prefilter / context pack need no changes.
"""
from __future__ import annotations

from typing import Any


def is_configured() -> bool:
    """True only if Kite credentials are present in the environment."""
    import os
    return bool(os.environ.get("KITE_API_KEY") and os.environ.get("KITE_ACCESS_TOKEN"))


def fetch_instruments() -> list[dict[str, Any]]:
    """Would return the Kite instruments master (symbol/token/segment)."""
    raise NotImplementedError(
        "Zerodha Kite adapter is a future hook and is not implemented. "
        "TODO(future): pip install kiteconnect; read KITE_* env vars inside this "
        "function; map results to the store's company/quote shapes."
    )
