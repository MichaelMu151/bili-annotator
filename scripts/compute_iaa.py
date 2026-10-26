"""Inter-annotator agreement (IAA) for the BiliAnnotator gold set.

Computes, from ``data/annotation.db``:

  * per-label agreement for D1/D2 (multi-label 0/1 -> per-label binary),
  * nominal Krippendorff's alpha for D3.type (single choice),
  * ordinal Krippendorff's alpha for D3.extent (1..4) and D4.toxicity (0..3),
  * Cohen's kappa where exactly two annotators overlap,
  * pairwise confusion counts for the ambiguous categories.

Implementation notes:
  * Items with fewer than 2 ratings are skipped (alpha needs overlap).
  * Missing values are treated as *not applicable*, not as a category.
  * Multi-label D1/D2 are decomposed: each label becomes its own binary item.

Usage:
    python scripts/compute_iaa.py                # all dimensions
    python scripts/compute_iaa.py --dim D3
    python scripts/compute_iaa.py --out report.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "annotation.db"

D1_LABELS = ("1.1", "1.2", "1.3", "1.4", "1.5")
D2_LABELS = ("2.1", "2.2", "2.3", "2.4", "2.5")


# Human-readable code → short name, so the IAA report matches the codebook.
D1_NAMES = {"1.1": "反讽/倒置", "1.2": "嘲讽/挖苦", "1.3": "犬儒虚无/解构",
            "1.4": "自嘲/苦情", "1.5": "直白陈述"}
D2_NAMES = {"2.1": "建设性论辩", "2.2": "情绪共鸣/连带", "2.3": "动机怀疑/诛心",
            "2.4": "纯泄愤/对抗", "2.5": "虚无解构/劝退"}
D3_NAMES = {"3.1": "议题聚焦", "3.2": "合理延展", "3.3": "离题转移", "3.4": "元争论"}


def _name(dim, code):
    table = {"D1": D1_NAMES, "D2": D2_NAMES, "D3": D3_NAMES}.get(dim, {})
    nm = table.get(code)
    return f"{code} {nm}" if nm else code


def _status(a) -> str:
    """Human-readable reliability verdict against conventional alpha thresholds."""
    if a is None:
        return "n/a (样本不足/无重叠)"
    if a >= 0.80:
        return "✓ 可靠"
    if a >= 0.67:
        return "⚠ 探索性可接受"
    return "✗ 需修订 codebook / 合并类"


def fmt(a) -> str:
    return "None" if a is None else f"{a:.3f}"


# --------------------------------------------------------------------------- #
# Krippendorff's alpha
# --------------------------------------------------------------------------- #
def _coincidence_matrix(units: list[list[str]]) -> tuple[dict, dict]:
    """Build the coincidence matrix for nominal data from per-unit rating lists."""
    o = defaultdict(float)  # (c,k) -> count
    n_total = defaultdict(float)  # c -> total count (marginal)
    for ratings in units:
        m = len(ratings)
        if m < 2:
            continue
        for i in range(m):
            for j in range(m):
                if i == j:
                    continue
                o[(ratings[i], ratings[j])] += 1.0 / (m - 1)
    for (c, k), v in o.items():
        n_total[c] += v
    return dict(o), dict(n_total)


def _delta_nominal(c: str, k: str) -> float:
    return 0.0 if c == k else 1.0


def _delta_ordinal(c: str, k: str, order: list[str], n_total: dict) -> float:
    """Krippendorff's ordinal difference function.

    δ²(c,k) = ( Σ_{g=lo..hi} n_g  −  (n_lo + n_hi)/2 )²
    where n_g are the marginal counts of the coincidence matrix for each value.
    """
    idx = {v: i for i, v in enumerate(order)}
    if c not in idx or k not in idx:
        return _delta_nominal(c, k)
    i, j = idx[c], idx[k]
    if i == j:
        return 0.0
    lo, hi = min(i, j), max(i, j)
    span = sum(n_total.get(order[g], 0.0) for g in range(lo, hi + 1))
    edge = (n_total.get(order[lo], 0.0) + n_total.get(order[hi], 0.0)) / 2.0
    return (span - edge) ** 2


def krippendorff_alpha(units: list[list[str]], level: str = "nominal",
                       order: list[str] | None = None) -> float | None:
    """Return alpha, or None if undefined (no overlap / single category)."""
    usable = [u for u in units if len(u) >= 2]
    if not usable:
        return None
    o, n_total = _coincidence_matrix(usable)
    n = sum(n_total.values())
    if n <= 1:
        return None

    def delta(c, k):
        if level == "ordinal" and order:
            return _delta_ordinal(c, k, order, n_total)
        return _delta_nominal(c, k)

    d_obs = sum(cnt * delta(c, k) for (c, k), cnt in o.items())
    d_exp = sum(n_total[c] * n_total[k] * delta(c, k)
                for c in n_total for k in n_total)
    if d_exp == 0:
        return None
    return 1.0 - (n - 1) * d_obs / d_exp


def cohens_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa for paired ratings (same length, same item order)."""
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


