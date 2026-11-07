"""Regex extractor: find regex matches, score them by nearby words, resolve overlaps.

Library use:
    cfg = load_config("extraction_config.yaml")
    matches, decisions = extract("some text", cfg)
    matches, _ = extract({"text": "...", "context": "..."}, cfg)   # ignore the log
    matches, decisions = extract(("context text", "text"), cfg, source="id-1")

extract() writes no files; the caller decides what to do with `decisions`
(flat dicts, columns REPORT_FIELDS). Optional helpers: write_report_csv(),
open_report(), log_context_miss(), reset_report(), reset_error_log().

CLI (one text per run; for many texts use the library):
    extract.py --config extraction_config.yaml [--text TEXT | < stdin]
               [--context TEXT] [--report] [--error-log]
Prints the matches as a JSON list. Nothing is written unless asked:
    --report     ./extract_report.csv  one row per decision (context, min_score, overlaps)
    --error-log  ./errorlog.csv        raw context and text of a context miss
Both files are overwritten when their flag is given.

Memory: for many texts, write decisions as you go (open_report) instead of
collecting them in a list.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import warnings
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, Union

import yaml

from words import next_words, prev_words

TIEBREAK_NAMES = ("score", "context_penalty", "length", "priority")
CONTEXT_DIRECTIONS = ("before", "after", "both")

# Written (relative to the working directory) whenever a context selected
# patterns but nothing was extracted. main() resets it once per CLI run.
ERROR_LOG_PATH = "errorlog.csv"
ERROR_LOG_FIELDS = ("timestamp", "file", "context", "text", "context_patterns", "reason")

# The decision report (extract's log) as CSV; main() resets it once per run.
REPORT_PATH = "extract_report.csv"
_CAND_COLS = ("label", "text", "start", "end", "score")
REPORT_FIELDS = (
    ("file", "kind", "outcome", "reason", "patterns")
    + tuple(f"a_{k}" for k in _CAND_COLS)
    + tuple(f"b_{k}" for k in _CAND_COLS)
    + ("decided_by", "penalty_skipped")
)
# decided_by: overlap -> score | context_penalty | length | priority | start_label;
# coexist -> coexist; below_min_score -> min_score; context / context_miss -> "".
PENALTY_SKIPPED_NOTE = "; context_penalty skipped: context selected no patterns"
REQUIRED_SETTINGS = (
    "context_window_words",
    "distance_decay",
    "min_score",
    "case_sensitive",
    "overlap_tiebreak",
)

# extract() accepts: plain text, {"text": ..., "context": ...}, or (context, text).
ExtractInput = Union[str, dict, tuple]


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------
@dataclass
class Pattern:
    label: str
    priority: int
    regex: "re.Pattern[str]"
    positive: list[str] = field(default_factory=list)          # context.positive: selects
    penalize: dict[str, float] = field(default_factory=dict)   # context.penalize: weight <= 0
    nearby: dict[str, float] = field(default_factory=dict)     # text window: signed weight


@dataclass
class Config:
    settings: dict[str, Any]
    normalize: dict[str, bool]
    patterns: list[Pattern]
    coexist: set[frozenset[str]]
    tests: list[dict[str, Any]]


@dataclass
class Candidate:
    label: str
    start: int
    end: int
    text: str
    priority: int
    score: float = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "score": round(float(self.score), 4),
        }


# One entry of the troubleshooting log: a flat dict with the REPORT_FIELDS
# columns (file, kind, outcome, reason, patterns, a_*, b_*, decided_by,
# penalty_skipped). It is the single
# form used for extract_report.csv, errorlog.csv and the CLI JSON output.
#
# kind: "context" | "context_miss" | "below_min_score" | "overlap" | "coexist"
Decision = dict[str, Any]


def _cand_columns(prefix: str, cand: Candidate | None) -> dict[str, Any]:
    data = cand.to_dict() if cand else {}
    return {f"{prefix}_{k}": data.get(k, "") for k in _CAND_COLS}


def _decision(
    kind: str,
    outcome: str,
    reason: str,
    a: Candidate | None = None,
    b: Candidate | None = None,
    patterns: Sequence[str] = (),
    decided_by: str = "",
    penalty_skipped: bool = False,
) -> Decision:
    """Build one log row. a/b are the candidates involved:
    overlap: a = winner, b = dropped. below_min_score: a = dropped.
    coexist: a and b are both kept. The `file` column is filled in later.
    `decided_by` names the rule that decided (see REPORT_FIELDS).
    """
    return {
        "file": "",
        "kind": kind,
        "outcome": outcome,
        "reason": reason,
        "patterns": ";".join(patterns),
        **_cand_columns("a", a),
        **_cand_columns("b", b),
        "decided_by": decided_by,
        "penalty_skipped": penalty_skipped,
    }


# --------------------------------------------------------------------------
# 1. load_config
# --------------------------------------------------------------------------
def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ValueError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _check_keyword(label: str, key_name: str, key: str, seen: Iterable[str]) -> None:
    """Fail loudly on keywords the scorer could never match.

    Words are ASCII letters only (see words.py), so a keyword with a digit or
    an accented letter would be silently reduced and match the wrong words.
    A keyword may not appear twice under the same key of a pattern.
    """
    where = f"pattern '{label}': {key_name} keyword '{key}'"
    if not _keyword_words(key) or any(c.isalnum() and not ("a" <= c <= "z") for c in key):
        raise ValueError(
            f"{where} must contain only ASCII letters (digits and accented "
            "letters can never match a word)"
        )
    if key in seen:
        raise ValueError(f"{where} is listed more than once in this pattern")


def _weight_map(label: str, key_name: str, raw: Any, non_positive: bool) -> dict[str, float]:
    """Validate a `word: weight` map (`nearby` or `context.penalize`)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"pattern '{label}': {key_name} must be a map of word: weight, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, float] = {}
    for keyword, weight in raw.items():
        key = str(keyword).lower()
        _check_keyword(label, key_name, key, out)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(
                f"pattern '{label}': {key_name} keyword '{key}' needs a numeric "
                f"weight, got {weight!r}"
            )
        if non_positive and weight > 0:
            raise ValueError(
                f"pattern '{label}': {key_name} keyword '{key}' has weight "
                f"{float(weight):g}; penalize weights must be <= 0"
            )
        out[key] = float(weight)
    return out


