"""Regex extractor: find regex matches, score them by nearby words and an
optional context string, resolve overlaps.

Library use:
    cfg = load_config("extraction_config.yaml")
    matches, decisions = extract("some text", cfg)
    matches, _ = extract({"text": "...", "context": "..."}, cfg)   # ignore the log
    matches, decisions = extract(("context text", "text"), cfg, source="id-1")

extract() writes no files; the caller decides what to do with `decisions`
(flat dicts, columns REPORT_FIELDS). Optional helpers: write_report_csv(),
open_report(), reset_report(), summarize_overlaps() / format_summary(),
summarize_misses() / format_misses() and summarize_validation() /
format_validation() (defined in summarize.py).

Phone numbers: a pattern with a `validate:` block is checked with the
phonenumbers library (phone.py); its matches carry `extra` (normalized
number, region, status, ...).

Scoring: every pattern runs on the text. A candidate's score is
    text_score    (its `nearby` + `keywords` words in the text window)
  + context_score (its `context` + `keywords` words in the context string,
                   all words read, the context's last word at distance 1).

CLI (one text per run; for many texts use the library):
    extract.py --config extraction_config.yaml [--text TEXT | < stdin]
               [--context TEXT] [--report]
    extract.py --summarize [PATH]
Prints the matches as a JSON list. Nothing is written unless asked:
    --report     ./extract_report.csv  one row per decision (overwritten)

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
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, Union

import yaml

import phone
from words import next_words, prev_words
# The report summaries live in summarize.py (a leaf module); re-exported here.
from summarize import (
    DEFAULT_REPORT_PATH,
    format_misses,
    format_summary,
    format_validation,
    summarize_misses,
    summarize_overlaps,
    summarize_validation,
)

TIEBREAK_NAMES = ("score", "length", "priority")
CONTEXT_DIRECTIONS = ("before", "after", "both")

# The decision report (extract's log) as CSV; main() resets it once per run.
REPORT_PATH = DEFAULT_REPORT_PATH       # "extract_report.csv", defined in summarize.py
_CAND_COLS = ("label", "text", "start", "end", "score", "text_score", "context_score", "family")
# Phone validation columns; blank on every other row kind.
_PHONE_COLS = ("status", "region", "normalized")
REPORT_FIELDS = (
    ("file", "kind", "outcome", "reason")
    + tuple(f"a_{k}" for k in _CAND_COLS)
    + tuple(f"b_{k}" for k in _CAND_COLS)
    + _PHONE_COLS
    + ("decided_by",)
)
# decided_by: overlap -> score | length | priority | start_label;
# coexist -> coexist; below_min_score -> min_score; miss -> "";
# validation -> the region source (phone.SOURCES: number | hint | list).
# miss outcome: no_candidates (regex gap) | candidates_dropped (tuning).
# validation outcome: kept | not_returned (a reading filtered out) | dropped (impossible).
REQUIRED_SETTINGS = (
    "context_window_words",
    "distance_decay",
    "min_score",
    "case_sensitive",
    "overlap_tiebreak",
)
OLD_CONTEXT_KEYS = ("positive", "penalize", "negative")

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
    nearby: dict[str, float] = field(default_factory=dict)    # text window: signed weight
    context: dict[str, float] = field(default_factory=dict)   # context string: signed weight
    validate: phone.PhoneSpec | None = None                   # the `validate:` block, if any


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
    score: float = 0            # text_score + context_score
    text_score: float = 0
    context_score: float = 0
    # Set only on matches from a pattern with `validate`: normalized, region,
    # region_source, status, number_type, readings.
    extra: dict[str, Any] | None = None
    # The pattern label when `label` is generated (valid_US_number ->
    # phone_number); "" means the same as label. coexist and misses use it.
    family: str = ""
    # Readings of one phone hit share a hit id and never conflict (-1: none).
    hit: int = -1

    @property
    def group(self) -> str:
        """The pattern label this match belongs to."""
        return self.family or self.label

    def to_dict(self) -> dict[str, Any]:
        """The CLI JSON form: label, text, start, end, score (+ extra if set)."""
        out: dict[str, Any] = {
            "label": self.label,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "score": round(float(self.score), 4),
        }
        if self.extra is not None:
            out["extra"] = dict(self.extra)
        return out

    def report_dict(self) -> dict[str, Any]:
        """to_dict() plus the score breakdown, for the report columns."""
        return {
            **{k: v for k, v in self.to_dict().items() if k != "extra"},
            "text_score": round(float(self.text_score), 4),
            "context_score": round(float(self.context_score), 4),
            "family": self.group,
        }


# One entry of the troubleshooting log: a flat dict with the REPORT_FIELDS
# columns (file, kind, outcome, reason, a_*, b_*, decided_by). It is the
# single form used for extract_report.csv.
#
# kind: "below_min_score" | "overlap" | "coexist" | "miss" | "validation"
Decision = dict[str, Any]


def _cand_columns(prefix: str, cand: Candidate | None) -> dict[str, Any]:
    data = cand.report_dict() if cand else {}
    return {f"{prefix}_{k}": data.get(k, "") for k in _CAND_COLS}


def _decision(
    kind: str,
    outcome: str,
    reason: str,
    a: Candidate | None = None,
    b: Candidate | None = None,
    decided_by: str = "",
    **phone_cols: Any,
) -> Decision:
    """Build one log row. a/b are the candidates involved:
    overlap: a = winner, b = dropped. below_min_score: a = dropped.
    coexist: a and b are both kept. validation: a = the checked candidate,
    with phone_cols status / region / normalized. The `file` column is
    filled in later. `decided_by` names the rule that decided (see REPORT_FIELDS).
    """
    return {
        "file": "",
        "kind": kind,
        "outcome": outcome,
        "reason": reason,
        **_cand_columns("a", a),
        **_cand_columns("b", b),
        **{k: phone_cols.get(k, "") or "" for k in _PHONE_COLS},
        "decided_by": decided_by,
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
    A keyword may not appear twice under the same key of a pattern, also
    not spelled differently ("amount-due" and "amount due" are the same words).
    """
    where = f"pattern '{label}': {key_name} keyword '{key}'"
    if not _keyword_words(key) or any(c.isalnum() and not ("a" <= c <= "z") for c in key):
        raise ValueError(
            f"{where} must contain only ASCII letters (digits and accented "
            "letters can never match a word)"
        )
    other = _same_words(key, seen)
    if other is not None:
        same = _same_suffix(key, other)
        raise ValueError(f"{where} is listed more than once in this pattern{same}")


