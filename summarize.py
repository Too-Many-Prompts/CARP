"""Summaries of a CARP report CSV (extract_report.csv).

    rows = summarize_overlaps()            # list of dicts, worst pairs first
    print(format_summary(rows))            # the overlap table `extract.py --summarize` prints
    misses = summarize_misses()            # list of dicts, most misses first
    print(format_misses(misses))           # the miss table printed under it
    checks = summarize_validation()        # phone validation, one row per label
    print(format_validation(checks))       # printed last, if there are any

The overlap table shows which label pairs overlap most often without being
resolved by score. The miss table shows which labels the context pointed to
but that produced no match. This module only reads report columns by name
(COLUMNS_USED); it imports nothing from extract.py, which re-exports the four
functions.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

DEFAULT_REPORT_PATH = "extract_report.csv"
# Report columns this module reads; all must be in extract.REPORT_FIELDS.
COLUMNS_USED = (
    "file", "kind", "a_label", "a_text", "a_start", "a_end",
    "b_label", "b_text", "b_start", "b_end", "decided_by", "outcome",
    "status", "normalized",
)

SUMMARY_CRITERIA = ("score", "length", "priority", "start_label")
SUMMARY_FIELDS = (
    ("pair", "overlaps")
    + tuple(f"by_{c}" for c in SUMMARY_CRITERIA)
    + ("not_by_score", "not_by_score_pct", "no_evidence", "wins", "examples", "hint")
)
_NO_EVIDENCE = ("priority", "start_label")   # fallback order, nothing in the input
_MAX_EXAMPLES = 3
HINT_NO_EVIDENCE = "add nearby, context or keywords words"
HINT_PARTIAL = "if both values are valid, add to coexist"
MISS_FIELDS = ("label", "misses", "no_candidates", "dropped", "examples", "hint")
HINT_MISS_REGEX = "the regex found nothing; check the regex against these inputs"
HINT_MISS_TUNING = "candidates lost; raise their nearby/context weights or check min_score"
VALIDATION_FIELDS = (
    "label", "checked", "valid", "possible", "impossible", "dropped", "ambiguous",
    "sources", "clusters", "examples", "hint",
)
_AMBIGUOUS_SOURCES = ("proximity", "format", "fallback")
_CLUSTER_MIN = 3        # possible numbers sharing leading digits before the hint shows
HINT_STALE = ("many possible numbers share these digits; phonenumbers metadata "
              "may be stale, upgrade it")
HINT_AMBIGUOUS = "ambiguous regions; add region hint words or pass region="
HINT_IMPOSSIBLE = "many impossible; tighten the regex or add try_regions"


@dataclass
class _PairStats:
    by: Counter = field(default_factory=Counter)        # decided_by -> count
    wins: Counter = field(default_factory=Counter)      # winning label -> count
    unsettled: int = 0                                  # not decided by score
    partial: int = 0                                    # ...of which spans differ
    examples: list[str] = field(default_factory=list)   # at most _MAX_EXAMPLES
    no_evidence_examples: int = 0                       # leading entries of `examples`

    def add_example(self, example: str, no_evidence: bool) -> None:
        """Keep the first no_evidence examples, then the first others, in
        report order, never holding more than _MAX_EXAMPLES."""
        if no_evidence:
            if self.no_evidence_examples < _MAX_EXAMPLES:
                self.examples.insert(self.no_evidence_examples, example)
                self.no_evidence_examples += 1
                del self.examples[_MAX_EXAMPLES:]
        elif len(self.examples) < _MAX_EXAMPLES:
            self.examples.append(example)


def _pct(part: int, whole: int) -> float:
    """part/whole as a percentage, one decimal, halves rounded up (6.25 -> 6.3)."""
    value = Decimal(100 * part) / Decimal(whole)
    return float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _example(row: dict[str, str]) -> str:
    return f'{row.get("file") or "(no id)"}: "{row.get("a_text", "")}" vs "{row.get("b_text", "")}"'


def summarize_overlaps(path: str = DEFAULT_REPORT_PATH) -> list[dict[str, Any]]:
    """Summarize a report CSV's `overlap` rows by label pair.

    Shows which pairs overlap most often without being resolved by score:
    counts per deciding criterion, `no_evidence` (decided by priority or
    start/label, i.e. nothing in the input), up to three example inputs and
    a hint. Rows are streamed; memory stays bounded per pair. Sorted by
    no_evidence, not_by_score, overlaps (highest first), then pair.
    Raises ValueError if the report has no `decided_by` column.
    """
    stats: dict[str, _PairStats] = {}
    fh, reader = _open_report(path)
    with fh:
        for row in reader:
            if row.get("kind") != "overlap":
                continue
            a, b = row.get("a_label", ""), row.get("b_label", "")
            st = stats.setdefault("/".join(sorted((a, b))), _PairStats())
            by = row.get("decided_by", "")
            st.by[by] += 1
            st.wins[a] += 1
            if by == "score":
                continue
            st.unsettled += 1
            spans_a = (row.get("a_start"), row.get("a_end"))
            if spans_a != (row.get("b_start"), row.get("b_end")):
                st.partial += 1
            st.add_example(_example(row), by in _NO_EVIDENCE)

    out: list[dict[str, Any]] = []
    for pair, st in stats.items():
        overlaps = sum(st.by.values())
        not_by_score = overlaps - st.by["score"]
        no_evidence = sum(st.by[c] for c in _NO_EVIDENCE)
        hints = []
        if no_evidence > 0:
            hints.append(HINT_NO_EVIDENCE)
            if st.partial * 2 > st.unsettled:
                hints.append(HINT_PARTIAL)
        wins = sorted(st.wins.items(), key=lambda kv: (-kv[1], kv[0]))
        out.append({
            "pair": pair,
            "overlaps": overlaps,
            **{f"by_{c}": st.by[c] for c in SUMMARY_CRITERIA},
            "not_by_score": not_by_score,
            "not_by_score_pct": _pct(not_by_score, overlaps),
            "no_evidence": no_evidence,
            "wins": "; ".join(f"{label} {n}" for label, n in wins),
            "examples": " | ".join(st.examples),
            "hint": "; ".join(hints) or "ok",
        })
    out.sort(key=lambda r: (-r["no_evidence"], -r["not_by_score"], -r["overlaps"], r["pair"]))
    return out


def _format_table(fields: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    """An aligned plain-text table: header + one line per row."""
    table = [list(fields)] + [[str(r[k]) for k in fields] for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(fields))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in table
    )


def format_summary(rows: list[dict[str, Any]]) -> str:
    """The overlap summary as an aligned plain-text table (one line per pair)."""
    return _format_table(SUMMARY_FIELDS, rows)


def _open_report(path: str) -> tuple[Any, csv.DictReader]:
    """Open a report CSV; fail if it predates the decided_by column."""
    fh = open(path, encoding="utf-8", errors="replace", newline="")
    reader = csv.DictReader(fh)
    if "decided_by" not in (reader.fieldnames or []):
        fh.close()
        raise ValueError(
            f"{path}: no decided_by column; the report is from an older "
            "version, regenerate it with --report"
        )
    return fh, reader


def summarize_misses(path: str = DEFAULT_REPORT_PATH) -> list[dict[str, Any]]:
    """Count a report CSV's `miss` rows per label.

    A miss is a label the context scored above 0 that ended with no match.
    `no_candidates`: its regex found nothing in the text (a regex gap);
    `dropped`: it had candidates but all lost (min_score or overlap; tuning).
    Sorted by misses (highest first), then label. Raises ValueError if the
    report has no `decided_by` column.
    """
    counts: dict[str, Counter] = {}
    examples: dict[str, list[str]] = {}
    fh, reader = _open_report(path)
    with fh:
        for row in reader:
            if row.get("kind") != "miss":
                continue
            label = row.get("a_label", "")
            c = counts.setdefault(label, Counter())
            kind = "no_candidates" if row.get("outcome") == "no_candidates" else "dropped"
            c[kind] += 1
            ex = examples.setdefault(label, [])
            if len(ex) < _MAX_EXAMPLES:
                ex.append(row.get("file") or "(no id)")
    out = [
        {
            "label": label,
            "misses": c["no_candidates"] + c["dropped"],
            "no_candidates": c["no_candidates"],
            "dropped": c["dropped"],
            "examples": " | ".join(examples[label]),
            "hint": HINT_MISS_REGEX if c["no_candidates"] >= c["dropped"] else HINT_MISS_TUNING,
        }
        for label, c in counts.items()
    ]
    out.sort(key=lambda r: (-r["misses"], r["label"]))
    return out


def format_misses(rows: list[dict[str, Any]]) -> str:
    """The miss summary as an aligned plain-text table (one line per label)."""
    return _format_table(MISS_FIELDS, rows)


def _cluster_key(normalized: str) -> str | None:
    """Leading digits of a full number: the area code for +1, else the first
    four digits after +. None for raw (local or unparsed) values."""
    if not normalized.startswith("+"):
        return None
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if digits.startswith("1") and len(digits) > 4:
        return f"+1 {digits[1:4]}"
    return f"+{digits[:4]}"


def summarize_validation(path: str = DEFAULT_REPORT_PATH) -> list[dict[str, Any]]:
    """Count a report CSV's `validation` rows (phone patterns) per label.

    Per label: counts by status, dropped, ambiguous (region chosen by
    proximity, format or fallback), counts by region source (`decided_by`),
    `possible` numbers grouped by leading digits (a cluster of possible
    numbers sharing an area code suggests the library's metadata is stale),
    up to three examples of ambiguous or dropped rows, and a hint. Sorted by
    rows checked (highest first), then label.
    """
    stats: dict[str, dict[str, Any]] = {}
    fh, reader = _open_report(path)
    with fh:
        for row in reader:
            if row.get("kind") != "validation":
                continue
            st = stats.setdefault(row.get("a_label", ""), {
                "status": Counter(), "sources": Counter(), "clusters": Counter(),
                "dropped": 0, "ambiguous": 0, "examples": [],
            })
            status, source = row.get("status", ""), row.get("decided_by", "")
            st["status"][status] += 1
            st["sources"][source] += 1
            dropped = row.get("outcome") == "dropped"
            ambiguous = source in _AMBIGUOUS_SOURCES
            st["dropped"] += dropped
            st["ambiguous"] += ambiguous
            if status == "possible":
                key = _cluster_key(row.get("normalized", ""))
                if key:
                    st["clusters"][key] += 1
            if (dropped or ambiguous) and len(st["examples"]) < _MAX_EXAMPLES:
                st["examples"].append(f'{row.get("file") or "(no id)"}: "{row.get("a_text", "")}"')

    out: list[dict[str, Any]] = []
    for label, st in stats.items():
        checked = sum(st["status"].values())
        clusters = sorted(st["clusters"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        hints = []
        if clusters and clusters[0][1] >= _CLUSTER_MIN:
            hints.append(HINT_STALE)
        if st["ambiguous"]:
            hints.append(HINT_AMBIGUOUS)
        if st["status"]["impossible"] * 2 > checked:
            hints.append(HINT_IMPOSSIBLE)
        sources = sorted(st["sources"].items(), key=lambda kv: (-kv[1], kv[0]))
        out.append({
            "label": label,
            "checked": checked,
            **{s: st["status"][s] for s in ("valid", "possible", "impossible")},
            "dropped": st["dropped"],
            "ambiguous": st["ambiguous"],
            "sources": "; ".join(f"{k} {n}" for k, n in sources),
            "clusters": "; ".join(f"{k} x{n}" for k, n in clusters),
            "examples": " | ".join(st["examples"]),
            "hint": "; ".join(hints) or "ok",
        })
    out.sort(key=lambda r: (-r["checked"], r["label"]))
    return out


def format_validation(rows: list[dict[str, Any]]) -> str:
    """The validation summary as an aligned plain-text table (one line per label)."""
    return _format_table(VALIDATION_FIELDS, rows)
