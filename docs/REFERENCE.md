# CARP reference

The full rules. For an overview, see the [README](../README.md).

## Usage

### CLI

Scans one text per run. For many texts, or a CSV of them, use the library.

```bash
.venv/bin/python extract.py --config extraction_config.yaml \
    [--text TEXT] [--context TEXT] [--report] [--error-log]
.venv/bin/python extract.py --summarize [PATH]
```

- `--text TEXT` is the text to scan. If omitted, the text is read from stdin
  (UTF-8, `errors="replace"`).
- `--context TEXT` selects which patterns run (see [Context](#context)).
- Prints the matches as a JSON list of `{label, text, start, end, score}`.
- Writes nothing else unless asked (both files are overwritten each time the
  flag is given):
  - `--report` writes the decision log to `./extract_report.csv`.
  - `--error-log` writes `./errorlog.csv` (raw context and text of a context miss).
- `--summarize [PATH]` prints the [overlap summary](#overlap-summary) of a
  report (default `./extract_report.csv`) and exits. It needs no `--config`
  and cannot be combined with `--text`, `--context`, `--report` or `--error-log`.
- Config warnings (e.g. [unused `penalize` words](#context_penalty)) go to
  stderr as `warning: ...` and do not change the exit code.
- Exit codes: `0` success; `2` for a config or report error (`error: ...` on
  stderr, nothing printed) or a usage error.

Example:

```bash
.venv/bin/python extract.py --config extraction_config.yaml --report \
    --text 'Invoice INV-004521 dated 03/14/2026, amount due $1,250.00'
echo 'call 555-123-4567' | .venv/bin/python extract.py --config extraction_config.yaml --context phone
.venv/bin/python extract.py --summarize
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

Then summarize the report:

```python
from extract import summarize_overlaps, format_summary   # defined in summarize.py

rows = summarize_overlaps()            # list of dicts, worst pairs first
print(format_summary(rows))            # the same table --summarize prints
```

## How it works

1. **Load**: read the YAML and compile each regex once (IGNORECASE unless
   `case_sensitive`). An invalid regex fails with a message naming its label.
   Bad keywords and old-format keys fail too (see [Keywords](#keywords) and
   [Migrating](#migrating-from-the-old-context-block)).
2. **Normalize** the text (and context): collapse whitespace, unicode dashes to
   `-`, smart quotes to ASCII, each only if enabled.
3. **Select patterns** from the context string with `context.positive` (below).
4. **Find candidates**: each pattern runs separately with `finditer`. Patterns
   are never combined into one alternation, so overlaps stay visible.
   Zero-length matches (e.g. from `\d*`) are skipped: they have no text to
   extract, and nothing is logged for them.
5. **Score** with `nearby`: take up to `context_window_words` words before
   and/or after the match (`context_direction`), stopping early at any
   `stop_at` character, and add the weight of each `nearby` keyword found.
   With `distance_decay`, a keyword's weight is divided by its word distance
   (nearest word = 1). Only the nearest occurrence of each keyword counts.
   The context string is never read here.
6. **Drop** candidates with `score < min_score`.
7. **Resolve overlaps**, best first, using `overlap_tiebreak` (in order; the
   example is score, context_penalty, length, priority). If every listed
   criterion ties, or the list is empty, the earlier start and then the label
   decide. A pair of labels listed in `coexist` keeps both. Identical spans
   count as overlapping. Every decision records what decided it (`decided_by`).

### Keywords: `keywords` (the usual way)

Write each word once, with a signed weight:

```yaml
- label: phone_number
  keywords: {phone: 3, call: 2, mobile: 2, acct: -3, order: -1}
```

`load_config` expands every word by its sign:

| weight | the word... |
| --- | --- |
| `> 0` | selects the pattern from the context string (`context.positive`) **and** raises the score near a match in the text (`nearby`, same weight) |
| `< 0` | lowers the score near a match (`nearby`, same weight) **and** counts against the pattern in the `context_penalty` tie-break (`context.penalize`, same weight) |
| `0` | load error: give it a sign or remove it |

This covers most patterns. When a word needs different roles (say, it should
select the pattern but not score), use the explicit keys below for that word.
A pattern may mix `keywords` and the explicit keys, but a word may not be in
`keywords` **and** in an explicit key of the same pattern (load error), so
there are no precedence rules.

### Explicit keys: `context` and `nearby`

`keywords` expands into these. Each kind reads only its own input:

```yaml
- label: phone_number
  context:                                # read from the context string only
    positive: [phone, call, mobile]       #   selects the pattern
    penalize: {account: -3, acct: -3}     #   context_penalty tie-break
  nearby: {phone: 3, call: 2, order: -1}  # read from the text window only
```

- `nearby` is a map of word -> signed weight, scored in the text around each
  match (step 5). It never reads the context string. Without `nearby`, every
  score is 0; it does **not** fall back to `context.positive`.
- `context.positive` is a list of words with no weights. It never reads the text.
- `context.penalize` is a map of word -> weight `<= 0`. It is only used by the
  `context_penalty` tie-break: it never lowers the score and never deselects
  a pattern.

The same word may appear in both `context` and `nearby` (it often should).

### Context

An optional context string restricts which patterns run on the text. It is
matched, as whole words or phrases and case-insensitively, against each
pattern's `context.positive` words. Only patterns with a hit run on the text.
The pattern regexes are never run on the context.

- No context, or no hits: all patterns run.
- `penalize` and `nearby` words never select a pattern.
- If the context selected patterns but nothing was extracted, the case is not
  dropped silently. `extract()` returns it as a `context_miss` decision.

### `context_penalty`

An `overlap_tiebreak` criterion. For each candidate it sums the weights of its
pattern's `context.penalize` words found in the context string; the penalty
closer to 0 wins (`context penalty 0 > -3` in the `reason`).

- **When it applies:** only when the context selected the patterns (the
  `context` decision is `patterns_restricted`). With no context, or a context
  that selected no patterns, it always ties.
- **It scores like `nearby`:** the same matcher, word reduction, whole-word
  case-insensitive phrase matching and normalization; each keyword counts once.
  The context has no match position, so `context_window_words`,
  `context_direction`, `stop_at` and `distance_decay` do not apply: the whole
  string is read and nothing is divided by distance.
- **Load warning:** if any pattern has explicit `context.penalize` words but
  `context_penalty` is not in `overlap_tiebreak`, `load_config` warns
  (`UserWarning`) naming the patterns, because those words are never used.
  The config still loads. Negative `keywords` words don't count: they also
  score in `nearby`, so they are used either way.
- **Skipped note:** if `context_penalty` is listed, a context was given but
  selected no patterns, and that context contains `penalize` words of either
  overlapping candidate, the overlap's `reason` ends with
  `; context_penalty skipped: context selected no patterns` and its
  `penalty_skipped` column is `True`. The result is the same as without the note.

## Keywords

Keywords are ASCII letters only. Apostrophes join a word (`don't` -> `dont`);
spaces, hyphens, digits and everything else separate words, so the keyword
`e-mail` means the two words `e` `mail`. The context string is split into
words the same way as the text. To avoid keywords that would silently match
the wrong words, `load_config` fails with a message naming the pattern, key
and keyword when:

- a keyword has a digit or an accented letter (`w2`, `1099`, `café`) or no letters;
- the same keyword appears twice under one key of a pattern
  (`keywords`, `context.positive`, `context.penalize` or `nearby`);
- a word is in `keywords` and also in an explicit key, or has weight 0 in `keywords`;
- a word is in both `context.positive` and `context.penalize` of a pattern;
- a `penalize` weight is above 0, or a weight is not a number.

Quote YAML words that YAML would read as something else: unquoted, `on`,
`yes`, `true` load as `True`, and `off`, `no`, `false` as `False`.
`load_config` rejects such keywords ("must be a string (quote it in the
YAML ...)") instead of silently turning them into "true" or "false". Write
`"on": 1`, not `on: 1`. A blank entry (a bare `-` list item, or a `null` /
`~` key) fails with "has an empty entry; remove it".

Leaving out `keywords`, `context`, `context.positive`, `context.penalize` or `nearby`, or
leaving them blank (`positive:`, `penalize: {}`), is fine: the pattern simply
has no keywords of that kind. A word without a weight (`nearby: {tel: }`) is
an error.

## Config

`extraction_config.yaml` is the source of truth. Keys:

| Key | Purpose |
| --- | --- |
| `settings` | `context_window_words`, `context_direction` (`before`/`after`/`both`, default `both`), `stop_at` (characters that end the window scan, default `""`), `distance_decay`, `min_score`, `case_sensitive`, `overlap_tiebreak` (any of `score`, `context_penalty`, `length`, `priority`, in order; may be empty) |
| `normalize` | `collapse_whitespace`, `unicode_dashes_to_hyphen`, `smart_quotes_to_ascii` |
| `patterns` | `label`, `priority`, `regex`, `keywords` (word -> signed weight; the shorthand), and/or the explicit `context.positive` (word list), `context.penalize` (word -> weight <= 0), `nearby` (word -> signed weight) |
| `coexist` | label pairs that may both be kept when they overlap |
| `tests` | cases `{text, expect: [{label, text}], context?, name?}` run by pytest |

### Migrating from the old `context:` block

Old configs do not load; `load_config` says what to change:

| Old | New |
| --- | --- |
| `context.positive: {word: weight}` | `context.positive: [word, ...]` to select, and the weights under `nearby` to score |
| `context.negative: {word: weight}` | `context.penalize` for the tie-break, and the weights under `nearby` if they should still lower the text score |
| `context_negative` in `overlap_tiebreak` | `context_penalty` |

To keep the old results exactly, merge both old maps into one `keywords`
map: `positive: {phone: 3}` plus `negative: {acct: -3}` becomes
`keywords: {phone: 3, acct: -3}`. That is exactly what the old format meant.

## Output files

`extract()` writes nothing. Both files are written to the working directory
by the CLI only when asked (`--report`, `--error-log`), or by the library
helpers below. Both are gitignored.

- `errorlog.csv`: `timestamp, file, context, text, context_patterns, reason`,
  one row per context miss. Unlike the report row it keeps the raw
  (un-normalized) context and the full text.
- `extract_report.csv`: the decision log, one row per decision. Columns:
  `file, kind, outcome, reason, patterns`, the `a_*` and `b_*` candidate columns
  (`label, text, start, end, score`), then `decided_by, penalty_skipped`.
  - `kind` is `context`, `context_miss`, `below_min_score`, `overlap` or `coexist`.
  - In an `overlap` row, `a` is the winner and `b` the dropped candidate.
  - `decided_by` is machine-readable: for `overlap`, the criterion that decided
    (`score`, `context_penalty`, `length`, `priority`, or `start_label` when
    everything tied); `coexist` for `coexist`; `min_score` for
    `below_min_score`; empty for `context` and `context_miss`.
  - `penalty_skipped` is `True` only in overlap rows with the
    [skipped note](#context_penalty).
- Library helpers (all optional, none called by `extract()`): `write_report_csv(decisions)`
  appends rows; `reset_report()` / `reset_error_log()` start a fresh file;
  `log_context_miss(source, context, text, decision)` appends an `errorlog.csv`
  row for a `context_miss` decision; `open_report()` opens the report once for
  many calls (see the loop above).

> **Memory:** do not collect decisions from many calls in a list; write them as
> you go with `open_report()`.

## Overlap summary

`summarize_overlaps(path="extract_report.csv")` (CLI: `--summarize`) reads
the report's `overlap` rows and returns one row per label pair:
`pair, overlaps, by_score, by_context_penalty, by_length, by_priority,
by_start_label, not_by_score, not_by_score_pct, no_evidence, penalty_skipped,
wins, examples, hint`. A report without a `decided_by` column is rejected:
regenerate it with `--report`.

How to read it:

- Start at the top. `no_evidence` (`by_priority + by_start_label`) counts
  overlaps where nothing in the input decided, only the fallback order did.
- `not_by_score` also counts overlaps settled by `length` or
  `context_penalty`, which are often fine.
- `hint` says what to try, and `examples` gives up to three inputs to look at
  (`<file>: "<a_text>" vs "<b_text>"`, fallback-decided ones first).
  - "add context.positive words": contexts mention `penalize` words but select
    no pattern, so `context_penalty` was skipped.
  - "add nearby words ...": the fallback order decided.
  - "add to coexist": most unsettled overlaps are partial (different spans),
    which often means two distinct values that may both belong in the output.
  - `ok`: nothing to fix.

### Tuning workflow

1. Run your inputs with the report on (`--report`, or `open_report()` in a loop).
2. `extract.py --summarize`.
3. Fix the pairs at the top (`nearby` words, `penalize` words, `context.positive`
   words or `coexist`, as the hint says), then repeat.

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

`test_extract.py` runs the `tests` section of the config and unit-tests coexist
and non-coexist overlaps, tie-breaks (including `context_penalty`, its context
gate and an empty list), `nearby`/`context_penalty` parity, multi-word keywords,
`context_direction`, keyword validation and old-format errors, the `keywords`
shorthand (expansion, conflicts, and that the example config loads exactly as
its explicit form did), the load warning,
the skipped note, `decided_by`, context selection, the error log, the report CSV,
the overlap summary, the CLI, and the public API as shown in the README and this reference.
