# CARP reference

The full rules. For an overview, see the [README](../README.md).

## Usage

### CLI

Scans one text per run. For many texts, or a CSV of them, use the library.

```bash
.venv/bin/python extract.py --config extraction_config.yaml \
    [--text TEXT] [--context TEXT] [--region CODE] [--report]
.venv/bin/python extract.py --summarize [PATH]
```

- `--text TEXT` is the text to scan. If omitted, the text is read from stdin
  (UTF-8, `errors="replace"`).
- `--context TEXT` is an optional context string. Its words add to the score
  of every pattern that lists them (see [Scoring](#scoring)).
- `--region CODE` (e.g. `GB`) is the caller's region for
  [phone patterns](#phone-numbers). An unknown code is an error (exit 2).
- Prints the matches as a JSON list of `{label, text, start, end, score}`.
  Matches of a pattern with `validate` also have `extra` (see
  [Phone numbers](#phone-numbers)).
- Writes nothing else unless asked: `--report` writes the decision log to
  `./extract_report.csv` (overwritten each time).
- `--summarize [PATH]` prints the [overlap and miss summaries](#summaries) of
  a report (default `./extract_report.csv`) and exits. It needs no `--config`
  and cannot be combined with `--text`, `--context`, `--region` or `--report`.
- Config warnings go to stderr as `warning: ...` and do not change the exit code.
- Exit codes: `0` success; `2` for a config or report error (`error: ...` on
  stderr, nothing printed) or a usage error.

Example:

```bash
.venv/bin/python extract.py --config extraction_config.yaml --report \
    --text 'Invoice INV-004521 dated 03/14/2026, amount due $1,250.00'
echo 'call 212-555-0123' | .venv/bin/python extract.py --config extraction_config.yaml --context phone
.venv/bin/python extract.py --summarize
```

### Library

```python
from extract import load_config, extract

cfg = load_config("extraction_config.yaml")

matches, decisions = extract("some text", cfg)
matches, _ = extract({"text": "some text", "context": "call"}, cfg)   # ignore the log
matches, decisions = extract(("call", "some text"), cfg, source="id-1")  # (context, text)
matches, _ = extract("office 020 7946 0018", cfg, region="GB")         # caller region for phones
```

`extract()` returns `(matches, decisions)` and **writes no files**.
`matches` are the candidates that survived scoring and overlap resolution; each
has `label`, `text`, `start`, `end`, `score`, and the breakdown `text_score`
and `context_score` (`score` is their sum), and `extra` (a dict for matches
of a pattern with `validate`, else `None`). Offsets refer to the
**normalized** text. `decisions` is the flat decision log (see
[Output files](#output-files)); ignore it, keep it, or write it with the helpers
below. `source` fills its `file` column.

Looping over many inputs, writing the report as you go:

```python
from extract import load_config, extract, open_report, reset_report

cfg = load_config("extraction_config.yaml")
reset_report()
items = [("id-1", "call", "Call 212-555-0123"), ("id-2", "", "Invoice INV-004521")]

with open_report() as write:           # opens the file once, holds nothing in memory
    for id, context, text in items:
        matches, decisions = extract((context, text), cfg, source=id)
        write(decisions)
        # use `matches` here
```

Then summarize the report:

```python
from extract import (summarize_overlaps, format_summary, summarize_misses, format_misses,
                     summarize_validation, format_validation)

print(format_summary(summarize_overlaps()))       # the overlap table --summarize prints
print(format_misses(summarize_misses()))          # the miss table printed under it
print(format_validation(summarize_validation()))  # phone validation, printed last
```

## How it works

1. **Load**: read the YAML and compile each regex once (IGNORECASE unless
   `case_sensitive`). An invalid regex fails with a message naming its label.
   Bad keywords and old-format keys fail too (see [Keywords](#keywords) and
   [Migrating](#migrating-from-the-old-context-rules)).
2. **Normalize** the text (and context): collapse whitespace, unicode dashes to
   `-`, smart quotes to ASCII, each only if enabled.
3. **Find candidates**: every pattern runs on the text, each separately with
   `finditer`. Patterns are never combined into one alternation, so overlaps
   stay visible. Zero-length matches (e.g. from `\d*`) are skipped. The
   context never decides which patterns run, and regexes never run on it.
   A pattern with `validate` is scanned the same way, but each regex hit is
   first trimmed or split by phonenumbers (see [Phone numbers](#phone-numbers)).
4. **Score** each candidate: `score = text_score + context_score` (below).
   **Validate** the candidates of `validate` patterns: each becomes one match
   per reading (`valid_US_number`, ...) or is dropped as impossible, before
   they compete.
5. **Drop** candidates with `score < min_score`.
6. **Resolve overlaps**, best first, using `overlap_tiebreak` (in order; any
   of `score`, `length`, `priority`). If every listed criterion ties, or the
   list is empty, the earlier start and then the label decide. A pair of
   labels listed in `coexist` keeps both. Identical spans count as
   overlapping. Every decision records what decided it (`decided_by`).
7. **Misses**: each label the context scored above 0 that ended with no match
   gets a `miss` decision (see [Misses](#misses)).

## Scoring

A candidate's score is two independent sums, added:

| Part | Reads | Keyword keys |
| --- | --- | --- |
| `text_score` | the words around the match in the text | `keywords` + `nearby` |
| `context_score` | every word of the context string | `keywords` + `context` |

Both use the same matcher: whole words or phrases, case-insensitive, each
keyword counted once at its nearest occurrence. With `distance_decay`, a
keyword's weight is divided by its word distance (nearest word = 1).

- **Text window:** up to `context_window_words` words before and/or after the
  match (`context_direction`: `before`, `after` or `both`), stopping early at
  any `stop_at` character. Before the match a phrase's last word sets its
  distance; after it, its first word.
- **Context string:** read whole, as if it came right before the match. Its
  **last** word is distance 1, the one before it 2, and so on; a phrase's last
  word sets its distance. `context_window_words`, `context_direction` and
  `stop_at` do not apply.
- A `keywords` word found in both places counts in both: with
  `keywords: {phone: 3}`, context `phone` and text `phone 212-555-0123`, the
  score is 3 + 3 = 6.
- The context has no position relative to any one match, so it adds the same
  amount to every candidate of a pattern.
- With no context (or an empty one), `context_score` is 0 and nothing else
  changes.

Example (from `extraction_config.yaml`, decay on):

```text
context:  "Acct"
text:     "Acct 2125550123 call 212-555-0123"
account_number 2125550123:  text acct +3, call -2  -> 1;  context acct +3  -> 4.0
phone_number   2125550123:  text call +2           -> 2;  context          -> 2.0
  -> overlap, account_number wins by score
phone_number   212-555-0123: text call +2          -> 2.0, no overlap, kept
                                                      (as valid_US_number)
```

## Keywords

Each pattern has up to three maps of `word: signed weight`:

```yaml
- label: phone_number
  keywords: {phone: 3, call: 2, acct: -3}   # text window AND context string
  nearby:   {ext: 1}                        # text window only
  context:  {customer: 2, invoice: -2}      # context string only
```

- Use `keywords` for most words. Use `nearby` or `context` when a word should
  count in only one place.
- A word in `keywords` may not also be in `nearby` or `context` of the same
  pattern (load error), so there are no precedence rules. The same word in both
  `nearby` and `context` is fine.
- A negative weight lowers the score. Weight 0 is a load error.

Keywords are ASCII letters only. Apostrophes join a word (`don't` -> `dont`);
spaces, hyphens, digits and everything else separate words, so the keyword
`e-mail` means the two words `e` `mail`. The context string is split into
words the same way as the text. To avoid keywords that would silently match
the wrong words, `load_config` fails with a message naming the pattern, key
and keyword when:

- a keyword has a digit or an accented letter (`w2`, `1099`, `café`) or no letters;
- the same keyword appears twice under one key of a pattern, also when spelled
  differently (`amount due` and `amount-due` are the same words);
- a word is in `keywords` and also in `nearby` or `context`;
- a weight is 0 or not a number.

Quote YAML words that YAML would read as something else: unquoted, `on`,
`yes`, `true` load as `True`, and `off`, `no`, `false` as `False`.
`load_config` rejects such keywords ("must be a string (quote it in the
YAML ...)") instead of silently turning them into "true" or "false". Write
`"on": 1`, not `on: 1`. A blank entry (a `null` / `~` key) fails with "has an
empty entry; remove it".

Leaving out `keywords`, `nearby` or `context`, or leaving them blank, is fine:
the pattern simply has no keywords of that kind and scores 0 there.

## Config

`extraction_config.yaml` is the source of truth. Keys:

| Key | Purpose |
| --- | --- |
| `settings` | `context_window_words`, `context_direction` (`before`/`after`/`both`, default `both`), `stop_at` (characters that end the text window scan, default `""`), `distance_decay`, `min_score`, `case_sensitive`, `overlap_tiebreak` (any of `score`, `length`, `priority`, in order; may be empty) |
| `normalize` | `collapse_whitespace`, `unicode_dashes_to_hyphen`, `smart_quotes_to_ascii` |
| `patterns` | `label`, `priority`, `regex`, `keywords`, `nearby`, `context` (each word -> signed weight), optional `validate` ([Phone numbers](#phone-numbers)) |
| `coexist` | label pairs that may both be kept when they overlap |
| `tests` | cases `{text, expect: [{label, text}], context?, name?}` run by pytest |

### Migrating from the old context rules

The context used to select which patterns run (`context.positive`) and break
ties (`context.penalize`, `context_penalty`). Now every pattern runs and the
context adds to the score. Old configs do not load; `load_config` says what
to change:

| Old | New |
| --- | --- |
| `context.positive: [word, ...]` | `context: {word: weight}` (pick a weight) |
| `context.penalize: {word: -w}` | `context: {word: -w}` (now lowers the score) |
| `context.negative`, `context.positive: {...}` | same as above |
| `context_penalty` / `context_negative` in `overlap_tiebreak` | remove it |

A `keywords` map keeps working unchanged; its words now also score in the
context, with the same weights.

Results change in one visible way: a context no longer excludes patterns,
so matches that do not overlap anything are kept whatever the context says.
To drop a label from a context, give it a negative weight large enough to push
its candidates below `min_score`.

## Phone numbers

A pattern with a `validate:` block checks its matches with the
[phonenumbers](https://github.com/daviddrysdale/python-phonenumbers) library
(Google's libphonenumber metadata). Its regex finds anything phone-shaped
(recall), and validation sorts what it found (precision). CARP does not guess
the country: **every plausible reading of a number comes back as its own
match**, and every impossible hit is logged in the report. Patterns without
`validate` work exactly as before, and configs without it never import the
library.

```yaml
- label: phone_number
  priority: 2
  regex: '\+?\(?\d[\d\s().-]{6,18}\d'   # broad on purpose; tighten it any time
  keywords: {phone: 3, tel: 3, call: 2}
  validate:
    with: phonenumbers          # required; the only value
    regions: [US, GB]           # required; parsed in this order; the first is the home region
    hints:                      # optional; words that point to a region, weight > 0
      GB: {uk: 3, london: 2}
      IN: {india: 3}
    readings: valid_and_home    # what the list returns: valid_and_home | all | valid_else_possible | valid
    hint_conflict: all          # valid in several hinted regions: all | nearest
    output_format: E164         # extra.normalized: E164 | INTERNATIONAL | NATIONAL
```

- The minimum is `validate: {with: phonenumbers, regions: [US]}`.
- Region codes are ISO 3166 alpha-2 (`US`, `GB`, `IN`, ...).
- Hint words follow the usual [keyword rules](#keywords), and they are read
  like `keywords`: in the text window and in the whole context.
- A hinted region need not be in `regions`.
- `load_config` names the pattern and key for an unknown key, region,
  value or keyword, and fails if phonenumbers is not installed.

### Labels

A reading is a status in a region. Each one becomes a match labeled
`{status}_{REGION}_number`:
- `valid_US_number`: a known number range in the US.
- `possible_GB_number`: the right length for GB but not a known range. A new
  area code the library doesn't know yet lands here.

Readings that give the same number count as one. US and CA share +1, so
`416-555-0123` gives one reading, labeled by its real region:
`valid_CA_number`.

### The rule

1. **A `+` number** uses its country code: one reading. Source `number`.
2. **Caller region or hint words.** The caller region (`extract(region=...)`
   / `--region`), or else every region whose hint words appear, is parsed
   in those regions only.
   - Valid readings are returned. If there are none, the possible ones are
     returned.
   - `hint_conflict: nearest` keeps only the region of the nearest hint
     word. A hint word in the text window is nearer than any word in the
     context; then the higher hint score wins; an exact tie keeps both.
   - Source `caller` or `hint`.
   - If the number fits none of those regions, the report notes e.g.
     `hint FR did not fit` and step 3 runs.
3. **The list:** parse in every `regions` entry. Source `list`.
   `readings` decides what is returned:

   | `readings` | Returned |
   | --- | --- |
   | `valid_and_home` (default) | every valid reading, plus the home (first) region's reading when it is only possible |
   | `all` | every valid and possible reading |
   | `valid_else_possible` | the valid readings if any, else every possible one |
   | `valid` | valid readings only |

4. **Impossible everywhere** (it doesn't parse, or it has the wrong length):
   no match, and a `dropped` row says why. Before giving up, a leading `00`
   is read as `+`, because `00` is the international prefix in most
   countries but not in the US. `011` from the US is handled by the library.

Example: `555-123-4567` is possible in the US (555 is no real area code) and
valid in GB. With `regions: [US, GB]`:

| `readings` | Matches |
| --- | --- |
| `valid_and_home` | `possible_US_number`, `valid_GB_number` |
| `all` | the same |
| `valid_else_possible`, `valid` | `valid_GB_number` |

### Before the rule: refining the regex hit

Each regex hit first goes to `PhoneNumberMatcher` (POSSIBLE leniency) in the
caller region, else the top hinted region, else the first of `regions`.
- It trims a greedy hit: `212-555-0123 2026` becomes `212-555-0123`.
- It splits two numbers found in one hit.
- The regex scan resumes after the last number found, so back-to-back
  numbers are all kept.
- If the matcher finds nothing (`94105-1234`), the raw hit goes through the
  rule as it is.

The refined numbers are then scored like any other candidate.

### Matches, overlaps, misses

- **Readings of one hit** share its span, score and offsets. They never
  conflict with each other.
- **Other patterns:** each reading competes normally with other patterns
  (e.g. `account_number`), by score, length and priority.
- **`family`:** every reading carries `family`, the pattern's own label
  (`phone_number`).
  - `coexist: [[phone_number, account_number]]` covers every reading. A
    generated label works in `coexist` too.
  - Miss rows count a reading as a match of its pattern.
- **`extra`:** kept matches carry
  `{normalized, region, status, region_source, number_type, readings}`.
  - `normalized` is in `output_format`. A local-only number (7 digits, no
    area code) keeps its raw text.
  - `number_type` is the library's type name (`MOBILE`, ...) for valid readings.
  - `readings` is how many readings the hit returned.
- **Choosing one answer:** if your code needs a single answer per number,
  take the `valid_*` match, or set `readings: valid` /
  `hint_conflict: nearest`.

### Limits

- Validation can't tell an account number from a phone number when the
  account number fits an assigned range. Keywords still decide that.
- **Every listed region adds readings, of junk too.** `94105-1234` (a ZIP+4
  code) is impossible in the US but valid in FR and possible in GB. List
  only the regions you expect; use `hints` for the rest.
- **Metadata goes stale.** Results can change when you upgrade phonenumbers
  (`python -m pip install -U phonenumbers`). Every `validation` row records
  the version in its `reason`. A cluster of `possible` numbers sharing an
  area code in the [validation summary](#validation-summary) is the sign to
  upgrade.
- **Hint words are plain words.** No grammar: "our UK team called a client
  in India" counts both words. They are ASCII only (no `£`, `€` or accents).
- **Extensions** (`x204`) are not captured.

## Output files

`extract()` writes nothing. The CLI writes `extract_report.csv` to the working
directory only with `--report`; library callers use the helpers below. The
file is gitignored.

- `extract_report.csv`: the decision log, one row per decision. Columns:
  `file, kind, outcome, reason`, the `a_*` and `b_*` candidate columns
  (`label, text, start, end, score, text_score, context_score, family`), the
  phone columns `status, region, normalized` (blank except on `validation`
  rows), then `decided_by`. `family` is the pattern label (it differs from
  `label` only for phone readings).
  - `kind` is `below_min_score`, `overlap`, `coexist`, `miss` or `validation`.
  - `validation` rows: one per reading of a phone hit (`a` = the reading):
    - `outcome` is `kept`, or `not_returned` when `readings` / `hint_conflict`
      filtered it out;
    - an impossible hit gets a single `dropped` row;
    - `decided_by` is the source (`number`, `caller`, `hint`, `list`);
    - `reason` reads e.g. `possible in GB; readings: valid; phonenumbers 9.0.43`.
  - In an `overlap` row, `a` is the winner and `b` the dropped candidate.
  - `decided_by` is machine-readable: for `overlap`, the criterion that decided
    (`score`, `length`, `priority`, or `start_label` when everything tied);
    `coexist` for `coexist`; `min_score` for `below_min_score`; empty for `miss`;
    the source (`number`, `caller`, `hint`, `list`) for `validation`.
- Library helpers (all optional, none called by `extract()`): `write_report_csv(decisions)`
  appends rows; `reset_report()` starts a fresh file; `open_report()` opens the
  report once for many calls (see the loop above).

> **Memory:** do not collect decisions from many calls in a list; write them as
> you go with `open_report()`.

### Misses

A miss is a hint of a false negative: the context pointed to a label (its
`context_score` is above 0) but no match of that label came out. One `miss`
row per such label, with `a_label` and `a_context_score` filled in:

| `outcome` | Meaning | Usually fix |
| --- | --- | --- |
| `no_candidates` | the regex found nothing in the text | the regex |
| `candidates_dropped` | it matched, but every candidate lost (min_score, overlap or validation) | weights, `min_score` or `on_*` |

The `reason` names the context words, e.g.
`context scored phone_number +1.00 (call); no candidates in text`.
A label the context scored at or below 0 never gets a miss row. Misses the
context gives no hint of can only be caught with labeled examples (the
`tests` section).

## Summaries

`extract.py --summarize [PATH]` prints the overlap table, then (if the report
has any `miss` rows) the miss table, then (if it has `validation` rows) the
validation table. A report without a `decided_by` column
is rejected: regenerate it with `--report`.

### Overlap summary

`summarize_overlaps(path="extract_report.csv")` reads the report's `overlap`
rows and returns one row per label pair:
`pair, overlaps, by_score, by_length, by_priority, by_start_label,
not_by_score, not_by_score_pct, no_evidence, wins, examples, hint`.

- Start at the top. `no_evidence` (`by_priority + by_start_label`) counts
  overlaps where nothing in the input decided, only the fallback order did.
- `not_by_score` also counts overlaps settled by `length`, which are often fine.
- `hint` says what to try, and `examples` gives up to three inputs to look at
  (`<file>: "<a_text>" vs "<b_text>"`, fallback-decided ones first).
  - "add nearby, context or keywords words": the fallback order decided.
  - "add to coexist": most unsettled overlaps are partial (different spans),
    which often means two distinct values that may both belong in the output.
  - `ok`: nothing to fix.

### Miss summary

`summarize_misses(path="extract_report.csv")` returns one row per label:
`label, misses, no_candidates, dropped, examples, hint`, most misses first.
`examples` lists up to three `file` ids; `hint` points at the regex when
`no_candidates` dominates, otherwise at the weights.

### Validation summary

`summarize_validation(path="extract_report.csv")` returns one row per phone
pattern (`family`):
`family, hits, kept, valid, possible, not_returned, dropped, multi, sources,
regions, clusters, examples, hint`.

- `hits` counts numbers in the text. `kept`, `valid` and `possible` count
  returned readings, and `not_returned` counts the filtered ones.
- `multi` counts hits with more than one returned reading.
- `clusters` groups `possible` readings by leading digits (the area code for +1).
- `examples` gives up to three multi or dropped hits.
- Hints:
  - "phonenumbers metadata may be stale, upgrade it": 3+ possible readings
    share leading digits.
  - "add hints, or set readings / hint_conflict": some numbers fit several
    regions.
  - "tighten the regex or add regions": most hits were impossible.

### Tuning workflow

1. Run your inputs with the report on (`--report`, or `open_report()` in a loop).
2. `extract.py --summarize`.
3. Fix the pairs and labels at the top (keywords, `coexist`, regexes, as the
   hints say), then repeat.

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

`test_extract.py` runs the `tests` section of the config and unit-tests coexist
and non-coexist overlaps, tie-breaks (including an empty list), text and
context scoring (decay, phrases, no window on the context, text/context
parity), `keywords` reaching both sources, `context_direction`, `stop_at`,
keyword validation and old-format errors, `decided_by`, `miss` rows, the
report CSV and its score breakdown, the summaries, the CLI, and the public API
as shown in the README and this reference. Phone tests cover the `validate`
load and migration errors, each status, every `readings` and `hint_conflict`
mode, caller and hints (including a hint that does not fit), `+`, `00` and
`011`, span refinement and back-to-back numbers, readings competing with
other patterns, `coexist` and misses through `family`, `extra`, the report
rows and the validation summary.
