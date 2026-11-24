---
name: notch
description: Use when working with Notch, the shared plain-text lab notebook (`notch info` shows where it lives). Use to catch up on a repo's or project's history at the start of work; to log durable findings, results, decisions, todos, questions, or end-of-session status; and to search or show past entries. Triggers include "add a notch", "log this", "notch this", "what's the status of", or starting work in a repo that uses Notch.
---

# Notch

Notch is a plain-text lab notebook for durable, cross-session project notes,
shared by several people and their agents through git. The daily text files
are the source of truth; the `notch` CLI appends and finds entries.

Notch holds *why*: results, decisions, findings, and where a thread stands.
Git holds *what changed*. Plans and code hold *what is next*. Write to Notch
only when there is an outcome (see "When to write").

- Files: `entries/<machine>/YYYY-MM-DD.txt` in the notebook (`notch info`
  prints where). Each machine writes only its own folder. Never edit these
  files by hand.
- Times and dates are UTC (entries written before notch 0.2 used the writer's
  local time). Convert to the user's time zone when you report them.
- Sync is automatic. `notch add` pushes in the background, and `notch catchup`
  pulls when the last sync is over an hour old. `notch sync` forces it.

## Catch up

Start work in a git repo with:

```sh
notch catchup            # inside the repo: finds the project logged from it
notch catchup PROJECT    # anywhere: a named project
```

- **Notices come first.** A failed sync, a newer notch, or a stale skill
  appears under `== Notices ==`. Tell the user. Run an update command only
  with their OK. Don't interrupt the task for this.
- **No project for the repo?** Catchup says so. Ask the user once whether to
  keep a notch log for this repo:
  - Yes: agree on a name, usually the repo name, after checking
    `notch projects` for an existing one. Then write the first entry as
    catchup shows.
  - No: run `notch ignore`. Don't create a project without asking.
- **Otherwise** catchup prints:
  - the project's latest weekly digest, in full
  - the metadata line and first line of every entry since (up to 40;
    `--limit N` changes this)
  - the tags in use
  - weeks that are ready for a digest

  Then open only what matters:

```sh
notch show ID1 ID2 ID3                               # full text of the few that matter
notch search --project PROJECT --type todo --brief   # narrower scans
```

- Treat the digest as an index, not a source. Before you rely on a specific
  number or claim from it, `notch show` the entry IDs it cites.
- If catchup lists undigested weeks and you can run a background sub-agent,
  give it this task: "Run `notch digest-prep PROJECT WEEK` for the oldest
  listed week, redirecting its output to a file and reading that. Follow the
  instructions it prints. Repeat for the next week until none remain."
  - Keep working on the user's task meanwhile.
  - If you can't run sub-agents, mention the gap to the user once.
  - Never write a digest in your own context mid-task. Notch only offers a
    week from the Wednesday (UTC) after it ends, and refuses a digest another
    machine already wrote.
- `search` prints full entries, at most the 20 most recent unless you pass
  `--limit N`. `--brief` prints only each entry's metadata line and first line,
  with no cap. Prefer `--brief` for scanning.
- `--limit N` keeps only the N most recent matches (still oldest first). Use
  it instead of piping to `head` or `tail`, which cut entries in half.
- Query words must all appear, in any order, case-insensitive:
  `notch search --project PROJECT nca pool --brief`.
- `--since` takes `7d` or a date such as `2026-11-16`.
- `notch show` ends each entry with `-> referenced by: ID (type), ...` when
  later entries cite it. Check that line before relying on an old entry: a
  later entry may correct it, supersede it, or close it.
- Project names and tags are case-insensitive. Tags also treat `-` and `_` as
  the same (`--tag nca_style` finds `nca-style`).

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

Don't write entries for:
- plans or proposals
- steps of a task, or retries and fixes along the way
- conversational back-and-forth
- anything a commit message, plan file, or code comment already records

