"""S19 内核。方案：`plans/S19-agent-runtime.md`。

一句话边界：**S19 把一次有界推理执行成一个不可变的候选结构交回发起方。**

它不写任何业务状态，不判定任何业务正确性，不持有供应商凭证，也不给 Agent
比发起者更大的权限（§0）。具体三条：

1. **输出永远叫 `proposal`。** 带 `content_hash`，发起方必须重校验并自己铸造
   所有业务 ID（§1.3）。S19 绝不回填 `scene_id` 之类。
2. **不做第二个路由、第二个预算账本。** 模型选择归 S25→S26；余额归 S23。
   `agent_task.cost_micro` 只是本任务的熔断计数（§1.6），不参与对账。
3. **不碰 `execution_fence` / `node_epoch`。** 只读地给结果打标；绝不因为
   收到 `manifest.committed` 就自行取消任务（§3.9）。

最容易被写错的一处是**纠正**：一次纠正 = 一个新 `AgentTask`，不是同一个任务
内部的 `for` 循环（§1.4）。那样做会让幂等键覆盖多次付费调用、`usage_trace`
无法按任务核对、取消一个任务停不住纠正。`correction_round` 由服务端从链上
推导，客户端传的值一律忽略。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from event_sdk import Outbox, new_event

from .schema import (
    ALL_STATES,
    CONTRACT_GAPS,
    EGRESS_MODES,
    FAILED_REASONS,
    FORBIDDEN_OUTPUT_KEYS,
    HARD_MAX_STEPS,
    ID_PREFIX,
    LIMIT_FIELDS,
    ON_UNRESOLVED,
    PERSPECTIVES,
    REQUIRED_SCOPES,
    ROLE_MAX_SCOPE,
    ROLES,
    SCOPE_KINDS,
    SCOPE_RANK,
    SCHEMA,
    STOP_REASONS,
    TERMINAL_STATES,
    TOOL_NAMES,
)

SOURCE = "S19"
SCHEMA_VERSION = "2.0"

EVENT_TASK_COMPLETED = "agent.task.completed"
TOPIC_TASK = "drama.agent-runtime.v2"

# §1.4：一条纠正链最多 2 环。第 3 环 422 `CORRECTION_LIMIT_REACHED`。
MAX_CORRECTION_ROUND = 2

# §7.2：工具被连续拒绝 3 次说明白名单与需求不匹配，不是模型在试错。
MAX_CONSECUTIVE_TOOL_DENIALS = 3

# lease 时长。worker 在 `lease_expires_at` 之后才可被接管（§6.3 第一行）。
LEASE_SECONDS = 60

# 缺一个就无法回答「这个任务属于哪个坐标」。与 `execution-context.v2` 的
# required 对齐；`command_id` / `request_hash` 在 context 里也有，但那是**发起方**
# 的幂等键，S19 自己的幂等键走 `Idempotency-Key` 头，不混用。
REQUIRED_CONTEXT = (
    "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
    "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
    "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref",
    "actor", "command_id", "request_hash", "trace_id",
)

TASK_REQUIRED = (
    "role", "definition_revision", "goal", "input_refs", "output_schema_ref",
    "budget_scope_id", "limits",
)

_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,128}$")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


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


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。一个命令一个事务，不嵌套。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：指纹

def definition_content_hash(revision: dict, tools: list[dict]) -> str:
    """§5.1：覆盖六件事 + 整个工具集，不含 `published_by`/`published_at`。

    元数据变了不该改内容指纹，否则同一个 revision 会因为「谁发布的」而算出
    两个 hash，`agent_task.definition_content_hash` 的 pin 就没法核对。
    """
    projection = {
        "role": revision.get("role"),
        "system_template_ref": revision.get("system_template_ref"),
        "output_schema_ref": revision.get("output_schema_ref"),
        "required_capabilities": sorted(revision.get("required_capabilities") or []),
        "limits": revision.get("limits"),
        "required_perspective": revision.get("required_perspective"),
        "on_unresolved": revision.get("on_unresolved"),
        "allow_schema_override": bool(revision.get("allow_schema_override")),
        "eval_set_ref": revision.get("eval_set_ref"),
        "tools": sorted(
            ({k: t.get(k) for k in ("tool_name", "permission_key", "scope_kind",
                                    "required_scope", "egress", "max_calls")}
             for t in tools),
            key=lambda t: t.get("tool_name") or ""),
    }
    return _sha256_hex(_jcs(projection))


def result_content_hash(proposal, source_refs, assumptions,
                        unresolved_issues) -> str:
    """§4.2：`sha256(JCS(proposal + source_refs + assumptions + unresolved))`。

    **不含 `usage_trace`**：用量是运行事实，不该改变内容指纹。否则同一份提案
    重跑一次 hash 就变了，「同输入复现」无法测。
    """
    return _sha256_hex(_jcs({
        "proposal": proposal,
        "source_refs": source_refs,
        "assumptions": assumptions,
        "unresolved_issues": unresolved_issues,
    }))


def limits_within(proposed: dict, definition_limits: dict) -> bool:
    """§2.4：`limits` 只能**收紧**定义里的上限，不能放宽。

    这条防止调用方用一个大 `max_steps` 把有界纠正变成无界。
    """
    for field in LIMIT_FIELDS:
        if field not in proposed or field not in definition_limits:
            return False
        if int(proposed[field]) > int(definition_limits[field]):
            return False
    return True


def forbidden_paths(proposal) -> list[str]:
    """§4.2：`proposal` 里出现即视为输出污染。

    只查**根级**属性名，与 §2.6 的定义期校验同一把尺子。深层同名字段是业务
    允许的（比如 `scenes[].notes` 里提到 "branch_id" 这个词），所以不递归。
    """
    if not isinstance(proposal, dict):
        return []
    return sorted(k for k in proposal if k in FORBIDDEN_OUTPUT_KEYS)


def _schema_error(instance, schema, path: str = "") -> str | None:
    """stdlib 子集 JSON Schema 校验（S19-16），运行期不 import jsonschema。

    支持 `type` / `required` / `properties` / `additionalProperties` /
    `enum` / `items` / `minimum` / `maximum`。遇到不认识的关键字**不放行**：
    返回一条错误，让任务 `FAILED(STRUCTURE_INVALID)`。把「不认识」当「通过」
    正是 E25 禁止的那件事。
    """
    known = {"type", "required", "properties", "additionalProperties", "enum",
             "items", "minimum", "maximum", "description", "title", "$schema",
             "definitions", " $defs"}
    unknown = set(schema) - known
    if unknown:
        return f"{path or '/'}: unsupported schema keyword(s) {sorted(unknown)}"

    if "enum" in schema:
        if instance not in schema["enum"]:
            return f"{path or '/'}: value not in enum"

    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        checks = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "null": lambda v: v is None,
        }
        if not any(checks.get(t, lambda _v: True)(instance) for t in types):
            return f"{path or '/'}: expected {expected}, got {type(instance).__name__}"

    if isinstance(instance, dict):
        for key in schema.get("required") or []:
            if key not in instance:
                return f"{path or '/'}: missing required property {key!r}"
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                sub = _schema_error(value, properties[key], f"{path}/{key}")
                if sub:
                    return sub
            elif additional is False:
                return f"{path or '/'}: unexpected property {key!r}"
            elif isinstance(additional, dict):
                sub = _schema_error(value, additional, f"{path}/{key}")
                if sub:
                    return sub

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, value in enumerate(instance):
            sub = _schema_error(value, schema["items"], f"{path}/{index}")
            if sub:
                return sub

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            return f"{path or '/'}: below minimum"
        if "maximum" in schema and instance > schema["maximum"]:
            return f"{path or '/'}: above maximum"
    return None


def validate_proposal(proposal, schema: dict) -> str | None:
    """根级禁止字段先拒（§4.2），再做结构校验。"""
    blocked = forbidden_paths(proposal)
    if blocked:
        return f"/: forbidden root property {blocked[0]!r}"
    return _schema_error(proposal, schema)


# ------------------------------------------------------------ 服务

class AgentRuntimeService:
    def __init__(self, conn: sqlite3.Connection, *, permission_source,
                 policy_source, pack_source, template_source,
                 generation_source, schema_source, tool_source,
                 audit_sink=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 必须在 executescript 之后、任何事务之前建一次：
        # sqlite3 的 `executescript` 隐式提交，若在事务里首次构造 outbox，
        # 那次隐式提交会让整个 COMMIT/ROLLBACK 语义失效。
        self.outbox = Outbox(self.conn)
        self.permission_source = permission_source
        self.policy_source = policy_source
        self.pack_source = pack_source
        self.template_source = template_source
        self.submit_generation, self.get_generation = generation_source
        self.schema_source = schema_source
        self.tool_source = tool_source
        # §7.5：唯一可降级的一条。未配置时是个 no-op。
        self.audit_sink = audit_sink or (lambda *_a, **_k: None)

    # ------------------------------------------------------------ 上游包装

    def _upstream(self, call, code: str):
        """把「上游不可达」统一成 503，**不吞错也不套默认值**（§7.5）。"""
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

    # ------------------------------------------------------------ 定义：发布

    def create_definition(self, tenant_id: str, body: dict) -> dict:
        """`POST /agent-definitions`。一个角色一个容器（§5.1）。"""
        role = body.get("role")
        if role not in ROLES:
            raise ServiceError(422, "UNKNOWN_ROLE",
                               f"role {role!r} is not one of the 13 registered roles")
        with Transaction(self.conn) as tx:
            existing = tx.conn.execute(
                "SELECT definition_id FROM agent_definition"
                " WHERE tenant_id=? AND role=?", (tenant_id, role)).fetchone()
            if existing is not None:
                # 容器已存在就是幂等成功：同一个角色不能有两个容器，
                # 再造一个会让 `UNIQUE (tenant_id, role)` 抛库错而不是业务错误。
                return {"definition_id": existing["definition_id"], "role": role,
                        "created": False}
            definition_id = body.get("definition_id") or (ID_PREFIX["definition"] + role)
            if not _ID_RE.match(definition_id):
                raise ServiceError(400, "INVALID_SCHEMA",
                                   "definition_id must be a simple identifier")
            tx.conn.execute(
                "INSERT INTO agent_definition (tenant_id, definition_id, role,"
                " current_revision, state, created_at) VALUES (?,?,?,0,'ACTIVE',?)",
                (tenant_id, definition_id, role, _now()))
            return {"definition_id": definition_id, "role": role, "created": True}

    def publish_definition(self, tenant_id: str, definition_id: str,
                           body: dict) -> dict:
        """`POST /agent-definitions/{id}/revisions`。§2.6 / §3.2 全部 422。"""
        with Transaction(self.conn) as tx:
            container = tx.conn.execute(
                "SELECT * FROM agent_definition WHERE tenant_id=? AND definition_id=?",
                (tenant_id, definition_id)).fetchone()
            if container is None:
                raise ServiceError(404, "NOT_FOUND", "definition does not exist")
            if container["state"] != "ACTIVE":
                raise ServiceError(422, "DEFINITION_NOT_PUBLISHED",
                                   "definition is RETIRED; publish a new container")
            role = container["role"]

            revision = self._validate_definition(tx, tenant_id, role, body)
            tools = revision.pop("_tools")

            next_revision = int(container["current_revision"]) + 1
            revision["revision"] = next_revision
            digest = definition_content_hash(revision, tools)

            tx.conn.execute(
                "INSERT INTO agent_definition_revision (tenant_id, definition_id,"
                " revision, role, system_template_ref, output_schema_ref,"
                " required_capabilities, limits_json, required_perspective,"
                " on_unresolved, allow_schema_override, eval_set_ref,"
                " content_hash, published_by, published_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, definition_id, next_revision, role,
                 _dumps(revision["system_template_ref"]),
                 _dumps(revision["output_schema_ref"]),
                 _dumps(revision["required_capabilities"]),
                 _dumps(revision["limits"]),
                 revision["required_perspective"],
                 revision["on_unresolved"],
                 1 if revision["allow_schema_override"] else 0,
                 _dumps(revision["eval_set_ref"]),
                 digest, body.get("published_by") or "unknown", _now()))
            for tool in tools:
                tx.conn.execute(
                    "INSERT INTO agent_definition_tool (tenant_id, definition_id,"
                    " revision, tool_name, permission_key, scope_kind,"
                    " required_scope, egress, max_calls) VALUES (?,?,?,?,?,?,?,?,?)",
                    (tenant_id, definition_id, next_revision, tool["tool_name"],
                     tool["permission_key"], tool["scope_kind"],
                     tool["required_scope"], tool["egress"], tool["max_calls"]))
            tx.conn.execute(
                "UPDATE agent_definition SET current_revision=?"
                " WHERE tenant_id=? AND definition_id=?",
                (next_revision, tenant_id, definition_id))
            return {"definition_id": definition_id, "revision": next_revision,
                    "role": role, "content_hash": digest}

    def _validate_definition(self, tx, tenant_id: str, role: str,
                             body: dict) -> dict:
        """§2.6 的定义期校验，全部 422，「运行时再说」是不允许的。"""
        template_ref = body.get("system_template_ref")
        if not isinstance(template_ref, dict) or not template_ref.get("content_hash"):
            raise ServiceError(422, "INVALID_SCHEMA",
                               "system_template_ref needs a content_hash (plan §3.2)")
        schema_ref = body.get("output_schema_ref")
        if not isinstance(schema_ref, dict) or not schema_ref.get("id"):
            raise ServiceError(422, "INVALID_SCHEMA",
                               "output_schema_ref needs an id")

        # 模板必须**读得到**。读不到就不许发布：pin 一个取不到正文的 revision
        # 等于让任务在运行时才发现定义不可用。
        template = self._upstream(
            lambda: self.template_source(tenant_id, template_ref),
            "TEMPLATE_UNAVAILABLE")
        if template.get("template_ref", {}).get("content_hash") not in (
                None, template_ref.get("content_hash")):
            raise ServiceError(422, "INVALID_SCHEMA",
                               "system_template_ref.content_hash does not match S20")

        # §3.2 最后一条：没有评测集的定义不许发布。
        eval_ref = body.get("eval_set_ref")
        if not isinstance(eval_ref, dict) or not eval_ref.get("id"):
            raise ServiceError(422, "INVALID_SCHEMA",
                               "eval_set_ref is required; a role with no "
                               "evaluation baseline must not go to production")

        required_perspective = body.get("required_perspective")
        if required_perspective not in PERSPECTIVES:
            raise ServiceError(422, "INVALID_SCHEMA",
                               f"required_perspective must be one of {PERSPECTIVES}")
        on_unresolved = body.get("on_unresolved", "fail")
        if on_unresolved not in ON_UNRESOLVED:
            raise ServiceError(422, "INVALID_SCHEMA",
                               f"on_unresolved must be one of {ON_UNRESOLVED}")

        limits = body.get("limits")
        if not isinstance(limits, dict):
            raise ServiceError(422, "INVALID_SCHEMA", "limits is required")
        for field in LIMIT_FIELDS:
            if limits.get(field) is None:
                # §2.6：四项都必填，不允许「默认值」。
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"limits.{field} is required")
            if int(limits[field]) < 0:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"limits.{field} must be non-negative")
        if int(limits["max_steps"]) > HARD_MAX_STEPS:
            raise ServiceError(422, "LIMITS_EXCEED_DEFINITION",
                               f"limits.max_steps exceeds the platform hard cap "
                               f"of {HARD_MAX_STEPS}")
        if int(limits["max_cost_micro"]) <= 0:
            raise ServiceError(422, "INVALID_SCHEMA",
                               "limits.max_cost_micro must be positive")

        tools = body.get("tools")
        if not isinstance(tools, list):
            raise ServiceError(422, "INVALID_SCHEMA", "tools must be a list")
        validated: list[dict] = []
        seen = set()
        max_scope_rank = SCOPE_RANK[ROLE_MAX_SCOPE[role]]
        for tool in tools:
            if not isinstance(tool, dict):
                raise ServiceError(422, "INVALID_SCHEMA", "each tool must be an object")
            name = tool.get("tool_name")
            # C7：名字必须来自平台注册表；`permission_key` 由作者给出。
            if name not in TOOL_NAMES:
                raise ServiceError(422, "TOOL_NOT_ALLOWED",
                                   f"tool {name!r} is not in the platform registry")
            if name in seen:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"tool {name!r} is listed twice")
            seen.add(name)
            key = tool.get("permission_key")
            if not isinstance(key, str) or not key:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"tool {name!r} needs a permission_key (C7)")
            scope_kind = tool.get("scope_kind")
            if scope_kind not in SCOPE_KINDS:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"tool {name!r} has an unknown scope_kind")
            required_scope = tool.get("required_scope")
            if required_scope not in REQUIRED_SCOPES:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"tool {name!r} has an unknown required_scope")
            egress = tool.get("egress", "internal")
            if egress not in EGRESS_MODES:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"tool {name!r} has an unknown egress mode")
            max_calls = tool.get("max_calls")
            if not isinstance(max_calls, int) or max_calls <= 0:
                raise ServiceError(422, "INVALID_SCHEMA",
                                   f"tool {name!r} needs max_calls > 0")
            # §2.6：`required_scope` 不能宽于角色的最大 scope。**比较的是
            # `scope_kind`**——S01 的 `SCOPE_RANK` 秩的是 scope_kind，
            # 拿它比 required_scope 是两套词表（S19-11）。
            if SCOPE_RANK[scope_kind] > max_scope_rank:
                raise ServiceError(
                    422, "LIMITS_EXCEED_DEFINITION",
                    f"tool {name!r} declares scope_kind={scope_kind!r}, wider "
                    f"than role {role!r} allows ({ROLE_MAX_SCOPE[role]!r})")
            validated.append({"tool_name": name, "permission_key": key,
                              "scope_kind": scope_kind,
                              "required_scope": required_scope,
                              "egress": egress, "max_calls": max_calls})

        # §2.6：`output_schema_ref` 的根级属性不得包含业务 ID（§3.2 同一条）。
        schema = self._upstream(
            lambda: self.schema_source(tenant_id, schema_ref),
            "SCHEMA_SOURCE_UNAVAILABLE")
        blocked = forbidden_paths_from_schema(schema)
        if blocked:
            raise ServiceError(
                422, "SCHEMA_NOT_ALLOWED",
                f"output schema root declares forbidden property {blocked[0]!r}")

        return {
            "role": role,
            "system_template_ref": template_ref,
            "output_schema_ref": schema_ref,
            "required_capabilities": sorted(body.get("required_capabilities") or []),
            "limits": {f: int(limits[f]) for f in LIMIT_FIELDS},
            "required_perspective": required_perspective,
            "on_unresolved": on_unresolved,
            "allow_schema_override": bool(body.get("allow_schema_override")),
            "eval_set_ref": eval_ref,
            "_tools": validated,
        }

    # ------------------------------------------------------------ 任务：创建

    def create_task(self, tenant_id: str, body: dict) -> dict:
        """`POST /agent-tasks`（§2.4）。准入同步完成，写 `ACCEPTED`。

        §4.4 / §6.1 第 4 步：**创建时不写 outbox**。任务创建不是业务事件。
        """
        context = body.get("context")
        if not isinstance(context, dict):
            raise ServiceError(400, "INVALID_SCHEMA", "context is required")
        missing = [k for k in REQUIRED_CONTEXT if context.get(k) is None]
        if missing:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"context is missing required field(s): {missing}")
        # §3.8 不变量 5：上下文与任务必须同租户，否则拒绝创建。
        if context["tenant_id"] != tenant_id:
            raise ServiceError(403, "TENANT_MISMATCH",
                               "context.tenant_id disagrees with the signed tenant")
        missing = [k for k in TASK_REQUIRED if body.get(k) is None]
        if missing:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"body is missing required field(s): {missing}")

        role = body["role"]
        if role not in ROLES:
            raise ServiceError(422, "UNKNOWN_ROLE",
                               f"role {role!r} is not one of the 13 registered roles")

        limits = body["limits"]
        if not isinstance(limits, dict):
            raise ServiceError(400, "INVALID_SCHEMA", "limits must be an object")
        for field in LIMIT_FIELDS:
            if limits.get(field) is None:
                raise ServiceError(400, "INVALID_SCHEMA",
                                   f"limits.{field} is required")

        input_refs = body["input_refs"]
        if not isinstance(input_refs, dict):
            raise ServiceError(400, "INVALID_SCHEMA", "input_refs must be an object")
        pack_ref = input_refs.get("context_pack_ref")
        if not isinstance(pack_ref, dict) or not pack_ref.get("id"):
            # §7.1 / §7.5：没有 pack 的 Agent 会自己编前情。这是拒绝，不是降级。
            raise ServiceError(400, "INVALID_SCHEMA",
                               "input_refs.context_pack_ref is required")

        actor = context.get("actor") or {}
        principal_id = actor.get("principal_id")
        if not principal_id:
            raise ServiceError(400, "INVALID_SCHEMA",
                               "context.actor.principal_id is required")

        defn = self._published_definition(tenant_id, role,
                                         int(body["definition_revision"]))
        if not limits_within(limits, defn["limits"]):
            raise ServiceError(422, "LIMITS_EXCEED_DEFINITION",
                               "limits may only tighten the definition's limits")

        schema_ref = body["output_schema_ref"]
        if not isinstance(schema_ref, dict):
            raise ServiceError(400, "INVALID_SCHEMA",
                               "output_schema_ref must be an object")
        expected = defn["output_schema_ref"]
        if schema_ref.get("id") != expected.get("id"):
            raise ServiceError(422, "SCHEMA_NOT_ALLOWED",
                               "output_schema_ref does not match the definition")
        pinned_hash = schema_ref.get("content_hash")
        if pinned_hash and expected.get("content_hash") and \
                pinned_hash != expected["content_hash"] and \
                not defn["allow_schema_override"]:
            raise ServiceError(422, "SCHEMA_NOT_ALLOWED",
                               "output_schema_ref.content_hash differs from the "
                               "definition and allow_schema_override is false")

        correction_of = body.get("correction_of_task_id")
        round_, root = 0, None
        parent = None
        if correction_of:
            parent = self._task(tenant_id, correction_of)
            if parent["role"] != role:
                raise ServiceError(422, "CORRECTION_DEFINITION_MISMATCH",
                                   "a correction must keep the parent's role")
            if int(parent["definition_revision"]) != int(body["definition_revision"]):
                # §3.4：纠正不可换定义。换定义就不是纠正，是另一个任务。
                raise ServiceError(422, "CORRECTION_DEFINITION_MISMATCH",
                                   "a correction must reuse the parent's "
                                   "definition_revision")
            round_ = int(parent["correction_round"]) + 1
            if round_ > MAX_CORRECTION_ROUND:
                raise ServiceError(422, "CORRECTION_LIMIT_REACHED",
                                   f"correction chain already has "
                                   f"{MAX_CORRECTION_ROUND} rounds; hand to review (S40)")
            # §6.3：次数上限**按轮**，不按请求。同一父任务已有一条纠正行，
            # 就说明这一轮已经付过钱了；否则并发的两个 round-1 各建一个任务，
            # 两笔钱只能用一个。要拦在业务层，不能靠唯一索引抛裸库错。
            already = self.conn.execute(
                "SELECT 1 FROM agent_task WHERE tenant_id=?"
                " AND correction_of_task_id=? LIMIT 1",
                (tenant_id, correction_of)).fetchone()
            if already is not None:
                raise ServiceError(
                    422, "CORRECTION_LIMIT_REACHED",
                    f"correction round {round_} for this parent was already "
                    f"created; corrections are counted per round, not per request")
            root = parent["root_task_id"]
            feedback = body.get("correction_feedback")
            if not isinstance(feedback, dict) or \
                    not isinstance(feedback.get("violations"), list) or \
                    not feedback["violations"]:
                # §2.4：必须是结构化违规列表，不是自由文本。自由文本会让模型
                # 把纠正当成「再想一遍」，那正是 docs/08 禁止的无界反思。
                raise ServiceError(400, "INVALID_SCHEMA",
                                   "correction_feedback.violations[] is required "
                                   "and must be non-empty")

        # 外部只读校验：在事务内做，失败整体回滚（§6.1 第 3 步）。
        # 代价是事务持有时间含网络往返，但把校验挪到事务外会出现
        # 「校验通过 → 开事务 → 期间权限被撤销」的窗口（E27）。
        self._authorize_create(tenant_id, principal_id, role, defn, context)
        self._policy_admit(tenant_id, context)
        self._check_pack(tenant_id, pack_ref, defn, context)

        task_id = _new_id("task")
        with Transaction(self.conn) as tx:
            tx.conn.execute(
                "INSERT INTO agent_task (tenant_id, task_id, role, definition_id,"
                " definition_revision, definition_content_hash, root_task_id,"
                " correction_of_task_id, correction_round, state, stop_reason,"
                " goal, execution_context, input_refs, output_schema_ref,"
                " budget_scope_id, limits_json, result_ref, steps_used, tokens_in,"
                " tokens_out, cost_micro, cost_state, publish_sequence,"
                " lease_owner, lease_expires_at, created_at, finished_at, command_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,'ACCEPTED',NULL,?,?,?,?,?,?,NULL,0,0,0,0,"
                " 'PROVISIONAL',0,NULL,NULL,?,NULL,?)",
                (tenant_id, task_id, role, defn["definition_id"],
                 defn["revision"], defn["content_hash"], root or task_id,
                 correction_of, round_, body["goal"], _dumps(context),
                 _dumps(input_refs), _dumps(schema_ref), body["budget_scope_id"],
                 _dumps({f: int(limits[f]) for f in LIMIT_FIELDS}), _now(),
                 body.get("command_id") or context.get("command_id")))
        return {"task_id": task_id, "state": "ACCEPTED", "role": role,
                "correction_round": round_, "root_task_id": root or task_id,
                "definition_ref": {"id": defn["definition_id"],
                                   "revision": defn["revision"],
                                   "content_hash": defn["content_hash"]}}

    def _authorize_create(self, tenant_id, principal_id, role, defn, context):
        """§3.1 ① / §7.1：发起方必须有 `agent.task.create`。

        `scope_hint` 不是授权（S01 §4.1），所以这里只发真实判定，不做本地粗筛
        放行。S01 不可用 → 503，**不降级为放行**。
        """
        scope_kind, scope_id = _scope_of(context)
        verdicts = self._upstream(
            lambda: self.permission_source(
                tenant_id, principal_id,
                [{"permission_key": "agent.task.create",
                  "scope_kind": scope_kind, "scope_id": scope_id,
                  "purpose": f"agent.{role}"}],
                f"agent.{role}"),
            "AUTHORIZATION_UNAVAILABLE")
        if not verdicts:
            raise ServiceError(503, "AUTHORIZATION_UNAVAILABLE",
                               "S01 returned no verdict for agent.task.create",
                               retryable=True)
        decision = verdicts[0]
        if not decision.get("allowed"):
            raise ServiceError(
                403, "PERMISSION_DENIED",
                f"agent.task.create was denied: {decision.get('reason')}",
                details={"reason": decision.get("reason"),
                         "decision_id": decision.get("decision_id")})

    def _policy_admit(self, tenant_id: str, context: dict) -> None:
        """C4：准入走 `:resolve-effective`，只核验 pin 仍是当前版。

        **这里没有「策略拒绝任务」这条分支**，因为 `:evaluate` 不存在，
        `POLICY_DENIED` 在真实上游下不可达。登记为 C4；不假装实现它。
        """
        self._upstream(
            lambda: self.policy_source(tenant_id, {
                "tenant_id": tenant_id,
                "project_id": context.get("project_id"),
                "episode_id": context.get("episode_id"),
                "node_id": context.get("node_id"),
                "policy_ref": context.get("policy_ref"),
            }),
            "POLICY_UNAVAILABLE")

    def _check_pack(self, tenant_id: str, pack_ref: dict, defn: dict,
                    context: dict) -> None:
        """§2.4 / §7.1：回读 pack，校验 perspective 与 watermark。

        S18 的 `pack.perspective` 是**标量字符串**（S19-14），所以直接比对，
        不要从里面取 `mode`。
        """
        result = self._upstream(
            lambda: self.pack_source(tenant_id, pack_ref["id"],
                                     pack_ref.get("revision")),
            "CONTEXT_UNAVAILABLE")
        pack = result["pack"]
        perspective = pack.get("perspective")
        if perspective != defn["required_perspective"]:
            raise ServiceError(
                422, "PERSPECTIVE_MISMATCH",
                f"task requires perspective {defn['required_perspective']!r} "
                f"but the pack is {perspective!r}; a pack may not be downgraded "
                f"(S18 §8)")
        watermark = pack.get("source_watermark")
        checkpoint = context.get("memory_checkpoint") or {}
        required = checkpoint.get("commit_sequence")
        if watermark is None or required is None:
            # 判不了就不放行：没有水位就无法证明 pack 追上了 manifest。
            raise ServiceError(
                503, "CONTEXT_UNAVAILABLE",
                "pack or execution context is missing a commit watermark; S19 "
                "cannot prove the pack is current", retryable=True)
        if int(watermark) < int(required):
            raise ServiceError(
                422, "CONTEXT_PACK_STALE",
                f"pack watermark {watermark} is behind the manifest checkpoint "
                f"{required}; re-fetch the pack", retryable=True)

    # ------------------------------------------------------------ 任务：取消/查询

    def cancel_task(self, tenant_id: str, task_id: str, body: dict) -> dict:
        """`POST /agent-tasks/{id}:cancel`（§6.4）。**软取消**。"""
        with Transaction(self.conn) as tx:
            task = tx.conn.execute(
                "SELECT * FROM agent_task WHERE tenant_id=? AND task_id=?",
                (tenant_id, task_id)).fetchone()
            if task is None:
                raise ServiceError(404, "NOT_FOUND", "task does not exist")
            if task["state"] in TERMINAL_STATES:
                raise ServiceError(409, "TASK_NOT_CANCELLABLE",
                                   f"task is already {task['state']}")
            if task["state"] == "CANCELLING":
                return {"task_id": task_id, "state": "CANCELLING",
                        "already_cancelling": True}
            if task["state"] == "ACCEPTED":
                # 没有在途调用，可以立即终态。§6.4 明说这条是允许的。
                # 这里已在事务内，所以走 `_finish_in_tx`：`finish_task` 自己
                # `BEGIN IMMEDIATE`，在事务里再开一次会抛库错。
                self._finish_in_tx(tx, tenant_id, dict(task), "CANCELLED",
                                   "USER_CANCELLED")
                return {"task_id": task_id, "state": "CANCELLED"}
            tx.conn.execute(
                "UPDATE agent_task SET state='CANCELLING'"
                " WHERE tenant_id=? AND task_id=? AND state='RUNNING'",
                (tenant_id, task_id))
            # 在途的 S26 调用**继续跑完并结算**，不假退款。这里只标状态。
            return {"task_id": task_id, "state": "CANCELLING"}

    def get_task(self, tenant_id: str, task_id: str) -> dict:
        task = self._task(tenant_id, task_id)
        chain = self.conn.execute(
            "SELECT task_id, correction_round, state, cost_micro FROM agent_task"
            " WHERE tenant_id=? AND root_task_id=? ORDER BY correction_round",
            (tenant_id, task["root_task_id"])).fetchall()
        return {
            "task_id": task["task_id"],
            "role": task["role"],
            "definition_ref": {"id": task["definition_id"],
                               "revision": int(task["definition_revision"]),
                               "content_hash": task["definition_content_hash"]},
            "root_task_id": task["root_task_id"],
            "correction_round": int(task["correction_round"]),
            "state": task["state"],
            "stop_reason": task["stop_reason"],
            "goal": task["goal"],
            "input_refs": _loads(task["input_refs"], {}),
            "output_schema_ref": _loads(task["output_schema_ref"], {}),
            "budget_scope_id": task["budget_scope_id"],
            "limits": _loads(task["limits_json"], {}),
            "result_ref": _loads(task["result_ref"], None),
            "usage": {
                "steps_used": int(task["steps_used"]),
                "tokens_in": int(task["tokens_in"]),
                "tokens_out": int(task["tokens_out"]),
                "cost_micro": int(task["cost_micro"]),
                "cost_state": task["cost_state"],
            },
            "chain": [{"task_id": row["task_id"],
                       "round": int(row["correction_round"]),
                       "state": row["state"],
                       "cost_micro": int(row["cost_micro"])} for row in chain],
            "created_at": task["created_at"],
            "finished_at": task["finished_at"],
        }

    def get_result(self, tenant_id: str, task_id: str) -> dict:
        task = self._task(tenant_id, task_id)
        row = self.conn.execute(
            "SELECT * FROM task_result WHERE tenant_id=? AND task_id=?",
            (tenant_id, task_id)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"task {task_id} has no result (state={task['state']})")
        return self._render_result(row)

    def get_steps(self, tenant_id: str, task_id: str) -> dict:
        self._task(tenant_id, task_id)
        steps = [dict(row) for row in self.conn.execute(
            "SELECT * FROM agent_step WHERE tenant_id=? AND task_id=?"
            " ORDER BY step_index", (tenant_id, task_id)).fetchall()]
        audits = [dict(row) for row in self.conn.execute(
            "SELECT * FROM tool_call_audit WHERE tenant_id=? AND task_id=?"
            " ORDER BY step_index, tool_name", (tenant_id, task_id)).fetchall()]
        by_step: dict[int, list] = {}
        for audit in audits:
            by_step.setdefault(int(audit["step_index"]), []).append({
                # 审计对外只给摘要：参数原文从未进库（§5.4 / docs/14）。
                "tool_name": audit["tool_name"],
                "verdict": audit["verdict"],
                "deny_reason": audit["deny_reason"],
                "permission_key": audit["permission_key"],
                "scope_kind": audit["scope_kind"],
                "scope_id": audit["scope_id"],
                "egress_target": audit["egress_target"],
                "arguments_digest": audit["arguments_digest"],
                "result_digest": audit["result_digest"],
                "duration_ms": audit["duration_ms"],
                "called_at": audit["called_at"],
            })
        for step in steps:
            step["tool_calls"] = by_step.get(int(step["step_index"]), [])
            if step.get("model_snapshot"):
                step["model_snapshot"] = _loads(step["model_snapshot"], None)
        return {"task_id": task_id, "steps": steps}

    def get_definition(self, tenant_id: str, definition_id: str,
                       revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM agent_definition_revision"
            " WHERE tenant_id=? AND definition_id=? AND revision=?",
            (tenant_id, definition_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "NOT_FOUND", "definition revision does not exist")
        tools = self.conn.execute(
            "SELECT tool_name, permission_key, scope_kind, required_scope,"
            " egress, max_calls FROM agent_definition_tool"
            " WHERE tenant_id=? AND definition_id=? AND revision=?"
            " ORDER BY tool_name",
            (tenant_id, definition_id, revision)).fetchall()
        return {
            "definition_id": definition_id,
            "revision": int(row["revision"]),
            "role": row["role"],
            "system_template_ref": _loads(row["system_template_ref"], {}),
            "output_schema_ref": _loads(row["output_schema_ref"], {}),
            "required_capabilities": _loads(row["required_capabilities"], []),
            "limits": _loads(row["limits_json"], {}),
            "required_perspective": row["required_perspective"],
            "on_unresolved": row["on_unresolved"],
            "allow_schema_override": bool(row["allow_schema_override"]),
            "eval_set_ref": _loads(row["eval_set_ref"], {}),
            "tools": [dict(t) for t in tools],
            "content_hash": row["content_hash"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
        }

    def list_roles(self, tenant_id: str) -> dict:
        """`GET /agent-roles`。13 个角色与各自**已发布**的定义（可能没有）。

        C9：不预置任何角色定义，因为发布要求 `eval_set_ref` 非空，而 13 份
        `agents/*/agent.yaml` 的 limits 全是 unassigned。所以这里的 `definition`
        多半是 null——那是真实状态，不是遗漏。
        """
        rows = self.conn.execute(
            "SELECT d.role, d.definition_id, d.current_revision, d.state,"
            " r.content_hash FROM agent_definition d"
            " LEFT JOIN agent_definition_revision r"
            "   ON r.tenant_id=d.tenant_id AND r.definition_id=d.definition_id"
            "  AND r.revision=d.current_revision"
            " WHERE d.tenant_id=?", (tenant_id,)).fetchall()
        published = {row["role"]: {"definition_id": row["definition_id"],
                                   "revision": int(row["current_revision"]),
                                   "content_hash": row["content_hash"],
                                   "state": row["state"]}
                     for row in rows if int(row["current_revision"]) >= 1}
        return {"roles": [
            {"role": role,
             "max_scope": ROLE_MAX_SCOPE[role],
             "definition": published.get(role)}
            for role in ROLES]}

    # ------------------------------------------------------------ 执行：步骤

    def acquire_task(self, tenant_id: str, role: str, owner: str) -> dict | None:
        """`UPDATE ... WHERE state='ACCEPTED'` 抢一个任务（§6.3 第一行）。

        只有一个 worker 能拿到：WHERE 子句原子。返回 None 表示队列空。
        """
        if role not in ROLES:
            raise ServiceError(422, "UNKNOWN_ROLE", f"role {role!r} is unknown")
        if owner not in ROLES and not owner:
            raise ServiceError(400, "INVALID_SCHEMA", "owner is required")
        from datetime import timedelta
        expires = (_now_dt() + timedelta(seconds=LEASE_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with Transaction(self.conn) as tx:
            row = tx.conn.execute(
                "SELECT task_id FROM agent_task"
                " WHERE tenant_id=? AND role=? AND state='ACCEPTED'"
                " ORDER BY created_at LIMIT 1", (tenant_id, role)).fetchone()
            if row is None:
                return None
            updated = tx.conn.execute(
                "UPDATE agent_task SET state='RUNNING', lease_owner=?,"
                " lease_expires_at=? WHERE tenant_id=? AND task_id=?"
                " AND state='ACCEPTED'", (owner, expires, tenant_id, row["task_id"]))
            if updated.rowcount != 1:
                return None
            return {"task_id": row["task_id"], "state": "RUNNING",
                    "lease_expires_at": expires}

    def record_step(self, tenant_id: str, task_id: str, *, kind: str,
                    outcome: str, owner: str, generation_job_ref: str | None = None,
                    model_snapshot: dict | None = None, tokens_in: int = 0,
                    tokens_out: int = 0, cost_micro: int = 0,
                    error_code: str | None = None) -> int:
        """写一步并推进熔断计数（§6.2）。同一事务内。

        `step_index` 用 `coalesce(max+1, 0)` 在事务里取，不靠应用层计数器：
        跳号即 bug（§3.8 不变量 2）。
        """
        with Transaction(self.conn) as tx:
            task = tx.conn.execute(
                "SELECT * FROM agent_task WHERE tenant_id=? AND task_id=?"
                " AND state='RUNNING' AND lease_owner=?",
                (tenant_id, task_id, owner)).fetchone()
            if task is None:
                # lease 被强夺时旧 worker 的所有写都到这里被拒（§7.3 最后一行）。
                raise ServiceError(409, "TASK_NOT_RUNNING",
                                   "task is not RUNNING under this lease owner")
            step_index = tx.conn.execute(
                "SELECT coalesce(max(step_index)+1, 0) FROM agent_step"
                " WHERE tenant_id=? AND task_id=?", (tenant_id, task_id)).fetchone()[0]
            tx.conn.execute(
                "INSERT INTO agent_step (tenant_id, task_id, step_index, kind,"
                " started_at, finished_at, outcome, generation_job_ref,"
                " model_snapshot, tokens_in, tokens_out, cost_micro, error_code)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, task_id, step_index, kind, _now(), _now(), outcome,
                 generation_job_ref,
                 None if model_snapshot is None else _dumps(model_snapshot),
                 int(tokens_in), int(tokens_out), int(cost_micro), error_code))
            tx.conn.execute(
                "UPDATE agent_task SET steps_used=steps_used+1,"
                " tokens_in=tokens_in+?, tokens_out=tokens_out+?,"
                " cost_micro=cost_micro+? WHERE tenant_id=? AND task_id=?",
                (int(tokens_in), int(tokens_out), int(cost_micro),
                 tenant_id, task_id))
            return int(step_index)

    def gate_check(self, tenant_id: str, task_id: str, *,
                   next_quote_micro: int = 0) -> str | None:
        """§3.5 的四道闸。返回命中的 `stop_reason`，None 表示可以继续。

        第四道用**报价上界**判断（不是「已经花了多少」）：先发出去再算，
        一次 4 秒视频的价格就能把上限捅穿。
        """
        task = self._task(tenant_id, task_id)
        limits = _loads(task["limits_json"], {})
        steps_used = int(task["steps_used"])
        if steps_used >= int(limits.get("max_steps", 0)):
            return "STEP_LIMIT"
        tokens = int(task["tokens_in"]) + int(task["tokens_out"])
        if tokens >= int(limits.get("max_tokens", 0)):
            return "TOKEN_LIMIT"
        wall = _seconds_since(task["created_at"])
        if wall >= int(limits.get("max_wall_seconds", 0)):
            return "TIME_LIMIT"
        if int(task["cost_micro"]) + int(next_quote_micro) > \
                int(limits.get("max_cost_micro", 0)):
            return "COST_LIMIT"
        return None

    # ------------------------------------------------------------ 执行：工具门

    def call_tool(self, tenant_id: str, task_id: str, owner: str, *,
                  step_index: int, tool_name: str, arguments: dict,
                  scope_id: str | None = None) -> dict:
        """§1.5 / §3.3 的三道门。**审计先于返回**。

        三道门任一失败都返回一条结构化拒绝（`verdict='DENY'`），而不是抛异常：
        Agent 需要看到拒绝理由才能换个做法，拒绝也计入步数。
        """
        task = self._task(tenant_id, task_id)
        defn = self._definition_row(tenant_id, task["definition_id"],
                                    int(task["definition_revision"]))
        context = _loads(task["execution_context"], {})
        actor = context.get("actor") or {}
        principal_id = actor.get("principal_id")

        grant = next((g for g in defn["tools"] if g["tool_name"] == tool_name), None)
        if grant is None:
            return self._deny(tenant_id, task_id, step_index, tool_name,
                              arguments, "TOOL_NOT_ALLOWED")
        used = self.conn.execute(
            "SELECT count(*) FROM tool_call_audit WHERE tenant_id=? AND task_id=?"
            " AND tool_name=? AND verdict='ALLOW'",
            (tenant_id, task_id, tool_name)).fetchone()[0]
        if int(used) >= int(grant["max_calls"]):
            return self._deny(tenant_id, task_id, step_index, tool_name,
                              arguments, "TOOL_CALL_LIMIT")

        resolved_scope = scope_id or _scope_id_of(context, grant["scope_kind"])
        verdicts = self._upstream(
            lambda: self.permission_source(
                tenant_id, principal_id,
                [{"permission_key": grant["permission_key"],
                  "scope_kind": grant["scope_kind"],
                  "scope_id": resolved_scope,
                  "purpose": f"agent.{task['role']}"}],
                f"agent.{task['role']}"),
            "AUTHORIZATION_UNAVAILABLE")
        decision = verdicts[0] if verdicts else {}
        if not decision.get("allowed"):
            return self._deny(tenant_id, task_id, step_index, tool_name,
                              arguments,
                              decision.get("reason") or "PERMISSION_DENIED",
                              grant=grant, scope_id=resolved_scope)

        if grant["egress"] == "external_allowlisted":
            target = (arguments or {}).get("target")
            if not _egress_allowed(context, target):
                return self._deny(tenant_id, task_id, step_index, tool_name,
                                  arguments, "EGRESS_DENIED", grant=grant,
                                  scope_id=resolved_scope,
                                  egress_target=target)

        started = _now_dt()
        result = self._upstream(
            lambda: self.tool_source(tenant_id, tool_name, arguments or {}, 60),
            "TOOL_BROKER_UNAVAILABLE")
        duration_ms = int((_now_dt() - started).total_seconds() * 1000)

        digest = _sha256_hex(_jcs(arguments or {}))
        result_digest = _sha256_hex(_jcs(result))
        with Transaction(self.conn) as tx:
            tx.conn.execute(
                "INSERT INTO tool_call_audit (tenant_id, task_id, step_index,"
                " tool_name, arguments_digest, arguments_ref, verdict,"
                " deny_reason, permission_key, scope_kind, scope_id,"
                " revocation_epoch, egress_target, result_digest, result_ref,"
                " duration_ms, called_at)"
                " VALUES (?,?,?,?,?,NULL,'ALLOW',NULL,?,?,?,NULL,?,?,NULL,?,?)",
                (tenant_id, task_id, step_index, tool_name, digest,
                 grant["permission_key"], grant["scope_kind"], resolved_scope,
                 (arguments or {}).get("target"), result_digest, duration_ms,
                 _now()))
        self.audit_sink(tenant_id, {"task_id": task_id, "step_index": step_index,
                                    "tool_name": tool_name,
                                    "arguments_digest": digest,
                                    "result_digest": result_digest,
                                    "verdict": "ALLOW"})
        # 工具结果只作为数据（§1.5 第 3 条）：它不进 system 模板、不选 schema、
        # 不改剩余步数或费用上限。调用方拿到的就是这个 dict。
        return {"verdict": "ALLOW", "tool_name": tool_name, "result": result}

    def _deny(self, tenant_id, task_id, step_index, tool_name, arguments,
              reason, *, grant=None, scope_id=None, egress_target=None) -> dict:
        digest = _sha256_hex(_jcs(arguments or {}))
        with Transaction(self.conn) as tx:
            tx.conn.execute(
                "INSERT INTO tool_call_audit (tenant_id, task_id, step_index,"
                " tool_name, arguments_digest, arguments_ref, verdict,"
                " deny_reason, permission_key, scope_kind, scope_id,"
                " revocation_epoch, egress_target, result_digest, result_ref,"
                " duration_ms, called_at)"
                " VALUES (?,?,?,?,?,NULL,'DENY',?,?,?,?,NULL,?,NULL,NULL,NULL,?)",
                (tenant_id, task_id, step_index, tool_name, digest, reason,
                 grant["permission_key"] if grant else None,
                 grant["scope_kind"] if grant else None, scope_id,
                 egress_target, _now()))
        self.audit_sink(tenant_id, {"task_id": task_id, "step_index": step_index,
                                    "tool_name": tool_name,
                                    "arguments_digest": digest,
                                    "verdict": "DENY", "deny_reason": reason})
        return {"verdict": "DENY", "tool_name": tool_name, "reason": reason}

    def consecutive_tool_denials(self, tenant_id: str, task_id: str) -> int:
        rows = self.conn.execute(
            "SELECT verdict FROM tool_call_audit WHERE tenant_id=? AND task_id=?"
            " ORDER BY step_index DESC, tool_name DESC",
            (tenant_id, task_id)).fetchall()
        count = 0
        for row in rows:
            if row["verdict"] != "DENY":
                break
            count += 1
        return count

    # ------------------------------------------------------------ 检查点

    def write_checkpoint(self, tenant_id: str, task_id: str, *, step_index: int,
                         messages_ref: dict, state_digest: str,
                         remaining: dict,
                         pending_generation_job_ref: str | None = None) -> int:
        """§3.7：`checkpoint_seq` 单调，旧 checkpoint 永远覆盖不了新的。"""
        with Transaction(self.conn) as tx:
            self._task_in(tx, tenant_id, task_id)
            seq = tx.conn.execute(
                "SELECT coalesce(max(checkpoint_seq)+1, 1) FROM task_checkpoint"
                " WHERE tenant_id=? AND task_id=?", (tenant_id, task_id)).fetchone()[0]
            tx.conn.execute(
                "INSERT INTO task_checkpoint (tenant_id, task_id, checkpoint_seq,"
                " step_index, messages_ref, state_digest,"
                " pending_generation_job_ref, remaining_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, task_id, int(seq), int(step_index),
                 _dumps(messages_ref), state_digest, pending_generation_job_ref,
                 _dumps(remaining), _now()))
            return int(seq)

    def latest_checkpoint(self, tenant_id: str, task_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM task_checkpoint WHERE tenant_id=? AND task_id=?"
            " ORDER BY checkpoint_seq DESC LIMIT 1",
            (tenant_id, task_id)).fetchone()
        if row is None:
            return None
        return {"checkpoint_seq": int(row["checkpoint_seq"]),
                "step_index": int(row["step_index"]),
                "messages_ref": _loads(row["messages_ref"], {}),
                "state_digest": row["state_digest"],
                "pending_generation_job_ref": row["pending_generation_job_ref"],
                "remaining": _loads(row["remaining_json"], {}),
                "created_at": row["created_at"]}

    def recover(self, tenant_id: str, task_id: str, owner: str) -> dict:
        """§3.7 的恢复顺序。**不重提、不重拼 prompt、不重算 remaining。**

        1. 读最后 checkpoint；
        2. 在途 S26 调用 → 终态就用结果，`SUBMISSION_UNKNOWN` 就
           `FAILED(GENERATION_UNKNOWN)` 等对账；
        3. `messages_ref` 重建上下文；
        4. `remaining` 用 checkpoint 的值，防止「重启后步数清零」绕过熔断。
        """
        checkpoint = self.latest_checkpoint(tenant_id, task_id)
        if checkpoint is None:
            raise ServiceError(409, "NO_CHECKPOINT",
                               "task has no checkpoint to recover from")
        pending = checkpoint["pending_generation_job_ref"]
        outcome: dict = {"checkpoint": checkpoint, "resumed_from_step":
                         checkpoint["step_index"]}
        if pending:
            job = self._upstream(
                lambda: self.get_generation(tenant_id, pending),
                "GENERATION_UNAVAILABLE")
            if job.get("must_reconcile_before_resubmit") or \
                    job.get("state") == "SUBMISSION_UNKNOWN":
                # S26 §7.2 铁律：不重提。等对账后人工处置。
                self.finish_task(tenant_id, task_id, state="FAILED",
                                 stop_reason="GENERATION_UNKNOWN", owner=owner)
                return {**outcome, "state": "FAILED",
                        "stop_reason": "GENERATION_UNKNOWN",
                        "pending_job": job.get("state")}
            outcome["pending_state"] = job.get("state")
            outcome["pending_output"] = (job.get("output") or {})
        # remaining 直接取 checkpoint 里的值，**不重算**（第 4 步）。
        constraints = checkpoint["remaining"]
        hit = _limits_from_remaining(constraints)
        if hit:
            self.finish_task(tenant_id, task_id, state="FAILED",
                             stop_reason=hit, owner=owner)
            return {**outcome, "state": "FAILED", "stop_reason": hit}
        return outcome

    # ------------------------------------------------------------ 终态

    def finish_task(self, tenant_id: str, task_id: str, *, state: str,
                    stop_reason: str, owner: str | None = None,
                    result: dict | None = None) -> dict:
        """推进到终态 + 写结果 + 发事件，**同一事务**（§6.5）。

        终态不回归（§3.6 表最后一行）：`UPDATE ... WHERE state IN
        ('RUNNING','CANCELLING','ACCEPTED')`，影响 0 行就是别人先到了终态。
        """
        if state not in TERMINAL_STATES:
            raise ServiceError(400, "INVALID_SCHEMA", f"{state!r} is not terminal")
        if stop_reason not in STOP_REASONS:
            raise ServiceError(400, "INVALID_SCHEMA",
                               f"{stop_reason!r} is not a stop_reason")
        if result is not None and state in ("SUCCEEDED", "NEEDS_REVIEW"):
            # §7.2 / §10.4：输出结构是否合法归 S19 判。不合法就**不能**落
            # 结果，任务转 FAILED(STRUCTURE_INVALID)——终态先落库再抛，
            # 否则异常会把 FAILED 一起回滚掉，任务停在 RUNNING。
            problem = self._result_problem(tenant_id, task_id, result)
            if problem is not None:
                state, stop_reason, result = "FAILED", "STRUCTURE_INVALID", None
                failure = problem
            else:
                failure = None
        else:
            failure = None
        with Transaction(self.conn) as tx:
            task = self._task_in(tx, tenant_id, task_id)
            outcome = self._finish_in_tx(tx, tenant_id, task, state, stop_reason,
                                         result=result)
        self.audit_sink(tenant_id, {"task_id": task_id, "state": state,
                                    "stop_reason": stop_reason})
        response = {"task_id": task_id, "state": state,
                    "stop_reason": stop_reason,
                    "result_ref": outcome["result_ref"], "usage": outcome["usage"]}
        if isinstance(failure, ServiceError):
            # §7.2 最后一行：禁止字段要 422 交给调用方，任务同时 FAILED。
            # 终态已经落库，所以抛出去不会把 FAILED 一起回滚。
            raise failure
        if failure is not None:
            # 纯结构非法：§7.2 没有给错误码，只要求终态可查。
            response["structure_error"] = failure
        return response

    def _result_problem(self, tenant_id: str, task_id: str,
                        result: dict) -> ServiceError | str | None:
        """§7.2 / §10.4：S19 判「输出结构是否合法」，发起方判语义。

        三件事按顺序：根级禁止字段（这是污染，`OUTPUT_CONTAINS_FORBIDDEN_FIELD`
        要原样交给调用方）→ 形状（三个列表）→ 按任务**自己的** pin 做 JSON
        Schema 校验。取不到 schema 正文就是 FAILED，**不当通过**（S19-16 / E25）。
        """
        proposal = result.get("proposal")
        blocked = forbidden_paths(proposal)
        if blocked:
            return ServiceError(
                422, "OUTPUT_CONTAINS_FORBIDDEN_FIELD",
                f"proposal contains forbidden root property {blocked[0]!r}",
                details={"property": blocked[0]})
        for name in ("source_refs", "assumptions", "unresolved_issues"):
            if not isinstance(result.get(name), list):
                return ServiceError(400, "INVALID_SCHEMA",
                                    f"result.{name} must be a list")
        task = self._task(tenant_id, task_id)
        schema_ref = _loads(task["output_schema_ref"], {})
        try:
            schema = self._upstream(
                lambda: self.schema_source(tenant_id, schema_ref),
                "SCHEMA_SOURCE_UNAVAILABLE")
        except ServiceError as exc:
            # 取不到正文 → 结构未知 → 结构非法。不降级，也不吞成 500。
            return {"schema_unavailable": str(exc), "code": exc.code}
        reason = _schema_error(proposal, schema)
        if reason is not None:
            return {"schema_error": reason}
        return None

    def _finish_in_tx(self, tx, tenant_id: str, task: dict, state: str,
                      stop_reason: str, *, result: dict | None = None) -> dict:
        """终态推进本体。调用方**必须**已在事务内。

        单独拆出来是因为 `cancel_task` 也要用：`ACCEPTED → CANCELLED` 是
        一条合法的立即终态路径（§6.4 第 4 条），而 `finish_task` 自己
        `BEGIN IMMEDIATE`，在事务里再调它会抛库错。
        """
        if task["state"] in TERMINAL_STATES:
            raise ServiceError(409, "TASK_NOT_RUNNING",
                               f"task is already {task['state']}")
        result_ref = None
        if result is not None:
            if state not in ("SUCCEEDED", "NEEDS_REVIEW"):
                raise ServiceError(
                    400, "INVALID_SCHEMA",
                    "only SUCCEEDED / NEEDS_REVIEW carry a task_result")
            result_ref = self._write_result(tx, tenant_id, task, result)
        sequence = int(task["publish_sequence"]) + 1
        updated = tx.conn.execute(
            "UPDATE agent_task SET state=?, stop_reason=?, result_ref=?,"
            " publish_sequence=?, finished_at=?, lease_owner=NULL,"
            " lease_expires_at=NULL, cost_state='FINAL'"
            " WHERE tenant_id=? AND task_id=?"
            " AND state IN ('ACCEPTED','RUNNING','CANCELLING')",
            (state, stop_reason,
             None if result_ref is None else _dumps(result_ref),
             sequence, _now(), tenant_id, task["task_id"]))
        if updated.rowcount != 1:
            raise ServiceError(409, "TASK_NOT_RUNNING",
                               "task reached a terminal state concurrently")
        self._emit(tx, tenant_id, task, state=state, stop_reason=stop_reason,
                   result_ref=result_ref, sequence=sequence)
        return {"result_ref": result_ref,
                "usage": {"steps_used": int(task["steps_used"]),
                          "tokens_in": int(task["tokens_in"]),
                          "tokens_out": int(task["tokens_out"]),
                          "cost_micro": int(task["cost_micro"]),
                          "cost_state": "FINAL"}}

    def _write_result(self, tx, tenant_id: str, task: dict, result: dict) -> dict:
        proposal = result.get("proposal")
        source_refs = result.get("source_refs")
        assumptions = result.get("assumptions")
        unresolved = result.get("unresolved_issues")
        for name, value, kind in (("source_refs", source_refs, list),
                                  ("assumptions", assumptions, list),
                                  ("unresolved_issues", unresolved, list)):
            if not isinstance(value, kind):
                raise ServiceError(400, "INVALID_SCHEMA",
                                   f"result.{name} must be a list")
        blocked = forbidden_paths(proposal)
        if blocked:
            # §4.2 / §7.2：输出污染 → 422，任务本身转 FAILED(STRUCTURE_INVALID)。
            raise ServiceError(
                422, "OUTPUT_CONTAINS_FORBIDDEN_FIELD",
                f"proposal contains forbidden root property {blocked[0]!r}",
                details={"property": blocked[0]})
        digest = result_content_hash(proposal, source_refs, assumptions, unresolved)
        existing = tx.conn.execute(
            "SELECT * FROM task_result WHERE tenant_id=? AND task_id=?",
            (tenant_id, task["task_id"])).fetchone()
        if existing is not None:
            # §7.4 第一行：已有成功结果 → 返回既有结果，不报错，也不写第二行。
            if existing["content_hash"] == digest:
                return {"id": existing["result_id"],
                        "revision": int(existing["revision"]),
                        "content_hash": existing["content_hash"]}
            raise ServiceError(409, "TASK_RESULT_IMMUTABLE",
                               "task already has a result with a different hash")
        result_id = result.get("result_id") or _new_id("result")
        tx.conn.execute(
            "INSERT INTO task_result (tenant_id, result_id, task_id, revision,"
            " proposal, source_refs, assumptions, unresolved_issues, confidence,"
            " suggested_actions, usage_trace, content_hash, created_at)"
            " VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?)",
            (tenant_id, result_id, task["task_id"], _dumps(proposal),
             _dumps(source_refs), _dumps(assumptions), _dumps(unresolved),
             result.get("confidence"),
             _dumps(result.get("suggested_actions") or []),
             _dumps(result.get("usage_trace") or {}), digest, _now()))
        return {"id": result_id, "revision": 1, "content_hash": digest}

    def _emit(self, tx, tenant_id: str, task: dict, *, state: str,
              stop_reason: str, result_ref: dict | None, sequence: int) -> None:
        """§4.3：**唯一**出站事件。只带引用，不带 `proposal` 正文。

        `usage` 严格按 `agent.task-completed.v2.schema.json` 的五个键发：
        `wall_seconds` / `tool_calls` / `tool_denials` 是 §4.1 **查询响应**里的
        字段，事件的 `usage` 是 `additionalProperties:false`，多一个键就不合契约。
        """
        context = _loads(task["execution_context"], {})
        data = {
            "role": task["role"],
            "state": state,
            "stop_reason": stop_reason,
            "root_task_id": task["root_task_id"],
            "correction_round": int(task["correction_round"]),
            "result_ref": result_ref,
            "usage": {"steps_used": int(task["steps_used"]),
                      "tokens_in": int(task["tokens_in"]),
                      "tokens_out": int(task["tokens_out"]),
                      "cost_micro": int(task["cost_micro"]),
                      "cost_state": "FINAL"},
            "node_id": context.get("node_id"),
            "node_epoch": context.get("node_epoch"),
            "execution_fence": context.get("execution_fence"),
        }
        event = new_event(
            source=SOURCE, type=EVENT_TASK_COMPLETED,
            subject=f"agent-task/{task['task_id']}",
            tenant_id=tenant_id, home_cell=context.get("home_cell") or "cell_a1",
            aggregate_id=task["task_id"], sequence=sequence,
            correlation_id=task["command_id"],
            causation_id=task["command_id"],
            data=data,
            # S19-13：SDK 的 `data_ref` 注解是 `str|None`，但 §4.3 要求对象。
            # 与 model-routing 一致传对象；envelope 不做类型校验。
            data_ref=result_ref,
            schema_version=SCHEMA_VERSION)
        self.outbox.append(event)

    # ------------------------------------------------------------ 内部

    def _task(self, tenant_id: str, task_id: str) -> dict:
        return self._task_in(self.conn, tenant_id, task_id)

    def _task_in(self, conn, tenant_id: str, task_id: str) -> dict:
        # 调用方有时传 `Transaction`，有时传裸连接。
        handle = getattr(conn, "conn", conn)
        row = handle.execute(
            "SELECT * FROM agent_task WHERE tenant_id=? AND task_id=?",
            (tenant_id, task_id)).fetchone()
        if row is None:
            # E28：跨租户与不存在同一个响应，不泄漏存在性。
            raise ServiceError(404, "NOT_FOUND", "task does not exist")
        return dict(row)

    def _published_definition(self, tenant_id: str, role: str,
                              revision: int) -> dict:
        container = self.conn.execute(
            "SELECT * FROM agent_definition WHERE tenant_id=? AND role=?",
            (tenant_id, role)).fetchone()
        if container is None or container["state"] != "ACTIVE":
            raise ServiceError(422, "DEFINITION_NOT_PUBLISHED",
                               f"role {role!r} has no active definition")
        if revision < 1 or revision > int(container["current_revision"]):
            raise ServiceError(422, "DEFINITION_NOT_PUBLISHED",
                               f"revision {revision} is not published for {role!r}")
        return self._definition_row(tenant_id, container["definition_id"], revision)

    def _definition_row(self, tenant_id: str, definition_id: str,
                        revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM agent_definition_revision"
            " WHERE tenant_id=? AND definition_id=? AND revision=?",
            (tenant_id, definition_id, revision)).fetchone()
        if row is None:
            raise ServiceError(422, "DEFINITION_NOT_PUBLISHED",
                               "definition revision does not exist")
        tools = [dict(t) for t in self.conn.execute(
            "SELECT tool_name, permission_key, scope_kind, required_scope,"
            " egress, max_calls FROM agent_definition_tool"
            " WHERE tenant_id=? AND definition_id=? AND revision=?"
            " ORDER BY tool_name", (tenant_id, definition_id, revision)).fetchall()]
        return {"definition_id": definition_id,
                "revision": int(row["revision"]),
                "role": row["role"],
                "system_template_ref": _loads(row["system_template_ref"], {}),
                "output_schema_ref": _loads(row["output_schema_ref"], {}),
                "required_capabilities": _loads(row["required_capabilities"], []),
                "limits": _loads(row["limits_json"], {}),
                "required_perspective": row["required_perspective"],
                "on_unresolved": row["on_unresolved"],
                "allow_schema_override": bool(row["allow_schema_override"]),
                "eval_set_ref": _loads(row["eval_set_ref"], {}),
                "content_hash": row["content_hash"],
                "tools": tools}

    def _render_result(self, row) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "result_id": row["result_id"],
            "task_id": row["task_id"],
            "revision": int(row["revision"]),
            "proposal": _loads(row["proposal"], {}),
            "source_refs": _loads(row["source_refs"], []),
            "assumptions": _loads(row["assumptions"], []),
            "unresolved_issues": _loads(row["unresolved_issues"], []),
            "confidence": row["confidence"],
            "suggested_actions": _loads(row["suggested_actions"], []),
            "usage_trace": _loads(row["usage_trace"], {}),
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
        }


# ------------------------------------------------------------ 辅助

def _scope_of(context: dict) -> tuple[str, str]:
    """把 `execution_context` 折成最窄的 scope。episode 优先。"""
    for kind, key in (("episode", "episode_id"), ("series", "series_id"),
                      ("project", "project_id")):
        if context.get(key):
            return kind, context[key]
    return "tenant", context.get("tenant_id", "")


def _scope_id_of(context: dict, scope_kind: str) -> str:
    mapping = {"tenant": "tenant_id", "org_unit": "org_unit_id",
               "project": "project_id", "series": "series_id",
               "episode": "episode_id"}
    return context.get(mapping.get(scope_kind, "tenant_id"), "")


def _egress_allowed(context: dict, target) -> bool:
    """§1.5 ③：外部检索类工具必须匹配企业出网策略。

    策略正文由 S02 解析，但 `:resolve-effective` 的返回里没有出网白名单
    （那是 gate_plan 的一部分）。所以这里只接受**显式**白名单：
    `context.egress_allowlist` 非空且 target 在其中。没有白名单即拒绝——
    出网默认关闭是 AGENTS.md 的开篇约束。
    """
    allowlist = context.get("egress_allowlist")
    if not isinstance(allowlist, list) or not allowlist:
        return False
    return bool(target) and target in allowlist


def forbidden_paths_from_schema(schema: dict) -> list[str]:
    """§2.6：`output_schema_ref` 的**根级**属性名不得是业务 ID。"""
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    return sorted(k for k in properties if k in FORBIDDEN_OUTPUT_KEYS)


def _limits_from_remaining(remaining: dict) -> str | None:
    """§3.7 第 4 步：用 checkpoint 里的 `remaining` 判闸，**不重算**。

    `remaining` 存的是**剩余**量，所以 `<= 0` 就是已经用尽。
    """
    if not isinstance(remaining, dict):
        return None
    if int(remaining.get("steps", 1)) <= 0:
        return "STEP_LIMIT"
    if int(remaining.get("tokens", 1)) <= 0:
        return "TOKEN_LIMIT"
    if int(remaining.get("cost_micro", 1)) <= 0:
        return "COST_LIMIT"
    return None


def _seconds_since(stamp: str) -> int:
    try:
        then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0
    return max(0, int((_now_dt() - then).total_seconds()))
