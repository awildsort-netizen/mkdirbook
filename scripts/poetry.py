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

import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# ── Configuration ─────────────────────────────────────────────────────────────

WINDOW_SIZE: int = 80          # L — lines per window
STRIDE: int = 15               # s — step between windows (in [10, 20])
BETA: float = 0.7              # short-line threshold factor
THRESHOLD: float = 0.35        # P_t ≥ τ → poetry

# Score weights
W_V: float = 0.45              # variability weight
W_S: float = 0.35              # short-line ratio weight
W_T: float = 0.0               # disabled: stanza-final consistency is common in this corpus

# Normalisation caps (used to map raw values into [0, 1])
_MAD_CAP: float = 40.0         # MAD values above this → 1.0
_TAIL_CAP: float = 5.0         # inverse-variance cap

# Prose guard: if mean line length exceeds this, the window is almost
# certainly prose (long paragraphs soft-wrapped or unwrapped).
_PROSE_MEAN_CAP: float = 120.0

# Sequence-aware line-break regression classifier configuration.
_STRONG_PUNCT_RE = re.compile(r"[.?!:;][\"'”’)]*$")
_OPEN_CONTINUATION_RE = re.compile(r"(?:[,—–-]|\(|\[|\{|[\"'“‘])\s*$")
_LOWERCASE_START_RE = re.compile(r"^[\s\"'“‘(\[]*[a-z]")
_CAPITAL_START_RE = re.compile(r"^[\s\"'“‘(\[]*[A-Z]")
_CONTINUATION_START_RE = re.compile(
    r"^[\s\"'“‘(\[]*(?:and|or|but|nor|for|so|yet|as|if|when|while|because|"
    r"though|although|unless|until|with|without|within|of|to|from|in|on|at|by|"
    r"through|under|over|into|onto|than|that|which|who|whose|whom|where)\b",
    re.IGNORECASE,
)
_LINE_BREAK_REGRESSION_WEIGHTS: dict[str, float] = {
    "bias": -2.25,
    "has_existing_break": 2.75,
    "current_no_strong_punct": 1.25,
    "next_starts_lowercase": 1.85,
    "syntactic_continuation": 1.55,
    "length_deviation": 1.35,
    "short_line": 0.90,
    "paragraph_jagged": 1.10,
    "explicit_verse_block": 1.60,
    "prev_candidate": 0.65,
    "next_candidate": 0.65,
    "prev_add_prediction": 0.85,
    "current_strong_punct": -2.80,
    "next_capital_reset": -1.60,
    "uniform_prose": -3.20,
}
_LINE_BREAK_PROBABILITY_THRESHOLD = 0.50
_TRAILING_SCORE_FILENAME = ".trailing.sco"
_TRAILING_SCORE_VERSION = 1
_REGRESSION_LEARNING_RATE = 0.25
_REGRESSION_L2 = 0.01
_REGRESSION_EPOCHS = 1200
_REGRESSION_TRAINING_EXCLUDE = {"has_existing_break"}

# Terminal colour escapes for CLI diagnostics.
_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_RED = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_CYAN = "\033[36m"
_ANSI_MAGENTA = "\033[35m"

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


@dataclass
class LineBreakFeatures:
    """Feature vector for the line-break regression classifier."""
    has_existing_break: float = 0.0
    current_no_strong_punct: float = 0.0
    next_starts_lowercase: float = 0.0
    syntactic_continuation: float = 0.0
    length_deviation: float = 0.0
    short_line: float = 0.0
    paragraph_jagged: float = 0.0
    explicit_verse_block: float = 0.0
    prev_candidate: float = 0.0
    next_candidate: float = 0.0
    prev_add_prediction: float = 0.0
    current_strong_punct: float = 0.0
    next_capital_reset: float = 0.0
    uniform_prose: float = 0.0


@dataclass
class LineBreakPrediction:
    """Regression classifier output for one line."""
    action: str
    probability: float
    logit: float
    features: LineBreakFeatures


@dataclass
class LineBreakTrainingRow:
    """Persisted labelled row for trailing-space regression data."""
    path: str
    line_no: int
    label: int
    prediction: LineBreakPrediction
    text: str
    next_text: str


@dataclass
class LineBreakRegressionModel:
    """Trained logistic regression parameters for line-break classification."""
    weights: dict[str, float]
    threshold: float = _LINE_BREAK_PROBABILITY_THRESHOLD


