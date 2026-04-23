#!/usr/bin/env python3
"""Rolling-window statistical poetry detector for mkdirbook.

Analyses hard line-broken text (Markdown convention: two trailing spaces =
forced line break) and classifies windows of lines as poetry-like or
prose-like using a composite score derived from:

  V_t  — line-length variability  (MAD-based, robust to outliers)
  S_t  — short-line ratio         (fraction of lines shorter than β·μ)
  T_t  — tail regularity          (prose concentrates shortness at paragraph ends)

Poetry score:  P_t = w_v·V_t + w_s·S_t − w_t·T_t

Public API
----------
detect_poetry(text)          → bool
    Whole-document classification (True = poetry-like).

poetry_scores(text)          → list[WindowScore]
    Per-window scores with metadata.

analyse_line_breaks(text)    → list[LineBreakWarning]
    Warnings about missing/unnecessary trailing double spaces.

fix_line_breaks(text, fixes) → str
    Apply a list of LineBreakFix actions and return corrected text.
"""
from __future__ import annotations

import re
import statistics
import sys
from dataclasses import dataclass, field
from typing import Sequence

# ── Configuration ─────────────────────────────────────────────────────────────

WINDOW_SIZE: int = 80          # L — lines per window
STRIDE: int = 15               # s — step between windows (in [10, 20])
BETA: float = 0.7              # short-line threshold factor
THRESHOLD: float = 0.35        # P_t ≥ τ → poetry

# Score weights
W_V: float = 0.45              # variability weight
W_S: float = 0.35              # short-line ratio weight
W_T: float = 0.30              # tail-regularity penalty weight

# Normalisation caps (used to map raw values into [0, 1])
_MAD_CAP: float = 40.0         # MAD values above this → 1.0
_TAIL_CAP: float = 5.0         # inverse-variance cap

# Prose guard: if mean line length exceeds this, the window is almost
# certainly prose (long paragraphs soft-wrapped or unwrapped).
_PROSE_MEAN_CAP: float = 120.0

# Markdown structural lines to skip in statistics (headings, lists, fences…)
_SKIP_RE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|\s{4})")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class WindowScore:
    """Score and features for a single rolling window."""
    start_line: int          # 0-based index of the first line in the window
    end_line: int            # exclusive
    mean_length: float
    mad: float               # median absolute deviation of line lengths
    short_ratio: float       # ρ_t
    tail_regularity: float   # T_t (higher → more prose-like)
    variability: float       # V_t (normalised)
    score: float             # P_t (composite poetry score)
    is_poetry: bool          # P_t ≥ threshold


@dataclass
class LineBreakWarning:
    """A warning about a line-break issue in the source text."""
    line_no: int             # 1-based
    kind: str                # "missing_break" or "unnecessary_break"
    context: str             # the offending line (trimmed)
    message: str             # human-readable explanation


@dataclass
class LineBreakFix:
    """An actionable fix for a line-break issue."""
    line_no: int             # 1-based
    kind: str                # "add_break" or "remove_break"
    original: str
    fixed: str


@dataclass
class InteractiveFixResult:
    """Result of an interactive line-break fixing session."""
    text: str | None
    quit_requested: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _effective_length(line: str) -> int:
    """Return the visual length of *line*, stripping trailing whitespace
    except the two-space break marker (which is not visual content)."""
    return len(line.rstrip())


def _is_hard_break(line: str) -> bool:
    """True if *line* ends with exactly two trailing spaces (Markdown hard break)."""
    stripped = line.rstrip("\n\r")
    return stripped.endswith("  ") and not stripped.endswith("   ")


def _has_two_trailing_spaces(line: str) -> bool:
    """True if *line* ends with at least two spaces (before any newline)."""
    stripped = line.rstrip("\n\r")
    return len(stripped) >= 2 and stripped[-1] == " " and stripped[-2] == " "


def _parse_lines(text: str) -> list[str]:
    """Split text into lines, preserving trailing spaces."""
    return text.split("\n")


