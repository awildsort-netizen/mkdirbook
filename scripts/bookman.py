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
import re
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
    "template":     ("TM", "#80cbc4", "Template"),
    "poetry":       ("PO", "#f48fb1", "Poetry"),
}

MANIFEST_FILE = ".bookmanifest"  # legacy; kept for migration only


def _title_to_slug(title: str) -> str:
    """Convert a title to a filesystem-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "untitled"


def manifest_path_for(base: Path, title: str) -> Path:
    """Return the canonical <work-dir>/<slug>.json path for a given title."""
    slug = _title_to_slug(title)
    return base / slug / f"{slug}.json"


def _looks_like_manifest(path: Path) -> bool:
    """Return True when a JSON file matches the expected manifest shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("title"), str) and isinstance(data.get("files"), list)


def find_manifest_paths(base: Path) -> list[Path]:
    """Find manifest JSON files kept inside top-level work directories."""
    manifests: list[Path] = []
    for jf in sorted(base.glob("*/*.json")):
        if jf.parent.name.startswith(".") or jf.parent.name in {"out", "scripts", "templates", ".venv"}:
            continue
        if _looks_like_manifest(jf):
            manifests.append(jf)
    return manifests


def _migrate_dotfile(base: Path) -> None:
    """If .bookmanifest exists and no manifests exist yet, migrate it."""
    old = base / MANIFEST_FILE
    if not old.exists():
        return
    if find_manifest_paths(base):
        return  # already migrated
    try:
        data = json.loads(old.read_text(encoding="utf-8"))
        title = data.get("title", base.name.replace("-", " ").replace("_", " ").title())
        dest = manifest_path_for(base, title)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        old.unlink()
    except Exception as exc:
        pass  # migration best-effort; original file stays


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
    output_dir: str = ""
    output_name: str = ""
    output_name_template: str = "{{ output_name }}"
    custom_templates: dict = field(default_factory=dict)
    files: list[FileEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "version": self.version,
            "output_dir": self.output_dir,
            "output_name": self.output_name,
            "output_name_template": self.output_name_template,
            "custom_templates": self.custom_templates,
            "files": [asdict(f) for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        files = [FileEntry(**f) for f in data.get("files", [])]
        title = data.get("title", "Untitled Book")
        output_dir = data.get("output_dir", "") or f"out/{_title_to_slug(title)}"
        output_name = data.get("output_name", "") or _title_to_slug(title)
        return cls(
            title=title,
            version=data.get("version", MANIFEST_VERSION),
            output_dir=output_dir,
            output_name=output_name,
            output_name_template=data.get("output_name_template", "{{ output_name }}"),
            custom_templates=data.get("custom_templates", {}),
            files=files,
        )


def load_manifest(manifest_path: Path, base: Path) -> tuple[Manifest, str | None]:
    """
    Load manifest from an explicit JSON path.
    Returns (manifest, error_or_None).
    On JSON error: backs up corrupt file and rebuilds from directory scan.
    On missing file: creates fresh manifest from directory scan.
    """
    if not manifest_path.exists():
        m = _scan_to_manifest(base)
        _normalize_appendices(m)
        return m, None

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        m = Manifest.from_dict(data)
        _normalize_appendices(m)
        return m, None
    except json.JSONDecodeError as exc:
        backup = manifest_path.with_suffix(".bak")
        shutil.copy(manifest_path, backup)
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


def save_manifest(manifest_path: Path, manifest: Manifest) -> str | None:
    """Save manifest to path. Returns error string or None on success."""
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return None
    except Exception as exc:
        return f"Failed to save: {exc}"


@dataclass
class WorkSummary:
    """Stats for one work shown in the launcher."""
    title: str
    path: Path
    file_count: int
    word_count: int
    last_modified: float  # epoch seconds


def scan_works(base: Path) -> list[WorkSummary]:
    """Scan top-level work directories and return summaries for all manifests."""
    summaries: list[WorkSummary] = []
    for jf in find_manifest_paths(base):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            m = Manifest.from_dict(data)
        except Exception:
            continue
        enabled = [e for e in m.files if e.enabled and e.role != "excluded"]
        file_count = 0
        word_count = 0
        last_modified = 0.0
        for e in enabled:
            fp = base / e.path
            if fp.exists():
                file_count += 1
                try:
                    word_count += len(fp.read_text(encoding="utf-8", errors="replace").split())
                    mtime = fp.stat().st_mtime
                    if mtime > last_modified:
                        last_modified = mtime
                except Exception:
                    pass
        summaries.append(WorkSummary(
            title=m.title,
            path=jf,
            file_count=file_count,
            word_count=word_count,
            last_modified=last_modified,
        ))
    return summaries


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


class FileExplorerScreen(ModalScreen):
    """Browse the project directory tree and select files to add to the manifest."""

    BINDINGS = [
        Binding("escape",    "dismiss(None)",    "Cancel"),
        Binding("space",     "toggle_selection", "Select"),
        Binding("enter",     "activate_item",    "Open/Select"),
        Binding("backspace", "go_up",            "Parent Dir", show=False),
    ]

    DEFAULT_CSS = """
    FileExplorerScreen { align: center middle; }
    #explorer-box {
        width: 70; height: 28;
        border: round $primary; background: $surface;
    }
    #explorer-path {
        height: 1; background: $primary-darken-2;
        color: $text; padding: 0 2; text-style: bold;
    }
    #explorer-list { height: 1fr; }
    #explorer-status {
        height: 1; background: $surface-lighten-1;
        color: $text-muted; padding: 0 2;
    }
    #explorer-btns { height: 3; align: right middle; padding: 0 1; }
    """

    def __init__(self, base: Path, existing: set[str]) -> None:
        super().__init__()
        self._base = base
        self._cwd = base
        self._existing = existing          # relative paths already in manifest
        self._selected: set[Path] = set() # absolute paths chosen
        self._entries: list[tuple[str, Path | None]] = []

    def compose(self) -> ComposeResult:
        with Container(id="explorer-box"):
            yield Static("", id="explorer-path")
            yield ListView(id="explorer-list")
            yield Static("", id="explorer-status")
            with Horizontal(id="explorer-btns"):
                yield Button("Add Selected", id="exp-add", variant="primary")
                yield Button("Cancel",       id="exp-cancel")

    def on_mount(self) -> None:
        self._refresh_list()

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self._base))

    def _make_label(self, kind: str, path: "Path | None") -> str:
        if kind == "up":
            return "  [dim]\u2191  ..[/dim]"
        assert path is not None
        if kind == "dir":
            return f"  [bold cyan]{chr(0x1f4c1)}  {path.name}/[/bold cyan]"
        rel = self._rel(path)
        in_manifest = rel in self._existing
        selected = path in self._selected
        if in_manifest:
            return f"  [dim]\u00b7  {path.name}  [in manifest][/dim]"
        marker = "[green]\u2713[/green]" if selected else " "
        name = f"[green]{path.name}[/green]" if selected else path.name
        return f"  {marker}  {name}"

    def _refresh_list(self) -> None:
        try:
            rel = self._cwd.relative_to(self._base)
            path_str = (
                f"{self._base.name}/{rel}/"
                if str(rel) != "."
                else f"{self._base.name}/"
            )
        except ValueError:
            path_str = str(self._cwd)

        self.query_one("#explorer-path", Static).update(f"  {chr(0x1f4c2)}  {path_str}")

        entries: list[tuple[str, Path | None]] = []
        if self._cwd != self._base:
            entries.append(("up", None))

        try:
            items = sorted(
                self._cwd.iterdir(),
                key=lambda x: (not x.is_dir(), x.name.lower()),
            )
            for item in items:
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    entries.append(("dir", item))
                elif item.suffix.lower() in SUPPORTED_EXTENSIONS:
                    entries.append(("file", item))
        except PermissionError:
            pass

        self._entries = entries
        lv = self.query_one("#explorer-list", ListView)
        lv.clear()
        for kind, path in entries:
            lv.append(ListItem(Static(self._make_label(kind, path), markup=True)))

        self.call_after_refresh(self._update_status)

    def _update_status(self) -> None:
        n = len(self._selected)
        self.query_one("#explorer-status", Static).update(
            f"  {n} file{'s' if n != 1 else ''} selected"
            "  \u00b7  Space/Enter = select  \u00b7  Backspace = up"
        )

    def _toggle_current(self) -> None:
        lv = self.query_one("#explorer-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._entries):
            return
        kind, path = self._entries[idx]
        if kind != "file" or path is None:
            return
        if self._rel(path) in self._existing:
            return
        if path in self._selected:
            self._selected.discard(path)
        else:
            self._selected.add(path)
        items = list(lv.query(ListItem))
        if 0 <= idx < len(items):
            items[idx].query_one(Static).update(self._make_label(kind, path))
        self._update_status()

    def action_toggle_selection(self) -> None:
        self._toggle_current()

    def action_activate_item(self) -> None:
        lv = self.query_one("#explorer-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._entries):
            return
        kind, path = self._entries[idx]
        if kind == "up":
            self.action_go_up()
        elif kind == "dir" and path is not None:
            self._cwd = path
            self._refresh_list()
        else:
            self._toggle_current()

    def action_go_up(self) -> None:
        if self._cwd != self._base:
            self._cwd = self._cwd.parent
            self._refresh_list()

    @on(ListView.Selected)
    def _on_list_selected(self, event: "ListView.Selected") -> None:
        idx = event.list_view.index
        if idx is None or idx >= len(self._entries):
            return
        kind, path = self._entries[idx]
        if kind in ("up", "dir"):
            self.action_activate_item()

    @on(Button.Pressed, "#exp-add")
    def _add(self) -> None:
        result = sorted(self._rel(p) for p in self._selected)
        self.dismiss(result if result else None)

    @on(Button.Pressed, "#exp-cancel")
    def _cancel(self) -> None:
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
    .exp-outdir { color: $text-muted; margin-bottom: 1; }
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
        out_rel = self.manifest.output_dir or f"out/{_title_to_slug(self.manifest.title)}"
        with Container(id="exp-box"):
            yield Label("Export Book", id="exp-title")
            yield Static(f"  Output: [dim]{out_rel}[/dim]", markup=True, classes="exp-outdir")
            yield Checkbox("PDF",                id="fmt-pdf",  value=True, classes="fmt-ck")
            yield Checkbox("HTML (via Jinja2)", id="fmt-html", value=True, classes="fmt-ck")
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

        out_rel = self.manifest.output_dir or f"out/{_title_to_slug(self.manifest.title)}"
        out = self.base / out_rel
        out.mkdir(parents=True, exist_ok=True)

        enabled = [e for e in self.manifest.files if e.enabled and e.role != "excluded"]
        for e in enabled:
            if not e.exists_at(self.base):
                self._log(f"warning  Skipping missing: {e.path}")

        exportable = [e for e in enabled if e.exists_at(self.base)]
        if not exportable:
            self._log("error  No exportable files found.")
            return

        output_name = self.manifest.output_name or _title_to_slug(self.manifest.title)

        # Render md via Jinja2 template
        rendered_md = render_book(self.base, self.manifest, "md")
        combined_path = out / "combined.md"
        combined_path.write_text(rendered_md, encoding="utf-8")
        chapter_count = len(_build_chapter_list(self.base, self.manifest))
        self._log(f"ok  Rendered {chapter_count} chapters -> combined.md")

        if fmts["md"]:
            dest = out / f"{output_name}.md"
            shutil.copy(combined_path, dest)
            self._log(f"ok  Markdown -> {dest.name}")

        if fmts["html"]:
            rendered_html = render_book(self.base, self.manifest, "html")
            dest = out / f"{output_name}.html"
            dest.write_text(rendered_html, encoding="utf-8")
            self._log(f"ok  HTML -> {dest.name}")

        if not any(fmts[f] for f in ("pdf", "docx")):
            self._log("-- Done --")
            return

        if not shutil.which("pandoc"):
            self._log("error  pandoc not found.")
            self._log("   Install: https://pandoc.org/installing.html")
            return

        for fmt in ("pdf", "docx"):
            if not fmts[fmt]:
                continue
            dest = out / f"{output_name}.{fmt}"
            cmd = ["pandoc", str(combined_path), "-o", str(dest)]
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
        Binding("f2",            "rename_file",    "Rename"),
        Binding("m",             "metadata",       "Metadata"),
        Binding("question_mark", "help",           "Help"),
        Binding("q",             "quit_app",       "Quit"),
    ]

    dirty: reactive[bool] = reactive(False)

    def __init__(self, base: Path, manifest_path: Path) -> None:
        super().__init__()
        self.base = base
        self.manifest_path = manifest_path
        self.manifest, self._startup_error = load_manifest(manifest_path, base)
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
            force_poetry = entry.role == "poetry"
            processed = _process_poetry_breaks(content, "  ", force=force_poetry)
            is_poetry = force_poetry or processed != content
            _, color, role_label = ROLES.get(entry.role, ("CH", "white", "Chapter"))
            poetry_tag = "  \u2139\ufe0f *poetry mode*" if is_poetry else ""
            header = f"**[{role_label}]**  `{entry.path}`{poetry_tag}\n\n---\n\n"
            self.query_one("#preview-md", Markdown).update(header + processed)
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
        err = save_manifest(self.manifest_path, self.manifest)
        if err:
            self.notify(err, severity="error")
        else:
            self.dirty = False
            self._update_title()
            self.notify(f"Saved {self.manifest_path.name}", severity="information")

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

        # Open file explorer — user browses project tree and picks files
        existing = {e.path for e in self.manifest.files}

        def apply(paths: list[str] | None) -> None:
            if paths:
                for rel in paths:
                    self.manifest.files.append(FileEntry(path=rel, role=_guess_role(rel)))
                self._mark_dirty_refresh(self._sel)

        self.push_screen(FileExplorerScreen(self.base, existing), apply)

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

    def action_rename_file(self) -> None:
        """Rename the selected file on disk and update the manifest."""
        if not self.manifest.files:
            return
        entry = self.manifest.files[self._sel]
        old_path = self.base / entry.path
        is_missing = not old_path.exists()

        from textual.widgets import Button

        class RenameScreen(ModalScreen):
            BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
            DEFAULT_CSS = """
            RenameScreen { align: center middle; }
            RenameScreen > Vertical {
                width: 64; height: auto;
                border: round $primary; background: $surface; padding: 1 2;
            }
            RenameScreen .field-label { text-style: bold; margin-bottom: 0; }
            RenameScreen .field-hint  { color: $text-muted; margin-bottom: 1; }
            RenameScreen Input { width: 100%; margin-bottom: 1; }
            RenameScreen .buttons { height: 3; align: right middle; }
            """
            def __init__(self, current: str, missing: bool) -> None:
                super().__init__()
                self._current = current
                self._missing = missing
            def compose(self):
                from textual.containers import Vertical, Horizontal
                hint = (
                    "[dim]File is missing on disk — only the manifest entry will be updated.[/dim]"
                    if self._missing else
                    "[dim]The file will be renamed on disk and in the manifest.[/dim]"
                )
                with Vertical():
                    yield Label("Rename File", classes="field-label")
                    yield Label(hint, classes="field-hint", markup=True)
                    yield Input(self._current, id="rename-input", select_on_focus=True)
                    with Horizontal(classes="buttons"):
                        yield Button("Rename", id="ok", variant="primary")
                        yield Button("Cancel", id="cancel")
            @on(Button.Pressed, "#ok")
            def _ok(self) -> None:
                val = self.query_one("#rename-input", Input).value.strip()
                self.dismiss(val or None)
            @on(Button.Pressed, "#cancel")
            def _cancel(self) -> None:
                self.dismiss(None)
            @on(Input.Submitted, "#rename-input")
            def _submit(self) -> None:
                val = self.query_one("#rename-input", Input).value.strip()
                self.dismiss(val or None)

        def _apply(new_name: str | None) -> None:
            if not new_name or new_name == entry.path:
                return
            new_path = self.base / new_name
            # Reject if the new name already exists (and isn't the same file)
            if new_path.exists() and new_path != old_path:
                self.notify(f"File already exists: {new_name}", severity="error")
                return
            # Rename on disk if the file exists
            if old_path.exists():
                try:
                    old_path.rename(new_path)
                except OSError as exc:
                    self.notify(f"Rename failed: {exc}", severity="error")
                    return
            # Update manifest entry
            entry.path = new_name
            self.dirty = True
            self._rebuild_list()
            self.notify(f"Renamed → {new_name}")

        self.push_screen(RenameScreen(entry.path, is_missing), _apply)

    def action_metadata(self) -> None:
        """Open the metadata editor (title, output directory)."""
        from textual.widgets import Button

        class MetadataScreen(ModalScreen):
            BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
            DEFAULT_CSS = """
            MetadataScreen { align: center middle; }
            MetadataScreen > Vertical {
                width: 68; height: auto;
                border: round $primary; background: $surface; padding: 1 2;
            }
            MetadataScreen .field-label { text-style: bold; margin-top: 1; }
            MetadataScreen .field-hint { color: $text-muted; margin-bottom: 0; }
            MetadataScreen Input { width: 100%; margin-bottom: 1; }
            MetadataScreen .buttons { height: 3; align: right middle; margin-top: 1; }
            """
            def __init__(self, title: str, output_dir: str, output_name: str = "") -> None:
                super().__init__()
                self._title = title
                self._output_dir = output_dir
                self._output_name = output_name
            def compose(self):
                from textual.containers import Vertical, Horizontal
                with Vertical():
                    yield Label("Book Metadata", classes="field-label")
                    yield Label("Title", classes="field-label")
                    yield Input(self._title, id="meta-title")
                    yield Label("Output Name  [dim](base filename, no extension)[/dim]",
                                classes="field-label", markup=True)
                    yield Label("[dim]e.g.  my-book  → my-book.pdf[/dim]", classes="field-hint", markup=True)
                    yield Input(self._output_name, id="meta-outname")
                    yield Label("Output Directory  [dim](relative to project root)[/dim]",
                                classes="field-label", markup=True)
                    yield Label("[dim]e.g.  out/my-book[/dim]", classes="field-hint", markup=True)
                    yield Input(self._output_dir, id="meta-outdir")
                    with Horizontal(classes="buttons"):
                        yield Button("Save", id="ok", variant="primary")
                        yield Button("Cancel", id="cancel")
            @on(Button.Pressed, "#ok")
            def _ok(self) -> None:
                title = self.query_one("#meta-title", Input).value.strip()
                outdir = self.query_one("#meta-outdir", Input).value.strip()
                outname = self.query_one("#meta-outname", Input).value.strip()
                self.dismiss((title or None, outdir, outname))
            @on(Button.Pressed, "#cancel")
            def _cancel(self) -> None:
                self.dismiss(None)

        cur_outdir = self.manifest.output_dir or f"out/{_title_to_slug(self.manifest.title)}"
        cur_outname = self.manifest.output_name or _title_to_slug(self.manifest.title)

        def _apply(result) -> None:
            if result is None:
                return
            new_title, new_outdir, new_outname = result
            changed = False
            if new_title and new_title != self.manifest.title:
                self.manifest.title = new_title
                # Rename the manifest file to match new title slug
                new_path = self.manifest_path.with_name(f"{_title_to_slug(new_title)}.json")
                if new_path != self.manifest_path:
                    try:
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        self.manifest_path.rename(new_path)
                        self.manifest_path = new_path
                    except Exception:
                        pass
                changed = True
            if new_outdir != self.manifest.output_dir:
                self.manifest.output_dir = new_outdir
                changed = True
            if new_outname and new_outname != self.manifest.output_name:
                self.manifest.output_name = new_outname
                changed = True
            if changed:
                self.dirty = True
                self._update_title()
                self.notify("Metadata saved")

        self.push_screen(MetadataScreen(self.manifest.title, cur_outdir, cur_outname), _apply)
    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        if not self.dirty:
            self.exit()
            return

        def handle(choice: str) -> None:
            if choice == "save-quit":
                save_manifest(self.manifest_path, self.manifest)
                self.exit()
            elif choice == "just-quit":
                self.exit()

        self.push_screen(QuitScreen(), handle)

    def watch_dirty(self, _dirty: bool) -> None:
        self._update_title()


# ---- Headless Export (used by make out) --------------------------------------

# ---- Jinja2 Render Engine ---------------------------------------------------

_BUILTIN_MD_TEMPLATE = """\
{%- macro render_chapter(ch) %}{% if ch.num is not none %}*Chapter {{ ch.num }}*

{% endif %}{{ ch.content_md }}{% endmacro -%}
{%- macro render_default(ch) %}{{ ch.content_md }}{% endmacro -%}
{%- macro render(ch) -%}
{%- if ch.role == "chapter" %}{{ render_chapter(ch) }}{%- else %}{{ render_default(ch) }}{%- endif %}
{%- endmacro -%}
---
title: "{{ title }}"
date: {{ date }}
---
{% for ch in chapters %}{{ render(ch) }}
{% if not loop.last %}
---

{% endif %}
{%- endfor %}
"""

_BUILTIN_HTML_TEMPLATE = """\
{%- macro render_chapter(ch) -%}
<section class="chapter">
{% if ch.num is not none %}<p class="chapter-label">Chapter {{ ch.num }}</p>{% endif %}
{{ ch.content_html | markdown | safe }}
</section>
{%- endmacro -%}
{%- macro render_default(ch) -%}
<section data-role="{{ ch.role }}">{{ ch.content_html | markdown | safe }}</section>
{%- endmacro -%}
{%- macro render(ch) -%}
{%- if ch.role == "chapter" %}{{ render_chapter(ch) }}{%- else %}{{ render_default(ch) }}{%- endif %}
{%- endmacro -%}
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{{ title }}</title>
<style>body{font-family:Georgia,serif;max-width:700px;margin:2em auto;padding:0 1.5em;line-height:1.7}
.chapter-label{font-size:.8em;text-transform:uppercase;letter-spacing:.1em;color:#999}
.cover{text-align:center;margin:3em 0}.cover h1{font-size:3em}
hr{border:none;border-top:1px solid #ddd;margin:2.5em 0}</style>
</head><body>
<div class="cover"><h1>{{ title }}</h1><p>{{ date }}</p></div>
{% for ch in chapters %}{{ render(ch) }}{% if not loop.last %}<hr>{% endif %}{% endfor %}
</body></html>
"""


# ---- Poetry Detection -------------------------------------------------------

_POETRY_SKIP_RE = None

def _poetry_skip_re():
    global _POETRY_SKIP_RE
    if _POETRY_SKIP_RE is None:
        import re
        _POETRY_SKIP_RE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|\s{4})")
    return _POETRY_SKIP_RE


