"""S19 的上游，全部注入式 callback，不 import（AGENTS.md 禁止跨库）。

七条上游读路径 + 一条写路径，形状都是**读上游代码核对过的**：

- **S01** `POST /permission-checks` 返回 `[{permission_key, allowed, reason,
  decision_id}]`，不是 §3.3 伪代码里的 `verdict.results[0].verdict`（S19-11）。
  一次最多 32 条（S01 §2.6），所以调用方必须按步聚合，不能逐工具往返。
- **S02** `POST /policy-decisions:resolve-effective` 是唯一可用的准入面：
  传 `context.policy_ref` 会先校验 pin 仍是当前版，然后解析生效策略。
  `:evaluate` 不存在（C4），所以「策略拒绝任务」走不到。
- **S18** `GET /context-packs/{id}?revision=` 返回
  `{pack, prohibited_assumption_details, x-contract-gaps}`。`pack` 里
  `perspective` 是**标量字符串**、`source_watermark` 是**整数**、
  `checkpoint` 是 `{commit_sequence, content_hash}`（S19-14）。
- **S26** `POST /generation-jobs` 的 body 必须同时满足
  `COMMAND_REQUIRED` 十项与 `CONTEXT_REQUIRED` 十八项；`GET
  /generation-jobs/{id}` 返回 14 个字段，其中 `must_reconcile_before_resubmit`
  就是 `state == 'SUBMISSION_UNKNOWN'`。
- **S20** `GET /templates/{id}/revisions/{revision}` 取模板正文。
- **S45** 审计外发**可降级**（§7.5 唯一一条），失败只记日志不阻塞。
- **工具 broker** 与 **proposal schema 注册表**在仓库里都还不存在
  （S19-16 / C7），所以它们未配置时是**必然失败**，不是空实现。

未配置一律 503，没有「忘了配就给默认值」的路径。§7.5 的六项里只有审计可降级。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError


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
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S01 授权

def build_permission_source(base_url: str | None):
    """`(tenant_id, principal_id, checks, purpose) -> [verdict]`。

    一次最多 32 条。超过就分批发，而不是截断——截断等于**默认放行**了没问到的
    工具（S01 §2.6 只给了上限，没说超出怎么处理）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AUTHORIZATION_UNAVAILABLE",
                "S01_BASE_URL is not configured; S19 will not run an agent "
                "without an authorization decision (plan §7.5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _check(tenant_id: str, principal_id: str, checks: list[dict],
               purpose: str | None = None) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(checks), 32):
            chunk = checks[start:start + 32]
            body: dict = {"checks": chunk}
            if purpose:
                body["purpose"] = purpose
            result = _request("POST", f"{prefix}/permission-checks", body=body,
                              headers={"X-Tenant-Id": tenant_id,
                                       "X-Principal-Id": principal_id},
                              timeout=10.0)
            verified = result.get("results")
            if not isinstance(verified, list):
                raise ServiceError(
                    503, "AUTHORIZATION_UNAVAILABLE",
                    "S01 returned no results array; an unreadable verdict is "
                    "not an allow")
            out.extend(verified)
        return out
    return _check


# ---------------------------------------------------------------- S02 策略

def build_policy_source(base_url: str | None):
    """`(tenant_id, context) -> resolved 生效策略`。

    只用于**准入核验**：pin 的 `policy_ref` 仍须是当前版，且企业策略必须存在。
    没有 `:evaluate`，所以这里不产生「允许/拒绝」的裁决（C4）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                "S02_BASE_URL is not configured; S19 will not admit an agent "
                "task against an unverified policy (plan §7.5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _resolve(tenant_id: str, context: dict) -> dict:
        result = _request("POST", f"{prefix}/policy-decisions:resolve-effective",
                          body={"context": context},
                          headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if not isinstance(result.get("policy_ref"), dict):
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                "S02 resolved no policy_ref; S19 cannot record which policy "
                "the task was admitted under")
        return result
    return _resolve


# ---------------------------------------------------------------- S18 上下文包

def build_pack_source(base_url: str | None):
    """`(tenant_id, pack_id, revision) -> {pack, ...}`。

    `pack.perspective` 是标量、`pack.source_watermark` 是整数。调用方按这两个
    字段做 `PERSPECTIVE_MISMATCH` / `CONTEXT_PACK_STALE`（S19-14），不要假设
    它们与方案散文里的形状一致。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CONTEXT_UNAVAILABLE",
                "S18_BASE_URL is not configured; an agent without a context "
                "pack will invent continuity (plan §7.5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _pack(tenant_id: str, pack_id: str, revision: int | None = None) -> dict:
        url = f"{prefix}/context-packs/{urllib.parse.quote(str(pack_id))}"
        if revision is not None:
            url += "?" + urllib.parse.urlencode({"revision": int(revision)})
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=15.0)
        if not isinstance(result.get("pack"), dict):
            raise ServiceError(
                503, "CONTEXT_UNAVAILABLE",
                "S18 returned no pack body; S19 will not run against an "
                "empty context")
        return result
    return _pack


# ---------------------------------------------------------------- S20 模板

