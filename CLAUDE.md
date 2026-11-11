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
   text_score=0, context_score=0, pattern="", hit=-1, raw="");
   `score = text_score + context_score`; `pattern` is filled from `label`
   when blank (`__post_init__`), and differs only for phone readings.
   A pattern with `validate` is scanned by `_find_validated()`: each regex hit
   is refined by `phone.refine_spans()` (PhoneNumberMatcher, POSSIBLE); if
   that finds nothing, `phone.trim_to_number()` drops trailing digit groups
   until a valid (else full possible) number is left (the matcher rejects
   space-grouped runs); failing both, the raw hit is kept. The scan resumes
   after the last refined number.
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
   before step 5: `phone.readings_for()` returns every reading of the hit
   (no tie-breaking between regions). The steps, first that applies:
   - `+` number (or `00` as a last resort);
   - the hinted regions (valid readings, else possible ones;
     `hint_conflict` all | nearest);
   - the `regions` list, filtered by `keep`. After a hint, it runs per
     `hint_fallback`: if_not_valid (default; no valid hinted reading),
     if_nothing (no hinted reading), always, never. Its readings are added
     (source `regions`); each reading keeps its own source.
   Each returned reading becomes a match labeled `{status}_{REGION}_number`,
   with `pattern` = the pattern label, a shared `hit` id, `text` = the
   number in `output_format` (E164 | INTERNATIONAL | NATIONAL | RAW; RAW keeps
   the input slice) and `raw` = the written form (report `a_text` only; never
   in `to_dict()`). The report's `normalized` column is always E.164.
   Validation rows: one per reading (kept / not_kept), or one `dropped`
   row when the hit is impossible everywhere.
5. `resolve_overlaps(...)`: drop below `min_score` (valid phone readings are
   exempt when `validate.valid_ignores_min_score`, the default; internal
   `Candidate.status`); for overlapping spans keep
   both if the label pair or the `pattern` pair is in `coexist` (readings of
   one hit never conflict), else apply `overlap_tiebreak` in
   order (`score`, `length`, `priority`). Empty list or full tie: earlier
   start, then label. Identical spans count as overlapping.
6. `extract(text, cfg, source="")` chains 2-5 and returns `(matches, decisions)`; it writes no files. `main()` is an argparse CLI:
   `extract.py --config extraction_config.yaml [--text TEXT | stdin] [--context TEXT] [--report]`
   It scans one text and prints the matches as a JSON list (`label, text, start,
   end, score`, the same for validated matches); the library is the main interface, so the CLI takes no files.
   Load warnings go to stderr as `warning: ...` (exit code unchanged).
7. `summarize_overlaps(path=REPORT_PATH)` (in `summarize.py`, a leaf module
   that imports nothing from `extract.py`; `extract.py` re-exports it) reads a report CSV's `overlap` rows
   and summarizes them per pattern pair (`a_pattern`) (counts by `decided_by`, `no_evidence`,
   examples, hint); `summarize_misses(path)` counts `miss` rows per label
   (no candidates / dropped); `summarize_validation(path)` counts `validation`
   rows per pattern (statuses, sources, not_kept, multi-reading
   hits, possible-number area-code clusters). CLI: `extract.py --summarize [PATH]` prints all three
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
- Only the current config format is supported (single user): no checks or
  messages for earlier formats. Unknown keys and values fail with the list of
  allowed ones.

## Phone validation (`validate:` block)
- Keys:
  - `with: phonenumbers` and `regions: [US, ...]` (required; the first is
    the home region);
  - `hints` ({REGION: {word: weight > 0}}, read like `keywords`);
  - `keep` (valid_plus_home | all | valid_else_possible | valid): which
    readings of the `regions` list come back;
  - `hint_conflict` (all | nearest);
  - `hint_fallback` (if_not_valid | if_nothing | always | never);
  - `output_format` (E164 | INTERNATIONAL | NATIONAL | RAW): the match `text`;
  - `valid_ignores_min_score` (bool, default true).

  An unknown key or value fails, naming the allowed ones.
- Never lose a hit silently: every validated regex hit ends up as matches or
  a `dropped` row with a reason (which includes the phonenumbers version).
- `coexist`, miss rows and `summarize_validation` use
  `Candidate.pattern`, so rules naming the pattern label cover every reading.
- Regions come only from the YAML (`regions`, `hints`, a `+` in the number);
  there is no per-call region argument or CLI flag.

## Decisions and report
- The decisions from `extract()` are flat dicts (`REPORT_FIELDS`, ending in
  `decided_by`). Kinds: `below_min_score`, `overlap`, `coexist`, `miss`,
  `validation` (`decided_by` = source: number | hint | regions; columns
  `status`, `region`, `normalized` sit just before `decided_by`, blank on other
  kinds; candidate columns end in `pattern`).
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
  empty `overlap_tiebreak`, multi-word keywords, invalid regex,
  context scoring (decay from the right, no window, phrases),
  `keywords` reaching both sources, `nearby`-only and `context`-only words,
  `miss` rows, the score breakdown, every `decided_by` value,
  `summarize_overlaps`, `summarize_misses` and `--summarize`, and the public
  API as the README shows it. Phone: `validate` load errors,
  each status, every `keep`, `hint_conflict` and `hint_fallback` mode, hints
  (including misfits), `+`/`00`/`011`, span refinement (incl. space-grouped trimming),
  `valid_ignores_min_score`, and back-to-back
  numbers, readings vs other patterns, coexist/misses via `pattern`, every `output_format`,
  report rows, `summarize_validation`.
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
