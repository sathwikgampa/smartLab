"""Deterministic read-only tools of the lab co-pilot (app/agents/tools.py)."""
from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta

import pytest

from app.agents import tools as T
from app.agents.tools import ToolEnv, run_tool
from app.agents.units import convert, fmt_number, normalise_unit


@pytest.fixture()
def env(app):
    return ToolEnv.from_app(app)


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def add_lot(conn, name, qty, unit, expiry_days=365, key=None, org=1, location="Shelf A", threshold=0):
    conn.execute("INSERT INTO reagents (name, quantity, unit, reorder_threshold, location, expiry_date, compound_key, "
                 "organization_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (name, qty, unit, threshold, location, _d(expiry_days), key, org))
    conn.commit()


def ok(name, args, env):
    result, ms = run_tool(name, args, env)
    assert isinstance(ms, int) and ms >= 0
    return result


# ------------------------------------------------------------------ units
def test_unit_normalisation_and_conversion():
    assert normalise_unit("mL") == normalise_unit("ml") == "ml"
    assert normalise_unit("µL") == normalise_unit("uL") == "ul"
    assert normalise_unit(" M ") == "m"
    assert convert(500, "mL", "L") == pytest.approx(0.5)
    assert convert(200, "mM", "M") == pytest.approx(0.2)
    assert fmt_number(12.114) == "12.11" and fmt_number(500.0) == "500"


# ------------------------------------------------------------------ SOP retrieval
def test_query_sop_returns_chunks_with_citation_fields(env):
    r = ok("query_sop_knowledge", {"query": "HPLC error E-104 high back-pressure"}, env)
    assert r.ok and r.data["relevant_count"] >= 1
    chunk = r.data["chunks"][0]
    assert chunk["chunk_id"].startswith("chunk-")
    assert {"doc_name", "page_number", "text", "similarity"} <= set(chunk)
    assert "HPLC" in chunk["doc_name"]


def test_query_sop_is_scoped_to_organisation(app, conn):
    conn.execute("UPDATE documents SET organization_id = 2")
    conn.commit()
    r = ok("query_sop_knowledge", {"query": "HPLC error E-104"}, ToolEnv.from_app(app, organization_id=1))
    assert r.ok and r.data["chunks"] == []
    r2 = ok("query_sop_knowledge", {"query": "HPLC error E-104"}, ToolEnv.from_app(app, organization_id=2))
    assert r2.data["relevant_count"] >= 1


# ------------------------------------------------------------------ calculator
def test_calculate_molar_preparation(env):
    r = ok("calculate_preparation", {"compound": "Tris base", "target_concentration": 200, "concentration_unit": "mM",
                                     "volume": 500, "volume_unit": "mL"}, env)
    assert r.ok
    assert r.data["mass_g"] == pytest.approx(0.2 * 0.5 * 121.14)
    assert r.data["mass"] == "12.11 g"
    assert "12.11 g" in r.data["numbers"]


def test_calculate_mass_concentration_needs_no_mw(env):
    r = ok("calculate_preparation", {"compound": "NaCl", "target_concentration": 5, "concentration_unit": "mg/mL",
                                     "volume": 100, "volume_unit": "mL"}, env)
    assert r.ok and r.data["mass_g"] == pytest.approx(0.5) and r.data["mass"] == "500 mg"


def test_calculate_refuses_without_molecular_weight(env):
    r = ok("calculate_preparation", {"compound": "PBS", "target_concentration": 1, "concentration_unit": "M",
                                     "volume": 1, "volume_unit": "L"}, env)
    assert not r.ok and "Molecular weight" in r.error and "mass_g" not in r.data


def test_calculate_ambiguous_tris_asks_for_clarification(env):
    r = ok("calculate_preparation", {"compound": "Tris", "target_concentration": 1, "concentration_unit": "M",
                                     "volume": 1, "volume_unit": "L"}, env)
    assert not r.ok and r.data["needs_clarification"]
    assert set(r.data["candidates"]) >= {"Tris base", "Tris-HCl"}


def test_tris_base_and_tris_hcl_are_different(env):
    a = ok("calculate_preparation", {"compound": "Tris-HCl", "target_concentration": 1, "concentration_unit": "M",
                                     "volume": 1, "volume_unit": "L"}, env)
    b = ok("calculate_preparation", {"compound": "trizma base", "target_concentration": 1, "concentration_unit": "M",
                                     "volume": 1, "volume_unit": "L"}, env)
    assert a.data["compound_key"] == "tris_hcl" and b.data["compound_key"] == "tris_base"
    assert a.data["mass_g"] != b.data["mass_g"]


def test_calculate_rejects_bad_units(env):
    r = ok("calculate_preparation", {"compound": "NaCl", "target_concentration": 1, "concentration_unit": "bottle",
                                     "volume": 1, "volume_unit": "L"}, env)
    assert not r.ok and "Invalid arguments" in r.error


# ------------------------------------------------------------------ inventory
def test_inventory_sufficient_with_unit_conversion(env, conn):
    add_lot(conn, "Tris base", 0.5, "kg", key="tris_base", location="Shelf B2")
    r = ok("check_reagent_inventory", {"reagent_name": "Tris base", "required_amount": 12.11, "unit": "g"}, env)
    assert r.ok and r.data["available"] and r.data["sufficient"] is True
    assert r.data["location"] == "Shelf B2"


def test_inventory_insufficient_stock(env, conn):
    add_lot(conn, "Sodium chloride", 5, "g", key="sodium_chloride")
    r = ok("check_reagent_inventory", {"reagent_name": "NaCl", "required_amount": 29.22, "unit": "g"}, env)
    assert r.ok and r.data["available"] and r.data["sufficient"] is False


def test_inventory_expired_lot(env):
    r = ok("check_reagent_inventory", {"reagent_name": "Fetal bovine serum"}, env)  # demo lot expired 12 days ago
    assert r.ok and r.data["found"] and r.data["expired"] and not r.data["available"]


def test_inventory_expiring_soon(env):
    r = ok("check_reagent_inventory", {"reagent_name": "Trypsin-EDTA"}, env)  # demo lot expires in 18 days
    assert r.data["available"] and r.data["expiring_soon"]


def test_inventory_missing_reagent(env):
    r = ok("check_reagent_inventory", {"reagent_name": "Glycine"}, env)  # known compound, no stock
    assert r.ok and not r.data["found"] and not r.data["available"]
    r2 = ok("check_reagent_inventory", {"reagent_name": "unobtainium"}, env)
    assert r2.data["match"] == "not_found"


def test_inventory_incomparable_units(env):
    r = ok("check_reagent_inventory", {"reagent_name": "DMEM", "required_amount": 100, "unit": "mL"}, env)
    assert r.data["sufficient"] is None and "manually" in r.data["sufficiency_note"]


def test_inventory_is_scoped_to_organisation(env, conn):
    add_lot(conn, "Glycine", 1, "kg", key="glycine", org=2)
    r = ok("check_reagent_inventory", {"reagent_name": "Glycine"}, env)
    assert not r.data["found"]


def test_inventory_name_is_parameterised(env):
    r = ok("check_reagent_inventory", {"reagent_name": "x'; DROP TABLE reagents; --"}, env)
    assert r.ok and r.data["match"] == "not_found"


# ------------------------------------------------------------------ compatibility
def test_compatibility_incompatible_pair(env):
    r = ok("verify_chemical_compatibility", {"reagents": ["bleach", "HCl"]}, env)
    assert r.data["any_incompatible"] and r.data["pairs"][0]["result"] == "incompatible"
    assert r.data["matrix_label"] == "Limited demo matrix, not expert-reviewed."
    assert r.data["expert_reviewed"] is False


def test_compatibility_unknown_is_not_safe(env):
    r = ok("verify_chemical_compatibility", {"reagents": ["DMSO", "Glycine", "made-up reagent"]}, env)
    results = {p["result"] for p in r.data["pairs"]}
    assert results == {"unknown"} and r.data["any_unknown"]
    assert "made-up reagent" in r.data["unresolved"]
    assert "safe" not in r.data["pairs"][0]["result"]


def test_compatibility_no_known_issue(env):
    r = ok("verify_chemical_compatibility", {"reagents": ["Tris base", "NaCl", "disodium EDTA"]}, env)
    assert {p["result"] for p in r.data["pairs"]} == {"no_known_issue"}
    assert "not expert-reviewed" in r.summary


# ------------------------------------------------------------------ hardware diagnosis
def test_diagnose_by_error_code(env):
    r = ok("diagnose_hardware_error", {"error_code": "e104"}, env)
    assert r.ok and r.data["error_code"] == "E-104" and not r.data["needs_confirmation"]
    assert r.data["resolution_steps"] and r.data["ppe_warnings"]


def test_diagnose_upload_needs_confirmation(env, conn):
    conn.execute("INSERT INTO agent_uploads (upload_id, stored_name, mime_type, size_bytes, extracted_codes_json) "
                 "VALUES (?, ?, ?, ?, ?)", ("a" * 32, "x.png", "image/png", 10, '["IMB-01"]'))
    conn.commit()
    r = ok("diagnose_hardware_error", {"upload_id": "a" * 32}, env)
    assert r.ok and r.data["needs_confirmation"] and r.data["extracted_code"] == "IMB-01"
    assert r.data["resolution_steps"] == []


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/passwd", "a" * 31, "A" * 32])
def test_diagnose_rejects_path_like_upload_ids(env, bad):
    r = ok("diagnose_hardware_error", {"upload_id": bad}, env)
    assert not r.ok and "Invalid arguments" in r.error


def test_diagnose_upload_from_other_org_is_not_found(env, conn):
    conn.execute("INSERT INTO agent_uploads (upload_id, organization_id, stored_name, mime_type, size_bytes) "
                 "VALUES (?, 2, 'x.png', 'image/png', 1)", ("b" * 32,))
    conn.commit()
    r = ok("diagnose_hardware_error", {"upload_id": "b" * 32}, env)
    assert not r.ok and r.error == "Upload not found."


def test_diagnose_hardware_error_from_other_org_is_not_found(env, conn):
    conn.execute("INSERT INTO troubleshooting (organization_id, team_id, equipment, symptom, keywords, probable_cause, fix, error_code, verified) "
                 "VALUES (2, 1, 'HPLC', 'Error E-999', 'hplc, filter', 'Filter leak', 'Replace filter.', 'E-999', 1)")
    conn.commit()
    r = ok("diagnose_hardware_error", {"error_code": "E-999"}, env)
    assert r.ok and r.data["matches"] == []


# ------------------------------------------------------------------ runner guarantees
def test_unknown_tool_and_extra_args_rejected(env):
    assert not ok("delete_everything", {}, env).ok
    r = ok("query_sop_knowledge", {"query": "hplc", "sql": "DROP TABLE chunks"}, env)
    assert not r.ok and "Invalid arguments" in r.error


def test_tools_run_on_read_only_connection(env):
    conn = env.open_conn()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM reagents")
    finally:
        conn.close()


def test_tool_timeout_fails_safe(env, monkeypatch):
    def slow(ctx, args):
        time.sleep(0.5)
        return T.ToolResult(True, {}, None, "late")

    monkeypatch.setitem(T.TOOLS, "query_sop_knowledge",
                        T.ToolSpec("query_sop_knowledge", slow, T.QuerySopArgs, "slow"))
    env.timeout_s = 0.05
    r = ok("query_sop_knowledge", {"query": "anything"}, env)
    assert not r.ok and "timed out" in r.error


def test_tool_exception_fails_safe(env, monkeypatch):
    def boom(ctx, args):
        raise RuntimeError("secret internal detail")

    monkeypatch.setitem(T.TOOLS, "check_reagent_inventory",
                        T.ToolSpec("check_reagent_inventory", boom, T.InventoryArgs, "boom"))
    r = ok("check_reagent_inventory", {"reagent_name": "x"}, env)
    assert not r.ok and "secret" not in r.error