def _same_suffix(key: str, other: str) -> str:
    """Error-message suffix naming the other spelling, if it differs."""
    return "" if other == key else f" (same words as '{other}')"


def _same_words(key: str, seen: Iterable[str]) -> str | None:
    """The first keyword in `seen` that reduces to the same words as `key`."""
    words = _keyword_words(key)
    return next((k for k in seen if _keyword_words(k) == words), None)


def _require_str(label: str, key_name: str, keyword: Any) -> None:
    """YAML reads some bare words as non-strings (on/yes/true -> True,
    off/no/false -> False, null/~ -> None); reject them instead of silently
    turning them into the keywords "true", "false" or "none". A blank entry
    (a bare `-` list item or an empty key) also loads as None."""
    if keyword is None:
        raise ValueError(f"pattern '{label}': {key_name} has an empty entry; remove it")
    if not isinstance(keyword, str):
        raise ValueError(
            f"pattern '{label}': {key_name} keyword {keyword!r} must be a string "
            '(quote it in the YAML, e.g. "on")'
        )


def _weight_map(label: str, key_name: str, raw: Any) -> dict[str, float]:
    """Validate a `word: signed weight` map (`keywords`, `nearby` or `context`)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"pattern '{label}': {key_name} must be a map of word: weight, "
            f"got {type(raw).__name__}"
        )
    out: dict[str, float] = {}
    for keyword, weight in raw.items():
        _require_str(label, key_name, keyword)
        key = keyword.lower()
        _check_keyword(label, key_name, key, out)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(
                f"pattern '{label}': {key_name} keyword '{key}' needs a numeric "
                f"weight, got {weight!r}"
            )
        if weight == 0:
            raise ValueError(
                f"pattern '{label}': {key_name} keyword '{key}' has weight 0; "
                "give it a sign or remove it"
            )
        out[key] = float(weight)
    return out


def _check_old_context(label: str, raw: Any) -> None:
    """Fail on the old `context: {positive, penalize, negative}` shape.

    Decided by the value, not just the key: `negative: -2` is a valid new
    keyword ("negative balance"); a list, map or blank value is the old shape."""
    if not isinstance(raw, dict):
        return
    old = [k for k in OLD_CONTEXT_KEYS
           if k in raw and (raw[k] is None or isinstance(raw[k], (list, dict)))]
    if old:
        raise ValueError(
            f"pattern '{label}': `context.{old[0]}` is the old format. Patterns no "
            "longer select or penalize; `context` is now a map of word: signed "
            "weight added to the score when the word is in the context string "
            "(e.g. context: {acct: 3, phone: -2}). Use `keywords` for words that "
            "should count in both the text and the context."
        )


def _load_keywords(
    label: str, entry: dict[str, Any]
) -> tuple[dict[str, float], dict[str, float]]:
    """Return (nearby, context) weight maps for one pattern: the explicit
    keys merged with the `keywords` shorthand, which goes into both."""
    _check_old_context(label, entry.get("context"))
    nearby = _weight_map(label, "nearby", entry.get("nearby"))
    context = _weight_map(label, "context", entry.get("context"))
    shorthand = _weight_map(label, "keywords", entry.get("keywords"))
    for key in shorthand:
        for where, seen in (("nearby", nearby), ("context", context)):
            other = _same_words(key, seen)
            if other is not None:
                same = _same_suffix(key, other)
                raise ValueError(
                    f"pattern '{label}': keyword '{key}' is in both keywords and {where}{same}"
                )
    for key, weight in shorthand.items():
        nearby[key] = weight
        context[key] = weight
    return nearby, context


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
        if name in ("context_penalty", "context_negative"):
            raise ValueError(
                f"settings.overlap_tiebreak: `{name}` was removed; the context now "
                "adds to `score` (pattern `context` / `keywords` weights). Remove it."
            )
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

        nearby, context = _load_keywords(label, entry)
        spec = (
            phone.load_validate(label, entry["validate"], _weight_map)
            if entry.get("validate") is not None else None
        )
        patterns.append(
            Pattern(
                label=label,
                priority=int(_require(entry, "priority", f"pattern '{label}'")),
                regex=compiled,
                nearby=nearby,
                context=context,
                validate=spec,
            )
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
# Report CSV: one flat row per decision
# --------------------------------------------------------------------------
def reset_report(path: str = REPORT_PATH) -> None:
    """Overwrite the report CSV with just the header row."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(REPORT_FIELDS)


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
# 4. scoring: score_candidate (text) and context_score (context)
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
    """The one keyword matcher behind the text score and the context score.

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


def _window(
    text: str, start: int, end: int, settings: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """The text window around a span: (words before, words after), per
    context_window_words, context_direction and stop_at."""
    n = int(settings["context_window_words"])
    direction = settings.get("context_direction", "both")
    stop_at = settings.get("stop_at", "")
    before = (
        prev_words(text, start, n, stop_at=stop_at)
        if direction in ("before", "both") else []
    )
    after = (
        next_words(text, end, n, stop_at=stop_at)
        if direction in ("after", "both") else []
    )
    return before, after


def score_candidate(
    text: str, cand: Candidate, pattern: Pattern, settings: dict[str, Any]
) -> float:
    """The text score: sum the weight of each `nearby` keyword (which includes
    the `keywords` shorthand) found near the match (nearest occurrence only),
    divided by word distance when distance_decay is true.

    settings["context_direction"] ("before", "after" or "both", default
    "both") chooses which side(s) of the match are searched, up to
    context_window_words words per side.
    settings["stop_at"] (default "") lists characters, e.g. ".!?", that end
    the scan early on either side.

    Keyword matching is always lowercase; case_sensitive only affects regex.
    """
    before, after = _window(text, cand.start, cand.end, settings)
    decay = bool(settings["distance_decay"])
    total, _ = _sum_keywords(
        pattern.nearby, lambda kw: _nearest_distance(before, after, kw), decay
    )
    return total


def context_words(context: str | None) -> list[str]:
    """All words of the context, reduced exactly like the text window's words
    (words.next_words). The leading space keeps the first word: next_words
    skips a word that the start position falls inside."""
    if not context:
        return []
    return next_words(" " + context, 0, len(context) + 1)


def context_score(
    words: list[str], pattern: Pattern, settings: dict[str, Any]
) -> tuple[float, list[str]]:
    """The context score of a pattern: (sum, keywords found).

    Same rules as the text score, applied to every word of the context
    (`words`, from context_words()): no window, no context_direction, no
    stop_at. The context is read as if it came right before the match, so
    its LAST word is distance 1 and a phrase's last word sets its distance.
    It does not depend on any one match.
    """
    decay = bool(settings["distance_decay"])
    return _sum_keywords(
        pattern.context, lambda kw: _nearest_distance(words, [], kw), decay
    )


# --------------------------------------------------------------------------
# 4b. phone validation (patterns with a `validate:` block; see phone.py)
# --------------------------------------------------------------------------
# One side's region evidence: (score, nearest distance, hit words nearest first).
_Side = tuple[float, Union[int, None], list[str]]


def _hint_side(weights: dict[str, float], find: DistanceFinder, decay: bool) -> _Side:
    """Score a region's keywords with the shared matcher, plus the nearest hit."""
    total, hits = _sum_keywords(weights, find, decay)
    dists = sorted((find(_keyword_words(k)) or 0, k) for k in hits)
    return total, (dists[0][0] if dists else None), [k for _, k in dists]