# ── Helpers ───────────────────────────────────────────────────────────────────

def _effective_length(line: str) -> int:
    """Return the visual length of *line*, stripping trailing whitespace
    except the two-space break marker (which is not visual content)."""
    return len(line.rstrip())


def _parse_lines(text: str) -> list[str]:
    """Split text into lines, preserving trailing spaces."""
    return text.split("\n")


def _line_core(line: str) -> str:
    """Return *line* without trailing whitespace or line-ending characters."""
    return line.rstrip("\n\r").rstrip()


def _ends_with_strong_punctuation(line: str) -> bool:
    """True if *line* ends in punctuation that usually closes a sentence."""
    return bool(_STRONG_PUNCT_RE.search(_line_core(line)))


def _starts_lowercase(line: str) -> bool:
    """True if the first lexical character of *line* is lowercase."""
    return bool(_LOWERCASE_START_RE.match(line.lstrip()))


def _starts_capitalized(line: str) -> bool:
    """True if the first lexical character of *line* is uppercase."""
    return bool(_CAPITAL_START_RE.match(line.lstrip()))


def _continues_syntactically(current: str, next_line: str) -> bool:
    """Heuristic for whether *next_line* continues *current* syntactically."""
    current_core = _line_core(current)
    next_core = next_line.strip()
    if not current_core or not next_core:
        return False
    if _OPEN_CONTINUATION_RE.search(current_core):
        return True
    if _CONTINUATION_START_RE.match(next_core):
        return True
    return not _ends_with_strong_punctuation(current) and _starts_lowercase(next_line)


def _has_two_trailing_spaces(line: str) -> bool:
    """True if *line* ends with at least two spaces before any line ending."""
    return line.rstrip("\n\r").endswith("  ")


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


