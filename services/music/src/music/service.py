"""S34 内核。方案：`plans/S34-music.md`。

核心原则一句话：**换着小节线收尾，不拉伸；拉伸退化成重生成，而重生成是最后手段。**

把它拆成两层的后果说清楚，因为这是全文件最容易被写坏的地方：

- **内容层**（这首曲子是什么）花钱，走 S26 × N，结果是 `MusicTake`，
  `content_fingerprint` 冻结它。剪辑把 cue 的需求从 42 秒改成 39 秒，内容层
  一个字都不用变。
- **位置层**（从第几小节进、第几小节收、中间要不要拼接）是 `cut_plan()` 这个
  纯函数，免费、可任意重算、可以缓存也可以丢掉缓存。把 CutPlan 写进权威表再
  当事实读出来，就等于让 S34 成为剪辑相关事实的第二权威。

所以 `content_fingerprint` **不含时长、不含 `candidate_count`**（§2.2 / §8.1）。
时长一旦进指纹，剪辑动一下就会产出新指纹，整集 BGM 重生成——正是本服务要防的
那笔钱。candidate_count 是"要几个版本"的偏好，不是内容需求（§7.1）。

六条不能违反的语义：

1. `cut_plan_cache` 可 TRUNCATE，任何对外读取都现算（§4.3 / T34-39）。
2. **不拉伸。** 五种策略全部只动切点；`bar_extend` 只延长尾音，不改中间时长。
   放不下就是放不下，由上层记录缺口或（最后手段）重生成。
3. **一个逻辑 job 的 N 个候选全部或全无**（§4.7 / F10）。中途失败不能交付半个
   候选集；预算必须在提交前按 N 份检查。
4. **许可四元组任一 `unknown` 即不可用**（§4.4.2）。`unknown` 不是"可能可用"，
   法律上无法证明有许可就是没有许可。
5. **自动选定只在 `candidate_count == 1` 时**（§4.6），且必须写
   `selected_by='system:auto_single'`；超时 24h 只记缺口，不替导演挑。
6. **S34 不做 ducking**（§4.5）。对白活跃度提示只透传，混音是 S42 的唯一权威。

事务与幂等与 S33 同构：平台幂等 SDK 的 `begin/finish` 是裸语句，所以命令事务
内把结果写回同一个 `idempotency_record` 行，提交与"记下已提交"原子成立。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    BAR_TOLERANCE_MS,
    BEAT_CONFIDENCE_THRESHOLD,
    CANDIDATE_STATES,
    CONTRACT_GAPS,
    EXTENDABLE_TAIL_KINDS,
    FADE_OUT_MS,
    GAP_KINDS,
    ID_PREFIX,
    INSTRUMENTATIONS,
    JOB_STATES,
    MAX_CANDIDATES,
    MAX_DURATION_SECONDS,
    MOODS,
    RETRYABLE_STATES,
    SCHEMA,
    SELECTION_TIMEOUT_SECONDS,
    SET_KILLING_FAILURES,
    TAIL_KINDS,
    TERMINAL_STATES,
)

SOURCE = "S34"
SCHEMA_VERSION = "2.0"

# 已登记事件（`contracts/event-topics.v2.json`）。
EVENT_MUSIC_READY = "production.music.ready"
TOPIC_MUSIC = "drama.music.v2"

# 未登记事件（方案 §6.4 自己提出的五个，缺口见 CONTRACT_GAPS MUS-6/MUS-11）。
LOCAL_JOB_STATE = "music.job.state-changed"
LOCAL_CUE_SELECTED = "music.cue.selected"
LOCAL_GAP_RECORDED = "music.gap.recorded"
LOCAL_GAP_CLOSED = "music.gap.closed"
LOCAL_RIGHTS_LAPSED = "music.take.rights-lapsed"

# §4.3 第 2 行没有给短语容差。乐句边界通常每 4 或 8 小节一条，提前收一小节
# 仍在同一乐句内（和声进行不被打断），所以这里按"最多早一小节"实现。
# 这是本服务的解释，不是方案给定的数字；换检测器时要连同 BAR_TOLERANCE_MS 一起看。
PHRASE_MAX_EARLY_BARS = 1

# 拼接段的硬上限。loop_stitch 是"长的唯一不花钱手段"，但一个 2 秒循环拼 200 段
# 已经不是音乐了——超过上限按放不下处理，交给缺口/重生成。
MAX_LOOP_SEGMENTS = 64

# §4.4.2：这些许可原因让 take 不可用于正式交付。`sync_territory_limited`
# 不在此列——"只在这些地区/媒体内"是发行范围问题，由 S43 判断，S34 只如实
# 记录，不替发行方拒绝。
BLOCKING_RIGHTS_REASONS = frozenset({
    "composition_unknown", "master_unknown", "performance_unknown",
    "sync_unknown", "sync_expired", "sync_expiry_unparseable",
})

# `execution-context.v2` 的 18 个必填键，直接从 S26 的 `CONTEXT_REQUIRED` 抄来
# （`services/generation-jobs/src/generation_jobs/service.py`）。抄常量而不是
# import，是因为 AGENTS.md 禁止跨服务 import：这份拷贝是契约的**读法**，变了
# 要跟着改，而不是"碰巧一样"。
EXECUTION_CONTEXT_REQUIRED = (
    "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
    "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
    "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref", "actor",
    "command_id", "request_hash", "trace_id",
)


def _route_ref(route: dict | None) -> dict | None:
    """S25 的响应把 id 叫 `route_id`，方案 §6.1 叫 `route_decision_id`。

    两个名字都认。都拿不到就返回 None——**不拼一个假的 id**：S26 会校验
    `route_decision_ref.content_hash`，编出来的 ref 只会在更晚、更贵的地方
    失败。
    """
    if not isinstance(route, dict):
        return None
    decision_id = route.get("route_decision_id") or route.get("route_id")
    if not decision_id:
        return None
    ref = {"service_id": "S25", "id": decision_id,
           "revision": int(route.get("revision") or 1)}
    content_hash = route.get("content_hash")
    if content_hash:
        ref["content_hash"] = content_hash
    return ref


def _quote_upper_bound(route: dict | None) -> int | None:
    """报价上界。S25 放在 `eligible[0]`（本服务经 route_resolver 归一到
    `selected`）或顶层，两处都认。

    **拿不到就返回 None，不返回 0**：0 会让"3 × 0 ≤ 预算"永远成立，
    F6 的检查就变成一句空话。
    """
    if not isinstance(route, dict):
        return None
    for holder in (route.get("selected") or {}, route):
        for key in ("quote_upper_bound_micro", "upper_bound_micro"):
            value = holder.get(key)
            if isinstance(value, int) and value >= 0:
                return value
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dumps(value) -> str:
    return _jcs(value if value is not None else {})


def _parse_ts(value: str) -> datetime:
    """接受 `Z` 与 `+08:00` 两种写法（同 S26）。

    字符串直接比较会在带偏移的输入上错位：`2026-11-12T20:00:00+08:00` 的
    字符串序比同刻的 `...Z` 大，于是"还没到"会被读成"已经过了"。
    """
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：指纹

def content_fingerprint(*, music_intent: str, mood: str, mood_intensity: float,
                        instrumentation: str, tempo_low_bpm: int | None,
                        tempo_high_bpm: int | None, key_hint: str | None,
                        instrumental_only: bool) -> str:
    """§2.2。时长**不在**里面——这是整个服务省钱的那一刀。

    用 `\\x1f` 分隔而不是直接拼接：直接拼会让 `("ab","c")` 和 `("a","bc")`
    撞成同一个指纹。方案写的是 `||`，但那是散文里的并置符号，不是 SQL 的
    字符串连接语义；这里按"字段可解析"实现。

    强度按 3 位小数入指纹：0.7000001 与 0.7 是同一个创作意图，浮点噪声不该
    产出新指纹。音量为 None 的提示写空串，与"提示有值但为空"区分开——后者
    由 `_validate` 拒绝。
    """
    return _sha256_hex("\x1f".join([
        music_intent,
        mood,
        f"{float(mood_intensity):.3f}",
        instrumentation,
        "" if tempo_low_bpm is None else str(int(tempo_low_bpm)),
        "" if tempo_high_bpm is None else str(int(tempo_high_bpm)),
        key_hint or "",
        "1" if instrumental_only else "0",
    ]))


# ---------------------------------------------------------- 纯函数：CutPlan

def _segment(output_start_ms: int, output_end_ms: int, source_in_ms: int,
             source_out_ms: int) -> dict:
    return {
        "output_start_ms": int(output_start_ms),
        "output_end_ms": int(output_end_ms),
        "source_in_ms": int(source_in_ms),
        "source_out_ms": int(source_out_ms),
    }


def _bar_ms(bpm: float, meter_num: int) -> float:
    return int(meter_num) * 60000.0 / float(bpm)


def cut_plan(*, natural_duration_ms: int, requested_duration_ms: int,
             bpm: float | None, meter_num: int | None, meter_den: int | None,
             downbeats_ms: list | None, phrases_ms: list | None,
             beat_confidence: float | None, tail_kind: str,
             loop_ranges: list | None) -> dict:
    """§4.3 判定表。**纯函数**：不看库、不看时间、不产生副作用。

    `end_strategy` 的取值对应 `cut_plan_cache` 的 CHECK：
    `hard` / `bar` / `phrase` / `loop` / `bar_extend` / `unplaceable`。

    行序即方案的行序。四个容易写错的地方：

    - **低置信度禁用 `bar_trim`（§2.4）。** 小节线不可信时按小节裁会切在半拍上；
      此时只允许 `phrase_trim` 或 `loop_stitch`。这条检查必须在"找到最近小节线"
      之前，否则低置信网格里的假小节线会先被选中。
    - **`hard` 是"没什么可决定的"。** 差在 ±80ms 以内时不做任何音乐决策，直接在
      需求长度处切。它不是第 5 条 `unplaceable`，也不是 `bar`——把它算成 `bar`
      会让"切点落在小节线上"这件事变成凭空断言。
    - **`loop_stitch` 的 confidence = min(seam 质量分, beat 置信度)。**
      §2.5 说 seam_score"越小越好"，§4.3 又当质量分用（MUS-15）。这里按 §4.3：
      入参 `ranges[].seam_score` 是 0..1 的**质量分**（越大越好）；检测器若输出
      距离度量，须在 probe 里先转换。两个上界取 min，因为拼接既依赖接缝也依赖
      拍点——任一不可信，结果就不可信。
    - **`bar_extend` 有前提。** 只有 `tail_kind ∈ {sustaining, fade_out}` 允许；
      干脆的终止拍上延长会听起来像卡住（§4.3 行 452–454）。

    `segments` 覆盖区间不超过 `requested_duration_ms`；`tail_gap_ms` 如实报告
    收尾提前留出的尾巴（例如 39 秒需求在第 14 小节末 37.3 秒收，尾巴 1.7 秒
    由 S42 按 `fade_out_ms` 处理）。
    """
    if natural_duration_ms <= 0 or requested_duration_ms <= 0:
        raise ServiceError(422, "DURATION_OUT_OF_RANGE",
                           "durations must be positive")

    confidence = None if beat_confidence is None else float(beat_confidence)
    tail = tail_kind if tail_kind in TAIL_KINDS else "unknown"
    downbeats = sorted(int(x) for x in (downbeats_ms or []) if 0 < int(x))
    phrases = sorted(int(x) for x in (phrases_ms or []) if 0 < int(x))
    loops = [r for r in (loop_ranges or [])
             if isinstance(r, dict)
             and int(r.get("out_ms") or 0) > int(r.get("in_ms") or 0)]

    usable_grid = (bpm is not None and float(bpm) > 0
                   and meter_num is not None and int(meter_num) > 0)
    bar = _bar_ms(float(bpm), int(meter_num)) if usable_grid else None

    def _result(strategy: str, segments: list, *, transition=None,
                fade_in_ms=0, fade_out_ms=0, confidence_value=None,
                reason=None, extra=None) -> dict:
        covered = segments[-1]["output_end_ms"] if segments else 0
        body = {
            "end_strategy": strategy,
            "segments": segments,
            "transition": transition,
            "fade_in_ms": fade_in_ms,
            "fade_out_ms": fade_out_ms,
            "confidence": confidence_value,
            "reason": reason,
            "covered_ms": covered,
            "tail_gap_ms": max(0, requested_duration_ms - covered),
            "requested_duration_ms": requested_duration_ms,
            "natural_duration_ms": natural_duration_ms,
        }
        body.update(extra or {})
        return body

    # 0. 差在容差以内：不决定任何事，照需求长度切。
    if abs(natural_duration_ms - requested_duration_ms) <= BAR_TOLERANCE_MS:
        return _result(
            "hard",
            [_segment(0, requested_duration_ms, 0,
                      min(natural_duration_ms, requested_duration_ms))],
            transition="hard_cut",
            fade_out_ms=0 if natural_duration_ms >= requested_duration_ms
            else FADE_OUT_MS,
            confidence_value=confidence,
            reason=None)

    # 1. bar_trim：需求不长于素材，且落点能对齐某条实测小节线（±80ms）。
    if (natural_duration_ms > requested_duration_ms and usable_grid
            and confidence is not None
            and confidence >= BEAT_CONFIDENCE_THRESHOLD and downbeats):
        best = min(downbeats, key=lambda d: abs(d - requested_duration_ms))
        if abs(best - requested_duration_ms) <= BAR_TOLERANCE_MS:
            return _result(
                "bar",
                [_segment(0, best, 0, best)],
                transition="none",
                fade_out_ms=FADE_OUT_MS if best < requested_duration_ms else 0,
                confidence_value=confidence,
                reason=None)

    # 2. phrase_trim：落点能对齐乐句线（4/8 小节），最多早一小节。
    #    低置信度也允许——这正是"小节线不可信时的退路"（§2.4）。
    if natural_duration_ms > requested_duration_ms and phrases:
        early_limit = (bar if bar is not None else 0) * PHRASE_MAX_EARLY_BARS
        candidates = [p for p in phrases
                      if p <= requested_duration_ms
                      and requested_duration_ms - p <= early_limit]
        if candidates:
            best = max(candidates)
            return _result(
                "phrase",
                [_segment(0, best, 0, best)],
                transition="none",
                fade_out_ms=FADE_OUT_MS if best < requested_duration_ms else 0,
                confidence_value=confidence,
                reason=None)

    # 3. loop_stitch：用最好的可循环区间把素材拼到需求长度。
    #    只在"需要变长"时有意义；变短是裁切的事，裁不了就是放不下。
    if requested_duration_ms > natural_duration_ms and loops:
        best_range = max(
            loops, key=lambda r: float(r.get("seam_score") or 0.0))
        in_ms = int(best_range["in_ms"])
        out_ms = int(best_range["out_ms"])
        try:
            seam = float(best_range.get("seam_score") or 0.0)
        except (TypeError, ValueError):
            seam = 0.0
        seam = min(max(seam, 0.0), 1.0)
        crossfade = max(0, int(best_range.get("crossfade_ms") or 0))
        loop_len = out_ms - in_ms
        if out_ms <= natural_duration_ms and loop_len > 0:
            segments = [_segment(0, out_ms, 0, out_ms)]
            covered = out_ms
            while covered < requested_duration_ms \
                    and len(segments) < MAX_LOOP_SEGMENTS:
                take = min(loop_len, requested_duration_ms - covered)
                segments.append(_segment(covered, covered + take,
                                         in_ms, in_ms + take))
                covered += take
            if covered >= requested_duration_ms:
                return _result(
                    "loop", segments,
                    transition="loop_stitch",
                    fade_out_ms=FADE_OUT_MS,
                    confidence_value=min(
                        seam, confidence if confidence is not None else seam),
                    reason=None,
                    extra={"loop_crossfade_ms": crossfade,
                           "loop_in_ms": in_ms, "loop_out_ms": out_ms})

    # 4. bar_extend：只差不到一小节，且尾音"可延续"。
    if requested_duration_ms > natural_duration_ms and bar is not None:
        deficit = requested_duration_ms - natural_duration_ms
        if deficit < bar and tail in EXTENDABLE_TAIL_KINDS:
            return _result(
                "bar_extend",
                [_segment(0, requested_duration_ms, 0, natural_duration_ms)],
                transition="none",
                fade_out_ms=FADE_OUT_MS,
                confidence_value=confidence,
                reason=None,
                extra={"extend_ms": deficit})

    # 5. 放不下。**这一条不自动触发重生成**——上层记录缺口给人看，
    #    `resynthesize_for_duration` 是最后手段（§4.2 / §8.2）。
    if requested_duration_ms > natural_duration_ms:
        deficit = requested_duration_ms - natural_duration_ms
        if bar is not None and deficit < bar and tail not in EXTENDABLE_TAIL_KINDS:
            reason = "tail_not_extendable"
        elif loops:
            reason = "loop_stitch_insufficient"
        else:
            reason = "duration_mismatch"
    else:
        reason = "no_aligned_cut_point"
    return _result("unplaceable", [], transition=None, fade_in_ms=0,
                   fade_out_ms=0, confidence_value=confidence, reason=reason)


# ---------------------------------------------------------------- 服务

def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _binding_unusable_reasons(resolved: dict) -> list:
    """§4.4.2：四个维度里任一 `unknown` 都让整份 binding 不可用。

    单独抽出来是因为它在**解析时**和**复查时**两处要用，且必须是同一份判断；
    两处各写一遍迟早会分叉，于是"提交时拒绝了、复查时放行了"。
    """
    reasons = []
    for dim in ("composition", "master", "performance", "sync"):
        if resolved.get(f"{dim}_status") == "unknown":
            reasons.append(f"{dim}_unknown")
    if resolved.get("sync_status") == "expired":
        reasons.append("sync_expired")
    elif resolved.get("sync_status") == "restricted":
        # `restricted` 不是过期也不是可用，它说的是"只在这些地区/媒体内"。
        # 地区覆盖是发行决策（S43），所以这里只把它记下来，不判死。
        reasons.append("sync_territory_limited")
    return reasons


class MusicService:
    def __init__(self, conn: sqlite3.Connection, *, rights_resolver,
                 route_resolver, submitter, artifact_check, analysis_probe,
                 budget_check, canceler, edl_provider=None, scene_check=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 内部会 executescript，而 sqlite3 的 executescript 会
        # **隐式提交当前事务**。所以只能在任何事务之外建一次，之后复用同一个实例；
        # 否则命令事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 就失效了。
        self.outbox = Outbox(self.conn)
        self.rights_resolver = rights_resolver
        self.route_resolver = route_resolver
        self.submitter = submitter
        self.artifact_check = artifact_check
        self.analysis_probe = analysis_probe
        self.budget_check = budget_check
        self.canceler = canceler
        self.edl_provider = edl_provider
        self.scene_check = scene_check

    # -------------------------------------------------------- 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        """一个命令一个事务；结果**在同一个事务里**写回幂等记录。

        平台幂等 SDK 的 `begin/finish` 是裸语句，不在我们的 `BEGIN IMMEDIATE`
        里。若把 `finish` 留到事务外，会存在"业务已提交、幂等未记录"的窗口，
        重试就会再花一次钱。这里直接把 `result` 覆盖进同一行——提交与"记下
        已经提交"原子成立。
        """
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _transition(self, job: dict, to_state: str, *, reason_code=None,
                    detail=None, outbox: Outbox | None = None) -> dict:
        if to_state not in JOB_STATES:
            raise AssertionError(f"unknown music job state {to_state}")
        seq = int(job["state_seq"]) + 1
        now = _now()
        self.conn.execute(
            "UPDATE music_job SET state=?, state_seq=?, updated_at=?, "
            "state_entered_at=?, failure_code=COALESCE(?, failure_code), "
            "failure_detail=COALESCE(?, failure_detail) WHERE music_job_id=?",
            (to_state, seq, now, now, reason_code, detail,
             job["music_job_id"]))
        self.conn.execute(
            "INSERT INTO job_transition (music_job_id, seq, from_state, to_state, "
            "reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job["music_job_id"], seq, job["state"], to_state, reason_code,
             detail, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_JOB_STATE,
                subject=f"music-job/{job['music_job_id']}",
                tenant_id=job["tenant_id"], home_cell="music",
                aggregate_id=job["music_job_id"], sequence=seq,
                correlation_id=job["music_job_id"],
                data={"music_job_id": job["music_job_id"],
                      "scene_lineage_id": job["scene_lineage_id"],
                      "cue_index": int(job["cue_index"]),
                      "cue_id": job["cue_id"], "from": job["state"],
                      "to": to_state, "reason_code": reason_code},
                schema_version=SCHEMA_VERSION))
        return {**job, "state": to_state, "state_seq": seq}

    def _job(self, tenant_id: str, job_id: str) -> dict:
        job = _row(self.conn.execute(
            "SELECT * FROM music_job WHERE tenant_id=? AND music_job_id=?",
            (tenant_id, job_id)).fetchone())
        if job is None:
            raise ServiceError(404, "MUSIC_JOB_NOT_FOUND", f"no job {job_id}")
        return job

    def _record_gap(self, *, tenant_id: str, scene_lineage_id: str,
                    cue_index: int, cue_id: str, kind: str, detail: str,
                    source_job_id: str | None,
                    outbox: Outbox | None) -> dict:
        """**必须与产生它的状态转移同事务调用**（§3.4）。调用方负责事务边界。"""
        if kind not in GAP_KINDS:
            raise AssertionError(f"unknown music gap kind {kind}")
        gap_id = _new_id("gap")
        now = _now()
        self.conn.execute(
            "INSERT INTO music_gap (gap_id, tenant_id, scene_lineage_id, "
            "cue_index, cue_id, kind, detail, source_job_id, state, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,'open',?,?)",
            (gap_id, tenant_id, scene_lineage_id, int(cue_index), cue_id, kind,
             detail, source_job_id, now, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_GAP_RECORDED,
                subject=f"music-gap/{gap_id}", tenant_id=tenant_id,
                home_cell="music", aggregate_id=scene_lineage_id,
                # S34 不持有 scene_lineage 的序列知识：缺口顺序由 `created_at`
                # 与 `gap_id` 给出。编一个猜出来的计数比填 1 更糟。
                sequence=1, correlation_id=gap_id,
                data={"gap_id": gap_id, "scene_lineage_id": scene_lineage_id,
                      "cue_index": int(cue_index), "cue_id": cue_id,
                      "kind": kind, "source_job_id": source_job_id},
                schema_version=SCHEMA_VERSION))
        return {"gap_id": gap_id, "kind": kind, "state": "open"}

    # -------------------------------------------------------- 校验（§4.1）
    def _validate(self, body: dict, tenant_id: str) -> dict:
        """§4.1 的九步，**顺序即方案顺序**：先便宜、先无副作用、先本地。

        有两处刻意与"友好"相反：

        - 跨租户的 scene / 策略返回 **404** 而不是 403。"这个 id 存在但不让
          你看"本身就是信息泄漏（F28）。
        - `deadline` 用解析后的时刻比较，不用字符串比较：带 `+08:00` 的时间串
          在字典序上恒大于同刻的 `Z`，用字符串比较会把"还有一小时"读成
          "已过期"（F30）。
        """
        ctx = body.get("context") or {}
        if not isinstance(ctx, dict):
            raise ServiceError(422, "INVALID_CONTEXT", "context must be object")
        if ctx.get("tenant_id") != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id does not match the credential")
        for key in ("project_id", "series_id", "episode_id", "branch_id",
                    "node_id", "command_id", "request_hash"):
            if not ctx.get(key):
                raise ServiceError(422, "INVALID_CONTEXT",
                                   f"context.{key} required")
        # 其余 11 键也必须齐：S26 的 `_validate_command` 逐键检查 18 键非空，
        # 少一个就是上游 400。这里先拒，是因为**这些值只能来自平台签名的那份
        # 上下文**——服务端没有权限替调用方补 `home_region` / `actor` /
        # `trace_id`，补出来的每个值都是伪造签名。
        for key in EXECUTION_CONTEXT_REQUIRED:
            if ctx.get(key) is None:
                raise ServiceError(
                    422, "INVALID_CONTEXT",
                    f"context.{key} required (execution-context.v2 has 18 "
                    "required keys and S26 checks every one)",
                    details={"missing": key})
        if ctx.get("node_epoch") is None or ctx.get("execution_fence") is None:
            raise ServiceError(422, "INVALID_CONTEXT",
                               "context.node_epoch and execution_fence required")

        cue = body.get("cue") or {}
        if not isinstance(cue, dict):
            raise ServiceError(422, "INVALID_CUE", "cue must be object")

        # (1) 意图原文。空白意图会与"随便什么都行"共用一个指纹，让两条本来
        #     不同的 cue 互相复用（F1）。
        intent = cue.get("music_intent")
        if not isinstance(intent, str) or not intent.strip():
            raise ServiceError(422, "EMPTY_MUSIC_INTENT",
                               "cue.music_intent must be non-blank")

        # (2) 情绪闭集 + 强度。**不从 music_intent 猜**（§2.3）：把"压抑"读成
        #     "庄严"会朝完全反的方向生成，而且不可复现（F2）。
        mood = cue.get("mood")
        if mood not in MOODS:
            raise ServiceError(422, "INVALID_MOOD",
                               f"cue.mood must be one of {list(MOODS)}",
                               details={"mood": mood})
        # 强度用自己的码：§10 的 T34-05 把"情绪不在闭集"和"强度越界"分成两条
        # 用例，合成一个码会让调用方无法区分"换情绪"和"改数值"。
        try:
            intensity = float(cue.get("mood_intensity"))
        except (TypeError, ValueError):
            raise ServiceError(422, "INVALID_MOOD_INTENSITY",
                               "cue.mood_intensity must be a number")
        if not (0.0 <= intensity <= 1.0):
            raise ServiceError(
                422, "INVALID_MOOD_INTENSITY",
                "cue.mood_intensity must be within [0, 1]",
                details={"mood_intensity": intensity})

        # (3) 编制闭集（F3）。
        instrumentation = cue.get("instrumentation")
        if instrumentation not in INSTRUMENTATIONS:
            raise ServiceError(
                422, "INVALID_INSTRUMENTATION",
                f"cue.instrumentation must be one of {list(INSTRUMENTATIONS)}",
                details={"instrumentation": instrumentation})

        # (4) 时长。契约只给了 exclusiveMinimum: 0，上限只是 DoS 防御（F4）。
        try:
            duration = float(body.get("duration_seconds"))
        except (TypeError, ValueError):
            raise ServiceError(422, "DURATION_OUT_OF_RANGE",
                               "duration_seconds must be a number")
        if not (0 < duration <= MAX_DURATION_SECONDS):
            raise ServiceError(
                422, "DURATION_OUT_OF_RANGE",
                f"duration_seconds must be in (0, {MAX_DURATION_SECONDS}]",
                details={"duration_seconds": duration})
        requested_ms = int(round(duration * 1000))
        if requested_ms <= 0:
            raise ServiceError(422, "DURATION_OUT_OF_RANGE",
                               "duration rounds to 0ms")

        # (5) 候选数。**不在这里降级为 1**——"几个候选"是已经对导演做出的
        #     承诺，悄悄改小会得到一个看起来成功的半个交付（F5）。
        try:
            candidate_count = int(body.get("candidate_count", 1))
        except (TypeError, ValueError):
            raise ServiceError(422, "CANDIDATE_COUNT_OUT_OF_RANGE",
                               "candidate_count must be an integer")
        if not (1 <= candidate_count <= MAX_CANDIDATES):
            raise ServiceError(
                422, "CANDIDATE_COUNT_OUT_OF_RANGE",
                f"candidate_count must be in [1, {MAX_CANDIDATES}]",
                details={"candidate_count": candidate_count})

        # (6) 许可策略归属（S02）。
        rights_ref = body.get("rights_policy_ref") or {}
        if not isinstance(rights_ref, dict) \
                or rights_ref.get("service_id") != "S02":
            raise ServiceError(422, "INVALID_RIGHTS_POLICY_REF",
                               "rights_policy_ref must be an S02 reference")
        for key in ("id", "revision", "content_hash"):
            if rights_ref.get(key) in (None, ""):
                raise ServiceError(422, "INVALID_RIGHTS_POLICY_REF",
                                   f"rights_policy_ref.{key} required")
        if len(str(rights_ref.get("content_hash"))) != 64:
            raise ServiceError(422, "INVALID_RIGHTS_POLICY_REF",
                               "rights_policy_ref.content_hash must be sha256 hex")

        # (7) 场景 / 拍点归属。只有 S06/S07 知道，靠注入的校验器。
        #     没配校验器就**不假装校验过**：未知归属不能变成 422 也不能变成通过。
        scene_lineage_id = cue.get("scene_lineage_id")
        if not scene_lineage_id:
            raise ServiceError(422, "INVALID_CUE",
                               "cue.scene_lineage_id required")
        try:
            cue_index = int(cue.get("cue_index"))
        except (TypeError, ValueError):
            raise ServiceError(422, "INVALID_CUE",
                               "cue.cue_index must be an integer")
        if cue_index < 0:
            raise ServiceError(422, "INVALID_CUE",
                               "cue.cue_index must be >= 0")
        beat_ref = cue.get("beat_ref")
        if self.scene_check is not None:
            try:
                scene = self.scene_check(tenant_id, scene_lineage_id, cue_index)
            except ServiceError:
                raise
            except ConnectionError as exc:
                raise ServiceError(503, "SCENE_LOOKUP_UNAVAILABLE", str(exc),
                                   retryable=True) from exc
            if scene is None:
                # F28：别的租户的场景在 S34 眼里和不存在是同一种情况。
                raise ServiceError(404, "SCENE_NOT_FOUND",
                                   f"no scene {scene_lineage_id}")
            known = scene.get("beat_refs")
            if beat_ref and known is not None and beat_ref not in known:
                raise ServiceError(
                    422, "INVALID_BEAT_REF",
                    f"beat_ref {beat_ref!r} is not on scene "
                    f"{scene_lineage_id}", details={"beat_ref": beat_ref})

        # (8) 截止时间（F30）。
        deadline = body.get("deadline")
        if not deadline:
            raise ServiceError(422, "INVALID_BODY", "deadline required")
        try:
            deadline_at = _parse_ts(deadline)
        except (TypeError, ValueError) as exc:
            raise ServiceError(422, "INVALID_BODY",
                               f"deadline is not an ISO-8601 timestamp: {exc}")
        if deadline_at <= datetime.now(timezone.utc):
            raise ServiceError(422, "DEADLINE_PASSED",
                               "deadline is not in the future",
                               details={"deadline": deadline})

        tempo = cue.get("tempo_hint") or {}
        if not isinstance(tempo, dict):
            raise ServiceError(422, "TEMPO_HINT_INVALID",
                               "cue.tempo_hint must be object")
        low_bpm = tempo.get("low_bpm")
        high_bpm = tempo.get("high_bpm")
        for value in (low_bpm, high_bpm):
            if value is not None and int(value) < 20:
                raise ServiceError(422, "TEMPO_HINT_INVALID",
                                   "tempo_hint bpm must be >= 20")
        if low_bpm is not None and high_bpm is not None \
                and int(low_bpm) > int(high_bpm):
            raise ServiceError(422, "TEMPO_HINT_INVALID",
                               "tempo_hint.low_bpm must be <= high_bpm")

        route_ref = body.get("route_policy_ref")
        if route_ref is not None and not isinstance(route_ref, dict):
            raise ServiceError(422, "INVALID_BODY",
                               "route_policy_ref must be an object")

        return {
            "context": ctx, "cue": cue, "mood": mood,
            "mood_intensity": intensity, "instrumentation": instrumentation,
            "duration": duration, "requested_ms": requested_ms,
            "candidate_count": candidate_count, "intent": intent,
            "rights_ref": rights_ref, "route_ref": route_ref,
            "scene_lineage_id": scene_lineage_id, "cue_index": cue_index,
            "beat_ref": beat_ref, "deadline": deadline,
        }

    # ---------------------------------------------------------- 许可解析
    def _bind_rights(self, tenant_id: str, ref: dict) -> dict:
        """把 `rights_policy_ref` 解析成冻结的四元组快照（§4.4）。

        C3：S02 没有公开的四元组读接口，所以这是注入 callback。没配 → 503，
        **不是"直接透传给 S26 让供应商去判断合规"**。

        `unknown` 一律写下来（快照要如实记录 S02 说的话），但它的存在让这份
        binding 不可用。**不在这里把 unknown 改写成 licensed**——那是把合规
        风险凭空消掉。
        """
        try:
            resolved = self.rights_resolver(ref, tenant_id)
        except ServiceError:
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        if not isinstance(resolved, dict):
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE",
                               "rights resolver returned no snapshot",
                               retryable=True)

        required = ("composition_status", "master_status", "performance_status",
                    "sync_status")
        for key in required:
            if not resolved.get(key):
                raise ServiceError(
                    503, "RIGHTS_RESOLVER_UNAVAILABLE",
                    f"rights resolver omitted {key}", retryable=True)

        binding_id = _new_id("binding")
        self.conn.execute(
            "INSERT INTO license_binding (license_binding_id, tenant_id, "
            "policy_ref, composition_status, composition_holder, master_status, "
            "master_holder, performance_status, sync_status, sync_territories, "
            "sync_media, sync_expires_at, work_title, isrc, iswc, "
            "attribution_text, training_basis_ref, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (binding_id, tenant_id, _jcs(ref),
             resolved["composition_status"], resolved.get("composition_holder"),
             resolved["master_status"], resolved.get("master_holder"),
             resolved["performance_status"], resolved["sync_status"],
             _jcs(resolved.get("sync_territories") or []),
             _jcs(resolved.get("sync_media") or []),
             resolved.get("sync_expires_at"), resolved.get("work_title"),
             resolved.get("isrc"), resolved.get("iswc"),
             resolved.get("attribution_text"),
             _jcs(resolved["training_basis_ref"])
             if resolved.get("training_basis_ref") is not None else None,
             _now()))
        return {"license_binding_id": binding_id,
                "unusable_reasons": _binding_unusable_reasons(resolved)}

    def _ensure_binding_usable(self, binding: dict) -> list:
        """§4.4.2 / §5.3：任一维度 `unknown` → 这个 take 不能正式交付。

        DDL 允许 `unknown` 落库（快照必须如实），所以这条只能在应用层守。
        返回**阻塞原因**列表：空列表 = 可用。`sync_territory_limited` 不算
        阻塞（它是发行范围问题），这里过滤掉它，但保留"解析时"的完整列表。

        与 `_binding_unusable_reasons` 共用同一份维度判断：两处各写一遍
        迟早分叉，于是"提交时拒绝了、复查时放行了"。
        """
        reasons = _binding_unusable_reasons(binding)
        expires_at = binding.get("sync_expires_at")
        if expires_at and binding.get("sync_status") == "licensed":
            try:
                if _parse_ts(expires_at) <= datetime.now(timezone.utc):
                    reasons.append("sync_expired")
            except (TypeError, ValueError):
                # 期限解析不了 = 无法证明它还没过期。不做乐观假设。
                reasons.append("sync_expiry_unparseable")
        return [r for r in dict.fromkeys(reasons)
                if r in BLOCKING_RIGHTS_REASONS]

    # ------------------------------------------------------ cue 意图存储
    def _upsert_cue(self, tenant_id: str, checked: dict) -> dict:
        """同一 cue 的**同一内容**只留一版 revision。

        导演把"不要太悲"改成"克制一点"会产出新指纹 → 新 revision；只改时长
        只改 EDL 时长需求，指纹不变，于是复用原 revision——这正是 §4.2 说
        的"变时长重算切点不花钱"在数据上的样子。
        """
        cue = checked["cue"]
        cue_id = cue.get("cue_id")
        if not cue_id:
            raise ServiceError(422, "INVALID_CUE", "cue.cue_id required")
        intent_hash = content_fingerprint(
            music_intent=checked["intent"], mood=checked["mood"],
            mood_intensity=checked["mood_intensity"],
            instrumentation=checked["instrumentation"],
            tempo_low_bpm=(cue.get("tempo_hint") or {}).get("low_bpm"),
            tempo_high_bpm=(cue.get("tempo_hint") or {}).get("high_bpm"),
            key_hint=cue.get("key_hint"),
            instrumental_only=bool(cue.get("instrumental_only", True)))

        existing = _row(self.conn.execute(
            "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
            "AND intent_hash=?",
            (tenant_id, cue_id, intent_hash)).fetchone())
        if existing is not None:
            # 归属必须一致：同一 cue_id 被指向另一个场景/序号通常是上游 bug，
            # 静默复用会让两个场景共享一条意图。
            if existing["scene_lineage_id"] != checked["scene_lineage_id"] \
                    or int(existing["cue_index"]) != checked["cue_index"]:
                raise ServiceError(
                    409, "CUE_LINEAGE_CONFLICT",
                    f"cue {cue_id} already belongs to "
                    f"{existing['scene_lineage_id']}#{existing['cue_index']}",
                    details={"scene_lineage_id": existing["scene_lineage_id"],
                             "cue_index": int(existing["cue_index"])})
            return existing

        latest = _row(self.conn.execute(
            "SELECT MAX(cue_revision) AS rev FROM music_cue_revision "
            "WHERE tenant_id=? AND cue_id=?", (tenant_id, cue_id)).fetchone())
        revision = int((latest or {}).get("rev") or 0) + 1
        if cue.get("cue_revision") is not None \
                and int(cue["cue_revision"]) != revision:
            # 调用方说的版本号与"下一版"不一致时**不覆盖**：版本是服务端
            # 分配的单调量，接受客户端的任意值会让历史出现空洞或倒序。
            raise ServiceError(
                409, "CUE_REVISION_CONFLICT",
                f"cue {cue_id} next revision is {revision}, request said "
                f"{cue['cue_revision']}",
                details={"expected_revision": revision})

        tempo = cue.get("tempo_hint") or {}
        self.conn.execute(
            "INSERT INTO music_cue_revision (tenant_id, cue_id, cue_revision, "
            "scene_lineage_id, cue_index, beat_ref, mood, mood_intensity, "
            "instrumentation, tempo_hint_low_bpm, tempo_hint_high_bpm, key_hint, "
            "music_intent, intent_hash, instrumental_only, dialogue_aware, "
            "source_ref, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant_id, cue_id, revision, checked["scene_lineage_id"],
             checked["cue_index"], checked["beat_ref"], checked["mood"],
             checked["mood_intensity"], checked["instrumentation"],
             tempo.get("low_bpm"), tempo.get("high_bpm"), cue.get("key_hint"),
             checked["intent"], intent_hash,
             1 if cue.get("instrumental_only", True) else 0,
             1 if cue.get("dialogue_aware", True) else 0,
             _jcs(cue.get("source_ref") or {"service_id": "S06"}),
             _now()))
        return _row(self.conn.execute(
            "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
            "AND cue_revision=?", (tenant_id, cue_id, revision)).fetchone())

    # ------------------------------------------------------------ 建 job
    def create_job(self, body: dict, tenant_id: str, command_id: str) -> dict:
        checked = self._validate(body, tenant_id)
        cue = checked["cue"]
        cue_id = cue.get("cue_id")
        if not cue_id:
            raise ServiceError(422, "INVALID_CUE", "cue.cue_id required")

        fingerprint = content_fingerprint(
            music_intent=checked["intent"], mood=checked["mood"],
            mood_intensity=checked["mood_intensity"],
            instrumentation=checked["instrumentation"],
            tempo_low_bpm=(cue.get("tempo_hint") or {}).get("low_bpm"),
            tempo_high_bpm=(cue.get("tempo_hint") or {}).get("high_bpm"),
            key_hint=cue.get("key_hint"),
            instrumental_only=bool(cue.get("instrumental_only", True)))
        requested_ms = checked["requested_ms"]
        candidate_count = checked["candidate_count"]

        # 命令键只用来满足 `mj_tenant_idem`；同命令重放由 idempotency_record
        # 在进入这里之前就拦掉了，落到这里说明是另一条命令。
        command_key = _sha256_hex("\x1f".join([
            tenant_id, command_id, str(body.get("request_hash") or "")]))

        def produce() -> dict:
            cue_rev = self._upsert_cue(tenant_id, checked)

            # 复用先于生成（T34-36 / T34-54 / §8.2）。指纹不含时长，所以
            # "42 秒改 39 秒"能在这里命中；命中就不建 job、不写台账、不碰 S26。
            # 放在建 job 之前：建了再退会留下一条 CREATED 记录，业务幂等索引
            # 也会占位，而这次根本没有生产意图。
            #
            # **只在 `candidate_count == 1` 时复用。** 手里最多只有那一份 take，
            # 要 3 个候选却交 1 个是静默降级。§7.1 / §11 U2 写的"复用已有 take
            # + 补 2 个"需要给缺额开子 job，方案明确标为未定（MUS-14），这里不
            # 自己发明；多候选走原路重买，行为与未加复用前一致。
            if candidate_count == 1:
                edl_revision = self._resolve_edl_revision(
                    tenant_id, cue, body.get("edl_revision"))
                reuse = self._find_reusable(
                    tenant_id, fingerprint=fingerprint,
                    scene_lineage_id=checked["scene_lineage_id"],
                    cue_index=int(checked["cue_index"]),
                    requested_ms=requested_ms,
                    edl_revision=edl_revision)
                if reuse is not None:
                    return self._reuse_response(
                        take=reuse["take"], artifact=reuse["artifact_ref"],
                        cut_plan=reuse["cut_plan"], fingerprint=fingerprint,
                        requested_ms=requested_ms,
                        candidate_count=candidate_count)

            job_id = _new_id("job")
            now = _now()
            try:
                self.conn.execute(
                    "INSERT INTO music_job (music_job_id, tenant_id, project_id, "
                    "series_id, episode_id, branch_id, cue_id, cue_revision, "
                    "scene_lineage_id, cue_index, requested_duration_ms, "
                    "candidate_count, candidate_index_base, license_binding_id, "
                    "content_fingerprint, node_id, node_epoch_at_create, "
                    "execution_fence, request_hash, idempotency_key, "
                    "variation_nonce, route_decision_ref, execution_context, "
                    "route_policy_ref, budget_scope_id, deadline, "
                    "business_attempt_no, state, state_seq, created_at, "
                    "updated_at, state_entered_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?,?,?,?,?,NULL,"
                    "?,?,?,?,1,'CREATED',0,?,?,?)",
                    (job_id, tenant_id, checked["context"]["project_id"],
                     checked["context"]["series_id"],
                     checked["context"]["episode_id"],
                     checked["context"]["branch_id"], cue_id,
                     int(cue_rev["cue_revision"]), checked["scene_lineage_id"],
                     checked["cue_index"], requested_ms, candidate_count,
                     fingerprint, checked["context"]["node_id"],
                     int(checked["context"]["node_epoch"]),
                     int(checked["context"]["execution_fence"]),
                     str(checked["context"].get("request_hash") or ""),
                     command_key, uuid.uuid4().hex,
                     _jcs(checked["context"]),
                     _jcs(checked["route_ref"])
                     if checked["route_ref"] is not None else None,
                     body.get("budget_scope_id") or "default",
                     checked["deadline"], now, now, now))
            except sqlite3.IntegrityError as exc:
                # SQLite 报的是**列名**而不是索引名。这条部分唯一索引撞了会得到
                # `UNIQUE constraint failed: music_job.tenant_id,
                # music_job.scene_lineage_id, ...`。按索引名 grep 永远不命中，
                # 会把并发双提交变成 500。
                if "UNIQUE constraint failed: music_job.tenant_id" in str(exc):
                    existing = _row(self.conn.execute(
                        "SELECT * FROM music_job WHERE tenant_id=? AND "
                        "scene_lineage_id=? AND cue_index=? AND "
                        "content_fingerprint=? AND requested_duration_ms=? "
                        "AND state NOT IN ('CANDIDATES_READY','POLICY_BLOCKED',"
                        "'GAP_RECORDED','REJECTED_LOCAL','FAILED',"
                        "'RECONCILED_FAILED','CANCELED')",
                        (tenant_id, checked["scene_lineage_id"],
                         checked["cue_index"], fingerprint,
                         requested_ms)).fetchone())
                    if existing is not None:
                        # 同一意图已经在路上。**不新建第二个 job**——那会让同
                        # 一条 cue 出两份钱、两份 ready 事件、两个检查点竞争。
                        return self._job_response(
                            existing, fingerprint, requested_ms,
                            extra={"duplicate_suppressed": True})
                raise

            job = self._job(tenant_id, job_id)
            outbox = self.outbox
            job = self._transition(job, "CHECKING_POLICY", outbox=outbox)

            # ---- CHECKING_POLICY：解析许可四元组（§4.4.4 时点 1）
            rights = self._bind_rights(tenant_id, checked["rights_ref"])
            self.conn.execute(
                "UPDATE music_job SET license_binding_id=?, updated_at=? "
                "WHERE music_job_id=?",
                (rights["license_binding_id"], _now(), job_id))
            job = self._job(tenant_id, job_id)

            if rights["unusable_reasons"]:
                # F7：缺一个维度就是 POLICY_BLOCKED + 缺口。**不花钱**，也
                # 不把它记成 FAILED——FAILED 可以 :retry 重提付费任务，而
                # "法务还没批"重提多少次都还是同一个结果。
                self._record_gap(
                    tenant_id=tenant_id,
                    scene_lineage_id=checked["scene_lineage_id"],
                    cue_index=checked["cue_index"], cue_id=cue_id,
                    kind="license_incomplete",
                    detail="license tuple unusable: "
                           + ",".join(rights["unusable_reasons"]),
                    source_job_id=job_id, outbox=outbox)
                job = self._transition(
                    job, "POLICY_BLOCKED", reason_code="LICENSE_INCOMPLETE",
                    detail=",".join(rights["unusable_reasons"]), outbox=outbox)
                return self._job_response(
                    job, fingerprint, requested_ms,
                    extra={"reason_code": "LICENSE_INCOMPLETE",
                           "license_unusable_reasons":
                               rights["unusable_reasons"]})

            # ---- PREPARING：取路由、查能力、查 N 份预算（§4.7 第 3 条）
            job = self._transition(job, "PREPARING", outbox=outbox)
            return self._submit(job, cue_rev, outbox,
                                fingerprint=fingerprint,
                                requested_ms=requested_ms)

        return self._command(tenant_id, command_id, produce)

    # ---------------------------------------------------------- 提交 S26
    def _reject_local(self, job: dict, code: str, outbox: Outbox, *,
                      detail: str, extra: dict | None = None) -> dict:
        """本地拒绝：没花钱、没留台账。终态但**不是失败**——换渠道/改策略
        就能好，所以它能被新 job 重开（业务幂等键只在未终结时占位）。"""
        self.conn.execute(
            "UPDATE music_job SET failure_code=?, updated_at=? WHERE music_job_id=?",
            (code, _now(), job["music_job_id"]))
        job = self._job(job["tenant_id"], job["music_job_id"])
        job = self._transition(job, "REJECTED_LOCAL", reason_code=code,
                               detail=detail, outbox=outbox)
        body = {"music_job_id": job["music_job_id"], "state": job["state"],
                "reason_code": code,
                "content_fingerprint": job["content_fingerprint"],
                "candidate_count": int(job["candidate_count"]),
                "requested_duration_ms": int(job["requested_duration_ms"]),
                "quote_upper_bound_micro_total": None}
        body.update(extra or {})
        return body

    def _policy_ref_of(self, job: dict) -> dict:
        """把**本次 job 绑定的那份**授权策略原样回传。

        不是从 cue 上取：一个 cue 修订可以先后绑多份策略（`license_binding`
        一行一份），取错了 S26 就会拿旧策略去付费。
        """
        row = self.conn.execute(
            "SELECT policy_ref FROM license_binding WHERE license_binding_id=?",
            (job["license_binding_id"],)).fetchone()
        if row is None:
            raise ServiceError(503, "LICENSE_BINDING_MISSING",
                               "job has no license binding to echo upstream")
        return _loads(row["policy_ref"], {})

    def _generation_command(self, job: dict, cue_rev: dict, *,
                            attempt_id: str, idempotency_key: str) -> dict:
        """`generation-command.v2` 的 music 分支。

        **11 个必填键一个都不能少**（S26 逐键查 `COMMAND_REQUIRED`），但
        `input` 里**只放契约允许的四个**（C1 的落地处）：

        C1：`mood` / `mood_intensity` / `instrumentation` / `tempo_hint` /
        `key_hint` / `beat_ref` / `candidate_count` 到这里为止。music 的
        `input` 是 `additionalProperties: false`，多塞一个键 S26 直接 422；
        "上层需要它们"不等于"供应商能收它们"。

        `context` **原样复用**请求里那份已签名上下文（存于 job），不重新拼：
        18 键里的 `actor` / `trace_id` / `home_region` 只能来自平台签名，服务
        端补出来的每个值都是伪造签名（AGENTS.md 红线）。
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "context": _loads(job["execution_context"], {}),
            # attempt_id 与 idempotency_key 逐候选不同：S26 的
            # `generation_attempt` 主键是 `(tenant_id, attempt_id)`，N 份候选
            # 必须落成 N 条 attempt。
            "attempt_id": attempt_id,
            "variation_nonce": job["variation_nonce"],
            "idempotency_key": idempotency_key,
            "modality": "music",
            "input": {
                "music_intent": cue_rev["music_intent"],
                "duration_seconds": round(int(job["requested_duration_ms"]) / 1000.0,
                                          3),
                "instrumental_only": bool(int(cue_rev["instrumental_only"])),
                "rights_policy_ref": self._policy_ref_of(job),
            },
            # `template_ref` 在契约里是 `oneOf` 的第二分支（允许 `null`）。
            # 音乐没有模板可套，显式给 `null`；省略会被判成"缺必填键" 422。
            "template_ref": None,
            "route_policy_ref": _loads(job["route_policy_ref"], None),
            "budget_scope_id": job["budget_scope_id"],
            "deadline": job["deadline"],
        }

    def _submit(self, job: dict, cue_rev: dict, outbox: Outbox, *,
                fingerprint: str, requested_ms: int) -> dict:
        """PREPARING：取路由 → 能力位 → N×报价预算 → N 次 POST。

        **路径 A（§4.7）**：N 个候选就是 N 次独立准入。这里每一处"要么全做
        要么不做"都是刻意的：

        - 预算按 `N × quote` 检查（F6），不是按一份。只查一份的后果是前两个
          候选成功、第三个因预算失败，而"3 个候选"已经对导演承诺过了。
        - 任何一份准入失败就整组作废（`FAILED(PARTIAL_CANDIDATES)`），**不
          降级候选数**。
        - `candidate_index` 没有任何合法位置可放（C2 / MUS-4），所以 N 次提交
          靠**互不相同的 attempt_id / idempotency_key** 区分，映射落在
          `music_job_candidate`。
        """
        candidate_count = int(job["candidate_count"])
        attempt_base = int(job["business_attempt_no"])

        # 先为**每个**候选各取一次路由决策。不能一次路由带 N 次提交：S26 会
        # 逐字段比对 route[field] 与 command[context][field]（含 `attempt_id`），
        # 而 route 上的 attempt_id 是 S25 收到那条命令里的单值。N 个不同
        # attempt_id 共用一个 route，第 2 份起必然 409 ROUTE_DECISION_STALE。
        plans = []
        for index in range(candidate_count):
            attempt_id = f"{job['music_job_id']}:{attempt_base}:{index}"
            # 幂等键同样逐候选：重试时要能各自重放，不能一条键被三份候选
            # 抢用（S26 的 attempt 主键是 `(tenant_id, attempt_id)`）。
            idempotency_key = f"{job['idempotency_key']}:{attempt_base}:{index}"
            command = self._generation_command(
                job, cue_rev, attempt_id=attempt_id,
                idempotency_key=idempotency_key)

            try:
                route = self.route_resolver(job["tenant_id"], job, command)
            except ServiceError:
                raise
            except ConnectionError as exc:
                raise ServiceError(503, "ROUTE_DECISION_UNAVAILABLE", str(exc),
                                   retryable=True) from exc

            if isinstance(route, dict) and route.get("feasible") is False:
                # 路由不可行。透传 degradation_options，**不自己降级**——缩短
                # 时长或减候选都是内容层的决定，替调用方做了就成了第二权威。
                return self._reject_local(
                    job, "ROUTE_INFEASIBLE", outbox,
                    detail=f"no feasible route for candidate {index}",
                    extra={"degradation_options":
                           route.get("degradation_options") or []})

            if isinstance(route, dict) \
                    and route.get("residency_allowed") is False:
                # F29：数据驻留。必须排在第一次付费提交之前。
                return self._reject_local(
                    job, "RESIDENCY_FORBIDDEN", outbox,
                    detail="route cannot satisfy the data residency requirement")

            if _route_ref(route) is None:
                # 拿不到可行路由时**不能**自己编一个 route_decision_id。
                raise ServiceError(
                    503, "ROUTE_DECISION_UNAVAILABLE",
                    "S25 returned no route id; S34 will not invent one",
                    retryable=True)

            caps = (route or {}).get("capabilities") or {}
            if candidate_count > 1 and caps.get("multi_candidate") is False:
                # F8：不降级为 1。悄悄少给两个候选会让导演以为只对比了一份。
                return self._reject_local(
                    job, "CAPABILITY_MISSING", outbox,
                    detail=f"route cannot produce {candidate_count} candidates; "
                           "refusing to silently degrade")

            if bool(int(cue_rev["instrumental_only"])) \
                    and caps.get("instrumental_only") is False:
                # F9：不降级为"带人声的版本"。无人声是剧情需求，不是偏好。
                return self._reject_local(
                    job, "CAPABILITY_MISSING", outbox,
                    detail="route cannot guarantee instrumental-only output; "
                           "refusing to silently degrade")

            quote = _quote_upper_bound(route)
            if quote is None:
                # 没有报价就不可能证明这一份在预算内。不能拿 None 当 0。
                raise ServiceError(
                    503, "ROUTE_DECISION_UNAVAILABLE",
                    "route decision carries no quote_upper_bound_micro",
                    retryable=True)
            plans.append({"command": command, "route": route, "quote": quote,
                          "index": index})

        # F6：**提交前**按总和检查一次（§4.7 第 3 条）。用 N 份报价之和而
        # 不是"一份乘 N"：不同候选可能落在不同渠道，单价本来就不一样。
        # 这里只读，不改预算——预算是 S23 的权威。
        total_quote = sum(int(p["quote"]) for p in plans)
        try:
            budget = self.budget_check(job["tenant_id"], job["budget_scope_id"],
                                       total_quote)
        except ServiceError:
            raise
        except ConnectionError as exc:
            raise ServiceError(503, "BUDGET_CHECK_UNAVAILABLE", str(exc),
                               retryable=True) from exc

        if isinstance(budget, dict) and budget.get("sufficient") is False:
            return self._reject_local(
                job, "BUDGET_INSUFFICIENT_FOR_CANDIDATES", outbox,
                detail=f"{candidate_count} candidates need {total_quote} micro",
                extra={"quote_upper_bound_micro_total": total_quote,
                       "available_micro": budget.get("available_micro")})

        self.conn.execute(
            "UPDATE music_job SET route_decision_ref=?, "
            "quote_upper_bound_micro_total=?, updated_at=? WHERE music_job_id=?",
            (_jcs(_route_ref(plans[0]["route"])), total_quote, _now(),
             job["music_job_id"]))
        job = self._job(job["tenant_id"], job["music_job_id"])

        now = _now()
        submitted_rows = []
        for plan in plans:
            index = plan["index"]
            try:
                submitted = self.submitter(job, plan["command"], plan["route"],
                                           index)
            except ServiceError as exc:
                # 第一份就没成：没花钱，按路由/上游不可用处理。
                if not submitted_rows:
                    raise
                # 后面某一份没成：整组作废。已经花出去的钱记在台账里，
                # 但**不交付半个候选集**。
                self._fail_partial(job, submitted_rows,
                                   f"candidate {index} submit failed: "
                                   f"{exc.code}", outbox)
                job = self._job(job["tenant_id"], job["music_job_id"])
                return self._job_response(
                    job, fingerprint, requested_ms,
                    extra={"reason_code": "PARTIAL_CANDIDATES",
                           "quote_upper_bound_micro_total": total_quote})
            except ConnectionError as exc:
                if not submitted_rows:
                    raise ServiceError(503, "GENERATION_JOBS_UNAVAILABLE",
                                       str(exc), retryable=True) from exc
                self._fail_partial(job, submitted_rows, str(exc), outbox)
                job = self._job(job["tenant_id"], job["music_job_id"])
                return self._job_response(
                    job, fingerprint, requested_ms,
                    extra={"reason_code": "PARTIAL_CANDIDATES",
                           "quote_upper_bound_micro_total": total_quote})

            if not isinstance(submitted, dict) \
                    or not submitted.get("provider_job_id"):
                raise ServiceError(503, "GENERATION_JOBS_UNAVAILABLE",
                                   "S26 did not return a provider_job_id",
                                   retryable=True)
            self.conn.execute(
                "INSERT INTO music_job_candidate (music_job_id, "
                "candidate_index, attempt_no, provider_job_id, attempt_id, "
                "quote_upper_bound_micro, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,'SUBMITTED',?,?)",
                (job["music_job_id"], int(job["candidate_index_base"]) + index,
                 attempt_base, submitted["provider_job_id"],
                 submitted.get("attempt_id") or plan["command"]["attempt_id"],
                 int(plan["quote"]), now, now))
            submitted_rows.append(submitted)

        # 只有真的把钱花出去的意图才进台账（§8.1）。一行 = 一个逻辑 job，
        # `candidate_count` 是字段——一次"生成 3 个候选"是一个决策。
        self.conn.execute(
            "INSERT INTO music_intent_ledger (entry_id, tenant_id, music_job_id, "
            "scene_lineage_id, cue_index, action, candidate_count, run_id, "
            "node_epoch, manifest_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ledger"), job["tenant_id"], job["music_job_id"],
             job["scene_lineage_id"], int(job["cue_index"]), "generate",
             candidate_count, None, int(job["node_epoch_at_create"]), None, now))

        job = self._job(job["tenant_id"], job["music_job_id"])
        job = self._transition(job, "SUBMITTED", outbox=outbox)
        job = self._transition(job, "WAITING_PROVIDER", outbox=outbox)
        return self._job_response(
            job, fingerprint, requested_ms,
            extra={"operation_id": submitted_rows[0].get("operation_id"),
                   "quote_upper_bound_micro_total": total_quote})

    def _fail_partial(self, job: dict, submitted_rows: list, detail: str,
                      outbox: Outbox) -> None:
        """F10/F11：半个候选集**不能交付**。已有的 provider job 全部作废。

        这里**不去取消**已经提交的那几份：取消是尽力而为的网络调用，而
        "整组作废"这个事实必须在本地先落定。能不能退钱由 S26/S23 决定，
        S34 不改状态去猜。
        """
        self.conn.execute(
            "UPDATE music_job_candidate SET state='FAILED', "
            "failure_code='PARTIAL_CANDIDATES', updated_at=? "
            "WHERE music_job_id=? AND attempt_no=? AND state IN "
            "('SUBMITTED','WAITING')",
            (_now(), job["music_job_id"], int(job["business_attempt_no"])))
        self.conn.execute(
            "UPDATE music_job SET failure_code='PARTIAL_CANDIDATES', "
            "failure_detail=?, updated_at=? WHERE music_job_id=?",
            (detail, _now(), job["music_job_id"]))
        job = self._job(job["tenant_id"], job["music_job_id"])
        self._transition(job, "FAILED", reason_code="PARTIAL_CANDIDATES",
                         detail=detail, outbox=outbox)

    # ------------------------------------------------------- 复用（§4.2/§8.2）
    def _find_reusable(self, tenant_id: str, *, fingerprint: str,
                       scene_lineage_id: str, cue_index: int,
                       requested_ms: int, edl_revision: int | None) -> dict | None:
        """T34-36 / T34-54：**只改时长不该再买一次**。

        指纹不含时长（§2.2），所以"42 秒改 39 秒"的指纹与原 job 相同。这里
        找一份**可用的、已归档的** take，重新算一次切点：能放下就复用，
        放不下才回落去生成（§4.3 第 4 步 `resynthesize_for_duration`）。

        限定在**同一条 cue**（有意的窄）：跨 cue 复用是 S33 的策略（它按镜头
        前缀挑），音乐这边方案没写，不自己发明——一条 cue 换另一个场景还要
        复用同一段音频，无损于钱却有损于"检查点指向哪一份"。

        **优先复用检查点上那一个**：多候选的 cue 里，位置层关心的是"当前
        排进去的是哪一份"。按 created_at 挑最新会让"时长改一下"把目录里的
        第 3 个候选当成已选的那一个，而检查点指的是第 1 个。没有检查点时
        才退回最新（§3.3 / §6.2）。

        四条前提，缺一不命中：

        - 同租户、同 cue、同内容指纹、`rights_state='usable'`。失效的 take
          复用了也是 void，S42 拿到位置就会混进去。
        - artifact 仍可引用。S28 是 bytes 的权威；查不到（None）不算否决——
          把"没问成"当成"没了"会让 S28 抖动白白花一次钱。
        - 有节拍网格。没有网格就算不出切点，复用等于把 `unplaceable` 交给
          S42。
        - 切点放得下（`end_strategy != 'unplaceable'`）。
        """
        rows = self.conn.execute(
            "SELECT * FROM music_take WHERE tenant_id=? AND scene_lineage_id=? "
            "AND cue_index=? AND content_fingerprint=? "
            "AND rights_state='usable' AND state IN ('READY','SELECTED') "
            "ORDER BY created_at DESC",
            (tenant_id, scene_lineage_id, int(cue_index),
             fingerprint)).fetchall()
        checkpoint = _row(self.conn.execute(
            "SELECT * FROM music_checkpoint WHERE tenant_id=? AND "
            "scene_lineage_id=? AND cue_index=?",
            (tenant_id, scene_lineage_id, int(cue_index))).fetchone())
        placed = None if checkpoint is None else checkpoint["music_take_id"]
        if placed is not None:
            # 只调顺序，不筛掉别的：检查点那一个若放不下，仍可退回另一份。
            rows = sorted(rows, key=lambda r: r["music_take_id"] != placed)
        for row in rows:
            take = dict(row)
            grid = self._beat_of(take["music_take_id"])
            if grid is None:
                continue
            artifact = _loads(take["artifact_ref"], {})
            try:
                still_there = self.artifact_check(artifact)
            except (ServiceError, ConnectionError):
                still_there = None
            if still_there is False:
                continue
            plan = self._cut_plan_for(take, requested_ms, edl_revision)
            if plan.get("end_strategy") == "unplaceable":
                # 放不下 → 这次复用等于交一份空位置，方向反了。
                continue
            return {"take": take, "artifact_ref": artifact, "cut_plan": plan}
        return None

    def _reuse_response(self, *, take: dict, artifact: dict, cut_plan: dict,
                        fingerprint: str, requested_ms: int,
                        candidate_count: int) -> dict:
        """复用时的响应形状。**不建 job、不写台账、不碰 S26。**

        刻意**不带 `state`**：`music_job` 的状态词表里没有"复用"这一档，编一个
        词表外的值会让调用方拿它去和状态机对账。S33 的复用响应也是这个形状
        （`sfx_job_id: null` + `reused: true`，无状态）。
        """
        return {
            "music_job_id": None,
            "reused": True,
            "reason_code": "CONTENT_REUSED_DURATION_ONLY",
            "music_take_id": take["music_take_id"],
            "artifact_ref": artifact,
            "natural_duration_ms": int(take["natural_duration_ms"]),
            "requested_duration_ms": requested_ms,
            "content_fingerprint": fingerprint,
            "candidate_count": candidate_count,
            "quote_upper_bound_micro_total": 0,
            "cut_plan": cut_plan,
            "note": ("content reused; only the cut plan changed, so nothing "
                     "was paid for and no ledger row was written"),
        }

    # ------------------------------------------------------------ 响应体
    @staticmethod
    def _job_response(job: dict, fingerprint: str, requested_ms: int,
                      *, extra: dict | None = None) -> dict:
        body = {
            "music_job_id": job["music_job_id"],
            "state": job["state"],
            "content_fingerprint": fingerprint,
            "candidate_count": int(job["candidate_count"]),
            "requested_duration_ms": requested_ms,
            "quote_upper_bound_micro_total":
                job["quote_upper_bound_micro_total"],
            "reused": False,
        }
        body.update(extra or {})
        return body

    # ---------------------------------------------------- 读取：CutPlan
    def _resolve_edl_revision(self, tenant_id: str, cue: dict,
                              edl_revision: int | None) -> int | None:
        """`?edl_revision=` 缺省时问一次 S41 (§6.2 末段)。

        **问不到就返回 None，不编一个 `0`。** `0` 会被写进
        `cut_plan_cache` 的主键，等于替 S41 断言"这是第 0 版 EDL"；而
        `None` 的语义是"没查/没给"，两者在响应里必须可区分（§6.2）。
        位置层是免费派生量，S41 抖动不该让这次读失败。
        """
        if edl_revision is not None:
            return int(edl_revision)
        if self.edl_provider is None:
            return None
        try:
            # 第三个参数是"已知的 EDL 版本"，这里传 None 表示"我不知道，请给
            # 当前版本"。不传 0：0 是"第 0 版"，与"未知"不是一件事。
            edl = self.edl_provider(tenant_id, cue.get("scene_lineage_id"), None)
        except (ServiceError, ConnectionError):
            return None
        if not isinstance(edl, dict):
            return None
        value = edl.get("edl_revision")
        return int(value) if isinstance(value, int) else None

    def _cut_plan_for(self, take: dict, requested_ms: int,
                      edl_revision: int | None) -> dict:
        """**每次现算**，不把 `cut_plan_cache` 当事实读（§4.3 / T34-39）。

        缓存可以被 TRUNCATE 而不影响任何正确性——这正是位置层与内容层分离
        的证据。读路径绕过缓存。
        """
        grid = _row(self.conn.execute(
            "SELECT * FROM beat_grid WHERE music_take_id=?",
            (take["music_take_id"],)).fetchone())
        loops = _row(self.conn.execute(
            "SELECT * FROM loop_point_set WHERE music_take_id=?",
            (take["music_take_id"],)).fetchone())
        plan = cut_plan(
            natural_duration_ms=int(take["natural_duration_ms"]),
            requested_duration_ms=int(requested_ms),
            bpm=None if grid is None else float(grid["bpm"]),
            meter_num=None if grid is None else int(grid["meter_num"]),
            meter_den=None if grid is None else int(grid["meter_den"]),
            downbeats_ms=_loads(grid["downbeat_ms"], []) if grid else [],
            phrases_ms=_loads(grid["phrase_ms"], []) if grid else [],
            beat_confidence=None if grid is None else float(grid["confidence"]),
            tail_kind=take.get("tail_kind") or "unknown",
            loop_ranges=_loads(loops["ranges"], []) if loops else [])
        plan["edl_revision"] = edl_revision
        plan["music_take_id"] = take["music_take_id"]
        plan["loop_available"] = bool(
            loops is not None and _loads(loops["ranges"], []))
        if take.get("rights_state") != "usable":
            # §4.4.4 时点 3：不可用的 take 不返回位置。返回了 S42 就会混进去。
            plan.update({"end_strategy": "unplaceable", "segments": [],
                         "transition": None, "fade_in_ms": 0,
                         "fade_out_ms": 0, "reason": "rights_lapsed",
                         "covered_ms": 0,
                         "tail_gap_ms": int(requested_ms)})
        return plan

    def _beat_of(self, take_id: str) -> dict | None:
        grid = _row(self.conn.execute(
            "SELECT * FROM beat_grid WHERE music_take_id=?",
            (take_id,)).fetchone())
        if grid is None:
            return None
        return {**grid, "downbeat_ms": _loads(grid["downbeat_ms"], []),
                "phrase_ms": _loads(grid["phrase_ms"], [])}

    def _loop_of(self, take_id: str) -> dict | None:
        loops = _row(self.conn.execute(
            "SELECT * FROM loop_point_set WHERE music_take_id=?",
            (take_id,)).fetchone())
        if loops is None:
            return None
        return {**loops, "ranges": _loads(loops["ranges"], [])}

    def _license_of(self, binding_id: str) -> dict | None:
        binding = _row(self.conn.execute(
            "SELECT * FROM license_binding WHERE license_binding_id=?",
            (binding_id,)).fetchone())
        if binding is None:
            return None
        return {
            "license_binding_id": binding["license_binding_id"],
            "composition": {"status": binding["composition_status"],
                            "holder": binding["composition_holder"]},
            "master": {"status": binding["master_status"],
                       "holder": binding["master_holder"]},
            "performance": {"status": binding["performance_status"]},
            "sync": {"status": binding["sync_status"],
                     "territories": _loads(binding["sync_territories"], []),
                     "media": _loads(binding["sync_media"], []),
                     "expires_at": binding["sync_expires_at"]},
            "isrc": binding["isrc"], "iswc": binding["iswc"],
            "work_title": binding["work_title"],
            "attribution_text": binding["attribution_text"],
            "training_basis_ref": _loads(binding["training_basis_ref"]),
            "resolved_at": binding["resolved_at"],
        }

    # ------------------------------------------- 读取：GET /music-cues/{id}
    def get_cue(self, tenant_id: str, cue_id: str, *,
                cue_revision: int | None = None,
                edl_revision: int | None = None) -> dict:
        """§6.2。`license` 在 **cue 级**返回：同一 job 的候选共享同一次许可
        解析（§6.2 末段）。逐个重复一遍会让"四元组一致"看起来像巧合。

        `cut_plan` 每次现算。`?edl_revision=` 只影响响应里标明的版本号——
        S34 不持有 EDL，只在 S41 给的时候记账。
        """
        if cue_revision is None:
            cue = _row(self.conn.execute(
                "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
                "ORDER BY cue_revision DESC LIMIT 1",
                (tenant_id, cue_id)).fetchone())
        else:
            cue = _row(self.conn.execute(
                "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
                "AND cue_revision=?",
                (tenant_id, cue_id, int(cue_revision))).fetchone())
        if cue is None:
            raise ServiceError(404, "MUSIC_CUE_NOT_FOUND", f"no cue {cue_id}")

        job = _row(self.conn.execute(
            "SELECT * FROM music_job WHERE tenant_id=? AND cue_id=? "
            "AND state IN ('CANDIDATES_READY','RECONCILED_CANDIDATES') "
            "ORDER BY updated_at DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())

        takes = []
        if job is not None:
            takes = [dict(r) for r in self.conn.execute(
                "SELECT * FROM music_take WHERE tenant_id=? AND music_job_id=? "
                "ORDER BY candidate_index",
                (tenant_id, job["music_job_id"])).fetchall()]

        selection = _row(self.conn.execute(
            "SELECT * FROM cue_selection WHERE tenant_id=? AND "
            "scene_lineage_id=? AND cue_index=? "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant_id, cue["scene_lineage_id"],
             int(cue["cue_index"]))).fetchone())

        checkpoint = _row(self.conn.execute(
            "SELECT * FROM music_checkpoint WHERE tenant_id=? AND "
            "scene_lineage_id=? AND cue_index=?",
            (tenant_id, cue["scene_lineage_id"],
             int(cue["cue_index"]))).fetchone())

        gap = _row(self.conn.execute(
            "SELECT * FROM music_gap WHERE tenant_id=? AND cue_id=? "
            "AND state='open' ORDER BY created_at DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())

        # §4.4.4 时点 3：这次 GET 可能把 usable 变成 void（只收紧，不放松）。
        checked_takes = [self.recheck_take(tenant_id, t["music_take_id"])
                         for t in takes]
        if any(t.get("rights_state") != "usable" for t in checked_takes):
            gap = gap or _row(self.conn.execute(
                "SELECT * FROM music_gap WHERE tenant_id=? AND cue_id=? "
                "AND state='open' AND kind='rights_lapsed' "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id, cue_id)).fetchone())

        requested_ms = int(job["requested_duration_ms"]) if job else None
        edl_revision = self._resolve_edl_revision(tenant_id, cue, edl_revision)
        cut_plan_body = None
        if checked_takes and requested_ms:
            selected_id = (selection or {}).get("music_take_id")
            anchor = next((t for t in checked_takes
                           if t["music_take_id"] == selected_id), None)
            cut_plan_body = self._cut_plan_for(anchor or checked_takes[0],
                                               requested_ms, edl_revision)

        candidates = []
        for take in checked_takes:
            grid = self._beat_of(take["music_take_id"])
            loops = self._loop_of(take["music_take_id"])
            candidates.append({
                "music_take_id": take["music_take_id"],
                "candidate_index": int(take["candidate_index"]),
                "artifact_ref": _loads(take["artifact_ref"], {}),
                "natural_duration_ms": int(take["natural_duration_ms"]),
                "detect_bpm": take["detect_bpm"],
                "detected_key": take["detected_key"],
                "meter": (None if grid is None
                          else {"num": int(grid["meter_num"]),
                                "den": int(grid["meter_den"])}),
                "tail_kind": take["tail_kind"],
                "rights_state": take["rights_state"],
                "loop_available": bool(loops is not None and loops["ranges"]),
                "dialogue_density_hint": _loads(
                    take["dialogue_density_hint"], None),
                "dialogue_density_source":
                    take["dialogue_density_source"] or "unknown",
                "state": take["state"],
            })

        license_body = None
        if job is not None and job["license_binding_id"]:
            license_body = self._license_of(job["license_binding_id"])

        return {
            "cue_id": cue_id,
            "cue_revision": int(cue["cue_revision"]),
            "scene_lineage_id": cue["scene_lineage_id"],
            "cue_index": int(cue["cue_index"]),
            "intent": {
                "mood": cue["mood"],
                "mood_intensity": float(cue["mood_intensity"]),
                "instrumentation": cue["instrumentation"],
                "tempo_hint": {"low_bpm": cue["tempo_hint_low_bpm"],
                               "high_bpm": cue["tempo_hint_high_bpm"]},
                "key_hint": cue["key_hint"],
                "intent_hash": cue["intent_hash"],
                "instrumental_only": bool(int(cue["instrumental_only"])),
            },
            "selection": selection,
            "checkpoint": checkpoint,
            "candidates": candidates,
            "license": license_body,
            "cut_plan": cut_plan_body,
            "gap": gap,
            "music_job_id": None if job is None else job["music_job_id"],
            "edl_revision": edl_revision,
        }

    # ----------------------------------------------- 读取：GET /music-cues
    def list_cues(self, tenant_id: str, *,
                  scene_lineage_id: str | None = None,
                  episode_id: str | None = None,
                  has_gap: bool | None = None) -> dict:
        """§6.3。`?episode_id=&has_gap=true` 是 S41 `completeness` 的输入。

        C6：缺口从这里取，**不**映射进 S41 的 `gaps[].kind` 闭集——那个闭集
        里没有"音乐缺失"，硬塞进去要么被拒，要么让 `formal_master` 在一个
        缺了整条 BGM 的集上仍然是真。
        """
        where = ["c.tenant_id=?"]
        args: list = [tenant_id]
        if scene_lineage_id:
            where.append("c.scene_lineage_id=?")
            args.append(scene_lineage_id)
        if episode_id:
            where.append("c.cue_id IN (SELECT j.cue_id FROM music_job j "
                         "WHERE j.tenant_id=? AND j.episode_id=?)")
            args.extend([tenant_id, episode_id])

        rows = self.conn.execute(
            "SELECT c.* FROM music_cue_revision c WHERE " + " AND ".join(where) +
            " AND c.cue_revision = (SELECT MAX(x.cue_revision) FROM "
            "music_cue_revision x WHERE x.tenant_id=c.tenant_id "
            "AND x.cue_id=c.cue_id) ORDER BY c.scene_lineage_id, c.cue_index",
            args).fetchall()

        items = []
        for row in rows:
            cue = dict(row)
            gap = _row(self.conn.execute(
                "SELECT gap_id, kind, detail FROM music_gap WHERE tenant_id=? "
                "AND cue_id=? AND state='open' ORDER BY created_at DESC LIMIT 1",
                (tenant_id, cue["cue_id"])).fetchone())
            if has_gap is True and gap is None:
                continue
            if has_gap is False and gap is not None:
                continue
            selection = _row(self.conn.execute(
                "SELECT selection_id, state, music_take_id, selected_by, "
                "selection_kind, decided_at FROM cue_selection WHERE tenant_id=? "
                "AND scene_lineage_id=? AND cue_index=? "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id, cue["scene_lineage_id"],
                 int(cue["cue_index"]))).fetchone())
            items.append({**cue, "gap": gap, "has_gap": gap is not None,
                          "selection": selection})

        return {"items": items, "total": len(items),
                "has_gap_filter": has_gap}

    # --------------------------- GET /scenes/{id}/music-checkpoint
    def get_scene_checkpoint(self, tenant_id: str,
                             scene_lineage_id: str) -> dict:
        """S42 的主读口：这个场景每条 cue 当前的基线 take。

        `cue_count` 数的是**不同的 cue_index**，不是行数：重选一次会推进
        `checkpoint_seq`，若用它数 cue，S42 会把"重选"读成"多了一条配乐"。
        """
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM music_checkpoint WHERE tenant_id=? AND "
            "scene_lineage_id=? ORDER BY cue_index",
            (tenant_id, scene_lineage_id)).fetchall()]
        gaps = [dict(r) for r in self.conn.execute(
            "SELECT gap_id, cue_index, cue_id, kind FROM music_gap "
            "WHERE tenant_id=? AND scene_lineage_id=? AND state='open' "
            "ORDER BY cue_index", (tenant_id, scene_lineage_id)).fetchall()]
        return {
            "scene_lineage_id": scene_lineage_id,
            "checkpoints": rows,
            "open_gaps": gaps,
            "cue_count": len({int(r["cue_index"]) for r in rows}),
        }

    # ---------------------------------------- GET /music-takes/{id}
    def get_take(self, tenant_id: str, take_id: str) -> dict:
        take = _row(self.conn.execute(
            "SELECT * FROM music_take WHERE tenant_id=? AND music_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "MUSIC_TAKE_NOT_FOUND", f"no take {take_id}")
        return {
            **take,
            "artifact_ref": _loads(take["artifact_ref"], {}),
            "beat_grid": self._beat_of(take_id),
            "loop_points": self._loop_of(take_id),
            "dialogue_density_hint": _loads(
                take["dialogue_density_hint"], None),
            "dialogue_density_source":
                take["dialogue_density_source"] or "unknown",
        }

    # ------------------------- GET /music-takes/{id}/license（S43 专用）
    def get_take_license(self, tenant_id: str, take_id: str) -> dict:
        """§6.3。**不含 `music_intent`。**

        这条端点的全部理由是权限分离：S43 申报只需要 `work_title` / ISRC /
        ISWC / 四元组 / `attribution_text` / 地区媒体期限，而音乐意图原文
        会泄漏剧情。把意图一起返回，等于让一个为合规开的读口变成剧情读口。
        """
        take = _row(self.conn.execute(
            "SELECT * FROM music_take WHERE tenant_id=? AND music_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "MUSIC_TAKE_NOT_FOUND", f"no take {take_id}")
        license_body = self._license_of(take["license_binding_id"])
        if license_body is None:
            raise ServiceError(503, "LICENSE_BINDING_MISSING",
                               "take references a binding that no longer exists")
        return {
            "music_take_id": take_id,
            "rights_state": take["rights_state"],
            "source_kind": take["source_kind"],
            "license": license_body,
        }

    # ------------------- POST /music-cues/{id}/cut-plan:recompute
    def recompute_cut_plan(self, tenant_id: str, cue_id: str,
                           edl_revision: int | None = None) -> dict:
        """**纯计算，不花钱、不碰 S26**（§4.2 第 1/2 层）。

        这就是"跟着剪辑走"的那一半：剪辑发新版 EDL，重算一次就完了。
        写缓存只是省算力的副本——写失败不影响响应里的数值，因为数值是
        现算的。
        """
        cue = _row(self.conn.execute(
            "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
            "ORDER BY cue_revision DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())
        if cue is None:
            raise ServiceError(404, "MUSIC_CUE_NOT_FOUND", f"no cue {cue_id}")

        job = _row(self.conn.execute(
            "SELECT * FROM music_job WHERE tenant_id=? AND cue_id=? "
            "AND state IN ('CANDIDATES_READY','RECONCILED_CANDIDATES') "
            "ORDER BY updated_at DESC LIMIT 1",
            (tenant_id, cue_id)).fetchone())
        if job is None:
            raise ServiceError(409, "NO_TAKE_TO_PLACE",
                               "cue has no CANDIDATES_READY job to place")

        takes = [dict(r) for r in self.conn.execute(
            "SELECT * FROM music_take WHERE tenant_id=? AND music_job_id=? "
            "AND state IN ('READY','SELECTED') ORDER BY candidate_index",
            (tenant_id, job["music_job_id"])).fetchall()]
        if not takes:
            raise ServiceError(409, "NO_TAKE_TO_PLACE",
                               "job has no usable take to place")

        requested_ms = int(job["requested_duration_ms"])
        edl_revision = self._resolve_edl_revision(tenant_id, cue, edl_revision)
        results = []
        now = _now()
        for take in takes:
            plan = self._cut_plan_for(take, requested_ms, edl_revision)
            self.conn.execute(
                "INSERT OR REPLACE INTO cut_plan_cache (tenant_id, "
                "music_take_id, edl_revision, requested_duration_ms, "
                "end_strategy, segments, transition, fade_in_ms, fade_out_ms, "
                "confidence, reason, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, take["music_take_id"],
                 int(edl_revision or 0), requested_ms, plan["end_strategy"],
                 _jcs(plan["segments"]), plan["transition"],
                 plan["fade_in_ms"], plan["fade_out_ms"], plan["confidence"],
                 plan["reason"], now))
            results.append(plan)

        return {"cue_id": cue_id, "edl_revision": edl_revision,
                "requested_duration_ms": requested_ms,
                "cut_plans": results}

    # --------------------------------- POST /music-selections
    def create_selection(self, tenant_id: str, body: dict,
                         command_id: str) -> dict:
        """§6.3 / §3.3。选定一个 take。

        三条不能松的边界：

        - `music_take_id` 必须属于**本租户**且属于这条 cue 的 job。这里不
          相信 body 里的 scene/cue，从 take 反查——body 说"这是第 2 条 cue 的
          take"而 take 实际属于第 7 条时，按 body 记会让检查点错位。
        - 只有 `selected` 的 selection 推进 `MusicCheckpoint`（§3.3）。
        - 旧的 `selected` 变 `superseded`，不是删除。历史要能回答"当时用的是
          哪一版"。
        """
        take_id = body.get("music_take_id")
        selected_by = body.get("selected_by")
        if not take_id:
            raise ServiceError(422, "INVALID_BODY", "music_take_id required")
        if not selected_by:
            # `ms_selected_has_decider` 的 DDL 会把这件事变成 IntegrityError
            # （500）。这里先给人话：谁选的必须有名字。
            raise ServiceError(
                422, "SELECTED_BY_REQUIRED",
                "selected_by is required; a selection always has a decider")

        def produce() -> dict:
            take = _row(self.conn.execute(
                "SELECT * FROM music_take WHERE tenant_id=? AND music_take_id=?",
                (tenant_id, take_id)).fetchone())
            if take is None:
                # F28：跨租户的 take 与不存在同一种回答。
                raise ServiceError(404, "MUSIC_TAKE_NOT_FOUND",
                                   f"no take {take_id}")
            job = self._job(tenant_id, take["music_job_id"])
            if job["state"] not in ("CANDIDATES_READY",
                                    "RECONCILED_CANDIDATES"):
                raise ServiceError(
                    409, "JOB_NOT_SELECTABLE",
                    f"job is {job['state']}; only a job with ready candidates "
                    "can be selected from")
            if take["state"] == "REJECTED":
                raise ServiceError(409, "TAKE_REJECTED",
                                   "a rejected take cannot be selected")
            # 时点 3 的复查：选定之前也要确认许可仍可用。选了一个已失效的
            # take 之后，检查点就已经前进了，撤回要重新选。
            take = self.recheck_take(tenant_id, take_id)
            if take.get("rights_state") != "usable":
                raise ServiceError(409, "TAKE_NOT_USABLE",
                                   "take rights are not usable")

            now = _now()
            # 旧的当前选择变 `superseded`，**不删除**：历史要能回答"当时用的
            # 是哪一版"。`cue_selection` 没有 `updated_at` 列，所以这里只改
            # `state`；新行自己带 `created_at`。
            self.conn.execute(
                "UPDATE cue_selection SET state='superseded' "
                "WHERE tenant_id=? AND scene_lineage_id=? AND cue_index=? "
                "AND state IN ('pending','selected')",
                (tenant_id, take["scene_lineage_id"],
                 int(take["cue_index"])))

            selection_id = _new_id("selection")
            self.conn.execute(
                "INSERT INTO cue_selection (selection_id, tenant_id, "
                "scene_lineage_id, cue_index, cue_id, cue_revision, "
                "music_job_id, music_take_id, state, selected_by, "
                "selection_kind, reason, created_at, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?,'selected',?,?,?,?,?)",
                (selection_id, tenant_id, take["scene_lineage_id"],
                 int(take["cue_index"]), take["cue_id"],
                 # `music_take` 上没有 `cue_revision` 列：take 是从 job 派生
                 # 的，版本号在 job 上。用 take 上的字段会 KeyError。
                 int(job["cue_revision"]), take["music_job_id"], take_id,
                 selected_by, body.get("selection_kind") or "human",
                 body.get("reason"), now, now))

            # 同组其它 take 标 SUPERSEDED？**不。** 未选中的候选仍然是 READY：
            # 导演可能第二天换成第二个，而"没被选"不等于"被否"。
            self.conn.execute(
                "UPDATE music_take SET state='SELECTED' WHERE "
                "music_take_id=?", (take_id,))

            seq = self._advance_checkpoint(
                tenant_id=tenant_id, scene_lineage_id=take["scene_lineage_id"],
                cue_index=int(take["cue_index"]), take_id=take_id,
                selection_id=selection_id, job_id=job["music_job_id"])
            # 选定闭合 awaiting_selection 缺口。
            self.conn.execute(
                "UPDATE music_gap SET state='closed', closed_by_take_id=?, "
                "updated_at=? WHERE tenant_id=? AND cue_id=? AND state='open' "
                "AND kind IN ('awaiting_selection','all_candidates_rejected')",
                (take_id, now, tenant_id, take["cue_id"]))

            outbox = self.outbox
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_CUE_SELECTED,
                subject=f"music-selection/{selection_id}",
                tenant_id=tenant_id, home_cell="music",
                aggregate_id=take["scene_lineage_id"], sequence=seq,
                correlation_id=selection_id,
                data={"selection_id": selection_id,
                      "scene_lineage_id": take["scene_lineage_id"],
                      "cue_index": int(take["cue_index"]),
                      "cue_id": take["cue_id"],
                      "music_take_id": take_id, "selected_by": selected_by,
                      "selection_kind": body.get("selection_kind") or "human"},
                schema_version=SCHEMA_VERSION))
            return {"selection_id": selection_id, "state": "selected",
                    "music_take_id": take_id, "selected_by": selected_by,
                    "checkpoint_seq": seq}

        return self._command(tenant_id, command_id, produce)

    # ------------------- POST /music-selections/{id}:reject-all
    def reject_all(self, tenant_id: str, selection_id: str, body: dict,
                   command_id: str) -> dict:
        """§3.3 / F21。全部候选被否 → `rejected_all` + 缺口。

        这是**显式状态**而不是"还没选"：不记录它，系统会一直等下去，或
        在某个超时后替导演挑一个。显式拒绝才能触发"需要新 job"。
        """
        def produce() -> dict:
            selection = _row(self.conn.execute(
                "SELECT * FROM cue_selection WHERE tenant_id=? AND "
                "selection_id=?", (tenant_id, selection_id)).fetchone())
            if selection is None:
                raise ServiceError(404, "SELECTION_NOT_FOUND",
                                   f"no selection {selection_id}")
            if selection["state"] == "rejected_all":
                return {"selection_id": selection_id, "state": "rejected_all",
                        "already": True}
            if selection["state"] not in ("pending", "selected"):
                raise ServiceError(409, "SELECTION_NOT_CURRENT",
                                   f"selection is {selection['state']}")
            if selection["state"] == "selected":
                # 已经选定并推进过检查点，再"全部否决"就自相矛盾。
                raise ServiceError(
                    409, "SELECTION_ALREADY_SELECTED",
                    "cannot reject all after a take was selected; supersede it "
                    "with a new selection instead")

            now = _now()
            self.conn.execute(
                "UPDATE cue_selection SET state='rejected_all', "
                "reason=?, decided_at=? WHERE selection_id=?",
                (body.get("reason"), now, selection_id))
            self.conn.execute(
                "UPDATE music_take SET state='REJECTED' WHERE tenant_id=? AND "
                "music_job_id=? AND state IN ('READY','SELECTED')",
                (tenant_id, selection["music_job_id"]))
            outbox = self.outbox
            self._record_gap(
                tenant_id=tenant_id,
                scene_lineage_id=selection["scene_lineage_id"],
                cue_index=int(selection["cue_index"]),
                cue_id=selection["cue_id"], kind="all_candidates_rejected",
                detail="all candidates were rejected; a new music job is needed",
                source_job_id=selection["music_job_id"], outbox=outbox)
            return {"selection_id": selection_id, "state": "rejected_all",
                    "music_job_id": selection["music_job_id"]}

        return self._command(tenant_id, command_id, produce)

    # ---------------------------------- POST /music-gaps/{id}:waive
    def waive_gap(self, tenant_id: str, gap_id: str, body: dict,
                  command_id: str) -> dict:
        """§6.3。**S34 不自己豁免自己**：必须带 S40 的裁决引用。"""
        decision = body.get("decision_ref") or {}
        if not isinstance(decision, dict) or decision.get("service_id") != "S40" \
                or not decision.get("id"):
            raise ServiceError(
                422, "DECISION_REF_REQUIRED",
                "waiving a music gap requires an S40 decision_ref; S34 never "
                "waives its own gap")

        def produce() -> dict:
            gap = _row(self.conn.execute(
                "SELECT * FROM music_gap WHERE tenant_id=? AND gap_id=?",
                (tenant_id, gap_id)).fetchone())
            if gap is None:
                raise ServiceError(404, "MUSIC_GAP_NOT_FOUND",
                                   f"no gap {gap_id}")
            if gap["state"] != "open":
                raise ServiceError(409, "GAP_NOT_OPEN",
                                   f"gap is {gap['state']}, cannot waive")
            now = _now()
            self.conn.execute(
                "UPDATE music_gap SET state='waived', waived_decision_id=?, "
                "updated_at=? WHERE gap_id=?",
                (_jcs(decision), now, gap_id))
            outbox = self.outbox
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_GAP_CLOSED,
                subject=f"music-gap/{gap_id}", tenant_id=tenant_id,
                home_cell="music", aggregate_id=gap["scene_lineage_id"],
                sequence=1, correlation_id=gap_id,
                data={"gap_id": gap_id, "closed_as": "waived",
                      "decision_ref": decision},
                schema_version=SCHEMA_VERSION))
            return {"gap_id": gap_id, "state": "waived",
                    "waived_decision_id": decision["id"]}

        return self._command(tenant_id, command_id, produce)

    # -------------------------------- POST /music-jobs/{id}:cancel
    def cancel_job(self, tenant_id: str, job_id: str, body: dict,
                   command_id: str) -> dict:
        def produce() -> dict:
            job = self._job(tenant_id, job_id)
            if job["state"] in TERMINAL_STATES:
                raise ServiceError(409, "JOB_ALREADY_TERMINAL",
                                   f"job is {job['state']}")
            outbox = self.outbox
            candidates = [dict(r) for r in self.conn.execute(
                "SELECT * FROM music_job_candidate WHERE music_job_id=? AND "
                "attempt_no=? AND state IN ('SUBMITTED','WAITING')",
                (job_id, int(job["business_attempt_no"]))).fetchall()]
            unconfirmed = []
            for candidate in candidates:
                try:
                    self.canceler(candidate["provider_job_id"],
                                  body.get("reason"))
                except (ServiceError, ConnectionError):
                    # 一份没确认就不能说"整组停了"。**进 WAITING_UNKNOWN，
                    # 不标 CANCELED**——标了就断言"确定没在做"，而它可能
                    # 正在做，并且可能照样收费。
                    unconfirmed.append(candidate["provider_job_id"])
            if unconfirmed:
                job = self._transition(
                    job, "WAITING_UNKNOWN", reason_code="CANCEL_UNCONFIRMED",
                    detail="provider did not confirm cancellation for "
                           + ",".join(unconfirmed), outbox=outbox)
                return {"music_job_id": job_id, "state": job["state"],
                        "reconciled": False,
                        "unconfirmed_provider_jobs": unconfirmed,
                        "note": "cancellation unconfirmed; must reconcile "
                                "before any resubmit"}
            self.conn.execute(
                "UPDATE music_job_candidate SET state='CANCELED', updated_at=? "
                "WHERE music_job_id=? AND attempt_no=? AND state IN "
                "('SUBMITTED','WAITING')",
                (_now(), job_id, int(job["business_attempt_no"])))
            job = self._transition(
                job, "CANCELED",
                reason_code=body.get("reason") or "REQUESTED", outbox=outbox)
            return {"music_job_id": job_id, "state": "CANCELED"}

        return self._command(tenant_id, command_id, produce)

    # --------------------------------- POST /music-jobs/{id}:retry
    def retry_job(self, tenant_id: str, job_id: str, body: dict,
                  command_id: str) -> dict:
        """§6.3。只有 `FAILED` / `RECONCILED_FAILED` 能重试（默认 409）。

        `POLICY_BLOCKED` 要等策略改；`WAITING_UNKNOWN` 必须先对账（盲目重提
        付费任务正是 AGENTS.md 的红线）；`GAP_RECORDED` 是"这条 cue 缺了"的
        事实，不是可重试的失败。
        """
        def produce() -> dict:
            job = self._job(tenant_id, job_id)
            if job["state"] not in RETRYABLE_STATES:
                raise ServiceError(
                    409, "JOB_NOT_RETRYABLE",
                    f"job is {job['state']}; only {sorted(RETRYABLE_STATES)} "
                    "can be retried", details={"state": job["state"]})
            self.conn.execute(
                "UPDATE music_job SET business_attempt_no=business_attempt_no+1, "
                "variation_nonce=?, failure_code=NULL, failure_detail=NULL, "
                "updated_at=? WHERE music_job_id=?",
                (uuid.uuid4().hex, _now(), job_id))
            job = self._job(tenant_id, job_id)
            outbox = self.outbox
            job = self._transition(job, "PREPARING", reason_code="RETRY",
                                   outbox=outbox)
            cue_rev = _row(self.conn.execute(
                "SELECT * FROM music_cue_revision WHERE tenant_id=? AND "
                "cue_id=? AND cue_revision=?",
                (tenant_id, job["cue_id"],
                 int(job["cue_revision"]))).fetchone())
            if cue_rev is None:
                raise ServiceError(409, "CUE_REVISION_MISSING",
                                   "the cue revision this job was created "
                                   "from no longer exists")
            return self._submit(job, cue_rev, outbox,
                                fingerprint=job["content_fingerprint"],
                                requested_ms=int(job["requested_duration_ms"]))

        return self._command(tenant_id, command_id, produce)

    # ================================================ 许可复查（§4.4.4）
    def recheck_take(self, tenant_id: str, take_id: str) -> dict:
        """**只收紧，永不放松。** usable → void 可以；void → usable 不可以。

        后者需要一次新的生成意图，不是一次读取。把"许可恢复了"直接改回
        usable，等于让一个曾经被判定不可用的音频重新进入成片，而没有任何人
        为这个决定负责。
        """
        take = _row(self.conn.execute(
            "SELECT * FROM music_take WHERE tenant_id=? AND music_take_id=?",
            (tenant_id, take_id)).fetchone())
        if take is None:
            raise ServiceError(404, "MUSIC_TAKE_NOT_FOUND", f"no take {take_id}")
        if take["rights_state"] == "void":
            return {**take, "rights_checked": False}

        binding = _row(self.conn.execute(
            "SELECT * FROM license_binding WHERE license_binding_id=?",
            (take["license_binding_id"],)).fetchone())
        if binding is None:
            return {**take, "rights_checked": False}
        try:
            resolved = self.rights_resolver(
                _loads(binding["policy_ref"], {}), tenant_id)
        except (ServiceError, ConnectionError):
            # 查不到许可 ≠ 许可失效。保持现状，并如实报告"这次没查到"。
            return {**take, "rights_state": take["rights_state"],
                    "rights_checked": False}
        if not isinstance(resolved, dict):
            return {**take, "rights_state": take["rights_state"],
                    "rights_checked": False}

        reasons = _binding_unusable_reasons(resolved)
        expires_at = resolved.get("sync_expires_at")
        if expires_at and resolved.get("sync_status") == "licensed":
            try:
                if _parse_ts(expires_at) <= datetime.now(timezone.utc):
                    reasons.append("sync_expired")
            except (TypeError, ValueError):
                reasons.append("sync_expiry_unparseable")
        blocking = [r for r in reasons if r in BLOCKING_RIGHTS_REASONS]
        if not blocking:
            return {**take, "rights_state": take["rights_state"],
                    "rights_checked": True}

        with Transaction(self.conn):
            outbox = self.outbox
            self.conn.execute(
                "UPDATE music_take SET rights_state='void', rights_checked_at=? "
                "WHERE music_take_id=?", (_now(), take_id))
            # F17：take 失效与缺口必须同事务，否则会出现"take 已经不能用了，
            # 但没人知道这条 cue 缺了"——那正是 S41 completeness 会漏掉的一格。
            self._record_gap(
                tenant_id=tenant_id,
                scene_lineage_id=take["scene_lineage_id"],
                cue_index=int(take["cue_index"]), cue_id=take["cue_id"],
                kind="rights_lapsed",
                detail="license tuple became unusable: " + ",".join(blocking),
                source_job_id=take["music_job_id"], outbox=outbox)
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_RIGHTS_LAPSED,
                subject=f"music-take/{take_id}", tenant_id=tenant_id,
                home_cell="music", aggregate_id=take_id, sequence=1,
                correlation_id=take_id,
                data={"music_take_id": take_id, "cue_id": take["cue_id"],
                      "reasons": blocking},
                schema_version=SCHEMA_VERSION))
        return {**self.get_take(tenant_id, take_id), "rights_checked": True}

    def on_policy_revision_published(self, envelope: dict) -> dict:
        """S02 `policy.revision.published` → 批量重查受影响 take（时点 4）。

        **只重查受影响的**：拿事件的 policy id 去比 binding 的 `policy_ref.id`。
        全表重查会把一次无关策略变更变成一次全站许可扫描。
        """
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        policy_id = data.get("policy_id") or (data.get("policy_ref") or {}).get("id")
        if not tenant_id or not policy_id:
            return {"ignored": True, "reason": "not_a_policy_event"}
        rows = self.conn.execute(
            "SELECT DISTINCT t.music_take_id FROM music_take t "
            "JOIN license_binding b ON b.license_binding_id = t.license_binding_id "
            "WHERE t.tenant_id=? AND json_extract(b.policy_ref, '$.id')=?",
            (tenant_id, policy_id)).fetchall()
        lapsed = []
        for row in rows:
            checked = self.recheck_take(tenant_id, row["music_take_id"])
            if checked.get("rights_state") != "usable":
                lapsed.append(row["music_take_id"])
        return {"tenant_id": tenant_id, "policy_id": policy_id,
                "checked": len(rows), "lapsed": lapsed}

    # ================================================ 对白提示透传
    def record_dialogue_hint(self, tenant_id: str, cue_id: str,
                             hint, source: str) -> dict:
        """§4.5 方案 C：**收到就存、就透出，不解读**。

        `level ∈ {sparse, normal, dense}` 只校验形状，不参与任何决策——S34
        不做 ducking，混音是 S42 的唯一权威。自己压过的 BGM 会被 S42 再压一次。
        """
        if source not in ("s37_segments", "s06_script", "provided", "unknown"):
            raise ServiceError(422, "INVALID_HINT_SOURCE",
                               f"unknown dialogue hint source {source!r}")
        if hint is not None and not isinstance(hint, list):
            raise ServiceError(422, "INVALID_DIALOGUE_HINT",
                               "dialogue_density_hint must be an array or null")
        normalized = []
        for item in (hint or []):
            if not isinstance(item, dict):
                raise ServiceError(422, "INVALID_DIALOGUE_HINT",
                                   "each hint span must be an object")
            try:
                start, end = int(item["start_ms"]), int(item["end_ms"])
            except (KeyError, TypeError, ValueError):
                raise ServiceError(422, "INVALID_DIALOGUE_HINT",
                                   "spans need integer start_ms and end_ms")
            if end <= start:
                raise ServiceError(422, "INVALID_DIALOGUE_HINT",
                                   "span end_ms must be after start_ms")
            level = item.get("level")
            if level not in ("sparse", "normal", "dense"):
                raise ServiceError(422, "INVALID_DIALOGUE_HINT",
                                   "level must be sparse/normal/dense")
            normalized.append({"start_ms": start, "end_ms": end,
                               "level": level})
        self.conn.execute(
            "INSERT INTO dialogue_hint (tenant_id, cue_id, hint, source, "
            "updated_at) VALUES (?,?,?,?,?) ON CONFLICT (tenant_id, cue_id) "
            "DO UPDATE SET hint=excluded.hint, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (tenant_id, cue_id, _jcs(normalized) if hint is not None else None,
             source, _now()))
        return {"cue_id": cue_id, "source": source,
                "span_count": len(normalized)}

    # ================================================ S26 状态事件
    def on_generation_state_changed(self, envelope: dict) -> dict:
        """S26 `generation.job.state-changed`，按 `provider_job_id` 归到候选行。

        **防御式映射**：`state` 在契约里是普通 string（16 个值只写在描述里，
        不是闭集），所以不认识的一律不动，返回 `ignored`。把未知状态当成功
        是这条链路最贵的一种错。
        """
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        provider_job_id = data.get("provider_job_id") or data.get("job_id")
        provider_state = data.get("state")
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_a_generation_job_event"}

        # 事件必须按租户过滤：`provider_job_id` 由供应商分配，不是全局唯一的
        # 名字空间，拿它单独查等于允许一次跨租户的状态注入。
        candidate = _row(self.conn.execute(
            "SELECT c.* FROM music_job_candidate c "
            "JOIN music_job j ON j.music_job_id = c.music_job_id "
            "WHERE j.tenant_id=? AND c.provider_job_id=?",
            (tenant_id, provider_job_id)).fetchone())
        if candidate is None:
            return {"ignored": True, "reason": "no_matching_music_candidate"}
        job = self._job(tenant_id, candidate["music_job_id"])
        if job["state"] in TERMINAL_STATES:
            return {"ignored": True, "reason": "job_already_terminal"}

        if provider_state == "SUBMISSION_UNKNOWN":
            with Transaction(self.conn):
                outbox = self.outbox
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='UNKNOWN', "
                    "updated_at=? WHERE music_job_id=? AND candidate_index=? "
                    "AND attempt_no=?",
                    (_now(), job["music_job_id"],
                     int(candidate["candidate_index"]),
                     int(candidate["attempt_no"])))
                # F25：冻结。**保留额度、不重提**，直到对账给出确定答案。
                new = self._transition(
                    job, "WAITING_UNKNOWN", reason_code="SUBMISSION_UNKNOWN",
                    detail="provider outcome unknown; must reconcile before "
                           "any resubmit", outbox=outbox)
            return {"music_job_id": new["music_job_id"], "state": new["state"]}

        if provider_state in ("QUEUED", "RUNNING"):
            with Transaction(self.conn):
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='WAITING', "
                    "updated_at=? WHERE music_job_id=? AND candidate_index=? "
                    "AND attempt_no=?",
                    (_now(), job["music_job_id"],
                     int(candidate["candidate_index"]),
                     int(candidate["attempt_no"])))
                if job["state"] == "SUBMITTED":
                    job = self._transition(job, "WAITING_PROVIDER",
                                           outbox=self.outbox)
            return {"music_job_id": job["music_job_id"], "state": job["state"]}

        if provider_state in ("PROVIDER_FAILED", "REJECTED_BUDGET",
                              "REJECTED_QUOTA", "ACCOUNT_UNAUTHORIZED",
                              "CANCELED"):
            reason = data.get("reason_code") or provider_state
            code = _failure_code(provider_state, reason)
            with Transaction(self.conn):
                outbox = self.outbox
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='FAILED', "
                    "failure_code=?, updated_at=? WHERE music_job_id=? AND "
                    "attempt_no=?", (code, _now(), job["music_job_id"],
                                     int(job["business_attempt_no"])))
                # 一份失败 = 凑不齐 N 份 = 整组作废（F10/F11）。**不**把剩
                # 下的候选当成"聊胜于无"收下——"3 个候选"已经承诺出去了。
                self.conn.execute(
                    "UPDATE music_job SET failure_code=?, failure_detail=?, "
                    "updated_at=? WHERE music_job_id=?",
                    (code, f"provider reported {provider_state}/{reason}",
                     _now(), job["music_job_id"]))
                new = self._transition(job, "FAILED", reason_code=code,
                                       detail=f"candidate "
                                              f"{candidate['candidate_index']} "
                                              f"failed: {provider_state}",
                                       outbox=outbox)
            return {"music_job_id": new["music_job_id"], "state": new["state"],
                    "reason_code": code}

        return {"ignored": True,
                "reason": f"unmapped_provider_state:{provider_state}"}

    # ================================================ S28 归档事件
    def on_artifact_ingested(self, envelope: dict) -> dict:
        """S28 `artifact.ingested` → 收齐 N 份归档 → ANALYZING → CANDIDATES_READY。

        **C4**：这个事件没有 payload schema，`natural_duration_ms` 只能从
        `artifact_ref` / `data` 里尽力取。取不到就是 F12 那一类：
        **不当 0、也不当"未知但先放着"**——0ms 的素材会让每个 CutPlan 都变成
        `unplaceable`，看起来像一个正常的缺口，实际是上游漏了字段。

        归档结果先停在 `music_job_candidate` 上。§3.4 要求 MusicTake 与
        BeatGrid / LoopPointSet **同一个事务**，所以不可能边收边写 take。
        """
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        artifact_ref = data.get("artifact_ref") or {}
        provider_job_id = (data.get("source") or {}).get("provider_job_id") \
            or data.get("provider_job_id")
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_shaped_for_music"}

        candidate = _row(self.conn.execute(
            "SELECT c.* FROM music_job_candidate c "
            "JOIN music_job j ON j.music_job_id = c.music_job_id "
            "WHERE j.tenant_id=? AND c.provider_job_id=?",
            (tenant_id, provider_job_id)).fetchone())
        if candidate is None:
            return {"ignored": True, "reason": "no_matching_music_candidate"}
        job = self._job(tenant_id, candidate["music_job_id"])
        if job["state"] in TERMINAL_STATES:
            return {"ignored": True, "reason": "job_already_terminal"}

        duration_ms = data.get("natural_duration_ms", data.get("duration_ms"))
        artifact_id = artifact_ref.get("id")
        content_hash = artifact_ref.get("content_hash")
        if not artifact_id or not content_hash:
            artifact_id = artifact_id or data.get("artifact_id")
            content_hash = content_hash or data.get("content_hash")

        with Transaction(self.conn):
            outbox = self.outbox
            # F13：URL 过期可以重取，不是重新生成。所以这里只把候选留成
            # WAITING，job 停在 INGESTING，等 S28 重试。
            if not artifact_id or not content_hash:
                new = self._transition(
                    job, "INGESTING", reason_code="ARTIFACT_REF_INCOMPLETE",
                    detail="artifact.ingested carried no usable artifact_ref",
                    outbox=outbox) if job["state"] != "INGESTING" \
                    else self._job(tenant_id, job["music_job_id"])
                return {"music_job_id": new["music_job_id"],
                        "state": new["state"],
                        "note": "artifact ref incomplete; waiting for re-fetch"}

            if not isinstance(duration_ms, int) or duration_ms <= 0:
                # 整组作废：没有时长，这份音频连"能不能放下"都答不了。
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='FAILED', "
                    "failure_code='DURATION_UNKNOWN', updated_at=? "
                    "WHERE music_job_id=? AND attempt_no=?",
                    (_now(), job["music_job_id"],
                     int(job["business_attempt_no"])))
                self.conn.execute(
                    "UPDATE music_job SET failure_code='DURATION_UNKNOWN', "
                    "failure_detail=?, updated_at=? WHERE music_job_id=?",
                    ("artifact.ingested carried no natural_duration_ms (C4)",
                     _now(), job["music_job_id"]))
                new = self._transition(job, "FAILED",
                                       reason_code="DURATION_UNKNOWN",
                                       detail="artifact carried no usable "
                                              "duration", outbox=outbox)
                return {"music_job_id": new["music_job_id"],
                        "state": new["state"], "reason_code": "DURATION_UNKNOWN"}

            # S28 是 bytes 完整性的权威。这里问一次，问不到（None）就继续用
            # 事件里的值，问到了 False 才按 hash 不符处理——不把"没问成"
            # 当成"不一致"。
            verified = None
            try:
                verified = self.artifact_check(
                    {"service_id": "S28", "id": artifact_id,
                     "revision": artifact_ref.get("revision"),
                     "content_hash": content_hash})
            except (ServiceError, ConnectionError):
                verified = None
            if verified is False:
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='FAILED', "
                    "failure_code='HASH_MISMATCH', updated_at=? "
                    "WHERE music_job_id=? AND attempt_no=?",
                    (_now(), job["music_job_id"],
                     int(job["business_attempt_no"])))
                self.conn.execute(
                    "UPDATE music_job SET failure_code='HASH_MISMATCH', "
                    "updated_at=? WHERE music_job_id=?",
                    (_now(), job["music_job_id"]))
                new = self._transition(job, "FAILED",
                                       reason_code="HASH_MISMATCH",
                                       detail="S28 reports the artifact content "
                                              "hash does not match",
                                       outbox=outbox)
                return {"music_job_id": new["music_job_id"],
                        "state": new["state"], "reason_code": "HASH_MISMATCH"}

            self.conn.execute(
                "UPDATE music_job_candidate SET state='ARCHIVED', "
                "artifact_ref=?, natural_duration_ms=?, sample_rate_hz=?, "
                "channel_count=?, container_format=?, content_hash=?, "
                "updated_at=? WHERE music_job_id=? AND candidate_index=? AND "
                "attempt_no=?",
                (_jcs({"service_id": "S28", "id": artifact_id,
                       "revision": artifact_ref.get("revision"),
                       "content_hash": content_hash}),
                 int(duration_ms),
                 int(data.get("sample_rate_hz")
                     or artifact_ref.get("sample_rate_hz") or 48000),
                 int(data.get("channel_count")
                     or artifact_ref.get("channel_count") or 2),
                 data.get("container_format") or artifact_ref.get("container")
                 or "audio/wav",
                 content_hash, _now(), job["music_job_id"],
                 int(candidate["candidate_index"]),
                 int(candidate["attempt_no"])))

            if job["state"] == "WAITING_PROVIDER":
                job = self._transition(job, "INGESTING", outbox=outbox)

            pending = self.conn.execute(
                "SELECT COUNT(*) AS n FROM music_job_candidate WHERE "
                "music_job_id=? AND attempt_no=? AND state NOT IN "
                "('ARCHIVED','FAILED','CANCELED')",
                (job["music_job_id"],
                 int(job["business_attempt_no"]))).fetchone()["n"]
            archived = self.conn.execute(
                "SELECT COUNT(*) AS n FROM music_job_candidate WHERE "
                "music_job_id=? AND attempt_no=? AND state='ARCHIVED'",
                (job["music_job_id"],
                 int(job["business_attempt_no"]))).fetchone()["n"]
            if pending:
                return {"music_job_id": job["music_job_id"],
                        "state": job["state"], "archived": archived,
                        "note": "waiting for the remaining candidates"}

            if archived < int(job["candidate_count"]):
                # F11：候选数少于请求。**不交付半个候选集**。
                self.conn.execute(
                    "UPDATE music_job SET failure_code='PARTIAL_CANDIDATES', "
                    "failure_detail=?, updated_at=? WHERE music_job_id=?",
                    (f"{archived} of {job['candidate_count']} candidates "
                     "archived", _now(), job["music_job_id"]))
                new = self._transition(
                    job, "FAILED", reason_code="PARTIAL_CANDIDATES",
                    detail=f"{archived} of {job['candidate_count']} archived",
                    outbox=outbox)
                return {"music_job_id": new["music_job_id"],
                        "state": new["state"],
                        "reason_code": "PARTIAL_CANDIDATES"}

            job = self._transition(job, "ANALYZING", outbox=outbox)
            return self._analyze(job, outbox)

    # ------------------------------------------------ ANALYZING → READY
    def _analyze(self, job: dict, outbox: Outbox) -> dict:
        """F14/F15/F16/F17 的落点。**take + BeatGrid + LoopPointSet 一个事务。**

        三处不能偷懒：

        - 低置信度的网格**照样入库**（`low_confidence=1`），因为它仍然允许
          `phrase_trim` 与 `loop_stitch`（§2.4）；只有连 BPM / 小节线都拿不到
          才是"这份音频没法定位"，该候选不登记。
        - `LoopPointSet` 为空**不是失败**（F16）：它只让 `loop_stitch` 不可用，
          其余策略照旧。
        - 一份都剩不下才记 `beat_unanalyzable` 缺口（F14）；剩下一份以上就
          正常 `CANDIDATES_READY`（F15）。
        """
        candidates = [dict(r) for r in self.conn.execute(
            "SELECT * FROM music_job_candidate WHERE music_job_id=? AND "
            "attempt_no=? AND state='ARCHIVED' ORDER BY candidate_index",
            (job["music_job_id"],
             int(job["business_attempt_no"]))).fetchall()]

        cue_rev = _row(self.conn.execute(
            "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
            "AND cue_revision=?",
            (job["tenant_id"], job["cue_id"],
             int(job["cue_revision"]))).fetchone())
        binding = _row(self.conn.execute(
            "SELECT * FROM license_binding WHERE license_binding_id=?",
            (job["license_binding_id"],)).fetchone())
        blocking = self._ensure_binding_usable(binding or {}) if binding else \
            ["license_binding_missing"]

        created = []
        skipped = []
        for candidate in candidates:
            artifact_ref = _loads(candidate["artifact_ref"], {})
            probe = None
            try:
                probe = self.analysis_probe(artifact_ref,
                                            int(candidate["natural_duration_ms"]))
            except (ServiceError, ConnectionError):
                probe = None
            if not isinstance(probe, dict) or not probe.get("bpm") \
                    or not (probe.get("downbeats_ms") or []):
                # 拿不到 BPM 或一条小节线 → 这份音频没法定位，不登记（F15）。
                skipped.append({"candidate_index":
                                int(candidate["candidate_index"]),
                                "reason": "BEAT_UNANALYZABLE"})
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='FAILED', "
                    "failure_code='BEAT_UNANALYZABLE', updated_at=? "
                    "WHERE music_job_id=? AND candidate_index=? AND attempt_no=?",
                    (_now(), job["music_job_id"],
                     int(candidate["candidate_index"]),
                     int(candidate["attempt_no"])))
                continue

            instrumental_required = bool(int(cue_rev["instrumental_only"]))
            vocal_presence = bool(probe.get("vocal_presence"))
            if instrumental_required and vocal_presence:
                # F9 的归档后版本：探测到人声而要求无人声。DB 的
                # `mt_no_unwanted_vocal` 也会拒绝，这里先给出原因。
                skipped.append({"candidate_index":
                                int(candidate["candidate_index"]),
                                "reason": "NOT_INSTRUMENTAL"})
                self.conn.execute(
                    "UPDATE music_job_candidate SET state='FAILED', "
                    "failure_code='NOT_INSTRUMENTAL', updated_at=? "
                    "WHERE music_job_id=? AND candidate_index=? AND attempt_no=?",
                    (_now(), job["music_job_id"],
                     int(candidate["candidate_index"]),
                     int(candidate["attempt_no"])))
                continue
            if blocking:
                # F17：归档后许可失效。take 标 void 并**不登记为候选**。
                skipped.append({"candidate_index":
                                int(candidate["candidate_index"]),
                                "reason": "RIGHTS_LAPSED"})
                self._record_gap(
                    tenant_id=job["tenant_id"],
                    scene_lineage_id=job["scene_lineage_id"],
                    cue_index=int(job["cue_index"]), cue_id=job["cue_id"],
                    kind="rights_lapsed",
                    detail="license tuple unusable at analyze time: "
                           + ",".join(blocking),
                    source_job_id=job["music_job_id"], outbox=outbox)
                continue

            created.append(self._write_take(
                job=job, cue_rev=cue_rev, candidate=candidate, probe=probe,
                blocking=blocking))

        if not created:
            # F14：一份都没剩下。记缺口，不交付空候选集。
            self._record_gap(
                tenant_id=job["tenant_id"],
                scene_lineage_id=job["scene_lineage_id"],
                cue_index=int(job["cue_index"]), cue_id=job["cue_id"],
                kind="beat_unanalyzable",
                detail="every candidate failed analysis: " + _jcs(skipped),
                source_job_id=job["music_job_id"], outbox=outbox)
            new = self._transition(job, "GAP_RECORDED",
                                   reason_code="BEAT_UNANALYZABLE",
                                   detail=_jcs(skipped), outbox=outbox)
            return {"music_job_id": new["music_job_id"], "state": new["state"],
                    "reason_code": "BEAT_UNANALYZABLE", "skipped": skipped}

        # N 份里活下来 M 份 → 候选数不符 → 仍是 F10/F11 那一类。
        if len(created) != int(job["candidate_count"]):
            self.conn.execute(
                "UPDATE music_job SET failure_code='PARTIAL_CANDIDATES', "
                "failure_detail=?, updated_at=? WHERE music_job_id=?",
                (f"{len(created)} of {job['candidate_count']} candidates "
                 f"survived analysis; skipped={_jcs(skipped)}",
                 _now(), job["music_job_id"]))
            new = self._transition(
                job, "FAILED", reason_code="PARTIAL_CANDIDATES",
                detail=f"{len(created)} of {job['candidate_count']} usable",
                outbox=outbox)
            return {"music_job_id": new["music_job_id"], "state": new["state"],
                    "reason_code": "PARTIAL_CANDIDATES", "skipped": skipped}

        # ---- CANDIDATES_READY：写 selection（必要时自动选定）→ ready 事件
        selection = self._open_selection(job, created, outbox)
        new = self._transition(job, "CANDIDATES_READY", outbox=outbox)
        self._publish_ready(new, created, selection, outbox)
        return {"music_job_id": new["music_job_id"], "state": new["state"],
                "music_take_ids": [t["music_take_id"] for t in created],
                "selection_id": selection["selection_id"],
                "selection_state": selection["state"],
                "skipped": skipped}

    def _write_take(self, *, job: dict, cue_rev: dict, candidate: dict,
                    probe: dict, blocking: list) -> dict:
        """一份 take + 它的 BeatGrid + LoopPointSet，**同一个事务**（§3.4）。

        缺了网格的 take 是"看起来有音乐、但放不进去"的那种坏数据：它会在
        `CANDIDATES_READY` 之后很久才表现为一次 `unplaceable`，那时已经看不
        出是哪一步漏了。所以三张表要么一起写，要么都不写。
        """
        take_id = _new_id("take")
        now = _now()
        artifact_ref = _loads(candidate["artifact_ref"], {})
        fingerprint = content_fingerprint(
            music_intent=cue_rev["music_intent"], mood=cue_rev["mood"],
            mood_intensity=float(cue_rev["mood_intensity"]),
            instrumentation=cue_rev["instrumentation"],
            tempo_low_bpm=cue_rev["tempo_hint_low_bpm"],
            tempo_high_bpm=cue_rev["tempo_hint_high_bpm"],
            key_hint=cue_rev["key_hint"],
            instrumental_only=bool(int(cue_rev["instrumental_only"])))

        hint = _row(self.conn.execute(
            "SELECT * FROM dialogue_hint WHERE tenant_id=? AND cue_id=?",
            (job["tenant_id"], job["cue_id"])).fetchone())
        hint_json = None if hint is None else hint["hint"]
        hint_source = "unknown" if hint is None else hint["source"]

        tail_kind = probe.get("tail_kind")
        if tail_kind not in TAIL_KINDS:
            # 检测器给不出可信的尾音类型时按 `unknown` 记，**不按 sustaining
            # 记**：后者会让 bar_extend 在一个干脆的终止拍上被放行。
            tail_kind = "unknown"

        self.conn.execute(
            "INSERT INTO music_take (music_take_id, tenant_id, music_job_id, "
            "candidate_index, cue_id, scene_lineage_id, cue_index, artifact_ref, "
            "natural_duration_ms, sample_rate_hz, channel_count, "
            "container_format, detect_bpm, detected_key, tail_kind, "
            "vocal_presence, instrumental_required, content_fingerprint, "
            "license_binding_id, rights_state, rights_checked_at, "
            "dialogue_density_hint, dialogue_density_source, provider_job_id, "
            "attempt_id, source_kind, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "'READY',?)",
            (take_id, job["tenant_id"], job["music_job_id"],
             int(candidate["candidate_index"]), job["cue_id"],
             job["scene_lineage_id"], int(job["cue_index"]), _jcs(artifact_ref),
             int(candidate["natural_duration_ms"]),
             int(candidate["sample_rate_hz"] or 48000),
             int(candidate["channel_count"] or 2),
             candidate["container_format"] or "audio/wav",
             probe.get("bpm"), probe.get("detected_key"), tail_kind,
             1 if probe.get("vocal_presence") else 0,
             1 if int(cue_rev["instrumental_only"]) else 0, fingerprint,
             job["license_binding_id"], "usable", now, hint_json, hint_source,
             candidate["provider_job_id"], candidate["attempt_id"] or "",
             "generated", now))

        confidence = float(probe.get("confidence") or 0.0)
        self.conn.execute(
            "INSERT INTO beat_grid (beat_grid_id, tenant_id, music_take_id, bpm, "
            "meter_num, meter_den, downbeat_ms, phrase_ms, confidence, "
            "low_confidence, detector_model, detector_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("beat"), job["tenant_id"], take_id, float(probe["bpm"]),
             int(probe.get("meter_num") or 4),
             int(probe.get("meter_den") or 4),
             _jcs(sorted(int(x) for x in probe["downbeats_ms"])),
             _jcs(sorted(int(x) for x in (probe.get("phrases_ms") or []))),
             confidence, 1 if confidence < BEAT_CONFIDENCE_THRESHOLD else 0,
             str(probe.get("detector_model") or "unknown"),
             str(probe.get("detector_version") or "unknown"), now))

        ranges = probe.get("loop_ranges") or []
        best = None
        for item in ranges:
            if isinstance(item, dict) and item.get("score") is not None:
                best = max(best or 0.0, min(max(float(item["score"]), 0.0), 1.0))
        # 空 ranges 也写一行（F16）：`LoopPointSet` 存在但为空，与"没探测过"
        # 是两件事；`loop_available` 由 `ranges` 是否非空推出（PMR-2）。
        self.conn.execute(
            "INSERT INTO loop_point_set (loop_point_set_id, tenant_id, "
            "music_take_id, ranges, best_seam_score, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (_new_id("loop"), job["tenant_id"], take_id, _jcs(ranges), best,
             now))

        return {"music_take_id": take_id,
                "candidate_index": int(candidate["candidate_index"]),
                "artifact_ref": artifact_ref,
                "natural_duration_ms": int(candidate["natural_duration_ms"]),
                "detect_bpm": probe.get("bpm"),
                "meter": f"{int(probe.get('meter_num') or 4)}/"
                         f"{int(probe.get('meter_den') or 4)}",
                "detected_key": probe.get("detected_key"),
                "tail_kind": tail_kind,
                "loop_available": bool(ranges),
                "rights_state": "usable"}

    def _open_selection(self, job: dict, takes: list, outbox: Outbox) -> dict:
        """§4.6：**只有 `candidate_count == 1` 才自动选定。**

        多候选时开一条 `pending`——没人选就是没人选，24h 后记
        `awaiting_selection` 缺口，**不替导演挑**。"哪个配乐更好"是创作决定。

        自动选定的 `selected_by` 必须写 `system:auto_single`，否则事后无法区分
        "忘了记"和"只有一个所以自动选了它"（DDL 的 `ms_selected_has_decider`
        会拒绝空值，这里给的是有意义的值）。
        """
        selection_id = _new_id("selection")
        now = _now()
        auto = int(job["candidate_count"]) == 1
        if auto:
            take = takes[0]
            self.conn.execute(
                "INSERT INTO cue_selection (selection_id, tenant_id, "
                "scene_lineage_id, cue_index, cue_id, cue_revision, "
                "music_job_id, music_take_id, state, selected_by, "
                "selection_kind, reason, created_at, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?,'selected','system:auto_single',"
                "'auto_single',?,?,?)",
                (selection_id, job["tenant_id"], job["scene_lineage_id"],
                 int(job["cue_index"]), job["cue_id"], int(job["cue_revision"]),
                 job["music_job_id"], take["music_take_id"],
                 "single candidate: selecting it is mechanical, not creative",
                 now, now))
            self.conn.execute(
                "UPDATE music_take SET state='SELECTED' WHERE music_take_id=?",
                (take["music_take_id"],))
            seq = self._advance_checkpoint(
                tenant_id=job["tenant_id"],
                scene_lineage_id=job["scene_lineage_id"],
                cue_index=int(job["cue_index"]),
                take_id=take["music_take_id"], selection_id=selection_id,
                job_id=job["music_job_id"])
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_CUE_SELECTED,
                subject=f"music-selection/{selection_id}",
                tenant_id=job["tenant_id"], home_cell="music",
                aggregate_id=job["scene_lineage_id"], sequence=seq,
                correlation_id=selection_id,
                data={"selection_id": selection_id,
                      "scene_lineage_id": job["scene_lineage_id"],
                      "cue_index": int(job["cue_index"]),
                      "cue_id": job["cue_id"],
                      "music_take_id": take["music_take_id"],
                      "selected_by": "system:auto_single",
                      "selection_kind": "auto_single"},
                schema_version=SCHEMA_VERSION))
            return {"selection_id": selection_id, "state": "selected",
                    "music_take_id": take["music_take_id"],
                    "checkpoint_seq": seq}

        self.conn.execute(
            "INSERT INTO cue_selection (selection_id, tenant_id, "
            "scene_lineage_id, cue_index, cue_id, cue_revision, music_job_id, "
            "music_take_id, state, selected_by, selection_kind, reason, "
            "created_at, decided_at) "
            "VALUES (?,?,?,?,?,?,?,NULL,'pending',NULL,NULL,?,?,NULL)",
            (selection_id, job["tenant_id"], job["scene_lineage_id"],
             int(job["cue_index"]), job["cue_id"], int(job["cue_revision"]),
             job["music_job_id"],
             f"{job['candidate_count']} candidates need a human decision", now))
        return {"selection_id": selection_id, "state": "pending",
                "music_take_id": None, "checkpoint_seq": None}

    def _advance_checkpoint(self, *, tenant_id: str, scene_lineage_id: str,
                            cue_index: int, take_id: str, selection_id: str,
                            job_id: str) -> int:
        """CAS 推进检查点。`checkpoint_seq` 是乐观锁那一位。

        并发两个 job 抢同一个 `(scene, cue_index)` 时只有一个能推进；另一个
        得到 409 并重读，而不是两边都写（后写的静默覆盖先写的）。
        """
        current = _row(self.conn.execute(
            "SELECT * FROM music_checkpoint WHERE tenant_id=? AND "
            "scene_lineage_id=? AND cue_index=?",
            (tenant_id, scene_lineage_id, int(cue_index))).fetchone())
        now = _now()
        if current is None:
            self.conn.execute(
                "INSERT INTO music_checkpoint (tenant_id, scene_lineage_id, "
                "cue_index, music_take_id, selection_id, checkpoint_seq, "
                "expected_revision, updated_by_job, updated_at) "
                "VALUES (?,?,?,?,?,1,0,?,?)",
                (tenant_id, scene_lineage_id, int(cue_index), take_id,
                 selection_id, job_id, now))
            return 1
        seq = int(current["checkpoint_seq"]) + 1
        cur = self.conn.execute(
            "UPDATE music_checkpoint SET music_take_id=?, selection_id=?, "
            "checkpoint_seq=?, expected_revision=expected_revision+1, "
            "updated_by_job=?, updated_at=? WHERE tenant_id=? AND "
            "scene_lineage_id=? AND cue_index=? AND checkpoint_seq=?",
            (take_id, selection_id, seq, job_id, now, tenant_id,
             scene_lineage_id, int(cue_index), int(current["checkpoint_seq"])))
        if cur.rowcount != 1:
            raise ServiceError(409, "CHECKPOINT_CAS_FAILED",
                               "concurrent checkpoint advance lost the race; "
                               "retry with a new selection", retryable=True)
        return seq

    def _publish_ready(self, job: dict, takes: list, selection: dict,
                       outbox: Outbox) -> None:
        """`production.music.ready`。**14 个顶层 + 每个候选 9 个，一个不多一个不少。**

        契约是 `additionalProperties: false` 且两个层级都列了 `required`。
        多带一个字段（比如 `music_intent` 原文、`attribution_text`、ISRC）会
        让整条事件被拒；而这些字段本来就**不该**进事件流：意图泄漏剧情，
        署名与 ISRC 是合同细节（§6.4）。
        """
        checkpoint = _row(self.conn.execute(
            "SELECT * FROM music_checkpoint WHERE tenant_id=? AND "
            "scene_lineage_id=? AND cue_index=?",
            (job["tenant_id"], job["scene_lineage_id"],
             int(job["cue_index"]))).fetchone())
        sequence = int(checkpoint["checkpoint_seq"]) if checkpoint else 1
        cue_rev = _row(self.conn.execute(
            "SELECT * FROM music_cue_revision WHERE tenant_id=? AND cue_id=? "
            "AND cue_revision=?",
            (job["tenant_id"], job["cue_id"],
             int(job["cue_revision"]))).fetchone())
        outbox.append(new_event(
            source=SOURCE, type=EVENT_MUSIC_READY,
            subject=f"music-cue/{job['cue_id']}", tenant_id=job["tenant_id"],
            home_cell="music", aggregate_id=job["scene_lineage_id"],
            sequence=sequence, correlation_id=job["music_job_id"],
            data={
                "schema_version": SCHEMA_VERSION,
                "music_job_id": job["music_job_id"],
                "cue_id": job["cue_id"],
                "cue_revision": int(job["cue_revision"]),
                "scene_lineage_id": job["scene_lineage_id"],
                "cue_index": int(job["cue_index"]),
                "mood": cue_rev["mood"],
                "mood_intensity": float(cue_rev["mood_intensity"]),
                "instrumentation": cue_rev["instrumentation"],
                "requested_duration_ms": int(job["requested_duration_ms"]),
                "candidates": [{
                    "music_take_id": take["music_take_id"],
                    "artifact_ref": take["artifact_ref"],
                    "natural_duration_ms": take["natural_duration_ms"],
                    "detect_bpm": take["detect_bpm"],
                    "meter": take["meter"],
                    "detected_key": take["detected_key"],
                    "tail_kind": take["tail_kind"],
                    "loop_available": take["loop_available"],
                    "rights_state": take["rights_state"],
                } for take in takes],
                "selection_state": selection["state"],
                "content_fingerprint": job["content_fingerprint"],
                "gap_state": "none",
            },
            schema_version=SCHEMA_VERSION))

    # ================================================ 超时与对账
    def sweep_selection_timeouts(self, now: str | None = None) -> dict:
        """F20 / §4.6：超过 24h 还是 `pending` → 记缺口 + 告警，**不自动选**。

        这是唯一一个不按 cue 逐个盯的检查；缺口的 `kind='awaiting_selection'`
        让 S41 的 completeness 能看见"这条 cue 还没有配乐基线"。
        """
        now_at = _parse_ts(now) if now else datetime.now(timezone.utc)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM cue_selection WHERE state='pending'").fetchall()]
        opened = []
        for selection in rows:
            try:
                created = _parse_ts(selection["created_at"])
            except (TypeError, ValueError):
                continue
            if (now_at - created).total_seconds() < SELECTION_TIMEOUT_SECONDS:
                continue
            existing = _row(self.conn.execute(
                "SELECT gap_id FROM music_gap WHERE tenant_id=? AND cue_id=? "
                "AND state='open' AND kind='awaiting_selection'",
                (selection["tenant_id"], selection["cue_id"])).fetchone())
            if existing is not None:
                continue
            with Transaction(self.conn):
                gap = self._record_gap(
                    tenant_id=selection["tenant_id"],
                    scene_lineage_id=selection["scene_lineage_id"],
                    cue_index=int(selection["cue_index"]),
                    cue_id=selection["cue_id"], kind="awaiting_selection",
                    detail="no human selection within "
                           f"{SELECTION_TIMEOUT_SECONDS}s; S34 will not pick "
                           "a candidate for the director",
                    source_job_id=selection["music_job_id"],
                    outbox=self.outbox)
            opened.append(gap["gap_id"])
        return {"checked": len(rows), "gaps_opened": opened}

    def reconcile_unknown(self, tenant_id: str, job_id: str,
                          provider_outcome: dict | None) -> dict:
        """`WAITING_UNKNOWN` 的出口。**不重提付费任务**，只收敛成已知状态。

        这就是 AGENTS.md 那条红线的落点：`SUBMISSION_UNKNOWN` 下重提会花两次
        钱，而"到底提交成没有"只有供应商能答。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "WAITING_UNKNOWN":
            raise ServiceError(409, "JOB_NOT_IN_UNKNOWN",
                               f"job is {job['state']}")
        with Transaction(self.conn):
            outbox = self.outbox
            if provider_outcome is None:
                # 还是不知道。**保持原状**，不改 state，不重提。
                return {"music_job_id": job_id, "state": job["state"],
                        "reconciled": False,
                        "note": "provider outcome still unknown; not "
                                "resubmitted"}
            self.conn.execute(
                "UPDATE music_job SET reconciled=1, updated_at=? "
                "WHERE music_job_id=?", (_now(), job_id))
            job = self._job(tenant_id, job_id)
            if provider_outcome.get("succeeded") \
                    and provider_outcome.get("artifacts"):
                # 确认成了且拿到了归档：回到正常的归档/分析路径，不重提。
                for artifact in provider_outcome["artifacts"]:
                    self.on_artifact_ingested({
                        "tenant_id": tenant_id,
                        "data": {"artifact_ref": artifact.get("artifact_ref"),
                                 "provider_job_id": artifact.get(
                                     "provider_job_id"),
                                 "natural_duration_ms": artifact.get(
                                     "natural_duration_ms"),
                                 "sample_rate_hz": artifact.get("sample_rate_hz"),
                                 "channel_count": artifact.get("channel_count"),
                                 "container_format":
                                     artifact.get("container_format")}})
                return {"music_job_id": job_id,
                        "state": self._job(tenant_id, job_id)["state"],
                        "reconciled": True}
            if provider_outcome.get("succeeded"):
                # 成了但没给归档 → 不能凭"成功了"就写 take，等 S28 事件。
                new = self._transition(
                    job, "RECONCILED_CANDIDATES",
                    reason_code="RECONCILED_SUCCESS",
                    detail="provider confirms success; awaiting artifacts",
                    outbox=outbox)
                return {"music_job_id": job_id, "state": new["state"],
                        "reconciled": True}
            # F26：对账确认没成 → 缺口 + GAP_RECORDED。**不是** FAILED：
            # FAILED 可以 :retry 重提付费任务，而这里意味着"不知道供应商侧
            # 有没有残留"，重提是错的。
            self.conn.execute(
                "UPDATE music_job_candidate SET state='FAILED', "
                "failure_code='SUBMISSION_UNRECOVERABLE', updated_at=? "
                "WHERE music_job_id=? AND attempt_no=?",
                (_now(), job_id, int(job["business_attempt_no"])))
            self._record_gap(
                tenant_id=tenant_id,
                scene_lineage_id=job["scene_lineage_id"],
                cue_index=int(job["cue_index"]), cue_id=job["cue_id"],
                kind="submission_unrecoverable",
                detail="reconciliation confirmed the submission did not "
                       "succeed; manual decision required before any resubmit",
                source_job_id=job_id, outbox=outbox)
            new = self._transition(job, "GAP_RECORDED",
                                   reason_code="RECONCILED_FAILURE",
                                   outbox=outbox)
            return {"music_job_id": job_id, "state": new["state"],
                    "reconciled": True}


def _failure_code(provider_state: str, reason: str) -> str:
    """S26 的失败原因 → 本服务 `failure_code`（F22/F23/F24 各自一个码）。

    F22/F23/F24 用不同的码不是为了好看：BUDGET 要等预算重置、QUOTA 要等配额
    重置、ACCOUNT_UNAUTHORIZED 要人去修账户。三者合成一个 "PROVIDER_FAILED"
    会让值班的人从日志里重新读一遍才知道该找谁。
    """
    text = f"{provider_state} {reason}".upper()
    if "BUDGET" in text:
        return "BUDGET"
    if "QUOTA" in text:
        return "QUOTA"
    if "UNAUTHORIZED" in text or "ACCOUNT" in text:
        return "ACCOUNT_UNAUTHORIZED"
    return "PROVIDER_FAILED"