def _load_keywords(
    label: str, entry: dict[str, Any]
) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """Return (context.positive, context.penalize, nearby) for one pattern."""
    context = entry.get("context") or {}
    if not isinstance(context, dict):
        raise ValueError(f"pattern '{label}': `context` must be a map (positive, penalize)")
    if "negative" in context:
        raise ValueError(
            f"pattern '{label}': `context.negative` was renamed to "
            "`context.penalize`; it no longer affects the text score (use `nearby`)."
        )
    raw_pos = context.get("positive")
    if isinstance(raw_pos, dict):
        raise ValueError(
            f"pattern '{label}': `context.positive` is now a list of words; "
            "move weights for text scoring to `nearby`."
        )
    if raw_pos is not None and not isinstance(raw_pos, list):
        raise ValueError(
            f"pattern '{label}': `context.positive` must be a list of words, "
            f"got {type(raw_pos).__name__}"
        )
    positive: list[str] = []
    for keyword in raw_pos or []:
        if not isinstance(keyword, str):
            raise ValueError(
                f"pattern '{label}': context.positive keyword {keyword!r} must be a string"
            )
        key = keyword.lower()
        _check_keyword(label, "context.positive", key, positive)
        positive.append(key)
    penalize = _weight_map(label, "context.penalize", context.get("penalize"), True)
    for key in penalize:
        if key in positive:
            raise ValueError(
                f"pattern '{label}': keyword '{key}' is in both context.positive "
                "and context.penalize"
            )
    nearby = _weight_map(label, "nearby", entry.get("nearby"), False)
    return positive, penalize, nearby


