# CARP: Context-Aware Regex Parser

Command-line Python program that extracts regex matches from text, scores
each match using nearby words in the text and words in an optional context
string, and resolves overlapping matches.

## Contract
- `extraction_config.yaml` is the source of truth. Keys: `settings`, `normalize`,
  `patterns`, `coexist`, `tests`. Do not invent new keys without asking.
- `words.py` contains `prev_words()` and `next_words()` (finished, commented).
  Reuse them for every word lookup (text window and context string).
- `phone.py` is a leaf module (imports nothing from `extract.py`) for patterns
  with a `validate:` block. It imports `phonenumbers` lazily, so configs
  without `validate` never need it. It never scores words: `extract.py`
  computes region hints with the shared matcher and passes them in.

## Architecture (one function per step, no global state)
1. `load_config(path)`: read YAML, compile each regex once. IGNORECASE unless
   `case_sensitive`. Fail with a message naming the label if a regex is invalid.
2. `normalize(text, cfg)`: apply only the enabled `normalize` options.
3. `find_candidates(text, patterns)`: run EACH pattern separately with
   `finditer`. Never combine patterns into one alternation (it hides overlaps).
   Every pattern always runs on the text; the context never selects patterns.
   Candidate = dataclass(label, start, end, text, priority, score=0,
   text_score=0, context_score=0, extra=None); `score = text_score + context_score`.
   A pattern with `validate` is scanned by `_find_validated()`: each regex hit
   is refined by `phone.refine_spans()` (PhoneNumberMatcher, POSSIBLE), the
   raw hit is kept if it finds nothing, and the scan resumes after the last
   refined number.
4. Scoring. One score per candidate, from two independent sums that are added:
   - `score_candidate(...)` (text score): take words before/after the match (per
     `settings.context_direction`: `before`, `after` or `both`, default `both`),
     up to `context_window_words` per side, and only up to the first
     `settings.stop_at` character (default `""`, none). Add each matching
     keyword's weight (phrases allowed), divided by word distance if
     `distance_decay` is true. Nearest occurrence only; each keyword counts once.
   - `context_score(...)`: the same rules on the whole context string: all
     words are read (no window, no `context_direction`, no `stop_at`), and the
     context's LAST word is distance 1 (a phrase's last word sets its distance).
     It depends only on the pattern, so `extract()` computes it once per pattern.
   Both go through one keyword matcher (`_sum_keywords`); keep it that way so
   they cannot drift apart. A `keywords` word found in both places counts in both.
4b. `validate_candidates(...)` (only `validate` patterns), after scoring and
   before step 5: `phone.pick_region()` gives status (valid / possible /
   impossible) and region (source: number, caller, keyword, proximity, trial,
   format, fallback, default; first that decides wins; a caller/keyword
   region where the number is only possible yields to a valid trial), then
   `on_invalid` / `on_possible` / `on_ambiguous` keep, flag or drop it.
   Non-valid kept numbers get `possible_label` if set. Kept ones get `extra`
   {normalized, region, region_source, status, number_type, ambiguous,
   runner_up}. One `validation` decision per candidate.
5. `resolve_overlaps(...)`: drop below `min_score`; for overlapping spans keep
   both if the label pair is in `coexist`, else apply `overlap_tiebreak` in
   order (`score`, `length`, `priority`). Empty list or full tie: earlier
   start, then label. Identical spans count as overlapping.
6. `extract(text, cfg, source="", region=None)` chains 2-5 and returns `(matches, decisions)`; it writes no files. `main()` is an argparse CLI:
   `extract.py --config extraction_config.yaml [--text TEXT | stdin] [--context TEXT] [--region CODE] [--report]`
   It scans one text and prints the matches as a JSON list (`label, text, start,
   end, score`, plus `extra` for validated matches); the library is the main interface, so the CLI takes no files.
   Load warnings go to stderr as `warning: ...` (exit code unchanged).
