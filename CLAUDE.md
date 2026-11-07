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
   `nearby` keyword's weight (phrases allowed), divided by word distance if
   `distance_decay` is true. Words are taken only up to the first `settings.stop_at`
   character (default `""`, none), e.g. `".!?"` keeps the scan in one sentence.
   `nearby` never reads the context string.
5. `resolve_overlaps(...)`: drop below `min_score`; for overlapping spans keep
   both if the label pair is in `coexist`, else apply `overlap_tiebreak` in
   order (`score`, `context_penalty`, `length`, `priority`). `context_penalty`:
   the sum of the pattern's `context.penalize` weights found in the context
   string, closer to 0 wins, only when that context selected the patterns.
   Empty list or full tie: earlier start, then label. Identical spans count as overlapping.
   `nearby`, `context_penalty` and pattern selection share one keyword matcher
   (`_sum_keywords`); keep it that way so they cannot drift apart.
6. `extract(text, cfg, source="")` chains 2-5 and returns `(matches, decisions)`; it writes no files. `main()` is an argparse CLI:
   `extract.py --config extraction_config.yaml [--text TEXT | stdin] [--context TEXT] [--report] [--error-log]`
   It scans one text and prints the matches as a JSON list (`label, text, start,
   end, score`); the library is the main interface, so the CLI takes no files.
   Load warnings go to stderr as `warning: ...` (exit code unchanged).
7. `summarize_overlaps(path=REPORT_PATH)` reads a report CSV's `overlap` rows
   and summarizes them per label pair (counts by `decided_by`, `no_evidence`,
   examples, hint). CLI: `extract.py --summarize [PATH]` (no `--config`; not
   combinable with the scan flags).

## Tests
- `test_extract.py` (pytest) loads the `tests` section of the config and checks
  `extract()` returns exactly the expected (label, text) pairs, order-insensitive.
  A test case may include an optional `context` string (and `name`), which is
  passed to `extract()` along with `text`. Tests unpack `matches, _ = extract(...)`.
- Pattern keywords: `context.positive` (word list; selects the pattern from
  the context string), `context.penalize` (word -> weight <= 0; only the
  `context_penalty` tie-break), `nearby` (word -> signed weight; scores the
  text window only). Old `context.positive` maps, `context.negative` and
  `context_negative` fail to load with a message naming the rename.
- A context is matched (whole words/phrases, case-insensitive) against each
  pattern's `context.positive` words; only patterns with a hit run on the
  text. No context or no hits: all patterns run. Pattern regexes are never run
  on the context.
- `load_config` warns (UserWarning) when `penalize` is set but
  `context_penalty` is not in `overlap_tiebreak`.
- A context that selects patterns but yields no matches is never dropped silently:
  `extract()` returns a `context_miss` decision. The library writes no files;
  with `--error-log`, `main()` writes a row (timestamp, file, context, text,
  context_patterns, reason) to `./errorlog.csv` for each one
  (`log_context_miss()`), overwriting the file.
- The decisions from `extract()` are flat dicts (`REPORT_FIELDS`, ending in
  `decided_by` and `penalty_skipped`). `decided_by` is set from the deciding
  criterion (`_why_wins` returns `(criterion, reason)`), never parsed from
  `reason`. An overlap where `context_penalty` was skipped (context given but
  selected nothing, and it has penalize words of either candidate) gets a
  reason note and `penalty_skipped=True`; it never changes results. With
  `--report`, `main()` writes them as CSV, one row per decision, to
  `./extract_report.csv` (`write_report_csv()` / `open_report()`), overwriting it.
  Library callers may use the same helpers.
- Keywords are ASCII letters only (matching `words.py`; the context string is
  split with `next_words` too); `load_config` rejects digit/accented keywords,
  duplicate keywords within a key, a word in both `positive` and `penalize`,
  non-numeric weights and positive `penalize` weights.
- Also unit-test: coexist overlap, non-coexist overlap, length tie-break,
  `context_penalty` tie-break (and its context gate), `nearby`/`context_penalty`
  parity, empty `overlap_tiebreak`, multi-word keywords, invalid regex,
  old-format errors, the load warning, the skipped note, every `decided_by`
  value, `summarize_overlaps` and `--summarize`, and the public API as the
  README shows it.
- Run `pytest -q` after every change and do not report success until it passes.

## Constraints
- Python 3.10+, standard library + PyYAML + pytest only. Ask before adding
  the third-party `regex` package.
- Type hints throughout; read files as UTF-8 with `errors="replace"`.
- If the config doesn't define a behavior, ask instead of guessing.

## Workflow
Work in small steps: design bullets first, then `extract.py`, then tests, then
fix only what fails. Show complete functions, never "rest unchanged".
