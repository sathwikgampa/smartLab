"""Deterministic, read-only tools for the lab co-pilot.

Every tool:

* takes typed, validated arguments (a Pydantic model; unknown fields are rejected),
* returns ``{"ok": bool, "data": dict, "error": str | None, "summary": str}``,
* runs on a read-only database connection (SQLite ``mode=ro`` / PostgreSQL ``READ ONLY``),
  so it cannot write even by mistake,
* makes no outbound network calls, runs no shell commands and opens no files,
* is scoped to one organisation.

The LLM decides *which* tool to call; the numbers, stock levels, compatibility results and
error-code matches all come from the code below.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..db import connect_readonly, database_target
from ..rag.engine import resolve_versions
from ..services.inventory import EXPIRING_SOON_DAYS, annotate
from ..services.troubleshooting import normalise_code
from . import compatibility
from .units import BASE_UNIT, UnitError, dimension_of, fmt_number, humanise, to_base

logger = logging.getLogger(__name__)

CHUNK_PREFIX = "chunk-"
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
ERROR_CODE = re.compile(r"^[A-Za-z]{1,4}[- ]?\d{2,4}$")
NO_PPE_RECORDED = ("No PPE guidance is recorded for this fix. Follow the instrument SOP and your lab's PPE rules "
                   "(at minimum safety glasses, lab coat and gloves).")


def chunk_ref(chunk_id: int) -> str:
    return f"{CHUNK_PREFIX}{chunk_id}"


# --------------------------------------------------------------------------- results
@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    summary: str = ""

    @classmethod
    def fail(cls, error: str, data: dict[str, Any] | None = None) -> "ToolResult":
        return cls(False, data or {}, error, error)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolContext:
    conn: Any
    rag: Any
    organization_id: int
    today: date
    team_id: int | None = None


# --------------------------------------------------------------------------- names
def normalise_name(name: str) -> str:
    """Case-, punctuation- and spacing-insensitive form: "Tris-HCl" == "tris hcl" == "TRIS  HCl"."""
    s = (name or "").lower().replace("µ", "u").replace("μ", "u")
    return re.sub(r"[^a-z0-9%]+", " ", s).strip()


@dataclass
class Resolution:
    status: str  # exact | ambiguous | not_found
    compound: dict[str, Any] | None = None
    candidates: list[str] = field(default_factory=list)


def _compound_names(c: dict[str, Any]) -> set[str]:
    names = {normalise_name(c["display_name"]), normalise_name(c["compound_key"].replace("_", " "))}
    names |= {normalise_name(s) for s in (c.get("synonyms") or "").split(";") if s.strip()}
    if c.get("cas_number"):
        names.add(normalise_name(c["cas_number"]))
    return {n for n in names if n}


def _org_lots(ctx: ToolContext) -> list[dict[str, Any]]:
    if ctx.team_id is not None:
        return [dict(r) for r in ctx.conn.execute(
            "SELECT * FROM reagents WHERE organization_id = ? AND team_id = ? ORDER BY expiry_date, id",
            (ctx.organization_id, ctx.team_id))]
    return [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM reagents WHERE organization_id = ? ORDER BY expiry_date, id", (ctx.organization_id,))]


def resolve_compound(ctx: ToolContext, name: str) -> Resolution:
    """Map a user-supplied name to exactly one compound, or report ambiguity.

    Only exact matches (display name, synonym, CAS number or an inventory lot name, after
    normalisation) resolve. A partial match such as "Tris" (Tris base? Tris-HCl?) is returned as
    candidates for the user to choose from; it is never silently picked.
    """
    query = normalise_name(name)
    if not query:
        return Resolution("not_found")
    compounds = [dict(r) for r in ctx.conn.execute("SELECT * FROM compounds ORDER BY display_name")]
    entries: list[tuple[dict[str, Any], set[str]]] = [(c, _compound_names(c)) for c in compounds]
    by_key = {c["compound_key"]: c for c in compounds}
    # Inventory lots that are not linked to a compound still count as their own (name-only) entry.
    for lot in _org_lots(ctx):
        if lot.get("compound_key") in by_key:
            entries.append((by_key[lot["compound_key"]], {normalise_name(lot["name"])}))
        elif not lot.get("compound_key"):
            pseudo = {"compound_key": f"name:{normalise_name(lot['name'])}", "display_name": lot["name"],
                      "molecular_weight": None, "cas_number": lot.get("cas_number"), "synonyms": ""}
            entries.append((pseudo, {normalise_name(lot["name"])}))

    exact = {c["compound_key"]: c for c, names in entries if query in names}
    if len(exact) == 1:
        return Resolution("exact", next(iter(exact.values())))
    if len(exact) > 1:
        return Resolution("ambiguous", candidates=sorted({c["display_name"] for c in exact.values()}))
    q_tokens = set(query.split())
    partial = {c["compound_key"]: c for c, names in entries
               if any(q_tokens <= set(n.split()) for n in names)}
    if partial:
        return Resolution("ambiguous", candidates=sorted({c["display_name"] for c in partial.values()})[:8])
    return Resolution("not_found")


def _lots_for(ctx: ToolContext, compound: dict[str, Any]) -> list[dict[str, Any]]:
    key = compound["compound_key"]
    names = _compound_names(compound) if not key.startswith("name:") else {key[5:]}
    lots = []
    for lot in _org_lots(ctx):
        linked = lot.get("compound_key")
        if linked == key or (not linked and normalise_name(lot["name"]) in names):
            lots.append(annotate(lot, ctx.today))
    return lots


def _clarify(name: str, res: Resolution) -> str:
    if res.status == "ambiguous":
        return f"'{name}' matches more than one compound ({', '.join(res.candidates)}). Please specify which one."
    return f"'{name}' was not found in the reagent database."


# --------------------------------------------------------------------------- arg models
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QuerySopArgs(_Args):
    query: str = Field(min_length=2, max_length=500, description="What to look up in the uploaded SOPs.")
    k: int = Field(default=5, ge=1, le=10, description="Number of passages to return.")
    document_ids: list[int] | None = Field(default=None, max_length=50,
                                           description="Restrict the search to these documents.")


class CalculateArgs(_Args):
    compound: str = Field(min_length=1, max_length=100)
    target_concentration: float = Field(gt=0, le=1e6)
    concentration_unit: str = Field(min_length=1, max_length=12, description="M, mM, uM, nM, mg/mL, g/L, ug/mL or %w/v")
    volume: float = Field(gt=0, le=1e6)
    volume_unit: str = Field(min_length=1, max_length=12, description="L, mL or uL")

    @field_validator("concentration_unit")
    @classmethod
    def _conc_unit(cls, v: str) -> str:
        if dimension_of(v) not in ("molar", "mass_conc"):
            raise ValueError("must be a concentration unit (M, mM, uM, nM, mg/mL, g/L, ug/mL or %w/v)")
        return v

    @field_validator("volume_unit")
    @classmethod
    def _vol_unit(cls, v: str) -> str:
        if dimension_of(v) != "volume":
            raise ValueError("must be a volume unit (L, mL or uL)")
        return v


class InventoryArgs(_Args):
    reagent_name: str = Field(min_length=1, max_length=100)
    required_amount: float | None = Field(default=None, gt=0, le=1e9)
    unit: str | None = Field(default=None, max_length=12)

    @model_validator(mode="after")
    def _amount_needs_unit(self) -> "InventoryArgs":
        if self.required_amount is not None and not self.unit:
            raise ValueError("unit is required when required_amount is given")
        return self


class CompatibilityArgs(_Args):
    reagents: list[str] = Field(min_length=2, max_length=8)

    @field_validator("reagents")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v]
        if any(not 1 <= len(s) <= 100 for s in cleaned):
            raise ValueError("each reagent name must be 1-100 characters")
        return cleaned


class DiagnoseArgs(_Args):
    upload_id: str | None = Field(default=None, description="Server-issued id of an uploaded image.")
    error_code: str | None = Field(default=None, description="Error code confirmed by the user, e.g. E-104.")

    @field_validator("upload_id")
    @classmethod
    def _upload_id(cls, v: str | None) -> str | None:
        if v is not None and not UPLOAD_ID.fullmatch(v):
            raise ValueError("upload_id must be a server-issued id")
        return v

    @field_validator("error_code")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        if v is not None and not ERROR_CODE.fullmatch(v):
            raise ValueError("error_code must look like E-104 or IMB-01")
        return v

    @model_validator(mode="after")
    def _one_of(self) -> "DiagnoseArgs":
        if not self.upload_id and not self.error_code:
            raise ValueError("provide upload_id or error_code")
        return self


# --------------------------------------------------------------------------- tools
def tool_query_sop_knowledge(ctx: ToolContext, args: QuerySopArgs) -> ToolResult:
    """Retrieve SOP passages (organisation-scoped) with similarity scores."""
    rag = ctx.rag
    retrieved, _ = resolve_versions(rag.retrieve(ctx.conn, args.query, k=args.k, document_ids=args.document_ids,
                                                 organization_id=ctx.organization_id, team_id=ctx.team_id))
    chunks = [{
        "chunk_id": chunk_ref(e.chunk_id),
        "doc_name": e.document_title,
        "document_id": e.document_id,
        "page_number": e.page,
        "text": e.text,
        "similarity": round(float(e.score), 4),
        "relevant": e.score >= rag.min_score,
        "approval_status": e.approval_status,
        "version": e.version,
        "doc_code": e.doc_code,
        "category": e.category,
        "doc_type": e.doc_type,
    } for e in retrieved]
    relevant = [c for c in chunks if c["relevant"]]
    top = max((c["similarity"] for c in chunks), default=0.0)
    data = {"chunks": chunks, "min_score": rag.min_score, "top_score": top, "relevant_count": len(relevant)}
    if not relevant:
        return ToolResult(True, data, None, f"No passage met the relevance threshold ({rag.min_score}); top score {top}.")
    docs = sorted({c["doc_name"] for c in relevant})
    return ToolResult(True, data, None,
                      f"{len(relevant)} relevant passage(s) from {', '.join(docs)}; top score {top}.")


def tool_calculate_preparation(ctx: ToolContext, args: CalculateArgs) -> ToolResult:
    """Mass of solute for a target concentration and volume. All arithmetic happens here."""
    res = resolve_compound(ctx, args.compound)
    if res.status != "exact":
        return ToolResult.fail(_clarify(args.compound, res),
                               {"needs_clarification": res.status == "ambiguous", "candidates": res.candidates})
    compound = res.compound
    conc_dim, conc_base = to_base(args.target_concentration, args.concentration_unit)
    _, volume_l = to_base(args.volume, args.volume_unit)
    conc_text = f"{fmt_number(args.target_concentration)} {args.concentration_unit}"
    vol_text = f"{fmt_number(args.volume)} {args.volume_unit}"
    steps = [f"Volume: {vol_text} = {fmt_number(volume_l)} L"]
    mw = compound.get("molecular_weight")

    if conc_dim == "molar":
        if not mw or mw <= 0:
            return ToolResult.fail(
                f"Molecular weight for {compound['display_name']} is not recorded in the reagent database, "
                "so the mass cannot be calculated.",
                {"compound": compound["display_name"], "missing": "molecular_weight"})
        moles = conc_base * volume_l
        mass_g = moles * mw
        steps.insert(0, f"Concentration: {conc_text} = {fmt_number(conc_base)} mol/L")
        steps += [f"Amount: {fmt_number(conc_base)} mol/L x {fmt_number(volume_l)} L = {fmt_number(moles)} mol",
                  f"Mass: {fmt_number(moles)} mol x {fmt_number(mw)} g/mol = {fmt_number(mass_g)} g"]
        formula = "mass (g) = concentration (mol/L) x volume (L) x molecular weight (g/mol)"
    else:
        mass_g = conc_base * volume_l
        steps.insert(0, f"Concentration: {conc_text} = {fmt_number(conc_base)} g/L")
        steps.append(f"Mass: {fmt_number(conc_base)} g/L x {fmt_number(volume_l)} L = {fmt_number(mass_g)} g")
        formula = "mass (g) = concentration (g/L) x volume (L)"

    mass = humanise(mass_g, "mass")
    steps.append(f"Weigh {mass} of {compound['display_name']} for {vol_text} of final solution.")
    data = {
        "compound": compound["display_name"],
        "compound_key": compound["compound_key"],
        "molecular_weight": mw,
        "target_concentration": args.target_concentration,
        "concentration_unit": args.concentration_unit,
        "volume": args.volume,
        "volume_unit": args.volume_unit,
        "mass_g": mass_g,
        "mass_value": float(fmt_number(mass.value)),
        "mass_unit": mass.unit,
        "mass": str(mass),
        "formula": formula,
        "steps": steps,
        # Every number this calculation produced, for the guardrail's number check.
        "numbers": [conc_text, vol_text, f"{fmt_number(volume_l)} L", f"{fmt_number(mass_g)} g", str(mass)]
                   + ([f"{fmt_number(mw)} g/mol"] if mw else []),
    }
    return ToolResult(True, data, None,
                      f"{compound['display_name']}: {mass} for {vol_text} at {conc_text} ({formula}).")


def tool_check_reagent_inventory(ctx: ToolContext, args: InventoryArgs) -> ToolResult:
    """Stock, location and expiry for one reagent, and whether stock covers a required amount."""
    res = resolve_compound(ctx, args.reagent_name)
    base = {"query": args.reagent_name, "match": res.status, "candidates": res.candidates,
            "needs_clarification": res.status == "ambiguous", "found": False, "available": False,
            "expired": False, "expiring_soon": False, "low_stock": False, "sufficient": None, "lots": [],
            "required": None, "location": None, "expires": None, "reagent": args.reagent_name}
    if res.status != "exact":
        return ToolResult(True, base, None, _clarify(args.reagent_name, res))
    compound = res.compound
    lots = _lots_for(ctx, compound)
    base["reagent"] = compound["display_name"]
    base["compound_key"] = compound["compound_key"]
    if not lots:
        return ToolResult(True, base, None, f"{compound['display_name']} is not in this lab's inventory.")

    usable = [l for l in lots if l["status"] != "expired" and l["quantity"] > 0]
    first = usable[0] if usable else lots[0]  # first-expiry-first-out
    base.update({
        "found": True,
        "available": bool(usable),
        "expired": not usable and any(l["status"] == "expired" for l in lots),
        "expiring_soon": any(l["days_to_expiry"] is not None and l["days_to_expiry"] <= EXPIRING_SOON_DAYS
                             for l in usable),
        "low_stock": sum(1 for l in usable if not l["low_stock"]) == 0 and bool(usable),
        "location": first.get("location"),
        "expires": first.get("expiry_date"),
        "lots": [{k: l.get(k) for k in ("name", "lot_number", "quantity", "unit", "location", "expiry_date",
                                        "status", "days_to_expiry")} for l in lots],
    })

    totals: dict[str, float] = {}
    uncountable = []
    for lot in usable:
        try:
            dim, value = to_base(lot["quantity"], lot["unit"])
            totals[dim] = totals.get(dim, 0.0) + value
        except UnitError:
            uncountable.append(f"{fmt_number(lot['quantity'])} {lot['unit']}")
    base["available_total"] = [str(humanise(v, d)) for d, v in totals.items()] + uncountable

    note = ""
    if args.required_amount is not None:
        base["required"] = f"{fmt_number(args.required_amount)} {args.unit}"
        try:
            req_dim, req_base = to_base(args.required_amount, args.unit)
        except UnitError:
            req_dim, req_base = None, None
        if req_dim is None:
            note = f"Required unit '{args.unit}' cannot be converted; sufficiency not checked."
        elif req_dim not in totals:
            note = (f"Stock is recorded in units that cannot be compared with {args.unit}; "
                    "check the quantity manually.") if usable else "No usable stock."
            base["sufficient"] = False if not usable else None
        else:
            have = totals[req_dim]
            base["sufficient"] = have >= req_base
            note = (f"Need {fmt_number(req_base)} {BASE_UNIT[req_dim]}, usable stock "
                    f"{fmt_number(have)} {BASE_UNIT[req_dim]} (converted explicitly).")
    base["sufficiency_note"] = note

    if not usable:
        state = "all lots expired" if base["expired"] else "out of stock"
    else:
        state = f"{', '.join(base['available_total'])} usable at {base['location']}, expires {base['expires']}"
        if base["expiring_soon"]:
            state += " (expiring soon)"
    suff = "" if base["sufficient"] is None else (" - sufficient" if base["sufficient"] else " - NOT sufficient")
    return ToolResult(True, base, None, f"{compound['display_name']}: {state}{suff}.")


def tool_verify_chemical_compatibility(ctx: ToolContext, args: CompatibilityArgs) -> ToolResult:
    """Pairwise check against the limited demo matrix. Unlisted pairs are 'unknown', never safe."""
    resolved: list[tuple[str, str | None]] = []
    unresolved = []
    for name in args.reagents:
        res = resolve_compound(ctx, name)
        key = res.compound["compound_key"] if res.status == "exact" else None
        if key is None:
            unresolved.append(name)
        label = res.compound["display_name"] if res.status == "exact" else name
        resolved.append((label, key))
    pairs = []
    for (a_name, a_key), (b_name, b_key) in itertools.combinations(resolved, 2):
        result, note = compatibility.lookup(a_key, b_key)
        pairs.append({"pair": [a_name, b_name], "result": result, "note": note})
    data = {
        "pairs": pairs,
        "unresolved": unresolved,
        "any_incompatible": any(p["result"] == compatibility.INCOMPATIBLE for p in pairs),
        "any_unknown": any(p["result"] == compatibility.UNKNOWN for p in pairs),
        "matrix_label": compatibility.MATRIX_LABEL,
        "expert_reviewed": compatibility.MATRIX_REVIEWED,
    }
    counts = {r: sum(1 for p in pairs if p["result"] == r) for r in
              (compatibility.INCOMPATIBLE, compatibility.UNKNOWN, compatibility.NO_KNOWN_ISSUE)}
    summary = (f"{len(pairs)} pair(s): {counts['incompatible']} incompatible, {counts['unknown']} unknown, "
               f"{counts['no_known_issue']} no known issue. {compatibility.MATRIX_LABEL}")
    return ToolResult(True, data, None, summary)


def tool_diagnose_hardware_error(ctx: ToolContext, args: DiagnoseArgs) -> ToolResult:
    """Resolution steps for an instrument error code, from the verified troubleshooting entries.

    With only an ``upload_id`` the tool returns the code(s) read from the image and
    ``needs_confirmation=True``; the user must confirm the code before any fix is returned.
    """
    upload = None
    if args.upload_id:
        upload_sql = "SELECT upload_id, equipment_type, observed_text_json, extracted_codes_json FROM agent_uploads WHERE upload_id = ? AND organization_id = ?"
        upload_params: list[Any] = [args.upload_id, ctx.organization_id]
        if ctx.team_id is not None:
            upload_sql += " AND team_id = ?"
            upload_params.append(ctx.team_id)
        row = ctx.conn.execute(upload_sql, upload_params).fetchone()
        if row is None:
            return ToolResult.fail("Upload not found.")
        upload = {"upload_id": row["upload_id"], "equipment_type": row["equipment_type"],
                  "observed_text": json.loads(row["observed_text_json"] or "[]"),
                  "extracted_codes": json.loads(row["extracted_codes_json"] or "[]")}

    if not args.error_code:
        codes = upload["extracted_codes"]
        data = {"needs_confirmation": True, "extracted_codes": codes, "extracted_code": codes[0] if codes else None,
                "equipment_type": upload["equipment_type"], "observed_text": upload["observed_text"],
                "resolution_steps": [], "ppe_warnings": []}
        if not codes:
            return ToolResult(True, data, None, "No error code could be read from the image; ask the user to type it.")
        return ToolResult(True, data, None, f"Read {', '.join(codes)} from the image; awaiting user confirmation.")

    code = normalise_code(args.error_code)
    ts_sql = "SELECT * FROM troubleshooting WHERE verified = 1 AND error_code IS NOT NULL AND organization_id = ?"
    ts_params: list[Any] = [ctx.organization_id]
    if ctx.team_id is not None:
        ts_sql += " AND team_id = ?"
        ts_params.append(ctx.team_id)
    matches = [dict(r) for r in ctx.conn.execute(ts_sql, ts_params) if normalise_code(r["error_code"]) == code]
    data: dict[str, Any] = {"needs_confirmation": False, "error_code": code, "matches": [],
                            "resolution_steps": [], "ppe_warnings": [], "upload": upload}
    if not matches:
        return ToolResult(True, data, None, f"No verified troubleshooting entry for {code}.")
    for m in matches:
        data["matches"].append({
            "equipment": m["equipment"],
            "machine_model": m.get("machine_model"),
            "symptom": m["symptom"],
            "probable_cause": m["probable_cause"],
            "resolution_steps": [s.strip() for s in re.split(r"(?<=\.)\s+", m["fix"]) if s.strip()],
            "ppe_warning": m.get("ppe_warning") or NO_PPE_RECORDED,
            "source_document": m.get("source_document"),
            "source_page": m.get("source_page"),
        })
    best = data["matches"][0]
    data["machine_model"] = best["machine_model"] or best["equipment"]
    data["resolution_steps"] = best["resolution_steps"]
    data["ppe_warnings"] = sorted({m["ppe_warning"] for m in data["matches"]})
    return ToolResult(True, data, None,
                      f"{code} on {data['machine_model']}: {len(best['resolution_steps'])} resolution step(s) "
                      f"from {best['source_document'] or 'the troubleshooting database'}.")


# --------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[[ToolContext, Any], ToolResult]
    args_model: type[_Args]
    description: str

    def schema(self) -> dict[str, Any]:
        """Anthropic tool definition (name, description, JSON input schema)."""
        return {"name": self.name, "description": self.description,
                "input_schema": self.args_model.model_json_schema()}


TOOLS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("query_sop_knowledge", tool_query_sop_knowledge, QuerySopArgs,
             "Search the uploaded SOPs. Returns passages with chunk_id, document, page and similarity."),
    ToolSpec("calculate_preparation", tool_calculate_preparation, CalculateArgs,
             "Compute the mass of a compound needed for a target concentration and volume, using the "
             "molecular weight in the reagent database."),
    ToolSpec("check_reagent_inventory", tool_check_reagent_inventory, InventoryArgs,
             "Look up stock, location and expiry of a reagent; optionally check a required amount."),
    ToolSpec("verify_chemical_compatibility", tool_verify_chemical_compatibility, CompatibilityArgs,
             "Check reagent pairs against a limited demo compatibility matrix (not expert-reviewed)."),
    ToolSpec("diagnose_hardware_error", tool_diagnose_hardware_error, DiagnoseArgs,
             "Resolution steps for an instrument error code, or the code read from an uploaded image."),
]}


@dataclass
class ToolEnv:
    """Everything a tool run needs except the connection, which each run opens itself (read-only)."""

    open_conn: Callable[[], Any]
    rag: Any
    organization_id: int
    team_id: int | None = None
    timeout_s: float = 10.0
    today: date = field(default_factory=date.today)

    @classmethod
    def from_app(cls, app, organization_id: int | None = None, team_id: int | None = None) -> "ToolEnv":
        cfg = app.config
        pool = app.extensions.get("smartlab_db_pool")
        target = database_target(cfg)
        return cls(open_conn=lambda: connect_readonly(target, pool),
                   rag=app.extensions["smartlab"]["rag"],
                   organization_id=cfg["DEFAULT_ORGANIZATION_ID"] if organization_id is None else organization_id,
                   team_id=cfg.get("DEFAULT_TEAM_ID") if team_id is None else team_id,
                   timeout_s=cfg.get("AGENT_TOOL_TIMEOUT_S", 10.0))


_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-tool")


def _invoke(spec: ToolSpec, args: _Args, env: ToolEnv) -> ToolResult:
    conn = env.open_conn()
    try:
        return spec.fn(ToolContext(conn, env.rag, env.organization_id, env.today, env.team_id), args)
    finally:
        conn.close()


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def run_tool(name: str, raw_args: dict[str, Any] | None, env: ToolEnv) -> tuple[ToolResult, int]:
    """Validate arguments, run the tool on a read-only connection under a timeout.

    Never raises: invalid arguments, timeouts and unexpected errors all come back as
    ``ok=False`` with a short reason (details go to the server log). Returns ``(result, duration_ms)``.
    """
    start = time.perf_counter()

    def done(result: ToolResult) -> tuple[ToolResult, int]:
        return result, int((time.perf_counter() - start) * 1000)

    spec = TOOLS.get(name)
    if spec is None:
        return done(ToolResult.fail(f"Unknown tool '{name}'."))
    if raw_args is not None and not isinstance(raw_args, dict):
        return done(ToolResult.fail("Tool arguments must be an object."))
    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        return done(ToolResult.fail(f"Invalid arguments for {name}: {_validation_message(exc)}"))
    future = _EXECUTOR.submit(_invoke, spec, args, env)
    try:
        return done(future.result(timeout=env.timeout_s))
    except FuturesTimeout:
        future.cancel()
        logger.warning("Tool %s timed out after %ss", name, env.timeout_s)
        return done(ToolResult.fail(f"{name} timed out after {fmt_number(env.timeout_s)} s."))
    except Exception:  # noqa: BLE001 - a tool failure must never crash the request
        logger.exception("Tool %s failed", name)
        return done(ToolResult.fail(f"{name} failed with an internal error."))
