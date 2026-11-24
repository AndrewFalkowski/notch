# Notch

Notch is a plain-text lab notebook CLI for humans and agents.

Entries are stored as daily text files in `~/Documents/notch-notebook/` by default.
Set `NB_DIR` to use another directory.

```sh
notch add --project skewGP --type finding --tags exp3,sGP --entry "Bimodality confirmed."
notch
notch cli
notch today
notch catchup skewGP
notch search --project skewGP --since 7d --brief
notch search --project skewgp bimodal alpha --limit 5
notch show skewGP-20260624-luna-003 skewGP-20260624-luna-004
notch info
notch update-check
```

Run `notch` without a subcommand for a compact full-screen entry form. Use
arrows to move and select, number keys to choose recent projects and entry
types, Enter to advance or leave the entry editor, arrow keys also move within
the entry editor, Ctrl-J to add an entry line, Esc to leave the entry editor or
cancel from the menu, and Ctrl-S, Ctrl-X, or `s` on the save row to save. The
active editable field shows a block cursor.

Run `notch cli` for the prompt-based add flow with selectable
projects and entry types, a multiline entry editor, and a save preview. In a real
terminal, number keys select menu items immediately, `0` creates a new project,
and `Esc` cancels. Finish the entry with a single `.` on its own line. Use
`NOTCH_AUTHOR` or `--author` to set the author marker.

`notch tui` remains available as an explicit alias for the default TUI.

Project names and tags are case-insensitive (tags also treat `-` and `_` as
the same); `add` reuses the most-used existing spelling of each. `search --brief` prints each entry's metadata line and first line,
`--limit N` keeps the N most recent matches, and query words must all appear.
Full-text search output is capped at 20 entries unless `--limit` is given.
`show` takes several IDs and lists later entries that reference each one.

`notch catchup PROJECT` prints the latest weekly digest, headlines of entries
since, and completed weeks still missing a digest. `notch digest-prep PROJECT
WEEK` prints instructions and material for an agent to write one, saved with
`notch add --type digest --covers WEEK`.

When `notch add` runs inside a git checkout, it records the repo, branch, and
commit on a line under the metadata, e.g. `git: notch main@abc1234 dirty`.

Agents should read `SKILLS.md` for project conventions before writing entries.

## Install And Updates

Install globally from this source directory:

```sh
uv tool install .
```

A global uv tool install does not automatically pick up later source edits.
When Notch can detect that local source is newer than the installed tool, the
interactive `notch` flow prompts before updating. You can also check manually:

```sh
notch update-check
```

The update command it runs is:

```sh
uv tool install --force --reinstall-package notch .
```

Run `notch info` to see the notebook directory, author, machine ID status, and
the update reminder.

## Development

Run tests from the project root:

```sh
uv run python -m unittest
```
