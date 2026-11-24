from __future__ import annotations

import argparse
import datetime as dt
import getpass
import importlib.metadata
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
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
META_LINE_RE = re.compile(r"^(?P<key>git|covers): (?P<value>\S.*)$")
ID_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]+-\d{8}-[A-Za-z0-9_-]+-\d{3}(?![A-Za-z0-9_-])")
WEEK_RE = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")
BRIEF_WIDTH = 200
CATCHUP_LIMIT = 40
SEARCH_FULL_LIMIT = 20
CATCHUP_TAGS = 15
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
    covers: str | None = None

    @property
    def body(self) -> str:
        lines = self.text.splitlines()[2:]
        present = {key for key, value in (("git", self.git), ("covers", self.covers)) if value}
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
    add.add_argument("--project", "-p", required=True)
    add.add_argument("--type", "-t", default="note", choices=sorted(ENTRY_TYPES))
    add.add_argument("--tags", default="")
    add.add_argument("--entry", "-e", dest="entry", help="entry text, or - to read it from stdin")
    add.add_argument("--message", "-m", dest="entry", help=argparse.SUPPRESS)
    add.add_argument("--author", default=argparse.SUPPRESS)
    add.add_argument("--covers", metavar="WEEK", help="for --type digest: the ISO week covered, e.g. 2026-W47")
    add.set_defaults(func=cmd_add)

    catchup = subparsers.add_parser("catchup", help="latest digest, recent entries, and undigested weeks")
    catchup.add_argument("project")
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

    update_check = subparsers.add_parser("update-check", help="check local source for a newer install")
    update_check.set_defaults(func=cmd_update_check)

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
    if args.entry == "-" or (args.entry is None and not sys.stdin.isatty()):
        entry = sys.stdin.read()
        if not entry.strip():
            raise NotchError("no entry text: pass --entry TEXT, or --entry - with text on stdin")
    else:
        entry = args.entry if args.entry is not None else read_entry_from_editor("")
    author = getattr(args, "author", None)
    projects = [project for project, _ in recent_projects(notebook_dir())]
    if projects and args.project.casefold() not in {project.casefold() for project in projects}:
        print(
            f"notch: started new project '{args.project}' (recent: {', '.join(projects[:8])})",
            file=sys.stderr,
        )
    entry_id = add_entry(
        project=args.project,
        entry_type=args.type,
        tags=args.tags,
        entry=entry,
        author=author,
        covers=args.covers,
    )
    if args.type == "digest":
        length = len(ID_RE.sub("", entry.strip()))
        print(f"notch: digest is {length:,} characters without cited IDs (target: under 2,500)", file=sys.stderr)
    if not entry_id.startswith(f"{args.project}-"):
        canonical = entry_id.rsplit("-", 4)[0]
        print(f"notch: using existing project name '{canonical}' for '{args.project}'", file=sys.stderr)
    print(entry_id)
    return 0


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
    path = notebook_dir() / f"{dt.date.today().isoformat()}.txt"
    if path.exists():
        print(path.read_text(), end="")
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
    project, entries = project_entries(iter_entries(notebook_dir()), args.project)
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
    project, entries = project_entries(iter_entries(notebook_dir()), args.project)
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
        print(f"A digest for {week} already exists ({digests[week].entry_id}); a new one replaces it.")
    print()
    print(DIGEST_INSTRUCTIONS.replace("PROJECT WEEK", f"{project} {week}"))
    print("Save it with (the quoted heredoc avoids shell-quoting problems):")
    print(f"  notch add --project {project} --type digest --covers {week} --author AGENT --entry - <<'DIGEST'")
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
    current = format_week(dt.date.today())
    return sorted(week for week in week_entries(entries) if week < current and week not in digests)


def cmd_info(args: argparse.Namespace) -> int:
    root = notebook_dir()
    update = local_update_check()
    source = update.source_dir if update else working_tree_source_dir() or local_source_dir()
    print(f"Version:      {notch_version()}")
    print(f"Notebook dir: {root}")
    print(f"Author:       {args.author or default_author()}")
    print(f"Machine ID:   {machine_id_status(root)}")
    print(f"Source dir:   {source}")
    print(f"Update check: {update_status_text(update)}")
    print()
    print("Notes are plain text daily files in the notebook dir.")
    print("Local source edits do not automatically update a global uv tool install.")
    print("After changing Notch, run this from the source repo:")
    print("  uv tool install --force --reinstall-package notch .")
    return 0