def load_config(path: str | Path) -> Config:
    """Read the YAML config and compile every regex once."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = yaml.safe_load(fh)

    settings = _require(raw, "settings", "config")
    for key in REQUIRED_SETTINGS:
        _require(settings, key, "settings")
    # A blank `overlap_tiebreak:` (None) means "no criteria", same as [].
    settings["overlap_tiebreak"] = settings["overlap_tiebreak"] or []
    for name in settings["overlap_tiebreak"]:
        if name == "context_negative":
            raise ValueError("`context_negative` was renamed to `context_penalty`.")
        if name not in TIEBREAK_NAMES:
            raise ValueError(
                f"settings.overlap_tiebreak: unknown criterion '{name}' "
                f"(expected one of {', '.join(TIEBREAK_NAMES)})"
            )

    direction = settings.get("context_direction", "both")
    if direction not in CONTEXT_DIRECTIONS:
        raise ValueError(
            f"settings.context_direction: unknown value '{direction}' "
            f"(expected one of {', '.join(CONTEXT_DIRECTIONS)})"
        )
    settings["context_direction"] = direction

    stop_at = settings.get("stop_at", "")
    if not isinstance(stop_at, str):
        raise ValueError(
            f"settings.stop_at: expected a string of characters, "
            f"got {type(stop_at).__name__}"
        )
    settings["stop_at"] = stop_at

    flags = 0 if settings["case_sensitive"] else re.IGNORECASE
    patterns: list[Pattern] = []
    seen: set[str] = set()
    for entry in _require(raw, "patterns", "config"):
        label = str(_require(entry, "label", "pattern"))
        if label in seen:
            raise ValueError(f"pattern '{label}': duplicate label")
        seen.add(label)
        try:
            compiled = re.compile(str(_require(entry, "regex", f"pattern '{label}'")), flags)
        except re.error as exc:
            raise ValueError(f"pattern '{label}': invalid regex: {exc}") from exc

        positive, penalize, nearby = _load_keywords(label, entry)
        patterns.append(
            Pattern(
                label=label,
                priority=int(_require(entry, "priority", f"pattern '{label}'")),
                regex=compiled,
                positive=positive,
                penalize=penalize,
                nearby=nearby,
            )
        )

    unused = [p.label for p in patterns if p.penalize]
    if unused and "context_penalty" not in settings["overlap_tiebreak"]:
        warnings.warn(
            f"context.penalize is set for {', '.join(unused)} but `context_penalty` "
            "is not in settings.overlap_tiebreak, so it is never used.",
            UserWarning,
            stacklevel=2,
        )

    coexist = {frozenset(pair) for pair in (raw.get("coexist") or [])}
    return Config(
        settings=settings,
        normalize=raw.get("normalize") or {},
        patterns=patterns,
        coexist=coexist,
        tests=raw.get("tests") or [],
    )


# --------------------------------------------------------------------------
# 2. normalize
# --------------------------------------------------------------------------
def normalize(text: str, cfg: Config) -> str:
    """Apply only the enabled normalize options. Offsets refer to this text."""
    opts = cfg.normalize
    if opts.get("unicode_dashes_to_hyphen"):
        text = text.replace("–", "-").replace("—", "-")
    if opts.get("smart_quotes_to_ascii"):
        for src, dst in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"')):
            text = text.replace(src, dst)
    if opts.get("collapse_whitespace"):
        text = re.sub(r"\s+", " ", text)
    return text


# --------------------------------------------------------------------------
# Context: choose which patterns run on the input text
# --------------------------------------------------------------------------
def select_patterns(
    context: str | None, patterns: list[Pattern]
) -> tuple[list[Pattern], Decision]:
    """Return the patterns to run on the input text.

    A pattern "matches" the context when any of its `context.positive`
    words/phrases occurs in it as whole words, case-insensitively (same
    matcher as `nearby` scoring). The pattern regexes are NOT run on the
    context, and `nearby` / `context.penalize` never select.
    If at least one pattern matches, only those run on the text. With no
    context, or no matches, all run.
    """
    if not context or not context.strip():
        return list(patterns), _decision(
            "context", "all_patterns_used", "no context supplied, all patterns used",
            patterns=[p.label for p in patterns],
        )
    words = _context_words(context)
    hits: list[Pattern] = []
    found: list[str] = []
    for p in patterns:
        kws = _context_hits(words, p)
        if kws:
            hits.append(p)
            found.append(f"{p.label} ({', '.join(kws)})")
    if not hits:
        return list(patterns), _decision(
            "context", "all_patterns_used", "no context hits, all patterns used",
            patterns=[p.label for p in patterns],
        )
    return hits, _decision(
        "context", "patterns_restricted",
        f"context matched {', '.join(found)}; only these patterns used",
        patterns=[p.label for p in hits],
    )


def _context_words(context: str) -> list[str]:
    """All words of the context, reduced exactly like the text window's words
    (words.next_words). The leading space keeps the first word: next_words
    skips a word that the start position falls inside."""
    return next_words(" " + context, 0, len(context) + 1)


def _context_hits(words: list[str], pattern: Pattern) -> list[str]:
    """`context.positive` keywords of `pattern` that occur in the context words."""
    _, hits = _sum_keywords(dict.fromkeys(pattern.positive, 1.0), _phrase_finder(words), False)
    return hits


# --------------------------------------------------------------------------
# Error log: context selected patterns but nothing was extracted
# --------------------------------------------------------------------------
def _reset_csv(path: str, fields: Sequence[str]) -> None:
    """Overwrite `path` with just a header row."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(fields)