def _context_hints(
    spec: phone.PhoneSpec, words: list[str], settings: dict[str, Any]
) -> dict[str, _Side]:
    """The context part of every hinted region; once per pattern."""
    decay = bool(settings["distance_decay"])
    return {
        region: _hint_side(weights, lambda kw: _nearest_distance(words, [], kw), decay)
        for region, weights in spec.hints.items()
    }


def _region_hints(
    text: str, start: int, end: int, spec: phone.PhoneSpec,
    ctx: dict[str, _Side], settings: dict[str, Any],
) -> list[phone.RegionHint]:
    """Every region's keyword evidence for the span: the text window (same
    rules as the text score) plus the precomputed context part."""
    before, after = _window(text, start, end, settings)
    decay = bool(settings["distance_decay"])
    hints: list[phone.RegionHint] = []
    for region, weights in spec.hints.items():
        t_score, t_dist, t_words = _hint_side(
            weights, lambda kw: _nearest_distance(before, after, kw), decay)
        c_score, c_dist, c_words = ctx.get(region, (0.0, None, []))
        hints.append(phone.RegionHint(
            region, t_score + c_score, t_dist, c_dist,
            t_words + [w for w in c_words if w not in t_words],
        ))
    return hints


def _find_validated(
    text: str, pattern: Pattern, ctx: dict[str, _Side],
    settings: dict[str, Any],
) -> list[Candidate]:
    """Candidates of a pattern with `validate`: its regex hits, each trimmed
    or split by PhoneNumberMatcher (POSSIBLE leniency).

    The matcher runs in the top hinted region, else the first of `regions`. The regex scan resumes where the last refined
    number ended, not where the regex hit ended: a greedy hit that ran into
    the next number ("212-555-0123 212-555") would otherwise lose it. If the
    matcher finds nothing, the raw hit is kept as it is, so validation (not
    this step) decides its fate and logs it.
    """
    spec = pattern.validate
    assert spec is not None
    out: list[Candidate] = []
    pos = 0
    while pos <= len(text):
        m = pattern.regex.search(text, pos)
        if m is None:
            break
        if m.end() == m.start():
            pos = m.end() + 1          # empty matches carry no text to extract
            continue
        hints = _region_hints(text, m.start(), m.end(), spec, ctx, settings)
        top = max((h for h in hints if h.score > 0), key=lambda h: h.score, default=None)
        region = top.region if top else spec.regions[0]
        spans = phone.refine_spans(m.group(0), region)
        if not spans:
            out.append(Candidate(pattern.label, m.start(), m.end(), m.group(0), pattern.priority))
            pos = m.end()
            continue
        for s, e in spans:
            a, b = m.start() + s, m.start() + e
            out.append(Candidate(pattern.label, a, b, text[a:b], pattern.priority))
        pos = m.start() + spans[-1][1]
    return out


