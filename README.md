# CARP: Context-Aware Regex Parser

```text
         ,-/|/|/|\-.
  |\   ,'/|/|/|/|/|`-.
  | `-'  ((((((((((   `.
  |  _   ))))))))))  o  \
 <  / `. ((((((((((    _.'~
  | `-.'`-.._____..-'
  |/   `-.
```

CARP is a command-line Python tool (and importable module) that extracts regex matches
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

Scans one text per run. For many texts, or a CSV of them, use the library.

```bash
.venv/bin/python extract.py --config extraction_config.yaml \
    [--text TEXT] [--context TEXT] [--report] [--error-log]
```

- `--text TEXT` is the text to scan. If omitted, the text is read from stdin
  (UTF-8, `errors="replace"`).
- `--context TEXT` selects which patterns run (see [Context](#context)).
- Prints the matches as a JSON list of `{label, text, start, end, score}`.
- Writes nothing else unless asked (both files are overwritten each time the
  flag is given):
  - `--report` writes the decision log to `./extract_report.csv`.
  - `--error-log` writes `./errorlog.csv` (raw context and text of a context miss).
- Exit codes: `0` success; `2` for a config error (`error: ...` on stderr,
  nothing printed).

Example:

```bash
.venv/bin/python extract.py --config extraction_config.yaml --report \
    --text 'Invoice INV-004521 dated 03/14/2026, amount due $1,250.00'
echo 'call 555-123-4567' | .venv/bin/python extract.py --config extraction_config.yaml --context phone
```

### Library

```python
from extract import load_config, extract

cfg = load_config("extraction_config.yaml")

matches, decisions = extract("some text", cfg)
matches, _ = extract({"text": "some text", "context": "call"}, cfg)   # ignore the log
matches, decisions = extract(("call", "some text"), cfg, source="id-1")  # (context, text)
```

`extract()` returns `(matches, decisions)` and **writes no files**.
`matches` are the candidates that survived scoring and overlap resolution; each
has `label`, `text`, `start`, `end` and `score`. Offsets refer to the
**normalized** text. `decisions` is the flat decision log (see
[Output files](#output-files)); ignore it, keep it, or write it with the helpers
below. `source` fills its `file` column.

Looping over many inputs, writing the report as you go:

```python
from extract import load_config, extract, open_report, reset_report, reset_error_log, log_context_miss

cfg = load_config("extraction_config.yaml")
reset_report()
reset_error_log()                      # only if you use log_context_miss
items = [("id-1", "call", "Call 555-123-4567"), ("id-2", "", "Invoice INV-004521")]

with open_report() as write:           # opens the file once, holds nothing in memory
    for id, context, text in items:
        matches, decisions = extract((context, text), cfg, source=id)
        write(decisions)
        for d in decisions:            # optional: deep troubleshooting
            if d["kind"] == "context_miss":
                log_context_miss(id, context, text, d)
        # use `matches` here
```

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
7. **Resolve overlaps**, best first, using `overlap_tiebreak` (in order; the
   example is score, length, priority). An optional `context_negative`
   criterion prefers the pattern with fewer/lighter negative keyword hits in
   the passed context; it only applies when that context selected the
   patterns, otherwise it ties. If every listed criterion ties, or the list is
   empty, the earlier start and then the label decide. A pair of labels
   listed in `coexist` keeps both. Identical spans count as overlapping.

### Context

An optional context string restricts which patterns run on the text. It is
matched, as whole words or phrases and case-insensitively, against each
pattern's `context.positive` keywords. Only patterns with a hit run on the text.
The pattern regexes are never run on the context.

- No context, or no keyword hits: all patterns run.
- Negative keywords never select a pattern. They only matter as the opt-in
  `context_negative` tie-break, and only when the context selected patterns.
- If the context selected patterns but nothing was extracted, the case is not
  dropped silently. `extract()` returns it as a `context_miss` decision.

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
| `settings` | `context_window_words`, `context_direction` (`before`/`after`/`both`, default `both`), `stop_at` (characters that end the context scan, default `""`), `distance_decay`, `min_score`, `case_sensitive`, `overlap_tiebreak` (any of `score`, `context_negative`, `length`, `priority`, in order; may be empty) |
| `normalize` | `collapse_whitespace`, `unicode_dashes_to_hyphen`, `smart_quotes_to_ascii` |
| `patterns` | `label`, `priority`, `regex`, and `context.positive` / `context.negative` keyword weights |
| `coexist` | label pairs that may both be kept when they overlap |
| `tests` | cases `{text, expect: [{label, text}], context?, name?}` run by pytest |

## Output files

`extract()` writes nothing. Both files are written to the working directory
by the CLI only when asked (`--report`, `--error-log`), or by the library
helpers below. Both are gitignored.

- `errorlog.csv`: `timestamp, file, context, text, context_patterns, reason`,
  one row per context miss. Unlike the report row it keeps the raw
  (un-normalized) context and the full text.
- `extract_report.csv`: the decision log, one row per decision. Columns:
  `file, kind, outcome, reason, patterns` plus `a_*` and `b_*` candidate columns
  (`label, text, start, end, score`).
  - `kind` is `context`, `context_miss`, `below_min_score`, `overlap` or `coexist`.
  - In an `overlap` row, `a` is the winner and `b` the dropped candidate.
- Library helpers (all optional, none called by `extract()`): `write_report_csv(decisions)`
  appends rows; `reset_report()` / `reset_error_log()` start a fresh file;
  `log_context_miss(source, context, text, decision)` appends an `errorlog.csv`
  row for a `context_miss` decision; `open_report()` opens the report once for
  many calls (see the loop above).

> **Memory:** do not collect decisions from many calls in a list; write them as
> you go with `open_report()`.

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

`test_extract.py` runs the `tests` section of the config and unit-tests coexist
and non-coexist overlaps, tie-breaks (including `context_negative` and an empty list), multi-word keywords, `context_direction`,
invalid regexes, context selection, the error log, the report CSV and the CLI.

## Files

| File | Role |
| --- | --- |
| `extract.py` | the extractor and CLI |
| `words.py` | `prev_words()` / `next_words()` word lookups around a match |
| `extraction_config.yaml` | settings, patterns, coexist pairs, test cases |
| `test_extract.py` | pytest suite |
| `CLAUDE.md` | project instructions for Claude Code |