def _looks_like_explicit_verse_block(lines: Sequence[str]) -> bool:
    """True when a paragraph already uses hard breaks like verse.

    This treats explicit trailing double-spaces as an author signal. When most
    non-final lines in a block already use them, prefer verse handling even if
    the statistical classifier would call the block prose.
    """
    usable = [line for line in lines if line.strip() and not _SKIP_RE.match(line)]
    if len(usable) < 3:
        return False
    candidates = usable[:-1]
    if len(candidates) < 2:
        return False
    explicit = sum(1 for line in candidates if _has_two_trailing_spaces(line))
    return explicit >= 2 and explicit * 2 >= len(candidates)


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ── Window statistics ─────────────────────────────────────────────────────────

def _window_stats(lengths: Sequence[int]) -> tuple[float, float, float, float]:
    """Compute (mean, MAD, short_ratio, tail_regularity) for a window of line lengths.

    *lengths* must contain only non-empty, non-structural lines.
    Returns (μ, MAD, ρ, T).
    """
    n = len(lengths)
    if n < 3:
        return 0.0, 0.0, 0.0, 0.0

    mu = statistics.mean(lengths)
    med = statistics.median(lengths)
    mad = statistics.median(abs(l - med) for l in lengths)

    # Short-line ratio
    threshold = BETA * mu
    short_count = sum(1 for l in lengths if l < threshold)
    rho = short_count / n

    return mu, mad, rho, 0.0  # tail_regularity computed separately


def _paragraph_tail_regularity(lines: list[str]) -> float:
    """Compute tail regularity T_t for a set of lines.

    Partition into paragraphs via blank lines.  For each paragraph with ≥2
    non-empty lines, compute τ_p = ℓ_last / μ_body.  T_t is the inverse
    variance of {τ_p} (high → prose-like, shortness concentrated at ends).
    """
    paragraphs: list[list[int]] = []
    current: list[int] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(current)
                current = []
        else:
            if not _SKIP_RE.match(line):
                current.append(_effective_length(line))
    if current:
        paragraphs.append(current)

    tau_values: list[float] = []
    for para in paragraphs:
        if len(para) < 2:
            continue
        body = para[:-1]
        last = para[-1]
        mu_body = statistics.mean(body)
        if mu_body > 0:
            tau_values.append(last / mu_body)

    if len(tau_values) < 2:
        return 0.0

    var_tau = statistics.variance(tau_values)
    if var_tau < 1e-9:
        return _TAIL_CAP  # perfectly regular → maximum prose signal

    inv_var = 1.0 / var_tau
    return min(inv_var, _TAIL_CAP)


# ── Core scoring ──────────────────────────────────────────────────────────────

def _score_window(lines: list[str]) -> WindowScore | None:
    """Score a single window of lines.  Returns None if too few usable lines."""
    # Filter to non-empty, non-structural lines for length stats
    usable: list[int] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not _SKIP_RE.match(line):
            usable.append(_effective_length(line))

    if len(usable) < 5:
        return None

    mu, mad, rho, _ = _window_stats(usable)
    tail_reg = _paragraph_tail_regularity(lines)

    # Normalise features to [0, 1]
    v_t = _clamp(mad / _MAD_CAP)
    s_t = rho  # already in [0, 1]
    t_t = _clamp(tail_reg / _TAIL_CAP)

    # Prose guard: very long mean line length → force prose classification.
    # Prose with unwrapped paragraphs can have high MAD (one short heading
    # among 300-char paragraphs) but is clearly not poetry.
    if mu > _PROSE_MEAN_CAP:
        v_t = 0.0
        s_t = 0.0

    # Composite score
    p_t = W_V * v_t + W_S * s_t - W_T * t_t

    return WindowScore(
        start_line=0,  # caller fills in actual indices
        end_line=0,
        mean_length=mu,
        mad=mad,
        short_ratio=rho,
        tail_regularity=tail_reg,
        variability=v_t,
        score=p_t,
        is_poetry=(p_t >= THRESHOLD),
    )