def _detect_block_poetry(lines: list) -> bool:
    """Return True if a list of content lines looks like a poetry block.

    Only interior lines (not the last line of the paragraph) are used for
    the short-line heuristic: paragraph-closing lines are often short even in
    prose and should not bias detection.
    """
    skip = _poetry_skip_re()
    filtered = [l for l in lines if l.strip() and not skip.match(l)]
    if len(filtered) < 3:
        return False
    body = filtered[:-1]  # exclude the paragraph-final line
    return max(len(l.rstrip()) for l in body) < 100


def _process_poetry_breaks(content: str, line_break: str = "  ", force: bool = False) -> str:
    """Add line breaks to poetry-like blocks within markdown content.

    When *force* is True every block is treated as poetry regardless of the
    automatic heuristic (used when the file role is explicitly \"poetry\").
    The last non-empty line of each block never gets a trailing hard break
    because the following paragraph separator already provides the break.
    """
    import re
    skip = _poetry_skip_re()
    blocks = re.split(r"\n\n+", content)
    result = []
    for block in blocks:
        lines = block.splitlines()
        filtered = [l for l in lines if l.strip() and not skip.match(l)]
        if force or _detect_block_poetry(filtered):
            processed = []
            last_idx = max((i for i, l in enumerate(lines) if l.strip()), default=-1)
            for i, line in enumerate(lines):
                if line.strip() and i != last_idx:
                    processed.append(line.rstrip() + line_break)
                else:
                    processed.append(line)
            result.append("\n".join(processed))
        else:
            result.append(block)
    return "\n\n".join(result)


