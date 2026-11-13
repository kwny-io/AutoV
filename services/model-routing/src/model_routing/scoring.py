"""§4 软评分。**只对 `eligible[]` 评分**，这是 §1.3 的第二半。

分数是排序信号，不是准入依据。三件事必须分开存，混起来就没人能复算：
`score`（加权和）、`score_parts`（归一化后的七分量）、`scoring`（本次决策的
归一化区间、权重、样本水位）。T25-21 要求"用 `scoring` + `score_parts`
重算 `score` 一致"——只存 `score` 的那种实现复算不出来。

四个数字容易写错：

- **单候选时所有分量记 0.5**（§4.3）。不是 1.0：唯一候选不该"自动满分"，
  否则一个只找到一条烂路由的镜头会显示成完美选择。
- **`C` 与 `U` 取反**（§4.1 行 162）。成本越低越好、不确定性越低越好，
  但归一化后高=好，所以要 `1 - normalized`。
- **归一化在单个决策内做**（§4.3）。跨决策比较分数没有意义——两个镜头
  各自的"最好"不可比。
- **`I` 无身份需求时权重归零并重新归一化**（§4.1 行 155）。不是记 0 分：
  记 0 分会把一个"不需要身份保持"的镜头判得比需要的差。
"""

from __future__ import annotations

import math

# §4.1 默认权重。方案 §13 第一条明说这是起点不是结论，要随策略快照记录。
DEFAULT_WEIGHTS = {"Q": 0.30, "I": 0.22, "M": 0.12, "R": 0.16,
                   "C": 0.12, "L": 0.05, "U": 0.03}

# §4.4：`α = 20`；`U = 1 - min(n/100, 1)`。
SMOOTHING_ALPHA = 20
UNCERTAINTY_SAMPLE_FULL = 100

# §4.2：`p_first_pass` 冷启动初值 0.7（方案 §4.2 行 180）。
P_FIRST_PASS_PRIOR = 0.7
# `docs/11 §3`：每镜头最多 3 个自动业务 attempt。
ATTEMPTS_BUDGET = 3

# §3.1：degraded 候选放行后的可靠性惩罚。
DEGRADED_RELIABILITY_PENALTY = 0.15


def smoothed_rate(passes, n, prior, alpha: int = SMOOTHING_ALPHA) -> float:
    """§4.4：`(passes + α·prior) / (n + α)`。n=0 时正好等于 prior。"""
    passes = int(passes or 0)
    n = int(n or 0)
    return (passes + alpha * float(prior)) / (n + alpha)


def uncertainty(n) -> float:
    """§4.4：`1 - min(n/100, 1)`。样本越多越确定。"""
    return 1.0 - min(int(n or 0) / UNCERTAINTY_SAMPLE_FULL, 1.0)


def expected_cost(candidate: dict, *, modality: str, breakdown_costs: dict,
                  p_first_pass: float) -> tuple[int, dict]:
    """§4.2 的五段成本。返回 `(总额, 明细)`。

    `generation` 用 S24 的**上界**，不是均值——报价留安全余量。`lipsync` 是
    "低价陷阱"的量化处：模型便宜 60% 但原生不出声，口型费能吃掉全部差额
    （T25-09）。所以 `audio_strategy` 不是 `native*` 时口型费必须进成本，
    哪怕是 0 元也必须有一行。
    """
    generation = int(candidate.get("quote_upper_bound_micro") or 0)
    strategy = (candidate.get("audio_strategy")
                or breakdown_costs.get("audio_strategy") or "silent")
    native_sound = strategy in ("native", "native_plus_tts")
    lipsync = 0 if native_sound else int(breakdown_costs.get("lipsync_micro") or 0)
    qc = int(breakdown_costs.get("qc_micro") or 0)
    ingest = int(breakdown_costs.get("ingest_micro") or 0)
    retry = int(round((1.0 - float(p_first_pass)) * generation * ATTEMPTS_BUDGET))
    parts = {"generation": generation, "lipsync": lipsync, "qc": qc,
             "expected_retry": retry, "ingest": ingest}
    return sum(parts.values()), parts


