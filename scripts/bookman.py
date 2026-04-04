#!.venv/bin/python
"""
bookman.py -- Book Manifest Manager

A Textual TUI for ordering book chapters and exporting via pandoc.
Manages a .bookmanifest JSON dotfile for ordering, roles, and metadata.

Usage:
    python bookman.py [directory]   # defaults to current directory
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Label,
    Input,
    ListItem,
    ListView,
    Markdown,
    Static,
)

# Role config: role -> (badge, rich-color, display-label)
ROLES: dict[str, tuple[str, str, str]] = {
    "chapter":      ("CH", "white",   "Chapter"),
    "introduction": ("IN", "cyan",    "Introduction"),
    "prologue":     ("PR", "#b39ddb", "Prologue"),
    "epilogue":     ("EP", "#ffca28", "Epilogue"),
    "appendix":     ("AP", "#78909c", "Appendix"),
    "excluded":     ("--", "#666666", "Excluded"),
}

MANIFEST_FILE = ".bookmanifest"
MANIFEST_VERSION = 1
OUTPUT_DIR = "out"
SUPPORTED_EXTENSIONS = {".md", ".txt"}


# ---- Data Model ---------------------------------------------------------------

@dataclass
class FileEntry:
    path: str
    role: str = "chapter"
    enabled: bool = True
    note: str = ""

    def exists_at(self, base: Path) -> bool:
        return (base / self.path).exists()


@dataclass
class Manifest:
    title: str = "Untitled Book"
    version: int = MANIFEST_VERSION
    files: list[FileEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "version": self.version,
            "files": [asdict(f) for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        files = [FileEntry(**f) for f in data.get("files", [])]
        return cls(
            title=data.get("title", "Untitled Book"),
            version=data.get("version", MANIFEST_VERSION),
            files=files,
        )


def load_manifest(base: Path) -> tuple[Manifest, str | None]:
    """
    Load .bookmanifest from base directory.
    Returns (manifest, error_or_None).
    On JSON error: backs up corrupt file and rebuilds from directory scan.
    On missing file: creates fresh manifest from directory scan.
    """
    path = base / MANIFEST_FILE
    if not path.exists():
        m = _scan_to_manifest(base)
        _normalize_appendices(m)
        return m, None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        m = Manifest.from_dict(data)
        _normalize_appendices(m)
        return m, None
    except json.JSONDecodeError as exc:
        backup = path.with_suffix(".bak")
        shutil.copy(path, backup)
        m = _scan_to_manifest(base)
        _normalize_appendices(m)
        return m, (
            f"Corrupt manifest (JSON error at line {exc.lineno}). "
            f"Backed up to {backup.name} -- rebuilt from directory."
        )
    except Exception as exc:
        m = _scan_to_manifest(base)
        _normalize_appendices(m)
        return m, f"Could not read manifest: {exc}"


def _scan_to_manifest(base: Path) -> Manifest:
    """Scan directory for supported files and build a Manifest with guessed roles."""
    entries = [
        FileEntry(path=f.name, role=_guess_role(f.name))
        for f in sorted(base.iterdir(), key=lambda p: p.name.lower())
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and not f.name.startswith(".")
    ]
    title = base.name.replace("-", " ").replace("_", " ").title()
    return Manifest(title=title, files=entries)


def _guess_role(name: str) -> str:
    """Heuristically guess a file role from its filename."""
    lower = name.lower()
    if "prologue" in lower:
        return "prologue"
    if "epilogue" in lower:
        return "epilogue"
    if "intro" in lower:
        return "introduction"
    if any(kw in lower for kw in ("appendix", "biography", "artist", "index")):
        return "appendix"
    return "chapter"


def save_manifest(base: Path, manifest: Manifest) -> str | None:
    """Save manifest to .bookmanifest. Returns error string or None on success."""
    try:
        (base / MANIFEST_FILE).write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return None
    except Exception as exc:
        return f"Failed to save: {exc}"


def _normalize_appendices(manifest: "Manifest") -> None:
    """Move all appendix-role files to the end, preserving relative order."""
    non_ap   = [e for e in manifest.files if e.role != "appendix"]
    appendices = [e for e in manifest.files if e.role == "appendix"]
    manifest.files[:] = non_ap + appendices


# ---- Widgets ------------------------------------------------------------------

class BookListView(ListView):
    """ListView with vim-style j/k navigation."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "j":
            self.action_cursor_down()
            event.stop()
        elif event.key == "k":
            self.action_cursor_up()
            event.stop()


