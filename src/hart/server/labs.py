"""Blood and lab results.

Imports ``data/blood_results.json`` (a list of panels: date + markers with
lab names in any language) into ``lab_results``, idempotent on (date, marker name).
New results come from pasting the lab's text (Claude parses it into a
preview, which the athlete confirms) or manual entry.

The marker catalogue maps lab names to keys, English names and categories.
Only catalogued markers drive reminders; the rest are kept and shown.
"""

from __future__ import annotations

import datetime
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from hart.storage.database import Database


@dataclass(frozen=True)
class Marker:
    key: str
    name: str  # English
    category: str  # blood_count | iron | thyroid | vitamins | inflammation | kidney | electrolytes | liver | metabolic | lipids | hormones | muscle
    aliases: tuple[str, ...]  # lab names, lower case
    unit: str | None = None  # preferred unit when a lab prints the marker in two


CATALOGUE: tuple[Marker, ...] = (
    Marker("hemoglobin", "Haemoglobin", "blood_count", ("hemoglobina", "hemoglobin", "hgb"), unit="g/dl"),
    Marker("hematocrit", "Haematocrit", "blood_count", ("hematokryt", "hematocrit", "hct")),
    Marker("rbc", "Red blood cells", "blood_count", ("erytrocyty", "rbc", "krwinki czerwone")),
    Marker("mcv", "MCV", "blood_count", ("mcv",)),
    Marker("mch", "MCH", "blood_count", ("mch",)),
    Marker("mchc", "MCHC", "blood_count", ("mchc",)),
    Marker("rdw_cv", "RDW-CV", "blood_count", ("rdw-cv", "rdw")),
    Marker("wbc", "White blood cells", "blood_count", ("leukocyty", "wbc", "krwinki białe")),
    Marker("platelets", "Platelets", "blood_count", ("płytki krwi", "plt", "trombocyty")),
    Marker("ferritin", "Ferritin", "iron", ("ferrytyna", "ferritin"), unit="ng/ml"),
    Marker("iron", "Iron", "iron", ("żelazo", "zelazo", "iron", "fe"), unit="µg/dl"),
    Marker("tsh", "TSH", "thyroid", ("tsh",)),
    Marker("ft4", "Free T4", "thyroid", ("ft4",), unit="pmol/l"),
    Marker("ft3", "Free T3", "thyroid", ("ft3",), unit="pmol/l"),
    Marker("anti_tpo", "Anti-TPO", "thyroid", ("anty-tpo", "anti-tpo", "atpo")),
    Marker("anti_tg", "Anti-TG", "thyroid", ("anty-tg", "anti-tg", "atg")),
    Marker(
        "vitamin_d_25oh",
        "Vitamin D (25-OH)",
        "vitamins",
        (
            "witamina d3 metabolit 25(oh)",
            "witamina d 25(oh)",
            "25(oh)d",
            "witamina d",
            "vitamin d3 25(oh)",
            "vitamin d 25(oh)",
            "vitamin d",
            "25-oh vitamin d",
        ),
        unit="ng/ml",
    ),
    Marker("vitamin_b12", "Vitamin B12", "vitamins", ("witamina b12", "vitamin b12", "b12"), unit="pg/ml"),
    Marker("crp", "CRP", "inflammation", ("crp ilościowo", "crp", "białko c-reaktywne")),
    Marker("esr", "ESR (OB)", "inflammation", ("ob", "esr")),
    Marker("creatinine", "Creatinine", "kidney", ("kreatynina", "creatinine"), unit="mg/dl"),
    Marker("egfr", "eGFR", "kidney", ("egfr",)),
    Marker("urea", "Urea", "kidney", ("mocznik", "urea"), unit="mg/dl"),
    Marker("uric_acid", "Uric acid", "kidney", ("kwas moczowy", "uric acid"), unit="mg/dl"),
    Marker("sodium", "Sodium", "electrolytes", ("sód", "sod", "na", "sodium")),
    Marker("potassium", "Potassium", "electrolytes", ("potas", "k", "potassium")),
    Marker("alt", "ALT", "liver", ("alt", "alat")),
    Marker("ast", "AST", "liver", ("ast", "aspat")),
    Marker("ldh", "LDH", "muscle", ("ldh",)),
    Marker("ck", "Creatine kinase", "muscle", ("ck", "kinaza kreatynowa")),
    Marker("glucose", "Glucose", "metabolic", ("glukoza", "glucose"), unit="mg/dl"),
    Marker("cholesterol_total", "Total cholesterol", "lipids", ("cholesterol", "cholesterol całkowity"), unit="mg/dl"),
    Marker("hdl", "HDL cholesterol", "lipids", ("cholesterol hdl", "hdl"), unit="mg/dl"),
    Marker("ldl", "LDL cholesterol", "lipids", ("cholesterol ldl", "ldl"), unit="mg/dl"),
    Marker("non_hdl", "Non-HDL cholesterol", "lipids", ("cholesterol nie-hdl", "non-hdl"), unit="mg/dl"),
    Marker(
        "triglycerides", "Triglycerides", "lipids", ("trójglicerydy", "triglicerydy", "triglycerides"), unit="mg/dl"
    ),
    Marker("testosterone", "Testosterone", "hormones", ("testosteron", "testosterone"), unit="ng/ml"),
    Marker("cortisol", "Cortisol", "hormones", ("kortyzol", "cortisol"), unit="µg/dl"),
    Marker("epo", "Erythropoietin", "hormones", ("erytropoetyna", "epo")),
)
BY_KEY = {m.key: m for m in CATALOGUE}
MARKER_KEYS: dict[str, str] = {alias: m.key for m in CATALOGUE for alias in m.aliases}

