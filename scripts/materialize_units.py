"""Materialize annotation units from the 805-video corpus into a small,
shippable ``data/annotation.db``.

This is the ONLY script that touches the big corpus (``analysis_805.db`` /
``corpus.db``). The web app reads only the resulting ``annotation.db``.

Reproducibility contract (academic):
  * deterministic candidate order (``order by rpid``), deterministic floor order
    (``order by pub_ts, rpid``);
  * the random seed is persisted in the ``meta`` table (``sampling.seed``);
  * every unit stores its ``stratum_key``, ``stratum_n`` and
    ``inclusion_prob`` (= quota / stratum size) so the sample can be reweighted
    back to the population;
  * a ``meta`` table records provenance (seed, target, source paths + sizes,
    stratification spec, schema version, timestamps).

Schema (annotation.db):
  meta(key, value)
  units(id, unit_key UNIQUE, bvid, video_title, video_author, video_tags_json,
        video_summary_suggested, video_summary_final, video_url,
        anchor_rpid, anchor_text, anchor_claim,
        path_json, core_json, target_rpid, target_user, target_ts,
        target_text, target_meta_json,
        stratum_key, stratum_n, inclusion_prob, sampled_by, created_at)
  annotations(id, unit_id, annotator, payload_json, schema_version,
              duration_ms, created_at, updated_at, UNIQUE(unit_id, annotator))
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sqlite3
import time
from collections import defaultdict

SCHEMA_VERSION = "1.0.0"
STRATIFICATION_SPEC = (
    "topic(labor/other) x floor_heat(0/1-5/6-20/>20) x depth(0/1/2/>=3) "
    "x polarity(pos/neu/neg) x flagtype(plain/tox/sticker/emoji/checkin) "
    "x length(s<=10/m11-50/l>50); quota ~ sqrt(stratum size)"
)

# ----------------------------- lexicon (seeds) ----------------------------- #
ATTACK = ("傻逼", "煞笔", "脑残", "弱智", "智障", "小丑", "废物", "去死", "你妈",
          "滚", "狗", "喷子", "杠精", "恶心", "贱", "野种", "杂种")
POS = ("支持", "赞", "加油", "厉害", "优秀", "喜欢", "谢谢", "棒", "牛", "感动", "有理", "确实")
NEG = ("垃圾", "差", "烂", "无脑", "呵呵", "阴阳", "讽刺", "嘲讽", "骗", "假",
       "悲哀", "惨", "剥削", "压榨", "无语", "笑死", "离谱", "恶心")
CHECKIN = ("会不会没", "还在", "报个到", "打卡", "守楼", "踢我", "蹲", "视频没",
           "别删", "留名", "看看视频", "会不会删")
LABOR = ("加班", "打工", "牛马", "裁员", "薪资", "工资", "老板", "996", "007",
         "劳动法", "大厂", "失业", "上班", "社畜", "内卷", "躺平", "离职",
         "外包", "派遣", "职场", "绩效", "剥削", "资本家", "劳资", "公积金",
         "流水线", "工厂", "班味", "耗材", "红利", "双休", "调休", "劳动")

STICKER_RE = re.compile(r"\[[^\[\]]{1,30}\]")

# Number of preceding same-floor comments shown as C_core (in addition to the
# floor root), matching the "本层 + 前面 3 个评论" context spec.
CORE_PRIOR_N = 3

SCHEMA = f"""
create table if not exists meta(key text primary key, value text);
create table if not exists units(
  id integer primary key autoincrement,
  unit_key text unique,
  bvid text, video_title text, video_author text, video_tags_json text,
  video_summary_suggested text, video_summary_final text, video_url text,
  anchor_rpid text, anchor_text text, anchor_claim text,
  path_json text, core_json text, target_rpid text, target_user text,
  target_ts integer, target_text text, target_meta_json text,
  context_degraded integer default 0, anchor_degraded integer default 0,
  degraded_reason text, context_status text, d3_assessable integer default 1,
  stratum_key text, stratum_n integer, inclusion_prob real, sampled_by text,
  created_at integer
);
create table if not exists annotations(
  id integer primary key autoincrement,
  unit_id integer, annotator text, payload_json text,
  schema_version text, duration_ms integer, target_rpid text,
  created_at integer, updated_at integer,
  unique(unit_id, annotator)
);
create index if not exists idx_ann_unit on annotations(unit_id);
create index if not exists idx_ann_annotator on annotations(annotator);
"""


# ----------------------------- schema management ----------------------------- #
def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables, rebuilding `units` when its columns drift.

    Why: ``create table if not exists`` silently keeps an OLD schema, which then
    fails at insert time with confusing errors (e.g. "no column named unit_key").
    We compare the expected column set and rebuild `units` on mismatch. The
    `annotations` table is left untouched — but because the rebuild changes unit
    ids, callers must be warned (main() refuses to run with --force absent).
    """
    conn.executescript(SCHEMA)  # no-op for tables that already match
    expected = {
        "id", "unit_key", "bvid", "video_title", "video_author", "video_tags_json",
        "video_summary_suggested", "video_summary_final", "video_url",
        "anchor_rpid", "anchor_text", "anchor_claim", "path_json", "core_json",
        "target_rpid", "target_user", "target_ts", "target_text",
        "target_meta_json", "context_degraded", "anchor_degraded", "degraded_reason",
        "context_status", "d3_assessable",
        "stratum_key", "stratum_n", "inclusion_prob",
        "sampled_by", "created_at",
    }
    actual = {r[1] for r in conn.execute("pragma table_info(units)")}
    if actual != expected:
        print(f"!! units schema drift (missing={sorted(expected - actual)}, "
              f"extra={sorted(actual - expected)}); rebuilding units table.")
        conn.executescript(
            "drop table if exists units; "
            "create table units("
            "  id integer primary key autoincrement,"
            "  unit_key text unique,"
            "  bvid text, video_title text, video_author text, video_tags_json text,"
            "  video_summary_suggested text, video_summary_final text, video_url text,"
            "  anchor_rpid text, anchor_text text, anchor_claim text,"
            "  path_json text, core_json text, target_rpid text, target_user text,"
            "  target_ts integer, target_text text, target_meta_json text,"
            "  context_degraded integer default 0, anchor_degraded integer default 0,"
            "  degraded_reason text, context_status text, d3_assessable integer default 1,"
            "  stratum_key text, stratum_n integer, inclusion_prob real,"
            "  sampled_by text, created_at integer);"
        )
        conn.commit()

    # --- annotations: additive migration (never drop annotator work) ---
    ann_expected = {
        "id", "unit_id", "annotator", "payload_json", "schema_version",
        "duration_ms", "target_rpid", "created_at", "updated_at",
    }
    ann_actual = {r[1] for r in conn.execute("pragma table_info(annotations)")}
    for col, decl in (("schema_version", "text"), ("duration_ms", "integer"),
                      ("target_rpid", "text")):
        if col not in ann_actual:
            print(f"!! annotations missing column {col}; adding.")
            conn.execute(f"alter table annotations add column {col} {decl}")
    if ann_actual - ann_expected:
        print(f"!! annotations has unexpected columns: {sorted(ann_actual - ann_expected)}")
    conn.commit()


