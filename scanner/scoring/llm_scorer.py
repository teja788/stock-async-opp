"""LLM scorer/chat for the dashboard's optional AI panels (Section 17 hook).

Still OFF for the CLI by default (settings.scoring.mode stays "agent"); nothing
here is imported unless the dashboard's AI panel is used. Supports two backends,
switchable per call: "claude" (Anthropic) and "openai" (GPT). The SDKs are
LAZY-IMPORTED inside each function, and API keys are read from the argument or
the environment ONLY at call time — importing this module never needs a key.

The same rubric (mirrors CLAUDE.md / Section 12) is used as the system prompt so
API ranking matches the in-session agent's behaviour.
"""
from __future__ import annotations

import os
from typing import Any

# Default models. gpt-5.5 per user preference; claude-opus-4-8 is the latest Opus.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-5.5"

RUBRIC = """You are a research assistant surfacing ASYMMETRIC opportunities in Indian
equities (Nifty 500) for further investigation. This is idea-generation, NOT
investment advice, and never a buy/sell recommendation.

You are given a CONTEXT PACK assembled by deterministic code: hard BSE filings
(high trust), disclosed investor/promoter deals (BSE+NSE), and reputed-outlet
news (lower trust). Produce a RANKED list of asymmetric opportunities.

For each candidate, judge:
1. Catalyst type & strength — what happened, how strong/durable.
2. Materiality relative to size — is it big FOR THIS COMPANY? Use the market cap
   shown in the pack. Prioritise high materiality-to-size.
3. Novelty / under-the-radar — likely not yet widely noticed or priced in.
4. Source credibility — hard filing > reputed news > single-source/unconfirmed.
   Label trust explicitly.
5. Plausible forward impact — could it meaningfully change revenue/earnings/
   re-rating? Reason briefly about the mechanism.

Output format (highest conviction first), as Markdown:
  **#. TICKER (Company) — <one line: what happened>**
  Why it may be asymmetric: <1-2 lines on materiality + mechanism>
  Catalyst: <type> | Trust: <filing/news/unconfirmed> | Conviction: <high/med/low>
  Source: <link>

Then a short "Watch, not act" section for weaker/ambiguous items, and a single
line "Nothing notable." if the day is quiet. Keep HARD FILINGS separate from
NEWS. Never imply certainty; never give buy/sell advice; ALWAYS keep source links.
"""

CHAT_SYSTEM = """You are a research assistant for an Indian-equities catalyst scanner.
Answer the user's question using ONLY the provided stored data (filings, deals,
news with source links). Cite the source link for specifics. If the data does not
contain the answer, say so plainly and suggest fetching a fresh pull. This is
research, not investment advice — never recommend buying or selling.
"""


def is_enabled(settings: dict[str, Any]) -> bool:
    """True only when the user has explicitly opted in via settings.yaml."""
    return settings.get("scoring", {}).get("mode") == "llm_api"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def score(context_pack: str, provider: str = "claude",
          model: str | None = None, api_key: str | None = None) -> str:
    """Rank the context pack into asymmetric signals (Markdown text)."""
    user = ("Here is today's context pack. Produce the ranked asymmetric-signal "
            f"list per the rubric.\n\n{context_pack}")
    if provider == "openai":
        return _openai_complete(RUBRIC, user, model or DEFAULT_OPENAI_MODEL, api_key)
    return _claude_complete(RUBRIC, user, model or DEFAULT_CLAUDE_MODEL, api_key)


def chat(question: str, context: str, provider: str = "claude",
         model: str | None = None, api_key: str | None = None) -> str:
    """Answer a follow-up question grounded in retrieved stored data."""
    user = f"Stored data relevant to the question:\n\n{context}\n\nQuestion: {question}"
    if provider == "openai":
        return _openai_complete(CHAT_SYSTEM, user, model or DEFAULT_OPENAI_MODEL, api_key)
    return _claude_complete(CHAT_SYSTEM, user, model or DEFAULT_CLAUDE_MODEL, api_key)


# --------------------------------------------------------------------------- #
# Backends (lazy-imported; key read only here)
# --------------------------------------------------------------------------- #
def _claude_complete(system: str, user: str, model: str, api_key: str | None) -> str:
    """Anthropic path. Adaptive thinking + effort, rubric prompt-cached."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    # System (stable) is prompt-cached; the volatile pack sits in messages after it.
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _openai_complete(system: str, user: str, model: str, api_key: str | None) -> str:
    """OpenAI path (gpt-5.5 by default). Uses chat.completions for broad compat."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key) if api_key else OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()