# Key markers for trends on the Health page, in display order.
TREND_MARKERS = (
    "hemoglobin",
    "ferritin",
    "tsh",
    "vitamin_d_25oh",
    "vitamin_b12",
    "hematocrit",
    "crp",
    "creatinine",
    "glucose",
    "ldl",
    "testosterone",
    "alt",
)

# Factor that turns a value in another unit (normalised by _unit) into the marker's preferred unit.
CONVERSIONS: dict[str, dict[str, float]] = {
    "hemoglobin": {"g/l": 0.1, "mmol/l": 1.611},
    "ferritin": {"ug/l": 1.0},
    "iron": {"umol/l": 5.585},
    "ft4": {"ng/dl": 12.87},
    "ft3": {"pg/ml": 1.536},
    "vitamin_d_25oh": {"nmol/l": 1 / 2.496},
    "vitamin_b12": {"pmol/l": 1.355},
    "creatinine": {"umol/l": 1 / 88.42},
    "urea": {"mmol/l": 6.006},
    "uric_acid": {"umol/l": 1 / 59.48},
    "glucose": {"mmol/l": 18.016},
    "cholesterol_total": {"mmol/l": 38.67},
    "hdl": {"mmol/l": 38.67},
    "ldl": {"mmol/l": 38.67},
    "non_hdl": {"mmol/l": 38.67},
    "triglycerides": {"mmol/l": 88.57},
    "testosterone": {"nmol/l": 1 / 3.467},
    "cortisol": {"nmol/l": 1 / 27.59},
}

# A marker name ending in one of these (e.g. "Glukoza (mocz)") is never a catalogue (blood) marker.
NON_BLOOD = ("(mocz)", "(urine)")

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def marker_key(name: str) -> str | None:
    return MARKER_KEYS.get(name.strip().lower())


def resolve_key(name: str, suggested: str | None = None) -> str | None:
    """The catalogue alias wins; otherwise the parser's suggestion when it is a real key."""
    if name.strip().lower().endswith(NON_BLOOD):
        return None
    return marker_key(name) or (suggested if suggested in BY_KEY else None)


def _unit(text: str | None) -> str:
    return (text or "").strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")


def _sig(value: float) -> str:
    """Three significant digits, no exponent: 72.06 → '72.1', 0.9048 → '0.905', 154.7 → '155'."""
    if value == 0:
        return "0"
    digits = 2 - math.floor(math.log10(abs(value)))
    return f"{round(value, digits):.{max(digits, 0)}f}"


