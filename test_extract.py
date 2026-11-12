"""Tests for extract.py. Config `tests` section is the source of truth for the
end-to-end cases; the rest are focused unit tests."""
from __future__ import annotations

import csv
import re
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
    context_score,
    context_words,
    extract,
    load_config,
    main,
    open_report,
    reset_report,
    summarize_misses,
    summarize_overlaps,
    write_report_csv,
)
from words import next_words, prev_words

CONFIG_PATH = Path(__file__).parent / "extraction_config.yaml"
REAL_CFG = load_config(CONFIG_PATH)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The report CSV is written to the working directory; keep it out of the repo."""
    monkeypatch.chdir(tmp_path)


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
         "nearby": {"bad": -1}},
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
         "nearby": {"amount due": 4}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of("amount due 100", cfg)[0].score == pytest.approx(4.0)  # due = distance 1
    assert matches_of("amount now due 100", cfg)[0].score == 0               # phrase broken
    assert matches_of("due amount 100", cfg)[0].score == 0                   # wrong order


def test_distance_decay_and_nearest_occurrence(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": "\\d+",
         "nearby": {"tel": 6}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    # "tel" occurs at distance 3 and distance 1; only the nearest counts.
    assert matches_of("tel a b tel 5", cfg)[0].score == pytest.approx(6.0)
    assert matches_of("tel a b 5", cfg)[0].score == pytest.approx(2.0)
    no_decay = make_cfg(tmp_path, patterns, distance_decay=False)
    assert matches_of("tel a b 5", no_decay)[0].score == pytest.approx(6.0)


DIRECTION_PATTERNS = [
    {"label": "n", "priority": 1, "regex": "\\d+",
     "nearby": {"pre": 2, "post": 3}},
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
         "nearby": {"usd": 2}},
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
N_CTX = [{"label": "n", "priority": 1, "regex": r"\d+", "context": {"acct": 6}}]


def test_context_none_or_empty_matches_plain() -> None:
    plain = [m.to_dict() for m in matches_of(CTX_TEXT, REAL_CFG)]
    for inp in ({"text": CTX_TEXT, "context": None}, {"text": CTX_TEXT},
                (None, CTX_TEXT), ("   ", CTX_TEXT), ("", CTX_TEXT)):
        assert [m.to_dict() for m in matches_of(inp, REAL_CFG)] == plain


def test_every_pattern_runs_whatever_the_context() -> None:
    text = "Acct 2125550123 call 212-555-0123"
    for context in (None, "acct", "phone", "nothing relevant"):
        assert "valid_US_number" in {m.label for m in matches_of((context, text), REAL_CFG)}


def test_context_score_decays_from_the_right(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, N_CTX)
    assert matches_of(("acct", "12"), cfg)[0].score == pytest.approx(6.0)        # distance 1
    assert matches_of(("acct a b", "12"), cfg)[0].score == pytest.approx(2.0)    # distance 3
    assert matches_of(("acct a b acct", "12"), cfg)[0].score == pytest.approx(6.0)  # nearest only
    no_decay = make_cfg(tmp_path, N_CTX, distance_decay=False)
    assert matches_of(("acct a b", "12"), no_decay)[0].score == pytest.approx(6.0)


def test_context_reads_every_word_without_a_window(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, N_CTX, context_window_words=1, distance_decay=False)
    far = "acct " + "word " * 50
    assert matches_of((far, "12"), cfg)[0].context_score == pytest.approx(6.0)
    assert matches_of(f"{far} 12", cfg)[0].text_score == 0      # context key: not the text


def test_context_ignores_stop_at_and_direction(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, N_CTX, stop_at=".!?", context_direction="after",
                   distance_decay=False)
    assert matches_of(("acct. Done!", "12"), cfg)[0].score == pytest.approx(6.0)


def test_context_phrase_last_word_sets_distance(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+", "context": {"amount due": 4}}]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of(("amount due", "12"), cfg)[0].score == pytest.approx(4.0)
    assert matches_of(("amount due now", "12"), cfg)[0].score == pytest.approx(2.0)
    assert matches_of(("amount now due", "12"), cfg)[0].score == 0               # broken


def test_context_score_is_the_same_for_every_candidate(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, N_CTX + [{"label": "w", "priority": 2, "regex": "zz"}])
    matches = matches_of(("acct", "1 zz 22 333"), cfg)
    assert {(m.label, m.context_score) for m in matches} == {("n", 6.0), ("w", 0.0)}


def test_keywords_count_in_text_and_context(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+", "keywords": {"tel": 3}}]
    cfg = make_cfg(tmp_path, patterns)
    (m,) = matches_of(("tel", "tel 12"), cfg)
    assert (m.text_score, m.context_score, m.score) == (3.0, 3.0, 6.0)   # both add


def test_nearby_reads_only_text_and_context_reads_only_context(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "nearby": {"tel": 3}, "context": {"acct": 2}}]
    cfg = make_cfg(tmp_path, patterns)
    (m,) = matches_of(("tel", "acct 12"), cfg)
    assert (m.text_score, m.context_score) == (0, 0)
    (m,) = matches_of(("acct", "tel 12"), cfg)
    assert (m.text_score, m.context_score) == (3.0, 2.0)


def test_negative_context_drops_below_min_score(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+", "context": {"memo": -1}}]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    assert len(matches_of("12", cfg)) == 1
    matches, decisions = extract(("memo", "12"), cfg)
    assert matches == []
    (row,) = [d for d in decisions if d["kind"] == "below_min_score"]
    assert (row["a_text_score"], row["a_context_score"], row["a_score"]) == (0.0, -1.0, -1.0)


def test_context_flips_an_overlap() -> None:
    assert [m.label for m in matches_of("2125550123 call", REAL_CFG)] == ["valid_US_number"]
    assert [m.label for m in matches_of(("account", "2125550123 call"), REAL_CFG)] == [
        "account_number"]


def test_context_match_ignores_case_and_punctuation() -> None:
    (m,) = matches_of(("ACCOUNT:", "2125550123 call"), REAL_CFG)
    assert m.label == "account_number"


def test_context_whole_words_only(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+", "context": {"tel": 1}}]
    assert matches_of(("hotel", "12"), make_cfg(tmp_path, patterns))[0].score == 0


def test_regex_never_runs_on_context() -> None:
    assert matches_of(("212-555-0123", "no numbers"), REAL_CFG) == []


def test_bad_input_type() -> None:
    with pytest.raises(ValueError):
        extract(42, REAL_CFG)  # type: ignore[arg-type]


# ---------------------------------------------------------------- miss rows
def _misses(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in decisions if d["kind"] == "miss"]


def test_miss_when_context_points_to_a_label_with_no_candidates() -> None:
    matches, decisions = extract(("call me", "Invoice INV-004521"), REAL_CFG, source="d1")
    assert [m.label for m in matches] == ["invoice_number"]
    (miss,) = _misses(decisions)
    assert (miss["file"], miss["outcome"], miss["a_label"]) == ("d1", "no_candidates", "phone_number")
    assert miss["a_context_score"] == 1.0                      # call (2) at distance 2
    assert miss["reason"] == "context scored phone_number +1.00 (call); no candidates in text"
    assert (miss["a_text"], miss["a_score"], miss["b_label"], miss["decided_by"]) == ("", "", "", "")
    assert not Path("extract_report.csv").exists()             # the library writes no files


def test_miss_when_candidates_all_dropped(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": {"num": 1}, "nearby": {"bad": -3}}]
    cfg = make_cfg(tmp_path, patterns)
    matches, decisions = extract(("num", "bad 12 bad 34"), cfg)
    assert matches == []
    (miss,) = _misses(decisions)
    assert miss["outcome"] == "candidates_dropped"
    assert miss["reason"].endswith("; 2 candidate(s) dropped (min_score/overlap/validation)")


def test_no_miss_otherwise(tmp_path: Path) -> None:
    for inp in (
        (None, "no matches at all"),                     # no context
        ("nothing relevant here", "no matches at all"),  # context, no hits
        ("tel", "call 212-555-0123"),                    # context hit, matched
        ("order", "no numbers"),                         # only phone scored, and < 0
    ):
        assert _misses(extract(inp, REAL_CFG)[1]) == []
    neg = make_cfg(tmp_path, [{"label": "n", "priority": 1, "regex": r"\d+",
                               "context": {"memo": -1}}])
    assert _misses(extract(("memo", "no digits"), neg)[1]) == []


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
                 "--text", "Please call 212-555-0123 about the order."]) == 0
    out = json.loads(capsys.readouterr().out)
    # A validated match's text is the number in output_format (E.164 by default).
    assert out[0]["text"] == "+12125550123"
    assert set(out[0]) == {"label", "text", "start", "end", "score"}
    assert not Path("extract_report.csv").exists()


def test_cli_reads_text_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io, json

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"call 212-555-0123")))
    assert main(["--config", str(CONFIG_PATH), "--context", "phone"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["label"] == "valid_US_number"

def read_report() -> list[dict[str, str]]:
    with open("extract_report.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_decision_log_overlap_and_coexist(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    _, decisions = extract("abc12", cfg, source="f.txt")
    (overlap,) = decisions
    assert overlap["kind"] == "overlap"
    assert overlap["file"] == "f.txt"
    assert overlap["outcome"] == "a_wins_b_dropped"
    assert overlap["a_label"] != overlap["b_label"] and overlap["b_label"]
    assert overlap["reason"]

    co_cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["A", "B"]])
    _, co_decisions = extract("abc12", co_cfg)
    co = [r for r in co_decisions if r["kind"] == "coexist"][0]
    assert co["outcome"] == "both_kept"
    assert {co["a_label"], co["b_label"]} == {"A", "B"}


def test_decision_log_min_score(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": "\\d+", "nearby": {"bad": -1}}]
    cfg = make_cfg(tmp_path, patterns, min_score=0)
    (dropped,) = extract("bad 12", cfg)[1]
    assert dropped["kind"] == "below_min_score"
    assert (dropped["outcome"], dropped["a_label"], dropped["a_text"]) == ("dropped", "n", "12")
    assert dropped["b_label"] == ""


def test_report_has_score_breakdown(tmp_path: Path) -> None:
    patterns = [
        {"label": "A", "priority": 1, "regex": "abcd", "nearby": {"go": 1}, "context": {"ctx": 2}},
        {"label": "B", "priority": 2, "regex": "abcd", "nearby": {"go": 0.5}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    write_report_csv(extract(("ctx", "go abcd"), cfg)[1])
    (row,) = read_report()
    assert (row["a_label"], row["a_score"], row["a_text_score"], row["a_context_score"]) == (
        "A", "3.0", "1.0", "2.0")
    assert (row["b_label"], row["b_score"], row["b_text_score"], row["b_context_score"]) == (
        "B", "0.5", "0.5", "0.0")


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
    assert [r["file"] for r in read_report() if r["kind"] == "overlap"] == [
        "row-0", "row-1", "row-2",
    ]
    with open_report() as write:                       # reopening appends, no 2nd header
        write(extract("abc12", cfg, source="row-3")[1])
    assert Path("extract_report.csv").read_text(encoding="utf-8").count("file,kind") == 1
    assert read_report()[-1]["file"] == "row-3"


def test_cli_report_flag_writes_and_overwrites_report() -> None:
    args = ["--config", str(CONFIG_PATH), "--text", "Acct 2125550123 is overdue.", "--report"]

    assert main(args) == 0
    rows = read_report()
    overlap = [r for r in rows if r["kind"] == "overlap"]
    assert len(overlap) == 1
    assert overlap[0]["a_label"] == "account_number"
    assert overlap[0]["b_label"] == "valid_US_number"
    assert "score 3.00 > 0.00" in overlap[0]["reason"]

    assert main(args) == 0                      # second run overwrites, not appends
    assert len(read_report()) == len(rows)



def test_cli_error_log_flag_is_gone(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(CONFIG_PATH), "--text", "x", "--error-log"])
    assert exc.value.code == 2
    assert not Path("errorlog.csv").exists()


# ---------------------------------------------------------------- keyword validation
def _one(**keys: Any) -> list[dict[str, Any]]:
    return [{"label": "p", "priority": 1, "regex": "x", **keys}]


KEY_NAMES = ["keywords", "nearby", "context"]


@pytest.mark.parametrize("keyword", ["w2", "1099", "café", "$"])
@pytest.mark.parametrize("where", KEY_NAMES)
def test_keyword_that_can_never_match_is_rejected(
    tmp_path: Path, keyword: str, where: str
) -> None:
    with pytest.raises(ValueError, match=rf"pattern 'p': {where} keyword .*ASCII letters"):
        make_cfg(tmp_path, _one(**{where: {keyword: 1}}))


@pytest.mark.parametrize("where", KEY_NAMES)
def test_duplicate_keyword_within_a_key_is_rejected(tmp_path: Path, where: str) -> None:
    with pytest.raises(ValueError, match=rf"pattern 'p': {where} keyword .* more than once"):
        make_cfg(tmp_path, _one(**{where: {"tel": 1, "Tel": 2}}))


def test_keyword_may_be_in_context_and_nearby(tmp_path: Path) -> None:
    p = make_cfg(tmp_path, _one(context={"tel": 1, "fax": -1}, nearby={"tel": 2})).patterns[0]
    assert (p.context, p.nearby) == ({"tel": 1.0, "fax": -1.0}, {"tel": 2.0})


@pytest.mark.parametrize("where", KEY_NAMES)
@pytest.mark.parametrize("weight", [-2, 2.5])
def test_weights_may_have_either_sign(tmp_path: Path, where: str, weight: float) -> None:
    p = make_cfg(tmp_path, _one(**{where: {"word": weight}})).patterns[0]
    assert {"word": float(weight)} in (p.nearby, p.context)


@pytest.mark.parametrize("where", KEY_NAMES)
def test_weight_zero_is_rejected(tmp_path: Path, where: str) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**{where: {"tel": 0}}))
    assert str(exc.value) == (
        f"pattern 'p': {where} keyword 'tel' has weight 0; give it a sign or remove it"
    )


@pytest.mark.parametrize("where", KEY_NAMES)
@pytest.mark.parametrize(
    "value, message",
    [
        ({"word": "high"}, "keyword 'word' needs a numeric weight"),
        ({"word": True}, "keyword 'word' needs a numeric weight"),
        (["word"], "must be a map of word: weight"),
    ],
)
def test_malformed_keyword_keys_are_rejected(
    tmp_path: Path, where: str, value: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(f"pattern 'p': {where} {message}")):
        make_cfg(tmp_path, _one(**{where: value}))


def test_hyphenated_keyword_matches_like_context_words(tmp_path: Path) -> None:
    # words.py splits on hyphens, so "e-mail" must be the two words e + mail.
    patterns = [{"label": "p", "priority": 1, "regex": r"\d+", "keywords": {"e-mail": 2}}]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of("e-mail 42", cfg)[0].score == 2.0
    assert matches_of(("send e-mail", "42"), cfg)[0].score == 2.0


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
    [("", "account_number"), (".!?", "valid_US_number")],
)
def test_stop_at_period_changes_overlap_winner(stop_at: str, expected_label: str) -> None:
    """'Acct' sits before a period. Without stop_at it boosts account_number
    (+3) so it beats phone_number. With stop_at='.' the scan ends at the period,
    both scores are 0, and the priority tie-break picks phone_number."""
    matches = matches_of("Acct. 2125550123", _real_cfg_with_stop_at(stop_at))
    expected_text = "+12125550123" if expected_label.startswith("valid_") else "2125550123"
    assert [(m.label, m.text) for m in matches] == [(expected_label, expected_text)]


@pytest.mark.parametrize("stop_at", ["", ".!?"])
def test_stop_at_keeps_context_after_period(stop_at: str) -> None:
    """'phone' is after the period and before the number, so it still counts
    whether or not stop_at is set: phone/2 words away = 1.5."""
    matches = matches_of("acct. phone is 212-456-2342", _real_cfg_with_stop_at(stop_at))
    assert [(m.label, m.text) for m in matches] == [("valid_US_number", "+12124562342")]
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



# Parity: the text score (no decay, window covering every word) and the
# context score use the same matcher, so they score the same keywords alike.
PARITY_CASES = [
    # (weights, words around the match / context string, expected sum)
    ({"bad": -2}, "bad", -2),
    ({"very bad": -3, "bad": -1}, "very bad", -4),              # phrase + its last word
    ({"very bad": -3}, "very nice bad", 0),                     # phrase must be contiguous
    ({"dont": -2}, "don't", -2),                                # apostrophe joins
    ({"don't": -2}, "DON'T", -2),                               # keyword apostrophe, mixed case
    ({"e-mail": -1}, "e mail", -1),                             # hyphen separates
    ({"bad": -2}, "bad Bad BAD", -2),                           # repeats count once
    ({"tel": -1}, "hotel", 0),                                  # whole words only
    ({"acct": -3, "call": -2}, "Acct: please CALL", -5),
    ({"acct": 3, "call": 2}, "Acct: please CALL", 5),
]


@pytest.mark.parametrize("weights, words, expected", PARITY_CASES)
def test_text_and_context_score_parity(
    tmp_path: Path, weights: dict[str, float], words: str, expected: float
) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": weights, "nearby": weights}]
    cfg = make_cfg(tmp_path, patterns, distance_decay=False, min_score=-100,
                   context_window_words=50)
    pattern = cfg.patterns[0]
    text_before = matches_of(f"{words} 12", cfg)[0].text_score
    text_after = matches_of(f"12 {words}", cfg)[0].text_score
    ctx, _ = context_score(context_words(words), pattern, cfg.settings)
    via_extract = matches_of((words, "12"), cfg)[0].context_score
    assert text_before == text_after == ctx == via_extract == pytest.approx(expected)


@pytest.mark.parametrize("words", ["acct", "a acct", "a b c acct", "acct a b"])
def test_context_decay_matches_text_before_window(tmp_path: Path, words: str) -> None:
    """The context reads like an unbounded window right before the match."""
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": {"acct": 6}, "nearby": {"acct": 6}}]
    cfg = make_cfg(tmp_path, patterns, context_window_words=50, context_direction="before")
    assert matches_of((words, "12"), cfg)[0].context_score == pytest.approx(
        matches_of(f"{words} 12", cfg)[0].text_score)


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
        make_cfg(tmp_path, OVERLAP_PATTERNS, overlap_tiebreak=["nope"])



# ------------------------------------------------- decided_by
def test_report_fields() -> None:
    assert REPORT_FIELDS[-1] == "decided_by"
    assert REPORT_FIELDS[:4] == ("file", "kind", "outcome", "reason")
    for side in "ab":
        assert REPORT_FIELDS.index(f"{side}_text_score") == REPORT_FIELDS.index(f"{side}_score") + 1
        assert f"{side}_context_score" in REPORT_FIELDS
    assert "penalty_skipped" not in REPORT_FIELDS and "patterns" not in REPORT_FIELDS


def _overlap(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    (row,) = [d for d in decisions if d["kind"] == "overlap"]
    return row


@pytest.mark.parametrize(
    "patterns, order, inp, decided_by",
    [
        ([{"label": "A", "priority": 1, "regex": "abcd", "nearby": {"go": 1}},
          {"label": "B", "priority": 2, "regex": "abcd"}],
         ["score", "length", "priority"], "go abcd", "score"),
        ([{"label": "A", "priority": 2, "regex": "abcd", "context": {"go": 1}},
          {"label": "B", "priority": 1, "regex": "abcd"}],
         ["score", "length", "priority"], ("go", "abcd"), "score"),
        ([{"label": "short", "priority": 1, "regex": "ab"},
          {"label": "long", "priority": 9, "regex": "abcd"}],
         ["score", "length", "priority"], "abcd", "length"),
        ([{"label": "B", "priority": 1, "regex": "abcd"},
          {"label": "A", "priority": 2, "regex": "abcd"}],
         ["score", "length", "priority"], "abcd", "priority"),
        ([{"label": "B", "priority": 1, "regex": "abcd"},
          {"label": "A", "priority": 2, "regex": "abcd"}],
         [], "abcd", "start_label"),
    ],
)
def test_overlap_decided_by(
    tmp_path: Path, patterns: Any, order: list[str], inp: Any, decided_by: str
) -> None:
    cfg = make_cfg(tmp_path, patterns, overlap_tiebreak=order)
    row = _overlap(extract(inp, cfg)[1])
    assert row["decided_by"] == decided_by
    if decided_by == "score":
        assert row["a_label"] == "A"


def test_other_decided_by_values(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["A", "B"]])
    rows = {d["kind"]: d for d in extract("abc12", cfg)[1]}
    assert rows["coexist"]["decided_by"] == "coexist"
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": {"num": 1}, "nearby": {"bad": -2}}]
    rows = {d["kind"]: d for d in extract(("num", "bad 12"), make_cfg(tmp_path, patterns))[1]}
    assert rows["below_min_score"]["decided_by"] == "min_score"
    assert rows["miss"]["decided_by"] == ""
    assert set(rows) == {"below_min_score", "miss"}


def test_decided_by_written_to_csv(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    write_report_csv(extract("abc12", cfg)[1])
    rows = read_report()
    assert list(rows[0])[-1] == "decided_by"
    assert [r["decided_by"] for r in rows if r["kind"] == "overlap"] == ["length"]


def test_real_config_loads_without_warning() -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load_config(CONFIG_PATH)


# ------------------------------------------------- summarize_overlaps
def _row(a: str, b: str, by: str, file: str = "f",
         a_span: tuple[int, int] = (0, 4), b_span: tuple[int, int] = (0, 4),
         a_text: str = "x", b_text: str = "y", kind: str = "overlap",
         outcome: str = "") -> dict[str, Any]:
    row = dict.fromkeys(REPORT_FIELDS, "")
    row.update(file=file, kind=kind, a_label=a, b_label=b, decided_by=by,
               a_pattern=a, b_pattern=b,
               a_start=a_span[0], a_end=a_span[1], outcome=outcome,
               b_start=b_span[0], b_end=b_span[1], a_text=a_text, b_text=b_text)
    return row


def _report(rows: list[dict[str, Any]], path: str = "extract_report.csv") -> str:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _by_pair(path: str = "extract_report.csv") -> dict[str, dict[str, Any]]:
    return {r["pair"]: r for r in summarize_overlaps(path)}


SUMMARY_KEYS = [
    "pair", "overlaps", "by_score", "by_length", "by_priority",
    "by_start_label", "not_by_score", "not_by_score_pct", "no_evidence",
    "wins", "examples", "hint",
]


def test_summary_counts_and_wins() -> None:
    _report([
        _row("phone", "acct", "score"), _row("phone", "acct", "score"),
        _row("acct", "phone", "length"), _row("phone", "acct", "length"),
        _row("phone", "acct", "priority"), _row("acct", "phone", "start_label"),
        _row("phone", "acct", "coexist", kind="coexist"),       # not an overlap row
        _row("n", "", "min_score", kind="below_min_score"),
        _row("n", "", "", kind="miss", outcome="no_candidates"),
    ])
    (row,) = summarize_overlaps()
    assert list(row) == SUMMARY_KEYS
    assert row["pair"] == "acct/phone"                            # sorted, joined by /
    assert (row["overlaps"], row["by_score"], row["by_length"],
            row["by_priority"], row["by_start_label"]) == (6, 2, 2, 1, 1)
    assert (row["not_by_score"], row["not_by_score_pct"]) == (4, 66.7)
    assert row["no_evidence"] == 2
    assert row["wins"] == "phone 4; acct 2"


def test_summary_sort_order() -> None:
    _report(
        [_row("a", "b", "score")] * 5                                         # a/b: 0, 0, 5
        + [_row("c", "d", "length")] * 2                                      # c/d: 0, 2, 2
        + [_row("e", "f", "priority")] + [_row("e", "f", "score")] * 3        # e/f: 1, 1, 4
        + [_row("g", "h", "start_label")]                                     # g/h: 1, 1, 1
        + [_row("i", "j", "score")] * 5                                       # i/j ties a/b
    )
    assert [r["pair"] for r in summarize_overlaps()] == ["e/f", "g/h", "c/d", "a/b", "i/j"]


def test_summary_rejects_a_csv_without_the_report_columns() -> None:
    with open("other.csv", "w", encoding="utf-8") as fh:
        fh.write("file,kind,outcome,reason\n,overlap,a_wins_b_dropped,x\n")
    with pytest.raises(ValueError, match=r"not a CARP report \(missing columns: a_label"):
        summarize_overlaps("other.csv")


def test_summary_of_real_report(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    write_report_csv(extract("abc12", cfg, source="doc")[1])
    (row,) = summarize_overlaps()
    assert (row["pair"], row["by_length"], row["wins"]) == ("A/B", 1, "A 1")


def test_summary_examples_limit_order_and_placeholder() -> None:
    _report([
        _row("a", "b", "length", file="len1", a_text="L1"),
        _row("a", "b", "score", file="s"),
        _row("a", "b", "priority", file="", a_text="P1", b_text="Q1"),
        _row("a", "b", "length", file="len2"),
        _row("a", "b", "start_label", file="sl", a_text="P2"),
        _row("a", "b", "priority", file="p3", a_text="P3"),
        _row("a", "b", "priority", file="p4", a_text="P4"),
    ])
    assert _by_pair()["a/b"]["examples"] == (
        '(no id): "P1" vs "Q1" | sl: "P2" vs "y" | p3: "P3" vs "y"'
    )
    _report([_row("a", "b", "length", file="len1", a_text="L1"),
             _row("a", "b", "priority", file="p", a_text="P")])
    assert _by_pair()["a/b"]["examples"] == 'p: "P" vs "y" | len1: "L1" vs "y"'


def test_summary_examples_empty_when_all_by_score() -> None:
    _report([_row("a", "b", "score")] * 4)
    row = _by_pair()["a/b"]
    assert (row["examples"], row["hint"]) == ("", "ok")


HINT_2 = "add nearby, context or keywords words"
HINT_3 = "if both values are valid, add to coexist"
PARTIAL = {"a_span": (0, 4), "b_span": (2, 6)}


@pytest.mark.parametrize(
    "rows, hint",
    [
        ([_row("a", "b", "length")], "ok"),
        ([_row("a", "b", "priority")], HINT_2),                                    # identical spans
        ([_row("a", "b", "priority", **PARTIAL)], f"{HINT_2}; {HINT_3}"),
        # unsettled = 3, partial = 1 (not more than half): no rule 3
        ([_row("a", "b", "priority", **PARTIAL), _row("a", "b", "length"),
          _row("a", "b", "start_label")], HINT_2),
        # unsettled = 2, partial = 1: exactly half is not more than half
        ([_row("a", "b", "priority", **PARTIAL), _row("a", "b", "priority")], HINT_2),
        # partial rows only among unsettled ones count; score rows are ignored
        ([_row("a", "b", "priority", **PARTIAL)] + [_row("a", "b", "score")] * 5,
         f"{HINT_2}; {HINT_3}"),
        ([_row("a", "b", "length", **PARTIAL), _row("a", "b", "score")], "ok"),
    ],
)
def test_summary_hint(rows: list[dict[str, Any]], hint: str) -> None:
    _report(rows)
    assert _by_pair()["a/b"]["hint"] == hint


# ------------------------------------------------- summarize_misses
MISS_KEYS = ["label", "misses", "no_candidates", "dropped", "examples", "hint"]
HINT_REGEX = "the regex found nothing; check the regex against these inputs"
HINT_TUNING = "candidates lost; raise their nearby/context weights or check min_score"


def _miss(label: str, outcome: str, file: str = "f") -> dict[str, Any]:
    return _row(label, "", "", file=file, kind="miss", outcome=outcome, a_text="")


def test_summarize_misses_counts_per_label() -> None:
    _report([
        _miss("phone", "no_candidates", "d1"), _miss("phone", "no_candidates", "d2"),
        _miss("phone", "candidates_dropped", "d3"), _miss("phone", "no_candidates", ""),
        _miss("acct", "candidates_dropped", "d5"),
        _row("a", "b", "priority"),                              # not a miss row
    ])
    rows = summarize_misses()
    assert [list(r) for r in rows] == [MISS_KEYS] * 2
    phone, acct = rows
    assert (phone["label"], phone["misses"], phone["no_candidates"], phone["dropped"]) == (
        "phone", 4, 3, 1)
    assert phone["examples"] == "d1 | d2 | d3"
    assert phone["hint"] == HINT_REGEX
    assert (acct["label"], acct["misses"], acct["dropped"], acct["hint"]) == (
        "acct", 1, 1, HINT_TUNING)


def test_summarize_misses_of_real_report() -> None:
    write_report_csv(extract(("call me", "Invoice INV-004521"), REAL_CFG, source="doc")[1])
    (row,) = summarize_misses()
    assert (row["label"], row["no_candidates"], row["examples"]) == ("phone_number", 1, "doc")


def test_summarize_misses_empty_and_non_report() -> None:
    _report([_row("a", "b", "score")])
    assert summarize_misses() == []
    with open("other.csv", "w", encoding="utf-8") as fh:
        fh.write("file,kind\n,miss\n")
    with pytest.raises(ValueError, match="not a CARP report"):
        summarize_misses("other.csv")


# ------------------------------------------------- --summarize CLI
def test_cli_summarize_prints_table_without_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _report([_row("phone", "acct", "priority", file="d1")] + [_row("x", "y", "score")])
    assert main(["--summarize"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == SUMMARY_KEYS
    assert lines[1].startswith("acct/phone ") and lines[2].startswith("x/y ")
    assert lines[1].index(" 1 ") == lines[2].index(" 1 ")          # aligned columns
    assert 'd1: "x" vs "y"' in lines[1]


def test_cli_summarize_explicit_path(capsys: pytest.CaptureFixture[str]) -> None:
    _report([_row("a", "b", "length")], path="other.csv")
    assert main(["--summarize", "other.csv"]) == 0
    assert "a/b" in capsys.readouterr().out



def test_cli_summarize_prints_miss_table_under_overlaps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _report([_row("a", "b", "length"), _miss("phone", "no_candidates", "d1")])
    assert main(["--summarize"]) == 0
    out = capsys.readouterr().out.split("\n\n")
    assert out[0].splitlines()[0].split() == SUMMARY_KEYS
    assert out[1].splitlines()[0].split() == MISS_KEYS
    assert out[1].splitlines()[1].startswith("phone ")
    _report([_row("a", "b", "length")])                         # no misses: no second table
    assert main(["--summarize"]) == 0
    assert "misses" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra", [["--text", "x"], ["--context", "x"], ["--report"]]
)
def test_cli_summarize_usage_errors(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    _report([_row("a", "b", "length")])
    with pytest.raises(SystemExit) as exc:
        main(["--summarize", *extra])
    assert exc.value.code == 2
    assert "--summarize" in capsys.readouterr().err


def test_cli_without_config_or_summarize_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--text", "x"])
    assert exc.value.code == 2
    assert "--config" in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, "file,kind\n"])
def test_cli_summarize_bad_report_is_a_clean_error(
    content: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    if content is not None:
        Path("extract_report.csv").write_text(content, encoding="utf-8")
    assert main(["--summarize"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:") and captured.out == ""



# ------------------------------------------------- public API, as the README shows it
def test_public_api_as_documented(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import json

    import summarize
    from extract import (format_misses, format_summary, open_report, reset_report,
                         summarize_misses, summarize_overlaps)

    cfg = load_config(str(CONFIG_PATH))
    assert isinstance(cfg, Config)

    # Library: the three input forms, (matches, decisions) shape.
    for inp in ("Call 212-555-0123 acct 2125550123",
                {"text": "Call 212-555-0123 acct 2125550123", "context": "call"},
                ("call", "Call 212-555-0123 acct 2125550123")):
        matches, decisions = extract(inp, cfg, source="id-1")
        base = {"label", "text", "start", "end", "score"}
        # Validated matches have the same shape; their text is the E.164 number.
        assert len(matches) == 2
        for m in matches:
            assert set(m.to_dict()) == base
        assert all(m.text == "+12125550123" for m in matches if m.pattern == "phone_number")
        assert decisions and all(list(d) == list(REPORT_FIELDS) for d in decisions)
        assert {d["file"] for d in decisions} == {"id-1"}
    matches, _ = extract({"text": "some text", "context": "call"}, cfg)
    assert isinstance(matches, list)
    m = matches_of(("call", "Call 212-555-0123"), cfg)[0]
    assert m.score == m.text_score + m.context_score

    # The README loop: reset, then stream with open_report.
    reset_report()
    items = [("id-1", "call", "Call 212-555-0123"), ("id-2", "tel", "Invoice INV-004521"),
             ("id-3", "", "Acct 2125550123 is overdue.")]
    with open_report() as write:
        for id, context, text in items:
            matches, decisions = extract((context, text), cfg, source=id)
            write(decisions)
    with open("extract_report.csv", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == REPORT_FIELDS
        rows = list(reader)
    # id-1 only has its phone's validation row; id-3's phone is checked too.
    assert {r["file"] for r in rows if r["kind"] != "validation"} == {"id-2", "id-3"}
    assert [(r["file"], r["status"], r["normalized"]) for r in rows if r["kind"] == "validation"] == [
        ("id-1", "valid", "+12125550123"), ("id-3", "valid", "+12125550123")]
    assert [(r["file"], r["a_label"]) for r in rows if r["kind"] == "miss"] == [
        ("id-2", "phone_number")]
    write_report_csv(extract("abc", cfg)[1])            # appends, no second header

    # Summaries from the library and their tables.
    rows = summarize_overlaps()
    assert rows and all(list(r) == list(summarize.SUMMARY_FIELDS) for r in rows)
    assert rows[0]["pair"] == "account_number/phone_number"
    assert format_summary(rows).splitlines()[0].split() == list(summarize.SUMMARY_FIELDS)
    misses = summarize_misses()
    assert [r["label"] for r in misses] == ["phone_number"]
    assert format_misses(misses).splitlines()[0].split() == list(summarize.MISS_FIELDS)

    # CLI, as in the README examples.
    config = str(CONFIG_PATH)
    assert main(["--config", config, "--report", "--text",
                 "Invoice INV-004521 dated 03/14/2026, amount due $1,250.00"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {m["label"] for m in out} == {"invoice_number", "date", "amount"}
    assert all(set(m) == {"label", "text", "start", "end", "score"} for m in out)
    assert main(["--config", config, "--context", "Acct", "--text",
                 "Acct 2125550123 call 212-555-0123"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(m["label"], m["score"]) for m in out] == [
        ("account_number", 4.0), ("valid_US_number", 2.0)]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"call 212-555-0123")))
    assert main(["--config", config, "--context", "phone"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["label"] == "valid_US_number"
    assert main(["--summarize"]) == 0
    assert capsys.readouterr().out.split()[:2] == ["pair", "overlaps"]


# ------------------------------------------------- YAML booleans/null as keywords
@pytest.mark.parametrize("where", KEY_NAMES)
@pytest.mark.parametrize("key, shown", [(True, "True"), (False, "False")])   # bare on / no
def test_non_string_keyword_is_rejected(
    tmp_path: Path, where: str, key: Any, shown: str
) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**{where: {key: 1}}))
    assert str(exc.value) == (
        f"pattern 'p': {where} keyword {shown} must be a string "
        '(quote it in the YAML, e.g. "on")'
    )


def test_bare_yaml_on_in_a_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bare.yaml"
    raw = CONFIG_PATH.read_text(encoding="utf-8").replace('"on": 1', "on: 1")
    assert raw != CONFIG_PATH.read_text(encoding="utf-8")
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="pattern 'date': keywords keyword True must be a string"):
        load_config(path)


# ------------------------------------------------- same words, different spelling
@pytest.mark.parametrize(
    "keys, where, key, other",
    [
        ({"nearby": {"amount due": 2, "amount-due": 2}}, "nearby", "amount-due", "amount due"),
        ({"context": {"don't": 1, "dont": 1}}, "context", "dont", "don't"),
        ({"keywords": {"e mail": -1, "e-mail": -1}}, "keywords", "e-mail", "e mail"),
    ],
)
def test_duplicate_after_word_reduction_is_rejected(
    tmp_path: Path, keys: dict[str, Any], where: str, key: str, other: str
) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**keys))
    assert str(exc.value) == (
        f"pattern 'p': {where} keyword '{key}' is listed more than once in this "
        f"pattern (same words as '{other}')"
    )


# ------------------------------------------------- summary: bounded examples, rounding
def test_summary_keeps_at_most_three_examples_per_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    import summarize as mod

    peak: list[int] = []
    real = mod._PairStats.add_example

    def spy(self: Any, example: str, no_evidence: bool) -> None:
        real(self, example, no_evidence)
        peak.append(len(self.examples))

    monkeypatch.setattr(mod._PairStats, "add_example", spy)
    _report([_row("a", "b", "length", file=f"l{i}") for i in range(5)]
            + [_row("a", "b", "priority", file=f"p{i}") for i in range(5)])
    assert _by_pair()["a/b"]["examples"] == 'p0: "x" vs "y" | p1: "x" vs "y" | p2: "x" vs "y"'
    assert max(peak) == 3


def test_summary_examples_mixed_order_stays_correct() -> None:
    _report([_row("a", "b", "length", file="l0"), _row("a", "b", "length", file="l1"),
             _row("a", "b", "priority", file="p0"), _row("a", "b", "length", file="l2"),
             _row("a", "b", "start_label", file="s0")])
    assert _by_pair()["a/b"]["examples"] == 'p0: "x" vs "y" | s0: "x" vs "y" | l0: "x" vs "y"'


@pytest.mark.parametrize("n, total, pct", [(1, 16, 6.3), (1, 8, 12.5), (2, 3, 66.7), (1, 3, 33.3)])
def test_summary_pct_rounds_half_up(n: int, total: int, pct: float) -> None:
    _report([_row("a", "b", "length")] * n + [_row("a", "b", "score")] * (total - n))
    assert _by_pair()["a/b"]["not_by_score_pct"] == pct


def test_cli_summarize_malformed_csv_is_a_clean_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _report([_row("a", "b", "length", a_text="x" * (csv.field_size_limit() + 1))])
    assert main(["--summarize"]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error:") and captured.out == ""


# ------------------------------------------------- blank entries
@pytest.mark.parametrize("where", KEY_NAMES)
def test_blank_keyword_entry_is_rejected(tmp_path: Path, where: str) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**{where: {None: 1}}))
    assert str(exc.value) == f"pattern 'p': {where} has an empty entry; remove it"


@pytest.mark.parametrize(
    "keys",
    [{}, {"context": None}, {"context": {}}, {"nearby": None}, {"nearby": {}},
     {"keywords": None, "nearby": {}, "context": None}],
)
def test_missing_or_blank_keyword_keys_load_as_empty(
    tmp_path: Path, keys: dict[str, Any]
) -> None:
    cfg = make_cfg(tmp_path, [{"label": "n", "priority": 1, "regex": r"\d+", **keys}])
    (p,) = cfg.patterns
    assert (p.nearby, p.context) == ({}, {})
    for inp in ("tel 12", ("tel", "12")):
        (m,) = matches_of(inp, cfg)
        assert (m.text, m.score) == ("12", 0)


# ------------------------------------------------- summarize.py module
def test_summary_lives_in_summarize_and_extract_reexports_it() -> None:
    import extract
    import summarize

    for name in ("summarize_overlaps", "format_summary", "summarize_misses", "format_misses"):
        assert getattr(extract, name) is getattr(summarize, name)
        assert getattr(summarize, name).__module__ == "summarize"
    assert extract.REPORT_PATH == summarize.DEFAULT_REPORT_PATH == "extract_report.csv"
    import ast

    tree = ast.parse(Path(summarize.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom)}
    assert not imported & {"extract", "words"}            # leaf module: stdlib only


def test_summarize_reads_only_report_columns() -> None:
    import summarize

    assert set(summarize.COLUMNS_USED) <= set(REPORT_FIELDS)
    # A report with only those columns summarizes the same as a full one.
    rows = [_row("a", "b", "priority", file="d"), _row("a", "b", "score"),
            _miss("p", "no_candidates", "d2")]
    _report(rows)
    full = (summarize_overlaps(), summarize_misses())
    with open("slim.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=summarize.COLUMNS_USED, lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    assert (summarize_overlaps("slim.csv"), summarize_misses("slim.csv")) == full


# ------------------------------------------------- keywords shorthand
def _pat(cfg: Config) -> Any:
    (p,) = cfg.patterns
    return p


def test_keywords_expand_into_nearby_and_context(tmp_path: Path) -> None:
    p = _pat(make_cfg(tmp_path, _one(keywords={"phone": 3, "Call": 2, "acct": -3})))
    expected = {"phone": 3.0, "call": 2.0, "acct": -3.0}
    assert p.nearby == expected and p.context == expected


def test_keywords_merge_with_explicit_keys(tmp_path: Path) -> None:
    keys = {"keywords": {"phone": 3, "acct": -3}, "context": {"mobile": 1}, "nearby": {"cell": 1}}
    p = _pat(make_cfg(tmp_path, _one(**keys)))
    assert p.context == {"mobile": 1.0, "phone": 3.0, "acct": -3.0}     # explicit words first
    assert p.nearby == {"cell": 1.0, "phone": 3.0, "acct": -3.0}


@pytest.mark.parametrize(
    "keys, where, word, same",
    [
        ({"keywords": {"tel": 1}, "context": {"tel": 2}}, "context", "tel", ""),
        ({"keywords": {"tel": 1}, "nearby": {"Tel": 2}}, "nearby", "tel", ""),
        ({"keywords": {"e-mail": 1}, "nearby": {"e mail": 2}}, "nearby", "e-mail",
         " (same words as 'e mail')"),
        ({"keywords": {"e-mail": -1}, "context": {"e mail": -2}}, "context", "e-mail",
         " (same words as 'e mail')"),
    ],
)
def test_word_in_keywords_and_explicit_key_is_rejected(
    tmp_path: Path, keys: dict[str, Any], where: str, word: str, same: str
) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**keys))
    assert str(exc.value) == f"pattern 'p': keyword '{word}' is in both keywords and {where}{same}"


# ------------------------------------------------- example config
REAL_CFG_KEYWORDS = {
    'invoice_number': {'invoice': 3.0, 'bill': 2.0, 'amount due': 2.0, 'ref': 1.0, 'phone': -3.0, 'tel': -3.0},
    'phone_number': {'phone': 3.0, 'tel': 3.0, 'call': 2.0, 'mobile': 2.0, 'fax': 1.0, 'invoice': -2.0, 'account': -2.0, 'order': -1.0},
    'account_number': {'account': 3.0, 'acct': 3.0, 'routing': 1.0, 'phone': -3.0, 'call': -2.0},
    'timestamp': {'at': 1.0, 'logged': 2.0, 'created': 2.0},
    'date': {'date': 2.0, 'due': 2.0, 'dated': 2.0, 'on': 1.0},
    'amount': {'total': 3.0, 'due': 2.0, 'amount': 2.0, 'balance': 2.0},
}


def test_example_config_keywords_reach_both_sources() -> None:
    for p in REAL_CFG.patterns:
        assert p.nearby == p.context == REAL_CFG_KEYWORDS[p.label]
        assert list(p.nearby) == list(REAL_CFG_KEYWORDS[p.label])     # same order, too


def test_example_config_uses_keywords_shorthand() -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert all("keywords" in p and "nearby" not in p and "context" not in p
               for p in raw["patterns"])


# ---------------------------------------------------------------- phone validation
import phone  # noqa: E402  (phonenumbers is a declared dependency)
import summarize  # noqa: E402
from summarize import format_validation, summarize_validation  # noqa: E402

PHONE_RE = r"\+?\(?\d[\d\s().-]{6,18}\d"


def phone_pattern(label: str = "ph", **validate: Any) -> dict[str, Any]:
    v = {"with": "phonenumbers", "regions": ["US"], **validate}
    return {"label": label, "priority": 1, "regex": PHONE_RE, "validate": v}


def run_phone(
    tmp_path: Path, inp: Any,
    patterns: list[dict[str, Any]] | None = None, **validate: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    cfg = make_cfg(tmp_path, patterns or [phone_pattern(**validate)])
    return extract(inp, cfg)


def vrows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in decisions if d["kind"] == "validation"]


def got(matches: list[Any]) -> list[tuple[str, str]]:
    """(label, text) of each phone match (text = the E.164 number), sorted (matches on one span
    are ordered by label)."""
    return sorted((m.label, m.text) for m in matches)


# ---- loading
@pytest.mark.parametrize("validate, message", [
    ({"with": "phonenumbers"}, "validate.regions: expected a non-empty list"),
    ({"with": "phonenumbers", "regions": []}, "validate.regions: expected a non-empty list"),
    ({"with": "regexlib", "regions": ["US"]}, r"validate.with: must be 'phonenumbers'"),
    ({"regions": ["US"]}, r"validate.with: must be 'phonenumbers'"),
    ({"with": "phonenumbers", "regions": ["US"], "colour": 1}, "unknown key 'colour'"),
    ({"with": "phonenumbers", "regions": ["XQ"]}, "validate.regions: unknown region 'XQ'"),
    ({"with": "phonenumbers", "regions": ["US", "us"]}, "'US' is listed twice"),
    ({"with": "phonenumbers", "regions": ["US"], "keep": "some"}, "validate.keep: unknown value 'some'"),
    ({"with": "phonenumbers", "regions": ["US"], "hint_conflict": "first"}, "validate.hint_conflict: unknown value"),
    ({"with": "phonenumbers", "regions": ["US"], "output_format": "e164"}, "validate.output_format: unknown value"),
    ({"with": "phonenumbers", "regions": ["US"], "valid_ignores_min_score": "yes"},
     "validate.valid_ignores_min_score: expected true or false"),
    ({"with": "phonenumbers", "regions": ["US"], "hints": {"GB": {"uk2": 1}}},
     "validate.hints.GB keyword 'uk2' must contain only ASCII letters"),
    ({"with": "phonenumbers", "regions": ["US"], "hints": {"GB": {"uk": -1}}},
     "validate.hints.GB: keyword 'uk' needs a weight > 0"),
    ({"with": "phonenumbers", "regions": ["US"], "hints": {"ZQ": {"zz": 1}}}, "validate.hints: unknown region 'ZQ'"),
    ("US", "validate must be a map"),
])
def test_validate_load_errors(tmp_path: Path, validate: Any, message: str) -> None:
    pattern = {"label": "ph", "priority": 1, "regex": PHONE_RE, "validate": validate}
    with pytest.raises(ValueError, match=rf"pattern 'ph': .*{message}"):
        make_cfg(tmp_path, [pattern])


def test_validate_defaults(tmp_path: Path) -> None:
    (p,) = make_cfg(tmp_path, [phone_pattern()]).patterns
    assert p.validate == phone.PhoneSpec(regions=["US"])
    assert (p.validate.keep, p.validate.hint_conflict) == ("valid_plus_home", "all")


def test_validate_needs_phonenumbers_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(phone, "_pn_module", None)
    monkeypatch.setattr(phone.importlib, "import_module", missing)
    with pytest.raises(ValueError, match="pattern 'ph' uses validate but phonenumbers is not installed"):
        make_cfg(tmp_path, [phone_pattern()])
    # Configs without validate never touch the library.
    cfg = make_cfg(tmp_path, [{"label": "n", "priority": 1, "regex": r"\d+"}])
    matches, decisions = extract("call 212-555-0123", cfg)
    assert [m.text for m in matches] == ["212", "555", "0123"]
    assert all(m.raw == "" for m in matches) and not vrows(decisions)


# ---- statuses and labels
def test_valid_number(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "call (212) 555-0123 today")
    (m,) = matches
    assert (m.label, m.text, m.pattern, m.start, m.end) == (
        "valid_US_number", "+12125550123", "ph", 5, 19)
    assert m.to_dict() == {"label": "valid_US_number", "text": "+12125550123",
                           "start": 5, "end": 19, "score": 0.0}
    (row,) = vrows(decisions)
    # The report keeps the written form in a_text and the E.164 form in normalized.
    assert (row["outcome"], row["status"], row["region"], row["normalized"], row["decided_by"],
            row["a_label"], row["a_text"], row["a_pattern"]) == (
        "kept", "valid", "US", "+12125550123", "regions", "valid_US_number", "(212) 555-0123", "ph")
    assert "FIXED_LINE_OR_MOBILE" not in m.to_dict().values()
    assert f"phonenumbers {phone.pn_version()}" in row["reason"]


def test_new_area_code_is_possible_and_kept(tmp_path: Path) -> None:
    """The critical case: right length, a range the library doesn't know (yet)."""
    matches, _ = run_phone(tmp_path, "call 221-234-5678")
    assert got(matches) == [("possible_US_number", "+12212345678")]