def cmd_update_check(args: argparse.Namespace) -> int:
    update = local_update_check()
    if not update:
        print("No local source update found.")
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
    covers: str | None = None,
) -> str:
    parsed_tags = validate_entry_metadata(
        project=project,
        entry_type=entry_type,
        tags=tags,
        author=author,
    )
    entry = escape_entry_boundaries(entry.strip())
    if not entry:
        raise NotchError("entry is empty")

    root = notebook_dir()
    root.mkdir(parents=True, exist_ok=True)
    project, parsed_tags = canonical_names(root, project, parsed_tags)
    if entry_type == "digest":
        covers = validate_digest(root, covers, entry)
        git_line = ""  # repo state at writing time says nothing about the week covered
    else:
        if covers:
            raise NotchError("--covers is only for --type digest")
        git = git_context(root)
        git_line = f"git: {git}\n" if git else ""
    covers_line = f"covers: {covers}\n" if covers else ""
    today = dt.date.today()
    path = root / f"{today.isoformat()}.txt"

    with file_lock(root):
        machine = machine_id(root)
        existing = path.read_text() if path.exists() else ""
        seq = next_sequence(existing, project, today, machine)
        entry_id = f"{project}-{today.strftime('%Y%m%d')}-{machine}-{seq:03d}"
        now = dt.datetime.now().strftime("%H:%M")
        tag_text = ", ".join(parsed_tags)
        text = f"---\n[{now}] {author or default_author()} | {entry_id} | {project} | {entry_type} | {tag_text}\n{git_line}{covers_line}{entry}\n"
        with path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            if existing:
                handle.write("\n")
            handle.write(text)
    return entry_id


def validate_digest(root: Path, covers: str | None, entry: str) -> str:
    if not covers:
        raise NotchError("digest entries need --covers WEEK, e.g. --covers 2026-W47")
    week = format_week(parse_week(covers)[0])
    require_closed_week(week)
    known = {existing.entry_id for existing in iter_entries(root)}
    unknown = sorted(set(ID_RE.findall(entry)) - known)
    if unknown:
        raise NotchError(f"digest cites unknown entry IDs: {', '.join(unknown)}")
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


def require_closed_week(week: str) -> None:
    if parse_week(week)[1] >= dt.date.today():
        raise NotchError(f"{week} is not over yet; digests cover completed weeks")


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
    text = f"{Path(toplevel).name} {branch}@{sha}"
    return f"{text} dirty" if dirty else text


def notebook_dir() -> Path:
    configured = os.environ.get("NB_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "notch-notebook"


def default_author() -> str:
    raw = os.environ.get("NOTCH_AUTHOR")
    if not raw and os.environ.get("CLAUDECODE") == "1" and not sys.stdin.isatty():
        # An agent's shell inside Claude Code; a human there would be at a terminal.
        raw = "claude"
    raw = raw or getpass.getuser() or "user"
    return sanitize_name(raw) or "user"


def machine_id(root: Path) -> str:
    path = root / ".machine-id"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        validate_name("machine id", value)
        return value

    base = sanitize_name(socket.gethostname().split(".")[0]) or "machine"
    value = f"{base}-{uuid.uuid4().hex[:4]}"
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


def iter_entries(root: Path) -> Iterable[Entry]:
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if not path.is_file() or not DATE_FILE_RE.match(path.name):
            continue
        date = dt.date.fromisoformat(path.stem)
        yield from parse_entries(path.read_text(encoding="utf-8"), date=date, path=path)


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
        allowed = {"git", "covers"} if entry_type == "digest" else {"git"}
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
                covers=extra.get("covers"),
            )
        )
    return entries


def parse_since(value: str) -> dt.date:
    match = re.match(r"^(\d+)d$", value)
    if match:
        return dt.date.today() - dt.timedelta(days=int(match.group(1)))
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