def convert(key: str | None, unit: str | None, row: dict[str, Any]) -> dict[str, Any] | None:
    """The row's value and range in the preferred unit, or None when it is already there or unknown.

    ``row`` has value_num, qualifier, ref_low, ref_high; the result also carries value_text,
    ref_text and unit as they should be stored.
    """
    factor = CONVERSIONS.get(key or "", {}).get(_unit(unit))
    if factor is None:
        return None
    value, low, high = (None if row[k] is None else row[k] * factor for k in ("value_num", "ref_low", "ref_high"))
    if low is not None and high is not None:
        ref_text = f"{_sig(low)} - {_sig(high)}"
    elif high is not None:
        ref_text = f"<{_sig(high)}"
    elif low is not None:
        ref_text = f">{_sig(low)}"
    else:
        ref_text = None
    return {
        "value_num": None if value is None else float(_sig(value)),
        "value_text": None if value is None else (row["qualifier"] or "") + _sig(value),
        "ref_low": None if low is None else float(_sig(low)),
        "ref_high": None if high is None else float(_sig(high)),
        "ref_text": ref_text,
        "unit": BY_KEY[key].unit,
    }


def parse_value(text: str) -> tuple[float | None, str | None]:
    """'16.5' → (16.5, None); '<8.00' → (8.0, '<'); '>90' → (90.0, '>'); text → (None, None)."""
    stripped = text.strip()
    qualifier = stripped[0] if stripped[:1] in ("<", ">") else None
    match = _NUM.search(stripped)
    if not match:
        return None, None
    return float(match.group().replace(",", ".")), qualifier


def parse_range(text: str | None) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    nums = [float(n.replace(",", ".")) for n in _NUM.findall(text)]
    if len(nums) >= 2:
        return nums[0], nums[1]
    if text.strip().startswith("<") and nums:
        return None, nums[0]
    if text.strip().startswith(">") and nums:
        return nums[0], None
    return None, None


class LabMarkerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=60, description="As printed, e.g. '16.5' or '<8.00'")
    unit: str | None = Field(default=None, max_length=30)
    reference_range: str | None = Field(default=None, max_length=60, description="As printed, e.g. '13.5 - 18'")
    flag: str | None = Field(default=None, max_length=10, description="The lab's flag (H, L, …) if printed")
    marker_key: str | None = Field(default=None, description="Catalogue key, or null when not in the catalogue")

    @field_validator("marker_key")
    @classmethod
    def _known_key(cls, v: str | None) -> str | None:
        return v if v in BY_KEY else None


class LabPanelIn(BaseModel):
    test_date: datetime.date
    markers: list[LabMarkerIn] = Field(min_length=1, max_length=200)


def insert_marker(db: Database, test_date: Any, m: LabMarkerIn | dict[str, Any], source: str) -> bool:
    """Insert one result in the marker's preferred unit; False when (date, marker name) already exists."""
    m = m if isinstance(m, LabMarkerIn) else LabMarkerIn.model_validate(m)
    name, value_text = m.name.strip(), m.value.strip()
    key = resolve_key(name, m.marker_key)
    value, qualifier = parse_value(value_text)
    low, high = parse_range(m.reference_range)
    unit, ref_text = (m.unit or "").strip() or None, (m.reference_range or "").strip() or None
    row = {"value_num": value, "qualifier": qualifier, "ref_low": low, "ref_high": high}
    if converted := convert(key, unit, row):
        value_text = converted["value_text"] or value_text
        value, low, high = converted["value_num"], converted["ref_low"], converted["ref_high"]
        unit, ref_text = converted["unit"], converted["ref_text"]
    found = db.fetchone(
        "INSERT INTO lab_results (test_date, marker_name, marker_key, value_text, value_num, qualifier, "
        "unit, ref_low, ref_high, ref_text, flag, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (test_date, marker_name) DO NOTHING RETURNING id",
        [
            test_date,
            name,
            key,
            value_text,
            value,
            qualifier,
            unit,
            low,
            high,
            ref_text,
            (m.flag or "").strip() or None,
            source,
        ],
    )
    return found is not None


def import_panel(db: Database, panel: LabPanelIn, source: str) -> dict[str, Any]:
    added = sum(insert_marker(db, panel.test_date, m, source) for m in panel.markers)
    return {"test_date": panel.test_date, "added": added, "skipped": len(panel.markers) - added}


def import_lab_results(db: Database, path: Path, source: str = "import") -> int:
    if not path.is_file():
        return 0
    panels: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    inserted = 0
    for panel in panels:
        for m in panel.get("markers", []):
            if not str(m.get("name", "")).strip() or not str(m.get("value", "")).strip():
                continue
            inserted += insert_marker(
                db,
                panel["date"],
                {
                    "name": str(m["name"]),
                    "value": str(m["value"]),
                    "unit": m.get("unit"),
                    "reference_range": m.get("reference_range"),
                    "flag": m.get("flag"),
                },
                source,
            )
    return inserted


