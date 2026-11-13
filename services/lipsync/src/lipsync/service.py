"""S32 内核。方案：`plans/S32-lipsync.md`。

一句话：**口型是局部事实，不是整帧重绘。** 全文件围绕这一句的五个可验证后果：

1. **有效期是算出来的。** 没有 `valid_until`、没有刷新入口。一个 take 对
   `(video_hash, audio_hash, window)` 有效，三项都在不可变的
   `input_fingerprint` 里。`validity()` 现场做三路合取：指纹相等
   ∧ S31 说音频权利仍 usable ∧ S28 说输入仍可引用。
2. **"不许动别人的脸"由 CHECK + 度量共同钉死。** `preserve_other_faces` 在
   表上是 `CHECK (= 1)`，写不进 false；非目标区域的逐像素最大差在 `INGESTING`
   阶段量，> warn 就 `FAILED(OTHER_FACE_ALTERED)` 且**不登记 take**。这不是
   QC，是权利约束。
3. **音频错了就不做口型。** S37 有未关闭的 major/blocker `AUD.TEXT.*` →
   `409 AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC`，带 `suggested_action=
   re_synthesize_speech` / `suggested_service=S31` / 缺陷 id / 报告引用。没有
   报告时**允许但打标** `voice_defect_check='not_yet_available'`，并把这件事
   写进 take 与 ready 事件。
4. **付费只走 S26。** 本文件没有供应商 URL、没有 HTTP；提交是一次注入的
   `submitter` 调用。路由来自 S25。不做"另一个渠道再试一次"——那属于 S24/S25。
5. **两个版本号从不混用。** `execution_fence`（run/branch）与
   `node_epoch_at_create`（单个 logical_node）是两列，只透传，不比较、不换算，
   也**不**用于推进任何接受指针。本服务没有接受指针，也不写 S08 的表。

三条容易写错、这里显式写死的语义：

- **`INPUTS_INELIGIBLE` 与 `REJECTED_LOCAL` 不合并。** 前者是"这份输入不该被
  做口型"，责任在上游（S31/S30/S10），回 409 并指向正确动作；后者是"请求参数
  或能力不允许"，责任在调用方，job 存在但没花钱。
- **`SUBMISSION_UNKNOWN` 冻结，绝不再提。** 与 S26/S30 同语义；对账成功也
  不等于结果可用——输入对变了就标 stale，不推进检查点。
- **过期不是删除。** take 只被标 `stale_basis`，没有 DELETE 路径。

事务与幂等与 S31 同构：平台幂等 SDK 的 `begin/finish` 是裸语句，命令事务内把
结果写回同一行 `idempotency_record`，"提交"与"记下已提交"原子成立。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    ALLOWED_VIDEO_ORIGINS,
    BLOCKING_SEVERITIES,
    EPS_PIXEL,
    HANDLE_FRAMES,
    ID_PREFIX,
    JOB_STATES,
    MASK_DILATE_PX,
    MAX_FRAMES_PER_JOB,
    MIN_FACE_PX,
    NON_BLOCKING_TEXT_CODES,
    OWNED_ACTIONS,
    RETRYABLE_STATES,
    SCHEMA,
    SELF_ORIGIN,
    TERMINAL_STATES,
    VISIBILITY_ABSENT,
    VISIBILITY_FULL,
    VISIBILITY_SPARSE,
    VOICE_ACTION,
    WARN_PIXEL,
    WINDOW_SOURCES,
)

SOURCE = "S32"
SCHEMA_VERSION = "2.0"

# 已登记事件：`contracts/event-topics.v2.json` 里只有这一个。
EVENT_LIPSYNC_READY = "production.lipsync.ready"

# 方案 §6.4 自己提出、但**没有登记**的四个（缺口 G13）。用本地类型名发出，
# 不冒充登记事件——订阅方按 event-topics 写路由，发了也进不了他们的表。
LOCAL_JOB_STATE = "lipsync.job.state-changed"
LOCAL_INPUTS_INELIGIBLE = "lipsync.inputs-ineligible"
LOCAL_TAKE_STALE = "lipsync.take.stale"
LOCAL_REPEAT_WITHOUT_CHANGE = "lipsync.repeat-without-change"

# §3.1 的转移表。放在模块级而不是散在 if 里，是为了让"独立升级回滚后状态机
# 一致"（T32-53）可以对着同一张表断言，而不是对着代码再读一遍。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"CHECKING_INPUTS", "CANCELED", "FAILED"}),
    "CHECKING_INPUTS": frozenset({"INPUTS_INELIGIBLE", "REJECTED_LOCAL",
                                  "PREPARING", "CANCELED"}),
    # PREPARING → WAITING_UNKNOWN 是 E16 的主路径：`submitter` 超时或 S26 收下
    # 命令却不给可对账 id 时，提交结果不明——必须能冻结在 UNKNOWN，不能因为
    # 状态机少一条边就把这笔钱当成"没花"。
    "PREPARING": frozenset({"REJECTED_LOCAL", "SUBMITTED", "WAITING_UNKNOWN",
                            "CANCELED"}),
    "SUBMITTED": frozenset({"WAITING_PROVIDER", "FAILED", "WAITING_UNKNOWN",
                            "CANCELED"}),
    "WAITING_PROVIDER": frozenset({"INGESTING", "FAILED", "WAITING_UNKNOWN",
                                   "CANCELED"}),
    "WAITING_UNKNOWN": frozenset({"INGESTING", "RECONCILED_READY",
                                  "RECONCILED_FAILED", "FAILED"}),
    "INGESTING": frozenset({"READY", "FAILED", "WAITING_UNKNOWN", "CANCELED"}),
    "READY": frozenset(),
    "INPUTS_INELIGIBLE": frozenset(),
    "REJECTED_LOCAL": frozenset(),
    "FAILED": frozenset({"PREPARING"}),
    "RECONCILED_READY": frozenset(),
    "RECONCILED_FAILED": frozenset({"PREPARING"}),
    "CANCELED": frozenset(),
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_ts(value) -> datetime | None:
    """ISO-8601 → 带时区的 `datetime`。**不能拿字符串比大小。**

    `deadline` 可能带 `+08:00` 偏移而 `_now()` 是 `Z`：字节比较会把早 8 小时的
    死线看成未到。解析不了的返回 None，由调用方保守处理。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _deadline_passed(deadline) -> bool:
    """解析不了按"已过"处理：宁可拒一个合法请求，也不为一个说不清时间点的
    请求去花一次钱。"""
    parsed = _parse_ts(deadline)
    if parsed is None:
        return True
    return parsed <= datetime.now(timezone.utc)


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _loads(raw, default=None):
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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


# ------------------------------------------------------------ 纯函数

def input_fingerprint(*, video_hash: str, audio_hash: str,
                      window_start_ms: int, window_end_ms: int,
                      trim_head_ms: int, trim_tail_ms: int,
                      process_frame_start: int, process_frame_end: int,
                      fps: float, face_track_hash: str) -> str:
    """§4.2 / §2.4：有效期的作用域就是这十项。

    用 `x1f` 分隔而不是直接拼接：直接拼会让 `("ab","c")` 与 `("a","bc")`
    撞成同一个指纹。

    不进来的那些（`variation_nonce` / `attempt_id` / `provider` / `model_id` /
    `command_id` / `trace_id` / `deadline` / `budget_scope_id` /
    `node_epoch` / `execution_fence` / `manifest_revision`）都是"怎么做的"，
    不是"做的是什么"。它们一旦进来，重跑整集会全部重新付费（E38）。

    `face_track_revision` 以内容 hash 的形式进来：换了人脸轨可能意味着之前
    框错了脸（§7.2 最后一行），这是一次不同的生产。
    """
    return _sha256_hex("\x1f".join([
        video_hash,
        audio_hash,
        str(int(window_start_ms)),
        str(int(window_end_ms)),
        str(int(trim_head_ms)),
        str(int(trim_tail_ms)),
        str(int(process_frame_start)),
        str(int(process_frame_end)),
        repr(float(fps)),
        face_track_hash,
    ]))


def frame_range(*, start_ms: int, end_ms: int, fps: float,
                frame_count: int | None) -> tuple[int, int, int]:
    """§2.4 的处理帧范围，返回 `(start, end, processed)`，**闭区间计数**。

    `handle_frames` 前后各留 2 帧：嘴部运动是连续的，在音频起点那一帧嘴已经
    在动，模型需要看到开口前的几帧才能推断运动方向。不留余量会在窗口边界
    产生突跳，被 S36 报成 temporal，然后被误判成"口型失败"。

    `frame_count` 给出时把尾部裁到最后一帧：否则"窗口正好到片尾"会要求
    处理不存在的帧。裁掉的帧数会体现在 `processed` 里，不藏在别处。
    """
    start = math.floor(start_ms * fps / 1000.0) - HANDLE_FRAMES
    end = math.ceil(end_ms * fps / 1000.0) + HANDLE_FRAMES
    if start < 0:
        start = 0
    if frame_count is not None and end > frame_count - 1:
        end = frame_count - 1
    return start, end, end - start + 1


def visibility_grade(visible_frames: int, window_frames: int,
                     min_face_px: int) -> tuple[str, str | None]:
    """§4.7。返回 `(grade, reject_code)`；`reject_code` 非空即拒绝。

    顺序是语义：`absent` 先于 `too_small`（一个根本没出现在窗口里的脸，
    它的像素尺寸不构成独立理由），`too_small` 先于 `sparse`（脸太小是
    能力问题，不能靠多几帧可见补上）。

    低可见度**不"尽力而为"**：模型在 85% 的帧上没有依据，产出要么是凭空
    长出的嘴，要么是"看起来做了但其实没做"。两种都不是"精确通过"。
    """
    if window_frames <= 0:
        return "absent", "NO_FACE_IN_WINDOW"
    ratio = visible_frames / float(window_frames)
    if visible_frames <= 0 or ratio < VISIBILITY_ABSENT:
        return "absent", "NO_FACE_IN_WINDOW"
    if min_face_px < MIN_FACE_PX:
        return "too_small", "FACE_TOO_SMALL"
    if ratio < VISIBILITY_SPARSE:
        return "sparse", "LOW_FACE_VISIBILITY"
    if ratio < VISIBILITY_FULL:
        return "partial", None
    return "full", None


def classify_other_region(max_diff: float) -> tuple[str, str | None]:
    """§4.6 的三档。返回 `(drift, failure_code)`。

    用逐像素最大差而不是 PSNR/SSIM：全局指标会被大片未变区域稀释。一张脸
    被重绘了，但在 1920x1080 里只占 3%，SSIM 可能还有 0.998，看起来完美。
    """
    if max_diff > WARN_PIXEL:
        return "none", "OTHER_FACE_ALTERED"
    if max_diff <= EPS_PIXEL:
        return "none", None
    return "minor", None


def strategy_class_for(*, action: str, model_family: str | None,
                       window_source: str, window_changed: bool | None,
                       face_track_revision) -> str:
    """§8.2。`window_changed` 是这条的关键：它把"改了窗口再试"与"什么都没改
    又试了一次"分成两个策略类，前者是一次真实的诊断步骤，后者是空转。

    `generate` 不归一——首轮不是一次策略尝试。
    """
    if action != "redo_lipsync":
        return "generate"
    return ":".join([
        "redo_lipsync",
        f"model_family={model_family or 'unknown'}",
        f"window_source={window_source}",
        f"window_changed={'true' if window_changed else 'false'}",
        f"face_track_revision={face_track_revision}",
    ])


def _ref_text(ref) -> str:
    if ref is None:
        return ""
    if isinstance(ref, str):
        return ref
    return _jcs(ref)


def _map_provider_failure(state: str, reason: str | None) -> str:
    """S26 失败 → 本地 `failure_code`。**透传，不换渠道。**

    F25/F26 的"不换渠道"在代码里就是"这里只有一个映射函数，没有 fallback
    分支"。换渠道等于换一个口型实现，属于 S24/S25 的决定。
    """
    if state == "CANCELED":
        return "CANCELED_BY_PROVIDER"
    mapping = {"POLICY_REJECTED": "POLICY_REJECTED",
               "ACCOUNT_UNAUTHORIZED": "ACCOUNT_UNAUTHORIZED",
               "BUDGET": "BUDGET", "QUOTA": "QUOTA",
               "INVALID_VIDEO": "INVALID_VIDEO",
               "VIDEO_TOO_LONG": "VIDEO_TOO_LONG"}
    if reason in mapping:
        return mapping[reason]
    return "PROVIDER_FAILED"


def _op_id(job_id: str) -> str:
    return ID_PREFIX["operation"] + _sha256_hex(job_id)[:20]


def _blocking_defects(defects: list) -> tuple[list[dict], list[dict]]:
    """§4.3：只有 `AUD.TEXT.*` 且 severity ∈ {major, blocker} 且**未关闭**
    的才算阻断；`SCRIPT_CONFLICT` 明确"不产生缺陷"，即使有 severity 也放行。

    返回值是 `(blocking, ignored)`，ignored 用于解释"为什么有缺陷还放行"。
    """
    blocking, ignored = [], []
    for defect in defects or []:
        if not isinstance(defect, dict):
            continue
        status = str(defect.get("status") or "open").lower()
        code = str(defect.get("code") or "")
        severity = str(defect.get("severity") or "").lower()
        if status in ("closed", "resolved", "dismissed", "fixed", "waived"):
            ignored.append({**defect, "why": "closed"})
            continue
        if code in NON_BLOCKING_TEXT_CODES:
            ignored.append({**defect, "why": "not_a_defect"})
            continue
        if not code.startswith("AUD.TEXT."):
            ignored.append({**defect, "why": "not_a_text_defect"})
            continue
        if severity not in BLOCKING_SEVERITIES:
            ignored.append({**defect, "why": f"severity:{severity or 'unknown'}"})
            continue
        blocking.append(defect)
    return blocking, ignored


