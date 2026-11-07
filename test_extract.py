"""Tests for extract.py. Config `tests` section is the source of truth for the
end-to-end cases; the rest are focused unit tests."""
from __future__ import annotations

import csv
import re
import sys
import warnings
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from extract import (
    REPORT_FIELDS,
    Config,
    _context_penalty,
    _context_words,
    extract,
    load_config,
    log_context_miss,
    main,
    open_report,
    select_patterns,
    summarize_overlaps,
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
         "context": {"positive": ["num"]}, "nearby": {"bad": -1}},
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


def test_select_patterns_penalize_and_nearby_words_do_not_select(tmp_path: Path) -> None:
    patterns = [
        {"label": "A", "priority": 1, "regex": "a", "context": {"positive": ["yes"]}},
        {"label": "B", "priority": 2, "regex": "b", "context": {"penalize": {"no": -1}},
         "nearby": {"maybe": 2}},
    ]
    cfg = make_cfg(tmp_path, patterns, overlap_tiebreak=["context_penalty"])
    chosen, decision = select_patterns("no maybe", cfg.patterns)   # only B's penalize/nearby words
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
         "context": {"positive": ["num"]}, "nearby": {"bad": -1}},
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
def _one(**keys: Any) -> list[dict[str, Any]]:
    return [{"label": "p", "priority": 1, "regex": "x", **keys}]


@pytest.mark.parametrize("keyword", ["w2", "1099", "café", "$"])
@pytest.mark.parametrize(
    "where, build",
    [
        ("context.positive", lambda k: {"context": {"positive": [k]}}),
        ("context.penalize", lambda k: {"context": {"penalize": {k: -1}}}),
        ("nearby", lambda k: {"nearby": {k: 1}}),
    ],
)
def test_keyword_that_can_never_match_is_rejected(
    tmp_path: Path, keyword: str, where: str, build: Any
) -> None:
    with pytest.raises(ValueError, match=rf"pattern 'p': {where} keyword .*ASCII letters"):
        make_cfg(tmp_path, _one(**build(keyword)), overlap_tiebreak=["context_penalty"])


@pytest.mark.parametrize(
    "keys, where",
    [
        ({"context": {"positive": ["invoice", "Invoice"]}}, "context.positive"),
        ({"context": {"penalize": {"bad": -1, "BAD": -2}}}, "context.penalize"),
        ({"nearby": {"tel": 1, "Tel": 2}}, "nearby"),
    ],
)
def test_duplicate_keyword_within_a_key_is_rejected(
    tmp_path: Path, keys: dict[str, Any], where: str
) -> None:
    with pytest.raises(ValueError, match=rf"pattern 'p': {where} keyword .* more than once"):
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])


def test_keyword_in_positive_and_penalize_is_rejected(tmp_path: Path) -> None:
    keys = {"context": {"positive": ["invoice"], "penalize": {"Invoice": -3}}}
    with pytest.raises(ValueError, match="pattern 'p': keyword 'invoice' is in both"):
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])


def test_keyword_may_be_in_context_and_nearby(tmp_path: Path) -> None:
    keys = {"context": {"positive": ["tel"], "penalize": {"fax": -1}},
            "nearby": {"tel": 2, "fax": -1}}
    cfg = make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])
    assert cfg.patterns[0].positive == ["tel"] and cfg.patterns[0].nearby == {"tel": 2, "fax": -1}


def test_positive_penalize_weight_is_rejected(tmp_path: Path) -> None:
    keys = {"context": {"penalize": {"word": 3}}}
    with pytest.raises(ValueError, match="context.penalize keyword 'word' has weight 3"):
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])


@pytest.mark.parametrize("weight", [-2, 0, 2.5])
def test_nearby_weights_may_have_any_sign(tmp_path: Path, weight: float) -> None:
    cfg = make_cfg(tmp_path, _one(nearby={"word": weight}))
    assert cfg.patterns[0].nearby == {"word": float(weight)}


@pytest.mark.parametrize(
    "keys, message",
    [
        ({"nearby": {"word": "high"}}, "nearby keyword 'word' needs a numeric weight"),
        ({"nearby": ["word"]}, "nearby must be a map"),
        ({"context": {"penalize": ["word"]}}, "context.penalize must be a map"),
        ({"context": {"positive": "word"}}, "context.positive` must be a list"),
        ({"context": {"positive": [True]}}, "context.positive keyword True must be a string"),
    ],
)
def test_malformed_keyword_keys_are_rejected(
    tmp_path: Path, keys: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])