def remap_keys(db: Database) -> int:
    """Apply catalogue changes to stored rows (new aliases map old rows too).

    Keys the parser assigned to names without an alias are kept, unless the name is non-blood.
    """
    changed = 0
    for name, current in db.fetchall("SELECT DISTINCT marker_name, marker_key FROM lab_results"):
        key = resolve_key(name, current)
        if key != current:
            db.execute("UPDATE lab_results SET marker_key = ? WHERE marker_name = ?", [key, name])
            changed += 1
    return changed


def convert_units(db: Database) -> int:
    """Convert stored results to each marker's preferred unit, so trends compare like with like. Idempotent."""
    changed = 0
    found = db.fetchall(
        "SELECT id, marker_key, unit, value_num, qualifier, ref_low, ref_high FROM lab_results "
        "WHERE marker_key IS NOT NULL AND unit IS NOT NULL"
    )
    for rid, key, unit, value, qualifier, low, high in found:
        row = {"value_num": value, "qualifier": qualifier, "ref_low": low, "ref_high": high}
        if (c := convert(key, unit, row)) is None:
            continue
        db.execute(
            "UPDATE lab_results SET value_num = ?, value_text = coalesce(?, value_text), ref_low = ?, "
            "ref_high = ?, ref_text = ?, unit = ? WHERE id = ?",
            [c["value_num"], c["value_text"], c["ref_low"], c["ref_high"], c["ref_text"], c["unit"], rid],
        )
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def out_of_range(r: dict[str, Any]) -> str | None:
    """'high' / 'low' when the value is outside the lab's range or flagged."""
    flag = (r.get("flag") or "").strip().upper()
    v, low, high = r.get("value_num"), r.get("ref_low"), r.get("ref_high")
    if v is not None and r.get("qualifier") is None:
        if high is not None and v > high:
            return "high"
        if low is not None and v < low:
            return "low"
    if flag.startswith("H"):
        return "high"
    if flag.startswith("L"):
        return "low"
    return None


def results(db: Database, key: str | None = None) -> list[dict[str, Any]]:
    from hart.server.data import rows

    where, params = ("WHERE marker_key = ?", [key]) if key else ("", [])
    found = rows(
        db,
        "SELECT id, test_date, marker_name, marker_key, value_text, value_num, qualifier, unit, "
        f"ref_low, ref_high, ref_text, flag, source FROM lab_results {where} ORDER BY test_date, marker_name",
        params,
    )
    for r in found:
        r["status"] = out_of_range(r)
        r["name_en"] = BY_KEY[r["marker_key"]].name if r["marker_key"] in BY_KEY else None
    return found


def panels(db: Database) -> list[dict[str, Any]]:
    """Results grouped by test date, newest first, catalogued markers first."""
    by_date: dict[Any, list[dict[str, Any]]] = {}
    for r in results(db):
        by_date.setdefault(r["test_date"], []).append(r)
    order = {m.key: i for i, m in enumerate(CATALOGUE)}
    out = []
    for d in sorted(by_date, reverse=True):
        items = sorted(by_date[d], key=lambda r: (order.get(r["marker_key"], 999), r["marker_name"].lower()))
        out.append({"test_date": d, "markers": items, "flagged": sum(1 for r in items if r["status"])})
    return out


# ---------------------------------------------------------------------------
# Paste → preview (Claude, Haiku)
# ---------------------------------------------------------------------------

PARSE_PROMPT_VERSION = "parse_labs@2"
MAX_PASTE_CHARS = 30000

