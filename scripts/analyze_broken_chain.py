"""Characterise 'broken-chain' comments (those excluded from annotation units).

A comment is *broken-chain* if any ancestor between it and its floor root is
missing from the corpus (deleted / not collected), so a faithful context card
cannot be built. This script answers:

 1) Are they mostly noise (stickers / check-ins / abuse) or substantive talk?
    -> compares broken vs valid comments on length, flag-type and attack words,
       and prints a readable sample.
 2) Where do they concentrate (which videos / topics)?
    -> per-video broken counts & rates joined to titles, plus labour-vs-other.
 3) How deep is the break (immediate parent missing vs deeper ancestor)?

Usage:
  python scripts/analyze_broken_chain.py \
      --a805 /Users/mushiquan/Desktop/bilibili_work/data/analysis_805.db \
      --corpus /Users/mushiquan/Desktop/bilibili_work/data/corpus.db
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from materialize_units import (  # noqa: E402
    ATTACK, CHECKIN, flagtype, is_emoji_only, is_sticker_only, video_is_labor,
)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def pct(a, b):
    return f"{(a / b * 100):.1f}%" if b else "n/a"


def analyse_video(rows, bvid):
    """Return (broken_records, valid_records, stats) for one video."""
    by_rpid = {r[0]: r for r in rows}
    # (rpid, parent, root, lvl, content)
    valid_cache: dict[str, bool] = {}

    def chain_valid(rpid):
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

    broken, valid = [], []
    for rpid, parent, root, lvl, content in rows:
        content = content or ""
        if lvl == "root" or not content:
            continue
        rec = {
            "rpid": rpid, "bvid": bvid, "parent": parent, "root": root,
            "content": content, "len": len(content),
            "parent_missing": parent not in by_rpid,
            "flag": flagtype(content, len(content)),
        }
        if chain_valid(rpid):
            rec["flag"] = rec["flag"]
            valid.append(rec)
        else:
            broken.append(rec)
    return broken, valid


def describe(records, label):
    n = len(records)
    if not n:
        print(f"  {label}: 0")
        return {}
    lens = [r["len"] for r in records]
    flags = Counter(r["flag"] for r in records)
    attack = sum(1 for r in records if any(w in r["content"] for w in ATTACK))
    checkin = sum(1 for r in records if any(w in r["content"] for w in CHECKIN))
    sticker = sum(1 for r in records if is_sticker_only(r["content"]))
    emoji = sum(1 for r in records if r["len"] <= 6 and is_emoji_only(r["content"]))
    short = sum(1 for r in records if r["len"] <= 5)
    long_ = sum(1 for r in records if r["len"] >= 30)
    substantive = sum(
        1 for r in records
        if r["len"] >= 15 and not any(w in r["content"] for w in ATTACK)
        and not is_sticker_only(r["content"])
    )
    reply_prefix = sum(1 for r in records if r["content"].startswith("回复 @"))
    print(f"\n  [{label}] n={n:,}")
    print(f"    长度: 中位 {median(lens):.0f} / 均值 {sum(lens)/n:.1f} "
          f"| ≤5字 {pct(short,n)} | ≥30字 {pct(long_,n)}")
    print(f"    flagtype: {dict(flags)}")
    print(f"    含攻击词 {pct(attack,n)} | 含打卡词 {pct(checkin,n)} "
          f"| 纯表情 {pct(sticker,n)} | 纯emoji {pct(emoji,n)}")
    print(f"    实质长文(≥15字且无攻击词且非纯表情) {pct(substantive,n)}")
    print(f"    以『回复 @』开头(楼中楼对话) {pct(reply_prefix,n)}")
    return {"n": n, "median": median(lens), "flags": flags, "attack": attack,
            "substantive": substantive}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a805",
                    default="/Users/mushiquan/Desktop/bilibili_work/data/analysis_805.db")
    ap.add_argument("--corpus",
                    default="/Users/mushiquan/Desktop/bilibili_work/data/corpus.db")
    ap.add_argument("--samples", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    a = sqlite3.connect(args.a805)
    bvids = [r[0] for r in a.execute("select bvid from videos order by bvid")]
    a.close()

    c = sqlite3.connect(args.corpus)
    c.row_factory = sqlite3.Row
    titles = {r["bvid"]: (r["title"] or "", r["author_name"] or "")
              for r in c.execute("select bvid,title,author_name from videos")}
    tags = defaultdict(list)
    for r in c.execute("select bvid,tag from video_tags"):
        tags[r["bvid"]].append(r["tag"])

    all_broken, all_valid = [], []
    per_video = []
    topic_broken = Counter()
    topic_total = Counter()
    immediate_missing = 0

    for i, bvid in enumerate(bvids):
        rows = c.execute(
            "select rpid,parent_rpid,root_rpid,hierarchy_level,content "
            "from comments where bvid=? order by rpid", (bvid,)).fetchall()
        rows = [(r["rpid"], r["parent_rpid"], r["root_rpid"],
                 r["hierarchy_level"], r["content"]) for r in rows]
        broken, valid = analyse_video(rows, bvid)
        all_broken.extend(broken)
        all_valid.extend(valid)
        total = len(broken) + len(valid)
        per_video.append({
            "bvid": bvid, "broken": len(broken), "total": total,
            "rate": (len(broken) / total) if total else 0.0,
            "title": titles.get(bvid, ("", ""))[0],
        })
        topic = video_is_labor({"title": titles.get(bvid, ("", ""))[0],
                                "tags": tags.get(bvid, []), "desc": ""})
        topic_broken[topic] += len(broken)
        topic_total[topic] += total
        immediate_missing += sum(1 for r in broken if r["parent_missing"])
        if (i + 1) % 200 == 0:
            print(f"  ... {i+1}/{len(bvids)} videos, broken so far={len(all_broken):,}")

    print("\n" + "=" * 70)
    print(f"总评论(非空、非根): {len(all_broken) + len(all_valid):,}")
    print(f"断链评论: {len(all_broken):,} ({pct(len(all_broken), len(all_broken)+len(all_valid))})")
    print(f"其中『直接父评论缺失』: {immediate_missing:,} "
          f"({pct(immediate_missing, len(all_broken))}) ；其余为更深层祖先缺失")

    print("\n== 内容画像（断链 vs 正常）==")
    describe(all_broken, "断链 BROKEN")
    describe(all_valid, "正常 VALID")

    print("\n== 断链评论按主题 ==")
    for t in topic_total:
        print(f"  {t}: {topic_broken[t]:,} / {topic_total[t]:,} "
              f"= {pct(topic_broken[t], topic_total[t])}")

    # concentration across videos
    affected = [v for v in per_video if v["broken"] > 0]
    tot_broken = len(all_broken)
    top10 = sorted(per_video, key=lambda x: -x["broken"])[:10]
    top10_share = sum(v["broken"] for v in top10)
    print(f"\n== 分布集中度 ==")
    print(f"  有断链评论的视频: {len(affected)} / {len(per_video)} "
          f"({pct(len(affected), len(per_video))})")
    print(f"  Top-10 视频占全部断链: {top10_share:,} / {tot_broken:,} "
          f"= {pct(top10_share, tot_broken)}")

    print("\n== 断链评论集中在哪些视频（按数量 Top 15）==")
    for v in sorted(per_video, key=lambda x: -x["broken"])[:15]:
        print(f"  {v['broken']:>5}  ({v['rate']*100:4.1f}%)  {v['bvid']}  {v['title'][:42]}")

    print("\n== 断链比例最高的视频（≥30 条回复，Top 15）==")
    cand = [v for v in per_video if v["total"] >= 30]
    for v in sorted(cand, key=lambda x: -x["rate"])[:15]:
        print(f"  {v['rate']*100:4.1f}%  {v['broken']:>4}/{v['total']:<5}  "
              f"{v['bvid']}  {v['title'][:42]}")

    rng = random.Random(args.seed)
    print(f"\n== 断链评论随机样例（n={args.samples}，自行判断是否有实质讨论）==")
    for r in rng.sample(all_broken, min(args.samples, len(all_broken))):
        print(f"  [{r['len']:>3}字] {r['content'][:90].replace(chr(10),' ')}")

    print("\n== 断链评论中较长/疑似实质讨论的样例 ==")
    substantive = [r for r in all_broken
                   if r["len"] >= 30 and not any(w in r["content"] for w in ATTACK)]
    for r in rng.sample(substantive, min(args.samples, len(substantive))):
        print(f"  [{r['len']:>3}字] {r['content'][:90].replace(chr(10),' ')}")


if __name__ == "__main__":
    main()