def _resolve_template(base: Path, manifest: "Manifest", fmt: str) -> str:
    """Return Jinja2 template source for *fmt* using resolution priority."""
    # 1. custom_templates field
    if fmt in manifest.custom_templates:
        tpath = base / manifest.custom_templates[fmt]
        if tpath.exists():
            return tpath.read_text(encoding="utf-8")
    # 2. files with role="template" whose note matches this fmt
    for e in manifest.files:
        if e.role == "template" and e.exists_at(base):
            fmts_listed = [f.strip() for f in (e.note or "").split(",") if f.strip()]
            if not fmts_listed or fmt in fmts_listed:
                return (base / e.path).read_text(encoding="utf-8")
    # 3. templates/book.<fmt>.j2 on disk
    tpath = base / "templates" / f"book.{fmt}.j2"
    if tpath.exists():
        return tpath.read_text(encoding="utf-8")
    # 4. built-in fallback
    if fmt == "html":
        return _BUILTIN_HTML_TEMPLATE
    return _BUILTIN_MD_TEMPLATE


def _render_markdown(text: str) -> str:
    """Convert markdown text to HTML. Uses the markdown package if available."""
    try:
        import markdown as _md
        return _md.markdown(text, extensions=["extra", "sane_lists"])
    except ImportError:
        # Minimal fallback: wrap paragraphs in <p>
        import re
        paras = re.split(r"\n\n+", text.strip())
        return "\n".join(f"<p>{p.strip()}</p>" for p in paras if p.strip())