def poetry_scores(text: str) -> list[WindowScore]:
    """Compute per-window poetry scores over the full text.

    Returns a list of :class:`WindowScore` objects, one per window.
    For texts shorter than WINDOW_SIZE lines, a single window covering
    the entire text is returned.
    """
    all_lines = _parse_lines(text)
    n = len(all_lines)

    if n == 0:
        return []

    # For short texts, use a single window
    effective_window = min(WINDOW_SIZE, n)
    results: list[WindowScore] = []

    start = 0
    while start + effective_window <= n or start == 0:
        end = min(start + effective_window, n)
        window_lines = all_lines[start:end]
        ws = _score_window(window_lines)
        if ws is not None:
            ws.start_line = start
            ws.end_line = end
            results.append(ws)
        if end >= n:
            break
        start += STRIDE

    return results


def detect_poetry(text: str, force: bool = False) -> bool:
    """Whole-document poetry classification.

    Returns True if the majority of windows are classified as poetry,
    or if *force* is True (explicit poetry role in manifest).

    For short texts (fewer lines than WINDOW_SIZE), falls back to a
    single-window analysis.
    """
    if force:
        return True

    scores = poetry_scores(text)
    if not scores:
        return False

    poetry_count = sum(1 for ws in scores if ws.is_poetry)
    return poetry_count > len(scores) / 2


def smoothed_score(scores: list[WindowScore], radius: int = 1) -> list[float]:
    """Return locally averaged P_t values across adjacent windows.

    *radius* controls how many neighbours on each side to include.
    """
    n = len(scores)
    if n == 0:
        return []
    result: list[float] = []
    for i in range(n):
        lo = max(0, i - radius)
        hi = min(n, i + radius + 1)
        avg = statistics.mean(s.score for s in scores[lo:hi])
        result.append(avg)
    return result


# ── Line-break warnings ──────────────────────────────────────────────────────

def analyse_line_breaks(text: str) -> list[LineBreakWarning]:
    """Analyse the text for line-break issues and return warnings.

    Two kinds of warnings:
    1. **missing_break** — poetry lines that don't end with two trailing spaces.
       Many Markdown viewers require exactly two trailing spaces to recognise
       a forced line break.
    2. **unnecessary_break** — prose paragraph lines that have trailing spaces
       where none are needed (prose paragraphs don't use hard line breaks).
    """
    all_lines = _parse_lines(text)
    scores = poetry_scores(text)
    warnings: list[LineBreakWarning] = []

    if not scores:
        return warnings

    # Build a per-line classification: is this line in a poetry window?
    line_poetry: list[bool] = [False] * len(all_lines)
    for ws in scores:
        if ws.is_poetry:
            for i in range(ws.start_line, ws.end_line):
                line_poetry[i] = True

    start = 0
    while start < len(all_lines):
        end = start
        while end < len(all_lines) and all_lines[end].strip():
            end += 1
        if _looks_like_explicit_verse_block(all_lines[start:end]):
            for i in range(start, end):
                if all_lines[i].strip() and not _SKIP_RE.match(all_lines[i]):
                    line_poetry[i] = True
        start = end + 1

    for i, line in enumerate(all_lines):
        stripped = line.strip()
        if not stripped:
            continue  # skip blank lines
        if _SKIP_RE.match(line):
            continue  # skip structural lines

        has_trailing = _has_two_trailing_spaces(line)

        if line_poetry[i]:
            # Poetry line: should have two trailing spaces
            if not has_trailing:
                # Check if this is the last non-empty line of its paragraph
                # (paragraph-final lines don't need hard breaks)
                is_para_final = _is_paragraph_final(all_lines, i)
                if not is_para_final:
                    warnings.append(LineBreakWarning(
                        line_no=i + 1,
                        kind="missing_break",
                        context=stripped[:80],
                        message=(
                            f"Line {i+1}: poetry line missing two trailing spaces. "
                            "Many Markdown viewers require exactly two trailing "
                            "spaces to recognise a forced line break."
                        ),
                    ))
        else:
            # Prose line: should NOT have trailing spaces
            if has_trailing:
                warnings.append(LineBreakWarning(
                    line_no=i + 1,
                    kind="unnecessary_break",
                    context=stripped[:80],
                    message=(
                        f"Line {i+1}: prose line has trailing spaces. "
                        "Prose paragraphs typically don't use hard line breaks; "
                        "consider removing the trailing spaces."
                    ),
                ))

    return warnings