# ----------------------------- feature helpers ----------------------------- #
def has_emoji(s: str) -> bool:
    return any(ord(c) >= 0x1F000 or 0x2600 <= ord(c) <= 0x27BF for c in s)


def is_emoji_only(s: str) -> bool:
    saw = False
    for c in s:
        if c.isspace() or c in ("\ufe0f", "\u200d"):
            continue
        if ord(c) >= 0x1F000 or 0x2600 <= ord(c) <= 0x27BF:
            saw = True
            continue
        return False
    return saw


def is_sticker_only(s: str) -> bool:
    return bool(STICKER_RE.search(s)) and STICKER_RE.sub("", s).strip() == ""


def polarity_bucket(text: str) -> str:
    p = sum(text.count(w) for w in POS)
    n = sum(text.count(w) for w in NEG)
    return "pos" if p > n else ("neg" if n > p else "neu")


def length_bucket(n: int) -> str:
    return "s" if n <= 10 else ("m" if n <= 50 else "l")


def heat_bucket(k: int) -> str:
    return "0" if k <= 0 else ("1-5" if k <= 5 else ("6-20" if k <= 20 else ">20"))


def depth_bucket(d: int) -> str:
    return "0" if d <= 0 else ("1" if d == 1 else ("2" if d == 2 else ">=3"))


def flagtype(text: str, tlen: int) -> str:
    if any(w in text for w in ATTACK):
        return "tox"
    if is_sticker_only(text):
        return "sticker"
    if tlen <= 6 and is_emoji_only(text):
        return "emoji"
    if any(w in text for w in CHECKIN):
        return "checkin"
    return "plain"


