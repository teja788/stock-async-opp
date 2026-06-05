"""FUTURE HOOK — notifications (email / Telegram). NOT IMPLEMENTED.

Section 17 of the build spec: a placeholder for pushing high-conviction signals
as alerts once the background job can pre-score (via scoring/llm_scorer.py in
llm_api mode). OFF by default; the core tool never calls this.

Design intent when built:
- Read channel credentials from env vars ONLY inside send() (no key at import).
- Accept the same ranked-signal structure the scorer/agent produces.
"""
from __future__ import annotations

from typing import Any


def send_alert(signals: list[dict[str, Any]], channel: str = "telegram") -> None:
    """Would push ranked signals to email/Telegram. Disabled."""
    raise NotImplementedError(
        "Notifications are a future hook and are not implemented. "
        "TODO(future): implement Telegram bot / SMTP email here; read "
        "TELEGRAM_BOT_TOKEN / SMTP_* from env inside this function only. "
        "Wire it to the background job once llm_api scoring is enabled."
    )
