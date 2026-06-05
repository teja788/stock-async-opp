"""Typer CLI for stock-async-opp.

This is the Milestone-1 skeleton: every command exists and is wired to the
console, but the data-bearing commands print a clear "not yet implemented"
notice pointing at the milestone that will build them. This lets us verify the
plumbing (venv, imports, CLI dispatch, config loading) before any network code.

Commands (Section 13 of the build spec):
  refresh         run all ingesters (catch-up since last run)        [M3-M6]
  scan            refresh -> prefilter -> write context pack         [M7-M9]
  ask             print stored data relevant to a question           [M10]
  digest          save a dated markdown digest                       [M10]
  setup-universe  (re)build the Nifty 500 <-> BSE map                 [M2]
  schedule        print/install the Windows Task Scheduler command    [M11]
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scanner import __app_name__, __version__
from scanner.config import load_settings, resolve_path

_IST = ZoneInfo("Asia/Kolkata")

app = typer.Typer(
    name=__app_name__,
    help="On-demand Indian-market catalyst scanner (local, free sources, research-only).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def _main() -> None:
    """Configure UTF-8 console + file logging once, before any command runs."""
    # Windows consoles default to a legacy codepage (cp1252); force UTF-8 so
    # rupee signs, em-dashes and curly quotes render instead of mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    log_dir = resolve_path("runtime/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[logging.FileHandler(log_dir / "scanner.log", encoding="utf-8")],
        force=True,  # reconfigure cleanly even if basicConfig ran before
    )


def _todo(command: str, milestone: str) -> None:
    """Uniform 'planned but not built yet' notice, so the CLI is honest about scope."""
    console.print(
        Panel.fit(
            f"[yellow]'{command}'[/yellow] is scaffolded but not implemented yet.\n"
            f"It will be built in [bold]{milestone}[/bold].",
            title="Not yet implemented",
            border_style="yellow",
        )
    )


@app.command()
def version() -> None:
    """Show app name, version, and the active lookback window."""
    settings = load_settings()
    table = Table(show_header=False, box=None)
    table.add_row("App", f"[bold]{__app_name__}[/bold]")
    table.add_row("Version", __version__)
    table.add_row("Universe", str(settings.get("universe")))
    table.add_row("Lookback (hours)", str(settings.get("lookback_hours")))
    table.add_row("Scoring mode", str(settings.get("scoring", {}).get("mode")))
    console.print(Panel(table, title="stock-async-opp", border_style="cyan"))


# Reusable typer options so every time-windowed command exposes the same flags.
_HOURS_OPT = typer.Option(None, "--hours", "-H", help="Look back this many hours (overrides settings lookback).")
_DAYS_OPT = typer.Option(None, "--days", "-D", help="Look back this many days. Combines with --hours.")
_LARGE_WINDOW_HOURS = 30 * 24  # beyond this, warn that a BSE pull will be slow


def resolve_window(hours: int | None, days: int | None) -> tuple[datetime | None, str | None]:
    """Turn --hours/--days into an explicit `since` instant (IST) + a label.

    Returns (None, None) when neither flag is given, so callers keep their
    default behaviour (catch-up cursor / settings.lookback_hours). Combines both
    flags when present (e.g. --days 2 --hours 12 -> 60h).
    """
    if hours is None and days is None:
        return None, None
    total = (days or 0) * 24 + (hours or 0)
    if total <= 0:
        raise typer.BadParameter("Window must be positive (use --hours and/or --days > 0).")
    since = datetime.now(_IST) - timedelta(hours=total)
    if days and hours:
        label = f"{days}d{hours}h"
    elif days:
        label = f"{days}d"
    else:
        label = f"{hours}h"
    return since, label


def _refresh_all(bse_limit: int | None = None,
                 since_override: datetime | None = None) -> dict[str, dict]:
    """Run every ingester with catch-up, store with dedupe, track runs.

    Returns {source: {"fetched", "new", "status"}}. Each source is isolated so
    one failure still lets the others (and the rest of a scan) proceed.
    `bse_limit` caps the number of BSE scrips polled (for a quick partial
    refresh / tests); None polls the full universe (~498 scrips, ~8 min).
    """
    from scanner import ingest_bse, ingest_deals, ingest_news, store
    from scanner.http import PoliteSession
    from scanner.universe import load_map

    store.init_db()
    universe = load_map()
    store.sync_companies(universe)
    session = PoliteSession()
    results: dict[str, dict] = {}

    def _do(source: str, fetch_and_store) -> None:
        try:
            fetched, new = fetch_and_store()
            store.mark_run(source, fetched, "ok")
            results[source] = {"fetched": fetched, "new": new, "status": "ok"}
        except Exception as exc:  # noqa: BLE001 - per-source isolation
            store.mark_run(source, 0, "error", note=str(exc)[:200])
            results[source] = {"fetched": 0, "new": 0, "status": f"error: {exc}"}
            log.warning("refresh source %s failed: %s", source, exc)

    def _bse():
        # An explicit window overrides the catch-up cursor for a deliberate wider pull.
        since = since_override or store.get_last_success("bse_announcements")
        codes = None
        if bse_limit:
            codes = [str(c["bse_code"]) for c in universe if c.get("bse_code")][:bse_limit]
        items = ingest_bse.ingest(session=session, since=since, scrip_codes=codes)
        return len(items), store.upsert_announcements(items)

    def _news():
        items = ingest_news.ingest(session=session)
        return len(items), store.upsert_news(items)

    def _deals():
        since = since_override or store.get_last_success("deals")
        items = ingest_deals.ingest(session=session, since=since)
        return len(items), store.upsert_deals(items)

    _do("bse_announcements", _bse)
    _do("news", _news)
    _do("deals", _deals)
    return results


def _render_refresh(results: dict[str, dict]) -> None:
    table = Table(title="Refresh — per-source results", border_style="cyan")
    table.add_column("Source")
    table.add_column("Fetched", justify="right")
    table.add_column("New (deduped)", justify="right")
    table.add_column("Status")
    for src, r in results.items():
        status = r["status"]
        colour = "green" if status == "ok" else "red"
        table.add_row(src, str(r["fetched"]), str(r["new"]), f"[{colour}]{status}[/{colour}]")
    console.print(table)


@app.command()
def refresh(hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Run all ingesters (catch-up since last run) and update SQLite.

    With --hours/--days, fetch that far back instead of from the last-run cursor
    (a deliberate wider backfill).
    """
    since, label = resolve_window(hours, days)
    window_note = f" (window: last {label})" if label else ""
    console.print(f"[dim]Refreshing all sources{window_note} (BSE full pull can take ~8 min)...[/dim]")
    if since and (datetime.now(_IST) - since) > timedelta(hours=_LARGE_WINDOW_HOURS):
        console.print("[yellow]Large window: the BSE pull re-polls ~498 scrips and may take a while.[/yellow]")
    _render_refresh(_refresh_all(since_override=since))