def _is_paragraph_final(lines: list[str], idx: int) -> bool:
    """Return True if lines[idx] is the last non-empty line before a blank
    line or end-of-text."""
    n = len(lines)
    # Look ahead for the next non-empty line
    for j in range(idx + 1, n):
        if not lines[j].strip():
            return True  # blank line follows → this is paragraph-final
        return False  # another non-empty line follows → not final
    return True  # end of text


# ── Line-break fixes ─────────────────────────────────────────────────────────

def generate_fixes(text: str) -> list[LineBreakFix]:
    """Generate actionable fixes for all line-break warnings."""
    warnings = analyse_line_breaks(text)
    all_lines = _parse_lines(text)
    fixes: list[LineBreakFix] = []

    for w in warnings:
        idx = w.line_no - 1
        original = all_lines[idx]

        if w.kind == "missing_break":
            # Add two trailing spaces before the newline
            fixed = original.rstrip() + "  "
            fixes.append(LineBreakFix(
                line_no=w.line_no,
                kind="add_break",
                original=original,
                fixed=fixed,
            ))
        elif w.kind == "unnecessary_break":
            # Remove trailing spaces
            fixed = original.rstrip()
            fixes.append(LineBreakFix(
                line_no=w.line_no,
                kind="remove_break",
                original=original,
                fixed=fixed,
            ))

    return fixes


def fix_line_breaks(text: str, fixes: list[LineBreakFix]) -> str:
    """Apply a list of LineBreakFix actions and return corrected text.

    Fixes are applied by line number; conflicting fixes on the same line
    are resolved by last-wins.
    """
    all_lines = _parse_lines(text)
    fix_map: dict[int, str] = {}
    for f in fixes:
        fix_map[f.line_no - 1] = f.fixed

    result = []
    for i, line in enumerate(all_lines):
        if i in fix_map:
            result.append(fix_map[i])
        else:
            result.append(line)

    return "\n".join(result)


# ── Process poetry breaks (replaces old _process_poetry_breaks) ───────────────

def process_poetry_breaks(content: str, line_break: str = "  ",
                          force: bool = False) -> str:
    """Add line breaks to poetry-like blocks within Markdown content.

    Uses the rolling-window statistical detector to classify blocks.
    Blocks that already contain explicit hard breaks are also treated as verse.
    When *force* is True every block is treated as poetry regardless of the
    detector (used when the file role is explicitly "poetry").

    The last non-empty line of each paragraph never gets a trailing hard
    break because the following paragraph separator already provides it.
    """
    blocks = re.split(r"\n\n+", content)
    result: list[str] = []

    for block in blocks:
        lines = block.splitlines()
        # Determine if this block is poetry
        if force or _looks_like_explicit_verse_block(lines) or detect_poetry(block, force=False):
            processed: list[str] = []
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


# ── Interactive fix mode ──────────────────────────────────────────────────────