def build_template_source(base_url: str | None):
    """`(tenant_id, ref) -> 模板正文`。

    §7.5：S20 读不到就不创建任务。**不缓存**，因为模板 revision 是定义的一部分，
    缓存会让「pin 的 revision」与「实际用的正文」分家。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "TEMPLATE_UNAVAILABLE",
                "S20_BASE_URL is not configured; the definition's system "
                "template cannot be resolved (plan §7.5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _template(tenant_id: str, ref: dict) -> dict:
        template_id = ref.get("id")
        revision = ref.get("revision")
        if not template_id or revision is None:
            raise ServiceError(422, "INVALID_SCHEMA",
                               "system_template_ref needs id and revision")
        result = _request(
            "GET", f"{prefix}/templates/{urllib.parse.quote(str(template_id))}"
                   f"/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if not isinstance(result.get("template"), dict):
            raise ServiceError(
                503, "TEMPLATE_UNAVAILABLE",
                "S20 returned no template body for the pinned revision")
        return result
    return _template


# ---------------------------------------------------------------- S26 生成

def build_generation_source(base_url: str | None):
    """返回两个 callback：`submit(tenant_id, command_id, body)` 与
    `get(tenant_id, provider_job_id)`。

    提交时 `Idempotency-Key` 用调用方给的确定性值（`task_id:step_index`），
    这样「同一步重放」不会变成第二个付费任务。恢复路径**只读**
    `SUBMISSION_UNKNOWN`，绝不重提（§3.7 第 2 步 / S26 §7.2 铁律）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "GENERATION_UNAVAILABLE",
                "S26_BASE_URL is not configured; S19 will not bypass S26 to "
                "call a model directly (AGENTS.md red line, plan §7.5)")
        return _unconfigured, _unconfigured

    prefix = base_url.rstrip("/")

    def _submit(tenant_id: str, idempotency_key: str, body: dict) -> dict:
        result = _request("POST", f"{prefix}/generation-jobs", body=body,
                          headers={"X-Tenant-Id": tenant_id,
                                   "Idempotency-Key": idempotency_key},
                          timeout=30.0)
        job_id = result.get("provider_job_id")
        if not job_id:
            raise ServiceError(
                503, "GENERATION_UNAVAILABLE",
                "S26 accepted the command but returned no provider_job_id; "
                "S19 cannot checkpoint a job it cannot name")
        return result

    def _get(tenant_id: str, provider_job_id: str) -> dict:
        return _request(
            "GET", f"{prefix}/generation-jobs/"
                   f"{urllib.parse.quote(str(provider_job_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _submit, _get


# ---------------------------------------------------------------- proposal schema

def build_schema_source(base_url: str | None):
    """`(tenant_id, output_schema_ref) -> schema 正文`。

    仓库里没有 proposal schema 注册表（S19-16），所以未配置即失败：未知的
    输出结构**不能当作通过**（E25 的同一逻辑）。取不到正文的任务
    `FAILED(STRUCTURE_INVALID)`，而不是「跳过校验」。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "SCHEMA_SOURCE_UNAVAILABLE",
                "no proposal schema registry is configured; S19 will not run "
                "an agent whose output cannot be validated (S19-16)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _schema(tenant_id: str, ref: dict) -> dict:
        schema_id = ref.get("id")
        revision = ref.get("revision")
        if not schema_id or revision is None:
            raise ServiceError(422, "INVALID_SCHEMA",
                               "output_schema_ref needs id and revision")
        result = _request(
            "GET", f"{prefix}/proposal-schemas/{urllib.parse.quote(str(schema_id))}"
                   f"/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        schema = result.get("schema")
        if not isinstance(schema, dict):
            raise ServiceError(
                503, "SCHEMA_SOURCE_UNAVAILABLE",
                "proposal schema registry returned no schema body for the "
                "pinned revision")
        return schema
    return _schema


# ---------------------------------------------------------------- 工具 broker

def build_tool_source(base_url: str | None):
    """`(tenant_id, tool_name, arguments, ttl) -> {result, ...}`。

    工具 broker 在仓库里还不存在（C7 只登记了名字与 scope）。未配置即
    `TOOL_NOT_ALLOWED` 级别的失败——**不发请求**，因为「假装调用了」比拒绝更坏。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "TOOL_BROKER_UNAVAILABLE",
                "no tool broker is configured; S19 will not simulate a tool "
                "call it cannot audit (C7)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _invoke(tenant_id: str, tool_name: str, arguments: dict,
                ttl: int = 60) -> dict:
        return _request("POST", f"{prefix}/tools/{urllib.parse.quote(tool_name)}:invoke",
                        body={"arguments": arguments, "ttl_seconds": ttl},
                        headers={"X-Tenant-Id": tenant_id}, timeout=30.0)
    return _invoke


# ---------------------------------------------------------------- S45 审计外发

def build_audit_sink(base_url: str | None):
    """`(tenant_id, record) -> None`。**可降级**（§7.5 唯一一条）。

    未配置时返回一个不做事、**不抛异常**的 callback：本地 `tool_call_audit`
    已经落库，外发失败只意味着补投台账少一条，不参与任何决策。
    """
    if not base_url:
        def _noop(*_args, **_kwargs):
            return None
        return _noop

    prefix = base_url.rstrip("/")

    def _sink(tenant_id: str, record: dict) -> None:
        try:
            _request("POST", f"{prefix}/audit-records", body=record,
                     headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        except (ServiceError, ConnectionError, OSError):
            # 审计外发不阻塞执行。吞掉是刻意的：抛出去会让一次已经成功的
            # 工具调用变成任务失败，而本地审计行已经在了。
            return None
    return _sink