def _append_csv_rows(
    path: str, fields: Sequence[str], rows: list[dict[str, Any]]
) -> None:
    """Append rows to `path`, writing the header first if the file is new."""
    target = Path(path)
    is_new = not target.exists() or target.stat().st_size == 0
    with open(target, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def reset_error_log() -> None:
    """Overwrite ERROR_LOG_PATH with just the header row."""
    _reset_csv(ERROR_LOG_PATH, ERROR_LOG_FIELDS)


def log_context_miss(source: str, context: str, text: str, miss: Decision) -> None:
    """Append one row to ERROR_LOG_PATH (header written if the file is new).

    `miss` is the "context_miss" decision; it supplies the patterns and reason.
    """
    _append_csv_rows(ERROR_LOG_PATH, ERROR_LOG_FIELDS, [{
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "file": source,
        "context": context,
        "text": text,
        "context_patterns": miss["patterns"],
        "reason": miss["reason"],
    }])


# --------------------------------------------------------------------------
# Report CSV: one flat row per decision
# --------------------------------------------------------------------------
def reset_report(path: str = REPORT_PATH) -> None:
    """Overwrite the report CSV with just the header row."""
    _reset_csv(path, REPORT_FIELDS)


@contextmanager
def open_report(path: str = REPORT_PATH) -> Iterator[Callable[[Iterable[Decision]], None]]:
    """Keep the report CSV open while many extractions run.

    Yields write(decisions), which appends rows straight to the file, so
    nothing accumulates in memory and the file is opened once, not per call:

        with open_report() as write:
            for rid, text in big_input:
                write(extract(text, cfg, source=rid)[1])

    The header is written first if the file is new or empty.
    """
    target = Path(path)
    is_new = not target.exists() or target.stat().st_size == 0
    with open(target, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS, lineterminator="\n")
        if is_new:
            writer.writeheader()
        yield writer.writerows


def write_report_csv(decisions: Iterable[Decision], path: str = REPORT_PATH) -> None:
    """Append the decisions as rows to the report CSV (header if file is new).

    Fine for a single call. For many calls use open_report(), which avoids
    reopening the file every time.
    """
    with open_report(path) as write:
        write(decisions)


# --------------------------------------------------------------------------
# Overlap summary: which label pairs are not settled by score
# --------------------------------------------------------------------------
SUMMARY_CRITERIA = ("score", "context_penalty", "length", "priority", "start_label")
SUMMARY_FIELDS = (
    ("pair", "overlaps")
    + tuple(f"by_{c}" for c in SUMMARY_CRITERIA)
    + ("not_by_score", "not_by_score_pct", "no_evidence", "penalty_skipped",
       "wins", "examples", "hint")
)
_NO_EVIDENCE = ("priority", "start_label")   # fallback order, nothing in the input
_MAX_EXAMPLES = 3
HINT_SKIPPED = ("contexts mention penalize words but don't select these patterns; "
                "add context.positive words")
HINT_NO_EVIDENCE = "add nearby words; if inputs come with a context, add penalize words"
HINT_PARTIAL = "if both values are valid, add to coexist"


@dataclass
class _PairStats:
    by: Counter = field(default_factory=Counter)        # decided_by -> count
    wins: Counter = field(default_factory=Counter)      # winning label -> count
    penalty_skipped: int = 0
    unsettled: int = 0                                  # not decided by score
    partial: int = 0                                    # ...of which spans differ
    no_evidence_examples: list[str] = field(default_factory=list)
    other_examples: list[str] = field(default_factory=list)


def _example(row: dict[str, str]) -> str:
    return f'{row.get("file") or "(no id)"}: "{row.get("a_text", "")}" vs "{row.get("b_text", "")}"'


def summarize_overlaps(path: str = REPORT_PATH) -> list[dict[str, Any]]:
    """Summarize a report CSV's `overlap` rows by label pair.

    Shows which pairs overlap most often without being resolved by score:
    counts per deciding criterion, `no_evidence` (decided by priority or
    start/label, i.e. nothing in the input), up to three example inputs and
    a hint. Rows are streamed; memory stays bounded per pair. Sorted by
    no_evidence, not_by_score, overlaps (highest first), then pair.
    Raises ValueError if the report has no `decided_by` column.
    """
    stats: dict[str, _PairStats] = {}
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        if "decided_by" not in (reader.fieldnames or []):
            raise ValueError(
                f"{path}: no decided_by column; the report is from an older "
                "version, regenerate it with --report"
            )
        for row in reader:
            if row.get("kind") != "overlap":
                continue
            a, b = row.get("a_label", ""), row.get("b_label", "")
            st = stats.setdefault("/".join(sorted((a, b))), _PairStats())
            by = row.get("decided_by", "")
            st.by[by] += 1
            st.wins[a] += 1
            if str(row.get("penalty_skipped", "")).strip().lower() == "true":
                st.penalty_skipped += 1
            if by == "score":
                continue
            st.unsettled += 1
            spans_a = (row.get("a_start"), row.get("a_end"))
            if spans_a != (row.get("b_start"), row.get("b_end")):
                st.partial += 1
            bucket = st.no_evidence_examples if by in _NO_EVIDENCE else st.other_examples
            if len(bucket) < _MAX_EXAMPLES:
                bucket.append(_example(row))

    out: list[dict[str, Any]] = []
    for pair, st in stats.items():
        overlaps = sum(st.by.values())
        not_by_score = overlaps - st.by["score"]
        no_evidence = sum(st.by[c] for c in _NO_EVIDENCE)
        hints = []
        if st.penalty_skipped > 0:
            hints.append(HINT_SKIPPED)
        if no_evidence > 0:
            hints.append(HINT_NO_EVIDENCE)
            if st.partial * 2 > st.unsettled:
                hints.append(HINT_PARTIAL)
        examples = (st.no_evidence_examples + st.other_examples)[:_MAX_EXAMPLES]
        wins = sorted(st.wins.items(), key=lambda kv: (-kv[1], kv[0]))
        out.append({
            "pair": pair,
            "overlaps": overlaps,
            **{f"by_{c}": st.by[c] for c in SUMMARY_CRITERIA},
            "not_by_score": not_by_score,
            "not_by_score_pct": round(100 * not_by_score / overlaps, 1),
            "no_evidence": no_evidence,
            "penalty_skipped": st.penalty_skipped,
            "wins": "; ".join(f"{label} {n}" for label, n in wins),
            "examples": " | ".join(examples),
            "hint": "; ".join(hints) or "ok",
        })
    out.sort(key=lambda r: (-r["no_evidence"], -r["not_by_score"], -r["overlaps"], r["pair"]))
    return out


def format_summary(rows: list[dict[str, Any]]) -> str:
    """The summary as an aligned plain-text table (header + one line per pair)."""
    table = [list(SUMMARY_FIELDS)] + [[str(r[k]) for k in SUMMARY_FIELDS] for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(SUMMARY_FIELDS))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in table
    )


