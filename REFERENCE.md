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
  of every pattern that lists them (see [Keyword Parsing Details](#keyword-parsing-details)).
- Prints the matches as a JSON list of `{label, text, start, end, score}`.
  For a pattern with `validate`, `text` is the number in `output_format`
  (E.164 by default), not the input slice (see [Phone Number Parsing Details](#phone-number-parsing-details)).
- Writes nothing else unless asked: `--report` writes the decision log to
  `./extract_report.csv` (overwritten each time).
- `--summarize [PATH]` prints the [overlap, miss and validation summaries](#summaries) of
  a report (default `./extract_report.csv`) and exits. It needs no `--config`
  and cannot be combined with `--text`, `--context` or `--report`.
- Config warnings go to stderr as `warning: ...` and do not change the exit code.
- Exit codes: `0` success; `2` for a config or report error (`error: ...` on
  stderr, nothing printed) or a usage error.

#### CLI Output Example:
  ```json
  {"label": "valid_US_number", "text": "+12125550123", "start": 5, "end": 17, "score": 1.0}
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
- `matches` are the candidates that survived scoring and overlap resolution, see datastructure below.
- `decisions` is the flat decision log. Ignore it, keep it, or write it with the helpers
in [Output files](#output-files).

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
Summarize the report:

```python
from extract import (summarize_overlaps, format_summary, summarize_misses, format_misses,
                     summarize_validation, format_validation)

print(format_summary(summarize_overlaps()))       # the overlap table --summarize prints
print(format_misses(summarize_misses()))          # the miss table printed under it
print(format_validation(summarize_validation()))  # phone validation, printed last
```

#### Matches data structure

  Each entry in `matches` is a `Candidate`:

  | Field | Type | Example | Meaning |
  |---|---|---|---|
  | `label` | str | `valid_US_number` | The pattern label. Phone matches use `{status}_{REGION}_number` or `unknown_number`. |
  | `text` | str | `+12125550123` | The matched text. For a phone match, the number in `output_format`. |
  | `start` | int | `5` | Start offset in the normalized text. |
  | `end` | int | `17` | End offset (exclusive). |
  | `priority` | int | `2` | The pattern's `priority`. |
  | `score` | float | `1.0` | `text_score + context_score`. |
  | `text_score` | float | `1.0` | Keyword score from the words around the match. |
  | `context_score` | float | `0.0` | Keyword score from the context string. |
  | `pattern` | str | `phone_number` | The pattern the match came from. The same as `label` except for phone matches. |
  | `hit` | int | `0` | Readings of one phone hit share this id. `-1` if not a phone match. |
  | `raw` | str | `212-555-0123` | The phone number as written. `""` if not a phone match. |
  | `status` | str | `valid` | `valid`, `possible` or `impossible` for phone matches. `""` otherwise. |

## Config options

Everything lives in `extraction_config.yaml`, in five sections: `settings`,
`normalize`, `patterns`, `coexist` and `tests`. Every option is listed below.
"Required" means `load_config` fails without it; an unknown key or a value of
the wrong type (a quoted `"false"`, a blank `regex:` or `priority:`) fails with
a message naming the section and key.

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
A list; each entry is one thing to find. This is the heart of CARP.

```yaml
  - label: invoice_number
    priority: 1
    regex: '\b(?:INV|INVOICE)[-\s#]?\d{5,8}\b'
    keywords: {invoice: 3, bill: 2, "amount due": 2, ref: 1, phone: -3, tel: -3}
```

| Option | Required / default | Values | What it does |
| --- | --- | --- | --- |
| `label` | required, unique | text, e.g. `phone_number` | The name a match is reported under. For a pattern with `validate`, matches get generated labels (`valid_US_number`) and this name is their `pattern`. A label of that generated form (`{valid,possible}_{REGION}_number`) is an error. |
| `priority` | required | a whole number | Breaks ties in `overlap_tiebreak`: the lower number wins. |
| `regex` | required | a regular expression (quote it in single quotes) | What to find. Each pattern's regex runs on its own, so overlaps stay visible. |
| `keywords` | optional | `{word: weight}` | Words that raise (positive weight) or lower (negative weight) the score, looked for in the text window **and** the context string. |
| `nearby` | optional | `{word: weight}` | Like `keywords`, but looked for in the text window only. |
| `context` | optional | `{word: weight}` | Like `keywords`, but looked for in the context string only. |
| `validate` | optional | a block, below | Checks matches as phone numbers. |

Keywords are whole words matched exactly (see [Keyword Parsing Details](#keyword-parsing-details)). A weight
of 0 is an error, and a word in `keywords` may not also be in `nearby` or
`context`.
- Use `keywords` for most words. Use `nearby` or `context` when a word should
  count in only one place.
- A word in `keywords` may not also be in `nearby` or `context` of the same
  pattern (load error), so there are no precedence rules. The same word in both
  `nearby` and `context` is fine.
- A negative weight lowers the score. Weight 0 is a load error.

### `validate` (inside a pattern)
```yaml
- label: phone_number
  priority: 2
  regex: '\+?\(?\d[\d\s().-]{6,18}\d'   # broad on purpose; tighten it any time
  keywords: {phone: 3, tel: 3, call: 2}
  # nearby: {phone: 3, tel: 3, call: 2}
  # context: {phone: 3, tel: 3, call: 2}
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
| Option | Required / default | Values | What it does |
| --- | --- | --- | --- |
| `with` | required | `phonenumbers` | The checking library. It is the only value. |
| `regions` | required | a list of country codes, e.g. `[US, GB]` | The countries a number is checked in, in order. The first is the home country. |
| `hints` | optional | `{COUNTRY: {word: weight}}`, weights above 0 | Words that point to a country (`GB: {uk: 3, london: 2}`). When one appears near the number or in the context, that country is checked first. The country need not be in `regions`. |
| `keep_possible` | default `home` | `never`, `if_no_valid`, `home`, `always` | Which *possible* readings from `regions` are kept; valid ones always are. `never`: none. `if_no_valid`: every possible one when there is no valid reading (a valid hinted reading counts, with `also_try_regions: always`). `home`: the home country's, even when another one is valid. `always`: every one. Hinted and `+` readings are not filtered by it. |
| `when_hints_disagree` | default `all` | `all`, `nearest` | When several hinted regions give a reading (valid ones if any, otherwise possible ones): `all` keeps every one; `nearest` keeps only the region whose hint word is nearest. A word in the text window is nearer than any context word, then the higher hint score wins, and an exact tie keeps both. |
| `also_try_regions` | default `if_hint_not_valid` | `never`, `if_hint_fails`, `if_hint_not_valid`, `always` | After a hint, whether `regions` is parsed too. `never`: never, so a number that fits no hinted region has no reading. `if_hint_fails`: only when the hinted regions gave no reading at all. `if_hint_not_valid`: when no hinted reading is valid. `always`: every time. |
| `output_format` | default `E164` | `E164`, `INTERNATIONAL`, `NATIONAL`, `RAW` | How a match's `text` is written: `+12125550123`, `+1 212-555-0123`, `(212) 555-0123`, or `RAW`, the number exactly as written in the input. |
| `valid_ignores_min_score` | default `true` | `true`, `false` | `true`: a reading the library confirms is valid is never dropped for scoring below `settings.min_score`, so a stray negative word can't erase a real number. It still competes in overlaps, so `Acct 2125550123` can still go to `account_number`. Possible readings always need `min_score`. `false`: valid readings need `min_score` too. |
| `keep_unknown` | default `false` | `true`, `false` | `true`: a hit that fits no region (or that `also_try_regions: never` stopped) is kept as `unknown_number` with its raw text instead of being dropped. It must reach `min_score` and competes in overlaps like any match. |

The full rule and examples are in [Phone Number Parsing Details](#phone-number-parsing-details).

### `coexist`

A list of label pairs, e.g. `- [timestamp, date]`. When two overlapping
matches form a listed pair, both are kept instead of tie-breaking (a
`coexist` row). Order within a pair doesn't matter. A pattern label such as
`phone_number` covers all of its generated labels. Each entry must be a pair
of two different labels, each a pattern label or (when a pattern has
`validate`) a generated label such as `valid_US_number`, or `unknown_number`
when a pattern has `keep_unknown: true`; anything else fails at load time.

## Reports

### Generating Reports

`extract()` writes nothing. With `--report` the CLI writes `extract_report.csv`
(gitignored) to the working directory; library callers use the helpers below.

| Library helper | Does |
| --- | --- |
| `open_report()` | opens the report once for many calls (see the loop in [Library](#library)) |
| `write_report_csv(decisions)` | appends rows |
| `reset_report()` | starts a fresh file |

### Reading the Report

Each row of `extract_report.csv` is one row per decision, columns in this order:

| Columns | Holds | Filled on |
| --- | --- | --- |
| `file` | the `source` passed to `extract()` | every row |
| `kind`, `outcome` | the step and what happened | every row |
| `reason` | readable explanation, e.g. `possible in GB; keep_possible: never; phonenumbers 9.0.43` | every row |
| `a_label, a_text, a_start, a_end, a_score, a_text_score, a_context_score, a_pattern` | the candidate: the winner of an `overlap`, the reading of a `validation` row. `a_text` is the number as written; `pattern` differs from `label` only for phone readings | every row (`miss`: only `a_label`, `a_context_score`) |
| `b_*` (same eight) | the other candidate: the loser of an `overlap` | `overlap`, `coexist` |
| `status, region, normalized` | the reading's status, region and E.164 number (blank for local-only, dropped or unknown hits) | `validation` |
| `decided_by` | the rule that decided | every row (empty on `miss`) |

Decision kinds:

| `kind` | Written when | `outcome` | `decided_by` |
| --- | --- | --- | --- |
| `validation` | a phone hit was checked: one row per reading, or one for a hit with no reading | `kept`, `not_kept`, `dropped`, `kept_unknown` | `number`, `hint`, `regions` |
| `below_min_score` | a candidate scored under `min_score` | `dropped` | `min_score` |
| `overlap` | two candidates overlapped and one lost | `a_wins_b_dropped` | `score`, `length`, `priority`, `start_label` |
| `coexist` | two candidates overlapped but their labels are a coexist pair | `both_kept` | `coexist` |
| `miss` | the context scored a label above 0 and it has no match | `no_candidates`, `candidates_dropped` | (empty) |

A phone hit gets one `validation` row per reading (`kept` / `not_kept`), or one `dropped` / `kept_unknown` row if it has no reading.

Questions the report answers:

| Question | Where to look |
| --- | --- |
| Why did `A` beat `B`? | the `overlap` row: `a_*` is the winner, `b_*` the loser; `reason` shows the numbers |
| Why is my match missing? | `b_` side of an `overlap` row; a `below_min_score` row; a `validation` row with `dropped`; or a `miss` row |
| Why `possible_US_number` and not `valid_US_number`? | the `validation` row: `status`, `region`, and `reason` |
| Why was a phone reading not kept? | a `validation` row with `not_kept`; `reason` names the `keep_possible` or `when_hints_disagree` setting |
| What do I tune first? | `extract.py --summarize` (see [Summaries](#summaries)) |

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
  - "if both values are valid, add to coexist": most unsettled overlaps are partial (different spans),
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

### Tuning workflow

1. Run your inputs with the report on (`--report`, or `open_report()` in a loop).
2. `extract.py --summarize`.
3. Fix the pairs and labels at the top (keywords, `coexist`, regexes, as the
   hints say), then repeat.

## Keyword Parsing Details

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

## Phone Number Parsing Details

A pattern with a `validate:` block checks its matches with the
[phonenumbers](https://github.com/daviddrysdale/python-phonenumbers) library
(Google's libphonenumber metadata). Its regex finds anything phone-shaped
(recall), and validation sorts what it found (precision). CARP does not guess
the country: **every plausible reading of a number comes back as its own
match**, and every impossible hit is logged in the report. Patterns without
`validate` work exactly as before, and configs without it never import the
library.

Each phone-shaped hit is read in one or more regions. Each reading is graded:
- **valid**: a real number in that region;
- **possible**: the right length, but the range isn't known (e.g. a new
  area code);
- **impossible**: it fits nowhere.

**Where to look, in order:**
1. A `+` number's country code decides. Nothing else is consulted.
2. Otherwise, regions named by **hint words** near the number are tried first.
3. Then the **`regions` list**, if `also_try_regions` allows it.

**What comes back:** valid readings always. `keep_possible` decides
possible readings from the `regions` list. Impossible hits are dropped and
logged, unless `keep_unknown: true` keeps them as `unknown_number`.

### Phone-Specific Labels

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

### Notes:

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

### Worked example: one number, three setups

`212-555-0123` is valid in the US and possible in GB, with `regions: [US, GB]`:

| Setup | Matches | Why |
| --- | --- | --- |
| no hints, `keep_possible: home` | `valid_US_number` | GB is possible but not home |
| no hints, `keep_possible: always` | `valid_US_number`, `possible_GB_number` | possibles from `regions` kept |
| "london" nearby, defaults | `possible_GB_number`, `valid_US_number` | the hint gave only a possible reading, so `regions` is tried too; the GB hint reading is kept whatever `keep_possible` says |

## Limits

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
- **Two space-grouped national numbers in one run, with a hint.** With a GB
  hint, `0161 496 0001 0161 496 0002` gives 4-digit fragments (`0161`,
  `0001`) as `possible_GB_number`: the matcher splits the run badly instead of
  finding nothing. Separate the numbers (a comma or `and`) or tighten the
  regex.

## Detailed Decision Logic

What happens to every piece of text, and which report row each decision
leaves. Read the steps top to bottom.

### From text to matches

Each candidate goes through these steps in order. The tag after an arrow is the
report row the step writes.

1. **Normalize.** Text and context: whitespace, dashes, quotes (only the
   options you enabled).
2. **Run every pattern's regex on its own.**
   - **no hit** → nothing to decide for that pattern.
3. **Is it a phone pattern (has `validate`)?**
   - **yes** → trim or split the hit with PhoneNumberMatcher. If it finds
     nothing, the raw hit stays.
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
7. **Overlaps a candidate already accepted?** Candidates are taken best first,
   in `overlap_tiebreak` order.
   - **no** → the candidate is kept. → *no row*
   - **yes** → go to step 8.
8. **Coexist pair, or readings of one phone number?**
   - **yes** → both are kept. Readings of one number never compete with each
     other. → `row: coexist` (pairs only)
   - **no** → run the [tie-break chain](#tie-break-chain). The loser is
     dropped. → `row: overlap`
9. **Misses, at the end.** Each label the context scored above 0 that has no
   match gets a `miss` row.

Notes:

- **Scoring comes before the phone check**, so a phone reading keeps the score
  of the hit it came from.
- **Candidates are taken best first**, in `overlap_tiebreak` order, and each
  is compared with the ones already accepted. That is why the loser is always
  the lower-ranked one.
- **A candidate that survives with no overlap writes no row.** Rows record
  decisions; nothing was decided.
- **Misses** are explained in [Misses](#misses).

### Phone decision flow

For one phone-shaped hit in a pattern with a `validate:` block. The result is
zero or more readings, and each reading becomes a match.

1. **Does the number start with `+`?**
   - **yes, possible** → parse it with its own country code: one reading,
     source `number`. **Done.**
   - **yes, not possible** (or too short to be a full number, which a
     country code rules out) → no reading: the hit is dropped, or kept as
     `unknown_number` with `keep_unknown: true`.
     → `row: validation, dropped` (or `kept_unknown`)
   - **no** → go to step 2.
2. **Are there hint words for any region?**
   - **no** → go to step 5.
   - **yes** → parse in the hinted regions: valid readings, else possible
     ones. `when_hints_disagree` (`all` or `nearest`) picks among regions.
3. **Did any hinted region give a reading?**
   - **none** → with `also_try_regions: never` there is no reading (dropped,
     or `unknown_number` with `keep_unknown`); otherwise go to step 5.
   - **some** → go to step 4.
4. **What does `also_try_regions` say?**
   - **`never`, `if_hint_fails`** → keep the hinted readings only, source
     `hint`. **Done.**
   - **`if_hint_not_valid`** → is any hinted reading valid? Yes: keep the
     hinted readings only (**done**). No: go to step 5.
   - **`always`** → go to step 5. The hinted readings stay.
5. **Parse in each region of `regions`.** Keep what `keep_possible` allows,
   and add the readings you already have from the hints, if any.
   - **some readings** → keep them, sources `hint` and `regions`. **Done.**
   - **none** → go to step 6.
6. **Does it start with `00`?**
   - **yes, and possible when `00` is read as `+`** → one reading, source
     `number`. **Done.**
   - **otherwise** → no reading: the hit is dropped, with the reason in the
     report, or kept as `unknown_number` with `keep_unknown: true`.
     → `row: validation, dropped` (or `kept_unknown`)

notes:

- **Never a guess about the country.** Every reading that survives is
  kept, labeled with its status and region (`valid_US_number`,
  `possible_GB_number`).
- **Hints narrow the search first.** A hint word is a reason to look at a
  region, not a verdict. `also_try_regions` says when the `regions` list gets
  a turn too.
- **Dropped means logged.** Every dropped hit has a `validation` row with the
  reason, such as `impossible in US (TOO_SHORT)`. So does a hit kept as
  `unknown_number` (outcome `kept_unknown`).
- **Several readings of one number** share its span and score and never
  compete with each other. Each competes with other patterns normally.

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

Notes:

An empty `overlap_tiebreak` goes straight to `start_label`. A `start_label` result in the summary means nothing
in your input decided it, which is the cue to add keywords.

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

What changes the outcome:

- **No context:** `account_number` wins 3.0 to 0.0. Both validation rows are
  still written; the phone reading is the loser in the overlap row.
- **`keep_possible: always`:** `possible_GB_number` (`+442125550123`) and
  `valid_US_number` both come back on the span, score 3.0 each. They do not
  compete with each other, and both beat `account_number`.
- **A GB hint** (`hints: {GB: {london: 2}}`, text `Acct 2125550123 london
  office`): GB is parsed first, and the number is only possible there. With the
  default `also_try_regions: if_hint_not_valid` the regions are parsed too, so
  both readings come back; the reason says `hint GB not valid;
  also_try_regions: if_hint_not_valid`.
- **The same hint with `also_try_regions: never`:** only `possible_GB_number`
  comes back. The US reading is never tried.
- **`acct` does not lower the phone score,** because the phone pattern lists
  `account`, not `acct`. Keywords are exact words; add `acct: -2` to the phone
  keywords to make it count.

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

`test_extract.py` runs the `tests` section of the config and unit-tests coexist
and non-coexist overlaps, tie-breaks (including an empty list), text and
context scoring (decay, phrases, no window on the context, text/context
parity), `keywords` reaching both sources, `context_direction`, `stop_at`,
keyword validation, `decided_by`, `miss` rows, the
report CSV and its score breakdown, the summaries, the CLI, and the public API
as shown in the README and this reference. Phone tests cover the `validate`
load errors, the renamed-setting aliases and the warnings for settings that
do nothing, each status, every `keep_possible`, `when_hints_disagree`
and `also_try_regions` mode, `keep_unknown`, hints (including a hint that does not fit), `+`, `00` and
`011`, span refinement and back-to-back numbers, readings competing with
other patterns, `coexist` and misses through `pattern`, every `output_format`, the report
rows and the validation summary.