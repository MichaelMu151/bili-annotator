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

# Canonical label sets — the single source of truth, mirroring CODEBOOK.md.
D1_LABELS = {"1.1", "1.2", "1.3", "1.4", "1.5"}
D2_LABELS = {"2.1", "2.2", "2.3", "2.4", "2.5"}
D3_TYPES = {"3.1", "3.2", "3.3", "3.4"}
D3_EXTENTS = {"1", "2", "3", "4"}
D4_POLARITY = {"-1", "0", "1"}
D4_AROUSAL = {"low", "high"}
D4_TOXICITY = {"0", "1", "2", "3"}
CONFIDENCE = {"high", "medium", "low"}

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
    _ensure_app_tables(conn)
    return conn


def _ensure_app_tables(conn: sqlite3.Connection) -> None:
    """Create app-owned tables that are not produced by materialize_units.py.

    ``adjudications`` stores the adjudicator's resolved value per (unit,
    dimension) once a disagreement has been reviewed — the raw per-annotator
    judgments stay untouched in ``annotations`` for full provenance.
    """
    conn.execute(
        "create table if not exists adjudications("
        "  id integer primary key autoincrement,"
        "  unit_id integer not null,"
        "  dimension text not null,"
        "  adjudicator text not null,"
        "  decision_json text,"
        "  note text,"
        "  created_at integer, updated_at integer,"
        "  unique(unit_id, dimension))")
    conn.execute("create index if not exists idx_adj_unit on adjudications(unit_id)")


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


@app.get("/disagree")
def disagree():
    return render_template("disagree.html")


# PWA at root scope (service worker must be served from / to control the site)
@app.get("/sw.js")
def service_worker():
    return app.send_static_file("sw.js")


@app.get("/manifest.webmanifest")
def pwa_manifest():
    resp = app.send_static_file("manifest.webmanifest")
    resp.mimetype = "application/manifest+json"
    return resp


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


@app.get("/api/offline_bundle")
def api_offline_bundle():
    """Full set of units for offline annotation (cached client-side in IndexedDB)."""
    conn = db()
    rows = conn.execute("select * from units order by id").fetchall()
    units = [row_to_unit(r) for r in rows]
    conn.close()
    return jsonify({"version": PAYLOAD_SCHEMA, "generated_at": int(time.time()),
                    "total": len(units), "units": units})


@app.post("/api/sync")
def api_sync():
    """Batch upload of annotations made offline. Idempotent per (unit, annotator)."""
    data = request.get_json(force=True, silent=True) or {}
    annotator = (data.get("annotator") or "").strip()
    items = data.get("items") or []
    if not annotator or not isinstance(items, list):
        return jsonify({"error": "annotator and items[] required"}), 400
    if len(items) > 5000:
        return jsonify({"error": "too many items in one sync (max 5000)"}), 413
    now = int(time.time())
    conn = db()
    results = []
    synced = 0
    for it in items:
        uid = it.get("unit_id") if isinstance(it, dict) else None
        payload = it.get("payload") if isinstance(it, dict) else None
        try:
            if not isinstance(uid, int):
                raise ValueError("bad unit_id")
            _validate_payload(payload)
            trow = conn.execute("select target_rpid from units where id=?", (uid,)).fetchone()
            if trow is None:
                raise ValueError("unit not found")
            try:
                duration = int(it.get("duration_ms")) if it.get("duration_ms") is not None else None
            except (TypeError, ValueError):
                duration = None
            conn.execute(
                "insert into annotations(unit_id,annotator,payload_json,schema_version,"
                "duration_ms,target_rpid,created_at,updated_at) values(?,?,?,?,?,?,?,?) "
                "on conflict(unit_id, annotator) do update set "
                "payload_json=excluded.payload_json, schema_version=excluded.schema_version, "
                "duration_ms=excluded.duration_ms, target_rpid=excluded.target_rpid, "
                "updated_at=excluded.updated_at",
                (uid, annotator, json.dumps(payload, ensure_ascii=False),
                 PAYLOAD_SCHEMA, duration, trow["target_rpid"], now, now))
            results.append({"unit_id": uid, "ok": True})
            synced += 1
        except Exception as e:  # noqa: BLE001
            results.append({"unit_id": uid, "ok": False, "error": str(e)})
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "synced": synced, "results": results})