class FileRow(ListItem):
    """One row in the file list: index, role badge, filename, status flags."""

    DEFAULT_CSS = """
    FileRow { height: 1; }
    FileRow > Label { width: 100%; padding: 0 1; }
    FileRow.drag-target { background: $primary-darken-2; }
    """

    def __init__(self, entry: FileEntry, index: int, missing: bool = False, chapter_num: int | None = None) -> None:
        super().__init__()
        self.entry = entry
        self.file_index = index
        self.missing = missing
        self.chapter_num = chapter_num

    def compose(self) -> ComposeResult:
        yield Label(self._make_text(), markup=True)

    def _make_text(self) -> str:
        role = self.entry.role if self.entry.role in ROLES else "chapter"
        badge, color, _ = ROLES[role]

        name = self.entry.path
        if self.missing:
            name = f"[red]{name}[/red]"
        elif role == "excluded":
            name = f"[dim]{name}[/dim]"

        flags = ""
        if self.missing:
            flags += " [dim red][MISSING][/dim red]"
        if not self.entry.enabled:
            flags += " [dim]x[/dim]"

        ch = f" [dim]Ch.{self.chapter_num}[/dim]" if self.chapter_num is not None else ""
        return f"[{color}]\\[{badge}][/] {name}{ch}{flags}"

    def refresh_label(self) -> None:
        self.query_one(Label).update(self._make_text())


# ---- Modal Screens ------------------------------------------------------------

class RolePickerScreen(ModalScreen):
    """Choose a role for the selected file."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    RolePickerScreen { align: center middle; }
    #role-box {
        width: 40; height: auto;
        padding: 1 2; border: solid $primary; background: $surface;
    }
    #role-title { margin-bottom: 1; text-style: bold; }
    .role-btn { width: 100%; margin-bottom: 0; }
    """

    def __init__(self, current_role: str) -> None:
        super().__init__()
        self.current_role = current_role

    def compose(self) -> ComposeResult:
        with Container(id="role-box"):
            yield Label("Choose Role", id="role-title")
            for role, (badge, color, label) in ROLES.items():
                active = " <" if role == self.current_role else ""
                yield Button(
                    f"[{color}]\\[{badge}][/]  {label}{active}",
                    id=f"role-{role}",
                    classes="role-btn",
                    variant="primary" if role == self.current_role else "default",
                )

    @on(Button.Pressed)
    def pick(self, event: Button.Pressed) -> None:
        role = event.button.id.removeprefix("role-")
        self.dismiss(role)


