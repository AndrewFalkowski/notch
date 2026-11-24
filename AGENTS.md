# Agent Instructions

This repository is the source code for the `notch` CLI. The notebook itself
lives elsewhere (`~/Documents/notch-notebook/`, or `$NB_DIR`); no notebook
entries are checked in here.

- Notch usage and entry-writing conventions: `src/notch/SKILL.md` (the packaged
  skill; `notch setup` installs it, `notch guide` prints it). Log work on this
  repo under project `notch`; catch up with
  `notch catchup notch`.
- Design and file format: `notebook-design.txt`.
- Tests: `uv run python -m unittest`.
- Source edits do not reach the global `notch` until it is reinstalled:
  `uv tool install --force --reinstall-package notch .`
- Keep the daily file format readable by older installs: put new metadata on
  its own `key: value` line under the metadata line, never as a new
  metadata-line field.
- Each machine writes only `entries/<machine-id>/` in the notebook. Keep it
  that way: anything two machines write to the same file breaks git sync.