_PARSE_SYSTEM = """\
You convert pasted lab results (any language and lab, e.g. a Polish report from Diagnostyka, ALAB or \
Medicover) into structured rows. Copy every value exactly as printed:
- test_date: the collection date ("data pobrania"); if only a report date exists, use it.
- name: the marker name exactly as printed (keep the original language, e.g. "Hemoglobina").
  Markers from a specimen other than blood get it in parentheses, in the report's language, so they stay \
distinct from blood markers: urine "Glukoza" → "Glukoza (mocz)", "Przejrzystość" → "Przejrzystość (mocz)", \
"Leukocyty" → "Leukocyty (mocz)". When one marker is printed in two units (e.g. glucose in mg/dl and mmol/l), \
output every row with the same plain name, without the unit note (e.g. "Glukoza", not "Glukoza (jedn. SI)"), \
each with its own unit and range — the app keeps the preferred unit.
- value: as printed, including "<" or ">" (e.g. "<8.00"); use a dot as the decimal separator. Keep \
descriptive results as words (e.g. "nie wykryto", "przejrzysty", "żółty").
- unit and reference_range as printed (e.g. "13.5 - 18"); flag only if the lab printed one (H, L, ↑, ↓ → H/L).
- marker_key: the catalogue key the marker is, whatever the lab calls it (e.g. "Krwinki białe (WBC)" → wbc, \
"Witamina D-25(OH)D" → vitamin_d_25oh), or null when it is none of them. Only blood markers have keys — \
urine and other specimens are always null. Related but different markers stay null (e.g. "Bazofile %", \
"Azot mocznika", "PSA").
Catalogue: {catalogue}
One panel per collection date. Skip headers, addresses, doctor names, patient data and comments. \
Never invent or compute values; anything you can't read goes into warnings.
""".replace("{catalogue}", "; ".join(f"{m.key} = {m.name}" for m in CATALOGUE))


def pick_units(markers: list[LabMarkerIn]) -> tuple[list[LabMarkerIn], list[str]]:
    """One row per marker name: a marker printed in several units keeps the catalogue's preferred unit
    (the first row when none matches), so results stay comparable across panels."""
    groups: dict[str, list[LabMarkerIn]] = {}
    for m in markers:
        groups.setdefault(m.name.strip().lower(), []).append(m)
    kept, notes = [], []
    for name, rows in groups.items():
        choice = rows[0]
        if len(rows) > 1:
            entry = BY_KEY.get(resolve_key(name, rows[0].marker_key) or "")
            preferred = [r for r in rows if entry and entry.unit and _unit(r.unit) == _unit(entry.unit)]
            choice = preferred[0] if preferred else rows[0]
            dropped = ", ".join(r.unit or r.value for r in rows if r is not choice)
            how = "the preferred unit" if preferred else "the first row (no preferred unit known)"
            notes.append(f"{choice.name}: kept {choice.unit or choice.value} as {how}, skipped {dropped}")
        kept.append(choice)
    return kept, notes


class ParsedLabs(BaseModel):
    panels: list[LabPanelIn] = Field(max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class LabError(Exception):
    pass


async def parse_paste(claude: Any, model: str, text: str) -> dict[str, Any]:
    from hart.server.claude.policy import ToolPolicy
    from hart.server.claude.runner import RunSpec
    from hart.server.grading import _structured_raw

    text = text.strip()
    if not text:
        raise LabError("nothing to parse")
    if len(text) > MAX_PASTE_CHARS:
        raise LabError(f"paste is too long (max {MAX_PASTE_CHARS} characters)")
    spec = RunSpec(
        purpose="parse_labs",
        prompt=f"Pasted lab results:\n<<<\n{text}\n>>>",
        model=model,
        system_prompt=_PARSE_SYSTEM,
        prompt_version=PARSE_PROMPT_VERSION,
        policy=ToolPolicy(allowed_mcp=frozenset(), web=False),
        max_turns=3,
        timeout_s=90,
        output_schema=ParsedLabs.model_json_schema(),
    )
    outcome = await claude.run(spec)
    if outcome.status != "ok":
        raise LabError(outcome.error or f"Claude run {outcome.status}")
    try:
        parsed = ParsedLabs.model_validate(_structured_raw(outcome))
    except ValidationError as exc:
        raise LabError(f"Couldn't read the results: {str(exc)[:300]}") from exc
    out, info = [], []
    for p in parsed.panels:
        kept, notes = pick_units(p.markers)
        info += [f"{p.test_date}: {n}" for n in notes]
        markers = []
        for m in kept:
            key = resolve_key(m.name, m.marker_key)
            value, qualifier = parse_value(m.value)
            low, high = parse_range(m.reference_range)
            row = {"value_num": value, "qualifier": qualifier, "ref_low": low, "ref_high": high}
            if (c := convert(key, m.unit, row)) and c["value_text"]:
                info.append(
                    f"{p.test_date}: {m.name} {m.value} {m.unit} will be saved as {c['value_text']} {c['unit']}"
                )
            markers.append({**m.model_dump(), "marker_key": key})
        out.append({"test_date": p.test_date.isoformat(), "markers": markers})
    return {"panels": out, "warnings": parsed.warnings, "notes": info, "claude_run_id": outcome.run_id}
