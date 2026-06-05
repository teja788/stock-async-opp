"""FUTURE HOOK — LLM-API scorer. OFF BY DEFAULT. Do not implement now.

Section 17 of the build spec: when settings.scoring.mode == "llm_api", this
module would send the context pack to an LLM API and return the SAME ranked
structure the in-session agent produces, so a background job could pre-score and
(later) push alerts.

Hard rule: importing this module must NEVER require an API key. The key is only
read if/when scoring is actually switched to llm_api mode by the user.
"""
from __future__ import annotations

from typing import Any


def is_enabled(settings: dict[str, Any]) -> bool:
    """True only when the user has explicitly opted in via settings.yaml."""
    return settings.get("scoring", {}).get("mode") == "llm_api"


def score(context_pack: str) -> list[dict[str, Any]]:
    """Return a ranked list of signals from a context pack.

    Stable interface so the background job and `digest` command can call it once
    enabled. Until then it must not run.

    Returns: list of dicts shaped like
        {"ticker", "company", "headline", "why_asymmetric",
         "catalyst", "trust", "conviction", "source"}
    """
    raise NotImplementedError(
        "llm_api scoring is a future hook and is disabled. "
        "Keep settings.scoring.mode = 'agent' (the in-session agent does the reasoning).\n"
        "TODO(future): build the LLM API client here. Read the key from an env var "
        "(e.g. ANTHROPIC_API_KEY) ONLY inside this function, never at import time."
    )
