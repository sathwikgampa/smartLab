"""AI-powered literature search.

Search -> summarise -> cross-reference:

1. **Search** Europe PMC's public REST API (no key required). When offline or
   when the API fails, a small bundled sample corpus is searched instead and the
   response is labelled ``source: "offline-sample"``.
2. **Summarise** each abstract: Claude writes a two-sentence summary grounded in
   the abstract when available; otherwise the two sentences most relevant to the
   query are extracted verbatim.
3. **Cross-reference** each paper against the internal SOP index so scientists
   see which in-house documents cover the same methods.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

import requests

from ..rag.embeddings import tokenize
from ..rag.pdf_extractor import split_sentences
from .llm import ClaudeClient, LLMUnavailable

logger = logging.getLogger(__name__)

EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

OFFLINE_CORPUS = [
    {
        "id": "sample-1", "title": "Optimisation of HEK293 transient transfection for recombinant protein yield",
        "authors": "Sample A, Example B", "journal": "Offline Sample Corpus", "year": 2023, "doi": None,
        "abstract": "Transient transfection of HEK293 cells with PEI remains a cost-effective route to recombinant "
                    "protein. We evaluated DNA:PEI ratios, cell density at transfection and post-transfection "
                    "temperature shift. A 1:3 DNA:PEI ratio at 2 million cells per mL with a shift to 32 °C after "
                    "24 h increased titres two-fold. Viability above 90% at transfection was critical.",
    },
    {
        "id": "sample-2", "title": "Root causes of high back-pressure in analytical HPLC: a systematic review",
        "authors": "Example C", "journal": "Offline Sample Corpus", "year": 2022, "doi": None,
        "abstract": "High system back-pressure is the most common HPLC fault. Blocked inline filters, guard column "
                    "fouling and buffer precipitation in organic-rich gradients account for most events. Flushing "
                    "with warm water before organic solvent and filtering mobile phases through 0.22 µm membranes "
                    "reduced pressure faults by 70% across 14 laboratories.",
    },
    {
        "id": "sample-3", "title": "Mycoplasma testing strategies for cell banks: PCR versus culture",
        "authors": "Sample D, Example E", "journal": "Offline Sample Corpus", "year": 2024, "doi": None,
        "abstract": "Mycoplasma contamination compromises cell-based assays. We compared qPCR detection with the "
                    "compendial culture method across 312 cell bank samples. qPCR achieved 98% concordance with a "
                    "turnaround of one day versus 28 days for culture. Routine monthly PCR screening is recommended "
                    "for actively passaged lines.",
    },
    {
        "id": "sample-4", "title": "Stability of trypsin-EDTA under repeated freeze-thaw cycles",
        "authors": "Example F", "journal": "Offline Sample Corpus", "year": 2021, "doi": None,
        "abstract": "Trypsin-EDTA activity declined by 25% after five freeze-thaw cycles. Aliquoting into single-use "
                    "volumes and storage at -20 °C preserved activity for 12 months. Thawed aliquots kept at 4 °C "
                    "retained activity for two weeks.",
    },
    {
        "id": "sample-5", "title": "qPCR efficiency and Ct reproducibility in pharmaceutical bioassays",
        "authors": "Sample G", "journal": "Offline Sample Corpus", "year": 2023, "doi": None,
        "abstract": "Amplification efficiency between 90% and 105% is required for reliable relative quantification. "
                    "Inhibitors carried over from extraction reduced efficiency and delayed Ct values by up to three "
                    "cycles. Dilution series of template identified inhibition in 11% of samples.",
    },
]


def _search_europe_pmc(query: str, limit: int, timeout: float, from_year: int | None = None,
                       to_year: int | None = None, sort: str = "relevance") -> list[dict[str, Any]]:
    q = query
    if from_year or to_year:
        q = f"({query}) AND PUB_YEAR:[{from_year or 1800} TO {to_year or 3000}]"
    params = {"query": q, "format": "json", "resultType": "core", "pageSize": limit}
    if sort == "date":
        params["sort"] = "P_PDATE_D desc"
    resp = requests.get(EUROPE_PMC_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    papers = []
    for item in resp.json().get("resultList", {}).get("result", []):
        abstract = re.sub(r"<[^>]+>", "", item.get("abstractText") or "")
        papers.append({
            "id": item.get("id"),
            "title": re.sub(r"<[^>]+>", "", item.get("title") or "Untitled"),
            "authors": item.get("authorString") or "",
            "journal": (item.get("journalInfo") or {}).get("journal", {}).get("title") or item.get("source", ""),
            "year": int(item["pubYear"]) if str(item.get("pubYear", "")).isdigit() else None,
            "doi": item.get("doi"),
            "url": f"https://doi.org/{item['doi']}" if item.get("doi") else
                   f"https://europepmc.org/article/{item.get('source')}/{item.get('id')}",
            "abstract": abstract,
            "publication_date": item.get("firstPublicationDate"),
            "is_open_access": item.get("isOpenAccess") == "Y",
            "is_sample": False,
        })
    return papers


def _search_offline(query: str, limit: int, from_year: int | None = None, to_year: int | None = None,
                    sort: str = "relevance") -> list[dict[str, Any]]:
    q = set(tokenize(query))
    scored = []
    for paper in OFFLINE_CORPUS:
        if (from_year and paper["year"] < from_year) or (to_year and paper["year"] > to_year):
            continue
        terms = set(tokenize(paper["title"] + " " + paper["abstract"]))
        overlap = len(q & terms)
        if overlap:
            scored.append((overlap, {**paper, "url": None, "publication_date": None, "is_open_access": False,
                                     "is_sample": True}))
    scored.sort(key=lambda t: (t[1]["year"] if sort == "date" else t[0]), reverse=True)
    return [p for _, p in scored[:limit]]


def extractive_summary(abstract: str, query: str, n: int = 2) -> str:
    sentences = split_sentences(abstract)
    if not sentences:
        return "No abstract available."
    q = set(tokenize(query))
    ranked = sorted(range(len(sentences)), key=lambda i: (-len(q & set(tokenize(sentences[i]))), i))[:n]
    return " ".join(sentences[i] for i in sorted(ranked))


def search(conn: sqlite3.Connection, rag_engine, llm: ClaudeClient, query: str, *, limit: int = 5,
           online: bool = True, timeout: float = 8.0, from_year: int | None = None, to_year: int | None = None,
           sort: str = "relevance", team_id: int | None = None) -> dict[str, Any]:
    query = (query or "").strip()
    if len(query) < 3:
        raise ValueError("Query must be at least 3 characters.")
    if from_year and to_year and from_year > to_year:
        raise ValueError("from_year must not be after to_year.")
    if sort not in ("relevance", "date"):
        raise ValueError("sort must be 'relevance' or 'date'.")
    limit = max(1, min(int(limit), 20))
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source, warnings = "europe-pmc", []
    papers: list[dict[str, Any]] = []
    if online:
        try:
            papers = _search_europe_pmc(query, limit, timeout, from_year, to_year, sort)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Europe PMC search failed: %s", exc)
            warnings.append("Europe PMC could not be reached, so only the built-in sample records are shown.")
    if not online or warnings:
        source = "offline-sample"
        papers = _search_offline(query, limit, from_year, to_year, sort)
        warnings.append("Sample records are illustrative placeholders, not real publications. "
                        "Connect to Europe PMC to search the literature.")

    for paper in papers:
        abstract = paper.get("abstract") or ""
        summary, mode = (extractive_summary(abstract, query), "extractive") if abstract else (None, "unavailable")
        if llm.available and abstract:
            try:
                summary = llm.complete(
                    "Summarise the abstract in at most two sentences for a laboratory scientist. Use only facts "
                    "stated in the abstract; do not add conclusions.",
                    f"Research question: {query}\n\nAbstract:\n{abstract}",
                    max_tokens=1000,
                )
                mode = "llm"
            except LLMUnavailable:
                pass
        paper["summary"] = summary
        paper["summary_mode"] = mode
        related = rag_engine.retrieve(conn, paper["title"] + ". " + abstract[:600], k=3, team_id=team_id) if abstract else []
        paper["related_documents"] = [
            {"document_id": e.document_id, "document_title": e.document_title, "page": e.page,
             "score": round(e.score, 3)}
            for e in related if e.score >= rag_engine.min_score
        ]
    return {"query": query, "source": source, "papers": papers, "warnings": warnings, "retrieved_at": retrieved_at,
            "filters": {"from_year": from_year, "to_year": to_year, "sort": sort}}
