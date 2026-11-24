# Notch Agent Guide

<!-- Keep in sync with ~/.claude/skills/notch/SKILL.md (same text minus frontmatter and title). -->

Notch is a plain-text lab notebook for durable, cross-session project notes. The
daily text files are the source of truth; the `notch` CLI is a convenience for
appending and finding entries.

Entries live in `~/Documents/notch-notebook/` by default (`$NB_DIR` overrides).
Daily files are named `YYYY-MM-DD.txt`; any `.index/` cache is safe to delete.

Notch holds *why*: results, decisions, findings, and where a thread stands.
Git holds *what changed*. Plans and code hold *what is next*. Write to Notch
only when there is an outcome (see "When to write").

## Catch up on a project

Start with one command:

```sh
notch catchup PROJECT
```

It prints the project's latest weekly digest in full, then the metadata line
and first line of every entry since (up to 40, `--limit N` to change), the
tags in use, and any completed weeks that still lack a digest. Then open only
what matters:

```sh
notch show ID1 ID2 ID3                               # full text of the few that matter
notch search --project PROJECT --type todo --brief   # narrower scans
```

- Treat the digest as an index, not a source. Before you rely on a specific
  number or claim from it, `notch show` the entry IDs it cites.
- If catchup lists undigested weeks: when you can run a background sub-agent,
  give it the task "Run `notch digest-prep PROJECT WEEK` for the oldest listed
  week, redirecting its output to a file and reading that; follow the
  instructions it prints; then repeat for the next week until none remain."
  Keep working on the user's task meanwhile. If you cannot run
  sub-agents, mention the gap to the user once. Never write a digest for the
  current week, and never write one in your own context mid-task.
- `search` prints full entries, at most the 20 most recent unless you pass
  `--limit N`. `--brief` prints only each entry's metadata line and first line,
  with no cap; prefer it for scanning.
- `--limit N` keeps only the N most recent matches (still oldest-first). Use it
  instead of piping to `head`/`tail`, which cut entries in half.
- Query words must all appear, in any order, case-insensitive:
  `notch search --project PROJECT nca pool --brief`.
- `--since` takes `7d` or a date such as `2026-11-16`.
- `notch show` ends each entry with `-> referenced by: ID (type), ...` when later
  entries cite it. Check that line before relying on an old entry: a later
  entry may correct it, supersede it, or close it.
- Project names and tags are case-insensitive, and tags treat `-` and `_` as
  the same (`--tag nca_style` finds `nca-style`). Replace `PROJECT` with the
  current project's name (often the repo/dir name).

## When to write

Write an entry when one of these happens, and not before:

- **A result is measured.** Final numbers, not "run launched" or "in progress".
  A run that will outlive the session goes in the end-of-session status.
- **A decision is settled**, by the user or by evidence. Log the decision once
  it is made, not the proposal while it awaits approval.
- **A finding changes the plan or the understanding of the problem.**
- **An experiment is pre-registered:** its design and predictions, written
  before any result is visible. This is the one deliberate exception to
  "outcomes only".
- **The user asks** you to note something.
- **The session ends** with work in flight: one `status` entry saying where
  things stand and what is next.

Do not write entries for: plans or proposals, steps of a task, retries and
fixes along the way, conversational back-and-forth, or anything a commit
message, plan file, or code comment already records.

Before writing, ask: would the next agent act differently for having read
this? If not, skip it. Prefer one entry per outcome, written when it is
settled. If you are on your third entry in a session, you are probably logging
steps, not outcomes. An entry you would have to correct within the hour was
written too early.

## Add entries

```sh
notch add --project PROJECT --type TYPE --tags tag1,tag2 --author AGENT --entry "Entry text."

# Long or quote-heavy text: read it from stdin with a quoted heredoc.
notch add --project PROJECT --type finding --tags tag1 --author AGENT --entry - <<'EOF'
Headline sentence.
- detail with "quotes" and $symbols
EOF
```

