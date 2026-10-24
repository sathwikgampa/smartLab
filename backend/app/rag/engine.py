"""Retrieval-augmented question answering with page-level citations.

Pipeline: PDF -> page-aware chunks (SQLite) -> embeddings -> vector store.
At query time the engine retrieves the top-k chunks, discards anything below the
evidence threshold, and either

* asks Claude to answer *only* from numbered sources with ``[S#]`` markers, or
* (offline) composes an extractive answer from the best-matching sentences.

Either way, citations are validated against the retrieved evidence: markers that
reference unknown sources are dropped, and an answer with no valid citation is
never returned as grounded. When evidence is missing or weak the response says
so explicitly instead of guessing.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..guardrails import screen_query
from ..security import ValidatedFile
from ..services.llm import ClaudeClient, LLMUnavailable
from .embeddings import Embedder, tokenize
from .pdf_extractor import chunk_pages, extract_pages, extract_title, is_heading, split_sentences

logger = logging.getLogger(__name__)

_SOURCE_MARKER = re.compile(r"\[S(\d+)\]")
INSUFFICIENT_TOKEN = "INSUFFICIENT_EVIDENCE"

ANSWER_SYSTEM_PROMPT = """You are SmartLab AI, a laboratory knowledge assistant for pharmaceutical and \
biotechnology scientists.

