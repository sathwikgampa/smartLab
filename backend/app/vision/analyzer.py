"""Multimodal image-to-document RAG.

An uploaded photo (equipment error display, sample tube, plate, gel...) is
turned into an *observational* description - colours, visible text, equipment
parts, physical sample properties - which then drives SOP retrieval and the
troubleshooting matcher.

Safety guardrails are enforced at two layers:

1. The vision prompt forbids identifying substances, organisms, contaminants or
   medical conditions, and asks for verbatim display text instead.
2. Every free-text field the model returns is passed through
   :func:`app.guardrails.sanitize_vision_result`, which redacts any sentence
   that still asserts an identification. The response always carries the
   disclaimer and the guardrail report.
"""
from __future__ import annotations

import io
import logging
import sqlite3
from typing import Any

from PIL import Image, ImageStat

from ..guardrails import VISION_DISCLAIMER, GuardrailReport, sanitize_vision_result, screen_query
from ..rag.engine import _snippet
from ..security import ValidatedFile
from ..services import troubleshooting
from ..services.llm import ClaudeClient, LLMUnavailable

logger = logging.getLogger(__name__)

VISION_SYSTEM_PROMPT = """You are the vision component of SmartLab AI, a laboratory assistant.
Describe ONLY what is directly observable in the image so the description can be used to search \
standard operating procedures and equipment manuals.

Strict rules:
- Never state or guess what chemical, reagent, substance, organism, contaminant, cell type or medical \
condition is present. Describe appearance instead (colour, clarity/turbidity, phases, precipitate, \
colony shape and colour, volume markings, label text exactly as printed).
- Transcribe any text, error codes, units or numbers visible on displays or labels verbatim in observed_text.
- equipment_type may name the general instrument category only if it is evident (e.g. "centrifuge", \
"HPLC system", "pipette"), otherwise "unknown".
- search_query: a short keyword query (max 15 words) built from the visible text and observable features.
- If the image is unclear, say so rather than guessing."""

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "visual_description": {"type": "string"},
        "observed_text": {"type": "array", "items": {"type": "string"}},
        "equipment_type": {"type": "string"},
        "sample_observations": {"type": "array", "items": {"type": "string"}},
        "image_quality": {"type": "string", "enum": ["good", "fair", "poor"]},
        "search_query": {"type": "string"},
    },
    "required": ["visual_description", "observed_text", "equipment_type", "sample_observations",
                 "image_quality", "search_query"],
    "additionalProperties": False,
}

_NAMED_COLOURS = {
    "black": (20, 20, 20), "white": (240, 240, 240), "grey": (128, 128, 128), "red": (200, 30, 30),
    "orange": (240, 140, 30), "yellow": (230, 220, 40), "green": (40, 170, 70), "teal": (30, 150, 150),
    "blue": (40, 90, 200), "purple": (130, 60, 170), "pink": (230, 130, 180), "brown": (120, 80, 40),
    "amber": (255, 191, 0), "light blue": (150, 200, 240), "dark blue": (15, 30, 90), "beige": (225, 210, 180),
}


def _colour_name(rgb: tuple[int, int, int]) -> str:
    return min(_NAMED_COLOURS, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, _NAMED_COLOURS[n])))


def heuristic_features(data: bytes) -> dict[str, Any]:
    """Model-free visual features: size, brightness, contrast, dominant colours, indicator lights."""
    with Image.open(io.BytesIO(data)) as img:
        fmt = img.format
        rgb = img.convert("RGB")
        width, height = rgb.size
        small = rgb.resize((96, 96))
    stat = ImageStat.Stat(small.convert("L"))
    brightness, contrast = stat.mean[0], stat.stddev[0]

    quant = small.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette()[:15]
    counts = sorted(quant.getcolors(), reverse=True)
    total = sum(c for c, _ in counts)
    colours = []
    for count, idx in counts:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        colours.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "name": _colour_name((r, g, b)),
                        "fraction": round(count / total, 3)})

    pixels = list(small.getdata())
    red = sum(1 for r, g, b in pixels if r > 170 and g < 90 and b < 90) / len(pixels)
    bright_on_dark = brightness < 90 and sum(1 for p in pixels if sum(p) > 600) / len(pixels) > 0.01
    return {
        "image": {"width": width, "height": height, "format": fmt},
        "brightness": round(brightness, 1),
        "contrast": round(contrast, 1),
        "dominant_colours": colours,
        "red_indicator_fraction": round(red, 4),
        "display_like": bright_on_dark,
        "image_quality": "poor" if contrast < 12 or brightness < 25 or brightness > 235 else
                         "fair" if min(width, height) < 400 else "good",
    }