def test_impossible_hit_is_dropped_and_logged(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "zip 94105-1234")
    assert matches == []
    (row,) = vrows(decisions)
    assert (row["outcome"], row["status"], row["normalized"], row["a_text"]) == (
        "dropped", "impossible", "", "94105-1234")
    assert "impossible in US" in row["reason"] and "TOO_SHORT" in row["reason"]


def test_local_only_number_keeps_raw_value(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "dial 555-1234")
    assert got(matches) == [("possible_US_number", "555-1234")]


def test_nanp_readings_are_one_reading(tmp_path: Path) -> None:
    """US and CA give the same +1 number: one reading, labeled by its real region."""
    matches, _ = run_phone(tmp_path, "call 416-555-0123", regions=["US", "CA"], keep="all")
    assert got(matches) == [("valid_CA_number", "+14165550123")]


@pytest.mark.parametrize("fmt, value", [
    ("E164", "+12125550123"), ("INTERNATIONAL", "+1 212-555-0123"), ("NATIONAL", "(212) 555-0123")])
def test_output_format(tmp_path: Path, fmt: str, value: str) -> None:
    matches, _ = run_phone(tmp_path, "212.555.0123", output_format=fmt)
    assert got(matches) == [("valid_US_number", value)]


# ---- the region list and `keep`
@pytest.mark.parametrize("keep, text, expected", [
    # 555-123-4567: possible in US (home), valid in GB.
    ("valid_plus_home", "call 555-123-4567", [("possible_US_number", "+15551234567"),
                                             ("valid_GB_number", "+445551234567")]),
    ("all", "call 555-123-4567", [("possible_US_number", "+15551234567"),
                                  ("valid_GB_number", "+445551234567")]),
    ("valid_else_possible", "call 555-123-4567", [("valid_GB_number", "+445551234567")]),
    ("valid", "call 555-123-4567", [("valid_GB_number", "+445551234567")]),
    # 212-555-0123: valid in US, possible in GB.
    ("valid_plus_home", "call 212-555-0123", [("valid_US_number", "+12125550123")]),
    ("all", "call 212-555-0123", [("valid_US_number", "+12125550123"),
                                  ("possible_GB_number", "+442125550123")]),
    ("valid_else_possible", "call 212-555-0123", [("valid_US_number", "+12125550123")]),
    ("valid", "call 212-555-0123", [("valid_US_number", "+12125550123")]),
    # A new area code: possible in US and GB, valid nowhere.
    ("valid_plus_home", "call 221-234-5678", [("possible_US_number", "+12212345678")]),
    ("valid_else_possible", "call 221-234-5678", [("possible_US_number", "+12212345678"),
                                                  ("possible_GB_number", "+442212345678")]),
    ("valid", "call 221-234-5678", []),
])
def test_keep_modes(tmp_path: Path, keep: str, text: str, expected: list) -> None:
    matches, decisions = run_phone(tmp_path, text, regions=["US", "GB"], keep=keep)
    assert got(matches) == sorted(expected)
    rows = vrows(decisions)
    assert {r["decided_by"] for r in rows} == {"regions"}
    assert sum(r["outcome"] == "kept" for r in rows) == len(expected)
    assert sum(r["outcome"] == "not_kept" for r in rows) == len(rows) - len(expected)
    for r in rows:
        if r["outcome"] == "not_kept":
            assert f"keep: {keep}" in r["reason"]