# ----------------------------- data loading ----------------------------- #
def load_bvids(a805_path: str) -> list[str]:
    con = sqlite3.connect(a805_path)
    rows = con.execute("select bvid from videos order by bvid").fetchall()
    con.close()
    return [r[0] for r in rows]


def load_video_meta(corpus_path: str, bvids: list[str]) -> dict[str, dict]:
    con = sqlite3.connect(corpus_path)
    meta: dict[str, dict] = {}
    CHUNK = 400
    for i in range(0, len(bvids), CHUNK):
        part = bvids[i:i + CHUNK]
        ph = ",".join("?" * len(part))
        for bvid, title, author, desc in con.execute(
            f"select bvid,title,author_name,coalesce(description,'') "
            f"from videos where bvid in ({ph})", part,
        ):
            meta[bvid] = {"title": title or "", "author": author or "",
                          "desc": desc or "", "tags": []}
        for bvid, tag in con.execute(
            f"select bvid,tag from video_tags where bvid in ({ph})", part
        ):
            if bvid in meta:
                meta[bvid]["tags"].append(tag)
    con.close()
    return meta


def suggested_summary(meta: dict) -> str:
    title = (meta.get("title") or "").strip()
    desc = re.sub(r"\s+", " ", meta.get("desc") or "").strip()
    first = re.split(r"[。！？.!?]", desc)[0][:60] if desc else ""
    s = title + ("｜" + first if first and first not in title else "")
    return s[:100]


def video_is_labor(meta: dict) -> str:
    blob = (meta.get("title") or "") + " " + " ".join(meta.get("tags") or []) \
        + " " + (meta.get("desc") or "")[:200]
    return "labor" if any(w in blob for w in LABOR) else "other"


# ----------------------------- per-video pass ----------------------------- #
def process_video(con, bvid, meta) -> tuple[list[tuple], int]:
    """Compact candidate records for one video: (rpid, stratum_key_json, bvid).

    Only comments whose **entire ancestor chain resolves to a floor root within
    the collected data** are emitted. Comments whose parent was deleted/not
    collected cannot be given a faithful context card (empty/degraded anchor and
    path), so they are excluded here rather than silently mis-contextualised
    downstream. Returns (candidates, n_excluded_broken_chain).
    """
    rows = con.execute(
        "select rpid,parent_rpid,root_rpid,hierarchy_level,content,pub_ts "
        "from comments where bvid=? order by rpid", (bvid,),
    ).fetchall()
    if not rows:
        return [], 0
    by_rpid = {r[0]: r for r in rows}
    floor_of = {}
    floor_size = defaultdict(int)
    for rpid, parent, root, lvl, content, ts in rows:
        fk = rpid if lvl == "root" else root
        floor_of[rpid] = fk
        floor_size[fk] += 1

    # --- ancestor-chain validity (memoised, per video) ---
    valid_cache: dict[str, bool] = {}

    def chain_valid(rpid: str) -> bool:
        if rpid in valid_cache:
            return valid_cache[rpid]
        stack = []
        cur = rpid
        result = None
        while True:
            if cur in valid_cache:
                result = valid_cache[cur]
                break
            row = by_rpid.get(cur)
            if row is None:
                result = False
                break
            lvl, par = row[3], row[1]
            if lvl == "root":
                result = True
                valid_cache[cur] = True
                break
            stack.append(cur)
            if (not par) or par == "0" or par == cur:
                result = False
                break
            cur = par
            if len(stack) > 5000:
                result = False
                break
        for node in stack:
            valid_cache[node] = result
        valid_cache[rpid] = result
        return result

    depth_cache: dict[str, int] = {}

    def depth(rpid):
        if rpid in depth_cache:
            return depth_cache[rpid]
        stack = []
        cur = rpid
        while cur in by_rpid and cur not in depth_cache:
            par = by_rpid[cur][1]
            stack.append(cur)
            if (not par) or par not in by_rpid or par == cur:
                depth_cache[cur] = 0
                stack.pop()
                break
            cur = par
        d = depth_cache.get(cur, 0)
        for node in reversed(stack):
            d += 1
            depth_cache[node] = d
        return depth_cache.get(rpid, 0)

    topic = video_is_labor(meta)
    out = []
    n_degraded = 0
    for rpid, parent, root, lvl, content, ts in rows:
        content = content or ""
        tlen = len(content)
        if tlen == 0:
            continue
        degraded = 0
        if lvl != "root" and not chain_valid(rpid):
            degraded = 1  # ancestor (parent or higher) missing -> degraded context
            n_degraded += 1
        d = 0 if lvl == "root" else depth(rpid)
        skey = (topic, heat_bucket(floor_size[floor_of[rpid]]), depth_bucket(d),
                polarity_bucket(content), flagtype(content, tlen), length_bucket(tlen))
        out.append((rpid, json.dumps(skey, ensure_ascii=False), bvid, degraded))
    return out, n_degraded


