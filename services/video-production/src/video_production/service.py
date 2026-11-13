"""S30 内核。方案：`plans/S30-video-production.md`。

一句话边界：**S30 决定“这段镜头视频要几秒、从哪裁”，不决定“花多少钱、走哪条路、
能不能派单”。**

三层与 S29 同构，这里只写 S30 特有的那一层：

- **意图层**（要哪一段视频）由 S30 拥有：`shot_lineage_id + segment_no +
  input_fingerprint` 冻结它。`keyframe_baseline` 冻结“这段视频照着哪一版关键帧
  造的”——是**制作时**的基线，不是当前基线（§2.5）。
- **时长层**（要 4 秒、供应商只能给 5 秒、裁不裁）是 S30 独有的。它落成
  `DurationMapping` + `VideoEditPlan` 两条不可变记录，并在 `INGESTING → READY`
  时一次性确定（§7.2）。
- **花费/选型/配额层**归 S23 / S25 / S22 / S26。本库不存余额、不存“预算已批”、
  不缓存路由报价。每次派单都是一次新的 S26 调用。

八条不能违反的语义：

1. **付费只走 S26。** 本模块没有任何供应商 URL；唯一出口是注入的 `submitter`。
2. **`normalized_input.duration_seconds` 逐字段等于提交的 `duration_seconds`。**
   §2.2：报价按 5 秒档算、S23 按 `quote_upper_bound_micro` 预留，提交 4 秒会让
   预留与实际不符（T30-03 / F5）。派单前一条断言，违反即
   `FAILED(SPEC_ASSERTION)`，**不提交、不 +1**（C30-29）。
3. **`SUBMISSION_UNKNOWN` 下绝不重提。** 状态机没有从 UNKNOWN 出发的可推进边；
   `video_job_open_uniq` 把 UNKNOWN 算未终结，再建一个同 fingerprint 的 job 会
   撞唯一约束（T30-14/15/16/17）。
4. **检查点不是 S08 的接受指针，也不含 segment 之外的任何全局含义。** 它只回答
   “S41 该拿哪一段视频”，主键是 `(tenant, lineage, segment)`（§2.6）。
5. **拆镜不由 S30 决定。** `segment_no` 由调用方给；不改、不派生（§8.2）。
6. **基线漂移只标记，不重生成。** `production.image.ready` 在途时置
   `keyframe_drifted` 并给已落定的候选补 `stale_basis`；候选落定时读该标记。
7. **C2 失败时钱已经花了。** 保留候选、标 `truncates_dialogue` +
   `requires_rehuman_decision`，**不自动重生成**（F15）。
8. **`observed_duration_ms` 未知就不写候选。** 允许“未知”会让 READY 变成假成功
   （T30-13）。

事务与幂等与 S29/S33/S34 同构：命令事务内把结果写回同一个 `idempotency_record`
行，提交与“记下已提交”原子成立。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Inbox, Outbox, new_event

from .schema import (
    AUDIO_STRATEGIES,
    CONTRACT_GAPS,
    CANDIDATE_STATES,
    DURATION_POLICIES,
    DURATION_TOLERANCE_MS,
    ID_PREFIX,
    JOB_STATES,
    LEDGER_ACTIONS,
    RETRYABLE_STATES,
    SCHEMA,
    TERMINAL_STATES,
    TRANSITIONS,
)

SOURCE = "S30"
SCHEMA_VERSION = "2.0"
EVENT_VIDEO_READY = "production.video.ready"
TOPIC_VIDEO = "drama.video-production.v2"

# 方案 §6.4：这三个都没有 topic / schema 声明（C30-17），先按本地事件名发。
LOCAL_JOB_STATE = "video.job.state-changed"
LOCAL_CLIP_ATTENTION = "video.clip-attention-needed"
LOCAL_DURATION_MISMATCH = "video.duration-mismatch"

# 与 S29 同一个 execution-context.v2 必填集。少一个就说明签名没覆盖它。
EXECUTION_CONTEXT_REQUIRED = (
    "schema_version", "tenant_id", "project_id", "series_id", "episode_id",
    "branch_id", "node_id", "node_epoch", "execution_fence", "manifest_ref",
    "memory_checkpoint", "policy_ref", "actor", "home_cell", "home_region",
    "trace_id", "command_id", "request_hash",
)

_IDENTITY_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# S26 侧的状态词表。本库只做投影，不做判断。
_S26_SUCCESS_STATES = frozenset({"PROVIDER_SUCCEEDED", "INGESTING", "ARCHIVED"})
_S26_UNKNOWN_STATES = frozenset({"SUBMISSION_UNKNOWN", "RECONCILING"})

# 方案 §4.6：lipsync 族只有 redo_lipsync。视频侧的重生成只由 S39 显式下发。
_VIDEO_REPAIR_ACTIONS = frozenset({"regenerate_video", "split_shot_regenerate"})


def _now() -> str:
    return (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z")


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
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_ts(value: str) -> datetime:
    """把时间字符串读成带时区的瞬间。

    直接比较字符串会把“还有一小时”读成“已过期”：同格式的定宽 UTC 字符串
    可以比较，但调用方给的是 `+08:00` 或 `Z` 混合格式。所有比较都走这里。
    """
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_prompt(text: str) -> str:
    return " ".join(str(text).split())


class ServiceError(Exception):
    """跨越 HTTP 层与内核的错误。

    内核里凡是“要留下一个 REJECTED_LOCAL job”的分支都**不能**用它——那会让
    整个事务回滚，job 就不存在了。用 `_LocalReject`。
    """

    def __init__(self, status: int, code: str, message: str,
                 *, retryable: bool = False, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """可重入的 `BEGIN IMMEDIATE`。

    SQLite 没有嵌套事务，而消费者需要在同一个事务里既写 inbox 去重行又改状态
    （分开就可能在两步之间崩溃，重投时事件被当成重复丢掉，状态却没落）。同一
    事务是唯一正确的选择，所以这里做重入。
    """

    def __init__(self, conn, depth=None):
        self.conn = conn
        self._depth = depth if depth is not None else [0]

    def __enter__(self):
        self.depth = self._depth[0]
        self.outermost = self.depth == 0
        if self.outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth[0] = self.depth + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._depth[0] = self.depth
        if not self.outermost:
            return False
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def reference_combination_hash(keyframe_artifact) -> str:
    """引用组合的内容哈希。

    视频的引用集只有首帧（以及可选的尾帧），没有 S29 的 identity_constraints。
    顺序固定为 first_frame → last_frame，角色进哈希，否则“换一张当首帧”会被
    当成同一个输入。
    """
    refs = []
    if keyframe_artifact:
        refs.append({"role": "first_frame",
                     "artifact_id": str(keyframe_artifact.get("id") or ""),
                     "sha256": str(keyframe_artifact.get("content_hash") or "")})
    return _sha256_hex(_jcs(refs))


def input_fingerprint(*, keyframe_baseline, prompt_intent, target_duration_ms,
                      duration_policy, aspect_ratio, resolution,
                      audio_strategy, required_capabilities) -> str:
    """业务幂等键的内容部分（§7.1）。

    `segment_no` 不进这里：它在 `video_job_open_uniq` 的列里单独参与。把它塞进
    fingerprint 会让同一个 segment 的“同输入”在改动 segment 号后变成另一个
    业务键，而 §8.1 要求 segment 只是审计维度。
    """
    payload = {
        "keyframe_baseline": keyframe_baseline,
        "prompt_intent": _normalize_prompt(prompt_intent),
        "target_duration_ms": int(target_duration_ms),
        "duration_policy": duration_policy,
        "aspect_ratio": aspect_ratio,
        "resolution": {"width": int(resolution["width"]),
                       "height": int(resolution["height"])},
        "audio_strategy": audio_strategy,
        "required_capabilities": sorted(str(c) for c in required_capabilities),
    }
    return _sha256_hex(_jcs(payload))


def canonical_transition(from_state, to_state: str, *, allow_retry: bool = False):
    """§3.1 的转移表。

    `allow_retry` 只放行 `{FAILED, RECONCILED_FAILED} → PREPARING`——那是显式的
    `POST /video-jobs/{id}:retry`，不是状态机自己长出来的边。
    """
    if to_state not in JOB_STATES:
        raise AssertionError(f"unknown video job state {to_state!r}")
    if allow_retry:
        if from_state in RETRYABLE_STATES and to_state == "PREPARING":
            return True
        return False
    allowed = TRANSITIONS.get(from_state)
    if allowed is None:
        raise AssertionError(f"unknown video job state {from_state!r}")
    return to_state in allowed


def _contains(window, head_ms: int, target_ms: int) -> bool:
    return (int(window["start_ms"]) >= head_ms
            and int(window["end_ms"]) <= head_ms + target_ms)


def solve_trim(*, tier_ms: int, target_ms: int, dialogue_window=None,
               action_window=None, allow_head: bool = False):
    """§4.3 的 C1–C4。

    可行头偏移是一个区间：

        lo = max(0, d_end - target, a_end - target)
        hi = min(tier - target, d_start, a_start)

    `head ∈ [lo, hi]` 时 C2 与 C3 同时成立；`hi` 的 `tier - target` 项就是 C4。
    默认 `allow_head=False`（§4.3“默认策略禁止 head”），此时 `head` 恒为 0，
    窗口装不下就如实标 `truncates_*`，再去截断——**绝不用“裁掉对白的一部分”
    来凑目标时长**。

    `allow_head=True` 只在 `duration_policy=allow_tier_snap_with_trim` 时传入，
    落成 `trim_policy='c2_minimal'`。此时若解出的 `head>0`、`tail==0`，表里唯一
    合法的 mode 是 `'both'`（`dm_head_only_forbidden`），文件头 C30-26 已记。
    """
    span = max(0, int(tier_ms) - int(target_ms))
    lo, hi = 0, span
    if dialogue_window:
        lo = max(lo, int(dialogue_window["end_ms"]) - int(target_ms))
        hi = min(hi, int(dialogue_window["start_ms"]))
    if action_window:
        lo = max(lo, int(action_window["end_ms"]) - int(target_ms))
        hi = min(hi, int(action_window["start_ms"]))

    head = 0
    if allow_head and lo > 0:
        head = min(lo, span)
    head = max(0, min(head, span))
    tail = span - head
    if head == 0 and tail == 0:
        mode = "none"
    elif head == 0:
        mode = "tail"
    else:
        mode = "both"

    return {
        "trim_mode": mode,
        "trim_head_ms": head,
        "trim_tail_ms": tail,
        "resulting_duration_ms": int(tier_ms) - head - tail,
        "truncates_dialogue": bool(dialogue_window)
        and not _contains(dialogue_window, head, target_ms),
        "truncates_action": bool(action_window)
        and not _contains(action_window, head, target_ms),
        "feasible": lo <= hi,
    }


class _LocalReject(Exception):
    """本地拒绝：调用方必须留下一个 `REJECTED_LOCAL` job。

    它不是 API 错误——`ServiceError` 跨出事务会让 job 行一起回滚，测试与运维就
    再也看不到“为什么没生成”。
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code



def body_key(norm) -> dict:
    """§7.1 的 `request_hash` 载荷。

    含 `context` 的业务字段、`shot_ref`、`segment_no`、关键帧候选、意图、时长
    策略、画幅、分辨率、音频策略、能力（排序）、`allow_trim`、`budget_scope_id`、
    `deadline`。**不含** `trace_id`——同一个业务请求重试时 trace 一定不同，把它
    算进来会让每次重试都变成“异体”，幂等就没了。
    """
    ctx = norm["context"]
    return {
        "context": {
            "project_id": ctx.get("project_id"),
            "series_id": ctx.get("series_id"),
            "episode_id": ctx.get("episode_id"),
            "branch_id": ctx.get("branch_id"),
            "node_id": ctx.get("node_id"),
        },
        "shot_ref": norm["shot_ref"],
        "segment_no": norm["segment_no"],
        "keyframe_baseline": (norm["keyframe_baseline"] or {}).get("candidate_id"),
        "prompt_intent": norm["prompt_intent"],
        "target_duration_ms": norm["target_duration_ms"],
        "duration_policy": norm["duration_policy"],
        "aspect_ratio": norm["aspect_ratio"],
        "resolution": norm["resolution"],
        "audio_strategy": norm["audio_strategy"],
        "required_capabilities": sorted(norm["required_capabilities"]),
        "allow_trim": norm["allow_trim"],
        "budget_scope_id": norm["budget_scope_id"],
        "deadline": norm["deadline"],
    }


def _row(row):
    return dict(row) if row is not None else None


def _provider_state_of(local_state: str) -> str:
    """本地状态 → S26 状态的**投影**，不是权威。

    `INGESTING` 同时覆盖 S26 的 INGESTING 与 ARCHIVED；`READY` 一律投影成
    ARCHIVED。读口靠 `provider_state_source: "local_map"` 明示这是本地映射，
    避免有人把它当成 S26 的原话。
    """
    return {
        "CREATED": "CREATED",
        "PREPARING": "RESERVING",
        "SUBMITTED": "SUBMITTING",
        "WAITING_PROVIDER": "RUNNING",
        "WAITING_UNKNOWN": "SUBMISSION_UNKNOWN",
        "INGESTING": "INGESTING",
        "READY": "ARCHIVED",
        "RECONCILED_READY": "ARCHIVED",
        "REJECTED_LOCAL": "REJECTED",
        "FAILED": "PROVIDER_FAILED",
        "RECONCILED_FAILED": "REJECTED",
        "CANCELED": "CANCELED",
    }.get(local_state, "UNKNOWN")