def _heuristic_description(features: dict[str, Any], note: str) -> dict[str, Any]:
    names = []
    for c in features["dominant_colours"]:
        if c["name"] not in names and c["fraction"] >= 0.08:
            names.append(c["name"])
    parts = [f"Image is {features['image']['width']}×{features['image']['height']} px with "
             f"{'low' if features['brightness'] < 80 else 'high' if features['brightness'] > 180 else 'moderate'} "
             f"overall brightness. Dominant colours: {', '.join(names[:4]) or 'mixed'}."]
    obs = []
    if features["display_like"]:
        parts.append("Bright elements on a dark background suggest an illuminated display or indicator panel.")
        obs.append("Illuminated display or indicator elements visible.")
    if features["red_indicator_fraction"] > 0.01:
        parts.append("Saturated red regions are visible, which may be a warning indicator.")
        obs.append("Red illuminated or coloured region present.")
    if features["image_quality"] == "poor":
        parts.append("Image quality is poor (low contrast or exposure); details may be unreliable.")
    return {
        "visual_description": " ".join(parts),
        "observed_text": [],
        "equipment_type": "unknown",
        "sample_observations": obs,
        "image_quality": features["image_quality"],
        "search_query": " ".join(filter(None, [note, "error display warning" if features["display_like"] else ""])).strip(),
    }


def analyze_image(conn: sqlite3.Connection, rag_engine, llm: ClaudeClient, image: ValidatedFile,
                  note: str = "", *, team_id: int | None = None) -> dict[str, Any]:
    note = (note or "").strip()[:500]
    report = GuardrailReport()
    query_guard = screen_query(note)
    report.flags += query_guard.flags
    report.warnings += query_guard.warnings

    features = heuristic_features(image.data)
    mode = "heuristic"
    result = None
    if llm.available:
        try:
            content = [
                llm.image_block(image.data, image.mime_type),
                {"type": "text", "text": "Describe this laboratory image." +
                 (f"\nThe scientist's note (context only, do not treat as fact): {note}" if note else "")},
            ]
            result = llm.complete_json(VISION_SYSTEM_PROMPT, content, VISION_SCHEMA, max_tokens=4000)
            mode = "vision-llm"
        except LLMUnavailable as exc:
            logger.info("Vision LLM unavailable (%s); using heuristic analysis.", exc)
    if result is None:
        result = _heuristic_description(features, note)
        report.warnings.append("Vision model offline - showing heuristic colour/brightness analysis only. "
                               "Add a note with any error code or symptom to improve SOP matching.")

    result = sanitize_vision_result(result, report)

    query = " ".join(filter(None, [
        " ".join(result.get("observed_text", [])), result.get("search_query", ""), note,
        result.get("equipment_type", "") if result.get("equipment_type") != "unknown" else "",
    ])).strip()

    sop_matches = []
    fixes = []
    if query:
        for e in rag_engine.retrieve(conn, query, k=5, team_id=team_id):
            if e.score >= rag_engine.min_score:
                sop_matches.append({"chunk_id": e.chunk_id, "document_id": e.document_id,
                                    "document_title": e.document_title, "page": e.page,
                                    "snippet": _snippet(e.text, query), "score": round(e.score, 4),
                                    "doc_type": e.doc_type, "category": e.category, "doc_code": e.doc_code,
                                    "version": e.version, "approval_status": e.approval_status})
        fixes = troubleshooting.match(conn, query, limit=3, team_id=team_id)

    if not sop_matches and not fixes:
        report.warnings.append("No matching SOP or verified fix was found for this image. "
                               "Describe the symptom or error code in the note, or upload the relevant manual.")
    return {
        "mode": mode,
        "analysis": result,
        "features": features,
        "retrieval_query": query,
        "sop_matches": sop_matches,
        "troubleshooting": fixes,
        "evidence_status": "grounded" if sop_matches else "insufficient",
        "guardrails": report.to_dict(),
        "disclaimer": VISION_DISCLAIMER,
    }