class LipsyncService:
    """所有上游都是注入的 callback（见 `deps.py`）。本文件不 import 任何服务。"""

    def __init__(self, conn: sqlite3.Connection, *, eligibility,
                 route_resolver, submitter, canceler, artifact_facts,
                 artifact_check, url_refresher, rights_reader, diff_measurer,
                 generation_state=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 会**隐式提交当前事务**。所以只能在事务外建一次，之后复用同一个实例——
        # 否则命令事务会在第一条事件写入时被悄悄提交掉，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        self.eligibility = eligibility
        self.route_resolver = route_resolver
        self.submitter = submitter
        self.canceler = canceler
        self.artifact_facts = artifact_facts
        self.artifact_check = artifact_check
        self.url_refresher = url_refresher
        self.rights_reader = rights_reader
        self.diff_measurer = diff_measurer
        self.generation_state = generation_state

    # -------------------------------------------------------- 事务助手
    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _transition(self, job: dict, to_state: str, *, reason_code=None,
                    detail=None, outbox: Outbox | None = None,
                    enforce: bool = True) -> dict:
        """状态转移 + 转移历史 + 本地状态事件。

        `enforce=True` 时对照 `ALLOWED_TRANSITIONS`：非法边直接断言失败，而不是
        悄悄写进去。新增状态忘了进表，会在第一次走那条边时就报出来——这正是
        T32-53（独立升级回滚后状态机一致）想要的性质。
        """
        if to_state not in JOB_STATES:
            raise AssertionError(f"unknown state {to_state}")
        current = job["state"]
        if enforce and to_state not in ALLOWED_TRANSITIONS.get(current,
                                                              frozenset()):
            raise AssertionError(f"illegal transition {current} -> {to_state}")
        seq = int(job["state_seq"]) + 1
        now = _now()
        self.conn.execute(
            "UPDATE lipsync_job SET state=?, state_seq=?, updated_at=?, "
            "state_entered_at=?, failure_code=COALESCE(?, failure_code), "
            "failure_detail=COALESCE(?, failure_detail) WHERE lipsync_job_id=?",
            (to_state, seq, now, now, reason_code, detail,
             job["lipsync_job_id"]))
        self.conn.execute(
            "INSERT INTO job_transition (lipsync_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job["lipsync_job_id"], seq, current, to_state, reason_code,
             detail, now))
        if outbox is not None:
            outbox.append(new_event(
                source=SOURCE, type=LOCAL_JOB_STATE,
                subject=f"lipsync-job/{job['lipsync_job_id']}",
                tenant_id=job["tenant_id"], home_cell="lipsync",
                aggregate_id=job["lipsync_job_id"], sequence=seq,
                correlation_id=job["lipsync_job_id"],
                data={"lipsync_job_id": job["lipsync_job_id"],
                      "shot_lineage_id": job["shot_lineage_id"],
                      "speaker_character_id": job["speaker_character_id"],
                      "from": current, "to": to_state,
                      "reason_code": reason_code},
                schema_version=SCHEMA_VERSION))
        return {**job, "state": to_state, "state_seq": seq}

    def _next_sequence(self, tenant_id: str, aggregate_id: str) -> int:
        """非 job 聚合的下一序号：**按流**取 `MAX(sequence)+1`。

        `sequence` 的语义是"同一条流里的第几条"，不是全局计数（docs/04：
        消费者靠聚合序号发现缺口）。job 流可以直接用 `state_seq`，但镜头流
        上有多个说话人、take 流上有多次过期，拿某一个 job 的 `state_seq`
        会在同镜第二个说话人 READY 时**重复发同一个序号** —— 消费者随后无法
        区分"重复投递"与"丢了一条"。

        取值来自 outbox 自己（这条流自己的日志），不是第二份权威；所有写入
        都在 `BEGIN IMMEDIATE` 事务里，所以读-写之间不会被插队。
        """
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS s FROM outbox "
            "WHERE tenant_id=? AND aggregate_id=?",
            (tenant_id, aggregate_id)).fetchone()
        return int(row["s"]) + 1

    def _job(self, tenant_id: str, job_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM lipsync_job WHERE tenant_id=? AND lipsync_job_id=?",
            (tenant_id, job_id)).fetchone())
        if row is None:
            raise ServiceError(404, "LIPSYNC_JOB_NOT_FOUND", f"no job {job_id}")
        return row

    def _take(self, tenant_id: str, take_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM lipsync_take WHERE tenant_id=? AND "
            "lipsync_take_id=?", (tenant_id, take_id)).fetchone())
        if row is None:
            raise ServiceError(404, "LIPSYNC_TAKE_NOT_FOUND",
                               f"no take {take_id}")
        return row

    # -------------------------------------------------------- 校验（§4.1 a–h）

    @staticmethod
    def _validate_context(body: dict, tenant_id: str) -> dict:
        """(a) 平台签名上下文。`tenant_id` 只比对，不赋值。"""
        ctx = body.get("context") or {}
        if not isinstance(ctx, dict):
            raise ServiceError(422, "INVALID_CONTEXT", "context must be object")
        if ctx.get("tenant_id") != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id does not match the credential")
        for key in ("project_id", "series_id", "episode_id", "branch_id",
                    "node_id"):
            if not ctx.get(key):
                raise ServiceError(422, "INVALID_CONTEXT",
                                   f"context.{key} required")
        for key in ("node_epoch", "execution_fence"):
            value = ctx.get(key)
            if not _is_int(value) or value < 1:
                raise ServiceError(422, "INVALID_CONTEXT",
                                   f"context.{key} must be an integer >= 1")
        return ctx

    @staticmethod
    def _validate_ref(value, *, field: str) -> dict:
        """标准媒体引用：`{service_id, id, revision, content_hash}`，只认 S28。

        `service_id` 不是 S28 时**在这里**就拒（T32-31 的第一道），DB 的
        `lj_video_is_s28` / `lt_artifact_is_s28` 是第二道。
        """
        if not isinstance(value, dict):
            raise ServiceError(422, "INVALID_BODY", f"{field} must be object")
        out = {}
        for key in ("service_id", "id", "revision", "content_hash"):
            if value.get(key) in (None, ""):
                raise ServiceError(422, "INVALID_BODY",
                                   f"{field}.{key} required")
            out[key] = value[key]
        if out["service_id"] != "S28":
            raise ServiceError(
                422, "UNEXPECTED_MEDIA_SERVICE",
                f"{field}.service_id must be S28; S32 holds no media and a "
                f"provider direct link is not a reference",
                details={"field": field, "service_id": out["service_id"]})
        if not isinstance(out["content_hash"], str) or \
                len(out["content_hash"]) != 64:
            raise ServiceError(422, "INVALID_BODY",
                               f"{field}.content_hash must be 64 hex chars")
        if not _is_int(out["revision"]) or out["revision"] < 1:
            raise ServiceError(422, "INVALID_BODY",
                               f"{field}.revision must be >= 1")
        return out

    def _validate_inputs(self, body: dict) -> dict:
        """(d)(g)：窗口形状、人脸轨形状、fps。**还不查时长。**"""
        shot = body.get("shot_lineage_id")
        if not isinstance(shot, str) or not shot.strip():
            raise ServiceError(422, "INVALID_BODY", "shot_lineage_id required")
        speaker = body.get("speaker_character_id")
        if not isinstance(speaker, str) or not speaker.strip():
            raise ServiceError(422, "INVALID_BODY",
                               "speaker_character_id required")

        window = body.get("audio_window")
        if window is None:
            # F5 / T32-03。错误信息必须指向能给出这个数的地方，否则调用方
            # 会去猜一个 start_ms，然后得到一个稳定地偏掉的结果。
            raise ServiceError(
                422, "AUDIO_WINDOW_REQUIRED",
                "audio_window is mandatory: lipsync.input carries no time "
                "fields, so without start_ms the mouth is offset by exactly "
                "the lead-in silence. Read it from S37 "
                "GET /alignment-reports/{id}/units (trim_head_ms = first word "
                "start), or from the S41 EDL",
                details={"how_to_obtain": {
                    "service_id": "S37",
                    "route": "GET /alignment-reports/{id}/units",
                    "fields": ["start_ms", "trim_head_ms", "trim_tail_ms"]}})
        if not isinstance(window, dict):
            raise ServiceError(422, "INVALID_BODY",
                               "audio_window must be object")
        start = window.get("start_ms")
        if not _is_int(start) or start < 0:
            raise ServiceError(422, "INVALID_BODY",
                               "audio_window.start_ms must be an integer >= 0")
        for key in ("trim_head_ms", "trim_tail_ms"):
            value = window.get(key, 0)
            if not _is_int(value) or value < 0:
                raise ServiceError(
                    422, "INVALID_BODY",
                    f"audio_window.{key} must be an integer >= 0")
        source = window.get("source")
        if source not in WINDOW_SOURCES:
            raise ServiceError(422, "INVALID_BODY",
                               f"audio_window.source must be one of "
                               f"{list(WINDOW_SOURCES)}")

        face = body.get("face_track")
        if not isinstance(face, dict):
            raise ServiceError(422, "INVALID_BODY", "face_track must be object")
        track_id = face.get("track_id")
        if not isinstance(track_id, str) or not track_id.strip():
            # 角色 id 替代不了轨 id：角色可能背对镜头或不在画面里（§2.5）。
            raise ServiceError(422, "FACE_TRACK_UNREADABLE",
                               "face_track.track_id required: a character id "
                               "cannot say which face in the frame to edit")
        track_ref = self._validate_ref(face.get("ref"), field="face_track.ref")
        span = face.get("frame_range") or {}
        if not isinstance(span, dict):
            raise ServiceError(422, "FACE_TRACK_UNREADABLE",
                               "face_track.frame_range must be object")
        span_start, span_end = span.get("start"), span.get("end")
        if not _is_int(span_start) or span_start < 0 or \
                not _is_int(span_end) or span_end <= span_start:
            raise ServiceError(422, "FACE_TRACK_UNREADABLE",
                               "face_track.frame_range needs start >= 0 and "
                               "end > start")
        vis = face.get("visibility") or {}
        if not isinstance(vis, dict):
            raise ServiceError(422, "FACE_TRACK_UNREADABLE",
                               "face_track.visibility must be object")
        visible = vis.get("visible_frames")
        covered = vis.get("covered_frames", 0)
        min_px = vis.get("min_face_px")
        for name, value in (("visible_frames", visible),
                            ("covered_frames", covered),
                            ("min_face_px", min_px)):
            if not _is_int(value) or value < 0:
                raise ServiceError(
                    422, "FACE_TRACK_UNREADABLE",
                    f"face_track.visibility.{name} must be an integer >= 0")
        detector = face.get("detector") or {}
        if not isinstance(detector, dict) or not detector.get("model"):
            raise ServiceError(422, "FACE_TRACK_UNREADABLE",
                               "face_track.detector.model required")

        fps = body.get("fps")
        if not _is_number(fps) or fps <= 0:
            raise ServiceError(422, "INVALID_BODY", "fps must be a number > 0")

        preserve = body.get("preserve_other_faces", True)
        if preserve is not True:
            # 契约是 `const: true`。这不是"默认开、可以关"的开关（§2.6）。
            raise ServiceError(
                422, "PRESERVE_OTHER_FACES_REQUIRED",
                "preserve_other_faces is a rights property, not an option: "
                "redrawing a whole frame changes faces that were never "
                "authorized for this job",
                details={"received": preserve, "required": True})

        deadline = body.get("deadline")
        if not deadline:
            raise ServiceError(422, "INVALID_BODY", "deadline required")
        if _deadline_passed(deadline):
            # (h) 放在最后一个**纯本地**检查：任何昂贵读取之前。
            raise ServiceError(422, "DEADLINE_PASSED",
                               "deadline is not in the future",
                               details={"deadline": deadline})

        return {
            "shot_lineage_id": shot, "speaker_character_id": speaker,
            "window": {"start_ms": start,
                       "trim_head_ms": int(window.get("trim_head_ms", 0)),
                       "trim_tail_ms": int(window.get("trim_tail_ms", 0)),
                       "source": source,
                       "end_ms": window.get("end_ms")},
            "face": {"ref": track_ref, "track_id": track_id.strip(),
                     "frame_range": {"start": span_start, "end": span_end},
                     "visibility": {"visible_frames": visible,
                                    "covered_frames": covered,
                                    "min_face_px": min_px},
                     "detector": {"model": detector.get("model"),
                                  "version": detector.get("version")}},
            "fps": float(fps), "deadline": deadline,
            "video_ref": self._validate_ref(body.get("video_ref"),
                                            field="video_ref"),
            "audio_ref": self._validate_ref(body.get("audio_ref"),
                                            field="audio_ref"),
            "audio_take_id": body.get("audio_take_id"),
            "budget_scope_id": str(body.get("budget_scope_id") or ""),
        }

    def _check_repair(self, body: dict, shot: str) -> dict | None:
        """§4.10 F28/F31。S32 只执行 `redo_lipsync`，且只针对 `lipsync` 缺陷。

        三个拒绝都发生在创建任何 job 之前：
        - `re_synthesize_speech`（voice 族）→ 422，S32 不参与
        - `redo_lipsync` 但 `defect_type='voice'` → 422（方向反了：音频错了
          该由 S31 重合成，不是让嘴去匹配错误台词）
        - lineage 不符 → 422 `LINEAGE_MISMATCH`
        """
        action = body.get("repair_action")
        if action is None:
            return None
        if isinstance(action, str):
            action = {"action_id": action}
        if not isinstance(action, dict):
            raise ServiceError(422, "INVALID_REPAIR_ACTION",
                               "repair_action must be object or action id")
        action_type = action.get("action_type")
        if action_type == VOICE_ACTION:
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                f"{VOICE_ACTION} belongs to the voice family and is executed "
                f"by S31; a lipsync defect does not mean the words were wrong",
                details={"received": action_type, "expected_type": "lipsync"})
        if action_type is not None and action_type not in OWNED_ACTIONS:
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                f"S32 executes only {list(OWNED_ACTIONS)}",
                details={"received": action_type, "expected_type": "lipsync"})
        defect_type = action.get("defect_type")
        if defect_type == "voice":
            raise ServiceError(
                422, "ACTION_TYPE_MISMATCH",
                "redo_lipsync against a voice defect is a cross-family reach: "
                "if the audio itself is wrong, re-synthesize the speech",
                details={"received_defect_type": defect_type,
                         "suggested_action": VOICE_ACTION,
                         "suggested_service": "S31"})
        action_lineage = action.get("shot_lineage_id")
        if action_lineage and shot and action_lineage != shot:
            raise ServiceError(
                422, "LINEAGE_MISMATCH",
                "repair action targets a different shot lineage",
                details={"repair_shot_lineage_id": action_lineage,
                         "shot_lineage_id": shot})
        declared = body.get("repair_action_id")
        action_id = action.get("action_id") or declared
        if not action_id:
            raise ServiceError(422, "INVALID_REPAIR_ACTION",
                               "repair_action.action_id required")
        if action.get("action_id") and declared and \
                action["action_id"] != declared:
            # 两个字段指两个动作时**不能挑一个用**：填错字段的调用方会以为
            # 自己应用的是 A，实际应用的是 B，而 `lj_repair_uniq` 之后会
            # 让真正的 A 永远应用不了。
            raise ServiceError(
                422, "INVALID_REPAIR_ACTION",
                "repair_action_id and repair_action.action_id disagree",
                details={"repair_action_id": declared,
                         "repair_action.action_id": action["action_id"]})
        return {"action_id": action_id,
                "action_type": action_type or OWNED_ACTIONS[0],
                "defect_type": defect_type,
                "window_hint": action.get("window_hint") or {}}

    # -------------------------------------------------------- 输入合格性（§4.3）
    def _read_eligibility(self, tenant_id: str, v: dict) -> dict:
        """向 S37 / S31 / S28 / S10 要一份**实时**事实。

        跨租户与不存在在这里是同一种答案（404），不区分——否则 404/403 的差异
        就成了一次存在性探测（E28）。不可达则是 503：编不出一份合格性快照，
        也就不该建 job。
        """
        request = {"video_ref": v["video_ref"], "audio_ref": v["audio_ref"],
                   "audio_take_id": v["audio_take_id"],
                   "speaker_character_id": v["speaker_character_id"],
                   "face_track_ref": v["face"]["ref"]}
        try:
            facts = self.eligibility(tenant_id, request)
        except ServiceError as exc:
            if exc.status == 404:
                raise ServiceError(
                    404, exc.code or "INPUT_REF_NOT_FOUND",
                    "one of video_ref / audio_ref / face_track_ref is not "
                    "reachable in this tenant; S32 does not distinguish "
                    "'missing' from 'another tenant's'",
                    details=exc.details) from exc
            raise
        except ConnectionError as exc:
            raise ServiceError(
                503, "INPUT_ELIGIBILITY_UNAVAILABLE",
                "cannot read S37/S31/S28/S10; refusing to create a job on "
                "guessed input facts",
                retryable=True,
                details={"cause": type(exc).__name__}) from exc
        if not isinstance(facts, dict):
            raise ServiceError(503, "INPUT_ELIGIBILITY_UNAVAILABLE",
                               "eligibility returned no object", retryable=True)
        return facts

    def _validate_window(self, v: dict, facts: dict) -> dict:
        """(e)(f) + §2.3/§2.4：窗口与时长、处理帧范围。

        `end_ms = start_ms + audio_duration_ms - trim_tail_ms`。调用方给了
        `end_ms` 就核对（±1 帧，容忍容器时长取整），不给就由 S32 算。
        """
        audio = facts.get("audio") or {}
        duration = audio.get("duration_ms")
        if not _is_int(duration) or duration <= 0:
            raise ServiceError(503, "AUDIO_DURATION_UNAVAILABLE",
                               "S28/S31 did not report the audio duration; a "
                               "window cannot be validated against a guess",
                               retryable=True)
        window = v["window"]
        fps = v["fps"]
        expected_end = window["start_ms"] + duration - window["trim_tail_ms"]
        frame_ms = 1000.0 / fps
        supplied_end = window.get("end_ms")
        if supplied_end is not None:
            if not _is_int(supplied_end) or supplied_end <= window["start_ms"]:
                raise ServiceError(
                    422, "INVALID_BODY",
                    "audio_window.end_ms must be an integer > start_ms")
            if abs(supplied_end - expected_end) > frame_ms:
                # F6 / T32-04：差一帧以上不是取整，是窗口写错了。
                raise ServiceError(
                    422, "AUDIO_WINDOW_MISMATCH",
                    "audio_window.end_ms disagrees with the audio duration by "
                    "more than one frame",
                    details={"supplied_end_ms": supplied_end,
                             "expected_end_ms": expected_end,
                             "audio_duration_ms": duration,
                             "frame_ms": frame_ms})
            end_ms = supplied_end
        else:
            end_ms = expected_end
        if end_ms <= window["start_ms"]:
            raise ServiceError(422, "AUDIO_WINDOW_MISMATCH",
                               "trim_tail_ms consumes the whole window",
                               details={"start_ms": window["start_ms"],
                                        "end_ms": end_ms})

        video = facts.get("video") or {}
        frame_count = video.get("frame_count")
        video_fps = video.get("fps")
        if _is_number(video_fps) and abs(float(video_fps) - fps) > 0.01:
            # U8：请求的 fps 必须与 S28 探测值一致。不一致时按哪个算帧范围，
            # 都会有一边是错的。
            raise ServiceError(
                422, "FPS_MISMATCH",
                "request fps disagrees with the video's probed frame rate",
                details={"request_fps": fps, "video_fps": float(video_fps)})
        video_duration = video.get("duration_ms")
        if _is_int(video_duration) and end_ms > video_duration:
            # F7 / T32-05。
            raise ServiceError(
                422, "AUDIO_WINDOW_OUT_OF_RANGE",
                "the audio window extends past the end of the video",
                details={"start_ms": window["start_ms"], "end_ms": end_ms,
                         "video_duration_ms": video_duration})
        start_frame, end_frame, processed = frame_range(
            start_ms=window["start_ms"], end_ms=end_ms, fps=fps,
            frame_count=frame_count if _is_int(frame_count) else None)
        return {"start_ms": window["start_ms"], "end_ms": end_ms,
                "trim_head_ms": window["trim_head_ms"],
                "trim_tail_ms": window["trim_tail_ms"],
                "source": window["source"],
                "process_frame_start": start_frame,
                "process_frame_end": end_frame,
                "processed_frames": processed,
                "video_frame_count": frame_count,
                "audio_duration_ms": duration}

    # -------------------------------------------------------- 创建
    def create_job(self, tenant_id: str, command_id: str, body: dict,
                   request_hash: str) -> tuple[int, dict]:
        """`POST /lipsync-jobs`。返回 `(http_status, body)`。

        200 = 命中已有 take，**没有创建付费任务**；202 = 新 job；
        409 = 输入不合格（job 落库、`INPUTS_INELIGIBLE`、不花钱）；
        422 = 请求参数不允许；503 = 上游不可达。

        409 之所以也走返回值而不是抛异常：`INPUTS_INELIGIBLE` 的 job 行与
        幂等结果必须在**同一个事务**里落下来，否则重放时又要去问一遍 S37，
        而且"这次为什么没做"这件事会随事务回滚一起消失。
        """
        body = body if isinstance(body, dict) else {}
        ctx = self._validate_context(body, tenant_id)
        # (c) 先做跨租户判定：它要调上游只读接口，且"这个引用不是你的"不该
        # 等到算完指纹才发现。此时只要求三个引用存在，形状留给 (d)(g)。
        self._require_refs(body)
        v = self._validate_inputs(body)
        v["context"] = ctx
        repair = self._check_repair(body, v["shot_lineage_id"])
        facts = self._read_eligibility(tenant_id, v)
        window = self._validate_window(v, facts)
        v["window_spec"] = window
        self._validate_chain(video_ref=v["video_ref"],
                             origin_service=body.get("video_origin_service")
                             or v["video_ref"].get("origin_service")
                             or (facts.get("video") or {}).get("origin_service"),
                             repair=repair, facts=facts)

        grade, reject = visibility_grade(
            visible_frames=v["face"]["visibility"]["visible_frames"],
            window_frames=window["processed_frames"],
            min_face_px=v["face"]["visibility"]["min_face_px"])
        # 分母**始终是整个窗口**，而不是人脸轨覆盖到的那一段。人脸轨没覆盖
        # 到的帧既不算可见也不算缺席，但把它们从分母里去掉等于只统计"看得
        # 见的地方有多可见"——那会把一段 40% 可见的窗口算成 100%。宁可判低。
        #
        # 帧预算放在可见度之后：一个既超预算又没脸的窗口，"没脸"是更根本的
        # 理由（拆窗口也长不出脸）。不自动拆分（§4.5）。
        if reject is None and window["processed_frames"] > MAX_FRAMES_PER_JOB:
            grade, reject = grade, "FRAME_BUDGET_EXCEEDED"

        fingerprint = input_fingerprint(
            video_hash=v["video_ref"]["content_hash"],
            audio_hash=v["audio_ref"]["content_hash"],
            window_start_ms=window["start_ms"], window_end_ms=window["end_ms"],
            trim_head_ms=window["trim_head_ms"],
            trim_tail_ms=window["trim_tail_ms"],
            process_frame_start=window["process_frame_start"],
            process_frame_end=window["process_frame_end"],
            fps=v["fps"], face_track_hash=v["face"]["ref"]["content_hash"])

        # 修复动作只许应用一次（§7.1）。在落库前查，比撞 UNIQUE 更好解释。
        if repair is not None:
            prior = _row(self.conn.execute(
                "SELECT lipsync_job_id, state FROM lipsync_job WHERE "
                "tenant_id=? AND repair_action_id=?",
                (tenant_id, repair["action_id"])).fetchone())
            if prior is not None:
                raise ServiceError(
                    409, "REPAIR_ALREADY_APPLIED",
                    "this repair action already produced a lipsync job; "
                    "applying it twice would spend twice on one decision",
                    details={"lipsync_job_id": prior["lipsync_job_id"],
                             "state": prior["state"]})

        # UNKNOWN 冻结：同指纹的在途 job 结果不明时，不得再发新的付费请求（E16）。
        blocked = self._unknown_for(tenant_id, v["shot_lineage_id"],
                                    v["speaker_character_id"], fingerprint)
        if blocked is not None:
            raise ServiceError(
                409, "MUST_RECONCILE_FIRST",
                "an earlier job for this exact input has an unknown "
                "submission outcome; a second paid submission could "
                "double-charge. Reconcile it first",
                details={"lipsync_job_id": blocked["lipsync_job_id"],
                         "state": blocked["state"],
                         "must_reconcile_before_resubmit": True})

        # 复用：**先于** job 落库。命中时响应里没有 job id，若先建一行再退，
        # 那一行就是一条永远不前进的孤儿记录。
        reuse = self._find_reusable(tenant_id=tenant_id,
                                    fingerprint=fingerprint, repair=repair)
        if reuse is not None:
            take = reuse["take"]
            return 200, {
                "lipsync_job_id": None, "reused": True,
                "lipsync_take_id": take["lipsync_take_id"],
                "artifact_ref": _loads(take["artifact_ref"], None),
                "processed_frames": int(take["output_frame_count"]),
                "note": "input_fingerprint matched an existing usable take; "
                        "no paid job was created",
            }

        job_id = _new_id("job")
        effective_hash = request_hash or _sha256_hex(_jcs(
            {k: val for k, val in body.items()
             if k not in ("command_id", "request_hash")}))
        idem_key = _sha256_hex("\x1f".join(
            [tenant_id, command_id, effective_hash]))
        prior_window = self._prior_window(tenant_id, v)
        window_changed = prior_window is not None and prior_window != (
            window["start_ms"], window["end_ms"], window["trim_head_ms"],
            window["trim_tail_ms"])
        model_family = facts.get("model_family")
        strategy = strategy_class_for(
            action="redo_lipsync" if repair else "generate",
            model_family=model_family, window_source=window["source"],
            window_changed=window_changed if repair else None,
            face_track_revision=v["face"]["ref"]["revision"])

        def produce():
            now = _now()
            ftb_id = self._record_face_track(tenant_id, v, now)
            snap_id = self._record_snapshot(tenant_id, v, facts, window, now)
            self.conn.execute(
                "INSERT INTO lipsync_job (lipsync_job_id, tenant_id, "
                "project_id, series_id, episode_id, branch_id, "
                "shot_lineage_id, speaker_character_id, video_ref, audio_ref, "
                "audio_take_id, window_start_ms, window_end_ms, trim_head_ms, "
                "trim_tail_ms, window_source, process_frame_start, "
                "process_frame_end, processed_frames, face_track_binding_id, "
                "eligibility_snapshot_id, preserve_other_faces, fps, "
                "visibility_grade, node_id, "
                "node_epoch_at_create, execution_fence, input_fingerprint, "
                "request_hash, idempotency_key, repair_action_id, "
                "strategy_class, variation_nonce, route_decision_ref, "
                "execution_context, budget_scope_id, deadline, state, "
                "state_seq, created_at, updated_at, state_entered_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, tenant_id, ctx["project_id"], ctx["series_id"],
                 ctx["episode_id"], ctx["branch_id"], v["shot_lineage_id"],
                 v["speaker_character_id"], _jcs(v["video_ref"]),
                 _jcs(v["audio_ref"]), v["audio_take_id"],
                 window["start_ms"], window["end_ms"],
                 window["trim_head_ms"], window["trim_tail_ms"],
                 window["source"], window["process_frame_start"],
                 window["process_frame_end"], window["processed_frames"],
                 ftb_id, snap_id, 1, v["fps"], grade, ctx["node_id"],
                 int(ctx["node_epoch"]), int(ctx["execution_fence"]),
                 fingerprint, effective_hash, idem_key,
                 repair["action_id"] if repair else None, strategy,
                 body.get("variation_nonce") or uuid.uuid4().hex, None,
                 _jcs(ctx), v["budget_scope_id"], str(v["deadline"]),
                 "CREATED", 0, now, now, now))
            job = self._job(tenant_id, job_id)
            job = self._transition(job, "CHECKING_INPUTS", outbox=self.outbox)
            return self._check_inputs(tenant_id, job, v, facts, window,
                                      grade, reject, repair, body,
                                      window_changed, model_family)

        try:
            status, result = self._command(tenant_id, command_id, produce)
            result = {**result, "window_changed": window_changed,
                      "strategy_class": strategy}
            return status, result
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            if "lj_repair_uniq" in text or \
                    "lipsync_job.tenant_id, lipsync_job.repair_action_id" in text:
                raise ServiceError(
                    409, "REPAIR_ALREADY_APPLIED",
                    "this repair action already produced a lipsync job",
                    details={"repair_action_id": repair["action_id"]
                             if repair else None}) from exc
            if "lipsync_job_open_uniq" in text:
                existing = self._unknown_for(
                    tenant_id, v["shot_lineage_id"],
                    v["speaker_character_id"], fingerprint)
                if existing is None:
                    existing = _row(self.conn.execute(
                        "SELECT * FROM lipsync_job WHERE tenant_id=? AND "
                        "shot_lineage_id=? AND speaker_character_id=? AND "
                        "input_fingerprint=? AND state NOT IN "
                        "('READY','INPUTS_INELIGIBLE','REJECTED_LOCAL',"
                        "'FAILED','RECONCILED_FAILED','CANCELED') "
                        "ORDER BY created_at DESC",
                        (tenant_id, v["shot_lineage_id"],
                         v["speaker_character_id"], fingerprint)).fetchone())
                if existing is None:
                    raise
                # 同一输入已有在途 job：**不**再开一条，也不重复付费。
                return 202, {"lipsync_job_id": existing["lipsync_job_id"],
                             "state": existing["state"],
                             "operation_id": None, "reused": False,
                             "duplicate_suppressed": True}
            raise

    @staticmethod
    def _require_refs(body: dict) -> None:
        """(c) 的前置：三个引用与两个人脸字段必须在。形状是 (d)(g) 的事。"""
        for field in ("video_ref", "audio_ref"):
            value = body.get(field)
            if not isinstance(value, dict) or not value.get("id"):
                raise ServiceError(422, "INVALID_BODY",
                                   f"{field}.id required")
        face = body.get("face_track")
        if not isinstance(face, dict) or not isinstance(face.get("ref"), dict) \
                or not face["ref"].get("id"):
            raise ServiceError(422, "FACE_TRACK_UNREADABLE",
                               "face_track.ref.id required")

    @staticmethod
    def _validate_chain(*, video_ref: dict, origin_service, repair,
                        facts: dict) -> None:
        """§4.9 链式自我叠加。

        `origin_service` 优先取调用方随 `video_ref` 传的来源，其次取 S28 的
        归档事实（C4：`artifact.ingested` 没有 `ingesting_service`，所以这条
        路常常给不出值）。**两处都没有时拒绝**：无法验证的链式约束不能当成
        满足——"审核已通过"不是放行依据的同一条道理。
        """
        if origin_service is None:
            raise ServiceError(
                422, "UNEXPECTED_VIDEO_ORIGIN",
                "the video's origin service is unknown; S32 must know whether "
                "this frame was already lipsynced once (two generations of "
                "redraw degrade the mouth). Pass video_origin_service",
                details={"video_ref": video_ref, "origin_service": None})
        if origin_service not in ALLOWED_VIDEO_ORIGINS:
            raise ServiceError(
                422, "UNEXPECTED_VIDEO_ORIGIN",
                "only S30 output (or S32 output on an explicit repair) may be "
                "lipsynced; another origin means S30 was bypassed",
                details={"origin_service": origin_service,
                         "allowed": sorted(ALLOWED_VIDEO_ORIGINS)})
        if origin_service == SELF_ORIGIN and repair is None:
            raise ServiceError(
                422, "SELF_CHAIN_FORBIDDEN",
                "lipsyncing S32's own output without a repair action stacks a "
                "second generation of redraw on already-redrawn pixels; only "
                "an explicit S39 repair may do this",
                details={"origin_service": origin_service,
                         "repair_action_id": None})

    def _record_face_track(self, tenant_id: str, v: dict, now: str) -> str:
        face = v["face"]
        binding_id = _new_id("ftb")
        self.conn.execute(
            "INSERT INTO face_track_binding (face_track_binding_id, tenant_id, "
            "face_track_ref, track_id, frame_range_start, frame_range_end, "
            "visible_frames, covered_frames, min_face_px, detector_model, "
            "detector_version, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (binding_id, tenant_id, _jcs(face["ref"]), face["track_id"],
             face["frame_range"]["start"], face["frame_range"]["end"],
             face["visibility"]["visible_frames"],
             face["visibility"]["covered_frames"],
             face["visibility"]["min_face_px"], face["detector"]["model"],
             str(face["detector"].get("version") or ""), now))
        return binding_id

    def _record_snapshot(self, tenant_id: str, v: dict, facts: dict,
                         window: dict, now: str) -> str:
        """合格性快照 = **观测到的那一刻**，不是"已合格"的证明。

        `defects_open` 必须带 id（DB 的 `ies_defects_have_ids` 也拦着）：
        不允许"我觉得音频有问题"而说不出是哪一条。
        """
        s37 = facts.get("s37") or {}
        ran = bool(s37.get("ran"))
        defects = s37.get("defects") or []
        blocking, _ignored = _blocking_defects(defects)
        if blocking:
            defect_check = "defects_open"
            ids = [str(d.get("defect_id") or d.get("id"))
                   for d in blocking if d.get("defect_id") or d.get("id")]
            if not ids:
                # 有阻断缺陷但说不出 id：这是上游给的数据不完整，不能写一条
                # 没有证据的 defects_open。
                raise ServiceError(
                    503, "S37_DEFECT_IDS_MISSING",
                    "S37 reports blocking voice defects without defect ids; "
                    "a snapshot that cannot name the evidence is not a "
                    "snapshot", retryable=True)
        elif ran:
            defect_check = "clean"
            ids = []
        else:
            defect_check = "not_yet_available"
            ids = []
        audio_take = facts.get("audio_take") or {}
        rights = audio_take.get("rights_state")
        if rights not in ("usable", "void"):
            rights = "unknown"
        snap_id = _new_id("ies")
        self.conn.execute(
            "INSERT INTO input_eligibility_snapshot (snapshot_id, tenant_id, "
            "video_ref, audio_ref, audio_take_ref, voice_defect_check, "
            "s37_report_ref, open_defect_ids, audio_rights_state, "
            "video_referenceable, face_authorization_state, captured_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (snap_id, tenant_id, _jcs(v["video_ref"]), _jcs(v["audio_ref"]),
             _jcs({"service_id": "S31", "id": v["audio_take_id"]})
             if v["audio_take_id"] else None,
             defect_check,
             _jcs(s37["report_ref"]) if s37.get("report_ref") else None,
             _jcs(ids), rights,
             1 if (facts.get("video") or {}).get("referenceable") is not False
             else 0,
             facts.get("face_authorization_state") or "unknown", now))
        return snap_id

    # -------------------------------------------------------- CHECKING_INPUTS
    def _eligibility_block(self, job: dict, v: dict, facts: dict):
        """§4.3 的四个"上游说的、不是我们判的"拒绝。命中即返回，
        未命中返回 None。

        抽成单独一段是为了让 `recover()` 复用：崩溃后重问一遍 S37/S31/S28/S10，
        得到同一个答案才能继续，而这条路径**绝不提交付费任务**。
        """
        s37 = facts.get("s37") or {}
        blocking, ignored = _blocking_defects(s37.get("defects") or [])
        if blocking:
            ids = [str(d.get("defect_id") or d.get("id")) for d in blocking]
            return self._inputs_ineligible(
                job, "AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC",
                detail=f"audio has {len(ids)} open voice defect(s); lip-syncing "
                       f"to this audio would produce a mouth that matches the "
                       f"wrong words",
                extra={"open_defect_ids": ids,
                       "suggested_action": VOICE_ACTION,
                       "suggested_service": "S31",
                       "s37_report_ref": s37.get("report_ref")})
        audio_take = facts.get("audio_take") or {}
        if audio_take.get("rights_state") == "void":
            return self._inputs_ineligible(
                job, "AUDIO_RIGHTS_VOID",
                detail="the audio take was voided by S31 after a voice "
                       "authorization change; its bytes are unchanged but it "
                       "may not be used",
                extra={"audio_take_id": v["audio_take_id"],
                       "suggested_action": VOICE_ACTION,
                       "suggested_service": "S31"})
        video = facts.get("video") or {}
        if video.get("tombstoned") is True or video.get("referenceable") is False:
            return self._inputs_ineligible(
                job, "VIDEO_TOMBSTONED",
                detail="S28 reports the source video is no longer referenceable",
                extra={"video_ref": v["video_ref"],
                       "suggested_action": "regenerate_video",
                       "suggested_service": "S30"})
        if facts.get("face_authorization_state") == "lapsed":
            return self._inputs_ineligible(
                job, "FACE_AUTHORIZATION_LAPSED",
                detail="the speaker's face authorization is no longer valid; "
                       "animating that mouth needs a fresh grant",
                extra={"speaker_character_id": v["speaker_character_id"],
                       "suggested_action": "renew_face_authorization",
                       "suggested_service": "S10"})
        return None

    def _check_inputs(self, tenant_id: str, job: dict, v: dict, facts: dict,
                      window: dict, grade: str, reject: str | None,
                      repair: dict | None, body: dict,
                      window_changed: bool, model_family) -> tuple[int, dict]:
        """§4.1 第 3 步。**先问声音对不对，再看能不能做。**

        顺序不是风格问题：一个"音频念错词 + 窗口写错"的请求，必须先得到
        `AUDIO_NOT_ELIGIBLE_FOR_LIPSYNC`。若先报窗口错误，调用方改完窗口
        再来一次，音频仍然是错的——正是 §4.3 要防的循环。

        资格判定本身在 `_eligibility_block` 里，重启时的 `recover()` 复用
        同一段逻辑（T32-45：崩溃前的内存结论一律作废，重问一遍）。
        """
        blocked = self._eligibility_block(job, v, facts)
        if blocked is not None:
            return blocked

        if reject is not None:
            # F8–F11：帧数上限与可见度。**不自动拆、不尽力而为。**
            extra = {"visibility_grade": grade,
                     "processed_frames": window["processed_frames"]}
            if reject == "FRAME_BUDGET_EXCEEDED":
                extra["suggestion"] = ("split the shot into windows of at most "
                                       f"{MAX_FRAMES_PER_JOB} frames; S32 does "
                                       f"not auto-split")
            return 202, self._reject_local(job, reject, extra=extra)

        job = self._transition(job, "PREPARING", outbox=self.outbox)
        route = self._route(tenant_id, job, v, body)
        if route.get("_reject") is not None:
            status, payload = route["_reject"]
            return status, payload
        return self._submit(tenant_id, job, v, route, repair, body,
                            window_changed=window_changed,
                            model_family=model_family,
                            window=window, facts=facts)

    def _inputs_ineligible(self, job: dict, code: str, *, detail: str,
                           extra: dict | None = None) -> tuple[int, dict]:
        """409 的形状是本服务最重要的一段输出（§6.1）。

        `suggested_action` / `suggested_service` 是给调用方与 S39 看的：一个
        不带指引的 409 会让自动化的修复规划把"音频错了"当成"口型暂时做不了"
        然后重试口型——那正是 §4.3 要防的。
        """
        self._transition(job, "INPUTS_INELIGIBLE", reason_code=code,
                         detail=detail, outbox=self.outbox)
        self.outbox.append(new_event(
            source=SOURCE, type=LOCAL_INPUTS_INELIGIBLE,
            subject=f"shot/{job['shot_lineage_id']}",
            tenant_id=job["tenant_id"], home_cell="lipsync",
            aggregate_id=job["shot_lineage_id"],
            sequence=self._next_sequence(job["tenant_id"],
                                         job["shot_lineage_id"]),
            correlation_id=job["lipsync_job_id"],
            data={"lipsync_job_id": job["lipsync_job_id"],
                  "shot_lineage_id": job["shot_lineage_id"],
                  "speaker_character_id": job["speaker_character_id"],
                  "reason_code": code, **(extra or {})},
            schema_version=SCHEMA_VERSION))
        payload = {"code": code, "message": detail, "retryable": False,
                   "correlation_id": "",
                   "lipsync_job_id": job["lipsync_job_id"],
                   "state": "INPUTS_INELIGIBLE",
                   "operation_id": _op_id(job["lipsync_job_id"]),
                   "reused": False,
                   "details": dict(extra or {})}
        payload.update(extra or {})
        return 409, payload

    def _reject_local(self, job: dict, code: str, *,
                      extra: dict | None = None) -> dict:
        """`REJECTED_LOCAL` 的**响应体**。调用方按 202 处理：job 存在，只是
        还没花钱就结束了。"""
        self._transition(job, "REJECTED_LOCAL", reason_code=code,
                         outbox=self.outbox)
        payload = {"lipsync_job_id": job["lipsync_job_id"],
                   "state": "REJECTED_LOCAL", "reason_code": code,
                   "operation_id": _op_id(job["lipsync_job_id"]),
                   "reused": False}
        payload.update(extra or {})
        return payload

    # -------------------------------------------------------- PREPARING
    def _route(self, tenant_id: str, job: dict, v: dict, body: dict) -> dict:
        """取路由、判能力、判驻留。**任何失败都在花钱之前。**

        `face_region_localized_edit` 是 §4.10 F15 要求的能力位（C7：S24 目前
        没有登记它，所以大多数路由会给 `None` 而不是 `False`——`None` 表示
        "不知道"，不是"不支持"；只有显式 `False` 才拒绝）。
        """
        request = {"modality": "lipsync",
                   "window_source": job["window_source"],
                   "processed_frames": int(job["processed_frames"]),
                   "fps": float(job["fps"]),
                   "preserve_other_faces": True,
                   "speaker_character_id": job["speaker_character_id"],
                   "route_policy_ref": _loads(job["route_decision_ref"], None)}
        try:
            route = self.route_resolver(tenant_id, request)
        except ServiceError as exc:
            raise ServiceError(exc.status, exc.code, str(exc),
                               retryable=exc.retryable,
                               details=exc.details) from exc
        except ConnectionError as exc:
            raise ServiceError(503, "ROUTE_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        route = route if isinstance(route, dict) else {}

        capabilities = route.get("capabilities") or {}
        if capabilities.get("face_region_localized_edit") is False:
            # **不降级为整帧重绘**：整帧重绘会改掉没有被授权的脸（§2.6）。
            return {"_reject": (202, self._reject_local(
                job, "CAPABILITY_MISSING",
                extra={"required_capability": "face_region_localized_edit",
                       "degradation_options": route.get("degradation_options")
                       or []}))}
        if route.get("feasible") is False:
            return {"_reject": (202, self._reject_local(
                job, route.get("reason_code") or "ROUTE_INFEASIBLE",
                extra={"degradation_options":
                       route.get("degradation_options") or []}))}
        if route.get("residency_allowed") is False:
            return {"_reject": (202, self._reject_local(
                job, "RESIDENCY_FORBIDDEN",
                extra={"region": route.get("region")}))}
        policy_ref = body.get("route_policy_ref") or route.get("policy_ref")
        decision_ref = route.get("route_decision_ref") or \
            route.get("route_decision_id")
        if not policy_ref or not decision_ref:
            # 没有决策引用就没法向 S26 交代"这次按哪条规则路由的"。宁可本地
            # 拒绝，也不提交一个无法审计的付费任务。
            return {"_reject": (202, self._reject_local(
                job, "ROUTE_UNAVAILABLE",
                extra={"detail": "route resolution returned no "
                                 "route_decision_ref"}))}
        return {"capabilities": capabilities, "policy_ref": policy_ref,
                "decision_ref": decision_ref, "route": route,
                "model_family": route.get("model_family") or
                capabilities.get("model_family")}

    def _submit(self, tenant_id: str, job: dict, v: dict, route: dict,
                repair: dict | None, body: dict, *, window_changed: bool,
                model_family, window: dict, facts: dict) -> tuple[int, dict]:
        """SUBMITTED：提交前复查输入对，然后**只**通过注入的 `submitter` 提交。

        提交前那一次复查最容易被当成"刚才不是查过了吗"。从取路由到真正提交
        之间横着 S23 的 reserve / S22 的 permit / S26 的 SubmissionIntent，
        配额紧张时可能是几分钟，足够上游把音频换掉。
        """
        refreshed = self._read_eligibility(tenant_id, v)
        current_video = ((refreshed.get("video") or {}).get("content_hash"))
        current_audio = ((refreshed.get("audio") or {}).get("content_hash"))
        if current_video and current_video != v["video_ref"]["content_hash"]:
            return 202, self._reject_local(
                job, "INPUT_CHANGED_BEFORE_SUBMIT",
                extra={"basis": "video_changed",
                       "stored_hash": v["video_ref"]["content_hash"],
                       "observed_hash": current_video})
        if current_audio and current_audio != v["audio_ref"]["content_hash"]:
            return 202, self._reject_local(
                job, "INPUT_CHANGED_BEFORE_SUBMIT",
                extra={"basis": "audio_changed",
                       "stored_hash": v["audio_ref"]["content_hash"],
                       "observed_hash": current_audio})
        if (refreshed.get("audio_take") or {}).get("rights_state") == "void":
            return 202, self._reject_local(
                job, "AUDIO_RIGHTS_VOID",
                extra={"basis": "audio_rights_void"})

        model_family = route.get("model_family") or model_family
        job = {**job,
               "strategy_class": strategy_class_for(
                   action="redo_lipsync" if repair else "generate",
                   model_family=model_family,
                   window_source=job["window_source"],
                   window_changed=window_changed if repair else None,
                   face_track_revision=v["face"]["ref"]["revision"])}
        command = self._generation_command(job, v, route)
        # **先记认领，再发请求**。`attempt_id` 在组命令时就已经确定
        # （`<job>:<attempt_no>`），而提交可能是"不明"的：S26 收下了命令但
        # 响应没回来。那一刻如果库里没有 attempt_id，之后 S26 发来的
        # `generation.job.state-changed` 会以 `unknown_attempt` 被丢掉，
        # 这个 job 就再也没法对账了（E16 的死角）。写在这里不影响回滚——
        # 整笔仍在同一个事务里，真异常会一起退回。
        self.conn.execute(
            "UPDATE lipsync_job SET attempt_id=?, strategy_class=?, "
            "updated_at=? WHERE lipsync_job_id=?",
            (command["attempt_id"], job["strategy_class"], _now(),
             job["lipsync_job_id"]))
        job = {**job, "attempt_id": command["attempt_id"]}
        try:
            submitted = self.submitter(tenant_id, command,
                                       route["decision_ref"])
        except ServiceError:
            raise
        except ConnectionError as exc:
            # 提交结果不明：**冻结在 WAITING_UNKNOWN**，绝不重提（E16）。
            self._transition(job, "WAITING_UNKNOWN",
                             reason_code="SUBMISSION_UNKNOWN",
                             detail=str(exc), outbox=self.outbox)
            return 202, self._accepted(job["lipsync_job_id"],
                                       processed_frames=job["processed_frames"])
        submitted = submitted if isinstance(submitted, dict) else {}
        provider_job_id = submitted.get("provider_job_id") or \
            submitted.get("generation_job_id") or submitted.get("operation_id")
        if not provider_job_id:
            # 与上一种同罪：S26 没给可对账的 id，就不能当它没发生。
            self._transition(job, "WAITING_UNKNOWN",
                             reason_code="SUBMISSION_UNKNOWN",
                             detail="S26 accepted the command without an id",
                             outbox=self.outbox)
            return 202, self._accepted(job["lipsync_job_id"],
                                       processed_frames=job["processed_frames"])
        attempt_id = submitted.get("attempt_id") or command["attempt_id"]
        now = _now()
        self.conn.execute(
            "UPDATE lipsync_job SET provider_job_id=?, attempt_id=?, "
            "strategy_class=?, route_decision_ref=?, quote_upper_bound_micro=?, "
            "updated_at=? WHERE lipsync_job_id=?",
            (provider_job_id, attempt_id, job["strategy_class"],
             _ref_text(route["decision_ref"]),
             submitted.get("quote_upper_bound_micro"), now,
             job["lipsync_job_id"]))
        repeat = self._write_ledger(job, repair=repair, now=now)
        job = self._job(tenant_id, job["lipsync_job_id"])
        job = self._transition(job, "SUBMITTED", outbox=self.outbox)
        self._transition(job, "WAITING_PROVIDER", outbox=self.outbox)
        body_out = self._accepted(job["lipsync_job_id"],
                                  processed_frames=int(job["processed_frames"]))
        body_out["repeat_without_input_change"] = repeat
        body_out["strategy_class"] = job["strategy_class"]
        return 202, body_out

    def _generation_command(self, job: dict, v: dict, route: dict) -> dict:
        """组 `generation-command.v2`，**恰好 11 个顶层键**。

        `context` 是原样重放平台签名的那一份（`execution_context` 列），不是
        现拼：18 个必填键里的 `actor` / `trace_id` / `home_region` 只能来自
        签名，现场拼出来的每个值都是伪造签名。

        `input` 只放 schema 允许的 5 个键；`audio_window`、`fps`、`track_id`、
        `visibility` 都进不来（C1/C2/C3），所以它们只存在于 S32 自己的库里。
        """
        attempt_no = int(job["business_attempt_no"])
        return {
            "schema_version": SCHEMA_VERSION,
            "context": _loads(job["execution_context"], {}),
            "attempt_id": f"{job['lipsync_job_id']}:{attempt_no}",
            "variation_nonce": job["variation_nonce"],
            "idempotency_key": f"{job['idempotency_key']}:{attempt_no}",
            "modality": "lipsync",
            "input": {
                "video_ref": _loads(job["video_ref"], {}),
                "audio_ref": _loads(job["audio_ref"], {}),
                "speaker_character_id": job["speaker_character_id"],
                "face_track_ref": v["face"]["ref"],
                "preserve_other_faces": True,
            },
            "route_policy_ref": route["policy_ref"],
            "template_ref": None,
            "budget_scope_id": job["budget_scope_id"],
            "deadline": job["deadline"],
        }

    def _accepted(self, job_id: str, *, processed_frames: int) -> dict:
        """202 的响应体。`state` 取**库里的事实**，不是 "CREATED"。

        `processed_frames` 在 202 里就返回，是让调用方立刻知道这次花了多少帧，
        而不是等账单（§6.1）。
        """
        row = self.conn.execute(
            "SELECT state, failure_code, window_start_ms, window_end_ms, "
            "trim_head_ms, trim_tail_ms FROM lipsync_job "
            "WHERE lipsync_job_id=?", (job_id,)).fetchone()
        body = {"lipsync_job_id": job_id,
                "state": row["state"] if row is not None else "CREATED",
                "operation_id": _op_id(job_id),
                "processed_frames": int(processed_frames),
                "reused": False}
        if row is not None:
            # §4.5 的 `window_duration_ms`：报价的另一个输入。给了调用方就能
            # 自己核 `processed_frames`，不必等账单。
            body["estimated_window_ms"] = (
                int(row["window_end_ms"]) - int(row["window_start_ms"])
                - int(row["trim_head_ms"]) - int(row["trim_tail_ms"]))
        if row is not None and row["failure_code"]:
            body["reason_code"] = row["failure_code"]
        return body

    def _write_ledger(self, job: dict, *, repair: dict | None,
                      now: str) -> bool:
        """§8.3：空转要留证据，但不拒绝。

        `repeat_without_input_change` 的判定是"同一镜头、同一说话人，**输入
        一个字节没变**（同 `input_fingerprint`）、已经有过一条台账"。

        用指纹而不是 `strategy_class` 比：首轮是 `generate`、重做是
        `redo_lipsync:...`，两个字符串永不相等，按策略类比会让 §8.4 的第 2 行
        （首轮之后的空转重做）永远记不上——而那一行正是这个字段存在的理由。
        """
        ctx = _loads(job["execution_context"], {})
        manifest = ctx.get("manifest_ref") or {}
        action = "redo_lipsync" if repair else "generate"
        repeat = False
        if action == "redo_lipsync":
            prior = self.conn.execute(
                "SELECT 1 FROM lipsync_intent_ledger l "
                "JOIN lipsync_job j ON j.lipsync_job_id = l.lipsync_job_id "
                "WHERE l.tenant_id=? AND l.shot_lineage_id=? AND "
                "l.speaker_character_id=? AND j.input_fingerprint=? "
                "LIMIT 1",
                (job["tenant_id"], job["shot_lineage_id"],
                 job["speaker_character_id"],
                 job["input_fingerprint"])).fetchone()
            repeat = prior is not None
        self.conn.execute(
            "INSERT INTO lipsync_intent_ledger (entry_id, tenant_id, "
            "lipsync_job_id, shot_lineage_id, speaker_character_id, action, "
            "strategy_class, repair_action_id, audio_take_id, "
            "repeat_without_input_change, run_id, node_epoch, manifest_id, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("ledger"), job["tenant_id"], job["lipsync_job_id"],
             job["shot_lineage_id"], job["speaker_character_id"], action,
             job["strategy_class"], job["repair_action_id"],
             job["audio_take_id"], 1 if repeat else 0, ctx.get("run_id"),
             # 单个 logical_node 的 epoch，与 execution_fence 是两回事。
             int(job["node_epoch_at_create"]), manifest.get("id"), now))
        if repeat:
            self.outbox.append(new_event(
                source=SOURCE, type=LOCAL_REPEAT_WITHOUT_CHANGE,
                subject=f"shot/{job['shot_lineage_id']}",
                tenant_id=job["tenant_id"], home_cell="lipsync",
                aggregate_id=job["shot_lineage_id"],
                sequence=self._next_sequence(job["tenant_id"],
                                             job["shot_lineage_id"]),
                correlation_id=job["lipsync_job_id"],
                data={"lipsync_job_id": job["lipsync_job_id"],
                      "shot_lineage_id": job["shot_lineage_id"],
                      "speaker_character_id": job["speaker_character_id"],
                      "strategy_class": job["strategy_class"],
                      "repeat_without_input_change": True},
                schema_version=SCHEMA_VERSION))
        return repeat

    def _prior_window(self, tenant_id: str, v: dict) -> tuple | None:
        """同一镜同一人上一次的窗口。`window_changed` 靠它算（§8.2）。"""
        row = _row(self.conn.execute(
            "SELECT window_start_ms, window_end_ms, trim_head_ms, trim_tail_ms "
            "FROM lipsync_job WHERE tenant_id=? AND shot_lineage_id=? AND "
            "speaker_character_id=? ORDER BY created_at DESC LIMIT 1",
            (tenant_id, v["shot_lineage_id"],
             v["speaker_character_id"])).fetchone())
        if row is None:
            return None
        return (int(row["window_start_ms"]), int(row["window_end_ms"]),
                int(row["trim_head_ms"]), int(row["trim_tail_ms"]))

    def _unknown_for(self, tenant_id: str, shot: str, speaker: str,
                     fingerprint: str) -> dict | None:
        return _row(self.conn.execute(
            "SELECT * FROM lipsync_job WHERE tenant_id=? AND shot_lineage_id=? "
            "AND speaker_character_id=? AND input_fingerprint=? AND "
            "state='WAITING_UNKNOWN' ORDER BY created_at DESC LIMIT 1",
            (tenant_id, shot, speaker, fingerprint)).fetchone())

    def _find_reusable(self, *, tenant_id: str, fingerprint: str,
                       repair: dict | None) -> dict | None:
        """§4.2 的三路合取，在**复用**这一侧。

        - 带 `repair_action_id` → 直接 `None`（修复必须真的重做）
        - `stale_basis` 非空 → 不参复用（已过期的东西不能当新产物交出去）
        - 音频权利 live 读不是 usable → 抛 409，**不建新 job**：命中一个已
          撤销音频的产物，说明这次请求要的正是那段音频。
        - artifact 不再可引用 → `None`（S28 可能已 tombstone）
        """
        if repair is not None:
            return None
        rows = self.conn.execute(
            "SELECT * FROM lipsync_take WHERE tenant_id=? AND "
            "input_fingerprint=? AND stale_basis IS NULL AND "
            "state IN ('READY','ACCEPTED') ORDER BY created_at DESC",
            (tenant_id, fingerprint)).fetchall()
        for raw in rows:
            take = dict(raw)
            if take["state"] not in ("READY", "ACCEPTED"):
                continue
            if not self._rights_usable(tenant_id, take.get("lipsync_job_id")):
                raise ServiceError(
                    409, "AUDIO_RIGHTS_VOID",
                    "a take with this exact input is now void because its "
                    "audio rights lapsed; S32 does not fall back to another "
                    "audio",
                    details={"lipsync_take_id": take["lipsync_take_id"]})
            artifact = _loads(take["artifact_ref"], {})
            try:
                referenceable = self.artifact_check(tenant_id, artifact)
            except (ServiceError, ConnectionError):
                # 查不到 ≠ 不可用，但**复用**要求"确认还在"。查不到就新建，
                # 反过来把没校验的字节递出去才不可接受（E23）。
                referenceable = False
            if not referenceable:
                continue
            return {"take": take, "artifact_ref": artifact}
        return None

    def _rights_usable(self, tenant_id: str, job_id: str) -> bool:
        """live 读 S31（§4.2：撤销发生在归档之后，拷进 take 的值永远是 usable）。

        没有 `audio_take_id` 的 job 不是 S31 来的，没有可查的权利状态——
        这一路不构成阻断，但也**不**被当成"usable"写进任何地方。
        """
        job = _row(self.conn.execute(
            "SELECT audio_take_id FROM lipsync_job WHERE tenant_id=? AND "
            "lipsync_job_id=?", (tenant_id, job_id)).fetchone())
        if job is None or not job["audio_take_id"]:
            return True
        try:
            state = self.rights_reader(tenant_id, job["audio_take_id"])
        except (ServiceError, ConnectionError):
            # 查不到授权 ≠ 有授权。不能凭一个失败的查询复用产物。
            return False
        state = state if isinstance(state, dict) else {}
        return state.get("rights_state") == "usable"


    # -------------------------------------------------------- 事件：S26
    def on_generation_state_changed(self, event: dict) -> dict:
        """S26 `generation.job.state-changed`。

        关联键先 `attempt_id`、后 `provider_job_id`（同 S31 C11 的理由：事件里
        没有后者的一等字段）。三条铁律与 S31 一致，这里再写一遍是因为它们
        在本服务里各自对应一条验收：

        - `SUBMISSION_UNKNOWN` → `WAITING_UNKNOWN` 冻结，**绝不重提**（E16）
        - 终态 job 不被乱序事件拉回（重复投递无害）
        - 不认识的供应商状态忽略并留原因，不猜
        """
        data = event.get("data") or {}
        job = self._job_by_attempt(event.get("tenant_id"),
                                   data.get("attempt_id"),
                                   data.get("provider_job_id"))
        if job is None:
            return {"ignored": True, "reason": "unknown_attempt"}
        if job["state"] in TERMINAL_STATES:
            return {"ignored": True, "reason": "job_terminal",
                    "state": job["state"]}
        state = data.get("state")
        reason = data.get("reason_code")
        if state == "SUBMISSION_UNKNOWN":
            if job["state"] != "WAITING_UNKNOWN":
                self._transition(job, "WAITING_UNKNOWN",
                                 reason_code="SUBMISSION_UNKNOWN",
                                 detail=reason, outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "state": "WAITING_UNKNOWN", "frozen": True,
                    "resubmitted": False}
        if state in ("QUEUED", "RUNNING"):
            if job["state"] == "SUBMITTED":
                self._transition(job, "WAITING_PROVIDER",
                                 outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "state": "WAITING_PROVIDER"}
        if state == "PROVIDER_SUCCEEDED":
            # 供应商跑完 ≠ 字节可用。**不进 READY**，等 S28 归档。
            if job["state"] in ("SUBMITTED", "WAITING_PROVIDER",
                                "WAITING_UNKNOWN"):
                if job["state"] == "WAITING_UNKNOWN":
                    # 从"提交结果不明"里恢复出来就是在对账。`_on_archived`
                    # 与 `on_artifact_ingested` 两条路径都记了这个标记，
                    # 这一条不记的话，同一件事实会因"先收到哪条事件"而
                    # 有两种写法（E16）。
                    self.conn.execute(
                        "UPDATE lipsync_job SET reconciled=1 "
                        "WHERE lipsync_job_id=?", (job["lipsync_job_id"],))
                    job = self._job(job["tenant_id"], job["lipsync_job_id"])
                self._transition(job, "INGESTING", outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "state": "INGESTING"}
        if state == "ARCHIVED":
            return self._on_archived(job, data, reason)
        if state in ("PROVIDER_FAILED", "CANCELED"):
            code = _map_provider_failure(state, reason)
            self._transition(job, "FAILED", reason_code=code,
                             detail=reason, outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": code, "channel_switched": False}
        if state == "REJECTED_BUDGET":
            # F27：不降帧率、不缩窗口。两者都是"用更差的输入换一次通过"，
            # 而口型的输入是被授权的那一对，改了就超出授权范围。
            self._transition(
                job, "FAILED", reason_code="BUDGET",
                detail="budget refused; S32 does not lower fps or shrink the "
                       "window to fit", outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "BUDGET", "degraded": False}
        if state == "REJECTED_QUOTA":
            self._transition(job, "FAILED", reason_code="QUOTA",
                             detail=reason, outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "QUOTA"}
        return {"ignored": True,
                "reason": f"unmapped_provider_state:{state}"}

    def _job_by_attempt(self, tenant_id, attempt_id,
                        provider_job_id) -> dict | None:
        if not tenant_id:
            return None
        for column, value in (("attempt_id", attempt_id),
                              ("provider_job_id", provider_job_id)):
            if not value:
                continue
            row = _row(self.conn.execute(
                f"SELECT * FROM lipsync_job WHERE tenant_id=? AND {column}=?",
                (tenant_id, value)).fetchone())
            if row is not None:
                return row
        return None

    def _on_archived(self, job: dict, data: dict,
                     reason: str | None = None) -> dict:
        """S26 说归档完了。

        - `INGEST_FAILED`：归档重试耗尽，成片拿不到 → `FAILED`，缺口可见。
          **不重新生成**（E23 的同一条：不能因为拿不到字节再付一次钱）。
        - 事件里带齐归档事实 → 直接进 `_finish_ingest`
        - 否则留在 `INGESTING` 等 `artifact.ingested`
        """
        artifact = data.get("artifact_ref")
        if reason == "INGEST_FAILED" and job["state"] == "INGESTING":
            self._transition(
                job, "FAILED", reason_code="ARTIFACT_UNAVAILABLE",
                detail="S26 could not archive the output after its retries; "
                       "recorded rather than regenerating (E23)",
                outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "ARTIFACT_UNAVAILABLE",
                    "resubmitted": False, "gap_visible": True}
        if job["state"] == "WAITING_UNKNOWN":
            self.conn.execute(
                "UPDATE lipsync_job SET reconciled=1 WHERE lipsync_job_id=?",
                (job["lipsync_job_id"],))
            job = self._job(job["tenant_id"], job["lipsync_job_id"])
            job = self._transition(job, "INGESTING", reason_code="RECONCILED",
                                   outbox=self.outbox)
        if artifact:
            if job["state"] != "INGESTING":
                return {"ignored": True, "reason": "job_not_ingesting",
                        "state": job["state"]}
            return self._finish_ingest(job, artifact, data)
        return {"lipsync_job_id": job["lipsync_job_id"], "state": job["state"],
                "waiting": "artifact.ingested"}

    # -------------------------------------------------------- 事件：S28
    def on_artifact_ingested(self, event: dict) -> dict:
        """S28 `artifact.ingested` → `INGESTING → READY`。

        `artifact.ingested.v2` 里**没有** `frame_count` / `timecode_start` /
        `fps` / `ingesting_service`（缺口 C4）。S32 的收尾判断全都依赖前三个，
        所以：

        - 缺任何一个 → **阻塞在 `INGESTING` 并告警**，不伪造 READY（T32-38）
        - URL 过期（F22）→ 向 S26 要新 URL，**不重新生成**（T32-37）
        - hash 不符 → `FAILED(HASH_MISMATCH)`（T32-39）

        第四个（`ingesting_service`）在这里用不上：链式判断已经改由调用方的
        `video_ref` 承担（C4）。
        """
        data = event.get("data") or {}
        artifact = data.get("artifact_ref") or {}
        source = data.get("source") or {}
        tenant = event.get("tenant_id")
        job = self._job_for_artifact(tenant, artifact, source, data)
        if job is None:
            return {"ignored": True, "reason": "unknown_artifact"}
        if job["state"] not in ("INGESTING", "WAITING_UNKNOWN"):
            return {"ignored": True, "reason": "job_not_ingesting",
                    "state": job["state"]}
        if data.get("error") in ("URL_EXPIRED", "PRESIGNED_URL_EXPIRED") or \
                data.get("reason_code") == "URL_EXPIRED":
            return self._refresh_url(job, artifact, source)
        if source.get("content_hash") and artifact.get("content_hash") and \
                source["content_hash"] != artifact["content_hash"]:
            self._transition(job, "FAILED", reason_code="HASH_MISMATCH",
                             detail="provider-reported hash differs from the "
                                    "archived artifact hash",
                             outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "HASH_MISMATCH"}
        if job["state"] == "WAITING_UNKNOWN":
            self.conn.execute(
                "UPDATE lipsync_job SET reconciled=1 WHERE lipsync_job_id=?",
                (job["lipsync_job_id"],))
            job = self._job(job["tenant_id"], job["lipsync_job_id"])
            job = self._transition(job, "INGESTING", reason_code="RECONCILED",
                                   outbox=self.outbox)
        return self._finish_ingest(job, artifact, data)

    def _refresh_url(self, job: dict, artifact: dict, source: dict) -> dict:
        """F22：归档 URL 过期 → 问 S26 要新的，**不重新生成**。

        这条路径存在的唯一理由是省钱：字节已经在 S28 里，只是取不出来。
        重跑一次生成能"解决"问题，但那是拿一次新的付费调用来换一个 HTTP
        超时。`url_refresher` 失败就继续等，job 留在 `INGESTING`。
        """
        try:
            refreshed = self.url_refresher(job["tenant_id"], artifact,
                                           source)
        except (ServiceError, ConnectionError) as exc:
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "state": job["state"], "waiting": "fresh_url",
                    "warning": f"could not refresh archive URL: "
                               f"{getattr(exc, 'code', type(exc).__name__)}",
                    "regenerated": False}
        refreshed = refreshed if isinstance(refreshed, dict) else {}
        if not refreshed.get("url"):
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "state": job["state"], "waiting": "fresh_url",
                    "regenerated": False}
        return {"lipsync_job_id": job["lipsync_job_id"], "state": job["state"],
                "waiting": "artifact.ingested", "url_refreshed": True,
                "regenerated": False}

    def _job_for_artifact(self, tenant, artifact: dict, source: dict,
                          data: dict) -> dict | None:
        if not tenant:
            return None
        job_id = data.get("lipsync_job_id")
        if job_id:
            row = _row(self.conn.execute(
                "SELECT * FROM lipsync_job WHERE tenant_id=? AND "
                "lipsync_job_id=?", (tenant, job_id)).fetchone())
            if row is not None:
                return row
        provider_job_id = source.get("provider_job_id") or \
            data.get("provider_job_id")
        if provider_job_id:
            return _row(self.conn.execute(
                "SELECT * FROM lipsync_job WHERE tenant_id=? AND "
                "provider_job_id=?", (tenant, provider_job_id)).fetchone())
        return None

    # -------------------------------------------------------- 收尾
    def _finish_ingest(self, job: dict, artifact: dict, data: dict) -> dict:
        """`INGESTING` 的全部判定，一个事务。

        顺序是**权利约束优先于质量约束**：

        1. 归档事实齐不齐（F21）——缺就阻塞，不猜
        2. 非目标区域差异（F17）——越界就 FAILED 且**不登记 take**。它不是
           QC，是 `preserve_other_faces` 这个权利性质的校验
        3. 帧数 / 时码（F18/F19）——花了钱但产物对不上
        4. 输入对复查（F20）——输入变了，take 登记但标 stale，检查点不动
        5. 都通过 → 登记 take、CAS 推进检查点、发 ready

        第 2 步在第 4 步之前是刻意的：一个"输入已经变了"的产物不需要再量
        差异就能判定为不可交付，但反过来说，一个越界的产物即使输入没变也
        不能交付。先做不可逆的那一条。
        """
        tenant = job["tenant_id"]
        now = _now()
        facts = self._artifact_facts(tenant, artifact, data)
        if facts.get("blocked"):
            # F21：帧数/时码/fps 不全。留一份可见的"我在等什么"，而不是
            # 一个看起来成功的 READY。
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "state": job["state"], "waiting": facts["blocked"],
                    "warning": "artifact.ingested carried no "
                               + facts["blocked"] + "; S32 will not fake a "
                               "READY (C4)", "alert": True}

        processed = int(job["processed_frames"])
        out_fps = facts.get("fps")
        if _is_number(out_fps) and \
                abs(float(out_fps) - float(job["fps"])) > 0.01:
            self._transition(
                job, "FAILED", reason_code="FPS_MISMATCH",
                detail=f"output fps {out_fps} != requested {job['fps']}",
                outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "FPS_MISMATCH"}

        measured = self._measure(job, artifact)
        verdict, fail_code = classify_other_region(
            measured["max_diff_complement"])
        if fail_code is not None:
            self._transition(
                job, "FAILED", reason_code=fail_code,
                detail="non-target region changed by "
                       f"{measured['max_diff_complement']}/255 (warn "
                       f"{WARN_PIXEL}); preserve_other_faces is a rights "
                       f"property, so no take is registered",
                outbox=self.outbox)
            # 不登记 take，也就不登记度量：`output_diff_measurement` 的外键
            # 指向 take，为一次被拒的结果伪造一行 take 才有地方挂它。度量值
            # 通过 job 的 `failure_detail` 与 `s32_other_face_altered_total`
            # 计数可见，原始值留在重算日志里。
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": fail_code, "take_registered": False,
                    "other_region_max_diff":
                        float(measured["max_diff_complement"])}

        out_frames = facts.get("frame_count")
        if _is_int(out_frames) and int(out_frames) < processed:
            self._transition(
                job, "FAILED", reason_code="FRAME_COUNT_MISMATCH",
                detail=f"output has {out_frames} frames, job processed "
                       f"{processed}", outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "FRAME_COUNT_MISMATCH"}
        out_start = facts.get("timecode_start")
        if _is_int(out_start) and int(out_start) != int(job["process_frame_start"]):
            self._transition(
                job, "FAILED", reason_code="TIMECODE_MISMATCH",
                detail=f"output starts at frame {out_start}, job processed "
                       f"from {job['process_frame_start']}",
                outbox=self.outbox)
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "FAILED",
                    "reason_code": "TIMECODE_MISMATCH"}

        stale = self._stale_basis(job)
        take_id = _new_id("take")
        snapshot = _row(self.conn.execute(
            "SELECT * FROM input_eligibility_snapshot WHERE snapshot_id=?",
            (job["eligibility_snapshot_id"],)).fetchone()) or {}
        voice_check = snapshot.get("voice_defect_check") or "clean"
        if voice_check not in ("clean", "not_yet_available"):
            # job 能走到这里就说明当时是干净的；defects_open 的记录只可能
            # 来自 INPUTS_INELIGIBLE，那些 job 不会归档。保守成 not_yet_available。
            voice_check = "not_yet_available"
        self.conn.execute(
            "INSERT INTO lipsync_take (lipsync_take_id, tenant_id, "
            "lipsync_job_id, shot_lineage_id, speaker_character_id, "
            "artifact_ref, video_hash, audio_hash, input_fingerprint, "
            "output_frame_start, output_frame_end, output_frame_count, "
            "output_fps, visibility_grade, other_region_max_diff, "
            "other_face_drift, voice_defect_check_at_build, s37_report_ref, "
            "provider_job_id, attempt_id, repair_action_id, strategy_class, "
            "stale_basis, stale_detected_at, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (take_id, tenant, job["lipsync_job_id"], job["shot_lineage_id"],
             job["speaker_character_id"], _jcs(artifact),
             _loads(job["video_ref"], {})["content_hash"],
             _loads(job["audio_ref"], {})["content_hash"],
             job["input_fingerprint"],
             int(job["process_frame_start"]),
             int(job["process_frame_start"]) + processed - 1, processed,
             float(job["fps"]), job["visibility_grade"],
             float(measured["max_diff_complement"]), verdict,
             voice_check, snapshot.get("s37_report_ref"),
             job["provider_job_id"] or "", job["attempt_id"] or "",
             job["repair_action_id"], job["strategy_class"], stale,
             now if stale else None, "READY", now))
        self.conn.execute(
            "INSERT INTO output_diff_measurement (measurement_id, tenant_id, "
            "lipsync_take_id, eps_pixel, warn_pixel, mask_dilate_px, "
            "max_diff_complement, max_diff_target_region, "
            "changed_frame_ratio_complement, frames_compared, measured_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_new_id("odm"), tenant, take_id, EPS_PIXEL, WARN_PIXEL,
             MASK_DILATE_PX, float(measured["max_diff_complement"]),
             float(measured["max_diff_target_region"]),
             float(measured["changed_frame_ratio_complement"]),
             int(measured["frames_compared"]), now))

        if stale is not None:
            # F20：花了钱，产物存在，但输入已变 —— 登记 take、标依据、**不**
            # 推进检查点、**不**发 ready。对账成功 ≠ 结果可用。
            self._transition(job, "READY", reason_code="STALE",
                             detail=f"input changed before ingest "
                                    f"({stale}); take kept for audit but the "
                                    f"checkpoint is not advanced",
                             outbox=self.outbox)
            self.outbox.append(new_event(
                source=SOURCE, type=LOCAL_TAKE_STALE,
                subject=f"lipsync-take/{take_id}", tenant_id=tenant,
                home_cell="lipsync", aggregate_id=take_id,
                sequence=self._next_sequence(tenant, take_id),
                correlation_id=job["lipsync_job_id"],
                data={"lipsync_take_id": take_id,
                      "lipsync_job_id": job["lipsync_job_id"],
                      "shot_lineage_id": job["shot_lineage_id"],
                      "stale_basis": stale},
                schema_version=SCHEMA_VERSION))
            return {"lipsync_job_id": job["lipsync_job_id"], "state": "READY",
                    "lipsync_take_id": take_id, "stale_basis": stale,
                    "checkpoint_advanced": False, "ready_published": False}

        final_state = "RECONCILED_READY" if job["reconciled"] else "READY"
        checkpoint = self._advance_checkpoint(job, take_id, now)
        job2 = self._job(tenant, job["lipsync_job_id"])
        job2 = self._transition(job2, final_state, outbox=self.outbox)
        self._publish_ready(job2, take_id, artifact, measured, checkpoint)
        return {"lipsync_job_id": job["lipsync_job_id"], "state": final_state,
                "lipsync_take_id": take_id,
                "checkpoint_seq": checkpoint["checkpoint_seq"],
                "checkpoint_advanced": True, "ready_published": True}

    def _artifact_facts(self, tenant: str, artifact: dict,
                        data: dict) -> dict:
        """归档事实：事件里有的用事件，没有再问 S28。**缺就 blocked。**

        从不猜：`frame_count` 猜小会让 F18 静默通过，猜大会让一条对的产物
        被判死。两者都比"阻塞并告警"更糟。
        """
        facts = {"frame_count": data.get("frame_count"),
                 "timecode_start": data.get("timecode_start"),
                 "fps": data.get("fps") or data.get("frame_rate")}
        missing = [k for k in ("frame_count", "timecode_start", "fps")
                   if facts[k] is None]
        if missing:
            try:
                fetched = self.artifact_facts(tenant, artifact)
            except (ServiceError, ConnectionError):
                fetched = None
            if isinstance(fetched, dict):
                for key in missing:
                    if fetched.get(key) is not None:
                        facts[key] = fetched[key]
                if facts["fps"] is None and fetched.get("frame_rate_num") and \
                        fetched.get("frame_rate_den"):
                    facts["fps"] = (float(fetched["frame_rate_num"]) /
                                    float(fetched["frame_rate_den"]))
            missing = [k for k in ("frame_count", "timecode_start", "fps")
                       if facts[k] is None]
        if missing:
            return {"blocked": ",".join(missing)}
        if isinstance(facts["frame_count"], bool) or \
                not _is_int(facts["frame_count"]):
            return {"blocked": "frame_count"}
        if facts["fps"] is None and data.get("frame_rate_num") and \
                data.get("frame_rate_den"):
            # artifact.ingested 的帧率是分数（num/den），可以从它推出 fps。
            facts["fps"] = (float(data["frame_rate_num"]) /
                            float(data["frame_rate_den"]))
        if not _is_number(facts["fps"]):
            return {"blocked": "fps"}
        facts["blocked"] = None
        return facts

    def _measure(self, job: dict, artifact: dict) -> dict:
        """§4.6 的确定性计算，由注入的 `diff_measurer` 执行。

        重启后**必须重算**（§7.3）：内存里的度量值在崩溃时已经没了，沿用
        一个不存在的值就是伪造证据（T32-46）。

        `diff_measurer` 不可达时**不能**当成"没越界"。抛 503 让整笔回滚，
        job 留在 `INGESTING`——一个量不出来的产物既不能交付也不能判死。
        """
        request = {"artifact_ref": artifact,
                   "video_ref": _loads(job["video_ref"], {}),
                   "face_track_binding_id": job["face_track_binding_id"],
                   "process_frame_start": int(job["process_frame_start"]),
                   "process_frame_end": int(job["process_frame_end"]),
                   "eps_pixel": EPS_PIXEL, "warn_pixel": WARN_PIXEL,
                   "mask_dilate_px": MASK_DILATE_PX}
        try:
            measured = self.diff_measurer(job["tenant_id"], request)
        except ConnectionError as exc:
            raise ServiceError(
                503, "DIFF_MEASUREMENT_UNAVAILABLE",
                "cannot measure the non-target region; refusing to call a "
                "take ready without evidence that other faces were preserved",
                retryable=True) from exc
        measured = measured if isinstance(measured, dict) else {}
        for key in ("max_diff_complement", "max_diff_target_region",
                    "changed_frame_ratio_complement", "frames_compared"):
            if key not in measured:
                raise ServiceError(
                    503, "DIFF_MEASUREMENT_UNAVAILABLE",
                    f"diff measurement is missing {key}", retryable=True)
        return measured

    def _stale_basis(self, job: dict) -> str | None:
        """§7.2 "作废优先"：`READY` 转移**事务内**重读输入对。

        重读本身在 `_finish_ingest` 的事务里，所以并发的撤销事件要么在它
        之前（那就会在这里被看到）要么在它之后（那它会看到 `READY` 并标
        stale）。两种顺序都得到一个"没有被推进的检查点"。
        """
        tenant = job["tenant_id"]
        video_ref = _loads(job["video_ref"], {})
        audio_ref = _loads(job["audio_ref"], {})
        binding = _row(self.conn.execute(
            "SELECT face_track_ref FROM face_track_binding "
            "WHERE face_track_binding_id=?",
            (job["face_track_binding_id"],)).fetchone()) or {}
        try:
            facts = self.eligibility(tenant, {
                "video_ref": video_ref, "audio_ref": audio_ref,
                "audio_take_id": job["audio_take_id"],
                "speaker_character_id": job["speaker_character_id"],
                "face_track_ref": _loads(binding.get("face_track_ref"), {}),
            })
        except (ServiceError, ConnectionError):
            # 复查不可达：不能因此判定"没变"。保守地当作已变——一个标了
            # stale 的 take 仍可查、仍可解释，而一个错标为可用的 take 会被
            # 下游当成当前基线。
            return "video_changed"
        facts = facts if isinstance(facts, dict) else {}
        current_video = (facts.get("video") or {}).get("content_hash")
        if current_video and current_video != video_ref.get("content_hash"):
            return "video_changed"
        current_audio = (facts.get("audio") or {}).get("content_hash")
        if current_audio and current_audio != audio_ref.get("content_hash"):
            return "audio_changed"
        if (facts.get("audio_take") or {}).get("rights_state") == "void":
            return "audio_rights_void"
        if (facts.get("video") or {}).get("tombstoned") is True:
            return "video_tombstoned"
        return None

    def _advance_checkpoint(self, job: dict, take_id: str,
                            now: str) -> dict:
        """按 `speaker_character_id` 分行的 CAS（§7.2）。

        不同说话人各占一行，同镜两人并发互不影响（T32-42）。同一人两行并发
        时后到者 `rowcount != 1` → 整笔回滚、可重试，而不是覆盖掉另一个
        take 的推进（T32-43）。
        """
        tenant = job["tenant_id"]
        shot = job["shot_lineage_id"]
        speaker = job["speaker_character_id"]
        row = _row(self.conn.execute(
            "SELECT * FROM lipsync_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND speaker_character_id=?",
            (tenant, shot, speaker)).fetchone())
        previous = row["lipsync_take_id"] if row else None
        if row is None:
            self.conn.execute(
                "INSERT INTO lipsync_checkpoint (tenant_id, shot_lineage_id, "
                "speaker_character_id, lipsync_take_id, checkpoint_seq, "
                "expected_revision, updated_by_job, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tenant, shot, speaker, take_id, 1, 0,
                 job["lipsync_job_id"], now))
            seq = 1
        else:
            old_seq = int(row["checkpoint_seq"])
            cur = self.conn.execute(
                "UPDATE lipsync_checkpoint SET lipsync_take_id=?, "
                "checkpoint_seq=?, expected_revision=?, updated_by_job=?, "
                "updated_at=? WHERE tenant_id=? AND shot_lineage_id=? AND "
                "speaker_character_id=? AND checkpoint_seq=?",
                (take_id, old_seq + 1, old_seq, job["lipsync_job_id"], now,
                 tenant, shot, speaker, old_seq))
            if cur.rowcount != 1:
                # G15：`expected_revision` 是本表行自己的版本号，与 S08 的
                # accepted pointer 无关。CAS 失败就是"别人抢先了"。
                raise ServiceError(409, "CHECKPOINT_CAS_FAILED",
                                   "checkpoint advanced concurrently",
                                   retryable=True)
            seq = old_seq + 1
        return {"shot_lineage_id": shot, "speaker_character_id": speaker,
                "lipsync_take_id": take_id, "checkpoint_seq": seq,
                "from": previous}

    def _publish_ready(self, job: dict, take_id: str, artifact: dict,
                       measured: dict, checkpoint: dict) -> None:
        """`production.lipsync.ready`，**16 个必填字段一个不少**。

        严格按契约的最窄形状发（G10）：
        - `source_video_ref` / `source_audio_ref` 只有 `{id, content_hash}`
        - `audio_window` 只有 `{start_ms, end_ms}`（trim/source 留在查询接口）
        - `frames` 只有 `{process_start, process_end, fps}`

        **不发**：媒体 URL、人脸轨引用（生物特征派生）、供应商信息、差异
        掩膜（§6.4）。`other_region_max_diff` 的原始值也不发——事件里只有
        折算后的 `other_face_drift`（PLR-5）。要原始值的消费者读
        `GET /lipsync-takes/{id}`。
        """
        video_ref = _loads(job["video_ref"], {})
        audio_ref = _loads(job["audio_ref"], {})
        take = _row(self.conn.execute(
            "SELECT * FROM lipsync_take WHERE lipsync_take_id=?",
            (take_id,)).fetchone()) or {}
        self.outbox.append(new_event(
            source=SOURCE, type=EVENT_LIPSYNC_READY,
            subject=f"shot/{job['shot_lineage_id']}",
            tenant_id=job["tenant_id"], home_cell="lipsync",
            aggregate_id=job["shot_lineage_id"],
            sequence=self._next_sequence(job["tenant_id"],
                                         job["shot_lineage_id"]),
            correlation_id=job["lipsync_job_id"],
            data={
                "schema_version": SCHEMA_VERSION,
                "lipsync_job_id": job["lipsync_job_id"],
                "lipsync_take_id": take_id,
                "shot_lineage_id": job["shot_lineage_id"],
                "speaker_character_id": job["speaker_character_id"],
                "artifact_ref": artifact,
                "source_video_ref": {"id": video_ref.get("id"),
                                     "content_hash":
                                         video_ref.get("content_hash")},
                "source_audio_ref": {"id": audio_ref.get("id"),
                                     "content_hash":
                                         audio_ref.get("content_hash")},
                "audio_take_id": job["audio_take_id"] or "",
                "audio_window": {"start_ms": int(job["window_start_ms"]),
                                 "end_ms": int(job["window_end_ms"])},
                "frames": {"process_start": int(job["process_frame_start"]),
                           "process_end": int(job["process_frame_end"]),
                           "fps": float(job["fps"])},
                "visibility_grade": take.get("visibility_grade") or "full",
                "other_face_drift": take.get("other_face_drift") or "none",
                "voice_defect_check_at_build":
                    take.get("voice_defect_check_at_build") or "clean",
                "checkpoint": {"from": checkpoint["from"], "to": take_id,
                               "checkpoint_seq":
                                   int(checkpoint["checkpoint_seq"])},
                "input_fingerprint": job["input_fingerprint"],
            },
            schema_version=SCHEMA_VERSION))

    # -------------------------------------------------------- 事件：作废
    def _mark_stale(self, *, tenant: str, take_id: str, basis: str,
                    now: str, detail: str | None = None) -> bool:
        """`stale_basis` 只写一次（`WHERE stale_basis IS NULL`）。

        第一条依据留下，后续依据不覆盖：一个 take 因为"音频被撤"过期，之后
        视频也换了，它依然是"因音频被撤而过期"。覆盖会让审计看到的因果随
        事件到达顺序漂移。
        """
        cur = self.conn.execute(
            "UPDATE lipsync_take SET stale_basis=?, stale_detected_at=? "
            "WHERE tenant_id=? AND lipsync_take_id=? AND stale_basis IS NULL",
            (basis, now, tenant, take_id))
        if cur.rowcount != 1:
            return False
        take = _row(self.conn.execute(
            "SELECT * FROM lipsync_take WHERE lipsync_take_id=?",
            (take_id,)).fetchone()) or {}
        self.outbox.append(new_event(
            source=SOURCE, type=LOCAL_TAKE_STALE,
            subject=f"lipsync-take/{take_id}", tenant_id=tenant,
            home_cell="lipsync", aggregate_id=take_id,
            sequence=1, correlation_id=take_id,
            data={"lipsync_take_id": take_id,
                  "lipsync_job_id": take.get("lipsync_job_id"),
                  "shot_lineage_id": take.get("shot_lineage_id"),
                  "stale_basis": basis,
                  "detail": detail},
            schema_version=SCHEMA_VERSION))
        return True

    def on_speech_ready(self, event: dict) -> dict:
        """S31 `production.speech.ready`：同一句有了新音频。

        判据是**指纹里的 audio_hash 变了**，不是"事件到了"。同一个音频迟到
        的重复事件不该把 take 作废 —— 那是把"投递次数"当成"内容变化"。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        shot = data.get("shot_lineage_id")
        speaker = data.get("speaker_character_id")
        audio_take_id = data.get("voice_take_id")
        artifact = data.get("artifact_ref") or {}
        new_hash = artifact.get("content_hash")
        if not tenant or not shot or not speaker:
            return {"ignored": True, "reason": "missing_shot_or_speaker"}
        now = _now()
        stale: list[str] = []
        rows = self.conn.execute(
            "SELECT * FROM lipsync_take WHERE tenant_id=? AND "
            "shot_lineage_id=? AND speaker_character_id=? AND "
            "stale_basis IS NULL", (tenant, shot, speaker)).fetchall()
        for raw in rows:
            take = dict(raw)
            if new_hash and take["audio_hash"] == new_hash:
                continue
            job = _row(self.conn.execute(
                "SELECT audio_take_id FROM lipsync_job WHERE lipsync_job_id=?",
                (take["lipsync_job_id"],)).fetchone()) or {}
            same_take = audio_take_id and \
                job.get("audio_take_id") == audio_take_id
            if not new_hash and not same_take:
                # 既说不出新 hash、又不是同一条 take：无法判断有没有变。
                # 判"变了"会误伤，判"没变"会让一个可能错的基线留下。选择
                # 不动并把这件事报给调用方，由上游补上 hash。
                continue
            if self._mark_stale(tenant=tenant,
                                take_id=take["lipsync_take_id"],
                                basis="audio_changed", now=now,
                                detail=f"new audio artifact "
                                       f"{artifact.get('id')}"):
                stale.append(take["lipsync_take_id"])
        return {"shot_lineage_id": shot, "speaker_character_id": speaker,
                "stale_takes": stale}

    def on_voice_authorization_lapsed(self, event: dict) -> dict:
        """S31 `speech.voice-authorization-lapsed`（建议名，缺口 G13）。

        音频字节没变，但它现在不许用了。take 标 `audio_rights_void`，**不
        删除**——"2026-10-30 那次口型用的音频后来被撤销了"是必须能回答的
        问题（E39）。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        affected = data.get("affected_take_ids")
        if not tenant:
            return {"ignored": True, "reason": "missing_tenant"}
        now = _now()
        take_ids: list[str] = []
        if affected:
            take_ids = [t for t in affected if isinstance(t, str)]
        else:
            rows = self.conn.execute(
                "SELECT lipsync_take_id FROM lipsync_take WHERE tenant_id=? "
                "AND stale_basis IS NULL", (tenant,)).fetchall()
            take_ids = [r["lipsync_take_id"] for r in rows]
        stale = [t for t in take_ids
                 if self._mark_stale(tenant=tenant, take_id=t,
                                     basis="audio_rights_void", now=now,
                                     detail=data.get("to_state"))]
        return {"stale_takes": stale, "voided": len(stale)}

    def on_video_ready(self, event: dict) -> dict:
        """S30 `production.video.ready`：同镜有了新片段。

        只按 `shot_lineage_id` 匹配，且要求新片段 hash 与该 take 的
        `video_hash` 不同——迟到/重复的同一条 ready 事件不该作废任何东西。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        shot = data.get("shot_lineage_id")
        artifact = data.get("artifact_ref") or {}
        new_hash = artifact.get("content_hash")
        if not tenant or not shot:
            return {"ignored": True, "reason": "missing_shot_lineage_id"}
        if not new_hash:
            return {"ignored": True, "reason": "no_content_hash",
                    "note": "cannot tell a new clip from a redelivery"}
        now = _now()
        stale = []
        for raw in self.conn.execute(
                "SELECT * FROM lipsync_take WHERE tenant_id=? AND "
                "shot_lineage_id=? AND stale_basis IS NULL",
                (tenant, shot)).fetchall():
            take = dict(raw)
            if take["video_hash"] == new_hash:
                continue
            if self._mark_stale(tenant=tenant,
                                take_id=take["lipsync_take_id"],
                                basis="video_changed", now=now,
                                detail=f"new clip {artifact.get('id')}"):
                stale.append(take["lipsync_take_id"])
        return {"shot_lineage_id": shot, "stale_takes": stale}

    def on_alignment_completed(self, event: dict) -> dict:
        """S37 对齐完成：**只挂引用，不改状态**。

        数值一律不复制（复制就是第二个权威，而 S37 换 ASR 模型重跑后会静默
        过期）。也不因为出现了 `AUD.LIPSYNC.OFFSET` 就自己再排一次口型——
        那正是 §8 的失控路径。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        report_id = data.get("report_id")
        generation = data.get("report_generation")
        if not tenant or not report_id:
            return {"ignored": True, "reason": "missing_report_id"}
        ref = _jcs({"service_id": "S37", "id": report_id,
                    "revision": int(generation or 1)})
        audio_take_id = data.get("voice_take_id")
        if audio_take_id:
            rowcount = self.conn.execute(
                "UPDATE lipsync_take SET s37_report_ref=? WHERE tenant_id=? "
                "AND s37_report_ref IS NULL AND lipsync_job_id IN ("
                "SELECT lipsync_job_id FROM lipsync_job WHERE tenant_id=? "
                "AND audio_take_id=?)",
                (ref, tenant, tenant, audio_take_id)).rowcount
            return {"voice_take_id": audio_take_id, "updated": bool(rowcount)}
        return {"ignored": True, "reason": "no_voice_take_id"}

    def on_timeline_revision(self, event: dict) -> dict:
        """S41：镜头被删 → take 变 `SUPERSEDED`（**不是 stale、不是删除**）。

        区别要紧：stale 是"输入变了，结论不再对应当前素材"；SUPERSEDED 是
        "这段剪辑不用了"。混成一个词，审计就分不清哪次是撤销。
        """
        data = event.get("data") or {}
        tenant = event.get("tenant_id")
        shot = data.get("shot_lineage_id")
        if not tenant or not shot:
            return {"ignored": True, "reason": "missing_shot_lineage_id"}
        if data.get("event_kind") not in ("shot_removed", "shot_deleted") and \
                data.get("change") not in ("removed", "deleted"):
            return {"ignored": True, "reason": "not_a_removal"}
        rowcount = self.conn.execute(
            "UPDATE lipsync_take SET state='SUPERSEDED' WHERE tenant_id=? AND "
            "shot_lineage_id=? AND state IN ('READY','ACCEPTED')",
            (tenant, shot)).rowcount
        return {"shot_lineage_id": shot, "superseded": rowcount}

    def on_repair_planned(self, event: dict) -> dict:
        """S39 修复计划：**只读**。绝不据此自动建 job。

        自动执行会让 S39 变成 S32 的第二权威：它说"重做"就花一次钱，而
        S32 的设计是"调用方带着 repair_action 来请求"。
        """
        data = event.get("data") or {}
        action = data.get("top_action")
        return {"observed": True, "top_action": action,
                "would_execute": action in OWNED_ACTIONS,
                "note": "S32 never creates a job from a repair plan; the "
                        "caller must POST /lipsync-jobs with the repair_action"}

    def on_manifest_committed(self, event: dict) -> dict:
        """S08 清单提交：只读投影。`node_epoch` 变化**不改本地状态**。"""
        return {"observed": True, "ignored": True,
                "reason": "read_only_projection"}

    # -------------------------------------------------------- 查询
    def get_job(self, tenant_id: str, job_id: str) -> dict:
        job = self._job(tenant_id, job_id)
        take = _row(self.conn.execute(
            "SELECT * FROM lipsync_take WHERE tenant_id=? AND lipsync_job_id=? "
            "ORDER BY created_at DESC", (tenant_id, job_id)).fetchone())
        binding = _row(self.conn.execute(
            "SELECT * FROM face_track_binding WHERE face_track_binding_id=?",
            (job["face_track_binding_id"],)).fetchone()) or {}
        snapshot = _row(self.conn.execute(
            "SELECT * FROM input_eligibility_snapshot WHERE snapshot_id=?",
            (job["eligibility_snapshot_id"],)).fetchone()) or {}
        checkpoint = _row(self.conn.execute(
            "SELECT * FROM lipsync_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? AND speaker_character_id=?",
            (tenant_id, job["shot_lineage_id"],
             job["speaker_character_id"])).fetchone())
        history = [dict(r) for r in self.conn.execute(
            "SELECT seq, from_state, to_state, reason_code, detail, at FROM "
            "job_transition WHERE lipsync_job_id=? ORDER BY seq", (job_id,))]
        return {
            "lipsync_job_id": job["lipsync_job_id"],
            "state": job["state"],
            "state_seq": int(job["state_seq"]),
            "business_attempt_no": int(job["business_attempt_no"]),
            "provider_job_id": job["provider_job_id"],
            "failure_code": job["failure_code"],
            "failure_detail": job["failure_detail"],
            "inputs": {"video_ref": _loads(job["video_ref"], None),
                       "audio_ref": _loads(job["audio_ref"], None),
                       "audio_take_id": job["audio_take_id"],
                       "face_track_track_id": binding.get("track_id")},
            "audio_window": {
                "start_ms": int(job["window_start_ms"]),
                "end_ms": int(job["window_end_ms"]),
                "trim_head_ms": int(job["trim_head_ms"]),
                "trim_tail_ms": int(job["trim_tail_ms"]),
                "source": job["window_source"]},
            "frames": {"process_start": int(job["process_frame_start"]),
                       "process_end": int(job["process_frame_end"]),
                       "processed": int(job["processed_frames"]),
                       "fps": float(job["fps"])},
            "input_fingerprint": job["input_fingerprint"],
            "repair_action_id": job["repair_action_id"],
            "strategy_class": job["strategy_class"],
            "node_epoch_at_create": int(job["node_epoch_at_create"]),
            "execution_fence": int(job["execution_fence"]),
            "eligibility": {
                "voice_defect_check": snapshot.get("voice_defect_check"),
                "audio_rights_state": snapshot.get("audio_rights_state"),
                "face_authorization_state":
                    snapshot.get("face_authorization_state"),
                "video_referenceable": bool(snapshot.get("video_referenceable")),
                "open_defect_ids": _loads(snapshot.get("open_defect_ids"), []),
                "s37_report_ref": _loads(snapshot.get("s37_report_ref"), None),
                "captured_at": snapshot.get("captured_at")},
            "take": None if take is None else {
                "lipsync_take_id": take["lipsync_take_id"],
                "artifact_ref": _loads(take["artifact_ref"], None),
                "visibility_grade": take["visibility_grade"],
                "other_region_max_diff": float(take["other_region_max_diff"]),
                "other_face_drift": take["other_face_drift"],
                "voice_defect_check_at_build":
                    take["voice_defect_check_at_build"],
                "stale_basis": take["stale_basis"],
                "stale_detected_at": take["stale_detected_at"],
                "state": take["state"]},
            "checkpoint": None if checkpoint is None else {
                "shot_lineage_id": checkpoint["shot_lineage_id"],
                "speaker_character_id": checkpoint["speaker_character_id"],
                "lipsync_take_id": checkpoint["lipsync_take_id"],
                "checkpoint_seq": int(checkpoint["checkpoint_seq"])},
            "frozen": job["state"] == "WAITING_UNKNOWN",
            "must_reconcile_before_resubmit":
                job["state"] == "WAITING_UNKNOWN",
            "history": history,
        }

    def list_jobs(self, tenant_id: str, *, shot_lineage_id=None,
                  speaker_character_id=None, state=None) -> dict:
        sql = "SELECT * FROM lipsync_job WHERE tenant_id=?"
        args: list = [tenant_id]
        for column, value in (("shot_lineage_id", shot_lineage_id),
                              ("speaker_character_id", speaker_character_id),
                              ("state", state)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        sql += " ORDER BY created_at DESC LIMIT 200"
        return {"items": [
            {"lipsync_job_id": r["lipsync_job_id"],
             "shot_lineage_id": r["shot_lineage_id"],
             "speaker_character_id": r["speaker_character_id"],
             "state": r["state"],
             "failure_code": r["failure_code"],
             "input_fingerprint": r["input_fingerprint"],
             "created_at": r["created_at"]}
            for r in self.conn.execute(sql, args)]}

    def get_shot_checkpoint(self, tenant_id: str, shot_lineage_id: str) -> dict:
        """S41 / S42 的主读口：该镜**全部说话人**的当前基线。

        一行一个说话人（T32-42）。检查点里的 take 若已 stale 或 SUPERSEDED，
        **仍然显示**但标出来：把它藏起来会让下游以为"这个镜头没人说话"。
        """
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM lipsync_checkpoint WHERE tenant_id=? AND "
            "shot_lineage_id=? ORDER BY speaker_character_id",
            (tenant_id, shot_lineage_id))]
        for row in rows:
            take = _row(self.conn.execute(
                "SELECT * FROM lipsync_take WHERE lipsync_take_id=?",
                (row["lipsync_take_id"],)).fetchone()) or {}
            row["take_state"] = take.get("state")
            row["stale_basis"] = take.get("stale_basis")
        return {"shot_lineage_id": shot_lineage_id, "items": rows}

    def get_take(self, tenant_id: str, take_id: str) -> dict:
        take = self._take(tenant_id, take_id)
        measurement = _row(self.conn.execute(
            "SELECT * FROM output_diff_measurement WHERE tenant_id=? AND "
            "lipsync_take_id=? ORDER BY measured_at DESC",
            (tenant_id, take_id)).fetchone())
        job = _row(self.conn.execute(
            "SELECT * FROM lipsync_job WHERE lipsync_job_id=?",
            (take["lipsync_job_id"],)).fetchone()) or {}
        return {
            "lipsync_take_id": take["lipsync_take_id"],
            "lipsync_job_id": take["lipsync_job_id"],
            "shot_lineage_id": take["shot_lineage_id"],
            "speaker_character_id": take["speaker_character_id"],
            "artifact_ref": _loads(take["artifact_ref"], None),
            "video_hash": take["video_hash"],
            "audio_hash": take["audio_hash"],
            "input_fingerprint": take["input_fingerprint"],
            "output_frames": {"start": int(take["output_frame_start"]),
                              "end": int(take["output_frame_end"]),
                              "count": int(take["output_frame_count"]),
                              "fps": float(take["output_fps"])},
            "visibility_grade": take["visibility_grade"],
            "other_region_max_diff": float(take["other_region_max_diff"]),
            "other_face_drift": take["other_face_drift"],
            "voice_defect_check_at_build":
                take["voice_defect_check_at_build"],
            "s37_report_ref": _loads(take["s37_report_ref"], None),
            "stale_basis": take["stale_basis"],
            "stale_detected_at": take["stale_detected_at"],
            "state": take["state"],
            "repair_action_id": take["repair_action_id"],
            "strategy_class": take["strategy_class"],
            "audio_take_id": job.get("audio_take_id"),
            "measurement": None if measurement is None else {
                "eps_pixel": float(measurement["eps_pixel"]),
                "warn_pixel": float(measurement["warn_pixel"]),
                "mask_dilate_px": int(measurement["mask_dilate_px"]),
                "max_diff_complement":
                    float(measurement["max_diff_complement"]),
                "max_diff_target_region":
                    float(measurement["max_diff_target_region"]),
                "changed_frame_ratio_complement":
                    float(measurement["changed_frame_ratio_complement"]),
                "frames_compared": int(measurement["frames_compared"]),
                "measured_at": measurement["measured_at"]},
        }

    def validity(self, tenant_id: str, take_id: str, *,
                 video_hash: str | None = None,
                 audio_hash: str | None = None) -> dict:
        """§4.2 的三路合取，**现场算**。

        1. 指纹相等（视频 hash、音频 hash、窗口都在指纹里）
        2. S31 说音频权利仍 usable（live 读——撤销发生在归档之后，拷进
           take 的值永远是 usable）
        3. S28 说输入仍可引用（tombstone 之后 hash 不变但字节没了）

        三路必须全真。任一路假就 `valid: false` 并给出 `basis`。**没有
        `valid_until`**：有效期不是时间函数，它只能被"输入变了"驱动。
        """
        take = self._take(tenant_id, take_id)
        if take["state"] not in ("READY", "ACCEPTED"):
            return {"lipsync_take_id": take_id, "valid": False,
                    "basis": "state_" + str(take["state"]).lower(),
                    "checks": {}}
        if take["stale_basis"] is not None:
            return {"lipsync_take_id": take_id, "valid": False,
                    "basis": take["stale_basis"], "checks": {}}
        checks = {"video_hash_matches":
                  video_hash is None or video_hash == take["video_hash"],
                  "audio_hash_matches":
                  audio_hash is None or audio_hash == take["audio_hash"]}
        basis = None
        if not checks["video_hash_matches"]:
            basis = "video_changed"
        elif not checks["audio_hash_matches"]:
            basis = "audio_changed"
        if basis is None:
            rights = self._rights_state(tenant_id, take["lipsync_job_id"])
            checks["audio_rights_usable"] = rights == "usable"
            if rights == "void":
                basis = "audio_rights_void"
            elif rights != "usable":
                # S31 读不到（`unknown`）与"已撤销"是两种事实，不能共用一个
                # 依据码；但结论一样：§4.2 的合取要求 `audio_rights_state ==
                # 'usable'`，读不到就不是 usable。曾经这里只在 `void` 上落
                # basis，于是 `checks.audio_rights_usable=False` 却 `valid=True`
                # —— 自证的检查与结论互相打架。
                basis = "audio_rights_unknown"
        if basis is None:
            artifact = _loads(take["artifact_ref"], {})
            try:
                referenceable = bool(self.artifact_check(tenant_id, artifact))
            except (ServiceError, ConnectionError):
                referenceable = False
            checks["artifact_referenceable"] = referenceable
            if not referenceable:
                basis = "artifact_unavailable"
        return {"lipsync_take_id": take_id, "valid": basis is None,
                "basis": basis, "checks": checks}

    def _rights_state(self, tenant_id: str, job_id: str) -> str:
        job = _row(self.conn.execute(
            "SELECT audio_take_id FROM lipsync_job WHERE lipsync_job_id=?",
            (job_id,)).fetchone()) or {}
        if not job.get("audio_take_id"):
            return "usable"
        try:
            state = self.rights_reader(tenant_id, job["audio_take_id"])
        except (ServiceError, ConnectionError):
            # 查不到 ≠ 有授权。有效期判定上宁可保守。
            return "unknown"
        state = state if isinstance(state, dict) else {}
        return state.get("rights_state") or "unknown"

    # -------------------------------------------------------- 命令
    def cancel_job(self, tenant_id: str, job_id: str) -> dict:
        job = self._job(tenant_id, job_id)
        if job["state"] in TERMINAL_STATES:
            raise ServiceError(409, "JOB_NOT_CANCELABLE",
                               f"job is {job['state']}",
                               details={"state": job["state"]})
        detail = None
        if job["provider_job_id"]:
            try:
                self.canceler(tenant_id, job["provider_job_id"], "USER_CANCEL")
            except (ServiceError, ConnectionError) as exc:
                # 取消没确认：转 `WAITING_UNKNOWN` 而不是假装取消。用户看到
                # 的必须与真实在途状态一致，否则下一次轮询会对不上。
                self._transition(job, "WAITING_UNKNOWN",
                                 reason_code="CANCEL_UNCONFIRMED",
                                 detail=str(exc), outbox=self.outbox)
                return {"lipsync_job_id": job_id, "state": "WAITING_UNKNOWN",
                        "canceled": False,
                        "must_reconcile_before_resubmit": True}
            detail = "S26 accepted the cancel"
        self._transition(job, "CANCELED", reason_code="USER_CANCEL",
                         detail=detail, outbox=self.outbox)
        return {"lipsync_job_id": job_id, "state": "CANCELED",
                "canceled": True}

    def retry_job(self, tenant_id: str, job_id: str) -> dict:
        """只有 `{FAILED, RECONCILED_FAILED}` 能重试。**409 而非 422。**

        `INPUTS_INELIGIBLE` 也回 409：重试同一个 job 没有意义——它的输入
        仍然不合格，要有新音频就得发一次新请求（§3.2）。`WAITING_UNKNOWN`
        同理，要先对账（E16）。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] == "INPUTS_INELIGIBLE":
            raise ServiceError(
                409, "JOB_NOT_RETRYABLE",
                "the input itself was ineligible; retrying the same job would "
                "re-check the same audio that cannot be used. Fix the upstream "
                "input and POST a new job",
                details={"state": "INPUTS_INELIGIBLE",
                         "failure_code": job["failure_code"]})
        if job["state"] == "WAITING_UNKNOWN":
            raise ServiceError(
                409, "MUST_RECONCILE_FIRST",
                "the previous submission's outcome is unknown; resubmitting "
                "blind would double-charge. Reconcile with S26 first",
                details={"state": "WAITING_UNKNOWN"})
        if job["state"] not in RETRYABLE_STATES:
            raise ServiceError(409, "JOB_NOT_RETRYABLE",
                               f"job is {job['state']}",
                               details={"state": job["state"]})
        nxt = int(job["business_attempt_no"]) + 1
        now = _now()
        self.conn.execute(
            "UPDATE lipsync_job SET business_attempt_no=?, state='PREPARING', "
            "state_seq=state_seq+1, failure_code=NULL, failure_detail=NULL, "
            "updated_at=?, state_entered_at=? WHERE lipsync_job_id=?",
            (nxt, now, now, job_id))
        self.conn.execute(
            "INSERT INTO job_transition (lipsync_job_id, seq, from_state, "
            "to_state, reason_code, detail, at) VALUES (?,?,?,?,?,?,?)",
            (job_id, int(job["state_seq"]) + 1, job["state"], "PREPARING",
             "RETRY", f"business_attempt_no={nxt}", now))
        return {"lipsync_job_id": job_id, "state": "PREPARING",
                "business_attempt_no": nxt}

    def reconcile_unknown(self, tenant_id: str, job_id: str,
                          provider_outcome: dict | None) -> dict:
        """对账。**`provider_outcome is None` = 还查不到 → 什么都不做。**

        这个函数里**没有任何**提交调用（E16）。`accepted is True` 只表示
        "S26 收过单"，不表示结果可用：进 `INGESTING` 之后 `_finish_ingest`
        会重读输入对，变了就标 stale（T32-36）。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "WAITING_UNKNOWN":
            raise ServiceError(409, "JOB_NOT_UNKNOWN", f"job is {job['state']}",
                               details={"state": job["state"]})
        if provider_outcome is None:
            return {"lipsync_job_id": job_id, "state": "WAITING_UNKNOWN",
                    "reconciled": False, "resubmitted": False,
                    "note": "S26 still has no answer; freezing costs nothing "
                            "and double-charging costs a lot"}
        self.conn.execute(
            "UPDATE lipsync_job SET reconciled=1 WHERE lipsync_job_id=?",
            (job_id,))
        job = self._job(tenant_id, job_id)
        if provider_outcome.get("accepted") is True:
            job = self._transition(job, "INGESTING", reason_code="RECONCILED",
                                   outbox=self.outbox)
            return {"lipsync_job_id": job_id, "state": "INGESTING",
                    "reconciled": True, "resubmitted": False}
        job = self._transition(job, "RECONCILED_FAILED",
                               reason_code=provider_outcome.get("reason_code")
                               or "NOT_ACCEPTED",
                               detail="S26 confirms the command was not "
                                      "accepted; no charge stands",
                               outbox=self.outbox)
        return {"lipsync_job_id": job_id, "state": "RECONCILED_FAILED",
                "reconciled": True, "resubmitted": False}

    # -------------------------------------------------------- 巡检
    def sweep_deadlines(self, tenant_id: str | None = None) -> dict:
        """`WAITING_UNKNOWN` 熬到死线 → `FAILED(DEADLINE)`。

        **没有提交调用，也不应该有。** 结果不明的付费提交既不能重提（可能
        已收单）也不能永远挂着。死线是唯一"什么都不用知道就能结束"的出口。
        """
        sql = "SELECT * FROM lipsync_job WHERE state='WAITING_UNKNOWN'"
        args: list = []
        if tenant_id:
            sql += " AND tenant_id=?"
            args.append(tenant_id)
        failed: list[str] = []
        for raw in self.conn.execute(sql, args).fetchall():
            job = dict(raw)
            if not _deadline_passed(job["deadline"]):
                continue
            self._transition(
                job, "FAILED", reason_code="DEADLINE",
                detail=f"deadline {job['deadline']} passed while the "
                       f"submission outcome was still unknown; not "
                       f"resubmitted (E16)", outbox=self.outbox)
            failed.append(job["lipsync_job_id"])
        return {"expired": failed, "resubmitted": False,
                "state_for_expired": "FAILED", "reason_code": "DEADLINE"}

    # -------------------------------------------------------- 重启恢复
    def recover(self, tenant_id: str | None = None) -> dict:
        """§7.3 的重启矩阵。**按状态分派，没有"统一重试"。**

        - `CHECKING_INPUTS`：**重做全部检查**（T32-45）。S37 可能新增了缺陷、
          S31 可能已撤销——崩溃前的内存结论一律作废。
        - `PREPARING`：不自动继续。报价可能已过期，重新取路由需要有事务
          语义的上下文，由调用方重放。
        - `SUBMITTED` / `WAITING_PROVIDER`：只读，等 S26 事件。
        - `INGESTING`：**重新测量**（T32-46）——内存里的差异值在崩溃时已经
          没了，沿用它就是伪造证据。这里只报告"需要重算"，真正的重算发生
          在 `artifact.ingested` 重投时（`_finish_ingest` 每次都调
          `diff_measurer`，从不读缓存）。
        - `WAITING_UNKNOWN`：什么都不做（E16）。
        - `INPUTS_INELIGIBLE` / 其他终态：什么都不做。
        """
        sql = "SELECT * FROM lipsync_job WHERE 1=1"
        args: list = []
        if tenant_id:
            sql += " AND tenant_id=?"
            args.append(tenant_id)
        actions = {"rechecked": [], "waiting": [], "noop": [],
                   "needs_reprepare": [], "needs_remeasure": [],
                   "unhandled": []}
        for raw in self.conn.execute(sql, args).fetchall():
            job = dict(raw)
            state = job["state"]
            if state == "CHECKING_INPUTS":
                # 真重做，不是打个标记等人来。重做**只可能**落到两个终态
                # （`INPUTS_INELIGIBLE`）或交给调用方重放（`needs_reprepare`），
                # 因为它拿不到 `v` 与窗口：那些只存在于崩溃前的内存里。
                actions["rechecked"].append(
                    self._recheck_after_crash(job))
            elif state == "PREPARING":
                actions["needs_reprepare"].append(job["lipsync_job_id"])
            elif state in ("SUBMITTED", "WAITING_PROVIDER"):
                actions["waiting"].append(job["lipsync_job_id"])
            elif state == "INGESTING":
                actions["needs_remeasure"].append(job["lipsync_job_id"])
            elif state in TERMINAL_STATES or state == "WAITING_UNKNOWN":
                actions["noop"].append(job["lipsync_job_id"])
            else:
                actions["unhandled"].append(job["lipsync_job_id"])
        return {"actions": actions, "resubmitted": False}

    def _recheck_after_crash(self, job: dict) -> dict:
        """`CHECKING_INPUTS` 的崩溃恢复（T32-45）。

        用**库里存下的引用**重问一次 S37 / S31 / S28 / S10，再走
        `_eligibility_block`。命中上游拒绝就落到 `INPUTS_INELIGIBLE`；
        没命中就交回去——重新提交要求重新取路由与报价，那是调用方的事。

        **绝不调用 `submitter`**：崩溃前是否已经花过钱不确定（可能正卡在
        `_submit` 的第二次资格读取与 `submitter` 之间），再发一次就是重复付费。
        """
        tenant = job["tenant_id"]
        v = {
            "video_ref": _loads(job["video_ref"], {}),
            "audio_ref": _loads(job["audio_ref"], {}),
            "audio_take_id": job["audio_take_id"],
            "speaker_character_id": job["speaker_character_id"],
            "face": {},
        }
        binding = _row(self.conn.execute(
            "SELECT face_track_ref FROM face_track_binding "
            "WHERE face_track_binding_id=?",
            (job["face_track_binding_id"],)).fetchone()) or {}
        v["face"] = {"ref": _loads(binding.get("face_track_ref"), {})}
        try:
            with Transaction(self.conn):
                facts = self._read_eligibility(tenant, v)
                blocked = self._eligibility_block(job, v, facts)
        except ServiceError as exc:
            if exc.status == 404:
                # 引用在崩溃之后消失了：就是"上游说它不可用"，同一个终态。
                with Transaction(self.conn):
                    blocked = self._inputs_ineligible(
                        job, "INPUT_REF_NOT_FOUND",
                        detail=str(exc),
                        extra={"retryable_via": "S39"})
            else:
                # 复查不可达 ≠ 未通过。留在原地，下一轮再试。
                return {"lipsync_job_id": job["lipsync_job_id"],
                        "outcome": "waiting", "reason_code": exc.code,
                        "resubmitted": False}
        except ConnectionError as exc:
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "outcome": "waiting",
                    "reason_code": "INPUT_ELIGIBILITY_UNAVAILABLE",
                    "cause": type(exc).__name__, "resubmitted": False}
        if blocked is None:
            return {"lipsync_job_id": job["lipsync_job_id"],
                    "outcome": "needs_reprepare",
                    "note": "inputs still eligible; re-route and resubmit "
                            "(a new quote) rather than reusing the crashed one",
                    "resubmitted": False}
        status, payload = blocked
        return {"lipsync_job_id": job["lipsync_job_id"],
                "outcome": "blocked", "status": status,
                "reason_code": payload.get("code"),
                "resubmitted": False}
