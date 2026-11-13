"""S33 的上游，全部是注入式 callback，不是 import。

AGENTS.md 禁止互相 import 业务 ORM / 跨库查询，只能走服务边界。五个上游里
**三个的真实契约和方案假设的形状不一样**，差异只在这里吸收，内核看不到：

- **S02** 没有"把 `rights_policy_ref` 解析成许可能力"的读接口（缺口 C5）。
  方案要求提交前就知道 `allowed_license_kinds`，否则无法预判 `POLICY_BLOCKED`，
  只能把 ref 透传给 S26 让供应商去判断合规——那等于把法务判断外包给模型。
  所以这里只能收一个 resolver callback，**不猜 endpoint**。
- **S25** 是 `design_only`。S26 的 `create_job` 要求一个可执行的路由决策
  （`route_decision_id` / `quote_valid_to` / `price_snapshot_id`），本服务拿不到。
  **没有"编一个默认路由"的路径**：没配就 503。
- **S28** 的 `artifact.ingested` 不带可循环性探针（C4）。`loop_probe` 只能自己
  实现（真实的频谱相关性检测），这里只定义接口。

未配置 base url 时返回必然失败的 callback。没有"忘了配就放行"的路径。

一个特例是 `edl_provider`：它不失败，返回 `None`。理由是 S41 的 EDL 决定
"位置"，而位置是免费可重算的派生量——问不到 EDL 不该阻断内容层（那是要花钱的
那一层）。`None` 的语义是"没查"，`{"edl_revision": 0}` 的语义是"查了，是第 0 版"，
两者在响应里必须可区分。
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
        # 超时/不可达要和"上游说了不行"分开：前者 503 可重试，后者是 4xx。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _get(url: str, headers: dict | None = None, timeout: float = 10.0) -> dict:
    return _request("GET", url, headers=headers, timeout=timeout)


def _post(url: str, body: dict, headers: dict | None = None,
          timeout: float = 15.0) -> dict:
    return _request("POST", url, body=body, headers=headers, timeout=timeout)


# ---------------------------------------------------------------- S02 许可

def build_rights_resolver(base_url: str | None):
    """`(rights_policy_ref, tenant_id) -> snapshot`。

    snapshot 至少要有 `allowed_license_kinds`（**空数组是合法且致命的**：它
    表示"这个策略下没有任何许可可用"，见 schemas 的 `rpb_kinds_present`）。
    可选 `attribution_required` / `territory_restrictions` / `blocked_families`
    / `expires_at`，缺省按"没限制"处理——这三个是收紧用的，缺了不会放宽。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "RIGHTS_RESOLVER_UNAVAILABLE",
                "S02_BASE_URL is not configured; S33 cannot resolve "
                "allowed_license_kinds, and passing the ref through to the "
                "provider would outsource compliance (gap C5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _resolve(ref: dict, tenant_id: str) -> dict:
        query = urllib.parse.urlencode({
            "revision": ref.get("revision"),
            "content_hash": ref.get("content_hash"),
        })
        try:
            result = _get(f"{prefix}/policies/{ref.get('id')}?{query}",
                          headers={"X-Tenant-Id": tenant_id}, timeout=2.0)
        except ConnectionError as exc:
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        # 跨租户的 ref 在 S33 眼里就是"不存在"——S02 若返回 404，这里原样上抛
        # 成 404，不转成 403（403 会泄漏"这个 id 存在"）。
        return result
    return _resolve


# ---------------------------------------------------------------- S25 路由