Answer the question using ONLY the numbered sources provided. Rules:
- Every sentence that states a fact, value, step or condition must end with one or more citation \
markers such as [S1] or [S2][S3], referring to the sources that support it.
- Never invent or extrapolate concentrations, temperatures, times, volumes, part numbers or error \
codes. If a value is not in the sources, say it is not documented.
- If the sources do not contain the answer, reply with the single word INSUFFICIENT_EVIDENCE \
followed by one sentence describing what information is missing.
- If the sources only partly answer the question, answer the supported part and state clearly what \
is not covered.
- Do not identify unknown substances or organisms and do not give medical or dosing advice for people.
- Format with concise Markdown (short paragraphs, numbered steps for procedures). LaTeX math may be \
written as $...$."""


class DuplicateDocumentError(ValueError):
    def __init__(self, existing: dict[str, Any]):
        super().__init__(f"Document already indexed as '{existing['title']}'.")
        self.existing = existing


APPROVAL_STATUSES = ("approved", "pending", "outdated", "unreviewed")
DOCUMENT_METADATA = ("category", "doc_code", "version", "owner", "approval_status")


@dataclass(frozen=True)
class Evidence:
    chunk_id: int
    document_id: int
    document_title: str
    page: int
    text: str
    score: float
    doc_type: str = "SOP"
    category: str = "General"
    doc_code: str | None = None
    version: str | None = None
    approval_status: str = "unreviewed"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["score"] = round(self.score, 4)
        return d


class RAGEngine:
    def __init__(self, embedder: Embedder, store, llm: ClaudeClient, *, chunk_size: int = 700,
                 chunk_overlap: int = 120, top_k: int = 5, min_score: float | None = None):
        self.embedder = embedder
        self.store = store
        self.llm = llm
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.top_k = top_k
        self.min_score = embedder.default_min_score if min_score is None else min_score
        # organization_id -> ids of its chunks. Only ever narrows a search, and is cleared by every index
        # mutation in this process, so a stale entry can drop a hit but never admit another org's chunk.
        self._scope_cache: dict[int, frozenset[int]] = {}
        self._scope_lock = threading.Lock()

    # ------------------------------------------------------------------ indexing
    def ensure_index(self, conn: sqlite3.Connection) -> None:
        """Load the persisted index, rebuilding it from SQLite if missing or stale."""
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        loaded = self.store.load()
        if chunk_count and (not loaded or len(self.store) != chunk_count):
            logger.info("Rebuilding vector index from %d stored chunks.", chunk_count)
            self.rebuild(conn)

    def rebuild(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, text FROM chunks ORDER BY id").fetchall()
        self.store.remove([r["id"] for r in rows])
        for start in range(0, len(rows), 256):
            batch = rows[start:start + 256]
            self.store.add([r["id"] for r in batch], self.embedder.embed([r["text"] for r in batch]))
        self.store.save()
        self._invalidate_scope()

    def ingest(self, conn: sqlite3.Connection, file: ValidatedFile, upload_dir: Path, *,
               title: str | None = None, doc_type: str = "SOP",
               metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        existing = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (file.sha256,)).fetchone()
        if existing:
            raise DuplicateDocumentError(dict(existing))

        pages = extract_pages(file.data)
        chunks = chunk_pages(pages, self.chunk_size, self.chunk_overlap)
        if not chunks:
            raise ValueError("No extractable text was found in this PDF (scanned PDFs need OCR first).")
        title = (title or "").strip() or extract_title(file.data, Path(file.safe_name).stem)
        meta = clean_metadata(metadata or {})
        # Embed before writing anything, so no database transaction stays open while the model runs.
        vectors = self.embedder.embed([c.text for c in chunks])

        upload_dir.mkdir(parents=True, exist_ok=True)
        path = upload_dir / file.stored_name
        path.write_bytes(file.data)
        try:
            cols = ["stored_name", "original_name", "title", "doc_type", "page_count", "chunk_count", "size_bytes",
                    "sha256", *meta]
            values = [file.stored_name, file.original_name, title[:200], doc_type[:40], len(pages), len(chunks),
                      file.size, file.sha256, *meta.values()]
            cur = conn.execute(f"INSERT INTO documents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               values)
            doc_id = cur.lastrowid
            chunk_ids = self._insert_chunks(conn, doc_id, chunks)
            conn.commit()
        except Exception:
            conn.rollback()
            path.unlink(missing_ok=True)
            raise
        self.store.add(chunk_ids, vectors)  # only committed rows enter the index
        self.store.save()
        self._invalidate_scope()
        return dict(conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())

    _INSERT_BATCH = 500  # rows per statement: 2,000 parameters, well inside SQLite's and PostgreSQL's limits

    @classmethod
    def _insert_chunks(cls, conn: sqlite3.Connection, doc_id: int, chunks) -> list[int]:
        """Insert ``chunks`` with multi-row INSERTs (one round trip per batch rather than per chunk) and
        return their ids in ``chunks`` order. Ids are matched by ``(page, chunk_index)``, which is unique within
        a document (``chunk_index`` restarts on each page), because PostgreSQL does not guarantee the order of
        ``RETURNING`` rows."""
        ids: dict[tuple[int, int], int] = {}
        for start in range(0, len(chunks), cls._INSERT_BATCH):
            batch = chunks[start:start + cls._INSERT_BATCH]
            rows = conn.execute(
                "INSERT INTO chunks (document_id, page, chunk_index, text) VALUES "
                + ", ".join(["(?, ?, ?, ?)"] * len(batch)) + " RETURNING id, page, chunk_index",
                [value for c in batch for value in (doc_id, c.page, c.index, c.text)],
            ).fetchall()
            ids.update({(r["page"], r["chunk_index"]): r["id"] for r in rows})
        return [ids[(c.page, c.index)] for c in chunks]

    def reindex(self, conn: sqlite3.Connection, document_id: int, upload_dir: Path) -> dict[str, Any] | None:
        """Re-extract and re-embed a stored PDF (e.g. after changing chunk settings)."""
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if doc is None:
            return None
        path = upload_dir / doc["stored_name"]
        if not path.exists():
            raise FileNotFoundError("The stored PDF for this document is missing.")
        pages = extract_pages(path.read_bytes())
        chunks = chunk_pages(pages, self.chunk_size, self.chunk_overlap)
        if not chunks:
            raise ValueError("No extractable text was found in this PDF.")
        vectors = self.embedder.embed([c.text for c in chunks])
        old_ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,))]
        try:
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            new_ids = self._insert_chunks(conn, document_id, chunks)
            conn.execute("UPDATE documents SET page_count = ?, chunk_count = ?, status = 'indexed', "
                         "updated_at = datetime('now') WHERE id = ?", (len(pages), len(chunks), document_id))
            conn.commit()
        except Exception:
            conn.rollback()  # the index was not touched, so it still matches the restored rows
            raise
        self.store.remove(old_ids)
        self.store.add(new_ids, vectors)
        self.store.save()
        self._invalidate_scope()
        return dict(conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone())

    def delete(self, conn: sqlite3.Connection, document_id: int, upload_dir: Path) -> bool:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not doc:
            return False
        ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,))]
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        conn.commit()
        self.store.remove(ids)
        self.store.save()
        self._invalidate_scope()
        (upload_dir / doc["stored_name"]).unlink(missing_ok=True)
        return True

    # ----------------------------------------------------------------- retrieval
    def retrieve(self, conn: sqlite3.Connection, query: str, k: int | None = None,
                 document_ids: list[int] | None = None, organization_id: int | None = None) -> list[Evidence]:
        """Top-k chunks for ``query``. ``document_ids`` and ``organization_id`` restrict the search space
        before ranking, so out-of-scope chunks can never be returned."""
        query = (query or "").strip()
        if not query:
            return []
        k = k or self.top_k
        allowed: set[int] | frozenset[int] | None = None
        if document_ids:
            sql = (f"SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id "
                   f"WHERE c.document_id IN ({','.join('?' * len(document_ids))})")
            params: list[Any] = list(document_ids)
            if organization_id is not None:
                sql += " AND d.organization_id = ?"
                params.append(organization_id)
            allowed = {r[0] for r in conn.execute(sql, params)}
            if not allowed:
                return []
        elif organization_id is not None:
            allowed = self._org_chunk_ids(conn, organization_id)
            if not allowed:
                return []
        hits = self.store.search(self.embedder.embed([query])[0], k * 3, allowed)
        if not hits:
            return []
        scores = dict(hits)
        placeholders = ",".join("?" * len(scores))
        rows = conn.execute(
            f"SELECT c.id, c.document_id, c.page, c.text, d.title, d.doc_type, d.category, d.doc_code, "
            f"d.version, d.approval_status FROM chunks c "
            f"JOIN documents d ON d.id = c.document_id WHERE c.id IN ({placeholders})",
            list(scores),
        ).fetchall()
        q_terms = set(tokenize(query))
        evidence = []
        for r in rows:
            evidence.append(Evidence(r["id"], r["document_id"], r["title"], r["page"], r["text"], scores[r["id"]],
                                     r["doc_type"], r["category"], r["doc_code"], r["version"], r["approval_status"]))

        # Light lexical re-rank: exact term overlap matters for error codes and part numbers.
        # Outdated documents rank slightly lower so a current version wins ties.
        def rank(e: Evidence) -> float:
            overlap = len(q_terms & set(tokenize(e.text))) / len(q_terms) if q_terms else 0.0
            return e.score + 0.1 * overlap - (0.05 if e.approval_status == "outdated" else 0.0)

        evidence.sort(key=rank, reverse=True)
        return evidence[:k]

    def _org_chunk_ids(self, conn: sqlite3.Connection, organization_id: int) -> frozenset[int]:
        """Ids of the organisation's chunks, cached until the next index mutation in this process."""
        with self._scope_lock:
            cached = self._scope_cache.get(organization_id)
        if cached is not None:
            return cached
        ids = frozenset(r[0] for r in conn.execute(
            "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.organization_id = ?",
            (organization_id,)))
        with self._scope_lock:
            self._scope_cache[organization_id] = ids
        return ids

    def _invalidate_scope(self) -> None:
        with self._scope_lock:
            self._scope_cache.clear()

    # ------------------------------------------------------------------- answers
    def answer(self, conn: sqlite3.Connection, question: str, k: int | None = None,
               document_ids: list[int] | None = None) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            raise ValueError("Question must not be empty.")
        guard = screen_query(question)
        retrieved = self.retrieve(conn, question, k, document_ids)
        evidence, source_issues = resolve_versions([e for e in retrieved if e.score >= self.min_score])

        if not evidence:
            return self._insufficient(question, guard, retrieved,
                                      "No passage in the indexed documents is relevant enough to this question.")

        mode = "extractive"
        body, used = None, []
        service_notice = None
        if self.llm.available:
            try:
                body, used = self._llm_answer(question, evidence)
                mode = "llm"
            except LLMUnavailable as exc:
                logger.info("LLM answer unavailable (%s); using extractive answer.", exc)
                service_notice = ("The AI language service was unavailable, so this answer was assembled directly "
                                  "from matching document sentences.")
            except _InsufficientFromLLM as exc:
                return self._insufficient(question, guard, retrieved, exc.reason)
        if body is None:
            body, used = self._extractive_answer(question, evidence)

        citations = [
            {
                "n": n,
                "chunk_id": e.chunk_id,
                "document_id": e.document_id,
                "document_title": e.document_title,
                "page": e.page,
                "snippet": _snippet(e.text, question),
                "score": round(e.score, 4),
                "doc_type": e.doc_type,
                "category": e.category,
                "doc_code": e.doc_code,
                "version": e.version,
                "approval_status": e.approval_status,
            }
            for n, e in enumerate(used, start=1)
        ]
        for e in {e.document_id: e for e in used}.values():
            if e.approval_status != "approved":
                source_issues.append({
                    "type": f"{e.approval_status}_source",
                    "document_id": e.document_id,
                    "message": f"'{e.document_title}' is {_STATUS_PHRASE.get(e.approval_status, e.approval_status)}. "
                               "Confirm against the current approved procedure before relying on it.",
                })
        unverified = any(i["type"] != "superseded_version_excluded" for i in source_issues)

        top = max(e.score for e in evidence)
        confidence = "high" if top >= self.min_score * 2.3 else "medium" if top >= self.min_score * 1.4 else "low"
        missing = self._missing_terms(question, evidence)
        warnings = list(guard.warnings)
        status = "grounded"
        if confidence == "low" or len(missing) > len(set(tokenize(question))) / 2:
            status = "partial"
            warnings.append(
                "Evidence for this answer is limited. Verify against the cited pages before acting on it."
                + (f" Not found in the documents: {', '.join(missing)}." if missing else "")
            )

        parts = []
        if guard.flags:
            parts.append("> **Safety notice:** " + " ".join(guard.warnings))
        if unverified and status == "grounded":
            status = "partial"
        if status == "partial":
            parts.append("> **Limited evidence:** the indexed documents only partly cover this question.")
        parts.append(body)
        parts.append(_sources_footer(citations))
        if service_notice:
            warnings.append(service_notice)
        requires_verification = bool(guard.flags) or unverified or any(c["category"] == "Safety" for c in citations)
        return {
            "source_issues": source_issues,
            "requires_verification": requires_verification,
            "service_notice": service_notice,
            "answer": "\n\n".join(parts),
            "citations": citations,
            "evidence_status": status,
            "confidence": confidence,
            "warnings": warnings,
            "guardrails": guard.to_dict(),
            "missing_terms": missing,
            "mode": mode,
            "retrieved": [e.to_dict() for e in retrieved],
        }

    def _insufficient(self, question: str, guard, retrieved: list[Evidence], reason: str) -> dict[str, Any]:
        lines = [
            "**I could not find supporting evidence for this in the indexed documents.**",
            reason,
            "I won't guess on laboratory procedures, values or safety steps without a source. You can:",
            "- upload the relevant SOP, manual or datasheet,\n- rephrase using terms from the document, or\n"
            "- check the reagent inventory and troubleshooting database via unified search.",
        ]
        if guard.flags:
            lines.insert(0, "> **Safety notice:** " + " ".join(guard.warnings))
        nearest = [e.to_dict() for e in retrieved[:3]]
        return {
            "source_issues": [],
            "requires_verification": bool(guard.flags),
            "service_notice": None,
            "answer": "\n\n".join(lines),
            "citations": [],
            "evidence_status": "insufficient",
            "confidence": "none",
            "warnings": list(guard.warnings) + ["Missing evidence: " + reason],
            "guardrails": guard.to_dict(),
            "missing_terms": sorted(set(tokenize(question)))[:8],
            "mode": "none",
            "retrieved": nearest,
        }

    def _llm_answer(self, question: str, evidence: list[Evidence]) -> tuple[str, list[Evidence]]:
        sources = "\n\n".join(
            f'<source id="S{i}" title="{e.document_title}" page="{e.page}">\n{e.text}\n</source>'
            for i, e in enumerate(evidence, start=1)
        )
        prompt = f"<sources>\n{sources}\n</sources>\n\nQuestion: {question}"
        text = self.llm.complete(ANSWER_SYSTEM_PROMPT, prompt, max_tokens=4000)
        if text.startswith(INSUFFICIENT_TOKEN):
            raise _InsufficientFromLLM(text[len(INSUFFICIENT_TOKEN):].strip(" :.-") or
                                       "The retrieved passages do not answer the question.")
        return renumber_citations(text, evidence)

    def _extractive_answer(self, question: str, evidence: list[Evidence]) -> tuple[str, list[Evidence]]:
        q_terms = set(tokenize(question))
        scored: list[tuple[float, int, str]] = []
        for idx, e in enumerate(evidence):
            for sentence in split_sentences(e.text):
                terms = set(tokenize(sentence))
                if len(sentence) < 25 or not terms or is_heading(sentence):
                    continue
                overlap = len(q_terms & terms)
                if overlap == 0:
                    continue
                scored.append((overlap / (len(q_terms) or 1) + e.score, idx, sentence))
        if not scored:  # fall back to the leading sentence of the best chunk(s)
            scored = [(e.score, i, split_sentences(e.text)[0]) for i, e in enumerate(evidence[:2])]
        scored.sort(key=lambda t: t[0], reverse=True)

        picked, seen = [], set()
        for _, idx, sentence in scored:
            key = sentence.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            picked.append((idx, sentence))
            if len(picked) == 4:
                break

        order: list[int] = []
        for idx, _ in picked:
            if idx not in order:
                order.append(idx)
        number = {idx: n for n, idx in enumerate(order, start=1)}
        lines = ["Based on the indexed laboratory documents:", ""]
        lines += [f"- {sentence.rstrip()} [{number[idx]}]" for idx, sentence in picked]
        return "\n".join(lines), [evidence[i] for i in order]

    @staticmethod
    def _missing_terms(question: str, evidence: list[Evidence]) -> list[str]:
        present = set()
        for e in evidence:
            present |= set(tokenize(e.text))
        return sorted(t for t in set(tokenize(question)) if t not in present and len(t) > 2)


