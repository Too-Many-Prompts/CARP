# Regex extractor

A command-line Python tool (and importable module) that extracts regex matches
from text, scores each match using nearby context words, and resolves
overlapping matches. Every rule lives in `extraction_config.yaml`.

## Setup

Python 3.10+.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # PyYAML, pytest
```

## Usage

### CLI

```bash
.venv/bin/python extract.py --config extraction_config.yaml FILE... \
    [--format json|csv] [--context TEXT]
```

- `--format json` (default) prints `{"matches": [...], "overlaps": [...]}`;
  `overlaps` is the decision log.
- `--format csv` prints matches with the columns
  `file,label,text,start,end,score`.
- `--context TEXT` selects which patterns run (see [Context](#context)).

Example:

```bash
printf 'Invoice INV-004521 dated 03/14/2026, amount due $1,250.00\n' > sample.txt
.venv/bin/python extract.py --config extraction_config.yaml --format csv sample.txt
```

### Library

```python
from extract import load_config, extract, extract_with_report

cfg = load_config("extraction_config.yaml")

extract("some text", cfg)
extract({"text": "some text", "context": "call"}, cfg)
extract(("call", "some text"), cfg)                    # (context, text)

matches, decisions = extract_with_report(("call", "some text"), cfg)
```

`extract()` returns the matches that survived scoring and overlap resolution.
Each has `label`, `text`, `start`, `end` and `score`. Offsets refer to the
**normalized** text.

## How it works

1. **Load**: read the YAML and compile each regex once (IGNORECASE unless
   `case_sensitive`). An invalid regex fails with a message naming its label.
2. **Normalize** the text (and context): collapse whitespace, unicode dashes to
   `-`, smart quotes to ASCII, each only if enabled.
3. **Select patterns** from the context (below).
4. **Find candidates**: each pattern runs separately with `finditer`. Patterns
   are never combined into one alternation, so overlaps stay visible.
5. **Score**: take up to `context_window_words` words before and/or after the
   match (`context_direction`), and add the weight of each keyword found. With
   `distance_decay`, a keyword's weight is divided by its word distance
   (nearest word = 1). Only the nearest occurrence of each keyword counts.
6. **Drop** candidates with `score < min_score`.
7. **Resolve overlaps**, best first, using `overlap_tiebreak` (in order: score,
   length, priority). A pair of labels listed in `coexist` keeps both.
   Identical spans count as overlapping.

### Context

An optional context string restricts which patterns run on the text. It is
matched, as whole words or phrases and case-insensitively, against each
pattern's `context.positive` keywords. Only patterns with a hit run on the text.
The pattern regexes are never run on the context.

- No context, or no keyword hits: all patterns run.
- Negative keywords never select a pattern.
- If the context selected patterns but nothing was extracted, the case is not
  dropped silently. It is appended to `errorlog.csv`.

## Config

`extraction_config.yaml` is the source of truth. Keys:

| Key | Purpose |
| --- | --- |
| `settings` | `context_window_words`, `context_direction` (`before`/`after`/`both`, default `both`), `distance_decay`, `min_score`, `case_sensitive`, `overlap_tiebreak` |
| `normalize` | `collapse_whitespace`, `unicode_dashes_to_hyphen`, `smart_quotes_to_ascii` |
| `patterns` | `label`, `priority`, `regex`, and `context.positive` / `context.negative` keyword weights |
| `coexist` | label pairs that may both be kept when they overlap |
| `tests` | cases `{text, expect: [{label, text}], context?, name?}` run by pytest |

## Output files

Both are written to the working directory and overwritten once per CLI run
(library calls append). Both are gitignored.

- `errorlog.csv`: `timestamp, file, context, text, context_patterns, reason`,
  one row per context miss.
- `extract_report.csv`: the decision log, one row per decision. Columns:
  `file, kind, outcome, reason, patterns` plus `a_*` and `b_*` candidate columns
  (`label, text, start, end, score`).
  - `kind` is `context`, `context_miss`, `below_min_score`, `overlap` or `coexist`.
  - In an `overlap` row, `a` is the winner and `b` the dropped candidate.
  - Library callers write it with `write_report_csv(decisions, source)`.

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

`test_extract.py` runs the `tests` section of the config and unit-tests coexist
and non-coexist overlaps, tie-breaks, multi-word keywords, `context_direction`,
invalid regexes, context selection, the error log, the report CSV and the CLI.

## Files

| File | Role |
| --- | --- |
| `extract.py` | the extractor and CLI |
| `words.py` | `prev_words()` / `next_words()` word lookups around a match |
| `extraction_config.yaml` | settings, patterns, coexist pairs, test cases |
| `test_extract.py` | pytest suite |
| `CLAUDE.md` | project instructions for Claude Code |
