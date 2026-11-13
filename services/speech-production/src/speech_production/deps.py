"""上游回调。**本服务不直连任何供应商，也不付任何钱。**

四个上游，四份职责，不能混：

- **S15**（voice-assets）：音色授权与词典。`check-eligibility` 是强一致读，
  五个复查时点全部走它。它返回的 `eligible` 是唯一放行依据；本服务存的
  `VoiceBindingRef` 只用于事后解释。
- **S25**（model-routing）：路由决策。只读，不产生费用。
- **S26**（generation-jobs）：**唯一**提交付费任务的地方。本服务不给供应商
  发 HTTP，也不知道供应商是谁。
- **S28**（artifact-ingest）：归档产物是否仍可引用。`None` = 不知道。

未配置的上游返回 503 且带自己的 code，绝不静默降级成"可以用"。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .service import ServiceError

DEFAULT_TIMEOUT = 15.0


def _request(method: str, url: str, *, body=None, headers=None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    payload = None
    hdrs = {"accept": "application/json"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["content-type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=payload, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        raise ServiceError(
            exc.code, payload.get("code") or "UPSTREAM_ERROR",
            payload.get("message") or f"upstream returned {exc.code}",
            retryable=bool(payload.get("retryable")),
            details=payload.get("details") or {}) from exc
    except (urllib.error.URLError, OSError) as exc:
        # 连接层失败 → ConnectionError。service.py 把它读成"不可达"，而
        # "不可达"永远不等于"可用"（E30）。
        raise ConnectionError(f"{method} {url}: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ConnectionError(f"{method} {url}: non-JSON response") from exc


def _unconfigured(code: str, message: str):
    def _fail(*_args, **_kwargs):
        raise ServiceError(503, code, message, retryable=True)
    return _fail


# ------------------------------------------------------------------ S15

def build_eligibility(base_url: str | None, *, tenant_header="x-tenant-id"):
    """`POST /voices/{id}:check-eligibility`。

    S15 的这条路由**没有幂等**（它是读），所以不带 `command_id`。带一个假的
    幂等键会让 S15 在同一个 key 上缓存旧结论，那时点 3/4 就白查了。

    `if_state_seq` 刻意不传：我们要的是**最新**状态，不是"与某个版本比较"。
    """
    if not base_url:
        return _unconfigured("VOICE_ELIGIBILITY_UNAVAILABLE",
                             "S15 is not configured")

    root = base_url.rstrip("/")

    def _call(tenant_id: str, voice_id: str, voice_revision: int,
              requested_use: dict) -> dict:
        body = {"voice_revision": int(voice_revision),
                "requested_use": requested_use}
        return _request("POST",
                        f"{root}/voices/{voice_id}:check-eligibility",
                        body=body, headers={tenant_header: tenant_id})

    return _call


def build_lexicon_reader(base_url: str | None, *,
                         tenant_header="x-tenant-id"):
    """`GET /lexicons/{id}/revisions/{revision}`。取回 `content_hash` 与 scope。

    词典的**内容**不进来：S31 只把 `{id, revision}` 交给 S26，由供应商侧
    解析。这里要 hash 与 scope 是为了指纹和范围校验。
    """
    if not base_url:
        return _unconfigured("LEXICON_UNAVAILABLE", "S15 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, lexicon_id: str, revision: int) -> dict:
        return _request(
            "GET", f"{root}/lexicons/{lexicon_id}/revisions/{int(revision)}",
            headers={tenant_header: tenant_id})

    return _read


def build_bindings_reader(base_url: str | None, *,
                          tenant_header="x-tenant-id"):
    """`GET /voices/{id}/bindings`：拿音色的默认词典绑定。

    这个接口同时承担 (c) 的跨租户判定：别的租户的音色在这里是 404。
    """
    if not base_url:
        return _unconfigured("VOICE_BINDINGS_UNAVAILABLE",
                             "S15 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, voice_id: str) -> dict:
        return _request("GET", f"{root}/voices/{voice_id}/bindings",
                        headers={tenant_header: tenant_id})

    return _read


# ------------------------------------------------------------------ S25

def build_route_resolver(base_url: str | None, *,
                         tenant_header="x-tenant-id"):
    """`POST /routes:resolve` → 归一化成内核读得懂的形状。

    归一化的三件事：
    - `eligible[0]` 提成 `selected`（S25 的原始响应是候选列表）
    - `capabilities` **只从显式布尔取**：缺失不等于 false。把 `None` 读成
      false 会让 `require_alignment` 在路由没声明能力时被误拒；读成 true
      则更糟（把没有的能力当成有）。
    - `feasible` = 任何一项显式为 False 就是 False
    """
    if not base_url:
        return _unconfigured("ROUTE_UNAVAILABLE", "S25 is not configured")

    root = base_url.rstrip("/")

    def _resolve(tenant_id: str, request: dict) -> dict:
        raw = _request("POST", f"{root}/routes:resolve", body=request,
                       headers={tenant_header: tenant_id})
        eligible = raw.get("eligible") or []
        first = eligible[0] if eligible else {}
        profile = first.get("execution_profile") or {}
        capabilities = {key: profile[key] for key in
                        ("word_timestamps", "ssml", "multi_speaker",
                         "voice_cloning", "streaming")
                        if isinstance(profile.get(key), bool)}
        rejected = raw.get("rejected") or []
        rejected_codes = {str(item.get("code") or "").upper()
                          for item in rejected if isinstance(item, dict)}
        residency_allowed = None
        if any("RESIDENCY" in code or "REGION" in code
               for code in rejected_codes):
            residency_allowed = False
        flags = [first.get("feasible")] + [
            item.get("feasible") for item in rejected
            if isinstance(item, dict)]
        feasible = None
        if any(value is False for value in flags):
            feasible = False
        elif any(value is True for value in flags):
            feasible = True
        route = first.get("route") or {}
        return {
            "feasible": feasible,
            "degradation_options": raw.get("degradation_options") or [],
            "capabilities": capabilities,
            "residency_allowed": residency_allowed,
            "route_decision_ref": raw.get("route_decision_id")
            or raw.get("decision_id") or route.get("route_id"),
            "policy_ref": raw.get("route_policy_ref") or request.get(
                "route_policy_ref"),
            "quote_valid_to": route.get("quote_valid_to")
            or first.get("quote_valid_to"),
            "selected": first or None,
        }

    return _resolve


# ------------------------------------------------------------------ S26

def build_submitter(base_url: str | None, *, tenant_header="x-tenant-id"):
    """`POST /generation-jobs`。**付费任务只从这里出去。**

    幂等键由 service.py 生成（`attempt_id` / `idempotency_key` 都带业务尝试
    号），所以同一次业务重试不会撞上 S26 的去重；而**同一次**业务尝试重放
    会被 S26 吞掉——这正是要的。

    返回必须带一个可对账的 id。没有 → 503：S26 说收下了却给不出 id，等同
    提交结果不明，绝不能当成功。
    """
    if not base_url:
        return _unconfigured("GENERATION_JOBS_UNAVAILABLE",
                             "S26 is not configured")

    root = base_url.rstrip("/")

    def _submit(tenant_id: str, command: dict, route_decision_ref) -> dict:
        body = {"command_id": command.get("idempotency_key"),
                "generation_command": command,
                "route_decision_ref": route_decision_ref}
        raw = _request("POST", f"{root}/generation-jobs", body=body,
                       headers={tenant_header: tenant_id})
        if not (raw.get("provider_job_id") or raw.get("generation_job_id")):
            raise ServiceError(503, "GENERATION_JOB_ID_MISSING",
                               "S26 accepted the command but returned no "
                               "job id", retryable=True)
        return raw

    return _submit


def build_canceler(base_url: str | None, *, tenant_header="x-tenant-id"):
    if not base_url:
        return _unconfigured("GENERATION_CANCEL_UNAVAILABLE",
                             "S26 is not configured")

    root = base_url.rstrip("/")

    def _cancel(tenant_id: str, provider_job_id: str, reason: str) -> dict:
        return _request(
            "POST", f"{root}/generation-jobs/{provider_job_id}:cancel",
            body={"reason_code": reason},
            headers={tenant_header: tenant_id})

    return _cancel


def build_generation_state(base_url: str | None, *,
                           tenant_header="x-tenant-id"):
    """`GET /generation-jobs/{id}`：对账用。**只读**。"""
    if not base_url:
        return _unconfigured("GENERATION_STATUS_UNAVAILABLE",
                             "S26 is not configured")

    root = base_url.rstrip("/")

    def _read(tenant_id: str, provider_job_id: str) -> dict | None:
        try:
            return _request(
                "GET", f"{root}/generation-jobs/{provider_job_id}",
                headers={tenant_header: tenant_id})
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise

    return _read


# ------------------------------------------------------------------ S28

def build_artifact_check(base_url: str | None, *,
                         tenant_header="x-tenant-id"):
    """归档产物是否仍 `REFERENCEABLE`。

    三态是刻意的：
    - `True`  → 可以复用
    - `False` → 不可复用（tombstone / 已删除）
    - `None`  → 未配置，不知道

    本服务的复用规则把 `None` 当**不可复用**处理：宁可多花一次合成的钱，
    也不把一个自己没校验过的对象递给下游（E23）。
    """
    if not base_url:
        return _unconfigured("ARTIFACT_CHECK_UNAVAILABLE",
                             "S28 is not configured")

    root = base_url.rstrip("/")

    def _check(tenant_id: str, artifact_ref: dict) -> bool | None:
        if not isinstance(artifact_ref, dict) or not artifact_ref.get("id"):
            return False
        raw = _request(
            "GET", f"{root}/artifacts/{artifact_ref['id']}",
            headers={tenant_header: tenant_id})
        state = str(raw.get("state") or "").upper()
        if not state:
            return None
        return state in ("AVAILABLE", "REFERENCEABLE")

    return _check


def build_all(env=None) -> dict:
    """从环境变量组装全部回调。全部未配置时服务仍可起（每个回调各自 503）。"""
    env = env if env is not None else os.environ
    s15 = env.get("S15_BASE_URL")
    s25 = env.get("S25_BASE_URL")
    s26 = env.get("S26_BASE_URL")
    s28 = env.get("S28_BASE_URL")
    return {
        "eligibility": build_eligibility(s15),
        "lexicon_reader": build_lexicon_reader(s15),
        "bindings_reader": build_bindings_reader(s15),
        "route_resolver": build_route_resolver(s25),
        "submitter": build_submitter(s26),
        "canceler": build_canceler(s26),
        "generation_state": build_generation_state(s26),
        "artifact_check": build_artifact_check(s28),
    }
