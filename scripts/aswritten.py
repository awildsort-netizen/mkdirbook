#!/usr/bin/env python3
from __future__ import annotations

"""Review markdown/text diffs from HEAD with `.aswritten` support.

Workflow:
- load a directory-level `.aswritten` file
- refresh approximate line references for good fuzzy matches against HEAD
- offer to remove whitelist entries that no longer match anything
- walk changed markdown/text files line by line
- auto-revert lines already protected by accepted `.aswritten` rules
- prompt on the rest to either keep the current change or whitelist the HEAD
  version as written
"""

import argparse
import inspect
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Sequence, TypeVar


TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
ENTRY_RE = re.compile(r"^\s*(\d+)\s*:\s*(.*?)\{(.*?)\}(.*)\s*$")
TEXT_SUFFIXES = {".md", ".txt"}
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_RESET = "\033[0m"
T = TypeVar("T")


@dataclass
class Entry:
    source_line: int
    document_path: Path
    approx_line: int
    left_context: str
    glitch: str
    right_context: str


@dataclass
class MatchResult:
    entry: Entry
    candidate_line: int
    candidate_text: str
    line_score: float
    context_score: float
    glitch_score: float
    total_score: float
    accepted: bool


@dataclass
class WhitelistHit:
    line_number: int
    rule: str


class ReviewAbort(Exception):
    pass


def context_value(context: object, name: str) -> Any:
    """Resolve one named value from a mapping or context object."""
    if isinstance(context, Mapping):
        return context[name]
    return getattr(context, name)


def call_with_context(
    operator: Callable[..., T],
    context: object,
    /,
    **overrides: Any,
) -> T:
    """Call an operator with missing parameters embedded from context by name."""
    signature = inspect.signature(operator)
    kwargs: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            continue
        if name in overrides:
            kwargs[name] = overrides[name]
            continue
        try:
            kwargs[name] = context_value(context, name)
        except (AttributeError, KeyError):
            if parameter.default is inspect.Parameter.empty:
                raise TypeError(f"Missing required context value: {name}") from None
    return operator(**kwargs)


def tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall(text.lower())


def normalize(text: str) -> str:
    return " ".join(tokenize(text))


def entry_rule_text(entry: Entry) -> str:
    return (
        f"{entry.approx_line}: {entry.left_context}"
        f"{{{entry.glitch}}}{entry.right_context}"
    )


def resolve_document_reference(base_dir: Path, raw_path: str) -> Path | None:
    candidate = (base_dir / raw_path.strip()).resolve()
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def parse_entries(text: str, base_dir: Path) -> List[Entry]:
    entries: List[Entry] = []
    active_document: Path | None = None
    for source_line, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            header_target = resolve_document_reference(base_dir, line[1:].strip())
            if header_target is not None:
                active_document = header_target
            continue

        entry_text = raw_line
        first_word, _, remainder = line.partition(" ")
        loose_target = resolve_document_reference(base_dir, first_word)
        if loose_target is not None:
            active_document = loose_target
            if not remainder.strip():
                continue
            entry_text = remainder.lstrip()

        if active_document is None:
            raise ValueError(
                f"Entry on line {source_line} has no active filename: {raw_line!r}"
            )

        match = ENTRY_RE.match(entry_text)
        if not match:
            raise ValueError(
                f"Invalid .aswritten entry on line {source_line}: {entry_text!r}"
            )
        approx, left, glitch, right = match.groups()
        entries.append(
            Entry(
                source_line=source_line,
                document_path=active_document,
                approx_line=int(approx),
                left_context=left,
                glitch=glitch,
                right_context=right,
            )
        )
    return entries


def serialize_entries(entries: Sequence[Entry], base_dir: Path) -> str:
    if not entries:
        return ""
    chunks: list[str] = []
    current_document: Path | None = None
    for entry in entries:
        if entry.document_path != current_document:
            if chunks:
                chunks.append("")
            chunks.append(f"# {display_path(entry.document_path, base_dir)}")
            current_document = entry.document_path
        chunks.append(entry_rule_text(entry))
    return "\n".join(chunks) + "\n"


def load_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


