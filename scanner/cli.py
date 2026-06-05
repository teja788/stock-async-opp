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

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scanner import __app_name__, __version__
from scanner.config import load_settings, resolve_path

app = typer.Typer(
    name=__app_name__,
    help="On-demand Indian-market catalyst scanner (local, free sources, research-only).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def _main() -> None:
    """Configure file logging once, before any command runs."""
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


def _refresh_all(bse_limit: int | None = None) -> dict[str, dict]:
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
        since = store.get_last_success("bse_announcements")
        codes = None
        if bse_limit:
            codes = [str(c["bse_code"]) for c in universe if c.get("bse_code")][:bse_limit]
        items = ingest_bse.ingest(session=session, since=since, scrip_codes=codes)
        return len(items), store.upsert_announcements(items)

    def _news():
        items = ingest_news.ingest(session=session)
        return len(items), store.upsert_news(items)

    def _deals():
        since = store.get_last_success("deals")
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
def refresh() -> None:
    """Run all ingesters (catch-up since last run) and update SQLite."""
    console.print("[dim]Refreshing all sources (BSE announcements may take ~8 min for the full universe)...[/dim]")
    results = _refresh_all()
    _render_refresh(results)


@app.command()
def scan(skip_refresh: bool = typer.Option(False, "--skip-refresh",
         help="Use already-stored data; don't fetch first (faster).")) -> None:
    """Catch-up refresh, then pre-filter, then write the context pack.

    Intended use: run this, then tell the agent "read the context pack and give
    me today's asymmetric signals."
    """
    from scanner.context_pack import build_context_pack

    if not skip_refresh:
        console.print("[dim]Catch-up refresh (BSE full pull can take ~8 min)...[/dim]")
        _render_refresh(_refresh_all())
    else:
        console.print("[dim]--skip-refresh: using stored data.[/dim]")

    with console.status("[cyan]Pre-filtering + assembling context pack..."):
        stats = build_context_pack()

    table = Table(title="Context pack assembled", border_style="green")
    table.add_column("Item"); table.add_column("Count", justify="right")
    table.add_row("Hard filings", f"{stats['filings']} ({stats['filings_tagged']} catalyst-tagged)")
    table.add_row("Investor deals (flagged)", str(stats["investor_deals"]))
    table.add_row("Company news", str(stats["company_news"]))
    table.add_row("Market-wide news", str(stats["market_news"]))
    console.print(table)
    console.print(f"\n[bold]Context pack:[/bold] {stats['md_path']}")
    console.print("[dim]Next: ask the agent to read the context pack and rank today's asymmetric signals.[/dim]")


@app.command()
def ask(question: str = typer.Argument(..., help="A question about a company / tag / date.")) -> None:
    """Print stored data relevant to a follow-up question."""
    _todo("ask", "Milestone 10")


@app.command()
def digest() -> None:
    """Generate and save a dated markdown digest to digests/."""
    _todo("digest", "Milestone 10")


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
def schedule() -> None:
    """Print/install the Windows Task Scheduler command."""
    _todo("schedule", "Milestone 11")


if __name__ == "__main__":
    app()