# ----------------------------- stratified sample ----------------------------- #
def stratified_sample(cands: list[tuple], target: int, seed: int) -> dict[str, dict]:
    """Stratified sample with *correct* first-order inclusion probabilities.

    Design: stratified SRSWOR with quota ∝ sqrt(stratum_size), min 1, capped at
    the stratum size; the union is then trimmed/ topped-up to exactly ``target``.

    Crucially, ``inclusion_prob`` is recomputed from the **realised** number of
    selected units per stratum (``n_h / N_h``) *after* the trim/top-up step —
    not from the pre-trim quota. This keeps the recorded design consistent with
    what was actually sampled, so downstream Horvitz–Thompson reweighting is
    unbiased. Deterministic: every ordering is sorted, every shuffle seeded.
    """
    by_stratum: dict[str, list[str]] = defaultdict(list)
    for rpid, skey, bvid, *rest in cands:
        by_stratum[skey].append(rpid)
    for k in by_stratum:
        by_stratum[k].sort()

    rng = random.Random(seed)
    keys = sorted(by_stratum)
    weights = {k: math.sqrt(len(by_stratum[k])) for k in keys}
    total_w = sum(weights.values()) or 1.0

    # 1) per-stratum quota -> shuffled prefix selection
    selected: list[tuple[str, str]] = []  # (stratum_key, rpid)
    for k in keys:
        quota = max(1, round(target * weights[k] / total_w))
        quota = min(quota, len(by_stratum[k]))
        pool = by_stratum[k][:]
        rng.shuffle(pool)
        selected.extend((k, r) for r in pool[:quota])

    # 2) exact-size adjustment (random but seeded)
    rng.shuffle(selected)
    if len(selected) > target:
        selected = selected[:target]
    elif len(selected) < target:
        chosen_set = {r for _, r in selected}
        skey_of = {r: sk for r, sk, *rest in cands}
        leftover = sorted(r for r, *rest in cands if r not in chosen_set)
        rng.shuffle(leftover)
        for r in leftover[: target - len(selected)]:
            selected.append((skey_of[r], r))

    # 3) recompute inclusion probability from the REALISED per-stratum counts
    final_n = defaultdict(int)
    for k, _ in selected:
        final_n[k] += 1
    picked: dict[str, dict] = {}
    for k, r in selected:
        N = len(by_stratum[k])
        n = final_n[k]
        picked[r] = {
            "stratum_key": k,
            "stratum_n": N,
            "inclusion_prob": round(n / N, 6),
            "sampled_by": "stratified_srswor",
        }
    return picked


