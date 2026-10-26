"""BiliAnnotator — online, multi-page annotation app for discourse-coded comments.

Pages:
  /            标注界面    — context card + 4-dimension form
  /codebook    标注手册    — rendered from codebook/CODEBOOK.md
  /progress    进度/质检   — per-annotator + overall progress, coverage, export

The app reads ONLY ``data/annotation.db`` (small, shippable). Building that DB
from the 1.6GB corpus is done once by ``scripts/materialize_units.py``.

Academic guarantees:
  * WAL mode + busy_timeout for safe concurrent annotators;
  * every annotation stores ``schema_version`` and optional ``duration_ms``;
  * provenance (sampling seed, inclusion probabilities) is exposed via
    ``/api/meta`` and per-unit fields, so downstream analysis can reweight;
  * ``/api/export`` emits analysis-ready CSV (incl. inclusion_prob);
  * ``scripts/compute_iaa.py`` computes inter-annotator agreement.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from pathlib import Path

import markdown
from flask import Flask, Response, abort, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "annotation.db"
CODEBOOK_PATH = ROOT / "codebook" / "CODEBOOK.md"

PAYLOAD_SCHEMA = "1.0"
DIMS = ("D1", "D2", "D3", "D4")

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=30000")
    conn.execute("pragma foreign_keys=on")
    return conn


def row_to_unit(r: sqlite3.Row) -> dict:
    d = dict(r)
    for json_key, out_key, default in (
        ("path_json", "path", []),
        ("core_json", "core", []),
        ("video_tags_json", "tags", []),
        ("target_meta_json", "target_meta", {}),
        ("stratum_key", "stratum", None),
    ):
        raw = d.get(json_key)
        if raw in (None, ""):
            d[out_key] = default
        else:
            try:
                d[out_key] = json.loads(raw)
            except (ValueError, TypeError):
                d[out_key] = default
    return d


def _read_meta(conn: sqlite3.Connection) -> dict:
    try:
        return {r["key"]: r["value"] for r in conn.execute("select key,value from meta")}
    except sqlite3.OperationalError:
        return {}


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return render_template("annotate.html")


@app.get("/codebook")
def codebook():
    if CODEBOOK_PATH.exists():
        html = markdown.markdown(
            CODEBOOK_PATH.read_text(encoding="utf-8"),
            extensions=["tables", "fenced_code", "toc", "sane_lists"],
        )
    else:
        html = "<p>没找到 codebook/CODEBOOK.md</p>"
    return render_template("codebook.html", codebook_html=html)


@app.get("/progress")
def progress():
    return render_template("progress.html")


# --------------------------------------------------------------------------- #
# API — meta / navigation
# --------------------------------------------------------------------------- #
@app.get("/api/meta")
def api_meta():
    conn = db()
    meta = _read_meta(conn)
    total = conn.execute("select count(*) from units").fetchone()[0]
    conn.close()
    return jsonify({"meta": meta, "total_units": total, "payload_schema": PAYLOAD_SCHEMA})


@app.get("/api/next")
def api_next():
    annotator = (request.args.get("annotator") or "").strip()
    if not annotator:
        return jsonify({"error": "need annotator"}), 400
    after_id = request.args.get("after_id", type=int)
    conn = db()
    base = ("select u.* from units u where u.id not in "
            "(select unit_id from annotations where annotator=?) ")
    row = None
    if after_id is not None:
        row = conn.execute(base + "and u.id > ? order by u.id asc limit 1",
                           (annotator, after_id)).fetchone()
    if row is None:
        row = conn.execute(base + "order by u.id asc limit 1", (annotator,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"done": True})
    unit = row_to_unit(row)
    unit["existing"] = _existing(conn, unit["id"], annotator)
    unit["position"] = conn.execute(
        "select count(*) from units where id <= ?", (unit["id"],)).fetchone()[0]
    conn.close()
    return jsonify(unit)


@app.get("/api/unit/<int:uid>")
def api_unit(uid):
    annotator = (request.args.get("annotator") or "").strip()
    conn = db()
    r = conn.execute("select * from units where id=?", (uid,)).fetchone()
    if not r:
        conn.close()
        abort(404)
    unit = row_to_unit(r)
    unit["existing"] = _existing(conn, uid, annotator) if annotator else None
    conn.close()
    return jsonify(unit)


def _existing(conn, uid, annotator):
    if not annotator:
        return None
    ann = conn.execute(
        "select payload_json from annotations where unit_id=? and annotator=?",
        (uid, annotator)).fetchone()
    return json.loads(ann["payload_json"]) if ann else None


@app.get("/api/units")
def api_units():
    """Lightweight list for the progress page (id, bvid, preview, status)."""
    annotator = (request.args.get("annotator") or "").strip()
    conn = db()
    done = set()
    if annotator:
        done = {r[0] for r in conn.execute(
            "select unit_id from annotations where annotator=?", (annotator,))}
    rows = conn.execute(
        "select id,bvid,target_user,target_text from units order by id").fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "bvid": r["bvid"],
            "preview": (r["target_text"] or "")[:60],
            "user": r["target_user"] or "",
            "status": "done" if r["id"] in done else "todo",
        })
    return jsonify(out)


# --------------------------------------------------------------------------- #
# API — writes
# --------------------------------------------------------------------------- #
def _validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    for dim in DIMS:
        val = payload.get(dim)
        if val is not None and not isinstance(val, dict):
            raise ValueError(f"{dim} must be an object")
    # primary must be among labels when both provided
    for dim in ("D1", "D2"):
        v = payload.get(dim) or {}
        labels, primary = v.get("labels") or [], v.get("primary")
        if primary and primary not in labels:
            raise ValueError(f"{dim}.primary {primary!r} not in labels {labels}")


@app.post("/api/annotate")
def api_annotate():
    data = request.get_json(force=True, silent=True) or {}
    unit_id = data.get("unit_id")
    annotator = (data.get("annotator") or "").strip()
    payload = data.get("payload")
    if not unit_id or not annotator or payload is None:
        return jsonify({"error": "unit_id/annotator/payload required"}), 400
    try:
        _validate_payload(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

    now = int(time.time())
    duration = data.get("duration_ms")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    pj = json.dumps(payload, ensure_ascii=False)
    conn = db()
    # snapshot the target identity so a later re-sample can never silently
    # reattach this annotation to a different comment.
    trow = conn.execute("select target_rpid from units where id=?", (unit_id,)).fetchone()
    if trow is None:
        conn.close()
        return jsonify({"error": f"unit {unit_id} not found"}), 404
    target_rpid = trow["target_rpid"]
    exists = conn.execute(
        "select id from annotations where unit_id=? and annotator=?",
        (unit_id, annotator)).fetchone()
    if exists:
        conn.execute(
            "update annotations set payload_json=?, schema_version=?, duration_ms=?, "
            "target_rpid=?, updated_at=? where id=?",
            (pj, PAYLOAD_SCHEMA, duration, target_rpid, now, exists["id"]))
    else:
        conn.execute(
            "insert into annotations(unit_id,annotator,payload_json,schema_version,"
            "duration_ms,target_rpid,created_at,updated_at) values(?,?,?,?,?,?,?,?)",
            (unit_id, annotator, pj, PAYLOAD_SCHEMA, duration, target_rpid, now, now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.post("/api/unit/<int:uid>/card")
def api_card(uid):
    """Save human-editable video summary and anchor claim (shared across annotators)."""
    data = request.get_json(force=True, silent=True) or {}
    sets, params = [], []
    for col in ("video_summary_final", "anchor_claim"):
        if col in data:
            sets.append(f"{col}=?")
            params.append((data[col] or "")[:2000])
    if not sets:
        return jsonify({"error": "nothing to update"}), 400
    params.append(uid)
    conn = db()
    conn.execute(f"update units set {', '.join(sets)} where id=?", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API — stats / export
# --------------------------------------------------------------------------- #
@app.get("/api/stats")
def api_stats():
    conn = db()
    total = conn.execute("select count(*) from units").fetchone()[0]
    anns = conn.execute("select unit_id,annotator,payload_json from annotations").fetchall()
    covered = len({a["unit_id"] for a in anns})

    by_ann: dict[str, int] = {}
    dim_filled = {d: 0 for d in DIMS}
    ambiguous = 0
    durations = []
    for a in anns:
        by_ann[a["annotator"]] = by_ann.get(a["annotator"], 0) + 1
        try:
            p = json.loads(a["payload_json"])
        except (ValueError, TypeError):
            continue
        for d in DIMS:
            v = p.get(d) or {}
            filled = bool(v.get("primary") or v.get("labels") or v.get("type")
                          or (d == "D4" and v.get("polarity") not in (None, "")))
            if filled:
                dim_filled[d] += 1
        if p.get("ambiguous"):
            ambiguous += 1
    conn.close()
    annotators = [{"annotator": k, "count": v}
                  for k, v in sorted(by_ann.items(), key=lambda kv: -kv[1])]
    return jsonify({
        "total_units": total,
        "units_covered": covered,
        "coverage_pct": round(covered / total * 100, 1) if total else 0.0,
        "annotations_total": len(anns),
        "dimension_filled": dim_filled,
        "ambiguous": ambiguous,
        "annotators": annotators,
    })


@app.get("/api/export")
def api_export():
    """Analysis-ready CSV: one row per (unit, annotator) with inclusion_prob."""
    annotator = (request.args.get("annotator") or "").strip()
    conn = db()
    q = ("select u.id unit_id, u.bvid, u.target_rpid, u.target_user, u.target_text, "
         "u.stratum_key, u.stratum_n, u.inclusion_prob, u.video_title, "
         "a.annotator, a.payload_json, a.created_at, a.updated_at, a.duration_ms, "
         "a.target_rpid as ann_target_rpid "
         "from units u left join annotations a on a.unit_id = u.id ")
    params = ()
    if annotator:
        q += "where a.annotator = ? "
        params = (annotator,)
    q += "order by u.id, a.annotator"
    rows = conn.execute(q, params).fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "unit_id", "bvid", "target_rpid", "target_user", "target_text",
        "stratum_key", "stratum_n", "inclusion_prob", "video_title",
        "annotator", "d1_labels", "d1_primary", "d1_mixed",
        "d2_labels", "d2_primary", "d2_mixed",
        "d3_type", "d3_extent", "d3_evidence",
        "d4_polarity", "d4_arousal", "d4_toxicity", "d4_evidence",
        "ambiguous", "confidence", "notes", "created_at", "duration_ms",
        "integrity_ok",
    ])
    for r in rows:
        p = {}
        if r["payload_json"]:
            try:
                p = json.loads(r["payload_json"])
            except (ValueError, TypeError):
                p = {}
        d1, d2, d3, d4 = p.get("D1", {}), p.get("D2", {}), p.get("D3", {}), p.get("D4", {})
        writer.writerow([
            r["unit_id"], r["bvid"], r["target_rpid"], r["target_user"],
            r["target_text"], r["stratum_key"], r["stratum_n"], r["inclusion_prob"],
            r["video_title"], r["annotator"],
            "|".join(d1.get("labels") or []), d1.get("primary"), d1.get("is_mixed"),
            "|".join(d2.get("labels") or []), d2.get("primary"), d2.get("is_mixed"),
            d3.get("type"), d3.get("extent"), d3.get("evidence"),
            d4.get("polarity"), d4.get("arousal"), d4.get("toxicity"), d4.get("evidence"),
            p.get("ambiguous"), p.get("confidence"), p.get("notes"),
            r["created_at"], r["duration_ms"],
            # integrity: annotation must still point at the unit it was made for
            ("" if r["ann_target_rpid"] is None
             else "1" if r["ann_target_rpid"] == r["target_rpid"] else "MISMATCH"),
        ])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bili_annotations.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=True)