def _paragraph_spans(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans for contiguous non-blank paragraphs."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip():
            if start is None:
                start = i
        elif start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def _paragraph_is_uniform_prose(lengths: Sequence[int]) -> bool:
    """True when line lengths look like mechanical prose wrapping."""
    if len(lengths) < 4:
        return False
    mean_length = statistics.mean(lengths)
    if mean_length <= 0:
        return False
    mad = statistics.median(abs(length - statistics.median(lengths)) for length in lengths)
    length_range = max(lengths) - min(lengths)
    return mad / mean_length <= 0.12 and length_range / mean_length <= 0.45


def _line_break_candidate(
    paragraph_lines: Sequence[str],
    index: int,
    mean_length: float,
    jagged: bool,
) -> bool:
    """Loose candidate test used for sequence-aware neighbour support."""
    if index >= len(paragraph_lines) - 1:
        return False
    current = paragraph_lines[index]
    next_line = paragraph_lines[index + 1]
    if not current.strip() or _SKIP_RE.match(current):
        return False
    if not next_line.strip() or _ends_with_strong_punctuation(current):
        return False
    length = _effective_length(current)
    deviates = mean_length > 0 and abs(length - mean_length) / mean_length >= 0.25
    return _starts_lowercase(next_line) or _continues_syntactically(current, next_line) or deviates or jagged


def _sigmoid(value: float) -> float:
    """Numerically stable logistic transform."""
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _line_break_features(
    paragraph_lines: Sequence[str],
    index: int,
    mean_length: float,
    jagged: bool,
    uniform_prose: bool,
    explicit_verse: bool,
    candidates: Sequence[bool],
    prev_add_prediction: bool,
) -> LineBreakFeatures:
    """Extract the regression feature vector for ``line[index]``.

    The vector is local-sequence aware: it uses the current line, the next line,
    adjacent candidate states, and the previous ADD_SPACES prediction.
    """
    current = paragraph_lines[index]
    next_line = paragraph_lines[index + 1]
    length = _effective_length(current)
    deviation = abs(length - mean_length) / mean_length if mean_length > 0 else 0.0
    strong_punct = _ends_with_strong_punctuation(current)
    syntactic_continuation = _continues_syntactically(current, next_line)
    next_capital_reset = _starts_capitalized(next_line) and not syntactic_continuation

    return LineBreakFeatures(
        has_existing_break=float(_has_two_trailing_spaces(current)),
        current_no_strong_punct=float(not strong_punct),
        next_starts_lowercase=float(_starts_lowercase(next_line)),
        syntactic_continuation=float(syntactic_continuation),
        length_deviation=min(deviation, 1.0),
        short_line=float(mean_length > 0 and length < mean_length * 0.70),
        paragraph_jagged=float(jagged),
        explicit_verse_block=float(explicit_verse),
        prev_candidate=float(index > 0 and candidates[index - 1]),
        next_candidate=float(index + 1 < len(candidates) and candidates[index + 1]),
        prev_add_prediction=float(prev_add_prediction),
        current_strong_punct=float(strong_punct),
        next_capital_reset=float(next_capital_reset),
        uniform_prose=float(uniform_prose and not explicit_verse),
    )


def _predict_line_break(
    features: LineBreakFeatures,
    model: LineBreakRegressionModel | None = None,
) -> LineBreakPrediction:
    """Run the logistic regression classifier."""
    weights = model.weights if model else _LINE_BREAK_REGRESSION_WEIGHTS
    threshold = model.threshold if model else _LINE_BREAK_PROBABILITY_THRESHOLD
    logit = weights.get("bias", 0.0)
    for name, value in features.__dict__.items():
        logit += weights.get(name, 0.0) * value
    probability = _sigmoid(logit)
    action = "ADD_SPACES" if probability >= threshold else "LEAVE"
    return LineBreakPrediction(
        action=action,
        probability=probability,
        logit=logit,
        features=features,
    )


def classify_line_break_predictions(
    lines: Sequence[str],
    model: LineBreakRegressionModel | None = None,
) -> list[LineBreakPrediction]:
    """Return regression classifier predictions for each raw input line."""
    default_features = LineBreakFeatures()
    predictions = [
        LineBreakPrediction("LEAVE", 0.0, float("-inf"), default_features)
        for _ in lines
    ]

    for start, end in _paragraph_spans(lines):
        paragraph = list(lines[start:end])
        usable_lengths = [
            _effective_length(line)
            for line in paragraph
            if line.strip() and not _SKIP_RE.match(line)
        ]
        if len(usable_lengths) < 2:
            continue

        mean_length = statistics.mean(usable_lengths)
        median_length = statistics.median(usable_lengths)
        mad = statistics.median(abs(length - median_length) for length in usable_lengths)
        jagged = mean_length > 0 and mad / mean_length >= 0.18
        uniform_prose = _paragraph_is_uniform_prose(usable_lengths)
        explicit_verse = _looks_like_explicit_verse_block(paragraph)
        candidates = [
            _line_break_candidate(paragraph, idx, mean_length, jagged)
            for idx in range(len(paragraph))
        ]

        prev_add_prediction = False
        for local_idx, current in enumerate(paragraph):
            global_idx = start + local_idx
            next_idx = global_idx + 1

            # Eligibility: only consider line i when line[i+1].strip() != "".
            if next_idx >= len(lines) or lines[next_idx].strip() == "":
                prev_add_prediction = False
                continue
            if not current.strip() or _SKIP_RE.match(current):
                prev_add_prediction = False
                continue

            features = _line_break_features(
                paragraph,
                local_idx,
                mean_length,
                jagged,
                uniform_prose,
                explicit_verse,
                candidates,
                prev_add_prediction,
            )
            prediction = _predict_line_break(features, model)
            predictions[global_idx] = prediction
            prev_add_prediction = prediction.action == "ADD_SPACES"

    return predictions


def classify_line_break_actions(
    lines: Sequence[str],
    model: LineBreakRegressionModel | None = None,
) -> list[str]:
    """Classify raw lines as ``ADD_SPACES`` or ``LEAVE``.

    This is a fixed-coefficient logistic regression sequence classifier. It
    extracts a feature vector from the local window ``(prev, current, next)`` and
    paragraph-level rhythm, computes ``sigmoid(w·x + b)``, then thresholds the
    probability into the required binary label.
    """
    return [prediction.action for prediction in classify_line_break_predictions(lines, model)]


def apply_line_break_actions(
    lines: Sequence[str],
    model: LineBreakRegressionModel | None = None,
) -> list[str]:
    """Return *lines* with exactly two trailing spaces appended for positives."""
    actions = classify_line_break_actions(lines, model)
    result: list[str] = []
    for line, action in zip(lines, actions):
        if action == "ADD_SPACES":
            result.append(_line_core(line) + "  ")
        else:
            result.append(line)
    return result


# ── Trailing-space regression data persistence ────────────────────────────────

def trailing_score_path(base: Path) -> Path:
    """Return the score-file path for a directory-level trailing-space model."""
    return base / _TRAILING_SCORE_FILENAME


def _feature_dict(features: LineBreakFeatures) -> dict[str, float]:
    """Serialise a feature vector with stable key ordering."""
    return {name: float(getattr(features, name)) for name in features.__dataclass_fields__}


def _relative_display_path(path: Path, base: Path) -> str:
    """Return a stable relative path when possible."""
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def collect_trailing_training_rows(
    filepath: Path,
    text: str,
    base: Path | None = None,
    model: LineBreakRegressionModel | None = None,
) -> list[LineBreakTrainingRow]:
    """Collect labelled regression rows from a document.

    Training convention: positive class is any eligible line already ending in
    two trailing spaces; negative class is every other eligible line.
    """
    root = base or filepath.parent
    display_path = _relative_display_path(filepath, root)
    lines = _parse_lines(text)
    predictions = classify_line_break_predictions(lines, model)
    rows: list[LineBreakTrainingRow] = []

    for i, line in enumerate(lines):
        if i + 1 >= len(lines) or lines[i + 1].strip() == "":
            continue
        if not line.strip() or _SKIP_RE.match(line):
            continue
        rows.append(LineBreakTrainingRow(
            path=display_path,
            line_no=i + 1,
            label=1 if _has_two_trailing_spaces(line) else 0,
            prediction=predictions[i],
            text=_line_core(line),
            next_text=_line_core(lines[i + 1]),
        ))

    return rows


def _training_row_to_record(row: LineBreakTrainingRow) -> dict[str, object]:
    """Convert a training row into JSON-serialisable data."""
    return {
        "path": row.path,
        "line_no": row.line_no,
        "label": row.label,
        "action": row.prediction.action,
        "probability": round(row.prediction.probability, 6),
        "logit": round(row.prediction.logit, 6),
        "features": _feature_dict(row.prediction.features),
        "text": row.text,
        "next_text": row.next_text,
    }


def save_trailing_score_data(
    score_path: Path,
    rows: Sequence[LineBreakTrainingRow],
    model: LineBreakRegressionModel | None = None,
) -> None:
    """Write trailing-space regression data to ``.trailing.sco`` as JSONL."""
    regression_model = model or LineBreakRegressionModel(dict(_LINE_BREAK_REGRESSION_WEIGHTS))
    score_path.parent.mkdir(parents=True, exist_ok=True)
    with score_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "version": _TRAILING_SCORE_VERSION,
            "kind": "trailing-space-regression-data",
            "positive_class": "line already ends with two trailing spaces",
            "negative_class": "eligible line does not end with two trailing spaces",
            "threshold": regression_model.threshold,
            "weights": regression_model.weights,
            "rows": len(rows),
        }, sort_keys=True) + "\n")
        for row in rows:
            f.write(json.dumps(_training_row_to_record(row), sort_keys=True) + "\n")


