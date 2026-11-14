"""Phone number validation for patterns with a `validate:` block.

A leaf module, like summarize.py: it imports nothing from extract.py, and
imports the `phonenumbers` library only when a config uses `validate`, so
configs without it never need the library. extract.py does all word
scoring and hands the region hints in (RegionHint); this module never reads
words, so there is no second copy of the keyword matcher.

Each phone-shaped hit is read in one or more regions. Each reading is graded:
  - valid: a real number in that region;
  - possible: the right length, but the range isn't known (e.g. a new area code);
  - impossible: it fits nowhere.

Where to look, in order (readings_for):
  1. A "+" number's country code decides. Nothing else is consulted.
     (A leading "00" is read as "+" when nothing else fits.)
  2. Otherwise, regions named by hint words near the number are tried first:
     valid readings, else possible ones; when_hints_disagree: all | nearest.
  3. Then the `regions` list, if also_try_regions allows it: never,
     if_hint_fails (no hinted reading), if_hint_not_valid (no valid hinted
     reading; default) or always.

What comes back: valid readings always. keep_possible decides possible
readings from the `regions` list (never | if_no_valid | home | always).
Impossible hits have no reading: extract.py drops and logs them, unless
keep_unknown: true keeps them as `unknown_number`.

Every reading becomes its own match labeled `{status}_{REGION}_number`; it
never guesses one country.

    spec = load_validate(label, raw_block, weight_map)     # at load time
    spans = refine_spans(span_text, region)                # trim / split a regex hit
    end = trim_to_number(span_text, regions)               # ...when the matcher finds nothing
    out = readings_for(raw, spec, hints)                   # every reading + why
"""
from __future__ import annotations

import importlib
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

VALIDATE_KEYS = ("with", "regions", "hints", "keep_possible", "when_hints_disagree",
                 "also_try_regions", "output_format", "valid_ignores_min_score",
                 "keep_unknown")
CHOICES = {
    # Possible readings from the `regions` list: which come back.
    "keep_possible": ("never", "if_no_valid", "home", "always"),
    # Several hinted regions give a reading: keep all, or the nearest hint's.
    "when_hints_disagree": ("all", "nearest"),
    # After a hint, also parse the `regions` list: from never to always, by
    # how good the hinted result had to be to stop the search.
    "also_try_regions": ("never", "if_hint_fails", "if_hint_not_valid", "always"),
    "output_format": ("E164", "INTERNATIONAL", "NATIONAL", "RAW"),
}
DEFAULTS = {"keep_possible": "home", "when_hints_disagree": "all",
            "also_try_regions": "if_hint_not_valid", "output_format": "E164"}
BOOLS = {"valid_ignores_min_score": True, "keep_unknown": False}

# Temporary aliases: remove with the next config change. Old key ->
# (new key, {old value: new value}); values not listed keep their name.
ALIASES: dict[str, tuple[str, dict[str, str]]] = {
    "keep": ("keep_possible", {"valid": "never", "valid_else_possible": "if_no_valid",
                               "valid_plus_home": "home", "all": "always"}),
    "hint_fallback": ("also_try_regions", {"if_nothing": "if_hint_fails",
                                           "if_not_valid": "if_hint_not_valid"}),
    "hint_conflict": ("when_hints_disagree", {}),
}
STATUSES = ("valid", "possible", "impossible")
SOURCES = ("number", "hint", "regions")   # decided_by of a validation row

_pn_module: Any = None


def pn() -> Any:
    """The phonenumbers module, imported on first use."""
    global _pn_module
    if _pn_module is None:
        _pn_module = importlib.import_module("phonenumbers")
    return _pn_module


def pn_version() -> str:
    return str(getattr(pn(), "__version__", "?"))


def label_for(status: str, region: str | None) -> str:
    """The match label of a reading: valid_US_number, possible_GB_number.
    Numbers that belong to no country (+800, +808, +870...) get region "001",
    phonenumbers' code for them: valid_001_number."""
    return f"{status}_{region or 'ZZ'}_number"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass
class PhoneSpec:
    regions: list[str]
    hints: dict[str, dict[str, float]] = field(default_factory=dict)
    keep_possible: str = "home"
    when_hints_disagree: str = "all"
    also_try_regions: str = "if_hint_not_valid"
    output_format: str = "E164"
    # A valid reading is never dropped for scoring below settings.min_score
    # (it still competes in overlaps).
    valid_ignores_min_score: bool = True
    # A hit with no reading (fits no region tried) becomes an `unknown_number`
    # match instead of being dropped.
    keep_unknown: bool = False