class VideoProductionService:
    """S30 的全部内核。没有网络调用；六个上游都是注入的回调。"""

    def __init__(self, conn, *, route_resolver, submitter, artifact_check,
                 artifact_importer, generation_state, canceler,
                 keyframe_lookup):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._tx_depth = [0]
        self.route_resolver = route_resolver
        self.submitter = submitter
        self.artifact_check = artifact_check
        self.artifact_importer = artifact_importer
        self.generation_state = generation_state
        self.canceler = canceler
        self.keyframe_lookup = keyframe_lookup
        # `executescript` 隐式提交，所以 Outbox/Inbox 必须在任何事务之外创建；
        # 否则第一次事件写入会悄悄提交命令事务，ROLLBACK 就失效了（与 S29 同坑）。
        self.outbox = Outbox(self.conn)
        self.inbox = Inbox(self.conn)

    # ------------------------------------------------------------ 事务/错误

    def _tx(self):
        return Transaction(self.conn, self._tx_depth)

    def _command(self, tenant_id, command_id, produce):
        """命令事务：产出与幂等结果同一事务落盘。

        SDK 的 `begin`/`finish` 是两条裸语句，跑在我们自己的 `BEGIN IMMEDIATE`
        外面。把 `finish` 留在事务之外会留下“业务已提交、幂等未记录”的窗口——
        重投时第二次付费。所以结果写回改在这里、和业务同一次提交。
        """
        with self._tx():
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result = ? "
                "WHERE tenant_id = ? AND command_id = ?",
                (json.dumps(result, ensure_ascii=False), tenant_id, command_id))
            return result

    def _upstream(self, call, code="DEPENDENCY_UNAVAILABLE"):
        try:
            return call()
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc

    # ------------------------------------------------------------ 状态机

    def _transition(self, job, to_state, *, reason_code=None, detail=None,
                    outbox=None, allow_retry=False):
        from_state = job["state"]
        if not canonical_transition(from_state, to_state,
                                    allow_retry=allow_retry):
            raise AssertionError(
                f"illegal video job transition {from_state} -> {to_state}")
        seq = int(job["state_seq"]) + 1
        at = _now()
        self.conn.execute(
            "UPDATE video_job SET state = ?, state_seq = ?, updated_at = ?, "
            "state_entered_at = ? WHERE video_job_id = ?",
            (to_state, seq, at, at, job["video_job_id"]))
        self.conn.execute(
            "INSERT INTO job_transition (video_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job["video_job_id"], seq, from_state, to_state, reason_code,
             detail, at))
        if outbox is not None:
            # 刻意不写 failure_code：`reason_code` 是“为什么走这一步”，不是
            # 失败。S29 曾用 COALESCE 把它写进失败字段，读口从此一直报一个
            # 不存在的失败。
            outbox.append(new_event(
                source=SOURCE,
                type=LOCAL_JOB_STATE,
                subject=f"video-job/{job['video_job_id']}",
                tenant_id=job["tenant_id"],
                home_cell="video-production",
                aggregate_id=job["video_job_id"],
                sequence=seq,
                correlation_id=job["video_job_id"],
                data={
                    "video_job_id": job["video_job_id"],
                    "shot_lineage_id": job["shot_lineage_id"],
                    "segment_no": int(job["segment_no"]),
                    "from": from_state,
                    "to": to_state,
                    "reason_code": reason_code,
                },
            ))
        return {**job, "state": to_state, "state_seq": seq,
                "updated_at": at, "state_entered_at": at}

    def _job(self, tenant_id, job_id):
        row = self.conn.execute(
            "SELECT * FROM video_job WHERE tenant_id = ? AND video_job_id = ?",
            (tenant_id, job_id)).fetchone()
        if row is None:
            raise ServiceError(404, "VIDEO_JOB_NOT_FOUND",
                               "video job not found")
        return _row(row)

    # ------------------------------------------------------------ 校验

    def _validate(self, body, tenant_id):
        """§6.1 的固定顺序。失败即返回。

        跨租户的引用一律 **404 而不是 403**：403 会告诉对方“这个 id 存在，只是
        不是你的”，等于把别的租户的 id 空间当成了存在性预言机（T30-31 / F2）。
        """
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_BODY", "body must be a JSON object")

        # --- 2. context 必填项 + 签名绑定 ---
        ctx = body.get("context")
        if not isinstance(ctx, dict):
            raise ServiceError(422, "INVALID_CONTEXT",
                               "context must be an execution-context object")
        declared_tenant = ctx.get("tenant_id")
        if declared_tenant != tenant_id:
            raise ServiceError(
                403, "TENANT_MISMATCH",
                "context tenant_id disagrees with the signed X-Tenant-Id")
        missing = [key for key in EXECUTION_CONTEXT_REQUIRED if key not in ctx]
        if missing:
            raise ServiceError(
                422, "INVALID_CONTEXT",
                "execution context is missing required keys", details={
                    "missing": missing})
        manifest = ctx.get("manifest_ref")
        if not isinstance(manifest, dict):
            raise ServiceError(422, "INVALID_CONTEXT",
                               "context.manifest_ref must be an object")
        for key in ("id", "revision", "content_hash"):
            if key not in manifest:
                raise ServiceError(
                    422, "INVALID_CONTEXT",
                    f"context.manifest_ref is missing {key}")
        if not _HEX64_RE.match(str(manifest.get("content_hash"))):
            raise ServiceError(
                422, "INVALID_CONTEXT",
                "context.manifest_ref.content_hash must be 64 lowercase hex")

        lineage = body.get("shot_lineage_id")
        if not isinstance(lineage, str) or not lineage.strip():
            raise ServiceError(422, "SHOT_LINEAGE_REQUIRED",
                               "shot_lineage_id is required")

        segment_no = body.get("segment_no", 1)
        if (isinstance(segment_no, bool) or not isinstance(segment_no, int)
                or segment_no < 1):
            # 契约对 `segment_no` 要求 integer ≥ 1。放行字符串会让业务幂等键的
            # 字面值（"1" vs 1）算出两个键，同一段视频被生产两次。
            raise ServiceError(422, "INVALID_SEGMENT_NO",
                               "segment_no must be an integer >= 1")

        # --- 4. shot_ref.revision 存在 ---
        shot_ref = body.get("shot_ref")
        if not isinstance(shot_ref, dict):
            raise ServiceError(422, "INVALID_SHOT_REF",
                               "shot_ref must be an object")
        for key in ("service_id", "id", "revision"):
            if key not in shot_ref:
                raise ServiceError(422, "INVALID_SHOT_REF",
                                   f"shot_ref is missing {key}")
        revision = shot_ref.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) \
                or revision < 1:
            raise ServiceError(422, "INVALID_SHOT_REF",
                               "shot_ref.revision must be an integer >= 1")

        # 同一 shot_ref 换了 lineage = 两个不同对象的同一行 id，必须先断。
        drift = self.conn.execute(
            "SELECT video_job_id FROM video_job "
            "WHERE tenant_id = ? AND json_extract(shot_ref, '$.id') = ? "
            "AND shot_lineage_id <> ? LIMIT 1",
            (tenant_id, str(shot_ref["id"]), lineage)).fetchone()
        if drift is not None:
            raise ServiceError(
                409, "SHOT_LINEAGE_DRIFT",
                "this shot_ref id already belongs to a different shot_lineage_id",
                details={"video_job_id": drift["video_job_id"]})

        # --- 5. keyframe_baseline 与 S29 当前检查点兼容 ---
        baseline = body.get("keyframe_baseline")
        baseline_norm = None
        if baseline is not None:
            if not isinstance(baseline, dict):
                raise ServiceError(422, "INVALID_KEYFRAME_BASELINE",
                                   "keyframe_baseline must be an object or null")
            candidate_id = baseline.get("candidate_id")
            seq = baseline.get("checkpoint_seq")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise ServiceError(422, "INVALID_KEYFRAME_BASELINE",
                                   "keyframe_baseline.candidate_id is required")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise ServiceError(
                    422, "INVALID_KEYFRAME_BASELINE",
                    "keyframe_baseline.checkpoint_seq must be an integer >= 1")
            current = self.keyframe_lookup(tenant_id, candidate_id, lineage)
            if current is None:
                # 跨租户与不存在走同一句：404 不区分。
                raise ServiceError(404, "KEYFRAME_CANDIDATE_NOT_FOUND",
                                   "keyframe candidate not found")
            current_seq = int(current.get("checkpoint_seq") or 0)
            if seq > current_seq:
                raise ServiceError(
                    422, "KEYFRAME_BASELINE_AHEAD",
                    "keyframe_baseline.checkpoint_seq is ahead of S29's "
                    "current checkpoint",
                    details={"requested": seq, "current": current_seq})
            artifact = current.get("artifact_ref")
            if not isinstance(artifact, dict) or artifact.get("service_id") != "S28":
                raise ServiceError(
                    422, "INVALID_KEYFRAME_BASELINE",
                    "S29 returned no S28 artifact for this keyframe candidate")
            baseline_norm = {
                "candidate_id": candidate_id,
                "checkpoint_seq": seq,
                "artifact_ref": artifact,
                "content_hash": str(current.get("content_hash")
                                    or artifact.get("content_hash") or ""),
                "current_checkpoint_seq": current_seq,
            }

        # --- 6. target_duration_ms > 0 ---
        target = body.get("target_duration_ms")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ServiceError(422, "INVALID_TARGET_DURATION",
                               "target_duration_ms must be an integer > 0")

        # --- 7. duration_policy 与 allow_trim 一致（§4.2 表） ---
        policy = body.get("duration_policy")
        if policy not in DURATION_POLICIES:
            raise ServiceError(
                422, "INVALID_DURATION_POLICY",
                "duration_policy must be one of native_only, allow_tier_snap, "
                "allow_tier_snap_with_trim",
                details={"allowed": sorted(DURATION_POLICIES)})
        expected_trim = policy == "allow_tier_snap_with_trim"
        allow_trim = body.get("allow_trim")
        if allow_trim is None:
            allow_trim = expected_trim
        elif bool(allow_trim) != expected_trim:
            # `allow_trim` 是 bool 装三态（C1）。调用方给的两个字段矛盾时不能
            # 挑一个信——那等于替调用方决定“要不要裁”。
            raise ServiceError(
                422, "ALLOW_TRIM_INCONSISTENT",
                "allow_trim disagrees with duration_policy",
                details={"duration_policy": policy,
                         "allow_trim": bool(allow_trim)})

        # --- 8. audio_strategy 与镜头是否有对白一致（§4.4） ---
        shot_ctx = body.get("shot_context") or {}
        if not isinstance(shot_ctx, dict):
            raise ServiceError(422, "INVALID_SHOT_CONTEXT",
                               "shot_context must be an object")
        has_dialogue = shot_ctx.get("has_dialogue")
        if has_dialogue is not None and not isinstance(has_dialogue, bool):
            raise ServiceError(422, "INVALID_SHOT_CONTEXT",
                               "shot_context.has_dialogue must be a boolean")
        voice_locked = shot_ctx.get("voice_locked_by_s15")
        if voice_locked is not None and not isinstance(voice_locked, bool):
            raise ServiceError(
                422, "INVALID_SHOT_CONTEXT",
                "shot_context.voice_locked_by_s15 must be a boolean")
        audio_strategy = body.get("audio_strategy")
        if audio_strategy not in AUDIO_STRATEGIES:
            raise ServiceError(
                422, "INVALID_AUDIO_STRATEGY",
                "audio_strategy must be one of silent_then_tts, "
                "native_locked_voice, non_dialogue_native")
        if has_dialogue is not None:
            if has_dialogue:
                expected_audio = ("native_locked_voice" if voice_locked
                                  else "silent_then_tts")
            else:
                expected_audio = "non_dialogue_native"
            if audio_strategy != expected_audio:
                raise ServiceError(
                    422, "AUDIO_STRATEGY_INCONSISTENT",
                    "audio_strategy disagrees with the shot's dialogue facts",
                    details={"audio_strategy": audio_strategy,
                             "expected": expected_audio,
                             "has_dialogue": has_dialogue,
                             "voice_locked_by_s15": bool(voice_locked)})

        dialogue_ms = shot_ctx.get("dialogue_duration_ms")
        if dialogue_ms is not None:
            if isinstance(dialogue_ms, bool) or not isinstance(dialogue_ms, int) \
                    or dialogue_ms <= 0:
                raise ServiceError(
                    422, "INVALID_SHOT_CONTEXT",
                    "shot_context.dialogue_duration_ms must be an integer > 0")
            # --- 9. silent_then_tts 时对白长度不得超过目标 ---
            if audio_strategy == "silent_then_tts" and dialogue_ms > target:
                raise ServiceError(
                    422, "DIALOGUE_EXCEEDS_TARGET",
                    "dialogue_duration_ms exceeds target_duration_ms",
                    details={"dialogue_duration_ms": dialogue_ms,
                             "target_duration_ms": target})

        dialogue_window = self._window(shot_ctx.get("dialogue_window"),
                                       "dialogue_window")
        action_window = self._window(shot_ctx.get("action_window"),
                                     "action_window")

        prompt = body.get("prompt_intent")
        if not isinstance(prompt, str) or not _normalize_prompt(prompt):
            raise ServiceError(422, "EMPTY_PROMPT_INTENT",
                               "prompt_intent must be a non-blank string")

        aspect = body.get("aspect_ratio")
        if not isinstance(aspect, str) or not aspect.strip():
            raise ServiceError(422, "INVALID_ASPECT_RATIO",
                               "aspect_ratio is required")

        resolution = body.get("resolution")
        if not isinstance(resolution, dict):
            raise ServiceError(422, "INVALID_RESOLUTION",
                               "resolution must be an object")
        for key in ("width", "height"):
            value = resolution.get(key)
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value <= 0:
                raise ServiceError(422, "INVALID_RESOLUTION",
                                   f"resolution.{key} must be an integer > 0")

        # --- 10. required_capabilities 非空 ---
        caps = body.get("required_capabilities")
        if not isinstance(caps, list) or not caps:
            raise ServiceError(422, "REQUIRED_CAPABILITIES_EMPTY",
                               "required_capabilities must be a non-empty list")
        for cap in caps:
            if not isinstance(cap, str) or not cap.strip():
                raise ServiceError(422, "INVALID_REQUIRED_CAPABILITIES",
                                   "required_capabilities entries must be "
                                   "non-blank strings")

        route_policy_ref = body.get("route_policy_ref")
        if not isinstance(route_policy_ref, dict):
            raise ServiceError(422, "INVALID_ROUTE_POLICY_REF",
                               "route_policy_ref must be an object")
        for key in ("service_id", "id", "revision"):
            if key not in route_policy_ref:
                raise ServiceError(
                    422, "INVALID_ROUTE_POLICY_REF",
                    f"route_policy_ref is missing {key}")
        if not isinstance(route_policy_ref.get("revision"), int):
            raise ServiceError(
                422, "INVALID_ROUTE_POLICY_REF",
                "route_policy_ref.revision must be an integer")

        # 绝不默认：一个 "default" 预算域会把所有漏传的租户汇进同一个桶，既不
        # 隔离也查不了账。
        budget_scope = body.get("budget_scope_id")
        if not isinstance(budget_scope, str) or not budget_scope.strip():
            raise ServiceError(422, "INVALID_BUDGET_SCOPE",
                               "budget_scope_id is required")

        # --- 11. deadline > now() ---
        deadline = body.get("deadline")
        if not isinstance(deadline, str) or not deadline.strip():
            raise ServiceError(422, "INVALID_DEADLINE", "deadline is required")
        try:
            parsed_deadline = _parse_ts(deadline)
        except ValueError as exc:
            raise ServiceError(422, "INVALID_DEADLINE",
                               "deadline is not an ISO-8601 timestamp") from exc
        if parsed_deadline <= datetime.now(timezone.utc):
            raise ServiceError(422, "DEADLINE_IN_PAST",
                               "deadline must be in the future")

        repair = self._validate_repair(body.get("repair_action"), lineage)

        return {
            "context": ctx,
            "shot_ref": {"service_id": str(shot_ref["service_id"]),
                         "id": str(shot_ref["id"]),
                         "revision": int(revision)},
            "shot_lineage_id": lineage,
            "segment_no": int(segment_no),
            "keyframe_baseline": baseline_norm,
            "prompt_intent": _normalize_prompt(prompt),
            "target_duration_ms": int(target),
            "duration_policy": policy,
            "aspect_ratio": aspect.strip(),
            "resolution": {"width": int(resolution["width"]),
                           "height": int(resolution["height"])},
            "audio_strategy": audio_strategy,
            "required_capabilities": [str(c) for c in caps],
            "allow_trim": bool(allow_trim),
            "route_policy_ref": route_policy_ref,
            "budget_scope_id": budget_scope,
            "deadline": deadline,
            "dialogue_duration_ms": dialogue_ms,
            "dialogue_window": dialogue_window,
            "action_window": action_window,
            "has_dialogue": has_dialogue,
            "repair": repair,
            "route_decision_hint": body.get("route_decision_id"),
        }

    @staticmethod
    def _window(value, name):
        """区间入参：`{start_ms, end_ms}`，0 ≤ start < end。"""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ServiceError(422, "INVALID_SHOT_CONTEXT",
                               f"shot_context.{name} must be an object")
        start = value.get("start_ms")
        end = value.get("end_ms")
        for key, val in (("start_ms", start), ("end_ms", end)):
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise ServiceError(
                    422, "INVALID_SHOT_CONTEXT",
                    f"shot_context.{name}.{key} must be an integer >= 0")
        if end <= start:
            raise ServiceError(
                422, "INVALID_SHOT_CONTEXT",
                f"shot_context.{name}.end_ms must be greater than start_ms")
        return {"start_ms": int(start), "end_ms": int(end)}

    def _validate_repair(self, repair, lineage):
        """§4.6 / §8.3：动作族守卫。

        这里**只检查本地前置条件**。上限（每镜 3 次）是 S39 在动作下发前查的，
        S30 没有那个台账，也不该假装有。
        """
        if repair is None:
            return None
        if not isinstance(repair, dict):
            raise ServiceError(422, "INVALID_REPAIR_ACTION",
                               "repair_action must be an object")
        action_type = repair.get("action_type")
        if action_type == "redo_lipsync":
            # §4.6：口型缺陷归 S32。收到它时不是“拒绝执行”，而是“你发错了
            # 服务”——所以不落 job，也不该在本服务的台账里留一行。
            raise ServiceError(
                422, "ACTION_NOT_OWNED",
                "redo_lipsync is S32's action; S30 does not create a video job",
                details={"owner": "S32", "action_type": action_type})
        if action_type not in _VIDEO_REPAIR_ACTIONS:
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                "repair_action.action_type is not a video action owned by S30",
                details={"action_type": action_type,
                         "allowed": sorted(_VIDEO_REPAIR_ACTIONS)})
        if action_type == "regenerate_video" \
                and str(repair.get("defect_class") or "") == "lipsync":
            # S35 §2.2.1 的 lipsync 族只有 redo_lipsync。真收到这一条说明上游
            # 族表写错了；静默执行会替 S32 重做一次昂贵的付费任务。
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                "lipsync defects must not be repaired by regenerate_video",
                details={"action_type": action_type,
                         "defect_class": "lipsync"})
        repair_id = repair.get("repair_action_id")
        if not isinstance(repair_id, str) or not repair_id.strip():
            raise ServiceError(
                422, "INVALID_REPAIR_ACTION",
                "repair_action.repair_action_id is required")
        repair_lineage = repair.get("shot_lineage_id")
        if repair_lineage is not None and repair_lineage != lineage:
            # §8.3：lineage 只能由 RepairAction 带进来，不能从 shot_ref 重新
            # 派生。两者不一致就是两个镜头在共用一个 Repairaction。
            raise ServiceError(
                422, "LINEAGE_MISMATCH",
                "repair_action.shot_lineage_id disagrees with the request",
                details={"repair": repair_lineage, "request": lineage})
        return {
            "repair_action_id": repair_id,
            "action_type": action_type,
            "strategy_class": repair.get("strategy_class"),
        }

    # ------------------------------------------------------------ 创建

    def create_job(self, body, tenant_id, command_id):
        """§4.1 / §6.1。返回 202 的 body。

        注意顺序：`CREATED → PREPARING` 必须在**判断本地拒绝之前**完成。§3.1 没有
        `CREATED → REJECTED_LOCAL` 这条边；S29 早期把顺序写反，每一次本地拒绝都撞
        状态机断言变成 500，测试根本走不到。
        """
        norm = self._validate(body, tenant_id)
        baseline = norm["keyframe_baseline"]
        if baseline is not None:
            combo_hash = reference_combination_hash(baseline["artifact_ref"])
        else:
            combo_hash = reference_combination_hash(None)
        fingerprint = input_fingerprint(
            keyframe_baseline=(baseline or {}).get("candidate_id"),
            prompt_intent=norm["prompt_intent"],
            target_duration_ms=norm["target_duration_ms"],
            duration_policy=norm["duration_policy"],
            aspect_ratio=norm["aspect_ratio"],
            resolution=norm["resolution"],
            audio_strategy=norm["audio_strategy"],
            required_capabilities=norm["required_capabilities"])

        # 只读核对首帧：这是 S28 的佐证，不是我们的账。放在 produce 之外——
        # 三态里的 None 表示“没查成”，不能和“查了、不一致”混成一件事。
        keyframe_verdict = None
        if baseline is not None:
            keyframe_verdict = self._check_keyframe_artifact(
                tenant_id, baseline["artifact_ref"])

        def produce():
            existing = self._find_open_job(norm, tenant_id, fingerprint)
            if existing is not None:
                return self._job_response(existing,
                                          extra={"duplicate_suppressed": True})
            if norm["repair"] is not None:
                prior = self.conn.execute(
                    "SELECT * FROM video_job WHERE tenant_id = ? "
                    "AND repair_action_id = ?",
                    (tenant_id, norm["repair"]["repair_action_id"])).fetchone()
                if prior is not None:
                    # §7.1 修复幂等键：同一 RepairAction 只能执行一次。返回 409
                    # 并带上首次 job——调用方要的是“已经做过了”，不是“再做一次”。
                    raise ServiceError(
                        409, "REPAIR_ACTION_ALREADY_EXECUTED",
                        "this repair_action_id already produced a video job",
                        details={"video_job_id": prior["video_job_id"]})

            baseline_id = self._freeze_baseline(tenant_id, baseline)
            job = self._insert_job(norm, tenant_id, command_id, fingerprint,
                                  combo_hash, baseline_id)
            if job.get("_duplicate"):
                return self._job_response(job,
                                          extra={"duplicate_suppressed": True})

            job = self._transition(job, "PREPARING", outbox=self.outbox)

            if keyframe_verdict is False:
                return self._reject_local(
                    job, "KEYFRAME_UNREADABLE",
                    detail="S28 says the keyframe artifact hash does not match")
            return self._dispatch(job)

        result = self._command(tenant_id, command_id, produce)
        if isinstance(result, dict) and "target_duration_ms" in result:
            # §6.1 的 202 回显。放在 `_command` 之外统一补：`produce` 有多个返回
            # 出口（派单 / 本地拒绝 / 业务键去重），逐个加会漏一个。这里是纯函数，
            # 只读 `target_duration_ms`，所以重放路径补出来的值与原次一致。
            result = {**result,
                      "duration_mapping_preview":
                      self.duration_mapping_preview(result)}
        return result

    def _find_open_job(self, norm, tenant_id, fingerprint):
        row = self.conn.execute(
            "SELECT * FROM video_job WHERE tenant_id = ? AND shot_lineage_id = ? "
            "AND segment_no = ? AND input_fingerprint = ? "
            "AND state NOT IN ('READY','RECONCILED_READY','REJECTED_LOCAL',"
            "'FAILED','RECONCILED_FAILED','CANCELED')",
            (tenant_id, norm["shot_lineage_id"], norm["segment_no"],
             fingerprint)).fetchone()
        return _row(row)

    def _freeze_baseline(self, tenant_id, baseline):
        """冻结 `(candidate_id, checkpoint_seq, content_hash)`（§2.5）。

        已存在同三元组的行时复用——基线是不可变事实，同一事实存两行只会让
        `keyframe_baseline_id` 在事件里指向两个 id。
        """
        if baseline is None:
            return None
        row = self.conn.execute(
            "SELECT keyframe_baseline_id FROM keyframe_baseline_ref "
            "WHERE tenant_id = ? AND candidate_id = ? AND checkpoint_seq = ?",
            (tenant_id, baseline["candidate_id"],
             baseline["checkpoint_seq"])).fetchone()
        if row is not None:
            return row["keyframe_baseline_id"]
        baseline_id = _new_id("baseline")
        self.conn.execute(
            "INSERT INTO keyframe_baseline_ref (keyframe_baseline_id, tenant_id, "
            "candidate_id, checkpoint_seq, artifact_ref, content_hash, "
            "captured_at) VALUES (?,?,?,?,?,?,?)",
            (baseline_id, tenant_id, baseline["candidate_id"],
             baseline["checkpoint_seq"], _dumps(baseline["artifact_ref"]),
             baseline["content_hash"], _now()))
        return baseline_id

    def _insert_job(self, norm, tenant_id, command_id, fingerprint, combo_hash,
                    baseline_id):
        ctx = norm["context"]
        job_id = _new_id("job")
        job = {
            "video_job_id": job_id,
            "tenant_id": tenant_id,
            "shot_ref": _dumps(norm["shot_ref"]),
            "shot_lineage_id": norm["shot_lineage_id"],
            "segment_no": norm["segment_no"],
            "state": "CREATED",
            "state_seq": 0,
            "business_attempt_no": 1,
        }
        try:
            self.conn.execute(
                "INSERT INTO video_job (video_job_id, tenant_id, project_id, "
                "series_id, episode_id, branch_id, shot_ref, shot_lineage_id, "
                "segment_no, node_id, node_epoch_at_create, execution_fence, "
                "prompt_intent, target_duration_ms, duration_policy, "
                "aspect_ratio, resolution, audio_strategy, "
                "required_capabilities, allow_trim, keyframe_baseline_id, "
                "reference_combination_hash, execution_context, "
                "route_policy_ref, variation_nonce, repair_action_id, "
                "repair_action_type, strategy_class, dialogue_duration_ms, "
                "dialogue_window, action_window, keyframe_drifted, "
                "input_fingerprint, request_hash, idempotency_key, "
                "route_decision_ref, budget_scope_id, deadline, "
                "business_attempt_no, submitted_duration_ms, state, state_seq, "
                "reconciled, created_at, updated_at, state_entered_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, tenant_id, str(ctx["project_id"]),
                 str(ctx["series_id"]), str(ctx["episode_id"]),
                 str(ctx["branch_id"]), job["shot_ref"],
                 norm["shot_lineage_id"], norm["segment_no"],
                 str(ctx["node_id"]), int(ctx["node_epoch"]),
                 int(ctx["execution_fence"]), norm["prompt_intent"],
                 norm["target_duration_ms"], norm["duration_policy"],
                 norm["aspect_ratio"], _dumps(norm["resolution"]),
                 norm["audio_strategy"], _dumps(norm["required_capabilities"]),
                 1 if norm["allow_trim"] else 0, baseline_id, combo_hash,
                 _dumps(ctx), _dumps(norm["route_policy_ref"]),
                 uuid.uuid4().hex,
                 (norm["repair"] or {}).get("repair_action_id"),
                 (norm["repair"] or {}).get("action_type"),
                 (norm["repair"] or {}).get("strategy_class"),
                 norm["dialogue_duration_ms"],
                 _dumps(norm["dialogue_window"])
                 if norm["dialogue_window"] else None,
                 _dumps(norm["action_window"])
                 if norm["action_window"] else None,
                 0, fingerprint, _sha256_hex(_jcs(body_key(norm))),
                 f"{tenant_id}\x1f{command_id}", None,
                 norm["budget_scope_id"], norm["deadline"], 1, None,
                 "CREATED", 0, 0, _now(), _now(), _now()))
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            # SQLite 报的是列名或索引名，两种都要认。三个唯一约束各自对应一条
            # “已经有人替我做完了”的路径；把它们混成一句“内部错误”会让并发下
            # 的重复投递变成 500。
            if "video_job_open_uniq" in text or "input_fingerprint" in text:
                existing = self._find_open_job(norm, tenant_id, fingerprint)
                if existing is not None:
                    return {**existing, "_duplicate": True}
            if "video_job_repair_uniq" in text or "repair_action_id" in text:
                raise ServiceError(
                    409, "REPAIR_ACTION_ALREADY_EXECUTED",
                    "this repair_action_id already produced a video job")
            if "vj_tenant_idem" in text or "idempotency_key" in text:
                row = self.conn.execute(
                    "SELECT * FROM video_job WHERE tenant_id = ? "
                    "AND idempotency_key = ?",
                    (tenant_id, f"{tenant_id}\x1f{command_id}")).fetchone()
                if row is not None:
                    return {**dict(row), "_duplicate": True}
            raise
        return self._job(tenant_id, job_id)

    def _check_keyframe_artifact(self, tenant_id, artifact_ref):
        try:
            return self.artifact_check(tenant_id, artifact_ref)
        except (ServiceError, ConnectionError):
            # 只读佐证拿不到时按“没查成”处理：不能把一次网络失败读成“首帧坏了”。
            return None

    # ------------------------------------------------------------- 提交 S26

    def _generation_command(self, job, *, attempt_id, idempotency_key,
                            normalized):
        """`generation-command.v2` 的 video 分支。

        11 个根键 + `input` 的 9 个键都要有（`additionalProperties: false`）。
        `duration_seconds` 取 **S25 的 `normalized_input`**，不是 `target_duration_ms
        / 1000`——报价按档位算、S23 按档位预留，提交目标秒数会让预留与实际
        不符（§2.2）。

        `references` 里首帧用 S28 的 artifact 三元组；没有基线（文生视频，U8）
        时是空数组，由调用方在 `required_capabilities` 里声明文生能力。
        """
        ctx = _loads(job["execution_context"], {})
        shot_ref = _loads(job["shot_ref"], {})
        baseline_artifact = self._baseline_artifact(job)
        references = []
        if baseline_artifact:
            references.append({
                "artifact_id": str(baseline_artifact.get("id") or ""),
                "sha256": str(baseline_artifact.get("content_hash") or ""),
                "role": "first_frame",
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "context": ctx,
            "attempt_id": attempt_id,
            "variation_nonce": job["variation_nonce"],
            "idempotency_key": idempotency_key,
            "modality": "video",
            "input": {
                "prompt_intent": job["prompt_intent"],
                # 契约要 `{id, revision}`；`service_id` 在 input 里没有位置。
                "shot_revision": {"id": str(shot_ref.get("id") or ""),
                                  "revision": int(shot_ref.get("revision") or 1)},
                "duration_seconds": normalized["duration_seconds"],
                "aspect_ratio": job["aspect_ratio"],
                "resolution": _loads(job["resolution"], {}),
                "references": references,
                "audio_strategy": job["audio_strategy"],
                "required_capabilities": _loads(job["required_capabilities"], []),
                "allow_trim": bool(int(job["allow_trim"])),
            },
            "template_ref": None,
            "route_policy_ref": _loads(job["route_policy_ref"], None),
            "budget_scope_id": job["budget_scope_id"],
            "deadline": job["deadline"],
        }

    def _baseline_artifact(self, job):
        if job.get("keyframe_baseline_id") is None:
            return None
        row = self.conn.execute(
            "SELECT artifact_ref FROM keyframe_baseline_ref "
            "WHERE keyframe_baseline_id = ?",
            (job["keyframe_baseline_id"],)).fetchone()
        return _loads(row["artifact_ref"], {}) if row is not None else None

    def _dispatch(self, job, outbox=None):
        """PREPARING → 取路由 → 时长映射 → 提交 S26。

        每次派单都是一次新的 S25 + S26 调用：本服务不存“预算已批”，也不缓存
        路由报价（报价会过期，F6）。
        """
        outbox = outbox or self.outbox

        for _attempt in range(2):
            attempt_base = int(job["business_attempt_no"])
            attempt_id = f"{job['video_job_id']}:{attempt_base}"
            idempotency_key = f"{job['idempotency_key']}:{attempt_base}"
            # 先按“目标值”占位组一份，只为让 S25 看到完整命令；真实秒数在拿到
            # normalized_input 之后重建。S25 用它做硬过滤（对白超时长等）。
            probe = self._generation_command(
                job, attempt_id=attempt_id, idempotency_key=idempotency_key,
                normalized={"duration_seconds": job["target_duration_ms"] / 1000})

            route = self._upstream(
                lambda: self.route_resolver(job["tenant_id"], job, probe),
                "ROUTE_DECISION_UNAVAILABLE")

            if isinstance(route, dict) and route.get("feasible") is False:
                # F3 / T30-06：透传原因与降级选项，**不缩短对白、不选降级**。
                detail = str(route.get("infeasible_reason")
                             or "S25 reported no feasible route")
                return self._reject_local(job, "ROUTE_INFEASIBLE",
                                          detail=detail, outbox=outbox,
                                          extra={"degradation_options":
                                                 route.get("degradation_options")
                                                 or []})
            if isinstance(route, dict) and route.get("residency_allowed") is False:
                # T30-35：数据驻留必须排在第一次付费提交之前。
                return self._reject_local(
                    job, "RESIDENCY_FORBIDDEN", outbox=outbox,
                    detail="no feasible route satisfies the residency requirement")

            quote_valid_to = (route or {}).get("quote_valid_to")
            if quote_valid_to:
                try:
                    if _parse_ts(quote_valid_to) <= datetime.now(timezone.utc):
                        self._note_quote_expired(job, outbox)
                        continue
                except ValueError:
                    raise ServiceError(
                        503, "ROUTE_DECISION_UNAVAILABLE",
                        "route quote_valid_to is not a timestamp",
                        retryable=True) from None

            if not (route.get("route_id") or route.get("route_decision_id")):
                # 拿不到路由 id 时不编：S26 会拿它校验预算与配额。
                raise ServiceError(
                    503, "ROUTE_DECISION_UNAVAILABLE",
                    "S25 returned no route id; S30 will not invent one",
                    retryable=True)

            mapping = self._resolve_mapping(job, route)
            if isinstance(mapping, tuple):
                code, detail = mapping
                return self._reject_local(job, code, detail=detail, outbox=outbox)

            normalized = mapping["normalized"]
            command = self._generation_command(
                job, attempt_id=attempt_id, idempotency_key=idempotency_key,
                normalized=normalized)

            # §2.2 的结构性约束。两处独立来源：命令里要写的秒数，与 job 上冻结
            # 的那一份（第一次解析时写入）。不一致说明两次路由给了不同档位，
            # 或者有人把 target 当成了提交值——两种都不能付费。
            if not self._duration_assertion_holds(job, command, normalized):
                return self._fail_spec_assertion(
                    job, outbox,
                    detail=(f"command duration_seconds "
                            f"{command['input']['duration_seconds']!r} != "
                            f"normalized {normalized['duration_seconds']!r}"))

            self.conn.execute(
                "UPDATE video_job SET route_decision_ref=?, "
                "submitted_duration_ms=?, quote_upper_bound_micro=?, "
                "updated_at=? WHERE video_job_id=?",
                (_jcs(self._route_ref(route)),
                 int(round(float(normalized["duration_seconds"]) * 1000)),
                 (route.get("selected") or {}).get("quote_upper_bound_micro"),
                 _now(), job["video_job_id"]))
            job = self._job(job["tenant_id"], job["video_job_id"])

            submitted = self._upstream(
                lambda: self.submitter(job, command, route, 0),
                "GENERATION_JOBS_UNAVAILABLE")
            if not isinstance(submitted, dict) \
                    or not submitted.get("provider_job_id"):
                raise ServiceError(
                    503, "GENERATION_JOBS_UNAVAILABLE",
                    "S26 accepted the request but returned no provider_job_id",
                    retryable=True)

            self.conn.execute(
                "UPDATE video_job SET provider_job_id=?, attempt_id=?, "
                "updated_at=? WHERE video_job_id=?",
                (submitted["provider_job_id"],
                 submitted.get("attempt_id") or attempt_id, _now(),
                 job["video_job_id"]))
            # 台账只记**已经付出去**的意图。放在提交成功之后、状态转移之前：
            # 这两步在同一事务里，不会出现“有台账没提交”。
            self._write_ledger(job, action=self._ledger_action(job))
            job = self._job(job["tenant_id"], job["video_job_id"])
            job = self._transition(job, "SUBMITTED", outbox=outbox)
            job = self._transition(job, "WAITING_PROVIDER", outbox=outbox)
            return self._job_response(
                job, extra={"operation_id": submitted.get("operation_id"),
                            "submitted_duration_ms": int(
                                job["submitted_duration_ms"] or 0)})

        raise ServiceError(
            503, "ROUTE_DECISION_UNAVAILABLE",
            "route quotes kept expiring; refusing to submit on a stale price",
            retryable=True)

    def _ledger_action(self, job):
        # `repair_action_type` 是台账 `action` 列的唯一出处；dispatch 时再猜一次
        # 动作类型就是编数据。
        value = job.get("repair_action_type")
        return value if value in LEDGER_ACTIONS else "generate"

    def _resolve_mapping(self, job, route):
        """§4.2 的三个分支。返回 `{normalized, tier_ms, branch}` 或 `(code, detail)`。

        这里只决定“能不能映射”，**不写 `duration_mapping` 行**：那份记录在
        `INGESTING → READY` 时用实测时长一次性确定（§7.2），之后不变。
        """
        selected = (route or {}).get("selected") or {}
        normalized = selected.get("normalized_input")
        if not isinstance(normalized, dict):
            # `generation-command.v2` 的 video input 必填 duration_seconds；S25
            # 不给就没法组命令。
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "S25 returned no normalized_input; S30 cannot size the command",
                retryable=True)
        seconds = normalized.get("duration_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or float(seconds) <= 0:
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "S25 normalized_input.duration_seconds is not a positive number",
                retryable=True)
        tier_ms = int(round(float(seconds) * 1000))
        target = int(job["target_duration_ms"])
        policy = job["duration_policy"]
        dialogue = _loads(job["dialogue_window"], None)
        action = _loads(job["action_window"], None)

        if tier_ms < target:
            # 拿不到比目标更长的素材，任何策略都裁不出来。
            return ("DURATION_UNMAPPABLE",
                    f"tier {tier_ms}ms is shorter than target {target}ms")

        if policy == "native_only" and tier_ms != target:
            # 分支 A（F4 / T30-05）：只要有档位不等于目标就拒绝。事后判定——
            # `require_native_duration` 这个 route policy 字段还不存在（C30-11）。
            return ("DURATION_UNSUPPORTED_NATIVE_ONLY",
                    f"native_only requested {target}ms but S25 normalized to "
                    f"{tier_ms}ms")

        if policy == "allow_tier_snap":
            # 分支 B：接受档位、不裁。下游按目标长度使用素材的前 target 毫秒。
            # C30-3：不裁时窗口装不下对白 = 下游一用就截断，必须在付款前拒绝。
            if dialogue and not _contains(dialogue, 0, target):
                return ("DIALOGUE_WOULD_BE_TRUNCATED",
                        "dialogue window does not fit the target window and "
                        "this policy forbids trimming")
            if action and not _contains(action, 0, target):
                return ("ACTION_WOULD_BE_TRUNCATED",
                        "action window does not fit the target window and this "
                        "policy forbids trimming")
            return {"normalized": normalized, "tier_ms": tier_ms,
                    "branch": "allow_tier_snap", "dialogue": dialogue,
                    "action": action}

        # 分支 C：接受档位并按 C1–C4 裁到目标。这里只做可行性判断：允许 head
        # trim 之后仍无解，说明目标时长与素材结构根本对不上。
        plan = solve_trim(tier_ms=tier_ms, target_ms=target,
                          dialogue_window=dialogue, action_window=action,
                          allow_head=True)
        if plan["trim_head_ms"] + target > tier_ms:
            return ("DURATION_UNMAPPABLE",
                    "no trim window of the target length exists in this tier")
        return {"normalized": normalized, "tier_ms": tier_ms,
                "branch": "allow_tier_snap_with_trim", "dialogue": dialogue,
                "action": action}

    @staticmethod
    def _duration_assertion_holds(job, command, normalized):
        """F5 / T30-04：提交秒数必须逐字段等于 S25 的归一化值。

        `submitted_duration_ms` 在第一次派单时冻结。两次路由给出不同档位时，
        差异会在这里暴露而不是变成一次“按 4 秒报价、按 5 秒扣款”。
        """
        sent = command["input"]["duration_seconds"]
        if sent != normalized["duration_seconds"]:
            return False
        frozen = job.get("submitted_duration_ms")
        if frozen is not None:
            if int(round(float(sent) * 1000)) != int(frozen):
                return False
        return True

    def _fail_spec_assertion(self, job, outbox, *, detail):
        """F5：内部断言失败 → `FAILED(SPEC_ASSERTION)`，**不提交、不 +1**。

        这不是供应商的错，也不是调用方的错；它是本服务组命令时的 bug。留一个
        终态 job 让巡检看见，并把 kind 作为告警维度（§9 的
        `vid_assertion_failures_total{kind}`）。
        """
        with self._tx():
            self.conn.execute(
                "UPDATE video_job SET failure_code=?, failure_detail=?, "
                "updated_at=? WHERE video_job_id=?",
                ("SPEC_ASSERTION", detail, _now(), job["video_job_id"]))
            job = self._job(job["tenant_id"], job["video_job_id"])
            job = self._transition(job, "FAILED", reason_code="SPEC_ASSERTION",
                                   detail=detail, outbox=outbox)
        return self._job_response(
            job, extra={"failure_code": "SPEC_ASSERTION",
                        "assertion": "duration_seconds == normalized_input",
                        "alert": "vid_assertion_failures_total"})

    def _note_quote_expired(self, job, outbox):
        """F6：报价过期，只记一笔并重取，**状态仍是 PREPARING**。

        `PREPARING → PREPARING` 不在 §3.1 的表里。硬造一个自环会让 `state_seq`
        涨一次却不代表任何状态变化，而下游用 `state_seq` 判断“这条事件是不是比
        我手上的新”。所以这里只发 outbox 事件，`from == to`，不动 `state_seq`，
        也不插 `job_transition`（主键是 `(video_job_id, seq)`，同 seq 再插一行会
        直接撞键）。
        """
        seq = int(job["state_seq"])
        outbox.append(new_event(
            source=SOURCE, type=LOCAL_JOB_STATE,
            subject=f"video-job/{job['video_job_id']}",
            tenant_id=job["tenant_id"], home_cell="video-production",
            aggregate_id=job["video_job_id"], sequence=seq,
            correlation_id=job["video_job_id"],
            data={"video_job_id": job["video_job_id"],
                  "shot_lineage_id": job["shot_lineage_id"],
                  "segment_no": int(job["segment_no"]),
                  "from": job["state"], "to": job["state"],
                  "reason_code": "QUOTE_EXPIRED"},
            schema_version=SCHEMA_VERSION))

    @staticmethod
    def _route_ref(route):
        """只存引用，不复制 `eligible[]`。拿不到 `content_hash` 就不编。"""
        route_id = (route or {}).get("route_id") \
            or (route or {}).get("route_decision_id")
        ref = {"service_id": "S25", "id": route_id,
               "revision": int((route or {}).get("revision") or 1)}
        if (route or {}).get("content_hash"):
            ref["content_hash"] = route["content_hash"]
        return ref

    # ------------------------------------------------------- 台账（只追加）

    def _write_ledger(self, job, *, action):
        """只有真的把钱花出去的意图才进台账（§8.1）。

        `run_id` 仅审计归因，不进任何上限键；execution-context 没有该字段时存
        空串，不拿 `trace_id` / `manifest_id` 冒充。
        """
        ctx = _loads(job["execution_context"], {})
        manifest = ctx.get("manifest_ref") or {}
        self.conn.execute(
            "INSERT INTO video_intent_ledger (entry_id, tenant_id, video_job_id, "
            "attempt_id, provider_job_id, shot_lineage_id, segment_no, action, "
            "strategy_class, repair_action_id, run_id, node_epoch, manifest_id, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ledger"), job["tenant_id"], job["video_job_id"],
             job["attempt_id"], job["provider_job_id"], job["shot_lineage_id"],
             int(job["segment_no"]), action, job["strategy_class"],
             job["repair_action_id"], str(ctx.get("run_id") or ""),
             int(job["node_epoch_at_create"]), str(manifest.get("id") or ""),
             _now()))

    def _reject_local(self, job, code, *, detail="", outbox=None, extra=None):
        """本地拒绝：没花钱、没留台账。终态但**不是失败**——换策略/换档位就能好，
        所以业务幂等键不占位（`video_job_open_uniq` 排除了 `REJECTED_LOCAL`），
        之后同输入还能重开。
        """
        outbox = outbox or self.outbox
        self.conn.execute(
            "UPDATE video_job SET failure_code=?, failure_detail=?, updated_at=? "
            "WHERE video_job_id=?",
            (code, detail, _now(), job["video_job_id"]))
        job = self._job(job["tenant_id"], job["video_job_id"])
        job = self._transition(job, "REJECTED_LOCAL", reason_code=code,
                               detail=detail, outbox=outbox)
        body = self._job_response(job, extra={"reason_code": code})
        body.update(extra or {})
        return body

    # ================================================ 事件消费者

    def on_generation_state_changed(self, envelope):
        """S26 `generation.job.state-changed`。

        防御式映射：S26 的 `state` 在契约里是普通 string，这里不假设认识每一个
        值。不认识的一律不动 job——把未知状态当成“成功”是这条链路最贵的错。
        """
        event_id = envelope.get("id") or ""
        if not event_id:
            # 没有 id 就无法去重，重投时会被再处理一遍。
            raise ServiceError(400, "INVALID_BODY",
                               "generation state event carries no id")
        data = envelope.get("data") or {}
        provider_job_id = envelope.get("aggregate_id") \
            or data.get("provider_job_id")
        tenant_id = envelope.get("tenant_id")
        if not provider_job_id or not tenant_id:
            return {"ignored": True, "reason": "not_shaped_for_video"}

        # inbox 去重行与状态变化必须同一事务：分开会在两步之间崩溃，重投时事件
        # 被当成重复丢掉、状态却没落。
        with self._tx():
            if not self.inbox.consume("S30", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            result = self._apply_provider_state(tenant_id, provider_job_id,
                                                data)
        # HTTP 放在事务外发：BEGIN IMMEDIATE 已持写锁，等 S28 会挡住整个租户的写。
        pending = result.pop("_request_import", None)
        if pending is not None:
            result = self._request_import(pending, result)
        return result

    def _stray_provider_state(self, local_state, provider_state):
        """C30-30：这条供应商状态在本 job 当前状态**没有**合法落点时返回原因。

        两类"事件比 job 快"的情形，共同点都是**不能改状态**：

        - `CREATED` / `PREPARING`：还没提交给 S26，任何成功/失败结论都属于某个
          旧 attempt 或别的 job。没提交就没有可失败的钱，也没有可归档的字节。
          这条路径只在 `retry_job` 之后真的会出现：重试保留旧的
          `provider_job_id`，于是上一个 attempt 的迟到事件会打到
          `PREPARING` 的新 attempt 上。
        - `INGESTING`：归档已经开始了，`SUBMISSION_UNKNOWN` 是**过去**那一刻的
          结论，不是现在的对账结论；把它当成结论会把已在归档的产物推进
          `WAITING_UNKNOWN`。

        `SUBMITTED` ← 成功类状态**不是**杂散事件：那说明 `QUEUED`/`RUNNING`
        那条事件丢了（§3.1 的 `SUBMITTED → WAITING_PROVIDER` 没人送）。丢掉它
        会让一个已付费的 job 永远停在 `SUBMITTED`，所以 `_enter_ingesting` 会先
        补记 `WAITING_PROVIDER` 再进 `INGESTING`——它确实经过那里。

        返回 `None` 表示"按正常映射处理"，包括那些本来就不认识的
        `QUEUED` / `RUNNING` 等中间态（它们由调用方的兜底分支处理）。
        """
        if provider_state in _S26_UNKNOWN_STATES:
            target = "WAITING_UNKNOWN"
        elif provider_state in _S26_SUCCESS_STATES:
            target = "INGESTING"
        elif provider_state in ("REJECTED_BUDGET", "REJECTED_QUOTA",
                                "REJECTED_ACCOUNT", "PROVIDER_FAILED"):
            target = "FAILED"
        elif provider_state == "CANCELED":
            target = "CANCELED"
        else:
            return None
        if local_state in ("CREATED", "PREPARING"):
            # 不能问转移表：`PREPARING → FAILED` 在表里（C30-29，给 F5 的内部
            # 断言用），但供应商事件**不是**内部断言。重试会把旧 attempt 的
            # `provider_job_id` 留在 job 上，于是旧 attempt 的迟到失败会打到
            # 新 attempt 的 PREPARING 上——按表放行就是"用过期事实否掉一次
            # 还没提交的新尝试"。
            if target == "INGESTING":
                return "provider_state_arrived_before_job_was_submitted"
            return "job_not_yet_submitted"
        if canonical_transition(local_state, target):
            return None
        if local_state == "SUBMITTED" and target in ("INGESTING", "FAILED",
                                                     "CANCELED"):
            # `WAITING_PROVIDER` 那一格的事件丢了：`_enter_ingesting` 会先补记
            # `SUBMITTED → WAITING_PROVIDER` 再往下走；`SUBMITTED → FAILED` 本来
            # 就在表里（上面那条已放行），这里补的是被同一跳过的 CANCELED。
            return None
        if target == "WAITING_UNKNOWN":
            return "unknown_state_after_ingest_started"
        if target == "INGESTING":
            return "provider_state_arrived_before_job_was_submitted"
        return "job_not_yet_submitted"

    def _apply_provider_state(self, tenant_id, provider_job_id, data):
        provider_state = str(data.get("state") or "")
        job = _row(self.conn.execute(
            "SELECT * FROM video_job WHERE tenant_id = ? AND provider_job_id = ?",
            (tenant_id, provider_job_id)).fetchone())
        if job is None:
            return {"ignored": True, "reason": "no_matching_video_job"}

        if job["state"] == "WAITING_UNKNOWN":
            return self._reconcile_from_unknown(job, provider_state, data)
        if job["state"] in TERMINAL_STATES:
            # 刻意忽略而不是断言：一条无害的迟到事件不该让消费者崩溃、无限重投。
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True,
                    "reason": f"job_already_terminal:{job['state']}"}
        stray = self._stray_provider_state(job["state"], provider_state)
        if stray is not None:
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True, "reason": stray}
        if provider_state in _S26_UNKNOWN_STATES:
            return self._enter_unknown(job)
        if provider_state in _S26_SUCCESS_STATES:
            return self._enter_ingesting(job, provider_state=provider_state)
        if provider_state == "REJECTED_BUDGET":
            # F7 / T30-32：不降分辨率、不缩时长——那是静默降规格。
            return self._fail(job, "BUDGET", provider_state)
        if provider_state == "REJECTED_QUOTA":
            # F8 / T30-33：配额由 S23 补偿，这里只记终态。
            return self._fail(job, "QUOTA", provider_state)
        if provider_state == "REJECTED_ACCOUNT":
            # T30-34：账户撤销是明确失败，不换渠道（换渠道是 S25 的事）。
            return self._fail(job, "ACCOUNT_REVOKED", provider_state)
        if provider_state == "PROVIDER_FAILED":
            # F9/F11：钱已花，台账在提交时已写过，不 +1。
            return self._fail(job,
                              str(data.get("reason_code") or "PROVIDER_FAILED"),
                              provider_state)
        if provider_state == "CANCELED":
            with self._tx():
                new = self._transition(job, "CANCELED",
                                       reason_code="PROVIDER_CANCELED",
                                       outbox=self.outbox)
            return {"video_job_id": new["video_job_id"], "state": new["state"]}
        return {"ignored": True,
                "reason": f"unmapped_provider_state:{provider_state}"}

    def _reconcile_from_unknown(self, job, provider_state, data):
        """`WAITING_UNKNOWN` 的两个出口（§3.1 / §4.5）。

        `ARCHIVED` 是对账结论：供应商确实收单并产出 → `RECONCILED_READY`（候选
        由随后到达的 `artifact.ingested` 落定，与 INGESTING 同一条路）。
        其余明确的失败结论 → `RECONCILED_FAILED`。

        `QUEUED` / `RUNNING` 这类“还在做”的状态**不是**对账结论：把它当成结论
        就是把“可能已付费”读成“一定没好”。
        """
        if provider_state in _S26_SUCCESS_STATES and provider_state == "ARCHIVED":
            with self._tx():
                self.conn.execute(
                    "UPDATE video_job SET reconciled = 1, updated_at = ? "
                    "WHERE video_job_id = ?", (_now(), job["video_job_id"]))
                job = self._job(job["tenant_id"], job["video_job_id"])
                new = self._transition(job, "RECONCILED_READY",
                                       reason_code="RECONCILED_ARCHIVED",
                                       outbox=self.outbox)
            return {"video_job_id": new["video_job_id"], "state": new["state"],
                    "reconciled": True}
        if provider_state in ("REJECTED_BUDGET", "REJECTED_QUOTA",
                              "PROVIDER_FAILED", "CANCELED"):
            with self._tx():
                self.conn.execute(
                    "UPDATE video_job SET reconciled = 1, failure_code = ?, "
                    "failure_detail = ?, updated_at = ? WHERE video_job_id = ?",
                    (provider_state, "reconciled_from_unknown", _now(),
                     job["video_job_id"]))
                job = self._job(job["tenant_id"], job["video_job_id"])
                new = self._transition(job, "RECONCILED_FAILED",
                                       reason_code=provider_state,
                                       outbox=self.outbox)
            return {"video_job_id": new["video_job_id"], "state": new["state"],
                    "reconciled": True, "failure_code": provider_state}
        return {"video_job_id": job["video_job_id"], "state": job["state"],
                "ignored": True, "frozen": True,
                "reason": f"not_a_reconciliation_conclusion:{provider_state}"}

    def _enter_unknown(self, job):
        if job["state"] == "WAITING_UNKNOWN":
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True}
        with self._tx():
            new = self._transition(
                job, "WAITING_UNKNOWN", reason_code="SUBMISSION_UNKNOWN",
                detail="provider outcome unknown; must reconcile before resubmit",
                outbox=self.outbox)
        return {"video_job_id": new["video_job_id"], "state": new["state"],
                "frozen": True, "must_reconcile_before_resubmit": True}

    def _enter_ingesting(self, job, *, provider_state):
        if job["state"] in ("INGESTING", "READY", "RECONCILED_READY"):
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True}
        outbox = self.outbox
        if job["state"] == "SUBMITTED":
            # `QUEUED`/`RUNNING` 那条事件丢了。先如实补记 WAITING_PROVIDER：
            # 跳过它会让 §3.1 的历史里缺一格，也让 `state_seq` 少跳一次。
            job = self._transition(job, "WAITING_PROVIDER",
                                   reason_code="STATE_EVENT_SKIPPED",
                                   outbox=outbox)
        new = self._transition(job, "INGESTING", reason_code=provider_state,
                               outbox=outbox)
        # 归档是异步的：这里只推进状态并把“该去催一次导入”带出去。候选由
        # `artifact.ingested` 落定；导入失败不能让 job 假装成功。
        return {"video_job_id": new["video_job_id"], "state": new["state"],
                "_request_import": new}

    def _fail(self, job, code, provider_state):
        if job["state"] in TERMINAL_STATES:
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True}
        with self._tx():
            self.conn.execute(
                "UPDATE video_job SET failure_code = ?, failure_detail = ?, "
                "updated_at = ? WHERE video_job_id = ?",
                (code, provider_state, _now(), job["video_job_id"]))
            job = self._job(job["tenant_id"], job["video_job_id"])
            new = self._transition(job, "FAILED", reason_code=code,
                                   outbox=self.outbox)
        return {"video_job_id": new["video_job_id"], "state": new["state"],
                "failure_code": code}

    def _request_import(self, job, result):
        """尽力触发一次 S28 导入（幂等键固定，重放安全）。

        **失败不改状态**：导入没被受理只说明“字节还没进 S28”，不是“生成失败”。
        URL 过期（F12 / T30-18）尤其不能重生成——向 S26 要新 URL 就够了。

        唯一的例外是 `HASH_MISMATCH`（F13）：那是 S28 取到字节、算完之后明确
        说“这不是那份”。S28 在这条路径上不发事件，所以这里是 S30 唯一能知道
        它的地方，按方案打终态 `FAILED(ARTIFACT_HASH_MISMATCH)`，不重试。
        """
        try:
            self.artifact_importer(job["tenant_id"], job)
        except (ServiceError, ConnectionError) as exc:
            code = getattr(exc, "code", None) or "ARTIFACT_INGEST_UNAVAILABLE"
            if code == "HASH_MISMATCH":
                with self._tx():
                    fresh = self._job(job["tenant_id"], job["video_job_id"])
                    if fresh["state"] not in TERMINAL_STATES:
                        self.conn.execute(
                            "UPDATE video_job SET failure_code = ?, "
                            "failure_detail = ?, updated_at = ? "
                            "WHERE video_job_id = ?",
                            ("ARTIFACT_HASH_MISMATCH", "s28_hash_mismatch",
                             _now(), fresh["video_job_id"]))
                        fresh = self._job(job["tenant_id"],
                                          job["video_job_id"])
                        self._transition(fresh, "FAILED",
                                         reason_code="ARTIFACT_HASH_MISMATCH",
                                         outbox=self.outbox)
                result["failure_code"] = "ARTIFACT_HASH_MISMATCH"
                result["note"] = "import rejected: HASH_MISMATCH"
                return result
            result["awaiting_fresh_url"] = code == "URL_EXPIRED"
            result["note"] = f"import not accepted: {code}"
        return result

    # ---------------------------------------------- S28：候选落定

    def on_artifact_ingested(self, envelope):
        """S28 `artifact.ingested` → 写候选、推进检查点、发 ready。

        与 S29 的差别只有一处、但很硬：**必须拿到 `observed_duration_ms` 与
        `frame_rate`**（§3.2 / T30-13）。缺任一项就停在 `INGESTING` 并告警——
        允许“未知”会让 READY 变成假成功，而这个服务下游是按秒数排时间线的。
        """
        event_id = envelope.get("id") or ""
        if not event_id:
            raise ServiceError(400, "INVALID_BODY",
                               "artifact ingested event carries no id")
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        artifact_ref = data.get("artifact_ref") or {}
        provider_job_id = ((data.get("source") or {}).get("provider_job_id")
                           or data.get("provider_job_id"))
        if not tenant_id or not provider_job_id:
            return {"ignored": True, "reason": "not_shaped_for_video"}

        with self._tx():
            if not self.inbox.consume("S30", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            if artifact_ref.get("service_id") != "S28":
                # 候选的 artifact_ref 只能指向 S28；供应商直链进来就是第二权威。
                # **已消费再拒**：这是一条给我们的毒消息，不消费会被无限重投。
                return {"ignored": True, "reason": "artifact_not_owned_by_s28"}
            if not artifact_ref.get("id") or not _HEX64_RE.match(
                    str(artifact_ref.get("content_hash") or "")):
                return {"ignored": True, "reason": "artifact_ref_incomplete"}
            job = _row(self.conn.execute(
                "SELECT * FROM video_job WHERE tenant_id = ? "
                "AND provider_job_id = ?",
                (tenant_id, provider_job_id)).fetchone())
            if job is None:
                return {"ignored": True, "reason": "no_matching_video_job"}
            return self._finalize(job, artifact_ref, data)

    def _observed_media(self, data):
        """把 S28 的字段名归一，**不做猜测性填补**（C30-12）。

        `duration_ms` → 毫秒；`frame_rate_num` / `frame_rate_den` → 一个浮点。
        缺任一源、或值为零/负，都返回 `None`，由调用方阻塞。
        """
        duration = data.get("observed_duration_ms")
        if duration is None:
            duration = data.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) \
                or float(duration) <= 0:
            return None
        rate = data.get("frame_rate")
        if rate is None:
            num = data.get("frame_rate_num")
            den = data.get("frame_rate_den")
            if isinstance(num, (int, float)) and not isinstance(num, bool) \
                    and isinstance(den, (int, float)) \
                    and not isinstance(den, bool) and den:
                rate = float(num) / float(den)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) \
                or float(rate) <= 0:
            return None
        return {"observed_duration_ms": int(round(float(duration))),
                "frame_rate": round(float(rate), 3)}

    def _finalize(self, job, artifact_ref, data):
        """候选落定 → 检查点 → ready 事件。**在同一事务里**。"""
        if job["state"] in ("WAITING_UNKNOWN", "RECONCILED_FAILED", "CANCELED",
                            "FAILED", "REJECTED_LOCAL"):
            # 这些状态的出口只有对账那条边。一条 `artifact.ingested` 不是对账
            # 结论：UNKNOWN 期间它可能来自上一次已经付过款的 attempt。
            # `RECONCILED_READY` 不在此列——对账已证明视频存在，缺的只是候选行。
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True,
                    "reason": f"artifact_ingested_while_{job['state'].lower()}"}
        if job["state"] in ("CREATED", "PREPARING"):
            # 重试会保留旧 attempt 的 `provider_job_id`，于是上一轮的归档会打到
            # 正在派单的新 attempt 上。那是**上一轮**的字节，不是这一轮要的；
            # 收下它会让一次已付费的重试看起来已经完成。
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "ignored": True, "reason": "artifact_ingested_before_submit"}

        media = self._observed_media(data)
        if media is None:
            # T30-13：停住，不写候选，不伪造时长。事件已消费（否则会无限重投）；
            # 恢复走 `recover_ingesting` 再向 S28 要一次导入。
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "blocked": True,
                    "reason": "observed_duration_missing_or_invalid",
                    "alert": "vid_duration_unknown_total"}

        observed = media["observed_duration_ms"]
        asserted_tier = int(job["submitted_duration_ms"] or 0)
        if asserted_tier and abs(observed - asserted_tier) > DURATION_TOLERANCE_MS:
            # F14 / T30-11：供应商没按档位交付。候选**不标 READY**。
            with self._tx():
                self.conn.execute(
                    "UPDATE video_job SET failure_code = ?, failure_detail = ?, "
                    "updated_at = ? WHERE video_job_id = ?",
                    ("DURATION_MISMATCH",
                     f"observed {observed}ms vs asserted {asserted_tier}ms",
                     _now(), job["video_job_id"]))
                job = self._job(job["tenant_id"], job["video_job_id"])
                job = self._transition(job, "FAILED",
                                       reason_code="DURATION_MISMATCH",
                                       outbox=self.outbox)
                self.outbox.append(new_event(
                    source=SOURCE, type=LOCAL_DURATION_MISMATCH,
                    subject=f"video-job/{job['video_job_id']}",
                    tenant_id=job["tenant_id"], home_cell="video-production",
                    aggregate_id=job["video_job_id"],
                    sequence=int(job["state_seq"]),
                    correlation_id=job["video_job_id"],
                    data={"video_job_id": job["video_job_id"],
                          "shot_lineage_id": job["shot_lineage_id"],
                          "segment_no": int(job["segment_no"]),
                          "submitted_duration_ms": asserted_tier,
                          "observed_duration_ms": observed,
                          "delta_ms": observed - asserted_tier,
                          "tolerance_ms": DURATION_TOLERANCE_MS},
                    schema_version=SCHEMA_VERSION))
            return {"video_job_id": job["video_job_id"], "state": job["state"],
                    "failure_code": "DURATION_MISMATCH",
                    "alert": "vid_duration_mismatch_total"}

        with self._tx():
            outbox = self.outbox
            existing = self._existing_candidate(job["tenant_id"], artifact_ref)
            if existing is not None:
                # 同一份 artifact 已经落过候选（另一个 job 产出同一份字节时）。
                # 复用它的映射与编辑计划：再造一份会让同一个 artifact 在两个 job
                # 下各有一条 `duration_mapping`，S41 / S42 读哪条都可能是错的。
                candidate = existing
                mapping = self._mapping_for_event(candidate)
                edit_plan = self._edit_plan_for_event(candidate["edit_plan_id"])
            else:
                mapping = self._write_mapping(job, observed)
                candidate_id = _new_id("candidate")
                edit_plan = self._write_edit_plan(job, mapping, candidate_id)
                candidate = self._insert_candidate(
                    job, artifact_ref, media, mapping, edit_plan, candidate_id)
            if job["state"] not in ("READY", "RECONCILED_READY"):
                if job["state"] in ("SUBMITTED", "WAITING_PROVIDER"):
                    # `WAITING_PROVIDER → READY` 不在 §3.1 的表里（正常路径必经
                    # `INGESTING`）。走到这里说明 `PROVIDER_SUCCEEDED` 那条事件
                    # 丢了、S28 的归档先到。补记 `INGESTING`——它就是实际发生的
                    # 那一格，不是跳过去。
                    if job["state"] == "SUBMITTED":
                        job = self._transition(job, "WAITING_PROVIDER",
                                               reason_code="STATE_EVENT_SKIPPED",
                                               outbox=outbox)
                    job = self._transition(job, "INGESTING",
                                           reason_code="ARTIFACT_BEFORE_STATE_EVENT",
                                           outbox=outbox)
                job = self._transition(job, "READY",
                                       reason_code="ARTIFACT_INGESTED",
                                       outbox=outbox)
            if self._checkpoint_already_points_at(job, candidate):
                # 重投：同一份字节、换了个 event id 再来一次。候选是同一行，
                # 指针也已经在它上面。再走一次 CAS 会把 `checkpoint_seq` 推进
                # 一格、发出 `checkpoint: {from: X, to: X}` 的第二次 ready——
                # 下游收到的是一次并不存在的 revision；attention 也会重复一次。
                # 幂等消费必须停在这里（T30-38：重放不触发二次生成，也不该
                # 触发二次发布）。
                return {"video_job_id": job["video_job_id"],
                        "candidate_id": candidate["clip_candidate_id"],
                        "state": job["state"],
                        "checkpoint_advanced": False,
                        "reason": "checkpoint_already_points_at_candidate"}
            if candidate["requires_recheck"] or candidate["stale_basis"] \
                    or candidate["truncates_dialogue"] \
                    or candidate["truncates_action"] \
                    or candidate["requires_rehuman_decision"]:
                self._publish_attention(job, candidate, outbox)
            try:
                advanced = self._advance_checkpoint(job, candidate)
            except ServiceError as exc:
                return {"video_job_id": job["video_job_id"],
                        "candidate_id": candidate["clip_candidate_id"],
                        "state": job["state"], "checkpoint_advanced": False,
                        "checkpoint_error": exc.code}
            if advanced is None:
                return {"video_job_id": job["video_job_id"],
                        "candidate_id": candidate["clip_candidate_id"],
                        "state": job["state"], "checkpoint_advanced": False,
                        "checkpoint_error": "CHECKPOINT_NOT_ADVANCED"}
            published = self._publish_ready(
                job, candidate, outbox, advanced["row"], mapping=mapping,
                edit_plan=edit_plan,
                from_candidate_id=advanced["from_candidate_id"])
        result = {"video_job_id": job["video_job_id"],
                  "candidate_id": candidate["clip_candidate_id"],
                  "state": job["state"], "checkpoint_advanced": True,
                  "checkpoint_seq": int(advanced["row"]["checkpoint_seq"]),
                  "ready_published": bool(published["published"])}
        if not published["published"]:
            # 文生视频（U8 / C30-28）：候选有效、检查点已推进，但没有可发的
            # `production.video.ready` 载荷。如实告知，不回滚也不静默。
            result["ready_blocked_by"] = published["reason"]
            result["contract_gap"] = published["gap"]
        return result

    def _write_mapping(self, job, observed_ms):
        """§2.4 / §4.3：在 `INGESTING → READY` 时一次性确定，之后不变。

        `allow_head` 只在 `duration_policy = allow_tier_snap_with_trim` 时为真：
        §4.3 第三个例子（对白 [4200,5000]、目标 4s、档位 5s）要求 `trim_head=1000`，
        默认策略下则禁止 head。`both` 一旦出现，表里唯一合法的搭配是
        `trim_policy='c2_minimal'`（`dm_both_forbidden_default` / C30-26），所以
        policy 列跟着 `trim_mode` 走，不是无条件写死。
        """
        target = int(job["target_duration_ms"])
        dialogue = _loads(job["dialogue_window"], None)
        action = _loads(job["action_window"], None)
        strategy = job["audio_strategy"]

        # C2 的输入按策略取（§4.4）：
        # - silent_then_tts：S31 的音频时长 + S06 的对白位置 → 本地窗口；
        # - native_locked_voice：要等 S37，READY 时不可得 → `dialogue_window_unverified`
        #   （本服务唯一允许“带未验证约束发 READY”的情形，必须显式标记）；
        # - non_dialogue_native：跳过 C2。
        unverified = strategy == "native_locked_voice"
        check_dialogue = dialogue if strategy == "silent_then_tts" else None
        check_action = action  # C3 对所有策略都用 S06 的节拍，不依赖 S37

        allow_head = job["duration_policy"] == "allow_tier_snap_with_trim"
        plan = solve_trim(tier_ms=observed_ms, target_ms=target,
                          dialogue_window=check_dialogue,
                          action_window=check_action, allow_head=allow_head)
        trim_policy = "c2_minimal" if plan["trim_mode"] == "both" else "tail_only"
        mapping_id = _new_id("mapping")
        self.conn.execute(
            "INSERT INTO duration_mapping (duration_mapping_id, tenant_id, "
            "video_job_id, target_duration_ms, tier_duration_ms, trim_mode, "
            "trim_head_ms, trim_tail_ms, resulting_duration_ms, cost_basis, "
            "trim_policy, dialogue_duration_ms, action_window, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'tier',?,?,?,?)",
            (mapping_id, job["tenant_id"], job["video_job_id"], target,
             int(observed_ms), plan["trim_mode"], plan["trim_head_ms"],
             plan["trim_tail_ms"], plan["resulting_duration_ms"],
             trim_policy,
             job["dialogue_duration_ms"],
             _dumps(action) if action else None, _now()))
        return {
            "duration_mapping_id": mapping_id,
            "target_duration_ms": target,
            "tier_duration_ms": int(observed_ms),
            "trim_mode": plan["trim_mode"],
            "trim_head_ms": plan["trim_head_ms"],
            "trim_tail_ms": plan["trim_tail_ms"],
            "resulting_duration_ms": plan["resulting_duration_ms"],
            "cost_basis": "tier",
            "dialogue_duration_ms": job["dialogue_duration_ms"],
            "truncates_dialogue": plan["truncates_dialogue"],
            "truncates_action": plan["truncates_action"],
            "dialogue_window_unverified": unverified,
        }

    def _mapping_for_event(self, candidate):
        """把复用的候选读成 `_write_mapping` 同样的 dict（事件要用）。

        `truncates_*` / `dialogue_window_unverified` 不在 `duration_mapping` 里：
        它们是判定结果，分别落在 `video_edit_plan` 与 `clip_candidate` 上。这里
        从**已经落盘的那两行**取，不重新算一遍——重算会给出与已发布事件不同的
        答案，而同一个 artifact 只能有一个裁切解释。
        """
        row = _row(self.conn.execute(
            "SELECT * FROM duration_mapping WHERE duration_mapping_id = ?",
            (candidate["duration_mapping_id"],)).fetchone())
        if row is None:
            raise AssertionError("candidate lost its duration mapping")
        return {
            "duration_mapping_id": row["duration_mapping_id"],
            "target_duration_ms": int(row["target_duration_ms"]),
            "tier_duration_ms": int(row["tier_duration_ms"]),
            "trim_mode": row["trim_mode"],
            "trim_head_ms": int(row["trim_head_ms"]),
            "trim_tail_ms": int(row["trim_tail_ms"]),
            "resulting_duration_ms": int(row["resulting_duration_ms"]),
            "cost_basis": row["cost_basis"],
            "dialogue_duration_ms": row["dialogue_duration_ms"],
            "truncates_dialogue": bool(int(candidate["truncates_dialogue"])),
            "truncates_action": bool(int(candidate["truncates_action"])),
            "dialogue_window_unverified": bool(
                int(candidate["dialogue_window_unverified"])),
        }

    def _edit_plan_for_event(self, edit_plan_id):
        row = _row(self.conn.execute(
            "SELECT * FROM video_edit_plan WHERE edit_plan_id = ?",
            (edit_plan_id,)).fetchone())
        if row is None:
            raise AssertionError("candidate lost its edit plan")
        return {
            "edit_plan_id": row["edit_plan_id"],
            "trim": _loads(row["trim"], {}),
            "first_frame_handling": row["first_frame_handling"],
            "last_frame_handling": row["last_frame_handling"],
            "truncates_dialogue": bool(int(row["truncates_dialogue"])),
            "truncates_action": bool(int(row["truncates_action"])),
        }

    def _write_edit_plan(self, job, mapping, candidate_id):
        """`VideoEditPlan` 是**意图**，不是执行。`executed_by` 只能是 S42（T30-25）。

        `speed` 恒为 null（U6 未决）。`last_frame_handling` 契约只要求非空字符串，
        这里保持 `'keep'`，不引入臆造的闭集。

        候选 id 由调用方先生成再传进来：`clip_candidate_id` 是 NOT NULL，先写 `''`
        再回填会在两步之间留下一个指向不存在候选的编辑计划（虽然同事务，但读口
        在那段时间是可加的，没必要给对方看一个半成品）。
        """
        plan_id = _new_id("edit_plan")
        self.conn.execute(
            "INSERT INTO video_edit_plan (edit_plan_id, tenant_id, "
            "clip_candidate_id, revision, trim, speed, first_frame_handling, "
            "last_frame_handling, truncates_dialogue, truncates_action, "
            "executed_by, executed_at, created_at) "
            "VALUES (?,?,?,1,?,NULL,'keep','keep',?,?,NULL,NULL,?)",
            (plan_id, job["tenant_id"], candidate_id,
             _dumps({"head_ms": mapping["trim_head_ms"],
                     "tail_ms": mapping["trim_tail_ms"],
                     "resulting_duration_ms":
                         mapping["resulting_duration_ms"]}),
             1 if mapping["truncates_dialogue"] else 0,
             1 if mapping["truncates_action"] else 0, _now()))
        return {"edit_plan_id": plan_id,
                "trim": {"head_ms": mapping["trim_head_ms"],
                         "tail_ms": mapping["trim_tail_ms"],
                         "resulting_duration_ms":
                             mapping["resulting_duration_ms"]},
                "first_frame_handling": "keep",
                "last_frame_handling": "keep",
                "truncates_dialogue": mapping["truncates_dialogue"],
                "truncates_action": mapping["truncates_action"]}

    def _existing_candidate(self, tenant_id, artifact_ref):
        return _row(self.conn.execute(
            "SELECT * FROM clip_candidate WHERE tenant_id = ? AND "
            "json_extract(artifact_ref, '$.id') = ? AND "
            "json_extract(artifact_ref, '$.content_hash') = ?",
            (tenant_id, artifact_ref["id"],
             artifact_ref["content_hash"])).fetchone())

    def _insert_candidate(self, job, artifact_ref, media, mapping, edit_plan,
                          candidate_id):
        """候选不可变；同 artifact 被两个 job 产出 → 复用已有候选。"""
        drifted = bool(int(job["keyframe_drifted"]))
        requires_rehuman = mapping["truncates_dialogue"] \
            or mapping["truncates_action"]
        try:
            self.conn.execute(
                "INSERT INTO clip_candidate (clip_candidate_id, tenant_id, "
                "video_job_id, shot_lineage_id, segment_no, artifact_ref, "
                "observed_duration_ms, frame_rate, duration_mapping_id, "
                "edit_plan_id, keyframe_baseline_id, provider_job_id, "
                "attempt_id, stale_basis, truncates_dialogue, truncates_action, "
                "dialogue_window_unverified, requires_recheck, "
                "requires_rehuman_decision, repair_action_id, strategy_class, "
                "state, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'READY',?)",
                (candidate_id, job["tenant_id"], job["video_job_id"],
                 job["shot_lineage_id"], int(job["segment_no"]),
                 _jcs(artifact_ref), media["observed_duration_ms"],
                 media["frame_rate"], mapping["duration_mapping_id"],
                 edit_plan["edit_plan_id"], job["keyframe_baseline_id"],
                 job["provider_job_id"], job["attempt_id"] or "",
                 1 if drifted else 0,
                 1 if mapping["truncates_dialogue"] else 0,
                 1 if mapping["truncates_action"] else 0,
                 1 if mapping["dialogue_window_unverified"] else 0,
                 1 if drifted else 0,
                 1 if requires_rehuman else 0,
                 job["repair_action_id"], job["strategy_class"], _now()))
        except sqlite3.IntegrityError as exc:
            again = self._existing_candidate(job["tenant_id"], artifact_ref)
            if again is not None and "clip_candidate" in str(exc):
                return again
            raise
        return self._candidate_row(job["tenant_id"], candidate_id)

    def _candidate_row(self, tenant_id, candidate_id):
        row = _row(self.conn.execute(
            "SELECT * FROM clip_candidate WHERE tenant_id = ? "
            "AND clip_candidate_id = ?",
            (tenant_id, candidate_id)).fetchone())
        if row is None:
            raise AssertionError(f"candidate {candidate_id} vanished")
        return row

    # ------------------------------------------------------- 检查点 / 发布

    def _checkpoint_already_points_at(self, job, candidate):
        """指针是否已经指向这个候选。重投的幂等出口，不是新的权威。"""
        current = self._checkpoint_row(job)
        return (current is not None
                and current["clip_candidate_id"]
                == candidate["clip_candidate_id"])

    def _advance_checkpoint(self, job, candidate):
        """CAS 推进（§7.2）。返回 `{row, from_candidate_id}`；不该推进时返回 None。

        主键含 `segment_no`：拆镜后每个 segment 各有一条指针，互不影响。

        "不该推进"只有一种：新候选比当前指向的更旧（U3 沿用 S29：`created_at`
        当优先级）。**检查点不回退**——回退是人工动作，需要 S40 权限。
        """
        current = self._checkpoint_row(job)
        target_created = candidate["created_at"]
        if current is not None:
            holder = self.conn.execute(
                "SELECT created_at FROM clip_candidate WHERE tenant_id = ? "
                "AND clip_candidate_id = ?",
                (job["tenant_id"], current["clip_candidate_id"])).fetchone()
            if holder is not None and holder["created_at"] > target_created:
                return None

        at = _now()
        if current is None:
            self.conn.execute(
                "INSERT INTO shot_video_checkpoint (tenant_id, shot_lineage_id, "
                "segment_no, clip_candidate_id, checkpoint_seq, "
                "expected_revision, updated_by_job, updated_at) "
                "VALUES (?,?,?,?,1,0,?,?)",
                (job["tenant_id"], job["shot_lineage_id"],
                 int(job["segment_no"]), candidate["clip_candidate_id"],
                 job["video_job_id"], at))
            return {"row": self._checkpoint_row(job), "from_candidate_id": None}

        for _attempt in range(2):
            prior_id = current["clip_candidate_id"]
            seq = int(current["checkpoint_seq"]) + 1
            cur = self.conn.execute(
                "UPDATE shot_video_checkpoint SET clip_candidate_id = ?, "
                "checkpoint_seq = ?, expected_revision = expected_revision + 1, "
                "updated_by_job = ?, updated_at = ? WHERE tenant_id = ? "
                "AND shot_lineage_id = ? AND segment_no = ? "
                "AND checkpoint_seq = ?",
                (candidate["clip_candidate_id"], seq, job["video_job_id"], at,
                 job["tenant_id"], job["shot_lineage_id"],
                 int(job["segment_no"]), int(current["checkpoint_seq"])))
            if cur.rowcount == 1:
                return {"row": self._checkpoint_row(job),
                        "from_candidate_id": prior_id}
            current = self._checkpoint_row(job)
            if current is None:
                return None
        raise ServiceError(
            409, "CHECKPOINT_CAS_FAILED",
            "concurrent checkpoint advance lost the race twice; leaving the "
            "decision to S41/S08", retryable=True)

    def _checkpoint_row(self, job):
        return _row(self.conn.execute(
            "SELECT * FROM shot_video_checkpoint WHERE tenant_id = ? "
            "AND shot_lineage_id = ? AND segment_no = ?",
            (job["tenant_id"], job["shot_lineage_id"],
             int(job["segment_no"]))).fetchone())

    def _publish_ready(self, job, candidate, outbox, checkpoint, *, mapping,
                       edit_plan, from_candidate_id):
        """`production.video.ready`：**15 个必填键一个不少**，
        `additionalProperties: false`，子对象也全部闭合。

        `segment_no` 必须是 int（契约要求 integer）：字符串 "1" 会让下游算出
        另一个业务键。`duration_mapping` 只放契约里那 8 个键——`resolution_source`
        是 §2.4 伪代码里的字段，契约没有它，所以只在 GET 里当常量回显（C30-27）。
        """
        candidate_id = candidate["clip_candidate_id"]
        seq = int(checkpoint["checkpoint_seq"])
        baseline = _row(self.conn.execute(
            "SELECT * FROM keyframe_baseline_ref WHERE keyframe_baseline_id = ?",
            (candidate["keyframe_baseline_id"],)).fetchone()) \
            if candidate["keyframe_baseline_id"] is not None else None
        data = {
            "schema_version": SCHEMA_VERSION,
            "video_job_id": job["video_job_id"],
            "clip_candidate_id": candidate_id,
            "shot_lineage_id": job["shot_lineage_id"],
            "segment_no": int(job["segment_no"]),
            "artifact_ref": _loads(candidate["artifact_ref"], {}),
            "observed_duration_ms": int(candidate["observed_duration_ms"]),
            "frame_rate": float(candidate["frame_rate"]),
            "duration_mapping": {
                "target_duration_ms": int(mapping["target_duration_ms"]),
                "tier_duration_ms": int(mapping["tier_duration_ms"]),
                "trim_mode": mapping["trim_mode"],
                "trim_head_ms": int(mapping["trim_head_ms"]),
                "trim_tail_ms": int(mapping["trim_tail_ms"]),
                "resulting_duration_ms": int(mapping["resulting_duration_ms"]),
                "cost_basis": "tier",
                "dialogue_duration_ms": mapping["dialogue_duration_ms"],
            },
            "edit_plan_ref": {
                "id": edit_plan["edit_plan_id"],
                "revision": 1,
                "trim": {"head_ms": int(edit_plan["trim"]["head_ms"]),
                         "tail_ms": int(edit_plan["trim"]["tail_ms"]),
                         "resulting_duration_ms": int(
                             edit_plan["trim"]["resulting_duration_ms"])},
                "first_frame_handling": edit_plan["first_frame_handling"],
                "last_frame_handling": edit_plan["last_frame_handling"],
            },
            "checkpoint": {"from": from_candidate_id, "to": candidate_id,
                           "checkpoint_seq": seq},
            "keyframe_baseline": {
                "candidate_id": (baseline["candidate_id"]
                                 if baseline is not None else None),
                "checkpoint_seq": (int(baseline["checkpoint_seq"])
                                   if baseline is not None else None),
            },
            "stale_basis": bool(int(candidate["stale_basis"])),
            "requires_recheck": bool(int(candidate["requires_recheck"])),
            "flags": {
                "truncates_dialogue": bool(int(candidate["truncates_dialogue"])),
                "truncates_action": bool(int(candidate["truncates_action"])),
                "dialogue_window_unverified": bool(
                    int(candidate["dialogue_window_unverified"])),
            },
        }
        if baseline is None:
            # 文生视频（U8）没有基线。契约把 `keyframe_baseline` 列为必填对象，
            # 两个字段都不是 nullable——发 `{null, null}` 撞 schema，编一个候选
            # id 是把"没有基线"写成"有基线"。
            #
            # **不能因此回滚**：钱已经花了，候选是有效资产。这里只拒发事件，把
            # 缺口如实报给调用方（C30-28），候选与检查点照常落库，S41 仍可从
            # `GET /shots/{lineage}/video-checkpoint` 读到它。
            return {"published": False, "reason": "KEYFRAME_BASELINE_ABSENT",
                    "gap": "C30-28"}
        assert data["clip_candidate_id"] == data["checkpoint"]["to"]
        outbox.append(new_event(
            source=SOURCE, type=EVENT_VIDEO_READY,
            subject=f"clip-candidate/{candidate_id}",
            tenant_id=job["tenant_id"], home_cell="video-production",
            aggregate_id=job["shot_lineage_id"], sequence=seq,
            correlation_id=job["video_job_id"], data=data,
            schema_version=SCHEMA_VERSION))
        return {"published": True, "reason": None}

    def _publish_attention(self, job, candidate, outbox):
        """`video.clip-attention-needed`（C8：契约无此事件，先按本地名发）。

        它是给 S39/人看的**提示**，不是状态转移；候选仍是 READY、钱已经花了。
        """
        candidate_id = candidate["clip_candidate_id"]
        reasons = []
        if int(candidate["stale_basis"]):
            reasons.append("stale_basis")
        if int(candidate["truncates_dialogue"]):
            reasons.append("truncates_dialogue")
        if int(candidate["truncates_action"]):
            reasons.append("truncates_action")
        if int(candidate["requires_rehuman_decision"]):
            reasons.append("requires_rehuman_decision")
        outbox.append(new_event(
            source=SOURCE, type=LOCAL_CLIP_ATTENTION,
            subject=f"clip-candidate/{candidate_id}",
            tenant_id=job["tenant_id"], home_cell="video-production",
            aggregate_id=candidate_id, sequence=1,
            correlation_id=job["video_job_id"],
            data={"video_job_id": job["video_job_id"],
                  "clip_candidate_id": candidate_id,
                  "shot_lineage_id": job["shot_lineage_id"],
                  "segment_no": int(job["segment_no"]),
                  "reasons": reasons,
                  "dialogue_window_unverified": bool(
                      int(candidate["dialogue_window_unverified"]))},
            schema_version=SCHEMA_VERSION))

    # ------------------------------------------------------------- 读口

    def get_job(self, tenant_id, job_id):
        job = self._job(tenant_id, job_id)
        body = self._job_response(job)
        candidates = [self._candidate_body(r) for r in self.conn.execute(
            "SELECT * FROM clip_candidate WHERE tenant_id = ? AND video_job_id = ? "
            "ORDER BY created_at", (tenant_id, job_id)).fetchall()]
        mapping_row = _row(self.conn.execute(
            "SELECT * FROM duration_mapping WHERE video_job_id = ?",
            (job_id,)).fetchone())
        checkpoint = _row(self.conn.execute(
            "SELECT * FROM shot_video_checkpoint WHERE tenant_id = ? "
            "AND shot_lineage_id = ? AND segment_no = ?",
            (tenant_id, job["shot_lineage_id"],
             int(job["segment_no"]))).fetchone())
        history = [
            {"seq": int(r["seq"]), "from": r["from_state"], "to": r["to_state"],
             "at": r["at"], "reason_code": r["reason_code"]}
            for r in self.conn.execute(
                "SELECT * FROM job_transition WHERE video_job_id = ? ORDER BY seq",
                (job_id,)).fetchall()]
        frozen = job["state"] == "WAITING_UNKNOWN"
        body.update({
            "source_revision": int(_loads(job["shot_ref"], {}).get("revision")
                                   or 0),
            "business_attempt_no": int(job["business_attempt_no"]),
            "provider_job_state": _provider_state_of(job["state"]),
            "provider_state_source": "local_map",
            "frozen": frozen,
            "must_reconcile_before_resubmit": frozen,
            "duration_mapping": (None if mapping_row is None
                                 else self._mapping_body(mapping_row)),
            "candidates": candidates,
            "checkpoint": None if checkpoint is None else {
                "shot_lineage_id": checkpoint["shot_lineage_id"],
                "segment_no": int(checkpoint["segment_no"]),
                "clip_candidate_id": checkpoint["clip_candidate_id"],
                "checkpoint_seq": int(checkpoint["checkpoint_seq"]),
            },
            "failure": None if job["failure_code"] is None else {
                "code": job["failure_code"], "detail": job["failure_detail"]},
            "history": history,
        })
        return body

    @staticmethod
    def _mapping_body(row):
        """GET 的映射视图。

        `resolution_source` 是 §2.4 伪代码里的字段，DDL 与事件契约都没有它
        （C30-27）。这里作为**常量回显**，不存列、不进事件——否则就成了本服务
        在声明"这个值来自 S25"，而那个断言属于 S25 的 `normalized_input`。
        """
        return {
            "target_duration_ms": int(row["target_duration_ms"]),
            "tier_duration_ms": int(row["tier_duration_ms"]),
            "trim_mode": row["trim_mode"],
            "trim_head_ms": int(row["trim_head_ms"]),
            "trim_tail_ms": int(row["trim_tail_ms"]),
            "resulting_duration_ms": int(row["resulting_duration_ms"]),
            "cost_basis": row["cost_basis"],
            "dialogue_duration_ms": row["dialogue_duration_ms"],
            "resolution_source": "s25_normalized_input",
        }

    def list_jobs(self, tenant_id, *, shot_lineage_id=None, segment_no=None,
                  state=None, limit=100):
        sql = "SELECT * FROM video_job WHERE tenant_id = ?"
        params = [tenant_id]
        if shot_lineage_id:
            sql += " AND shot_lineage_id = ?"
            params.append(shot_lineage_id)
        if segment_no is not None:
            sql += " AND segment_no = ?"
            params.append(int(segment_no))
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        watermark = self.conn.execute(
            "SELECT MAX(state_seq) AS m FROM video_job WHERE tenant_id = ?",
            (tenant_id,)).fetchone()["m"]
        return {"items": [self._job_response(_row(r)) for r in rows],
                "projection_watermark": int(watermark or 0),
                "count": len(rows)}

    def get_checkpoint(self, tenant_id, shot_lineage_id, segment_no=None):
        """S41 的主读口。**不含 accepted 语义**——它只说"该拿哪一段视频"。"""
        sql = ("SELECT * FROM shot_video_checkpoint WHERE tenant_id = ? "
               "AND shot_lineage_id = ?")
        params = [tenant_id, shot_lineage_id]
        if segment_no is not None:
            sql += " AND segment_no = ?"
            params.append(int(segment_no))
        sql += " ORDER BY segment_no"
        rows = self.conn.execute(sql, params).fetchall()
        return {"shot_lineage_id": shot_lineage_id,
                "checkpoints": [
                    {"segment_no": int(r["segment_no"]),
                     "clip_candidate_id": r["clip_candidate_id"],
                     "checkpoint_seq": int(r["checkpoint_seq"]),
                     "updated_by_job": r["updated_by_job"],
                     "updated_at": r["updated_at"]}
                    for r in rows]}

    def get_candidate(self, tenant_id, candidate_id):
        """可追溯验收的读口：候选 + 完整映射链（§6.3）。"""
        row = _row(self.conn.execute(
            "SELECT * FROM clip_candidate WHERE tenant_id = ? "
            "AND clip_candidate_id = ?", (tenant_id, candidate_id)).fetchone())
        if row is None:
            raise ServiceError(404, "CLIP_CANDIDATE_NOT_FOUND",
                               f"no clip candidate {candidate_id}")
        body = self._candidate_body(row)
        job = self._job(tenant_id, row["video_job_id"])
        body["built_for"] = self._built_for(job)
        body["strategy_class"] = row["strategy_class"]
        body["reference_combination_hash"] = job["reference_combination_hash"]
        return body

    def get_edit_plan(self, tenant_id, candidate_id):
        row = _row(self.conn.execute(
            "SELECT * FROM clip_candidate WHERE tenant_id = ? "
            "AND clip_candidate_id = ?", (tenant_id, candidate_id)).fetchone())
        if row is None:
            raise ServiceError(404, "CLIP_CANDIDATE_NOT_FOUND",
                               f"no clip candidate {candidate_id}")
        plan = _row(self.conn.execute(
            "SELECT * FROM video_edit_plan WHERE edit_plan_id = ?",
            (row["edit_plan_id"],)).fetchone())
        if plan is None:
            raise ServiceError(404, "EDIT_PLAN_NOT_FOUND",
                               "this candidate has no edit plan")
        return {"clip_candidate_id": candidate_id,
                "edit_plan_id": plan["edit_plan_id"],
                "revision": int(plan["revision"]),
                "trim": _loads(plan["trim"], {}),
                "speed": _loads(plan["speed"], None),
                "first_frame_handling": plan["first_frame_handling"],
                "last_frame_handling": plan["last_frame_handling"],
                "truncates_dialogue": bool(int(plan["truncates_dialogue"])),
                "truncates_action": bool(int(plan["truncates_action"])),
                # 执行归 S42（`vep_executor`）。读口如实说明"还没执行"。
                "executed_by": plan["executed_by"],
                "executed_at": plan["executed_at"],
                "note": "planning only; S42 executes"}

    # -------------------------------------------------------- 取消 / 重试

    def cancel_job(self, tenant_id, job_id, body, command_id):
        """§6.3。只调 S26 cancel，**不承诺退款**。"""
        def produce():
            job = self._job(tenant_id, job_id)
            if job["state"] == "WAITING_UNKNOWN":
                # §4.5：UNKNOWN 期间既不重提也不取消——未收单的取消没意义，
                # 已收单的可能仍在计费。先去对账。
                raise ServiceError(
                    409, "MUST_RECONCILE_FIRST",
                    "job is WAITING_UNKNOWN; reconcile with S26 before "
                    "cancelling or resubmitting")
            if job["state"] in TERMINAL_STATES:
                raise ServiceError(409, "JOB_ALREADY_TERMINAL",
                                   f"job is {job['state']}")
            outbox = self.outbox
            if job["provider_job_id"]:
                try:
                    self.canceler(job["provider_job_id"],
                                  body.get("reason") or "video_canceled")
                except (ServiceError, ConnectionError) as exc:
                    # 取消没被确认就不能标 CANCELED——那等于断言"它确定没在
                    # 做"，而它可能正在做并照样收费。保持原状态。
                    return {"video_job_id": job_id, "state": job["state"],
                            "cancel_state": "CANCEL_PENDING",
                            "refund_promised": False,
                            "note": f"cancellation unconfirmed: {exc}"}
            job = self._transition(
                job, "CANCELED",
                reason_code=body.get("reason") or "REQUESTED", outbox=outbox)
            return {"video_job_id": job_id, "state": job["state"],
                    "cancel_state": "CANCELED", "refund_promised": False}

        return self._command(tenant_id, command_id, produce)

    def retry_job(self, tenant_id, job_id, body, command_id):
        """§6.3。只有 `{FAILED, RECONCILED_FAILED}` 能重试。

        `WAITING_UNKNOWN` 必须先把 S26 的结论拿回来——盲目重提付费任务正是
        AGENTS.md 的红线，也是 `video_job_open_uniq` 在 API 层的落点。

        原地重试：bump `business_attempt_no`、换 `variation_nonce`、清失败码。
        修复幂等键 `(tenant, repair_action_id)` 因此仍然成立。
        """
        def produce():
            job = self._job(tenant_id, job_id)
            if job["state"] == "WAITING_UNKNOWN":
                raise ServiceError(
                    409, "MUST_RECONCILE_FIRST",
                    "job is WAITING_UNKNOWN; resubmitting could pay twice")
            if job["state"] not in RETRYABLE_STATES:
                raise ServiceError(
                    409, "JOB_NOT_RETRYABLE",
                    f"job is {job['state']}; only {sorted(RETRYABLE_STATES)} "
                    "can be retried", details={"state": job["state"]})
            self.conn.execute(
                "UPDATE video_job SET business_attempt_no = "
                "business_attempt_no + 1, variation_nonce = ?, "
                "failure_code = NULL, failure_detail = NULL, reconciled = 0, "
                "updated_at = ? WHERE video_job_id = ?",
                (uuid.uuid4().hex, _now(), job_id))
            job = self._job(tenant_id, job_id)
            outbox = self.outbox
            job = self._transition(job, "PREPARING", reason_code="RETRY",
                                   allow_retry=True, outbox=outbox)
            return self._dispatch(job, outbox)

        return self._command(tenant_id, command_id, produce)

    # ---------------------------------------------- 关键帧漂移 / 对白时长

    def on_image_ready(self, envelope):
        """S29 `production.image.ready` → 标记基线漂移（§2.5 / F17 / T30-19）。

        两件事，都不动候选本身：

        1. 把**未终结** job 的 `keyframe_drifted` 置 1：它落定时据此置
           `stale_basis` / `requires_recheck`。
        2. 对**已经落定**的候选补 `stale_basis` + `requires_recheck`：候选写入
           与这次推进并发时，"写候选后再判 stale"（§7.2）才能覆盖到。

        **不自动重生成。** 重生成是 S39 的决策（§2.5）。
        """
        event_id = envelope.get("id") or ""
        if not event_id:
            raise ServiceError(400, "INVALID_BODY",
                               "image ready event carries no id")
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        lineage = data.get("shot_lineage_id")
        checkpoint = data.get("checkpoint") or {}
        seq = checkpoint.get("checkpoint_seq")
        if not tenant_id or not lineage or not isinstance(seq, int):
            return {"ignored": True, "reason": "not_shaped_for_video"}

        with self._tx():
            if not self.inbox.consume("S30", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            jobs = self.conn.execute(
                "SELECT * FROM video_job WHERE tenant_id = ? "
                "AND shot_lineage_id = ? AND state NOT IN "
                "('READY','RECONCILED_READY','REJECTED_LOCAL','FAILED',"
                "'RECONCILED_FAILED','CANCELED')",
                (tenant_id, lineage)).fetchall()
            drifted_jobs = []
            for row in jobs:
                job = _row(row)
                baseline = None
                if job["keyframe_baseline_id"] is not None:
                    baseline = _row(self.conn.execute(
                        "SELECT checkpoint_seq FROM keyframe_baseline_ref "
                        "WHERE keyframe_baseline_id = ?",
                        (job["keyframe_baseline_id"],)).fetchone())
                if baseline is None:
                    # 文生视频没有基线可漂移；不因为"关键帧变了"把它标 stale。
                    continue
                if int(baseline["checkpoint_seq"]) >= int(seq):
                    continue
                self.conn.execute(
                    "UPDATE video_job SET keyframe_drifted = 1, updated_at = ? "
                    "WHERE video_job_id = ?", (_now(), job["video_job_id"]))
                drifted_jobs.append(job["video_job_id"])

            backfilled = []
            candidates = self.conn.execute(
                "SELECT c.* FROM clip_candidate c "
                "JOIN keyframe_baseline_ref b "
                "ON c.keyframe_baseline_id = b.keyframe_baseline_id "
                "WHERE c.tenant_id = ? AND c.shot_lineage_id = ? "
                "AND b.checkpoint_seq < ? AND c.stale_basis = 0",
                (tenant_id, lineage, int(seq))).fetchall()
            for row in candidates:
                candidate = _row(row)
                self.conn.execute(
                    "UPDATE clip_candidate SET stale_basis = 1, "
                    "requires_recheck = 1 WHERE clip_candidate_id = ?",
                    (candidate["clip_candidate_id"],))
                backfilled.append(candidate["clip_candidate_id"])
                job = self._job(tenant_id, candidate["video_job_id"])
                self._publish_attention(job, {
                    **candidate, "stale_basis": 1, "requires_recheck": 1},
                    self.outbox)

        return {"drifted_jobs": drifted_jobs,
                "backfilled_candidates": backfilled,
                "auto_regenerated": False}

    def on_speech_ready(self, envelope):
        """S31 `production.speech.ready` → `silent_then_tts` 的对白时长（§7.2）。

        只影响**还没落定**的 job：映射在 `INGESTING → READY` 时一次性确定
        （§7.2），之后改它会让 S42 拿到的裁切计划与已发布的 `edit_plan_ref`
        不一致。已经 READY 的 job 只做记录，不改。

        这里**不重算、不重生成**：`dialogue_duration_ms` 的合法性（≤ target）
        在入口已经校验过；新值超长意味着上游台词变了，那是 S21/S25 的问题。
        """
        event_id = envelope.get("id") or ""
        if not event_id:
            raise ServiceError(400, "INVALID_BODY",
                               "speech ready event carries no id")
        data = envelope.get("data") or {}
        tenant_id = envelope.get("tenant_id")
        lineage = data.get("shot_lineage_id")
        duration = data.get("audio_duration_ms")
        if not tenant_id or not lineage:
            return {"ignored": True, "reason": "not_shaped_for_video"}
        if isinstance(duration, bool) or not isinstance(duration, int) \
                or duration <= 0:
            return {"ignored": True, "reason": "no_usable_duration"}

        with self._tx():
            if not self.inbox.consume("S30", event_id):
                return {"applied": False, "reason": "DUPLICATE"}
            rows = self.conn.execute(
                "SELECT * FROM video_job WHERE tenant_id = ? "
                "AND shot_lineage_id = ? AND audio_strategy = 'silent_then_tts' "
                "AND state IN ('CREATED','PREPARING')",
                (tenant_id, lineage)).fetchall()
            updated = []
            for row in rows:
                job = _row(row)
                self.conn.execute(
                    "UPDATE video_job SET dialogue_duration_ms = ?, "
                    "updated_at = ? WHERE video_job_id = ?",
                    (duration, _now(), job["video_job_id"]))
                updated.append(job["video_job_id"])
        return {"updated_jobs": updated}

    # ------------------------------------------------------------ 重启恢复

    def recover_non_terminal(self, tenant_id=None):
        """F19 / §7.3：从 S26 拉权威状态，**不从内存恢复任何东西**。

        `INGESTING` 的 job 恢复时必须重新确认 `observed_duration_ms`：本地没有
        缓存可沿用的值，候选要等一条新的 `artifact.ingested`。所以这里只做
        "再催一次导入"，不写候选、不推检查点。
        """
        sql = ("SELECT * FROM video_job WHERE state IN "
               "('SUBMITTED','WAITING_PROVIDER','WAITING_UNKNOWN','INGESTING')")
        params = []
        if tenant_id:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        rows = [_row(r) for r in self.conn.execute(sql, params).fetchall()]
        report = {"scanned": len(rows), "reconciled": [], "imports_retried": [],
                  "errors": []}
        for job in rows:
            if not job["provider_job_id"]:
                report["errors"].append(
                    {"video_job_id": job["video_job_id"],
                     "reason": "no_provider_job_id"})
                continue
            try:
                snapshot = self._upstream(
                    lambda j=job: self.generation_state(j["tenant_id"],
                                                        j["provider_job_id"]),
                    "GENERATION_JOBS_UNAVAILABLE")
            except ServiceError as exc:
                report["errors"].append(
                    {"video_job_id": job["video_job_id"],
                     "reason": f"{exc.code}: {exc}"})
                continue
            state = str((snapshot or {}).get("state") or "")
            with self._tx():
                fresh = self._job(job["tenant_id"], job["video_job_id"])
                result = self._apply_provider_state(
                    fresh["tenant_id"], fresh["provider_job_id"],
                    {"state": state, **(snapshot or {})})
            pending = result.pop("_request_import", None)
            if pending is not None:
                result = self._request_import(pending, result)
                report["imports_retried"].append(job["video_job_id"])
            if fresh["state"] == "INGESTING":
                result = self._request_import(
                    self._job(job["tenant_id"], job["video_job_id"]), result)
                report["imports_retried"].append(job["video_job_id"])
            report["reconciled"].append(
                {"video_job_id": job["video_job_id"],
                 "state": result.get("state")})
        return report

    # ------------------------------------------------------------- 视图

    def _built_for(self, job):
        """`built_for`：这段视频照着哪一版输入造的。

        `manifest_ref` 来自**已签名的 context**（`{id, revision, content_hash}`），
        所以不另存列。`node_epoch_input` 与 `execution_fence` 分别属于单节点与
        run/branch 两个作用域，**不互相派生**（AGENTS.md）。
        """
        ctx = _loads(job["execution_context"], {})
        return {"manifest_ref": ctx.get("manifest_ref") or {},
                "node_epoch_input": int(job["node_epoch_at_create"]),
                "execution_fence": int(job["execution_fence"])}

    def _candidate_body(self, row):
        mapping = _row(self.conn.execute(
            "SELECT * FROM duration_mapping WHERE duration_mapping_id = ?",
            (row["duration_mapping_id"],)).fetchone())
        plan = _row(self.conn.execute(
            "SELECT * FROM video_edit_plan WHERE edit_plan_id = ?",
            (row["edit_plan_id"],)).fetchone())
        baseline = None
        if row["keyframe_baseline_id"] is not None:
            baseline = _row(self.conn.execute(
                "SELECT * FROM keyframe_baseline_ref WHERE keyframe_baseline_id = ?",
                (row["keyframe_baseline_id"],)).fetchone())
        return {
            "clip_candidate_id": row["clip_candidate_id"],
            "video_job_id": row["video_job_id"],
            "shot_lineage_id": row["shot_lineage_id"],
            "segment_no": int(row["segment_no"]),
            "artifact_ref": _loads(row["artifact_ref"], {}),
            "observed_duration_ms": int(row["observed_duration_ms"]),
            "frame_rate": float(row["frame_rate"]),
            "duration_mapping": (None if mapping is None
                                 else self._mapping_body(mapping)),
            "edit_plan_ref": (None if plan is None else {
                "id": plan["edit_plan_id"],
                "revision": int(plan["revision"]),
                "trim": _loads(plan["trim"], {}),
                "first_frame_handling": plan["first_frame_handling"],
                "last_frame_handling": plan["last_frame_handling"]}),
            "keyframe_baseline": (None if baseline is None else {
                "candidate_id": baseline["candidate_id"],
                "checkpoint_seq": int(baseline["checkpoint_seq"]),
                "content_hash": baseline["content_hash"]}),
            "stale_basis": bool(int(row["stale_basis"])),
            "truncates_dialogue": bool(int(row["truncates_dialogue"])),
            "truncates_action": bool(int(row["truncates_action"])),
            "dialogue_window_unverified": bool(
                int(row["dialogue_window_unverified"])),
            "requires_recheck": bool(int(row["requires_recheck"])),
            "requires_rehuman_decision": bool(
                int(row["requires_rehuman_decision"])),
            "repair_action_id": row["repair_action_id"],
            "strategy_class": row["strategy_class"],
            "provider_job_id": row["provider_job_id"],
            "attempt_id": row["attempt_id"],
            "state": row["state"],
            "created_at": row["created_at"],
        }

    def _job_response(self, job, *, extra=None):
        body = {
            "video_job_id": job["video_job_id"],
            "state": job["state"],
            "state_seq": int(job["state_seq"]),
            "shot_lineage_id": job["shot_lineage_id"],
            "segment_no": int(job["segment_no"]),
            "duration_policy": job["duration_policy"],
            "audio_strategy": job["audio_strategy"],
            "target_duration_ms": int(job["target_duration_ms"]),
            "submitted_duration_ms": (None if job["submitted_duration_ms"] is None
                                      else int(job["submitted_duration_ms"])),
            "input_fingerprint": job["input_fingerprint"],
            "reference_combination_hash": job["reference_combination_hash"],
            "provider_job_id": job["provider_job_id"],
            "route_decision_ref": _loads(job["route_decision_ref"], None),
            "failure_code": job["failure_code"],
        }
        body.update(extra or {})
        return body

    def duration_mapping_preview(self, job):
        """§6.1 的 202 回显。**不是承诺**：真实映射要等 S25 的档位回来。

        回显里刻意只有一个数字加一句 note，不给 `tier_duration_ms`——给了就会
        有人把它当成"最终会生成 5 秒"。
        """
        return {"target_duration_ms": int(job["target_duration_ms"]),
                "note": "tier snap and trim are decided after PREPARING; "
                        "this is the requested value, not a commitment"}
