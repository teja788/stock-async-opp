# AGENTS.md — stock-async-opp (for Codex / OpenAI agents)

This project is **agent-agnostic**. The Python layer (`scanner/`) is deterministic
plumbing and needs **no API key**; the *reasoning* (ranking/explaining asymmetric
signals) is done live by whatever agent is in the session — Claude Code, Codex, etc.

**The full reasoning rubric and command reference live in [`CLAUDE.md`](CLAUDE.md).
Read it — it is the single source of truth and applies verbatim to Codex too.**

## The loop (same for any agent)

1. Run a scan (pure Python — Windows `run.bat scan` or `python -m scanner.cli scan`).
   - It does: catch-up refresh (BSE filings + BSE/NSE deals + insider/SAST) → pre-filter
     (drop noise, tag catalysts) → write `runtime/context_pack.md` (+ `.json`).
   - Flags: `--days N` / `--hours N` widen the window; `--skip-refresh` uses stored data.
2. Read `runtime/context_pack.md`.
3. Produce a **ranked list of asymmetric opportunities** per the rubric in `CLAUDE.md`:
   judge catalyst strength, materiality-relative-to-size, novelty/under-the-radar,
   source credibility (filing > news > unconfirmed), and plausible forward impact.
   Keep HARD FILINGS separate from NEWS; always cite the source link; never give
   buy/sell advice — these are research leads.
4. Follow-ups: `python -m scanner.cli ask "<company>" --fetch` pulls stored + fresh
   data (with sources) for the agent to reason over.

## Commands

`setup-universe` · `refresh` · `scan` · `ask "<q>"` · `digest` · `schedule`
(All accept `--hours/--days` except `setup-universe`/`schedule`.)

## Setup (one time, Windows)

```bat
setup.bat            REM create .venv + install deps
run.bat scan         REM then read runtime/context_pack.md and rank
```

No OpenAI/Anthropic key is required for the Python layer. If you later enable the
optional LLM scorer (`config/settings.yaml` -> `scoring.mode: llm_api`), wire your
provider's client inside `scanner/scoring/llm_scorer.py` (read the key from an env
var **inside the function**, never at import time).
