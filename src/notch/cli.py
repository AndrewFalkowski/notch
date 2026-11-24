from __future__ import annotations

import argparse
import datetime as dt
import getpass
import importlib.metadata
import importlib.resources
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import termios
import tomllib
import tty
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is best-effort.
    fcntl = None


ENTRY_TYPES = {"note", "result", "todo", "question", "status", "finding", "digest"}
ENTRY_TYPE_DESCRIPTIONS = {
    "note": "general observation",
    "finding": "qualitative conclusion",
    "result": "numbers, tables, outputs",
    "todo": "follow-up work",
    "question": "open issue",
    "status": "where things stand",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.txt$")
META_RE = re.compile(
    r"^\[(?P<time>\d{2}:\d{2})\] "
    r"(?P<author>[A-Za-z0-9_-]+) \| "
    r"(?P<id>[A-Za-z0-9_-]+-\d{8}-[A-Za-z0-9_-]+-\d{3}) \| "
    r"(?P<project>[A-Za-z0-9_-]+) \| "
    r"(?P<type>[A-Za-z]+) \| "
    r"(?P<tags>.*)$"
)
# Optional "key: value" lines between the metadata line and the body.
META_LINE_RE = re.compile(r"^(?P<key>git|model|covers): (?P<value>\S.*)$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ID_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]+-\d{8}-[A-Za-z0-9_-]+-\d{3}(?![A-Za-z0-9_-])")
WEEK_RE = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")
BRIEF_WIDTH = 200
CATCHUP_LIMIT = 40
SEARCH_FULL_LIMIT = 20
CATCHUP_TAGS = 15
ENTRIES_DIR = "entries"  # entries/<machine-id>/YYYY-MM-DD.txt; each machine writes only its own folder
SETTINGS_FILE = "notch.toml"  # shared notebook settings, committed with the notebook
DIGEST_GRACE_DAYS = 2  # a week can be digested from the Wednesday after it (UTC)
SYNC_INTERVAL = 3600  # catchup pulls when the last sync is older than this (seconds)
UPDATE_INTERVAL = 86400  # check GitHub for a newer notch at most once a day
AGENT_AUTHORS = {"claude", "codex", "gemini", "cursor", "copilot", "agent"}
NOTEBOOK_GITIGNORE = [".index/", ".notch.lock", ".machine-id"]
BLOCK_BEGIN = "<!-- notch:begin (managed by `notch setup`; edits inside are overwritten) -->"
BLOCK_END = "<!-- notch:end -->"
DIGEST_INSTRUCTIONS = """\
Write a digest of one project-week. Agents will read it instead of the
entries, so it must be short, exact, and grounded in the material below: the
previous digest and this week's entries.

Format: plain text, "-" bullets under each heading. Aim for under 2,500
characters, not counting cited IDs. Group closely related items in one bullet.
  Digest PROJECT WEEK: <the week's bottom line, one sentence>
  State:       Where the project stands now. Start from the previous digest's
               State and update it.
  Open:        Todos and questions still open. Start from the previous digest's
               Open, add new ones, and drop any the entries close, answer, or
               make obsolete (say in a few words what replaced it).
  This week:   Findings, results, and decisions that matter, most important first.
  Corrections: "<new ID> corrects <old ID>: ..." where an entry corrects or
               supersedes an earlier one; "<ID> conflicts with <ID>: ..." where
               entries disagree and none settles it. Omit if none.
  Dead ends:   Approaches tried and dropped, and why. Omit if none.

Rules:
- Cite the entry IDs each bullet rests on, copied exactly, character for
  character; one or two per bullet is enough. Notch rejects a digest that
  cites an ID that does not exist.
- Copy numbers exactly as the entries give them, with units.
- Reconcile, don't invent: drop or update items the entries show are obsolete,
  but add no facts, opinions, or corrections of your own.
- Say each thing once. A correction goes under Corrections; State just shows
  the current value.
- Refactors, renames, and speedups get one line at most, and only if they
  change what the project can do (e.g. "generator 5x faster, 12 s/geometry").
"""


@dataclass(frozen=True)
class Entry:
    date: dt.date
    path: Path
    text: str
    time: str
    author: str
    entry_id: str
    project: str
    entry_type: str
    tags: tuple[str, ...]
    git: str | None = None
    model: str | None = None
    covers: str | None = None

    @property
    def body(self) -> str:
        lines = self.text.splitlines()[2:]
        present = {
            key for key, value in (("git", self.git), ("model", self.model), ("covers", self.covers)) if value
        }
        while lines and present:
            match = META_LINE_RE.match(lines[0])
            if not match or match.group("key") not in present:
                break
            present.discard(match.group("key"))
            lines = lines[1:]
        return "\n".join(lines)

    @property
    def headline(self) -> str:
        for line in self.body.splitlines():
            if line.strip():
                line = line.strip()
                return line if len(line) <= BRIEF_WIDTH else line[: BRIEF_WIDTH - 3].rstrip() + "..."
        return ""


@dataclass(frozen=True)
class UpdateCheck:
    source_dir: Path
    installed_at: float
    source_updated_at: float


@dataclass
class TuiState:
    project_options: list[str]
    project_index: int
    new_project: str
    type_options: list[str]
    type_index: int
    tags: str
    entry: str


class NotchError(Exception):
    pass


class Cancelled(NotchError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command is None:
            if not interactive_terminal():
                parser.print_help()
                return 0
            if maybe_prompt_for_update():
                return 0
            return cmd_tui(args)
        return args.func(args)
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled")
        return 130
    except NotchError as exc:
        if isinstance(exc, Cancelled):
            print("\ncancelled")
            return 130
        print(f"notch: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"notch: {format_os_error(exc)}", file=sys.stderr)
        return 2
    except UnicodeError as exc:
        print(f"notch: could not read text file: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notch")
    parser.add_argument("--author", help="author marker for interactive entries")
    parser.add_argument("--version", action="version", version=f"%(prog)s {notch_version()}")
    parser.set_defaults(command=None)
    subparsers = parser.add_subparsers(dest="command")

    add = subparsers.add_parser("add", help="add a notebook entry")
    add.add_argument("--project", "-p", help="defaults to the project logged from the current repo")
    add.add_argument("--type", "-t", default="note", choices=sorted(ENTRY_TYPES))
    add.add_argument("--tags", default="")
    add.add_argument("--entry", "-e", dest="entry", help="entry text, or - to read it from stdin")
    add.add_argument("--message", "-m", dest="entry", help=argparse.SUPPRESS)
    add.add_argument("--author", default=argparse.SUPPRESS)
    add.add_argument("--model", help="the agent's model ID, e.g. claude-opus-5-5 (or set NOTCH_MODEL)")
    add.add_argument("--covers", metavar="WEEK", help="for --type digest: the ISO week covered, e.g. 2026-W47")
    add.add_argument("--replace", action="store_true", help="for --type digest: supersede the week's existing digest")
    add.set_defaults(func=cmd_add)

    catchup = subparsers.add_parser("catchup", help="latest digest, recent entries, and undigested weeks")
    catchup.add_argument("project", nargs="?", help="defaults to the project logged from the current repo")
    catchup.add_argument("--limit", "-n", type=positive_int, default=CATCHUP_LIMIT, help="max recent entries listed")
    catchup.set_defaults(func=cmd_catchup)

    digest_prep = subparsers.add_parser("digest-prep", help="print instructions and material for one digest")
    digest_prep.add_argument("project")
    digest_prep.add_argument("week", help="ISO week, e.g. 2026-W47")
    digest_prep.set_defaults(func=cmd_digest_prep)

    cli = subparsers.add_parser("cli", help="open the prompt-based entry flow")
    cli.set_defaults(func=cmd_interactive)

    tui = subparsers.add_parser("tui", help="open a simple full-screen entry form")
    tui.set_defaults(func=cmd_tui)

    today = subparsers.add_parser("today", help="print today's notebook file")
    today.set_defaults(func=cmd_today)

    show = subparsers.add_parser("show", help="print entries by ID, with the entries that reference them")
    show.add_argument("entry_ids", nargs="+", metavar="entry_id")
    show.set_defaults(func=cmd_show)

    search = subparsers.add_parser("search", help="search entries")
    search.add_argument("query", nargs="*", help="words that must all appear (case-insensitive)")
    search.add_argument("--project", "-p", help="project name (case-insensitive)")
    search.add_argument("--type", "-t", choices=sorted(ENTRY_TYPES))
    search.add_argument("--tag")
    search.add_argument("--since", help="7d or 2026-11-16")
    search.add_argument("--limit", "-n", type=positive_int, help="only the N most recent matches")
    search.add_argument("--brief", "-b", action="store_true", help="metadata line plus first line of each entry")
    search.set_defaults(func=cmd_search)

    info = subparsers.add_parser("info", help="show notebook location and install notes")
    info.set_defaults(func=cmd_info)

    update_check = subparsers.add_parser("update-check", help="check for a newer notch and other install notices")
    update_check.set_defaults(func=cmd_update_check)

    sync = subparsers.add_parser("sync", help="commit this machine's entries, pull, and push")
    sync.add_argument("--quiet", "-q", action="store_true")
    sync.set_defaults(func=cmd_sync)

    setup = subparsers.add_parser(
        "setup",
        help="set up this machine: notebook, name, agent skill and instructions",
        description="Set up notch on this machine. Safe to rerun; with no flags it refreshes the "
        "agent skill and instructions using the saved settings.",
    )
    setup.add_argument("--name", help="your name, used as your author name and in this machine's ID")
    setup.add_argument("--notebook", help="notebook directory (default ~/Documents/notch-notebook)")
    setup.add_argument("--clone", metavar="URL", help="clone a shared notebook repo into the notebook directory")
    setup.set_defaults(func=cmd_setup)

    guide = subparsers.add_parser("guide", help="print the agent conventions (the notch skill)")
    guide.set_defaults(func=cmd_guide)

    projects = subparsers.add_parser("projects", help="list projects with their repos and authors")
    projects.set_defaults(func=cmd_projects)

    ignore = subparsers.add_parser("ignore", help="stop catchup offering to log the current repo")
    ignore.add_argument("--undo", action="store_true", help="offer again")
    ignore.set_defaults(func=cmd_ignore)

    migrate = subparsers.add_parser("migrate", help="move old shared daily files into per-machine folders")
    migrate.set_defaults(func=cmd_migrate)

    return parser


def cmd_interactive(args: argparse.Namespace) -> int:
    print_header("New Entry")
    project = select_project(notebook_dir())
    entry_type = select_entry_type()
    tags = prompt_tags()
    validate_entry_metadata(project=project, entry_type=entry_type, tags=tags, author=args.author)
    entry = read_entry_from_terminal()
    if not confirm_entry(project=project, entry_type=entry_type, tags=tags, entry=entry, author=args.author):
        print("not saved")
        return 1
    entry_id = add_entry(project=project, entry_type=entry_type, tags=tags, entry=entry, author=args.author)
    print()
    print(f"saved {entry_id}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    root = notebook_dir()
    project = args.project or repo_default_project(root)
    if args.entry == "-" or (args.entry is None and not sys.stdin.isatty()):
        entry = sys.stdin.read()
        if not entry.strip():
            raise NotchError("no entry text: pass --entry TEXT, or --entry - with text on stdin")
    else:
        entry = args.entry if args.entry is not None else read_entry_from_editor("")
    author = getattr(args, "author", None)
    model = args.model or os.environ.get("NOTCH_MODEL") or None
    if model is None and (author or default_author()).casefold() in AGENT_AUTHORS:
        print("notch: agents should pass --model with their model ID, e.g. --model claude-opus-5-5", file=sys.stderr)
    projects = [name for name, _ in recent_projects(root)]
    if projects and project.casefold() not in {name.casefold() for name in projects}:
        print(
            f"notch: started new project '{project}' (recent: {', '.join(projects[:8])})",
            file=sys.stderr,
        )
    if args.type == "digest" and notebook_remote(root) and not os.environ.get("NOTCH_NO_SYNC"):
        error = sync_notebook(root)  # another machine may have written this digest already
        if error:
            raise NotchError(f"digest not written: sync failed ({error}); retry when the notebook can sync")
    entry_id = add_entry(
        project=project,
        entry_type=args.type,
        tags=args.tags,
        entry=entry,
        author=author,
        model=model,
        covers=args.covers,
        replace=args.replace,
    )
    if args.type == "digest":
        length = len(ID_RE.sub("", entry.strip()))
        print(f"notch: digest is {length:,} characters without cited IDs (target: under 2,500)", file=sys.stderr)
        if notebook_remote(root) and not os.environ.get("NOTCH_NO_SYNC"):
            error = sync_notebook(root)
            if error:
                print(f"notch: digest saved but not pushed ({error}); `notch sync` retries", file=sys.stderr)
    if not entry_id.startswith(f"{project}-"):
        canonical = entry_id.rsplit("-", 4)[0]
        print(f"notch: using existing project name '{canonical}' for '{project}'", file=sys.stderr)
    print(entry_id)
    return 0


def repo_default_project(root: Path) -> str:
    repo = current_repo(root)
    if repo is None:
        raise NotchError("pass --project (not inside a git repo; `notch projects` lists projects)")
    projects = repo_projects(iter_entries(root), repo)
    if not projects:
        raise NotchError(
            f"pass --project: nothing is logged from repo {repo[0]} yet "
            f"(the repo name, {repo[0]}, is the usual choice; `notch projects` lists projects)"
        )
    return projects[0]


def cmd_tui(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise NotchError("tui requires an interactive terminal")
    from .tui_textual import run_textual_tui

    entry_id = run_textual_tui(root=notebook_dir(), author=args.author)
    if entry_id:
        print(f"saved {entry_id}")
        return 0
    print("not saved")
    return 1


def cmd_today(args: argparse.Namespace) -> int:
    today = utc_today()
    for idx, entry in enumerate(entry for entry in iter_entries(notebook_dir()) if entry.date == today):
        if idx:
            print()
        print(entry.text, end="")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    entries = list(iter_entries(notebook_dir()))
    by_id = {entry.entry_id: entry for entry in entries}
    by_folded_id = {entry.entry_id.casefold(): entry for entry in entries}
    missing: list[str] = []
    printed = 0
    for requested in args.entry_ids:
        entry = by_id.get(requested) or by_folded_id.get(requested.casefold())
        if entry is None:
            missing.append(requested)
            continue
        if printed:
            print()
        print(entry.text, end="" if entry.text.endswith("\n") else "\n")
        refs = referencing_entries(entries, entry.entry_id)
        if refs:
            listed = ", ".join(f"{ref.entry_id} ({ref.entry_type})" for ref in refs)
            print(f"  -> referenced by: {listed}")
        printed += 1
    if missing:
        raise NotchError(f"entry not found: {', '.join(missing)}")
    return 0


def referencing_entries(entries: list[Entry], entry_id: str) -> list[Entry]:
    pattern = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(entry_id)}(?![A-Za-z0-9_-])")
    return [entry for entry in entries if entry.entry_id != entry_id and pattern.search(entry.body)]


def cmd_search(args: argparse.Namespace) -> int:
    cutoff = parse_since(args.since) if args.since else None
    project = args.project.casefold() if args.project else None
    terms = " ".join(args.query).casefold().split()
    matches: list[Entry] = []
    for entry in iter_entries(notebook_dir()):
        if cutoff and entry.date < cutoff:
            continue
        if project and entry.project.casefold() != project:
            continue
        if args.type and entry.entry_type != args.type:
            continue
        if args.tag and tag_key(args.tag) not in {tag_key(tag) for tag in entry.tags}:
            continue
        if terms:
            text = entry.text.casefold()
            if not all(term in text for term in terms):
                continue
        matches.append(entry)

    limit = args.limit or (None if args.brief else SEARCH_FULL_LIMIT)
    if limit and len(matches) > limit:
        if not args.limit:
            print(
                f"(showing the {limit} most recent of {len(matches)} matches; "
                "add --brief to scan them all, or --limit N)"
            )
            print()
        matches = matches[-limit:]
    for idx, entry in enumerate(matches):
        if args.brief:
            print(entry.text.splitlines()[1])
            print(f"  {entry.headline}")
            continue
        if idx:
            print()
        print(entry.text, end="" if entry.text.endswith("\n") else "\n")
    return 0


def cmd_catchup(args: argparse.Namespace) -> int:
    root = notebook_dir()
    print_notices([notice for notice in [sync_if_stale(root), *update_notices(root)] if notice])
    all_entries = iter_entries(root)
    requested = args.project
    if requested is None:
        repo = current_repo(root)
        if repo is None:
            raise NotchError("not inside a git repo: pass the project, e.g. notch catchup PROJECT (see notch projects)")
        if repo_ignored(repo):
            print(f"Notch logging is off for repo {repo[0]} on this machine (`notch ignore --undo` turns it on).")
            return 0
        linked = repo_projects(all_entries, repo)
        if not linked:
            print(
                f"== No notch project for repo {repo[0]} ==\n"
                "Nothing has been logged from this repo. Ask the user once whether to keep a notch log for it.\n"
                f"- Yes: agree on a project name (usually the repo name, {repo[0]}; check `notch projects` for\n"
                "  an existing one), then from this repo run:\n"
                f"  notch add --project {repo[0]} --type note --entry \"Started a notch log for {repo[0]}: <what it is>.\"\n"
                "- No: run `notch ignore` here so later sessions do not ask again."
            )
            return 0
        requested = linked[0]
        if len(linked) > 1:
            print(f"(repo {repo[0]} also has entries under: {', '.join(linked[1:])})")
    project, entries = project_entries(all_entries, requested)
    digests = digests_by_week(entries)
    since: dt.date | None = None
    if digests:
        week = max(digests)
        since = parse_week(week)[1]
        print(f"== Latest digest: {week} ==")
        print(digests[week].text, end="")
    else:
        print(f"== No digest yet for {project} ==")

    recent = [entry for entry in entries if entry.entry_type != "digest" and (since is None or entry.date > since)]
    print()
    label = f"since {since + dt.timedelta(days=1)}" if since else "most recent"
    print(f"== Entries {label}: {len(recent)} ==")
    if len(recent) > args.limit:
        hidden = recent[: len(recent) - args.limit]
        print(
            f"({len(hidden)} older {'entry' if len(hidden) == 1 else 'entries'} not listed; "
            f"notch search --project {project} --since {hidden[0].date} --brief)"
        )
        recent = recent[-args.limit :]
    for entry in recent:
        print(entry.text.splitlines()[1])
        print(f"  {entry.headline}")

    tags = most_used_spellings((tag for entry in entries for tag in entry.tags), tag_key)
    counts: dict[str, int] = {}
    for entry in entries:
        for tag in {tag_key(tag) for tag in entry.tags}:
            counts[tag] = counts.get(tag, 0) + 1
    if counts:
        top = sorted(counts, key=lambda key: -counts[key])[:CATCHUP_TAGS]
        print()
        print(f"== Tags in use: {', '.join(f'{tags[key]} ({counts[key]})' for key in top)} ==")

    missing = undigested_weeks(entries)
    if missing:
        weeks = week_entries(entries)
        total = sum(len(weeks[week]) for week in missing)
        listed = (
            ", ".join(f"{week} ({count_entries(len(weeks[week]))})" for week in missing)
            if len(missing) <= 4
            else f"{missing[0]} .. {missing[-1]} ({len(missing)} weeks, {count_entries(total)})"
        )
        print()
        print(f"== Undigested weeks: {listed} ==")
        print(
            "Digests are written one week at a time, oldest first. If you can run a background\n"
            f"sub-agent, have it run `notch digest-prep {project} {missing[0]}` and follow the\n"
            "instructions it prints, then the next week. The output can be long: redirect it\n"
            "to a file and read that. Otherwise mention it to the user.\n"
            "Do not interrupt the current task for this."
        )
    return 0


def cmd_digest_prep(args: argparse.Namespace) -> int:
    monday, sunday = parse_week(args.week)
    week = format_week(monday)
    require_closed_week(week)
    root = notebook_dir()
    if notebook_remote(root) and not os.environ.get("NOTCH_NO_SYNC"):
        error = sync_notebook(root)  # the digest must see every machine's entries
        if error:
            raise NotchError(f"sync failed ({error}); a digest needs every machine's entries, so retry when online")
    project, entries = project_entries(iter_entries(root), args.project)
    weeks = week_entries(entries)
    if week not in weeks:
        raise NotchError(f"no {project} entries in {week}")
    earlier = [missing for missing in undigested_weeks(entries) if missing < week]
    if earlier:
        raise NotchError(
            f"digest earlier weeks first ({', '.join(earlier)}): each digest builds on the previous one"
        )
    digests = digests_by_week(entries)
    previous = max((digest_week for digest_week in digests if digest_week < week), default=None)

    print(f"DIGEST TASK: {project} {week} ({monday} to {sunday}), {count_entries(len(weeks[week]))}")
    if week in digests:
        print(
            f"A digest for {week} already exists ({digests[week].entry_id}). Stop unless the user asked for a\n"
            "replacement; then add --replace to the save command."
        )
    print()
    print(DIGEST_INSTRUCTIONS.replace("PROJECT WEEK", f"{project} {week}"))
    print("Save it with (the quoted heredoc avoids shell-quoting problems):")
    print(
        f"  notch add --project {project} --type digest --covers {week} --author AGENT --model MODEL "
        "--entry - <<'DIGEST'"
    )
    print("  ...digest text...")
    print("  DIGEST")
    spellings = sorted({entry.project for entry in weeks[week]})
    if len(spellings) > 1:
        print()
        print(
            f"Note: this week's entries use more than one project spelling ({', '.join(spellings)}). "
            "IDs that differ only in case are different entries."
        )
    print()
    print("== Previous digest ==")
    if previous:
        print(digests[previous].text, end="")
    else:
        print("None: this is the project's first digest.")
    print()
    print(f"== Entries {week} ==")
    for idx, entry in enumerate(weeks[week]):
        if idx:
            print()
        print(entry.text, end="")
    return 0


def count_entries(count: int) -> str:
    return f"{count} entry" if count == 1 else f"{count} entries"


def project_entries(entries: Iterable[Entry], project: str) -> tuple[str, list[Entry]]:
    """The project's most used spelling and all its entries, case-insensitively."""
    folded = project.casefold()
    matches = [entry for entry in entries if entry.project.casefold() == folded]
    if not matches:
        raise NotchError(f"no entries for project {project}")
    return most_used_spellings((entry.project for entry in matches), str.casefold)[folded], matches


def digests_by_week(entries: Iterable[Entry]) -> dict[str, Entry]:
    """Latest digest written for each covered week."""
    return {entry.covers: entry for entry in entries if entry.entry_type == "digest" and entry.covers}


def week_entries(entries: Iterable[Entry]) -> dict[str, list[Entry]]:
    weeks: dict[str, list[Entry]] = {}
    for entry in entries:
        if entry.entry_type != "digest":
            weeks.setdefault(format_week(entry.date), []).append(entry)
    return weeks


def undigested_weeks(entries: list[Entry]) -> list[str]:
    """Completed weeks that have entries but no digest, oldest first."""
    digests = digests_by_week(entries)
    return sorted(
        week for week in week_entries(entries) if week not in digests and utc_today() >= digestible_from(week)
    )


def cmd_info(args: argparse.Namespace) -> int:
    root = notebook_dir()
    sync = last_sync(root)
    if not notebook_remote(root):
        sync_text = "off (not a git repository with a remote)"
    elif sync is None:
        sync_text = "never"
    else:
        when = dt.datetime.fromtimestamp(sync[0], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sync_text = f"{when}, {sync[1]}"
    print(f"Version:      {notch_version()}")
    print(f"Notebook dir: {root}")
    print(f"Config:       {config_path()}{'' if config_path().exists() else ' (missing: run notch setup)'}")
    print(f"Author:       {args.author or default_author()}")
    print(f"Machine ID:   {machine_id_status(root)}")
    print(f"Last sync:    {sync_text}")
    print(f"Update with:  {upgrade_command()}")
    print()
    print("Entries are plain text: entries/<machine>/YYYY-MM-DD.txt in the notebook dir, times in UTC.")
    print()
    print_notices(update_notices(root))
    return 0


def cmd_update_check(args: argparse.Namespace) -> int:
    notices = update_notices(notebook_dir())
    update = local_update_check()
    if not update or not interactive_terminal():
        print_notices(notices)
        if not notices:
            print("notch is up to date.")
        return 0
    print(f"Local source is newer: {update.source_dir}")
    if prompt_yes_no("Update global notch now? [y/N]", default=False):
        return run_local_update(update.source_dir)
    print("not updated")
    return 1


def initial_tui_state(root: Path) -> TuiState:
    projects = [project for project, _ in recent_projects(root)[:9]]
    project_options = ["New project", *projects]
    return TuiState(
        project_options=project_options,
        project_index=1 if projects else 0,
        new_project="",
        type_options=["note", "finding", "result", "todo", "question", "status"],
        type_index=0,
        tags="",
        entry="",
    )


def select_project(root: Path) -> str:
    projects = recent_projects(root)[:9]
    options = ["New project", *[project for project, _ in projects]]
    labels = ["New project", *[f"{project}  ({last_used.isoformat()})" for project, last_used in projects]]
    choice = select_from_menu(
        "Project",
        options,
        default=options[1] if len(options) > 1 else "New project",
        labels=labels,
        numbers=list(range(len(options))),
    )
    if choice != "New project":
        return choice
    while True:
        value = prompt("New project")
        try:
            validate_name("project", value)
        except NotchError as exc:
            print(f"notch: {exc}", file=sys.stderr)
            continue
        return value


def select_entry_type() -> str:
    ordered_types = ["note", "finding", "result", "todo", "question", "status"]
    labels = [f"{entry_type:<8} {ENTRY_TYPE_DESCRIPTIONS[entry_type]}" for entry_type in ordered_types]
    return select_from_menu("Type", ordered_types, default="note", labels=labels)


def select_from_menu(
    label: str,
    options: list[str],
    *,
    default: str,
    labels: list[str] | None = None,
    numbers: list[int] | None = None,
) -> str:
    print_section(label)
    display_labels = labels or options
    option_numbers = numbers or list(range(1, len(options) + 1))
    for number, option, display_label in zip(option_numbers, options, display_labels):
        suffix = "  [default]" if option == default else ""
        print(f"  {number:>2}. {display_label}{suffix}")
    valid_numbers = {str(number): idx for idx, number in enumerate(option_numbers)}
    while True:
        raw = prompt_key(f"{label} number", valid_keys=[*valid_numbers, ""])
        if not raw:
            return default
        if raw in valid_numbers:
            return options[valid_numbers[raw]]
        print(f"notch: choose one of {', '.join(valid_numbers)}", file=sys.stderr)


def recent_projects(root: Path) -> list[tuple[str, dt.date]]:
    entries = list(iter_entries(root))
    projects: dict[str, tuple[int, dt.date]] = {}
    for idx, entry in enumerate(entries):
        projects[entry.project.casefold()] = (idx, entry.date)
    names = most_used_spellings((entry.project for entry in entries), str.casefold)
    return [
        (names[key], date)
        for key, (_, date) in sorted(projects.items(), key=lambda item: item[1][0], reverse=True)
    ]


def tag_key(tag: str) -> str:
    """Tags match regardless of case and of '-' vs '_'."""
    return tag.casefold().replace("_", "-")


def most_used_spellings(names: Iterable[str], key) -> dict[str, str]:
    """Map key(name) to the most used spelling of that name (latest wins ties)."""
    counts: dict[str, dict[str, int]] = {}
    for name in names:
        spellings = counts.setdefault(key(name), {})
        # Re-inserting keeps dict order as last-used order, so reversed() puts the latest first.
        spellings[name] = spellings.pop(name, 0) + 1
    return {
        folded: max(reversed(list(spellings)), key=spellings.__getitem__)
        for folded, spellings in counts.items()
    }


def canonical_names(root: Path, project: str, tags: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """Reuse existing spellings of the project and tags; drop tags repeated under another spelling."""
    entries = list(iter_entries(root))
    projects = most_used_spellings((entry.project for entry in entries), str.casefold)
    known_tags = most_used_spellings((tag for entry in entries for tag in entry.tags), tag_key)
    unique_tags: dict[str, str] = {}
    for tag in tags:
        unique_tags.setdefault(tag_key(tag), known_tags.get(tag_key(tag), tag))
    return projects.get(project.casefold(), project), tuple(unique_tags.values())


def prompt_tags() -> str:
    print_section("Tags")
    print("Comma-separated; leave blank for none.")
    return prompt("Tags")


def prompt(label: str) -> str:
    if not interactive_terminal():
        return input(f"\n{label}: ").strip()
    return read_line(f"\n{label}: ").strip()


def prompt_key(label: str, *, valid_keys: list[str]) -> str:
    if not interactive_terminal():
        return input(f"\n{label}: ").strip()

    sys.stdout.write(f"\n{label}: ")
    sys.stdout.flush()
    while True:
        char = read_char()
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x1b":
            raise Cancelled()
        if char in {"\r", "\n"}:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return ""
        if char in valid_keys:
            sys.stdout.write(f"{char}\n")
            sys.stdout.flush()
            return char


def print_header(label: str) -> None:
    print()
    print(label)
    print("=" * len(label))


def print_section(label: str) -> None:
    print()
    print(label)
    print("-" * len(label))


def confirm_entry(*, project: str, entry_type: str, tags: str, entry: str, author: str | None) -> bool:
    print_section("Preview")
    print(f"Author:  {author or default_author()}")
    print(f"Project: {project}")
    print(f"Type:    {entry_type}")
    print(f"Tags:    {tags or '-'}")
    print()
    print(entry.strip())
    print()
    while True:
        raw = prompt_key("Save entry? [Y/n]", valid_keys=["", "y", "Y", "n", "N"]).lower()
        if raw in {"", "y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("notch: answer y or n", file=sys.stderr)


def add_entry(
    *,
    project: str,
    entry_type: str,
    tags: str,
    entry: str,
    author: str | None = None,
    model: str | None = None,
    covers: str | None = None,
    replace: bool = False,
) -> str:
    parsed_tags = validate_entry_metadata(
        project=project,
        entry_type=entry_type,
        tags=tags,
        author=author,
    )
    if model is not None and not MODEL_RE.match(model):
        raise NotchError("model must use only letters, numbers, '.', '_' and '-', e.g. claude-opus-5-5")
    entry = escape_entry_boundaries(entry.strip())
    if not entry:
        raise NotchError("entry is empty")

    root = notebook_dir()
    root.mkdir(parents=True, exist_ok=True)
    require_min_version(root)
    project, parsed_tags = canonical_names(root, project, parsed_tags)
    if entry_type == "digest":
        covers = validate_digest(root, project, covers, entry, replace=replace)
        git_line = ""  # repo state at writing time says nothing about the week covered
    else:
        if covers:
            raise NotchError("--covers is only for --type digest")
        git = git_context(root)
        git_line = f"git: {git}\n" if git else ""
    model_line = f"model: {model}\n" if model else ""
    covers_line = f"covers: {covers}\n" if covers else ""
    now = utc_now()
    today = now.date()

    with file_lock(root):
        machine = machine_id(root)
        path = root / ENTRIES_DIR / machine / f"{today.isoformat()}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        seq = next_sequence(existing, project, today, machine)
        entry_id = f"{project}-{today.strftime('%Y%m%d')}-{machine}-{seq:03d}"
        tag_text = ", ".join(parsed_tags)
        text = (
            f"---\n[{now.strftime('%H:%M')}] {author or default_author()} | {entry_id} | {project} | "
            f"{entry_type} | {tag_text}\n{git_line}{model_line}{covers_line}{entry}\n"
        )
        with path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            if existing:
                handle.write("\n")
            handle.write(text)
    if entry_type != "digest":  # cmd_add pushes digests in the foreground
        start_background_sync(root)
    return entry_id


def validate_digest(root: Path, project: str, covers: str | None, entry: str, *, replace: bool = False) -> str:
    if not covers:
        raise NotchError("digest entries need --covers WEEK, e.g. --covers 2026-W47")
    week = format_week(parse_week(covers)[0])
    require_closed_week(week)
    entries = list(iter_entries(root))
    known = {existing.entry_id for existing in entries}
    unknown = sorted(set(ID_RE.findall(entry)) - known)
    if unknown:
        raise NotchError(f"digest cites unknown entry IDs: {', '.join(unknown)}")
    folded = project.casefold()
    existing = digests_by_week(entry for entry in entries if entry.project.casefold() == folded).get(week)
    if existing and not replace:
        raise NotchError(
            f"{project} {week} already has a digest ({existing.entry_id}), possibly from another machine; "
            "nothing written. Pass --replace to supersede it."
        )
    return week


def parse_week(value: str) -> tuple[dt.date, dt.date]:
    """Return the Monday and Sunday of an ISO week such as 2026-W47."""
    match = WEEK_RE.match(value.strip())
    if not match:
        raise NotchError(f"week must look like 2026-W47, not '{value}'")
    try:
        monday = dt.date.fromisocalendar(int(match.group("year")), int(match.group("week")), 1)
    except ValueError:
        raise NotchError(f"no such ISO week: {value}") from None
    return monday, monday + dt.timedelta(days=6)


def format_week(date: dt.date) -> str:
    year, week, _ = date.isocalendar()
    return f"{year}-W{week:02d}"


def digestible_from(week: str) -> dt.date:
    """First UTC day a week may be digested: a grace period lets every machine sync its entries."""
    return parse_week(week)[1] + dt.timedelta(days=DIGEST_GRACE_DAYS + 1)


def require_closed_week(week: str) -> None:
    if parse_week(week)[1] >= utc_today():
        raise NotchError(f"{week} is not over yet; digests cover completed weeks")
    if utc_today() < digestible_from(week):
        raise NotchError(
            f"{week} can be digested from {digestible_from(week)} (UTC), "
            "once every machine has had time to sync its entries"
        )


def escape_entry_boundaries(entry: str) -> str:
    """Quote metadata lines pasted into an entry so they cannot start a new entry.

    The parser treats '---' followed by a metadata line as an entry boundary, so a
    quoted 'notch show' result would otherwise split off as a duplicate entry.
    """
    lines = entry.split("\n")
    for idx in range(1, len(lines)):
        if lines[idx - 1].strip() == "---" and META_RE.match(lines[idx]):
            lines[idx] = f"> {lines[idx]}"
    return "\n".join(lines)


def validate_entry_metadata(
    *,
    project: str,
    entry_type: str,
    tags: str,
    author: str | None = None,
) -> tuple[str, ...]:
    validate_name("project", project)
    validate_name("author", author or default_author())
    if entry_type not in ENTRY_TYPES:
        raise NotchError(f"invalid type: {entry_type}")
    return parse_tags(tags)


def git_context(root: Path, cwd: Path | None = None) -> str | None:
    """Describe the git checkout notch is run from as 'repo branch@sha [dirty]'."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=cwd,
        )
        if head.returncode != 0:
            return None
        toplevel, sha = head.stdout.strip().splitlines()
        if Path(toplevel).resolve() == root.resolve():
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=no"],
            capture_output=True, text=True, timeout=3, cwd=cwd,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    branch = "detached"
    dirty = False
    for line in status.stdout.splitlines():
        if line.startswith("# branch.head "):
            head_name = line.split(" ", 2)[2]
            branch = "detached" if head_name == "(detached)" else head_name
        elif line and not line.startswith("#"):
            dirty = True
    text = f"{repo_label(Path(toplevel), cwd)} {branch}@{sha}"
    return f"{text} dirty" if dirty else text


def repo_label(toplevel: Path, cwd: Path | None = None) -> str:
    """The repo's name as its origin remote spells it, so every clone agrees; else the folder name."""
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=3, cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        remote = None
    if remote and remote.returncode == 0 and remote.stdout.strip():
        name = re.split(r"[/:]", remote.stdout.strip().rstrip("/"))[-1].removesuffix(".git")
        name = sanitize_name(name)
        if name:
            return name
    return sanitize_name(toplevel.name) or toplevel.name


def current_repo(root: Path, cwd: Path | None = None) -> tuple[str, str] | None:
    """(remote name, folder name) of the git checkout notch runs in, unless it is the notebook itself."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3, cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0 or not top.stdout.strip():
        return None
    toplevel = Path(top.stdout.strip())
    if toplevel.resolve() == root.resolve():
        return None
    return repo_label(toplevel, cwd), toplevel.name


def repo_projects(entries: Iterable[Entry], repo: tuple[str, str]) -> list[str]:
    """Projects logged from this repo (by the git line), most entries first."""
    names = {name.casefold() for name in repo}
    entries = list(entries)
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.git and entry.git.split(" ", 1)[0].casefold() in names:
            counts[entry.project.casefold()] = counts.get(entry.project.casefold(), 0) + 1
    spellings = most_used_spellings((entry.project for entry in entries), str.casefold)
    return [spellings[key] for key in sorted(counts, key=lambda key: -counts[key])]


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "notch" / "config.toml"


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise NotchError(f"could not read {path}: {exc}") from None


def save_config(config: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by `notch setup`."]
    # JSON strings and lists of strings are valid TOML values.
    lines += [f"{key} = {json.dumps(value)}" for key, value in config.items() if value not in (None, "", [])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def notebook_dir() -> Path:
    configured = os.environ.get("NB_DIR") or load_config().get("notebook")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "notch-notebook"


def default_author() -> str:
    raw = os.environ.get("NOTCH_AUTHOR")
    if not raw and os.environ.get("CLAUDECODE") == "1" and not sys.stdin.isatty():
        # An agent's shell inside Claude Code; a human there would be at a terminal.
        raw = "claude"
    raw = raw or load_config().get("name") or getpass.getuser() or "user"
    return sanitize_name(raw) or "user"


def user_name() -> str:
    """The human using this machine, for machine IDs: the configured name, else the OS user."""
    return sanitize_name(load_config().get("name") or getpass.getuser() or "") or "user"


def new_machine_id() -> str:
    host = sanitize_name(socket.gethostname().split(".")[0]) or "machine"
    return f"{user_name()[:16]}-{host[:16]}-{uuid.uuid4().hex[:4]}"


def machine_id(root: Path) -> str:
    path = root / ".machine-id"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        validate_name("machine id", value)
        return value
    value = new_machine_id()
    path.write_text(value + "\n", encoding="utf-8")
    return value


def machine_id_status(root: Path) -> str:
    path = root / ".machine-id"
    if not path.exists():
        return "not created yet"
    return path.read_text(encoding="utf-8").strip()


def maybe_prompt_for_update() -> bool:
    update = local_update_check()
    if not update or not interactive_terminal():
        return False
    print(f"Local Notch source has changed since this install: {update.source_dir}")
    if prompt_yes_no("Update global notch now? [y/N]", default=False):
        run_local_update(update.source_dir)
        return True
    return False


def prompt_yes_no(label: str, *, default: bool) -> bool:
    while True:
        raw = prompt_key(label, valid_keys=["", "y", "Y", "n", "N"]).lower()
        if raw == "":
            return default
        if raw == "y":
            return True
        if raw == "n":
            return False
        print("notch: answer y or n", file=sys.stderr)


def run_local_update(source_dir: Path) -> int:
    try:
        subprocess.run(
            ["uv", "tool", "install", "--force", "--reinstall-package", "notch", str(source_dir)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise NotchError(f"update failed with status {exc.returncode}") from exc
    print("updated. Restart notch to use the refreshed install.")
    return 0


def update_status_text(update: UpdateCheck | None) -> str:
    if update:
        return "local source is newer"
    return "no local update found"


def local_update_check() -> UpdateCheck | None:
    dist = notch_distribution()
    if dist is None:
        return None
    source_dir = working_tree_source_dir() or installed_source_dir(dist)
    if source_dir is None:
        return None
    if source_dir == local_source_dir():
        return None
    installed_at = installed_at_time(dist)
    source_updated_at = source_updated_time(source_dir)
    if source_updated_at > installed_at:
        return UpdateCheck(source_dir=source_dir, installed_at=installed_at, source_updated_at=source_updated_at)
    return None


def notch_distribution() -> importlib.metadata.Distribution | None:
    try:
        return importlib.metadata.distribution("notch")
    except importlib.metadata.PackageNotFoundError:
        return None


def installed_source_dir(dist: importlib.metadata.Distribution) -> Path | None:
    text = dist.read_text("direct_url.json")
    if not text:
        return None
    data = json.loads(text)
    if data.get("dir_info", {}).get("editable"):
        return None
    parsed = urlparse(data.get("url", ""))
    if parsed.scheme != "file":
        return None
    source = Path(unquote(parsed.path)).expanduser().resolve()
    if not (source / "pyproject.toml").exists():
        return None
    return source


def working_tree_source_dir() -> Path | None:
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        pyproject = candidate / "pyproject.toml"
        if not pyproject.exists() or not (candidate / "src" / "notch").is_dir():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("project", {}).get("name") == "notch":
            return candidate
    return None


def installed_at_time(dist: importlib.metadata.Distribution) -> float:
    files = list(dist.files or [])
    mtimes = []
    for file in files:
        try:
            path = Path(dist.locate_file(file))
            if path.exists() and "site-packages" in path.parts:
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes, default=0.0)


def source_updated_time(source_dir: Path) -> float:
    candidates = [source_dir / "pyproject.toml"]
    candidates.extend((source_dir / "src").rglob("*.py"))
    mtimes = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes, default=0.0)


def local_source_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def notch_version() -> str:
    try:
        return importlib.metadata.version("notch")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-_")
    return cleaned[:32]


def validate_name(label: str, value: str) -> None:
    if not value or not NAME_RE.match(value):
        raise NotchError(f"{label} must use only letters, numbers, '_' and '-'")


def parse_tags(tags: str) -> tuple[str, ...]:
    parsed = tuple(tag.strip() for tag in tags.split(",") if tag.strip())
    for tag in parsed:
        validate_name("tag", tag)
    return parsed


def next_sequence(existing: str, project: str, date: dt.date, machine: str) -> int:
    prefix = f"{project}-{date.strftime('%Y%m%d')}-{machine}-"
    highest = 0
    for entry in parse_entries(existing, date=date, path=Path("")):
        if entry.entry_id.startswith(prefix):
            highest = max(highest, int(entry.entry_id.rsplit("-", 1)[1]))
    return highest + 1


def daily_files(root: Path) -> list[Path]:
    """Daily files: entries/<machine>/YYYY-MM-DD.txt, plus old-layout YYYY-MM-DD.txt at the root."""
    if not root.exists():
        return []
    files = [path for path in root.iterdir() if path.is_file() and DATE_FILE_RE.match(path.name)]
    entries_dir = root / ENTRIES_DIR
    if entries_dir.is_dir():
        for machine in entries_dir.iterdir():
            if machine.is_dir():
                files += [path for path in machine.iterdir() if path.is_file() and DATE_FILE_RE.match(path.name)]
    return sorted(files)


def iter_entries(root: Path) -> list[Entry]:
    """All entries, ordered by date then time (UTC since 0.2); a stable sort keeps file order on ties."""
    entries: list[Entry] = []
    for path in daily_files(root):
        date = dt.date.fromisoformat(path.stem)
        entries += parse_entries(path.read_text(encoding="utf-8"), date=date, path=path)
    return sorted(entries, key=lambda entry: (entry.date, entry.time))


def parse_entries(text: str, *, date: dt.date, path: Path) -> list[Entry]:
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    for idx, line in enumerate(lines[:-1]):
        if line.strip() == "---" and META_RE.match(lines[idx + 1].rstrip("\n")):
            starts.append(idx)

    entries: list[Entry] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        text_end = end
        while text_end > start and not lines[text_end - 1].strip():
            text_end -= 1
        raw = "".join(lines[start:text_end])
        if raw and not raw.endswith("\n"):
            raw += "\n"
        meta = META_RE.match(lines[start + 1].rstrip("\n"))
        if not meta:
            continue
        entry_type = meta.group("type")
        if entry_type not in ENTRY_TYPES:
            continue
        try:
            tags = parse_tags(meta.group("tags"))
        except NotchError:
            continue
        allowed = {"git", "model", "covers"} if entry_type == "digest" else {"git", "model"}
        extra: dict[str, str] = {}
        idx = start + 2
        while idx < text_end:
            match = META_LINE_RE.match(lines[idx].rstrip("\n"))
            if not match or match.group("key") not in allowed or match.group("key") in extra:
                break
            extra[match.group("key")] = match.group("value")
            idx += 1
        entries.append(
            Entry(
                date=date,
                path=path,
                text=raw,
                time=meta.group("time"),
                author=meta.group("author"),
                entry_id=meta.group("id"),
                project=meta.group("project"),
                entry_type=entry_type,
                tags=tags,
                git=extra.get("git"),
                model=extra.get("model"),
                covers=extra.get("covers"),
            )
        )
    return entries


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_today() -> dt.date:
    return utc_now().date()


def parse_since(value: str) -> dt.date:
    match = re.match(r"^(\d+)d$", value)
    if match:
        return utc_today() - dt.timedelta(days=int(match.group(1)))
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise NotchError("--since must look like 7d or 2026-11-16") from None


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def read_entry_from_editor(initial: str) -> str:
    editor = os.environ.get("EDITOR")
    if not editor:
        raise NotchError("$EDITOR is not set; use --entry for inline entries")
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as handle:
        handle.write(initial)
        handle.flush()
        name = handle.name
    try:
        try:
            subprocess.run([*shlex.split(editor), name], check=True)
        except subprocess.CalledProcessError as exc:
            raise NotchError(f"editor exited with status {exc.returncode}") from exc
        return Path(name).read_text(encoding="utf-8")
    finally:
        try:
            Path(name).unlink()
        except FileNotFoundError:
            pass


def format_os_error(exc: OSError) -> str:
    if exc.filename:
        return f"{exc.strerror}: {exc.filename}"
    return str(exc)


def read_entry_from_terminal() -> str:
    print_section("Entry")
    print("Write as much as you need. End with a single '.' on its own line.")
    lines: list[str] = []
    while True:
        try:
            if interactive_terminal():
                line = read_line("\n> ")
            else:
                line = input("> ")
        except EOFError:
            break
        if line == ".":
            break
        lines.append(line)
    return "\n".join(lines)

def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def read_char() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_line(prompt_text: str) -> str:
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    chars: list[str] = []
    while True:
        char = read_char()
        if char == "\x1b":
            raise Cancelled()
        if char in {"\r", "\n"}:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars)
        if char in {"\x7f", "\b"}:
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x04" and not chars:
            raise Cancelled()
        if char.isprintable() or char == "\t":
            chars.append(char)
            sys.stdout.write(char)
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Sync: each machine commits only entries/<its id>/, so pulls never conflict.


def run_git(root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang waiting for credentials
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], 124, "", f"git {args[0]} timed out after {timeout:.0f}s")
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], 127, "", str(exc))


def git_error(result: subprocess.CompletedProcess) -> str:
    lines = [line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip()]
    for line in lines:  # git's own diagnosis, rather than the hints that follow it
        if line.startswith(("fatal:", "error:", "! [")):
            return line
    return lines[-1] if lines else f"exit status {result.returncode}"


def notebook_remote(root: Path) -> bool:
    """True when the notebook is a git checkout with a remote to sync with."""
    if not (root / ".git").exists():
        return False
    result = run_git(root, "remote", timeout=5)
    return result.returncode == 0 and bool(result.stdout.strip())


def sync_stamp(root: Path) -> Path:
    return root / ".git" / "notch-last-sync"  # inside .git, so it is never committed


def sync_notebook(root: Path) -> str | None:
    """Commit this machine's entries, pull, and push. Returns an error message, or None on success."""
    if not notebook_remote(root):
        return "the notebook is not a git repository with a remote"
    error = None
    with file_lock(root):
        machine = machine_id(root)
        if (root / ENTRIES_DIR / machine).exists():
            run_git(root, "add", "-A", "--", f"{ENTRIES_DIR}/{machine}")
            if run_git(root, "diff", "--cached", "--quiet").returncode == 1:
                commit = run_git(root, "commit", "-q", "-m", f"notch: entries from {machine}")
                if commit.returncode != 0:
                    error = f"commit failed: {git_error(commit)}"
        has_upstream = run_git(root, "rev-parse", "--abbrev-ref", "@{u}", timeout=5).returncode == 0
        branch = run_git(root, "symbolic-ref", "-q", "--short", "HEAD", timeout=5).stdout.strip()
        if error is None and not has_upstream and branch:
            # A clone of an empty remote has no upstream until someone pushes; adopt it once it exists.
            fetch = run_git(root, "fetch", "-q", "origin")
            if fetch.returncode != 0:
                error = f"fetch failed: {git_error(fetch)}"
            elif run_git(root, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}", timeout=5).returncode == 0:
                run_git(root, "branch", "-q", f"--set-upstream-to=origin/{branch}", timeout=5)
                has_upstream = True
        if error is None and has_upstream:
            pull = run_git(root, "pull", "--rebase", "--autostash", "-q")
            if pull.returncode != 0:
                if (root / ".git" / "rebase-merge").exists() or (root / ".git" / "rebase-apply").exists():
                    run_git(root, "rebase", "--abort")
                error = f"pull failed: {git_error(pull)}"
        if error is None and run_git(root, "rev-parse", "-q", "--verify", "HEAD", timeout=5).returncode == 0:
            ahead = run_git(root, "rev-list", "--count", "@{u}..HEAD", timeout=5) if has_upstream else None
            if ahead is None or ahead.stdout.strip() not in {"", "0"}:
                push = run_git(root, "push", "-q", *(() if has_upstream else ("-u", "origin", "HEAD")))
                if push.returncode != 0:
                    error = f"push failed: {git_error(push)}"
        sync_stamp(root).write_text(error or "ok", encoding="utf-8")
    return error


def last_sync(root: Path) -> tuple[float, str] | None:
    """(time, 'ok' or the error) of the last sync attempt on this machine."""
    stamp = sync_stamp(root)
    if not stamp.exists():
        return None
    return stamp.stat().st_mtime, stamp.read_text(encoding="utf-8").strip() or "ok"


def sync_if_stale(root: Path) -> str | None:
    """Sync when the last attempt is over SYNC_INTERVAL old; return a message for the reader, if any."""
    if os.environ.get("NOTCH_NO_SYNC") or not notebook_remote(root):
        return None
    previous = last_sync(root)
    if previous is None or time.time() - previous[0] > SYNC_INTERVAL:
        error = sync_notebook(root)
    else:
        error = None if previous[1] == "ok" else previous[1]
    if error:
        return (
            f"Notebook sync failed ({error}). Entries from other machines may be missing. "
            "Tell the user; `notch sync` retries."
        )
    return None


def start_background_sync(root: Path) -> None:
    """Push a new entry without making the writer wait; failures show up in the next catchup."""
    if os.environ.get("NOTCH_NO_SYNC") or not notebook_remote(root):
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "notch.cli", "sync", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ, "NB_DIR": str(root)},
        )
    except OSError:
        pass


def cmd_sync(args: argparse.Namespace) -> int:
    root = notebook_dir()
    error = sync_notebook(root)
    if error:
        if not args.quiet:
            print(f"notch: sync failed: {error}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"synced {root}")
    return 0


# ---------------------------------------------------------------------------
# Versions: notch never updates itself; it tells the reader what to run.


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def notebook_settings(root: Path) -> dict:
    path = root / SETTINGS_FILE
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {}


def min_version_notice(root: Path) -> str | None:
    needed = str(notebook_settings(root).get("min_version", ""))
    installed = notch_version()
    if needed and installed != "unknown" and version_tuple(installed) < version_tuple(needed):
        return (
            f"This notebook needs notch {needed} or newer; {installed} is installed. "
            f"Ask the user before updating: {upgrade_command()}"
        )
    return None


def require_min_version(root: Path) -> None:
    notice = min_version_notice(root)
    if notice:
        raise NotchError(f"not written. {notice}")


def install_info() -> dict:
    """uv's record of where notch was installed from (direct_url.json), or {}."""
    dist = notch_distribution()
    text = dist.read_text("direct_url.json") if dist else None
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def upgrade_command() -> str:
    info = install_info()
    url = info.get("url", "")
    if info.get("vcs_info", {}).get("vcs") == "git":
        return f"uv tool install --force git+{url} && notch setup"
    if url.startswith("file:") and not info.get("dir_info", {}).get("editable"):
        source = Path(unquote(urlparse(url).path))
        return f"uv tool install --force --reinstall-package notch {source} && notch setup"
    return "uv tool upgrade notch && notch setup"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "notch"


def remote_update_notice() -> str | None:
    """Compare the installed git commit with the remote's, at most once per UPDATE_INTERVAL."""
    if os.environ.get("NOTCH_NO_UPDATE_CHECK"):
        return None
    vcs = install_info().get("vcs_info", {})
    url = install_info().get("url", "")
    installed = vcs.get("commit_id", "")
    if vcs.get("vcs") != "git" or not url or not installed:
        return None
    cache = cache_dir() / "update-check.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = {}
    if cached.get("url") == url and time.time() - cached.get("checked", 0) < UPDATE_INTERVAL:
        latest = cached.get("latest", "")
    else:
        ref = vcs.get("requested_revision") or "HEAD"
        result = run_git(Path.home(), "ls-remote", url, ref, timeout=10)
        latest = result.stdout.split()[0] if result.returncode == 0 and result.stdout.split() else ""
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"url": url, "checked": time.time(), "latest": latest}), encoding="utf-8")
        except OSError:
            pass
    if latest and latest != installed:
        return (
            f"A newer notch is available ({installed[:7]} -> {latest[:7]}). "
            f"Ask the user before updating: {upgrade_command()}"
        )
    return None


def update_notices(root: Path) -> list[str]:
    """Everything the reader should tell the user about this install, most urgent first."""
    notices = [min_version_notice(root), remote_update_notice()]
    local = local_update_check()
    if local:
        notices.append(f"The local notch source is newer than the install. Ask the user, then run: {upgrade_command()}")
    if not config_path().exists():
        notices.append(
            "Notch is not set up on this machine. Ask the user for their name and notebook, then run "
            "`notch setup --name NAME` (see `notch setup --help`)."
        )
    elif skill_path().exists() and skill_path().read_text(encoding="utf-8") != packaged_skill():
        notices.append("The installed notch skill does not match this version of notch. Run `notch setup` to refresh it.")
    return [notice for notice in notices if notice]


def print_notices(notices: list[str]) -> None:
    if notices:
        print("== Notices ==")
        for notice in notices:
            print(f"- {notice}")
        print()


# ---------------------------------------------------------------------------
# Agent instructions, shipped inside the package so they update with notch.


def packaged_skill() -> str:
    return importlib.resources.files("notch").joinpath("SKILL.md").read_text(encoding="utf-8")


def skill_body() -> str:
    """The skill without its frontmatter, for agents that have no skills directory."""
    text = packaged_skill()
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1].lstrip("\n")
    return text


def skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "notch" / "SKILL.md"


GLOBAL_INSTRUCTIONS = """\
## Notch

Notch is a shared plain-text lab notebook for durable, cross-session project
notes. Before starting work in a git repo, run `notch catchup` there and act on
what it prints. {how}

Log outcomes, not progress: write an entry when a result is measured, a
decision is settled, or a session ends with work in flight.
"""
CLAUDE_HOW = "The `notch` skill has the conventions."
OTHER_HOW = "Run `notch guide` once per session first: it prints the conventions."

SETUP_EXPLAINER = """\
How notch works
- Notch is a shared lab notebook. You and your agents log outcomes (results,
  decisions, findings, where work stands at the end of a session), not steps.
- Each machine writes only its own files, entries/<machine>/YYYY-MM-DD.txt, so
  syncing through git never conflicts. Times and dates are UTC.
- Sync is automatic: `notch add` pushes in the background, and `notch catchup`
  pulls when the last sync is over an hour old. `notch sync` does it by hand.
- Agents start work in a repo with `notch catchup`. A repo with no project yet
  makes the agent ask you whether to start one (`notch ignore` says no).
- Each project gets a weekly digest, which agents write from the Wednesday
  after the week ends.
- When notch needs an update, it shows your agent the command and the agent
  asks you first.
Try: `notch` (entry form), `notch projects`, `notch catchup PROJECT`.
"""


def upsert_block(path: Path, content: str) -> str:
    """Put content between the notch markers in path, replacing an earlier block. Returns what happened."""
    block = f"{BLOCK_BEGIN}\n{content.rstrip()}\n{BLOCK_END}\n"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if BLOCK_BEGIN in text and BLOCK_END in text:
        head, rest = text.split(BLOCK_BEGIN, 1)
        tail = rest.split(BLOCK_END, 1)[1].lstrip("\n")
        updated = head + block + (f"\n{tail}" if tail else "")
        action = "updated" if updated != text else "unchanged"
    else:
        updated = (text.rstrip("\n") + "\n\n" + block) if text.strip() else block
        action = "added"
    if updated != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    return action


def cmd_guide(args: argparse.Namespace) -> int:
    print(skill_body(), end="")
    return 0


# ---------------------------------------------------------------------------
# Setup: everything one machine needs, runnable by an agent (flags only).


def cmd_setup(args: argparse.Namespace) -> int:
    config = load_config()
    name = sanitize_name(args.name or config.get("name") or getpass.getuser() or "")
    if not name:
        raise NotchError("pass --name with your name, e.g. --name andrew")
    default = Path(config.get("notebook") or Path.home() / "Documents" / "notch-notebook")
    root = Path(args.notebook).expanduser() if args.notebook else default.expanduser()
    root = root.resolve()

    if args.clone:
        if root.exists() and any(root.iterdir()):
            origin = run_git(root, "remote", "get-url", "origin", timeout=5).stdout.strip() if (root / ".git").exists() else ""
            if origin.rstrip("/").removesuffix(".git") != args.clone.rstrip("/").removesuffix(".git"):
                raise NotchError(f"{root} exists and is not a clone of {args.clone}; pass --notebook with another path")
            print(f"notebook: {root} (already cloned)")
        else:
            root.parent.mkdir(parents=True, exist_ok=True)
            clone = run_git(root.parent, "clone", "-q", args.clone, str(root), timeout=300)
            if clone.returncode != 0:
                raise NotchError(f"clone failed: {git_error(clone)}")
            print(f"notebook: cloned {args.clone} to {root}")
    else:
        existed = root.exists()
        root.mkdir(parents=True, exist_ok=True)
        print(f"notebook: {root}{'' if existed else ' (created)'}")

    config.update(notebook=str(root), name=name)
    save_config(config)
    print(f"config:   {config_path()} (name {name})")
    if os.environ.get("NB_DIR") and Path(os.environ["NB_DIR"]).expanduser().resolve() != root:
        print(f"warning:  NB_DIR={os.environ['NB_DIR']} is set and overrides the configured notebook")

    id_path = root / ".machine-id"
    old_id = id_path.read_text(encoding="utf-8").strip() if id_path.exists() else ""
    if old_id.startswith(f"{name}-"):
        print(f"machine:  {old_id}")
    else:
        new_id = new_machine_id()
        id_path.write_text(new_id + "\n", encoding="utf-8")
        print(f"machine:  {new_id}" + (f" (was {old_id}; earlier entries keep their IDs)" if old_id else ""))

    changed = []
    gitignore = root / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    missing = [line for line in NOTEBOOK_GITIGNORE if line not in lines]
    if missing:
        gitignore.write_text("\n".join([*lines, *missing]) + "\n", encoding="utf-8")
        changed.append(".gitignore")
    settings = root / SETTINGS_FILE
    if not settings.exists():
        settings.write_text(
            "# Shared notch settings for this notebook. Raise min_version when the\n"
            "# notebook needs a newer notch; older installs then refuse to write.\n"
            f'min_version = "{notch_version()}"\n',
            encoding="utf-8",
        )
        changed.append(SETTINGS_FILE)
    if changed and (root / ".git").exists():
        run_git(root, "add", "--", *changed)
        run_git(root, "commit", "-q", "-m", "notch: notebook settings", "--", *changed)
    if changed:
        print(f"files:    wrote {', '.join(changed)}")

    legacy = [path for path in root.iterdir() if path.is_file() and DATE_FILE_RE.match(path.name)]
    if legacy:
        print(
            f"layout:   {len(legacy)} daily files use the old shared layout. Once every machine has synced\n"
            "          them, run `notch migrate` on one machine to move them into per-machine folders."
        )

    skill = skill_path()
    before = skill.read_text(encoding="utf-8") if skill.exists() else None
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(packaged_skill(), encoding="utf-8")
    print(f"skill:    {skill} ({'unchanged' if before == packaged_skill() else 'installed'})")
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    action = upsert_block(claude_md, GLOBAL_INSTRUCTIONS.format(how=CLAUDE_HOW))
    print(f"claude:   {claude_md} ({action})")
    codex_dir = Path.home() / ".codex"
    if codex_dir.is_dir():
        action = upsert_block(codex_dir / "AGENTS.md", GLOBAL_INSTRUCTIONS.format(how=OTHER_HOW))
        print(f"codex:    {codex_dir / 'AGENTS.md'} ({action})")
    print("other agents: point them at `notch guide` and `notch catchup`.")

    if notebook_remote(root):
        if not run_git(root, "config", "user.name", timeout=5).stdout.strip():
            print("warning:  git user.name is not set; sync cannot commit until it is (git config --global user.name ...)")
        error = sync_notebook(root)
        print(f"sync:     {'ok' if error is None else 'FAILED: ' + error}")
    else:
        print("sync:     off (the notebook is not a git repository with a remote; entries stay on this machine)")
    print()
    print(SETUP_EXPLAINER, end="")
    return 0


# ---------------------------------------------------------------------------
# Migration from the old layout (all machines appending to YYYY-MM-DD.txt).


def entry_machine(meta: re.Match) -> str | None:
    """The machine segment of an entry ID: <project>-<YYYYMMDD>-<machine>-<NNN>."""
    entry_id, project = meta.group("id"), meta.group("project")
    if not entry_id.startswith(f"{project}-"):
        return None
    machine = entry_id[len(project) + 10 : -4]
    return machine if NAME_RE.match(machine) else None


def cmd_migrate(args: argparse.Namespace) -> int:
    root = notebook_dir()
    legacy = sorted(path for path in root.iterdir() if path.is_file() and DATE_FILE_RE.match(path.name)) if root.exists() else []
    if not legacy:
        print("nothing to migrate: no daily files at the notebook root")
        return 0
    with file_lock(root):
        before = sorted(entry.entry_id for entry in iter_entries(root))
        additions: dict[Path, list[str]] = {}
        for path in legacy:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            starts = [
                idx for idx, line in enumerate(lines[:-1])
                if line.strip() == "---" and META_RE.match(lines[idx + 1].rstrip("\n"))
            ]
            if not starts or "".join(lines[: starts[0]]).strip():
                raise NotchError(f"{path.name} has text before its first entry; move it into an entry by hand, then rerun")
            for pos, start in enumerate(starts):
                end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
                chunk = "".join(lines[start:end]).rstrip("\n") + "\n"
                machine = entry_machine(META_RE.match(lines[start + 1].rstrip("\n")))
                if machine is None:
                    raise NotchError(f"{path.name}: cannot read the machine from '{lines[start + 1].strip()}'")
                additions.setdefault(root / ENTRIES_DIR / machine / path.name, []).append(chunk)
        new_text: dict[Path, str] = {}
        for target, chunks in additions.items():
            existing = target.read_text(encoding="utf-8").rstrip("\n") if target.exists() else ""
            new_text[target] = "\n".join(([existing + "\n"] if existing else []) + chunks)
        # Check nothing is lost before touching any file.
        after = [entry.entry_id for path in daily_files(root) if path not in legacy and path not in new_text
                 for entry in parse_entries(path.read_text(encoding="utf-8"), date=dt.date.fromisoformat(path.stem), path=path)]
        for target, text in new_text.items():
            after += [entry.entry_id for entry in parse_entries(text, date=dt.date.fromisoformat(target.stem), path=target)]
        if sorted(after) != before:
            raise NotchError("migration check failed: entries would change; nothing was moved")
        for target, text in new_text.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for path in legacy:
            path.unlink()
        machines = sorted({target.parent.name for target in new_text})
        print(f"moved {len(before)} entries from {len(legacy)} daily files into {ENTRIES_DIR}/ ({', '.join(machines)})")
        if (root / ".git").exists():
            run_git(root, "rm", "-q", "--cached", "--ignore-unmatch", "--", *(path.name for path in legacy))
            run_git(root, "add", "-A", "--", ENTRIES_DIR)
            commit = run_git(root, "commit", "-q", "-m", "notch: move daily files into per-machine folders")
            if commit.returncode == 0:
                print("committed; run `notch sync` to push")
            else:
                print(f"not committed: {git_error(commit)}")
    return 0


# ---------------------------------------------------------------------------
# Projects and repos.


def cmd_projects(args: argparse.Namespace) -> int:
    entries = list(iter_entries(notebook_dir()))
    spellings = most_used_spellings((entry.project for entry in entries), str.casefold)
    stats: dict[str, dict] = {}
    for entry in entries:
        item = stats.setdefault(entry.project.casefold(), {"count": 0, "last": entry.date, "repos": {}, "authors": set()})
        item["count"] += 1
        item["last"] = max(item["last"], entry.date)
        item["authors"].add(entry.author)
        if entry.git:
            repo = entry.git.split(" ", 1)[0]
            item["repos"][repo] = item["repos"].get(repo, 0) + 1
    if not stats:
        print("no projects yet")
        return 0
    for key in sorted(stats, key=lambda key: stats[key]["last"], reverse=True):
        item = stats[key]
        repos = ", ".join(sorted(item["repos"], key=lambda repo: -item["repos"][repo])) or "-"
        print(
            f"{spellings[key]}  {count_entries(item['count'])}, last {item['last']}, "
            f"repos: {repos}, authors: {', '.join(sorted(item['authors']))}"
        )
    return 0


def cmd_ignore(args: argparse.Namespace) -> int:
    root = notebook_dir()
    repo = current_repo(root)
    if repo is None:
        raise NotchError("run this inside the git repo to ignore")
    config = load_config()
    ignored = [name for name in config.get("ignored_repos", []) if name.casefold() != repo[0].casefold()]
    if not args.undo:
        ignored.append(repo[0])
    config["ignored_repos"] = sorted(ignored)
    save_config(config)
    if args.undo:
        print(f"notch catchup will offer to log repo {repo[0]} again")
    else:
        print(f"notch catchup will not offer to log repo {repo[0]} on this machine (`notch ignore --undo` reverses it)")
    return 0


def repo_ignored(repo: tuple[str, str]) -> bool:
    return repo[0].casefold() in {name.casefold() for name in load_config().get("ignored_repos", [])}



class file_lock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.handle = None

    def __enter__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.handle = (self.root / ".notch.lock").open("w", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