def fit_trailing_regression_model(
    rows: Sequence[LineBreakTrainingRow],
) -> LineBreakRegressionModel:
    """Fit logistic regression weights from labelled trailing-space rows."""
    feature_names = [
        name for name in LineBreakFeatures.__dataclass_fields__
        if name not in _REGRESSION_TRAINING_EXCLUDE
    ]
    labels = [row.label for row in rows]
    if not rows or len(set(labels)) < 2:
        weights = dict(_LINE_BREAK_REGRESSION_WEIGHTS)
        for name in _REGRESSION_TRAINING_EXCLUDE:
            weights[name] = 0.0
        return LineBreakRegressionModel(weights=weights)

    positive_count = sum(labels)
    negative_count = len(labels) - positive_count
    class_weight = {
        0: len(labels) / (2 * negative_count) if negative_count else 1.0,
        1: len(labels) / (2 * positive_count) if positive_count else 1.0,
    }
    prior = min(max(positive_count / len(labels), 1e-6), 1 - 1e-6)
    weights = {name: 0.0 for name in _LINE_BREAK_REGRESSION_WEIGHTS}
    weights["bias"] = math.log(prior / (1 - prior))

    for _ in range(_REGRESSION_EPOCHS):
        gradients = {name: 0.0 for name in weights}
        for row in rows:
            features = row.prediction.features
            logit = weights["bias"]
            for name in feature_names:
                logit += weights[name] * getattr(features, name)
            error = (_sigmoid(logit) - row.label) * class_weight[row.label]
            gradients["bias"] += error
            for name in feature_names:
                gradients[name] += error * getattr(features, name)

        scale = 1.0 / len(rows)
        weights["bias"] -= _REGRESSION_LEARNING_RATE * gradients["bias"] * scale
        for name in feature_names:
            penalty = _REGRESSION_L2 * weights[name]
            weights[name] -= _REGRESSION_LEARNING_RATE * (gradients[name] * scale + penalty)
        for name in _REGRESSION_TRAINING_EXCLUDE:
            weights[name] = 0.0

    return LineBreakRegressionModel(weights=weights)