def validate_candidates(
    text: str, cands: list[Candidate], cfg: Config,
    ctx_hints: dict[str, dict[str, _Side]],
) -> tuple[list[Candidate], list[Decision]]:
    """Turn each phone hit (a candidate of a pattern with `validate`) into one
    match per returned reading, labeled valid_US_number, possible_GB_number...
    (phone.readings_for). The readings share the hit's span and score and
    never conflict with each other. Logs one `validation` row per reading
    (kept / not_returned), or one `dropped` row when the hit is impossible
    everywhere. Runs after scoring and before min_score/overlaps; other
    candidates pass through."""
    by_label = {p.label: p for p in cfg.patterns}
    kept: list[Candidate] = []
    decisions: list[Decision] = []
    version = ""
    for hit_id, c in enumerate(cands):
        spec = by_label[c.label].validate
        if spec is None:
            kept.append(c)
            continue
        version = version or phone.pn_version()
        hints = _region_hints(text, c.start, c.end, spec, ctx_hints[c.label], cfg.settings)
        out = phone.readings_for(c.text, spec, hints)
        tail = out.notes + [f"phonenumbers {version}"]
        if not out.readings:
            c.family = c.label
            decisions.append(_decision(
                "validation", "dropped", "; ".join([out.why] + tail), a=c,
                decided_by=out.source, status="impossible", normalized=c.text,
            ))
            continue
        returned = out.returned
        for r in out.readings:
            m = replace(c, label=r.label, family=c.label, hit=hit_id, extra={
                "normalized": r.normalized, "region": r.region, "status": r.status,
                "region_source": r.source, "number_type": r.number_type,
                "readings": len(returned),
            })
            verdict = "kept" if r.returned else "not_returned"
            head = f"{r.status} in {r.region}" + (f"; {r.why}" if r.why else "")
            decisions.append(_decision(
                "validation", verdict, "; ".join([head] + tail), a=m,
                decided_by=r.source, status=r.status, region=r.region,
                normalized=r.normalized,
            ))
            if r.returned:
                kept.append(m)
    return kept, decisions