# --------------------------------------------------------------------------
# 3. find_candidates
# --------------------------------------------------------------------------
def find_candidates(text: str, patterns: list[Pattern]) -> list[Candidate]:
    """Run EACH pattern separately with finditer (never one big alternation)."""
    found: list[Candidate] = []
    for pat in patterns:
        for m in pat.regex.finditer(text):
            if m.end() == m.start():
                continue  # empty matches carry no text to extract
            found.append(
                Candidate(pat.label, m.start(), m.end(), m.group(0), pat.priority)
            )
    found.sort(key=lambda c: (c.start, c.end, c.label))
    return found


# --------------------------------------------------------------------------
# 4. score_candidate
# --------------------------------------------------------------------------
def _keyword_words(keyword: str) -> list[str]:
    """Reduce a keyword/phrase to the same lowercase words that
    prev_words/next_words produce: apostrophes join a word ("don't" ->
    "dont"); every other non-letter (space, hyphen, digit...) separates."""
    return re.findall(r"[a-z]+", keyword.lower().replace("'", "").replace("’", ""))


DistanceFinder = Callable[[list[str]], Union[int, None]]


def _sum_keywords(
    weights: dict[str, float], find: DistanceFinder, decay: bool
) -> tuple[float, list[str]]:
    """The one keyword matcher behind `nearby`, `context_penalty` and selection.

    Each keyword/phrase is reduced with _keyword_words and handed to `find`,
    which returns the word distance of its nearest occurrence (or None).
    Every keyword counts once. Returns (sum of weights found, keywords found);
    with `decay`, each weight is divided by its distance.
    """
    total = 0.0
    hits: list[str] = []
    for keyword, weight in weights.items():
        kw = _keyword_words(keyword)
        if not kw:
            continue
        dist = find(kw)
        if dist is None:
            continue
        hits.append(keyword)
        total += weight / dist if decay else weight
    return total, hits