def test_readings_of_one_hit_share_span_and_count(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "call 555-123-4567", regions=["US", "GB"])
    # Two readings on one span; their texts differ (+1..., +44...).
    assert {(m.start, m.end) for m in matches} == {(5, 17)}
    assert len(matches) == 2 and len({m.text for m in matches}) == 2
    assert not [d for d in decisions if d["kind"] in ("overlap", "coexist")]


def test_foreign_national_numbers_through_the_list(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "office 020 7946 0018", regions=["US", "GB"])
    assert got(matches) == [("valid_GB_number", "+442079460018")]
    # Without GB in the list, the UK national number is impossible in the US.
    assert run_phone(tmp_path, "office 020 7946 0018")[0] == []


# ---- `+` and 00
def test_plus_number_uses_its_country_code(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "tel +44 20 7946 0018", hints={"IN": {"tel": 5}})
    assert got(matches) == [("valid_GB_number", "+442079460018")]
    assert vrows(decisions)[0]["decided_by"] == "number"


@pytest.mark.parametrize("text", ["call 00 44 20 7946 0018", "call 0044 20 7946 0018"])
def test_double_zero_prefix_reads_as_plus(tmp_path: Path, text: str) -> None:
    """00 is not an international prefix in the US; as a last resort it is read as +."""
    matches, decisions = run_phone(tmp_path, text)
    assert got(matches) == [("valid_GB_number", "+442079460018")]
    (row,) = vrows(decisions)
    assert row["decided_by"] == "number" and "read the leading 00 as +" in row["reason"]