@app.command()
def scan(skip_refresh: bool = typer.Option(False, "--skip-refresh",
         help="Use already-stored data; don't fetch first (faster)."),
         hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Catch-up refresh, then pre-filter, then write the context pack.

    Intended use: run this, then tell the agent "read the context pack and give
    me today's asymmetric signals." Use --hours/--days to widen the window (the
    pack covers that span, and a non-skipped refresh fetches that far back).
    """
    from scanner.context_pack import build_context_pack

    since, label = resolve_window(hours, days)
    window_note = f" [cyan](window: last {label})[/cyan]" if label else ""

    if not skip_refresh:
        console.print(f"[dim]Catch-up refresh{window_note} (BSE full pull can take ~8 min)...[/dim]")
        _render_refresh(_refresh_all(since_override=since))
    else:
        console.print(f"[dim]--skip-refresh: using stored data{window_note}.[/dim]")

    with console.status("[cyan]Pre-filtering + assembling context pack..."):
        stats = build_context_pack(since=since)

    table = Table(title="Context pack assembled", border_style="green")
    table.add_column("Item"); table.add_column("Count", justify="right")
    table.add_row("Hard filings", f"{stats['filings']} ({stats['filings_tagged']} catalyst-tagged)")
    table.add_row("Investor deals (flagged)", str(stats["investor_deals"]))
    table.add_row("Company news", str(stats["company_news"]))
    table.add_row("Market-wide news", str(stats["market_news"]))
    console.print(table)
    console.print(f"\n[bold]Context pack:[/bold] {stats['md_path']}")
    console.print("[dim]Next: ask the agent to read the context pack and rank today's asymmetric signals.[/dim]")


def _short(text: str, n: int = 100) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _print_ann_block(anns: list[dict]) -> None:
    if not anns:
        console.print("  [dim]none[/dim]")
        return
    for a in anns:
        tags = a.get("candidate_tags")
        import json as _json
        try:
            taglist = _json.loads(tags) if isinstance(tags, str) else (tags or [])
        except _json.JSONDecodeError:
            taglist = []
        tagstr = f" [magenta]{', '.join(taglist)}[/magenta]" if taglist else ""
        when = (a.get("published_at") or "")[:16]
        console.print(f"  [green][FILING][/green] {a.get('symbol','?')} — {when} | {a.get('category','')}{tagstr}")
        console.print(f"    {_short(a.get('headline',''))}")
        if a.get("pdf_url"):
            console.print(f"    [dim]{a['pdf_url']}[/dim]")


def _print_ask_results(anns: list[dict], news: list[dict], deals: list[dict], universe: list[dict]) -> None:
    idx = {c["isin"]: c for c in universe}
    console.print(f"\n[bold]Hard filings ({len(anns)}):[/bold]")
    _print_ann_block(anns)

    console.print(f"\n[bold]Deals ({len(deals)}):[/bold]")
    if not deals:
        console.print("  [dim]none[/dim]")
    for d in deals:
        flag = "MARQUEE" if d.get("is_marquee") else ("PROMOTER" if d.get("is_promoter_buy") else "")
        who = d.get("matched_investor") or d.get("client_name") or "?"
        console.print(f"  [yellow][DEAL][/yellow] {d.get('symbol','?')} {d.get('deal_type','')} [{flag}] — "
                      f"{who} {d.get('side','')} {d.get('qty')} ({(d.get('date') or '')[:10]})")
        if d.get("url"):
            console.print(f"    [dim]{d['url']}[/dim]")

    console.print(f"\n[bold]News ({len(news)}):[/bold]")
    if not news:
        console.print("  [dim]none[/dim]")
    import json as _json
    for n in news:
        raw = n.get("company_isins")
        try:
            isins = _json.loads(raw) if isinstance(raw, str) and raw not in ("", "[]") else []
        except _json.JSONDecodeError:
            isins = []
        syms = ", ".join(idx.get(i, {}).get("symbol", "?") for i in isins) or "?"
        console.print(f"  [blue][NEWS][/blue] {syms} — {(n.get('published_at') or '')[:16]} | {n.get('source','')}")
        console.print(f"    {_short(n.get('headline',''))}")
        if n.get("url"):
            console.print(f"    [dim]{n['url']}[/dim]")


def _resolve_companies(question: str, universe: list[dict]) -> list[dict]:
    """Lenient company resolver for a user's explicit question (recall-first).

    A user naming a company wants a match, so this is more permissive than the
    news Tagger: case-insensitive whole-word match on ticker or any alias>=4.
    """
    import re
    q = question.lower()
    hits, seen = [], set()
    for c in universe:
        if c["isin"] in seen:
            continue
        candidates = [(c.get("symbol") or "").lower()] + [a for a in c.get("aliases", [])]
        for cand in candidates:
            if len(cand) < 4 and cand != (c.get("symbol") or "").lower():
                continue
            if cand and re.search(rf"(?<![a-z0-9]){re.escape(cand)}(?![a-z0-9])", q):
                hits.append(c)
                seen.add(c["isin"])
                break
    return hits


@app.command()
def ask(question: str = typer.Argument(..., help="A question about a company / tag / date."),
        fetch: bool = typer.Option(False, "--fetch",
            help="Do a fresh targeted BSE pull for the resolved company first."),
        hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Print stored data relevant to a follow-up question (for the agent to reason over).

    --hours/--days restrict the results (and the --fetch pull-back) to that window;
    without them, recent stored data is shown.
    """
    from scanner import ingest_bse, store
    from scanner.http import PoliteSession
    from scanner.ingest_bse import _now_ist
    from scanner.prefilter import tag_catalysts
    from scanner.universe import load_map

    since, label = resolve_window(hours, days)
    since_iso = since.isoformat() if since else None

    store.init_db()
    universe = load_map()
    companies = _resolve_companies(question, universe)
    tags = tag_catalysts(question)

    if fetch and companies:
        # Window controls how far back the fresh pull goes; default 7 days.
        fetch_since = since or (_now_ist() - timedelta(days=7))
        codes = [str(c["bse_code"]) for c in companies if c.get("bse_code")]
        with console.status(f"[cyan]Fresh BSE pull for {', '.join(c['symbol'] for c in companies)}..."):
            items = ingest_bse.ingest(session=PoliteSession(), since=fetch_since, scrip_codes=codes)
            store.upsert_announcements(items)
        console.print(f"[dim]Fetched {len(items)} recent filings for the resolved company(ies).[/dim]")

    console.print(Panel.fit(
        f"[bold]Question:[/bold] {question}\n"
        f"Resolved companies: {', '.join(c['symbol'] for c in companies) or '—'}\n"
        f"Detected catalyst tags: {', '.join(tags) or '—'}\n"
        f"Window: {('last ' + label) if label else 'recent (default)'}",
        title="ask", border_style="cyan"))

    isins = [c["isin"] for c in companies]
    if isins:
        anns = store.announcements_for_isins(isins, limit=40, since_iso=since_iso)
        news = store.news_for_isins(isins, limit=40, since_iso=since_iso)
        deals = store.deals_for_isins(isins, limit=40, since_iso=since_iso)
        _print_ask_results(anns, news, deals, universe)
        if not (anns or news or deals):
            console.print("[dim]No stored data for this company in-window. Re-run with [bold]--fetch[/bold] or widen --days.[/dim]")
    elif tags:
        for t in tags:
            anns = store.announcements_by_tag(t, limit=30, since_iso=since_iso)
            console.print(f"\n[bold]Filings tagged[/bold] '{t}': {len(anns)}")
            _print_ann_block(anns)
    else:
        console.print("[yellow]No company or catalyst tag recognised in the question.[/yellow]")
        console.print("[dim]Tip: name a company (e.g. 'GPIL', 'Tata Motors') and add --fetch for a fresh pull.[/dim]")