def _build_chapter_list(base: Path, manifest: "Manifest") -> list[dict]:
    """Return chapter dicts with rendered content for use in templates."""
    enabled = [
        e for e in manifest.files
        if e.enabled and e.role not in ("excluded", "template") and e.exists_at(base)
    ]
    chapter_num = 0
    chapters = []
    for e in enabled:
        if e.role == "chapter":
            chapter_num += 1
            num: int | None = chapter_num
        else:
            num = None
        content = (base / e.path).read_text(encoding="utf-8").strip()
        first_line = content.splitlines()[0].lstrip("# ").strip() if content else Path(e.path).stem
        force_poetry = e.role == "poetry"
        content_md = _process_poetry_breaks(content, "  ", force=force_poetry)
        content_html_raw = _process_poetry_breaks(content, "  <br>", force=force_poetry)
        is_poetry = force_poetry or content_md != content  # any block was processed
        chapters.append({
            "title": first_line,
            "filename": e.path,
            "role": e.role,
            "num": num,
            "content": content,
            "content_md": content_md,
            "content_html_raw": content_html_raw,
            "content_html": _render_markdown(content_html_raw),
            "is_poetry": is_poetry,
        })
    return chapters


def render_book(base: Path, manifest: "Manifest", fmt: str) -> str:
    """Render the book using Jinja2, returning the rendered string."""
    from jinja2 import Environment, BaseLoader
    from datetime import datetime

    template_src = _resolve_template(base, manifest, fmt)
    try:
        import markdown as _md_pkg
        def _markdown_filter(text: str) -> str:
            return _md_pkg.markdown(text, extensions=["extra", "sane_lists"])
    except ImportError:
        def _markdown_filter(text: str) -> str:
            # Fallback: wrap in <p> if markdown not installed
            return "<p>" + text.replace("\n\n", "</p>\n<p>") + "</p>"

    env = Environment(loader=BaseLoader(), keep_trailing_newline=True)
    env.filters["markdown"] = _markdown_filter
    chapters = _build_chapter_list(base, manifest)
    body = "\n\n---\n\n".join(ch["content"] for ch in chapters)
    output_name = manifest.output_name or _title_to_slug(manifest.title)
    ctx = {
        "title": manifest.title,
        "output_name": output_name,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "year": datetime.now().year,
        "chapters": chapters,
        "body": body,
        "metadata": manifest.to_dict(),
    }
    return env.from_string(template_src).render(**ctx)