Before writing, ask: would the next agent act differently for having read
this? If not, skip it. Prefer one entry per outcome, written when it is
settled. If you are on your third entry in a session, you are probably logging
steps, not outcomes. An entry you would have to correct within the hour was
written too early.

## Add entries

```sh
notch add --type TYPE --tags tag1,tag2 --author AGENT --model MODEL_ID --entry "Entry text."

# Long or quote-heavy text: read it from stdin with a quoted heredoc.
notch add --type finding --tags tag1 --author AGENT --model MODEL_ID --entry - <<'EOF'
Headline sentence.
- detail with "quotes" and $symbols
EOF
```

- **Run `notch add` from inside the repo the entry is about.** Notch then:
  - defaults `--project` to the project already logged from that repo
  - records the repo, branch, and commit on a `git:` line, e.g.
    `git: betasense main@2335dc7`, plus `dirty` if tracked files have
    uncommitted changes

  If the entry describes code work, commit first so the recorded commit
  contains it. Don't repeat the hash in the text.
- **Work that spans repos:** write one entry per repo, each from that repo's
  checkout, and cite each other's IDs.
- **Outside a repo,** pass `--project`. If the name is new, Notch says so and
  lists recent projects: check it is not a typo or a duplicate. If the project
  exists with different capitalization, Notch uses the existing spelling.
- **`--author`** identifies the writer (`claude`, `codex`, or a person's
  name). Inside Claude Code it defaults to `claude`; other agents must pass
  it.
- **`--model`** is your exact model ID, e.g. `claude-opus-5-5`. Agents always
  pass it. Humans leave it out.
- Author, project, and tags may contain only letters, numbers, `_`, and `-`.
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
- Always pass the text with `--entry TEXT` or `--entry -`. With neither, Notch
  opens `$EDITOR` at a terminal and reads stdin otherwise. `--message` is a
  legacy alias.

On success, `notch add` prints the new entry's ID (e.g.
`PROJECT-20261124-andrew-luna-ab12-003`). Capture it and report it back
when relevant.

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

Example: instead of an 1,100-character paragraph that starts "Completed
agreed deployment simplification. build_quantile_ensemble_bundle.py now
exports every selected ONNX member and ...", write:

```text
Bundle build is one command; parity now checks PyTorch→ONNX→Rust directly (max diff 3.1e-06, tol 1e-3).
- Fixtures are release-only; dev parity no longer needs them.
- Bundle manifest records checkpoint SHA-256 for provenance.
- Verified on 3 multi-scale images; ruff, ty, runtime (12) and UI (13) tests pass.
```

## Reference, don't rewrite

Entry IDs are stable. Cite them in entry text, and copy them verbatim from
`add`/`search` output. Never hand-construct one: the machine segment
(e.g. `andrew-luna-ab12`) differs on every machine, so a guessed ID will
usually be wrong.

When a new entry changes the standing of an older one, say so in its first
line, so that the old entry's `referenced by` line points readers to it:

```text
Corrects PROJECT-20260626-host-ab12-003: bimodality disappears above lambda 1.4, not 1.2.
Closes PROJECT-20260626-host-ab12-005: seeds rerun at 32; conclusions unchanged.
Follows PROJECT-20260626-host-ab12-007: ...
```

Don't edit existing entries, yours or anyone else's. Corrections, summaries,
and follow-ups go in new entries that cite the relevant IDs.

## Setup and sandboxes

- Notch is set up per machine with `notch setup`, which you can run for the
  user. The user's name, plus `--clone URL` for a shared notebook, is all it
  needs:
  ```sh
  notch setup --name NAME --clone URL
  ```
  Rerunning it is safe.
- In sandboxed environments, `notch` commands may need escalation:
  - they read the notebook directory outside the project root
  - `add` writes its daily file and lock file
  - sync needs network access and git credentials

  If a sync fails for these reasons, tell the user rather than retrying.