def test_011_prefix_from_the_us(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "call 011 44 20 7946 0018")
    assert got(matches) == [("valid_GB_number", "+442079460018")]


# ---- hints
def test_regions_come_only_from_the_config(tmp_path: Path) -> None:
    """There is no per-call region: extract() and the CLI take none."""
    with pytest.raises(TypeError):
        extract("call 212-555-0123", make_cfg(tmp_path, [phone_pattern()]), region="GB")
    with pytest.raises(SystemExit):
        main(["--config", str(CONFIG_PATH), "--region", "GB", "--text", "x"])


UK_INDIA = {"GB": {"uk": 3, "london": 2}, "IN": {"india": 3}}
FR_NL = {"FR": {"paris": 2}, "NL": {"amsterdam": 2}}


def test_hints_parse_only_the_hinted_regions(tmp_path: Path) -> None:
    """'our UK office called India at ...': valid only in IN; GB's reading isn't valid."""
    matches, decisions = run_phone(tmp_path, "our UK office called India at 98765 43210",
                                   hints=UK_INDIA)
    assert got(matches) == [("valid_IN_number", "+919876543210")]
    rows = vrows(decisions)
    assert {r["decided_by"] for r in rows} == {"hint"}
    assert [(r["region"], r["outcome"]) for r in rows] == [("IN", "kept"), ("GB", "not_kept")]


