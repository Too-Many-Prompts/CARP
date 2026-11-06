# CARP: Context-Aware Regex Parser

Command-line Python program that extracts regex matches from text, scores
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
   `distance_decay` is true. Words are taken only up to the first `settings.stop_at`
   character (default `""`, none), e.g. `".!?"` keeps the scan in one sentence.
5. `resolve_overlaps(...)`: drop below `min_score`; for overlapping spans keep
   both if the label pair is in `coexist`, else apply `overlap_tiebreak` in
   order (score, length, priority). Identical spans count as overlapping.
6. `extract(text, cfg, source="")` chains 2-5 and returns `(matches, decisions)`; it writes no files. `main()` is an argparse CLI:
   `extract.py --config extraction_config.yaml [--text TEXT | stdin] [--context TEXT] [--report] [--error-log]`
   It scans one text and prints the matches as a JSON list (`label, text, start,
   end, score`); the library is the main interface, so the CLI takes no files.

## Tests
- `test_extract.py` (pytest) loads the `tests` section of the config and checks
  `extract()` returns exactly the expected (label, text) pairs, order-insensitive.
  A test case may include an optional `context` string (and `name`), which is
  passed to `extract()` along with `text`. Tests unpack `matches, _ = extract(...)`.
- A context is matched (whole words/phrases, case-insensitive) against each
  pattern's `context.positive` keywords; only patterns with a hit run on the
  text. No context or no hits: all patterns run. Pattern regexes are never run
  on the context.
- A context that selects patterns but yields no matches is never dropped silently:
  `extract()` returns a `context_miss` decision. The library writes no files;
  with `--error-log`, `main()` writes a row (timestamp, file, context, text,
  context_patterns, reason) to `./errorlog.csv` for each one
  (`log_context_miss()`), overwriting the file.
- The decisions from `extract()` are flat dicts (`REPORT_FIELDS`). with
  `--report`, `main()` writes them as CSV, one row per decision, to
  `./extract_report.csv` (`write_report_csv()` / `open_report()`), overwriting it.
  Library callers may use the same helpers.
- Context keywords are ASCII letters only (matching `words.py`); `load_config`
  rejects digit/accented keywords, duplicate keywords within a pattern and
  wrong-signed weights.
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
