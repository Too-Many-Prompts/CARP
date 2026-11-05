"""Regex extractor: find regex matches, score them by nearby words, resolve overlaps.

Library use:
    cfg = load_config("extraction_config.yaml")
    matches = extract("some text", cfg)
    matches = extract({"text": "...", "context": "..."}, cfg)
    matches, decisions = extract_with_report(("context text", "text"), cfg)

CLI:
    extract.py --config extraction_config.yaml FILE... [--format json|csv]
               [--context TEXT]

Files written to the working directory:
    errorlog.csv       context selected patterns (by positive keywords) but nothing was extracted
    extract_report.csv one row per decision (context, min_score, overlaps)
Both are overwritten once per CLI run. Library callers can use
write_report_csv(decisions, source) with the log from extract_with_report().
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence, Union

import yaml

from words import next_words, prev_words

TIEBREAK_NAMES = ("score", "length", "priority")
CONTEXT_DIRECTIONS = ("before", "after", "both")

# Written (relative to the working directory) whenever a context selected
# patterns but nothing was extracted. main() resets it once per CLI run.
ERROR_LOG_PATH = "errorlog.csv"
ERROR_LOG_FIELDS = ("timestamp", "file", "context", "text", "context_patterns", "reason")

# The decision report (extract_with_report's log) as CSV; main() resets it once per run.
REPORT_PATH = "extract_report.csv"
_CAND_COLS = ("label", "text", "start", "end", "score")
REPORT_FIELDS = (
    ("file", "kind", "outcome", "reason", "patterns")
    + tuple(f"a_{k}" for k in _CAND_COLS)
    + tuple(f"b_{k}" for k in _CAND_COLS)
)
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
    weights: dict[str, float]  # lowercase keyword/phrase -> signed weight


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


@dataclass
class Decision:
    """One entry of the troubleshooting log.

    kind: "context" | "context_miss" | "below_min_score" | "overlap" | "coexist"
    restricted: True on a "context" decision when the context hit some
    patterns and so limited which ones run on the text.
    """

    kind: str
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    winner: Candidate | None = None
    patterns: list[str] = field(default_factory=list)
    restricted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "patterns": list(self.patterns),
            "candidates": [c.to_dict() for c in self.candidates],
            "winner": self.winner.to_dict() if self.winner else None,
        }


# --------------------------------------------------------------------------
# 1. load_config
# --------------------------------------------------------------------------
def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ValueError(f"{where}: missing required key '{key}'")
    return mapping[key]


def _check_keyword(
    label: str, group: str, key: str, weight: float, seen: dict[str, float]
) -> None:
    """Fail loudly on keywords the scorer could never match or could mis-score.

    Context words are ASCII letters only (see words.py), so a keyword with a
    digit or an accented letter would be silently reduced and match the wrong
    words. A keyword may not appear twice in a pattern, and the weight's sign
    must agree with its group (positive > 0, negative < 0).
    """
    where = f"pattern '{label}': context.{group} keyword '{key}'"
    if not _keyword_words(key) or any(c.isalnum() and not ("a" <= c <= "z") for c in key):
        raise ValueError(
            f"{where} must contain only ASCII letters (digits and accented "
            "letters can never match a context word)"
        )
    if key in seen:
        raise ValueError(f"{where} is listed more than once in this pattern")
    if (group == "positive" and weight < 0) or (group == "negative" and weight > 0):
        raise ValueError(
            f"{where} has weight {weight:g}; positive weights must be >= 0 "
            "and negative weights <= 0"
        )


def load_config(path: str | Path) -> Config:
    """Read the YAML config and compile every regex once."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = yaml.safe_load(fh)

    settings = _require(raw, "settings", "config")
    for key in REQUIRED_SETTINGS:
        _require(settings, key, "settings")
    for name in settings["overlap_tiebreak"]:
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

        weights: dict[str, float] = {}
        context = entry.get("context") or {}
        for group in ("positive", "negative"):
            for keyword, weight in (context.get(group) or {}).items():
                key = str(keyword).lower()
                _check_keyword(label, group, key, float(weight), weights)
                weights[key] = float(weight)
        patterns.append(
            Pattern(
                label=label,
                priority=int(_require(entry, "priority", f"pattern '{label}'")),
                regex=compiled,
                weights=weights,
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
# Context: choose which patterns run on the input text
# --------------------------------------------------------------------------
def select_patterns(
    context: str | None, patterns: list[Pattern]
) -> tuple[list[Pattern], Decision]:
    """Return the patterns to run on the input text.

    A pattern "matches" the context when any of its positive keywords/phrases
    (weight > 0 under `context.positive` in the YAML) occurs in it as whole
    words, case-insensitively. The pattern regexes are NOT run on the context.
    If at least one pattern matches, only those run on the text. With no
    context, or no matches, all run.
    """
    if not context or not context.strip():
        return list(patterns), Decision(
            "context", "no context supplied, all patterns used",
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
        return list(patterns), Decision(
            "context", "no context hits, all patterns used",
            patterns=[p.label for p in patterns],
        )
    return hits, Decision(
        "context", f"context matched {', '.join(found)}; only these patterns used",
        patterns=[p.label for p in hits], restricted=True,
    )


def _context_words(context: str) -> list[str]:
    """Lowercase letter-only words of the context (same reduction as keywords)."""
    return _keyword_words(context)


def _context_hits(words: list[str], pattern: Pattern) -> list[str]:
    """Positive keywords of `pattern` that occur in `words` as a contiguous phrase."""
    hits: list[str] = []
    for keyword, weight in pattern.weights.items():
        kw = _keyword_words(keyword)
        size = len(kw)
        if weight <= 0 or size == 0:
            continue
        if any(words[i:i + size] == kw for i in range(len(words) - size + 1)):
            hits.append(keyword)
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


def log_context_miss(
    source: str, context: str, text: str, labels: list[str], reason: str
) -> None:
    """Append one row to ERROR_LOG_PATH (header written if the file is new)."""
    _append_csv_rows(ERROR_LOG_PATH, ERROR_LOG_FIELDS, [{
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "file": source,
        "context": context,
        "text": text,
        "context_patterns": ";".join(labels),
        "reason": reason,
    }])


# --------------------------------------------------------------------------
# Report CSV: one flat row per decision
# --------------------------------------------------------------------------
def _cand_columns(prefix: str, cand: Candidate | None) -> dict[str, Any]:
    data = cand.to_dict() if cand else {}
    return {f"{prefix}_{k}": data.get(k, "") for k in _CAND_COLS}


def decision_rows(decisions: list[Decision], source: str = "") -> list[dict[str, Any]]:
    """Map decisions to flat report rows.

    a_* / b_* are the candidates involved. overlap: a = winner, b = dropped.
    below_min_score: a = dropped. coexist: a and b are both kept.
    """
    rows: list[dict[str, Any]] = []
    for d in decisions:
        a: Candidate | None = None
        b: Candidate | None = None
        if d.kind == "overlap":
            a = d.winner
            b = next((c for c in d.candidates if c is not d.winner), None)
            outcome = "a_wins_b_dropped"
        elif d.kind == "below_min_score":
            a = d.candidates[0] if d.candidates else None
            outcome = "dropped"
        elif d.kind == "coexist":
            a, b = d.candidates[0], d.candidates[1]
            outcome = "both_kept"
        elif d.kind == "context_miss":
            outcome = "context_miss"
        else:  # context
            outcome = "patterns_restricted" if d.restricted else "all_patterns_used"
        rows.append({
            "file": source,
            "kind": d.kind,
            "outcome": outcome,
            "reason": d.reason,
            "patterns": ";".join(d.patterns),
            **_cand_columns("a", a),
            **_cand_columns("b", b),
        })
    return rows


def reset_report(path: str = REPORT_PATH) -> None:
    """Overwrite the report CSV with just the header row."""
    _reset_csv(path, REPORT_FIELDS)


def write_report_csv(
    decisions: list[Decision], source: str = "", path: str = REPORT_PATH
) -> None:
    """Append the decisions as rows to the report CSV (header if file is new)."""
    _append_csv_rows(path, REPORT_FIELDS, decision_rows(decisions, source))


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
    """Sum the weight of each keyword found near the match (nearest occurrence
    only), divided by word distance when distance_decay is true.

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

    total = 0.0
    for keyword, weight in pattern.weights.items():
        kw = _keyword_words(keyword)
        if not kw:
            continue
        dist = _nearest_distance(before, after, kw)
        if dist is None:
            continue
        total += weight / dist if decay else weight
    return total


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


def _why_wins(winner: Candidate, loser: Candidate, order: Sequence[str]) -> str:
    """Explain which tiebreak criterion decided, naming the ones that tied."""
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
        return "; ".join([f"{t} tied" for t in tied] + [detail])
    return "all tiebreak criteria tied; kept earlier start/label"


def resolve_overlaps(
    cands: list[Candidate], cfg: Config
) -> tuple[list[Candidate], list[Decision]]:
    """Drop candidates below min_score, then resolve overlaps greedily.

    Candidates are ranked by overlap_tiebreak (in config order). Each is
    accepted unless it overlaps an already-accepted candidate whose label
    pair is not in `coexist`. Every decision is logged with its reason.
    """
    settings = cfg.settings
    order = settings["overlap_tiebreak"]
    min_score = settings["min_score"]
    decisions: list[Decision] = []

    survivors: list[Candidate] = []
    for c in cands:
        if c.score < min_score:
            decisions.append(
                Decision("below_min_score",
                         f"score {c.score:.2f} < min_score {min_score}", [c])
            )
        else:
            survivors.append(c)

    accepted: list[Candidate] = []
    for c in sorted(survivors, key=lambda x: _rank_key(x, order)):
        pending: list[Decision] = []
        beaten = False
        for a in accepted:
            if not _overlaps(a, c):
                continue
            if frozenset((a.label, c.label)) in cfg.coexist:
                pending.append(
                    Decision("coexist",
                             f"{a.label}/{c.label} pair is in coexist; both kept",
                             [a, c])
                )
            else:
                decisions.append(
                    Decision("overlap", _why_wins(a, c, order), [a, c], winner=a)
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


def extract_with_report(
    inp: ExtractInput, cfg: Config, source: str = ""
) -> tuple[list[Candidate], list[Decision]]:
    """Run steps 2-5 and return (matches, decision log).

    If the context selected patterns but nothing was extracted, the raw
    context and text are appended to errorlog.csv (`source` fills its
    `file` column) and a "context_miss" decision is logged.
    """
    raw_text, context = _split_input(inp)
    text = normalize(raw_text, cfg)
    norm_context = normalize(context, cfg) if context else None

    patterns, ctx_decision = select_patterns(norm_context, cfg.patterns)
    by_label = {p.label: p for p in cfg.patterns}

    cands = find_candidates(text, patterns)
    for c in cands:
        c.score = score_candidate(text, c, by_label[c.label], cfg.settings)

    matches, decisions = resolve_overlaps(cands, cfg)
    decisions = [ctx_decision] + decisions

    if ctx_decision.restricted and not matches:
        labels = ctx_decision.patterns
        if cands:
            reason = (f"context matched {', '.join(labels)}; {len(cands)} candidate(s) "
                      "found in text but all dropped (min_score/overlap)")
        else:
            reason = f"context matched {', '.join(labels)}; no matches in text"
        decisions.append(Decision("context_miss", reason, patterns=labels))
        log_context_miss(source, context or "", raw_text, labels, reason)
    return matches, decisions


def extract(inp: ExtractInput, cfg: Config, source: str = "") -> list[Candidate]:
    """Extract matches from text (optionally with context). See module docs."""
    return extract_with_report(inp, cfg, source)[0]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract regex matches with context scoring.")
    parser.add_argument("--config", required=True, help="path to extraction_config.yaml")
    parser.add_argument("files", nargs="+", metavar="FILE")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--context", default=None,
                        help="optional context text; patterns whose positive keywords appear in it are the only ones run")
    args = parser.parse_args(argv)

    # One fresh errorlog.csv and report per CLI run, even if the run fails
    # below, so a failed run never leaves the previous run's files behind.
    reset_error_log()
    reset_report()

    try:
        cfg = load_config(args.config)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    texts: list[tuple[str, str]] = []
    for name in args.files:  # read everything first: no partial output on a bad path
        try:
            texts.append((name, Path(name).read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            print(f"error: cannot read {name}: {exc.strerror or exc}", file=sys.stderr)
            return 2

    rows: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for name, text in texts:
        matches, decisions = extract_with_report((args.context, text), cfg, source=name)
        rows.extend({"file": name, **m.to_dict()} for m in matches)
        log.extend({"file": name, **d.to_dict()} for d in decisions)
        write_report_csv(decisions, source=name)

    fields = ["file", "label", "text", "start", "end", "score"]
    if args.format == "json":
        json.dump({"matches": rows, "overlaps": log}, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        for entry in log:  # keep CSV columns clean: decisions go to stderr
            print(json.dumps(entry, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
