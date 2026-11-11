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
        assert "phone_number" in {m.label for m in matches_of((context, text), REAL_CFG)}


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
    assert [m.label for m in matches_of("2125550123 call", REAL_CFG)] == ["phone_number"]
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
    assert out[0]["text"] == "212-555-0123"
    assert set(out[0]) == {"label", "text", "start", "end", "score", "extra"}
    assert out[0]["extra"]["normalized"] == "+12125550123"
    assert not Path("extract_report.csv").exists()


def test_cli_reads_text_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io, json

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"call 212-555-0123")))
    assert main(["--config", str(CONFIG_PATH), "--context", "phone"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["label"] == "phone_number"

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
    assert overlap[0]["b_label"] == "phone_number"
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


# Old-format configs fail with a message that says what to change.
OLD_CONTEXT_MSG = (
    "Patterns no longer select or penalize; `context` is now a map of word: signed "
    "weight added to the score when the word is in the context string "
    "(e.g. context: {acct: 3, phone: -2}). Use `keywords` for words that "
    "should count in both the text and the context."
)


@pytest.mark.parametrize(
    "context, key",
    [
        ({"positive": ["phone"]}, "positive"),
        ({"positive": {"phone": 3}}, "positive"),
        ({"penalize": {"acct": -3}}, "penalize"),
        ({"negative": {"acct": -3}}, "negative"),
        ({"positive": None, "penalize": None}, "positive"),
    ],
)
def test_old_context_shape_is_rejected(tmp_path: Path, context: Any, key: str) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(context=context))
    assert str(exc.value) == f"pattern 'p': `context.{key}` is the old format. {OLD_CONTEXT_MSG}"


def test_old_key_names_are_fine_as_new_keywords(tmp_path: Path) -> None:
    p = _pat(make_cfg(tmp_path, _one(context={"negative": -2, "positive": 1, "penalize": -1})))
    assert p.context == {"negative": -2.0, "positive": 1.0, "penalize": -1.0}


@pytest.mark.parametrize("name", ["context_penalty", "context_negative"])
def test_old_tiebreak_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(), overlap_tiebreak=["score", name])
    assert str(exc.value) == (
        f"settings.overlap_tiebreak: `{name}` was removed; the context now adds to "
        "`score` (pattern `context` / `keywords` weights). Remove it."
    )


def test_old_config_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.yaml"
    raw = CONFIG_PATH.read_text(encoding="utf-8").replace(
        "keywords: {phone: 3, tel: 3, call: 2, mobile: 2, fax: 1, invoice: -2, account: -2, order: -1}",
        "context:\n      positive: [phone]\n      penalize: {acct: -3}")
    assert raw != CONFIG_PATH.read_text(encoding="utf-8")
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match=r"pattern 'phone_number': `context.positive` is the old format"):
        load_config(path)


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
    [("", "account_number"), (".!?", "phone_number")],
)
def test_stop_at_period_changes_overlap_winner(stop_at: str, expected_label: str) -> None:
    """'Acct' sits before a period. Without stop_at it boosts account_number
    (+3) so it beats phone_number. With stop_at='.' the scan ends at the period,
    both scores are 0, and the priority tie-break picks phone_number."""
    matches = matches_of("Acct. 2125550123", _real_cfg_with_stop_at(stop_at))
    assert [(m.label, m.text) for m in matches] == [(expected_label, "2125550123")]


@pytest.mark.parametrize("stop_at", ["", ".!?"])
def test_stop_at_keeps_context_after_period(stop_at: str) -> None:
    """'phone' is after the period and before the number, so it still counts
    whether or not stop_at is set: phone/2 words away = 1.5."""
    matches = matches_of("acct. phone is 212-456-2342", _real_cfg_with_stop_at(stop_at))
    assert [(m.label, m.text) for m in matches] == [("phone_number", "212-456-2342")]
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


def test_summary_needs_decided_by_column() -> None:
    with open("old.csv", "w", encoding="utf-8") as fh:
        fh.write("file,kind,outcome,reason\n,overlap,a_wins_b_dropped,x\n")
    with pytest.raises(ValueError, match="regenerate it with --report"):
        summarize_overlaps("old.csv")


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