7. `summarize_overlaps(path=REPORT_PATH)` (in `summarize.py`, a leaf module
   that imports nothing from `extract.py`; `extract.py` re-exports it) reads a report CSV's `overlap` rows
   and summarizes them per label pair (counts by `decided_by`, `no_evidence`,
   examples, hint); `summarize_misses(path)` counts `miss` rows per label
   (no candidates / dropped); `summarize_validation(path)` counts `validation`
   rows per label (statuses, sources, ambiguous, possible-number area-code
   clusters). CLI: `extract.py --summarize [PATH]` prints all three
   (no `--config`; not combinable with the scan flags).

## Keywords
- Three keys per pattern, all flat `word: signed weight` maps:
  `keywords` (text window AND context), `nearby` (text window only),
  `context` (context string only). A `keywords` word may not also be in
  `nearby` or `context`; the same word in both `nearby` and `context` is fine.
  Weight 0 is an error.
- Keywords are ASCII letters only (matching `words.py`); `load_config` rejects
  digit/accented keywords, duplicate keywords within a key (also spelled
  differently, e.g. "amount-due" / "amount due") and non-numeric weights.
- Old formats fail to load with a message naming the migration: `context`
  with `positive`/`penalize`/`negative` sub-keys, and `context_penalty` or
  `context_negative` in `overlap_tiebreak`.

## Phone validation (`validate:` block)
- Keys: `with: phonenumbers` and `default_region` (both required);
  `try_regions`, `region_from`, `regions` ({REGION: {word: weight > 0}}),
  `possible_label`, `on_invalid` (drop), `on_possible` (keep),
  `possible_min_score` (1, total score), `on_ambiguous` (first),
  `output_format` (E164). The defaults are in brackets. Unknown keys,
  regions or values fail to load.
- Never lose a hit silently: every validated regex hit ends up as a match or
  a `validation` row with a reason (which includes the phonenumbers version).
- `coexist` pairs naming `label` also cover `possible_label`. A
  `possible_label` match counts as found for miss rows.

## Decisions and report
- The decisions from `extract()` are flat dicts (`REPORT_FIELDS`, ending in
  `decided_by`). Kinds: `below_min_score`, `overlap`, `coexist`, `miss`,
  `validation` (`decided_by` = region source; columns `status`, `region`,
  `normalized` sit just before `decided_by`, blank on other kinds).
  `decided_by` is set from the deciding criterion (`_why_wins` returns
  `(criterion, reason)`), never parsed from `reason`. Candidate columns
  include the `text_score` / `context_score` breakdown.
- `miss` (false-negative hint): one row per label whose context score is
  > 0 but that has no surviving match; the reason says "no candidates in text"
  (regex gap) or "N candidate(s) dropped" (tuning).
- With `--report`, `main()` writes the decisions as CSV, one row per decision,
  to `./extract_report.csv` (`write_report_csv()` / `open_report()`),
  overwriting it. Library callers may use the same helpers.

## Tests
- `test_extract.py` (pytest) loads the `tests` section of the config and checks
  `extract()` returns exactly the expected (label, text) pairs, order-insensitive.
  A test case may include an optional `context` string (and `name`), which is
  passed to `extract()` along with `text`. Tests unpack `matches, _ = extract(...)`.
- Also unit-test: coexist overlap, non-coexist overlap, length tie-break,
  empty `overlap_tiebreak`, multi-word keywords, invalid regex, old-format
  errors, context scoring (decay from the right, no window, phrases),
  `keywords` reaching both sources, `nearby`-only and `context`-only words,
  `miss` rows, the score breakdown, every `decided_by` value,
  `summarize_overlaps`, `summarize_misses` and `--summarize`, and the public
  API as the README shows it. Phone: `validate` load errors, each status and
  `on_*`, every region source, span refinement and back-to-back numbers,
  `possible_label` with coexist and misses, `extra`, report columns,
  `summarize_validation`.
- Run `pytest -q` after every change and do not report success until it passes.

## Constraints
- Python 3.10+, standard library + PyYAML + pytest + phonenumbers
  (approved for `validate`) only. Ask before adding the third-party `regex`
  package.
- Type hints throughout; read files as UTF-8 with `errors="replace"`.
- If the config doesn't define a behavior, ask instead of guessing.

## Workflow
Work in small steps: design bullets first, then `extract.py`, then tests, then
fix only what fails. Show complete functions, never "rest unchanged".
