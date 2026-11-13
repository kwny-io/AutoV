"""S48 内核。方案：`plans/S48-adaptation-training.md`。

一句话边界：**S48 是"谁在用哪些已授权的数据、把哪个钉住的基座训成了什么"
的权威；它不知道也不能知道"这些素材现在归谁、这个 LoRA 现在能不能挂载"。**

五条结构性事实，代码里每一处判断都从它们推出来：

1. **训练产物不是新的权威，训练也不能绕过任何一条素材授权**（§1.3）。
   每一项数据都有 `rights_ref`，复核失败就排除；权威不可达就整个作业
   REJECTED（fail closed），不是跳过。
2. **基座必须钉住 revision + content_hash**（§2.1）。"用最新的"会让训练
   完成后无法复现，也无法判断兼容性。
3. **`base_model_compatibility` 是训练时刻的记录，不是当前兼容性**（§1.5）。
   读接口必须告诉调用方"当前问 S24"。
4. **`EVAL_FAILED` ≠ `FAILED`**（§3.1）。前者是"训出来了但不好"，后者是
   "没训出来"；合并会让排查无从下手。
5. **撤销不中断在训的任务，但产物永远不能发布**（§6.2 / §3.8）。
   `BLOCKED` 不能复活——即使后来重新授权，也要重训。

钱在 S23、permit 在 S22、字节在 S28、授权在 S11–S15、当前兼容性在 S24、
在线加载在 S25/S26。本服务对这些只有引用与历史快照，没有写路径。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Inbox, Outbox, new_event
from idempotency_sdk import canonical_request_hash

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA

SOURCE = "S48"
SCHEMA_VERSION = "2.0"
TOPIC = "drama.adaptation-training.v2"
EVENT_COMPLETED = "adaptation.training.completed"

# §3.3：剩余项比例低于它就不该硬训。
DEFAULT_MIN_RATIO = 0.9
DEFAULT_MIN_ITEMS = 1
# 单作业数据集上限。方案没给数（§4.5 只给了码），这里按部署配置留默认值。
DEFAULT_MAX_ITEMS = 5000
# §6.3 / C48-22：每租户活跃作业上限（拿过 permit 之前的都算"占位"）。
DEFAULT_TENANT_ACTIVE_CAP = 2
# §6.3 的公平性：队列满时的重试提示。
QUEUE_RETRY_AFTER_SECONDS = 60
# §7.2：ADMITTED 超过 24h 仍未准入。
ADMISSION_TTL = timedelta(hours=24)
# §6.4：心跳超时后置为可续。
HEARTBEAT_TIMEOUT = timedelta(minutes=10)
# §6.5：评测基础设施失败最多重试 1 次。
MAX_EVAL_ATTEMPTS = 2
# §7.3：checkpoint 写失败重试 3 次。
MAX_CHECKPOINT_RETRIES = 3

ADAPTATION_TYPES = ("character_lora", "style_lora", "voice_clone")
DATASET_MODES = ("explicit", "derived_from_run", "derived_from_artifact_set")
# §3.1 的 14 个状态。
STATES = ("REQUESTED", "REJECTED", "ADMITTED", "QUEUED", "PREPARING",
          "TRAINING", "RESUMING", "EVALUATING", "EVAL_FAILED", "CANDIDATE",
          "PUBLISHED", "BLOCKED", "FAILED", "EXPIRED")
TERMINAL_STATES = frozenset({"REJECTED", "EVAL_FAILED", "PUBLISHED", "BLOCKED",
                             "FAILED", "EXPIRED"})
ACTIVE_STATES = frozenset({"QUEUED", "PREPARING", "TRAINING", "RESUMING",
                           "EVALUATING"})
# §3.1 的可取消状态。C48-12 说清每个状态取消后去哪。
CANCELLABLE = frozenset({"REQUESTED", "ADMITTED", "QUEUED", "PREPARING",
                         "TRAINING", "RESUMING"})

# 状态机的唯一边集合（§3.1）。`:accept` / `:reject` / janitor / 撤销事件
# 之外的每一条迁移都在这里，白名单外的边一律 409。
TRANSITIONS = {
    "REQUESTED": frozenset({"REJECTED", "ADMITTED"}),
    "ADMITTED": frozenset({"REJECTED", "QUEUED", "EXPIRED"}),
    "QUEUED": frozenset({"PREPARING", "REJECTED", "FAILED"}),
    # PREPARING → ADMITTED 只为 C48-21 的「permit 过期退回等 permit」；
    # 不是普通回退路径，进入 TRAINING 前仍要再验一次 permit。
    "PREPARING": frozenset({"TRAINING", "ADMITTED", "REJECTED", "FAILED"}),
    # TRAINING 没有通往 BLOCKED 的边：§6.2 说训练**不中断**，撤销只挂账。
    "TRAINING": frozenset({"RESUMING", "EVALUATING", "FAILED", "CANDIDATE"}),
    "RESUMING": frozenset({"TRAINING", "FAILED", "EVALUATING"}),
    # EVALUATING → BLOCKED 是 §6.2 的落点：训练中授权被撤销时不中断，
    # 训完直接 BLOCKED，既不发布也不让调用方等下一个人来按。
    "EVALUATING": frozenset({"CANDIDATE", "EVAL_FAILED", "FAILED", "BLOCKED"}),
    "CANDIDATE": frozenset({"PUBLISHED", "EVAL_FAILED", "BLOCKED", "FAILED"}),
    # 已发布后仍可能因撤销/基座退役被 BLOCKED（§3.8 / §7.3）。这是唯一
    # 允许离开 PUBLISHED 的边，且只通向终态 BLOCKED。
    "PUBLISHED": frozenset({"BLOCKED"}),
    # 终态没有出边。BLOCKED / PUBLISHED / REJECTED / FAILED / EXPIRED /
    # EVAL_FAILED 都不在表里 = 不能离开。
}

# §3.6 的训练侧指标。`voice_clone` 多两项。
BASE_METRICS = ("identity_preservation", "overfit_score", "holdout_loss")
VOICE_METRICS = ("voice_similarity", "intelligibility")
# §12.5 / C48-14：阈值是部署配置，不是业务事实。方向说明：
# identity_preservation / voice_similarity / intelligibility 越高越好；
# overfit_score / holdout_loss 越低越好。
DEFAULT_THRESHOLDS = {
    "identity_preservation": {"min": 0.85},
    "overfit_score": {"max": 0.20},
    "holdout_loss": {"max": 0.45},
    "voice_similarity": {"min": 0.80},
    "intelligibility": {"min": 0.90},
}

# §3.3 的排除理由 → 快照状态。授权权威给什么就照什么记，不重命名。
EXCLUSION_REASONS = ("REVOKED", "SUSPENDED", "EXPIRED", "NO_TRAINING_GRANT",
                     "HASH_MISMATCH", "MISSING", "EXPLICITLY_EXCLUDED")
# §1.4 / T48-02：`revoked` 是法律状态，`suspended` 是技术下架。**只有前者
# 让素材出局**——技术下架说的是"这个供应商暂时不下发"，素材本身仍然合法。
# 两者都记进快照，审计要能分开；但排除理由只有法律状态才成立。
SNAPSHOT_STATE_TO_REASON = {
    "revoked": "REVOKED",
    "expired": "EXPIRED",
}
# 可以入册的状态：active 正常，suspended 仍合法（T48-02）。
TRAINABLE_RIGHTS_STATES = frozenset({"active", "suspended"})

# 幂等 hash 里剥掉的易变上下文——同一个逻辑命令换个 trace 仍是同一次重放。
VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp", "requested_at",
                         "issued_at", "server_time")

# 这些键出现在响应里也算泄漏训练内容；事件里绝对不发。
NEVER_IN_EVENT = frozenset({"hyperparams", "dataset_items", "weights_url",
                            "prompt", "checkpoint_ref"})


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def bare_hash(value) -> str | None:
    """`sha256:ab…` 与 `ab…` 统一成裸 64 hex。两种上游写法都存在。"""
    if not isinstance(value, str):
        return None
    digest = value.split(":", 1)[1] if value.startswith("sha256:") else value
    digest = digest.strip().lower()
    return digest if len(digest) == 64 else None


def stable_request_hash(body: dict) -> str:
    """§6.1：`sha256(JCS(body 去掉 command_id 与 request_hash))`。

    SDK 的 `canonical_request_hash` 只剥那两个键。方案（与 S41 §7.1 同一纪律）
    要求 trace 与时间戳也不参与，所以先剥 context 里的易变键再交给 SDK。
    """
    if not isinstance(body, dict):
        return canonical_request_hash({})
    scrubbed = dict(body)
    context = scrubbed.get("context")
    if isinstance(context, dict):
        scrubbed["context"] = {k: v for k, v in context.items()
                               if k not in VOLATILE_CONTEXT_KEYS}
    return canonical_request_hash(scrubbed)


def items_hash(items: list[dict]) -> str:
    """§3.4：`sha256(JCS(sorted (artifact_id, content_hash)))`。

    排序是必须的：同一批数据换个提交顺序不该改变"这是哪一批"。
    只取两个字段——artifact service 与 ordinal 都不进摘要，前者由
    `dataset_item.artifact_service` 约束为 S28，后者是查询便利字段。
    """
    projection = sorted(
        [{"artifact_id": str(item["artifact_id"]),
          "content_hash": str(item["content_hash"])} for item in items],
        key=lambda pair: (pair["artifact_id"], pair["content_hash"]))
    return _sha256_hex(_jcs(projection))


def holdout_items_hash(refs: list[dict]) -> str:
    """holdout 也要有摘要，否则"用哪一版做评测"无法复现。"""
    projection = sorted(
        [{"service_id": str(ref.get("service_id") or ""),
          "id": str(ref.get("id") or ""),
          "content_hash": str(bare_hash(ref.get("content_hash")) or "")}
         for ref in refs if isinstance(ref, dict)],
        key=lambda pair: (pair["service_id"], pair["id"],
                          pair["content_hash"]))
    return _sha256_hex(_jcs(projection))


def evaluate_metrics(adaptation_type: str, metrics: dict,
                     thresholds: dict | None = None) -> tuple[bool, list[str]]:
    """§3.6：逐指标比阈值。**缺指标不是"通过"**——记 failures。

    返回 `(passed, failed_metric_names)`。方向由阈值键决定（min / max），
    没有隐式的"越大越好"：`overfit_score` 越大越差，靠 max 表达。
    """
    limits = dict(DEFAULT_THRESHOLDS)
    for name, bound in (thresholds or {}).items():
        if isinstance(bound, dict):
            limits[name] = bound
    required = list(BASE_METRICS)
    if adaptation_type == "voice_clone":
        required.extend(VOICE_METRICS)
    failures = []
    for name in required:
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            failures.append(name)
            continue
        bound = limits.get(name) or {}
        if "min" in bound and float(value) < float(bound["min"]):
            failures.append(name)
        elif "max" in bound and float(value) > float(bound["max"]):
            failures.append(name)
    return (not failures), failures


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


def _require_object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{name} must be an object")
    return value


def _require_text(value, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and value == ""):
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{name} must be a non-empty string")
    return value


def _require_int(value, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{name} must be an integer")
    if value < minimum:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{name} must be >= {minimum}")
    return value


def _require_hash(value, name: str) -> str:
    digest = bare_hash(value)
    if digest is None:
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"{name} must be a sha256 hex digest")
    return digest


def _require_list(value, name: str) -> list:
    if not isinstance(value, list):
        raise ServiceError(400, "MALFORMED_REQUEST", f"{name} must be an array")
    return value


class AdaptationTrainingService:
    def __init__(self, conn: sqlite3.Connection, *, rights_source,
                 base_model_source, budget_source, permit_source,
                 artifact_source, registry_sink=None, metrics=None,
                 tenant_active_cap: int = DEFAULT_TENANT_ACTIVE_CAP,
                 max_items: int = DEFAULT_MAX_ITEMS,
                 min_ratio: float = DEFAULT_MIN_RATIO,
                 min_items: int = DEFAULT_MIN_ITEMS,
                 training_enabled=True, home_cell: str = "cell_a1"):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        # Inbox 同理：撤销是至少一次投递，去重必须在事务里做。
        self.inbox = Inbox(self.conn)

        # 五个上游。**全部必需**：任何一个缺了，本服务不是"降级"而是
        # "无法回答自己的问题"。未配置的 builder 会抛 503，不是返回空。
        self.rights_source = rights_source        # S11/S12/S13/S14/S15
        self.base_model_source = base_model_source  # S24
        self.budget_source = budget_source        # S23
        self.permit_source = permit_source        # S22
        self.artifact_source = artifact_source    # S28
        # 注册 LoRA 权重到 S28：可选（C48-15 之前 CANDIDATE 不需要）。
        # 没配置时 `:accept` 保持 CANDIDATE 并说明原因，绝不伪造 published。
        self.registry_sink = registry_sink
        # 指标只在本进程内累加，供 readiness 与测试断言（§8.1 的形态）。
        self._metrics: dict[str, int] = metrics if metrics is not None else {}
        self.tenant_active_cap = tenant_active_cap
        self.max_items = max_items
        self.min_ratio = min_ratio
        self.min_items = min_items
        # §4.5：客户未订购 S48 → 403 TRAINING_NOT_ENABLED。默认关（docs/16 §3）。
        self.training_enabled = training_enabled
        self.home_cell = home_cell

    # ------------------------------------------------------------ 基础设施
    @property
    def metrics(self) -> dict:
        return dict(self._metrics)

    def _bump(self, name: str, amount: int = 1) -> None:
        self._metrics[name] = self._metrics.get(name, 0) + amount

    def _upstream(self, call, code: str = "DEPENDENCY_UNAVAILABLE"):
        """上游不可达 → 503。**绝不拿缓存结论兜底**（§1.4、§10.1）。

        这里刻意不把 5xx 一律变成"没有数据"：授权复核的 fail closed 需要
        区分"权威说不授权"（422）与"权威说不出来"（503/REJECTED），
        后者在复核路径上由调用方转成 REJECTED。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    def _audit(self, tenant_id: str, action: str, *, job_id=None,
               actor=None, details=None) -> None:
        """§8.3 的本地留痕（C48-16）。

        **不写进 details**：超参全文、数据集内容、权重 URL。
        调用方传入的 details 已由各处收敛到引用与计数。
        """
        self.conn.execute(
            "INSERT INTO training_audit (tenant_id, audit_id, job_id, action,"
            " actor, details, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, _new_id("audit"), job_id, action, actor,
             _jcs(details or {}), _now()))

    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    # ------------------------------------------------------------ 读行
    def _job(self, tenant_id: str, job_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM training_job WHERE tenant_id=? AND job_id=?",
            (tenant_id, job_id)).fetchone())
        if row is None:
            # 跨租户与不存在同形（E28）：确认"存在但你没权限"本身就是泄漏。
            raise ServiceError(404, "NOT_FOUND", f"no training job {job_id}")
        return row

    def _manifest(self, tenant_id: str, dataset_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM dataset_manifest WHERE tenant_id=? AND dataset_id=?",
            (tenant_id, dataset_id)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND", f"no dataset {dataset_id}")
        return row

    def _artifact(self, tenant_id: str, artifact_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM lora_artifact WHERE tenant_id=? AND artifact_id=?",
            (tenant_id, artifact_id)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no adaptation artifact {artifact_id}")
        return row

    # ------------------------------------------------------------ 状态机
    def _transition(self, job: dict, to_state: str, *, reason=None,
                    sets: dict | None = None, correlation_id: str = "") -> dict:
        """状态机唯一入口。白名单外的边 409，同态是空操作。

        `sets` 与状态在同一条 UPDATE 里写：否则会短暂出现"状态已变、
        引用还没写"的窗口，崩溃恢复时那条窗口无法解释。
        迁移与 `aggregate_sequence + 1` 同事务；事件由调用方在本事务内追加。
        """
        from_state = job["state"]
        if from_state == to_state:
            return job
        allowed = TRANSITIONS.get(from_state, frozenset())
        if to_state not in allowed:
            raise ServiceError(
                409, "JOB_STATE_CONFLICT",
                f"{from_state} cannot go to {to_state}",
                details={"from": from_state, "to": to_state})
        now = _now()
        assignments = ["state = ?", "state_since = ?",
                       "aggregate_sequence = aggregate_sequence + 1"]
        args: list = [to_state, now]
        for column, value in (sets or {}).items():
            assignments.append(f"{column} = ?")
            args.append(value)
        args.extend([job["tenant_id"], job["job_id"], from_state])
        cur = self.conn.execute(
            "UPDATE training_job SET " + ", ".join(assignments)
            + " WHERE tenant_id = ? AND job_id = ? AND state = ?", args)
        if cur.rowcount != 1:
            # 并发：别人已经推过了。不覆盖，让调用方按自己的语义处理。
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               "job state changed concurrently", retryable=True,
                               details={"expected": from_state})
        updated = self._job(job["tenant_id"], job["job_id"])
        if reason is not None:
            self.conn.execute(
                "UPDATE training_job SET rejection=? "
                "WHERE tenant_id=? AND job_id=?",
                (_jcs(reason), job["tenant_id"], job["job_id"]))
            updated = self._job(job["tenant_id"], job["job_id"])
        self._audit(
            job["tenant_id"], f"training.job.{to_state.lower()}",
            job_id=job["job_id"], actor="S48",
            details={"from": from_state, "to": to_state,
                     "reason": (reason or {}).get("reason")
                     if isinstance(reason, dict) else None,
                     "aggregate_sequence": int(updated["aggregate_sequence"])})
        return updated

    # ------------------------------------------------------------ 建单
    def create_job(self, tenant_id: str, body: dict, command_id: str,
                   request_hash: str, *, requested_by=None,
                   home_region: str | None = None) -> dict:
        """§2.1 / §3.1：受理 → 复核 → 冻结数据集 → 准入。

        REQUESTED 是**先落一行**再复核：复核失败也要留下"提过、拒过、
        为什么"的证据（E39）。所以拒绝不是"不建单"，是 `state=REJECTED`。
        """
        if not self.training_enabled:
            # 默认关（docs/16 §3）。不是 404——服务在，只是这个客户没订购。
            raise ServiceError(403, "TRAINING_NOT_ENABLED",
                               "adaptation training is not enabled for this tenant")
        spec = self._validate_create_body(body)
        self._check_queue_cap(tenant_id)

        job_id = _new_id("job")
        now = _now()
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO training_job (job_id, tenant_id, command_id,"
                " request_hash, adaptation_type, target_service, target_id,"
                " target_revision, target_content_hash, base_service, base_id,"
                " base_revision, base_content_hash, hyperparams, evaluation_spec,"
                " state, state_since, budget_auth_ref, requested_by, trace_id,"
                " home_region, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                "         'REQUESTED', ?, ?, ?, ?, ?, ?)",
                (job_id, tenant_id, command_id, request_hash,
                 spec["adaptation_type"], spec["target_ref"]["service_id"],
                 spec["target_ref"]["id"], spec["target_ref"]["revision"],
                 spec["target_ref"]["content_hash"],
                 spec["base_model"]["service_id"], spec["base_model"]["id"],
                 spec["base_model"]["revision"],
                 spec["base_model"]["content_hash"],
                 _jcs(spec["hyperparams"]), _jcs(spec["evaluation"]),
                 now, _jcs(spec["budget_authorization_ref"]),
                 _jcs(requested_by or {"actor": "unknown"}), spec["trace_id"],
                 home_region or spec["home_region"] or "unspecified", now))
            job = self._job(tenant_id, job_id)
            self._audit(tenant_id, "training.job.create", job_id=job_id,
                        actor=_actor_id(requested_by),
                        details={"adaptation_type": spec["adaptation_type"],
                                 "target_ref": spec["target_ref"],
                                 "base_ref": spec["base_model"],
                                 "budget_auth_ref":
                                     spec["budget_authorization_ref"],
                                 "trace_id": spec["trace_id"]})
            reviewed = self._review_and_freeze(tenant_id, job, spec,
                                               correlation_id=command_id)
            return self._job_response(tenant_id, reviewed["job_id"],
                                      include_items=True)

    def _check_queue_cap(self, tenant_id: str) -> None:
        """§6.3 公平性：每租户活跃作业上限。只压本租户，不阻塞他人（T48-27）。

        数的是"还没结束的"（REQUESTED…EVALUATING）。PUBLISHED/BLOCKED/
        EVAL_FAILED/REJECTED/FAILED/EXPIRED 不占位。
        """
        active = ["REQUESTED", "ADMITTED", "QUEUED", "PREPARING", "TRAINING",
                  "RESUMING", "EVALUATING"]
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM training_job WHERE tenant_id=?"
            " AND state IN (" + ",".join("?" * len(active)) + ")",
            (tenant_id, *active)).fetchone()
        if int(row["n"]) >= self.tenant_active_cap:
            self._bump("s48_queue_rejections_total")
            raise ServiceError(
                429, "TRAINING_QUEUE_FULL",
                f"tenant already has {row['n']} active training job(s)",
                retryable=True,
                details={"active_jobs": int(row["n"]),
                         "cap": self.tenant_active_cap,
                         "retry_after_seconds": QUEUE_RETRY_AFTER_SECONDS})

    def _validate_create_body(self, body: dict) -> dict:
        """§2.1 的三条硬规则：钉住基座、枚举数据集模式、必填预算授权。"""
        body = _require_object(body, "body")
        adaptation_type = _require_text(body.get("adaptation_type"),
                                        "adaptation_type")
        if adaptation_type not in ADAPTATION_TYPES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"adaptation_type must be one of "
                               f"{list(ADAPTATION_TYPES)}")

        target = _require_object(body.get("target_ref"), "target_ref")
        target_service = _require_text(target.get("service_id"),
                                       "target_ref.service_id")
        _require_text(target.get("id"), "target_ref.id")
        _require_int(target.get("revision"), "target_ref.revision", minimum=1)
        _require_hash(target.get("content_hash"),
                      "target_ref.content_hash")

        base = _require_object(body.get("base_model"), "base_model")
        base_service = _require_text(base.get("service_id") or "S24",
                                     "base_model.service_id")
        # §2.1：不接受"最新"。缺 revision 或 hash 是 400，不是"取默认"。
        if base.get("revision") is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "base_model.revision is required: this service "
                               "never trains against 'latest'")
        if base.get("content_hash") is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "base_model.content_hash is required: a base "
                               "without a pinned hash cannot be reproduced")
        _require_text(base.get("id"), "base_model.id")
        _require_int(base.get("revision"), "base_model.revision", minimum=1)
        _require_hash(base.get("content_hash"), "base_model.content_hash")

        dataset = _require_object(body.get("dataset"), "dataset")
        mode = _require_text(dataset.get("mode"), "dataset.mode")
        if mode not in DATASET_MODES:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"dataset.mode must be one of {list(DATASET_MODES)}")
        refs = _require_list(dataset.get("artifact_refs") or [],
                             "dataset.artifact_refs")
        excluded = _require_list(dataset.get("exclude_artifact_refs") or [],
                                 "dataset.exclude_artifact_refs")
        for index, ref in enumerate(refs):
            _require_object(ref, f"dataset.artifact_refs[{index}]")
            _require_text(ref.get("id"), f"dataset.artifact_refs[{index}].id")
            _require_hash(ref.get("content_hash"),
                          f"dataset.artifact_refs[{index}].content_hash")
        for index, ref in enumerate(excluded):
            _require_object(ref, f"dataset.exclude_artifact_refs[{index}]")
            _require_text(ref.get("id"),
                          f"dataset.exclude_artifact_refs[{index}].id")
        if mode != "explicit" and not (dataset.get("source_ref")
                                       or isinstance(dataset.get("source"),
                                                     dict)):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "derived dataset modes require source_ref")

        hyperparams = body.get("hyperparams")
        if hyperparams is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "hyperparams is required")
        hyperparams = _require_object(hyperparams, "hyperparams")
        _require_text(hyperparams.get("profile"), "hyperparams.profile")
        if hyperparams.get("steps") is not None:
            _require_int(hyperparams.get("steps"), "hyperparams.steps",
                         minimum=1)
        if hyperparams.get("lora_rank") is not None:
            _require_int(hyperparams.get("lora_rank"), "hyperparams.lora_rank",
                         minimum=1)

        evaluation = body.get("evaluation")
        if evaluation is None:
            raise ServiceError(400, "MALFORMED_REQUEST", "evaluation is required")
        evaluation = _require_object(evaluation, "evaluation")
        holdouts = _require_list(evaluation.get("holdout_refs") or [],
                                 "evaluation.holdout_refs")
        for index, ref in enumerate(holdouts):
            _require_object(ref, f"evaluation.holdout_refs[{index}]")
            _require_text(ref.get("id"), f"evaluation.holdout_refs[{index}].id")
        thresholds = evaluation.get("thresholds")
        if thresholds is not None and not isinstance(thresholds, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "evaluation.thresholds must be an object")

        budget_ref = body.get("budget_authorization_ref")
        if budget_ref is None:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "budget_authorization_ref is required")
        if not isinstance(budget_ref, (dict, str)):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "budget_authorization_ref must be an object or id")

        context = body.get("context") if isinstance(body.get("context"),
                                                   dict) else {}
        return {
            "adaptation_type": adaptation_type,
            "target_ref": {"service_id": target_service,
                           "id": target.get("id"),
                           "revision": int(target["revision"]),
                           "content_hash": bare_hash(target["content_hash"])},
            "base_model": {"service_id": base_service,
                           "id": base.get("id"),
                           "revision": int(base["revision"]),
                           "content_hash": bare_hash(base["content_hash"])},
            "dataset": {"mode": mode,
                        "artifact_refs": refs,
                        "exclude_artifact_refs": excluded,
                        "source_ref": dataset.get("source_ref")
                        or dataset.get("source")},
            "hyperparams": hyperparams,
            "evaluation": evaluation,
            "budget_authorization_ref": budget_ref,
            "trace_id": context.get("trace_id"),
            "home_region": context.get("home_region"),
        }

    # ------------------------------------------------------------ 复核
    def _review_and_freeze(self, tenant_id: str, job: dict, spec: dict, *,
                           correlation_id: str) -> dict:
        """§3.3 / §3.4：解析 → 逐项复核 → 交集检查 → 冻结。

        返回更新后的 job 行（可能已 REJECTED）。拒绝是状态 + rejection，
        不是异常：调用方拿到的是 201 与 `state=REJECTED`，理由在
        `rejection` 里。这样"提过、被拒、为什么"留在同一个 job_id 下（E39）。
        """
        try:
            resolved = self._resolve_dataset(tenant_id, spec["dataset"])
        except ServiceError as exc:
            # 解析阶段也可能撞上"权威连不上"（推导模式调 S28），同样整单拒。
            if exc.code == "RIGHTS_UNVERIFIABLE":
                return self._reject(tenant_id, job, "RIGHTS_UNVERIFIABLE",
                                    str(exc), correlation_id,
                                    details=exc.details)
            raise
        try:
            reviewed = self._review_rights(tenant_id, job, resolved)
        except ServiceError as exc:
            if exc.code == "RIGHTS_UNVERIFIABLE":
                # §7.1：权威不可达 → 整个作业 REJECTED（fail closed）。
                # 这里**不**向外抛 422：job 行必须留在同一个 job_id 下
                # （E39），而抛异常会让外层事务回滚、幂等占位被 abort，
                # 重试时撞 `tj_cmd_uniq`。同表其它拒绝理由
                # （BASE_MODEL_INCOMPATIBLE / HOLDOUT_OVERLAP / ...）也都是
                # 201 + state=REJECTED，这里保持一致。
                return self._reject(tenant_id, job, "RIGHTS_UNVERIFIABLE",
                                    str(exc), correlation_id,
                                    details=exc.details)
            raise

        items = reviewed["items"]
        exclusions = reviewed.get("exclusions") or []
        if not items:
            return self._reject(tenant_id, job, "EMPTY_DATASET_AFTER_FILTER",
                                "every dataset item was excluded by rights review",
                                correlation_id,
                                details={"remaining": 0,
                                         "original": len(resolved),
                                         "min_ratio": self.min_ratio})
        if len(items) > self.max_items:
            return self._reject(tenant_id, job, "DATASET_TOO_LARGE",
                                f"{len(items)} items exceeds {self.max_items}",
                                correlation_id)
        # `min_ratio` 的分母是"**提交者本来打算训的**项"，不含调用方自己
        # 点名排除的（C48-18）。把主动排除算成丢失会让"用户删掉一条"变成
        # "数据集质量下降"，那是两回事（T48-15b）。
        requested = [item for item in resolved
                     if not item.get("explicitly_excluded")]
        ratio = len(items) / max(1, len(requested))
        if (len(items) < self.min_items or ratio < self.min_ratio):
            return self._reject(
                tenant_id, job, "EMPTY_DATASET_AFTER_FILTER",
                f"{len(items)}/{len(requested)} items remain "
                f"({ratio:.3f} < min_ratio {self.min_ratio})",
                correlation_id,
                details={"remaining": len(items), "original": len(requested),
                         "min_ratio": self.min_ratio})

        # §3.6：holdout 与训练集不得有交集。用 artifact id 判，因为那是
        # 两侧唯一共同的稳定标识。
        overlap = self._holdout_overlap(spec["evaluation"], items)
        if overlap:
            return self._reject(tenant_id, job, "HOLDOUT_OVERLAP",
                                f"holdout shares {len(overlap)} item(s) with "
                                f"the training set", correlation_id,
                                details={"overlapping_ids": overlap[:20]})

        # §7.1：S24 在花 GPU 之前说不兼容就拒。
        compat = self._check_base(tenant_id, spec["base_model"])
        if compat == "NOT_FOUND":
            return self._reject(tenant_id, job, "BASE_MODEL_NOT_FOUND",
                                "S24 does not know this base revision",
                                correlation_id)
        if compat == "INCOMPATIBLE":
            return self._reject(tenant_id, job, "BASE_MODEL_INCOMPATIBLE",
                                "S24 reports this base revision incompatible "
                                "with training", correlation_id)

        frozen = self._freeze_dataset(tenant_id, job, spec, items,
                                      excluded_count=len(resolved) - len(items),
                                      exclusions=exclusions)
        self._bump("s48_dataset_frozen_total")
        # 复核通过 → ADMITTED（等 S22 permit）。§3.2 的顺序：先冻结，再准入。
        job = self._transition(frozen["job"], "ADMITTED",
                               sets={"dataset_id": frozen["dataset_id"],
                                     "admitted_at": _now()},
                               correlation_id=correlation_id)
        return self._admit(tenant_id, job, spec, correlation_id)

    def _reject(self, tenant_id: str, job: dict, code: str, message: str,
                correlation_id: str, details: dict | None = None) -> dict:
        """复核/准入失败 → REJECTED（终态）。理由进 `rejection` 与审计。"""
        reason = {"reason": code, "message": message,
                  "details": details or {}, "at": _now()}
        job = self._transition(job, "REJECTED", reason=reason,
                               sets={"finished_at": _now()},
                               correlation_id=correlation_id)
        self._bump("s48_jobs_total")
        self._audit(tenant_id, "training.job.reject", job_id=job["job_id"],
                    actor="S48", details={"reason": code})
        return job

    def _resolve_dataset(self, tenant_id: str, dataset: dict) -> list[dict]:
        """§2.2：显式取列表；推导模式调 S28 再与显式项合并、减去排除项。

        **推导结果必须可枚举**：返回完整列表，响应里也回给调用方确认。
        不"按描述推了 800 张就这么用"（§2.2 末段）。
        C48-18：求交/求差未裁决，这里用「并集 − 排除项」并登记。
        """
        explicit = [{"artifact_id": ref["id"],
                     "service_id": ref.get("service_id") or "S28",
                     "content_hash": bare_hash(ref.get("content_hash")),
                     "rights_ref": ref.get("rights_ref")}
                    for ref in dataset.get("artifact_refs") or []]
        mode = dataset["mode"]
        if mode == "explicit":
            merged = list(explicit)
        else:
            derived = self._upstream(
                lambda: self.artifact_source(tenant_id, mode,
                                             dataset.get("source_ref")))
            if not isinstance(derived, list):
                raise ServiceError(502, "UPSTREAM_INVALID",
                                   "artifact source returned a non-list for a "
                                   "derived dataset mode")
            merged = list(explicit)
            for item in derived:
                if not isinstance(item, dict):
                    continue
                digest = bare_hash(item.get("content_hash"))
                if item.get("artifact_id") is None and item.get("id") is None:
                    continue
                merged.append({
                    "artifact_id": item.get("artifact_id") or item.get("id"),
                    "service_id": item.get("service_id") or "S28",
                    "content_hash": digest,
                    "rights_ref": item.get("rights_ref"),
                })
        excluded_ids = {ref["id"] for ref in
                        dataset.get("exclude_artifact_refs") or []}
        seen = set()
        result = []
        for item in merged:
            key = item["artifact_id"]
            if key in seen:
                continue
            seen.add(key)
            if key in excluded_ids:
                item = {**item, "explicitly_excluded": True}
            result.append(item)
        return result

    def _review_rights(self, tenant_id: str, job: dict,
                       resolved: list[dict]) -> dict:
        """§3.3：逐项回查授权权威，排除不合法项，**fail closed**。

        三条必须坚持的点：
        1. 权威不可达 → 整个作业 REJECTED，不是"跳过这一项"；
        2. `grant_kind` 不含 training → 排除（C48-3：默认拒绝）；
        3. 被排除的项连同当时的 rights_ref 一起落 `dataset_exclusion`。
        """
        kept: list[dict] = []
        exclusions: list[dict] = []
        for item in resolved:
            if item.get("explicitly_excluded"):
                exclusions.append({"artifact_id": item["artifact_id"],
                                   "reason": "EXPLICITLY_EXCLUDED",
                                   "rights_ref": item.get("rights_ref")})
                continue
            if not item.get("content_hash"):
                exclusions.append({"artifact_id": item["artifact_id"],
                                   "reason": "MISSING",
                                   "rights_ref": item.get("rights_ref")})
                continue
            try:
                rights = self._upstream(
                    lambda item=item: self.rights_source(
                        tenant_id, item["service_id"], item["artifact_id"]))
            except ServiceError as exc:
                if exc.status >= 500 or exc.code == "DEPENDENCY_UNAVAILABLE":
                    # §3.3 第 1 条：不可达 → 整个作业拒绝。
                    self._bump("s48_rights_unverifiable_total")
                    raise ServiceError(
                        422, "RIGHTS_UNVERIFIABLE",
                        f"rights authority for {item['artifact_id']} is "
                        f"unreachable; refusing to train on a subset",
                        details={"artifact_id": item["artifact_id"],
                                 "service_id": item["service_id"]}) from exc
                if exc.status == 404:
                    exclusions.append({"artifact_id": item["artifact_id"],
                                       "reason": "MISSING",
                                       "rights_ref": item.get("rights_ref")})
                    continue
                raise
            if not isinstance(rights, dict):
                raise ServiceError(422, "RIGHTS_UNVERIFIABLE",
                                   "rights authority returned a non-object")
            snapshot = self._rights_snapshot(rights, item)
            state = (snapshot.get("state") or "").lower()
            if state not in TRAINABLE_RIGHTS_STATES:
                # active 之外的 revoked/expired/未知状态都不入册。
                # **suspended 是例外**：技术下架不等于法律撤销（§1.4 / T48-02）。
                reason = SNAPSHOT_STATE_TO_REASON.get(state, "MISSING")
                exclusions.append({"artifact_id": item["artifact_id"],
                                   "reason": reason,
                                   "rights_ref": snapshot})
                self._bump("s48_rights_excluded_total")
                continue
            grant_kind = snapshot.get("grant_kind")
            if not _grants_training(grant_kind):
                exclusions.append({"artifact_id": item["artifact_id"],
                                   "reason": "NO_TRAINING_GRANT",
                                   "rights_ref": snapshot})
                self._bump("s48_rights_excluded_total")
                continue
            # 复核通过：用权威给的 hash 与命令给的 hash 对一次。不一致说明
            # 提交之后字节变了——这是 §7.1 的 HASH_MISMATCH。
            authority_hash = bare_hash(rights.get("content_hash"))
            if authority_hash is not None and \
                    authority_hash != item["content_hash"]:
                exclusions.append({"artifact_id": item["artifact_id"],
                                   "reason": "HASH_MISMATCH",
                                   "rights_ref": snapshot})
                continue
            kept.append({**item, "rights": snapshot})
        kept.sort(key=lambda pair: pair["artifact_id"])
        return {"items": kept, "exclusions": exclusions}

    def _rights_snapshot(self, rights: dict, item: dict) -> dict:
        """§2.3 的 rights_ref 快照。缺字段就缺字段，不补默认值。

        `grant_kind` 尤其不能补：补成 `"generation"` 会让训练静默失效（C48-3），
        补成 `"training"` 等于伪造授权。缺了就是 `None`，由复核判排除。
        """
        supplied = item.get("rights_ref")
        base = supplied if isinstance(supplied, dict) else {}
        service_id = (rights.get("service_id") or base.get("service_id")
                      or rights.get("authority") or "unknown")
        identifier = (rights.get("id") or base.get("id")
                      or rights.get("rights_id") or item["artifact_id"])
        revision = rights.get("revision", base.get("revision"))
        content_hash = bare_hash(rights.get("content_hash")) \
            or bare_hash(base.get("content_hash"))
        grant_kind = rights.get("grant_kind", base.get("grant_kind"))
        return {
            "service_id": service_id,
            "id": identifier,
            "revision": int(revision) if isinstance(revision, int) else 0,
            "content_hash": content_hash or "",
            "grant_kind": grant_kind,
            "checked_at": _now(),
            "state": rights.get("state") or rights.get("status"),
        }

    @staticmethod
    def _holdout_overlap(evaluation: dict, items: list[dict]) -> list[str]:
        dataset_ids = {item["artifact_id"] for item in items}
        overlapping = []
        for ref in evaluation.get("holdout_refs") or []:
            for key in ("id", "artifact_id"):
                value = ref.get(key)
                if isinstance(value, str) and value in dataset_ids:
                    overlapping.append(value)
        return sorted(set(overlapping))

    def _check_base(self, tenant_id: str, base: dict) -> str:
        """§7.1：S24 说不知道/不兼容都在花 GPU 之前拒。"""
        found = self._upstream(
            lambda: self.base_model_source(tenant_id, base["id"],
                                           base["revision"],
                                           base["content_hash"]))
        if not isinstance(found, dict) or not found.get("found", True):
            self._bump("s48_base_incompat_rejections_total")
            return "NOT_FOUND"
        verdict = found.get("compatible")
        if verdict is False:
            self._bump("s48_base_incompat_rejections_total")
            return "INCOMPATIBLE"
        # `None` 是"没回答"，不是"兼容"。继续训练但把 UNKNOWN 记下来。
        return "COMPATIBLE" if verdict is True else "UNKNOWN"

    def _freeze_dataset(self, tenant_id: str, job: dict, spec: dict,
                        items: list[dict], *, excluded_count: int,
                        exclusions: list[dict] | None = None) -> dict:
        """§3.4：写 manifest(FROZEN) + 逐项 rights 快照。

        `items_hash` 由输入决定，确定性可复算（T48-12）。manifest 的
        `item_count` 是**入册**项数，`excluded_count` 是复核排除项数。

        排除项必须落 `dataset_exclusion`：§3.3 第 3 条要求"谁被排除、为什么、
        当时的状态"可查（E30/E39）。只在响应里答一次不算留痕。
        """
        dataset_id = _new_id("dataset")
        digest = items_hash(items)
        now = _now()
        self.conn.execute(
            "INSERT INTO dataset_manifest (dataset_id, tenant_id, job_id, mode,"
            " source_ref, items_hash, item_count, excluded_count, exclusions,"
            " state, frozen_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 'FROZEN', ?, ?)",
            (dataset_id, tenant_id, job["job_id"], spec["dataset"]["mode"],
             _jcs(spec["dataset"]["source_ref"])
             if spec["dataset"].get("source_ref") is not None else None,
             digest, len(items), excluded_count, now, now))
        for entry in exclusions or []:
            self.conn.execute(
                "INSERT INTO dataset_exclusion (tenant_id, dataset_id,"
                " artifact_id, reason, rights_ref) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, dataset_id, entry["artifact_id"],
                 entry["reason"],
                 _jcs(entry["rights_ref"])
                 if entry.get("rights_ref") is not None else None))
        # ordinal 从 1 开始：游标语义是 `ordinal > cursor`，从 0 起会让
        # 第一页的第一项永远查不回来（0 > 0 为假）。
        for ordinal, item in enumerate(items, start=1):
            rights = item["rights"]
            self.conn.execute(
                "INSERT INTO dataset_item (tenant_id, dataset_id, artifact_id,"
                " artifact_service, content_hash, rights_service, rights_id,"
                " rights_revision, rights_content_hash, rights_grant_kind,"
                " rights_state_at_check, rights_checked_at, ordinal)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, dataset_id, item["artifact_id"],
                 item["service_id"], item["content_hash"],
                 rights["service_id"], rights["id"], rights["revision"],
                 rights["content_hash"] or ("0" * 64),
                 _jcs(rights["grant_kind"]) if rights["grant_kind"]
                 is not None else "none",
                 str(rights["state"]), rights["checked_at"], ordinal))
        self._audit(tenant_id, "training.dataset.freeze", job_id=job["job_id"],
                    actor="S48",
                    details={"dataset_id": dataset_id, "items_hash": digest,
                             "item_count": len(items),
                             "excluded_count": excluded_count})
        return {"dataset_id": dataset_id, "items_hash": digest,
                "job": self._job(tenant_id, job["job_id"])}

    # ------------------------------------------------------------ 准入
    def _admit(self, tenant_id: str, job: dict, spec: dict,
               correlation_id: str) -> dict:
        """§3.2：S23 授权有效 → S22 permit → QUEUED。

        **S23 无效 → REJECTED**（BUDGET_AUTH_INVALID）；**S22 没有 permit →
        保持 ADMITTED**（配额可能稍后释放，不是业务错误）。
        """
        budget = self._upstream(
            lambda: self.budget_source(tenant_id, spec["budget_authorization_ref"]))
        if not isinstance(budget, dict) or budget.get("valid") is False:
            return self._reject(tenant_id, job, "BUDGET_AUTH_INVALID",
                                "S23 rejected the budget authorization",
                                correlation_id)
        permit = self._upstream(
            lambda: self.permit_source(tenant_id, "training", "gpu_second",
                                       job["job_id"], budget))
        if not isinstance(permit, dict) or permit.get("granted") is not True:
            # 不是拒绝：ADMITTED 是"等 permit"的合法停留状态（§7.2）。
            self._metrics["s48_admitted_waiting"] = \
                self._metrics.get("s48_admitted_waiting", 0) + 1
            return job
        return self._transition(
            job, "QUEUED",
            sets={"quota_permit_ref": _jcs(permit)},
            correlation_id=correlation_id)

    def admit(self, tenant_id: str, job_id: str, *, correlation_id: str = "") -> dict:
        """重试准入：janitor 或调用方对 ADMITTED 作业再要一次 permit。

        `EXPIRED` 是终态：过期后不能再准入。这里返回 409 而不是静默返回
        一个 EXPIRED 的响应——静默会让调用方以为"又等了一轮"。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "ADMITTED":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}, not ADMITTED",
                               details={"from": job["state"], "to": "ADMITTED"})
        self._expire_if_stale(tenant_id, job)
        job = self._job(tenant_id, job_id)
        if job["state"] == "EXPIRED":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} expired while waiting for a permit",
                               details={"from": "EXPIRED", "to": "ADMITTED"})
        budget = self._upstream(
            lambda: self.budget_source(tenant_id,
                                       _loads(job["budget_auth_ref"], {})))
        if isinstance(budget, dict) and budget.get("valid") is False:
            return self._as_response(self._reject(
                tenant_id, job, "BUDGET_AUTH_INVALID",
                "S23 rejected the budget authorization", correlation_id))
        permit = self._upstream(
            lambda: self.permit_source(tenant_id, "training", "gpu_second",
                                       job_id, budget))
        if not isinstance(permit, dict) or permit.get("granted") is not True:
            return self._as_response(job)
        job = self._transition(job, "QUEUED",
                               sets={"quota_permit_ref": _jcs(permit)},
                               correlation_id=correlation_id)
        return self._as_response(job)

    def _expire_if_stale(self, tenant_id: str, job: dict) -> None:
        """§7.2：ADMITTED 超过 24h 仍未准入 → EXPIRED（终态，释放预占）。"""
        if job["state"] != "ADMITTED":
            return
        admitted_at = job.get("admitted_at")
        if not admitted_at:
            return
        try:
            moment = datetime.strptime(admitted_at, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            return
        if _now_dt().replace(tzinfo=None) - moment > ADMISSION_TTL:
            self._transition(job, "EXPIRED",
                             reason={"reason": "ADMISSION_TIMEOUT"},
                             sets={"finished_at": _now()})
            self._bump("s48_jobs_total")

    # ------------------------------------------------------------ 准备与训练
    def prepare(self, tenant_id: str, job_id: str, *, correlation_id: str = ""):
        """QUEUED → PREPARING：回读 S28 校验字节没变（§3.4 第 4 条）。"""
        job = self._job(tenant_id, job_id)
        job = self._transition(job, "PREPARING", correlation_id=correlation_id)
        manifest = self._manifest(tenant_id, job["dataset_id"])
        rows = self.conn.execute(
            "SELECT * FROM dataset_item WHERE dataset_id=? ORDER BY ordinal",
            (manifest["dataset_id"],)).fetchall()
        for row in rows:
            item = dict(row)
            current = self._upstream(
                lambda item=item: self.artifact_source(
                    tenant_id, "artifact", item["artifact_id"]))
            digest = bare_hash((current or {}).get("content_hash"))
            if digest is not None and digest != item["content_hash"]:
                return self._as_response(self._reject(
                    tenant_id, job, "HASH_MISMATCH",
                    f"artifact {item['artifact_id']} changed after review",
                    correlation_id))
        return self._as_response(self._job(tenant_id, job_id))

    def start(self, tenant_id: str, job_id: str, *, correlation_id: str = "",
              until: str | None = None):
        """PREPARING → TRAINING：TRAINING 前**再确认一次 permit 未过期**（§3.2）。"""
        job = self._job(tenant_id, job_id)
        if job["state"] != "PREPARING":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}, not PREPARING",
                               details={"from": job["state"], "to": "TRAINING"})
        permit = _loads(job["quota_permit_ref"], None)
        if not isinstance(permit, dict):
            raise ServiceError(409, "PERMIT_INVALID",
                               "job has no permit to start training",
                               details={"from": job["state"]})
        recheck = self._upstream(
            lambda: self.permit_source(tenant_id, "training", "gpu_second",
                                       job_id, {"recheck": True}))
        if not isinstance(recheck, dict) or recheck.get("granted") is not True:
            # §7.2：permit 失效退回 ADMITTED，重新排队等 permit（不是失败）。
            job = self._transition(job, "ADMITTED",
                                   sets={"quota_permit_ref": None},
                                   correlation_id=correlation_id)
            raise ServiceError(409, "PERMIT_INVALID",
                               "permit is no longer valid; job is back to ADMITTED",
                               retryable=True,
                               details={"state": job["state"]})
        job = self._transition(
            job, "TRAINING",
            sets={"started_at": job["started_at"] or _now(),
                  "last_heartbeat_at": _now()},
            correlation_id=correlation_id)
        self._audit(tenant_id, "training.job.start", job_id=job_id,
                    actor="S48", details={"state": job["state"]})
        return self._as_response(job)

    def progress(self, tenant_id: str, job_id: str, *, gpu_seconds: int = 0,
                 checkpoint_ref: str | None = None,
                 heartbeat: bool = True) -> dict:
        """TRAINING 中的心跳/checkpoint。**TRAINING 之外一律拒**（§6.2）。

        用量写在 `training_slot_usage`（不是账本，C48-20）；checkpoint 是
        本服务桶里的 key，不是 S28 制品（§3.5）。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "TRAINING":
            raise ServiceError(
                409, "JOB_STATE_CONFLICT",
                f"progress is only accepted in TRAINING, job is {job['state']}",
                details={"from": job["state"], "to": "TRAINING"})
        now = _now()
        sets = ["last_heartbeat_at = ?"]
        args: list = [now]
        if checkpoint_ref is not None:
            sets.append("checkpoint_ref = ?")
            args.append(checkpoint_ref)
        if gpu_seconds:
            if isinstance(gpu_seconds, bool) or not isinstance(gpu_seconds, int) \
                    or gpu_seconds <= 0:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "gpu_seconds must be a positive integer")
            sets.append("gpu_seconds_consumed = gpu_seconds_consumed + ?")
            args.append(gpu_seconds)
        # 心跳与 checkpoint **不**推进 `aggregate_sequence`：那是状态迁移
        # 的计数器，心跳一秒一条会让事件序号失去"第几次状态变化"的含义。
        args.extend([tenant_id, job_id, "TRAINING"])
        self.conn.execute(
            "UPDATE training_job SET " + ", ".join(sets)
            + " WHERE tenant_id = ? AND job_id = ? AND state = ?", args)
        if gpu_seconds:
            self.conn.execute(
                "INSERT INTO training_slot_usage (usage_id, tenant_id, job_id,"
                " permit_ref, gpu_seconds, resource_class, started_at,"
                " recorded_at) VALUES (?, ?, ?, ?, ?, 'gpu_second', ?, ?)",
                (_new_id("usage"), tenant_id, job_id,
                 job["quota_permit_ref"] or "{}", gpu_seconds,
                 job["started_at"] or now, now))
            self._bump("s48_gpu_seconds_total", gpu_seconds)
        if checkpoint_ref is not None:
            self._audit(tenant_id, "training.checkpoint.write", job_id=job_id,
                        actor="S48",
                        details={"checkpoint_ref": checkpoint_ref})
        return self._as_response(self._job(tenant_id, job_id))

    def resume(self, tenant_id: str, job_id: str, *, checkpoint_ref: str,
               expected_checkpoint: str | None = None,
               correlation_id: str = "") -> dict:
        """§6.4：TRAINING → RESUMING，CAS 用 checkpoint_ref 防两个 runner。

        `expected_checkpoint` 是 runner 开始续跑时看到的那个 key。不一致
        说明另一个 runner 已经续过了 —— 409，不覆盖（T48-32）。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "TRAINING":
            # §6.4 的 CAS 条件是 `state='TRAINING' AND checkpoint_ref=$old`。
            # 已经是 RESUMING 说明**另一个 runner 赢了**——不能当成幂等
            # 重放返回成功，否则两个 runner 都会去写 checkpoint。
            self._bump("s48_resume_conflicts_total")
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}; another runner "
                               "already claimed the resume", retryable=False,
                               details={"from": job["state"], "to": "RESUMING"})
        expected = expected_checkpoint if expected_checkpoint is not None \
            else job["checkpoint_ref"]
        if expected is not None and expected != job["checkpoint_ref"]:
            self._bump("s48_resume_conflicts_total")
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               "checkpoint changed concurrently; another runner "
                               "already resumed this job", retryable=False,
                               details={"expected": expected,
                                        "current": job["checkpoint_ref"]})
        job = self._transition(
            job, "RESUMING",
            sets={"checkpoint_ref": checkpoint_ref,
                  "resume_count": int(job["resume_count"]) + 1,
                  "last_heartbeat_at": _now()},
            correlation_id=correlation_id)
        return self._as_response(job)

    def start_resumed(self, tenant_id: str, job_id: str, *, correlation_id: str = ""):
        """RESUMING → TRAINING。续跑也要 permit 仍有效（§6.4）。

        permit 失效时退回 `ADMITTED` 重新排队——和 `start()` 同一条路径。
        续跑**不重新申请**预算，也不再花一次"新作业"的钱：GPU 秒数继续累加。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "RESUMING":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}, not RESUMING",
                               details={"from": job["state"],
                                        "to": "TRAINING"})
        recheck = self._upstream(
            lambda: self.permit_source(tenant_id, "training", "gpu_second",
                                       job_id, {"recheck": True}))
        if not isinstance(recheck, dict) or recheck.get("granted") is not True:
            self._transition(job, "ADMITTED",
                             sets={"quota_permit_ref": None},
                             correlation_id=correlation_id)
            raise ServiceError(409, "PERMIT_INVALID",
                               "permit expired while resuming; job is back to "
                               "ADMITTED", retryable=True,
                               details={"state": "ADMITTED"})
        job = self._transition(job, "TRAINING",
                               sets={"last_heartbeat_at": _now()},
                               correlation_id=correlation_id)
        self._bump("s48_resume_total")
        return self._as_response(job)

    def sweep_stalled(self, tenant_id: str | None = None) -> dict:
        """§6.4：心跳超时 → RESUMING（可续）。janitor 的入口。

        **不自动续跑**：只把作业标成可续，交给队列决定谁续。这样避免
        "两个 runner 都以为自己该续"。
        """
        cutoff = (_now_dt() - HEARTBEAT_TIMEOUT) \
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        sql = ("SELECT * FROM training_job WHERE state='TRAINING'"
               " AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?)")
        params: tuple = (cutoff,)
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params += (tenant_id,)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        resumed = []
        for job in rows:
            try:
                self._transition(job, "RESUMING",
                                 reason={"reason": "HEARTBEAT_TIMEOUT"},
                                 sets={"resume_count": int(job["resume_count"]) + 1})
            except ServiceError:
                continue
            self._bump("s48_resume_total")
            resumed.append(job["job_id"])
        return {"resumed": resumed}

    def expire_stale_admissions(self, tenant_id: str | None = None) -> dict:
        """§7.2 的 janitor 入口：ADMITTED 超 24h → EXPIRED。"""
        sql = "SELECT * FROM training_job WHERE state='ADMITTED'"
        params: tuple = ()
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params = (tenant_id,)
        expired = []
        for job in [dict(r) for r in self.conn.execute(sql, params).fetchall()]:
            self._expire_if_stale(job["tenant_id"], job)
            if self._job(job["tenant_id"], job["job_id"])["state"] == "EXPIRED":
                expired.append(job["job_id"])
        return {"expired": expired}

    # ------------------------------------------------------------ 完成与评测
    def finish(self, tenant_id: str, job_id: str, *, outcome: str,
               weights_blob_ref: str | None = None,
               weights_hash: str | None = None,
               size_bytes: int | None = None,
               gpu_seconds: int = 0, checkpoint_ref: str | None = None,
               correlation_id: str = "", reason: str | None = None) -> dict:
        """TRAINING → EVALUATING / FAILED。

        `outcome='trained'` 进 EVALUATING；`outcome='failed'` 进 FAILED
        （保留最后 checkpoint，§7.3）。GPU 秒只记在用量视图里。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] not in ("TRAINING", "RESUMING"):
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}",
                               details={"from": job["state"], "to": "EVALUATING"})
        if gpu_seconds:
            self._record_usage(tenant_id, job, gpu_seconds)
        if outcome == "failed":
            failed = self._transition(
                job, "FAILED", reason={"reason": reason or "TRAINING_FAILED"},
                sets={"finished_at": _now(),
                      "checkpoint_ref": checkpoint_ref or job["checkpoint_ref"]},
                correlation_id=correlation_id)
            self._bump("s48_jobs_total")
            self._audit(tenant_id, "training.job.fail", job_id=job_id,
                        actor="S48",
                        details={"reason": reason or "TRAINING_FAILED",
                                 "gpu_seconds_consumed":
                                     int(failed["gpu_seconds_consumed"])})
            return self._as_response(failed)
        if outcome != "trained":
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "outcome must be 'trained' or 'failed'")
        if not weights_blob_ref or not weights_hash:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "weights_blob_ref and weights_hash are required "
                               "when the run trained successfully")
        blob = _require_text(weights_blob_ref, "weights_blob_ref")
        digest = _require_hash(weights_hash, "weights_hash")
        size = _require_int(size_bytes if size_bytes is not None else 0,
                            "size_bytes")
        manifest = self._manifest(tenant_id, job["dataset_id"])
        artifact_id = self._register_candidate(
            tenant_id, job, manifest, blob=blob, digest=digest, size=size)
        evaluating = self._transition(
            job, "EVALUATING",
            sets={"artifact_id": artifact_id,
                  "checkpoint_ref": checkpoint_ref or job["checkpoint_ref"]},
            correlation_id=correlation_id)
        self._audit(tenant_id, "training.job.complete", job_id=job_id,
                    actor="S48",
                    details={"artifact_id": artifact_id,
                             "weights_hash": digest,
                             "gpu_seconds_consumed":
                                 int(evaluating["gpu_seconds_consumed"])})
        return self._as_response(evaluating)

    def _record_usage(self, tenant_id: str, job: dict, gpu_seconds: int) -> None:
        if isinstance(gpu_seconds, bool) or not isinstance(gpu_seconds, int) \
                or gpu_seconds <= 0:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "gpu_seconds must be a positive integer")
        now = _now()
        self.conn.execute(
            "INSERT INTO training_slot_usage (usage_id, tenant_id, job_id,"
            " permit_ref, gpu_seconds, resource_class, started_at, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, 'gpu_second', ?, ?)",
            (_new_id("usage"), tenant_id, job["job_id"],
             job["quota_permit_ref"] or "{}", gpu_seconds,
             job["started_at"] or now, now))
        self.conn.execute(
            "UPDATE training_job SET gpu_seconds_consumed = "
            "gpu_seconds_consumed + ? WHERE tenant_id=? AND job_id=?",
            (gpu_seconds, tenant_id, job["job_id"]))
        self._bump("s48_gpu_seconds_total", gpu_seconds)

    def _register_candidate(self, tenant_id: str, job: dict, manifest: dict, *,
                            blob: str, digest: str, size: int) -> str:
        """写 `lora_artifact(CANDIDATE)`。**同权重 hash 复用**（§5.3 / T48-40）。

        复用只在租户内成立；跨租户共享权重是 E28/E31 的灾难（§5.6）。
        """
        existing = _row(self.conn.execute(
            "SELECT * FROM lora_artifact WHERE tenant_id=? AND weights_hash=?",
            (tenant_id, digest)).fetchone())
        if existing is not None:
            self._bump("s48_artifact_reuse_total")
            return existing["artifact_id"]
        artifact_id = _new_id("artifact")
        self.conn.execute(
            "INSERT INTO lora_artifact (artifact_id, tenant_id, job_id,"
            " adaptation_type, target_service, target_id, target_revision,"
            " weights_blob_ref, storage_ref, weights_hash, size_bytes,"
            " dataset_id, base_service, base_id, base_revision,"
            " base_content_hash, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?,"
            "         'CANDIDATE', ?)",
            (artifact_id, tenant_id, job["job_id"], job["adaptation_type"],
             job["target_service"], job["target_id"], job["target_revision"],
             blob, digest, size, manifest["dataset_id"], job["base_service"],
             job["base_id"], job["base_revision"], job["base_content_hash"],
             _now()))
        return artifact_id

    def evaluate(self, tenant_id: str, job_id: str, *, metrics: dict,
                 passed: bool | None = None,
                 thresholds: dict | None = None,
                 infrastructure_failure: bool = False,
                 correlation_id: str = "") -> dict:
        """EVALUATING → CANDIDATE / EVAL_FAILED / FAILED（§3.6 / §6.5）。

        - 指标不达标 → `EVAL_FAILED`（**不是** FAILED）；
        - 评测基础设施失败重试一次，再失败 → `FAILED`；
        - 缺指标不是"通过"（C48-21）。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "EVALUATING":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}, not EVALUATING",
                               details={"from": job["state"], "to": "EVALUATING"})
        hold = _loads(job["rights_hold"], [])
        if hold:
            # T48-07：训练期间的撤销已挂账。训完了也不评测、不发布 —— 直接
            # BLOCKED。继续跑评测只是多烧一次算力证明一个不能用的东西。
            return self._as_response(self._block(
                tenant_id, job,
                {"reason": "RIGHTS_REVOKED_DURING_TRAINING",
                 "artifacts": hold},
                correlation_id=correlation_id))
        if infrastructure_failure:
            attempts = int(job["eval_attempts"]) + 1
            self.conn.execute(
                "UPDATE training_job SET eval_attempts=? WHERE tenant_id=? AND job_id=?",
                (attempts, tenant_id, job_id))
            if attempts < MAX_EVAL_ATTEMPTS:
                return self._as_response(self._job(tenant_id, job_id))
            failed = self._transition(
                job, "FAILED",
                reason={"reason": "EVALUATION_INFRASTRUCTURE_FAILURE"},
                sets={"finished_at": _now(), "eval_attempts": attempts},
                correlation_id=correlation_id)
            self._bump("s48_jobs_total")
            self._audit(tenant_id, "training.job.fail", job_id=job_id,
                        actor="S48",
                        details={"reason": "EVALUATION_INFRASTRUCTURE_FAILURE"})
            return self._as_response(failed)

        metrics = _require_object(metrics, "metrics")
        spec = _loads(job["evaluation_spec"], {})
        limits = thresholds if thresholds is not None else spec.get("thresholds")
        computed, failures = evaluate_metrics(job["adaptation_type"], metrics,
                                              limits)
        if passed is not None and passed != computed:
            # 调用方说"过了"但指标没过：以指标为准，不采信"过了"这个词。
            if computed is False:
                failures = failures or ["explicit_pass_contradicts_metrics"]
            computed = computed and passed
        holdouts = spec.get("holdout_refs") or []
        evaluation = {
            "metrics": {name: metrics.get(name) for name in
                        list(BASE_METRICS) + list(VOICE_METRICS)
                        if name in metrics},
            "failed_metrics": failures,
            "thresholds": limits or {},
            "holdout_items_hash": holdout_items_hash(holdouts),
            "baseline_ref": {"service_id": job["base_service"],
                             "id": job["base_id"],
                             "revision": job["base_revision"]},
            "passed": bool(computed),
            "evaluated_at": _now(),
        }
        if not computed:
            failed = self._transition(
                job, "EVAL_FAILED",
                reason={"reason": "EVALUATION_BELOW_THRESHOLD",
                        "failed_metrics": failures},
                sets={"evaluation": _jcs(evaluation),
                      "finished_at": _now()},
                correlation_id=correlation_id)
            self.conn.execute(
                "UPDATE lora_artifact SET evaluation=? WHERE tenant_id=? AND"
                " artifact_id=?", (_jcs(evaluation), tenant_id,
                                   job["artifact_id"]))
            self._bump("s48_jobs_total")
            self._publish_completed(failed, artifact_ref=None,
                                    correlation_id=correlation_id)
            return self._as_response(failed)
        candidate = self._transition(
            job, "CANDIDATE", sets={"evaluation": _jcs(evaluation)},
            correlation_id=correlation_id)
        self.conn.execute(
            "UPDATE lora_artifact SET evaluation=? WHERE tenant_id=? AND"
            " artifact_id=?", (_jcs(evaluation), tenant_id, job["artifact_id"]))
        self._bump("s48_eval_passed_total")
        self._publish_completed(candidate, artifact_ref=self._artifact_ref(job),
                                correlation_id=correlation_id)
        return self._as_response(candidate)

    # ------------------------------------------------------------ 接受 / 否决 / 取消
    def accept(self, tenant_id: str, job_id: str, *, decision_ref=None,
               correlation_id: str = "") -> dict:
        """CANDIDATE → PUBLISHED（§3.7）。

        三步全在一个事务里：注册 S28 → 写兼容性记录 → 状态迁移。任何一步
        失败都不置 PUBLISHED（T48-39）。
        **接受是人工或策略的**：本服务不自己判"这个 LoRA 够好了"。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] != "CANDIDATE":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}, not CANDIDATE",
                               details={"from": job["state"], "to": "PUBLISHED"})
        artifact = self._artifact(tenant_id, job["artifact_id"])
        if artifact["state"] == "BLOCKED":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               "artifact is BLOCKED and cannot be published",
                               details={"artifact_state": "BLOCKED"})
        # 接受时授权已变 → 拒绝接受，job → BLOCKED（§7.4）。
        hold = _loads(job["rights_hold"], [])
        if hold:
            return self._as_response(self._block(
                tenant_id, job, {"reason": "RIGHTS_CHANGED",
                                 "artifacts": hold},
                correlation_id=correlation_id))
        if self.registry_sink is None:
            # 没有注册通道时绝不伪造 published（C48-15）。
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "artifact registry (S28) is not configured; a CANDIDATE cannot "
                "be published without a registered artifact id", retryable=True)
        registered = self._upstream(
            lambda: self.registry_sink(tenant_id, artifact["weights_hash"],
                                       artifact["weights_blob_ref"],
                                       artifact["size_bytes"],
                                       {"job_id": job_id,
                                        "base_id": artifact["base_id"],
                                        "base_revision": artifact["base_revision"]}))
        if not isinstance(registered, dict) or not registered.get("artifact_id"):
            raise ServiceError(502, "UPSTREAM_INVALID",
                               "S28 did not return an artifact id")
        s28_id = str(registered["artifact_id"])

        verdict, reason = self._compat_verdict(tenant_id, artifact)
        self.conn.execute(
            "INSERT OR IGNORE INTO base_model_compatibility (compat_id,"
            " tenant_id, artifact_id, base_service, base_id, base_revision,"
            " base_content_hash, verdict, reason, checked_by, checked_at)"
            " VALUES (?, ?, ?, 'S24', ?, ?, ?, ?, ?, 'S48', ?)",
            (_new_id("compat"), tenant_id, artifact["artifact_id"],
             artifact["base_id"], artifact["base_revision"],
             artifact["base_content_hash"], verdict, reason, _now()))
        if verdict == "INCOMPATIBLE":
            # §7.3：基座在训练中退役 → 完成也 BLOCKED。
            return self._as_response(self._block(
                tenant_id, job, {"reason": "BASE_RETIRED",
                                 "verdict": verdict},
                correlation_id=correlation_id))
        now = _now()
        self.conn.execute(
            "UPDATE lora_artifact SET state='PUBLISHED', storage_ref=?,"
            " published_at=? WHERE tenant_id=? AND artifact_id=? AND"
            " state='CANDIDATE'",
            (s28_id, now, tenant_id, artifact["artifact_id"]))
        if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               "artifact state changed concurrently",
                               retryable=True)
        published = self._transition(
            job, "PUBLISHED",
            sets={"finished_at": now,
                  "evaluation": job["evaluation"]},
            correlation_id=correlation_id)
        self._bump("s48_jobs_total")
        self._audit(tenant_id, "training.artifact.publish", job_id=job_id,
                    actor=_actor_id(decision_ref),
                    details={"artifact_id": artifact["artifact_id"],
                             "weights_hash": artifact["weights_hash"],
                             "base_ref": {"service_id": job["base_service"],
                                          "id": artifact["base_id"],
                                          "revision": artifact["base_revision"],
                                          "content_hash":
                                              artifact["base_content_hash"]}})
        response = self._as_response(published)
        # C48-4：建议事件未注册，只能随响应给出；S11/S15 **不自动接受**。
        response["advisories"] = self._advisories(published, artifact)
        return response

    def reject_candidate(self, tenant_id: str, job_id: str, *, reason=None,
                         correlation_id: str = "") -> dict:
        """CANDIDATE → EVAL_FAILED（人工否决，§7.4）。不是 FAILED。"""
        job = self._job(tenant_id, job_id)
        if job["state"] != "CANDIDATE":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']}, not CANDIDATE",
                               details={"from": job["state"],
                                        "to": "EVAL_FAILED"})
        failed = self._transition(
            job, "EVAL_FAILED",
            reason={"reason": "HUMAN_REJECTED",
                    "message": (reason or {}).get("message")
                    if isinstance(reason, dict) else reason},
            sets={"finished_at": _now()}, correlation_id=correlation_id)
        self.conn.execute(
            "UPDATE lora_artifact SET state='SUPERSEDED'"
            " WHERE tenant_id=? AND artifact_id=? AND state='CANDIDATE'",
            (tenant_id, job["artifact_id"]))
        self._bump("s48_jobs_total")
        self._audit(tenant_id, "training.job.reject", job_id=job_id,
                    actor=_actor_id(reason),
                    details={"reason": "HUMAN_REJECTED"})
        self._publish_completed(failed, artifact_ref=None,
                                correlation_id=correlation_id)
        return self._as_response(failed)

    def cancel(self, tenant_id: str, job_id: str, *, reason=None,
               correlation_id: str = "") -> dict:
        """§3.1 / C48-12：未开训 → REJECTED，已开训 → FAILED。

        原因写在 rejection 里。**不删行**：取消本身是审计信息。
        """
        job = self._job(tenant_id, job_id)
        if job["state"] not in CANCELLABLE:
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"job {job_id} is {job['state']} and cannot be "
                               f"cancelled",
                               details={"from": job["state"],
                                        "cancellable": sorted(CANCELLABLE)})
        target = "FAILED" if job["state"] in ("TRAINING", "RESUMING") \
            else "REJECTED"
        cancelled = self._transition(
            job, target,
            reason={"reason": "CANCELLED",
                    "message": (reason or {}).get("message")
                    if isinstance(reason, dict) else reason,
                    "cancelled_from": job["state"]},
            sets={"finished_at": _now()}, correlation_id=correlation_id)
        self._bump("s48_jobs_total")
        if target == "FAILED":
            self._publish_completed(cancelled, artifact_ref=None,
                                    correlation_id=correlation_id)
        return self._as_response(cancelled)

    # ------------------------------------------------------------ 撤销 / 退役
    def block_artifact(self, tenant_id: str, artifact_id: str, *,
                       reason: dict, correlation_id: str = "") -> dict:
        """§3.8：PUBLISHED → BLOCKED。**终态，不能复活**（T48-06）。

        不删字节（S28 的事），不改任何 `S24`/`S25` 的记录；发了会通知它们
        的事件未注册（C48-4），所以只在本服务里落账。
        """
        artifact = self._artifact(tenant_id, artifact_id)
        if artifact["state"] == "BLOCKED":
            return {"artifact_id": artifact_id, "state": "BLOCKED",
                    "changed": False}
        if artifact["state"] != "PUBLISHED":
            raise ServiceError(409, "JOB_STATE_CONFLICT",
                               f"artifact {artifact_id} is {artifact['state']}",
                               details={"from": artifact["state"],
                                        "to": "BLOCKED"})
        self.conn.execute(
            "UPDATE lora_artifact SET state='BLOCKED', blocked_reason=?"
            " WHERE tenant_id=? AND artifact_id=? AND state='PUBLISHED'",
            (_jcs(reason), tenant_id, artifact_id))
        job = _row(self.conn.execute(
            "SELECT * FROM training_job WHERE tenant_id=? AND job_id=?",
            (tenant_id, artifact["job_id"])).fetchone())
        if job is not None and job["state"] == "PUBLISHED":
            self._transition(job, "BLOCKED",
                             reason={"reason": reason.get("reason", "BLOCKED")},
                             sets={"finished_at": _now()},
                             correlation_id=correlation_id)
        self._bump("s48_blocked_artifacts_total")
        self._audit(tenant_id, "training.artifact.block",
                    job_id=artifact["job_id"], actor="S48",
                    details={"artifact_id": artifact_id,
                             "reason": reason.get("reason"),
                             "dataset_id": artifact["dataset_id"]})
        return {"artifact_id": artifact_id, "state": "BLOCKED", "changed": True}

    def _block(self, tenant_id: str, job: dict, reason: dict, *,
               correlation_id: str = "") -> dict:
        """把 job 与它的 artifact 一起置 BLOCKED。"""
        artifact_id = job.get("artifact_id")
        if artifact_id:
            artifact = self._artifact(tenant_id, artifact_id)
            if artifact["state"] in ("PUBLISHED", "CANDIDATE"):
                self.conn.execute(
                    "UPDATE lora_artifact SET state='BLOCKED', blocked_reason=?"
                    " WHERE tenant_id=? AND artifact_id=?",
                    (_jcs(reason), tenant_id, artifact_id))
                self._audit(tenant_id, "training.artifact.block",
                            job_id=job["job_id"], actor="S48",
                            details={"artifact_id": artifact_id,
                                     "reason": reason.get("reason"),
                                     "dataset_id": artifact["dataset_id"]})
        blocked = self._transition(
            job, "BLOCKED", reason={"reason": reason.get("reason", "BLOCKED")},
            sets={"finished_at": _now()}, correlation_id=correlation_id)
        self._bump("s48_blocked_artifacts_total")
        return blocked

    def on_rights_revoked(self, tenant_id: str, envelope: dict) -> dict:
        """S15/S11–S14 的撤销事件入口（§3.8 / §6.2）。

        - job 在 TRAINING：**不中断**，把引用记进 `rights_hold`，
          完成时 `BLOCKED`（T48-07）；
        - 已 PUBLISHED：立即 `BLOCKED`（T48-05）；
        - 重授权也不能复活一个已 BLOCKED 的产物（T48-06）。
        """
        event = envelope.get("event", envelope)
        if not isinstance(event, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event must be an object")
        event_id = str(event.get("id") or "")
        event_type = str(event.get("type") or "")
        if event.get("tenant_id") not in (None, tenant_id):
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "event tenant disagrees with the signed context")
        if event_id:
            # 至少一次投递：同 event_id 第二次进来必须是无操作。
            if not self.inbox.consume("s48.rights", event_id):
                return {"consumed": False, "blocked": [], "held": []}
        data = event.get("data") or {}
        rights_id = data.get("rights_id") or data.get("id") or \
            data.get("voice_id") or data.get("asset_id")
        state = str(data.get("state") or data.get("status") or "").lower()
        revoked = state in ("revoked", "expired", "terminated") or \
            event_type.endswith("revoked")
        target_state = "revoked" if revoked else state
        if rights_id is None:
            return {"consumed": True, "blocked": [], "held": []}

        # 撤销事件可能按授权 id 也可能按素材 id 定位（各权威的字段名未统一，
        # C48-2）。两列都查，宁可比对多一条，不可漏掉一个数据集。
        rows = self.conn.execute(
            "SELECT DISTINCT dataset_id FROM dataset_item"
            " WHERE tenant_id=? AND (rights_id=? OR artifact_id=?)",
            (tenant_id, rights_id, rights_id)).fetchall()
        dataset_ids = [row["dataset_id"] for row in rows]
        if not dataset_ids:
            return {"consumed": True, "blocked": [], "held": []}
        placeholders = ",".join("?" * len(dataset_ids))
        job_rows = self.conn.execute(
            "SELECT * FROM training_job WHERE tenant_id=? AND dataset_id IN ("
            + placeholders + ")", (tenant_id, *dataset_ids)).fetchall()
        blocked, held = [], []
        if target_state not in ("revoked", "expired"):
            # suspended（技术下架）不改产物合法性，只影响新训练的入册；
            # 已训出的 LoRA 仍可挂载（§1.4）。不发 BLOCKED 是最小惊讶。
            return {"consumed": True, "blocked": [], "held": []}
        for job in [dict(row) for row in job_rows]:
            if job["state"] in ("TRAINING", "RESUMING", "EVALUATING"):
                # §6.2：不中断。挂账，完成时统一处理（T48-07）。
                hold = _loads(job["rights_hold"], [])
                if rights_id not in hold:
                    hold.append(rights_id)
                self.conn.execute(
                    "UPDATE training_job SET rights_hold=? WHERE tenant_id=?"
                    " AND job_id=?", (_jcs(hold), tenant_id, job["job_id"]))
                held.append(job["job_id"])
                continue
            if job["state"] == "CANDIDATE":
                self._block(tenant_id, job,
                            {"reason": "RIGHTS_REVOKED",
                             "rights_id": rights_id})
                blocked.append(job["job_id"])
                continue
            if job["state"] == "PUBLISHED" and job["artifact_id"]:
                self.block_artifact(
                    tenant_id, job["artifact_id"],
                    reason={"reason": "RIGHTS_REVOKED", "rights_id": rights_id})
                blocked.append(job["job_id"])
        return {"consumed": True, "blocked": blocked, "held": held}

    def on_base_retired(self, tenant_id: str, envelope: dict) -> dict:
        """S24 基座退役（§7.3 / F48-02）。

        在训的不中断；已发布的产物 → BLOCKED，verdict 记 INCOMPATIBLE。
        本地只投影"收到过退役通知"，基座可用性仍是 S24 的事实。
        """
        event = envelope.get("event", envelope)
        if not isinstance(event, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "event must be an object")
        event_id = str(event.get("id") or "")
        if event.get("tenant_id") not in (None, tenant_id):
            raise ServiceError(403, "TENANT_SCOPE_DENIED",
                               "event tenant disagrees with the signed context")
        if event_id and not self.inbox.consume("s48.base", event_id):
            return {"consumed": False, "blocked": []}
        data = event.get("data") or {}
        base_id = data.get("base_id") or data.get("id")
        revision = data.get("revision")
        if base_id is None or revision is None:
            return {"consumed": True, "blocked": []}
        revision = int(revision)
        now = _now()
        self.conn.execute(
            "INSERT OR REPLACE INTO base_retirement (tenant_id, base_service,"
            " base_id, base_revision, reason, retired_at, recorded_at)"
            " VALUES (?, 'S24', ?, ?, ?, ?, ?)",
            (tenant_id, base_id, revision, data.get("reason"),
             str(data.get("retired_at") or now), now))
        rows = self.conn.execute(
            "SELECT * FROM lora_artifact WHERE tenant_id=? AND base_id=?"
            " AND base_revision=?", (tenant_id, base_id, revision)).fetchall()
        blocked = []
        for artifact in [dict(row) for row in rows]:
            if artifact["state"] in ("PUBLISHED", "CANDIDATE"):
                job = _row(self.conn.execute(
                    "SELECT * FROM training_job WHERE tenant_id=? AND job_id=?",
                    (tenant_id, artifact["job_id"])).fetchone())
                if artifact["state"] == "PUBLISHED":
                    self.block_artifact(
                        tenant_id, artifact["artifact_id"],
                        reason={"reason": "BASE_RETIRED", "base_id": base_id,
                                "revision": revision})
                else:
                    self.conn.execute(
                        "UPDATE lora_artifact SET state='BLOCKED',"
                        " blocked_reason=? WHERE tenant_id=? AND artifact_id=?",
                        (_jcs({"reason": "BASE_RETIRED", "base_id": base_id,
                               "revision": revision}), tenant_id,
                         artifact["artifact_id"]))
                    if job is not None:
                        self._block(tenant_id, job,
                                    {"reason": "BASE_RETIRED"})
                self.conn.execute(
                    "INSERT OR IGNORE INTO base_model_compatibility (compat_id,"
                    " tenant_id, artifact_id, base_service, base_id,"
                    " base_revision, base_content_hash, verdict, reason,"
                    " checked_by, checked_at)"
                    " VALUES (?, ?, ?, 'S24', ?, ?, ?, 'INCOMPATIBLE',"
                    " 'BASE_RETIRED', 'S48', ?)",
                    (_new_id("compat"), tenant_id, artifact["artifact_id"],
                     base_id, revision, artifact["base_content_hash"], now))
                blocked.append(artifact["artifact_id"])
        return {"consumed": True, "blocked": blocked}

    # ------------------------------------------------------------ 事件
    def _publish_completed(self, job: dict, *, artifact_ref, correlation_id: str):
        """§4.3：`adaptation.training.completed`。

        与状态迁移在同一事务（outbox）。事件里**不含**训练数据、权重 URL、
        prompt、超参。`aggregate_sequence` 用 job 的阶段序号。
        `data_ref` 给 `null`（ATR-1）：`training_job` 既没有 `revision` 也
        没有 `content_hash` 列，凑一个"文件句柄"就是伪造版本。
        """
        dataset = None
        if job.get("dataset_id"):
            manifest = self._manifest(job["tenant_id"], job["dataset_id"])
            dataset = {"service_id": "S48", "id": manifest["dataset_id"],
                       "items_hash": manifest["items_hash"]}
        evaluation = _loads(job.get("evaluation"), {}) or {}
        metrics = evaluation.get("metrics") or {}
        data = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job["job_id"],
            "state": job["state"],
            "adaptation_type": job["adaptation_type"],
            # 事件 schema 的 target_ref 只有三键（ATR-4 / C48-19）；
            # content_hash 留在行与查询里，不塞进契约没允许的键。
            "target_ref": {"service_id": job["target_service"],
                           "id": job["target_id"],
                           "revision": int(job["target_revision"])},
            "base_model_ref": {"service_id": job["base_service"],
                               "id": job["base_id"],
                               "revision": int(job["base_revision"]),
                               "content_hash": job["base_content_hash"]},
            "artifact_ref": artifact_ref,
            "dataset_ref": dataset,
            "evaluation_passed": bool(evaluation.get("passed")),
            "metrics_summary": {name: metrics.get(name)
                                for name in ("identity_preservation",
                                             "overfit_score",
                                             "holdout_loss")
                                if name in metrics},
            "gpu_seconds_consumed": int(job["gpu_seconds_consumed"]),
        }
        envelope = new_event(
            source=SOURCE,
            type=EVENT_COMPLETED,
            subject=f"training-job/{job['job_id']}",
            tenant_id=job["tenant_id"],
            home_cell=self.home_cell,
            aggregate_id=job["job_id"],
            sequence=int(job["aggregate_sequence"]),
            correlation_id=correlation_id or job["command_id"],
            causation_id=None,
            data=data,
            data_ref=None,
        )
        self.outbox.append(envelope)

    def _artifact_ref(self, job: dict) -> dict | None:
        if not job.get("artifact_id"):
            return None
        return {"service_id": "S28", "id": job["artifact_id"]}

    def _advisories(self, job: dict, artifact: dict) -> list[dict]:
        """C48-4：建议事件未注册，只能随响应返回。**永不自动接受。**"""
        if job["adaptation_type"] == "voice_clone":
            return [{"event_type": "training.voice_clone.ready",
                     "target": "S15",
                     "artifact_ref": {"service_id": "S28",
                                      "id": artifact["storage_ref"]},
                     "effect": "advisory_only",
                     "note": "S15 decides whether to write clone_artifact_ref"}]
        return [{"event_type": "training.lora.registered",
                 "target": job["target_service"],
                 "artifact_ref": {"service_id": "S28",
                                  "id": artifact["storage_ref"]},
                 "effect": "advisory_only",
                 "note": "the asset service writes a suggestion, never an "
                         "auto-accept"}]

    def _compat_verdict(self, tenant_id: str, artifact: dict) -> tuple[str, str | None]:
        """写兼容性记录时的 verdict。**UNKNOWN 不是 COMPATIBLE。**

        先看本地退役投影（S24 已经说过基座退役），再回源问 S24 当前结论。
        """
        retired = _row(self.conn.execute(
            "SELECT 1 FROM base_retirement WHERE tenant_id=? AND base_id=?"
            " AND base_revision=?", (tenant_id, artifact["base_id"],
                                     artifact["base_revision"])).fetchone())
        if retired is not None:
            return "INCOMPATIBLE", "BASE_RETIRED"
        try:
            found = self._upstream(
                lambda: self.base_model_source(
                    tenant_id, artifact["base_id"], artifact["base_revision"],
                    artifact["base_content_hash"]))
        except ServiceError:
            return "UNKNOWN", "AUTHORITY_UNREACHABLE"
        if not isinstance(found, dict) or found.get("found") is False:
            return "INCOMPATIBLE", "BASE_NOT_FOUND"
        verdict = found.get("compatible")
        if verdict is True:
            return "COMPATIBLE", None
        if verdict is False:
            return "INCOMPATIBLE", found.get("reason") or "INCOMPATIBLE"
        return "UNKNOWN", found.get("reason") or "NOT_REPORTED"

    # ------------------------------------------------------------ 查询
    def _as_response(self, job: dict) -> dict:
        return self._job_response(job["tenant_id"], job["job_id"])

    def _job_response(self, tenant_id: str, job_id: str,
                      include_items: bool = False) -> dict:
        job = self._job(tenant_id, job_id)
        manifest = None
        resolved_items = None
        if job.get("dataset_id"):
            manifest = self._manifest(tenant_id, job["dataset_id"])
            if include_items:
                resolved_items = self._dataset_items(tenant_id,
                                                     job["dataset_id"])
        return {
            "job_id": job["job_id"],
            "state": job["state"],
            "adaptation_type": job["adaptation_type"],
            "target_ref": {"service_id": job["target_service"],
                           "id": job["target_id"],
                           "revision": int(job["target_revision"]),
                           "content_hash": job["target_content_hash"]},
            "base_model": {"service_id": job["base_service"],
                           "id": job["base_id"],
                           "revision": int(job["base_revision"]),
                           "content_hash": job["base_content_hash"]},
            "hyperparams": _loads(job["hyperparams"], {}),
            "evaluation_spec": _loads(job["evaluation_spec"], {}),
            "dataset_ref": None if manifest is None else {
                "service_id": "S48", "id": manifest["dataset_id"],
                "items_hash": manifest["items_hash"],
                "item_count": int(manifest["item_count"]),
                "excluded_count": int(manifest["excluded_count"]),
                "state": manifest["state"]},
            "resolved_items": resolved_items,
            "evaluation": _loads(job["evaluation"], None),
            "artifact_ref": self._artifact_ref(job),
            "rejection": _loads(job["rejection"], None),
            "gpu_seconds_consumed": int(job["gpu_seconds_consumed"]),
            "resume_count": int(job["resume_count"]),
            "aggregate_sequence": int(job["aggregate_sequence"]),
            "home_region": job["home_region"],
            "state_since": job["state_since"],
            "created_at": job["created_at"],
            "admitted_at": job["admitted_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }

    def get_job(self, tenant_id: str, job_id: str) -> dict:
        return self._job_response(tenant_id, job_id)

    def list_jobs(self, tenant_id: str, *, state: str | None = None,
                  created_from: str | None = None,
                  created_to: str | None = None,
                  cursor: str | None = None, limit: int = 50) -> dict:
        sql = "SELECT * FROM training_job WHERE tenant_id=?"
        params: list = [tenant_id]
        if state:
            if state not in STATES:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"unknown state {state}")
            sql += " AND state=?"
            params.append(state)
        if created_from:
            sql += " AND created_at >= ?"
            params.append(created_from)
        if created_to:
            sql += " AND created_at <= ?"
            params.append(created_to)
        if cursor:
            sql += " AND created_at < ?"
            params.append(cursor)
        sql += " ORDER BY created_at DESC, job_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        items = [self._job_response(tenant_id, row["job_id"]) for row in rows]
        next_cursor = rows[-1]["created_at"] if rows else None
        return {"items": items, "next_cursor": next_cursor}

    def _dataset_items(self, tenant_id: str, dataset_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM dataset_item WHERE tenant_id=? AND dataset_id=?"
            " ORDER BY ordinal", (tenant_id, dataset_id)).fetchall()
        return [{
            "artifact_ref": {"service_id": row["artifact_service"],
                             "id": row["artifact_id"],
                             "content_hash": row["content_hash"]},
            "rights_ref": {
                "service_id": row["rights_service"],
                "id": row["rights_id"],
                "revision": int(row["rights_revision"]),
                "content_hash": row["rights_content_hash"],
                "grant_kind": _loads(row["rights_grant_kind"], None),
                "state": row["rights_state_at_check"],
                "checked_at": row["rights_checked_at"]},
            "ordinal": int(row["ordinal"]),
        } for row in rows]

    def get_dataset(self, tenant_id: str, dataset_id: str) -> dict:
        manifest = self._manifest(tenant_id, dataset_id)
        rows = self.conn.execute(
            "SELECT * FROM dataset_exclusion WHERE tenant_id=? AND dataset_id=?"
            " ORDER BY artifact_id", (tenant_id, dataset_id)).fetchall()
        return {
            "dataset_id": manifest["dataset_id"],
            "job_id": manifest["job_id"],
            "mode": manifest["mode"],
            "source_ref": _loads(manifest["source_ref"], None),
            "items_hash": manifest["items_hash"],
            "item_count": int(manifest["item_count"]),
            "excluded_count": int(manifest["excluded_count"]),
            "state": manifest["state"],
            "frozen_at": manifest["frozen_at"],
            "created_at": manifest["created_at"],
            "exclusions": [{
                "artifact_id": row["artifact_id"],
                "reason": row["reason"],
                "rights_ref": _loads(row["rights_ref"], None),
            } for row in rows],
        }

    def get_dataset_items(self, tenant_id: str, dataset_id: str, *,
                          cursor: str | None = None,
                          limit: int = 100) -> dict:
        """§4.2：逐项列出 rights 快照，ordinal 游标分页。

        `next_cursor` 只在**确实还有下一项**时才给。把最后一页的最后一项
        也当游标会让调用方多跑一次空页，并且拿不到"到底了"的信号。
        """
        self._manifest(tenant_id, dataset_id)
        sql = ("SELECT * FROM dataset_item WHERE tenant_id=? AND dataset_id=?"
               " AND ordinal > ? ORDER BY ordinal LIMIT ?")
        start = 0
        if cursor:
            try:
                start = int(cursor)
            except (TypeError, ValueError):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "cursor must be an integer ordinal")
        page_size = max(1, min(int(limit), 500))
        # 多取一条用来判断"还有没有下一项"，不把它返回给调用方。
        rows = self.conn.execute(
            sql, (tenant_id, dataset_id, start, page_size + 1)).fetchall()
        page = [dict(row) for row in rows[:page_size]]
        has_more = len(rows) > page_size
        next_cursor = page[-1]["ordinal"] if page and has_more else None
        return {"items": [{
            "artifact_ref": {"service_id": row["artifact_service"],
                             "id": row["artifact_id"],
                             "content_hash": row["content_hash"]},
            "rights_ref": {
                "service_id": row["rights_service"],
                "id": row["rights_id"],
                "revision": int(row["rights_revision"]),
                "content_hash": row["rights_content_hash"],
                "grant_kind": _loads(row["rights_grant_kind"], None),
                "state": row["rights_state_at_check"],
                "checked_at": row["rights_checked_at"]},
            "ordinal": int(row["ordinal"]),
        } for row in page], "next_cursor": next_cursor}

    def get_artifact(self, tenant_id: str, artifact_id: str) -> dict:
        """§4.2。`usable_for_generation` 是**计算出来的快照**，不是判定。

        `__hints` 必须把三个权威点明：当前兼容性 S24、基座可用性 S24、
        授权 S11/S15。少了它 UI 会把"上次检查时可用"读成"现在可用"。
        """
        artifact = self._artifact(tenant_id, artifact_id)
        compat = _row(self.conn.execute(
            "SELECT * FROM base_model_compatibility WHERE tenant_id=?"
            " AND artifact_id=? ORDER BY checked_at DESC LIMIT 1",
            (tenant_id, artifact_id)).fetchone())
        hold = self._rights_still_active(tenant_id, artifact)
        return {
            "artifact_id": artifact["artifact_id"],
            "state": artifact["state"],
            "adaptation_type": artifact["adaptation_type"],
            "target_ref": {"service_id": artifact["target_service"],
                           "id": artifact["target_id"],
                           "revision": int(artifact["target_revision"])},
            "base_model": {
                "service_id": artifact["base_service"] if "base_service"
                in artifact else "S24",
                "id": artifact["base_id"],
                "revision": int(artifact["base_revision"]),
                "content_hash": artifact["base_content_hash"],
                "compat_recorded_at": compat["checked_at"] if compat else None,
                "recorded_verdict": compat["verdict"] if compat else None,
            },
            "weights": {
                "artifact_ref": None if artifact["storage_ref"] is None else {
                    "service_id": "S28", "id": artifact["storage_ref"]},
                "weights_hash": artifact["weights_hash"],
                "size_bytes": int(artifact["size_bytes"]),
            },
            "dataset_ref": {"service_id": "S48",
                            "id": artifact["dataset_id"],
                            "items_hash": self._manifest(
                                tenant_id, artifact["dataset_id"])["items_hash"]},
            "evaluation": _loads(artifact["evaluation"], None),
            "as_of": _now(),
            "__hints": {
                "authority": {
                    "current_compatibility": "S24",
                    "base_available": "S24",
                    "rights_current": "S11/S15",
                },
                # 快照：当前授权仍 active 且记录里的 verdict 是 COMPATIBLE。
                # 真正放行在 S25 路由时实时判。
                "usable_for_generation": bool(
                    artifact["state"] == "PUBLISHED" and not hold
                    and (compat is not None and compat["verdict"] ==
                         "COMPATIBLE")),
                "snapshot_not_verdict": True,
            },
        }

    def _rights_still_active(self, tenant_id: str, artifact: dict) -> list[str]:
        """回查数据集的授权是否仍全部 active。返回**不再 active** 的 id。

        这是展示用的快照——权威仍是 S11–S15。不可达时返回 `["unverified"]`
        而不是空列表：空列表会被读成"全部 active"。
        """
        # 用**复核时用过的同一个键**（artifact）回查，而不是 rights_id：
        # 权威接受哪种键没有任何契约说明（C48-2），但"复核能问、回查也能问"
        # 至少保证两个方向的答案可比。换键去问会得到一个"查不到"的假阴性。
        rows = self.conn.execute(
            "SELECT DISTINCT artifact_service, artifact_id FROM dataset_item"
            " WHERE tenant_id=? AND dataset_id=?",
            (tenant_id, artifact["dataset_id"])).fetchall()
        stale = []
        for row in rows:
            try:
                rights = self._upstream(
                    lambda row=row: self.rights_source(
                        tenant_id, row["artifact_service"], row["artifact_id"]))
            except ServiceError:
                return ["unverified"]
            state = str((rights or {}).get("state")
                        or (rights or {}).get("status") or "").lower()
            if state not in TRAINABLE_RIGHTS_STATES:
                stale.append(row["artifact_id"])
        return stale

    def get_compatibility(self, tenant_id: str, artifact_id: str) -> dict:
        """§1.5：这是**训练时刻**的记录，不是当前兼容性。"""
        self._artifact(tenant_id, artifact_id)
        row = _row(self.conn.execute(
            "SELECT * FROM base_model_compatibility WHERE tenant_id=?"
            " AND artifact_id=? ORDER BY checked_at DESC LIMIT 1",
            (tenant_id, artifact_id)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no compatibility record for {artifact_id}")
        return {
            "artifact_id": artifact_id,
            "base_ref": {"service_id": row["base_service"],
                         "id": row["base_id"],
                         "revision": int(row["base_revision"]),
                         "content_hash": row["base_content_hash"]},
            "verdict": row["verdict"],
            "reason": row["reason"],
            "checked_by": row["checked_by"],
            "checked_at": row["checked_at"],
            "__hints": {"authority": {
                "current_compatibility": "S24",
                "note": "recorded at training time; may be stale — ask S24 "
                        "for the current verdict before routing"}},
        }

    def retention_check(self, tenant_id: str, *, kind: str, identifier: str) -> dict:
        """§10.3：S28 删训练数据前问「这 artifact 还被训练引用吗」。

        LaRA 一旦 PUBLISHED，字节不该因数据集里的某张图被删而消失；
        但若数据集仍被任何 job 引用，训练来源就还没清账。两种引用分开答。
        """
        now = _now()
        if kind in ("lora_artifact", "adaptation_artifact"):
            artifact = _row(self.conn.execute(
                "SELECT * FROM lora_artifact WHERE tenant_id=? AND artifact_id=?",
                (tenant_id, identifier)).fetchone())
            if artifact is None:
                return {"referenced": False, "reason": None, "checked_at": now}
            if artifact["state"] in ("PUBLISHED", "BLOCKED"):
                return {"referenced": True, "reason": "PUBLISHED_LORA",
                        "checked_at": now,
                        "state": artifact["state"]}
            return {"referenced": True, "reason": "PUBLISHED_LORA",
                    "checked_at": now, "state": artifact["state"]}
        if kind in ("dataset_artifact", "artifact"):
            rows = self.conn.execute(
                "SELECT DISTINCT dataset_id FROM dataset_item WHERE tenant_id=?"
                " AND artifact_id=?", (tenant_id, identifier)).fetchall()
            if not rows:
                return {"referenced": False, "reason": None, "checked_at": now}
            return {"referenced": True, "reason": "DATASET_SOURCE",
                    "checked_at": now,
                    "datasets": [row["dataset_id"] for row in rows]}
        raise ServiceError(400, "MALFORMED_REQUEST",
                           f"unknown retention kind {kind}")

    def list_audit(self, tenant_id: str, job_id: str) -> dict:
        """§8.3 的本地审计链。**不替代 S45**（C48-16）。"""
        self._job(tenant_id, job_id)
        rows = self.conn.execute(
            "SELECT * FROM training_audit WHERE tenant_id=? AND job_id=?"
            " ORDER BY recorded_at, audit_id", (tenant_id, job_id)).fetchall()
        return {"job_id": job_id, "items": [{
            "action": row["action"],
            "actor": row["actor"],
            "details": _loads(row["details"], {}),
            "recorded_at": row["recorded_at"],
        } for row in rows]}

    def readiness(self) -> dict:
        row = self.conn.execute("SELECT 1").fetchone()
        return {
            "ready": row is not None,
            "training_enabled": self.training_enabled,
            "inference_pool_borrow_total": self._metrics.get(
                "s48_inference_pool_borrow_total", 0),
            "metrics": self.metrics,
            "contract_gaps": CONTRACT_GAPS,
        }


def _grants_training(grant_kind) -> bool:
    """C48-3：授权必须**明确**包含 training。缺失一律不通过。

    `grant_kind` 可能是字符串（`"generation,training"`）、列表，或对象
    （`{"generation": true, "training": false}`）。三种都按"能不能证明
    覆盖训练"判，证不出来就是 False——绝不假设「没说不行就是允许」。
    """
    if grant_kind is None:
        return False
    if isinstance(grant_kind, str):
        tokens = [token.strip().lower()
                  for token in grant_kind.replace("|", ",").split(",")]
        return any("training" in token for token in tokens)
    if isinstance(grant_kind, (list, tuple, set)):
        for entry in grant_kind:
            if isinstance(entry, str) and "training" in entry.lower():
                return True
            if isinstance(entry, dict) and entry.get("training") is True:
                return True
        return False
    if isinstance(grant_kind, dict):
        if grant_kind.get("training") is True:
            return True
        allowed = grant_kind.get("allowed_uses") or grant_kind.get("uses") or []
        return any(isinstance(entry, str) and "training" in entry.lower()
                   for entry in allowed)
    return False


def _actor_id(value) -> str:
    if isinstance(value, dict):
        return str(value.get("actor") or value.get("principal_id")
                   or value.get("id") or "unknown")
    if isinstance(value, str) and value:
        return value
    return "unknown"
