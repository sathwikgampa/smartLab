"""Data management: datasets with server-side sort/filter, statistics and CSV import."""
from __future__ import annotations

import csv
import io
import statistics
from typing import Any

from flask import Blueprint, jsonify, request

from ..db import dumps, execute, loads, query_all, query_one
from ..security import validate_upload
from ..tenancy import team_id
from .common import ApiError, int_arg, json_body, require, upload_limits

bp = Blueprint("data", __name__, url_prefix="/api/datasets")

MAX_ROWS = 50_000


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip()
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return value


def _dataset(ds_id: int) -> dict:
    t_id = team_id()
    ds = query_one("SELECT * FROM datasets WHERE id = ? AND team_id = ?", (ds_id, t_id))
    if ds is None:
        raise ApiError("Dataset not found.", 404)
    ds["columns"] = loads(ds.pop("columns_json"), [])
    ds["rows"] = loads(ds.pop("rows_json"), [])
    return ds


def column_stats(columns: list[str], rows: list[dict]) -> dict[str, dict]:
    stats = {}
    for col in columns:
        values = [r.get(col) for r in rows]
        nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums and len(nums) == len([v for v in values if v not in (None, "")]):
            stats[col] = {"type": "numeric", "count": len(nums), "mean": round(statistics.fmean(nums), 4),
                          "std": round(statistics.stdev(nums), 4) if len(nums) > 1 else 0.0,
                          "min": min(nums), "max": max(nums), "median": statistics.median(nums)}
        else:
            distinct = sorted({str(v) for v in values if v not in (None, "")})
            stats[col] = {"type": "categorical", "count": len(values), "distinct": len(distinct), "values": distinct[:25]}
    return stats


def _validate_rows(columns: Any, rows: Any) -> tuple[list[str], list[dict]]:
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) and c for c in columns):
        raise ApiError("columns must be a non-empty list of names.", 422)
    if len(set(columns)) != len(columns):
        raise ApiError("Column names must be unique.", 422)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ApiError("rows must be a list of objects.", 422)
    if len(rows) > MAX_ROWS:
        raise ApiError(f"Datasets are limited to {MAX_ROWS} rows.", 413)
    return columns, [{c: _coerce(r.get(c)) for c in columns} for r in rows]


@bp.get("")
def list_datasets():
    t_id = team_id()
    rows = query_all("SELECT id, project_id, experiment_id, name, description, columns_json, rows_json, created_at "
                     "FROM datasets WHERE team_id = ? ORDER BY id DESC", (t_id,))
    for r in rows:
        r["columns"] = loads(r.pop("columns_json"), [])
        r["row_count"] = len(loads(r.pop("rows_json"), []))
    return jsonify(rows)


@bp.get("/<int:ds_id>")
def get_dataset(ds_id: int):
    ds = _dataset(ds_id)
    rows = ds["rows"]
    # Filters: ?filter=column:substring (repeatable), ?min=column:value, ?max=column:value
    for spec in request.args.getlist("filter"):
        col, _, needle = spec.partition(":")
        if col not in ds["columns"]:
            raise ApiError(f"Unknown filter column '{col}'.")
        rows = [r for r in rows if needle.lower() in str(r.get(col, "")).lower()]
    for bound, op in (("min", lambda a, b: a >= b), ("max", lambda a, b: a <= b)):
        for spec in request.args.getlist(bound):
            col, _, raw = spec.partition(":")
            try:
                limit = float(raw)
            except ValueError as exc:
                raise ApiError(f"{bound} value must be numeric.") from exc
            rows = [r for r in rows if isinstance(r.get(col), (int, float)) and op(r[col], limit)]
    sort = request.args.get("sort")
    if sort:
        if sort not in ds["columns"]:
            raise ApiError(f"Unknown sort column '{sort}'.")
        rows = sorted(rows, key=lambda r: (r.get(sort) is None, isinstance(r.get(sort), str), r.get(sort)),
                      reverse=request.args.get("order") == "desc")
    total = len(rows)
    offset = int_arg("offset", 0, 0) or 0
    limit = int_arg("limit", 500, 1, 5000)
    ds["rows"] = rows[offset:offset + limit]
    ds["total_rows"] = total
    ds["stats"] = column_stats(ds["columns"], rows)
    return jsonify(ds)


@bp.post("")
def create_dataset():
    t_id = team_id()
    if request.files.get("file"):
        validated = validate_upload(request.files["file"], {"csv"}, **upload_limits())
        reader = csv.DictReader(io.StringIO(validated.data.decode("utf-8-sig")))
        if not reader.fieldnames:
            raise ApiError("CSV file has no header row.", 422)
        columns, rows = _validate_rows([c.strip() for c in reader.fieldnames],
                                       [{k.strip(): v for k, v in r.items() if k} for r in reader])
        name = request.form.get("name") or validated.safe_name.rsplit(".", 1)[0]
        description = request.form.get("description")
        project_id = request.form.get("project_id") or None
        experiment_id = request.form.get("experiment_id") or None
    else:
        data = json_body()
        require(data, "name")
        columns, rows = _validate_rows(data.get("columns"), data.get("rows", []))
        name, description = data["name"], data.get("description")
        project_id, experiment_id = data.get("project_id"), data.get("experiment_id")

    if project_id and not query_one("SELECT id FROM projects WHERE id = ? AND team_id = ?", (project_id, t_id)):
        raise ApiError("Project not found.", 404)
    if experiment_id and not query_one("SELECT id FROM experiments WHERE id = ? AND team_id = ?", (experiment_id, t_id)):
        raise ApiError("Experiment not found.", 404)

    ds_id = execute(
        "INSERT INTO datasets (team_id, project_id, experiment_id, name, description, columns_json, rows_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (t_id, project_id, experiment_id, str(name)[:200], description, dumps(columns), dumps(rows)),
    )
    return jsonify(_dataset(ds_id)), 201


@bp.post("/<int:ds_id>/rows")
def append_rows(ds_id: int):
    ds = _dataset(ds_id)
    data = json_body()
    _, new_rows = _validate_rows(ds["columns"], data.get("rows", []))
    rows = ds["rows"] + new_rows
    if len(rows) > MAX_ROWS:
        raise ApiError(f"Datasets are limited to {MAX_ROWS} rows.", 413)
    execute("UPDATE datasets SET rows_json = ? WHERE id = ? AND team_id = ?", (dumps(rows), ds_id, team_id()))
    return jsonify(_dataset(ds_id))


@bp.delete("/<int:ds_id>")
def delete_dataset(ds_id: int):
    _dataset(ds_id)
    execute("DELETE FROM datasets WHERE id = ? AND team_id = ?", (ds_id, team_id()))
    return "", 204