def iter_candidate_indexes(
    line_count: int, approx_line: int, search_radius: int
) -> Iterable[int]:
    if line_count <= 0:
        return range(0)
    target = max(1, approx_line) - 1
    target = min(target, line_count - 1)
    start = max(0, target - search_radius)
    stop = min(line_count, target + search_radius + 1)
    return range(start, stop)


def line_component(delta_l: int, alpha: float) -> float:
    return math.exp(-alpha * delta_l)


def token_presence_score(
    token: str, candidate_index: int, lines: Sequence[str], context_radius: int
) -> float:
    best = 0.0
    start = max(0, candidate_index - context_radius)
    stop = min(len(lines), candidate_index + context_radius + 1)
    for index in range(start, stop):
        line_tokens = tokenize(lines[index])
        if token not in line_tokens:
            continue
        distance = abs(index - candidate_index)
        best = max(best, 1.0 / (1.0 + distance))
    return best


def context_component(
    entry: Entry, candidate_index: int, lines: Sequence[str], context_radius: int
) -> float:
    context_tokens = tokenize(f"{entry.left_context} {entry.right_context}")
    if not context_tokens:
        return 0.0
    scores = [
        token_presence_score(token, candidate_index, lines, context_radius)
        for token in context_tokens
    ]
    return sum(scores) / len(scores)


def token_overlap_score(a_tokens: Sequence[str], b_tokens: Sequence[str]) -> float:
    if not a_tokens:
        return 0.0
    a_set = set(a_tokens)
    b_set = set(b_tokens)
    if not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set)


def glitch_component(
    entry: Entry, candidate_index: int, lines: Sequence[str], context_radius: int
) -> float:
    candidate_text = lines[candidate_index]
    if entry.right_context == "":
        if entry.glitch == "":
            return 1.0 if candidate_text == entry.left_context else 0.0
        if entry.glitch.strip() == "":
            return (
                1.0
                if candidate_text == f"{entry.left_context}{entry.glitch}"
                else 0.0
            )

    glitch_text = normalize(entry.glitch)
    glitch_tokens = tokenize(entry.glitch)
    if not glitch_text:
        return 0.0

    start = max(0, candidate_index - context_radius)
    stop = min(len(lines), candidate_index + context_radius + 1)
    region_lines = list(lines[start:stop])
    region_text = normalize(" ".join(region_lines))

    if glitch_text and glitch_text in region_text:
        return 1.0

    best_similarity = 0.0
    best_overlap = 0.0
    for candidate_text in region_lines:
        normalized_candidate = normalize(candidate_text)
        if not normalized_candidate:
            continue
        similarity = SequenceMatcher(None, glitch_text, normalized_candidate).ratio()
        overlap = token_overlap_score(glitch_tokens, tokenize(candidate_text))
        best_similarity = max(best_similarity, similarity)
        best_overlap = max(best_overlap, overlap)

    return min(1.0, (0.65 * best_similarity) + (0.35 * best_overlap))


def score_entry(
    entry: Entry,
    lines: Sequence[str],
    alpha: float,
    weight_line: float,
    weight_context: float,
    weight_glitch: float,
    tau: float,
    search_radius: int,
    context_radius: int,
) -> MatchResult:
    if not lines:
        return MatchResult(
            entry=entry,
            candidate_line=0,
            candidate_text="",
            line_score=0.0,
            context_score=0.0,
            glitch_score=0.0,
            total_score=0.0,
            accepted=False,
        )
    best: MatchResult | None = None
    for candidate_index in iter_candidate_indexes(
        len(lines), entry.approx_line, search_radius
    ):
        delta_l = abs((candidate_index + 1) - entry.approx_line)
        line_score = line_component(delta_l, alpha)
        context_score = context_component(entry, candidate_index, lines, context_radius)
        glitch_score = glitch_component(entry, candidate_index, lines, context_radius)
        total = (
            (weight_line * line_score)
            + (weight_context * context_score)
            + (weight_glitch * glitch_score)
        )
        result = MatchResult(
            entry=entry,
            candidate_line=candidate_index + 1,
            candidate_text=lines[candidate_index],
            line_score=line_score,
            context_score=context_score,
            glitch_score=glitch_score,
            total_score=total,
            accepted=total >= tau,
        )
        if best is None or result.total_score > best.total_score:
            best = result
    assert best is not None
    return best


