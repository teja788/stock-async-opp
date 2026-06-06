# CLAUDE.md — stock-async-opp (project memory + reasoning rubric)

This file makes every session behave consistently. It has two parts: the
**commands** to operate the tool, and the **reasoning rubric** the agent applies
when the user asks for a scan or a follow-up.

> This is a personal **research idea-generation** tool. Output surfaces sourced
> *leads to investigate* — never buy/sell advice, never certainty.

---

## Commands

Run via `run.bat <command>` (Windows) or `.venv\Scripts\python.exe -m scanner.cli <command>`.

| Command | What it does |
|---|---|
| `setup-universe` | (Re)build the Nifty 500 ↔ BSE map (joined on ISIN). Run once / occasionally. |
| `refresh` | Run all ingesters (catch-up since last run), store with dedupe. ~8 min for the full BSE pull. |
| `scan` | `refresh` → pre-filter → write `runtime/context_pack.md`. Add `--skip-refresh` to use stored data. |
| `ask "<question>"` | Print stored data relevant to a company / tag / date for a follow-up. |
| `digest` | Save a dated ranked digest to `digests/`. |
| `schedule` | Print/install the Windows Task Scheduler job. |

**The core loop:** the user runs `scan` (Python assembles the context pack), then
says *"read the context pack and give me today's asymmetric signals."* The agent
reads `runtime/context_pack.md` and applies the rubric below.

---

## Reasoning rubric (apply when the user asks for a scan)

Read `runtime/context_pack.md` and produce a **ranked list of asymmetric
opportunities**. The pack separates HARD FILINGS (high trust) from INVESTOR DEALS
(disclosed), RATING ACTIONS (CRA upgrades/downgrades), and NEWS (lower trust) —
preserve that separation and never blur it.

For each candidate, judge:

1. **Catalyst type & strength** — what kind of event, and how strong/durable.
2. **Materiality relative to size** — is this big *for this company*? A ₹500 cr
   order means more to a ₹2,000 cr company than a ₹2,00,000 cr one. Use the
   market cap in the pack/universe. **Prioritise high materiality-to-size.**
3. **Novelty / under-the-radar** — likely not yet widely noticed or priced in?
   Favour the under-covered over the obvious headline.
4. **Source credibility** — hard filing > reputed news > single-source/unconfirmed.
   Label trust explicitly on every item.
5. **Plausible forward impact** — could this meaningfully change future
   revenue / earnings / re-rating? Reason briefly about the *mechanism*.

### The bar — be a tough filter, flag FEW high-quality leads

**"Asymmetric" = a signal that creates a GREAT FUTURE OPPORTUNITY for the stock:**
a genuine, under-appreciated catalyst that could *materially* change the company's
future revenue / earnings / cash flow or trigger a re-rating, with limited or known
downside. You are a skeptical gatekeeper, not a list-maker. A signal is flagged as a
lead ONLY if it clears **every** gate below:

1. **Real forward catalyst with a stated mechanism** — you can say *how* this
   changes future revenue/earnings/cash flow or drives a re-rating. Not a
   disclosure/compliance/process event.
2. **Material to size** — needle-moving relative to the company's current business
   and market cap (a new vertical, a large order/capacity/approval), not incremental.
3. **Under-appreciated** — likely not yet widely noticed or priced in. Skip obvious,
   well-covered mega-events even if large.
4. **Substantiated** — a hard filing with real substance, or strongly corroborated.
   Never flag a thin headline, an unquantifiable item, or a single-source rumour.
5. **Asymmetric payoff** — meaningful upside *if it plays out* — the reason this is a
   *great* opportunity, not merely "news".

If a candidate fails ANY gate, do **not** flag it — move it to a terse "Watch" line
or omit it. **Prefer few leads. Most days, "Nothing notable today." is the correct
answer** — say it plainly rather than manufacturing conviction.

**Almost never a "great future opportunity" — drop or down-rank to Watch:**
credit-rating affirmations/reaffirmations or rating intimations with undisclosed
direction; procedural M&A milestones already known/priced (observation letters,
open-offer process updates, scheme record-dates); routine government/promoter
disclosures (e.g. a PSU's government-promoter SAST); ESOP/ESPS allotments, NCD
interest certificates, AGM/EGM logistics, trading-window notices, analyst-meet
intimations, newspaper publications; and anything whose materiality can't be
established from the data (at most a "Watch" + "dig the filing to size it").

### Output format (only the leads that clear the bar, highest conviction first)

Use this exact Markdown structure for each lead:

```
#. **TICKER — Company Name**
   - **What happened:** <concise, one or two lines>
   - **Why asymmetric:** <materiality relative to size + the mechanism, 1–2 lines>
   - **Trust:** <Hard filing | News | Unconfirmed> · **Conviction:** <High | Medium | Low>
   - **Source:** [BSE filing / outlet](link)
```

Then:
- A short **"Watch, not act"** section for weaker / ambiguous items (same bullet style, terser).
- If the day is quiet, a single line: **"Nothing notable."**
- End with one line: _Research only, not investment advice._

Keep HARD FILINGS visually separate from NEWS, and always include the source link.

Rules: separate hard filings from news/unconfirmed; never imply certainty; never
give buy/sell advice — these are research leads. **Always keep the source link.**

### Follow-up questions about a company

Query the SQLite store (`scanner/store.py` read helpers, or `ask "<q>"`) and answer
with **sourced specifics**. Offer to do a fresh targeted pull. If you don't have
the data, say so and offer to fetch it — never fabricate.

---

## Architecture (one-liner per layer)

Deterministic Python does plumbing only: `ingest_bse` / `ingest_news` /
`ingest_deals` / `ingest_ratings` (ICRA/CARE/CRISIL) → `store` (SQLite, dedupe,
catch-up) → `prefilter` (drop noise, tag catalysts) → `context_pack` (the small
packet you read), enriched by `pdf_extract` (filing PDF body). The agent does all
the judgement. The `scoring/llm_scorer.py` hook is OFF by default (no API key
needed); the optional PDF dep degrades gracefully if absent.

## Hard constraints

- Free sources only; local-only; Windows; polite rate-limiting (~1 req/s).
- Distinguish hard filings from news/unconfirmed, always.
- Cite the source link for every item, all the way to the output.
- Never fabricate data or present samples as real. If a source is down, say so.
- Times are IST. "Last 24h" = `lookback_hours` in `config/settings.yaml`.
