"""Generate a static dashboard into docs/ for GitHub Pages.

Two jobs:
1. SAVE outputs: snapshot the current context pack (md + json) into
   docs/data/packs/ — tracked in git, so scan results survive and publish.
2. RENDER a fully static, self-contained site (inline CSS, no CDNs, no JS
   fetches) from the saved packs, the research log, and the digests.

Host it with GitHub Pages: Settings → Pages → "Deploy from a branch" →
branch `master`, folder `/docs`. Then commit + push after `publish`.

Everything rendered carries the research-only disclaimer.
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scanner import leads
from scanner.config import resolve_path

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

DOCS = resolve_path("docs")
PACKS_DIR = DOCS / "data" / "packs"

_CSS = """
:root { --bg:#0f1217; --card:#161b23; --text:#dbe2ea; --dim:#8b96a5; --acc:#5cc8a5;
        --warn:#e0b34c; --link:#7ab4e8; --border:#242c37; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
main { max-width:1080px; margin:0 auto; padding:24px 20px 60px; }
nav { display:flex; gap:18px; flex-wrap:wrap; padding:14px 20px; background:var(--card);
      border-bottom:1px solid var(--border); position:sticky; top:0; }
nav a { color:var(--link); text-decoration:none; font-weight:600; }
nav .brand { color:var(--acc); }
h1 { font-size:1.5em; } h2 { font-size:1.15em; margin-top:1.6em; color:var(--acc); }
h3 { font-size:1.02em; }
a { color:var(--link); } hr { border:0; border-top:1px solid var(--border); }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 16px; }
.card .num { font-size:1.7em; font-weight:700; color:var(--acc); }
.card .lbl { color:var(--dim); font-size:.85em; }
table { border-collapse:collapse; width:100%; font-size:.92em; }
th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--border); }
th { color:var(--dim); font-weight:600; }
.pos { color:var(--acc); } .neg { color:#e07a6a; } .dim { color:var(--dim); }
.pill { display:inline-block; background:#22304a; color:var(--link); border-radius:20px;
        padding:1px 10px; font-size:.8em; margin:1px 2px; }
.md { background:var(--card); border:1px solid var(--border); border-radius:10px;
      padding:6px 22px 16px; overflow-x:auto; }
.md blockquote { border-left:3px solid var(--warn); margin:8px 0; padding:2px 12px; color:var(--dim); }
.md li { margin:3px 0; }
footer { color:var(--dim); font-size:.82em; margin-top:40px; border-top:1px solid var(--border); padding-top:12px; }
.scroll { overflow-x:auto; }
"""

_DISCLAIMER = ("Personal research idea-generation only — sourced leads to investigate, "
               "never buy/sell advice, never certainty. Not investment advice.")


# --------------------------------------------------------------------------- #
# Minimal Markdown -> HTML (covers the subset our own outputs use)
# --------------------------------------------------------------------------- #
def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    # bare URLs -> links (source lines in packs are bare)
    text = re.sub(r"(?<![\">])(https?://[^\s<]+)",
                  r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def md_to_html(md: str) -> str:
    """Small, deterministic converter for our own markdown outputs."""
    out: list[str] = []
    in_ul = in_ol = False

    def _close():
        nonlocal in_ul, in_ol
        if in_ul: out.append("</ul>"); in_ul = False
        if in_ol: out.append("</ol>"); in_ol = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*<!--.*-->\s*$", line):
            continue  # dedupe markers in the research log
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            _close()
            lvl = min(len(m.group(1)) + 1, 5)  # page h1 is reserved
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue
        if re.match(r"^\s*---+\s*$", line):
            _close(); out.append("<hr>"); continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_ul: _close(); out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(m.group(1))}</li>"); continue
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            if not in_ol: _close(); out.append("<ol>"); in_ol = True
            out.append(f"<li>{_inline(m.group(1))}</li>"); continue
        m = re.match(r"^>\s?(.*)$", line)
        if m:
            _close(); out.append(f"<blockquote>{_inline(m.group(1))}</blockquote>"); continue
        if not line.strip():
            _close(); continue
        _close()
        out.append(f"<p>{_inline(line)}</p>")
    _close()
    return "\n".join(out)


def _page(title: str, body: str, generated: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body>
<nav><a class="brand" href="index.html">📡 stock-async-opp</a>
<a href="pack.html">Latest pack</a><a href="log.html">Research log</a>
<a href="review.html">Lead review</a><a href="digests.html">Digests</a></nav>
<main>
{body}
<footer>Generated {generated} · {_DISCLAIMER}</footer>
</main></body></html>"""


# --------------------------------------------------------------------------- #
# Output saving (snapshot the context pack into tracked docs/data/packs/)
# --------------------------------------------------------------------------- #
def snapshot_pack() -> str | None:
    """Copy the current runtime context pack into docs/data/packs/ (tracked).

    Snapshot name comes from the pack's own generated_at, so re-publishing an
    unchanged pack is a no-op. Returns the snapshot stem or None.
    """
    md_path = resolve_path("runtime/context_pack.md")
    json_path = md_path.with_suffix(".json")
    if not (md_path.exists() and json_path.exists()):
        return None
    try:
        generated = json.loads(json_path.read_text(encoding="utf-8"))["generated_at"]
        stamp = datetime.fromisoformat(generated).strftime("%Y%m%d-%H%M")
    except (json.JSONDecodeError, KeyError, ValueError):
        stamp = datetime.fromtimestamp(md_path.stat().st_mtime, IST).strftime("%Y%m%d-%H%M")
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"pack-{stamp}"
    if not (PACKS_DIR / f"{stem}.md").exists():
        shutil.copyfile(md_path, PACKS_DIR / f"{stem}.md")
        shutil.copyfile(json_path, PACKS_DIR / f"{stem}.json")
        log.info("Pack snapshot saved: docs/data/packs/%s.*", stem)
    return stem


def _latest_pack() -> tuple[dict[str, Any] | None, str | None]:
    """(pack_json, md_text) of the newest saved snapshot."""
    snaps = sorted(PACKS_DIR.glob("pack-*.json"))
    if not snaps:
        return None, None
    latest = snaps[-1]
    try:
        pack = json.loads(latest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, None
    md_file = latest.with_suffix(".md")
    return pack, (md_file.read_text(encoding="utf-8") if md_file.exists() else None)


# --------------------------------------------------------------------------- #
# Page builders
# --------------------------------------------------------------------------- #
def _index_page(pack: dict[str, Any] | None, review_summary: dict | None,
                log_entries: list[dict], digest_files: list[Path], now: str) -> str:
    body = ["<h1>Asymmetric-opportunity scanner — saved outputs</h1>",
            f"<blockquote>{_DISCLAIMER}</blockquote>"]
    if pack:
        s = pack.get("stats", {})
        body.append(f"<p class='dim'>Latest pack: window since {html.escape(str(pack.get('window_since',''))[:16])} "
                    f"· generated {html.escape(str(pack.get('generated_at',''))[:16])}</p>")
        body.append("<div class='cards'>")
        for lbl, key in (("Hard filings", "filings"), ("Investor deals", "investor_deals"),
                         ("Rating actions", "rating_actions"), ("Company news", "company_news")):
            body.append(f"<div class='card'><div class='num'>{s.get(key, 0)}</div>"
                        f"<div class='lbl'>{lbl}</div></div>")
        body.append("</div>")
        conf = pack.get("confluence") or []
        if conf:
            body.append("<h2>Confluence (≥2 independent signals)</h2><div class='scroll'><table>"
                        "<tr><th>Company</th><th>Mcap ₹cr</th><th>Signals</th></tr>")
            for c in conf[:12]:
                sig = "".join(f"<span class='pill'>{html.escape(x)}</span>" for x in c.get("signals", []))
                mc = c.get("market_cap_cr")
                body.append(f"<tr><td>{html.escape(str(c.get('symbol') or '?'))} "
                            f"<span class='dim'>{html.escape(str(c.get('company') or ''))}</span></td>"
                            f"<td>{mc:,.0f}</td><td>{sig}</td></tr>" if mc else
                            f"<tr><td>{html.escape(str(c.get('symbol') or '?'))}</td><td>—</td><td>{sig}</td></tr>")
            body.append("</table></div>")
        acc = pack.get("insider_accumulation") or []
        if acc:
            body.append("<h2>Insider accumulation (trailing 90d)</h2><div class='scroll'><table>"
                        "<tr><th>Company</th><th>Buys</th><th>Stake +pp</th><th>Crossed 5%</th></tr>")
            for r in acc[:10]:
                body.append(f"<tr><td>{html.escape(str(r.get('symbol') or '?'))}</td>"
                            f"<td>{r.get('n_buys')}</td><td>{(r.get('cum_pct') or 0):+.2f}</td>"
                            f"<td>{'✅' if r.get('crossed_5pct') else ''}</td></tr>")
            body.append("</table></div>")
    else:
        body.append("<p>No saved context packs yet — run <code>scan</code> then <code>publish</code>.</p>")

    if review_summary:
        r = review_summary
        body.append("<h2>Lead review</h2><div class='cards'>"
                    f"<div class='card'><div class='num'>{r['positive']}/{r['scored']}</div><div class='lbl'>leads positive</div></div>"
                    f"<div class='card'><div class='num'>{r['median']:+.1f}%</div><div class='lbl'>median move</div></div>"
                    f"<div class='card'><div class='num'>{r['average']:+.1f}%</div><div class='lbl'>average move</div></div>"
                    "</div><p><a href='review.html'>Full table →</a></p>")

    if log_entries:
        e = log_entries[0]
        body.append(f"<h2>Latest signals — {html.escape(e['stamp'])}</h2>"
                    f"<div class='md'>{md_to_html(e['content'])}</div>"
                    "<p><a href='log.html'>All research-log entries →</a></p>")

    if digest_files:
        body.append("<h2>Digests</h2><ul>")
        for p in digest_files[:8]:
            body.append(f"<li><a href='digest-{p.stem}.html'>{p.stem}</a></li>")
        body.append("</ul>")
    return _page("stock-async-opp — dashboard", "\n".join(body), now)


def _parse_log() -> list[dict[str, str]]:
    path = leads.LOG_PATH
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    entries = []
    for m in re.finditer(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2} IST) — (.+?)$([\s\S]*?)(?=^## \d{4}-|\Z)",
                         text, re.MULTILINE):
        entries.append({"stamp": m.group(1), "title": m.group(2).strip(),
                        "content": m.group(3).strip()})
    entries.reverse()  # newest first
    return entries


def build_site() -> dict[str, Any]:
    """Snapshot the current pack, then (re)generate the whole static site."""
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    snapshot_pack()
    pack, pack_md = _latest_pack()
    log_entries = _parse_log()
    digest_files = sorted((p for p in resolve_path("digests").glob("*.md")
                           if p.name != "research_log.md"), reverse=True)

    # Lead review (best-effort: needs the universe map + price history).
    scored: list[dict[str, Any]] = []
    summary = None
    try:
        from scanner.universe import load_map
        scored = leads.score_leads(load_map())
        summary = leads.summarize(scored)
    except Exception as exc:  # noqa: BLE001 - site build must not die on this
        log.warning("Lead scoring skipped: %s", exc)

    # index
    (DOCS / "index.html").write_text(
        _index_page(pack, summary, log_entries, digest_files, now), encoding="utf-8")

    # latest pack page + archive pages
    archive_links = []
    for snap in sorted(PACKS_DIR.glob("pack-*.md"), reverse=True):
        page_name = f"{snap.stem}.html"
        archive_links.append(f"<li><a href='{page_name}'>{snap.stem}</a></li>")
        (DOCS / page_name).write_text(
            _page(snap.stem, f"<h1>{snap.stem}</h1><div class='md'>"
                  f"{md_to_html(snap.read_text(encoding='utf-8'))}</div>", now),
            encoding="utf-8")
    pack_body = ["<h1>Latest context pack</h1>"]
    pack_body.append(f"<div class='md'>{md_to_html(pack_md)}</div>" if pack_md
                     else "<p>No pack saved yet.</p>")
    if archive_links:
        pack_body.append("<h2>All saved packs</h2><ul>" + "".join(archive_links) + "</ul>")
    (DOCS / "pack.html").write_text(_page("Latest pack", "\n".join(pack_body), now), encoding="utf-8")

    # research log
    log_body = ["<h1>Research log</h1>"]
    for e in log_entries:
        log_body.append(f"<h2>{html.escape(e['stamp'])} — {html.escape(e['title'])}</h2>"
                        f"<div class='md'>{md_to_html(e['content'])}</div>")
    if not log_entries:
        log_body.append("<p>No research-log entries yet.</p>")
    (DOCS / "log.html").write_text(_page("Research log", "\n".join(log_body), now), encoding="utf-8")

    # review
    rev_body = ["<h1>Lead review — calibration, not a performance record</h1>"]
    if scored:
        rev_body.append("<div class='scroll'><table><tr><th>Logged</th><th>Ticker</th>"
                        "<th>Then</th><th>Now</th><th>Move</th><th>Age (d)</th></tr>")
        for s in scored:
            if s["pct_move"] is not None:
                cls = "pos" if s["pct_move"] > 0 else "neg"
                move = f"<span class='{cls}'>{s['pct_move']:+.1f}%</span>"
                then_s, now_s = f"{s['then_close']:,.1f}", f"{s['now_close']:,.1f}"
            else:
                move, then_s, now_s = "<span class='dim'>no price data</span>", "—", "—"
            rev_body.append(f"<tr><td>{s['date']}</td><td>{html.escape(s['ticker'])}</td>"
                            f"<td>{then_s}</td><td>{now_s}</td><td>{move}</td><td>{s['age_days']}</td></tr>")
        rev_body.append("</table></div>")
    else:
        rev_body.append("<p>No scoreable leads yet — leads appear here once the research "
                        "log has flagged items and price history covers their dates.</p>")
    (DOCS / "review.html").write_text(_page("Lead review", "\n".join(rev_body), now), encoding="utf-8")

    # digests
    dig_body = ["<h1>Digests</h1><ul>"]
    for p in digest_files:
        dig_body.append(f"<li><a href='digest-{p.stem}.html'>{p.stem}</a></li>")
        (DOCS / f"digest-{p.stem}.html").write_text(
            _page(p.stem, f"<h1>Digest {p.stem}</h1><div class='md'>"
                  f"{md_to_html(p.read_text(encoding='utf-8'))}</div>", now),
            encoding="utf-8")
    dig_body.append("</ul>" if digest_files else "</ul><p>No digests saved yet.</p>")
    (DOCS / "digests.html").write_text(_page("Digests", "\n".join(dig_body), now), encoding="utf-8")

    pages = len(list(DOCS.glob("*.html")))
    return {"docs_dir": str(DOCS), "pages": pages,
            "packs_saved": len(list(PACKS_DIR.glob("pack-*.md"))),
            "log_entries": len(log_entries), "digests": len(digest_files)}
