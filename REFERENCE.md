# CARP reference

The full rules. For an overview, see the [README](README.md).

## Usage

### CLI

Scans one text per run. For many texts, or a CSV of them, use the library.

```bash
.venv/bin/python extract.py --config extraction_config.yaml \
    [--text TEXT] [--context TEXT] [--report]
.venv/bin/python extract.py --summarize [PATH]
```

- `--text TEXT` is the text to scan. If omitted, the text is read from stdin
  (UTF-8, `errors="replace"`).
- `--context TEXT` is an optional context string. Its words add to the score
  of every pattern that lists them (see [Scoring](#scoring)).
- Prints the matches as a JSON list of `{label, text, start, end, score}`.
  For a pattern with `validate`, `text` is the number in `output_format`
  (E.164 by default), not the input slice (see [Phone numbers](#phone-numbers)).
- Writes nothing else unless asked: `--report` writes the decision log to
  `./extract_report.csv` (overwritten each time).
- `--summarize [PATH]` prints the [overlap, miss and validation summaries](#summaries) of
  a report (default `./extract_report.csv`) and exits. It needs no `--config`
  and cannot be combined with `--config`, `--text`, `--context` or `--report`.
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
```

`extract()` returns `(matches, decisions)` and **writes no files**.
`matches` are the candidates that survived scoring and overlap resolution; each
has `label`, `text`, `start`, `end`, `score`, the breakdown `text_score` and
`context_score` (`score` is their sum), `priority`, and `pattern` (the pattern
label; see [Phone numbers](#phone-numbers)). Offsets refer to the
**normalized** text. For a pattern with `validate`, `text` is the formatted
number and `raw` is the number as written (`raw` is `""` for other matches).
The CLI JSON has only `label`, `text`, `start`, `end` and `score`. `decisions` is the flat decision log (see
[Output files](#output-files)); ignore it, keep it, or write it with the helpers
below. `source` fills its `file` column. When one report holds many texts,
give each call its own `source` (as below): the validation summary tells phone
hits apart by `file`, offsets and written text, so two texts with the same or
a blank `source` can have their hits counted as one.

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

## Decision flow

### From text to matches

Each candidate goes through these steps in order. The tag after an arrow is the
report row the step writes.

1. **Normalize.** Text and context: whitespace, dashes, quotes (only the
   options you enabled).
2. **Run every pattern's regex on the text, each on its own** (never
   combined, never on the context; zero-length matches are skipped).
   - **no hit** → nothing to decide for that pattern.
3. **Is it a phone pattern (has `validate`)?**
   - **yes** → trim or split the hit with PhoneNumberMatcher. If it finds
     nothing, trailing digit groups are trimmed until a number is left;
     failing that, the raw hit stays.
   - **no** → use the hit as it is.
4. **Score: text score + context score.** A phone reading keeps the score of
   the hit it came from.
5. **Phone pattern?**
   - **yes** → run the [phone flow](#phone-decision-flow). It returns zero or
     more readings.
     - **no reading** → the hit is dropped and logged. → `row: validation, dropped`.
       With `keep_unknown: true` it becomes one `unknown_number` match
       instead, which goes on to step 6. → `row: validation, kept_unknown`
   - **no** → go to step 6.
6. **Score at least `min_score`?**
   - **no** → the candidate is dropped. → `row: below_min_score`
   - **yes** → continue.
7. **Overlaps a candidate already accepted?** (Identical spans count.)
   Candidates are taken best first, in `overlap_tiebreak` order, so the loser
   is always the lower-ranked one.
   - **no** → the candidate is kept. → *no row*
   - **yes** → go to step 8.
8. **Coexist pair, or readings of one phone number?**
   - **yes** → both are kept. Readings of one number never compete with each
     other. → `row: coexist` (pairs only)
   - **no** → run the [tie-break chain](#tie-break-chain). The loser is
     dropped. → `row: overlap`
9. **Misses, at the end.** Each label the context scored above 0 that has no
   match gets a `miss` row (see [Misses](#misses)).

### Tie-break chain

When two candidates overlap and are not a `coexist` pair, `overlap_tiebreak`
runs these in order. A criterion you leave out is skipped.

1. **Does the score differ?**
   - **yes** → the higher score wins. → `decided_by: score`
   - **no** → next criterion.
2. **Does the length differ?**
   - **yes** → the longer match wins. → `decided_by: length`
   - **no** → next criterion.
3. **Does the priority differ?**
   - **yes** → the lower number wins. → `decided_by: priority`
   - **no** → last resort.
4. **Still tied.** The earlier start wins, then the label.
   → `decided_by: start_label` (nothing in your input decided it; add
   keywords)

An empty `overlap_tiebreak` goes straight to step 4.

### Worked example

Context `Acct`, text `Acct 2125550123 call 212-555-0123`, with the example
config (decay on, `min_score` 0, `phone_number` validated for the US):

| Step | What happens |
| --- | --- |
| Find | `account_number` hits `2125550123`. `phone_number` hits `2125550123` and `212-555-0123`. |
| Score | `account_number`: text `acct` +3, `call` -2 = 1.0; context `acct` +3; total **4.0**. Phone hit 1: `call` +2 = **2.0**. Phone hit 2: `call` +2 = **2.0**. |
| Validate | Both phone hits are valid in the US: two `validation` rows, `kept`, source `regions`. Labels become `valid_US_number`. |
| `min_score` | All three are at or above 0. No `below_min_score` rows. |
| Overlap | `account_number` and the first phone reading share a span. Not a coexist pair, so the chain runs: score 4.00 > 2.00. The `overlap` row says `decided_by: score`, and the phone reading is dropped. |
| Result | `account_number` `2125550123` (4.0) and `valid_US_number` `+12125550123` (2.0, written `212-555-0123`). The second phone hit overlapped nothing, so it has no row beyond its validation row. |

The same text without the context scores `account_number` 1.0 against the
first phone reading's 2.0, so the phone reading wins instead. That flip is the
context doing its job.

### Worked example: account or phone, two regions

The example config with `regions: [US, GB]`. Context `customer phone`, text
`Acct 2125550123 on file`. The number is real in the US and only the right
shape in GB, and the context points to a phone.

| Step | What happens |
| --- | --- |
| Find | `account_number` and `phone_number` both hit `2125550123` (5-15). The phone library confirms the span, so nothing is trimmed. |
| Score | `account_number`: text `acct` +3 (1 word away); context `phone` -3 (the context's last word, distance 1); total **0.0**. Phone hit: no phone words in the text; context `phone` +3; total **3.0**. |
| Validate | No `+`, no hint words, so it is parsed in each region of `regions`. US: `+12125550123` **valid**. GB: `+442125550123` only **possible**. `keep_possible: home` keeps the valid reading and the home region's possible one; the home region (US) is already valid, so GB is not kept. |
| `min_score` | 0.0 and 3.0 are both at or above 0. |
| Overlap | `valid_US_number` and `account_number` share a span and are not a coexist pair. Score 3.00 > 0.00: the phone reading wins. |
| Result | `valid_US_number`, `text` `+12125550123`, `start` 5, `end` 15, score 3.0. |

The report has three rows:

| kind | outcome | a_label | a_text | normalized | decided_by | reason |
| --- | --- | --- | --- | --- | --- | --- |
| validation | kept | valid_US_number | 2125550123 | +12125550123 | regions | valid in US |
| validation | not_kept | possible_GB_number | 2125550123 | +442125550123 | regions | possible in GB; keep_possible: home |
| overlap | a_wins_b_dropped | valid_US_number (b: account_number) | 2125550123 | | score | score 3.00 > 0.00 |

The overlap row also has `b_text_score` 3.0 and `b_context_score` -3.0, which
shows the context cancelling the `Acct`.

Without the context, `account_number` wins 3.0 to 0.0; both validation rows
are still written, and the phone reading is the loser in the overlap row.

## Reading the report

Each row of `extract_report.csv` is one decision. `kind` says which step wrote
it, `outcome` what happened, and `decided_by` the rule that decided.

| `kind` | Written when | `outcome` | `decided_by` |
| --- | --- | --- | --- |
| `validation` | a phone hit was checked: one row per reading, or one for a hit with no reading | `kept`, `not_kept`, `dropped`, `kept_unknown` | `number`, `hint`, `regions` |
| `below_min_score` | a candidate scored under `min_score` | `dropped` | `min_score` |
| `overlap` | two candidates overlapped and one lost | `a_wins_b_dropped` | `score`, `length`, `priority`, `start_label` |
| `coexist` | two candidates overlapped but their labels are a coexist pair | `both_kept` | `coexist` |
| `miss` | the context scored a label above 0 and it has no match | `no_candidates`, `candidates_dropped` | (empty) |

Questions the report answers:

| Question | Where to look |
| --- | --- |
| Why did `A` beat `B`? | the `overlap` row: `a_*` is the winner, `b_*` the loser; `reason` shows the numbers |
| Why is my match missing? | `b_` side of an `overlap` row; a `below_min_score` row; a `validation` row with `dropped`; or a `miss` row |
| Why `possible_US_number` and not `valid_US_number`? | the `validation` row: `status`, `region`, and `reason` |
| Why was a phone reading not kept? | a `validation` row with `not_kept`; `reason` names the `keep_possible` or `when_hints_disagree` setting, or says "a hinted region gives a valid reading" |
| What do I tune first? | `extract.py --summarize` (see [Summaries](#summaries)) |

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

Example: see the [worked example](#worked-example).

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
- The same word in both `nearby` and `context` is fine. A negative weight
  lowers the score.

**Keywords match whole words exactly.** There is no stemming, no
abbreviation and no partial match: `account` does not match `acct` or
`accounts`. List each spelling your data uses (`account: 3, acct: 3`).

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

## Config options

Everything lives in `extraction_config.yaml`, in five sections: `settings`,
`normalize`, `patterns`, `coexist` and `tests`. Every option is listed below.
"Required" means `load_config` fails without it; an unknown key or a value of
the wrong type (a quoted `"false"`, a blank `regex:` or `priority:`) fails with
a message naming the section and key; an invalid regex fails naming its label.

### `settings`

| Option | Required / default | Values | What it does |
| --- | --- | --- | --- |
| `context_window_words` | required | a whole number, e.g. `8` | How many words on each side of a match are searched for keywords. |
| `context_direction` | default `both` | `before`, `after`, `both` | Which side(s) of the match the text window covers. The context string is always read whole. |
| `stop_at` | default `""` (none) | a string of characters, e.g. `".!?"` | The text window scan stops at the first of these characters, so words in the next sentence don't count. Does not apply to the context string. |
| `distance_decay` | required | `true`, `false` | `true`: a keyword's weight is divided by its distance in words (1 = next to the match; in the context, the last word is 1). `false`: full weight at any distance. |
| `min_score` | required | a number, e.g. `0` | A candidate scoring below this is dropped (`below_min_score` row). Valid phone readings are exempt by default (`valid_ignores_min_score`). |
| `case_sensitive` | required | `true`, `false` | Whether the regexes are case-sensitive. Keywords always match regardless of case. |
| `overlap_tiebreak` | required (may be empty) | a list of `score`, `length`, `priority`, in the order to try | How an overlap between two candidates is decided: higher score, then longer match, then lower priority number. Anything still tied (or an empty list) goes to the earlier start, then the label. |

### `normalize`

Each option is off unless set to `true`. Offsets (`start`, `end`) refer to the
normalized text.

| Option | What it does |
| --- | --- |
| `collapse_whitespace` | Turns every run of spaces, tabs and newlines into one space. |
| `unicode_dashes_to_hyphen` | Turns en and em dashes (`–` `—`) into `-`. |
| `smart_quotes_to_ascii` | Turns curly quotes (`‘ ’ “ ”`) into straight ones (`' "`). |

### `patterns`

A list; each entry is one thing to find.

| Option | Required / default | Values | What it does |
| --- | --- | --- | --- |
| `label` | required, unique | text, e.g. `phone_number` | The name a match is reported under. For a pattern with `validate`, matches get generated labels (`valid_US_number`) and this name is their `pattern`. A label of that generated form (`{valid,possible}_{REGION}_number`) is an error. |
| `priority` | required | a whole number | Breaks ties in `overlap_tiebreak`: the lower number wins. |
| `regex` | required | a regular expression (quote it in single quotes) | What to find. Each pattern's regex runs on its own, so overlaps stay visible. |
| `keywords` | optional | `{word: weight}` | Words that raise (positive weight) or lower (negative weight) the score, looked for in the text window **and** the context string. |
| `nearby` | optional | `{word: weight}` | Like `keywords`, but looked for in the text window only. |
| `context` | optional | `{word: weight}` | Like `keywords`, but looked for in the context string only. |
| `validate` | optional | a block, below | Checks matches as phone numbers. |

Keyword rules: see [Keywords](#keywords).

### `validate` (inside a pattern)

`load_config` warns (it does not fail) about a setting you wrote that can do
nothing: `also_try_regions` or `when_hints_disagree` with no `hints`, and
`keep_possible: if_no_valid`, `home` or `always` with a single region
(unless `hints` and `also_try_regions: always` make them differ).

| Option | Required / default | Values | What it does |
| --- | --- | --- | --- |
| `with` | required | `phonenumbers` | The checking library. It is the only value. |
| `regions` | required | a list of country codes, e.g. `[US, GB]` | The countries a number is checked in, in order. The first is the home country. |
| `hints` | optional | `{COUNTRY: {word: weight}}`, weights above 0 | Words that point to a country (`GB: {uk: 3, london: 2}`). When one appears near the number or in the context, that country is checked first. The country need not be in `regions`. |
| `keep_possible` | default `home` | `never`, `if_no_valid`, `home`, `always` | Which *possible* readings from `regions` are kept; valid ones always are. `never`: none. `if_no_valid`: every possible one when there is no valid reading (a valid hinted reading counts, with `also_try_regions: always`). `home`: the home country's, even when another one is valid. `always`: every one. Hinted and `+` readings are not filtered by it: see [Who filters which readings](#who-filters-which-readings). |
| `when_hints_disagree` | default `all` | `all`, `nearest` | When several hinted regions give a reading (valid ones if any, otherwise possible ones): `all` keeps every one; `nearest` keeps only the region whose hint word is nearest. A word in the text window is nearer than any context word, then the higher hint score wins, and an exact tie keeps both. |
| `also_try_regions` | default `if_hint_not_valid` | `never`, `if_hint_fails`, `if_hint_not_valid`, `always` | After a hint, whether `regions` is parsed too. `never`: never, so a number that fits no hinted region has no reading. `if_hint_fails`: only when the hinted regions gave no reading at all. `if_hint_not_valid`: when no hinted reading is valid. `always`: every time. |
| `output_format` | default `E164` | `E164`, `INTERNATIONAL`, `NATIONAL`, `RAW` | How a match's `text` is written: `+12125550123`, `+1 212-555-0123`, `(212) 555-0123`, or `RAW`, the number exactly as written in the input. |
| `valid_ignores_min_score` | default `true` | `true`, `false` | `true`: a reading the library confirms is valid is never dropped for scoring below `settings.min_score`, so a stray negative word can't erase a real number. It still competes in overlaps, so `Acct 2125550123` can still go to `account_number`. Possible readings always need `min_score`. `false`: valid readings need `min_score` too. |
| `keep_unknown` | default `false` | `true`, `false` | `true`: a hit that fits no region (or that `also_try_regions: never` stopped) is kept as `unknown_number` with its raw text instead of being dropped. |

The full rule and examples are in [Phone numbers](#phone-numbers).

### `coexist`

A list of label pairs, e.g. `- [timestamp, date]`. When two overlapping
matches form a listed pair, both are kept instead of tie-breaking (a
`coexist` row). Order within a pair doesn't matter. A pattern label such as
`phone_number` covers all of its generated labels. Each entry must be a pair
of two different labels, each a pattern label or (when a pattern has
`validate`) a generated label such as `valid_US_number`, or `unknown_number`
when a pattern has `keep_unknown: true`; anything else fails at load time.

### `tests`

A list of cases that `pytest` runs against the config:

| Option | Required | What it does |
| --- | --- | --- |
| `name` | optional | A description shown when the case fails. |
| `text` | required | The text to scan. |
| `context` | optional | The context string passed with the text. |
| `expect` | required | The exact matches, each `{label, text}`, in any order. An empty list means "no matches". |

Any other key in a case (e.g. a misspelled `contxt`) fails at load time.

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
    keep_possible: home         # possible `regions` readings to keep: never | if_no_valid | home | always
    when_hints_disagree: all    # several hinted regions fit: all | nearest
    also_try_regions: if_hint_not_valid  # after a hint, also parse `regions`: never | if_hint_fails | if_hint_not_valid | always
    keep_unknown: false         # true: keep a hit that fits nowhere as unknown_number
    output_format: E164         # the match text: E164 | INTERNATIONAL | NATIONAL | RAW
```

- The minimum is `validate: {with: phonenumbers, regions: [US]}`.
- Region codes are ISO 3166 alpha-2 (`US`, `GB`, `IN`, ...).
- Hint words follow the usual [keyword rules](#keywords), and they are read
  like `keywords`: in the text window and in the whole context.
- Regions come only from the config; `extract()` and the CLI take no region.
- `load_config` names the pattern and key for an unknown key, region,
  value or keyword, fails on an empty `validate:`, and fails if phonenumbers
  is not installed.

### Labels

A reading is a status in a region. Each one becomes a match labeled
`{status}_{REGION}_number`:
- `valid_US_number`: a known number range in the US.
- `possible_GB_number`: the right length for GB but not a known range. A new
  area code the library doesn't know yet lands here.
- The region is the country the number belongs to, not the one it was read
  in: with `regions: [US]`, `011 44 20 7946 095` (through the US
  international prefix) is `possible_GB_number`.
- Numbers that belong to no country (`+800`, `+808`, `+870`...) get region
  `001`, the library's code for them: `valid_001_number`.
- `unknown_number`: with `keep_unknown: true`, a hit that has no reading. Its
  `text` is the raw hit (`output_format` can't apply: there is no parsed
  number). Several phone patterns share this label, as they share reading
  labels; `pattern` tells them apart. No pattern may be labeled
  `unknown_number`.

Readings that give the same number count as one. US and CA share +1, so
`416-555-0123` gives one reading, labeled by its real region:
`valid_CA_number`.

### Phone decision flow

1. **A `+` number** uses its country code: one reading, source `number`;
   nothing else is tried. One that is not possible (or too short to be a full
   number) has no reading (step 4).
2. **Hint words.** If hint words for any region appear, the number is parsed
   in those regions first.
   - Valid readings are kept. If there are none, the possible ones are
     kept.
   - `when_hints_disagree` picks among several hinted regions that give a
     reading (see [`validate`](#validate-inside-a-pattern)).
   - Source `hint`.
   - `also_try_regions` decides whether step 3 also runs. Its readings are
     added, with source `regions`, and the hinted readings stay. The values
     form a scale, by how good the hinted result had to be to stop the
     search:

     | What the hinted regions gave | `never` | `if_hint_fails` | `if_hint_not_valid` (default) | `always` |
     | --- | --- | --- | --- | --- |
     | nothing fits | no reading* | + regions | + regions | + regions |
     | only possible readings | hint only | hint only | hint + regions | hint + regions |
     | a valid reading | hint only | hint only | hint only | hint + regions |

     \* dropped, or `unknown_number` with `keep_unknown: true` (step 4).
   - The report notes why `regions` was parsed too, e.g. `hint FR did not fit` or
     `hint GB not valid; also_try_regions: if_hint_not_valid`.
3. **`regions`:** parse in every `regions` entry. Source `regions`.
   Valid readings are always kept; `keep_possible` decides which possible
   ones are (values in [`validate`](#validate-inside-a-pattern)). Any reading
   here, even one filtered out (`not_kept`), ends the flow: there is no
   `dropped` row and no `00` retry.

4. **No reading** (it doesn't parse, or it has the wrong length everywhere
   tried): before giving up, a leading `00` is read as `+`, because `00` is
   the international prefix in most countries but not in the US. `011` from
   the US is handled by the library. Then the hit is dropped, and a
   `dropped` row says why. With `keep_unknown: true` it becomes one
   `unknown_number` match instead (raw text, the hit's span and score) and
   the row says `kept_unknown`, e.g.
   `impossible in US (TOO_SHORT); keep_unknown; phonenumbers 9.0.43`. It must
   reach `min_score` (`valid_ignores_min_score` does not exempt it) and
   competes in overlaps like any match. A hit whose readings were all
   filtered out (`not_kept` rows only) is not "no reading": it never becomes
   `unknown_number`.

Example: `555-123-4567` is possible in the US (555 is no real area code) and
valid in GB. With `regions: [US, GB]`:

| `keep_possible` | Matches |
| --- | --- |
| `home` | `possible_US_number`, `valid_GB_number` |
| `always` | the same |
| `if_no_valid`, `never` | `valid_GB_number` |

#### Who filters which readings

| Where the reading came from (`decided_by`) | What filters it |
| --- | --- |
| `number` (`+` code, or `00` read as `+`) | nothing: always kept |
| `hint` | a fixed rule: valid readings if any, else possible ones; then `when_hints_disagree`. **`keep_possible` does not apply.** |
| `regions` | `keep_possible` |

- **"Home" is the first entry of `regions`**, never a hinted region.
- **A valid hinted reading counts as "a valid reading" for
  `keep_possible: if_no_valid`.** This only happens with
  `also_try_regions: always`, the only mode where the regions list runs
  after a valid hint. Example: a hint gives `valid_GB_number`, so possible
  readings from `regions` are dropped.
- **A number already read through a hint is not read again** from
  `regions` (readings are deduplicated by E.164).
- **`keep_possible: home` with an impossible home region:** if the number is
  impossible in the home region, valid nowhere, and possible only in other
  listed regions, nothing comes back, and the hit has only `not_kept` rows.
  With `regions: [US, GB]`, `0406 812 4158` is impossible in the US and only
  possible in GB: `home` (and `never`) give no match, while `if_no_valid` or
  `always` give `possible_GB_number` (`+444068124158`).

#### Worked example: one number, three setups

`212-555-0123` is valid in the US and possible in GB, with `regions: [US, GB]`:

| Setup | Matches | Why |
| --- | --- | --- |
| no hints, `keep_possible: home` | `valid_US_number` | GB is possible but not home |
| no hints, `keep_possible: always` | `valid_US_number`, `possible_GB_number` | possibles from `regions` kept |
| "london" nearby, defaults | `possible_GB_number`, `valid_US_number` | the hint gave only a possible reading, so `regions` is tried too; the GB hint reading is kept whatever `keep_possible` says |
| "london" nearby, `also_try_regions: never` | `possible_GB_number` | the US reading is never tried |

### Before the rule: refining the regex hit

Each regex hit first goes to `PhoneNumberMatcher` (POSSIBLE leniency) in the
top hinted region, else the first of `regions`.
- It trims a greedy hit: `212-555-0123 2026` becomes `212-555-0123`.
- It splits two numbers found in one hit.
- The regex scan resumes after the last number found, so back-to-back
  numbers are all kept.
- If the matcher finds nothing, trailing digit groups are dropped until what
  is left is a valid number (else one possible as a full number), and the
  scan resumes after it. The matcher rejects space-grouped runs as one
  over-long number, so this is what keeps `212 555 0123` from
  `call 212 555 0123 2026`, and both numbers from `212 555 0123 212 555 0124`.
- If nothing is left either (`94105-1234`), the raw hit goes through the rule
  as it is.

The refined numbers are then scored like any other candidate.

### Matches, overlaps, misses

- **Readings of one hit** share its span, score and offsets. They never
  conflict with each other.
- **Other patterns:** each reading competes normally with other patterns
  (e.g. `account_number`), by score, length and priority.
- **`pattern`:** every match carries `pattern`, the label of the pattern it
  came from. For a reading that is the phone pattern (`phone_number`); for
  any other match it equals `label`.
  - `coexist: [[phone_number, account_number]]` covers every reading. A
    generated label works in `coexist` too.
  - Miss rows count a reading, or an `unknown_number`, as a match of its
    pattern.
- **Match shape:** validated matches have the usual `label, text, start, end,
  score` and nothing more. The label gives status and region; matches with the
  same `(start, end)` are readings of one number.
- **`text` is the number in `output_format`** (examples in
  [`validate`](#validate-inside-a-pattern)):
  - Except with `RAW`, `text` is not the input slice; use `start`/`end` to
    highlight or redact.
  - A local-only number (7 digits, no area code) keeps its raw text.
  - The report keeps the written form in `a_text` and always puts E.164 in
    `normalized`, whatever the format. It is blank when there is no E.164
    number (a local-only reading, or a dropped or unknown hit). The source is in the
    validation rows (`decided_by`).
- **Choosing one answer:** if your code needs a single answer per number,
  take the `valid_*` match, or set `keep_possible: never` /
  `when_hints_disagree: nearest`.

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
- **Two space-grouped national numbers in one run.** With a GB hint (or GB
  first in `regions`), `0161 496 0001 0161 496 0002` gives fragments (`0161`,
  `0001`, `0161 4`) as `possible_GB_number`: the matcher splits the run badly instead of
  finding nothing. Separate the numbers (a comma or `and`) or tighten the
  regex.

### Renamed settings

Three `validate` settings were renamed. The old names still load, for now,
and print a warning naming the new key and value; giving both the old and
the new name of one setting is an error.

- `keep` → `keep_possible`: `valid` → `never`, `valid_else_possible` →
  `if_no_valid`, `valid_plus_home` (default) → `home`, `all` → `always`.
- `hint_fallback` → `also_try_regions`: `if_nothing` → `if_hint_fails`,
  `if_not_valid` (default) → `if_hint_not_valid`; `never`, `always` unchanged.
- `hint_conflict` → `when_hints_disagree`: values unchanged.

## Output files

The CLI writes `extract_report.csv` to the working directory only with
`--report`; library callers use the helpers below. The file is gitignored.

- `extract_report.csv`: the decision log, one row per decision. Columns:
  `file, kind, outcome, reason`, the `a_*` and `b_*` candidate columns
  (`label, text, start, end, score, text_score, context_score, pattern`), the
  phone columns `status, region, normalized` (blank except on `validation`
  rows), then `decided_by`. `pattern` is the pattern label (it differs from
  `label` only for phone readings).
  - `validation` rows: one per reading of a phone hit (`a` = the reading):
    - `outcome` is `kept`, or `not_kept` when `keep_possible` /
      `when_hints_disagree` filtered it out, or a hinted possible reading lost
      to a valid hinted one;
    - a hit with no reading gets a single `dropped` row (`status` impossible;
      a `+` number possible only as a local number gets `status` possible),
      or `kept_unknown` instead when `keep_unknown: true` kept it as
      `unknown_number` (`normalized` blank);
    - one hit can mix `hint` and `regions` readings (see `also_try_regions`);
    - `reason` reads e.g. `possible in GB; keep_possible: never; phonenumbers 9.0.43`.
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
| `candidates_dropped` | it matched, but every candidate lost (min_score, overlap or validation) | weights, `min_score` or the `validate` settings |

The `reason` names the context words, e.g.
`context scored phone_number +1.00 (call); no candidates in text`.
A surviving `unknown_number` counts as a match of its phone pattern, so it
suppresses that pattern's miss row; its `kept_unknown` row explains it.
A label the context scored at or below 0 never gets a miss row. Misses the
context gives no hint of can only be caught with labeled examples (the
`tests` section).

## Summaries

`extract.py --summarize [PATH]` prints the overlap table, then (if the report
has any `miss` rows) the miss table, then (if it has `validation` rows) the
validation table. A CSV missing any report column the summaries read is
rejected ("not a CARP report").

### Overlap summary

`summarize_overlaps(path="extract_report.csv")` reads the report's `overlap`
rows and returns one row per pattern pair (a phone reading counts as its pattern, e.g. `phone_number`), so a pair can go straight into `coexist`:
`pair, overlaps, by_score, by_length, by_priority, by_start_label,
not_by_score, not_by_score_pct, no_evidence, wins, examples, hint`.

- Start at the top. `no_evidence` (`by_priority + by_start_label`) counts
  overlaps where nothing in the input decided, only the fallback order did.
- `not_by_score` also counts overlaps settled by `length`, which are often fine.
- `hint` says what to try, and `examples` gives up to three inputs to look at
  (`<file>: "<a_text>" vs "<b_text>"`, fallback-decided ones first).
  - "add nearby, context or keywords words": the fallback order decided.
  - "if both values are valid, add to coexist" (only with the hint above): most unsettled overlaps are partial (different spans),
    which often means two distinct values that may both belong in the output.
  - `ok`: nothing to fix.

### Miss summary

`summarize_misses(path="extract_report.csv")` returns one row per label:
`label, misses, no_candidates, dropped, examples, hint`, most misses first.
`examples` lists up to three `file` ids; `hint` points at the regex when
`no_candidates` dominates, otherwise at the weights.

### Validation summary

`summarize_validation(path="extract_report.csv")` returns one row per phone
pattern (`pattern`):
`pattern, hits, kept, valid, possible, not_kept, dropped, unknown, multi,
sources, regions, clusters, examples, hint`.

- `hits` counts numbers in the text. `kept`, `valid` and `possible` count
  kept readings, and `not_kept` counts the filtered ones. `dropped` counts
  hits with no reading, and `unknown` those kept as `unknown_number`.
- `multi` counts hits with more than one kept reading.
- `clusters` groups `possible` readings by leading digits (the area code for +1).
- `examples` gives up to three multi, dropped or unknown hits.
- Hints:
  - "phonenumbers metadata may be stale, upgrade it": 3+ possible readings
    share leading digits.
  - "add hints, or set keep_possible / when_hints_disagree": some numbers fit several
    regions.
  - "tighten the regex or add regions": most hits were impossible, whether
    dropped or kept as `unknown_number` (local-only `+` numbers don't count).

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

`test_extract.py` runs the `tests` section of the config plus unit tests of
the rules on this page and the API as the README shows it.