def default_whitelist_path(base_dir: Path) -> Path:
    return base_dir / ".aswritten"


def display_path(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def git_output(repo_root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def repo_root_for(path: Path) -> Path:
    return Path(git_output(path, "rev-parse", "--show-toplevel").strip()).resolve()


def changed_text_files(repo_root: Path, base_dir: Path) -> list[Path]:
    output = git_output(repo_root, "diff", "--name-only", "HEAD", "--")
    files: list[Path] = []
    for raw in output.splitlines():
        if not raw:
            continue
        path = (repo_root / raw).resolve()
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            relative = path.relative_to(base_dir)
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return files


def resolve_requested_files(base_dir: Path, requested: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for raw in requested:
        path = raw if raw.is_absolute() else (base_dir / raw)
        path = path.resolve()
        if not path.exists():
            raise RuntimeError(f"file not found: {raw}")
        if not path.is_file():
            raise RuntimeError(f"not a file: {raw}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise RuntimeError(f"not a markdown/text file: {raw}")
        try:
            relative = path.relative_to(base_dir)
        except ValueError as exc:
            raise RuntimeError(f"file outside base directory: {raw}") from exc
        if any(part.startswith(".") for part in relative.parts):
            raise RuntimeError(f"dotfiles are not reviewed by default: {raw}")
        files.append(path)
    return files


def read_head_lines(repo_root: Path, path: Path) -> list[str] | None:
    relative = path.relative_to(repo_root)
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"HEAD:{relative.as_posix()}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def read_worktree_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def prompt_choice(prompt: str, valid: set[str]) -> str:
    while True:
        choice = input(prompt).strip().lower()
        if choice in valid:
            return choice


def build_whitelist_hits(
    entries: Sequence[Entry],
    base_dir: Path,
    head_line_cache: dict[Path, list[str] | None],
    context: object,
) -> dict[Path, dict[int, WhitelistHit]]:
    hits: dict[Path, dict[int, WhitelistHit]] = {}
    for entry in entries:
        lines = head_line_cache.get(entry.document_path)
        if not lines:
            continue
        result = call_with_context(
            score_entry,
            context,
            entry=entry,
            lines=lines,
        )
        if not result.accepted:
            continue
        hits.setdefault(entry.document_path, {})[result.candidate_line] = WhitelistHit(
            line_number=result.candidate_line,
            rule=entry_rule_text(entry),
        )
    return hits


def refresh_whitelist_entries(
    entries: Sequence[Entry],
    base_dir: Path,
    head_line_cache: dict[Path, list[str] | None],
    context: object,
) -> list[Entry]:
    refreshed: list[Entry] = []
    for entry in entries:
        lines = head_line_cache.get(entry.document_path)
        if not lines:
            print(
                f"MISS file:{display_path(entry.document_path, base_dir)} "
                f"rule:{entry_rule_text(entry)}"
            )
            choice = prompt_choice("[r]emove reference, [k]eep reference, [q]uit: ", {"r", "k", "q"})
            if choice == "q":
                raise ReviewAbort()
            if choice == "k":
                refreshed.append(entry)
            continue

        result = call_with_context(
            score_entry,
            context,
            entry=entry,
            lines=lines,
        )
        if not result.accepted:
            print(
                f"MISS file:{display_path(entry.document_path, base_dir)} "
                f"rule:{entry_rule_text(entry)}"
            )
            print(f"  best line:{result.candidate_line} score:{result.total_score:.3f}")
            print(f"  best text:{result.candidate_text}")
            choice = prompt_choice("[r]emove reference, [k]eep reference, [q]uit: ", {"r", "k", "q"})
            if choice == "q":
                raise ReviewAbort()
            if choice == "k":
                refreshed.append(entry)
            continue

        updated_entry = Entry(
            source_line=entry.source_line,
            document_path=entry.document_path,
            approx_line=result.candidate_line,
            left_context=entry.left_context,
            glitch=entry.glitch,
            right_context=entry.right_context,
        )
        if result.candidate_line != entry.approx_line:
            print(
                f"UPDATE file:{display_path(entry.document_path, base_dir)} "
                f"line:{entry.approx_line}->{result.candidate_line} "
                f"score:{result.total_score:.3f}"
            )
        elif result.total_score < 0.999:
            print(
                f"REFRESH file:{display_path(entry.document_path, base_dir)} "
                f"line:{entry.approx_line} score:{result.total_score:.3f}"
            )
        refreshed.append(updated_entry)
    return refreshed


def write_whitelist(path: Path, entries: Sequence[Entry], base_dir: Path) -> None:
    text = serialize_entries(entries, base_dir)
    path.write_text(text, encoding="utf-8")


def derive_whitelist_rule(
    approx_line: int, head_line: str, current_line: str | None
) -> str:
    if current_line is None:
        return f"{approx_line}: {{{head_line}}}"

    matcher = SequenceMatcher(None, head_line, current_line)
    opcodes = matcher.get_opcodes()
    spans = [
        (i1, i2)
        for tag, i1, i2, _j1, _j2 in opcodes
        if tag in {"replace", "delete"} and i1 != i2
    ]
    if not spans:
        if (
            len(opcodes) == 2
            and opcodes[0][0] == "equal"
            and opcodes[0][1] == 0
            and opcodes[0][2] == len(head_line)
            and opcodes[1][0] == "insert"
            and opcodes[1][1] == len(head_line)
            and current_line[opcodes[1][3] : opcodes[1][4]].strip() == ""
        ):
            return f"{approx_line}: {head_line}{{}}"
        return f"{approx_line}: {{{head_line}}}"

    start = spans[0][0]
    end = spans[-1][1]
    left = head_line[:start]
    glitch = head_line[start:end]
    right = head_line[end:]
    return f"{approx_line}: {left}{{{glitch}}}{right}"


def append_whitelist_entry(
    entries: list[Entry],
    base_dir: Path,
    document_path: Path,
    rule: str,
) -> None:
    match = ENTRY_RE.match(rule)
    if not match:
        raise ValueError(f"Invalid generated whitelist rule: {rule!r}")
    approx, left, glitch, right = match.groups()
    entries.append(
        Entry(
            source_line=0,
            document_path=document_path,
            approx_line=int(approx),
            left_context=left,
            glitch=glitch,
            right_context=right,
        )
    )


def write_lines(path: Path, lines: Sequence[str]) -> None:
    text = "\n".join(lines)
    if lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def colorize(color: str, text: str) -> str:
    return f"{color}{text}{ANSI_RESET}"


def review_diff_file(
    base_dir: Path,
    path: Path,
    head_lines: list[str] | None,
    current_lines: list[str],
    whitelist_hits: dict[int, WhitelistHit],
    whitelist_path: Path,
    whitelist_entries: list[Entry],
) -> None:
    original_lines = list(current_lines)
    result_lines: list[str] = []
    matcher = SequenceMatcher(None, head_lines or [], current_lines)

    print(f"\n== {display_path(path, base_dir)} ==")

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            result_lines.extend(current_lines[j1:j2])
            continue

        old_block = (head_lines or [])[i1:i2]
        new_block = current_lines[j1:j2]
        max_len = max(len(old_block), len(new_block))

        for offset in range(max_len):
            old_line = old_block[offset] if offset < len(old_block) else None
            new_line = new_block[offset] if offset < len(new_block) else None
            old_line_number = i1 + offset + 1 if old_line is not None else None

            if old_line_number is not None and old_line_number in whitelist_hits:
                hit = whitelist_hits[old_line_number]
                print(
                    f"AUTO {display_path(path, base_dir)}:{old_line_number} "
                    f"restored HEAD via .aswritten rule: {hit.rule}"
                )
                if old_line is not None:
                    result_lines.append(old_line)
                continue

            line_label = (
                str(old_line_number)
                if old_line_number is not None
                else f"+{j1 + offset + 1}"
            )
            print(f"\n{display_path(path, base_dir)}:{line_label}")
            print(colorize(ANSI_RED, f"- {old_line if old_line is not None else '<no line>'}"))
            print(colorize(ANSI_GREEN, f"+ {new_line if new_line is not None else '<deleted>'}"))

            if old_line is None:
                choice = prompt_choice("[a]ccept insert, [r]eject insert, [q]uit: ", {"a", "r", "q"})
            else:
                choice = prompt_choice(
                    "[a]ccept change, [w]hitelist HEAD as written, [q]uit: ",
                    {"a", "w", "q"},
                )

            if choice == "q":
                raise ReviewAbort()
            if choice == "a":
                if new_line is not None:
                    result_lines.append(new_line)
                continue
            if choice == "r":
                continue
            if choice == "w":
                assert old_line is not None and old_line_number is not None
                rule = derive_whitelist_rule(old_line_number, old_line, new_line)
                append_whitelist_entry(
                    whitelist_entries,
                    base_dir=base_dir,
                    document_path=path,
                    rule=rule,
                )
                write_whitelist(whitelist_path, whitelist_entries, base_dir)
                print(f"WHITELIST {display_path(path, base_dir)}:{old_line_number} -> {rule}")
                result_lines.append(old_line)
                continue

    if result_lines != original_lines:
        write_lines(path, result_lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aswritten",
        description="Review markdown/text diffs from HEAD with .aswritten support.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Specific files to review (default: all changed non-dotfiles)",
    )
    parser.add_argument(
        "--whitelist",
        type=Path,
        help="Whitelist file (default: <base-dir>/.aswritten)",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path("."),
        help="Base directory for file resolution and .aswritten (default: cwd)",
    )
    parser.add_argument("--alpha", type=float, default=0.35, help="Line decay factor")
    parser.add_argument("--weight-line", type=float, default=0.2)
    parser.add_argument("--weight-context", type=float, default=0.3)
    parser.add_argument("--weight-glitch", type=float, default=0.5)
    parser.add_argument("--tau", type=float, default=0.65)
    parser.add_argument("--search-radius", type=int, default=8)
    parser.add_argument("--context-radius", type=int, default=2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    base_dir = args.base_dir.resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        print(f"aswritten: directory not found: {base_dir}", file=sys.stderr)
        return 1

    repo_root = repo_root_for(base_dir)
    whitelist_path = (
        args.whitelist.resolve()
        if args.whitelist is not None
        else default_whitelist_path(base_dir)
    )

    try:
        requested_files = resolve_requested_files(base_dir, args.files) if args.files else []
    except RuntimeError as exc:
        print(f"aswritten: {exc}", file=sys.stderr)
        return 1

    changed_files = changed_text_files(repo_root, base_dir)
    if requested_files:
        changed_set = set(changed_files)
        files = [path for path in requested_files if path in changed_set]
        skipped = [path for path in requested_files if path not in changed_set]
        for path in skipped:
            print(f"aswritten: skipping unchanged file {display_path(path, base_dir)}")
    else:
        files = changed_files

    whitelist_entries: list[Entry] = []
    if whitelist_path.exists():
        try:
            whitelist_entries = parse_entries(
                whitelist_path.read_text(encoding="utf-8"), base_dir=base_dir
            )
        except ValueError as exc:
            print(f"aswritten: {exc}", file=sys.stderr)
            return 1

    referenced_files = {entry.document_path for entry in whitelist_entries}
    head_line_cache = {
        path: read_head_lines(repo_root, path) for path in set(files) | referenced_files
    }

    try:
        refreshed_entries = refresh_whitelist_entries(
            entries=whitelist_entries,
            base_dir=base_dir,
            head_line_cache=head_line_cache,
            context=args,
        )
    except ReviewAbort:
        print("aswritten: stopped by user")
        return 1

    if refreshed_entries != whitelist_entries or (whitelist_path.exists() and not refreshed_entries):
        write_whitelist(whitelist_path, refreshed_entries, base_dir)
    whitelist_entries = refreshed_entries

    if not files:
        print("aswritten: no changed markdown/text files against HEAD")
        return 0

    whitelist_hits = build_whitelist_hits(
        entries=whitelist_entries,
        base_dir=base_dir,
        head_line_cache=head_line_cache,
        context=args,
    )

    try:
        for path in files:
            review_diff_file(
                base_dir=base_dir,
                path=path,
                head_lines=head_line_cache[path],
                current_lines=read_worktree_lines(path),
                whitelist_hits=whitelist_hits.get(path, {}),
                whitelist_path=whitelist_path,
                whitelist_entries=whitelist_entries,
            )
    except ReviewAbort:
        print("aswritten: stopped by user")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