def test_hint_that_does_not_fit_falls_back_to_the_list(tmp_path: Path) -> None:
    """'paris' hints FR, where 212-555-0123 is impossible: the list decides."""
    matches, decisions = run_phone(tmp_path, ("paris", "call 212-555-0123"), hints=FR_NL)
    assert got(matches) == [("valid_US_number", "+12125550123")]
    (row,) = vrows(decisions)
    assert row["decided_by"] == "regions" and "hint FR did not fit" in row["reason"]
    # Impossible everywhere: dropped, with both notes.
    _, decisions = run_phone(tmp_path, ("paris", "call 1234-5678-9012345"), hints=FR_NL)
    (row,) = vrows(decisions)
    assert row["outcome"] == "dropped" and "hint FR did not fit" in row["reason"]


@pytest.mark.parametrize("fallback, expected", [
    ("if_nothing", [("possible_GB_number", "+442125550123")]),
    ("if_not_valid", [("possible_GB_number", "+442125550123"), ("valid_US_number", "+12125550123")]),
    ("always", [("possible_GB_number", "+442125550123"), ("valid_US_number", "+12125550123")]),
    ("never", [("possible_GB_number", "+442125550123")]),
])
def test_hint_fallback_when_the_hint_is_only_possible(
    tmp_path: Path, fallback: str, expected: list
) -> None:
    """'london' hints GB, where 212-555-0123 is only possible (it is valid in the US)."""
    matches, decisions = run_phone(tmp_path, ("london", "phone 212-555-0123"),
                                   hints=UK_INDIA, hint_fallback=fallback)
    assert got(matches) == expected
    sources = {r["region"]: r["decided_by"] for r in vrows(decisions)}
    assert sources["GB"] == "hint"
    if fallback not in ("if_nothing", "never"):
        assert sources["US"] == "regions"
        kept = {r["a_label"]: r["decided_by"] for r in vrows(decisions) if r["outcome"] == "kept"}
        assert kept == {"possible_GB_number": "hint", "valid_US_number": "regions"}
    if fallback == "if_not_valid":
        assert "hint_fallback: if_not_valid parsed the regions too" in vrows(decisions)[0]["reason"]


