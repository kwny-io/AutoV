"""S30 的上游，全部是注入式 callback，不是 import。

AGENTS.md 禁止跨服务 import 业务 ORM / 跨库查询，只能走服务边界。这里把
**真实契约与方案假设之间的差异吸收在一处**，内核（`service.py`）看不到。

**本文件里没有、也不允许出现任何供应商 base url。** 唯一的付费出口是
`build_submitter`，它 POST 的是 S26 的 `/generation-jobs`。想"直接调一下模型
试试"就得先在这里加一个供应商域名——那正是评审要拦下的东西，测试
`test_t30_no_provider_base_url` 会扫这个模块。bypass S26 等于同时绕过 S25 的
选型、S23 的预算、S22 的配额（AGENTS.md 明令禁止）。

四处刻意的不对称：

- **`route_resolver` / `submitter` / `canceler` / `generation_state` 未配置 →
  503。** 它们都在付费链路上。特别是 `submitter`：`S26_BASE_URL` 没配时
  **绝不能**返回一个假的 `provider_job_id`，那会让内核以为已经提交，然后去等
  一个不存在的 job。
- **`artifact_check` 未配置 → 返回 `None`。** 它是只读旁证，不阻断派单；
  `None` 的语义是"没查"，与"查了，不匹配"必须可区分。
- **`artifact_importer` 未配置 → 503。** 触发导入是"请求把字节交给 S28"，
  没配就是不知道字节在哪；假装成功会让 job 永远停在 INGESTING 而没人知道。
- **`keyframe_lookup` 未配置 → 503（不是返回 `None`）。** 它给出"这一版关键帧
  是哪个 artifact"，而 `None` 在内核里等于 **404 候选不存在**——一个没配的上游
  会让调用方以为自己的候选 id 是错的。宁可 503 说"查不了"。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .service import ServiceError


# ------------------------------------------------------------ HTTP 基座

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
        # 超时/不可达与"上游明确说了不行"分开：前者可重试，后者是 4xx。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _get(url: str, headers: dict | None = None, timeout: float = 10.0) -> dict:
    return _request("GET", url, headers=headers, timeout=timeout)


def _post(url: str, body: dict, headers: dict | None = None,
          timeout: float = 15.0) -> dict:
    return _request("POST", url, body=body, headers=headers, timeout=timeout)


def _unconfigured(code: str, message: str):
    """未配置时的 callback：**永远失败**，绝不返回"看起来成功"的值。"""
    def _fail(*_args, **_kwargs):
        raise ServiceError(503, code, message)
    return _fail


# ---------------------------------------------------------------- S25 路由

def build_route_resolver(base_url: str | None):
    """`(tenant_id, job, generation_command) -> route decision`。

    三处必须在这里归一，不能推给内核：

    1. **S25 的 HTTP 响应没有顶层 `selected`。** `selected` 只出现在
       `model.route-selected.v2` 事件里；`POST /route-decisions` 回的是
       `{"route": {...}, "eligible": [...], "rejected": [...]}`。内核读
       `route["selected"]["normalized_input"]` 来定 `duration_seconds`，所以把
       `eligible[0]` 抬成 `selected`。
    2. **`quote_valid_to` 的位置。** 它在 `route` 里，内核先读顶层；这里提到
       顶层，免得内核把一份有效报价当成"没有有效期"。
    3. **`residency_allowed` 的判定。** S25 没有独立字段，只看 `rejected[]`
       里的拒绝码。S30 在第一次付费提交**之前**就要用它（T30-35）。

    `feasible` 有两条来源（顶层与 `route.feasible`），取"任一为假即为假"。
    """
    if not base_url:
        return _unconfigured(
            "ROUTE_DECISION_UNAVAILABLE",
            "S25_BASE_URL is not configured; S30 will not invent a route "
            "decision or a price for a paid generation")

    prefix = base_url.rstrip("/")

    def _decide(tenant_id: str, job: dict, generation_command: dict) -> dict:
        route = _post(
            f"{prefix}/route-decisions",
            {"generation_command": generation_command,
             "command_id": f"s30-route-{job['video_job_id']}-"
                           f"{generation_command['attempt_id']}"},
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)

        inner = route.get("route") or {}
        merged = dict(inner)
        merged["feasible"] = (inner.get("feasible") is not False
                              and route.get("feasible") is not False)
        merged["degradation_options"] = route.get("degradation_options") or []
        merged["infeasible_reason"] = (inner.get("infeasible_reason")
                                       or route.get("infeasible_reason"))
        merged["residency_allowed"] = _residency_of(route)

        eligible = route.get("eligible") or []
        if eligible and not merged.get("selected"):
            top = eligible[0]
            merged["selected"] = {
                "candidate_key": top.get("candidate_key"),
                "snapshot_id": top.get("snapshot_id"),
                "price_snapshot_id": top.get("price_snapshot_id"),
                "combination_key": top.get("combination_key"),
                "normalized_input": top.get("normalized_input"),
                "expected_cost_micro": top.get("expected_cost_micro"),
                "quote_upper_bound_micro": top.get("quote_upper_bound_micro"),
                "currency": top.get("currency"),
            }
        if not merged.get("quote_valid_to") and inner.get("quote_valid_to"):
            merged["quote_valid_to"] = inner["quote_valid_to"]
        # 内核只认 `route_id` / `route_decision_id` 两个名字。
        if not merged.get("route_id") and inner.get("route_decision_id"):
            merged["route_id"] = inner["route_decision_id"]
        return merged
    return _decide


def _residency_of(result: dict) -> bool | None:
    """数据驻留。`None` = 没有信息（不阻断）；`False` = 明确拒绝。"""
    for entry in result.get("rejected") or []:
        code = str(entry.get("rejection_code") or "")
        if "RESIDENCY" in code.upper() or "REGION" in code.upper():
            return False
    return None


# --------------------------------------------------------------- S26 提交

def build_submitter(base_url: str | None):
    """`(job, generation_command, route, candidate_index) -> {provider_job_id, …}`。

    S26 的真实契约（`POST /generation-jobs`）：

    - body 必填 `command_id` + `route_decision_id` + `generation_command`；
      `route_decision_ref` 可选回显，S26 收到就校验 content_hash，不一致
      **409 `ROUTE_DECISION_HASH_MISMATCH`**——必须带上而不是省掉。
    - `generation_command` 由内核组装（S26 逐键查 11 个必填键），这里**原样
      转发**。在 deps 里"修正"命令 = 把签名对象重造一遍。
    - 返回 `{operation_id, provider_job_id, attempt_id, state, admission_id}`。

    未配置 → 503，**绝不返回伪造的 `provider_job_id`**。
    """
    if not base_url:
        return _unconfigured(
            "GENERATION_JOBS_UNAVAILABLE",
            "S26_BASE_URL is not configured; S30 will not submit a paid "
            "generation job (AGENTS.md forbids bypassing S26 to pay)")

    prefix = base_url.rstrip("/")

    def _submit(job: dict, generation_command: dict, route: dict,
                candidate_index: int) -> dict:
        route_id = route.get("route_id") or route.get("route_decision_id")
        if not route_id:
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "S25 returned no route id; refusing to submit without one",
                retryable=True)

        body = {
            "command_id": f"s30-{job['video_job_id']}-{candidate_index}-"
                          f"{job['business_attempt_no']}",
            "generation_command": generation_command,
            "route_decision_id": route_id,
        }
        ref = {"service_id": "S25", "id": route_id,
               "revision": int(route.get("revision") or 1)}
        if route.get("content_hash"):
            # 只在 S25 给了的时候回显。编一个假 hash 会变成"校验过但没校验"。
            ref["content_hash"] = route["content_hash"]
        body["route_decision_ref"] = ref

        result = _post(f"{prefix}/generation-jobs", body,
                       headers={"X-Tenant-Id": job["tenant_id"]})
        provider_job_id = (result.get("provider_job_id")
                           or result.get("generation_job_id"))
        if not provider_job_id:
            # 202 但没回 id = 无法跟踪，也**无法判断是否已付费**。绝不造一个。
            raise ServiceError(
                503, "GENERATION_JOBS_UNAVAILABLE",
                "S26 accepted the request but returned no provider_job_id",
                retryable=True)
        return {"provider_job_id": provider_job_id,
                "operation_id": result.get("operation_id"),
                "attempt_id": result.get("attempt_id"),
                "admission_id": result.get("admission_id"),
                "state": result.get("state")}
    return _submit


# ------------------------------------------------------- S28 artifact 存在性

def build_artifact_check(base_url: str | None):
    """`(tenant_id, artifact_ref) -> True|False|None`。

    `True` 在且 hash 一致、`False` 明确不一致/不在、`None` 查不到。

    **把"查不到"当成"不在"会在 S28 抖动时白扔一次已经批准的生产**；反过来当成
    "在"则会把没校验的字节当事实。所以三态由内核区分（内核把 `None` 与 `False`
    都判成本地拒绝，但用不同的 reason_code，日志里能分开）。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _check(tenant_id: str, artifact_ref: dict) -> bool | None:
        artifact_id = artifact_ref.get("id")
        revision = artifact_ref.get("revision")
        if not artifact_id:
            return None
        try:
            result = _get(
                f"{prefix}/artifacts/{artifact_id}/revisions/{revision}",
                headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except ServiceError as exc:
            # 404 = 不存在或不属于本租户（S28 按租户过滤）。内核把它变成 404，
            # 与"不存在"一致，不泄漏存在性。
            return False if exc.status == 404 else None
        except ConnectionError:
            return None
        expected = artifact_ref.get("content_hash")
        actual = result.get("content_hash") or (
            result.get("artifact_ref") or {}).get("content_hash")
        if expected and actual and expected != actual:
            return False
        return True
    return _check


# --------------------------------------------------------- S28 触发导入

def build_artifact_importer(base_url: str | None, *,
                            generation_base_url: str | None = None):
    """`(tenant_id, job) -> import 受理结果`。**不拥有字节，只请求归档。**

    正常路径上 S26 自己会建导入（provider 归档流程）；这里是 F12/F16 的重驱动：
    URL 过期或进程重启导致 job 停在 INGESTING 时，再请求一次。

    C30-21：方案 §4.5 说"URL 过期就先向 S26 查询新 URL"，但 S26 没有公开的 URL
    刷新端点；URL 只在最后一次观测里带出。所以这里先 `GET
    /generation-jobs/{id}` 取 `output` 里的 URL；拿不到就不发导入，让内核把
    `awaiting_fresh_url=true` 暴露出来。

    **每次导入都要求 S28 探测媒体时长**（`extract_media_metadata: true` /
    `require_duration`）：`INGESTING → READY` 的硬门是 `observed_duration_ms`，
    S28 不探测就没有 READY（C30-12 / T30-13）。

    幂等键按 `(job, business_attempt_no)` 固定：重驱动同一 attempt 是同一个
    `command_id`，S28 会重放受理，不会造出第二份 artifact。
    """
    if not base_url:
        return _unconfigured(
            "ARTIFACT_INGEST_UNAVAILABLE",
            "S28_BASE_URL is not configured; S30 cannot request archival of the "
            "generated bytes")

    prefix = base_url.rstrip("/")
    gen_prefix = (generation_base_url or "").rstrip("/") or None

    def _import(tenant_id: str, job: dict) -> dict:
        url, expires_at, expected_sha = None, None, None
        if gen_prefix:
            snapshot = _get(
                f"{gen_prefix}/generation-jobs/{job['provider_job_id']}",
                headers={"X-Tenant-Id": tenant_id}, timeout=5.0)
            for obs in reversed(snapshot.get("observations") or []):
                if obs.get("provider_url"):
                    url = obs["provider_url"]
                    expires_at = obs.get("provider_url_expires_at")
                    expected_sha = obs.get("expected_sha256")
                    break
            output = snapshot.get("output") or {}
            if not url and output.get("provider_url"):
                url, expires_at = output["provider_url"], output.get(
                    "provider_url_expires_at")
                expected_sha = output.get("expected_sha256")
        if not url:
            # **不自造 URL，也不把 provider_job_id 当成 URL。** 内核据此暴露
            # awaiting_fresh_url。
            raise ServiceError(
                409, "URL_EXPIRED",
                "S26 exposed no fresh provider URL; cannot request archival",
                retryable=True)

        result = _post(
            f"{prefix}/artifact-imports",
            {"command_id": f"s30-import-{job['video_job_id']}-"
                           f"{job['business_attempt_no']}",
             "tenant_id": tenant_id,
             "content_class": "generated_video",
             "expected_sha256": expected_sha,
             "owner_ref": {"service_id": "S30", "id": job["video_job_id"]},
             "scope_ref": {"project_id": job["project_id"]},
             "source": {"kind": "provider_url",
                        "provider_url": url,
                        "provider_url_expires_at": expires_at,
                        "provider_job_id": job["provider_job_id"],
                        "attempt_id": job["attempt_id"]},
             "mime_declared": "video/mp4",
             # 时长与帧率是 S30 的硬门（§3.2）。要求 S28 在归档时探测并写进
             # `artifact.ingested`；它不探测，S30 就只能停在 INGESTING。
             "extract_media_metadata": True},
            headers={"X-Tenant-Id": tenant_id, "Idempotency-Key":
                     f"s30-import-{job['video_job_id']}-"
                     f"{job['business_attempt_no']}"}, timeout=10.0)
        return result if isinstance(result, dict) else {}
    return _import


# ------------------------------------------------------- S26 当前状态读取

def build_generation_state(base_url: str | None):
    """`(tenant_id, provider_job_id) -> {state, …}`。

    §6.2 的 `provider_job_state` 只有 S26 知道。内核的 GET 不调用它（一次读
    不该变成对上游的硬依赖），这条 callback 留给对账与重启恢复（§7.3）。
    """
    if not base_url:
        return _unconfigured(
            "GENERATION_JOBS_UNAVAILABLE",
            "S26_BASE_URL is not configured; the provider state is unknowable")

    prefix = base_url.rstrip("/")

    def _state(tenant_id: str, provider_job_id: str) -> dict:
        return _get(f"{prefix}/generation-jobs/{provider_job_id}",
                    headers={"X-Tenant-Id": tenant_id}, timeout=5.0)
    return _state


# ------------------------------------------------------------ S26 取消

def build_canceler(base_url: str | None):
    """`(provider_job_id, reason) -> None`。

    **没有确认就不能标 CANCELED**：标了就是断言"它确定没在做"，而它可能正在
    做并照样收费。内核在异常时保留原状态并返回 `CANCEL_PENDING`。
    """
    if not base_url:
        return _unconfigured(
            "GENERATION_JOBS_UNAVAILABLE",
            "S26_BASE_URL is not configured; cancellation cannot be confirmed")

    prefix = base_url.rstrip("/")

    def _cancel(provider_job_id: str, reason: str | None) -> None:
        _post(f"{prefix}/generation-jobs/{provider_job_id}:cancel",
              {"reason": reason or "video_canceled"})
    return _cancel


# ------------------------------------------------------ S29 关键帧读取（C7）

def build_keyframe_lookup(base_url: str | None):
    """`(tenant_id, candidate_id, shot_lineage_id) -> {checkpoint_seq, artifact_ref, …}`。

    C7：S30 需要"当前关键帧是哪一版"，而 S29 的 `service.yaml` 里没有这个读口。
    这里按 `plans/S29-image-production.md` §6.3 **假定的**两个端点实现：

    - `GET /shots/{shot_lineage_id}/image-checkpoint` → 每段关键帧的
      `{candidate_id, checkpoint_seq}`；
    - `GET /keyframe-candidates/{candidate_id}` → 该候选的 `artifact_ref` 与
      `content_hash`。

    `None` **只**表示"这个候选不存在或不属于本租户"（内核据此返 404）。上游没配
    或不可达一律 503：把"查不了"返成 `None` 等于告诉调用方"你的 id 是错的"。
    """
    if not base_url:
        return _unconfigured(
            "KEYFRAME_LOOKUP_UNAVAILABLE",
            "S29_BASE_URL is not configured; S30 cannot verify which keyframe "
            "revision a baseline points at (C30-26 / S29 C7)")

    prefix = base_url.rstrip("/")

    def _lookup(tenant_id: str, candidate_id: str,
                shot_lineage_id: str) -> dict | None:
        headers = {"X-Tenant-Id": tenant_id}
        try:
            checkpoints = _get(
                f"{prefix}/shots/{shot_lineage_id}/image-checkpoint",
                headers=headers, timeout=5.0)
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
        match = None
        for entry in checkpoints.get("checkpoints") or []:
            if str(entry.get("candidate_id") or entry.get("keyframe_candidate_id")
                   or "") == candidate_id:
                match = entry
                break
        if match is None:
            # 检查点里没有它：可能是旧版本（已被后续生成取代）或不是本 lineage
            # 的候选。两种都按"不存在"处理——跨租户与不存在同形。
            return None

        try:
            candidate = _get(
                f"{prefix}/keyframe-candidates/{candidate_id}",
                headers=headers, timeout=5.0)
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise

        artifact = candidate.get("artifact_ref") or {}
        return {
            "candidate_id": candidate_id,
            "checkpoint_seq": int(match.get("checkpoint_seq")
                                  or candidate.get("checkpoint_seq") or 0),
            "artifact_ref": artifact,
            "content_hash": str(candidate.get("content_hash")
                                or artifact.get("content_hash") or ""),
        }
    return _lookup


# ---------------------------------------------------------------- 装配

def build_all(base_urls: dict | None = None) -> dict:
    """一条命令装配全部上游。键名即 `VideoProductionService` 的关键字参数。"""
    urls = base_urls or {}
    s25 = urls.get("S25")
    s26 = urls.get("S26")
    s28 = urls.get("S28")
    s29 = urls.get("S29")
    return {
        "route_resolver": build_route_resolver(s25),
        "submitter": build_submitter(s26),
        "artifact_check": build_artifact_check(s28),
        "artifact_importer": build_artifact_importer(s28,
                                                     generation_base_url=s26),
        "generation_state": build_generation_state(s26),
        "canceler": build_canceler(s26),
        "keyframe_lookup": build_keyframe_lookup(s29),
    }