def interactive_fix(filepath: str, text: str) -> InteractiveFixResult:
    """Interactively prompt the user to fix line-break issues.

    Returns the corrected text, or None if no changes were made.
    If quit_requested is True, the caller should stop interactive processing.
    Prints to stdout/stderr and reads from stdin.
    """
    fixes = generate_fixes(text)
    if not fixes:
        return InteractiveFixResult(text=None)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  Interactive fix: {filepath}", file=sys.stderr)
    print(f"  {len(fixes)} line-break issue(s) found", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    accepted: list[LineBreakFix] = []

    abort = False
    for i, fix in enumerate(fixes, 1):
        kind_label = "ADD two trailing spaces" if fix.kind == "add_break" else "REMOVE trailing spaces"
        print(f"[{i}/{len(fixes)}] Line {fix.line_no}: {kind_label}", file=sys.stderr)
        print(f"  Current:  {fix.original!r}", file=sys.stderr)
        print(f"  Proposed: {fix.fixed!r}", file=sys.stderr)

        choice = None
        while True:
            try:
                choice = input("  Apply? [y]es / [s]kip / [a]ll / [q]uit: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Quitting interactive mode.", file=sys.stderr)
                abort = True
                choice = "q"
                break

            if choice in ("y", "yes"):
                accepted.append(fix)
                break
            elif choice in ("s", "skip", "n", "no"):
                break
            elif choice in ("a", "all"):
                accepted.extend(fixes[i - 1:])
                print(f"  Applying all remaining {len(fixes) - i + 1} fixes.", file=sys.stderr)
                break
            elif choice in ("q", "quit"):
                print("  Quitting interactive mode.", file=sys.stderr)
                updated_text = fix_line_breaks(text, accepted) if accepted else None
                return InteractiveFixResult(
                    text=updated_text,
                    quit_requested=True,
                )
            else:
                print("  Please enter y, s, a, or q.", file=sys.stderr)

        if abort or choice in ("a", "all"):
            break

    if not accepted:
        print("  No changes applied.", file=sys.stderr)
        return InteractiveFixResult(text=None, quit_requested=abort)

    print(f"\n  Applied {len(accepted)} fix(es).\n", file=sys.stderr)
    return InteractiveFixResult(
        text=fix_line_breaks(text, accepted),
        quit_requested=abort,
    )


# ── CLI entry point (standalone testing) ──────────────────────────────────────

def _cli_main() -> None:
    """Simple CLI for testing the poetry detector on files."""
    import argparse

    p = argparse.ArgumentParser(
        prog="poetry",
        description="Rolling-window statistical poetry detector.",
    )
    p.add_argument("files", nargs="+", help="Markdown files to analyse")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Show per-window scores")
    p.add_argument("-w", "--warnings", action="store_true",
                   help="Show line-break warnings")
    p.add_argument("-i", "-I", "--interactive", action="store_true",
                    help="Interactive fix mode")
    args = p.parse_args()

    detailed_output = args.verbose or args.warnings or args.interactive

    for filepath in args.files:
        text = open(filepath, encoding="utf-8").read()
        is_poem = detect_poetry(text)
        scores = poetry_scores(text)

        if not detailed_output:
            if is_poem:
                print(filepath)
            continue

        label = "POETRY" if is_poem else "PROSE"
        print(f"\n{filepath}: {label}")
        if scores:
            avg = statistics.mean(s.score for s in scores)
            print(f"  windows: {len(scores)}, avg score: {avg:.3f}, "
                  f"threshold: {THRESHOLD}")

        if args.verbose and scores:
            for ws in scores:
                tag = "P" if ws.is_poetry else "."
                print(f"  [{tag}] lines {ws.start_line+1}-{ws.end_line}: "
                      f"score={ws.score:.3f} V={ws.variability:.3f} "
                      f"S={ws.short_ratio:.3f} T={ws.tail_regularity:.3f} "
                      f"MAD={ws.mad:.1f} μ={ws.mean_length:.1f}")

        if args.warnings:
            warnings = analyse_line_breaks(text)
            for w in warnings:
                print(f"  {w.message}")

        if args.interactive:
            result = interactive_fix(filepath, text)
            if result.text is not None:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(result.text)
                print(f"  Saved {filepath}")
            if result.quit_requested:
                break


if __name__ == "__main__":
    _cli_main()
