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

- `--format json` (default) prints `{"matches": [...], "overlaps": [...]}`.
  Despite its name, `overlaps` is the **whole decision log** (context,
  `below_min_score`, overlap and coexist entries), so filter on `kind`.
- `--format csv` prints matches on stdout with the columns
  `file,label,text,start,end,score`. The decision log is printed to **stderr**
  as one JSON object per line, which keeps stdout clean CSV. Redirect with
  `2>/dev/null` if you don't want it; it is also in `extract_report.csv`.
- `--context TEXT` selects which patterns run (see [Context](#context)). The
  same context is applied to **every** FILE in the run.
- Exit codes: `0` success; `2` for a config error or an unreadable input file
  (`error: ...` on stderr, no matches printed).

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
   Zero-length matches (e.g. from `\d*`) are skipped: they have no text to
   extract, and nothing is logged for them.
5. **Score**: take up to `context_window_words` words before and/or after the
   match (`context_direction`), stopping early at any `stop_at` character, and
   add the weight of each keyword found. With
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

## Keywords

Context words are ASCII letters only. Apostrophes join a word (`don't` ->
`dont`); spaces, hyphens, digits and everything else separate words, so the
keyword `e-mail` means the two words `e` `mail`. To avoid keywords that would
silently match the wrong words, `load_config` fails with a message naming the
pattern and keyword when:

- a keyword has a digit or an accented letter (`w2`, `1099`, `café`) or no letters;
- the same keyword appears twice in a pattern (including once under `positive`
  and once under `negative`);
- a weight has the wrong sign (`positive` must be >= 0, `negative` <= 0).

## Config

`extraction_config.yaml` is the source of truth. Keys:

| Key | Purpose |
| --- | --- |
| `settings` | `context_window_words`, `context_direction` (`before`/`after`/`both`, default `both`), `stop_at` (characters that end the context scan, default `""`), `distance_decay`, `min_score`, `case_sensitive`, `overlap_tiebreak` |
| `normalize` | `collapse_whitespace`, `unicode_dashes_to_hyphen`, `smart_quotes_to_ascii` |
| `patterns` | `label`, `priority`, `regex`, and `context.positive` / `context.negative` keyword weights |
| `coexist` | label pairs that may both be kept when they overlap |
| `tests` | cases `{text, expect: [{label, text}], context?, name?}` run by pytest |

## Output files

Both are written to the working directory and overwritten once per CLI run
(library calls append). They are reset at the start of every run, so a run that
fails (bad config, missing input file) leaves them empty rather than stale.
Both are gitignored.

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