def _phrase_finder(words: list[str]) -> DistanceFinder:
    """Finder for a position-less word list (the context): 1 if the phrase
    occurs anywhere as contiguous whole words, else None."""
    def find(kw: list[str]) -> int | None:
        size = len(kw)
        found = any(words[i:i + size] == kw for i in range(len(words) - size + 1))
        return 1 if found else None
    return find


def _context_penalty(words: list[str], pattern: Pattern) -> tuple[float, list[str]]:
    """(sum, hits) of the pattern's `context.penalize` words found in the
    context words. The sum is <= 0; closer to 0 wins the `context_penalty`
    tie-break. Same matcher as `nearby`, without window or distance."""
    return _sum_keywords(pattern.penalize, _phrase_finder(words), False)


def _nearest_distance(
    before: list[str], after: list[str], kw: list[str]
) -> int | None:
    """Word distance to the closest occurrence of kw (nearest word = 1).

    Before the match a phrase's LAST word sets the distance; after the match
    its FIRST word does. Both sides are searched, and the nearest wins.
    """
    size = len(kw)
    best: int | None = None
    for i in range(len(before) - size + 1):
        if before[i:i + size] == kw:
            d = len(before) - (i + size - 1)
            best = d if best is None else min(best, d)
    for i in range(len(after) - size + 1):
        if after[i:i + size] == kw:
            d = i + 1
            best = d if best is None else min(best, d)
    return best


def score_candidate(
    text: str, cand: Candidate, pattern: Pattern, settings: dict[str, Any]
) -> float:
    """Sum the weight of each `nearby` keyword found near the match (nearest
    occurrence only), divided by word distance when distance_decay is true.
    The context string is never read here.

    settings["context_direction"] ("before", "after" or "both", default
    "both") chooses which side(s) of the match are searched.
    settings["stop_at"] (default "") lists characters, e.g. ".!?", that end
    the context scan early on either side.

    Keyword matching is always lowercase; case_sensitive only affects regex.
    """
    n = int(settings["context_window_words"])
    direction = settings.get("context_direction", "both")
    stop_at = settings.get("stop_at", "")
    before = (
        prev_words(text, cand.start, n, stop_at=stop_at)
        if direction in ("before", "both") else []
    )
    after = (
        next_words(text, cand.end, n, stop_at=stop_at)
        if direction in ("after", "both") else []
    )
    decay = bool(settings["distance_decay"])
    total, _ = _sum_keywords(
        pattern.nearby, lambda kw: _nearest_distance(before, after, kw), decay
    )
    return total


# --------------------------------------------------------------------------
# 5. resolve_overlaps
# --------------------------------------------------------------------------
def _overlaps(a: Candidate, b: Candidate) -> bool:
    if a.start == b.start and a.end == b.end:
        return True  # identical spans count as overlapping
    return a.start < b.end and b.start < a.end


def _rank_key(
    c: Candidate, order: Sequence[str], ctx_penalty: dict[str, float]
) -> tuple:
    parts: list[Any] = []
    for name in order:
        if name == "score":
            parts.append(-c.score)
        elif name == "context_penalty":  # penalty <= 0; closer to 0 wins
            parts.append(-ctx_penalty.get(c.label, 0.0))
        elif name == "length":
            parts.append(-(c.end - c.start))
        else:  # priority: lower number wins
            parts.append(c.priority)
    parts.extend((c.start, c.label))  # deterministic final tie-break
    return tuple(parts)