# Old-format configs fail with a message that says what to change.
def test_old_positive_map_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(context={"positive": {"phone": 3}}))
    assert str(exc.value) == (
        "pattern 'p': `context.positive` is now a list of words; "
        "move weights for text scoring to `nearby`."
    )


def test_old_negative_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(context={"negative": {"acct": -3}}))
    assert str(exc.value) == (
        "pattern 'p': `context.negative` was renamed to `context.penalize`; "
        "it no longer affects the text score (use `nearby`)."
    )


def test_old_context_negative_tiebreak_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(), overlap_tiebreak=["score", "context_negative"])
    assert str(exc.value) == "`context_negative` was renamed to `context_penalty`."


def test_hyphenated_keyword_matches_like_context_words(tmp_path: Path) -> None:
    # words.py splits on hyphens, so "e-mail" must be the two words e + mail.
    patterns = [{"label": "p", "priority": 1, "regex": r"\d+",
                 "context": {"positive": ["e-mail"]}, "nearby": {"e-mail": 2}}]
    cfg = make_cfg(tmp_path, patterns)
    assert matches_of("e-mail 42", cfg)[0].score == 2.0
    assert _sel_in(cfg, "send e-mail") == ["p"]


def _sel_in(cfg: Config, context: str) -> list[str]:
    chosen, decision = select_patterns(context, cfg.patterns)
    return [p.label for p in chosen] if decision["outcome"] == "patterns_restricted" else []


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


# ------------------------------------------------- context_penalty tie-break
# Two patterns match the same span "abcd"; both have a positive hit in the
# context. B has the higher priority number, so priority alone would pick A.
CTX_PEN_PATTERNS = [
    {"label": "A", "priority": 1, "regex": "abcd",
     "context": {"positive": ["alpha"], "penalize": {"bad": -2, "very bad": -3}}},
    {"label": "B", "priority": 2, "regex": "abcd",
     "context": {"positive": ["beta"], "penalize": {}}},
]
CTX_PEN_ORDER = ["score", "context_penalty", "length", "priority"]


def test_context_penalty_applies_when_context_selected_patterns(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=CTX_PEN_ORDER)
    matches, decisions = extract({"text": "abcd", "context": "alpha beta bad"}, cfg)
    assert pairs(matches) == Counter({("B", "abcd"): 1})
    overlap = [d for d in decisions if d["kind"] == "overlap"][0]
    assert "context penalty 0 > -2" in overlap["reason"]


def test_context_penalty_phrase(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=CTX_PEN_ORDER)
    matches, decisions = extract({"text": "abcd", "context": "alpha beta very bad"}, cfg)
    assert pairs(matches) == Counter({("B", "abcd"): 1})
    # "bad" (-2) and "very bad" (-3) both occur: sum is -5
    assert "-5" in [d for d in decisions if d["kind"] == "overlap"][0]["reason"]


def test_context_penalty_is_opt_in(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="never used"):
        cfg = make_cfg(tmp_path, CTX_PEN_PATTERNS)  # default order, no context_penalty
    matches, _ = extract({"text": "abcd", "context": "alpha beta bad"}, cfg)
    assert pairs(matches) == Counter({("A", "abcd"): 1})  # priority decides


def test_context_penalty_ties_without_context(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=CTX_PEN_ORDER)
    assert pairs(matches_of("abcd", cfg)) == Counter({("A", "abcd"): 1})


def test_context_penalty_ties_when_context_selects_nothing(tmp_path: Path) -> None:
    """Context with no positive hits runs all patterns: penalties must NOT apply."""
    cfg = make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=CTX_PEN_ORDER)
    matches, _ = extract({"text": "abcd", "context": "bad"}, cfg)
    assert pairs(matches) == Counter({("A", "abcd"): 1})  # falls through to priority


def test_penalize_never_lowers_score_or_selects(tmp_path: Path) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": {"positive": ["num"], "penalize": {"bad": -5}}}]
    cfg = make_cfg(tmp_path, patterns, overlap_tiebreak=CTX_PEN_ORDER)
    for inp in ("bad 12", ("bad", "bad 12"), ("num bad", "bad 12")):
        (m,) = matches_of(inp, cfg)
        assert m.score == 0
    assert _sel_in(cfg, "bad") == []


