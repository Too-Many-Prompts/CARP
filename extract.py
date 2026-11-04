"""Regex extractor: find regex matches, score them by nearby words, resolve overlaps.

Library use:
    cfg = load_config("extraction_config.yaml")
    matches = extract("some text", cfg)
    matches = extract({"text": "...", "context": "..."}, cfg)
    matches, decisions = extract_with_report(("context text", "text"), cfg)

CLI:
    extract.py --config extraction_config.yaml FILE... [--format json|csv]
               [--context TEXT]
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
                weights[key] = weights.get(key, 0.0) + float(weight)
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

    A pattern "matches" the (already normalized) context on any raw regex hit.
    If at least one does, only those run. With no context, or no hits, all run.
    """
    if not context or not context.strip():
        return list(patterns), Decision(
            "context", "no context supplied, all patterns used",
            patterns=[p.label for p in patterns],
        )
    hits = [p for p in patterns if p.regex.search(context) is not None]
    if not hits:
        return list(patterns), Decision(
            "context", "no context hits, all patterns used",
            patterns=[p.label for p in patterns],
        )
    labels = [p.label for p in hits]
    return hits, Decision(
        "context", f"context matched {', '.join(labels)}; only these patterns used",
        patterns=labels, restricted=True,
    )


# --------------------------------------------------------------------------
# Error log: context selected patterns but nothing was extracted
# --------------------------------------------------------------------------
def reset_error_log() -> None:
    """Overwrite ERROR_LOG_PATH with just the header row."""
    with open(ERROR_LOG_PATH, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(ERROR_LOG_FIELDS)


def log_context_miss(
    source: str, context: str, text: str, labels: list[str], reason: str
) -> None:
    """Append one row to ERROR_LOG_PATH (header written if the file is new)."""
    path = Path(ERROR_LOG_PATH)
    is_new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ERROR_LOG_FIELDS, lineterminator="\n")
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "file": source,
            "context": context,
            "text": text,
            "context_patterns": ";".join(labels),
            "reason": reason,
        })


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
    """Reduce a keyword/phrase to the same lowercase letter-only words that
    prev_words/next_words produce."""
    parts = (re.sub(r"[^a-z]", "", tok) for tok in keyword.lower().split())
    return [p for p in parts if p]


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

    Keyword matching is always lowercase; case_sensitive only affects regex.
    """
    n = int(settings["context_window_words"])
    direction = settings.get("context_direction", "both")
    before = prev_words(text, cand.start, n) if direction in ("before", "both") else []
    after = next_words(text, cand.end, n) if direction in ("after", "both") else []
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
                        help="optional context text; patterns matching it are the only ones run")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    reset_error_log()  # one fresh errorlog.csv per CLI run

    rows: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for name in args.files:
        text = Path(name).read_text(encoding="utf-8", errors="replace")
        matches, decisions = extract_with_report((args.context, text), cfg, source=name)
        rows.extend({"file": name, **m.to_dict()} for m in matches)
        log.extend({"file": name, **d.to_dict()} for d in decisions)

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