def _why_wins(
    winner: Candidate,
    loser: Candidate,
    order: Sequence[str],
    ctx_penalty: dict[str, float],
) -> tuple[str, str]:
    """Return (criterion, reason): the tiebreak criterion that decided and a
    text naming the ones that tied. criterion is "start_label" when every
    listed criterion tied (or the list is empty)."""
    tied: list[str] = []
    for name in order:
        if name == "score" and winner.score != loser.score:
            detail = f"score {winner.score:.2f} > {loser.score:.2f}"
        elif (
            name == "context_penalty"
            and ctx_penalty.get(winner.label, 0.0) != ctx_penalty.get(loser.label, 0.0)
        ):
            detail = (
                f"context penalty {ctx_penalty.get(winner.label, 0.0):g} "
                f"> {ctx_penalty.get(loser.label, 0.0):g}"
            )
        elif (
            name == "length"
            and (winner.end - winner.start) != (loser.end - loser.start)
        ):
            detail = (
                f"length {winner.end - winner.start} > {loser.end - loser.start}"
            )
        elif name == "priority" and winner.priority != loser.priority:
            detail = (
                f"priority {winner.priority} < {loser.priority} (lower number wins)"
            )
        else:
            tied.append(name)
            continue
        return name, "; ".join([f"{t} tied" for t in tied] + [detail])
    return "start_label", "all tiebreak criteria tied; kept earlier start/label"


def resolve_overlaps(
    cands: list[Candidate],
    cfg: Config,
    ctx_penalty: dict[str, float] | None = None,
    skipped_hits: set[str] | None = None,
) -> tuple[list[Candidate], list[Decision]]:
    """Drop candidates below min_score, then resolve overlaps greedily.

    Candidates are ranked by overlap_tiebreak (in config order); if it is
    empty or everything ties, the earlier start, then the label, decides.
    `ctx_penalty` maps label -> summed context.penalize weight and feeds the
    `context_penalty` criterion (empty/None: that criterion always ties).
    `skipped_hits` names labels whose penalize words occur in a context that
    selected no patterns; it only adds the "skipped" note to overlap rows and
    never affects ranking.
    Each candidate is accepted unless it overlaps an already-accepted one
    whose label pair is not in `coexist`. Every decision is logged.
    """
    ctx_penalty = ctx_penalty or {}
    skipped_hits = skipped_hits or set()
    settings = cfg.settings
    order = settings["overlap_tiebreak"]
    min_score = settings["min_score"]
    decisions: list[Decision] = []

    survivors: list[Candidate] = []
    for c in cands:
        if c.score < min_score:
            decisions.append(
                _decision("below_min_score", "dropped",
                          f"score {c.score:.2f} < min_score {min_score}", a=c,
                          decided_by="min_score")
            )
        else:
            survivors.append(c)

    accepted: list[Candidate] = []
    for c in sorted(survivors, key=lambda x: _rank_key(x, order, ctx_penalty)):
        pending: list[Decision] = []
        beaten = False
        for a in accepted:
            if not _overlaps(a, c):
                continue
            if frozenset((a.label, c.label)) in cfg.coexist:
                pending.append(
                    _decision("coexist", "both_kept",
                              f"{a.label}/{c.label} pair is in coexist; both kept",
                              a=a, b=c, decided_by="coexist")
                )
            else:
                criterion, reason = _why_wins(a, c, order, ctx_penalty)
                skipped = bool({a.label, c.label} & skipped_hits)
                if skipped:
                    reason += PENALTY_SKIPPED_NOTE
                decisions.append(
                    _decision("overlap", "a_wins_b_dropped", reason, a=a, b=c,
                              decided_by=criterion, penalty_skipped=skipped)
                )
                beaten = True
                break
        if not beaten:
            accepted.append(c)
            decisions.extend(pending)

    accepted.sort(key=lambda c: (c.start, c.end, c.label))
    return accepted, decisions


# --------------------------------------------------------------------------
# 6. extract
# --------------------------------------------------------------------------
def _split_input(inp: ExtractInput) -> tuple[str, str | None]:
    """Return (text, context) from str, {"text","context"}, or (context, text)."""
    if isinstance(inp, str):
        return inp, None
    if isinstance(inp, dict):
        if "text" not in inp:
            raise ValueError("input dict needs a 'text' key (optional: 'context')")
        return inp["text"], inp.get("context")
    if isinstance(inp, (tuple, list)) and len(inp) == 2:
        context, text = inp
        return text, context
    raise ValueError("input must be str, {'text','context'} dict, or (context, text)")