@app.command()
def digest(hours: int = _HOURS_OPT, days: int = _DAYS_OPT) -> None:
    """Generate and save a dated markdown digest to digests/.

    In agent mode (default) the digest is the deterministic *candidate set* with
    sources — the ranked interpretation is produced live by the agent. If
    scoring.mode is 'llm_api' (future hook), the LLM scorer would rank it here.
    Use --hours/--days for a custom span (e.g. --days 7 for a weekly digest).
    """
    from pathlib import Path
    from scanner.context_pack import build_context_pack
    from scanner.config import resolve_path
    from scanner.scoring import llm_scorer

    settings = load_settings()
    since, _label = resolve_window(hours, days)
    stats = build_context_pack(since=since)
    md = Path(stats["md_path"]).read_text(encoding="utf-8")

    note = ("_Deterministic candidate set (sourced leads). In agent mode the ranking is "
            "produced live by the agent reading this pack._")
    if llm_scorer.is_enabled(settings):
        note = "[note] scoring.mode=llm_api is set but the LLM scorer is a future stub; saved candidate set instead."
        console.print(f"[yellow]{note}[/yellow]")

    today = datetime.now(_IST).strftime("%Y-%m-%d")
    digest_dir = resolve_path(settings.get("output", {}).get("digest_dir", "digests"))
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{today}.md"
    path.write_text(f"# Daily Digest — {today}\n\n{note}\n\n---\n\n{md}", encoding="utf-8")
    console.print(f"[green]Digest saved:[/green] {path}")


