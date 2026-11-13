"""S25 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

三个上游、五条读路径的全部形状都是**读上游代码核对过的**，不是从方案里抄的：

- **S24** `services/model-registry`：
  - `GET /model-candidates` 返回 `{capability_bundle_revision, projection_watermark,
    candidates[], excluded[]}`。候选体**没有** `mode_details`、`combination_key`、
    `provider_idempotency`、`url_ttl_seconds`（C24-5 明确不发布）。
  - `GET /capability-snapshots/{id}` 才有 `mode_details`（含 `reference_rules` /
    `output_specs` / `capability_claims`）。**没有它 §3 的第 9–13 条过滤全部
    无从判断**，所以这一跳不是可选的。
  - `POST /capability-checks` 是唯一能拿到 `combination_key` 的地方，而
    §12 要求 S26 校验 `selected.combination_key`。它需要幂等头。
  - `POST /capability-validity` 批量回查，不要幂等头。
- **S02** `GET /policies/{id}/revisions/{revision}` 返回 `{policy_ref, policy}`；
  `policy` 就是策略正文。
- **S23** `GET /budgets/{id}/availability` 返回 `available_micro` 等。

**未配置一律必然失败**（S24 / S02 / S23），没有"忘了配就给默认值"的路径：
§9 的三条 503 分支正是为了这个。唯一的例外是 `telemetry`——仓库里根本没有这个
数据源（MRT-15/MRT-16），它返回空 dict，让过滤 14/17 走"判不了就跳过、
并记账"的分支，而不是伪造 p90。
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


# ---------------------------------------------------------------- S02 策略

def build_policy_source(base_url: str | None):
    """`(tenant_id, policy_ref) -> {"flags":..., "weights":..., "thresholds":...}`。

    返回体里**必须有 `flags`**：`service._resolve_policy` 据此区分"策略真的没有
    开关"与"上游给了一份不能用的东西"。空 dict 不等于"没有限制"。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "POLICY_UNAVAILABLE",
                "S02_BASE_URL is not configured; S25 will not fall back to "
                "built-in routing defaults, because a default policy may "
                "relax a tenant's hard constraints (plan §9)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _resolve(tenant_id: str, ref: dict) -> dict:
        policy_id = ref.get("id")
        revision = ref.get("revision")
        if not policy_id or revision is None:
            raise ServiceError(422, "INVALID_GENERATION_COMMAND",
                               "context.policy_ref needs id and revision")
        result = _request(
            "GET", f"{prefix}/policies/{urllib.parse.quote(str(policy_id))}"
                   f"/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        document = result.get("policy")
        if not isinstance(document, dict):
            raise ServiceError(503, "POLICY_UNAVAILABLE",
                               "S02 returned a policy revision with no document")
        # 策略正文里的路由段落。没有 `routing` 段时给空 flags——这是"策略没说"，
        # 与"读不到策略"是两件事，后者已经在上面 503 了。
        routing = document.get("routing") or {}
        return {"flags": dict(routing.get("flags") or {}),
                "weights": routing.get("weights"),
                "thresholds": dict(routing.get("thresholds") or {}),
                "document_ref": result.get("policy_ref")}
    return _resolve


# ---------------------------------------------------------------- S23 预算

def build_budget_source(base_url: str | None):
    """`(tenant_id, budget_id) -> availability`。

    只读 `available_micro`；**不做任何本地缓存**。§12 第 2 条要求 S26 派单前
    实时回查，S25 自己的读数也一样——缓存一次余额就等于让 OVER_BUDGET
    变成"几分钟前没超"。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "BUDGET_SOURCE_UNAVAILABLE",
                "S23_BASE_URL is not configured; S25 will not skip the "
                "OVER_BUDGET filter (plan §9)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _availability(tenant_id: str, budget_id: str) -> dict:
        return _request(
            "GET", f"{prefix}/budgets/{urllib.parse.quote(str(budget_id))}"
                   f"/availability",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _availability


# ---------------------------------------------------------------- S24 能力

def build_capability_source(base_url: str | None):
    """`(tenant_id, req, flags) -> candidates 响应`。

    `residency` 与 `allow_byok` 来自**策略**而不是请求：策略要求的驻留标签
    必须传给 S24 做预筛，否则 S25 会拿到一批注定被第 5 条拒掉的候选。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "S24_BASE_URL is not configured; S25 will not route against "
                "cached capability conclusions (plan §9)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _candidates(tenant_id: str, req: dict, flags: dict | None = None) -> dict:
        flags = flags or {}
        query = {"tenant_id": tenant_id, "modality": req.get("modality")}
        if req.get("mode"):
            query["mode"] = req["mode"]
        if req.get("aspect_ratio"):
            query["aspect_ratio"] = req["aspect_ratio"]
        if req.get("duration_seconds") is not None:
            query["duration_seconds"] = req["duration_seconds"]
        if req.get("home_region"):
            query["home_region"] = req["home_region"]
        capabilities = req.get("required_capabilities") or []
        if capabilities:
            query["required_capabilities"] = ",".join(capabilities)
        residency = flags.get("required_residency") or []
        if residency:
            query["residency"] = ",".join(residency)
        if flags.get("allow_byok") is False:
            query["allow_byok"] = "false"
        url = f"{prefix}/model-candidates?{urllib.parse.urlencode(query)}"
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
    return _candidates


def build_snapshot_source(base_url: str | None):
    """`(tenant_id, snapshot_id) -> snapshot，含 mode_details`。

    §3 第 9–13 条（参考形状 / 画幅 / 分辨率 / 时长 / 声音）**全部**依赖
    `mode_details`，而 `/model-candidates` 的候选体不带它。少了这一跳，这五条
    过滤会因为 `detail is None` 而被静默跳过——候选照样进 `eligible[]`。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "S24_BASE_URL is not configured; without mode_details filters "
                "9-13 of plan §3 cannot be evaluated")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _snapshot(tenant_id: str, snapshot_id: str) -> dict:
        return _request(
            "GET", f"{prefix}/capability-snapshots/"
                   f"{urllib.parse.quote(str(snapshot_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
    return _snapshot


def build_combination_source(base_url: str | None):
    """`(tenant_id, command_id, snapshot_id, req) -> {combination_key, verdict}`。

    唯一能拿到 `combination_key` 的路径，而 §12 第 3 条要求 S26 校验它。
    `check_capability` 在模式不支持时抛 404 `MODE_NOT_FOUND`、在画幅不支持时
    正常返回 `UNSUPPORTED`——前者说明"这个形状根本没法规范化"，与第 7 条过滤
    是同一件事，所以调用方把 404 当成"没有 key"而不是路由失败。

    幂等键由调用方给定且必须**确定性**：同一条路由命令重放时要拿到同一个
    combination_key，否则两次决策的 `content_hash` 会不同（T25-22）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "S24_BASE_URL is not configured; S25 cannot obtain a "
                "combination_key, which S26 must verify before dispatch")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _check(tenant_id: str, command_id: str, snapshot_id: str,
               req: dict) -> dict:
        body = {
            "snapshot_id": snapshot_id,
            "mode": req.get("mode"),
            "refs": req.get("references") or [],
            "audio_strategy": (req.get("audio") or {}).get("strategy"),
            "duration_seconds": req.get("duration_seconds"),
            "aspect_ratio": req.get("aspect_ratio"),
            "resolution": req.get("resolution"),
            "lora": bool(req.get("lora")),
            "required_capabilities": req.get("required_capabilities") or [],
        }
        try:
            return _request("POST", f"{prefix}/capability-checks", body=body,
                            headers={"X-Tenant-Id": tenant_id,
                                     "Idempotency-Key": command_id},
                            timeout=15.0)
        except ServiceError as exc:
            # 404 = 这个模式/快照没有可规范化的形状 → 没有 key，交给过滤 7。
            if exc.status == 404:
                return {"combination_key": None, "verdict": None,
                        "unavailable": exc.code}
            raise
    return _check