def extract(
    inp: ExtractInput, cfg: Config, source: str = ""
) -> tuple[list[Candidate], list[Decision]]:
    """Run steps 2-5 and return (matches, decisions). Writes no files.

    `decisions` is the flat decision log; `source` fills its `file` column.
    If the context selected patterns but nothing was extracted, it includes a
    "context_miss" row. Ignore the log with `matches, _ = extract(...)`, or
    write it with write_report_csv() / open_report() / log_context_miss().
    """
    raw_text, context = _split_input(inp)
    text = normalize(raw_text, cfg)
    norm_context = normalize(context, cfg) if context else None

    patterns, ctx_decision = select_patterns(norm_context, cfg.patterns)
    by_label = {p.label: p for p in cfg.patterns}

    cands = find_candidates(text, patterns)
    for c in cands:
        c.score = score_candidate(text, c, by_label[c.label], cfg.settings)

    # context_penalty only applies when the context actually chose the patterns.
    ctx_penalty: dict[str, float] = {}
    skipped_hits: set[str] = set()
    has_context = bool(norm_context and norm_context.strip())
    if has_context and ctx_decision["outcome"] == "patterns_restricted":
        ctx_words = _context_words(norm_context)
        ctx_penalty = {p.label: _context_penalty(ctx_words, p)[0] for p in patterns}
    elif has_context and "context_penalty" in cfg.settings["overlap_tiebreak"]:
        # Gate off: note (only) which labels' penalize words were in the context.
        ctx_words = _context_words(norm_context)
        skipped_hits = {p.label for p in patterns if _context_penalty(ctx_words, p)[1]}

    matches, decisions = resolve_overlaps(cands, cfg, ctx_penalty, skipped_hits)
    decisions = [ctx_decision] + decisions

    if ctx_decision["outcome"] == "patterns_restricted" and not matches:
        labels = [p.label for p in patterns]
        if cands:
            reason = (f"context matched {', '.join(labels)}; {len(cands)} candidate(s) "
                      "found in text but all dropped (min_score/overlap)")
        else:
            reason = f"context matched {', '.join(labels)}; no matches in text"
        miss = _decision("context_miss", "context_miss", reason, patterns=labels)
        decisions.append(miss)
    return matches, [{**d, "file": source} for d in decisions]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract regex matches with context scoring from one text. "
                    "Prints the matches as JSON. For many texts, use the library.")
    parser.add_argument("--config", default=None,
                        help="path to extraction_config.yaml (required unless --summarize)")
    parser.add_argument("--text", default=None, help="the text to scan (default: read stdin)")
    parser.add_argument("--context", default=None,
                        help="optional context text; patterns whose context.positive words appear in it are the only ones run")
    parser.add_argument("--report", action="store_true",
                        help=f"write the decision log to ./{REPORT_PATH} (overwritten)")
    parser.add_argument("--error-log", action="store_true",
                        help=f"write ./{ERROR_LOG_PATH} (overwritten): raw context and text of a context miss")
    parser.add_argument("--summarize", nargs="?", const=REPORT_PATH, default=None,
                        metavar="PATH",
                        help=f"print a per-label-pair overlap summary of a report CSV "
                             f"(default ./{REPORT_PATH}) and exit")
    args = parser.parse_args(argv)

    if args.summarize is not None:
        clashes = [flag for flag, used in (
            ("--text", args.text is not None), ("--context", args.context is not None),
            ("--report", args.report), ("--error-log", args.error_log),
        ) if used]
        if clashes:
            parser.error(f"--summarize cannot be combined with {', '.join(clashes)}")
        try:
            rows = summarize_overlaps(args.summarize)
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(format_summary(rows))
        return 0
    if args.config is None:
        parser.error("the following arguments are required: --config (unless --summarize)")

    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = load_config(args.config)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for w in caught:
        print(f"warning: {w.message}", file=sys.stderr)

    text = args.text
    if text is None:
        if sys.stdin.isatty():
            parser.error("no text: pass --text TEXT or pipe the text on stdin")
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")

    matches, decisions = extract((args.context, text), cfg)

    if args.report:
        reset_report()
        write_report_csv(decisions)
    if args.error_log:
        reset_error_log()
        for d in decisions:
            if d["kind"] == "context_miss":
                log_context_miss("", args.context or "", text, d)

    json.dump([m.to_dict() for m in matches], sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
