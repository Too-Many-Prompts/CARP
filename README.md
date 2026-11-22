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

CARP finds things in text with regular expressions, then decides which finds
to trust by reading the words around them. Everything it knows lives in one
YAML file, and every decision it makes can be written to a report.

**In one sentence:** CARP finds every possible match, scores each by the words
around it (and in the context you pass), and when two matches claim the same
text, the higher score wins.

**The full rules and the decision flow are in
[REFERENCE.md](REFERENCE.md).** This page is the overview.

## Why CARP

A regex can tell you that `2125550123` is ten digits. It can't tell you
whether those digits are a phone number or an account number, or whether
`2026-03-14 10:00` is a date or a timestamp. The answer is in the words
nearby: `acct` points one way, `call` the other.

CARP adds that missing step:

- **Find broadly, decide by context.** Write simple regexes, even ones that
  overlap on purpose, and list the words that make each one right or wrong.
- **Use outside context when you have it.** A column name, a form field or an
  email subject can be passed as a `context` string. Its words count too.
- **Tune it in YAML, not code.** No model, no training. Change a weight, run
  again, see the result.
- **See why.** Every drop, win and tie-break is logged with the rule that
  decided it, and a summary shows where to tune first.
- **Don't lose things quietly.** Phone numbers can be checked with Google's
  phone metadata, and a number in a range the library doesn't know yet is kept
  as `possible`, not thrown away. Labels the context pointed to that produced
  nothing are flagged as likely misses.

## How to use it

1. **Describe what to find.** One entry per thing: a label, a priority (lower
   wins ties), and a regex. See [YAML Config options](REFERENCE.md#config-options)
2. **Add the words that make it right.** List words that raise the score
   (`call: 2`) and words that lower it (`acct: -3`).
3. **Run it.** From the command line for one text, or from Python for many.
4. **Check why, then tune.** Add `--report`, run `--summarize`, and adjust the
   weights the summary points at.

## How it decides

1. **Normalize** the text: whitespace, dashes, quotes.
2. **Find candidates.** Every regex runs on the text on its own, so
   overlaps stay visible.
3. **Score** each candidate: add the weights of its pattern's words found near
   it in the text, plus those found anywhere in the context. Closer words count
   more; the context's last word counts as the closest.
4. **Drop** candidates below `min_score`.
5. **Resolve overlaps.** If the two labels are listed in `coexist`, keep both.
   Otherwise apply `overlap_tiebreak` in order: score, length, priority.

Phone patterns add a check between steps 3 and 4. A number can come back
with readings for several countries, but at most one per country: readings
are merged by E.164, and if two readings of one country differ, the valid one
wins, then the longer one ([details](REFERENCE.md#phone-specific-labels)).

## Setup

Python 3.10+.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # PyYAML, pytest, phonenumbers
```

## Quick start

### CLI Example:

```bash
.venv/bin/python extract.py --config extraction_config.yaml --report \
    --text 'Invoice INV-004521 dated 03/14/2026, amount due $1,250.00'
echo 'call 212-555-0123' | .venv/bin/python extract.py --config extraction_config.yaml --context phone
.venv/bin/python extract.py --summarize
```

### Calling as a Library:

```python
from extract import load_config, extract

cfg = load_config("extraction_config.yaml")

matches, decisions = extract("some text", cfg)
```

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


### YAML Config

Each option's values are listed in [YAML Config options](REFERENCE.md#config-options). The exact rule,
with every branch, is the [phone decision flow](REFERENCE.md#phone-decision-flow).


## Explaining Results and Decisions

**When a result surprises you, read its row in the report.** That is the normal
way to tune CARP: run, look at the row that explains the surprise, change a
weight, run again.

Add `--report` to the CLI, or write `decisions` from the library. That
produces `extract_report.csv` with one row per decision and each score split
into its text and context parts. Then run:

```bash
.venv/bin/python extract.py --summarize
```

It prints up to three tables:

- **Overlaps worth tuning:** pairs of patterns that fight over the same text
  without the score deciding.
- **Likely misses:** labels the context pointed to that produced no match.
- **Phone checks:** statuses, regions, numbers with several readings, and
  possible numbers sharing an area code (a sign that phonenumbers' data is
  stale).

See [CLI](REFERENCE.md#cli), [Library](REFERENCE.md#library),
[Reading the report](REFERENCE.md#reading-the-report) and
[Summaries](REFERENCE.md#summaries).

## Tests

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider
```

## Files

| File | Role |
| --- | --- |
| `extract.py` | the extractor and CLI |
| `summarize.py` | `summarize_overlaps()` / `summarize_misses()` / `summarize_validation()` and their `format_*()` tables: the summaries of a report CSV (re-exported by `extract.py`) |
| `phone.py` | phone validation for `validate:` patterns (phonenumbers; imported only when used) |
| `words.py` | `prev_words()` / `next_words()` word lookups around a match |
| `extraction_config.yaml` | settings, patterns, coexist pairs, test cases |
| `test_extract.py` | pytest suite |
| `REFERENCE.md` | the full rules: CLI flags, decision flow, every config option, scoring, keywords, tie-breaks, output files |
| `CLAUDE.md` | project instructions for Claude Code |