# ----------------------------- card building ----------------------------- #
def build_card(con, target_rpid: str, meta_by_bvid: dict) -> dict:
    row = con.execute(
        "select rpid,parent_rpid,root_rpid,hierarchy_level,user_name,content,pub_ts,"
        "like_count,bvid from comments where rpid=?", (target_rpid,)
    ).fetchone()
    (rpid, parent, root, lvl, user, content, ts, like, bvid) = row
    meta = meta_by_bvid.get(bvid, {"title": "", "author": "", "desc": "", "tags": []})
    is_root = (lvl == "root")
    floor_key = rpid if is_root else root

    floor = con.execute(
        "select rpid,parent_rpid,user_name,content,pub_ts from comments "
        "where bvid=? and (root_rpid=? or rpid=?) order by pub_ts, rpid",
        (bvid, floor_key, floor_key),
    ).fetchall()
    fmap = {f[0]: f for f in floor}

    # ancestor chain root -> ... -> parent
    path = []
    cur = parent
    guard = 0
    while cur and cur in fmap and guard < 10000:
        f = fmap[cur]
        path.append({"rpid": f[0], "user": f[2] or "", "text": f[3] or ""})
        if f[1] == f[0] or not f[1]:
            break
        cur = f[1]
        guard += 1
    path.reverse()

    # context integrity + per-dimension assessability.
    #   complete      : full chain to floor root
    #   ancestor_gap  : parent present (local exchange intact) but a higher
    #                   ancestor is missing -> all four dimensions assessable
    #   parent_gap    : the immediate parent (the comment being replied to) is
    #                   gone -> D1/D2/D4 assessable from target text, but D3
    #                   (topic drift, measured *relative to the parent*) is not.
    context_degraded = 0
    degraded_reason = None
    context_status = "complete"
    if not is_root:
        if parent not in fmap:
            context_degraded = 1
            degraded_reason = "parent_missing"
            context_status = "parent_gap"
        elif not path or path[0]["rpid"] != floor_key:
            context_degraded = 1
            degraded_reason = "ancestor_missing"
            context_status = "ancestor_gap"
    d3_assessable = 0 if context_status == "parent_gap" else 1

    # branch root = deepest ancestor whose parent is the floor root
    anchor_rpid, anchor_text = "", ""
    anchor_degraded = 0
    if is_root:
        anchor_rpid, anchor_text = rpid, content or ""
    elif parent == floor_key:
        anchor_rpid, anchor_text = rpid, content or ""
    else:
        cur, guard = rpid, 0
        while cur in fmap and guard < 10000:
            f = fmap[cur]
            if f[1] == floor_key:
                anchor_rpid, anchor_text = cur, f[3] or ""
                break
            cur = f[1]
            guard += 1
        if not anchor_rpid:
            # chain is broken before reaching the branch root: degrade the anchor
            # to the floor root so D3 (drift) is still well-defined.
            rootrow = fmap.get(floor_key)
            anchor_rpid = floor_key
            anchor_text = (rootrow[3] or "") if rootrow else ""
            anchor_degraded = 1

    # core: same-floor, ts <= parent.ts (floor root + nearest <=5)
    core = []
    if not is_root:
        p_ts = int(fmap[parent][4] or 0) if parent in fmap else int(ts or 0)
        prior = [f for f in floor if f[0] != rpid and int(f[4] or 0) <= p_ts]
        rootrow = fmap.get(floor_key)
        picked = []
        if rootrow and rootrow[0] != rpid:
            picked.append(rootrow)
        # "前面 N 个评论": the N nearest preceding comments in this floor
        tail = [f for f in prior if not (rootrow and f[0] == rootrow[0])][-CORE_PRIOR_N:]
        picked.extend(tail)
        core = [{"rpid": f[0], "user": f[2] or "", "ts": int(f[4] or 0),
                 "text": f[3] or ""} for f in picked]

    # if the immediate parent is gone, record what was lost (rpid + how many
    # still-present replies point at it) so the UI can say exactly what's missing.
    missing_parent_rpid = None
    orphan_replies_n = 0
    if degraded_reason == "parent_missing":
        missing_parent_rpid = parent
        orphan_replies_n = con.execute(
            "select count(*) from comments where bvid=? and parent_rpid=?",
            (bvid, parent)).fetchone()[0]

    return {
        "unit_key": f"{bvid}:{rpid}",
        "bvid": bvid,
        "video_title": meta["title"],
        "video_author": meta["author"],
        "video_tags_json": json.dumps(meta["tags"], ensure_ascii=False),
        "video_summary_suggested": suggested_summary(meta),
        "video_summary_final": "",
        "video_url": f"https://www.bilibili.com/video/{bvid}",
        "anchor_rpid": anchor_rpid,
        "anchor_text": anchor_text,
        "anchor_claim": "",
        "path_json": json.dumps(path, ensure_ascii=False),
        "core_json": json.dumps(core, ensure_ascii=False),
        "target_rpid": rpid,
        "target_user": user or "",
        "target_ts": int(ts or 0),
        "target_text": content or "",
        "target_meta_json": json.dumps(
            {"like": like, "is_root": is_root, "depth_hint": len(path),
             "context_degraded": context_degraded, "degraded_reason": degraded_reason,
             "context_status": context_status,
             "missing_parent_rpid": missing_parent_rpid,
             "orphan_replies": orphan_replies_n},
            ensure_ascii=False),
        "context_degraded": context_degraded,
        "anchor_degraded": anchor_degraded,
        "degraded_reason": degraded_reason,
        "context_status": context_status,
        "d3_assessable": d3_assessable,
        "created_at": int(time.time()),
    }


