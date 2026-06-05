# stock-async-opp

A **local, free, on-demand research tool** for discovering *asymmetric opportunities* in
Indian equities (Nifty 500) to investigate further.

> ⚠️ This is a personal **idea-generation and research-acceleration tool** — **not**
> investment advice, **not** a buy/sell signal generator, and **not** a redistribution of
> exchange/news data. It surfaces sourced *leads*; you research and decide.

## How it works

Deterministic Python does the fragile, cacheable plumbing — fetch → dedupe → store →
pre-filter — and assembles a compact **context pack**. The *reasoning* (ranking catalysts
by materiality-vs-size, novelty, trust, and plausible impact) is done **live by the agent**
(Claude Code / Codex) in-session against the rubric in `CLAUDE.md`. No LLM API key is
required for the Python layer to function.

```
INGEST (BSE filings + news RSS + bulk/block/insider deals)
  -> SQLite store (dedupe + catch-up since last run)
  -> PRE-FILTER (drop routine noise, tag candidate catalysts)
  -> CONTEXT PACK (runtime/context_pack.md)
  -> REASONING (live agent now; optional LLM-API stub later)
```

Hard BSE **filings** (high trust) are kept strictly separate from **news** (lower trust),
and every item carries its **source link** all the way to the final output.

## Setup (Windows)

```bat
setup.bat            REM create .venv + install deps (one time)
run.bat --help       REM see all commands
run.bat version      REM sanity check: prints version + active config
```

## Commands

| Command | What it does | Status |
|---|---|---|
| `setup-universe` | Build the Nifty 500 ↔ BSE map (via ISIN) | Milestone 2 |
| `refresh` | Run all ingesters (catch-up since last run) | Milestones 3–6 |
| `scan` | `refresh` → pre-filter → write context pack | Milestones 7–9 |
| `ask "<q>"` | Print stored data relevant to a follow-up | Milestone 10 |
| `digest` | Save a dated markdown digest | Milestone 10 |
| `schedule` | Print/install the Windows Task Scheduler job | Milestone 11 |

## Configuration (edit freely)

- `config/settings.yaml` — lookback window, rate limits, paths, scoring mode
- `config/sources.yaml` — news RSS feeds + BSE/NSE endpoints
- `config/investors.yaml` — marquee investor watchlist
- `config/noise_filters.yaml` — routine filing types to drop / down-rank

## Status

**All 12 milestones complete** — usable end-to-end against live data.
Deterministic ingest → SQLite (dedupe + catch-up) → prefilter → context pack;
the agent ranks live per `CLAUDE.md`. Future hooks (LLM scorer, Zerodha Kite,
watchlist UX, notifications) are present as clearly-marked, off-by-default stubs.

Typical loop: `setup.bat` once → `run.bat scan` → ask the agent to read the
context pack and rank today's asymmetric signals → `run.bat ask "<company>" --fetch`
to dig deeper.