def test_nearby_never_reads_context_and_positive_never_reads_text(tmp_path: Path) -> None:
    patterns = [
        {"label": "n", "priority": 1, "regex": r"\d+",
         "context": {"positive": ["num"]}, "nearby": {"tel": 3}},
        {"label": "w", "priority": 2, "regex": "zz", "context": {"positive": ["word"]}},
    ]
    cfg = make_cfg(tmp_path, patterns)
    (m,) = matches_of(("num tel", "12"), cfg)          # tel only in the context
    assert m.score == 0
    _, decisions = extract(("tel", "num 12 zz"), cfg)  # num only in the text
    assert decisions[0]["outcome"] == "all_patterns_used"
    _, decisions = extract(("word", "num 12 zz"), cfg)
    assert decisions[0]["patterns"] == "w"


# Parity: `nearby` (no decay, window covering every word) and context_penalty
# score the same keywords with the same rules.
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
]


@pytest.mark.parametrize("weights, words, expected", PARITY_CASES)
def test_nearby_and_context_penalty_parity(
    tmp_path: Path, weights: dict[str, float], words: str, expected: float
) -> None:
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": {"penalize": weights}, "nearby": weights}]
    cfg = make_cfg(tmp_path, patterns, distance_decay=False, min_score=-100,
                   context_window_words=50, overlap_tiebreak=CTX_PEN_ORDER)
    pattern = cfg.patterns[0]
    nearby_before = matches_of(f"{words} 12", cfg)[0].score
    nearby_after = matches_of(f"12 {words}", cfg)[0].score
    penalty, _ = _context_penalty(_context_words(words), pattern)
    assert nearby_before == nearby_after == penalty == pytest.approx(expected)


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
        make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=["nope"])


# ------------------------------------------------- decided_by / penalty_skipped
def test_report_fields_end_with_decided_by_and_penalty_skipped() -> None:
    assert REPORT_FIELDS[-2:] == ("decided_by", "penalty_skipped")
    assert REPORT_FIELDS[:5] == ("file", "kind", "outcome", "reason", "patterns")