@pytest.mark.parametrize("fallback, expected", [
    ("if_nothing", [("valid_IN_number", "+919876543210")]),
    ("if_not_valid", [("valid_IN_number", "+919876543210")]),
    ("always", [("possible_US_number", "+19876543210"), ("valid_IN_number", "+919876543210")]),
    ("never", [("valid_IN_number", "+919876543210")]),
])
def test_hint_fallback_when_the_hint_is_valid(tmp_path: Path, fallback: str, expected: list) -> None:
    """A valid hinted reading stops the search unless hint_fallback: always."""
    matches, _ = run_phone(tmp_path, "india office 98765 43210", hints=UK_INDIA,
                           hint_fallback=fallback)
    assert got(matches) == expected


def test_hint_fallback_default_and_errors(tmp_path: Path) -> None:
    (p,) = make_cfg(tmp_path, [phone_pattern()]).patterns
    assert p.validate.hint_fallback == "if_not_valid"
    with pytest.raises(ValueError, match="validate.hint_fallback: unknown value 'sometimes'"):
        make_cfg(tmp_path, [phone_pattern(hint_fallback="sometimes")])


@pytest.mark.parametrize("fallback, expected", [
    ("never", []),
    ("if_nothing", [("valid_US_number", "+12125550123")]),
    ("if_not_valid", [("valid_US_number", "+12125550123")]),
    ("always", [("valid_US_number", "+12125550123")]),
])
def test_hint_fallback_when_the_hint_does_not_fit(tmp_path: Path, fallback: str, expected: list) -> None:
    """'paris' hints FR, where 212-555-0123 is impossible."""
    matches, decisions = run_phone(tmp_path, ("paris", "call 212-555-0123"),
                                   hints=FR_NL, hint_fallback=fallback)
    assert got(matches) == expected
    if fallback == "never":
        (row,) = vrows(decisions)
        assert (row["outcome"], row["decided_by"]) == ("dropped", "hint")
        assert "impossible in hinted FR; hint_fallback: never" in row["reason"]


FR_NUM, NL_NUM = ("valid_FR_number", "+33612345678"), ("valid_NL_number", "+31612345678")


def test_hint_conflict_all_returns_both(tmp_path: Path) -> None:
    """06 12 34 56 78 is valid in both FR and NL."""
    matches, _ = run_phone(tmp_path, "amsterdam desk forwarded paris office 06 12 34 56 78",
                           hints=FR_NL)
    assert got(matches) == [FR_NUM, NL_NUM]                # nearest hint first


def test_hint_conflict_nearest(tmp_path: Path) -> None:
    text = "amsterdam desk forwarded paris office 06 12 34 56 78"
    matches, decisions = run_phone(tmp_path, text, hints=FR_NL, hint_conflict="nearest")
    assert got(matches) == [FR_NUM]
    (nl,) = [r for r in vrows(decisions) if r["region"] == "NL"]
    assert nl["outcome"] == "not_kept" and "hint_conflict: nearest" in nl["reason"]


def test_hint_conflict_nearest_text_beats_context(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, ("paris", "amsterdam desk: 06 12 34 56 78"),
                           hints=FR_NL, hint_conflict="nearest")
    assert got(matches) == [NL_NUM]


def test_hint_conflict_nearest_exact_tie_returns_both(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "paris 06 12 34 56 78 amsterdam",
                           hints=FR_NL, hint_conflict="nearest")
    assert sorted(got(matches)) == sorted([FR_NUM, NL_NUM])


def test_hint_regions_need_not_be_listed(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "paris office 06 12 34 56 78", hints=FR_NL)
    assert got(matches) == [FR_NUM]


# ---- refinement
def test_span_refinement_trims_and_splits(tmp_path: Path) -> None:
    text = "call 212-555-0123 2026 renewal"
    matches, _ = run_phone(tmp_path, text)
    assert [(text[m.start:m.end], m.start, m.end) for m in matches] == [("212-555-0123", 5, 17)]
    text = "call 212-555-0123 or 212-555-0199"
    matches, _ = run_phone(tmp_path, text)
    assert [text[m.start:m.end] for m in matches] == ["212-555-0123", "212-555-0199"]


def test_span_refinement_scores_the_trimmed_span(tmp_path: Path) -> None:
    """Refined candidates are scored at their own span (digits are not words,
    so "renewal" is the first word after the number)."""
    pattern = {**phone_pattern(), "keywords": {"renewal": 2}}
    (m,) = run_phone(tmp_path, "212-555-0123 2026 renewal", patterns=[pattern])[0]
    assert (m.text, m.start, m.end) == ("+12125550123", 0, 12) and m.score == pytest.approx(2.0)


@pytest.mark.parametrize("text, expected", [
    ("212-555-0123 212-555-0199", ["212-555-0123", "212-555-0199"]),
    ("2125550123 2125550199", ["2125550123", "2125550199"]),
    ("(212) 555-0123 (212) 555-0199", ["(212) 555-0123", "(212) 555-0199"]),
    ("555-1234 555-5678", ["555-1234", "555-5678"]),
    ("212-555-0123, 212-555-0199", ["212-555-0123", "212-555-0199"]),
    # Space-grouped: the matcher rejects the run, so trailing groups are trimmed.
    ("212 555 0123 212 555 0124", ["212 555 0123", "212 555 0124"]),
    ("212 555 0123  212 555 0124", ["212 555 0123", "212 555 0124"]),
    ("Tel 212 555 0123 12 units", ["212 555 0123"]),
    ("call 212 555 0123 2026", ["212 555 0123"]),
])
def test_back_to_back_numbers_are_all_found(tmp_path: Path, text: str, expected: list[str]) -> None:
    """A greedy hit can run into the next number; the scan resumes where the
    refined number ended, so the next one is still found whole."""
    matches, _ = run_phone(tmp_path, text)
    assert [text[m.start:m.end] for m in matches] == expected