def file_info(path: str) -> dict:
    try:
        st = os.stat(path)
        return {"path": path, "size": st.st_size, "mtime": int(st.st_mtime)}
    except OSError:
        return {"path": path, "size": None, "mtime": None}


# ----------------------------- main ----------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a805", default="/Users/mushiquan/Desktop/bilibili_work/data/analysis_805.db")
    ap.add_argument("--corpus", default="/Users/mushiquan/Desktop/bilibili_work/data/corpus.db")
    ap.add_argument("--out", default="/Users/mushiquan/Desktop/bili-annotator/data/annotation.db")
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--force", action="store_true",
                    help="overwrite even if annotations already exist")
    args = ap.parse_args()

    if os.path.exists(args.out):
        con0 = sqlite3.connect(args.out)
        try:
            n_ann = con0.execute("select count(*) from annotations").fetchone()[0]
        except sqlite3.OperationalError:
            n_ann = 0
        con0.close()
        if n_ann and not args.force:
            raise SystemExit(
                f"!! {args.out} 已有 {n_ann} 条标注，拒绝覆盖。"
                f"确认要重建请加 --force（会清空标注）。")

    bvids = load_bvids(args.a805)
    print(f"bvids: {len(bvids)}")
    meta_by_bvid = load_video_meta(args.corpus, bvids)
    print(f"video meta loaded: {len(meta_by_bvid)}")

    con = sqlite3.connect(args.corpus)
    all_cands: list[tuple] = []
    n_degraded = 0
    for i, bvid in enumerate(bvids):
        cands, degraded = process_video(con, bvid, meta_by_bvid.get(
            bvid, {"title": "", "author": "", "desc": "", "tags": []}))
        all_cands.extend(cands)
        n_degraded += degraded
        if (i + 1) % 200 == 0:
            print(f"  processed {i+1}/{len(bvids)} videos, cands={len(all_cands):,}, "
                  f"context_degraded={n_degraded:,}")
    print(f"total candidate comments: {len(all_cands):,} "
          f"(context_degraded: {n_degraded:,})")

    picked = stratified_sample(all_cands, args.target, args.seed)
    print(f"sampled {len(picked)} units; building cards...")

    out = sqlite3.connect(args.out)
    ensure_schema(out)
    out.execute("delete from units")
    out.execute("delete from sqlite_sequence where name='units'")
    if args.force:
        out.execute("delete from annotations")
    built = 0
    for j, rpid in enumerate(picked):
        try:
            card = build_card(con, rpid, meta_by_bvid)
            info = picked[rpid]
            card["stratum_key"] = info["stratum_key"]
            card["stratum_n"] = info["stratum_n"]
            card["inclusion_prob"] = info["inclusion_prob"]
            card["sampled_by"] = info["sampled_by"]
            cols = ",".join(card.keys())
            ph = ",".join("?" * len(card))
            out.execute(f"insert into units({cols}) values({ph})", list(card.values()))
            built += 1
        except Exception as e:  # noqa: BLE001 - keep going on a single bad row
            print(f"  skip {rpid}: {e}")
    # provenance
    prov = {
        "schema_version": SCHEMA_VERSION,
        "created_at": str(int(time.time())),
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sampling.seed": str(args.seed),
        "sampling.target_n": str(args.target),
        "sampling.n_units": str(built),
        "sampling.n_candidates": str(len(all_cands)),
        "sampling.n_context_degraded_candidates": str(n_degraded),
        "sampling.context_degraded_note": (
            "comments whose ancestor chain is broken (deleted/uncaptured parent) "
            "are KEPT and flagged context_degraded=1; D3 anchor degrades to floor root"),
        "sampling.stratification": STRATIFICATION_SPEC,
        "sampling.quota_rule": "quota ~ sqrt(stratum_size), min 1, then trim/topup",
        "source.a805": json.dumps(file_info(args.a805), ensure_ascii=False),
        "source.corpus": json.dumps(file_info(args.corpus), ensure_ascii=False),
        "script.materialize_units": os.path.basename(__file__),
    }
    for k, v in prov.items():
        out.execute("insert or replace into meta(key,value) values(?,?)", (k, str(v)))
    out.commit()
    n = out.execute("select count(*) from units").fetchone()[0]
    out.close()
    con.close()
    print(f"\nDone. units in {args.out}: {n}")
    print(f"seed={args.seed} persisted in meta('sampling.seed')")


if __name__ == "__main__":
    main()