def _overlap(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    (row,) = [d for d in decisions if d["kind"] == "overlap"]
    return row


@pytest.mark.parametrize(
    "patterns, order, inp, decided_by",
    [
        ([{"label": "A", "priority": 1, "regex": "abcd", "nearby": {"go": 1}},
          {"label": "B", "priority": 2, "regex": "abcd"}],
         ["score", "length", "priority"], "go abcd", "score"),
        (CTX_PEN_PATTERNS, CTX_PEN_ORDER, ("alpha beta bad", "abcd"), "context_penalty"),
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
    assert _overlap(extract(inp, cfg)[1])["decided_by"] == decided_by


def test_other_decided_by_values(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS, coexist=[["A", "B"]])
    rows = {d["kind"]: d for d in extract("abc12", cfg)[1]}
    assert rows["coexist"]["decided_by"] == "coexist"
    assert rows["context"]["decided_by"] == ""
    patterns = [{"label": "n", "priority": 1, "regex": r"\d+",
                 "context": {"positive": ["num"]}, "nearby": {"bad": -1}}]
    rows = {d["kind"]: d for d in extract(("num", "bad 12"), make_cfg(tmp_path, patterns))[1]}
    assert rows["below_min_score"]["decided_by"] == "min_score"
    assert rows["context_miss"]["decided_by"] == ""
    assert all(d["penalty_skipped"] is False for d in rows.values())


def test_decided_by_written_to_csv(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, OVERLAP_PATTERNS)
    write_report_csv(extract("abc12", cfg)[1])
    rows = read_report()
    assert list(rows[0])[-2:] == ["decided_by", "penalty_skipped"]
    assert [r["decided_by"] for r in rows if r["kind"] == "overlap"] == ["length"]


# ------------------------------------------------- load warning
PENALIZE_ONLY = [
    {"label": "phone_number", "priority": 1, "regex": "a", "context": {"penalize": {"acct": -1}}},
    {"label": "account_number", "priority": 2, "regex": "b", "context": {"penalize": {"tel": -1}}},
    {"label": "plain", "priority": 3, "regex": "c"},
]


def test_load_warning_when_penalize_is_unused(tmp_path: Path) -> None:
    with pytest.warns(UserWarning) as record:
        make_cfg(tmp_path, PENALIZE_ONLY, overlap_tiebreak=["score", "length"])
    assert len(record) == 1
    assert str(record[0].message) == (
        "context.penalize is set for phone_number, account_number but "
        "`context_penalty` is not in settings.overlap_tiebreak, so it is never used."
    )


@pytest.mark.parametrize(
    "patterns, order",
    [(PENALIZE_ONLY, ["context_penalty"]), (PENALIZE_ONLY[2:], ["score"])],
)
def test_no_load_warning_otherwise(tmp_path: Path, patterns: Any, order: list[str]) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_cfg(tmp_path, patterns, overlap_tiebreak=order)


def test_real_config_loads_without_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load_config(CONFIG_PATH)


def test_cli_shows_load_warning_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    path = tmp_path / "warn.yaml"
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["settings"]["overlap_tiebreak"] = ["score", "length", "priority"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert main(["--config", str(path), "--text", "call 555-123-4567"]) == 0
    captured = capsys.readouterr()
    assert captured.err.startswith("warning: context.penalize is set for invoice_number, ")
    assert json.loads(captured.out)[0]["label"] == "phone_number"


# ------------------------------------------------- skipped context_penalty note
SKIP_NOTE = "; context_penalty skipped: context selected no patterns"


@pytest.mark.parametrize(
    "order, context, skipped",
    [
        (CTX_PEN_ORDER, "bad", True),                            # all three hold
        (CTX_PEN_ORDER, "very bad", True),                       # phrase hit
        (["score", "length", "priority"], "bad", False),         # criterion not listed
        (CTX_PEN_ORDER, "alpha beta bad", False),                # gate on: it applied
        (CTX_PEN_ORDER, None, False),                            # no context at all
        (CTX_PEN_ORDER, "nothing relevant", False),              # gate off, no penalize words
    ],
)
def test_penalty_skipped_note(
    tmp_path: Path, order: list[str], context: str | None, skipped: bool
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # the "not in overlap_tiebreak" case warns
        cfg = make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=order)
    row = _overlap(extract({"text": "abcd", "context": context}, cfg)[1])
    assert row["reason"].endswith(SKIP_NOTE) is skipped
    assert row["penalty_skipped"] is skipped
    assert SKIP_NOTE[2:] not in row["reason"] or skipped


def test_penalty_skipped_note_for_either_candidate(tmp_path: Path) -> None:
    patterns = [
        {"label": "A", "priority": 1, "regex": "abcd", "context": {"positive": ["alpha"]}},
        {"label": "B", "priority": 2, "regex": "abcd", "context": {"penalize": {"bad": -1}}},
    ]
    cfg = make_cfg(tmp_path, patterns, overlap_tiebreak=CTX_PEN_ORDER)
    row = _overlap(extract(("bad", "abcd"), cfg)[1])
    assert (row["a_label"], row["penalty_skipped"]) == ("A", True)   # B (loser) has the hit
    assert row["decided_by"] == "priority"
    assert row["reason"] == "score tied; context_penalty tied; length tied; " \
        "priority 1 < 2 (lower number wins)" + SKIP_NOTE


def test_penalty_skipped_note_changes_no_result(tmp_path: Path) -> None:
    with_note = make_cfg(tmp_path, CTX_PEN_PATTERNS, overlap_tiebreak=CTX_PEN_ORDER)
    texts = ["abcd", "abcd abcd", "x abcd y"]
    for text in texts:
        m1, d1 = extract(("bad", text), with_note)
        m2, d2 = extract(("unrelated", text), with_note)   # same gate, no note
        assert [c.to_dict() for c in m1] == [c.to_dict() for c in m2]
        strip = lambda ds: [(d["kind"], d["a_label"], d["b_label"], d["decided_by"],
                             d["reason"].replace(SKIP_NOTE, "")) for d in ds[1:]]
        assert strip(d1) == strip(d2)
        assert any(d["penalty_skipped"] for d in d1) and not any(d["penalty_skipped"] for d in d2)


# ------------------------------------------------- summarize_overlaps
def _row(a: str, b: str, by: str, file: str = "f", skipped: bool = False,
         a_span: tuple[int, int] = (0, 4), b_span: tuple[int, int] = (0, 4),
         a_text: str = "x", b_text: str = "y", kind: str = "overlap") -> dict[str, Any]:
    row = dict.fromkeys(REPORT_FIELDS, "")
    row.update(file=file, kind=kind, a_label=a, b_label=b, decided_by=by,
               penalty_skipped=skipped, a_start=a_span[0], a_end=a_span[1],
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
    "pair", "overlaps", "by_score", "by_context_penalty", "by_length", "by_priority",
    "by_start_label", "not_by_score", "not_by_score_pct", "no_evidence",
    "penalty_skipped", "wins", "examples", "hint",
]


def test_summary_counts_and_wins() -> None:
    _report([
        _row("phone", "acct", "score"), _row("phone", "acct", "score"),
        _row("acct", "phone", "length"), _row("phone", "acct", "context_penalty"),
        _row("phone", "acct", "priority", skipped=True), _row("acct", "phone", "start_label"),
        _row("phone", "acct", "coexist", kind="coexist"),       # not an overlap row
        _row("n", "", "min_score", kind="below_min_score"),
    ])
    (row,) = summarize_overlaps()
    assert list(row) == SUMMARY_KEYS
    assert row["pair"] == "acct/phone"                            # sorted, joined by /
    assert (row["overlaps"], row["by_score"], row["by_context_penalty"], row["by_length"],
            row["by_priority"], row["by_start_label"]) == (6, 2, 1, 1, 1, 1)
    assert (row["not_by_score"], row["not_by_score_pct"]) == (4, 66.7)
    assert (row["no_evidence"], row["penalty_skipped"]) == (2, 1)
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


HINT_1 = "contexts mention penalize words but don't select these patterns; add context.positive words"
HINT_2 = "add nearby words; if inputs come with a context, add penalize words"
HINT_3 = "if both values are valid, add to coexist"
PARTIAL = {"a_span": (0, 4), "b_span": (2, 6)}


@pytest.mark.parametrize(
    "rows, hint",
    [
        ([_row("a", "b", "length", skipped=True)], HINT_1),
        ([_row("a", "b", "priority")], HINT_2),                                    # identical spans
        ([_row("a", "b", "priority", **PARTIAL)], f"{HINT_2}; {HINT_3}"),
        ([_row("a", "b", "priority", skipped=True, **PARTIAL)], f"{HINT_1}; {HINT_2}; {HINT_3}"),
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


@pytest.mark.parametrize(
    "extra", [["--text", "x"], ["--context", "x"], ["--report"], ["--error-log"]]
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

    import extract as mod
    from extract import (format_summary, open_report, reset_error_log, reset_report,
                         summarize_overlaps)

    cfg = load_config(str(CONFIG_PATH))
    assert isinstance(cfg, Config)

    # Library: the three input forms, (matches, decisions) shape.
    for inp in ("Call 555-123-4567", {"text": "Call 555-123-4567", "context": "call"},
                ("call", "Call 555-123-4567")):
        matches, decisions = extract(inp, cfg, source="id-1")
        assert [set(m.to_dict()) for m in matches] == [{"label", "text", "start", "end", "score"}]
        assert all(list(d) == list(REPORT_FIELDS) for d in decisions)
        assert {d["file"] for d in decisions} == {"id-1"}
    matches, _ = extract({"text": "some text", "context": "call"}, cfg)
    assert isinstance(matches, list)

    # The README loop: reset, stream with open_report, log context misses.
    reset_report()
    reset_error_log()
    items = [("id-1", "call", "Call 555-123-4567"), ("id-2", "tel", "Invoice INV-004521"),
             ("id-3", "", "Acct 5551234567 is overdue.")]
    with open_report() as write:
        for id, context, text in items:
            matches, decisions = extract((context, text), cfg, source=id)
            write(decisions)
            for d in decisions:
                if d["kind"] == "context_miss":
                    log_context_miss(id, context, text, d)
    with open("extract_report.csv", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == REPORT_FIELDS
        assert REPORT_FIELDS[-2:] == ("decided_by", "penalty_skipped")
        assert {r["file"] for r in reader} == {"id-1", "id-2", "id-3"}
    (miss,) = read_error_log()
    assert list(miss) == list(mod.ERROR_LOG_FIELDS) and miss["file"] == "id-2"
    write_report_csv(extract("abc", cfg)[1])            # appends, no second header

    # Summary from the library and its table.
    rows = summarize_overlaps()
    assert rows and all(list(r) == list(mod.SUMMARY_FIELDS) for r in rows)
    assert rows[0]["pair"] == "account_number/phone_number"
    assert format_summary(rows).splitlines()[0].split() == list(mod.SUMMARY_FIELDS)

    # CLI, as in the README examples.
    config = str(CONFIG_PATH)
    assert main(["--config", config, "--report", "--text",
                 "Invoice INV-004521 dated 03/14/2026, amount due $1,250.00"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {m["label"] for m in out} == {"invoice_number", "date", "amount"}
    assert all(set(m) == {"label", "text", "start", "end", "score"} for m in out)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"call 555-123-4567")))
    assert main(["--config", config, "--context", "phone"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["label"] == "phone_number"
    assert main(["--summarize"]) == 0
    assert capsys.readouterr().out.split()[:2] == ["pair", "overlaps"]


# ------------------------------------------------- YAML booleans/null as keywords
@pytest.mark.parametrize(
    "keys, where, shown",
    [
        ({"nearby": {True: 1}}, "nearby", "True"),                     # bare `on:` / `yes:`
        ({"context": {"penalize": {False: -1}}}, "context.penalize", "False"),  # bare `no:`
        ({"context": {"positive": [True]}}, "context.positive", "True"),
    ],
)
def test_non_string_keyword_is_rejected(
    tmp_path: Path, keys: dict[str, Any], where: str, shown: str
) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])
    assert str(exc.value) == (
        f"pattern 'p': {where} keyword {shown} must be a string "
        '(quote it in the YAML, e.g. "on")'
    )


def test_bare_yaml_on_in_a_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bare.yaml"
    raw = CONFIG_PATH.read_text(encoding="utf-8").replace('"on": 1', "on: 1")
    assert raw != CONFIG_PATH.read_text(encoding="utf-8")
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="pattern 'date': nearby keyword True must be a string"):
        load_config(path)


# ------------------------------------------------- same words, different spelling
@pytest.mark.parametrize(
    "keys, where, key, other",
    [
        ({"nearby": {"amount due": 2, "amount-due": 2}}, "nearby", "amount-due", "amount due"),
        ({"context": {"positive": ["don't", "dont"]}}, "context.positive", "dont", "don't"),
        ({"context": {"penalize": {"e mail": -1, "e-mail": -1}}}, "context.penalize",
         "e-mail", "e mail"),
    ],
)
def test_duplicate_after_word_reduction_is_rejected(
    tmp_path: Path, keys: dict[str, Any], where: str, key: str, other: str
) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])
    assert str(exc.value) == (
        f"pattern 'p': {where} keyword '{key}' is listed more than once in this "
        f"pattern (same words as '{other}')"
    )


def test_positive_and_penalize_compare_reduced_words(tmp_path: Path) -> None:
    keys = {"context": {"positive": ["don't"], "penalize": {"dont": -1}}}
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])
    assert str(exc.value) == (
        "pattern 'p': keyword 'dont' is in both context.positive "
        "and context.penalize (same words as 'don't')"
    )


# ------------------------------------------------- summary: bounded examples, rounding
def test_summary_keeps_at_most_three_examples_per_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    import extract as mod

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
@pytest.mark.parametrize(
    "keys, where",
    [
        ({"context": {"positive": ["tel", None]}}, "context.positive"),   # bare `-` item
        ({"context": {"penalize": {None: -1}}}, "context.penalize"),     # blank / `~` key
        ({"nearby": {None: 1}}, "nearby"),
    ],
)
def test_blank_keyword_entry_is_rejected(
    tmp_path: Path, keys: dict[str, Any], where: str
) -> None:
    with pytest.raises(ValueError) as exc:
        make_cfg(tmp_path, _one(**keys), overlap_tiebreak=["context_penalty"])
    assert str(exc.value) == f"pattern 'p': {where} has an empty entry; remove it"


def test_blank_list_item_in_a_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "blank.yaml"
    raw = CONFIG_PATH.read_text(encoding="utf-8").replace(
        "positive: [phone, tel, call, mobile, fax]",
        "positive:\n        - phone\n        -")
    assert raw != CONFIG_PATH.read_text(encoding="utf-8")
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="pattern 'phone_number': context.positive has an empty entry"):
        load_config(path)


@pytest.mark.parametrize(
    "keys",
    [{}, {"context": None}, {"context": {}}, {"context": {"positive": None, "penalize": None}},
     {"context": {"positive": [], "penalize": {}}, "nearby": None}, {"nearby": {}}],
)
def test_missing_or_blank_keyword_keys_load_as_empty(
    tmp_path: Path, keys: dict[str, Any]
) -> None:
    cfg = make_cfg(tmp_path, [{"label": "n", "priority": 1, "regex": r"\d+", **keys}],
                   overlap_tiebreak=["context_penalty"])
    (p,) = cfg.patterns
    assert (p.positive, p.penalize, p.nearby) == ([], {}, {})
    for inp in ("tel 12", ("tel", "12")):
        (m,) = matches_of(inp, cfg)
        assert (m.text, m.score) == ("12", 0)