# --------------------------------------------------------------------------- #
# API — writes
# --------------------------------------------------------------------------- #
def _validate_payload(payload: dict) -> None:
    """Server-side validation of the full payload against the codebook domains.

    Mirrors the client checks but is authoritative: a tampered/aborted request
    can never write an out-of-domain value into the gold set.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    for dim in DIMS:
        val = payload.get(dim)
        if val is not None and not isinstance(val, dict):
            raise ValueError(f"{dim} must be an object")

    # D1 / D2: multi-label subset of canonical codes + primary must be in labels
    for dim, domain in (("D1", D1_LABELS), ("D2", D2_LABELS)):
        v = payload.get(dim) or {}
        labels = v.get("labels") or []
        if not isinstance(labels, list):
            raise ValueError(f"{dim}.labels must be a list")
        bad = set(labels) - domain
        if bad:
            raise ValueError(f"{dim}.labels contains invalid codes {sorted(bad)}")
        primary = v.get("primary")
        if primary and primary not in labels:
            raise ValueError(f"{dim}.primary {primary!r} not in labels {labels}")
        if primary and primary not in domain:
            raise ValueError(f"{dim}.primary {primary!r} not a valid code")

    # D3: single type from domain, extent ordinal 1-4.
    # When the immediate parent is missing (parent_gap unit) the annotator may
    # mark D3 as unassessable instead of guessing topic drift.
    d3 = payload.get("D3") or {}
    d3_unassessable = bool(d3.get("unassessable"))
    if not d3_unassessable:
        if d3.get("type") and d3["type"] not in D3_TYPES:
            raise ValueError(f"D3.type {d3['type']!r} invalid")
        if d3.get("extent") not in (None, "") and str(d3["extent"]) not in D3_EXTENTS:
            raise ValueError(f"D3.extent {d3['extent']!r} invalid (want 1-4)")

    # D4: polarity/arousal/toxicity enums
    d4 = payload.get("D4") or {}
    if d4.get("polarity") not in (None, "") and str(d4["polarity"]) not in D4_POLARITY:
        raise ValueError(f"D4.polarity {d4['polarity']!r} invalid")
    if d4.get("arousal") not in (None, "") and d4["arousal"] not in D4_AROUSAL:
        raise ValueError(f"D4.arousal {d4['arousal']!r} invalid")
    if d4.get("toxicity") not in (None, "") and str(d4["toxicity"]) not in D4_TOXICITY:
        raise ValueError(f"D4.toxicity {d4['toxicity']!r} invalid")

    if payload.get("confidence") not in (None, "") and \
            payload["confidence"] not in CONFIDENCE:
        raise ValueError(f"confidence {payload['confidence']!r} invalid")

    # stance_recoverable: only meaningful for parent_gap units; enum when present.
    sr = payload.get("stance_recoverable")
    if sr not in (None, "") and str(sr) not in ("yes", "no", "uncertain", "true", "false"):
        raise ValueError(f"stance_recoverable {sr!r} invalid")

    # Reject a completely empty annotation: an item must carry at least one
    # substantive judgement or be explicitly flagged ambiguous.
    d1h = bool((payload.get("D1") or {}).get("labels") or (payload.get("D1") or {}).get("primary"))
    d2h = bool((payload.get("D2") or {}).get("labels") or (payload.get("D2") or {}).get("primary"))
    d3h = bool((payload.get("D3") or {}).get("type") or (payload.get("D3") or {}).get("unassessable"))
    d4h = (payload.get("D4") or {}).get("polarity") not in (None, "") or \
          (payload.get("D4") or {}).get("toxicity") not in (None, "") or \
          (payload.get("D4") or {}).get("arousal") not in (None, "")
    if not (d1h or d2h or d3h or d4h or payload.get("ambiguous")):
        raise ValueError("empty annotation: fill at least one dimension or mark ambiguous")


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
    # Atomic upsert on the UNIQUE(unit_id, annotator) key — no read-then-write
    # race under concurrent saves from the same annotator (e.g. double submit).
    conn.execute(
        "insert into annotations(unit_id,annotator,payload_json,schema_version,"
        "duration_ms,target_rpid,created_at,updated_at) values(?,?,?,?,?,?,?,?) "
        "on conflict(unit_id,annotator) do update set "
        "payload_json=excluded.payload_json, schema_version=excluded.schema_version, "
        "duration_ms=excluded.duration_ms, target_rpid=excluded.target_rpid, "
        "updated_at=excluded.updated_at",
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
# API — disagreement detection & adjudication
# --------------------------------------------------------------------------- #
def _dim_signature(payload: dict, dim: str) -> object:
    """Normalise one dimension into a comparable signature (None = not rated).

    D1/D2 compare the *label set* (order-independent); D3 compares type and,
    separately, extent; D4 splits into polarity/arousal/toxicity. Returns a
    hashable signature so agreement is `len(set(sigs)) == 1`.
    """
    v = (payload or {}).get(dim)
    if not isinstance(v, dict):
        return None
    if dim in ("D1", "D2"):
        labels = v.get("labels") or []
        if not labels and not v.get("primary"):
            return None
        return frozenset(labels) if labels else frozenset([v["primary"]])
    if dim == "D3":
        t = v.get("type")
        if not t:
            return None
        return (t, str(v.get("extent")) if v.get("extent") not in (None, "") else None)
    if dim == "D4":
        pol = v.get("polarity")
        if pol in (None, "") and v.get("toxicity") in (None, "") and v.get("arousal") in (None, ""):
            return None
        return (str(pol) if pol not in (None, "") else None,
                v.get("arousal") or None,
                str(v.get("toxicity")) if v.get("toxicity") not in (None, "") else None)
    return None


def _sig_to_json(sig):
    """Convert a signature (may contain frozenset) into JSON-serialisable form."""
    if isinstance(sig, frozenset):
        return sorted(sig)
    if isinstance(sig, tuple):
        return list(sig)
    return sig


@app.get("/api/disagreements")
def api_disagreements():
    """List units where ≥2 annotators disagree on one or more dimensions.

    A disagreement is any dimension whose signature differs across the
    annotators who rated it (≥2 ratings present, ≥2 distinct signatures).
    Already-adjudicated dimensions are flagged so the UI can collapse them.
    """
    only_open = request.args.get("open") == "1"
    conn = db()
    units = {r["id"]: dict(r) for r in conn.execute("select * from units")}
    anns_by_unit: dict[int, list] = {}
    for a in conn.execute("select unit_id,annotator,payload_json from annotations order by unit_id"):
        try:
            payload = json.loads(a["payload_json"])
        except (ValueError, TypeError):
            continue
        anns_by_unit.setdefault(a["unit_id"], []).append(
            {"annotator": a["annotator"], "payload": payload})
    adj = {}
    try:
        for r in conn.execute("select unit_id,dimension,decision_json,adjudicator,note from adjudications"):
            adj[(r["unit_id"], r["dimension"])] = {
                "decision": json.loads(r["decision_json"]) if r["decision_json"] else None,
                "adjudicator": r["adjudicator"], "note": r["note"]}
    except sqlite3.OperationalError:
        pass
    conn.close()

    out = []
    for uid, annlist in anns_by_unit.items():
        if len(annlist) < 2:
            continue
        dim_reports = {}
        for dim in DIMS:
            rated = [(a["annotator"], _dim_signature(a["payload"], dim)) for a in annlist]
            rated = [(n, s) for n, s in rated if s is not None]
            if len(rated) < 2:
                continue
            sigs = {s for _, s in rated}
            if len(sigs) > 1:
                is_adj = (uid, dim) in adj
                dim_reports[dim] = {
                    "ratings": [{"annotator": n, "value": _sig_to_json(s)} for n, s in rated],
                    "adjudicated": is_adj,
                    "decision": adj.get((uid, dim), {}).get("decision"),
                    "adjudicator": adj.get((uid, dim), {}).get("adjudicator"),
                }
        if not dim_reports:
            continue
        if only_open and all(d["adjudicated"] for d in dim_reports.values()):
            continue
        u = units.get(uid, {})
        out.append({
            "unit_id": uid,
            "target_text": u.get("target_text", ""),
            "target_user": u.get("target_user", ""),
            "video_title": u.get("video_title", ""),
            "anchor_text": u.get("anchor_text", ""),
            "n_annotators": len(annlist),
            "dims": dim_reports,
            "open_dims": [d for d, r in dim_reports.items() if not r["adjudicated"]],
        })
    out.sort(key=lambda x: (-len(x["open_dims"]), x["unit_id"]))
    return jsonify({"disagreements": out,
                    "dims_meta": {"D1": sorted(D1_LABELS), "D2": sorted(D2_LABELS),
                                  "D3": sorted(D3_TYPES), "D4": None}})


@app.post("/api/adjudicate")
def api_adjudicate():
    """Persist an adjudicator's resolved decision for one (unit, dimension)."""
    data = request.get_json(force=True, silent=True) or {}
    unit_id = data.get("unit_id")
    dim = data.get("dimension")
    adjudicator = (data.get("adjudicator") or "").strip()
    decision = data.get("decision")
    note = (data.get("note") or "")[:1000]
    if not unit_id or dim not in DIMS or not adjudicator or decision is None:
        return jsonify({"error": "unit_id/dimension/adjudicator/decision required"}), 400
    now = int(time.time())
    conn = db()
    if conn.execute("select 1 from units where id=?", (unit_id,)).fetchone() is None:
        conn.close()
        return jsonify({"error": f"unit {unit_id} not found"}), 404
    conn.execute(
        "insert into adjudications(unit_id,dimension,adjudicator,decision_json,note,"
        "created_at,updated_at) values(?,?,?,?,?,?,?) "
        "on conflict(unit_id,dimension) do update set "
        "adjudicator=excluded.adjudicator, decision_json=excluded.decision_json, "
        "note=excluded.note, updated_at=excluded.updated_at",
        (unit_id, dim, adjudicator, json.dumps(decision, ensure_ascii=False), note, now, now))
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
    total_degraded = conn.execute(
        "select count(*) from units where context_degraded=1").fetchone()[0]
    status_counts = {row[0]: row[1] for row in conn.execute(
        "select context_status, count(*) from units group by context_status")}
    conn.close()
    annotators = [{"annotator": k, "count": v}
                  for k, v in sorted(by_ann.items(), key=lambda kv: -kv[1])]
    return jsonify({
        "total_units": total,
        "total_degraded": total_degraded,
        "context_status": status_counts,
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
         "u.context_degraded, u.degraded_reason, u.context_status, u.d3_assessable, "
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
        "context_degraded", "degraded_reason", "context_status", "d3_assessable",
        "annotator", "d1_labels", "d1_primary", "d1_mixed",
        "d2_labels", "d2_primary", "d2_mixed",
        "d3_type", "d3_extent", "d3_evidence", "d3_unassessable", "stance_recoverable",
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
            r["video_title"], r["context_degraded"], r["degraded_reason"],
            r["context_status"], r["d3_assessable"],
            r["annotator"],
            "|".join(d1.get("labels") or []), d1.get("primary"), d1.get("is_mixed"),
            "|".join(d2.get("labels") or []), d2.get("primary"), d2.get("is_mixed"),
            d3.get("type"), d3.get("extent"), d3.get("evidence"), d3.get("unassessable"),
            p.get("stance_recoverable"),
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


@app.get("/api/export_excluded")
def api_export_excluded():
    """CSV of comments excluded from annotation due to missing context."""
    label = (request.args.get("label") or "").strip()
    conn = db()
    q = ("select bvid, target_rpid, parent_rpid, root_rpid, target_user, "
         "target_text, target_len, exclusion_label, parent_present, "
         "orphan_replies, pub_ts from excluded_comments ")
    params = ()
    if label:
        q += "where exclusion_label = ? "
        params = (label,)
    q += "order by bvid, target_rpid"
    try:
        rows = conn.execute(q, params).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["bvid", "target_rpid", "parent_rpid", "root_rpid", "target_user",
                "target_text", "target_len", "exclusion_label", "parent_present",
                "orphan_replies", "pub_ts"])
    for r in rows:
        w.writerow([r[k] for k in r.keys()])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bili_excluded.csv"})


if __name__ == "__main__":
    import os
    # Bind to localhost by default; opt into LAN sharing explicitly. Never run
    # the Werkzeug debugger on a public interface — it allows code execution.
    host = os.environ.get("ANNOTATOR_HOST", "127.0.0.1")
    port = int(os.environ.get("ANNOTATOR_PORT", "5005"))
    debug = os.environ.get("ANNOTATOR_DEBUG") == "1" and host in ("127.0.0.1", "localhost")
    app.run(host=host, port=port, debug=debug, use_reloader=debug)
