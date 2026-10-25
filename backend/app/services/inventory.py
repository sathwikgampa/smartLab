"""Reagent inventory: stock levels, expiry tracking, storage lookup and NL queries."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from ..db import audit
from ..rag.embeddings import tokenize

EXPIRING_SOON_DAYS = 30

FIELDS = {
    "name": str, "category": str, "cas_number": str, "catalog_number": str, "lot_number": str, "quantity": float,
    "unit": str, "reorder_threshold": float, "location": str, "storage_condition": str,
    "hazard_class": str, "expiry_date": str, "supplier": str, "notes": str,
}
_CAS = re.compile(r"^\d{2,7}-\d{2}-\d$")


class InventoryError(ValueError):
    pass


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def annotate(reagent: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Add computed ``status``, ``days_to_expiry`` and ``low_stock`` fields."""
    today = today or date.today()
    r = dict(reagent)
    expiry = _parse_date(r.get("expiry_date"))
    r["days_to_expiry"] = (expiry - today).days if expiry else None
    r["low_stock"] = r["quantity"] <= r["reorder_threshold"]
    if expiry and expiry < today:
        r["status"] = "expired"
    elif r["quantity"] <= 0:
        r["status"] = "out_of_stock"
    elif r["low_stock"]:
        r["status"] = "low_stock"
    elif expiry and (expiry - today).days <= EXPIRING_SOON_DAYS:
        r["status"] = "expiring_soon"
    else:
        r["status"] = "ok"
    return r