class AddFileScreen(ModalScreen):
    """Select untracked files to add to the manifest."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    AddFileScreen { align: center middle; }
    #add-box {
        width: 54; height: auto; max-height: 30;
        padding: 1 2; border: solid $primary; background: $surface;
    }
    #add-title { margin-bottom: 1; text-style: bold; }
    #add-scroll { height: auto; max-height: 16; }
    .file-ck { width: 100%; }
    #add-btns { margin-top: 1; }
    """

    def __init__(self, untracked: list[str]) -> None:
        super().__init__()
        self.untracked = untracked

    @staticmethod
    def _safe(name: str) -> str:
        import re
        return re.sub(r"[^a-zA-Z0-9_-]", "_", name)

    def compose(self) -> ComposeResult:
        with Container(id="add-box"):
            yield Label(f"Add Untracked Files  ({len(self.untracked)} found)", id="add-title")
            with VerticalScroll(id="add-scroll"):
                for f in self.untracked:
                    yield Checkbox(f, id=f"ck-{self._safe(f)}", classes="file-ck")
            with Horizontal(id="add-btns"):
                yield Button("Add Selected", id="add-sel", variant="primary")
                yield Button("Add All", id="add-all")
                yield Button("Cancel", id="add-cancel")

    @on(Button.Pressed, "#add-sel")
    def confirm_selected(self) -> None:
        result = [
            f for f in self.untracked
            if self.query_one(f"#ck-{self._safe(f)}", Checkbox).value
        ]
        self.dismiss(result or None)

    @on(Button.Pressed, "#add-all")
    def confirm_all(self) -> None:
        self.dismiss(self.untracked)

    @on(Button.Pressed, "#add-cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class ExportScreen(ModalScreen):
    """Export the book in selected formats via pandoc."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    DEFAULT_CSS = """
    ExportScreen { align: center middle; }
    #exp-box {
        width: 58; height: auto; max-height: 34;
        padding: 1 2; border: solid $primary; background: $surface;
    }
    #exp-title { text-style: bold; margin-bottom: 1; }
    .fmt-ck { width: 100%; }
    #exp-log {
        height: 8; border: solid $surface-lighten-2;
        padding: 0 1; overflow-y: auto; margin-top: 1;
        background: $surface-darken-1;
    }
    #exp-btns { margin-top: 1; }
    """

    def __init__(self, base: Path, manifest: Manifest) -> None:
        super().__init__()
        self.base = base
        self.manifest = manifest
        self._log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Container(id="exp-box"):
            yield Label("Export Book", id="exp-title")
            yield Checkbox("PDF",                id="fmt-pdf",  value=True, classes="fmt-ck")
            yield Checkbox("HTML (standalone)", id="fmt-html", value=True, classes="fmt-ck")
            yield Checkbox("DOCX (Word)",       id="fmt-docx", value=True, classes="fmt-ck")
            yield Checkbox("Markdown",          id="fmt-md",   value=True, classes="fmt-ck")
            yield Static("", id="exp-log")
            with Horizontal(id="exp-btns"):
                yield Button("Export", id="exp-go", variant="primary")
                yield Button("Close",  id="exp-close")

    def _log(self, line: str) -> None:
        self._log_lines.append(line)
        self.query_one("#exp-log", Static).update("\n".join(self._log_lines))

    @on(Button.Pressed, "#exp-go")
    def do_export(self) -> None:
        self._log_lines.clear()
        fmts = {
            "pdf":  self.query_one("#fmt-pdf",  Checkbox).value,
            "html": self.query_one("#fmt-html", Checkbox).value,
            "docx": self.query_one("#fmt-docx", Checkbox).value,
            "md":   self.query_one("#fmt-md",   Checkbox).value,
        }

        out = self.base / OUTPUT_DIR
        out.mkdir(exist_ok=True)

        enabled = [e for e in self.manifest.files if e.enabled and e.role != "excluded"]
        for e in enabled:
            if not e.exists_at(self.base):
                self._log(f"warning  Skipping missing: {e.path}")

        exportable = [e for e in enabled if e.exists_at(self.base)]
        if not exportable:
            self._log("error  No exportable files found.")
            return

        combined = "\n\n---\n\n".join(
            (self.base / e.path).read_text(encoding="utf-8") for e in exportable
        )
        combined_path = out / "combined.md"
        combined_path.write_text(combined, encoding="utf-8")
        self._log(f"ok  Combined {len(exportable)} files -> combined.md")

        if fmts["md"]:
            dest = out / "book.md"
            shutil.copy(combined_path, dest)
            self._log(f"ok  Markdown -> {dest.name}")

        if not any(fmts[f] for f in ("pdf", "html", "docx")):
            self._log("-- Done --")
            return

        if not shutil.which("pandoc"):
            self._log("error  pandoc not found.")
            self._log("   Install: https://pandoc.org/installing.html")
            return

        for fmt in ("pdf", "html", "docx"):
            if not fmts[fmt]:
                continue
            dest = out / f"book.{fmt}"
            extra = ["--standalone"] if fmt == "html" else []
            cmd = ["pandoc", str(combined_path), "-o", str(dest)] + extra
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    self._log(f"ok  {fmt.upper()} -> {dest.name}")
                else:
                    self._log(f"error  {fmt.upper()}: {r.stderr.strip()[:120]}")
            except subprocess.TimeoutExpired:
                self._log(f"error  {fmt.upper()} timed out (>120s)")
            except Exception as exc:
                self._log(f"error  {fmt.upper()} error: {exc}")

        self._log("-- Export complete --")

    @on(Button.Pressed, "#exp-close")
    def close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen):
    """Keybinding reference overlay."""

    BINDINGS = [Binding("escape,question_mark", "dismiss(None)", "Close")]

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 64; height: auto; max-height: 38;
        padding: 1 2; border: solid $primary; background: $surface;
    }
    #help-close { margin-top: 1; }
    """

    HELP_MD = """\
# Bookman -- Keybindings

| Key | Action |
|-----|--------|
| Up / k | Move cursor up |
| Down / j | Move cursor down |
| Shift+Up / K | Move selected file UP in order |
| Shift+Down / J | Move selected file DOWN in order |
| 0 | Set chapter number for the selected chapter file |
| Mouse drag | Drag files to reorder |
| R | Change role of selected file |
| A | Re-include excluded file, or add untracked file from disk |
| D | Exclude selected file from export |
| X | Remove selected file from manifest entirely |
| S | Save manifest (.bookmanifest) |
| E | Open export dialog |
| ? | Show this help |
| Q | Quit (prompts if unsaved changes) |

## Roles

| Badge | Role | Color | Purpose |
|-------|------|-------|---------|
| [CH] | Chapter | white | Standard content (default) |
| [IN] | Introduction | cyan | Front matter introduction |
| [PR] | Prologue | purple | Opening before main text |
| [EP] | Epilogue | amber | Closing after main text |
| [AP] | Appendix | steel blue | Back matter / supplemental (always last) |
| [--] | Excluded | dim | Present but not exported |
"""

    def compose(self) -> ComposeResult:
        with Container(id="help-box"):
            yield Markdown(self.HELP_MD)
            yield Button("Close", id="help-close", variant="primary")

    @on(Button.Pressed, "#help-close")
    def close(self) -> None:
        self.dismiss(None)