def export_book_cli(base: Path, manifest: Manifest, fmts: set[str]) -> None:
    """Run export pipeline without the GUI; prints progress to stdout."""
    out_rel = manifest.output_dir or f"out/{_title_to_slug(manifest.title)}"
    out = base / out_rel
    out.mkdir(parents=True, exist_ok=True)
    print(f"output  {out_rel}")

    enabled = [e for e in manifest.files if e.enabled and e.role != "excluded"]
    skipped = [e.path for e in enabled if not e.exists_at(base)]
    exportable = [e for e in enabled if e.exists_at(base)]

    for s in skipped:
        print(f"warning  Skipping missing: {s}")

    if not exportable:
        print("error  No exportable files found.", file=sys.stderr)
        sys.exit(1)

    output_name = manifest.output_name or _title_to_slug(manifest.title)

    # Render md via Jinja2 template
    rendered_md = render_book(base, manifest, "md")
    combined_path = out / "combined.md"
    combined_path.write_text(rendered_md, encoding="utf-8")
    exportable_count = len(_build_chapter_list(base, manifest))
    print(f"ok  Rendered {exportable_count} chapters -> combined.md")

    if "md" in fmts:
        dest = out / f"{output_name}.md"
        shutil.copy(combined_path, dest)
        print(f"ok  Markdown -> {dest.name}")

    if "html" in fmts:
        rendered_html = render_book(base, manifest, "html")
        dest = out / f"{output_name}.html"
        dest.write_text(rendered_html, encoding="utf-8")
        print(f"ok  HTML -> {dest.name}")

    if not fmts & {"pdf", "docx"}:
        return

    if not shutil.which("pandoc"):
        print("error  pandoc not found. Install: https://pandoc.org/installing.html", file=sys.stderr)
        sys.exit(1)

    for fmt in ("pdf", "docx"):
        if fmt not in fmts:
            continue
        dest = out / f"{output_name}.{fmt}"
        cmd = ["pandoc", str(combined_path), "-o", str(dest)]
        import subprocess
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print(f"ok  {fmt.upper()} -> {dest.name}")
        else:
            print(f"error  {fmt.upper()}: {r.stderr.strip()[:200]}", file=sys.stderr)


