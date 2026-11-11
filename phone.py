"""Phone number validation for patterns with a `validate:` block.

A leaf module, like summarize.py: it imports nothing from extract.py, and
imports the `phonenumbers` library only when a config uses `validate`, so
configs without it never need the library. extract.py does all word
scoring and hands the region hints in (RegionHint); this module never reads
words, so there is no second copy of the keyword matcher.

    spec = load_validate(label, raw_block, weight_map)     # at load time
    spans = refine_spans(span_text, region)                # trim / split a regex hit
    result = pick_region(raw, spec, caller, hints)         # status, region, value
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

VALIDATE_KEYS = (
    "with", "default_region", "try_regions", "region_from", "regions",
    "possible_label", "on_invalid", "on_possible", "possible_min_score",
    "on_ambiguous", "output_format",
)
CHOICES = {
    "region_from": ("both", "context", "text"),
    "on_invalid": ("drop", "keep_flagged"),
    "on_possible": ("keep", "drop", "keep_if_score"),
    "on_ambiguous": ("first", "drop", "keep_flagged"),
    "output_format": ("E164", "INTERNATIONAL", "NATIONAL"),
}
DEFAULTS = {
    "region_from": "both",
    "on_invalid": "drop",
    "on_possible": "keep",       # never lose a number the library doesn't know yet
    "possible_min_score": 1.0,
    "on_ambiguous": "first",
    "output_format": "E164",
}
STATUSES = ("valid", "possible", "impossible")
# decided_by values of a `validation` row (where the region came from).
SOURCES = ("number", "caller", "keyword", "proximity", "trial", "format",
           "fallback", "default")

_pn_module: Any = None


def pn() -> Any:
    """The phonenumbers module, imported on first use."""
    global _pn_module
    if _pn_module is None:
        _pn_module = importlib.import_module("phonenumbers")
    return _pn_module


def pn_version() -> str:
    return str(getattr(pn(), "__version__", "?"))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
@dataclass
class PhoneSpec:
    default_region: str
    try_regions: list[str]
    region_from: str = "both"
    regions: dict[str, dict[str, float]] = field(default_factory=dict)
    possible_label: str | None = None
    on_invalid: str = "drop"
    on_possible: str = "keep"
    possible_min_score: float = 1.0
    on_ambiguous: str = "first"
    output_format: str = "E164"


WeightMap = Callable[[str, str, Any], dict[str, float]]


def _region(label: str, key: str, value: Any) -> str:
    where = f"pattern '{label}': validate.{key}"
    if not isinstance(value, str):
        raise ValueError(f"{where}: expected a region code like US, got {value!r}")
    code = value.upper()
    if code not in pn().SUPPORTED_REGIONS:
        raise ValueError(f"{where}: unknown region '{value}' (phonenumbers has no metadata for it)")
    return code


def load_validate(label: str, raw: Any, weight_map: WeightMap) -> PhoneSpec:
    """Check one pattern's `validate` block and return its PhoneSpec.

    `weight_map(label, key_name, raw)` is extract.py's keyword-map checker,
    so region keywords follow exactly the same rules as pattern keywords.
    """
    where = f"pattern '{label}': validate"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a map, got {type(raw).__name__}")
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
    if "default_region" not in raw:
        raise ValueError(f"{where}: missing required key 'default_region'")
    default = _region(label, "default_region", raw["default_region"])

    tries_raw = raw.get("try_regions")
    if tries_raw is None:
        tries = [default]
    else:
        if not isinstance(tries_raw, list) or not tries_raw:
            raise ValueError(f"{where}.try_regions: expected a non-empty list of region codes")
        tries = []
        for r in tries_raw:
            code = _region(label, "try_regions", r)
            if code in tries:
                raise ValueError(f"{where}.try_regions: '{code}' is listed twice")
            tries.append(code)
        if default not in tries:
            raise ValueError(f"{where}.try_regions: must include default_region '{default}'")

    opts: dict[str, Any] = {}
    for key, choices in CHOICES.items():
        value = raw.get(key, DEFAULTS[key])
        if value not in choices:
            raise ValueError(f"{where}.{key}: unknown value {value!r} (expected one of {', '.join(choices)})")
        opts[key] = value

    min_score = raw.get("possible_min_score", DEFAULTS["possible_min_score"])
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise ValueError(f"{where}.possible_min_score: expected a number, got {min_score!r}")

    possible_label = raw.get("possible_label")
    if possible_label is not None:
        if not isinstance(possible_label, str) or not possible_label:
            raise ValueError(f"{where}.possible_label: expected a label name, got {possible_label!r}")
        if possible_label == label:
            raise ValueError(f"{where}.possible_label: must differ from the pattern's own label")

    regions_raw = raw.get("regions") or {}
    if not isinstance(regions_raw, dict):
        raise ValueError(f"{where}.regions: expected a map of REGION: {{word: weight}}")
    regions: dict[str, dict[str, float]] = {}
    for code_raw, words in regions_raw.items():
        code = _region(label, "regions", code_raw)
        if code in regions:
            raise ValueError(f"{where}.regions: '{code}' is listed twice")
        weights = weight_map(label, f"validate.regions.{code}", words)
        bad = [w for w, v in weights.items() if v <= 0]
        if bad:
            raise ValueError(f"{where}.regions.{code}: keyword '{bad[0]}' needs a weight > 0")
        regions[code] = weights

    return PhoneSpec(
        default_region=default,
        try_regions=tries,
        regions=regions,
        possible_label=possible_label,
        possible_min_score=float(min_score),
        **opts,
    )


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

    @property
    def e164(self) -> str | None:
        """E.164 of a full number; None if impossible or local-only."""
        if self.number is None or self.status == "impossible" or not self.full:
            return None
        return pn().format_number(self.number, pn().PhoneNumberFormat.E164)


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


def _group_lengths(s: str) -> list[int]:
    """Digit-group lengths: "(555) 123-4567" -> [3, 3, 4]."""
    return [len(g) for g in re.findall(r"\d+", s)]


# --------------------------------------------------------------------------
# Region choice
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

    def where(self) -> str:
        if self.text_dist is not None:
            return f"{self.text_dist} word{'s' if self.text_dist != 1 else ''} away"
        return f"in the context, {self.context_dist} from its end"


@dataclass
class Result:
    status: str
    region: str | None
    source: str
    normalized: str
    number_type: str = ""
    ambiguous: bool = False
    tie: bool = False               # exact tie: on_ambiguous applies
    runner_up: str | None = None
    detail: str = ""                # human-readable part of the report reason

    def extra(self) -> dict[str, Any]:
        return {
            "normalized": self.normalized,
            "region": self.region,
            "region_source": self.source,
            "status": self.status,
            "number_type": self.number_type,
            "ambiguous": self.ambiguous,
            "runner_up": self.runner_up,
        }


def _result(raw: str, c: Check, source: str, spec: PhoneSpec, **kw: Any) -> Result:
    lib = pn()
    region = c.region
    number_type = ""
    normalized = raw
    if c.number is not None and c.status != "impossible" and c.full:
        fmt = getattr(lib.PhoneNumberFormat, spec.output_format)
        normalized = lib.format_number(c.number, fmt)
        if c.status == "valid":
            region = lib.region_code_for_number(c.number) or region
            number_type = _type_name(lib.number_type(c.number))
        elif region is None:
            region = lib.region_code_for_country_code(c.number.country_code)
    return Result(c.status, region, source, normalized, number_type, **kw)


def _type_name(value: int) -> str:
    lib = pn()
    for name in dir(lib.PhoneNumberType):
        if not name.startswith("_") and getattr(lib.PhoneNumberType, name) == value:
            return name
    return str(value)


def _international(raw: str) -> bool:
    return raw.lstrip(" (").startswith("+")


def pick_region(
    raw: str, spec: PhoneSpec, caller: str | None, hints: list[RegionHint]
) -> Result:
    """Decide the region and status of one candidate (first step that decides wins).

    A step decides only when the number is not impossible there, so a wrong
    caller region or keyword never turns a fitting number into a loss.
    1. number: a leading "+" sets the country (00 / 011 prefixes are
       handled by parse() in the regions below, per their own IDD).
    2. caller: extract(..., region=).
    3. keyword: hinted regions (score > 0), valid before possible, then the
       nearest hint word; a valid tie between different numbers is
       `proximity` (decided by distance) or `fallback` (exact tie). A hint
       that only gives "possible" waits: a valid trial result (step 4) wins.
    4. trial: every try_regions region; different valid numbers are split
       by the formatting cue (`format`) or are an exact tie (`fallback`).
    5. default: default_region, else the first try region where it is possible.
    """
    if _international(raw):
        c = check(raw, None)
        return _result(raw, c, "number", spec, detail=c.why)

    if caller:
        c = check(raw, caller)
        if c.status != "impossible":
            return _result(raw, c, "caller", spec)

    hinted = [h for h in hints if h.score > 0]
    by_kw = _by_keyword(raw, spec, hinted) if hinted else None
    if by_kw is not None and by_kw.status == "valid":
        return by_kw

    # A hint that only gives "possible" yields to a valid trial result.
    checks = [check(raw, r) for r in spec.try_regions]
    decided = _by_trial(raw, spec, checks)
    if decided is not None:
        if by_kw is not None:
            decided.runner_up = decided.runner_up or by_kw.region
            decided.detail = "; ".join(filter(None, (
                decided.detail, f"hint {by_kw.region} only possible ({by_kw.detail})")))
        return decided
    if by_kw is not None:
        return by_kw

    default = next(c for c in checks if c.region == spec.default_region)
    if default.status != "impossible":
        return _result(raw, default, "default", spec)
    other = next((c for c in checks if c.status != "impossible"), None)
    if other is not None:
        return _result(raw, other, "trial", spec)
    return _result(raw, default, "default", spec, detail=default.why)


def _by_keyword(raw: str, spec: PhoneSpec, hinted: list[RegionHint]) -> Result | None:
    order = {r: i for i, r in enumerate(spec.try_regions)}
    scored = []
    for h in hinted:
        c = check(raw, h.region)
        if c.status != "impossible":
            scored.append((h, c))
    if not scored:
        return None
    scored.sort(key=lambda hc: (
        hc[1].status != "valid", hc[0].nearness(), -hc[0].score,
        order.get(hc[0].region, len(order)), hc[0].region,
    ))
    best_h, best_c = scored[0]
    rivals = [(h, c) for h, c in scored[1:]
              if best_c.status == "valid" and c.status == "valid" and c.e164 != best_c.e164]
    if not rivals:
        return _result(raw, best_c, "keyword", spec,
                       detail=f"'{best_h.words[0]}' {best_h.where()}" if best_h.words else "")
    rival_h, rival_c = rivals[0]
    names = f"valid in {best_h.region} and {rival_h.region}"
    near = (f"{best_h.region} chosen: '{best_h.words[0]}' {best_h.where()} "
            f"vs '{rival_h.words[0]}' {rival_h.where()}")
    if best_h.nearness() != rival_h.nearness():
        return _result(raw, best_c, "proximity", spec, ambiguous=True,
                       runner_up=rival_h.region, detail=f"{names}; {near}")
    return _result(raw, best_c, "fallback", spec, ambiguous=True, tie=True,
                   runner_up=rival_h.region, detail=f"{names}; hint words equally near; {near}")


def _by_trial(raw: str, spec: PhoneSpec, checks: list[Check]) -> Result | None:
    """Group the valid results by E.164. The default region's `possible`
    reading also competes, so a new area code at home is never silently
    replaced by a coincidental valid number abroad."""
    valid = [c for c in checks if c.status == "valid"]
    if not valid:
        return None
    pool = list(valid)
    default = next(c for c in checks if c.region == spec.default_region)
    if default.status == "possible" and default.full:
        pool.append(default)
    pool.sort(key=lambda c: spec.try_regions.index(c.region))
    distinct: dict[str, Check] = {}
    for c in pool:
        distinct.setdefault(c.e164 or "", c)
    if len(distinct) == 1:
        return _result(raw, pool[0], "trial", spec)

    groups = _group_lengths(raw)
    fits = [c for c in distinct.values()
            if len(groups) > 1
            and _group_lengths(pn().format_number(c.number, pn().PhoneNumberFormat.NATIONAL)) == groups]
    others = [c for c in distinct.values() if c not in fits]
    regions = " and ".join(f"{c.region} ({c.status})" for c in distinct.values())
    if len(fits) == 1:
        return _result(raw, fits[0], "format", spec, ambiguous=True,
                       runner_up=others[0].region,
                       detail=f"fits {regions}; grouping matches {fits[0].region}")
    first, second = list(distinct.values())[:2]
    return _result(raw, first, "fallback", spec, ambiguous=True, tie=True,
                   runner_up=second.region, detail=f"fits {regions}; first in try_regions")


def outcome(result: Result, spec: PhoneSpec, score: float) -> tuple[str, str]:
    """(kept | kept_flagged | dropped, why) from the on_* settings."""
    rules: list[tuple[str, str]] = []
    if result.status == "impossible":
        rules.append(("kept_flagged" if spec.on_invalid == "keep_flagged" else "dropped",
                      f"on_invalid: {spec.on_invalid}"))
    elif result.status == "possible":
        if spec.on_possible == "keep":
            rules.append(("kept", "on_possible: keep"))
        elif spec.on_possible == "drop":
            rules.append(("dropped", "on_possible: drop"))
        elif score >= spec.possible_min_score:
            rules.append(("kept", f"score {score:.2f} >= possible_min_score {spec.possible_min_score:g}"))
        else:
            rules.append(("dropped", f"score {score:.2f} < possible_min_score {spec.possible_min_score:g}"))
    if result.tie:
        verdict = {"first": "kept", "drop": "dropped", "keep_flagged": "kept_flagged"}[spec.on_ambiguous]
        rules.append((verdict, f"on_ambiguous: {spec.on_ambiguous}"))
    if not rules:
        return "kept", "valid"
    for verdict in ("dropped", "kept_flagged", "kept"):
        for v, why in rules:
            if v == verdict:
                return v, why
    return rules[0]