def build_route_resolver(base_url: str | None):
    """`(tenant_id, job, generation_command) -> route decision`。

    S25 目前是 `design_only`，所以这条路径在任何真实部署里都会 503。这不是
    缺陷：`design_only` 的上游不能被"默认参数"顶掉，否则第一个真实调用会把
    一次未定价的生成送进账单。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "S25_BASE_URL is not configured and S25 (model-routing) is "
                "design_only; S33 will not invent a route decision")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _decide(tenant_id: str, job: dict, generation_command: dict) -> dict:
        body = {
            "modality": generation_command["modality"],
            "tenant_id": tenant_id,
            "budget_scope_id": job["budget_scope_id"],
            "deadline": job["deadline"],
            "node_id": job["node_id"],
            "execution_fence": int(job["execution_fence"]),
        }
        result = _post(f"{prefix}/route-decisions", body,
                       headers={"X-Tenant-Id": tenant_id}, timeout=5.0)
        if not result.get("feasible", True):
            raise ServiceError(409, "NO_FEASIBLE_ROUTE",
                               "S25 found no route that fits the deadline and "
                               "budget", details=result)
        return result
    return _decide


# --------------------------------------------------------------- S26 提交

def build_submitter(base_url: str | None):
    """`(job, generation_command, route) -> {provider_job_id, attempt_id, ...}`。

    S26 的真实契约：`POST /generation-jobs` 的 body 必须带 `route_decision_id`
    与 `generation_command`；路由决策得是一个**已被 S26 读过且报价仍有效**的
    决策，所以这里把它原样带上。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "GENERATION_JOBS_UNAVAILABLE",
                "S26_BASE_URL is not configured; S33 will not submit a paid "
                "generation job")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _submit(job: dict, generation_command: dict, route: dict) -> dict:
        body = {
            "command_id": f"sfx-{job['sfx_job_id']}-{job['business_attempt_no']}",
            "tenant_id": job["tenant_id"],
            "route_decision_id": route["route_decision_id"],
            "route_decision_ref": {
                "service_id": "S25", "id": route["route_decision_id"],
                "revision": route.get("revision", 1)},
            "generation_command": generation_command,
        }
        headers = {
            "X-Tenant-Id": job["tenant_id"],
            "Idempotency-Key": job["idempotency_key"],
        }
        result = _post(f"{prefix}/generation-jobs", body, headers=headers)
        return {
            "provider_job_id": result.get("generation_job_id")
                                or result.get("provider_job_id"),
            "operation_id": result.get("operation_id"),
            "attempt_id": result.get("attempt_id"),
        }
    return _submit


# ------------------------------------------------------- S28 artifact 存在性

def build_artifact_check(base_url: str | None):
    """`(artifact_ref, tenant_id) -> bool | None`。

    `True` 存在、`False` 已经不在了（改用别的素材）、`None` 查不到（不改判）。
    这三者必须分开：把"查不到"当成"不在了"会让 S33 在 S28 抖动时白花一次钱。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _exists(artifact_ref: dict, tenant_id: str) -> bool | None:
        try:
            result = _get(
                f"{prefix}/artifacts/{artifact_ref.get('id')}"
                f"/revisions/{artifact_ref.get('revision')}",
                headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except ServiceError as exc:
            return False if exc.status == 404 else None
        except ConnectionError:
            return None
        return bool(result.get("artifact_ref") or result.get("id"))
    return _exists


# ---------------------------------------------------------------- S41 EDL

def build_edl_provider(base_url: str | None):
    """`(tenant_id, shot_lineage_id, edl_revision) -> edl | None`。

    返回形状：`{"edl_revision": int, "shots": {shot_lineage_id: {"start_ms",
    "end_ms", "beats": {beat_ref: ms}}}}`。**不返回也不报错时给 None**——
    位置层可以推迟，内容层不该被它拖住。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _edl(tenant_id: str, shot_lineage_id: str,
             edl_revision: int | None) -> dict | None:
        query = "" if edl_revision is None else f"?edl_revision={edl_revision}"
        try:
            return _get(f"{prefix}/timelines/shots/{shot_lineage_id}{query}",
                        headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except (ServiceError, ConnectionError):
            return None
    return _edl


# ------------------------------------------------------------ 循环性探针

def build_loop_probe(base_url: str | None):
    """`artifact_ref -> bool | None`。C4: S28 不提供这个，得自己算。

    真实现应该是"首尾 N 个采样点的频谱/能量相关性 + 过零点连续性"，属于 S42
    的音频处理能力。这里只定义接口：`True` 验过可循环，`False` 验过不可，
    `None` 没法验（都按失败处理，见 service.on_artifact_ingested）。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _probe(artifact_ref: dict) -> bool | None:
        try:
            result = _post(f"{prefix}/audio/loop-analysis",
                           {"artifact_ref": artifact_ref}, timeout=10.0)
        except (ServiceError, ConnectionError):
            return None
        if "loopable" not in result:
            return None
        return bool(result["loopable"])
    return _probe


# ------------------------------------------------------------ S26 取消

def build_canceler(base_url: str | None):
    """`(provider_job_id, reason) -> None`。取消没被确认时**不能**标 CANCELED。"""
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "GENERATION_JOBS_UNAVAILABLE",
                "S26_BASE_URL is not configured; cancellation cannot be "
                "confirmed, so the job must go to WAITING_UNKNOWN")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _cancel(provider_job_id: str, reason: str | None) -> None:
        _post(f"{prefix}/generation-jobs/{provider_job_id}:cancel",
              {"reason": reason or "sfx_canceled"})
    return _cancel
