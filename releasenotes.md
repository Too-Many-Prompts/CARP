# CARP release notes (for programs that call the library)

What a program calling `extract.py` / `summarize.py` must look out for. Config
(YAML) details are in `docs/REFERENCE.md`; this file lists only effects on
calling code. CARP has no version numbers, so entries are named by branch and
merge commit. Newest first.

**Unchanged in every entry:** `load_config(path)`, `extract(inp, cfg, source="")`
and its `(matches, decisions)` return shape, the accepted inputs (`str`,
`{"text", "context"}`, `(context, text)`), `write_report_csv`, `open_report`,
`reset_report`, and the CLI flags `--config --text --context --report --summarize`.

---

## phone-validation (not merged yet)

Affects you if your config has a `validate:` block. The shipped
`extraction_config.yaml` now has one on `phone_number`. Items marked **all** hit
every caller.

1. **Labels of validated patterns are generated.** `m.label` is
   `{status}_{REGION}_number` (`valid_US_number`, `possible_GB_number`), never
   `phone_number`.
   - Look for: `m.label == "phone_number"`, label sets, dicts keyed by label.
   - Do: use `m.pattern` (new field, the pattern label `"phone_number"`; equal
     to `m.label` for every other match) to identify the pattern. Status and region are in the label:
     `status, region = m.label.split("_")[:2]` (`valid` | `possible`, `US`).
2. **One number can return several matches on the same span** (one per plausible
   reading, e.g. `possible_US_number` and `valid_GB_number`).
   - Look for: code assuming one match per `(start, end)` or per `text`.
   - Do: group by `(m.start, m.end)` (the group size is the reading count), or
     keep labels starting `valid_`.
3. **`m.text` of a validated match is the normalized number** (`+12125550123`),
   not the input slice. The match shape is unchanged (`label, text, start, end,
   score`; no new keys in `to_dict()` or the CLI JSON).
   - Look for: code that slices, highlights, redacts or re-finds `m.text` in the
     input, or compares it to the input. Do: use `input[m.start:m.end]`
     (offsets still point at the number as written), or set
     `output_format: RAW` in the pattern's `validate` block to keep `text` as
     the input slice. `INTERNATIONAL` and `NATIONAL` are also available.
4. **Phone results differ from before:** international forms (`+44 …`) are now
   found, and a hit impossible in every configured region is dropped (and
   logged). Re-record golden outputs. `555-xxx-xxxx` is `possible`, not `valid`.
5. **(all) Report CSV has new columns:** `a_pattern`, `b_pattern`, `status`,
   `region`, `normalized`. Column-name readers are fine; position-based readers
   break.
   - **Appending to an old report file misaligns it:** `open_report()` writes the
     header only for a new or empty file. Call `reset_report()` once, or use a
     new path.
6. **(all, if you read `decisions`) New decision kind `validation`** with outcomes
   `kept` / `not_kept` / `dropped`; `decided_by` is `number | hint | regions`.
   Ignore kinds you don't know. Only `validate` configs produce them.
7. **(all) `miss` reason text** now ends `(min_score/overlap/validation)`. Don't
   parse `reason`; use `outcome` (`no_candidates` | `candidates_dropped`).
8. **(all) `--summarize` output** has a third table when the report has
   `validation` rows. New importable functions: `summarize_validation`,
   `format_validation`.

No effect on callers: `Candidate` gained `pattern`, `hit`, `raw` after its
existing fields (all defaulted), so existing construction still works;
`phonenumbers` is imported only when a config uses `validate` (install
`phonenumbers>=9.0` only then); there is deliberately no per-call region argument.

**Opt out:** remove the `validate:` block and restore the old regex from
`git show main:extraction_config.yaml`. Items 1-4 then disappear; 5-8 still apply
to the report format.

---

## context-scoring (merged, `9e841c1`)

1. **Old configs fail to load** with a `ValueError` naming the fix:
   `context: {positive|penalize|negative: <list or map>}`, and `context_penalty`
   or `context_negative` in `overlap_tiebreak`. Now `context: {word: weight}`
   adds to the score; `overlap_tiebreak` takes `score`, `length`, `priority`.
2. **A context no longer excludes patterns** (every pattern always runs;
   `score = text_score + context_score`). Matches that overlap nothing are kept
   whatever the context. Re-check anything that relied on the context filtering
   results.
3. **Removed:** `--error-log`, `errorlog.csv`, `log_context_miss`,
   `reset_error_log`, `ERROR_LOG_*`, decision kind `context`. Replacement:
   `miss` decisions in the normal `decisions` list and report.
4. **Report columns:** removed `penalty_skipped`, `patterns`; added
   `a_/b_text_score` and `a_/b_context_score`. Same appending caveat as above:
   `reset_report()` first.
5. `Candidate` gained `text_score` and `context_score` (`score` is their sum).
   CLI JSON is unchanged (`label, text, start, end, score`).