WeightMap = Callable[[str, str, Any], dict[str, float]]


def _region(label: str, key: str, value: Any) -> str:
    where = f"pattern '{label}': validate.{key}"
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected a region code like US, got {value!r}")
    code = value.upper()
    if code not in pn().SUPPORTED_REGIONS:
        raise ValueError(f"{where}: unknown region '{value}' (phonenumbers has no metadata for it)")
    return code


def _renamed(label: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The block with old key names and values (ALIASES) rewritten to the new
    ones, warning once per old key. Old and new form of one key: an error."""
    where = f"pattern '{label}': validate"
    out = dict(raw)
    for old, (new, values) in ALIASES.items():
        if old not in raw:
            continue
        if new in raw:
            raise ValueError(f"{where}: both '{old}' and '{new}' are given; "
                             f"'{old}' is the old name of '{new}', keep only '{new}'")
        value = out.pop(old)
        renamed = values.get(value, value) if isinstance(value, str) else value
        out[new] = renamed
        warnings.warn(f"pattern '{label}': validate.{old} is renamed {new}; "
                      f"{old}: {value} -> {new}: {renamed}", stacklevel=2)
    return out


def _warn_inert(label: str, raw: dict[str, Any], spec: PhoneSpec) -> None:
    """Warn about settings the user wrote that cannot change anything."""
    where = f"pattern '{label}': validate"
    if not spec.hints:
        for key in ("also_try_regions", "when_hints_disagree"):
            if key in raw:
                warnings.warn(f"{where}.{key} has no effect without hints", stacklevel=3)
    # With one region there is at most one list reading, and it is the home
    # one, so if_no_valid, home and always keep the same readings. Except:
    # with also_try_regions: always a valid hinted reading makes if_no_valid
    # drop a possible list reading.
    if ("keep_possible" in raw and spec.keep_possible != "never" and len(spec.regions) == 1
            and not (spec.hints and spec.also_try_regions == "always")):
        warnings.warn(f"{where}.keep_possible: with one region, if_no_valid, home and "
                      "always behave the same", stacklevel=3)


def load_validate(label: str, raw: Any, weight_map: WeightMap) -> PhoneSpec:
    """Check one pattern's `validate` block and return its PhoneSpec.

    `weight_map(label, key_name, raw)` is extract.py's keyword-map checker,
    so hint words follow exactly the same rules as pattern keywords.
    """
    where = f"pattern '{label}': validate"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a map, got {type(raw).__name__}")
    raw = _renamed(label, raw)
    unknown = [k for k in raw if k not in VALIDATE_KEYS]
    if unknown:
        raise ValueError(f"{where}: unknown key '{unknown[0]}' (expected one of {', '.join(VALIDATE_KEYS)})")
    if raw.get("with") != "phonenumbers":
        raise ValueError(f"{where}.with: must be 'phonenumbers', got {raw.get('with')!r}")
    try:
        pn()
    except ImportError as exc:
        raise ValueError(
            f"pattern '{label}' uses validate but phonenumbers is not installed "
            "(pip install phonenumbers)"
        ) from exc

    listed = raw.get("regions")
    if not isinstance(listed, list) or not listed:
        raise ValueError(f"{where}.regions: expected a non-empty list of region codes, e.g. [US]")
    regions: list[str] = []
    for r in listed:
        code = _region(label, "regions", r)
        if code in regions:
            raise ValueError(f"{where}.regions: '{code}' is listed twice")
        regions.append(code)

    opts: dict[str, Any] = {}
    for key, choices in CHOICES.items():
        value = raw.get(key, DEFAULTS[key])
        if value not in choices:
            raise ValueError(f"{where}.{key}: unknown value {value!r} (expected one of {', '.join(choices)})")
        opts[key] = value
    for key, default in BOOLS.items():
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{where}.{key}: expected true or false, got {value!r}")
        opts[key] = value

    hints_raw = raw.get("hints") or {}
    if not isinstance(hints_raw, dict):
        raise ValueError(f"{where}.hints: expected a map of REGION: {{word: weight}}")
    hints: dict[str, dict[str, float]] = {}
    for code_raw, words in hints_raw.items():
        code = _region(label, "hints", code_raw)
        if code in hints:
            raise ValueError(f"{where}.hints: '{code}' is listed twice")
        weights = weight_map(label, f"validate.hints.{code}", words)
        bad = [w for w, v in weights.items() if v <= 0]
        if bad:
            raise ValueError(f"{where}.hints.{code}: keyword '{bad[0]}' needs a weight > 0")
        hints[code] = weights

    spec = PhoneSpec(regions=regions, hints=hints, **opts)
    _warn_inert(label, raw, spec)
    return spec


# --------------------------------------------------------------------------
# Span refinement
# --------------------------------------------------------------------------
def refine_spans(span_text: str, region: str) -> list[tuple[int, int]]:
    """Numbers PhoneNumberMatcher (POSSIBLE leniency) finds inside one regex
    hit, as (start, end) offsets into span_text. It trims a greedy hit
    ("212-555-0123 2026") and splits two numbers in one hit. An empty list
    means the matcher found nothing; the caller then keeps the raw span, so
    no hit is lost here."""
    lib = pn()
    matcher = lib.PhoneNumberMatcher(span_text, region, leniency=lib.Leniency.POSSIBLE)
    return [(m.start, m.end) for m in matcher]


_GROUP = re.compile(r"\+?\(?\d+\)?")


def trim_to_number(span_text: str, regions: list[str]) -> int | None:
    """For a hit the matcher found nothing in: drop digit groups from the end
    until what is left is a number, and return where it ends.

    The matcher reads space-separated groups ("212 555 0123 2026") as one
    over-long candidate and rejects it. Here the longest prefix of whole
    groups that is valid in some region wins; failing that, the longest one
    that is possible as a full number (never a local-only one). None: no
    prefix is a number, so the caller keeps the raw hit."""
    ends = [m.end() for m in _GROUP.finditer(span_text)]
    possible: int | None = None
    for end in reversed(ends):          # longest first; the whole hit counts too
        prefix = span_text[:end]
        stripped = prefix.lstrip(" (")
        tries: list[str | None] = [None] if stripped.startswith("+") else list(regions)
        found = [check(prefix, r) for r in tries]
        if stripped.startswith("00"):
            found.append(check("+" + stripped[2:], None))
        if any(c.status == "valid" for c in found):
            return end
        if possible is None and any(c.status == "possible" and c.full for c in found):
            possible = end
    return possible


# --------------------------------------------------------------------------
# Status in one region
# --------------------------------------------------------------------------
@dataclass
class Check:
    region: str | None          # the region parsed against (None: "+" number)
    status: str                 # valid | possible | impossible
    number: Any = None          # phonenumbers.PhoneNumber, if it parsed
    full: bool = False          # possible as a full number (not local-only)
    why: str = ""               # why impossible


def check(raw: str, region: str | None) -> Check:
    """Parse `raw` in `region` and grade it: valid, possible or impossible."""
    lib = pn()
    try:
        num = lib.parse(raw, region)
    except lib.NumberParseException as exc:
        return Check(region, "impossible", why=f"does not parse ({_parse_error(exc)})")
    reason = lib.is_possible_number_with_reason(num)
    VR = lib.ValidationResult
    if lib.is_valid_number(num):
        return Check(region, "valid", num, full=True)
    if reason == VR.IS_POSSIBLE:
        return Check(region, "possible", num, full=True)
    if reason == VR.IS_POSSIBLE_LOCAL_ONLY:
        return Check(region, "possible", num, full=False)
    return Check(region, "impossible", num, why=_reason_name(reason))


def _parse_error(exc: Any) -> str:
    names = {0: "INVALID_COUNTRY_CODE", 1: "NOT_A_NUMBER", 2: "TOO_SHORT_AFTER_IDD",
             3: "TOO_SHORT_NSN", 4: "TOO_LONG"}
    return names.get(getattr(exc, "error_type", None), "parse error")


def _reason_name(reason: int) -> str:
    names = {1: "INVALID_COUNTRY_CODE", 2: "TOO_SHORT", 3: "TOO_LONG", 5: "INVALID_LENGTH"}
    return names.get(reason, f"not possible ({reason})")


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------
@dataclass
class RegionHint:
    """A region's keyword evidence for one candidate (computed by extract.py)."""
    region: str
    score: float                    # sum of its keyword weights (with decay)
    text_dist: int | None = None    # nearest hint word in the text window
    context_dist: int | None = None # nearest hint word in the context (from the right)
    words: list[str] = field(default_factory=list)

    def nearness(self) -> tuple[int, float]:
        """Lower is nearer: any text-window hit beats every context-only hit."""
        if self.text_dist is not None:
            return (0, self.text_dist)
        if self.context_dist is not None:
            return (1, self.context_dist)
        return (2, 0)


@dataclass
class Reading:
    """One interpretation of a number in one region."""
    status: str                 # valid | possible
    region: str | None          # the number's region (416 -> CA; 011 44... -> GB;
                                # 001: no country, e.g. +800)
    normalized: str             # the match text: in output_format (RAW: as written);
                                # raw text if local-only
    key: str                    # E.164, or region:raw when there is none
    number_type: str = ""
    parsed_in: str | None = None  # the region it was parsed in (None: "+" number)
    source: str = "regions"     # number | hint | regions
    returned: bool = True       # False: filtered out by keep_possible / when_hints_disagree
    why: str = ""               # why not returned
    e164: str = ""              # always E.164 ("" if local-only: there is none): the
                                # report's `normalized` column, whatever output_format says

    @property
    def label(self) -> str:
        return label_for(self.status, self.region)


@dataclass
class Outcome:
    readings: list[Reading]     # returned and not returned, in order
    source: str                 # where the search ended: number | hint | regions
    notes: list[str] = field(default_factory=list)
    why: str = ""               # why there is no reading at all
    # The dropped hit's status: impossible, or possible for a "+" number that
    # is possible only as a local number (a country code rules that out).
    status: str = "impossible"

    @property
    def returned(self) -> list[Reading]:
        return [r for r in self.readings if r.returned]


def _reading(raw: str, c: Check, spec: PhoneSpec) -> Reading:
    lib = pn()
    region = c.region
    number_type = ""
    normalized, e164 = raw, ""
    key = f"{region}:{raw}"
    if c.full:
        e164 = key = lib.format_number(c.number, lib.PhoneNumberFormat.E164)
        if spec.output_format != "RAW":
            fmt = getattr(lib.PhoneNumberFormat, spec.output_format)
            normalized = lib.format_number(c.number, fmt)
        code = c.number.country_code
        if c.status == "valid":
            region = lib.region_code_for_number(c.number) or region
            number_type = _type_name(lib.number_type(c.number))
        elif region is None or code != lib.country_code_for_region(region):
            # "+44 ..." or an IDD ("011 44 ..." parsed in US): the country
            # code, not the region parsed in, says whose number it is.
            region = lib.region_code_for_country_code(code)
    return Reading(c.status, region, normalized, key, number_type, parsed_in=c.region,
                   e164=e164)


def _type_name(value: int) -> str:
    lib = pn()
    for name in dir(lib.PhoneNumberType):
        if not name.startswith("_") and getattr(lib.PhoneNumberType, name) == value:
            return name
    return str(value)


def _fitting(raw: str, regions: list[str], spec: PhoneSpec) -> tuple[list[Reading], list[Check]]:
    """Readings (valid or possible) in each region, one per distinct number
    (US and CA give the same +1 number: one reading), in region order."""
    checks = [check(raw, r) for r in regions]
    seen: dict[str, Reading] = {}
    for c in checks:
        if c.status == "impossible":
            continue
        r = _reading(raw, c, spec)
        seen.setdefault(r.key, r)
    return list(seen.values()), checks


def _hold(readings: list[Reading], keep: Callable[[Reading], bool], why: str) -> None:
    for r in readings:
        if r.returned and not keep(r):
            r.returned, r.why = False, why


def readings_for(
    raw: str, spec: PhoneSpec, hints: list[RegionHint]
) -> Outcome:
    """Every reading of `raw`; the ones not returned are kept with returned=False."""
    if raw.lstrip(" (").startswith("+"):
        c = check(raw, None)
        if c.status == "impossible":
            return Outcome([], "number", why=c.why)
        if not c.full:
            return Outcome([], "number", why=_LOCAL_WITH_CODE, status="possible")
        return Outcome([_numbered(raw, c, spec)], "number")

    notes: list[str] = []
    from_hints: list[Reading] = []
    hinted = [h for h in hints if h.score > 0]
    if hinted:
        from_hints = _hinted(raw, spec, hinted)
        live = [r for r in from_hints if r.returned]
        names = ", ".join(h.region for h in hinted)
        if spec.also_try_regions == "never":
            if live:
                return Outcome(from_hints, "hint")
            return Outcome([], "hint", why=f"impossible in hinted {names}; also_try_regions: never")
        if not live:
            notes.append(f"hint {names} did not fit")
        elif spec.also_try_regions == "if_hint_fails" or (
                spec.also_try_regions == "if_hint_not_valid"
                and any(r.status == "valid" for r in live)):
            return Outcome(from_hints, "hint")
        elif spec.also_try_regions == "if_hint_not_valid":
            notes.append(f"hint {names} not valid; also_try_regions: if_hint_not_valid")

    from_list, checks = _fitting(raw, spec.regions, spec)
    known = {r.key for r in from_hints}
    from_list = [r for r in from_list if r.key not in known]
    _filter_list(from_list, spec, hinted_valid=any(
        r.returned and r.status == "valid" for r in from_hints))
    readings = from_hints + from_list
    if readings:
        return Outcome(readings, "regions", notes)

    stripped = raw.lstrip(" (")
    if stripped.startswith("00"):       # "00" is not an international prefix in NANP
        c = check("+" + stripped[2:], None)
        if c.status != "impossible" and c.full:
            return Outcome([_numbered(raw, c, spec)], "number", notes + ["read the leading 00 as +"])
    home = checks[0]
    return Outcome([], "regions", notes, why=f"impossible in {', '.join(spec.regions)} ({home.why})")


# A "+" number too short to be a full number: possible only as a local
# number, which a country code rules out. It is dropped.
_LOCAL_WITH_CODE = "has a country code but is possible only as a local number (too short)"


def _numbered(raw: str, c: Check, spec: PhoneSpec) -> Reading:
    """The reading of a number whose country code decides (source `number`)."""
    r = _reading(raw, c, spec)
    r.source = "number"
    return r


def _hinted(raw: str, spec: PhoneSpec, hinted: list[RegionHint]) -> list[Reading]:
    """Readings in the hinted regions: valid ones, else possible ones;
    when_hints_disagree: nearest keeps only the nearest hint word's region(s)."""
    by_region = {h.region: h for h in hinted}
    order = sorted(hinted, key=lambda h: (h.nearness(), -h.score, h.region))
    found: list[Reading] = []
    seen: set[str] = set()
    hint_of: dict[str, RegionHint] = {}
    for h in order:
        c = check(raw, h.region)
        if c.status == "impossible":
            continue
        r = _reading(raw, c, spec)
        r.source = "hint"
        if r.key not in seen:
            seen.add(r.key)
            found.append(r)
            hint_of[r.key] = by_region[h.region]
    if any(r.status == "valid" for r in found):
        _hold(found, lambda r: r.status == "valid", "a hinted region gives a valid reading")
    if spec.when_hints_disagree == "nearest":
        live = [r for r in found if r.returned]
        if live:
            best = min((hint_of[r.key].nearness(), -hint_of[r.key].score) for r in live)
            _hold(found, lambda r: (hint_of[r.key].nearness(), -hint_of[r.key].score) == best,
                  "when_hints_disagree: nearest (a nearer hint word won)")
    return found


def _filter_list(found: list[Reading], spec: PhoneSpec, hinted_valid: bool = False) -> None:
    """Apply `keep_possible` to the list readings (in region order). A valid
    hinted reading (also_try_regions: always) counts as a valid reading here,
    so if_no_valid drops the possible list readings next to it."""
    has_valid = hinted_valid or any(r.status == "valid" for r in found)
    home = next((r for r in found if r.parsed_in == spec.regions[0]), None)
    mode = spec.keep_possible
    if mode == "never":
        _hold(found, lambda r: r.status == "valid", "keep_possible: never")
    elif mode == "if_no_valid" and has_valid:
        _hold(found, lambda r: r.status == "valid", "keep_possible: if_no_valid")
    elif mode == "home":
        _hold(found, lambda r: r.status == "valid" or r is home, "keep_possible: home")
