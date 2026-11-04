# Regex extractor

Command-line Python program that extracts regex matches from text files, scores
each match using nearby context words, and resolves overlapping matches.

## Contract
- `extraction_config.yaml` is the source of truth. Keys: `settings`, `normalize`,
  `patterns`, `coexist`, `tests`. Do not invent new keys without asking.
- `words.py` already contains `prev_words()` (finished, commented). Reuse it for
  context lookups; write a mirror `next_words()` for words after a match.

## Architecture (one function per step, no global state)
1. `load_config(path)`: read YAML, compile each regex once. IGNORECASE unless
   `case_sensitive`. Fail with a message naming the label if a regex is invalid.
2. `normalize(text, cfg)`: apply only the enabled `normalize` options.
3. `find_candidates(text, patterns)`: run EACH pattern separately with
   `finditer`. Never combine patterns into one alternation (it hides overlaps).
   Candidate = dataclass(label, start, end, text, priority, score=0).
4. `score_candidate(...)`: take words before/after the match (per
   `settings.context_direction`: `before`, `after` or `both`, default `both`), add each matching
   keyword's weight (phrases allowed), divided by word distance if
   `distance_decay` is true.
5. `resolve_overlaps(...)`: drop below `min_score`; for overlapping spans keep
   both if the label pair is in `coexist`, else apply `overlap_tiebreak` in
   order (score, length, priority). Identical spans count as overlapping.
6. `extract(text, cfg)` chains 2-5. `main()` is an argparse CLI:
   `extract.py --config extraction_config.yaml FILE... [--format json|csv]`
   with output fields `file, label, text, start, end, score`.

## Tests
- `test_extract.py` (pytest) loads the `tests` section of the config and checks
  `extract()` returns exactly the expected (label, text) pairs, order-insensitive.
  A test case may include an optional `context` string (and `name`), which is
  passed to `extract()` along with `text`.
- A context that selects patterns but yields no matches is never dropped silently:
  `extract()` appends a row (timestamp, file, context, text, context_patterns,
  reason) to `./errorlog.csv`. `main()` overwrites that file once per CLI run.
- The decision log from `extract_with_report()` is also written as CSV, one row
  per decision, to `./extract_report.csv` (`write_report_csv()`). `main()`
  overwrites it once per CLI run.
- Also unit-test: coexist overlap, non-coexist overlap, length tie-break,
  multi-word keywords, invalid regex.
- Run `pytest -q` after every change and do not report success until it passes.

## Constraints
- Python 3.10+, standard library + PyYAML + pytest only. Ask before adding
  the third-party `regex` package.
- Type hints throughout; read files as UTF-8 with `errors="replace"`.
- If the config doesn't define a behavior, ask instead of guessing.

## Workflow
Work in small steps: design bullets first, then `extract.py`, then tests, then
fix only what fails. Show complete functions, never "rest unchanged".