def build_validity_source(base_url: str | None):
    """`(tenant_id, items) -> {items:[...]}`。批量 ≤200，见 S24 的校验。"""
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "S24_BASE_URL is not configured; S25 will not reuse cached "
                "validity conclusions (plan §9)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _validity(tenant_id: str, items: list[dict]) -> dict:
        return _request("POST", f"{prefix}/capability-validity",
                        body={"items": items},
                        headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
    return _validity


# ---------------------------------------------------------------- 遥测

def build_telemetry_source(base_url: str | None):
    """`(tenant_id, req, candidates) -> {pool_id|candidate_key: samples}`。

    全仓没有这个数据源（MRT-15/MRT-16）。**未配置时返回空 dict，不是失败**：
    空遥测的正确后果是分数退化成冷启动先验（§4.4 的 α 平滑），以及过滤
    14/17 记"判不了"，而不是让整条路由 503。伪造 p90 才是有害的。

    配置了 base_url 时按 `GET {base}/routing-samples?...` 取；这一条的协议
    是本服务自己定的，因为上游还没有实现——所以它只在这里出现，不外泄。
    """
    if not base_url:
        def _empty(*_args, **_kwargs):
            return {}
        return _empty

    prefix = base_url.rstrip("/")

    def _samples(tenant_id: str, req: dict, candidates: list[dict]) -> dict:
        pool_ids = sorted({c.get("account_pool_id") for c in candidates
                           if c.get("account_pool_id")})
        if not pool_ids:
            return {}
        url = (f"{prefix}/routing-samples?"
               + urllib.parse.urlencode({"pool_id": ",".join(pool_ids),
                                         "modality": req.get("modality")}))
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=10.0)
        return result.get("samples") or {}
    return _samples
