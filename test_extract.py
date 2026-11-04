"""Tests for extract.py. Config `tests` section is the source of truth for the
end-to-end cases; the rest are focused unit tests."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from extract import (
    Config,
    extract,
    extract_with_report,
    load_config,
    main,
)
from words import next_words, prev_words

CONFIG_PATH = Path(__file__).parent / "extraction_config.yaml"
REAL_CFG = load_config(CONFIG_PATH)


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
@pytest.mark.parametrize(
    "case", REAL_CFG.tests, ids=[c["text"][:40] for c in REAL_CFG.tests]
)
def test_config_cases(case: dict[str, Any]) -> None:
    expected = Counter((e["label"], e["text"]) for e in case["expect"])
    assert pairs(extract(case["text"], REAL_CFG)) == expected


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


def test_context_restricts_to_matched_patterns() -> None:
    result = extract({"text": CTX_TEXT, "context": "2026-03-14"}, REAL_CFG)
    assert pairs(result) == Counter(
        {("date", "03/14/2026"): 1, ("date", "2026-03-14"): 1}
    )


def test_context_no_hits_falls_back_to_all() -> None:
    plain = pairs(extract(CTX_TEXT, REAL_CFG))
    _, decisions = extract_with_report(("nothing relevant here", CTX_TEXT), REAL_CFG)
    assert pairs(extract(("nothing relevant here", CTX_TEXT), REAL_CFG)) == plain
    assert "no context hits" in decisions[0].reason


def test_context_hit_without_text_match_returns_nothing() -> None:
    # Context matches phone_number only; the text has no phone number.
    assert extract(("555-123-4567", "Invoice INV-004521"), REAL_CFG) == []


def test_context_none_or_empty_matches_plain() -> None:
    plain = pairs(extract(CTX_TEXT, REAL_CFG))
    assert pairs(extract({"text": CTX_TEXT, "context": None}, REAL_CFG)) == plain
    assert pairs(extract({"text": CTX_TEXT}, REAL_CFG)) == plain
    assert pairs(extract((None, CTX_TEXT), REAL_CFG)) == plain
    assert pairs(extract(("   ", CTX_TEXT), REAL_CFG)) == plain


def test_context_is_normalized() -> None:
    # Context uses an en dash; normalization turns it into a hyphen so the
    # date pattern still hits.
    result = extract(("2026–03–14", CTX_TEXT), REAL_CFG)
    assert {m.label for m in result} == {"date"}


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