- `--author` identifies the writer (`claude`, `codex`, or a human username).
  Inside Claude Code it defaults to `claude`; Codex and others must pass it.
  Author, project, and tags may contain only letters, numbers, `_`, and `-`.
- If the project already exists with different capitalization, Notch uses the
  existing spelling and says so. If the name is new, Notch says so and lists
  recent projects; check that it is not a typo.
- Tags: one or two, reused from the tags `catchup` lists. Don't repeat the
  project, type, or author as a tag. Notch rewrites each tag to its most-used
  existing spelling, ignoring case and `-`/`_`.
- Entry types:
  - `finding`: qualitative conclusion or insight
  - `result`: quantitative output, tables, or measurements
  - `status`: where a thread stands (outcomes, not activity)
  - `todo`: something to investigate or return to
  - `question`: unresolved issue that needs an answer
  - `note`: context that fits nothing above
  - `digest`: weekly project summary; written only via `notch digest-prep`
- Always pass the text with `--entry TEXT` or `--entry -`. (With neither,
  Notch opens `$EDITOR` at a terminal and reads stdin otherwise. `--message`
  is a legacy alias.)
- Run `notch add` from inside the project's git checkout. Notch records the
  repo, branch, and commit automatically on a `git:` line under the metadata
  (`git: betasense main@2335dc7`, plus `dirty` if tracked files have
  uncommitted changes). If the entry describes code work, commit first so
  the recorded commit contains it. Don't repeat the hash in the text.

On success, `notch add` prints the new entry's ID (e.g.
`PROJECT-20260626-host-ab12-003`). Capture it and report it back when relevant.

## Writing style

Entries are read later by agents with limited context. Write like a lab
notebook: lead with the conclusion, then the evidence. Do not narrate.

1. **The first line is the bottom line.** One sentence, under ~120 characters,
   that states the result, conclusion, or decision. It is all that `--brief`
   shows, so a reader must be able to act on it alone.
2. **Then supporting facts, most important first.** Stop when the reader has
   enough to trust and reproduce the first line. Aim for under ~800 characters;
   go longer only for result tables.
3. **One fact per sentence. Short sentences, active voice.** Prefer `-` lists
   and small tables to paragraphs. Sentence fragments are fine.
4. **Be exact.** Give numbers with units, n/seeds, and the baseline compared
   against. Name files, commands, and config keys exactly.
5. **Record what was learned or decided, not the steps taken.** Git has the
   diff. Say why the change was made and what it showed; don't list every
   renamed file.
6. **No filler.** Drop "Successfully", "I have now", "comprehensive", and
   restatements of the task.
7. **Mark confidence.** "1 seed only", "unverified", "user-confirmed".

Example — instead of an 1,100-character paragraph that starts "Completed
agreed deployment simplification. build_quantile_ensemble_bundle.py now
exports every selected ONNX member and ...":

```text
Bundle build is one command; parity now checks PyTorch→ONNX→Rust directly (max diff 3.1e-06, tol 1e-3).
- Fixtures are release-only; dev parity no longer needs them.
- Bundle manifest records checkpoint SHA-256 for provenance.
- Verified on 3 multi-scale images; ruff, ty, runtime (12) and UI (13) tests pass.
```

## Reference, don't rewrite

Entry IDs are stable. Cite them in entry text, and copy them verbatim from
`add`/`search` output. Never hand-construct one: the host segment (e.g.
`host-ab12`) varies by machine, so a guessed ID will usually be wrong.

When a new entry changes the standing of an older one, say so in its first
line so the old entry's `referenced by` line points readers to it:

```text
Corrects PROJECT-20260626-host-ab12-003: bimodality disappears above lambda 1.4, not 1.2.
Closes PROJECT-20260626-host-ab12-005: seeds rerun at 32; conclusions unchanged.
Follows PROJECT-20260626-host-ab12-007: ...
```

Do not edit existing entries. Corrections, summaries, and follow-ups go in new
entries that cite the relevant IDs.

## Sandbox note

In sandboxed environments, `notch` commands may need escalation: they read the
notebook directory outside the project root, and `add` also writes its daily
file and lock file.
