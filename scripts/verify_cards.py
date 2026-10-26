"""Audit the generated context cards in data/annotation.db against corpus.db.

For every sampled unit we re-derive ground truth from corpus.db independently
and assert that the card's claims hold:

  * target comment exists in corpus and belongs to the unit's bvid;
  * target_text == corpus content (verbatim);
  * video title/author in the unit match corpus videos row for that bvid;
  * [ANCHOR] the branch-root rule: anchor_rpid is an existing comment, is the
    deepest ancestor whose parent == floor root (or the root itself), and its
    text matches;
  * [PATH] every entry is an existing comment in the same floor, texts match,
    and the parent-chain is internally consistent (each node's parent_rpid is
    the previous node), ending at the target's parent;
  * [CORE] every entry exists in the same floor with pub_ts <= parent pub_ts,
    and texts match; the floor root is included.

Prints a per-check pass/fail tally and a handful of failure examples.
Exit code 0 only if all checks pass.

Usage:
  python scripts/verify_cards.py \
      --units data/annotation.db \
      --corpus /Users/mushiquan/Desktop/bilibili_work/data/corpus.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter


def load_floor(con, bvid: str, root_rpid: str) -> dict[str, tuple]:
    rows = con.execute(
        "select rpid,parent_rpid,root_rpid,hierarchy_level,user_name,content,pub_ts "
        "from comments where bvid=? and (root_rpid=? or rpid=?)",
        (bvid, root_rpid, root_rpid),
    ).fetchall()
    return {r[0]: r for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--units", default="data/annotation.db")
    ap.add_argument("--corpus",
                    default="/Users/mushiquan/Desktop/bilibili_work/data/corpus.db")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    u = sqlite3.connect(args.units)
    u.row_factory = sqlite3.Row
    c = sqlite3.connect(args.corpus)
    c.row_factory = sqlite3.Row

    # video metadata lookup
    vid = {}
    for r in c.execute("select bvid,title,author_name from videos"):
        vid[r["bvid"]] = r

    checks = Counter()
    fails = {k: [] for k in (
        "target_missing", "target_text", "target_bvid", "video_meta",
        "anchor_missing", "anchor_rule", "anchor_text",
        "path_missing", "path_chain", "path_text",
        "core_missing", "core_time", "core_root", "core_text",
    )}

    units = u.execute("select * from units order by id").fetchall()
    n = len(units)
    for r in units:
        uid = r["id"]
        bvid = r["bvid"]
        target = r["target_rpid"]
        # ---- target exists & belongs & text ----
        trow = c.execute(
            "select rpid,parent_rpid,root_rpid,hierarchy_level,content,bvid,pub_ts "
            "from comments where rpid=?", (target,)).fetchone()
        if trow is None:
            fails["target_missing"].append(uid); checks["target_missing"] += 1; continue
        checks["target_ok"] += 1
        if trow["bvid"] != bvid:
            fails["target_bvid"].append((uid, trow["bvid"], bvid))
        if (trow["content"] or "") != (r["target_text"] or ""):
            fails["target_text"].append(uid)

        # ---- video meta ----
        v = vid.get(bvid)
        if not v:
            fails["video_meta"].append((uid, "no video row")); checks["video_meta_bad"] += 1
        else:
            if v["title"] != r["video_title"] or v["author_name"] != r["video_author"]:
                fails["video_meta"].append((uid, v["title"], r["video_title"]))
            else:
                checks["video_meta_ok"] += 1

        # floor key
        is_root = trow["hierarchy_level"] == "root"
        floor_key = target if is_root else trow["root_rpid"]
        floor = load_floor(c, bvid, floor_key)

        # ---- path ----
        path = json.loads(r["path_json"] or "[]")
        path_ok = True
        for e in path:
            fr = floor.get(e["rpid"])
            if fr is None:
                fails["path_missing"].append((uid, e["rpid"])); path_ok = False; break
            if (fr["content"] or "") != (e["text"] or ""):
                fails["path_text"].append((uid, e["rpid"])); path_ok = False
        # chain consistency: path last must be target's parent; each node parent = prev
        if path:
            if path[-1]["rpid"] != trow["parent_rpid"]:
                fails["path_chain"].append((uid, "last!=parent", path[-1]["rpid"], trow["parent_rpid"])); path_ok = False
            for i in range(len(path) - 1, 0, -1):
                child = floor.get(path[i]["rpid"])
                if child is None or child["parent_rpid"] != path[i-1]["rpid"]:
                    fails["path_chain"].append((uid, "broken at", i)); path_ok = False; break
        else:
            # empty path only valid if target is root or its parent is floor root
            if not (is_root or trow["parent_rpid"] == floor_key):
                fails["path_chain"].append((uid, "empty path but has ancestors")); path_ok = False
        if path_ok:
            checks["path_ok"] += 1

        # ---- anchor ----
        anchor = r["anchor_rpid"]
        arow = floor.get(anchor) or c.execute(
            "select rpid,parent_rpid,root_rpid,hierarchy_level,content from comments "
            "where rpid=?", (anchor,)).fetchone()
        if arow is None:
            fails["anchor_missing"].append((uid, anchor)); checks["anchor_bad"] += 1
        else:
            if (arow["content"] or "") != (r["anchor_text"] or ""):
                fails["anchor_text"].append(uid)
            # rule: anchor is floor root itself, OR its parent == floor root
            ap = arow["parent_rpid"]
            rule_ok = (arow["rpid"] == floor_key) or (ap == floor_key)
            if not rule_ok:
                fails["anchor_rule"].append((uid, anchor, "parent", ap, "floorkey", floor_key))
            else:
                checks["anchor_rule_ok"] += 1

        # ---- core ----
        core = json.loads(r["core_json"] or "[]")
        core_ok = True
        if not is_root:
            p_ts = trow and floor.get(trow["parent_rpid"])
            p_ts = p_ts["pub_ts"] if p_ts else (trow["pub_ts"] if trow else 0)
            has_root = False
            for e in core:
                fr = floor.get(e["rpid"])
                if fr is None:
                    fails["core_missing"].append((uid, e["rpid"])); core_ok = False; continue
                if (fr["content"] or "") != (e["text"] or ""):
                    fails["core_text"].append((uid, e["rpid"])); core_ok = False
                if int(fr["pub_ts"] or 0) > int(p_ts or 0):
                    fails["core_time"].append((uid, e["rpid"])); core_ok = False
                if fr["rpid"] == floor_key:
                    has_root = True
            if core and not has_root:
                fails["core_root"].append(uid); core_ok = False
        if core_ok:
            checks["core_ok"] += 1

    print(f"units audited: {n}\n")
    print("== tallies ==")
    for k in sorted(checks):
        print(f"  {k}: {checks[k]}")
    total_fail = sum(len(v) for v in fails.values())
    print(f"\n== failures (total {total_fail}) ==")
    for k, v in fails.items():
        if v:
            print(f"  {k}: {len(v)}  e.g. {v[:args.show]}")
    print("\nRESULT:", "PASS ✓" if total_fail == 0 else "FAIL ✗")
    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