class _InsufficientFromLLM(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_STATUS_PHRASE = {"pending": "pending review", "outdated": "marked outdated", "unreviewed": "not yet reviewed"}


def clean_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Validate document metadata (category, code, version, owner, approval status)."""
    clean: dict[str, Any] = {}
    for key in DOCUMENT_METADATA:
        if key not in meta:
            continue
        value = meta[key]
        value = str(value).strip()[:120] if value not in (None, "") else None
        if key == "approval_status" and value not in APPROVAL_STATUSES:
            raise ValueError(f"approval_status must be one of: {', '.join(APPROVAL_STATUSES)}.")
        if key == "category" and not value:
            value = "General"
        clean[key] = value
    return clean


def resolve_versions(evidence: list[Evidence]) -> tuple[list[Evidence], list[dict[str, Any]]]:
    """Prefer the current approved version when several versions of one document match.

    Documents sharing a ``doc_code`` are versions of the same procedure. If an
    approved version is among the matches, passages from the other versions are
    dropped and the exclusion is reported, because the versions may disagree.
    """
    approved = {e.doc_code: e for e in evidence if e.doc_code and e.approval_status == "approved"}
    kept, issues, seen = [], [], set()
    for e in evidence:
        current = approved.get(e.doc_code) if e.doc_code else None
        if current is not None and e.approval_status != "approved":
            if e.document_id not in seen:
                seen.add(e.document_id)
                issues.append({
                    "type": "superseded_version_excluded",
                    "document_id": e.document_id,
                    "message": f"Version {e.version or '(unversioned)'} of {e.doc_code}, which is "
                               f"{_STATUS_PHRASE.get(e.approval_status, e.approval_status)}, also matched. It was left "
                               f"out in favour of the approved version {current.version or '(unversioned)'}. "
                               "The two versions may give different instructions.",
                })
            continue
        kept.append(e)
    return kept, issues


def renumber_citations(text: str, evidence: list[Evidence]) -> tuple[str, list[Evidence]]:
    """Map ``[S#]`` markers to sequential ``[n]`` markers, dropping unknown sources.

    Raises :class:`LLMUnavailable` if no valid citation remains, so the caller
    falls back to the extractive path rather than returning an ungrounded answer.
    """
    order: list[int] = []

    def replace(match: re.Match) -> str:
        idx = int(match.group(1)) - 1
        if not 0 <= idx < len(evidence):
            return ""  # hallucinated source id
        if idx not in order:
            order.append(idx)
        return f"[{order.index(idx) + 1}]"

    renumbered = _SOURCE_MARKER.sub(replace, text)
    renumbered = re.sub(r"(\[\d+\])(?:\1)+", r"\1", renumbered)  # collapse duplicates like [1][1]
    if not order:
        raise LLMUnavailable("Model answer contained no valid citations.")
    return renumbered.strip(), [evidence[i] for i in order]


def verify_citations(answer: str, citations: list[dict[str, Any]], conn: sqlite3.Connection) -> list[str]:
    """Return a list of grounding problems (empty when every citation checks out).

    Checks that each inline marker has a citation entry, each citation is used,
    and each cited snippet really occurs on the cited page of the cited document.
    """
    problems = []
    markers = {int(m) for m in re.findall(r"\[(\d+)\]", answer.split("\n---\n")[0])}
    numbers = {c["n"] for c in citations}
    for m in sorted(markers - numbers):
        problems.append(f"Marker [{m}] has no citation.")
    for n in sorted(numbers - markers):
        problems.append(f"Citation {n} is never referenced in the answer.")
    for c in citations:
        row = conn.execute("SELECT text, page, document_id FROM chunks WHERE id = ?", (c["chunk_id"],)).fetchone()
        if row is None:
            problems.append(f"Citation {c['n']} points to a missing chunk.")
            continue
        if row["page"] != c["page"] or row["document_id"] != c["document_id"]:
            problems.append(f"Citation {c['n']} page/document does not match its chunk.")
        snippet = c["snippet"].replace("…", "").strip()
        if snippet and snippet not in " ".join(row["text"].split()):
            problems.append(f"Citation {c['n']} snippet is not on the cited page.")
    return problems


def _snippet(text: str, query: str, width: int = 220) -> str:
    """Best sentence window from ``text`` for ``query`` (verbatim, whitespace-normalised)."""
    text = " ".join(text.split())
    q_terms = set(tokenize(query))
    best, best_score = text[:width], -1
    for sentence in split_sentences(text):
        score = len(q_terms & set(tokenize(sentence)))
        if score > best_score:
            best, best_score = sentence, score
    best = best[:width]
    if best not in text:  # split_sentences may have merged newlines; stay verbatim
        best = text[:width]
    return best


def _sources_footer(citations: list[dict[str, Any]]) -> str:
    lines = ["---", "**Sources**"]
    lines += [f"{c['n']}. *{c['document_title']}*, p. {c['page']}" for c in citations]
    return "\n".join(lines)