class QuitScreen(ModalScreen):
    """Confirm quit when there are unsaved changes."""

    DEFAULT_CSS = """
    QuitScreen { align: center middle; }
    #quit-box {
        width: 46; height: auto;
        padding: 1 2; border: solid $warning; background: $surface;
    }
    #quit-msg { margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="quit-box"):
            yield Label("You have unsaved changes.", id="quit-msg")
            with Horizontal():
                yield Button("Save & Quit",         id="save-quit",   variant="primary")
                yield Button("Quit Without Saving",  id="just-quit",   variant="warning")
                yield Button("Cancel",               id="cancel-quit")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)



class GotoScreen(ModalScreen):
    """Jump to a specific position by typing its number."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    DEFAULT_CSS = """
    GotoScreen { align: center middle; }
    #goto-box {
        width: 50; height: auto;
        padding: 1 2; border: solid $primary; background: $surface;
    }
    #goto-label { margin-bottom: 1; }
    #goto-input { width: 100%; }
    #goto-btns { margin-top: 1; }
    """

    def __init__(self, total: int, current: int) -> None:
        super().__init__()
        self.total = total
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="goto-box"):
            yield Label(
                f"Set chapter number (1-{self.total}), currently {self.current}:",
                id="goto-label",
            )
            yield Input(
                value=str(self.current),
                placeholder=f"1-{self.total}",
                id="goto-input",
            )
            with Horizontal(id="goto-btns"):
                yield Button("Go", id="goto-go", variant="primary")
                yield Button("Cancel", id="goto-cancel")

    def on_mount(self) -> None:
        self.query_one("#goto-input").focus()

    @on(Input.Submitted, "#goto-input")
    def on_submit(self, _event) -> None:
        self._confirm()

    @on(Button.Pressed, "#goto-go")
    def on_go(self) -> None:
        self._confirm()

    @on(Button.Pressed, "#goto-cancel")
    def on_cancel(self) -> None:
        self.dismiss(None)

    def _confirm(self) -> None:
        raw = self.query_one("#goto-input").value.strip()
        try:
            pos = int(raw)
            if 1 <= pos <= self.total:
                self.dismiss(pos - 1)  # return 0-based index
                return
        except ValueError:
            pass
        self.query_one("#goto-label", Label).update(
            f"[bold red]Enter a number between 1 and {self.total}[/bold red]"
        )


# ---- Main Application ---------------------------------------------------------

class BookManApp(App):
    """Book Manifest Manager -- order chapters, tag roles, export."""

    TITLE = "Book Manifest Manager"

    CSS = """
    #book-title {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        text-align: center;
        text-style: bold;
        padding: 0 2;
    }
    #main-layout {
        layout: horizontal;
        height: 1fr;
    }
    #file-panel {
        width: 40%;
        height: 100%;
        border-right: solid $surface-lighten-2;
    }
    #preview-panel {
        width: 60%;
        height: 100%;
    }
    .panel-header {
        height: 1;
        background: $surface-lighten-1;
        padding: 0 2;
        color: $text-muted;
        text-style: bold;
    }
    BookListView {
        height: 1fr;
    }
    #preview-scroll {
        height: 1fr;
        padding: 0 2;
    }
    #status-bar {
        height: 1;
        background: $surface-lighten-1;
        padding: 0 2;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("shift+up",      "move_up",        "Move Up",   show=False),
        Binding("shift+down",    "move_down",      "Move Down", show=False),
        Binding("K",             "move_up",        "Move Up",   show=False),
        Binding("J",             "move_down",      "Move Down", show=False),
        Binding("0",             "goto_position",  "Ch. #"),
        Binding("s",             "save",           "Save"),
        Binding("e",             "export",         "Export"),
        Binding("r",             "change_role",    "Role"),
        Binding("a",             "add_file",       "Add/Include"),
        Binding("d",             "toggle_exclude", "Exclude"),
        Binding("x",             "remove_file",    "Remove"),
        Binding("t",             "rename_title",   "Title"),
        Binding("question_mark", "help",           "Help"),
        Binding("q",             "quit_app",       "Quit"),
    ]

    dirty: reactive[bool] = reactive(False)

    def __init__(self, base: Path) -> None:
        super().__init__()
        self.base = base
        self.manifest, self._startup_error = load_manifest(base)
        self._sel: int = 0
        self._drag_src: int | None = None
        self._drag_dst: int | None = None

    # -- Layout -----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="book-title")
        with Horizontal(id="main-layout"):
            with Container(id="file-panel"):
                yield Static("  FILE ORDER", classes="panel-header")
                yield BookListView(id="file-list-view")
            with Container(id="preview-panel"):
                yield Static("  PREVIEW", classes="panel-header")
                with VerticalScroll(id="preview-scroll"):
                    yield Markdown("*Select a file to preview.*", id="preview-md")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._normalize_manifest()
        self._rebuild_list()
        self._update_title()
        if self._startup_error:
            self.notify(self._startup_error, severity="warning", timeout=10)
        if self.manifest.files:
            self.call_after_refresh(lambda: self._set_index(0))

    # -- Internal Helpers -------------------------------------------------------

    def _normalize_manifest(self) -> None:
        """Sort: normal files, then appendices, then missing files at the very end."""
        present = [e for e in self.manifest.files if e.exists_at(self.base)]
        missing = [e for e in self.manifest.files if not e.exists_at(self.base)]
        non_ap  = [e for e in present if e.role != "appendix"]
        appendices = [e for e in present if e.role == "appendix"]
        self.manifest.files[:] = non_ap + appendices + missing

    def _rebuild_list(self) -> None:
        lv = self.query_one("#file-list-view", BookListView)
        lv.clear()

        tracked = {e.path for e in self.manifest.files}
        untracked_count = sum(
            1 for f in self.base.iterdir()
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
            and not f.name.startswith(".")
            and f.name not in tracked
        )

        ch_num = 0
        for i, entry in enumerate(self.manifest.files):
            if entry.role == "chapter" and entry.enabled:
                ch_num += 1
                chapter_num = ch_num
            else:
                chapter_num = None
            lv.append(FileRow(entry, i, missing=not entry.exists_at(self.base), chapter_num=chapter_num))

        n_enabled = sum(1 for e in self.manifest.files if e.enabled and e.role != "excluded")
        n_missing = sum(1 for e in self.manifest.files if not e.exists_at(self.base))
        self.query_one("#status-bar", Static).update(
            f"  {len(self.manifest.files)} files in manifest "
            f"({n_enabled} enabled)  *  "
            f"{untracked_count} untracked  *  "
            f"{n_missing} missing"
        )

    def _update_title(self) -> None:
        mark = " (unsaved)" if self.dirty else ""
        self.title = f"Book Manifest Manager -- {self.manifest.title}{mark}"
        try:
            self.query_one("#book-title", Static).update(
                f"★  {self.manifest.title}{mark}  ★"
            )
        except Exception:
            pass

    def _update_preview(self, idx: int) -> None:
        if not (0 <= idx < len(self.manifest.files)):
            return
        entry = self.manifest.files[idx]
        fpath = self.base / entry.path
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8")
            _, color, role_label = ROLES.get(entry.role, ("CH", "white", "Chapter"))
            header = f"**[{role_label}]**  `{entry.path}`\n\n---\n\n"
            self.query_one("#preview-md", Markdown).update(header + content)
        else:
            self.query_one("#preview-md", Markdown).update(
                f"*File not found on disk:* `{entry.path}`"
            )

    def _set_index(self, idx: int) -> None:
        lv = self.query_one("#file-list-view", BookListView)
        lv.index = idx

    def _mark_dirty_refresh(self, new_idx: int) -> None:
        self.dirty = True
        self._rebuild_list()
        self.call_after_refresh(lambda: self._set_index(new_idx))
        self._update_preview(new_idx)

    # -- Event Handlers ---------------------------------------------------------

    @on(ListView.Highlighted)
    def on_list_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, FileRow):
            self._sel = event.item.file_index
            self._update_preview(self._sel)

    # -- Mouse Drag to Reorder --------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        row = self._row_at(*event.screen_offset)
        if row:
            self._drag_src = row.file_index
            self._drag_dst = row.file_index

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_src is None:
            return
        row = self._row_at(*event.screen_offset)
        if row and row.file_index != self._drag_dst:
            for w in self.query(FileRow):
                w.remove_class("drag-target")
            self._drag_dst = row.file_index
            row.add_class("drag-target")

    def on_mouse_up(self, event: events.MouseUp) -> None:
        src, dst = self._drag_src, self._drag_dst
        for w in self.query(FileRow):
            w.remove_class("drag-target")
        self._drag_src = None
        self._drag_dst = None
        if src is not None and dst is not None and src != dst:
            files = self.manifest.files
            moving = files[src]
            target = files[dst]
            # Enforce appendix boundary
            if moving.role == "appendix" and target.role != "appendix" and dst < src:
                self.notify("Appendices stay at the end.", severity="warning")
            elif moving.role != "appendix" and target.role == "appendix" and dst > src:
                self.notify("Appendices stay at the end.", severity="warning")
            else:
                item = files.pop(src)
                files.insert(dst, item)
                self._sel = dst
                self._mark_dirty_refresh(dst)

    def _row_at(self, x: int, y: int) -> FileRow | None:
        """Return FileRow under screen coordinates, or None."""
        try:
            widget, _ = self.get_widget_at(x, y)
            w = widget
            for _ in range(6):
                if isinstance(w, FileRow):
                    return w
                if not hasattr(w, "parent") or w.parent is None:
                    break
                w = w.parent
        except Exception:
            pass
        return None

    # -- Actions ----------------------------------------------------------------

    def action_move_up(self) -> None:
        idx = self._sel
        if idx > 0:
            f = self.manifest.files
            above = f[idx - 1]
            cur   = f[idx]
            cur_missing  = not cur.exists_at(self.base)
            abv_missing  = not above.exists_at(self.base)
            if cur_missing and not abv_missing:
                self.notify("Missing files stay at the end.", severity="warning")
                return
            if cur.role == "appendix" and above.role != "appendix" and not abv_missing:
                self.notify("Appendices stay at the end.", severity="warning")
                return
            f[idx], f[idx - 1] = f[idx - 1], f[idx]
            self._sel = idx - 1
            self._mark_dirty_refresh(idx - 1)

    def action_move_down(self) -> None:
        idx = self._sel
        if idx < len(self.manifest.files) - 1:
            f = self.manifest.files
            below = f[idx + 1]
            cur   = f[idx]
            cur_missing  = not cur.exists_at(self.base)
            blw_missing  = not below.exists_at(self.base)
            if not cur_missing and blw_missing:
                self.notify("Missing files stay at the end.", severity="warning")
                return
            if cur.role != "appendix" and below.role == "appendix" and not blw_missing:
                self.notify("Appendices stay at the end.", severity="warning")
                return
            f[idx], f[idx + 1] = f[idx + 1], f[idx]
            self._sel = idx + 1
            self._mark_dirty_refresh(idx + 1)

    def action_goto_position(self) -> None:
        if not self.manifest.files:
            return
        entry = self.manifest.files[self._sel]
        if entry.role != "chapter":
            self.notify("Chapter numbering only applies to chapter files.", severity="information")
            return

        chapter_indices = [i for i, e in enumerate(self.manifest.files) if e.role == "chapter"]
        n_chapters = len(chapter_indices)
        if n_chapters < 2:
            self.notify("Need at least two chapters to renumber.", severity="information")
            return
        current_ch_num = chapter_indices.index(self._sel) + 1

        def apply(target_ch_num: int | None) -> None:
            if target_ch_num is None or target_ch_num == current_ch_num:
                return
            files = self.manifest.files
            item = files.pop(self._sel)
            # Recount chapter positions after the pop
            ch_idx_after = [i for i, e in enumerate(files) if e.role == "chapter"]
            if target_ch_num <= len(ch_idx_after):
                insert_at = ch_idx_after[target_ch_num - 1]
            elif ch_idx_after:
                insert_at = ch_idx_after[-1] + 1
            else:
                insert_at = len(files)
            files.insert(insert_at, item)
            self._sel = insert_at
            self._mark_dirty_refresh(insert_at)

        self.push_screen(GotoScreen(n_chapters, current_ch_num), apply)

    def action_save(self) -> None:
        err = save_manifest(self.base, self.manifest)
        if err:
            self.notify(err, severity="error")
        else:
            self.dirty = False
            self._update_title()
            self.notify("Saved .bookmanifest", severity="information")

    def action_export(self) -> None:
        self.push_screen(ExportScreen(self.base, self.manifest))

    def action_change_role(self) -> None:
        if not self.manifest.files:
            return
        entry = self.manifest.files[self._sel]

        def apply(role: str | None) -> None:
            if role:
                entry.role = role
                if role == "appendix":
                    _normalize_appendices(self.manifest)
                    self._sel = next(
                        i for i, e in enumerate(self.manifest.files) if e is entry
                    )
                self._mark_dirty_refresh(self._sel)

        self.push_screen(RolePickerScreen(entry.role), apply)

    def action_add_file(self) -> None:
        # If cursor is on an excluded file, re-include it instead of scanning disk
        if self.manifest.files and self.manifest.files[self._sel].role == "excluded":
            self.manifest.files[self._sel].role = "chapter"
            self._mark_dirty_refresh(self._sel)
            return

        # Otherwise scan disk for files not yet in the manifest
        tracked = {e.path for e in self.manifest.files}
        untracked = sorted(
            f.name for f in self.base.iterdir()
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
            and not f.name.startswith(".")
            and f.name not in tracked
        )
        if not untracked:
            self.notify("No files on disk outside the manifest.", severity="information")
            return

        def apply(paths: list[str] | None) -> None:
            if paths:
                for p in paths:
                    self.manifest.files.append(FileEntry(path=p, role=_guess_role(p)))
                self._mark_dirty_refresh(self._sel)

        self.push_screen(AddFileScreen(untracked), apply)

    def action_toggle_exclude(self) -> None:
        if not self.manifest.files:
            return
        entry = self.manifest.files[self._sel]
        if entry.role == "excluded":
            self.notify("Already excluded. Press A to re-include.", severity="information")
            return
        entry.role = "excluded"
        self._mark_dirty_refresh(self._sel)

    def action_remove_file(self) -> None:
        if not self.manifest.files:
            return
        entry = self.manifest.files.pop(self._sel)
        new_sel = min(self._sel, len(self.manifest.files) - 1)
        self._sel = max(new_sel, 0)
        self._mark_dirty_refresh(self._sel)
        self.notify(f"Removed '{entry.path}' from manifest.", severity="information")

    def action_rename_title(self) -> None:
        from textual.widgets import Button

        class TitleScreen(ModalScreen):
            BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
            DEFAULT_CSS = """
            TitleScreen { align: center middle; }
            TitleScreen > Vertical { width: 60; height: auto; border: round $primary; background: $surface; padding: 1 2; }
            TitleScreen Label { margin-bottom: 1; text-style: bold; }
            TitleScreen Input { width: 100%; }
            TitleScreen .buttons { height: 3; align: right middle; }
            """
            def __init__(self, current: str) -> None:
                super().__init__()
                self._current = current
            def compose(self):
                from textual.containers import Vertical, Horizontal
                with Vertical():
                    yield Label("Book Title")
                    yield Input(self._current, id="title-input")
                    with Horizontal(classes="buttons"):
                        yield Button("OK", id="ok", variant="primary")
                        yield Button("Cancel", id="cancel")
            @on(Button.Pressed, "#ok")
            def _ok(self) -> None:
                val = self.query_one("#title-input", Input).value.strip()
                self.dismiss(val or None)
            @on(Button.Pressed, "#cancel")
            def _cancel(self) -> None:
                self.dismiss(None)
            @on(Input.Submitted, "#title-input")
            def _submit(self) -> None:
                val = self.query_one("#title-input", Input).value.strip()
                self.dismiss(val or None)

        def _apply(new_title):
            if new_title and new_title != self.manifest.title:
                self.manifest.title = new_title
                self.dirty = True
                self._update_title()
                self.notify(f"Title set to \"{new_title}\"")

        self.push_screen(TitleScreen(self.manifest.title), _apply)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        if not self.dirty:
            self.exit()
            return

        def handle(choice: str) -> None:
            if choice == "save-quit":
                save_manifest(self.base, self.manifest)
                self.exit()
            elif choice == "just-quit":
                self.exit()

        self.push_screen(QuitScreen(), handle)

    def watch_dirty(self, _dirty: bool) -> None:
        self._update_title()