def test_trim_keeps_a_valid_prefix_over_a_longer_possible_one() -> None:
    assert phone.trim_to_number("212 555 0123 2026", ["US"]) == len("212 555 0123")
    assert phone.trim_to_number("94105-1234", ["US"]) is None       # nothing: raw hit kept


# ---- validate.valid_ignores_min_score
MIN_SCORE_PHONE = {**phone_pattern(), "keywords": {"account": -2, "call": 1}}


@pytest.mark.parametrize("ignores, expected", [
    (True, [("valid_US_number", "+12125550145")]),
    (False, []),
])
def test_valid_reading_survives_a_low_score(tmp_path: Path, ignores: bool, expected: list) -> None:
    """'account' in the context pushes the phone below min_score 0."""
    pattern = {**MIN_SCORE_PHONE, "validate": {**MIN_SCORE_PHONE["validate"],
                                               "valid_ignores_min_score": ignores}}
    matches, decisions = run_phone(tmp_path, ("Account", "call me at 212-555-0145"), [pattern])
    assert got(matches) == expected
    assert any(d["kind"] == "below_min_score" for d in decisions) == (not ignores)
    if ignores:
        assert matches[0].score < 0


def test_possible_reading_still_needs_min_score(tmp_path: Path) -> None:
    """555 is no real area code: possible, so min_score still applies."""
    matches, decisions = run_phone(tmp_path, ("Account", "call me at 555-123-4567"), [MIN_SCORE_PHONE])
    assert matches == []
    assert [d["a_label"] for d in decisions if d["kind"] == "below_min_score"] == ["possible_US_number"]


def test_valid_reading_below_min_score_still_loses_overlaps(tmp_path: Path) -> None:
    acct = {"label": "account_number", "priority": 3, "regex": r"\b\d{10}\b",
            "keywords": {"account": 3}}
    matches, decisions = run_phone(tmp_path, ("Account", "2125550145"), [MIN_SCORE_PHONE, acct])
    assert [m.label for m in matches] == ["account_number"]
    (row,) = [d for d in decisions if d["kind"] == "overlap"]
    assert (row["b_label"], row["decided_by"]) == ("valid_US_number", "score")


def test_matcher_finding_nothing_keeps_the_raw_span(tmp_path: Path) -> None:
    """PhoneNumberMatcher finds nothing in "94105-1234"; the hit is still
    checked and logged, never silently lost."""
    _, decisions = run_phone(tmp_path, "zip 94105-1234")
    assert vrows(decisions)[0]["a_text"] == "94105-1234"


def test_timestamp_is_not_a_phone_in_the_real_config() -> None:
    matches = matches_of("logged 2026-03-14 10:00", REAL_CFG)
    assert {m.label for m in matches} == {"timestamp", "date"}


# ---- overlaps, coexist, misses
ACCOUNT = {"label": "account", "priority": 9, "regex": r"\b\d{10}\b", "keywords": {"acct": 3}}


def test_readings_compete_with_other_patterns(tmp_path: Path) -> None:
    patterns = [phone_pattern(regions=["US", "GB"]), ACCOUNT]
    matches, decisions = run_phone(tmp_path, "acct 5551234567", patterns=patterns)
    assert [(m.label, m.text) for m in matches] == [("account", "5551234567")]
    overlaps = [d for d in decisions if d["kind"] == "overlap"]
    assert sorted(d["b_label"] for d in overlaps) == ["possible_US_number", "valid_GB_number"]
    matches, _ = run_phone(tmp_path, "call 5551234567", patterns=patterns)
    assert sorted(m.label for m in matches) == ["possible_US_number", "valid_GB_number"]


def test_coexist_by_pattern_label_covers_every_reading(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [phone_pattern(), ACCOUNT], coexist=[["ph", "account"]])
    matches, _ = extract("acct 2125550123", cfg)
    assert sorted(m.label for m in matches) == ["account", "valid_US_number"]


def test_coexist_by_generated_label(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [phone_pattern(), ACCOUNT], coexist=[["valid_US_number", "account"]])
    assert len(extract("acct 2125550123", cfg)[0]) == 2


def test_a_reading_counts_for_its_pattern_in_miss_rows(tmp_path: Path) -> None:
    pattern = {**phone_pattern(), "context": {"call": 1}}
    matches, decisions = run_phone(tmp_path, ("call", "221-234-5678"), patterns=[pattern])
    assert [m.label for m in matches] == ["possible_US_number"]
    assert not [d for d in decisions if d["kind"] == "miss"]


def test_validation_drop_counts_as_dropped_in_miss_row(tmp_path: Path) -> None:
    pattern = {**phone_pattern(), "context": {"call": 1}}
    _, decisions = run_phone(tmp_path, ("call", "94105-1234"), patterns=[pattern])
    (miss,) = [d for d in decisions if d["kind"] == "miss"]
    assert (miss["outcome"], miss["a_label"]) == ("candidates_dropped", "ph")


# ---- CLI, report, summary
def test_normalized_number_in_cli_json(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(["--config", str(CONFIG_PATH), "--text", "office +44 20 7946 0018"]) == 0
    (out,) = json.loads(capsys.readouterr().out)
    assert (out["label"], out["text"], out["start"], out["end"]) == (
        "valid_GB_number", "+442079460018", 7, 23)


@pytest.mark.parametrize("fmt, expected", [
    ("E164", "+12125550123"),
    ("INTERNATIONAL", "+1 212-555-0123"),
    ("NATIONAL", "(212) 555-0123"),
    ("RAW", "212.555.0123"),
])
def test_output_format_sets_the_match_text(tmp_path: Path, fmt: str, expected: str) -> None:
    text = "call 212.555.0123"
    matches, decisions = run_phone(tmp_path, text, output_format=fmt)
    (m,) = matches
    assert (m.label, m.text) == ("valid_US_number", expected)
    if fmt == "RAW":
        assert m.text == text[m.start:m.end]
    (row,) = vrows(decisions)
    # The report: written form in a_text, E.164 in normalized, in every format.
    assert (row["a_text"], row["normalized"]) == ("212.555.0123", "+12125550123")


def test_output_format_rejects_unknown_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RAW"):
        run_phone(tmp_path, "call 212-555-0123", output_format="E.164")


def test_local_only_number_keeps_its_text(tmp_path: Path) -> None:
    for fmt in ("E164", "NATIONAL"):
        matches, _ = run_phone(tmp_path, "call 555-0123", output_format=fmt, keep="all")
        assert [(m.label, m.text) for m in matches] == [("possible_US_number", "555-0123")]


def test_overlap_summary_groups_phone_readings_by_pattern(tmp_path: Path) -> None:
    acct = {"label": "account_number", "priority": 3, "regex": r"\b\d{10}\b",
            "keywords": {"acct": 3}}
    cfg = make_cfg(tmp_path, [phone_pattern(regions=["US", "GB"], keep="all"), acct])
    reset_report()
    with open_report() as write:
        for i, text in enumerate(["acct 2125550123", "acct 5551234567"]):
            write(extract(text, cfg, source=f"r{i}")[1])
    (row,) = summarize_overlaps()
    assert row["pair"] == "account_number/ph"
    assert row["overlaps"] >= 2 and row["wins"].startswith("account_number")


def test_validation_rows_in_report_and_summary(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [phone_pattern(regions=["US", "GB"])])
    reset_report()
    with open_report() as write:
        for i, text in enumerate(["call 221-234-5678", "call 221-234-9999", "call 221-555-0000",
                                  "ref 1234-5678-9012345", "call 212-555-0123", "call 555-123-4567"]):
            write(extract(text, cfg, source=f"r{i}")[1])
    (row,) = summarize_validation()
    assert row["pattern"] == "ph"
    assert (row["hits"], row["kept"], row["valid"], row["possible"], row["dropped"], row["multi"]) == (
        6, 6, 2, 4, 1, 1)
    assert row["not_kept"] == 4          # the possible GB readings of the 221 and 212 numbers
    assert row["clusters"] == "+1 221 x3; +1 555 x1"
    assert summarize.HINT_STALE in row["hint"] and summarize.HINT_MULTI in row["hint"]
    assert 'r5: "555-123-4567"' in row["examples"] and 'r3: "1234-5678-9012345"' in row["examples"]
    assert format_validation([row]).splitlines()[0].split() == list(summarize.VALIDATION_FIELDS)


def test_summarize_cli_prints_validation_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_report_csv(extract("call 212-555-0123", REAL_CFG)[1])
    assert main(["--summarize"]) == 0
    out = capsys.readouterr().out
    assert "pattern" in out and "hits" in out and "phone_number" in out


def test_report_phone_columns_sit_before_decided_by() -> None:
    assert REPORT_FIELDS[-4:] == ("status", "region", "normalized", "decided_by")
    assert "a_pattern" in REPORT_FIELDS and "b_pattern" in REPORT_FIELDS


# ---- code-review fixes
def test_idd_number_is_labeled_by_its_country(tmp_path: Path) -> None:
    """011 44 parsed in US is a UK number: its label and region say GB, not US."""
    matches, decisions = run_phone(tmp_path, "call 011 44 20 7946 095")
    assert got(matches) == [("possible_GB_number", "+44207946095")]
    (row,) = vrows(decisions)
    assert (row["region"], row["normalized"]) == ("GB", "+44207946095")
    assert "possible in GB" in row["reason"]


def test_plus_number_too_short_for_a_full_number_is_dropped(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "tel +1 555 0123")
    assert matches == []
    (row,) = vrows(decisions)
    assert (row["outcome"], row["decided_by"], row["normalized"]) == ("dropped", "number", "")
    assert "possible only as a local number" in row["reason"]


def test_non_geographic_number_gets_region_001(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "free +800 1234 5678")
    assert got(matches) == [("valid_001_number", "+80012345678")]
    assert vrows(decisions)[0]["region"] == "001"


def test_local_only_reading_has_blank_normalized(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "dial 555-1234")
    assert got(matches) == [("possible_US_number", "555-1234")]
    (row,) = vrows(decisions)
    assert (row["status"], row["normalized"], row["a_text"]) == ("possible", "", "555-1234")


def test_normalized_column_is_e164_or_blank(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [phone_pattern(regions=["US", "GB"], keep="all")])
    texts = ["call 212-555-0123", "dial 555-1234", "zip 94105-1234", "tel +1 555 0123",
             "call 011 44 20 7946 095", "+44 20 7946 0958"]
    for text in texts:
        for row in vrows(extract(text, cfg)[1]):
            assert row["normalized"] == "" or re.fullmatch(r"\+\d{6,15}", row["normalized"]), row


@pytest.mark.parametrize("where, raw, message", [
    ("top", {"setings": {}}, r"config: unknown key 'setings' \(expected one of settings, "),
    ("settings", {"min_scor": 1}, r"settings: unknown key 'min_scor' \(expected one of .*stop_at"),
    ("normalize", {"collapse_space": True}, r"normalize: unknown key 'collapse_space'"),
    ("pattern", {"neaby": {"call": 1}}, r"pattern 'p': unknown key 'neaby' \(expected one of .*validate"),
    ("pattern", {"validat": {"with": "phonenumbers"}}, r"pattern 'p': unknown key 'validat'"),
])
def test_unknown_config_keys_fail(tmp_path: Path, where: str, raw: dict, message: str) -> None:
    settings = {"context_window_words": 8, "distance_decay": True, "min_score": 0,
                "case_sensitive": False, "overlap_tiebreak": []}
    pattern = {"label": "p", "priority": 1, "regex": r"\d+"}
    cfg: dict[str, Any] = {"settings": settings, "normalize": {}, "patterns": [pattern]}
    if where == "top":
        cfg.update(raw)
    elif where == "settings":
        settings.update(raw)
    elif where == "normalize":
        cfg["normalize"] = raw
    else:
        pattern.update(raw)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_blank_validate_block_fails(tmp_path: Path) -> None:
    pattern = {"label": "ph", "priority": 1, "regex": PHONE_RE, "validate": None}
    with pytest.raises(ValueError, match=r"pattern 'ph': validate is empty"):
        make_cfg(tmp_path, [pattern])


def test_summarize_validation_separates_hits_at_same_offsets(tmp_path: Path) -> None:
    """Hits from different texts with a blank source and equal offsets stay apart."""
    cfg = make_cfg(tmp_path, [phone_pattern()])
    reset_report()
    with open_report() as write:
        for text in ["call 212-555-0123", "call 212-555-0123", "call 415-555-0123"]:
            write(extract(text, cfg)[1])
    (row,) = summarize_validation()
    assert (row["hits"], row["kept"], row["multi"]) == (3, 3, 0)
    assert summarize.HINT_MULTI not in row["hint"]


def test_summarize_validation_still_counts_multi_reading_hits(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [phone_pattern(regions=["US", "GB"], keep="all")])
    reset_report()
    with open_report() as write:
        for _ in range(2):
            write(extract("call 221-234-5678", cfg)[1])
    (row,) = summarize_validation()
    assert (row["hits"], row["multi"]) == (2, 2)


def test_region_hints_are_computed_once_for_unrefined_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import extract as ex
    cfg = make_cfg(tmp_path, [phone_pattern(regions=["US", "GB"], hints={"GB": {"london": 1}})])
    calls: list[tuple[int, int]] = []
    real = ex._region_hints
    def spy(text: str, start: int, end: int, *a: Any) -> Any:
        calls.append((start, end))
        return real(text, start, end, *a)
    monkeypatch.setattr(ex, "_region_hints", spy)
    matches, _ = ex.extract("london office 020 7946 0958", cfg)
    assert got(matches) == [("valid_GB_number", "+442079460958")]
    assert len(calls) == 1


# ---- review fixes: load-time validation of every config value
def _write_raw(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "raw.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _base_raw(**over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "settings": {
            "context_window_words": 8, "distance_decay": True, "min_score": 0,
            "case_sensitive": False, "overlap_tiebreak": ["score"],
        },
        "patterns": [
            {"label": "A", "priority": 1, "regex": r"\d+"},
            {"label": "B", "priority": 2, "regex": r"[a-z]+"},
        ],
    }
    raw.update(over)
    return raw