def validate_payload(payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, cast in FIELDS.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "":
            clean[key] = None if cast is str else 0.0
            continue
        try:
            clean[key] = cast(value) if cast is float else str(value).strip()[:500]
        except (TypeError, ValueError) as exc:
            raise InventoryError(f"Field '{key}' must be a number.") from exc
    if not partial and not clean.get("name"):
        raise InventoryError("Reagent name is required.")
    if "name" in clean and partial and not clean["name"]:
        raise InventoryError("Reagent name cannot be empty.")
    for key in ("quantity", "reorder_threshold"):
        if key in clean and clean[key] is not None and clean[key] < 0:
            raise InventoryError(f"'{key}' cannot be negative.")
    if clean.get("expiry_date") and not _parse_date(clean["expiry_date"]):
        raise InventoryError("expiry_date must be in YYYY-MM-DD format.")
    if clean.get("cas_number") and not _CAS.match(clean["cas_number"]):
        raise InventoryError("cas_number must look like 64-17-5.")
    return clean


def list_reagents(conn: sqlite3.Connection, *, search: str | None = None, status: str | None = None,
                  location: str | None = None, category: str | None = None, expiring_within: int | None = None,
                  sort: str = "name", order: str = "asc", team_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reagents WHERE 1=1"
    params: list[Any] = []
    if team_id is not None:
        sql += " AND team_id = ?"
        params.append(team_id)
    if search:
        sql += (" AND (name LIKE ? OR cas_number LIKE ? OR catalog_number LIKE ? OR lot_number LIKE ? OR supplier LIKE ?"
                " OR category LIKE ?)")
        params += [f"%{search}%"] * 6
    if location:
        sql += " AND location LIKE ?"
        params.append(f"%{location}%")
    if category:
        sql += " AND category = ?"
        params.append(category)
    sort_col = sort if sort in FIELDS or sort == "id" else "name"
    sql += f" ORDER BY {sort_col} {'DESC' if order.lower() == 'desc' else 'ASC'}"
    reagents = [annotate(dict(r)) for r in conn.execute(sql, params)]
    if status:
        reagents = [r for r in reagents if r["status"] == status or (status == "low_stock" and r["low_stock"])]
    if expiring_within is not None:
        reagents = [r for r in reagents if r["days_to_expiry"] is not None and r["days_to_expiry"] <= expiring_within]
    return reagents


def get_reagent(conn: sqlite3.Connection, reagent_id: int, team_id: int | None = None) -> dict[str, Any] | None:
    if team_id is not None:
        row = conn.execute("SELECT * FROM reagents WHERE id = ? AND team_id = ?", (reagent_id, team_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM reagents WHERE id = ?", (reagent_id,)).fetchone()
    return annotate(dict(row)) if row else None


def create_reagent(conn: sqlite3.Connection, payload: dict[str, Any], actor: str | None = None,
                   team_id: int | None = None) -> dict[str, Any]:
    clean = validate_payload(payload)
    if team_id is not None:
        clean["team_id"] = team_id
    cols = ", ".join(clean)
    cur = conn.execute(f"INSERT INTO reagents ({cols}) VALUES ({', '.join('?' * len(clean))})", list(clean.values()))
    audit(conn, "create", "reagent", cur.lastrowid, f"Added reagent '{clean['name']}'", actor)
    conn.commit()
    return get_reagent(conn, cur.lastrowid, team_id=team_id)


def update_reagent(conn: sqlite3.Connection, reagent_id: int, payload: dict[str, Any],
                   actor: str | None = None, team_id: int | None = None) -> dict[str, Any] | None:
    clean = validate_payload(payload, partial=True)
    if not get_reagent(conn, reagent_id, team_id=team_id):
        return None
    if clean:
        sets = ", ".join(f"{k} = ?" for k in clean)
        if team_id is not None:
            conn.execute(f"UPDATE reagents SET {sets} WHERE id = ? AND team_id = ?", [*clean.values(), reagent_id, team_id])
        else:
            conn.execute(f"UPDATE reagents SET {sets} WHERE id = ?", [*clean.values(), reagent_id])
        audit(conn, "edit", "reagent", reagent_id, f"Edited {', '.join(clean)} of reagent #{reagent_id}", actor)
        conn.commit()
    return get_reagent(conn, reagent_id, team_id=team_id)


def adjust_stock(conn: sqlite3.Connection, reagent_id: int, delta: float,
                 actor: str | None = None, team_id: int | None = None) -> dict[str, Any] | None:
    """Consume (negative delta) or restock (positive). Stock may never go negative."""
    reagent = get_reagent(conn, reagent_id, team_id=team_id)
    if reagent is None:
        return None
    new_qty = reagent["quantity"] + float(delta)
    if new_qty < 0:
        raise InventoryError(
            f"Cannot consume {abs(delta)} {reagent['unit']}; only {reagent['quantity']} {reagent['unit']} in stock."
        )
    if team_id is not None:
        conn.execute("UPDATE reagents SET quantity = ? WHERE id = ? AND team_id = ?", (round(new_qty, 6), reagent_id, team_id))
    else:
        conn.execute("UPDATE reagents SET quantity = ? WHERE id = ?", (round(new_qty, 6), reagent_id))
    audit(conn, "stock", "reagent", reagent_id,
          f"{reagent['name']}: {'+' if delta > 0 else ''}{delta:g} {reagent['unit']} (now {new_qty:g} {reagent['unit']})", actor)
    conn.commit()
    return get_reagent(conn, reagent_id, team_id=team_id)


def delete_reagent(conn: sqlite3.Connection, reagent_id: int, actor: str | None = None,
                   team_id: int | None = None) -> bool:
    reagent = get_reagent(conn, reagent_id, team_id=team_id)
    if not reagent:
        return False
    if team_id is not None:
        cur = conn.execute("DELETE FROM reagents WHERE id = ? AND team_id = ?", (reagent_id, team_id))
    else:
        cur = conn.execute("DELETE FROM reagents WHERE id = ?", (reagent_id,))
    audit(conn, "delete", "reagent", reagent_id, f"Removed reagent '{reagent['name']}'", actor)
    conn.commit()
    return cur.rowcount > 0


def summary(conn: sqlite3.Connection, team_id: int | None = None) -> dict[str, Any]:
    reagents = list_reagents(conn, team_id=team_id)
    counts: dict[str, int] = {}
    for r in reagents:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "total": len(reagents),
        "by_status": counts,
        "low_stock": [r for r in reagents if r["low_stock"]],
        "expired": [r for r in reagents if r["status"] == "expired"],
        "expiring_soon": [r for r in reagents if r["days_to_expiry"] is not None
                          and 0 <= r["days_to_expiry"] <= EXPIRING_SOON_DAYS],
    }


# --------------------------------------------------------------------------- NL query
_INTENTS = [
    ("expired", re.compile(r"\b(expired|out of date|past (their )?expiry)\b", re.I)),
    ("expiring", re.compile(r"\b(expir\w*|shelf life|use by|going off)\b", re.I)),
    ("low_stock", re.compile(r"\b(low|running out|reorder|re-order|need to order|almost out|out of stock)\b", re.I)),
    ("storage", re.compile(r"\b(stor\w*|keep|kept|temperature|fridge|freezer|-20|-80|4 ?°?c)\b", re.I)),
    ("location", re.compile(r"\b(where|location|located|find|shelf|cabinet)\b", re.I)),
    ("stock", re.compile(r"\b(how much|how many|stock|quantity|amount|left|remaining|have we)\b", re.I)),
]
_GENERIC = set(tokenize(
    "reagent reagents chemical chemicals stock quantity much many left remaining store stored storage where "
    "expire expiry expiring expired low reorder order have we our lab inventory need find located location "
    "kept keep temperature condition conditions shelf life month week days soon"
))


def natural_language_query(conn: sqlite3.Connection, question: str, *, team_id: int | None = None) -> dict[str, Any]:
    """Answer simple inventory questions ("how much ethanol is left?", "what expires soon?")."""
    intent = next((name for name, rx in _INTENTS if rx.search(question)), "stock")
    days = EXPIRING_SOON_DAYS
    m = re.search(r"(\d+)\s*(day|week|month)", question, re.I)
    if m:
        days = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()]
    elif re.search(r"\bthis week\b", question, re.I):
        days = 7
    elif re.search(r"\bthis month\b", question, re.I):
        days = 30

    terms = [t for t in tokenize(question) if t not in _GENERIC and len(t) > 1]
    reagents = list_reagents(conn, team_id=team_id)

    def matches(r: dict[str, Any]) -> bool:
        haystack = set(tokenize(" ".join(str(r.get(k) or "") for k in ("name", "cas_number", "catalog_number", "supplier"))))
        return any(t in haystack for t in terms)

    named = [r for r in reagents if matches(r)] if terms else []

    if intent == "expired":
        results = [r for r in (named or reagents) if r["status"] == "expired"]
        text = f"{len(results)} reagent(s) are past their expiry date." if results else "No expired reagents found."
    elif intent == "expiring" and not named:
        results = [r for r in reagents if r["days_to_expiry"] is not None and 0 <= r["days_to_expiry"] <= days]
        text = (f"{len(results)} reagent(s) expire within {days} days." if results
                else f"No reagents expire within {days} days.")
    elif intent == "low_stock" and not named:
        results = [r for r in reagents if r["low_stock"]]
        text = f"{len(results)} reagent(s) are at or below their reorder threshold." if results else "All reagents are above their reorder thresholds."
    elif named:
        results = named
        lines = []
        for r in named:
            if intent == "storage":
                lines.append(f"{r['name']}: store {r['storage_condition'] or 'condition not recorded'} (location: {r['location'] or 'n/a'}).")
            elif intent == "location":
                lines.append(f"{r['name']} is in {r['location'] or 'an unrecorded location'}.")
            elif intent == "expiring":
                lines.append(f"{r['name']} (lot {r['lot_number'] or 'n/a'}) expires {r['expiry_date'] or 'n/a'}"
                             + (f" ({r['days_to_expiry']} days)." if r["days_to_expiry"] is not None else "."))
            else:
                lines.append(f"{r['name']}: {r['quantity']:g} {r['unit']} in stock"
                             + (" - below reorder threshold." if r["low_stock"] else "."))
        text = " ".join(lines)
    else:
        results = []
        text = ("No matching reagent was found in the inventory. Check the spelling, CAS number or catalog number."
                if terms else "Please name a reagent, or ask about expired, expiring or low-stock items.")
    return {"intent": intent, "answer": text, "results": results, "matched_terms": terms}
