"""S21 orchestration 的业务内核。

范围：plans/S21-orchestration.md §1（数据模型）、§3（关键流程）、§4（停止/取消/
恢复）、§5（对外契约）、§6（幂等与并发）。

**这个文件不是第二个执行状态机。** 执行 history 归 Temporal；这里只有
业务投影（`run_projection`）、Run↔Temporal 绑定、跨代检查点、在途节点索引、
命令流水与修复额度。§0.2 的裁决：投影与 Temporal 冲突时 **Temporal 赢，
投影被修复**。

## 为什么依赖是注入的 callable

`temporalio` 不在依赖里，而且本仓库没有任何服务发过出站 HTTP（S22 / S23 / S24 /
S28 都是纯内核 + HTTP 入口）。所以 S21 对五个依赖的调用一律走端口：

| 端口 | 契约 | 真实实现 |
|---|---|---|
| `fence` | S08 `POST /branches/{series_id}/{branch_id}:fence` | §4.2 / §4.6 |
| `effective_policy` | S02 `GET /projects/{project_id}/effective-policy` | §3.1 步骤 4（**S02 是 design_only，按 mock 走**） |
| `read_manifest` | S08 `GET /manifests/{id}` | §3.1 步骤 3 的 fence 复查 |
| `query_jobs` | S26 `GET /generation-jobs/{id}` | §4.3 恢复前必须调 |
| `cancel_job` | S26 取消接口 | §4.2 步骤 3a，返回 cancelled/not_cancellable/unknown |
| `start_workflow` | Temporal `start_workflow` | §3.1 步骤 7（确定性 workflow_id） |

测试直接注入 fake。没注入就抛 `DEPENDENCY_UNAVAILABLE`（503，契约里标
`retryable=true`）——**不静默降级**。

## 三条不能破的性质

1. **`execution_fence` 只由 S08 写。** S21 是触发者，不是发起者：取消与故障
   切换调 S08，自己从不 +1，也从不缓存它当作判定依据（§0.3）。
2. **`node_epoch` 只用于单个 `logical_node`。** 消费 `changed_node_epochs` 时
   只取消 `node_epoch == from` 的那一项；`from == to` 说明 epoch 没动，
   **不能取消**（SMC-5 的守卫，见 `_apply_node_epoch_change`）。
3. **fence 先于 cancel。** 顺序反了会出现"workflow 已取消但 S08 还接受提交"
   的窗口，迟到 `:commit` 会把已取消 Run 的产物写进 manifest（§4.2，T21-26）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from event_sdk import Inbox, Outbox, new_event

# §2.6：11 个状态，与 openapi `Run.state` 完全一致。不允许第 12 个。
RUN_STATES = (
    "QUEUED", "RUNNING", "WAITING_INPUT", "WAITING_REVIEW", "WAITING_MEMORY",
    "WAITING_BUDGET", "PAUSED", "SUCCEEDED", "INCOMPLETE", "FAILED", "CANCELED",
)
TERMINAL_STATES = frozenset({"SUCCEEDED", "INCOMPLETE", "FAILED", "CANCELED"})
WAITING_STATES = frozenset({
    "WAITING_INPUT", "WAITING_REVIEW", "WAITING_MEMORY", "WAITING_BUDGET",
})

# §2.6：`WAITING_*` 与 `PAUSED` 下在途任务照常跑完、归档、结算。
# 只有"不再准入新的"——不是"中止已有的"。
SETTLING_STATES = frozenset({"WAITING_INPUT", "WAITING_REVIEW", "WAITING_MEMORY",
                             "WAITING_BUDGET", "PAUSED"})

# `Operation.state` 只有五个值（C21-3）。Run 进的第一个状态映射到它；
# 之外的映射到 `accepted`，因为 Operation 只表达"受理瞬间"。
OPERATION_STATE = {
    "QUEUED": "accepted",
    "RUNNING": "accepted",
    "PAUSED": "accepted",
    "WAITING_INPUT": "waiting_input",
    "WAITING_REVIEW": "waiting_review",
    "WAITING_MEMORY": "waiting_memory",
    "WAITING_BUDGET": "waiting_budget",
}

# §4.5 的取消分类表。`SUBMISSION_UNKNOWN` 明确**不可取消**。
NOT_CANCELLABLE_JOB_STATES = frozenset({"SUBMISSION_UNKNOWN", "UNKNOWN"})

INTENT_KINDS = ("episode_production", "change_subgraph", "repair", "memory_migration")

# 创建 Run 时受支持的 `intent`。openapi 的 body 没有这一项（C21-12）；
# 缺省按集级生产处理。
DEFAULT_INTENT = "episode_production"

# `generation_mode=manual` 时 Run 落这个状态（§3.1 末段）。
WAITING_REASON_MANUAL = "GENERATION_MODE_MANUAL"

# §4.7：派单超时。没有 provider_job_id，只有 S21 生成的 dispatch_request_id。
BLOCKED_REASON_DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
WAITING_REASON_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
BLOCKED_REASON_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


def _now() -> str:
    return _ts(datetime.now(timezone.utc))


def _ts(value: datetime) -> str:
    """固定宽度 27 字符 UTC 时间戳；理由同 S22/S23（字符串序 == 时间序）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(text):
    return json.loads(text) if text else None


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self, correlation_id: str = "") -> dict:
        # openapi `Error` 是**扁平**的，correlation_id 必填。
        out = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id or "s21",
        }
        if self.details:
            out["details"] = self.details
        return out


class Transaction:
    """显式事务。`isolation_level=None` 下 `with conn:` 不是事务（C21-16）。

    **可重入**：SQLite 不允许嵌套 `BEGIN`，而 `_resume` 这类流程天然是
    "外层一个事务、内层若干辅助写入"。内层只挂在外层上，不自己 BEGIN、
    不自己 COMMIT；异常抛到最外层才 ROLLBACK。这样内层也不会吞掉一个
    半途的提交。
    """

    def __init__(self, conn, depth: list[int]):
        # 深度放列表里：`sqlite3.Connection` 不接受自定义属性。
        self.conn = conn
        self.depth = depth
        self.outermost = False

    def __enter__(self):
        self.outermost = self.depth[0] == 0
        if self.outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        self.depth[0] += 1
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.depth[0] -= 1
        if not self.outermost:
            return False
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


# ---------------------------------------------------------------- 确定性 id

def workflow_id_for(workflow_type: str, tenant_id: str, business_idem_key: str,
                    run_generation: int) -> str:
    """§6.1 第三层：确定性 `workflow_id`，`start_workflow` 重试不产生第二个。

    形如 `{workflow_type}:{tenant_id}:{business_idem_key}:g{run_generation}`。
    确定性是它唯一的价值——换任何一个输入都会破坏重试幂等。
    """
    if run_generation < 1:
        raise ValueError("run_generation must be >= 1")
    return f"{workflow_type}:{tenant_id}:{business_idem_key}:g{run_generation}"


def business_idem_key_for(*, tenant_id: str, series_id: str, branch_id: str,
                          episode_id: str, base_manifest_ref: dict,
                          automation_mode: str, policy_revision,
                          budget_scope_id: str, intent: str) -> str:
    """§6.1 第二层：同一次"意图"不产生第二个 Run。

    九个输入一个都不能少：少一个就会把两次不同的生产意图判成同一个 Run。
    `base_manifest_ref` 参与整个三键（id/revision/content_hash），因为
    "同一 manifest 的同一版本"是幂等的，换一个内容哈希就是另一件事。
    """
    if intent not in INTENT_KINDS:
        raise ValueError(f"unknown intent {intent!r}")
    material = {
        "tenant_id": tenant_id,
        "series_id": series_id,
        "branch_id": branch_id,
        "episode_id": episode_id,
        "base_manifest_ref": base_manifest_ref,
        "automation_mode": automation_mode,
        "policy_revision": policy_revision,
        "budget_scope_id": budget_scope_id,
        "intent": intent,
    }
    return hashlib.sha256(_dumps(material).encode("utf-8")).hexdigest()