def apply_regression_model_to_rows(
    rows: Sequence[LineBreakTrainingRow],
    model: LineBreakRegressionModel,
) -> list[LineBreakTrainingRow]:
    """Return rows with predictions recomputed from fitted weights."""
    fitted_rows: list[LineBreakTrainingRow] = []
    for row in rows:
        fitted_rows.append(LineBreakTrainingRow(
            path=row.path,
            line_no=row.line_no,
            label=row.label,
            prediction=_predict_line_break(row.prediction.features, model),
            text=row.text,
            next_text=row.next_text,
        ))
    return fitted_rows


def _iter_training_rows(
    files: Sequence[Path],
    base: Path,
) -> list[LineBreakTrainingRow]:
    """Collect training rows from a set of files."""
    rows: list[LineBreakTrainingRow] = []
    for filepath in files:
        rows.extend(collect_trailing_training_rows(
            filepath=filepath,
            text=filepath.read_text(encoding="utf-8"),
            base=base,
        ))
    return rows


def train_trailing_score_data(
    score_path: Path,
    rows: Sequence[LineBreakTrainingRow],
) -> LineBreakRegressionModel:
    """Fit and persist a trailing-space regression model."""
    model = fit_trailing_regression_model(rows)
    save_trailing_score_data(score_path, apply_regression_model_to_rows(rows, model), model)
    return model


def load_trailing_regression_model(score_path: Path) -> LineBreakRegressionModel | None:
    """Load trained regression weights from a ``.trailing.sco`` JSONL file."""
    if not score_path.exists():
        return None
    try:
        first_line = score_path.read_text(encoding="utf-8").splitlines()[0]
        header = json.loads(first_line)
    except (IndexError, OSError, json.JSONDecodeError):
        return None
    if header.get("kind") != "trailing-space-regression-data":
        return None
    weights = header.get("weights")
    if not isinstance(weights, dict):
        return None
    model_weights = dict(_LINE_BREAK_REGRESSION_WEIGHTS)
    for name, value in weights.items():
        if name in model_weights:
            try:
                model_weights[name] = float(value)
            except (TypeError, ValueError):
                return None
    try:
        threshold = float(header.get("threshold", _LINE_BREAK_PROBABILITY_THRESHOLD))
    except (TypeError, ValueError):
        threshold = _LINE_BREAK_PROBABILITY_THRESHOLD
    return LineBreakRegressionModel(weights=model_weights, threshold=threshold)


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

def _visible_warning_context(line: str, kind: str, limit: int = 80) -> str:
    """Return warning context with trailing spaces visible when relevant."""
    if kind != "unnecessary_break":
        return line.strip()[:limit]
    without_newline = line.rstrip("\n\r")
    core = without_newline.rstrip(" ")
    trailing_count = len(without_newline) - len(core)
    marker = "␠" * trailing_count
    if len(core) + len(marker) <= limit:
        return core + marker
    return core[:max(0, limit - len(marker))] + marker