# ---- Works Launcher -----------------------------------------------------------

class WorksLauncherApp(App):
    """Launcher screen: lists all works found in the project tree."""

    TITLE = "Book Works Launcher"

    CSS = """
    WorksLauncherApp {
        background: $surface;
    }
    #launcher-title {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        text-align: center;
        text-style: bold;
        padding: 0 2;
    }
    #works-list {
        height: 1fr;
        padding: 1 2;
    }
    #works-list > ListItem {
        height: 4;
        padding: 0 1;
        margin-bottom: 1;
        border: solid $surface-lighten-2;
    }
    #works-list > ListItem:focus-within {
        border: solid $primary;
        background: $surface-lighten-1;
    }
    #works-list > ListItem.--highlight {
        border: solid $primary;
        background: $surface-lighten-1;
    }
    .work-title {
        text-style: bold;
        color: $text;
    }
    .work-stats {
        color: $text-muted;
        height: 1;
    }
    #no-works {
        padding: 2 4;
        color: $text-muted;
    }
    #launcher-footer {
        height: 1;
        background: $primary-darken-2;
        color: $text-muted;
        text-align: center;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("enter", "open_work", "Open"),
        Binding("n",     "new_work",  "New Work"),
        Binding("q",     "quit",      "Quit"),
        Binding("k",     "cursor_up",   "Up",   show=False),
        Binding("j",     "cursor_down", "Down", show=False),
    ]

    def __init__(self, base: Path) -> None:
        super().__init__()
        self.base = base
        self._works: list[WorkSummary] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="launcher-title")
        yield ListView(id="works-list")
        yield Static(
            "  ↑↓/jk Navigate   Enter Open   N New Work   Q Quit  ",
            id="launcher-footer",
            markup=False,
        )

    def on_mount(self) -> None:
        _migrate_dotfile(self.base)
        self._works = scan_works(self.base)
        self.query_one("#launcher-title", Static).update(
            f"★  Book Works — {self.base.name}  ★"
        )
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        from datetime import datetime
        lv = self.query_one("#works-list", ListView)
        lv.clear()
        if not self._works:
            lv.mount(ListItem(Static(
                "No works found.  Press [bold]N[/bold] to create a new work.",
                id="no-works",
                markup=True,
            )))
            return
        for w in self._works:
            lm = (
                datetime.fromtimestamp(w.last_modified).strftime("%Y-%m-%d %H:%M")
                if w.last_modified else "unknown"
            )
            words_k = f"{w.word_count // 1000}k" if w.word_count >= 1000 else str(w.word_count)
            stats = (
                f"{w.file_count} file{'s' if w.file_count != 1 else ''}  ·  "
                f"{words_k} words  ·  modified {lm}"
            )
            lv.append(ListItem(
                Static(f"[bold]{w.title}[/bold]", classes="work-title", markup=True),
                Static(stats, classes="work-stats"),
            ))
        # Focus list and set initial selection
        def _init_focus():
            lv.focus()
            if self._works:
                lv.index = 0
        self.call_after_refresh(_init_focus)

    def action_cursor_up(self) -> None:
        lv = self.query_one("#works-list", ListView)
        lv.action_cursor_up()

    def action_cursor_down(self) -> None:
        lv = self.query_one("#works-list", ListView)
        lv.action_cursor_down()

    @on(ListView.Selected)
    def _on_selected(self, event: "ListView.Selected") -> None:
        self.action_open_work()

    def action_open_work(self) -> None:
        if not self._works:
            return
        lv = self.query_one("#works-list", ListView)
        idx = lv.index if lv.index is not None else 0
        if 0 <= idx < len(self._works):
            self.exit(result=("open", self._works[idx].path))

    def action_new_work(self) -> None:
        class NewWorkScreen(ModalScreen):
            BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
            DEFAULT_CSS = """
            NewWorkScreen { align: center middle; }
            NewWorkScreen > Vertical {
                width: 60; height: auto;
                border: round $primary; background: $surface; padding: 1 2;
            }
            NewWorkScreen Label { margin-bottom: 1; text-style: bold; }
            NewWorkScreen Input { width: 100%; }
            NewWorkScreen .buttons { height: 3; align: right middle; }
            """
            def compose(self):
                from textual.containers import Vertical, Horizontal
                from textual.widgets import Button
                with Vertical():
                    yield Label("New Work — Enter title:")
                    yield Input("", placeholder="e.g. My Novel", id="new-title-input")
                    with Horizontal(classes="buttons"):
                        yield Button("Create", id="ok", variant="primary")
                        yield Button("Cancel", id="cancel")
            @on(Button.Pressed, "#ok")
            def _ok(self) -> None:
                val = self.query_one("#new-title-input", Input).value.strip()
                self.dismiss(val or None)
            @on(Button.Pressed, "#cancel")
            def _cancel(self) -> None:
                self.dismiss(None)
            @on(Input.Submitted, "#new-title-input")
            def _submit(self) -> None:
                val = self.query_one("#new-title-input", Input).value.strip()
                self.dismiss(val or None)

        def _create(title: str | None) -> None:
            if not title:
                return
            mp = manifest_path_for(self.base, title)
            mp.parent.mkdir(parents=True, exist_ok=True)
            if not mp.exists():
                fresh = Manifest(title=title, output_dir=f"out/{_title_to_slug(title)}")
                save_manifest(mp, fresh)
            self.exit(result=("open", mp))

        self.push_screen(NewWorkScreen(), _create)


def run_launcher(base: Path) -> None:
    """Run the launcher; if a work is selected, open it in BookManApp."""
    while True:
        launcher = WorksLauncherApp(base)
        result = launcher.run()
        if not result:
            break
        action, manifest_path = result
        if action == "open":
            BookManApp(base, manifest_path).run()
        else:
            break


# ---- Entry Point --------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        prog="bookman",
        description="Book Manifest Manager",
    )
    parser.add_argument(
        "path", nargs="?", default=".",
        help="Book directory (default: cwd) or path to a .json manifest file",
    )
    parser.add_argument(
        "--manifest", metavar="FILE",
        help="Open a specific manifest .json file directly (skips launcher)",
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

    given = Path(args.path).resolve()
    if given.is_file() and given.suffix == ".json":
        # Direct manifest path given
        manifest_file = given
        base_dir = given.parent.parent
        _migrate_dotfile(base_dir)
        if args.export:
            pass  # handled below
        else:
            BookManApp(base_dir, manifest_file).run()
            sys.exit(0)
    elif given.is_dir():
        base_dir = given
    else:
        print(f"error: not a directory or manifest file: {given}", file=sys.stderr)
        sys.exit(1)

    _migrate_dotfile(base_dir)

    # --manifest flag overrides launcher
    if getattr(args, "manifest", None):
        mf = Path(args.manifest).resolve()
        if not mf.exists():
            print(f"error: manifest not found: {mf}", file=sys.stderr)
            sys.exit(1)
        manifest_base = mf.parent.parent
        if not args.export:
            BookManApp(manifest_base, mf).run()
            sys.exit(0)
        # export with explicit manifest
        manifest, err = load_manifest(mf, manifest_base)
        if err:
            print(f"warning  {err}", file=sys.stderr)
        export_book_cli(manifest_base, manifest, set(args.formats.split(",")))
        sys.exit(0)

    if args.export:
        _migrate_dotfile(base_dir)
        works = scan_works(base_dir)
        if not works:
            print("error  no manifests found in the project", file=sys.stderr)
            sys.exit(1)
        chosen = works[0]  # default: first manifest
        manifest, err = load_manifest(chosen.path, base_dir)
        if err:
            print(f"warning  {err}", file=sys.stderr)
        export_book_cli(base_dir, manifest, set(args.formats.split(",")))
    else:
        run_launcher(base_dir)