@app.command(name="setup-universe")
def setup_universe() -> None:
    """(Re)build the Nifty 500 <-> BSE map (joined on ISIN)."""
    from scanner.universe import build_map

    with console.status("[cyan]Fetching Nifty 500 list + BSE scrip master..."):
        stats = build_map()

    table = Table(title="Universe built", border_style="green")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Nifty 500 rows fetched", str(stats["nifty_count"]))
    table.add_row("BSE master securities", str(stats["bse_master_count"]))
    table.add_row("Matched (ISIN join)", f"[bold green]{stats['matched']}[/bold green]")
    table.add_row("Unmatched", str(stats["unmatched"]))
    console.print(table)
    if stats["unmatched"]:
        console.print(f"[yellow]Unmatched samples:[/yellow] {', '.join(stats['unmatched_samples'])}")
    console.print(f"[dim]Map written to {stats['out_dir']}[/dim]")


@app.command()
def schedule(install: bool = typer.Option(False, "--install", help="Actually create the scheduled tasks."),
             remove: bool = typer.Option(False, "--remove", help="Delete the scheduled tasks.")) -> None:
    """Print (or install) the Windows Task Scheduler jobs for background refresh.

    Two jobs: a 45-minute recurring refresh while the laptop is on, plus an
    evening catch-up at 20:00 local (≈8pm IST) after the day's filings.
    """
    import subprocess

    root = resolve_path(".")
    bat = root / "scheduled_refresh.bat"
    name_45 = "stock-async-opp-refresh-45m"
    name_eve = "stock-async-opp-evening-catchup"

    # schtasks arg lists (avoids shell-quoting pitfalls when we --install).
    create_45 = ["schtasks", "/Create", "/TN", name_45, "/TR", str(bat),
                 "/SC", "MINUTE", "/MO", "45", "/F"]
    create_eve = ["schtasks", "/Create", "/TN", name_eve, "/TR", str(bat),
                  "/SC", "DAILY", "/ST", "20:00", "/F"]

    if remove:
        for name in (name_45, name_eve):
            r = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                               capture_output=True, text=True)
            console.print(f"[{'green' if r.returncode == 0 else 'yellow'}]{(r.stdout or r.stderr).strip()}[/]")
        return

    if install:
        for cmd in (create_45, create_eve):
            r = subprocess.run(cmd, capture_output=True, text=True)
            ok = r.returncode == 0
            console.print(f"[{'green' if ok else 'red'}]{(r.stdout or r.stderr).strip()}[/]")
        console.print("[dim]Installed. Remove with: run.bat schedule --remove[/dim]")
    else:
        console.print(Panel(
            "Run these in an [bold]Administrator[/bold] Command Prompt (or use [bold]--install[/bold]):\n\n"
            f"[cyan]schtasks /Create /TN {name_45} /TR \"{bat}\" /SC MINUTE /MO 45 /F[/cyan]\n\n"
            f"[cyan]schtasks /Create /TN {name_eve} /TR \"{bat}\" /SC DAILY /ST 20:00 /F[/cyan]\n\n"
            "Remove later with [cyan]run.bat schedule --remove[/cyan].",
            title="Windows Task Scheduler", border_style="cyan"))

    console.print(
        "\n[bold yellow]Laptop-only caveat:[/bold yellow] coverage = \"whenever the laptop is on and the task ran.\"\n"
        "Because [bold]scan[/bold] always does a catch-up pull first, opening it at any random time still\n"
        "gets everything since the last successful run — you never miss filings just because the\n"
        "laptop slept. The evening 20:00 job ensures a daily catch-up after market filings close.")


if __name__ == "__main__":
    app()