def analyse_line_breaks(
    text: str,
    model: LineBreakRegressionModel | None = None,
) -> list[LineBreakWarning]:
    """Analyse the text for line-break issues and return warnings.

    The sequence-aware classifier labels each eligible line as either
    ``ADD_SPACES`` (preserve this break as poetic continuation) or ``LEAVE``
    (allow normal prose wrapping). Warnings report missing Markdown hard-break
    markers for positive lines and unnecessary markers for negative prose lines.
    """
    all_lines = _parse_lines(text)
    actions = classify_line_break_actions(all_lines, model)
    warnings: list[LineBreakWarning] = []

    for i, line in enumerate(all_lines):
        stripped = line.strip()
        if not stripped:
            continue  # skip blank lines
        if _SKIP_RE.match(line):
            continue  # skip structural lines

        has_trailing = _has_two_trailing_spaces(line)

        if actions[i] == "ADD_SPACES":
            if not has_trailing:
                warnings.append(LineBreakWarning(
                    line_no=i + 1,
                    kind="missing_break",
                    context=_visible_warning_context(line, "missing_break"),
                    message=(
                        f"Line {i+1}: poetic continuation missing two trailing spaces. "
                        "Markdown requires exactly two trailing spaces to preserve "
                        "the intentional line break."
                    ),
                ))
        elif has_trailing:
            warnings.append(LineBreakWarning(
                line_no=i + 1,
                kind="unnecessary_break",
                context=_visible_warning_context(line, "unnecessary_break"),
                message=(
                    f"Line {i+1}: prose line has trailing spaces. "
                    "Prose paragraphs typically don't use hard line breaks; "
                    "consider removing the trailing spaces."
                ),
            ))

    return warnings


# ── Line-break fixes ─────────────────────────────────────────────────────────

def generate_fixes(
    text: str,
    model: LineBreakRegressionModel | None = None,
) -> list[LineBreakFix]:
    """Generate actionable fixes for all line-break warnings."""
    warnings = analyse_line_breaks(text, model)
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
                          force: bool = False,
                          model: LineBreakRegressionModel | None = None) -> str:
    """Add line breaks to poetry-like continuations within Markdown content.

    Uses the sequence-aware action classifier to decide line-by-line whether a
    break should be preserved. When *force* is True every eligible non-final line
    is treated as ``ADD_SPACES``; otherwise the classifier avoids hard breaks in
    uniformly wrapped prose while preserving jagged poetic continuations.
    """
    lines = _parse_lines(content)
    actions = classify_line_break_actions(lines, model)
    processed: list[str] = []

    for i, line in enumerate(lines):
        eligible = i + 1 < len(lines) and lines[i + 1].strip() != ""
        should_break = force and eligible and line.strip() and not _SKIP_RE.match(line)
        if actions[i] == "ADD_SPACES" or should_break:
            processed.append(_line_core(line) + line_break)
        else:
            processed.append(line)

    return "\n".join(processed)


def _gitignore_patterns(base: Path) -> list[str]:
    """Load simple path patterns from the repository .gitignore."""
    gitignore = base / ".gitignore"
    if not gitignore.exists():
        return []
    patterns: list[str] = []
    for raw in gitignore.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def _matches_ignore_pattern(relative_path: Path, pattern: str) -> bool:
    """Return True for common .gitignore-style file/directory patterns."""
    rel = relative_path.as_posix()
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/")
    directory_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    if not pattern:
        return False

    parts = relative_path.parts
    if directory_only:
        if anchored:
            return len(parts) > 1 and parts[0] == pattern
        return pattern in parts[:-1]

    if "/" in pattern:
        return relative_path.match(pattern) or rel == pattern
    return relative_path.match(pattern) or any(part == pattern for part in parts)


def _is_ignored_path(relative_path: Path, patterns: Sequence[str]) -> bool:
    """Return True when *relative_path* is excluded by supported ignore rules."""
    if any(part.startswith(".") for part in relative_path.parts[:-1]):
        return True
    return any(_matches_ignore_pattern(relative_path, pattern) for pattern in patterns)


