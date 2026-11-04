"""Tests for extract.py. Config `tests` section is the source of truth for the
end-to-end cases; the rest are focused unit tests."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from extract import (
    REPORT_FIELDS,
    Config,
    decision_rows,
    extract,
    extract_with_report,
    load_config,
    main,
    select_patterns,
    write_report_csv,
)
from words import next_words, prev_words

CONFIG_PATH = Path(__file__).parent / "extraction_config.yaml"
REAL_CFG = load_config(CONFIG_PATH)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """errorlog.csv is written to the working directory; keep it out of the repo."""
    monkeypatch.chdir(tmp_path)


def read_error_log() -> list[dict[str, str]]:
    path = Path("errorlog.csv")
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def pairs(matches: list[Any]) -> Counter:
    return Counter((m.label, m.text) for m in matches)


def make_cfg(
    tmp_path: Path,
    patterns: list[dict[str, Any]],
    coexist: list[list[str]] | None = None,
    **settings: Any,
) -> Config:
    base = {
        "context_window_words": 8,
        "distance_decay": True,
        "min_score": 0,
        "case_sensitive": False,
        "overlap_tiebreak": ["score", "length", "priority"],
    }
    base.update(settings)
    raw = {
        "settings": base,
        "normalize": {},
        "patterns": patterns,
        "coexist": coexist or [],
        "tests": [],
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(path)


# ---------------------------------------------------------------- config cases
def _case_id(case: dict[str, Any]) -> str:
    return case.get("name") or case["text"][:40]


@pytest.mark.parametrize(
    "case", REAL_CFG.tests, ids=[_case_id(c) for c in REAL_CFG.tests]
)
def test_config_cases(case: dict[str, Any]) -> None:
    """Each case may carry an optional `context` string."""
    inp = {"text": case["text"], "context": case.get("context")}
    expected = Counter((e["label"], e["text"]) for e in case["expect"] or [])
    assert pairs(extract(inp, REAL_CFG)) == expected


# ---------------------------------------------------------------- overlaps
OVERLAP_PATTERNS = [
    {"label": "A", "priority": 1, "regex": "abc\\d"},
    {"label": "B", "priority": 2, "regex": "c\\d\\d"},
]


def test_coexist_overlap_keeps_both(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["B", "A"]])  # order-insensitive
    matches, decisions = extract_with_report("abc12", cfg)
    assert pairs(matches) == Counter({("A", "abc1"): 1, ("B", "c12"): 1})
    assert any(d.kind == "coexist" for d in decisions)


def test_non_coexist_overlap_keeps_one(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    matches, decisions = extract_with_report("abc12", cfg)
    assert len(matches) == 1
    overlap = [d for d in decisions if d.kind == "overlap"]
    assert len(overlap) == 1
    assert overlap[0].winner is not None and overlap[0].reason


def test_length_tiebreak(tmp_path: Path) -> None:
    patterns = [
        {"label": "short", "priority": 1, "regex": "ab"},
        {"label": "long", "priority": 9, "regex": "abcd"},
    ]
    cfg = make_cfg(tmp_path, patterns)
    matches, decisions = extract_with_report("abcd", cfg)
    assert pairs(matches) == Counter({("long", "abcd"): 1})
    assert "length 4 > 2" in [d for d in decisions if d.kind == "overlap"][0].reason


def test_priority_breaks_full_tie(tmp_path: Path) -> None:
    patterns = [
        {"label": "second", "priority": 2, "regex": "abcd"},
        {"label": "first", "priority": 1, "regex": "abcd"},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert pairs(extract("abcd", cfg)) == Counter({("first", "abcd"): 1})


def test_min_score_is_strict(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"negative": {"bad": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    assert len(extract("12", cfg)) == 1           # score 0 == min_score: kept
    matches, decisions = extract_with_report("bad 12", cfg)  # score -1: dropped
    assert matches == []
    assert any(d.kind == "below_min_score" for d in decisions)


# ---------------------------------------------------------------- keywords
def test_multi_word_keyword(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"amount due": 4}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert extract("amount due 100", cfg)[0].score == pytest.approx(4.0)  # due = distance 1
    assert extract("amount now due 100", cfg)[0].score == 0               # phrase broken
    assert extract("due amount 100", cfg)[0].score == 0                   # wrong order


def test_distance_decay_and_nearest_occurrence(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"tel": 6}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    # "tel" occurs at distance 3 and distance 1; only the nearest counts.
    assert extract("tel a b tel 5", cfg)[0].score == pytest.approx(6.0)
    assert extract("tel a b 5", cfg)[0].score == pytest.approx(2.0)
    no_decay = make_cfg(tmp_path, patterns, distance_decay=False)
    assert extract("tel a b 5", no_decay)[0].score == pytest.approx(6.0)


DIRECTION_PATTERNS = [
    {"label": "n", "priority": 1, "regex": "\\d+",
     "context": {"positive": {"pre": 2, "post": 3}}},
]


@pytest.mark.parametrize(
    "direction, expected_before, expected_after",
    [("both", 2.0, 3.0), ("before", 2.0, 0.0), ("after", 0.0, 3.0)],
)
def test_context_direction(
    tmp_path: Path, direction: str, expected_before: float, expected_after: float
) -> None:
    cfg = make_cfg(tmp_path, DIRECTION_PATTERNS, context_direction=direction)
    assert extract("pre 5", cfg)[0].score == pytest.approx(expected_before)
    assert extract("5 post", cfg)[0].score == pytest.approx(expected_after)


def test_context_direction_defaults_to_both(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, DIRECTION_PATTERNS)
    assert cfg.settings["context_direction"] == "both"
    assert extract("pre 5 post", cfg)[0].score == pytest.approx(5.0)


def test_context_direction_invalid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="context_direction"):
        make_cfg(tmp_path, DIRECTION_PATTERNS, context_direction="sideways")


def test_keyword_after_match(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"usd": 2}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert extract("5 usd", cfg)[0].score == pytest.approx(2.0)


# ---------------------------------------------------------------- invalid regex
def test_invalid_regex_names_label(tmp_path: Path) -> None:
    patterns = [{"label": "bad_label", "priority": 1, "regex": "("}]
    with pytest.raises(ValueError, match="bad_label"):
        make_cfg(tmp_path, patterns)


# ---------------------------------------------------------------- context
CTX_TEXT = "Invoice INV-004521 dated 03/14/2026 on 2026-03-14"


# The restrict / fall-back / no-match context scenarios live in the YAML
# `tests` section (run by test_config_cases). These cover the decision log
# and input forms.
def test_context_decisions_are_logged() -> None:
    _, no_hits = extract_with_report(("nothing relevant here", CTX_TEXT), REAL_CFG)
    assert "no context hits" in no_hits[0].reason
    _, hit = extract_with_report(("dated", CTX_TEXT), REAL_CFG)
    assert hit[0].patterns == ["date"]
    assert "date (dated)" in hit[0].reason


def test_context_none_or_empty_matches_plain() -> None:
    plain = pairs(extract(CTX_TEXT, REAL_CFG))
    assert pairs(extract({"text": CTX_TEXT, "context": None}, REAL_CFG)) == plain
    assert pairs(extract({"text": CTX_TEXT}, REAL_CFG)) == plain
    assert pairs(extract((None, CTX_TEXT), REAL_CFG)) == plain
    assert pairs(extract(("   ", CTX_TEXT), REAL_CFG)) == plain


def test_context_miss_is_written_to_error_log() -> None:
    inp = ("please call", "Invoice INV-004521,\nno phone here")
    matches, decisions = extract_with_report(inp, REAL_CFG, source="doc1.txt")
    assert matches == []
    assert decisions[-1].kind == "context_miss"
    rows = read_error_log()
    assert len(rows) == 1
    row = rows[0]
    assert list(row) == ["timestamp", "file", "context", "text", "context_patterns", "reason"]
    assert row["file"] == "doc1.txt"
    assert row["context"] == "please call"              # raw, un-normalized
    assert row["text"] == "Invoice INV-004521,\nno phone here"
    assert row["context_patterns"] == "phone_number"
    assert "no matches in text" in row["reason"]


def test_context_miss_when_candidates_all_dropped(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"num": 1}, "negative": {"bad": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    assert extract(("num", "bad 12"), cfg) == []
    assert "dropped" in read_error_log()[0]["reason"]


def test_no_error_log_when_not_a_context_miss() -> None:
    extract((None, "no matches at all"), REAL_CFG)                    # no context
    extract(("nothing relevant here", "no matches at all"), REAL_CFG)  # context, no hits
    extract(("tel", "call 555-123-4567"), REAL_CFG)                   # context hit, matched
    assert read_error_log() == []


def test_error_log_accumulates_within_a_run() -> None:
    for i in range(2):
        extract(("tel", f"nothing {i}"), REAL_CFG)
    assert [r["text"] for r in read_error_log()] == ["nothing 0", "nothing 1"]


def test_context_match_ignores_case_and_punctuation() -> None:
    result = extract(("ACCT:", "Acct 5551234567 call 555-123-4567"), REAL_CFG)
    assert {m.label for m in result} == {"account_number"}


def _sel(context: str) -> list[str]:
    chosen, _ = select_patterns(context, REAL_CFG.patterns)
    return [p.label for p in chosen]


def test_select_patterns_whole_words_only() -> None:
    # 'tel' inside 'hotel' is not a hit, so nothing is selected and all run.
    chosen, decision = select_patterns("hotel", REAL_CFG.patterns)
    assert not decision.restricted
    assert [p.label for p in chosen] == [p.label for p in REAL_CFG.patterns]


def test_select_patterns_phrase_must_be_contiguous() -> None:
    assert "amount" in _sel("amount due")
    assert _sel("due total") != _sel("amount due")      # 'amount due' phrase absent
    assert "invoice_number" not in _sel("due total")


def test_select_patterns_negative_keywords_do_not_select(tmp_path: Path) -> None:
    patterns = [
        {"label": "A", "priority": 1, "regex": "a", "context": {"positive": {"yes": 1}}},
        {"label": "B", "priority": 2, "regex": "b", "context": {"negative": {"no": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    chosen, decision = select_patterns("no", cfg.patterns)   # only B's negative word
    assert [p.label for p in chosen] == ["A", "B"] and not decision.restricted
    chosen, decision = select_patterns("yes no", cfg.patterns)
    assert [p.label for p in chosen] == ["A"] and decision.restricted


def test_select_patterns_never_runs_regex_on_context() -> None:
    # A phone number in the context has no keyword, so nothing is selected.
    assert _sel("555-123-4567") == [p.label for p in REAL_CFG.patterns]


def test_bad_input_type() -> None:
    with pytest.raises(ValueError):
        extract(42, REAL_CFG)  # type: ignore[arg-type]


# ---------------------------------------------------------------- words
def test_next_words_mirrors_prev_words() -> None:
    text = "alpha beta 123 gamma don't delta"
    start, end = text.index("123"), text.index("123") + 3
    assert next_words(text, end, 2) == ["gamma", "dont"]
    assert prev_words(text, start, 2) == ["alpha", "beta"]
    # a match ending mid-word skips the rest of that word
    assert next_words("INV-12abc def", 6, 1) == ["def"]


# ---------------------------------------------------------------- CLI
def test_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "in.txt"
    f.write_text("Please call 555-123-4567 about the order.", encoding="utf-8")
    assert main(["--config", str(CONFIG_PATH), str(f)]) == 0
    import json

    out = json.loads(capsys.readouterr().out)
    assert out["matches"][0]["text"] == "555-123-4567"
    assert set(out["matches"][0]) == {"file", "label", "text", "start", "end", "score"}
    assert any(o["kind"] == "context" for o in out["overlaps"])


def read_report() -> list[dict[str, str]]:
    with open("extract_report.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_decision_rows_overlap_and_coexist(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    _, decisions = extract_with_report("abc12", cfg)
    rows = {r["kind"]: r for r in decision_rows(decisions, source="f.txt")}
    overlap = rows["overlap"]
    assert overlap["file"] == "f.txt"
    assert overlap["outcome"] == "a_wins_b_dropped"
    assert overlap["a_label"] != overlap["b_label"] and overlap["b_label"]
    assert overlap["reason"]
    assert rows["context"]["outcome"] == "all_patterns_used"

    co_cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["A", "B"]])
    _, co_decisions = extract_with_report("abc12", co_cfg)
    co = [r for r in decision_rows(co_decisions) if r["kind"] == "coexist"][0]
    assert co["outcome"] == "both_kept"
    assert {co["a_label"], co["b_label"]} == {"A", "B"}


def test_decision_rows_min_score_and_context_miss(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"num": 1}, "negative": {"bad": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    _, decisions = extract_with_report(("num", "bad 12"), cfg)
    rows = {r["kind"]: r for r in decision_rows(decisions)}
    assert rows["context"]["outcome"] == "patterns_restricted"
    assert rows["context"]["patterns"] == "n"
    dropped = rows["below_min_score"]
    assert (dropped["outcome"], dropped["a_label"], dropped["a_text"]) == ("dropped", "n", "12")
    assert dropped["b_label"] == ""
    assert rows["context_miss"]["outcome"] == "context_miss"


def test_write_report_csv_header_once_and_appends(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    _, decisions = extract_with_report("abc12", cfg)
    write_report_csv(decisions, source="one")
    write_report_csv(decisions, source="two")
    text = Path("extract_report.csv").read_text(encoding="utf-8")
    assert text.count(",".join(REPORT_FIELDS)) == 1
    assert [r["file"] for r in read_report()][-1] == "two"
    assert {r["file"] for r in read_report()} == {"one", "two"}


def test_cli_writes_and_overwrites_report(tmp_path: Path) -> None:
    f = tmp_path / "in.txt"
    f.write_text("Acct 5551234567 is overdue.", encoding="utf-8")
    args = ["--config", str(CONFIG_PATH), str(f)]

    assert main(args) == 0
    rows = read_report()
    overlap = [r for r in rows if r["kind"] == "overlap"]
    assert len(overlap) == 1
    assert overlap[0]["file"] == str(f)
    assert overlap[0]["a_label"] == "account_number"
    assert overlap[0]["b_label"] == "phone_number"
    assert "score 3.00 > 0.00" in overlap[0]["reason"]

    assert main(args) == 0                      # second run overwrites, not appends
    assert len(read_report()) == len(rows)


def test_cli_overwrites_error_log_each_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    miss = tmp_path / "miss.txt"
    miss.write_text("Invoice INV-004521", encoding="utf-8")
    ok = tmp_path / "ok.txt"
    ok.write_text("call 555-123-4567", encoding="utf-8")
    args = ["--config", str(CONFIG_PATH), "--context", "tel"]

    assert main(args + [str(miss), str(ok)]) == 0
    rows = read_error_log()
    assert [r["file"] for r in rows] == [str(miss)]       # only the miss is logged

    assert main(args + [str(ok)]) == 0                     # clean run wipes old rows
    assert read_error_log() == []
    assert Path("errorlog.csv").read_text(encoding="utf-8").startswith("timestamp,")


def test_cli_csv_keeps_columns_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "in.txt"
    f.write_text("Acct 5551234567 is overdue.", encoding="utf-8")
    assert main(["--config", str(CONFIG_PATH), str(f), "--format", "csv"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == "file,label,text,start,end,score"
    assert "account_number" in captured.out
    assert "overlap" in captured.err
