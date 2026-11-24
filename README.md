# Notch

Notch is a plain-text lab notebook CLI for humans and agents. A notebook is a
folder of daily text files, usually a git repo that several people and their
agents share.

## Getting started

Notch needs [uv](https://docs.astral.sh/uv/) and git. Install it and set up the machine:

```sh
uv tool install git+https://github.com/AndrewFalkowski/notch
notch setup --name YOUR_NAME --clone https://github.com/ORG/NOTEBOOK_REPO   # join a shared notebook
notch setup --name YOUR_NAME                                               # or: a local notebook
```

An agent can run `notch setup` for you; it takes only flags. It:

1. clones or creates the notebook (default `~/Documents/notch-notebook`; `--notebook PATH` picks another)
2. saves your name and the notebook path to `~/.config/notch/config.toml`
3. gives this machine an ID such as `andrew-melchior-8ec2`
4. installs the agent skill to `~/.claude/skills/notch/SKILL.md`
5. adds a short "run `notch catchup`" block to `~/.claude/CLAUDE.md`, and to
   `~/.codex/AGENTS.md` if Codex is installed
6. runs a test sync, and explains how notch works

Rerunning `notch setup` is safe: it refreshes the skill and instructions. Any
other agent can read the conventions with `notch guide`.

To start a shared notebook, create an empty private repo, then run
`notch setup --clone URL` on each machine.

## How it works

- **Files.** Each machine writes only `entries/<machine-id>/YYYY-MM-DD.txt`.
  Two machines never edit the same file, so git never has to merge one.
  Notch reads every machine's folder and orders entries by time.
- **Times.** Entry times and file dates are UTC.
- **Sync.** `notch add` commits and pushes in the background. `notch catchup`
  pulls when the last sync is over an hour old. `notch sync` does both now.
  Sync never asks for credentials, so set up git access (e.g. `gh auth login`)
  first.
- **Projects follow repos.** Entries record the repo they were written from.
  Inside a repo, `notch catchup` and `notch add` default to the project logged
  from it. A repo with nothing logged makes the agent ask whether to start a
  project; `notch ignore` declines for that repo on this machine.
- **Digests.** Each project gets a weekly digest, which an agent writes from
  the Wednesday (UTC) after the week ends, so every machine has synced by
  then. A second digest for the same week is refused unless you pass
  `--replace`.
- **Updates.** Notch never updates itself. Once a day it compares the
  installed commit with GitHub. If it is behind, or the notebook's `notch.toml`
  asks for a newer `min_version`, `catchup` and `info` print the command to
  run, and agents ask you first.

## Commands

```sh
notch                                   # full-screen entry form (humans)
notch catchup [PROJECT]                 # digest + recent headlines; inside a repo the project is optional
notch add --type finding --tags exp3 --entry "Bimodality confirmed."
notch search --project skewGP --since 7d --brief
notch search --project skewgp bimodal alpha --limit 5
notch show skewGP-20260624-luna-003 skewGP-20260624-luna-004
notch projects                          # projects, their repos and authors
notch today                             # today's entries (UTC), all machines
notch sync                              # commit, pull, push now
notch info                              # paths, machine ID, last sync, notices
notch guide                             # agent conventions
notch digest-prep PROJECT WEEK          # material for one weekly digest
notch ignore [--undo]                   # don't offer to log this repo
notch migrate                           # one-time move from the old shared-file layout
```

- Agents pass `--author` (Claude Code defaults to `claude`) and
  `--model MODEL_ID`. Notch records the model on a `model:` line.
- Project names and tags are case-insensitive, and tags treat `-` and `_` as
  the same. `add` reuses the most-used existing spelling.
- `search --brief` prints each entry's metadata line and first line.
  `--limit N` keeps the N most recent matches. Full search output is capped at
  20 entries unless you pass `--limit`.

### Entry form

Run `notch` without a subcommand for a compact full-screen entry form:

| To | Press |
| --- | --- |
| Move and select | Arrow keys |
| Choose a recent project or entry type | Number keys |
| Advance, or leave the entry editor | Enter |
| Add a line in the entry editor | Ctrl-J |
| Leave the editor, or cancel from the menu | Esc |
| Save | Ctrl-S, Ctrl-X, or `s` on the save row |

`notch cli` is a prompt-based alternative.

## Moving an old notebook to the per-machine layout

Before 0.2, every machine appended to shared `YYYY-MM-DD.txt` files at the
notebook root. Notch 0.2 still reads those, but writes per-machine files. To
move them:

1. On every machine, commit and push the old daily files.
2. Upgrade notch on every machine.
3. On one machine, run `notch migrate && notch sync`. It splits each old file
   by the machine in each entry ID and checks that no entry was lost before it
   deletes anything.
4. On the other machines, run `notch setup` and then `notch sync`.

## Development

```sh
uv run python -m unittest                              # tests
uv tool install --force --reinstall-package notch .   # make the global notch use this checkout
```

The agent skill is `src/notch/SKILL.md`. It ships inside the package and
`notch setup` installs it. Design notes are in `notebook-design.txt`.