def _minmax(values: list[float]) -> tuple[list[float], dict]:
    """§4.3：单个决策内 min-max。单值或全等时全记 0.5。

    "全等时全记 0.5" 是对方案的一处补全：`max == min` 时 0/0 无定义，
    让所有候选都拿 0.5 才不改变它们的相对顺序（它们本来就并列）。
    """
    if not values:
        return [], {"min": None, "max": None}
    lo, hi = min(values), max(values)
    if len(values) == 1 or hi == lo:
        return [0.5 for _ in values], {"min": lo, "max": hi, "degenerate": True}
    return ([(v - lo) / (hi - lo) for v in values],
            {"min": lo, "max": hi, "degenerate": False})


def score_all(entries: list[dict], *, weights: dict | None = None,
              identity_required: bool = False) -> tuple[list[dict], dict]:
    """对 eligibles 打分并排名。`entries` 每项需带原始分量。

    原始分量键：`quality_pass`/`quality_n`、`identity_pass`/`identity_n`、
    `motion_defect`/`motion_n`、`reliability`、`expected_cost_micro`、
    `latency_p90_s`、`sample_size`、`zero_weight`（可选的受限分量集合）。

    返回 `(带 score/score_parts/rank 的列表, scoring 汇总)`。
    scoring 里放：权重、每个分量的归一化区间、样本水位。**这是 T25-21
    能复算的全部依据**，所以区间必须连退化情况一起记。
    """
    effective = dict(weights or DEFAULT_WEIGHTS)
    if not identity_required:
        # §4.1：无身份需求时 `I` 权重归零并重新归一化。
        effective["I"] = 0.0
    total = sum(effective.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    effective = {k: v / total for k, v in effective.items()}

    quality = [smoothed_rate(e.get("quality_pass"), e.get("quality_n"),
                             e.get("quality_prior", 0.7)) for e in entries]
    identity = [smoothed_rate(e.get("identity_pass"), e.get("identity_n"),
                              e.get("identity_prior", 0.7)) for e in entries]
    # 缺陷率是"越低越好"，先取反再归一化。
    motion = [1.0 - smoothed_rate(e.get("motion_defect"), e.get("motion_n"),
                                  e.get("motion_prior", 0.1)) for e in entries]
    reliability = [float(e.get("reliability", 0.5)) for e in entries]
    cost = [float(e["expected_cost_micro"]) for e in entries]
    # 延迟越低越好，同样取反。
    latency = [-float(e.get("latency_p90_s") or 0.0) for e in entries]
    unc = [uncertainty(e.get("sample_size")) for e in entries]

    n_q, r_q = _minmax(quality)
    n_i, r_i = _minmax(identity)
    n_m, r_m = _minmax(motion)
    n_r, r_r = _minmax(reliability)
    n_c, r_c = _minmax(cost)
    n_l, r_l = _minmax(latency)
    n_u, r_u = _minmax(unc)

    out = []
    for idx, entry in enumerate(entries):
        parts = {
            "Q": n_q[idx], "I": n_i[idx], "M": n_m[idx], "R": n_r[idx],
            # 成本与不确定性：归一化后"高"表示数值大，所以取反。
            "C": 1.0 - n_c[idx], "L": n_l[idx], "U": 1.0 - n_u[idx],
        }
        if entry.get("degraded"):
            # §3.1：R 记 0 并给固定惩罚。R 记 0 而不是"扣 0.15"，
            # 是为了让复算的人不用知道"degraded"这个额外状态。
            parts["R"] = 0.0
        score = sum(effective[k] * parts[k] for k in parts)
        if entry.get("degraded"):
            score -= DEGRADED_RELIABILITY_PENALTY
        out.append({**entry,
                    "score": round(score, 6),
                    "score_parts": {k: round(v, 6) for k, v in parts.items()}})

    # 排名：分数降序，同分按 candidate_key 升序（稳定，可复算）。
    out.sort(key=lambda e: (-e["score"], e["candidate_key"]))
    for rank, entry in enumerate(out, start=1):
        entry["rank"] = rank

    scoring = {
        "weights": effective,
        "weights_declared": dict(weights or DEFAULT_WEIGHTS),
        "identity_weighted": bool(identity_required),
        "ranges": {"Q": r_q, "I": r_i, "M": r_m, "R": r_r, "C": r_c,
                   "L": r_l, "U": r_u},
        "sample_watermark": max((e.get("watermark") or "" for e in entries),
                                default=None) or None,
        "alpha": SMOOTHING_ALPHA,
        "candidate_count": len(entries),
    }
    return out, scoring