def discover_writings(base: Path) -> list[Path]:
    """Find Markdown/text files under *base*, respecting .gitignore patterns."""
    writings: list[Path] = []
    ignore_patterns = _gitignore_patterns(base)
    for path in sorted(base.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file():
            continue
        relative_path = path.relative_to(base)
        if _is_ignored_path(relative_path, ignore_patterns):
            continue
        if path.name == _TRAILING_SCORE_FILENAME:
            continue
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        writings.append(relative_path)
    return writings


# ── CLI formatting ────────────────────────────────────────────────────────────

def _supports_color(stream: object = sys.stdout) -> bool:
    """Return True when ANSI colour output should be emitted."""
    return hasattr(stream, "isatty") and stream.isatty()


def _color(text: str, code: str, enabled: bool) -> str:
    """Wrap *text* in an ANSI colour when enabled."""
    if not enabled:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _warning_color(kind: str) -> str:
    """Return the display colour for a line-break warning kind."""
    return _ANSI_YELLOW if kind == "missing_break" else _ANSI_RED


def _format_warning(filepath: Path | str, warning: LineBreakWarning, color: bool = False) -> tuple[str, str]:
    """Return the two CLI output lines for a line-break warning."""
    kind_color = _warning_color(warning.kind)
    location = _color(f"{filepath}:{warning.line_no}", _ANSI_CYAN + _ANSI_BOLD, color)
    kind = _color(warning.kind, kind_color + _ANSI_BOLD, color)
    context_color = _ANSI_MAGENTA if warning.kind == "unnecessary_break" else _ANSI_BOLD
    context = _color(warning.context, context_color, color)
    message = _color(f"  {warning.message}", _ANSI_DIM, color)
    return f"{location}: {kind}: {context}", message


# ── Interactive fix mode ──────────────────────────────────────────────────────

def interactive_fix(
    filepath: str,
    text: str,
    model: LineBreakRegressionModel | None = None,
) -> InteractiveFixResult:
    """Interactively prompt the user to fix line-break issues.

    Returns the corrected text, or None if no changes were made.
    If quit_requested is True, the caller should stop interactive processing.
    Prints to stdout/stderr and reads from stdin.
    """
    fixes = generate_fixes(text, model)
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
    p.add_argument("files", nargs="*", help="Markdown/text files to analyse")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Show per-window scores")
    p.add_argument("-i", "-I", "--interactive", action="store_true",
                    help="Interactive fix mode")
    p.add_argument("-t", "--train", action="store_true",
                   help="Train/write regression data (default: .trailing.sco; custom: -t=path/to/file.sco)")
    p.add_argument("--_train-path", dest="train_path", type=Path, default=None,
                   help=argparse.SUPPRESS)
    raw_args = sys.argv[1:]
    normalised_args: list[str] = []
    for arg in raw_args:
        if arg.startswith("-t="):
            normalised_args.extend(["-t", "--_train-path", arg.split("=", 1)[1]])
        elif arg.startswith("--train="):
            normalised_args.extend(["--train", "--_train-path", arg.split("=", 1)[1]])
        else:
            normalised_args.append(arg)
    args = p.parse_args(normalised_args)
    base = Path.cwd()
    files = [Path(filepath) for filepath in args.files] if args.files else discover_writings(base)

    if not files:
        p.error("No Markdown or text files found outside dot directories.")

    resolved_files = [(base / filepath).resolve() if not filepath.is_absolute() else filepath for filepath in files]

    if args.train:
        score_path = args.train_path or trailing_score_path(base)
        rows = _iter_training_rows(resolved_files, base)
        train_trailing_score_data(score_path, rows)
        print(f"wrote {score_path} ({len(rows)} regression rows)")
        return

    model = load_trailing_regression_model(trailing_score_path(base))
    any_warnings = False
    color_output = _supports_color(sys.stdout)

    for filepath in files:
        text = filepath.read_text(encoding="utf-8")
        is_poem = detect_poetry(text)
        scores = poetry_scores(text)
        warnings = analyse_line_breaks(text, model)

        if warnings:
            any_warnings = True
            for w in warnings:
                summary, detail = _format_warning(filepath, w, color_output)
                print(summary)
                print(detail)

        if args.verbose:
            label = "POETRY" if is_poem else "PROSE"
            print(f"\n{filepath}: {label}")
            if scores:
                avg = statistics.mean(s.score for s in scores)
                print(f"  windows: {len(scores)}, avg score: {avg:.3f}, "
                      f"threshold: {THRESHOLD}")
            for ws in scores:
                tag = "P" if ws.is_poetry else "."
                print(f"  [{tag}] lines {ws.start_line+1}-{ws.end_line}: "
                      f"score={ws.score:.3f} V={ws.variability:.3f} "
                      f"S={ws.short_ratio:.3f} T={ws.tail_regularity:.3f} "
                      f"MAD={ws.mad:.1f} μ={ws.mean_length:.1f}")

        if args.interactive:
            result = interactive_fix(filepath, text, model)
            if result.text is not None:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(result.text)
                print(f"  Saved {filepath}")
            if result.quit_requested:
                break

    if not any_warnings and not args.verbose and not args.interactive:
        print("No line-break issues found.")


if __name__ == "__main__":
    _cli_main()