# --------------------------------------------------------------------------- #
# Data extraction
# --------------------------------------------------------------------------- #
def load(conn) -> dict[int, dict[str, dict]]:
    """{unit_id: {annotator: payload}}"""
    out: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in conn.execute("select unit_id, annotator, payload_json from annotations"):
        try:
            out[r["unit_id"]][r["annotator"]] = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            continue
    return out


def multi_label_units(data, dim: str, label: str) -> list[list[str]]:
    """For a multi-label dimension: per-unit ratings of presence('1')/absent('0')."""
    units = []
    for uid, by_ann in data.items():
        ratings = []
        for payload in by_ann.values():
            v = payload.get(dim) or {}
            if not (v.get("labels") or v.get("primary")):
                continue  # skipped this dimension -> not a rating
            ratings.append("1" if label in (v.get("labels") or []) else "0")
        units.append(ratings)
    return units


def single_units(data, dim: str, field: str) -> list[list[str]]:
    units = []
    for uid, by_ann in data.items():
        ratings = []
        for payload in by_ann.values():
            v = payload.get(dim) or {}
            val = v.get(field)
            if val in (None, ""):
                continue
            ratings.append(str(val))
        units.append(ratings)
    return units


def paired(data, dim: str, field: str) -> tuple[list[str], list[str], list[str]]:
    """Return (annotator_a, annotator_b, unit_ids) pairs for 2-annotator overlap."""
    ann_counts = Counter()
    for by_ann in data.values():
        for a in by_ann:
            ann_counts[a] += 1
    if len(ann_counts) < 2:
        return "", "", []
    anames = [a for a, _ in ann_counts.most_common()]
    for a1, a2 in itertools.combinations(anames, 2):
        xs, ys, ids = [], [], []
        for uid, by_ann in data.items():
            if a1 in by_ann and a2 in by_ann:
                v1 = (by_ann[a1].get(dim) or {}).get(field)
                v2 = (by_ann[a2].get(dim) or {}).get(field)
                if v1 in (None, "") or v2 in (None, ""):
                    continue
                xs.append(str(v1))
                ys.append(str(v2))
                ids.append(uid)
        if xs:
            return a1, a2, list(zip(ids, xs, ys))
    return "", "", []


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dim", default="all", help="all | D1 | D2 | D3 | D4")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    data = load(conn)
    conn.close()

    n_units = len(data)
    n_anns = len({a for by_ann in data.values() for a in by_ann})
    print(f"units with >=1 annotation: {n_units}; annotators: {n_anns}")
    if n_anns < 2:
        print("\n!! 只有 <2 名标注员，无法计算 IAA。"
              "请让至少两人重叠标注同一批单元后重跑。")
        return

    report: dict = {"n_units": n_units, "n_annotators": n_anns, "per_label": {}}

    if args.dim in ("all", "D1"):
        print("\n== D1 修辞策略（多标签，逐标签二值 alpha） ==")
        for lab in D1_LABELS:
            a = krippendorff_alpha(multi_label_units(data, "D1", lab))
            report["per_label"][f"D1.{lab}"] = a
            print(f"  {_name('D1', lab):<16} alpha = {fmt(a):<7} {_status(a)}")

    if args.dim in ("all", "D2"):
        print("\n== D2 语用动机（多标签，逐标签二值 alpha） ==")
        for lab in D2_LABELS:
            a = krippendorff_alpha(multi_label_units(data, "D2", lab))
            report["per_label"][f"D2.{lab}"] = a
            print(f"  {_name('D2', lab):<16} alpha = {fmt(a):<7} {_status(a)}")

    if args.dim in ("all", "D3"):
        print("\n== D3 议题漂移 ==")
        a_type = krippendorff_alpha(single_units(data, "D3", "type"))
        a_ext = krippendorff_alpha(single_units(data, "D3", "extent"),
                                   level="ordinal", order=["1", "2", "3", "4"])
        report["D3.type_alpha"] = a_type
        report["D3.extent_alpha_ordinal"] = a_ext
        print(f"  type (nominal):   alpha = {fmt(a_type)}")
        print(f"  extent (ordinal): alpha = {fmt(a_ext)}")
        a1, a2, pairs = paired(data, "D3", "type")
        if pairs:
            k = cohens_kappa([p[1] for p in pairs], [p[2] for p in pairs])
            report["D3.type_kappa"] = k
            print(f"  type Cohen's kappa ({a1} vs {a2}, n={len(pairs)}) = {fmt(k)}")
            conf = Counter((p[1], p[2]) for p in pairs)
            diag = sum(v for (x, y), v in conf.items() if x == y)
            print(f"  raw agreement = {diag}/{len(pairs)} = "
                  f"{round(diag / len(pairs) * 100, 1)}%")
            print("  confusion(type) [row=rater1, col=rater2]:")
            types = sorted({x for x, _ in conf} | {y for _, y in conf})
            hdr = "        " + " ".join(f"{t:>6}" for t in types)
            print(hdr)
            for x in types:
                cells = " ".join(f"{conf.get((x, y), 0):>6}" for y in types)
                print(f"    {x:>4} {cells}")

    if args.dim in ("all", "D4"):
        print("\n== D4 情感与毒性 ==")
        a_pol = krippendorff_alpha(single_units(data, "D4", "polarity"),
                                   level="ordinal", order=["-1", "0", "1"])
        a_tox = krippendorff_alpha(single_units(data, "D4", "toxicity"),
                                   level="ordinal", order=["0", "1", "2", "3"])
        a_aro = krippendorff_alpha(single_units(data, "D4", "arousal"))
        report["D4.polarity_alpha_ordinal"] = a_pol
        report["D4.toxicity_alpha_ordinal"] = a_tox
        report["D4.arousal_alpha_nominal"] = a_aro
        print(f"  polarity (ordinal): alpha = {fmt(a_pol):<7} {_status(a_pol)}")
        print(f"  toxicity (ordinal): alpha = {fmt(a_tox):<7} {_status(a_tox)}")
        print(f"  arousal (nominal):  alpha = {fmt(a_aro):<7} {_status(a_aro)}")

    print("\n== 判定阈值（学术惯例） ==")
    print("  alpha >= 0.80 可靠；0.67-0.80 探索性可接受；< 0.67 需修订 codebook 或合并类")
    bad = [k for k, v in report["per_label"].items() if v is not None and v < 0.67]
    if bad:
        print("  !! 低于 0.67 的标签:", bad)
    else:
        ok = [k for k, v in report["per_label"].items() if v is not None]
        if ok:
            print("  所有可计算标签均 >= 0.67 ✓")
        else:
            print("  （暂无可计算的标签级 alpha：需 ≥2 名标注员重叠标注同一批单元）")

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nreport written to {args.out}")


if __name__ == "__main__":
    main()
