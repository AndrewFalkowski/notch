from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cli import NotchError, add_entry, initial_tui_state, validate_entry_metadata


TYPE_OPTIONS = ["note", "finding", "result", "todo", "question", "status"]
ENTRY_VISIBLE_LINES = 6
ENTRY_FALLBACK_WRAP_WIDTH = 72


@dataclass(frozen=True)
class EntryDisplayLine:
    text: str
    start: int
    end: int


def save_tui_entry(
    *,
    project: str,
    entry_type: str,
    tags: str,
    entry: str,
    author: str | None = None,
) -> str:
    project = project.strip()
    validate_entry_metadata(project=project, entry_type=entry_type, tags=tags, author=author)
    return add_entry(project=project, entry_type=entry_type, tags=tags, entry=entry, author=author)


def run_textual_tui(*, root: Path, author: str | None) -> str | None:
    return build_textual_tui(root=root, author=author).run()


def build_textual_tui(*, root: Path, author: str | None):
    try:
        from rich.text import Text
        from textual.app import App, ComposeResult
        from textual.containers import Vertical
        from textual.events import Key
        from textual.widgets import Static
    except ImportError as exc:  # pragma: no cover - exercised only without declared dependency installed.
        raise NotchError("tui requires Textual; reinstall Notch from this source directory") from exc

    state = initial_tui_state(root)
    project_options = state.project_options[:10]

    class NotchTui(App[str | None]):
        CSS = """
        Screen {
            background: #100806;
            color: #fff3dd;
        }

        #wrap {
            height: 1fr;
            padding: 0 1;
        }

        Static {
            height: auto;
        }

        .panel {
            color: #f1b36a;
        }

        .active {
            color: #fff8ea;
            text-style: bold;
        }

        #entry {
            height: 9;
            margin-top: 0;
            margin-bottom: 0;
            border: solid #9d6a3d;
            padding: 0 1;
            color: #fff3dd;
            background: #1b0f09;
        }

        #entry.active {
            border: solid #ffbd66;
        }

        #status {
            height: 1;
            color: #ffd083;
        }

        #save {
            height: 1;
            color: #ffb64d;
        }
        """

        BINDINGS = [
            ("f2", "save", "save"),
            ("ctrl+s", "save", "save"),
            ("ctrl+x", "save", "save"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.project_index = state.project_index
            self.type_index = state.type_index
            self.new_project = state.new_project
            self.tags = state.tags
            self.entry = state.entry
            self.entry_cursor = len(self.entry)
            self.entry_scroll = 0
            self.status = ""

        def compose(self) -> ComposeResult:
            with Vertical(id="wrap"):
                yield Static(id="title")
                yield Static(id="project", classes="panel")
                yield Static(id="type", classes="panel")
                yield Static(id="tags", classes="panel")
                yield Static(id="entry", classes="panel")
                yield Static(id="save", classes="panel")
                yield Static(id="status")

        def on_mount(self) -> None:
            self.title = "Notch"
            self.sub_title = ""
            self.refresh_screen()

        def on_key(self, event: Key) -> None:
            key = event.key
            if key in {"f2", "ctrl+s", "ctrl+x"}:
                event.stop()
                self.action_save()
                return
            if key == "escape":
                event.stop()
                event.prevent_default()
                if self.active == 3:
                    self.active = 2
                    self.status = "left entry"
                    self.refresh_screen()
                else:
                    self.action_cancel()
                return

            handled = self.handle_key(key, event.character)
            if handled:
                event.stop()
                event.prevent_default()
                self.refresh_screen()

        def handle_key(self, key: str, character: str | None) -> bool:
            if key == "tab":
                self.move_active(1)
                return True
            if key == "shift+tab":
                self.move_active(-1)
                return True
            if self.active == 4:
                if key == "enter" or character == "s":
                    self.action_save()
                    return True
                return False
            if key == "enter" and self.active != 3:
                self.move_active(1)
                return True
            if self.active == 0:
                return self.handle_project_key(key, character)
            if self.active == 1:
                return self.handle_type_key(key)
            if self.active == 2:
                return self.handle_tags_key(key, character)
            if self.active == 3:
                return self.handle_entry_key(key, character)
            if key == "down":
                self.move_active(1)
                return True
            if key == "up":
                self.move_active(-1)
                return True
            return False

        def handle_project_key(self, key: str, character: str | None) -> bool:
            if key == "left" or (character == "h" and self.project_index != 0):
                self.project_index = max(0, self.project_index - 1)
                return True
            if key == "right" or (character == "l" and self.project_index != 0):
                self.project_index = min(len(project_options) - 1, self.project_index + 1)
                return True
            if key in {"backspace", "ctrl+h"}:
                if self.project_index == 0:
                    self.new_project = self.new_project[:-1]
                return True
            if key == "ctrl+u":
                self.project_index = 0
                self.new_project = ""
                return True
            if character and character.isdigit():
                selected = int(character)
                if selected < len(project_options):
                    self.project_index = selected
                return True
            if character and character.isprintable():
                self.project_index = 0
                self.new_project += character
                return True
            return False

        def handle_type_key(self, key: str) -> bool:
            if len(key) == 1 and key.isdigit():
                selected = int(key) - 1
                if 0 <= selected < len(TYPE_OPTIONS):
                    self.type_index = selected
                return True
            if key == "left" or key == "h":
                self.type_index = max(0, self.type_index - 1)
                return True
            if key == "right" or key == "l":
                self.type_index = min(len(TYPE_OPTIONS) - 1, self.type_index + 1)
                return True
            return False

        def handle_tags_key(self, key: str, character: str | None) -> bool:
            if key in {"backspace", "ctrl+h"}:
                self.tags = self.tags[:-1]
                return True
            if key == "ctrl+u":
                self.tags = ""
                return True
            if key == "enter":
                self.move_active(1)
                return True
            if character == " ":
                self.tags = append_tag_separator(self.tags)
                return True
            if character and character.isprintable():
                self.tags += character
                return True
            return False

        def handle_entry_key(self, key: str, character: str | None) -> bool:
            if key == "enter":
                self.move_active(1)
                return True
            if key in {"ctrl+j", "ctrl+m"}:
                self.insert_entry("\n")
                return True
            if key in {"backspace", "ctrl+h"}:
                self.delete_entry()
                return True
            if key == "ctrl+u":
                self.entry = ""
                self.entry_cursor = 0
                self.entry_scroll = 0
                return True
            if key == "left":
                self.move_entry_cursor(-1)
                return True
            if key == "right":
                self.move_entry_cursor(1)
                return True
            if key == "up":
                self.move_entry_vertical(-1)
                return True
            if key == "down":
                self.move_entry_vertical(1)
                return True
            if key == "home":
                self.move_entry_home()
                return True
            if key == "end":
                self.move_entry_end()
                return True
            if character and character.isprintable():
                self.insert_entry(character)
                return True
            return False

        def move_active(self, delta: int) -> None:
            self.active = (self.active + delta) % 5
            self.refresh_screen()

        def action_cancel(self) -> None:
            self.exit(None)

        def action_save(self) -> None:
            try:
                entry_id = save_tui_entry(
                    project=self.current_project(),
                    entry_type=TYPE_OPTIONS[self.type_index],
                    tags=self.tags,
                    entry=self.entry,
                    author=author,
                )
            except NotchError as exc:
                self.status = str(exc)
                self.refresh_screen()
                return
            self.exit(entry_id)

        def current_project(self) -> str:
            if self.project_index == 0:
                return self.new_project
            return project_options[self.project_index]

        def refresh_screen(self) -> None:
            self.query_one("#title", Static).update(self.title_text())
            self.query_one("#project", Static).update(self.project_text())
            self.query_one("#type", Static).update(self.type_text())
            self.query_one("#tags", Static).update(self.tags_text())
            self.query_one("#entry", Static).update(self.entry_text())
            self.query_one("#save", Static).update(self.save_text())
            self.query_one("#status", Static).update(self.status)
            for idx, widget_id in enumerate(["#project", "#type", "#tags", "#entry", "#save"]):
                widget = self.query_one(widget_id, Static)
                widget.set_class(self.active == idx, "active")

        def title_text(self) -> Text:
            title = Text("NOTCH", style="bold #ffb64d")
            title.append("  arrows move/select  nums choose  enter next  ctrl-j newline  s save  esc back/quit", style="#caa47e")
            return title

        def line_text(self, label: str) -> Text:
            if label == "SAVE":
                active = self.active == 4
            else:
                active = self.section_label(label)
            text = Text(f"{label:<7}", style="bold #ffd083" if active else "#f1b36a")
            return text

        def section_label(self, label: str) -> bool:
            return ["PROJECT", "TYPE", "TAGS", "ENTRY"].index(label) == self.active

        def project_text(self) -> Text:
            text = self.line_text("PROJECT")
            for idx, project in enumerate(project_options):
                if idx == 0:
                    self.append_new_project_choice(text, selected=idx == self.project_index)
                    continue
                self.append_choice(text, f"{idx}:{project}", selected=idx == self.project_index, active=self.active == 0)
            return text

        def type_text(self) -> Text:
            text = self.line_text("TYPE")
            for idx, entry_type in enumerate(TYPE_OPTIONS):
                label = f"{idx + 1}:{entry_type}"
                self.append_choice(text, label, selected=idx == self.type_index, active=self.active == 1)
            return text

        def tags_text(self) -> Text:
            text = self.line_text("TAGS")
            text.append(" ")
            text.append(self.tags or "_", style="#fff3dd")
            if self.active == 2:
                self.append_cursor(text)
            return text

        def entry_text(self) -> Text:
            text = self.line_text("ENTRY")
            text.append("\n")
            visible = self.entry_display_lines()
            start, end = self.entry_window(visible)
            cursor_line = self.entry_cursor_line(visible)
            cursor_col = self.entry_cursor_col(visible)
            for idx in range(start, end):
                if idx > start:
                    text.append("\n")
                line = visible[idx].text
                line_style = "#fff3dd"
                if self.active == 3 and idx == cursor_line:
                    rendered = line or ""
                    left = rendered[:cursor_col]
                    cursor = rendered[cursor_col:cursor_col + 1] or " "
                    right = rendered[cursor_col + 1 :]
                    text.append("> ", style=line_style)
                    if left:
                        text.append(left, style=line_style)
                    text.append(cursor, style="#100806 on #ffd083")
                    if right:
                        text.append(right, style=line_style)
                else:
                    text.append(f"> {line or ' '}", style=line_style)
            return text

        def save_text(self) -> Text:
            text = self.line_text("SAVE")
            style = "#100806 on #ffb64d bold" if self.active == 4 else "#ffb64d"
            text.append(" s:save  ctrl-s:save  ctrl-x:save  enter:save", style=style)
            return text

        def append_new_project_choice(self, text: Text, *, selected: bool) -> None:
            text.append(" ")
            style = self.choice_style(selected=selected, active=self.active == 0)
            text.append(" 0:new=", style=style)
            text.append(self.new_project or "_", style=style)
            if selected and self.active == 0:
                self.append_cursor(text)
            text.append(" ", style=style)

        def append_choice(self, text: Text, label: str, *, selected: bool, active: bool) -> None:
            text.append(" ")
            style = self.choice_style(selected=selected, active=active)
            text.append(f" {label} ", style=style)

        def choice_style(self, *, selected: bool, active: bool) -> str:
            if selected and active:
                return "#100806 on #ffb64d bold"
            if selected:
                return "#100806 on #d8944f"
            return "#f1b36a"

        def append_cursor(self, text: Text) -> None:
            text.append(" ", style="#100806 on #ffd083")

        def entry_display_lines(self) -> list[EntryDisplayLine]:
            return display_entry_lines(self.entry, self.entry_wrap_width())

        def entry_wrap_width(self) -> int:
            try:
                width = self.query_one("#entry", Static).content_size.width
            except Exception:
                width = ENTRY_FALLBACK_WRAP_WIDTH + 2
            return max(1, width - 2)

        def entry_window(self, lines: list[EntryDisplayLine]) -> tuple[int, int]:
            height = ENTRY_VISIBLE_LINES
            cursor_line = self.entry_cursor_line(lines)
            if cursor_line < self.entry_scroll:
                self.entry_scroll = cursor_line
            elif cursor_line >= self.entry_scroll + height:
                self.entry_scroll = cursor_line - height + 1
            self.entry_scroll = max(0, min(self.entry_scroll, max(0, len(lines) - height)))
            return self.entry_scroll, min(len(lines), self.entry_scroll + height)

        def entry_cursor_line(self, lines: list[EntryDisplayLine] | None = None) -> int:
            lines = lines if lines is not None else self.entry_display_lines()
            cursor = min(self.entry_cursor, len(self.entry))
            for idx, line in enumerate(lines):
                if line.start <= cursor <= line.end:
                    return idx
            return max(0, len(lines) - 1)

        def entry_cursor_col(self, lines: list[EntryDisplayLine] | None = None) -> int:
            lines = lines if lines is not None else self.entry_display_lines()
            cursor = min(self.entry_cursor, len(self.entry))
            for line in lines:
                if line.start <= cursor <= line.end:
                    return min(cursor - line.start, len(line.text))
            return len(lines[-1].text) if lines else 0

        def move_entry_cursor(self, delta: int) -> None:
            self.entry_cursor = max(0, min(len(self.entry), self.entry_cursor + delta))
            self.keep_entry_cursor_visible()

        def move_entry_vertical(self, delta: int) -> None:
            lines = self.entry_display_lines()
            current_line = self.entry_cursor_line(lines)
            current_col = self.entry_cursor_col(lines)
            target_line = max(0, min(len(lines) - 1, current_line + delta))
            self.entry_cursor = min(lines[target_line].start + current_col, lines[target_line].end)
            self.keep_entry_cursor_visible()

        def move_entry_home(self) -> None:
            lines = self.entry_display_lines()
            self.entry_cursor = lines[self.entry_cursor_line(lines)].start
            self.keep_entry_cursor_visible()

        def move_entry_end(self) -> None:
            lines = self.entry_display_lines()
            line = self.entry_cursor_line(lines)
            self.entry_cursor = lines[line].end
            self.keep_entry_cursor_visible()

        def insert_entry(self, chunk: str) -> None:
            self.entry = self.entry[: self.entry_cursor] + chunk + self.entry[self.entry_cursor :]
            self.entry_cursor += len(chunk)
            self.keep_entry_cursor_visible()

        def delete_entry(self) -> None:
            if self.entry_cursor == 0:
                return
            self.entry = self.entry[: self.entry_cursor - 1] + self.entry[self.entry_cursor :]
            self.entry_cursor -= 1
            self.keep_entry_cursor_visible()

        def keep_entry_cursor_visible(self) -> None:
            lines = self.entry_display_lines()
            line = self.entry_cursor_line(lines)
            height = ENTRY_VISIBLE_LINES
            if line < self.entry_scroll:
                self.entry_scroll = line
            elif line >= self.entry_scroll + height:
                self.entry_scroll = line - height + 1
            self.entry_scroll = max(0, min(self.entry_scroll, max(0, len(lines) - height)))

    return NotchTui()


def append_tag_separator(tags: str) -> str:
    if not tags:
        return tags
    if tags.endswith(", "):
        return tags
    if tags.endswith(","):
        return f"{tags} "
    return f"{tags}, "


def display_entry_lines(entry: str, width: int) -> list[EntryDisplayLine]:
    wrap_width = max(1, width)
    lines: list[EntryDisplayLine] = []
    index = 0
    for raw in entry.split("\n"):
        if not raw:
            lines.append(EntryDisplayLine("", index, index))
            index += 1
            continue
        offset = 0
        while offset < len(raw):
            if len(raw) - offset <= wrap_width:
                chunk = raw[offset:]
                start = index + offset
                lines.append(EntryDisplayLine(chunk, start, start + len(chunk)))
                break

            limit = offset + wrap_width
            chunk_end = limit
            next_offset = limit
            if raw[limit].isspace():
                next_offset = limit + 1
            else:
                break_at = next((pos for pos in range(limit - 1, offset, -1) if raw[pos].isspace()), None)
                if break_at is not None:
                    chunk_end = break_at
                    next_offset = break_at + 1

            while next_offset < len(raw) and raw[next_offset].isspace():
                next_offset += 1
            chunk = raw[offset:chunk_end].rstrip()
            start = index + offset
            lines.append(EntryDisplayLine(chunk, start, index + next_offset))
            offset = next_offset
        index += len(raw) + 1
    return lines or [EntryDisplayLine("", 0, 0)]
