"""Research Trail Milestone 1A: a dependency-free local web app."""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


STOP_WORDS = frozenset(
    "a an and are as at be by for from how in is it of on or that the this to what when where which who why with".split()
)
TOKEN_RE = re.compile(r"[a-z0-9]+")
FEEDBACK_VALUES = {"", "useful", "not useful"}
UNKNOWN = "unknown"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def terms(text: str) -> set[str]:
    """Return normalized, meaningful keywords from user-entered text."""
    return {token for token in TOKEN_RE.findall(text.lower()) if token not in STOP_WORDS}


def paper_link(doi: str, url: str) -> str:
    """Return a resolver URL for a DOI, otherwise the supplied HTTP(S) URL."""
    doi = doi.strip()
    if doi:
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
        return f"https://doi.org/{doi}"
    return url.strip()


def valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_paper(data: dict[str, str]) -> list[str]:
    errors = []
    if not data.get("title", "").strip():
        errors.append("Title is required.")
    if not data.get("source_text", "").strip():
        errors.append("Abstract or source excerpt is required.")
    doi, url = data.get("doi", "").strip(), data.get("url", "").strip()
    if not doi and not url:
        errors.append("Enter a DOI or URL.")
    if url and not valid_http_url(url):
        errors.append("URL must start with http:// or https:// and include a host.")
    if doi and not re.match(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/\S+$", doi, re.I):
        errors.append("DOI must look like 10.xxxx/example or a doi.org URL.")
    return errors


@dataclass(frozen=True)
class RankedPaper:
    paper: sqlite3.Row
    score: int
    matched_terms: tuple[str, ...]
    matches_by_field: dict[str, tuple[str, ...]]
    excerpts_by_field: dict[str, str]


def matched_excerpt(text: str, matched: set[str], context: int = 55) -> str:
    """Return a short, exact slice of text around its first matched term."""
    matches = [match for match in TOKEN_RE.finditer(text.lower()) if match.group() in matched]
    if not matches:
        return ""
    first, last = matches[0], matches[-1]
    if last.end() - first.start() > context * 2:
        last = first
    start = max(0, first.start() - context)
    end = min(len(text), last.end() + context)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def rank_papers(question: str, papers: list[sqlite3.Row]) -> list[RankedPaper]:
    """Rank by unique question matches: title=2 points, source text=1 point."""
    question_terms = terms(question)
    ranked = []
    for paper in papers:
        title_matches = question_terms & terms(paper["title"])
        source_matches = (
            question_terms & terms(paper["source_text"])
            if paper["source_text_reviewed"]
            else set()
        )
        all_matches = tuple(sorted(title_matches | source_matches))
        ranked.append(
            RankedPaper(
                paper=paper,
                score=2 * len(title_matches) + len(source_matches),
                matched_terms=all_matches,
                matches_by_field={
                    "Title": tuple(sorted(title_matches)),
                    "Source text": tuple(sorted(source_matches)),
                },
                excerpts_by_field={
                    "Title": matched_excerpt(paper["title"], title_matches),
                    "Source text": matched_excerpt(paper["source_text"], source_matches),
                },
            )
        )
    return sorted(ranked, key=lambda item: (-item.score, item.paper["id"]))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                question TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_text TEXT NOT NULL,
                source_text_reviewed INTEGER NOT NULL DEFAULT 1 CHECK (source_text_reviewed IN (0, 1)),
                notes TEXT NOT NULL DEFAULT '',
                doi TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'unknown',
                access_level TEXT NOT NULL DEFAULT 'unknown',
                license TEXT NOT NULL DEFAULT 'unknown',
                feedback TEXT NOT NULL DEFAULT '' CHECK (feedback IN ('', 'useful', 'not useful')),
                created_at TEXT NOT NULL
            );
            """
        )
        # Milestone 1A originally combined source text and notes. Preserve existing
        # local data by treating that legacy field as source text during upgrade.
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(papers)")}
        if "source_text" not in columns:
            self.connection.executescript(
                """
                ALTER TABLE papers RENAME TO papers_legacy;
                CREATE TABLE papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    source_text_reviewed INTEGER NOT NULL DEFAULT 1 CHECK (source_text_reviewed IN (0, 1)),
                    notes TEXT NOT NULL DEFAULT '',
                    doi TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'unknown',
                    access_level TEXT NOT NULL DEFAULT 'unknown',
                    license TEXT NOT NULL DEFAULT 'unknown',
                    feedback TEXT NOT NULL DEFAULT '' CHECK (feedback IN ('', 'useful', 'not useful')),
                    created_at TEXT NOT NULL
                );
                INSERT INTO papers
                    (id, title, source_text, source_text_reviewed, notes, doi, url, source, access_level, license, feedback, created_at)
                SELECT id, title, abstract_or_notes, 0, '', doi, url, source, access_level, license, feedback, created_at
                FROM papers_legacy;
                DROP TABLE papers_legacy;
                """
            )
        elif "source_text_reviewed" not in columns:
            self.connection.execute(
                "ALTER TABLE papers ADD COLUMN source_text_reviewed INTEGER NOT NULL DEFAULT 1 "
                "CHECK (source_text_reviewed IN (0, 1))"
            )
        timestamp = now()
        self.connection.execute(
            "INSERT OR IGNORE INTO workspace (id, question, created_at, updated_at) VALUES (1, '', ?, ?)",
            (timestamp, timestamp),
        )
        self.connection.commit()

    def question(self) -> str:
        with self._lock:
            return self.connection.execute("SELECT question FROM workspace WHERE id = 1").fetchone()[0]

    def save_question(self, question: str) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE workspace SET question = ?, updated_at = ? WHERE id = 1", (question.strip(), now())
            )
            self.connection.commit()

    def papers(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connection.execute("SELECT * FROM papers ORDER BY id"))

    def paper(self, paper_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()

    @staticmethod
    def _paper_values(data: dict[str, str]) -> tuple[str, ...]:
        return (
            data["title"].strip(),
            data["source_text"].strip(),
            data.get("notes", "").strip(),
            data.get("doi", "").strip(),
            data.get("url", "").strip(),
            data.get("source", "").strip() or UNKNOWN,
            data.get("access_level", "").strip() or UNKNOWN,
            data.get("license", "").strip() or UNKNOWN,
        )

    def add_paper(self, data: dict[str, str]) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO papers
                   (title, source_text, notes, doi, url, source, access_level, license, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*self._paper_values(data), now()),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    def update_paper(self, paper_id: int, data: dict[str, str]) -> None:
        with self._lock:
            self.connection.execute(
                """UPDATE papers SET title = ?, source_text = ?, source_text_reviewed = 1,
                   notes = ?, doi = ?, url = ?, source = ?, access_level = ?, license = ? WHERE id = ?""",
                (*self._paper_values(data), paper_id),
            )
            self.connection.commit()

    def delete_paper(self, paper_id: int) -> None:
        with self._lock:
            self.connection.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
            self.connection.commit()

    def save_feedback(self, paper_id: int, feedback: str) -> None:
        if feedback not in FEEDBACK_VALUES:
            raise ValueError("Invalid feedback")
        with self._lock:
            self.connection.execute("UPDATE papers SET feedback = ? WHERE id = ?", (feedback, paper_id))
            self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()


CSS = """
:root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #18332d; background: #f3f0e8; }
* { box-sizing: border-box; }
body { margin: 0; }
header { padding: 2rem max(1.25rem, calc((100% - 1100px) / 2)); background: #173f36; color: white; }
header h1 { margin: 0 0 .3rem; font-size: 2rem; } header p { margin: 0; color: #cfe4d9; }
main { max-width: 1100px; margin: auto; padding: 1.5rem; }
.grid { display: grid; grid-template-columns: minmax(280px, .8fr) minmax(360px, 1.2fr); gap: 1.5rem; align-items: start; }
.panel, .card { background: white; border: 1px solid #d8d4ca; border-radius: 12px; box-shadow: 0 3px 12px #173f3610; }
.panel { padding: 1.25rem; margin-bottom: 1.25rem; } .card { padding: 1rem; margin-bottom: .8rem; }
h2 { margin-top: 0; font-size: 1.2rem; } h3 { margin: 0; font-size: 1.05rem; }
label { display: block; font-weight: 650; margin: .8rem 0 .3rem; }
input, textarea, select { width: 100%; padding: .65rem; border: 1px solid #aaa79f; border-radius: 7px; font: inherit; background: #fff; }
textarea { min-height: 95px; resize: vertical; }
button, .button { border: 0; border-radius: 7px; padding: .62rem .85rem; background: #1f6755; color: white; font: inherit; font-weight: 700; cursor: pointer; text-decoration: none; display: inline-block; }
.secondary { background: #e6ece9; color: #18332d; } .danger { background: transparent; color: #9c302d; padding-left: .25rem; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; margin-top: .8rem; } .actions form { margin: 0; }
.meta, .hint { color: #5f6965; font-size: .9rem; } .metadata { display: flex; gap: .5rem; flex-wrap: wrap; margin: .7rem 0; }
.pill { padding: .22rem .48rem; background: #edf2ef; border-radius: 99px; font-size: .82rem; }
.rank { float: left; width: 2rem; height: 2rem; margin-right: .7rem; border-radius: 50%; background: #e7a73d; display: grid; place-items: center; font-weight: 800; }
.reason { margin: .8rem 0; padding: .7rem; background: #f3f7f5; border-left: 3px solid #4e8b77; font-size: .9rem; clear: both; }
.notes { margin: .8rem 0; padding: .7rem; background: #faf7ef; border-left: 3px solid #c7ab6c; font-size: .9rem; white-space: pre-wrap; }
.error { background: #fff0ef; color: #862824; padding: .7rem; border-radius: 7px; }
.warning { background: #fff4d6; color: #684d0b; padding: .7rem; border-radius: 7px; margin-bottom: .8rem; }
.empty { text-align: center; color: #68736e; padding: 2rem; border: 1px dashed #aaa79f; border-radius: 10px; }
.feedback { display: flex; align-items: center; gap: .5rem; } .feedback select { width: auto; }
@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } header { padding: 1.4rem; } main { padding: 1rem; } }
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def layout(content: str, title: str = "Research Trail") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style></head><body><header><h1>Research Trail</h1>
<p>A small, explainable queue for the papers you choose.</p></header><main>{content}</main></body></html>"""


def paper_form(values: dict[str, str] | sqlite3.Row | None = None, errors: list[str] | None = None, action: str = "/papers") -> str:
    values = values or {}
    get = lambda key: esc(values[key] if key in values else "")
    error_html = "" if not errors else '<div class="error">' + " ".join(esc(e) for e in errors) + "</div>"
    review_html = (
        '<div class="warning"><strong>Source text needs review.</strong> This text came from the old combined '
        'abstract/notes field and is excluded from ranking. Confirm or replace it, then save this paper.</div>'
        if "source_text_reviewed" in values.keys() and not values["source_text_reviewed"]
        else ""
    )
    label = "Save changes" if action != "/papers" else "Add paper"
    return f"""{error_html}{review_html}<form method="post" action="{esc(action)}">
<label for="title">Title</label><input id="title" name="title" required value="{get('title')}">
<label for="source-text">Abstract or source excerpt</label><textarea id="source-text" name="source_text" required>{get('source_text')}</textarea>
<p class="hint">Paste paper text here. This field is used for ranking.</p>
<label for="notes">My notes</label><textarea id="notes" name="notes">{get('notes')}</textarea>
<p class="hint">Your notes stay separate and do not affect ranking.</p>
<label for="doi">DOI</label><input id="doi" name="doi" placeholder="10.1000/example" value="{get('doi')}">
<label for="url">URL</label><input id="url" name="url" type="url" placeholder="https://…" value="{get('url')}">
<p class="hint">A DOI or URL is required.</p>
<label for="source">Source</label><input id="source" name="source" placeholder="unknown" value="{get('source')}">
<label for="access">Access level</label><input id="access" name="access_level" placeholder="unknown" value="{get('access_level')}">
<label for="license">License</label><input id="license" name="license" placeholder="unknown" value="{get('license')}">
<p class="hint">Blank provenance fields are saved as “unknown”.</p>
<div class="actions"><button type="submit">{label}</button>{'<a class="button secondary" href="/">Cancel</a>' if action != '/papers' else ''}</div></form>"""


def queue_card(item: RankedPaper, position: int) -> str:
    paper = item.paper
    review_warning = (
        '<div class="warning"><strong>Source text needs review.</strong> It was migrated from the old combined '
        'abstract/notes field and is not used as source evidence or included in this score. '
        f'<a href="/papers/{paper["id"]}/edit">Review it now</a>.</div>'
        if not paper["source_text_reviewed"]
        else ""
    )
    field_reasons = []
    for field, matches in item.matches_by_field.items():
        if matches:
            field_reasons.append(
                f'<strong>{esc(field)}:</strong> “{esc(item.excerpts_by_field[field])}” '
                f'<span class="meta">(matched: {esc(", ".join(matches))})</span>'
            )
    question_matches = ", ".join(item.matched_terms) if item.matched_terms else "none"
    link = paper_link(paper["doi"], paper["url"])
    selected = lambda value: " selected" if paper["feedback"] == value else ""
    return f"""<article class="card"><div class="rank">{position}</div><h3>{esc(paper['title'])}</h3>
<div class="metadata"><span class="pill">Score {item.score}</span><span class="pill">Source: {esc(paper['source'])}</span>
<span class="pill">Access: {esc(paper['access_level'])}</span><span class="pill">License: {esc(paper['license'])}</span></div>
{review_warning}
<div class="reason"><strong>Matched question terms:</strong> {esc(question_matches)}.<br>{'<br>'.join(field_reasons) if field_reasons else 'No title or source-text keywords matched.'}</div>
{f'<div class="notes"><strong>My notes (not ranked):</strong> {esc(paper["notes"])}</div>' if paper['notes'] else ''}
<div class="actions"><a class="button" href="{esc(link)}" target="_blank" rel="noopener noreferrer">Open paper ↗</a>
<a class="button secondary" href="/papers/{paper['id']}/edit">Edit</a>
<form class="feedback" method="post" action="/papers/{paper['id']}/feedback"><label for="feedback-{paper['id']}" class="meta">Suggestion</label>
<select id="feedback-{paper['id']}" name="feedback" onchange="this.form.submit()"><option value=""{selected('')}>Not rated</option>
<option value="useful"{selected('useful')}>Useful</option><option value="not useful"{selected('not useful')}>Not useful</option></select>
<noscript><button>Save feedback</button></noscript></form>
<form method="post" action="/papers/{paper['id']}/delete"><button class="danger" onclick="return confirm('Remove this paper?')">Remove</button></form></div></article>"""


def home_page(store: Store, form_values=None, errors=None) -> str:
    question = store.question()
    papers = store.papers()
    ranked = rank_papers(question, papers)
    queue = "".join(queue_card(item, index) for index, item in enumerate(ranked, 1))
    if not queue:
        queue = '<div class="empty">Add your first paper to build the queue.</div>'
    target = f"{len(papers)} saved · target 5–10 for evaluation"
    return layout(f"""<section class="panel"><h2>Research question</h2>
<form method="post" action="/question"><label for="question">What are you investigating?</label>
<textarea id="question" name="question" required>{esc(question)}</textarea><button type="submit">Save question</button></form></section>
<div class="grid"><section class="panel"><h2>Add a paper</h2><p class="hint">Add papers one at a time. There is no minimum required to save.</p>
{paper_form(form_values, errors)}</section><section><h2>Reading queue</h2><p class="meta">{esc(target)} · Title matches score 2; source-text matches score 1. Notes are not ranked.</p>{queue}</section></div>""")


class AppHandler(BaseHTTPRequestHandler):
    store: Store

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def redirect_home(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        parsed = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def route_id(self, suffix: str) -> int | None:
        match = re.fullmatch(r"/papers/(\d+)/" + suffix, urlparse(self.path).path)
        return int(match.group(1)) if match else None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_html(home_page(self.store))
            return
        paper_id = self.route_id("edit")
        paper = self.store.paper(paper_id) if paper_id is not None else None
        if paper:
            self.send_html(layout(f'<section class="panel"><h2>Edit paper</h2>{paper_form(paper, action=f"/papers/{paper_id}/edit")}</section>', "Edit paper · Research Trail"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path, data = urlparse(self.path).path, self.form_data()
        if path == "/question":
            question = data.get("question", "").strip()
            if not question:
                self.send_html(layout('<div class="error">Research question is required.</div><p><a href="/">Return home</a></p>'), HTTPStatus.BAD_REQUEST)
                return
            self.store.save_question(question)
            self.redirect_home()
            return
        if path == "/papers":
            errors = validate_paper(data)
            if errors:
                self.send_html(home_page(self.store, data, errors), HTTPStatus.BAD_REQUEST)
            else:
                self.store.add_paper(data)
                self.redirect_home()
            return
        for suffix, action in (("edit", "edit"), ("feedback", "feedback"), ("delete", "delete")):
            paper_id = self.route_id(suffix)
            if paper_id is None:
                continue
            if not self.store.paper(paper_id):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if action == "edit":
                errors = validate_paper(data)
                if errors:
                    self.send_html(layout(f'<section class="panel"><h2>Edit paper</h2>{paper_form(data, errors, f"/papers/{paper_id}/edit")}</section>'), HTTPStatus.BAD_REQUEST)
                    return
                self.store.update_paper(paper_id, data)
            elif action == "feedback":
                try:
                    self.store.save_feedback(paper_id, data.get("feedback", ""))
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback")
                    return
            else:
                self.store.delete_paper(paper_id)
            self.redirect_home()
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def run(host: str, port: int, database: str) -> None:
    store = Store(database)
    handler = type("ResearchTrailHandler", (AppHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Research Trail running at http://{host}:{port}")
    print(f"Local data: {database}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Research Trail app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--database", default=os.environ.get("RESEARCH_TRAIL_DB", "data/research_trail.db"))
    args = parser.parse_args()
    run(args.host, args.port, args.database)


if __name__ == "__main__":
    main()