def test_summarize_misses_empty_and_old_report() -> None:
    _report([_row("a", "b", "score")])
    assert summarize_misses() == []
    with open("old.csv", "w", encoding="utf-8") as fh:
        fh.write("file,kind\n,miss\n")
    with pytest.raises(ValueError, match="regenerate it with --report"):
        summarize_misses("old.csv")


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
        # Matches of a pattern with `validate` also carry `extra`.
        assert len(matches) == 2
        for m in matches:
            assert set(m.to_dict()) == (base | {"extra"} if "phone" in m.label else base)
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
        ("account_number", 4.0), ("phone_number", 2.0)]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"call 212-555-0123")))
    assert main(["--config", config, "--context", "phone"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["label"] == "phone_number"
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
    return {"label": label, "priority": 1, "regex": PHONE_RE,
            "validate": {"with": "phonenumbers", "default_region": "US", **validate}}


def run_phone(
    tmp_path: Path, inp: Any, region: str | None = None,
    patterns: list[dict[str, Any]] | None = None, **validate: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    cfg = make_cfg(tmp_path, patterns or [phone_pattern(**validate)])
    return extract(inp, cfg, region=region)


def vrows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in decisions if d["kind"] == "validation"]


def extra_of(matches: list[Any], *keys: str) -> list[tuple]:
    return [tuple(m.extra[k] for k in keys) for m in matches]


@pytest.mark.parametrize("validate, message", [
    ({"with": "phonenumbers"}, "missing required key 'default_region'"),
    ({"with": "regexlib", "default_region": "US"}, r"validate.with: must be 'phonenumbers'"),
    ({"default_region": "US"}, r"validate.with: must be 'phonenumbers'"),
    ({"with": "phonenumbers", "default_region": "US", "colour": 1}, "unknown key 'colour'"),
    ({"with": "phonenumbers", "default_region": "XQ"}, "validate.default_region: unknown region 'XQ'"),
    ({"with": "phonenumbers", "default_region": "US", "try_regions": ["GB"]},
     "must include default_region 'US'"),
    ({"with": "phonenumbers", "default_region": "US", "try_regions": ["US", "us"]}, "'US' is listed twice"),
    ({"with": "phonenumbers", "default_region": "US", "try_regions": []}, "non-empty list"),
    ({"with": "phonenumbers", "default_region": "US", "on_possible": "maybe"},
     "validate.on_possible: unknown value 'maybe'"),
    ({"with": "phonenumbers", "default_region": "US", "region_from": "anywhere"},
     "validate.region_from: unknown value"),
    ({"with": "phonenumbers", "default_region": "US", "output_format": "e164"},
     "validate.output_format: unknown value"),
    ({"with": "phonenumbers", "default_region": "US", "possible_min_score": "high"},
     "possible_min_score: expected a number"),
    ({"with": "phonenumbers", "default_region": "US", "regions": {"GB": {"uk2": 1}}},
     "validate.regions.GB keyword 'uk2' must contain only ASCII letters"),
    ({"with": "phonenumbers", "default_region": "US", "regions": {"GB": {"uk": -1}}},
     "validate.regions.GB: keyword 'uk' needs a weight > 0"),
    ({"with": "phonenumbers", "default_region": "US", "regions": {"ZQ": {"zz": 1}}},
     "validate.regions: unknown region 'ZQ'"),
    ({"with": "phonenumbers", "default_region": "US", "possible_label": "ph"},
     "possible_label: must differ"),
    ("US", "validate must be a map"),
])
def test_validate_load_errors(tmp_path: Path, validate: Any, message: str) -> None:
    pattern = {"label": "ph", "priority": 1, "regex": PHONE_RE, "validate": validate}
    with pytest.raises(ValueError, match=rf"pattern 'ph': .*{message}"):
        make_cfg(tmp_path, [pattern])


def test_possible_label_may_not_reuse_a_label(tmp_path: Path) -> None:
    patterns = [phone_pattern(possible_label="other"),
                {"label": "other", "priority": 2, "regex": "x"}]
    with pytest.raises(ValueError, match="possible_label 'other' is already used"):
        make_cfg(tmp_path, patterns)


def test_validate_defaults(tmp_path: Path) -> None:
    (p,) = make_cfg(tmp_path, [phone_pattern()]).patterns
    assert p.validate == phone.PhoneSpec(default_region="US", try_regions=["US"])
    assert p.validate.on_possible == "keep"          # never lose an unknown area code


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
    assert all(m.extra is None for m in matches) and not vrows(decisions)


def test_valid_number_gets_e164_extra(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "call (212) 555-0123 today")
    (m,) = matches
    assert (m.label, m.text) == ("ph", "(212) 555-0123")
    assert m.extra == {"normalized": "+12125550123", "region": "US", "region_source": "trial",
                       "status": "valid", "number_type": "FIXED_LINE_OR_MOBILE",
                       "ambiguous": False, "runner_up": None}
    (row,) = vrows(decisions)
    assert (row["outcome"], row["status"], row["region"], row["normalized"], row["decided_by"]) == (
        "kept", "valid", "US", "+12125550123", "trial")
    assert f"phonenumbers {phone.pn_version()}" in row["reason"]


def test_new_area_code_is_possible_and_kept_by_default(tmp_path: Path) -> None:
    """The critical case: right length, a range the library doesn't know (yet)."""
    matches, decisions = run_phone(tmp_path, "call 221-234-5678", possible_label="maybe_ph")
    assert extra_of(matches, "status", "normalized", "region_source") == [
        ("possible", "+12212345678", "default")]
    assert matches[0].label == "maybe_ph"
    assert vrows(decisions)[0]["outcome"] == "kept"


@pytest.mark.parametrize("on_possible, text, kept", [
    ("keep", "221-234-5678", True),
    ("drop", "call 221-234-5678", False),
    ("keep_if_score", "call 221-234-5678", True),     # total score 2 >= 1
    ("keep_if_score", "221-234-5678", False),         # score 0 < 1
])
def test_on_possible(tmp_path: Path, on_possible: str, text: str, kept: bool) -> None:
    pattern = {**phone_pattern(on_possible=on_possible), "keywords": {"call": 2}}
    matches, decisions = run_phone(tmp_path, text, patterns=[pattern])
    assert bool(matches) is kept
    (row,) = vrows(decisions)
    assert row["outcome"] == ("kept" if kept else "dropped")
    if on_possible == "keep_if_score":
        assert "possible_min_score 1" in row["reason"]


@pytest.mark.parametrize("on_invalid, outcome", [("drop", "dropped"), ("keep_flagged", "kept_flagged")])
def test_on_invalid(tmp_path: Path, on_invalid: str, outcome: str) -> None:
    matches, decisions = run_phone(tmp_path, "zip 94105-1234", on_invalid=on_invalid,
                                   possible_label="maybe_ph")
    (row,) = vrows(decisions)
    assert (row["outcome"], row["status"], row["normalized"]) == (outcome, "impossible", "94105-1234")
    assert "TOO_SHORT" in row["reason"]
    if outcome == "dropped":
        assert matches == []
    else:
        assert [(m.label, m.extra["status"]) for m in matches] == [("maybe_ph", "impossible")]


def test_local_only_number_keeps_raw_value(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "dial 555-1234")
    assert extra_of(matches, "status", "normalized") == [("possible", "555-1234")]


def test_invalid_phone_drops_before_overlap(tmp_path: Path) -> None:
    """A dropped phone never competes, so the account number wins without an overlap row."""
    patterns = [phone_pattern(on_possible="drop"),
                {"label": "account", "priority": 9, "regex": r"\b\d{10}\b"}]
    matches, decisions = run_phone(tmp_path, "ref 0123456789", patterns=patterns)
    assert [(m.label, m.text) for m in matches] == [("account", "0123456789")]
    assert [d["kind"] for d in decisions] == ["validation"]


def test_span_refinement_trims_and_splits(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "call 212-555-0123 2026 renewal")
    assert [(m.text, m.start, m.end) for m in matches] == [("212-555-0123", 5, 17)]
    matches, _ = run_phone(tmp_path, "call 212-555-0123 or 212-555-0199")
    assert [m.text for m in matches] == ["212-555-0123", "212-555-0199"]


def test_span_refinement_scores_the_trimmed_span(tmp_path: Path) -> None:
    """Refined candidates are scored at their own span (digits are not words,
    so "renewal" is the first word after the number)."""
    pattern = {**phone_pattern(), "keywords": {"renewal": 2}}
    (m,) = run_phone(tmp_path, "212-555-0123 2026 renewal", patterns=[pattern])[0]
    assert (m.text, m.end) == ("212-555-0123", 12) and m.score == pytest.approx(2.0)


def test_matcher_finding_nothing_keeps_the_raw_span(tmp_path: Path) -> None:
    """PhoneNumberMatcher finds nothing in "94105-1234"; the hit is still
    checked and logged, never silently lost."""
    matches, decisions = run_phone(tmp_path, "zip 94105-1234", on_invalid="keep_flagged")
    assert [m.text for m in matches] == ["94105-1234"]
    assert vrows(decisions)[0]["a_text"] == "94105-1234"


def test_timestamp_is_not_a_phone_in_the_real_config() -> None:
    matches = matches_of("logged 2026-03-14 10:00", REAL_CFG)
    assert {m.label for m in matches} == {"timestamp", "date"}


def test_source_number(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "tel +44 20 7946 0018", regions={"IN": {"tel": 5}})
    assert extra_of(matches, "region", "region_source", "normalized") == [
        ("GB", "number", "+442079460018")]


def test_source_caller(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "office 020 7946 0018", region="gb")
    assert extra_of(matches, "region", "region_source", "status") == [("GB", "caller", "valid")]
    # Without the caller region the UK national number is impossible in the US.
    assert run_phone(tmp_path, "office 020 7946 0018")[0] == []


def test_caller_region_that_does_not_fit_falls_through(tmp_path: Path) -> None:
    """212-555-0123 is only possible in GB; the valid US reading wins.
    (A caller region where it is valid, e.g. DE, would win: the caller is trusted.)"""
    matches, decisions = run_phone(tmp_path, "call 212-555-0123", region="GB")
    assert extra_of(matches, "region", "region_source", "runner_up") == [("US", "trial", "GB")]
    assert "caller GB only possible" in vrows(decisions)[0]["reason"]
    matches, _ = run_phone(tmp_path, "call 212-555-0123", region="DE")
    assert extra_of(matches, "region", "region_source") == [("DE", "caller")]


def test_unknown_caller_region_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="region: unknown region 'XQ'"):
        run_phone(tmp_path, "call 212-555-0123", region="XQ")


UK_INDIA = {"GB": {"uk": 3, "london": 2}, "IN": {"india": 3}}


def test_source_keyword_validity_decides(tmp_path: Path) -> None:
    """'our UK office called India at ...': the number is valid only in IN."""
    matches, decisions = run_phone(tmp_path, "our UK office called India at 98765 43210",
                                   regions=UK_INDIA)
    assert extra_of(matches, "region", "region_source", "normalized", "ambiguous") == [
        ("IN", "keyword", "+919876543210", False)]
    assert "'india' 2 words away" in vrows(decisions)[0]["reason"]


FR_NL = {"FR": {"paris": 2}, "NL": {"amsterdam": 2}}


def test_source_proximity_nearer_hint_wins(tmp_path: Path) -> None:
    """06 12 34 56 78 is valid in both FR and NL: the nearer hint word decides."""
    text = "amsterdam desk forwarded paris office 06 12 34 56 78"
    matches, decisions = run_phone(tmp_path, text, regions=FR_NL)
    assert extra_of(matches, "region", "region_source", "ambiguous", "runner_up", "normalized") == [
        ("FR", "proximity", True, "NL", "+33612345678")]
    reason = vrows(decisions)[0]["reason"]
    assert "valid in FR and NL; FR chosen: 'paris' 2 words away vs 'amsterdam' 5 words away" in reason


def test_text_hint_beats_context_hint(tmp_path: Path) -> None:
    text = "amsterdam desk forwarded it: 06 12 34 56 78"
    matches, _ = run_phone(tmp_path, ("paris", text), regions=FR_NL)
    assert extra_of(matches, "region", "region_source") == [("NL", "proximity")]


@pytest.mark.parametrize("region_from, region", [("text", "NL"), ("context", "FR"), ("both", "NL")])
def test_region_from(tmp_path: Path, region_from: str, region: str) -> None:
    matches, _ = run_phone(tmp_path, ("paris", "amsterdam 06 12 34 56 78"),
                           regions=FR_NL, region_from=region_from)
    assert extra_of(matches, "region") == [(region,)]


@pytest.mark.parametrize("on_ambiguous, outcome", [
    ("first", "kept"), ("drop", "dropped"), ("keep_flagged", "kept_flagged")])
def test_exact_proximity_tie_uses_on_ambiguous(
    tmp_path: Path, on_ambiguous: str, outcome: str
) -> None:
    matches, decisions = run_phone(tmp_path, "paris 06 12 34 56 78 amsterdam",
                                   regions=FR_NL, on_ambiguous=on_ambiguous)
    (row,) = vrows(decisions)
    assert (row["outcome"], row["decided_by"]) == (outcome, "fallback")
    assert "hint words equally near" in row["reason"]
    assert len(matches) == (0 if outcome == "dropped" else 1)


def test_possible_hint_yields_to_valid_trial(tmp_path: Path) -> None:
    """A hint word cannot turn a valid home number into a merely possible foreign one."""
    matches, decisions = run_phone(tmp_path, ("london", "phone 416-555-0123"), regions=UK_INDIA)
    assert extra_of(matches, "region", "region_source", "status", "runner_up") == [
        ("CA", "trial", "valid", "GB")]
    assert "keyword GB only possible" in vrows(decisions)[0]["reason"]


def test_nanp_regions_are_not_ambiguous(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "call 416-555-0123", try_regions=["US", "CA"])
    assert extra_of(matches, "region", "region_source", "ambiguous") == [("CA", "trial", False)]


def test_trial_collision_is_ambiguous(tmp_path: Path) -> None:
    matches, decisions = run_phone(tmp_path, "call 0612345678", default_region="FR",
                                   try_regions=["FR", "NL"])
    assert extra_of(matches, "region", "region_source", "ambiguous", "runner_up") == [
        ("FR", "fallback", True, "NL")]
    assert vrows(decisions)[0]["outcome"] == "kept"         # on_ambiguous: first


def test_home_possible_number_is_not_replaced_by_a_foreign_valid_one(tmp_path: Path) -> None:
    """555-123-4567 is possible in the US and valid in GB. The grouping 3-3-4
    matches the US format, so the US reading wins (source format), with GB
    logged as the runner-up."""
    matches, _ = run_phone(tmp_path, "call 555-123-4567", try_regions=["US", "GB"])
    assert extra_of(matches, "region", "status", "region_source", "runner_up") == [
        ("US", "possible", "format", "GB")]


def test_trial_finds_a_region_listed_after_the_default(tmp_path: Path) -> None:
    matches, _ = run_phone(tmp_path, "office 020 7946 0018", try_regions=["US", "GB"])
    assert extra_of(matches, "region", "region_source", "normalized") == [
        ("GB", "trial", "+442079460018")]


@pytest.mark.parametrize("fmt, value", [
    ("E164", "+12125550123"), ("INTERNATIONAL", "+1 212-555-0123"), ("NATIONAL", "(212) 555-0123")])
def test_output_format(tmp_path: Path, fmt: str, value: str) -> None:
    matches, _ = run_phone(tmp_path, "212.555.0123", output_format=fmt)
    assert extra_of(matches, "normalized") == [(value,)]


def test_coexist_covers_possible_label(tmp_path: Path) -> None:
    patterns = [phone_pattern(possible_label="maybe_ph"),
                {"label": "digits", "priority": 2, "regex": r"\d{3}-\d{4}"}]
    cfg = make_cfg(tmp_path, patterns, coexist=[["ph", "digits"]])
    assert frozenset({"maybe_ph", "digits"}) in cfg.coexist
    matches, _ = extract("call 221-234-5678", cfg)
    assert sorted(m.label for m in matches) == ["digits", "maybe_ph"]


def test_possible_label_match_is_not_a_miss(tmp_path: Path) -> None:
    pattern = {**phone_pattern(possible_label="maybe_ph"), "context": {"call": 1}}
    matches, decisions = run_phone(tmp_path, ("call", "221-234-5678"), patterns=[pattern])
    assert [m.label for m in matches] == ["maybe_ph"]
    assert not [d for d in decisions if d["kind"] == "miss"]


def test_validation_drop_counts_as_dropped_in_miss_row(tmp_path: Path) -> None:
    pattern = {**phone_pattern(on_possible="drop"), "context": {"call": 1}}
    _, decisions = run_phone(tmp_path, ("call", "221-234-5678"), patterns=[pattern])
    (miss,) = [d for d in decisions if d["kind"] == "miss"]
    assert miss["outcome"] == "candidates_dropped"


def test_extra_in_cli_json_and_region_flag(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(["--config", str(CONFIG_PATH), "--region", "GB",
                 "--text", "office 020 7946 0018"]) == 0
    (out,) = json.loads(capsys.readouterr().out)
    assert (out["label"], out["extra"]["normalized"], out["extra"]["region_source"]) == (
        "phone_number", "+442079460018", "caller")
    assert main(["--config", str(CONFIG_PATH), "--region", "XQ", "--text", "x"]) == 2
    assert "unknown region 'XQ'" in capsys.readouterr().err


def test_validation_rows_in_report_and_summary(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, [phone_pattern(possible_label="maybe_ph", regions=FR_NL)])
    reset_report()
    with open_report() as write:
        for i, text in enumerate(["call 221-234-5678", "call 221-234-9999", "call 221-555-0000",
                                  "zip 94105-1234", "call 212-555-0123",
                                  "paris 06 12 34 56 78 amsterdam"]):
            write(extract(text, cfg, source=f"r{i}")[1])
    rows = {r["label"]: r for r in summarize_validation()}
    maybe, ph = rows["maybe_ph"], rows["ph"]
    assert (maybe["checked"], maybe["possible"], maybe["impossible"], maybe["dropped"]) == (4, 3, 1, 1)
    assert maybe["clusters"] == "+1 221 x3"
    assert summarize.HINT_STALE in maybe["hint"]
    assert (ph["valid"], ph["ambiguous"]) == (2, 1)
    assert summarize.HINT_AMBIGUOUS in ph["hint"]
    assert 'r5: "06 12 34 56 78"' in ph["examples"]
    assert format_validation(list(rows.values())).splitlines()[0].split() == list(
        summarize.VALIDATION_FIELDS)


def test_summarize_cli_prints_validation_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_report_csv(extract("call 212-555-0123", REAL_CFG)[1])
    assert main(["--summarize"]) == 0
    out = capsys.readouterr().out
    assert "label" in out and "checked" in out and "phone_number" in out


def test_report_phone_columns_sit_before_decided_by() -> None:
    assert REPORT_FIELDS[-4:] == ("status", "region", "normalized", "decided_by")


@pytest.mark.parametrize("text, expected", [
    ("212-555-0123 212-555-0199", ["212-555-0123", "212-555-0199"]),
    ("2125550123 2125550199", ["2125550123", "2125550199"]),
    ("(212) 555-0123 (212) 555-0199", ["(212) 555-0123", "(212) 555-0199"]),
    ("555-1234 555-5678", ["555-1234", "555-5678"]),
    ("212-555-0123, 212-555-0199", ["212-555-0123", "212-555-0199"]),
])
def test_back_to_back_numbers_are_all_found(tmp_path: Path, text: str, expected: list[str]) -> None:
    """A greedy hit can run into the next number; the scan resumes where the
    refined number ended, so the next one is still found whole."""
    matches, _ = run_phone(tmp_path, text)
    assert [m.text for m in matches] == expected
