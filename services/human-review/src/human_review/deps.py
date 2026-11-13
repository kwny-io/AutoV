"""S40 的三个上游，全部是注入式 callback，不是 import。

AGENTS.md 禁止互相 import 业务 ORM / 跨库查询，所以只能走服务边界。三者的
真实契约与本方案假设的长度不一样，适配写在这里，**不让内核看到差异**：

- **S01** `GET /authorization-context?scope_kind=project&scope_id=prj_...`
  返回 `permissions`/`denied`，**不返回角色**。方案 §8.2 写的是"角色仍匹配"，
  但契约注册表里真正能判的是权限键 `review.decide`（缺口 G8）。适配层把它
  翻译成内核要的 `permissions` 列表，`roles` 留空 —— 职责分离因此退化，登记在
  G6/G8，不假装能判。
- **S02** 没有 `validate_review_decision`（缺口 G7）。不配置时内核 503，**不是
  默认 allow**。
- **S08** 的接受是**两步**：先 `POST /evidence/review` 把决定登记成证据，再
  `POST /manifests/{id}:accept` 带 `decision_ref` 走 CAS。方案 §5 把它画成一次
  `request_acceptance_CAS` 且返回 `{result: "committed"}`，那是不存在的形状
  （缺口 G9）。适配层做这两步，并把失败翻译成内核认识的 `result` 枚举。

未配置 base url 时返回必然失败的 callback。没有"忘了配就放行"的路径。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError

# S08 的 accept 要求 decision_ref 是 {service_id,id,revision,content_hash}，
# 且该行已在 review_evidence 里。S40 的决定内容哈希由自己算，S08 只把它当
# 不透明字符串存 —— 谁算的都能核对，但只有 S40 是权威（缺口 G10）。
DECISION_EVIDENCE_REF = "s40.human-review"


def _request(method: str, url: str, *, body: dict | None = None,
             headers: dict | None = None, timeout: float = 15.0) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=merged,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            pass
        raise ServiceError(exc.code, payload.get("code") or "UPSTREAM_ERROR",
                           payload.get("message") or str(exc),
                           retryable=bool(payload.get("retryable")),
                           details=payload.get("details") or {})
    except (urllib.error.URLError, OSError) as exc:
        # 内核靠"非 ServiceError 即不可达"判 503 并进重试队列，
        # 这里直接抛 ServiceError(503) 会绕过那条分支，所以用 URLError 原样抛。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _get(url: str, headers: dict | None = None, timeout: float = 10.0) -> dict:
    return _request("GET", url, headers=headers, timeout=timeout)


def _post(url: str, body: dict, headers: dict | None = None,
          timeout: float = 15.0) -> dict:
    return _request("POST", url, body=body, headers=headers, timeout=timeout)


# ---- S01 ---------------------------------------------------------------

def build_authz_source(base_url: str | None):
    """S01 `GET /authorization-context`。**实时**，不缓存（§8.2、E27）。"""
    if not base_url:
        def _unconfigured(tenant_id: str, principal_id: str, task: dict) -> dict:
            raise ServiceError(
                503, "AUTHZ_UNCONFIGURED",
                "S01_BASE_URL is not configured; decisions cannot be validated")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _authz(tenant_id: str, principal_id: str, task: dict) -> dict:
        binding = task.get("binding")
        binding = json.loads(binding) if isinstance(binding, str) else (binding or {})
        subject = task.get("subject")
        subject = json.loads(subject) if isinstance(subject, str) else (subject or {})
        scope_kind = subject.get("scope_kind") or "project"
        scope_id = subject.get("scope_id") or subject.get("project_id")
        if not scope_id:
            # 不知道作用域就无法判定授权。宁可失败，不要拿租户级权限冒充。
            raise ServiceError(
                422, "TASK_SCOPE_MISSING",
                "review task subject carries no scope_id/project_id; "
                "authorization cannot be scoped")
        query = urllib.parse.urlencode({
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "principal_id": principal_id,
            "purpose": "review.decide",
        })
        result = _get(f"{prefix}/authorization-context?{query}",
                      headers={"X-Tenant-Id": tenant_id,
                               "X-Principal-Id": principal_id})
        # S01 不返回 roles（G8）。内核的职责分离检查因此只能依赖投票时的
        # actor.roles，缺了就判不满足。
        return {**result, "roles": result.get("roles") or []}
    return _authz


# ---- S02 ---------------------------------------------------------------

def build_policy_validator(base_url: str | None):
    """S02 的 `validate_review_decision`。

    **这个接口今天不存在**（缺口 G7）：S02 只有
    `/policy-decisions:resolve-effective`、`/projects/{id}/effective-policy`、
    `/permission-checks`。配了 url 也调不通 —— 所以这里**总是**返回 abstain 的
    callback，而不是假装调一个不存在的路径。等 S02 补了这个面再替换。

    返回 abstain 而不是 allow，是 E25 的落点：未知不能当通过。
    """
    def _validator(tenant_id: str, task: dict, body: dict, authz: dict) -> dict:
        return {
            "verdict": "abstain",
            "reason": "POLICY_VALIDATOR_UNCONFIGURED",
            "detail": "S02 has no validate_review_decision surface (S40 G7); "
                      "no verdict can be produced, so no vote is recorded",
            "base_url_configured": bool(base_url),
        }
    return _validator


# ---- S08 ---------------------------------------------------------------

def _decision_hash(decision: dict) -> str:
    """决定内容哈希。S08 只把它当不透明串存，核对权威在 S40。"""
    import hashlib
    canonical = json.dumps(
        {k: decision.get(k) for k in
         ("decision_id", "review_task_id", "decision", "option_id", "reason",
          "decided_against", "recorded_at")},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_VERDICT_OF = {"accept": "approved", "reject": "rejected",
               "request_repair": "rejected"}


def build_acceptance_cas(base_url: str | None):
    """S08 接受，**两步**：登记证据 → CAS。返回内核认识的 `result` 枚举。

    归一化表（方案 §5 的四值），映射自 S08 真实错误码：

    | S08 | 内核 result |
    |---|---|
    | 2xx + acceptance_commit_id | committed |
    | 409 STALE_EXECUTION_FENCE    | conflict |
    | 409 VERSION_CONFLICT         | conflict |
    | 409 ILLEGAL_STATE_TRANSITION | conflict |
    | 409 STALE_DECISION           | stale_epoch |
    | 422 NODE_LOCKED              | locked |
    | 其它 4xx                     | BINDING_MISMATCH（走默认分支）|
    | 5xx / 不可达                  | 抛，内核转 503 并进重试队列 |

    `STALE_DECISION` 归到 `stale_epoch` 是**刻意保守**：它是 S08 说"这份决定
    覆盖的节点 epoch 已经不是当前值"，与 stale_epoch 语义一致，而 S08 没有单独
    的 stale_epoch 错误码。
    """
    if not base_url:
        return None

    prefix = base_url.rstrip("/")

    def _accept(tenant_id: str, task: dict, command_id: str) -> dict:
        binding = task.get("binding")
        binding = json.loads(binding) if isinstance(binding, str) else (binding or {})
        manifest_ref = binding.get("manifest_ref") or {}
        manifest_id = manifest_ref.get("id")
        if not manifest_id:
            raise ServiceError(
                422, "TASK_BINDING_INCOMPLETE",
                "binding.manifest_ref.id is required to release to S08")
        node_id = binding.get("node_id")
        if not node_id:
            raise ServiceError(
                422, "TASK_BINDING_INCOMPLETE",
                "binding.node_id is required to release to S08")

        decisions = _applied_or_recorded_decisions(task)
        if not decisions:
            raise ServiceError(
                409, "NO_DECISION_TO_RELEASE",
                "no RECORDED decision on this task")

        # 只有 accept 才把指针往前推。reject/request_repair 释放给 S08 只会
        # 产生一条 REJECTED 的审计行 —— S08 的 accept 只认 approved 决定
        #（_check_decision_against 里 verdict != approved 直接 stale）。
        accepted = [d for d in decisions if d.get("decision") == "accept"]
        if not accepted:
            return {"result": "recorded_only",
                    "detail": "the recorded decisions are not accepts; S08 "
                              "does not advance the pointer for these"}

        head_decision = accepted[-1]
        evidence_ref = {
            "service_id": DECISION_EVIDENCE_REF,
            "id": head_decision["decision_id"],
            "revision": 1,
            "content_hash": _decision_hash(head_decision),
        }
        node_epochs = {node_id: binding.get("node_epoch")}
        artifact_hash = (binding.get("artifact") or {}).get("content_hash")
        _post(f"{prefix}/evidence/review", {
            "decision_id": head_decision["decision_id"],
            "decision_revision": 1,
            "content_hash": evidence_ref["content_hash"],
            "manifest_id": manifest_id,
            "manifest_revision": manifest_ref.get("revision"),
            "execution_fence": binding.get("execution_fence"),
            "verdict": _VERDICT_OF.get(head_decision["decision"], "rejected"),
            "node_epochs": node_epochs,
            "artifact_hashes": ({node_id: artifact_hash}
                                if artifact_hash else {}),
        }, headers={"X-Tenant-Id": tenant_id,
                    "Idempotency-Key": f"{command_id}:evidence"})

        actor = head_decision.get("actor") or {}
        try:
            result = _post(f"{prefix}/manifests/{manifest_id}:accept", {
                "node_ids": [node_id],
                "expected_revision": manifest_ref.get("revision"),
                "expected_fence": binding.get("execution_fence"),
                "decision_ref": evidence_ref,
                "policy_ref": binding.get("policy_ref"),
                "actor": {"principal_id": actor.get("principal_id") or "unknown"},
                "qc_report_refs": [],
                "command_id": command_id,
            }, headers={"X-Tenant-Id": tenant_id,
                        "Idempotency-Key": command_id})
        except ServiceError as exc:
            code = exc.code
            if exc.status >= 500:
                raise
            if code in ("STALE_EXECUTION_FENCE", "VERSION_CONFLICT",
                        "ILLEGAL_STATE_TRANSITION"):
                return {"result": "conflict", "code": code,
                        "detail": exc.details}
            if code == "STALE_DECISION":
                return {"result": "stale_epoch", "code": code,
                        "detail": exc.details}
            if code == "NODE_LOCKED":
                return {"result": "locked", "code": code,
                        "detail": exc.details}
            raise
        return {"result": "committed",
                "acceptance_commit_id": result.get("acceptance_commit_id"),
                "accepted_revision": result.get("accepted_revision"),
                "rebased": result.get("rebased")}
    return _accept


def _applied_or_recorded_decisions(task: dict) -> list[dict]:
    """内核传进来的 task 里没有决定 —— 由 service 在调用前注入 `_decisions`。"""
    return task.get("_decisions") or []


# ---- S08 node diff -----------------------------------------------------

def build_node_diff(base_url: str | None):
    """§6.4：manifest revision 前进但本节点没变 → **不该** stale。

    S08 已经提供 `GET /manifests/{id}/node-diff`（缺口 G3 部分解决）。
    返回"本节点是否受影响"的 bool；查询本身失败时抛，让内核退化成严格判定。
    """
    if not base_url:
        return None

    prefix = base_url.rstrip("/")

    def _node_diff(manifest_id: str, from_revision, to_revision,
                   node_id) -> bool:
        if not manifest_id or node_id is None:
            return True  # 判不了就当受影响：更严
        query = urllib.parse.urlencode({
            "from_revision": from_revision,
            "to_revision": to_revision,
            "node_id": node_id,
        })
        result = _get(f"{prefix}/manifests/{manifest_id}/node-diff?{query}")
        nodes = result.get("nodes") or []
        for node in nodes:
            if node.get("node_id") == node_id:
                return node.get("change") not in (None, "UNCHANGED")
        return True  # 没查到这个节点 → 当作受影响
    return _node_diff
