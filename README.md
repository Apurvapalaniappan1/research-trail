# Research Trail

Research Trail is a dependency-free, local-first web app for turning one
research question and a short list of real papers into an explainable reading
queue. Milestone 1A deliberately uses transparent keyword matching—no external
AI, search service, or PDF ingestion.

## Run locally

Python 3.11 or newer is the only requirement.

```bash
python research_trail.py
```

Open <http://127.0.0.1:8000>, enter a research question, and add papers one at a
time. Start with any number of papers; 5–10 is the target for evaluating the
queue. Stop the server with **Ctrl+C**.

Data is stored in `data/research_trail.db`. The `data/` directory, SQLite files,
and PDFs are ignored by Git. To choose another local database or port:

```bash
python research_trail.py --database /path/to/research.db --port 8080
```

The threaded local server serializes access to its shared SQLite connection so
simultaneous requests cannot overlap transactions.

Run the tests with:

```bash
python -m unittest discover -s tests -v
```

## Milestone 1A: keyword-ranked reading queue

### Implemented workflow

A user can enter one research question, add papers incrementally, and receive a
ranked queue whose reasons point back to words in the question and source text.
They can open each paper's DOI or URL and record whether each suggestion was
useful. A set of 5–10 papers is the target for evaluating the queue, not a
restriction on saving papers.

### Scope

The local app provides one workspace with:

- one research question in plain text;
- manually entered papers, each with a title, an abstract or clearly identified
  source excerpt, separate personal notes, a DOI or URL, and its source, access
  level, and license when known;
- a reading queue ranked by transparent keyword overlap between the research
  question and each paper's title and source text; personal notes are excluded;
- a short reason for every rank, showing exact excerpts around matched terms and
  labelling them as `Title` or `Source text`;
- a link that opens the paper's DOI (using its resolver URL) or supplied URL;
- one feedback value per suggestion: `useful`, `not useful`, or not yet rated;
  and
- local persistence so the current question, papers, ranking, and feedback
  remain available after restarting the app.

The app lets the user save papers one at a time and is intended to evaluate the
queue with a target set of 5–10. Each entry must have source text—either an
abstract or an excerpt the user identifies as coming from the paper—plus either
a DOI or URL. Its source, access level, and license must be recorded as provided
values or explicitly as `unknown`. Papers are supplied by the user; Milestone 1A
does not search for or invent citations.

### Ranking approach

Milestone 1A uses a deterministic keyword score:

1. Normalize and tokenize the question, title, and source text.
2. Ignore punctuation and a small documented stop-word list.
3. Score overlap, giving title matches more weight than source-text matches and
   never scoring personal notes.
4. Break ties consistently, such as by paper creation order.

The queue reason exposes each matched question term with a short, exact excerpt
from the title or source text, labelled with that input field. Personal notes
remain visibly separate. The same inputs always produce the same order and
explanation.

### Simple data model

The app keeps the model small:

- **Workspace:** `id`, `question`, `created_at`, `updated_at`.
- **Paper:** `id`, `title`, `source_text`, `source_text_reviewed`, `notes`, `doi`,
  `url`, `source`, `access_level`, `license`, `feedback`, `created_at`. The three
  provenance and access fields use `unknown` when the user does not know the
  value; association with the single workspace is implicit.
- **Suggestion:** computed from each paper with `score`, `matched_terms`, and a
  field-by-field reason; only `feedback` is persisted with the paper.

There is only one active workspace in Milestone 1A. Suggestions are recomputed
from the question and papers whenever the queue is displayed.

When opening a database from the earlier combined abstract/notes schema, the app
preserves that text but marks it as needing review. Migrated text is excluded
from ranking and labelled in the queue until the user confirms or replaces the
source text by editing and saving the paper.

### Completion criteria

Milestone 1A is complete when a user can:

1. enter and later edit one research question;
2. add, edit, and remove papers incrementally, including saving fewer than five,
   and evaluate the queue with a target set of 5–10 papers;
3. see every paper in a deterministic keyword-ranked queue;
4. understand each position from exact, field-labelled excerpts while seeing
   personal notes separately;
5. open every valid DOI/URL; and
6. mark and revise each suggestion as useful or not useful, with that feedback
   retained locally.

Automated tests cover incremental saving, explicit unknown metadata, input
validation, DOI link construction, ranking and tie-breaking, explanation data,
feedback changes, and local reload.

## Not in Milestone 1A

- Semantic or embedding-based ranking, reranking, and recommendations.
- Citation discovery, metadata lookup, PDF ingestion, or full-text extraction.
- Accounts, sync, collaboration, or hosted deployment.
- A detailed event, ranking, or feedback history.
- Thesis planning, evidence synthesis, outlines, drafting, and citation export.

## Roadmap

After Milestone 1A is validated with real use:

1. **Semantic ranking:** compare semantic and hybrid ranking with the keyword
   baseline while keeping reasons inspectable.
2. **Detailed history:** retain question revisions, ranking runs, reading state,
   and feedback events so changes can be reviewed over time.
3. **Thesis workflow:** add synthesis, evidence organization, outlines, drafting
   support, and export only after the reading-queue workflow proves useful.