# ---- Headless Export (used by make out) --------------------------------------

def export_book_cli(base: Path, manifest: Manifest, fmts: set[str]) -> None:
    """Run export pipeline without the GUI; prints progress to stdout."""
    out = base / OUTPUT_DIR
    out.mkdir(exist_ok=True)

    enabled = [e for e in manifest.files if e.enabled and e.role != "excluded"]
    skipped = [e.path for e in enabled if not e.exists_at(base)]
    exportable = [e for e in enabled if e.exists_at(base)]

    for s in skipped:
        print(f"warning  Skipping missing: {s}")

    if not exportable:
        print("error  No exportable files found.", file=sys.stderr)
        sys.exit(1)

    combined = "\n\n---\n\n".join(
        (base / e.path).read_text(encoding="utf-8") for e in exportable
    )
    combined_path = out / "combined.md"
    combined_path.write_text(combined, encoding="utf-8")
    print(f"ok  Combined {len(exportable)} files -> combined.md")

    if "md" in fmts:
        dest = out / "book.md"
        shutil.copy(combined_path, dest)
        print(f"ok  Markdown -> {dest.name}")

    if not fmts & {"pdf", "html", "docx"}:
        return

    if not shutil.which("pandoc"):
        print("error  pandoc not found. Install: https://pandoc.org/installing.html", file=sys.stderr)
        sys.exit(1)

    for fmt in ("pdf", "html", "docx"):
        if fmt not in fmts:
            continue
        dest = out / f"book.{fmt}"
        extra = ["--standalone"] if fmt == "html" else []
        cmd = ["pandoc", str(combined_path), "-o", str(dest)] + extra
        import subprocess
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print(f"ok  {fmt.upper()} -> {dest.name}")
        else:
            print(f"error  {fmt.upper()}: {r.stderr.strip()[:200]}", file=sys.stderr)


# ---- Entry Point --------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        prog="bookman",
        description="Book Manifest Manager",
    )
    parser.add_argument(
        "directory", nargs="?", default=".",
        help="Book directory (default: current directory)",
    )
    parser.add_argument(
        "--export", action="store_true",
        help="Export headlessly without launching the GUI",
    )
    parser.add_argument(
        "--formats", default="pdf,html,docx,md",
        metavar="LIST",
        help="Comma-separated export formats (default: pdf,html,docx,md)",
    )
    args = parser.parse_args()

    base_dir = Path(args.directory).resolve()
    if not base_dir.is_dir():
        print(f"error: not a directory: {base_dir}", file=sys.stderr)
        sys.exit(1)

    if args.export:
        manifest, err = load_manifest(base_dir)
        if err:
            print(f"warning  {err}", file=sys.stderr)
        export_book_cli(base_dir, manifest, set(args.formats.split(",")))
    else:
        BookManApp(base_dir).run()