def _with(path: str, value: Any) -> dict[str, Any]:
    """_base_raw() with one value replaced; path like 'settings.min_score' or 'patterns.0.priority'."""
    raw = _base_raw()
    *parents, last = path.split(".")
    node: Any = raw
    for p in parents:
        node = node[int(p)] if p.isdigit() else node[p]
    node[int(last) if last.isdigit() else last] = value
    return raw


@pytest.mark.parametrize("raw, message", [
    (_base_raw(coexist=["A", "B"]), "coexist: entry 'A' must be a pair of labels"),
    (_base_raw(coexist=[["A", "B", "A"]]), "must be a pair of labels"),
    (_base_raw(coexist=[["A", "A"]]), "names 'A' twice"),
    (_base_raw(coexist=[["A", "C"]]), "coexist: unknown label 'C' (expected one of A, B"),
    (_base_raw(coexist=[["A", "valid_US_number"]]), "unknown label 'valid_US_number'"),
    (_base_raw(coexist={"A": "B"}), "coexist: expected a list of label pairs"),
    (_with("settings.case_sensitive", "false"), "settings.case_sensitive: expected true or false"),
    (_with("settings.distance_decay", "no"), "settings.distance_decay: expected true or false"),
    (_base_raw(normalize={"collapse_whitespace": "no"}),
     "normalize.collapse_whitespace: expected true or false"),
    (_base_raw(normalize=[]), "normalize: expected a map"),
    (_base_raw(normalize=False), "normalize: expected a map"),
    (_with("settings.min_score", "x"), "settings.min_score: expected a number"),
    (_with("settings.min_score", True), "settings.min_score: expected a number"),
    (_with("settings.context_window_words", 2.5), "settings.context_window_words: expected a whole number"),
    (_with("settings.context_window_words", -1), "settings.context_window_words: expected a whole number"),
    (_with("patterns.0.priority", None), "pattern 'A': priority: expected a whole number"),
    (_with("patterns.0.priority", "high"), "pattern 'A': priority: expected a whole number"),
    (_with("patterns.0.regex", None), "pattern 'A': regex: expected a non-empty string"),
    (_with("patterns.0.regex", ""), "pattern 'A': regex: expected a non-empty string"),
    (_with("patterns.0.label", None), "pattern: label: expected a non-empty string"),
    (_with("patterns.0.label", 5), "pattern: label: expected a non-empty string"),
    (_base_raw(patterns=None), "config: patterns: expected a list of patterns"),
    (_base_raw(patterns=["A"]), "config: patterns: entry 'A' must be a map"),
    (_with("patterns.0.label", "valid_US_number"), "is the form of a phone reading label"),
    (_with("patterns.0.label", "possible_001_number"), "is the form of a phone reading label"),
    (_base_raw(tests={"text": "x"}), "tests: expected a list of test cases"),
    (_base_raw(tests=[{"txt": "x", "expect": []}]), "tests[0]: unknown key 'txt'"),
    (_base_raw(tests=[{"text": "x", "contxt": "uk", "expect": []}]), "tests[0]: unknown key 'contxt'"),
    (_base_raw(tests=[{"expect": []}]), "tests[0]: missing required key 'text'"),
    (_base_raw(tests=[{"text": "x"}]), "tests[0]: missing required key 'expect'"),
    (_base_raw(tests=[{"text": 5, "expect": []}]), "tests[0].text: expected a string"),
    (_base_raw(tests=[{"text": "x", "context": 5, "expect": []}]), "tests[0].context: expected a string"),
    (_base_raw(tests=[{"text": "x", "expect": {"label": "A"}}]), "tests[0].expect: expected a list"),
    (_base_raw(tests=[{"text": "x", "expect": [{"label": "A"}]}]),
     "tests[0].expect[0]: expected a map with label and text"),
    (_base_raw(tests=[{"text": "x", "expect": [{"label": "A", "text": "1", "x": 1}]}]),
     "tests[0].expect[0]: expected a map with label and text"),
])
def test_load_rejects_bad_values(tmp_path: Path, raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as exc:
        load_config(_write_raw(tmp_path, raw))
    assert message in str(exc.value)


def test_load_accepts_valid_tests_and_coexist(tmp_path: Path) -> None:
    raw = _base_raw(
        coexist=[["A", "B"]],
        normalize=None,
        tests=[
            {"name": "n", "text": "x", "context": "c", "expect": [{"label": "B", "text": "x"}]},
            {"text": "y", "context": None, "expect": None},
        ],
    )
    cfg = load_config(_write_raw(tmp_path, raw))
    assert cfg.coexist == {frozenset(("A", "B"))}
    assert cfg.normalize == {}
    assert len(cfg.tests) == 2


def test_coexist_accepts_reading_labels_with_a_validate_pattern(tmp_path: Path) -> None:
    raw = _base_raw(coexist=[["valid_US_number", "A"], ["possible_001_number", "B"]])
    raw["patterns"].append(phone_pattern())
    cfg = load_config(_write_raw(tmp_path, raw))
    assert frozenset(("valid_US_number", "A")) in cfg.coexist


def test_blank_priority_is_an_error_line_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_raw(tmp_path, _with("patterns.0.priority", None))
    assert main(["--config", str(path), "--text", "x"]) == 2
    assert "error: pattern 'A': priority" in capsys.readouterr().err


def test_summarize_rejects_config(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--summarize", "--config", "x.yaml"])
    assert exc.value.code == 2


def test_plus_number_local_only_drop_is_logged_as_possible(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "Call +1 555 1234")
    assert matches == []
    (row,) = vrows(decisions)
    assert (row["outcome"], row["status"]) == ("dropped", "possible")
    assert "possible only as a local number" in row["reason"]


def test_local_only_drops_do_not_trigger_the_impossible_hint(tmp_path: Path) -> None:
    import summarize
    cfg = make_cfg(tmp_path, [phone_pattern()])
    reset_report()
    write_report_csv(extract("Call +1 555 1234", cfg)[1])
    (row,) = summarize_validation()
    assert row["dropped"] == 1
    assert summarize.HINT_IMPOSSIBLE not in row["hint"]


def test_keep_counts_valid_hinted_readings_with_hint_fallback_always(tmp_path: Path) -> None:
    validate = {"regions": ["GB", "US"], "hints": {"US": {"usa": 1}},
                "keep": "valid_else_possible", "hint_fallback": "always"}
    plain, _ = run_phone(tmp_path, "call 212 555 0100", **validate)
    hinted, decisions = run_phone(tmp_path, "usa call 212 555 0100", **validate)
    assert got(plain) == got(hinted) == [("valid_US_number", "+12125550100")]
    gb = [r for r in vrows(decisions) if r["region"] == "GB"]
    assert [(r["outcome"], r["status"]) for r in gb] == [("not_kept", "possible")]
    assert "keep: valid_else_possible" in gb[0]["reason"]
