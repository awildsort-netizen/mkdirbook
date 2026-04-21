#!/usr/bin/env python3
from __future__ import annotations
"""bookcc — book compiler

gcc-style CLI for rendering book manifests to PDF, HTML, DOCX, Markdown, or TiddlyWiki.

Usage:
  bookcc [OPTIONS] [MANIFEST] [FILE ...]

  MANIFEST   .json manifest file  (if omitted, scans the project for a single manifest)
  FILE ...   .md source files     (creates an ad-hoc manifest if no MANIFEST given)

Options:
  -o OUTPUT   Output path; extension selects format (.pdf .html .md .docx .tw.html).
              Repeat for multiple outputs:  -o book.pdf -o book.html
              Comma-suffix for multiple formats from one base: -o book.pdf,html,md
  -f FMT      Format(s) using manifest output settings (pdf html md docx tw).
              Repeat or comma-separate: -f pdf,html
  -t TITLE    Override manifest title
  -d DIR      Override output directory
  -T FILE     Override Jinja2 template for a format: -T html=templates/my.html.j2
  -I          Interactive fix mode — prompt to fix line-break issues
  -n          Dry run — show what would be compiled, don't write output
  -v          Verbose (show chapter list and template resolution)
  --version   Print version and exit

Examples:
  bookcc free2move/free2move.json -o out/free2move.pdf
  bookcc free2move/free2move.json -o out/free2move.pdf,html,md
  bookcc ch1.md ch2.md ch3.md -o draft.pdf -t "My Draft"
  bookcc free2move/free2move.json -f pdf,html
  bookcc free2move/free2move.json            # all formats to manifest output_dir
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# ── Import shared logic from bookman ─────────────────────────────────────────
_here = Path(__file__).parent
sys.path.insert(0, str(_here))
from bookman import (  # noqa: E402
    FileEntry,
    Manifest,
    _build_chapter_list,
    _title_to_slug,
    load_manifest,
    render_book,
    render_book_tw,
    scan_works,
)
from poetry import (  # noqa: E402
    analyse_line_breaks,
    generate_fixes,
    interactive_fix,
)

__version__ = "1.0.0"

KNOWN_FMTS = {"pdf", "html", "md", "docx", "tw"}


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bookcc",
        description="Compile a book manifest to PDF, HTML, DOCX, Markdown, or TiddlyWiki.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  bookcc free2move/free2move.json -o out/free2move.pdf\n"
            "  bookcc free2move/free2move.json -o out/free2move.pdf,html,md\n"
            "  bookcc ch1.md ch2.md -o draft.pdf -t \"My Draft\"\n"
            "  bookcc free2move/free2move.json -f pdf,html\n"
        ),
        add_help=True,
    )
    p.add_argument("inputs", nargs="*", metavar="FILE",
                   help="Manifest (.json) and/or source files (.md)")
    p.add_argument("-o", dest="outputs", action="append", default=[],
                   metavar="OUTPUT",
                   help="Output file; extension = format. Repeat or comma-suffix.")
    p.add_argument("-f", dest="fmts", action="append", default=[],
                   metavar="FMT",
                   help="Format(s): pdf html md docx tw. Repeat or comma-separate.")
    p.add_argument("-t", dest="title", default=None, metavar="TITLE",
                   help="Override title")
    p.add_argument("-d", dest="outdir", default=None, metavar="DIR",
                   help="Override output directory")
    p.add_argument("-T", dest="templates", action="append", default=[],
                   metavar="FMT=FILE",
                   help="Template override, e.g. -T html=templates/my.html.j2")
    p.add_argument("-I", dest="interactive", action="store_true",
                   help="Interactive fix mode — prompt to fix line-break issues")
    p.add_argument("-n", dest="dry_run", action="store_true",
                   help="Dry run — don't write output")
    p.add_argument("-v", dest="verbose", action="store_true",
                   help="Verbose output")
    p.add_argument("--version", action="version", version=f"bookcc {__version__}")
    return p.parse_args(argv)


# ── Format / output resolution ────────────────────────────────────────────────

def _resolve_outputs(args: argparse.Namespace, manifest: Manifest
                     ) -> list[tuple[str, Path | None]]:
    """Return list of (fmt, dest_path_or_None) pairs to produce.

    dest_path is None when the output dir/name comes from the manifest.
    """
    result: list[tuple[str, Path | None]] = []

    # Explicit -o flags
    for out in args.outputs:
        # Split comma-suffix: "book.pdf,html,md" → base="book.pdf", extras=["html","md"]
        parts = out.split(",")
        base_out = parts[0].strip()
        base_path = Path(base_out)

        # Recognise .tw.html as the tw format
        if base_path.name.endswith(".tw.html"):
            ext = "tw"
        else:
            ext = base_path.suffix.lstrip(".").lower()

        if ext in KNOWN_FMTS:
            result.append((ext, base_path))
        elif not ext:
            # bare name or dir: produce all known formats
            for fmt in ("pdf", "html", "md", "docx", "tw"):
                suffix = ".tw.html" if fmt == "tw" else f".{fmt}"
                result.append((fmt, base_path.with_suffix(suffix)))
        else:
            _die(f"Unknown format '.{ext}' in -o {out}")

        # Extra comma-suffixed formats reuse base stem
        for extra in parts[1:]:
            fmt = extra.strip().lower().lstrip(".")
            if fmt not in KNOWN_FMTS:
                _die(f"Unknown format '{fmt}' in -o {out}")
            if fmt == "tw":
                result.append((fmt, base_path.with_name(base_path.stem.split(".")[0] + ".tw.html")))
            else:
                result.append((fmt, base_path.with_suffix(f".{fmt}")))

    # Explicit -f flags (use manifest output settings for path)
    for fspec in args.fmts:
        for fmt in fspec.split(","):
            fmt = fmt.strip().lower()
            if fmt not in KNOWN_FMTS:
                _die(f"Unknown format '{fmt}' in -f {fspec}")
            result.append((fmt, None))

    # No explicit outputs: use all formats implied by manifest (default: all four)
    if not result:
        outname = manifest.output_name or _title_to_slug(manifest.title)
        outdir  = Path(args.outdir or manifest.output_dir or f"out/{_title_to_slug(manifest.title)}")
        for fmt in ("pdf", "html", "md", "docx"):
            result.append((fmt, None))
        # tw is opt-in only; not included in the default all-formats run

    return result


def _dest_for(fmt: str, dest: Path | None, manifest: Manifest,
              base: Path, outdir_override: str | None) -> Path:
    """Resolve final output Path."""
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest
    outname = manifest.output_name or _title_to_slug(manifest.title)
    out_rel = outdir_override or manifest.output_dir or f"out/{_title_to_slug(manifest.title)}"
    out = base / out_rel
    out.mkdir(parents=True, exist_ok=True)
    # TiddlyWiki output uses .tw.html extension to distinguish from regular HTML
    if fmt == "tw":
        return out / f"{outname}.tw.html"
    return out / f"{outname}.{fmt}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _die(msg: str, code: int = 1) -> None:
    print(f"bookcc: error: {msg}", file=sys.stderr)
    sys.exit(code)


def _info(msg: str, verbose: bool = False, always: bool = False) -> None:
    if always or verbose:
        print(msg)


def _warn(msg: str) -> None:
    print(f"bookcc: warning: {msg}", file=sys.stderr)


# ── Manifest construction ─────────────────────────────────────────────────────

def _manifest_from_files(files: list[Path], title: str) -> tuple[Manifest, Path]:
    """Build an ad-hoc manifest from a list of .md files."""
    entries = [FileEntry(path=str(f), role="chapter", enabled=True) for f in files]
    m = Manifest(
        title=title,
        output_name=_title_to_slug(title),
        files=entries,
    )
    # Use the directory of the first file as base
    base = files[0].parent.resolve()
    # Make paths relative to base
    for e in m.files:
        try:
            e.path = str(Path(e.path).relative_to(base))
        except ValueError:
            pass  # keep absolute if not under base
    return m, base


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # ── Separate inputs into manifest and md files ────────────────────────────
    manifest_inputs = [Path(i) for i in args.inputs if i.endswith(".json")]
    md_inputs       = [Path(i) for i in args.inputs if i.endswith(".md")]
    other_inputs    = [i for i in args.inputs if not i.endswith((".json", ".md"))]

    if other_inputs:
        _die(f"Unrecognised inputs: {' '.join(other_inputs)}")

    # ── Resolve base dir + manifest ───────────────────────────────────────────
    if len(manifest_inputs) > 1:
        _die("Only one manifest can be specified.")

    if manifest_inputs:
        manifest_path = manifest_inputs[0].resolve()
        if not manifest_path.exists():
            _die(f"Manifest not found: {manifest_path}")
        base = manifest_path.parent.parent
        manifest, err = load_manifest(manifest_path, base)
        if err:
            _warn(f"Manifest loaded with warnings: {err}")
        if md_inputs:
            _warn("Extra .md files ignored when a manifest is provided.")
    elif md_inputs:
        # Ad-hoc manifest from files on command line
        title = args.title or "Untitled"
        manifest, base = _manifest_from_files([p.resolve() for p in md_inputs], title)
    else:
        # No inputs: scan the project for a single manifest
        cwd = Path.cwd()
        works = scan_works(cwd)
        if not works:
            _die("No manifests found in the project.")
        if len(works) > 1:
            names = ", ".join(w.title for w in works)
            _die(f"Multiple manifests found ({names}); specify one.")
        manifest_path = works[0].path
        base = cwd
        manifest, err = load_manifest(manifest_path, base)
        if err:
            _warn(err)

    # ── Apply overrides ────────────────────────────────────────────────────────
    if args.title:
        manifest.title = args.title
        if not manifest.output_name:
            manifest.output_name = _title_to_slug(args.title)

    custom_templates: dict[str, str] = dict(manifest.custom_templates)
    for tspec in args.templates:
        if "=" not in tspec:
            _die(f"Bad -T spec '{tspec}'; expected fmt=file, e.g. -T html=my.j2")
        fmt_t, tfile = tspec.split("=", 1)
        fmt_t = fmt_t.strip().lower()
        if fmt_t not in KNOWN_FMTS:
            _die(f"Unknown format '{fmt_t}' in -T {tspec}")
        custom_templates[fmt_t] = tfile
    manifest.custom_templates = custom_templates

    # ── Validate inputs ────────────────────────────────────────────────────────
    chapters = _build_chapter_list(base, manifest)
    skipped = [e.path for e in manifest.files
               if e.enabled and e.role not in ("excluded", "template")
               and not e.exists_at(base)]
    for s in skipped:
        _warn(f"Skipping missing file: {s}")

    if not chapters:
        _die("No exportable chapters found.")

    # ── Interactive fix mode (-I) ───────────────────────────────────────────────────────
    if args.interactive:
        any_fixed = False
        enabled = [
            e for e in manifest.files
            if e.enabled and e.role not in ("excluded", "template") and e.exists_at(base)
        ]
        for e in enabled:
            filepath = base / e.path
            content = filepath.read_text(encoding="utf-8")
            result = interactive_fix(e.path, content)
            if result is not None:
                filepath.write_text(result, encoding="utf-8")
                print(f"  Saved {e.path}", file=sys.stderr)
                any_fixed = True
        if any_fixed:
            # Rebuild chapter list after fixes
            chapters = _build_chapter_list(base, manifest)
            print("  Chapter list rebuilt after fixes.", file=sys.stderr)

    # ── Resolve outputs ────────────────────────────────────────────────────────────
    outputs = _resolve_outputs(args, manifest)  # Verbose: show plan
    if args.verbose or args.dry_run:
        print(f"  title    {manifest.title}")
        print(f"  base     {base}")
        print(f"  chapters {len(chapters)}")
        for ch in chapters:
            num = f"Ch.{ch['num']}  " if ch['num'] is not None else "       "
            poetry = "  [poetry]" if ch['is_poetry'] else ""
            print(f"    {num}{ch['filename']}{poetry}")
        print(f"  outputs  {len(outputs)}")
        for fmt, dest in outputs:
            dest_str = str(_dest_for(fmt, dest, manifest, base, args.outdir)) if not args.dry_run else (str(dest) if dest else f"<manifest-dir>/{manifest.output_name or 'book'}.{fmt}")
            print(f"    {fmt:<6} {dest_str}")
        if args.dry_run:
            return 0

    # ── Render combined markdown once (reused by pdf/docx/md) ─────────────────
    rendered_md: str | None = None
    combined_path: Path | None = None

    fmts_needed = {fmt for fmt, _ in outputs}

    # Fix: "tw" removed — TiddlyWiki does not use rendered_md
    # Pass the pre-built chapters list to avoid re-running _build_chapter_list
    # (and re-emitting its warnings) for every format.
    if fmts_needed & {"pdf", "docx", "md"}:
        rendered_md = render_book(base, manifest, "md", _chapters=chapters)

    # ── Produce outputs ────────────────────────────────────────────────────────
    errors = 0

    for fmt, dest_arg in outputs:
        dest = _dest_for(fmt, dest_arg, manifest, base, args.outdir)

        if fmt == "md":
            assert rendered_md is not None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rendered_md, encoding="utf-8")
            print(f"{dest}")

        elif fmt == "html":
            rendered_html = render_book(base, manifest, "html", _chapters=chapters)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rendered_html, encoding="utf-8")
            print(f"{dest}")

        elif fmt == "tw":
            try:
                rendered_tw = render_book_tw(base, manifest, _chapters=chapters)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(rendered_tw, encoding="utf-8")
                print(f"{dest}")
            except FileNotFoundError as exc:
                print(f"bookcc: error: tw: {exc}", file=sys.stderr)
                errors += 1

        elif fmt in ("pdf", "docx"):
            assert rendered_md is not None
            if not shutil.which("pandoc"):
                _die("pandoc not found. Install: https://pandoc.org/installing.html")
            # Write combined.md to a temp location next to dest
            combined_path = dest.parent / "_bookcc_combined.md"
            combined_path.write_text(rendered_md, encoding="utf-8")
            cmd = ["pandoc", str(combined_path), "-o", str(dest)]
            if args.verbose:
                print(f"  run  {' '.join(cmd)}")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                if r.returncode == 0:
                    print(f"{dest}")
                else:
                    print(f"bookcc: error: {fmt}: {r.stderr.strip()[:200]}", file=sys.stderr)
                    errors += 1
            except subprocess.TimeoutExpired:
                print(f"bookcc: error: {fmt}: pandoc timed out (>180s)", file=sys.stderr)
                errors += 1
            finally:
                if combined_path and combined_path.exists():
                    combined_path.unlink()

    return errors


if __name__ == "__main__":
    sys.exit(main())
