import datetime as dt
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from notch import cli
from notch.tui_textual import append_tag_separator, build_textual_tui, display_entry_lines, save_tui_entry

REAL_GIT_CONTEXT = cli.git_context


class NotchCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"NB_DIR": self.tmp.name, "NOTCH_AUTHOR": "andrew"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git = patch.object(cli, "git_context", return_value=None)
        self.git.start()
        self.addCleanup(self.git.stop)

    def run_cli(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_quoted_entry_does_not_split_into_a_duplicate(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        first = cli.add_entry(project="skewGP", entry_type="note", tags="", entry="original")
        quoted = next(cli.iter_entries(root)).text
        second = cli.add_entry(project="skewGP", entry_type="note", tags="", entry=f"Quoting:\n{quoted}tail text")

        entries = list(cli.iter_entries(root))

        self.assertEqual([e.entry_id for e in entries], [first, second])
        self.assertIn(f"---\n> [", entries[1].text)
        self.assertTrue(entries[1].text.endswith("tail text\n"))

    def test_search_project_is_case_insensitive(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        (root / "2026-06-24.txt").write_text(
            "---\n[10:00] andrew | MicroStruct-20260624-luna-001 | MicroStruct | note | \nupper\n\n"
            "---\n[10:01] codex | microstruct-20260624-luna-001 | microstruct | note | \nlower\n",
            encoding="utf-8",
        )

        code, out, _ = self.run_cli("search", "--project", "MICROSTRUCT")

        self.assertEqual(code, 0)
        self.assertIn("upper", out)
        self.assertIn("lower", out)

    def test_add_reuses_most_used_project_spelling(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="MicroStruct", entry_type="note", tags="", entry="one")
        cli.add_entry(project="MicroStruct", entry_type="note", tags="", entry="two")

        code, out, err = self.run_cli("add", "--project", "microstruct", "--entry", "three")

        today = dt.date.today().strftime("%Y%m%d")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"MicroStruct-{today}-luna-003")
        self.assertIn("using existing project name 'MicroStruct'", err)
        self.assertEqual([p for p, _ in cli.recent_projects(root)], ["MicroStruct"])

    def test_show_accepts_multiple_ids_and_lists_backlinks(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        first = cli.add_entry(project="skewGP", entry_type="todo", tags="", entry="check seeds")
        second = cli.add_entry(project="skewGP", entry_type="finding", tags="", entry=f"Seeds fine; closes {first}.")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry=f"Unrelated {first}0 lookalike.")

        code, out, _ = self.run_cli("show", first, second)

        self.assertEqual(code, 0)
        self.assertIn("check seeds", out)
        self.assertIn("Seeds fine", out)
        self.assertIn(f"-> referenced by: {second} (finding)\n", out)
        self.assertEqual(out.count("referenced by"), 1)

    def test_show_reports_missing_ids_after_printing_found_ones(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        entry_id = cli.add_entry(project="skewGP", entry_type="note", tags="", entry="present")

        code, out, err = self.run_cli("show", entry_id, "skewGP-20990101-luna-001")

        self.assertEqual(code, 2)
        self.assertIn("present", out)
        self.assertIn("entry not found: skewGP-20990101-luna-001", err)

    def test_search_brief_limit_and_all_words(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="alpha is bimodal\nsecond line")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="bimodal only")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="Bimodal ALPHA again\nmore detail")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="latest alpha bimodal")

        code, out, _ = self.run_cli("search", "bimodal", "alpha", "--brief", "--limit", "2")

        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("skewGP-", lines[0])
        self.assertEqual(lines[1], "  Bimodal ALPHA again")
        self.assertEqual(lines[3], "  latest alpha bimodal")
        self.assertNotIn("more detail", out)

    def test_since_accepts_iso_date(self):
        self.assertEqual(cli.parse_since("2026-11-16"), dt.date(2026, 11, 16))
        with self.assertRaises(cli.NotchError):
            cli.parse_since("last week")

    def test_add_writes_git_line_and_parser_reads_it(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        with patch.object(cli, "git_context", return_value="notch main@abc1234 dirty"):
            cli.add_entry(project="skewGP", entry_type="note", tags="", entry="headline\nbody")

        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("| skewGP | note | \ngit: notch main@abc1234 dirty\nheadline\n", text)
        entry = next(cli.iter_entries(root))
        self.assertEqual(entry.git, "notch main@abc1234 dirty")
        self.assertEqual(entry.headline, "headline")
        self.assertEqual(entry.body, "headline\nbody")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_context_reports_repo_branch_commit_and_dirty(self):
        repo = Path(self.tmp.name) / "work"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        (repo / "f.txt").write_text("a\n", encoding="utf-8")
        subprocess.run([*git, "add", "f.txt"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        sha = subprocess.run([*git, "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        notebook = Path(self.tmp.name)

        self.assertEqual(REAL_GIT_CONTEXT(notebook, cwd=repo), f"work main@{sha}")
        (repo / "f.txt").write_text("b\n", encoding="utf-8")
        self.assertEqual(REAL_GIT_CONTEXT(notebook, cwd=repo), f"work main@{sha} dirty")
        self.assertIsNone(REAL_GIT_CONTEXT(repo, cwd=repo))
        self.assertIsNone(REAL_GIT_CONTEXT(notebook, cwd=notebook))

    def test_add_generates_machine_scoped_ids(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        first = cli.add_entry(project="skewGP", entry_type="note", tags="exp3", entry="first")
        second = cli.add_entry(project="skewGP", entry_type="note", tags="exp3", entry="second")

        today = dt.date.today().strftime("%Y%m%d")
        self.assertEqual(first, f"skewGP-{today}-luna-001")
        self.assertEqual(second, f"skewGP-{today}-luna-002")

    def test_parser_allows_markdown_rule_inside_entry(self):
        text = (
            "---\n"
            "[14:32] andrew | skewGP-20260624-luna-001 | skewGP | note | exp3\n"
            "before\n"
            "---\n"
            "after\n"
        )

        entries = cli.parse_entries(text, date=dt.date(2026, 6, 24), path=Path("x"))

        self.assertEqual(len(entries), 1)
        self.assertIn("---\nafter", entries[0].text)

    def test_parser_skips_invalid_hand_edited_tags(self):
        text = (
            "---\n"
            "[14:32] andrew | bad-20260624-luna-001 | bad | note | bad tag\n"
            "invalid\n"
            "---\n"
            "[14:33] andrew | good-20260624-luna-001 | good | note | ok\n"
            "valid\n"
        )

        entries = cli.parse_entries(text, date=dt.date(2026, 6, 24), path=Path("x"))

        self.assertEqual([entry.project for entry in entries], ["good"])

    def test_parser_skips_invalid_hand_edited_type(self):
        text = (
            "---\n"
            "[14:32] andrew | bad-20260624-luna-001 | bad | custom | tag\n"
            "invalid\n"
        )

        entries = cli.parse_entries(text, date=dt.date(2026, 6, 24), path=Path("x"))

        self.assertEqual(entries, [])

    def test_add_separates_entries_with_blank_line(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="first")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="second")

        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("first\n\n---\n", text)

    def test_parser_trims_inter_entry_spacer_from_entry_text(self):
        text = (
            "---\n"
            "[14:32] andrew | one-20260624-luna-001 | one | note | tag\n"
            "first\n"
            "\n"
            "---\n"
            "[14:33] andrew | two-20260624-luna-001 | two | note | tag\n"
            "second\n"
        )

        entries = cli.parse_entries(text, date=dt.date(2026, 6, 24), path=Path("x"))

        self.assertEqual(entries[0].text, "---\n[14:32] andrew | one-20260624-luna-001 | one | note | tag\nfirst\n")

    def test_show_prints_matching_entry(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        entry_id = cli.add_entry(project="skewGP", entry_type="finding", tags="exp3", entry="found it")

        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["show", entry_id])

        self.assertEqual(code, 0)
        self.assertIn("found it", out.getvalue())

    def test_global_author_applies_to_add_subcommand(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["--author", "codex", "add", "--project", "skewGP", "--entry", "from global"])

        self.assertEqual(code, 0)
        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("codex |", text)

    def test_add_message_alias_still_writes_entry(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["add", "--project", "skewGP", "--message", "legacy inline text"])

        self.assertEqual(code, 0)
        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("legacy inline text", text)

    def test_add_reports_nb_dir_file_without_traceback(self):
        nb_file = Path(self.tmp.name) / "not-a-directory"
        nb_file.write_text("", encoding="utf-8")
        err = StringIO()

        with patch.dict(os.environ, {"NB_DIR": str(nb_file)}, clear=False), redirect_stderr(err):
            code = cli.main(["add", "--project", "skewGP", "--entry", "entry"])

        self.assertEqual(code, 2)
        self.assertIn("notch:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_editor_failure_reports_without_traceback(self):
        err = StringIO()

        with patch.dict(os.environ, {"EDITOR": "false"}, clear=False), patch("sys.stdin.isatty", return_value=True), redirect_stderr(err):
            code = cli.main(["add", "--project", "skewGP"])

        self.assertEqual(code, 2)
        self.assertIn("editor exited with status", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_add_subcommand_author_overrides_global_author(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        out = StringIO()
        with redirect_stdout(out):
            code = cli.main([
                "--author",
                "codex",
                "add",
                "--project",
                "skewGP",
                "--author",
                "claude",
                "--entry",
                "from subcommand",
            ])

        self.assertEqual(code, 0)
        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("claude |", text)

    def test_search_filters_metadata_tag(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="skewGP", entry_type="note", tags="exp3", entry="match")
        cli.add_entry(project="skewGP", entry_type="note", tags="other", entry="exp3 only in entry")

        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["search", "--tag", "exp3"])

        self.assertEqual(code, 0)
        self.assertIn("match", out.getvalue())
        self.assertNotIn("exp3 only in entry", out.getvalue())

    def test_search_tag_ignores_case_and_separator(self):
        root = Path(self.tmp.name)
        (root / "2026-06-24.txt").write_text(
            "---\n[10:00] andrew | one-20260624-luna-001 | one | note | nca_style\nunderscore\n\n"
            "---\n[10:01] andrew | one-20260624-luna-002 | one | note | NCA-Style\nmixed case\n\n"
            "---\n[10:02] andrew | one-20260624-luna-003 | one | note | nca\nother tag\n",
            encoding="utf-8",
        )

        code, out, _ = self.run_cli("search", "--tag", "nca-style")

        self.assertEqual(code, 0)
        self.assertIn("underscore", out)
        self.assertIn("mixed case", out)
        self.assertNotIn("other tag", out)

    def test_add_reuses_existing_tag_spelling_and_drops_repeats(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="one", entry_type="note", tags="nca-style", entry="first")
        cli.add_entry(project="one", entry_type="note", tags="nca-style", entry="second")

        cli.add_entry(project="one", entry_type="note", tags="NCA_style, nca-style, New_Tag, new-tag", entry="third")

        latest = list(cli.iter_entries(root))[-1]
        self.assertEqual(latest.tags, ("nca-style", "New_Tag"))

    def write_day(self, date, *entries):
        """Write raw entries, given as (id, type, body) or (id, type, extra_line, body), to a day file."""
        blocks = []
        for entry_id, entry_type, *rest in entries:
            extra = f"{rest[0]}\n" if len(rest) == 2 else ""
            blocks.append(f"---\n[10:00] andrew | {entry_id} | one | {entry_type} | exp\n{extra}{rest[-1]}\n")
        (Path(self.tmp.name) / f"{date}.txt").write_text("\n".join(blocks), encoding="utf-8")

    def digest_fixture(self):
        # 2026-W02 has a digest; 2026-W03 and 2026-W04 do not.
        self.write_day("2026-01-05", ("one-20260105-luna-001", "finding", "week two finding"))
        self.write_day(
            "2026-01-12",
            ("one-20260112-luna-001", "result", "week three result"),
            ("one-20260112-luna-002", "digest", "covers: 2026-W02",
             "Digest one 2026-W02: found the thing (one-20260105-luna-001).\nState:\n- stable"),
        )
        self.write_day("2026-01-19", ("one-20260119-luna-001", "todo", "week four todo"))

    def test_add_digest_writes_covers_line_without_git(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        self.write_day("2026-01-05", ("one-20260105-luna-001", "finding", "week two finding"))

        with patch.object(cli, "git_context", return_value="repo main@abc1234"):
            cli.add_entry(project="one", entry_type="digest", tags="", covers="2026-W02",
                          entry="Digest one 2026-W02: found it (one-20260105-luna-001).")

        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("| one | digest | \ncovers: 2026-W02\nDigest one", text)
        self.assertNotIn("git:", text)
        digest = list(cli.iter_entries(root))[-1]
        self.assertEqual(digest.covers, "2026-W02")
        self.assertTrue(digest.body.startswith("Digest one 2026-W02"))

    def test_add_digest_validates_week_and_citations(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        self.write_day("2026-01-05", ("one-20260105-luna-001", "finding", "week two finding"))
        add = lambda **kw: cli.add_entry(project="one", entry_type="digest", tags="", **kw)

        with self.assertRaisesRegex(cli.NotchError, "need --covers"):
            add(entry="Digest")
        with self.assertRaisesRegex(cli.NotchError, "not over yet"):
            add(entry="Digest", covers=cli.format_week(dt.date.today()))
        with self.assertRaisesRegex(cli.NotchError, "unknown entry IDs: one-20260105-luna-009"):
            add(entry="Digest (one-20260105-luna-001, one-20260105-luna-009)", covers="2026-W02")
        with self.assertRaisesRegex(cli.NotchError, "only for --type digest"):
            cli.add_entry(project="one", entry_type="note", tags="", entry="x", covers="2026-W02")

    def test_catchup_prints_digest_recent_entries_and_undigested_weeks(self):
        self.digest_fixture()

        code, out, _ = self.run_cli("catchup", "ONE")

        self.assertEqual(code, 0)
        self.assertIn("== Latest digest: 2026-W02 ==", out)
        self.assertIn("State:\n- stable", out)
        self.assertIn("== Entries since 2026-01-12: 2 ==", out)
        self.assertIn("  week three result", out)
        self.assertIn("  week four todo", out)
        self.assertNotIn("week two finding", out)
        self.assertIn("== Undigested weeks: 2026-W03 (1 entry), 2026-W04 (1 entry) ==", out)
        self.assertIn("`notch digest-prep one 2026-W03`", out)
        self.assertIn("== Tags in use:", out)

        code, out, _ = self.run_cli("catchup", "one", "--limit", "1")
        self.assertIn("(1 older entry not listed; notch search --project one --since 2026-01-12 --brief)", out)
        self.assertNotIn("week three result", out)

    def test_digest_prep_requires_week_order_and_prints_material(self):
        self.digest_fixture()

        code, _, err = self.run_cli("digest-prep", "one", "2026-W04")
        self.assertEqual(code, 2)
        self.assertIn("digest earlier weeks first (2026-W03)", err)

        code, out, _ = self.run_cli("digest-prep", "one", "2026-W03")
        self.assertEqual(code, 0)
        self.assertIn("DIGEST TASK: one 2026-W03 (2026-01-12 to 2026-01-18), 1 entry", out)
        self.assertIn("Digest one 2026-W03: <the week's bottom line", out)
        self.assertIn("notch add --project one --type digest --covers 2026-W03", out)
        self.assertIn("== Previous digest ==\n---\n[10:00] andrew | one-20260112-luna-002", out)
        self.assertIn("week three result", out)
        self.assertNotIn("week four todo", out)

    def test_add_reads_entry_from_stdin_and_reports_digest_length(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        self.write_day("2026-01-05", ("one-20260105-luna-001", "finding", "week two finding"))
        text = "Digest one 2026-W02: it's \"quoted\" (one-20260105-luna-001).\n"

        with patch("sys.stdin", StringIO(text)):
            code, out, err = self.run_cli("add", "-p", "one", "-t", "digest", "--covers", "2026-W02", "--entry", "-")

        self.assertEqual(code, 0)
        self.assertEqual(list(cli.iter_entries(root))[-1].body, text.strip())
        self.assertIn(f"digest is {len(text.strip()) - len('one-20260105-luna-001')} characters without cited IDs", err)

    def test_digest_prep_warns_about_mixed_project_spellings(self):
        self.write_day(
            "2026-01-05",
            ("one-20260105-luna-001", "finding", "lower"),
        )
        (Path(self.tmp.name) / "2026-01-06.txt").write_text(
            "---\n[10:00] andrew | One-20260106-luna-001 | One | note | \nupper\n", encoding="utf-8"
        )

        code, out, _ = self.run_cli("digest-prep", "one", "2026-W02")

        self.assertEqual(code, 0)
        self.assertIn("more than one project spelling (One, one)", out)

    def test_info_shows_notebook_location_and_update_reminder(self):
        out = StringIO()

        with redirect_stdout(out):
            code = cli.main(["info"])

        self.assertEqual(code, 0)
        self.assertIn(f"Notebook dir: {self.tmp.name}", out.getvalue())
        self.assertIn("uv tool install --force --reinstall-package notch .", out.getvalue())

    def test_update_check_reports_no_update(self):
        out = StringIO()

        with patch("notch.cli.local_update_check", return_value=None), redirect_stdout(out):
            code = cli.main(["update-check"])

        self.assertEqual(code, 0)
        self.assertIn("No local source update found.", out.getvalue())

    def test_local_update_check_uses_cwd_source_checkout(self):
        source = Path(self.tmp.name)
        (source / "src" / "notch").mkdir(parents=True)
        (source / "pyproject.toml").write_text("[project]\nname = \"notch\"\n", encoding="utf-8")
        (source / "src" / "notch" / "cli.py").write_text("# newer\n", encoding="utf-8")

        with patch("notch.cli.Path.cwd", return_value=source):
            with patch("notch.cli.notch_distribution", return_value=object()):
                with patch("notch.cli.local_source_dir", return_value=Path("/installed/notch")):
                    with patch("notch.cli.installed_at_time", return_value=1.0):
                        with patch("notch.cli.source_updated_time", return_value=2.0):
                            update = cli.local_update_check()

        self.assertIsNotNone(update)
        self.assertEqual(update.source_dir, source)

    def test_tui_requires_tty(self):
        err = StringIO()

        with patch("sys.stdin.isatty", return_value=False), redirect_stderr(err):
            code = cli.main(["tui"])

        self.assertEqual(code, 2)
        self.assertIn("tui requires an interactive terminal", err.getvalue())

    def test_default_command_opens_tui(self):
        with patch("notch.cli.maybe_prompt_for_update", return_value=False), patch(
            "notch.cli.interactive_terminal", return_value=True
        ):
            with patch("notch.cli.cmd_tui", return_value=17) as cmd_tui:
                code = cli.main([])

        self.assertEqual(code, 17)
        cmd_tui.assert_called_once()

    def test_default_command_without_terminal_prints_help(self):
        with patch("notch.cli.interactive_terminal", return_value=False), patch("notch.cli.cmd_tui") as cmd_tui:
            code, out, _ = self.run_cli()

        self.assertEqual(code, 0)
        self.assertIn("catchup", out)
        cmd_tui.assert_not_called()

    def test_add_without_terminal_reads_stdin_instead_of_editor(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        with patch.dict(os.environ, {"EDITOR": "false"}, clear=False), patch("sys.stdin", StringIO("")):
            code, _, err = self.run_cli("add", "-p", "one")
        self.assertEqual(code, 2)
        self.assertIn("no entry text", err)

        with patch("sys.stdin", StringIO("piped text\n")):
            code, _, _ = self.run_cli("add", "-p", "one")
        self.assertEqual(code, 0)
        self.assertEqual(next(cli.iter_entries(root)).body, "piped text")

    def test_add_notes_new_project(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="alpha", entry_type="note", tags="", entry="first")

        _, _, err = self.run_cli("add", "-p", "alhpa", "--entry", "typo")
        self.assertIn("started new project 'alhpa' (recent: alpha)", err)
        _, _, err = self.run_cli("add", "-p", "ALPHA", "--entry", "same project")
        self.assertNotIn("started new project", err)

    def test_default_author_is_claude_inside_claude_code_without_terminal(self):
        env = {k: v for k, v in os.environ.items() if k != "NOTCH_AUTHOR"}
        with patch.dict(os.environ, {**env, "CLAUDECODE": "1"}, clear=True), patch("sys.stdin", StringIO("")):
            self.assertEqual(cli.default_author(), "claude")
        with patch.dict(os.environ, {**env, "CLAUDECODE": "1", "NOTCH_AUTHOR": "codex"}, clear=True):
            self.assertEqual(cli.default_author(), "codex")

    def test_search_caps_full_output_but_not_brief(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        for idx in range(cli.SEARCH_FULL_LIMIT + 5):
            cli.add_entry(project="one", entry_type="note", tags="", entry=f"entry {idx:02d}")

        _, out, _ = self.run_cli("search", "-p", "one")
        self.assertTrue(out.startswith(f"(showing the {cli.SEARCH_FULL_LIMIT} most recent of 25 matches;"))
        self.assertNotIn("entry 04\n", out)
        self.assertIn("entry 24\n", out)

        _, out, _ = self.run_cli("search", "-p", "one", "--brief")
        self.assertEqual(out.count("\n  entry "), 25)

    def test_tui_save_helper_writes_existing_project_entry(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        entry_id = save_tui_entry(
            project="skewGP",
            entry_type="finding",
            tags="exp3",
            entry="from textual",
            author="codex",
        )

        self.assertIn("skewGP-", entry_id)
        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("| skewGP | finding | exp3", text)
        self.assertIn("from textual", text)

    def test_tui_save_helper_trims_project_value(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        save_tui_entry(
            project=" new-project ",
            entry_type="note",
            tags="",
            entry="new project entry",
            author="codex",
        )

        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("| new-project | note |", text)

    def test_initial_tui_state_uses_recent_project_default(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="alpha", entry_type="note", tags="", entry="first")
        cli.add_entry(project="beta", entry_type="note", tags="", entry="second")

        state = cli.initial_tui_state(root)

        self.assertEqual(state.project_options[:3], ["New project", "beta", "alpha"])
        self.assertEqual(state.project_options[state.project_index], "beta")

    def test_textual_tags_space_adds_comma_separator(self):
        self.assertEqual(append_tag_separator("user"), "user, ")
        self.assertEqual(append_tag_separator("user,"), "user, ")
        self.assertEqual(append_tag_separator("user, "), "user, ")
        self.assertEqual(append_tag_separator(""), "")

    def test_textual_entry_up_down_stay_in_entry_editor(self):
        app = build_textual_tui(root=Path(self.tmp.name), author="codex")
        app.active = 3
        app.entry = "one\ntwo\nthree"
        app.entry_cursor = len("one\ntwo")

        self.assertTrue(app.handle_key("up", None))
        self.assertEqual(app.active, 3)
        self.assertEqual(app.entry_cursor_line(), 0)

        self.assertTrue(app.handle_key("down", None))
        self.assertEqual(app.active, 3)
        self.assertEqual(app.entry_cursor_line(), 1)

    def test_textual_entry_view_keeps_bottom_cursor_line_visible(self):
        app = build_textual_tui(root=Path(self.tmp.name), author="codex")
        app.active = 3
        app.entry = "\n".join(str(idx) for idx in range(9))
        app.entry_cursor = len(app.entry)

        rendered = app.entry_text().plain

        self.assertNotIn("arrows move cursor", rendered)
        self.assertNotIn("> 0", rendered)
        self.assertIn("> 7", rendered)
        self.assertIn("> 8", rendered)

    def test_textual_entry_view_scrolls_for_wrapped_current_line(self):
        app = build_textual_tui(root=Path(self.tmp.name), author="codex")
        app.active = 3
        app.entry_wrap_width = lambda: 10
        app.entry = "x" * 85
        app.entry_cursor = len(app.entry)

        rendered = app.entry_text().plain

        self.assertEqual(app.entry_scroll, 3)
        self.assertEqual(rendered.count("\n"), 6)
        self.assertIn("> xxxxx", rendered)

    def test_textual_entry_view_shows_final_newline_cursor_row(self):
        app = build_textual_tui(root=Path(self.tmp.name), author="codex")
        app.active = 3
        app.entry_wrap_width = lambda: 10
        app.entry = "\n".join(str(idx) for idx in range(7))
        app.entry_cursor = len(app.entry)

        app.insert_entry("\n")
        rendered = app.entry_text().plain

        self.assertEqual(app.entry_scroll, 2)
        self.assertNotIn("> 0", rendered)
        self.assertIn("> 6", rendered)
        self.assertTrue(rendered.endswith(">  "))

    def test_display_entry_lines_tracks_wrapped_source_offsets(self):
        lines = display_entry_lines("abcdef\ng", 3)

        self.assertEqual([(line.text, line.start, line.end) for line in lines], [
            ("abc", 0, 3),
            ("def", 3, 6),
            ("g", 7, 8),
        ])

    def test_display_entry_lines_wraps_on_words_when_possible(self):
        lines = display_entry_lines("alpha beta gamma", 10)

        self.assertEqual([line.text for line in lines], ["alpha beta", "gamma"])
        self.assertEqual([(line.start, line.end) for line in lines], [(0, 11), (11, 16)])

    def test_display_entry_lines_hard_wraps_long_words(self):
        lines = display_entry_lines("alphabetagamma", 5)

        self.assertEqual([line.text for line in lines], ["alpha", "betag", "amma"])

    def test_update_check_prompts_and_runs_update(self):
        source = Path(self.tmp.name)
        update = cli.UpdateCheck(source_dir=source, installed_at=1.0, source_updated_at=2.0)
        out = StringIO()

        with patch("notch.cli.local_update_check", return_value=update):
            with patch("notch.cli.prompt_yes_no", return_value=True):
                with patch("notch.cli.run_local_update", return_value=0) as run_update:
                    with redirect_stdout(out):
                        code = cli.main(["update-check"])

        self.assertEqual(code, 0)
        run_update.assert_called_once_with(source)

    def test_default_update_prompt_exits_after_update(self):
        source = Path(self.tmp.name)
        update = cli.UpdateCheck(source_dir=source, installed_at=1.0, source_updated_at=2.0)
        out = StringIO()

        with patch("notch.cli.local_update_check", return_value=update), redirect_stdout(out):
            with patch("notch.cli.interactive_terminal", return_value=True):
                with patch("notch.cli.prompt_yes_no", return_value=True):
                    with patch("notch.cli.run_local_update", return_value=0) as run_update:
                        code = cli.main([])

        self.assertEqual(code, 0)
        run_update.assert_called_once_with(source)

    def test_default_update_decline_continues_to_tui(self):
        source = Path(self.tmp.name)
        update = cli.UpdateCheck(source_dir=source, installed_at=1.0, source_updated_at=2.0)
        out = StringIO()

        with patch("notch.cli.local_update_check", return_value=update), redirect_stdout(out):
            with patch("notch.cli.interactive_terminal", return_value=True):
                with patch("notch.cli.prompt_yes_no", return_value=False):
                    with patch("notch.cli.cmd_tui", return_value=42):
                        code = cli.main([])

        self.assertEqual(code, 42)

    def test_recent_projects_uses_last_entry_order(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="alpha", entry_type="note", tags="", entry="first")
        cli.add_entry(project="beta", entry_type="note", tags="", entry="second")
        cli.add_entry(project="alpha", entry_type="note", tags="", entry="third")

        self.assertEqual([project for project, _ in cli.recent_projects(root)], ["alpha", "beta"])

    def test_interactive_reprompts_invalid_new_project(self):
        out = StringIO()
        err = StringIO()
        with patch("builtins.input", side_effect=["0", "bad project", "good-project", "", "", ""]), redirect_stdout(out), redirect_stderr(err):
            with patch("notch.cli.read_entry_from_terminal", return_value="entry"):
                code = cli.main(["cli"])

        self.assertEqual(code, 0)
        self.assertIn("project must use only", err.getvalue())

    def test_interactive_validates_tags_before_entry(self):
        out = StringIO()
        err = StringIO()
        with patch("builtins.input", side_effect=["0", "good-project", "", "bad tag"]), redirect_stdout(out), redirect_stderr(err):
            with patch("notch.cli.read_entry_from_terminal") as entry:
                code = cli.main(["cli"])

        self.assertEqual(code, 2)
        self.assertIn("tag must use only", err.getvalue())
        entry.assert_not_called()

    def test_interactive_accepts_global_author(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")

        out = StringIO()
        with patch("builtins.input", side_effect=["0", "skewGP", "", "agent", ""]), redirect_stdout(out):
            with patch("notch.cli.read_entry_from_terminal", return_value="agent note"):
                code = cli.main(["--author", "codex", "cli"])

        self.assertEqual(code, 0)
        self.assertIn("skewGP-", out.getvalue())
        self.assertIn("[", (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8"))
        self.assertIn("codex |", (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8"))

    def test_interactive_selects_existing_project_and_type(self):
        root = Path(self.tmp.name)
        (root / ".machine-id").write_text("luna\n", encoding="utf-8")
        cli.add_entry(project="skewGP", entry_type="note", tags="", entry="existing")

        out = StringIO()
        with patch("builtins.input", side_effect=["1", "2", "exp3", ""]), redirect_stdout(out):
            with patch("notch.cli.read_entry_from_terminal", return_value="found"):
                code = cli.main(["cli"])

        self.assertEqual(code, 0)
        text = (root / f"{dt.date.today().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("| skewGP | finding | exp3", text)

    def test_read_entry_from_terminal_ends_on_dot_line(self):
        out = StringIO()
        with patch("builtins.input", side_effect=["first", "", "second", "."]), redirect_stdout(out):
            entry = cli.read_entry_from_terminal()

        self.assertEqual(entry, "first\n\nsecond")

    def test_interactive_can_cancel_at_preview(self):
        out = StringIO()
        with patch("builtins.input", side_effect=["0", "skewGP", "", "agent", "n"]), redirect_stdout(out):
            with patch("notch.cli.read_entry_from_terminal", return_value="agent note"):
                code = cli.main(["cli"])

        self.assertEqual(code, 1)
        self.assertEqual(list(Path(self.tmp.name).glob("*.txt")), [])

    def test_escape_cancels_key_prompt(self):
        out = StringIO()
        with patch("notch.cli.interactive_terminal", return_value=True), redirect_stdout(out):
            with patch("notch.cli.read_char", return_value="\x1b"):
                with self.assertRaises(cli.Cancelled):
                    cli.prompt_key("Choice", valid_keys=["1"])

    def test_ctrl_c_cancels_key_prompt(self):
        out = StringIO()
        with patch("notch.cli.interactive_terminal", return_value=True), redirect_stdout(out):
            with patch("notch.cli.read_char", return_value="\x03"):
                with self.assertRaises(KeyboardInterrupt):
                    cli.prompt_key("Choice", valid_keys=["1"])

    def test_escape_cancels_line_prompt(self):
        out = StringIO()
        with patch("notch.cli.interactive_terminal", return_value=True), redirect_stdout(out):
            with patch("notch.cli.read_char", return_value="\x1b"):
                with self.assertRaises(cli.Cancelled):
                    cli.prompt("Tags")

    def test_eof_cancels_cleanly(self):
        out = StringIO()
        with patch("builtins.input", side_effect=EOFError), redirect_stdout(out):
            code = cli.main(["cli"])

        self.assertEqual(code, 130)
        self.assertIn("cancelled", out.getvalue())


if __name__ == "__main__":
    unittest.main()
