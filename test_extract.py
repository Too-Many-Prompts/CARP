"""Tests for extract.py. Config `tests` section is the source of truth for the
end-to-end cases; the rest are focused unit tests."""
from __future__ import annotations

import csv
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from extract import (
    REPORT_FIELDS,
    Config,
    extract,
    load_config,
    log_context_miss,
    main,
    open_report,
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


def matches_of(inp: Any, cfg: Config) -> list[Any]:
    """extract() without the decision log."""
    return extract(inp, cfg)[0]


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
    assert pairs(matches_of(inp, REAL_CFG)) == expected


# ---------------------------------------------------------------- overlaps
OVERLAP_PATTERNS = [
    {"label": "A", "priority": 1, "regex": "abc\\d"},
    {"label": "B", "priority": 2, "regex": "c\\d\\d"},
]


def test_coexist_overlap_keeps_both(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["B", "A"]])  # order-insensitive
    matches, decisions = extract("abc12", cfg)
    assert pairs(matches) == Counter({("A", "abc1"): 1, ("B", "c12"): 1})
    assert any(d["kind"] == "coexist" for d in decisions)


def test_non_coexist_overlap_keeps_one(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    matches, decisions = extract("abc12", cfg)
    assert len(matches) == 1
    overlap = [d for d in decisions if d["kind"] == "overlap"]
    assert len(overlap) == 1
    assert overlap[0]["a_label"] and overlap[0]["reason"]


def test_length_tiebreak(tmp_path: Path) -> None:
    patterns = [
        {"label": "short", "priority": 1, "regex": "ab"},
        {"label": "long", "priority": 9, "regex": "abcd"},
    ]
    cfg = make_cfg(tmp_path, patterns)
    matches, decisions = extract("abcd", cfg)
    assert pairs(matches) == Counter({("long", "abcd"): 1})
    assert "length 4 > 2" in [d for d in decisions if d["kind"] == "overlap"][0]["reason"]


def test_priority_breaks_full_tie(tmp_path: Path) -> None:
    patterns = [
        {"label": "second", "priority": 2, "regex": "abcd"},
        {"label": "first", "priority": 1, "regex": "abcd"},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert pairs(matches_of("abcd", cfg)) == Counter({("first", "abcd"): 1})


def test_min_score_is_strict(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"negative": {"bad": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    assert len(matches_of("12", cfg)) == 1           # score 0 == min_score: kept
    matches, decisions = extract("bad 12", cfg)  # score -1: dropped
    assert matches == []
    assert any(d["kind"] == "below_min_score" for d in decisions)


# ---------------------------------------------------------------- keywords
def test_multi_word_keyword(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"amount due": 4}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of("amount due 100", cfg)[0].score == pytest.approx(4.0)  # due = distance 1
    assert matches_of("amount now due 100", cfg)[0].score == 0               # phrase broken
    assert matches_of("due amount 100", cfg)[0].score == 0                   # wrong order


def test_distance_decay_and_nearest_occurrence(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"tel": 6}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    # "tel" occurs at distance 3 and distance 1; only the nearest counts.
    assert matches_of("tel a b tel 5", cfg)[0].score == pytest.approx(6.0)
    assert matches_of("tel a b 5", cfg)[0].score == pytest.approx(2.0)
    no_decay = make_cfg(tmp_path, patterns, distance_decay=False)
    assert matches_of("tel a b 5", no_decay)[0].score == pytest.approx(6.0)


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
    assert matches_of("pre 5", cfg)[0].score == pytest.approx(expected_before)
    assert matches_of("5 post", cfg)[0].score == pytest.approx(expected_after)


def test_context_direction_defaults_to_both(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, DIRECTION_PATTERNS)
    assert cfg.settings["context_direction"] == "both"
    assert matches_of("pre 5 post", cfg)[0].score == pytest.approx(5.0)


def test_context_direction_invalid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="context_direction"):
        make_cfg(tmp_path, DIRECTION_PATTERNS, context_direction="sideways")


@pytest.mark.parametrize(
    "stop_at, before_score, after_score",
    [("", 2.0, 3.0), (".!?", 0.0, 0.0)],
)
def test_stop_at(
    tmp_path: Path, stop_at: str, before_score: float, after_score: float
) -> None:
    cfg = make_cfg(tmp_path, DIRECTION_PATTERNS, stop_at=stop_at)
    assert matches_of("pre. 5", cfg)[0].score == pytest.approx(before_score)
    assert matches_of("5. post", cfg)[0].score == pytest.approx(after_score)


def test_stop_at_keeps_same_sentence_context(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, DIRECTION_PATTERNS, stop_at=".!?")
    assert matches_of("pre 5 post", cfg)[0].score == pytest.approx(5.0)


def test_stop_at_defaults_to_empty(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, DIRECTION_PATTERNS)
    assert cfg.settings["stop_at"] == ""


def test_stop_at_invalid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="stop_at"):
        make_cfg(tmp_path, DIRECTION_PATTERNS, stop_at=5)


def test_keyword_after_match(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"usd": 2}}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of("5 usd", cfg)[0].score == pytest.approx(2.0)


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
    _, no_hits = extract(("nothing relevant here", CTX_TEXT), REAL_CFG)
    assert "no context hits" in no_hits[0]["reason"]
    _, hit = extract(("dated", CTX_TEXT), REAL_CFG)
    assert hit[0]["patterns"] == "date"
    assert "date (dated)" in hit[0]["reason"]


def test_context_none_or_empty_matches_plain() -> None:
    plain = pairs(matches_of(CTX_TEXT, REAL_CFG))
    assert pairs(matches_of({"text": CTX_TEXT, "context": None}, REAL_CFG)) == plain
    assert pairs(matches_of({"text": CTX_TEXT}, REAL_CFG)) == plain
    assert pairs(matches_of((None, CTX_TEXT), REAL_CFG)) == plain
    assert pairs(matches_of(("   ", CTX_TEXT), REAL_CFG)) == plain


def _misses(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in decisions if d["kind"] == "context_miss"]


def test_context_miss_is_returned_and_nothing_is_written() -> None:
    inp = ("please call", "Invoice INV-004521,\nno phone here")
    matches, decisions = extract(inp, REAL_CFG, source="doc1.txt")
    assert matches == []
    (miss,) = _misses(decisions)
    assert miss["file"] == "doc1.txt"
    assert miss["patterns"] == "phone_number"
    assert "no matches in text" in miss["reason"]
    assert not Path("errorlog.csv").exists()            # the library writes no files
    assert not Path("extract_report.csv").exists()


def test_log_context_miss_writes_error_log_row() -> None:
    context, text = "please call", "Invoice INV-004521,\nno phone here"
    _, decisions = extract((context, text), REAL_CFG, source="doc1.txt")
    log_context_miss("doc1.txt", context, text, _misses(decisions)[0])
    (row,) = read_error_log()
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
    matches, decisions = extract(("num", "bad 12"), cfg)
    assert matches == []
    assert "dropped" in _misses(decisions)[0]["reason"]


def test_no_context_miss_when_not_a_miss() -> None:
    for inp in (
        (None, "no matches at all"),                     # no context
        ("nothing relevant here", "no matches at all"),  # context, no hits
        ("tel", "call 555-123-4567"),                    # context hit, matched
    ):
        assert _misses(extract(inp, REAL_CFG)[1]) == []


def test_log_context_miss_accumulates_within_a_run() -> None:
    for i in range(2):
        text = f"nothing {i}"
        _, decisions = extract(("tel", text), REAL_CFG)
        log_context_miss("", "tel", text, _misses(decisions)[0])
    assert [r["text"] for r in read_error_log()] == ["nothing 0", "nothing 1"]


def test_context_match_ignores_case_and_punctuation() -> None:
    result = matches_of(("ACCT:", "Acct 5551234567 call 555-123-4567"), REAL_CFG)
    assert {m.label for m in result} == {"account_number"}


def _sel(context: str) -> list[str]:
    chosen, _ = select_patterns(context, REAL_CFG.patterns)
    return [p.label for p in chosen]


def test_select_patterns_whole_words_only() -> None:
    # 'tel' inside 'hotel' is not a hit, so nothing is selected and all run.
    chosen, decision = select_patterns("hotel", REAL_CFG.patterns)
    assert decision["outcome"] == "all_patterns_used"
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
    assert [p.label for p in chosen] == ["A", "B"] and decision["outcome"] == "all_patterns_used"
    chosen, decision = select_patterns("yes no", cfg.patterns)
    assert [p.label for p in chosen] == ["A"] and decision["outcome"] == "patterns_restricted"


def test_select_patterns_never_runs_regex_on_context() -> None:
    # A phone number in the context has no keyword, so nothing is selected.
    assert _sel("555-123-4567") == [p.label for p in REAL_CFG.patterns]


def test_bad_input_type() -> None:
    with pytest.raises(ValueError):
        extract(42, REAL_CFG)  # type: ignore[arg-type]


# ---------------------------------------------------------------- words
def test_context_words_are_ascii_letters_only() -> None:
    # Non-ASCII characters that lowercase to ASCII-looking text (U+0130, the
    # Kelvin sign U+212A) are separators, like every other non-ASCII char.
    text = "alpha \u0130 beta \u212a gamma 5"
    assert prev_words(text, len(text) - 1, 5) == ["alpha", "beta", "gamma"]
    text = "5 alpha \u0130 beta \u212a gamma"
    assert next_words(text, 1, 5) == ["alpha", "beta", "gamma"]


def test_next_words_mirrors_prev_words() -> None:
    text = "alpha beta 123 gamma don't delta"
    start, end = text.index("123"), text.index("123") + 3
    assert next_words(text, end, 2) == ["gamma", "dont"]
    assert prev_words(text, start, 2) == ["alpha", "beta"]
    # a match ending mid-word skips the rest of that word
    assert next_words("INV-12abc def", 6, 1) == ["def"]


# ---------------------------------------------------------------- CLI
def test_cli_text_prints_matches_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    assert main(["--config", str(CONFIG_PATH),
                 "--text", "Please call 555-123-4567 about the order."]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["text"] == "555-123-4567"
    assert set(out[0]) == {"label", "text", "start", "end", "score"}
    assert not Path("extract_report.csv").exists()
    assert not Path("errorlog.csv").exists()


def test_cli_reads_text_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io, json

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"call 555-123-4567")))
    assert main(["--config", str(CONFIG_PATH), "--context", "phone"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["label"] == "phone_number"


def read_report() -> list[dict[str, str]]:
    with open("extract_report.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_decision_log_overlap_and_coexist(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    _, decisions = extract("abc12", cfg, source="f.txt")
    rows = {r["kind"]: r for r in decisions}
    overlap = rows["overlap"]
    assert overlap["file"] == "f.txt"
    assert overlap["outcome"] == "a_wins_b_dropped"
    assert overlap["a_label"] != overlap["b_label"] and overlap["b_label"]
    assert overlap["reason"]
    assert rows["context"]["outcome"] == "all_patterns_used"

    co_cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["A", "B"]])
    _, co_decisions = extract("abc12", co_cfg)
    co = [r for r in co_decisions if r["kind"] == "coexist"][0]
    assert co["outcome"] == "both_kept"
    assert {co["a_label"], co["b_label"]} == {"A", "B"}


def test_decision_log_min_score_and_context_miss(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "context": {"positive": {"num": 1}, "negative": {"bad": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    _, decisions = extract(("num", "bad 12"), cfg)
    rows = {r["kind"]: r for r in decisions}
    assert rows["context"]["outcome"] == "patterns_restricted"
    assert rows["context"]["patterns"] == "n"
    dropped = rows["below_min_score"]
    assert (dropped["outcome"], dropped["a_label"], dropped["a_text"]) == ("dropped", "n", "12")
    assert dropped["b_label"] == ""
    assert rows["context_miss"]["outcome"] == "context_miss"


def test_write_report_csv_header_once_and_appends(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    write_report_csv(extract("abc12", cfg, source="one")[1])
    write_report_csv(extract("abc12", cfg, source="two")[1])
    text = Path("extract_report.csv").read_text(encoding="utf-8")
    assert text.count(",".join(REPORT_FIELDS)) == 1
    assert [r["file"] for r in read_report()][-1] == "two"
    assert {r["file"] for r in read_report()} == {"one", "two"}


def test_open_report_streams_many_calls_with_one_header(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    with open_report() as write:
        for i in range(3):
            write(extract("abc12", cfg, source=f"row-{i}")[1])
    text = Path("extract_report.csv").read_text(encoding="utf-8")
    assert text.count(",".join(REPORT_FIELDS)) == 1
    assert [r["file"] for r in read_report() if r["kind"] == "context"] == [
        "row-0", "row-1", "row-2",
    ]
    with open_report() as write:                       # reopening appends, no 2nd header
        write(extract("abc12", cfg, source="row-3")[1])
    assert Path("extract_report.csv").read_text(encoding="utf-8").count("file,kind") == 1
    assert read_report()[-1]["file"] == "row-3"


def test_cli_report_flag_writes_and_overwrites_report() -> None:
    args = ["--config", str(CONFIG_PATH), "--text", "Acct 5551234567 is overdue.", "--report"]

    assert main(args) == 0
    rows = read_report()
    overlap = [r for r in rows if r["kind"] == "overlap"]
    assert len(overlap) == 1
    assert overlap[0]["a_label"] == "account_number"
    assert overlap[0]["b_label"] == "phone_number"
    assert "score 3.00 > 0.00" in overlap[0]["reason"]

    assert main(args) == 0                      # second run overwrites, not appends
    assert len(read_report()) == len(rows)


def test_cli_error_log_flag_overwrites_each_run() -> None:
    args = ["--config", str(CONFIG_PATH), "--context", "tel", "--error-log"]

    assert main(args + ["--text", "Invoice INV-004521"]) == 0
    rows = read_error_log()
    assert len(rows) == 1 and rows[0]["text"] == "Invoice INV-004521"
    assert rows[0]["context"] == "tel"                     # raw context kept

    assert main(args + ["--text", "call 555-123-4567"]) == 0   # clean run wipes old rows
    assert read_error_log() == []
    assert Path("errorlog.csv").read_text(encoding="utf-8").startswith("timestamp,")





# ---------------------------------------------------------------- keyword validation
@pytest.mark.parametrize("keyword", ["w2", "1099", "café", "$"])
def test_keyword_that_can_never_match_is_rejected(tmp_path: Path, keyword: str) -> None:
    patterns = [{"label": "p", "priority": 1, "regex": "x",
                 "context": {"positive": {keyword: 1}}}]
    with pytest.raises(ValueError, match=r"pattern 'p'.*ASCII letters"):
        make_cfg(tmp_path, patterns)


def test_keyword_in_both_groups_is_rejected(tmp_path: Path) -> None:
    patterns = [{"label": "p", "priority": 1, "regex": "x",
                 "context": {"positive": {"invoice": 3}, "negative": {"Invoice": -3}}}]
    with pytest.raises(ValueError, match="more than once"):
        make_cfg(tmp_path, patterns)


@pytest.mark.parametrize(
    "group,weight", [("positive", -2), ("negative", 3)]
)
def test_wrong_signed_weight_is_rejected(tmp_path: Path, group: str, weight: int) -> None:
    patterns = [{"label": "p", "priority": 1, "regex": "x",
                 "context": {group: {"word": weight}}}]
    with pytest.raises(ValueError, match="weight"):
        make_cfg(tmp_path, patterns)


def test_hyphenated_keyword_matches_like_context_words(tmp_path: Path) -> None:
    # words.py splits on hyphens, so "e-mail" must be the two words e + mail.
    patterns = [{"label": "p", "priority": 1, "regex": r"\d+",
                 "context": {"positive": {"e-mail": 2}}}]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of("e-mail 42", cfg)[0].score == 2.0


# ---------------------------------------------------------------- CLI failure paths



def test_cli_bad_config_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("settings: {}\n", encoding="utf-8")
    assert main(["--config", str(bad), "--text", "x", "--report"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:") and captured.out == ""
    assert not Path("extract_report.csv").exists()


def _real_cfg_with_stop_at(stop_at: str) -> Config:
    return replace(REAL_CFG, settings={**REAL_CFG.settings, "stop_at": stop_at})


@pytest.mark.parametrize(
    "stop_at, expected_label",
    [("", "account_number"), (".!?", "phone_number")],
)
def test_stop_at_period_changes_overlap_winner(stop_at: str, expected_label: str) -> None:
    """'Acct' sits before a period. Without stop_at it boosts account_number
    (+3) so it beats phone_number. With stop_at='.' the scan ends at the period,
    both scores are 0, and the priority tie-break picks phone_number."""
    matches = matches_of("Acct. 5551234567", _real_cfg_with_stop_at(stop_at))
    assert [(m.label, m.text) for m in matches] == [(expected_label, "5551234567")]


@pytest.mark.parametrize("stop_at", ["", ".!?"])
def test_stop_at_keeps_context_after_period(stop_at: str) -> None:
    """'phone' is after the period and before the number, so it still counts
    whether or not stop_at is set: phone/2 words away = 1.5."""
    matches = matches_of("acct. phone is 123-456-2342", _real_cfg_with_stop_at(stop_at))
    assert [(m.label, m.text) for m in matches] == [("phone_number", "123-456-2342")]
    assert matches[0].score == pytest.approx(1.5)


def test_cli_without_text_on_a_terminal_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(CONFIG_PATH)])
    assert exc.value.code == 2
    assert "no text" in capsys.readouterr().err


# ------------------------------------------------- context_negative tie-break
# Two patterns match the same span "abcd"; both have a positive hit in the
# context. B has the higher priority number, so priority alone would pick A.
CTX_NEG_PATTERNS = [
    {"label": "A", "priority": 1, "regex": "abcd",
     "context": {"positive": {"alpha": 1}, "negative": {"bad": -2, "very bad": -3}}},
    {"label": "B", "priority": 2, "regex": "abcd",
     "context": {"positive": {"beta": 1}, "negative": {}}},
]
CTX_NEG_ORDER = ["score", "context_negative", "length", "priority"]


def test_context_negative_breaks_tie(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_NEG_PATTERNS, overlap_tiebreak=CTX_NEG_ORDER)
    matches, decisions = extract({"text": "abcd", "context": "alpha beta bad"}, cfg)
    assert pairs(matches) == Counter({("B", "abcd"): 1})
    overlap = [d for d in decisions if d["kind"] == "overlap"][0]
    assert "context negatives 0 > -2" in overlap["reason"]


def test_context_negative_phrase(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_NEG_PATTERNS, overlap_tiebreak=CTX_NEG_ORDER)
    matches, decisions = extract({"text": "abcd", "context": "alpha beta very bad"}, cfg)
    assert pairs(matches) == Counter({("B", "abcd"): 1})
    # "bad" (-2) and "very bad" (-3) both occur: sum is -5
    assert "-5" in [d for d in decisions if d["kind"] == "overlap"][0]["reason"]


def test_context_negative_is_opt_in(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_NEG_PATTERNS)  # default order, no context_negative
    matches, _ = extract({"text": "abcd", "context": "alpha beta bad"}, cfg)
    assert pairs(matches) == Counter({("A", "abcd"): 1})  # priority decides


def test_context_negative_needs_context(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_NEG_PATTERNS, overlap_tiebreak=CTX_NEG_ORDER)
    assert pairs(matches_of("abcd", cfg)) == Counter({("A", "abcd"): 1})


def test_context_negative_ignored_when_context_selects_nothing(tmp_path: Path) -> None:
    """Context with no positive hits runs all patterns: negatives must NOT apply."""
    cfg = make_cfg(tmp_path, CTX_NEG_PATTERNS, overlap_tiebreak=CTX_NEG_ORDER)
    matches, _ = extract({"text": "abcd", "context": "bad"}, cfg)
    assert pairs(matches) == Counter({("A", "abcd"): 1})  # falls through to priority


@pytest.mark.parametrize("empty", [[], None])
def test_empty_tiebreak_uses_start_then_label(tmp_path: Path, empty: Any) -> None:
    patterns = [
        {"label": "Z", "priority": 1, "regex": "abcd"},
        {"label": "B", "priority": 9, "regex": "abcd"},
    ]
    cfg = make_cfg(tmp_path, patterns, overlap_tiebreak=empty)
    assert cfg.settings["overlap_tiebreak"] == []
    assert pairs(matches_of("abcd", cfg)) == Counter({("B", "abcd"): 1})  # label, not priority
    early = [
        {"label": "late", "priority": 1, "regex": "bcd"},
        {"label": "early", "priority": 9, "regex": "abc"},
    ]
    cfg = make_cfg(tmp_path, early, overlap_tiebreak=empty)
    assert pairs(matches_of("abcd", cfg)) == Counter({("early", "abc"): 1})  # earlier start


def test_unknown_tiebreak_name_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown criterion 'nope'"):
        make_cfg(tmp_path, CTX_NEG_PATTERNS, overlap_tiebreak=["nope"])
