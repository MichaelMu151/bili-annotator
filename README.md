# BiliAnnotator

面向 **B 站评论区话语分析** 的在线标注平台：把"**一条评论 + 它的上下文树**"作为标注单元，
支持四维度（修辞 / 语用 / 议题漂移 / 情感毒性）标注、进度追踪、标注者一致性与可回推总体的抽样溯源。

> 配套大库（`corpus.db` 1.6GB / `analysis_805.db` 1.0GB）**不进仓**；本仓只含已被抽样出来、
> 体积很小的 `data/annotation.db`（默认 500 条单元）。这样任何人克隆后即可直接标注。

---

## 快速开始

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5005 （只绑定本机）
```

如需让同事通过局域网访问：

```bash
ANNOTATOR_HOST=0.0.0.0 python app.py
```

> 出于安全，`debug` 仅在绑定 `127.0.0.1/localhost` 且显式 `ANNOTATOR_DEBUG=1` 时开启，
> 公网接口**永不**启动 Werkzeug 调试器（否则有 RCE 风险）。

三个界面：

| 路径 | 用途 |
|---|---|
| `/` | 标注界面：上下文卡 + 四维度表单（草稿自动暂存、`⌘/Ctrl+Enter` 保存） |
| `/disagree` | 分歧裁决：集中展示多人判断不一致的单元，逐维度裁决（写入 `adjudications`），用于产出决策卡 |
| `/codebook` | 标注手册（直接渲染 `codebook/CODEBOOK.md`，随时切换查看标准） |
| `/progress` | 进度 / 质检 / 导出：总体覆盖、维度填写率、标注员对比、抽样溯源、单元列表 |

> **原始大库**（`analysis_805.db` 1.1GB / `corpus.db` 1.7GB）放在
> [Release: data-805-v1](https://github.com/MichaelMu151/bili-annotator/releases/tag/data-805-v1)。
> 标注本身只依赖仓库内 `data/annotation.db`(1.2MB)，无需下载大库；只有重建抽样时才需要。

---

## 设计要点（为什么这样做）

### 1. 标注单元 = 评论 + 上下文卡
每张卡固定五个区块，对应三层上下文假设：

| 区块 | 内容 |
|---|---|
| `[VIDEO]` | 标题 / UP / 标签 / **可编辑摘要** + BV 链接（建议亲自核对视频） |
| `[ANCHOR]` | 分支 root 原文 + **人工 anchor claim（≤30字）**；维度 3 的漂移基准 |
| `[PATH]` | 祖先链 `root → … → parent` **完整逐字**（长链默认折叠；micro 上下文） |
| `[CORE]` | **本层** = 楼层根评论 + **前面 3 个评论**（`C_CORE_PRIOR_N=3`；meso 上下文） |
| `[TARGET]` | 待标评论 |

**上下文完整性（维度感知，`context_status`）**：断链评论**不丢弃、可回收**，按可判性分级：
- `complete`：祖先链完整到楼层根。
- `ancestor_gap`：**直接父评论仍在**、仅更高层祖先缺失 → 局部对话完整，**四维全可标**。
- `parent_gap`：**直接父评论缺失**（被删/未采集）→ D1/D2/D4 仍可从 target 文本判定；**D3 议题漂移不可靠**（无被回复对象），`d3_assessable=0`，卡面显示「无法判定漂移（父评论缺失）」选项，标注后但仍可从 D3 分析中排除。
- 卡面标记 `context_degraded`、`anchor_degraded`、`degraded_reason`，并显示缺失父评论 rpid 与其下存活回复数（`target_meta.missing_parent_rpid / orphan_replies`）；锚点回退楼层根。
- 实测约占候选 5.2%，且**更长、更偏实质辩论**——丢弃会引入选择偏差，故保留（详见 `scripts/analyze_broken_chain.py`）。`scripts/verify_cards.py` 逐条回查 corpus 校验。
**不呈现 `C_extra`（溢读带）**：避免标注员的 hindsight bias；该字段留给模型/分析使用。

### 2. 量表（详见 codebook）
- **D1 / D2**：多标签 `0/1` + `primary` + `is_mixed`（两轴正交，可同时命中）
- **D3**：`type` 单选（4 类）+ `extent` 序数 1–4（type 与 extent 分开测量）
- **D4**：极性 `-1/0/+1`、唤醒度 `low/high`、毒性 `0–3`（三者解耦）
- 全局：`ambiguous`（合法结果）、`confidence`、`notes`

### 3. 抽样可复现 & 可回推总体（学术必需）
- 分层键：`topic × floor_heat × depth × polarity × flagtype × length`
- 配额 `∝ sqrt(stratum_size)`，覆盖长尾而不被大层淹没
- **seed 持久化**在 `meta.sampling.seed`；候选按 `rpid`、楼层按 `(pub_ts, rpid)` 排序，**重建结果逐条一致**
- `inclusion_prob` 在 trim/top-up **之后**由**实际入选数**重算为 `n_h / N_h`（分层 SRSWOR 一阶入样概率），
  因此 `Horvitz–Thompson` 加权**无偏**——这点在初版里是错的（用了 trim 前的配额），已修正并用单测守护
- 每条单元存 `stratum_key / stratum_n / inclusion_prob / sampled_by` → 下游可用**入样概率加权**回推总体

### 4. 数据完整性与校验
- **服务端全字段校验**：D1/D2 标签必须属于 canonical 集合且 `primary ∈ labels`；D3.type/extent、
  D4 极性/唤醒/毒性、confidence 全部按 codebook 枚举校验；**空标注**（未填任何维度且未标 ambiguous）被拒绝
- `annotations.target_rpid`：标注时快照目标评论；导出含 `integrity_ok`，**重建抽样也不会把标注错接到别的评论**
- 竞态安全：保存用 `INSERT … ON CONFLICT(unit_id,annotator) DO UPDATE` 原子 upsert
- `annotations.schema_version` / `duration_ms`：可做标注质量与效率分析
- WAL + `busy_timeout`：多人同时标注安全；重建会**拒绝覆盖已有标注**（除非 `--force`）

### 5. 一致性（IAA）
```bash
python scripts/compute_iaa.py --out iaa.json
```
输出每标签 α、D3.type κ、D4 序数 α 与混淆矩阵。
> 实现已与 PyPI `krippendorff` 库**逐位对齐**（nominal/ordinal 误差 ~1e-16），见 `tests/test_core.py`。

---

## 目录

```
app.py                        Flask 应用（页面 + REST API）
app/templates/                annotate / codebook / progress
app/static/style.css          样式
codebook/CODEBOOK.md          标注手册（可在 UI 内查看）
data/annotation.db            500 条标注单元（小，可分享）
scripts/materialize_units.py  从大库抽样并生成单元（唯一接触大库的脚本）
scripts/verify_cards.py       逐条回查 corpus 校验上下文卡（parent/root/video/anchor/path/core）
scripts/analyze_broken_chain.py  诊断断链评论（内容画像 + 视频/主题分布）
scripts/compute_iaa.py        Krippendorff α / Cohen κ
tests/test_core.py            统计核心回归测试
```

---

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 抽样溯源（seed、分层规格、schema 版本） |
| GET | `/api/next?annotator=` | 取下一条未标单元（含 `position`） |
| GET | `/api/unit/<id>?annotator=` | 取指定单元 |
| GET | `/api/units?annotator=` | 单元列表（进度页用） |
| POST | `/api/annotate` | 保存标注（校验 `primary ∈ labels`） |
| POST | `/api/unit/<id>/card` | 保存视频摘要 / anchor claim |
| GET | `/api/stats` | 覆盖、标注员、维度填写率 |
| GET | `/api/disagreements[?open=1]` | 多人判断不一致的单元（含各人取值与裁决状态） |
| POST | `/api/adjudicate` | 保存某单元某维度的裁决（写入 `adjudications`） |
| GET | `/api/export[?annotator=]` | 分析用 CSV（含 `inclusion_prob`、`integrity_ok`） |

---

## 重建数据（需要大库）

```bash
python scripts/materialize_units.py --target 500 --seed 20261026
```

默认读取上级工作区的 `data/analysis_805.db` 与 `data/corpus.db`（可用 `--a805/--corpus` 覆盖）。

**扩充规模**：`--target 2000` 或 `--target 3000` 即可（实测 3000 条约 40 秒，抽样确定性不变）。

**校验上下文卡**（强烈建议每次重建后运行）：

```bash
python scripts/verify_cards.py            # 逐条回查 corpus，失败则退出码非 0
```

---

## 当前阶段与后续

- 当前 = **pilot（500 条）**。目标是先跑通流程、暴露歧义。
- **决策卡（decision cards）尚未固化**：需要 pilot 的双人分歧与"近似但不算"的负例，才能写出真正的裁决卡。见 `codebook/CODEBOOK.md` §7。
- 后续：pilot → 修订 codebook → 扩大 gold 规模 → 校准 LLM 标注（把 LLM 当 surrogate，用 gold 校准）。