class OrchestrationService:
    def __init__(self, conn, *, clock=_now, home_cell: str = "cell_a1",
                 source: str = "S21", namespace: str = "drama",
                 task_queue: str = "orchestration-episode",
                 workflow_type: str = "episode_production",
                 # §2.2 `MAX_SHOTS_PER_EPISODE = 800`。2000 是 Temporal 的
                 # `MAX_CONCURRENT_CHILDREN` 硬上限，不是集规模闸门——拿它
                 # 当闸门等于没有闸门（C21-24）。
                 max_nodes_per_episode: int = 800,
                 # ---- 依赖端口（§3.1 / §4.2 / §4.3 / §4.6）----
                 fence: Callable[[str, dict], dict] | None = None,
                 effective_policy: Callable[[str, dict], dict] | None = None,
                 read_manifest: Callable[[str, str], dict] | None = None,
                 query_jobs: Callable[[str, dict], dict] | None = None,
                 cancel_job: Callable[[str, dict], dict] | None = None,
                 start_workflow: Callable[[str, dict], dict] | None = None):
        self.conn = conn
        # 必须是 `sqlite3.Row`：幂等 SDK 用 `stored_hash, result = row` 按**下标**
        # 解包（S23 踩过，C23-16）。
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = [0]
        self._clock = clock
        self.home_cell = home_cell
        self.source = source
        self.namespace = namespace
        self.task_queue = task_queue
        self.workflow_type = workflow_type
        self.max_nodes_per_episode = max_nodes_per_episode

        self.fence = fence
        self.effective_policy = effective_policy
        self.read_manifest = read_manifest
        self.query_jobs = query_jobs
        self.cancel_job = cancel_job
        self.start_workflow = start_workflow

        self.outbox = Outbox(conn)
        self.inbox = Inbox(conn)

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return self._clock()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:24]}"

    def _require(self, port: Callable | None, name: str) -> Callable:
        if port is None:
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                f"dependency port {name!r} is not wired", retryable=True,
                details={"port": name})
        return port

    # ------------------------------------------------------------ 创建 Run

    def create_run(self, tenant_id: str, episode_id: str, body: dict, *,
                   correlation_id: str) -> dict:
        """§3.1。三层幂等由调用方（HTTP 层）与这里分工：

        - 命令层在 HTTP 层（`idempotency_sdk`）。
        - 业务层与编排层在这里，两者同一个事务落库。
        """
        context = body.get("context")
        if not isinstance(context, dict):
            raise ServiceError(400, "INVALID_CONTEXT", "`context` must be an object")
        self._validate_context(context)

        # C21-10：创建 Run 是**集级**操作，`context.node_id`/`node_epoch` 是单节点
        # 语义。这里只做 fence 校验，节点的 epoch 由 workflow 派单时重新组装。
        if context["episode_id"] != episode_id:
            raise ServiceError(
                400, "INVALID_CONTEXT",
                "context.episode_id does not match the path episode_id",
                details={"path_episode_id": episode_id,
                         "context_episode_id": context["episode_id"]})

        fence_at_start = context["execution_fence"]
        manifest_ref = context["manifest_ref"]

        # §3.1 步骤 3：manifest 的存在性与当前 fence 由 S08 复查。
        read_manifest = self._require(self.read_manifest, "read_manifest")
        manifest = read_manifest(tenant_id, manifest_ref["id"])
        current_fence = int(manifest["execution_fence"])
        if current_fence != fence_at_start:
            raise ServiceError(
                409, "STALE_EXECUTION_FENCE",
                "the execution fence in the request is stale", retryable=True,
                details={"requested_fence": fence_at_start,
                         "current_fence": current_fence})
        if int(manifest["revision"]) != int(manifest_ref["revision"]):
            # 引用了一版不存在的 revision：请求自身不可行，422 不是 409。
            raise ServiceError(
                422, "DEPENDENCY_PRECHECK_FAILED",
                "manifest revision does not exist", retryable=False,
                details={"requested_revision": manifest_ref["revision"],
                         "current_revision": manifest["revision"]})

        # §9 C21-8 / 上限：镜头数超限是"请求本身不可行"（422）。
        node_count = int(manifest.get("node_count") or 0)
        if node_count > self.max_nodes_per_episode:
            raise ServiceError(
                422, "EPISODE_TOO_LARGE",
                f"{node_count} nodes exceeds the limit "
                f"{self.max_nodes_per_episode}",
                details={"node_count": node_count,
                         "max_nodes_per_episode": self.max_nodes_per_episode})

        # §3.1 步骤 4：gate 来自 S02，不来自 S21。
        effective_policy = self._require(self.effective_policy, "effective_policy")
        policy = effective_policy(tenant_id, {
            "project_id": context["project_id"],
            "series_id": context["series_id"],
            "episode_id": context["episode_id"],
            "policy_ref": context["policy_ref"],
        })
        automation_mode = policy["automation_mode"]
        generation_mode = policy["generation_mode"]

        intent = body.get("intent") or DEFAULT_INTENT
        budget_scope_id = body["budget_scope_id"]
        business_idem_key = business_idem_key_for(
            tenant_id=tenant_id,
            series_id=context["series_id"],
            branch_id=context["branch_id"],
            episode_id=context["episode_id"],
            base_manifest_ref=manifest_ref,
            automation_mode=automation_mode,
            policy_revision=context["policy_ref"]["revision"],
            budget_scope_id=budget_scope_id,
            intent=intent,
        )

        # 编排层的 generation 必须递增，不能恒为 1（C21-18）。
        #
        # §6.1 说"取消后重建"靠业务层的部分索引解决，但 `business_idem_key`
        # 跨重建不变，于是 `workflow_id` 也不变：既会撞 `wb_workflow_uniq`，
        # 又会让 Temporal 对同 id 的 start 返回**那个已终结的句柄**——于是新
        # Run 根本没启动，而 S21 以为启动了。这正是 AGENTS.md 里"盲目重提"
        # 的镜像。所以 generation 取同键历史最大值 + 1。
        run_generation = self._next_generation(tenant_id, business_idem_key)
        workflow_id = workflow_id_for(
            self.workflow_type, tenant_id, business_idem_key, run_generation)

        # §3.1 步骤 6：binding + projection + outbox，同一事务。
        with Transaction(self.conn, self._tx_depth):
            existing = self._active_binding(tenant_id, business_idem_key)
            if existing is not None:
                # T21-42：同意图已有 ACTIVE Run。返回原 Run 而不是再建一个。
                # 契约上没有 `RUN_ALREADY_ACTIVE` 这个码（§5.7 没有，T21-42 提了）
                # ——登记为 C21-13，这里选"返回原 Run"这一支。
                run_id = existing["run_id"]
                needs_start = self._needs_start(existing)
            else:
                run_id = self._new_id("run")
                needs_start = generation_mode != "manual"
                self._insert_binding(
                    tenant_id, run_id, workflow_id, run_generation,
                    business_idem_key, context, manifest_ref, fence_at_start,
                    automation_mode, budget_scope_id, generation_mode,
                    correlation_id=correlation_id)

            binding = self._binding_row(tenant_id, run_id)

        # §3.1 步骤 7：事务提交后才启动 workflow。反过来会产生"有 Run 但没绑定"
        # 的孤儿；这一顺序失败只需重试 `start_workflow`（workflow_id 确定性）。
        if needs_start:
            self._start_workflow_once(tenant_id, binding, manifest_ref,
                                      run_generation, business_idem_key,
                                      automation_mode, fence_at_start)
            binding = self._binding_row(tenant_id, run_id)

        return self._operation_for(binding, replayed=False)

    def _needs_start(self, binding: dict) -> bool:
        """既有 ACTIVE Run 是否还没被启动过（C21-21）。

        `generation_mode=manual` 的 Run 永远不启动 workflow，它在
        `waiting_reason` 上是可辨识的；其余 ACTIVE 但没有
        `workflow_started_at` 的，正卡在 §3.1 步骤 6/7 之间的窗口里。
        """
        if binding["workflow_started_at"] is not None:
            return False
        projection = self._projection_row(binding["tenant_id"], binding["run_id"])
        return not (projection and projection["waiting_reason"] == WAITING_REASON_MANUAL)

    def _insert_binding(self, tenant_id: str, run_id: str, workflow_id: str,
                        run_generation: int, business_idem_key: str, context: dict,
                        manifest_ref: dict, fence_at_start: int,
                        automation_mode: str, budget_scope_id: str,
                        generation_mode: str, *, correlation_id: str) -> None:
        """§3.1 步骤 6。binding + projection + outbox，同一事务。

        `workflow_started_at` 留 NULL：步骤 7 成功后才写（C21-21）。
        """
        now = self._now()
        # generation_mode=manual：不启动 workflow，但**仍然创建** binding
        # 与 projection，落到 WAITING_INPUT。这样"等人工"是可见的（§3.1）。
        initial_state = ("WAITING_INPUT" if generation_mode == "manual"
                         else "QUEUED")
        waiting_reason = WAITING_REASON_MANUAL if generation_mode == "manual" else None

        self.conn.execute(
            "INSERT INTO workflow_binding (tenant_id, run_id, workflow_id,"
            " run_generation, namespace, task_queue, workflow_type,"
            " business_idem_key, series_id, episode_id, branch_id, manifest_id,"
            " manifest_revision_at_start, execution_fence_at_start,"
            " automation_mode, policy_ref, budget_scope_id, status, created_at,"
            " workflow_started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,NULL)",
            (tenant_id, run_id, workflow_id, run_generation, self.namespace,
             self.task_queue, self.workflow_type, business_idem_key,
             context["series_id"], context["episode_id"], context["branch_id"],
             manifest_ref["id"], int(manifest_ref["revision"]), fence_at_start,
             automation_mode, _dumps(context["policy_ref"]), budget_scope_id,
             now))

        self.conn.execute(
            "INSERT INTO run_projection (tenant_id, run_id, state,"
            " manifest_revision, execution_fence, completed_nodes,"
            " blocked_nodes, failed_nodes, waiting_reason, blocked_reason,"
            " current_stage, projection_watermark, last_event_at, terminal_at,"
            " version) VALUES (?,?,?,?,?,0,0,0,?,NULL,NULL,1,?,NULL,1)",
            (tenant_id, run_id, initial_state, int(manifest_ref["revision"]),
             fence_at_start, waiting_reason, now))

        self._emit_state(tenant_id, run_id, previous_state=None,
                         correlation_id=correlation_id,
                         causation_id=correlation_id)

    def _start_workflow_once(self, tenant_id: str, binding: dict,
                             manifest_ref: dict, run_generation: int,
                             business_idem_key: str, automation_mode: str,
                             fence_at_start: int) -> None:
        """§3.1 步骤 7。**成功后才写 `workflow_started_at`**（C21-21）。

        方案说"启动失败只需重试 `start_workflow`"，但重试走的是同一个
        `POST`，命中业务层幂等就返回原 Run 了——不记下启动过没有，workflow
        永远不会被启动，Run 永远停在 QUEUED。所以这里既补启又要标记。

        端口失败映射成 503 `TEMPORAL_UNAVAILABLE`（§5.7 有这一条，
        `retryable=true`），而不是让它冒成 500。
        """
        starter = self.start_workflow
        if starter is None:
            # 未接线：Run 已落库，workflow 尚未启动。不标记，下次重试会补启。
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "dependency port 'start_workflow' is not wired", retryable=True,
                details={"port": "start_workflow"})
        try:
            starter(binding["workflow_id"], {
                "run_id": binding["run_id"],
                "tenant_id": tenant_id,
                "run_generation": run_generation,
                "business_idem_key": business_idem_key,
                "namespace": self.namespace,
                "task_queue": self.task_queue,
                "workflow_type": self.workflow_type,
                "manifest_ref": manifest_ref,
                "execution_fence": fence_at_start,
                "automation_mode": automation_mode,
            })
        except ServiceError:
            raise
        except Exception as exc:      # 传输层/客户端异常：可重试
            raise ServiceError(
                503, "TEMPORAL_UNAVAILABLE",
                f"starting the workflow failed: {exc}", retryable=True,
                details={"workflow_id": binding["workflow_id"]}) from exc

        self.conn.execute(
            "UPDATE workflow_binding SET workflow_started_at = ?"
            " WHERE tenant_id = ? AND run_id = ? AND workflow_started_at IS NULL",
            (self._now(), tenant_id, binding["run_id"]))

    def _validate_context(self, context: dict) -> None:
        """`execution-context.v2` 的 18 个 required 全在（§3.1 步骤 3）。

        只校验形状，不校验业务存在性——那是 S08 / S02 的事。
        """
        missing = [k for k in (
            "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
            "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
            "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref",
            "actor", "command_id", "request_hash", "trace_id") if k not in context]
        if missing:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "execution context is missing required fields",
                               details={"missing": missing})
        if context["schema_version"] != "2.0":
            raise ServiceError(400, "INVALID_CONTEXT",
                               "unsupported context schema_version",
                               details={"schema_version": context["schema_version"]})
        for key in ("manifest_ref", "policy_ref", "actor", "memory_checkpoint"):
            if not isinstance(context[key], dict):
                raise ServiceError(400, "INVALID_CONTEXT",
                                   f"`{key}` must be an object")
        # fence 是 run/branch 级的正整数；0 从来不是合法值。
        fence = context["execution_fence"]
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "`execution_fence` must be an integer >= 1")
        revision = context["manifest_ref"].get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "`manifest_ref.revision` must be an integer >= 1")

    # ----------------------------------------------------------- Run 命令

    def command_run(self, tenant_id: str, run_id: str, action: str, *,
                    correlation_id: str) -> dict:
        """§4.1 / §4.2 / §4.3。`action ∈ {pause, resume, cancel}`。"""
        if action not in ("pause", "resume", "cancel"):
            raise ServiceError(400, "INVALID_CONTEXT",
                               f"unknown command action {action!r}",
                               details={"action": action})

        projection = self._projection_row(tenant_id, run_id)
        if projection is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")

        state = projection["state"]
        if state in TERMINAL_STATES:
            # §5.7：对终态 Run 发命令是 409 RUN_NOT_ACTIVE，不是 404。
            raise ServiceError(409, "RUN_NOT_ACTIVE",
                               f"run is in terminal state {state}",
                               details={"state": state})
        if action == "pause" and state == "PAUSED":
            raise ServiceError(409, "ILLEGAL_STATE_TRANSITION",
                               "run is already paused", details={"state": state})
        if action == "resume" and state != "PAUSED":
            raise ServiceError(409, "ILLEGAL_STATE_TRANSITION",
                               "run is not paused", details={"state": state})

        if action == "cancel":
            return self._cancel(tenant_id, run_id, correlation_id=correlation_id)
        if action == "pause":
            return self._pause(tenant_id, run_id, correlation_id=correlation_id)
        return self._resume(tenant_id, run_id, correlation_id=correlation_id)

    def _pause(self, tenant_id: str, run_id: str, *, correlation_id: str) -> dict:
        """§4.1：不中止在途 Activity，不调 S08 / S23 / S22。"""
        with Transaction(self.conn, self._tx_depth):
            row = self._projection_row(tenant_id, run_id)
            # 幂等/非法转换要在事务内再看一次：两个并发 pause 只有一个能过。
            if row["state"] == "PAUSED":
                raise ServiceError(409, "ILLEGAL_STATE_TRANSITION",
                                   "run is already paused",
                                   details={"state": row["state"]})
            self._record_command(tenant_id, "pause", run_id, correlation_id)
            new_state = "PAUSED"
            self._record_state(tenant_id, run_id, new_state,
                               expected_watermark=row["projection_watermark"],
                               terminal=False, correlation_id=correlation_id)
        return self._operation_for(self._binding_row(tenant_id, run_id),
                                   replayed=False)

    def _resume(self, tenant_id: str, run_id: str, *, correlation_id: str) -> dict:
        """§4.3：**恢复的第一个动作必须是读**，不是写。

        先查 S26 的在途 job 现状；终态的推进、在跑的重新挂等、UNKNOWN 不动。
        跳过这一步就会重新派单，造成重复付费与重复产物。
        """
        with Transaction(self.conn, self._tx_depth):
            row = self._projection_row(tenant_id, run_id)
            if row["state"] != "PAUSED":
                raise ServiceError(409, "ILLEGAL_STATE_TRANSITION",
                                   "run is not paused",
                                   details={"state": row["state"]})
            self._record_command(tenant_id, "resume", run_id, correlation_id)

        # 读在事务外：它是外部调用，不该占着写锁。
        jobs = self._probe_in_flight(tenant_id, run_id)

        with Transaction(self.conn, self._tx_depth):
            row = self._projection_row(tenant_id, run_id)
            for job in jobs:
                if job["verdict"] == "TERMINAL":
                    # 已经结束的任务直接推进，**不重新生成**。
                    self._settle_node(tenant_id, run_id, job["node_id"],
                                      job["node_epoch"])
                elif job["verdict"] == "UNKNOWN":
                    # §4.7：保持挂起，不重提。Run 回到 WAITING_BUDGET。
                    self._set_blocker(tenant_id, run_id, job["node_id"],
                                      job["node_epoch"], job.get("stage"),
                                      BLOCKED_REASON_DISPATCH_UNKNOWN,
                                      attempt_id=job.get("attempt_id"))
                # RUNNING：不动，仍是 node_in_flight 的开项。
                self._store_job_state(tenant_id, run_id, job["node_id"],
                                      job["node_epoch"], job.get("job_state"))

            has_unknown = any(j["verdict"] == "UNKNOWN" for j in jobs)
            new_state = "WAITING_BUDGET" if has_unknown else "RUNNING"
            self._record_state(
                tenant_id, run_id, new_state,
                expected_watermark=row["projection_watermark"],
                terminal=False, correlation_id=correlation_id,
                waiting_reason=(WAITING_REASON_SUBMISSION_UNKNOWN
                                if has_unknown else None))
        return self._operation_for(self._binding_row(tenant_id, run_id),
                                   replayed=False)

    def _probe_in_flight(self, tenant_id: str, run_id: str) -> list[dict]:
        """§4.3 步骤 2：对每个在途引用问 S26。"""
        rows = self.conn.execute(
            "SELECT node_id, node_epoch, stage, attempt_id, provider_job_id,"
            " job_state FROM node_in_flight"
            " WHERE tenant_id = ? AND run_id = ? AND settled_at IS NULL"
            " ORDER BY stage, node_id, node_epoch",
            (tenant_id, run_id)).fetchall()
        if not rows:
            return []
        query = self._require(self.query_jobs, "query_jobs")
        out = []
        for row in rows:
            if not row["provider_job_id"] and not row["attempt_id"]:
                # 还没派单：没有可查的东西，保持原样。
                continue
            answer = query(tenant_id, {
                "run_id": run_id,
                "node_id": row["node_id"],
                "node_epoch": row["node_epoch"],
                "attempt_id": row["attempt_id"],
                "provider_job_id": row["provider_job_id"],
            })
            job_state = answer.get("state")
            if job_state in NOT_CANCELLABLE_JOB_STATES:
                verdict = "UNKNOWN"
            elif answer.get("terminal"):
                verdict = "TERMINAL"
            else:
                verdict = "RUNNING"
            out.append({**dict(row), "verdict": verdict, "job_state": job_state})
        return out

    def _cancel(self, tenant_id: str, run_id: str, *, correlation_id: str) -> dict:
        """§4.2。**fence 先于 cancel**，顺序不能反（T21-26）。

        步骤 2 的 fence 必须在写 CANCELED 之前完成：否则会出现"workflow 已
        取消但 S08 还接受提交"的窗口，迟到的 `:commit` 会把已取消 Run 的产物
        写进 manifest。
        """
        binding = self._binding_row(tenant_id, run_id)
        fence_at_start = int(binding["execution_fence_at_start"])

        # 1) 先 fence。S08 是 execution_fence 的唯一写者（§0.3）。
        fence_port = self._require(self.fence, "fence")
        fence_answer = fence_port(
            tenant_id,
            {
                "series_id": binding["series_id"],
                "branch_id": binding["branch_id"],
                "command_id": correlation_id,
                "expected_fence": fence_at_start,
                "reason": "RUN_CANCELED",
                "fence_ref": run_id,
            },
        )
        new_fence = int(fence_answer["execution_fence"])

        # 2) 再取消可取消的在途工作。
        dispositions = self._cancel_in_flight(tenant_id, run_id)

        # 3) 最后落 CANCELED。§4.2：cancel 不退款，已提交任务照常结算。
        with Transaction(self.conn, self._tx_depth):
            row = self._projection_row(tenant_id, run_id)
            if row["state"] in TERMINAL_STATES:
                # 迟到的第二个 cancel：终态不倒退（T21-25）。
                return self._operation_for(binding, replayed=False)
            self._record_command(tenant_id, "cancel", run_id, correlation_id)
            self.conn.execute(
                "UPDATE workflow_binding SET status = 'TERMINATED', closed_at = ?"
                " WHERE tenant_id = ? AND run_id = ? AND status = 'ACTIVE'",
                (self._now(), tenant_id, run_id))
            self._record_state(tenant_id, run_id, "CANCELED",
                               expected_watermark=row["projection_watermark"],
                               terminal=True, correlation_id=correlation_id,
                               execution_fence=new_fence)
            result = self._operation_for(self._binding_row(tenant_id, run_id),
                                         replayed=False)
        return {**result, "dispositions": dispositions}

    def _cancel_in_flight(self, tenant_id: str, run_id: str) -> list[dict]:
        """§4.2 步骤 3 / §4.5 分类表。

        `SUBMISSION_UNKNOWN` **不调取消接口**——取消一个 UNKNOWN 的 job 会让
        "钱可能已经花了但没人跟踪"变成现实。标 `DISCARD_ON_COMPLETION`。
        """
        rows = self.conn.execute(
            "SELECT node_id, node_epoch, stage, attempt_id, provider_job_id,"
            " job_state FROM node_in_flight"
            " WHERE tenant_id = ? AND run_id = ? AND settled_at IS NULL"
            " ORDER BY stage, node_id, node_epoch",
            (tenant_id, run_id)).fetchall()
        out = []
        for row in rows:
            job_state = self._known_job_state(tenant_id, run_id, row)
            # `None` = 问不到。问不到就不取消（§4.5「S21 不猜」）。
            if job_state is None or job_state in NOT_CANCELLABLE_JOB_STATES:
                out.append(self._set_disposition(
                    tenant_id, run_id, row, "DISCARD_ON_COMPLETION",
                    reason=job_state or "STATE_UNKNOWN"))
                continue
            if not row["provider_job_id"]:
                # 尚未派单到 S26：取消 Activity 即可，成本 0。
                out.append(self._set_disposition(
                    tenant_id, run_id, row, "CANCELED", reason="NOT_DISPATCHED"))
                continue
            cancel = self._require(self.cancel_job, "cancel_job")
            answer = cancel(tenant_id, {
                "run_id": run_id,
                "node_id": row["node_id"],
                "node_epoch": row["node_epoch"],
                "attempt_id": row["attempt_id"],
                "provider_job_id": row["provider_job_id"],
            })
            verdict = answer.get("result")
            if verdict == "cancelled":
                out.append(self._set_disposition(
                    tenant_id, run_id, row, "CANCELED", reason="PROVIDER_CANCELED"))
            else:
                # `not_cancellable` 与 `unknown` 都按 discard_on_completion
                # 处理——与 S26 的 UNKNOWN 铁律一致（§4.2 末段）。
                out.append(self._set_disposition(
                    tenant_id, run_id, row, "DISCARD_ON_COMPLETION",
                    reason=verdict or "unknown"))
        return out

    def _known_job_state(self, tenant_id: str, run_id: str, row) -> str | None:
        """取消前的状态判定：**先问 S26，问不到就返回 None**。

        本地 `job_state` 只是快照，可能从没被写过。§4.5 说 S21 不猜，所以
        不能把"没记录"当成"可以取消"——那会把一个可能已经提交成功的付费
        任务取消掉，正好是 §4.5 与 F-05 要防的后果。查不到时按 ENOTKNOWN
        处理：不调取消接口，标 discard。
        """
        if row["job_state"] in NOT_CANCELLABLE_JOB_STATES:
            return row["job_state"]
        if self.query_jobs is None:
            return row["job_state"]
        if not row["provider_job_id"] and not row["attempt_id"]:
            return row["job_state"]
        answer = self.query_jobs(tenant_id, {
            "run_id": run_id,
            "node_id": row["node_id"],
            "node_epoch": row["node_epoch"],
            "attempt_id": row["attempt_id"],
            "provider_job_id": row["provider_job_id"],
        })
        return answer.get("state")

    def _set_disposition(self, tenant_id: str, run_id: str, row, disposition: str,
                         *, reason: str) -> dict:
        with Transaction(self.conn, self._tx_depth):
            self.conn.execute(
                "UPDATE node_in_flight SET disposition = ?"
                " WHERE tenant_id = ? AND run_id = ? AND node_id = ?"
                " AND node_epoch = ? AND settled_at IS NULL",
                (disposition, tenant_id, run_id, row["node_id"],
                 row["node_epoch"]))
        return {"node_id": row["node_id"], "node_epoch": row["node_epoch"],
                "disposition": disposition, "reason": reason}

    def _record_command(self, tenant_id: str, action: str, run_id: str,
                        correlation_id: str) -> None:
        """§1.6 的命令流水。**调用方必须在事务内。**

        `command_id` 与幂等 SDK 的键不同：SDK 管"同一次请求重试"，
        `run_command` 管"这条命令的语义结局"。两表同事务写（C21-16）。
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO run_command (tenant_id, command_id, run_id,"
            " action, request_hash, state, result, created_at, applied_at)"
            " VALUES (?,?,?,?,?,'APPLIED',NULL,?,?)",
            (tenant_id, correlation_id, run_id, action, correlation_id,
             self._now(), self._now()))

    # --------------------------------------------------------- 事件消费

    def consume_manifest_committed(self, event: dict, *, consumer: str = "s21.manifest",
                                   correlation_id: str = "") -> dict:
        """§3.3 / §4.4。**这里是两套 epoch 的分界线。**

        - `changed_node_epochs` → 只取消 `node_epoch == from` 的那一项。
        - `from == to`（epoch 没动）→ **不取消**（SMC-5 的守卫）。
        - `trigger == FENCE_VOID` 或 fence 前进 → 整 Run 响应 fence，推向 CANCELED。
          **即使是这种，S21 也是"响应 fence"，不是"发起 fence"**（§4.4）：
          它不再调 S08 `:fence`，只接受 S08 已经写好的新值。
        """
        event_id = event["id"]
        if not self.inbox.consume(consumer, event_id):
            return {"duplicate": True, "event_id": event_id}

        tenant_id = event["tenant_id"]
        data = event["data"]
        manifest_id = data["manifest_ref"]["id"]
        run_id, binding = self._run_for_manifest(tenant_id, manifest_id)
        if run_id is None:
            # 不是本 Run 的 manifest（或属于别的分支）。at-least-once 下正常。
            return {"ignored": True, "reason": "no_active_run_for_manifest",
                    "manifest_id": manifest_id}

        event_fence = int(data["execution_fence"])
        binding_fence = int(binding["execution_fence_at_start"])
        body = {"trigger": data["trigger"]}

        # §4.4 末段：fence 自己前进了 —— 这是 execution_fence 语义，
        # 不是 node_epoch。整 Run 的候选作废。
        if data["trigger"] == "FENCE_VOID" or event_fence > binding_fence:
            projection = self._projection_row(tenant_id, run_id)
            if projection["state"] in TERMINAL_STATES:
                return {"run_id": run_id, "state": projection["state"],
                        "ignored": True, "reason": "terminal"}
            with Transaction(self.conn, self._tx_depth):
                self._record_state(
                    tenant_id, run_id, "CANCELED",
                    expected_watermark=projection["projection_watermark"],
                    terminal=True, correlation_id=correlation_id,
                    execution_fence=event_fence)
                self.conn.execute(
                    "UPDATE workflow_binding SET status = 'TERMINATED',"
                    " closed_at = ? WHERE tenant_id = ? AND run_id = ?"
                    " AND status = 'ACTIVE'",
                    (self._now(), tenant_id, run_id))
            body.update({"state": "CANCELED", "reason": "FENCE_ADVANCED",
                         "execution_fence": event_fence})
            return body

        changed = data.get("changed_node_epochs") or []
        applied = [self._apply_node_epoch_change(tenant_id, run_id, item,
                                                 correlation_id=correlation_id)
                   for item in changed]

        # §3.3 步骤 3：只有 accepted_pointers_delta、没有 epoch 变化 →
        # **不启动任何子图**（只是指针变了，没有新的生产工作）。
        delta = data.get("accepted_pointers_delta") or []
        body.update({
            "run_id": run_id,
            "changed": applied,
            "accepted_pointers": len(delta),
            "subgraph": bool(changed),
        })
        return body

    def _apply_node_epoch_change(self, tenant_id: str, run_id: str, item: dict,
                                 *, correlation_id: str) -> dict:
        """§4.4。**不能把 node_epoch 当 execution_fence 用。**

        `from == to` 说明输入指纹没变、epoch 没动，在途任务**仍然有效**——
        按 §3.2 的规则它根本没有过期。取消它正是方案 §4.4 明文禁止的错误。
        """
        node_id = item["node_id"]
        from_epoch = int(item["from"])
        to_epoch = int(item["to"])
        if to_epoch == from_epoch:
            return {"node_id": node_id, "from": from_epoch, "to": to_epoch,
                    "cancelled": False, "reason": "EPOCH_UNCHANGED"}
        rows = self.conn.execute(
            "SELECT node_id, node_epoch, stage, attempt_id, provider_job_id,"
            " job_state FROM node_in_flight"
            " WHERE tenant_id = ? AND run_id = ? AND node_id = ?"
            " AND node_epoch = ? AND settled_at IS NULL",
            (tenant_id, run_id, node_id, from_epoch)).fetchall()
        if not rows:
            return {"node_id": node_id, "from": from_epoch, "to": to_epoch,
                    "cancelled": False, "reason": "NOT_IN_FLIGHT"}
        # 只处理这一项，`shot_3_4` 之类 epoch 没变的节点完全不受影响。
        dispositions = [self._set_disposition(
            tenant_id, run_id, row, "CANCELED", reason="NODE_EPOCH_CHANGED")
            for row in rows]
        return {"node_id": node_id, "from": from_epoch, "to": to_epoch,
                "cancelled": True, "dispositions": dispositions,
                "correlation_id": correlation_id}

    def consume_job_state_changed(self, event: dict, *,
                                  consumer: str = "s21.jobs",
                                  correlation_id: str = "") -> dict:
        """§5.6：按 `attempt_id` 反查 `node_in_flight` → `run_id`。"""
        event_id = event["id"]
        if not self.inbox.consume(consumer, event_id):
            return {"duplicate": True, "event_id": event_id}
        data = event["data"]
        tenant_id = event["tenant_id"]
        rows = self.conn.execute(
            "SELECT run_id, node_id, node_epoch, stage FROM node_in_flight"
            " WHERE tenant_id = ? AND attempt_id = ? AND settled_at IS NULL",
            (tenant_id, data["attempt_id"])).fetchall()
        if not rows:
            return {"ignored": True, "reason": "no_open_in_flight",
                    "attempt_id": data["attempt_id"]}
        touched = []
        for row in rows:
            run_id = row["run_id"]
            projected = self.conn.execute(
                "SELECT state, projection_watermark FROM run_projection"
                " WHERE tenant_id = ? AND run_id = ?",
                (tenant_id, run_id)).fetchone()
            if projected is None or projected["state"] in TERMINAL_STATES:
                continue
            self._store_job_state(tenant_id, run_id, row["node_id"],
                                  row["node_epoch"], data["state"])
            touched.append(run_id)
        return {"touched_runs": sorted(set(touched)),
                "attempt_id": data["attempt_id"], "state": data["state"]}

    def consume_permit_changed(self, event: dict, *, consumer: str = "s21.permits",
                               correlation_id: str = "") -> dict:
        """§5.6：permit 被外部释放/过期 → 影响该节点的判定。

        这里**不做预算或配额判断**（那是 S22 / S23 的事）。只把它记到
        `node_in_flight` 的旁证里，供 `/blockers` 解释"为什么停了"。
        """
        event_id = event["id"]
        if not self.inbox.consume(consumer, event_id):
            return {"duplicate": True, "event_id": event_id}
        data = event["data"]
        if data["state"] not in ("RELEASED", "EXPIRED"):
            return {"ignored": True, "reason": "permit_still_live",
                    "state": data["state"]}
        rows = self.conn.execute(
            "SELECT run_id, node_id, node_epoch, stage FROM node_in_flight"
            " WHERE tenant_id = ? AND attempt_id = ? AND settled_at IS NULL",
            (event["tenant_id"], data["attempt_id"])).fetchall()
        for row in rows:
            self._set_blocker(
                event["tenant_id"], row["run_id"], row["node_id"],
                row["node_epoch"], row["stage"],
                f"PERMIT_{data['state']}", attempt_id=data["attempt_id"],
                detail={"release_reason": data["release_reason"],
                        "permit_id": data["permit_id"]})
        return {"permit_id": data["permit_id"], "state": data["state"],
                "affected": len(rows)}

    def consume_change_plan_applied(self, event: dict, *,
                                    consumer: str = "s21.changes",
                                    correlation_id: str = "") -> dict:
        """§5.6：为 change_subgraph 准备子图执行范围（来自 S07 已批准的计划）。

        §5.6 末段：S21 **不消费** `lineage.impact.computed`。影响分析经
        ChangePlan 进来，否则 S21 会在计划还没被批准时就开始排程。
        """
        event_id = event["id"]
        if not self.inbox.consume(consumer, event_id):
            return {"duplicate": True, "event_id": event_id}
        data = event["data"]
        return {
            "plan_id": data["plan_id"],
            "regenerate_node_ids": data["regenerate_node_ids"],
            "recheck_node_ids": data["recheck_node_ids"],
            "reuse_artifact_refs": data["reuse_artifact_refs"],
            "changed_node_epochs": data["changed_node_epochs"],
            "execution_fence": data["execution_fence"],
        }

    def _run_for_manifest(self, tenant_id: str, manifest_id: str):
        row = self.conn.execute(
            "SELECT * FROM workflow_binding WHERE tenant_id = ? AND manifest_id = ?"
            " AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (tenant_id, manifest_id)).fetchone()
        if row is None:
            return None, None
        return row["run_id"], row

    # ------------------------------------------------------- 投影与事件发射

    def _record_state(self, tenant_id: str, run_id: str, state: str, *,
                      expected_watermark: int, terminal: bool,
                      correlation_id: str,
                      waiting_reason: str | None = None,
                      blocked_reason: str | None = None,
                      execution_fence: int | None = None) -> int | None:
        """§6.3 的 CAS。`projection_watermark + 1` 在 SQL 里做，不在调用方算。

        返回新的 watermark；返回 `None` 说明 CAS 落了 0 行——即"上一次其实
        已经成功"。Activity 重试时这就是正确的信号，不是错误。

        **调用方必须在事务内。**
        """
        if state not in RUN_STATES:
            raise ValueError(f"unknown run state {state!r}")
        if (state in TERMINAL_STATES) != bool(terminal):
            raise ValueError(
                f"`terminal` flag disagrees with state {state!r}")
        # rp_terminal_has_ts / rp_waiting_has_reason 是 DDL 约束；这里先给出
        # 可读的错误，而不是让 SQLite 抛 CHECK constraint failed。
        if state in WAITING_STATES and not waiting_reason:
            raise ValueError(f"state {state!r} requires a waiting_reason")
        if state not in WAITING_STATES and waiting_reason:
            raise ValueError(f"state {state!r} must not carry a waiting_reason")

        # 事件里 `previous_state` 是必填的 11 状态枚举，没有"无前态"这种取值
        # （C21-17）。所以先读出当前值再改。
        before = self._projection_row(tenant_id, run_id)
        previous_state = before["state"] if before is not None else "QUEUED"

        now = self._now()
        terminal_at = now if terminal else None
        fence_clause = "" if execution_fence is None else ", execution_fence = ?"
        params: list[Any] = [state, waiting_reason, blocked_reason,
                             terminal_at, now]
        if execution_fence is not None:
            params.append(int(execution_fence))
        params.extend([tenant_id, run_id, int(expected_watermark)])

        cur = self.conn.execute(
            "UPDATE run_projection SET"
            " state = ?, waiting_reason = ?, blocked_reason = ?,"
            " terminal_at = ?, last_event_at = ?,"
            " projection_watermark = projection_watermark + 1,"
            " version = version + 1"
            f"{fence_clause}"
            " WHERE tenant_id = ? AND run_id = ?"
            " AND projection_watermark = ?"
            " RETURNING projection_watermark",
            params)
        row = cur.fetchone()
        if row is None:
            return None
        watermark = int(row[0])
        self._emit_state(tenant_id, run_id, previous_state=previous_state,
                         correlation_id=correlation_id,
                         causation_id=correlation_id)
        return watermark

    def _emit_state(self, tenant_id: str, run_id: str, *,
                    previous_state: str | None, correlation_id: str,
                    causation_id: str | None) -> None:
        """§5.5。`data` 必须恰好是契约的 13 个字段。

        `aggregate_sequence = projection_watermark`——不是"第 n 次状态变更"。
        理由是投影每次写都会 +1（含计数更新），序列稠密无缺口，S47 的
        缺口检测才有意义。

        **必须在调用方的事务内调用**：Outbox 与投影变更同事务。
        """
        projection = self._projection_row(tenant_id, run_id)
        state = projection["state"]
        data = {
            "schema_version": "2.0",
            "run_id": run_id,
            "previous_state": previous_state,
            "state": state,
            "manifest_revision": int(projection["manifest_revision"]),
            "execution_fence": int(projection["execution_fence"]),
            "completed_nodes": int(projection["completed_nodes"]),
            "blocked_nodes": int(projection["blocked_nodes"]),
            "failed_nodes": int(projection["failed_nodes"]),
            "waiting_reason": projection["waiting_reason"],
            "blocked_reason": projection["blocked_reason"],
            # 契约要求非空字符串（minLength 1），而投影在第一个 stage 开始前
            # 没有真值。用 `ADMISSION` 作哨兵，并登记为 C21-17：取值集合是
            # x-needs-owner 的开放集，没有更好的选择。
            "current_stage": projection["current_stage"] or "ADMISSION",
            "terminal": state in TERMINAL_STATES,
        }
        envelope = new_event(
            source=self.source,
            type="orchestration.run.state-changed",
            subject=f"run/{run_id}",
            tenant_id=tenant_id,
            home_cell=self.home_cell,
            aggregate_id=run_id,
            sequence=int(projection["projection_watermark"]),
            correlation_id=correlation_id,
            causation_id=causation_id or correlation_id,
            data=data,
        )
        self.outbox.append(envelope)

    # ------------------------------------------------------------ 节点辅助

    def _store_job_state(self, tenant_id: str, run_id: str, node_id: str,
                         node_epoch: int, job_state: str | None) -> None:
        if job_state is None:
            return
        with Transaction(self.conn, self._tx_depth):
            self.conn.execute(
                "UPDATE node_in_flight SET job_state = ?"
                " WHERE tenant_id = ? AND run_id = ? AND node_id = ?"
                " AND node_epoch = ? AND settled_at IS NULL",
                (job_state, tenant_id, run_id, node_id, int(node_epoch)))

    def _settle_node(self, tenant_id: str, run_id: str, node_id: str,
                     node_epoch: int) -> None:
        with Transaction(self.conn, self._tx_depth):
            self.conn.execute(
                "UPDATE node_in_flight SET settled_at = ?"
                " WHERE tenant_id = ? AND run_id = ? AND node_id = ?"
                " AND node_epoch = ? AND settled_at IS NULL",
                (self._now(), tenant_id, run_id, node_id, int(node_epoch)))
            self.conn.execute(
                "UPDATE node_blocker SET resolved_at = ?"
                " WHERE tenant_id = ? AND run_id = ? AND node_id = ?"
                " AND node_epoch = ? AND resolved_at IS NULL",
                (self._now(), tenant_id, run_id, node_id, int(node_epoch)))

    def _set_blocker(self, tenant_id: str, run_id: str, node_id: str,
                     node_epoch: int, stage: str | None, reason: str, *,
                     attempt_id: str | None = None, suggestion: str | None = None,
                     detail: dict | None = None) -> None:
        with Transaction(self.conn, self._tx_depth):
            self.conn.execute(
                "INSERT INTO node_blocker (tenant_id, run_id, node_id, node_epoch,"
                " stage, blocked_reason, attempt_id, suggestion, detail, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (tenant_id, run_id, node_id, node_epoch,"
                " blocked_reason) DO UPDATE SET"
                " attempt_id = excluded.attempt_id,"
                " suggestion = excluded.suggestion, detail = excluded.detail",
                (tenant_id, run_id, node_id, int(node_epoch), stage, reason,
                 attempt_id, suggestion, _dumps(detail or {}), self._now()))
            # 投影的 blocked_nodes / failed_nodes 是计数，跟着事实走。
            counts = self.conn.execute(
                "SELECT COUNT(*) AS n FROM node_blocker"
                " WHERE tenant_id = ? AND run_id = ? AND resolved_at IS NULL",
                (tenant_id, run_id)).fetchone()
            self.conn.execute(
                "UPDATE run_projection SET blocked_nodes = ?"
                " WHERE tenant_id = ? AND run_id = ?",
                (int(counts["n"]), tenant_id, run_id))

    def register_in_flight(self, tenant_id: str, run_id: str, node_id: str,
                           node_epoch: int, stage: str, *,
                           provider_job_id: str | None = None,
                           attempt_id: str | None = None) -> dict:
        """派单时登记一项在途工作。`node_epoch`/`execution_fence` 存的是
        **派单那一刻的快照**，只用于事后解释"为什么这条被取消了"。
        """
        if node_epoch < 1:
            raise ServiceError(400, "INVALID_CONTEXT",
                               "node_epoch must be >= 1")
        binding = self._binding_row(tenant_id, run_id)
        with Transaction(self.conn, self._tx_depth):
            self.conn.execute(
                "INSERT INTO node_in_flight (tenant_id, run_id, node_id,"
                " node_epoch, execution_fence, stage, provider_job_id, attempt_id,"
                " job_state, disposition, dispatched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,NULL,?)"
                " ON CONFLICT (tenant_id, run_id, node_id, node_epoch) DO UPDATE SET"
                " provider_job_id = COALESCE(excluded.provider_job_id,"
                "   node_in_flight.provider_job_id),"
                " attempt_id = COALESCE(excluded.attempt_id,"
                "   node_in_flight.attempt_id),"
                " stage = excluded.stage,"
                " disposition = NULL, settled_at = NULL",
                (tenant_id, run_id, node_id, int(node_epoch),
                 int(binding["execution_fence_at_start"]), stage,
                 provider_job_id, attempt_id, None, self._now()))
            projection = self._projection_row(tenant_id, run_id)
            if projection["state"] == "QUEUED":
                # §2.6：QUEUED → RUNNING 的触发是"第一个可执行 Activity 开始"。
                self._record_state(
                    tenant_id, run_id, "RUNNING",
                    expected_watermark=projection["projection_watermark"],
                    terminal=False, correlation_id=run_id)
        return {"run_id": run_id, "node_id": node_id, "node_epoch": int(node_epoch)}

    def record_dispatch_unknown(self, tenant_id: str, run_id: str, node_id: str,
                                node_epoch: int, stage: str, *,
                                dispatch_request_id: str) -> dict:
        """§4.7 / F-05。**绝不重试**，记 dispatch_request_id 供事后查询。

        `dispatch_request_id` 由 S21 生成并持久化（作 `client_dispatch_id`），
        因为超时时拿不到 S26 生成的 `attempt_id`（C21-6）。
        """
        self.register_in_flight(tenant_id, run_id, node_id, node_epoch, stage,
                                attempt_id=dispatch_request_id)
        self._set_blocker(tenant_id, run_id, node_id, node_epoch, stage,
                          BLOCKED_REASON_DISPATCH_UNKNOWN,
                          attempt_id=dispatch_request_id)
        with Transaction(self.conn, self._tx_depth):
            projection = self._projection_row(tenant_id, run_id)
            if projection["state"] not in TERMINAL_STATES:
                self._record_state(
                    tenant_id, run_id, "WAITING_BUDGET",
                    expected_watermark=projection["projection_watermark"],
                    terminal=False, correlation_id=run_id,
                    waiting_reason=WAITING_REASON_SUBMISSION_UNKNOWN)
        return {"run_id": run_id, "node_id": node_id,
                "blocked_reason": BLOCKED_REASON_DISPATCH_UNKNOWN,
                "waiting_reason": WAITING_REASON_SUBMISSION_UNKNOWN,
                "attempt_id": dispatch_request_id}

    # ------------------------------------------------------------- 查询

    def get_run(self, tenant_id: str, run_id: str) -> dict:
        """§5.2：**恰好 7 个字段**（openapi `Run`，additionalProperties:false）。

        `failed_nodes` / `waiting_reason` / `current_stage` 在内部有，但
        **不能**进这个响应——它们走 §5.4 的补充查询。
        """
        row = self._projection_row(tenant_id, run_id)
        if row is None:
            # T21-47：跨租户与不存在同码 404，不泄漏存在性。
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")
        return {
            "run_id": row["run_id"],
            "manifest_revision": int(row["manifest_revision"]),
            "execution_fence": int(row["execution_fence"]),
            "state": row["state"],
            "completed_nodes": int(row["completed_nodes"]),
            "blocked_nodes": int(row["blocked_nodes"]),
            "projection_watermark": int(row["projection_watermark"]),
        }

    def list_runs(self, tenant_id: str, *, episode_id: str | None = None,
                  branch_id: str | None = None, state: str | None = None,
                  after: str | None = None, limit: int = 100) -> dict:
        """§5.4 的列表查询。键集分页，边界是 `(last_event_at, run_id)`。"""
        clauses = ["p.tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if episode_id:
            clauses.append("b.episode_id = ?")
            params.append(episode_id)
        if branch_id:
            clauses.append("b.branch_id = ?")
            params.append(branch_id)
        if state:
            clauses.append("p.state = ?")
            params.append(state)
        if after:
            clauses.append("p.last_event_at < ?")
            params.append(after)
        params.append(limit)
        rows = self.conn.execute(
            "SELECT p.*, b.episode_id, b.branch_id, b.series_id"
            " FROM run_projection p"
            " JOIN workflow_binding b ON b.tenant_id = p.tenant_id"
            " AND b.run_id = p.run_id"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY p.last_event_at DESC, p.run_id DESC LIMIT ?",
            params).fetchall()
        return {"items": [{
            "run_id": r["run_id"],
            "episode_id": r["episode_id"],
            "series_id": r["series_id"],
            "branch_id": r["branch_id"],
            "state": r["state"],
            "manifest_revision": int(r["manifest_revision"]),
            "execution_fence": int(r["execution_fence"]),
            "completed_nodes": int(r["completed_nodes"]),
            "blocked_nodes": int(r["blocked_nodes"]),
            "failed_nodes": int(r["failed_nodes"]),
            "projection_watermark": int(r["projection_watermark"]),
            "last_event_at": r["last_event_at"],
        } for r in rows]}

    def list_nodes(self, tenant_id: str, run_id: str, *, state: str | None = None,
                   stage: str | None = None, after: str | None = None,
                   limit: int = 200) -> dict:
        """§5.4 节点级进度（含 `node_epoch`、`blocked_reason`、`attempt_id`）。"""
        if self._projection_row(tenant_id, run_id) is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")
        clauses = ["n.tenant_id = ?", "n.run_id = ?"]
        params: list[Any] = [tenant_id, run_id]
        if stage:
            clauses.append("n.stage = ?")
            params.append(stage)
        if after:
            clauses.append("n.node_id > ?")
            params.append(after)
        # `state` 在 `node_in_flight` 上是"开/关 + disposition"，映射到 SQL。
        if state == "OPEN":
            clauses.append("n.settled_at IS NULL")
        elif state == "SETTLED":
            clauses.append("n.settled_at IS NOT NULL")
        elif state == "CANCELED":
            clauses.append("n.disposition = 'CANCELED'")
        elif state == "DISCARD_ON_COMPLETION":
            clauses.append("n.disposition = 'DISCARD_ON_COMPLETION'")
        params.append(limit)
        rows = self.conn.execute(
            "SELECT n.*, (SELECT blocked_reason FROM node_blocker bl"
            "  WHERE bl.tenant_id = n.tenant_id AND bl.run_id = n.run_id"
            "  AND bl.node_id = n.node_id AND bl.node_epoch = n.node_epoch"
            "  AND bl.resolved_at IS NULL LIMIT 1) AS open_blocked_reason"
            " FROM node_in_flight n"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY n.stage, n.node_id, n.node_epoch LIMIT ?",
            params).fetchall()
        return {"items": [{
            "node_id": r["node_id"],
            "node_epoch": int(r["node_epoch"]),
            "stage": r["stage"],
            "execution_fence": int(r["execution_fence"]),
            "provider_job_id": r["provider_job_id"],
            "attempt_id": r["attempt_id"],
            "job_state": r["job_state"],
            "disposition": r["disposition"],
            "blocked_reason": r["open_blocked_reason"],
            "settled": r["settled_at"] is not None,
        } for r in rows]}

    def list_blockers(self, tenant_id: str, run_id: str) -> dict:
        """§5.4 / E06 失败报告的数据源（T21-02）。"""
        if self._projection_row(tenant_id, run_id) is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")
        rows = self.conn.execute(
            "SELECT * FROM node_blocker WHERE tenant_id = ? AND run_id = ?"
            " AND resolved_at IS NULL ORDER BY stage, node_id, node_epoch",
            (tenant_id, run_id)).fetchall()
        return {"run_id": run_id, "items": [{
            "node_id": r["node_id"],
            "node_epoch": int(r["node_epoch"]),
            "stage": r["stage"],
            "blocked_reason": r["blocked_reason"],
            "attempt_id": r["attempt_id"],
            "suggestion": r["suggestion"],
            "detail": _loads(r["detail"]) or {},
        } for r in rows]}

    def get_repair_budget(self, tenant_id: str, run_id: str) -> dict:
        """§5.4 / C21-7（S39 C6）。

        `ceiling_micro` 是 S23 的只读复制，只做本地预检。**S21 不把估算当
        拒绝依据**——S39 拿到后仍要问 S23。
        """
        projection = self._projection_row(tenant_id, run_id)
        if projection is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")
        row = self.conn.execute(
            "SELECT * FROM repair_budget WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, run_id)).fetchone()
        if row is None:
            # 没有额度记录 ≠ 有无限额度。返回零额度，让 S39 走"问 S23"。
            return {
                "run_id": run_id,
                "remaining_business_attempts": 0,
                "remaining_recheck_rounds": 0,
                "budget_exhausted": True,
                "ceiling_micro": None,
                "spent_micro": None,
                "source_watermark": int(projection["projection_watermark"]),
            }
        remaining_attempts = max(
            0, int(row["max_business_attempts"]) - int(row["used_business_attempts"]))
        remaining_rechecks = max(
            0, int(row["max_recheck_rounds"]) - int(row["used_recheck_rounds"]))
        return {
            "run_id": run_id,
            "remaining_business_attempts": remaining_attempts,
            "remaining_recheck_rounds": remaining_rechecks,
            "budget_exhausted": remaining_attempts == 0,
            "ceiling_micro": row["ceiling_micro"],
            "spent_micro": None,
            "source_watermark": int(projection["projection_watermark"]),
        }

    def seed_repair_budget(self, tenant_id: str, run_id: str, *,
                           max_business_attempts: int, max_recheck_rounds: int,
                           ceiling_micro: int | None = None) -> dict:
        """Run 创建时按 S02 的 `repair_limits` 落一条起始额度。"""
        if self._binding_row(tenant_id, run_id) is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")
        with Transaction(self.conn, self._tx_depth):
            self.conn.execute(
                "INSERT INTO repair_budget (tenant_id, run_id,"
                " max_business_attempts, used_business_attempts,"
                " max_recheck_rounds, used_recheck_rounds, ceiling_micro,"
                " updated_at) VALUES (?,?,?,0,?,0,?,?)"
                " ON CONFLICT (tenant_id, run_id) DO UPDATE SET"
                " max_business_attempts = excluded.max_business_attempts,"
                " max_recheck_rounds = excluded.max_recheck_rounds,"
                " ceiling_micro = excluded.ceiling_micro,"
                " updated_at = excluded.updated_at",
                (tenant_id, run_id, int(max_business_attempts),
                 int(max_recheck_rounds), ceiling_micro, self._now()))
        return self.get_repair_budget(tenant_id, run_id)

    def consume_repair_attempt(self, tenant_id: str, run_id: str,
                               *, kind: str = "business") -> dict:
        """S39 每次 attempt 开始时调用。额度用尽即 409（不是 422）。

        409 而不是 422：改额度或有新 Run 之后重试可能行（§5.7 的分工）。
        """
        column = ("used_business_attempts" if kind == "business"
                  else "used_recheck_rounds")
        max_column = ("max_business_attempts" if kind == "business"
                      else "max_recheck_rounds")
        with Transaction(self.conn, self._tx_depth):
            row = self.conn.execute(
                f"SELECT {column}, {max_column} FROM repair_budget"
                " WHERE tenant_id = ? AND run_id = ?",
                (tenant_id, run_id)).fetchone()
            if row is None:
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    "run has no repair budget")
            if int(row[column]) >= int(row[max_column]):
                raise ServiceError(
                    409, "ILLEGAL_STATE_TRANSITION",
                    f"{kind} repair budget is exhausted",
                    details={"used": int(row[column]),
                             "max": int(row[max_column]), "kind": kind})
            self.conn.execute(
                f"UPDATE repair_budget SET {column} = {column} + 1,"
                " updated_at = ? WHERE tenant_id = ? AND run_id = ?",
                (self._now(), tenant_id, run_id))
        return self.get_repair_budget(tenant_id, run_id)

    def rebuild_projection(self, tenant_id: str, run_id: str,
                           *, truth: dict) -> dict:
        """§1.4 / T21-33：投影被清空后从 Temporal 重建。

        `truth` 来自 Temporal Query + `describe_workflow_execution`。
        **Temporal 赢**：如果本地投影与 `truth` 不一致，以 `truth` 为准，
        但 `projection_watermark` **不回退**——它是 S47 的游标，回退会让
        消费者重放已经见过的状态。
        """
        row = self._projection_row(tenant_id, run_id)
        current_watermark = int(row["projection_watermark"]) if row else 0
        # 水位只前进：它是 S47 的游标，回退会让消费者重放已见过的状态。
        target_watermark = max(current_watermark + 1,
                               int(truth.get("projection_watermark") or 1))
        with Transaction(self.conn, self._tx_depth):
            if row is None:
                self.conn.execute(
                    "INSERT INTO run_projection (tenant_id, run_id, state,"
                    " manifest_revision, execution_fence, completed_nodes,"
                    " blocked_nodes, failed_nodes, waiting_reason,"
                    " blocked_reason, current_stage, projection_watermark,"
                    " last_event_at, terminal_at, version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (tenant_id, run_id, truth["state"],
                     int(truth["manifest_revision"]),
                     int(truth["execution_fence"]),
                     int(truth.get("completed_nodes") or 0),
                     int(truth.get("blocked_nodes") or 0),
                     int(truth.get("failed_nodes") or 0),
                     truth.get("waiting_reason"),
                     truth.get("blocked_reason"),
                     truth.get("current_stage"),
                     target_watermark, self._now(),
                     self._now() if truth["state"] in TERMINAL_STATES else None))
            else:
                self.conn.execute(
                    "UPDATE run_projection SET state = ?, manifest_revision = ?,"
                    " execution_fence = ?, completed_nodes = ?, blocked_nodes = ?,"
                    " failed_nodes = ?, waiting_reason = ?, blocked_reason = ?,"
                    " current_stage = ?, projection_watermark = ?,"
                    " last_event_at = ?, terminal_at = ?, version = version + 1"
                    " WHERE tenant_id = ? AND run_id = ?",
                    (truth["state"], int(truth["manifest_revision"]),
                     int(truth["execution_fence"]),
                     int(truth.get("completed_nodes") or 0),
                     int(truth.get("blocked_nodes") or 0),
                     int(truth.get("failed_nodes") or 0),
                     truth.get("waiting_reason"), truth.get("blocked_reason"),
                     truth.get("current_stage"), target_watermark, self._now(),
                     self._now() if truth["state"] in TERMINAL_STATES else None,
                     tenant_id, run_id))
        return self.get_run(tenant_id, run_id)

    # ------------------------------------------------------------ 行读取

    def _binding_row(self, tenant_id: str, run_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM workflow_binding WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, run_id)).fetchone()
        if row is None:
            raise ServiceError(404, "RUN_NOT_FOUND", "run not found")
        return dict(row)

    def _projection_row(self, tenant_id: str, run_id: str):
        return self.conn.execute(
            "SELECT * FROM run_projection WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, run_id)).fetchone()

    def _next_generation(self, tenant_id: str, business_idem_key: str) -> int:
        """同 business_idem_key 的历史最大 generation + 1（C21-18）。

        Continue-As-New 用**同一个** workflow_id 递增 generation，那是它本来
        的语义；重建 Run 用**新** workflow_id，靠这个数区分。两者共用
        `run_generation` 列是对的——它表达的都是"第几代"。
        """
        row = self.conn.execute(
            "SELECT MAX(run_generation) FROM workflow_binding"
            " WHERE tenant_id = ? AND business_idem_key = ?",
            (tenant_id, business_idem_key)).fetchone()
        current = row[0] if row is not None else None
        return int(current) + 1 if current is not None else 1

    def _active_binding(self, tenant_id: str, business_idem_key: str):
        row = self.conn.execute(
            "SELECT * FROM workflow_binding WHERE tenant_id = ?"
            " AND business_idem_key = ? AND status = 'ACTIVE'",
            (tenant_id, business_idem_key)).fetchone()
        return dict(row) if row is not None else None

    def _operation_for(self, binding: dict, *, replayed: bool) -> dict:
        """§5.1 / §5.3 的 `Operation`。`state` 取五个值之一（C21-3）。

        `replayed` **不能**进响应——openapi `Operation` 是
        `additionalProperties:false`（C21-12）。这里刻意不带。
        """
        projection = self._projection_row(binding["tenant_id"], binding["run_id"])
        state = projection["state"] if projection else "QUEUED"
        return {
            "operation_id": self._new_id("op"),
            "state": OPERATION_STATE.get(state, "accepted"),
            "run_id": binding["run_id"],
        }