# --------------------------------------------------------------------------
# 5. resolve_overlaps
# --------------------------------------------------------------------------
def _overlaps(a: Candidate, b: Candidate) -> bool:
    if a.start == b.start and a.end == b.end:
        return True  # identical spans count as overlapping
    return a.start < b.end and b.start < a.end


def _rank_key(c: Candidate, order: Sequence[str]) -> tuple:
    parts: list[Any] = []
    for name in order:
        if name == "score":
            parts.append(-c.score)
        elif name == "length":
            parts.append(-(c.end - c.start))
        else:  # priority: lower number wins
            parts.append(c.priority)
    parts.extend((c.start, c.label))  # deterministic final tie-break
    return tuple(parts)


def _why_wins(
    winner: Candidate, loser: Candidate, order: Sequence[str]
) -> tuple[str, str]:
    """Return (criterion, reason): the tiebreak criterion that decided and a
    text naming the ones that tied. criterion is "start_label" when every
    listed criterion tied (or the list is empty)."""
    tied: list[str] = []
    for name in order:
        if name == "score" and winner.score != loser.score:
            detail = f"score {winner.score:.2f} > {loser.score:.2f}"
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
    cands: list[Candidate], cfg: Config
) -> tuple[list[Candidate], list[Decision]]:
    """Drop candidates below min_score, then resolve overlaps greedily.

    Candidates are ranked by overlap_tiebreak (in config order); if it is
    empty or everything ties, the earlier start, then the label, decides.
    Each candidate is accepted unless it overlaps an already-accepted one
    whose label pair is not in `coexist`. Every decision is logged.
    """
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
    for c in sorted(survivors, key=lambda x: _rank_key(x, order)):
        pending: list[Decision] = []
        beaten = False
        for a in accepted:
            if not _overlaps(a, c) or (a.hit >= 0 and a.hit == c.hit):
                continue                # readings of one phone hit never conflict
            if (frozenset((a.label, c.label)) in cfg.coexist
                    or frozenset((a.group, c.group)) in cfg.coexist):
                pending.append(
                    _decision("coexist", "both_kept",
                              f"{a.label}/{c.label} pair is in coexist; both kept",
                              a=a, b=c, decided_by="coexist")
                )
            else:
                criterion, reason = _why_wins(a, c, order)
                decisions.append(
                    _decision("overlap", "a_wins_b_dropped", reason, a=a, b=c,
                              decided_by=criterion)
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


def _miss_decisions(
    cfg: Config,
    ctx_scores: dict[str, tuple[float, list[str]]],
    cands: list[Candidate],
    matches: list[Candidate],
) -> list[Decision]:
    """One `miss` row per label the context scored above 0 that has no match.
    Labels are compared by pattern (Candidate.group), so a valid_US_number
    match counts for its phone pattern."""
    kept = {m.group for m in matches}
    found: dict[str, int] = {}
    for c in cands:
        found[c.group] = found.get(c.group, 0) + 1
    rows: list[Decision] = []
    for p in cfg.patterns:
        score, hits = ctx_scores[p.label]
        if score <= 0 or p.label in kept:
            continue
        n = found.get(p.label, 0)
        outcome, why = (
            ("candidates_dropped", f"{n} candidate(s) dropped (min_score/overlap/validation)") if n
            else ("no_candidates", "no candidates in text")
        )
        reason = f"context scored {p.label} {score:+.2f} ({', '.join(hits)}); {why}"
        row = _decision("miss", outcome, reason)
        row["a_label"] = p.label
        row["a_context_score"] = round(score, 4)
        rows.append(row)
    return rows


def extract(
    inp: ExtractInput, cfg: Config, source: str = ""
) -> tuple[list[Candidate], list[Decision]]:
    """Run steps 2-5 and return (matches, decisions). Writes no files.

    Every pattern runs on the text. Each candidate's score is its text score
    plus its pattern's context score. `decisions` is the flat decision log;
    `source` fills its `file` column. It includes a "miss" row for each label
    the context scored above 0 that ended with no match. Ignore the log with
    `matches, _ = extract(...)`, or write it with write_report_csv() /
    open_report().
    """
    raw_text, context = _split_input(inp)
    text = normalize(raw_text, cfg)
    words = context_words(normalize(context, cfg) if context else None)
    ctx_scores = {p.label: context_score(words, p, cfg.settings) for p in cfg.patterns}
    by_label = {p.label: p for p in cfg.patterns}
    ctx_hints = {
        p.label: _context_hints(p.validate, words, cfg.settings)
        for p in cfg.patterns if p.validate
    }

    cands = find_candidates(text, [p for p in cfg.patterns if not p.validate])
    for p in cfg.patterns:
        if p.validate:
            cands += _find_validated(text, p, ctx_hints[p.label], cfg.settings)
    cands.sort(key=lambda c: (c.start, c.end, c.label))
    for c in cands:
        c.text_score = score_candidate(text, c, by_label[c.label], cfg.settings)
        c.context_score = ctx_scores[c.label][0]
        c.score = c.text_score + c.context_score

    found = list(cands)              # before validation drops any, for the miss rows
    checked: list[Decision] = []
    if ctx_hints:
        cands, checked = validate_candidates(text, cands, cfg, ctx_hints)
    matches, decisions = resolve_overlaps(cands, cfg)
    decisions = checked + decisions + _miss_decisions(cfg, ctx_scores, found, matches)
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
                        help="optional context text; its words add each pattern's "
                             "context/keywords weights to that pattern's score")
    parser.add_argument("--report", action="store_true",
                        help=f"write the decision log to ./{REPORT_PATH} (overwritten)")
    parser.add_argument("--summarize", nargs="?", const=REPORT_PATH, default=None,
                        metavar="PATH",
                        help=f"print the overlap and miss summaries of a report CSV "
                             f"(default ./{REPORT_PATH}) and exit")
    args = parser.parse_args(argv)

    if args.summarize is not None:
        clashes = [flag for flag, used in (
            ("--text", args.text is not None), ("--context", args.context is not None),
            ("--report", args.report),
        ) if used]
        if clashes:
            parser.error(f"--summarize cannot be combined with {', '.join(clashes)}")
        try:
            rows = summarize_overlaps(args.summarize)
            misses = summarize_misses(args.summarize)
            checks = summarize_validation(args.summarize)
        except (ValueError, OSError, csv.Error) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(format_summary(rows))
        if misses:
            print()
            print(format_misses(misses))
        if checks:
            print()
            print(format_validation(checks))
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

    json.dump([m.to_dict() for m in matches], sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
