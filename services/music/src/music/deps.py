"""S34 的上游，全部是注入式 callback，不是 import。

AGENTS.md 禁止跨服务 import 业务 ORM / 跨库查询，只能走服务边界。这里把
**真实契约和方案假设之间的差异全部吸收在一处**，内核（`service.py`）看不到。

未配置 base url 时返回**必然失败**的 callback。没有"忘了配就放行"的路径——
音乐生成是付费操作，一个默认放行等于把账单和合规一起交给运气。

三个刻意不对称的地方：

- **`rights_resolver` / `route_resolver` / `submitter` / `budget_check` /
  `canceler` 未配置 → 503。** 它们各自是付费链路或合规链路的一环，缺一环就
  不能继续。特别是 `submitter`：`S26_BASE_URL` 没配时**绝不能**返回一个假的
  `provider_job_id`，那会让内核以为已经提交，然后进入等待一个不存在的 job。
- **`artifact_check` / `analysis_probe` / `edl_provider` / `scene_check`
  未配置 → 返回 `None`。** 它们都是"查了更好、查不到不阻断"的旁证：S28 的
  存在性、音频探针、S41 的 EDL 版本、S06 的场景坐标。`None` 的语义是
  **"没查"**，与"查了，值是这样"必须可区分（见各 builder 的 docstring）。
  把"没查"当成"不在了/不可用"会让 S34 在 S28 抖动时白花一次生成的钱。
- **`budget_check` 未配置 → 503，不是"假设预算够"。** F6 要求在付费前证明
  `N × quote` 在预算内；查不到预算就不能证明，只能拒。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import SCHEMA_VERSION, ServiceError, _jcs, _loads


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
        # 超时/不可达要和"上游明确说了不行"分开：前者可重试，后者是 4xx。
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


# ---------------------------------------------------------------- S02 许可

def build_rights_resolver(base_url: str | None):
    """`(rights_policy_ref, tenant_id) -> snapshot`。

    返回的 snapshot 必须有四元组（`composition_status` / `master_status` /
    `performance_status` / `sync_status`），可选 `sync_territories` /
    `sync_media` / `sync_expires_at` / `work_title` / `isrc` / `iswc` /
    `attribution_text` / `training_basis_ref`。

    C3：S02 没有公开的"把 ref 解析成四元组"读端点，路径按 S02 的
    `GET /policies/{id}` 猜。**猜错的代价只是 404/503，不会造成误放行**——
    解析失败时内核写不下 binding，job 停在 `CHECKING_POLICY`。真正危险的是
    "解析不出来就当成 public_domain"，那条路这里不存在。
    """
    if not base_url:
        return _unconfigured(
            "RIGHTS_RESOLVER_UNAVAILABLE",
            "S02_BASE_URL is not configured; S34 cannot resolve the licence "
            "four-tuple, and passing the ref through to the provider would "
            "outsource compliance (gap C3)")

    prefix = base_url.rstrip("/")

    def _resolve(ref: dict, tenant_id: str) -> dict:
        query = urllib.parse.urlencode({
            "revision": ref.get("revision"),
            "content_hash": ref.get("content_hash"),
        })
        try:
            return _get(f"{prefix}/policies/{ref.get('id')}?{query}",
                        headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except ConnectionError as exc:
            raise ServiceError(503, "RIGHTS_RESOLVER_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        # 跨租户的 ref 对 S34 来说就是"不存在"。S02 回 404 就原样上抛成 404，
        # 不转 403——403 会泄漏"这个 id 存在"。
    return _resolve


# ---------------------------------------------------------------- S25 路由

def build_route_resolver(base_url: str | None):
    """`(tenant_id, job, generation_command) -> route decision`。

    **两处必须在这里归一，不能推给内核：**

    1. **S25 的 HTTP 响应没有顶层 `selected`。** `selected` 只出现在
       `model.route-selected.v2` 事件里；`GET /route-decisions/{id}` 返回的是
       `{"route": {...}, "eligible": [...], "rejected": [...]}`。而 S26 的
       `create_job` 读的是 `route.get("selected")`，还要里面的
       `normalized_input` / `price_snapshot_id`。所以这里把 `eligible[0]`
       抬成 `selected`，并把 `quote_valid_to` 从 `route` 提到顶层——S26 先读
       顶层、读不到再读 `selected`。
    2. **`route_id` 的命名。** 内核的 `_route_ref` 两个名字都认，但这里显式
       保留 `route_id`，不复制成 `route_decision_id`：一个值两个名字会让
       "哪个是真的"在日志里永远说不清。

    S25 未配置 → 503。**没有"编一个默认路由"的路径。**
    """
    if not base_url:
        return _unconfigured(
            "ROUTE_DECISION_UNAVAILABLE",
            "S25_BASE_URL is not configured; S34 will not invent a route "
            "decision or a price for a paid generation")

    prefix = base_url.rstrip("/")

    def _decide(tenant_id: str, job: dict, generation_command: dict) -> dict:
        # S25 的 `POST /route-decisions` body 是
        # `{generation_command, command_id}`，`additionalProperties: false`。
        # 把 job 上的预算/deadline 当顶层键塞进去会被 422 掉——它们已经在
        # `generation_command` 里。
        result = _post(
            f"{prefix}/route-decisions",
            {"generation_command": generation_command,
             "command_id": f"music-route-{job['music_job_id']}-"
                           f"{generation_command['attempt_id']}"},
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)

        route = result.get("route") or {}
        merged = dict(route)
        # 顶层 `feasible` 是内核读的那一个；`route.feasible` 是 S25 的权威值。
        merged["feasible"] = bool(route.get("feasible",
                                           result.get("feasible", False)))
        merged["degradation_options"] = result.get("degradation_options") or []
        merged["capabilities"] = _capabilities_of(result)
        merged["residency_allowed"] = _residency_of(result)

        eligible = result.get("eligible") or []
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
        # S26 先读顶层 `quote_valid_to`，读不到才读 `selected`。S25 把它放在
        # `route` 里，这里提到顶层，免得 S26 报 "carries no quote_valid_to"。
        if not merged.get("quote_valid_to") and route.get("quote_valid_to"):
            merged["quote_valid_to"] = route["quote_valid_to"]
        return merged
    return _decide


def _capabilities_of(result: dict) -> dict:
    """从 S25 的 `execution_profile` 里读能力位。

    **读不到就返回空 dict，不是 `{"multi_candidate": True}`。** 内核只在值为
    显式 `False` 时拒（`caps.get(...) is False`），所以空 dict 等价于"没有
    关于这条路由的能力声明"——继续走，但不假装验过。反过来把缺省当 True 会
    让"路由不支持多候选"这件事在数据缺失时静默通过。
    """
    eligible = result.get("eligible") or []
    if not eligible:
        return {}
    profile = eligible[0].get("execution_profile") or {}
    caps = {}
    for key in ("multi_candidate", "instrumental_only"):
        if isinstance(profile.get(key), bool):
            caps[key] = profile[key]
    return caps


def _residency_of(result: dict) -> bool | None:
    """数据驻留。S25 没有独立字段，只能看 `rejected[]` 里有没有驻留类拒绝。

    `None` = 没有关于驻留的信息（不阻断）；`False` = 明确拒绝。
    """
    for entry in result.get("rejected") or []:
        code = str(entry.get("rejection_code") or "")
        if "RESIDENCY" in code.upper() or "REGION" in code.upper():
            return False
    return None


# --------------------------------------------------------------- S26 提交

def build_submitter(base_url: str | None):
    """`(job, generation_command, route, candidate_index) -> {provider_job_id, ...}`。

    S26 的真实契约（`POST /generation-jobs`）：

    - body 三个键：`command_id` / `generation_command` / `route_decision_id`
      （顶层，必填）。`route_decision_ref` 是可选的 content_hash 回显，
      S26 收到就校验、不一致 409 `ROUTE_DECISION_HASH_MISMATCH`——**这说明
      它是用来抓"发送方记错了路由"的，所以必须带上而不是省掉**。
    - `generation_command` 11 个必填键由内核组装（S26 逐键查
      `COMMAND_REQUIRED`）；这里**不再二次拼装**，原样转发。任何在 deps 里
      "修正"命令的冲动都是把签名对象重造一遍。
    - `route_decision_id` 读 `route["route_id"]`：S25 的 HTTP 响应叫
      `route_id`，S26 的 body 字段叫 `route_decision_id`，名字在这里换一次。
      注意 **S33 的 submitter 读 `route["route_decision_id"]`，那对真实 S25
      是取不到值的**（得靠 route_resolver 的兼容分支才活下来）；S34 不复制
      那个隐患。

    未配置 → 503，**绝不返回伪造的 `provider_job_id`**。
    """
    if not base_url:
        return _unconfigured(
            "GENERATION_JOBS_UNAVAILABLE",
            "S26_BASE_URL is not configured; S34 will not submit a paid "
            "generation job (AGENTS.md forbids bypassing S26 to pay)")

    prefix = base_url.rstrip("/")

    def _submit(job: dict, generation_command: dict, route: dict,
                candidate_index: int) -> dict:
        route_id = route.get("route_id") or route.get("route_decision_id")
        if not route_id:
            # 路由 id 必须来自 S25。缺了就在付费前停下。
            raise ServiceError(
                503, "ROUTE_DECISION_UNAVAILABLE",
                "S25 returned no route id; refusing to submit without one",
                retryable=True)

        body = {
            # `command_id` 逐候选different：S26 的一次 create_job 对应一条
            # attempt，N 个候选就是 N 次 create_job。
            "command_id": f"music-{job['music_job_id']}-{candidate_index}-"
                          f"{job['business_attempt_no']}",
            "generation_command": generation_command,
            "route_decision_id": route_id,
        }
        ref = {"service_id": "S25", "id": route_id,
               "revision": int(route.get("revision") or 1)}
        if route.get("content_hash"):
            # 只在 S25 给了的时候回显：不给 content_hash 的 ref 是"没有可
            # 校验的东西"，比编一个假的强。
            ref["content_hash"] = route["content_hash"]
        body["route_decision_ref"] = ref

        headers = {"X-Tenant-Id": job["tenant_id"]}
        result = _post(f"{prefix}/generation-jobs", body, headers=headers)
        provider_job_id = (result.get("provider_job_id")
                           or result.get("generation_job_id"))
        if not provider_job_id:
            # 202 但没回 id = 无法跟踪。绝不造一个。
            raise ServiceError(
                503, "GENERATION_JOBS_UNAVAILABLE",
                "S26 accepted the request but returned no provider_job_id",
                retryable=True)
        return {
            "provider_job_id": provider_job_id,
            "operation_id": result.get("operation_id"),
            "attempt_id": result.get("attempt_id"),
            "admission_id": result.get("admission_id"),
            "state": result.get("state"),
        }
    return _submit


# ------------------------------------------------------- S28 artifact 存在性

def build_artifact_check(base_url: str | None):
    """`artifact_ref -> True|False|None`（**单参数**，内核就是这么调的）。

    `True` 在、`False` 明确不在了（hash/修订不符）、`None` 查不到。
    **把"查不到"当成"不在了"会让 S34 在 S28 抖动时白扔掉一份已经付费的
    take**，反之当成"在"则会把校验过的 bytes 当成事实。`None` 让内核继续
    用事件里的值，并保留"这次没验成"这个事实。

    单参数是在核心里定死的：`artifact_ref` 自带 `service_id`，租户要靠
    header 传，而这条路径在内核里没有租户上下文可拿。**不为了补租户去改
    内核的调用签名**——那会让"验存在性"这条旁路有机会否决一次付费结果。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _exists(artifact_ref: dict) -> bool | None:
        artifact_id = artifact_ref.get("id")
        revision = artifact_ref.get("revision")
        if not artifact_id:
            return None
        try:
            result = _get(f"{prefix}/artifacts/{artifact_id}/revisions/{revision}",
                          timeout=3.0)
        except ServiceError as exc:
            return False if exc.status == 404 else None
        except ConnectionError:
            return None
        expected = artifact_ref.get("content_hash")
        actual = result.get("content_hash")
        if expected and actual and expected != actual:
            return False
        return bool(result.get("artifact_ref") or result.get("id"))
    return _exists


# ---------------------------------------------------------- 音频分析探针

def build_analysis_probe(base_url: str | None):
    """`(artifact_ref, natural_duration_ms) -> probe`。

    probe 形状（内核 `_analyze` / `_write_take` 读的键）：
    `{bpm, meter_num, meter_den, downbeats_ms[], phrases_ms[], confidence,
    tail_kind, vocal_presence, loop_ranges[{in_ms, out_ms, crossfade_ms,
    score}], detected_key, detector_model, detector_version}`。

    这是 S42 的音频能力，S28 不提供（C4）。真实现应当自己算（频谱自相关
    取 BPM、能量包络取小节线、首尾相关性取循环点）。未配置 → `None`，
    内核把它当 `BEAT_UNANALYZABLE`——那份 take **不登记**。这不花钱、
    不误判，是最保守的失败方向。

    probe 里的 `score` 必须是 **0..1 的质量分（越大越好）**。检测器若输出
    距离度量，**在 probe 里转换**，别让 `cut_plan` 去猜（MUS-15）。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _probe(artifact_ref: dict, natural_duration_ms: int) -> dict | None:
        try:
            return _post(f"{prefix}/audio/analysis",
                         {"artifact_ref": artifact_ref,
                          "duration_ms": int(natural_duration_ms)},
                         timeout=20.0)
        except (ServiceError, ConnectionError):
            return None
    return _probe


# ---------------------------------------------------------------- S23 预算

def build_budget_check(base_url: str | None):
    """`(tenant_id, budget_scope_id, total_quote) -> {sufficient, available_micro}`。

    **只读**。S34 从不预留、从不扣减——余额是 S23 的权威，在这里记一份就是
    第二个权威（AGENTS.md）。

    F6 要求提交前证明 `N × quote` 在预算内。查不到 → 503，**不是"假设够"**：
    一次越界提交会把预算检查变成事后通知。
    """
    if not base_url:
        return _unconfigured(
            "BUDGET_CHECK_UNAVAILABLE",
            "S23_BASE_URL is not configured; S34 cannot prove the candidate "
            "batch fits the budget before spending")

    prefix = base_url.rstrip("/")

    def _check(tenant_id: str, budget_scope_id: str,
               total_quote_micro: int) -> dict:
        try:
            body = _get(f"{prefix}/budgets/{budget_scope_id}/availability",
                        headers={"X-Tenant-Id": tenant_id}, timeout=5.0)
        except ConnectionError as exc:
            raise ServiceError(503, "BUDGET_CHECK_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        available = body.get("available_micro")
        if not isinstance(available, int):
            # 上游答了但答不清 → 不能当 0（0 会把一切判成不够），也不能当够。
            raise ServiceError(
                503, "BUDGET_CHECK_UNAVAILABLE",
                "budget availability carried no available_micro",
                retryable=True)
        return {"sufficient": available >= int(total_quote_micro),
                "available_micro": available,
                "currency": body.get("currency"),
                "revision": body.get("revision")}
    return _check


# ------------------------------------------------------------ S26 取消

def build_canceler(base_url: str | None):
    """`(provider_job_id, reason) -> None`。

    取消**没有确认就不能标 CANCELED**——标了就断言"它确定没在做"，而它可能
    正在做并照样收费。内核在收到异常时进 `WAITING_UNKNOWN`，等对账。
    """
    if not base_url:
        return _unconfigured(
            "GENERATION_JOBS_UNAVAILABLE",
            "S26_BASE_URL is not configured; cancellation cannot be "
            "confirmed, so the job must go to WAITING_UNKNOWN")

    prefix = base_url.rstrip("/")

    def _cancel(provider_job_id: str, reason: str | None) -> None:
        _post(f"{prefix}/generation-jobs/{provider_job_id}:cancel",
              {"reason": reason or "music_canceled"})
    return _cancel


# ---------------------------------------------------------------- S41 EDL

def build_edl_provider(base_url: str | None):
    """`(tenant_id, scene_lineage_id, edl_revision) -> edl | None`。

    返回 `{"edl_revision": int, ...}`。**不返回、报错、没配 → `None`。**
    位置层是免费可重算的派生量，问不到 EDL 不该阻断内容层（付费的那一层）；
    `None` 的语义是"没查/没给"，与 `{"edl_revision": 0}`（"第 0 版"）必须
    可区分——踩混了就会把 `0` 写进 `cut_plan_cache` 的主键。
    """
    if not base_url:
        return lambda *_args, **_kwargs: None

    prefix = base_url.rstrip("/")

    def _edl(tenant_id: str, scene_lineage_id: str | None,
             edl_revision: int | None) -> dict | None:
        if not scene_lineage_id:
            return None
        query = "" if edl_revision is None else f"?edl_revision={edl_revision}"
        try:
            return _get(f"{prefix}/timelines/scenes/{scene_lineage_id}{query}",
                        headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except (ServiceError, ConnectionError):
            return None
    return _edl


# --------------------------------------------------------------- S06 场景

def build_scene_check(base_url: str | None):
    """`(tenant_id, scene_lineage_id, cue_index) -> scene | None`。

    F28：跨租户的场景在 S34 眼里**和不存在是同一种情况**——都返回 `None`，
    内核统一 404，不区分 404/403（403 会泄漏 id 存在）。

    未配置 → `None`（不阻断）。没有这条检查时内核跳过场景归属验证，这是
    **已知的松**，记在 `CONTRACT_GAPS`（MUS-16）。
    """
    if not base_url:
        return None

    prefix = base_url.rstrip("/")

    def _scene(tenant_id: str, scene_lineage_id: str,
               cue_index: int) -> dict | None:
        try:
            return _get(f"{prefix}/scenes/{scene_lineage_id}",
                        headers={"X-Tenant-Id": tenant_id}, timeout=3.0)
        except (ServiceError, ConnectionError):
            # 404（没有）、403（别人的）、503（问不到）都落到 `None`：
            # 内核把它们统一成 404，不区分——403 会泄漏"这个 id 存在"。
            return None
    return _scene


# ------------------------------------------------------------ 工厂：整组

def build_all(base_urls: dict) -> dict:
    """按环境装配整组 callback。

    `service.api.app` 只用这一个入口，避免每个 builder 各自读 env 时出现
    "某个 base url 用了旧值"这种只在一个请求里暴露的错。
    """
    return {
        "rights_resolver": build_rights_resolver(base_urls.get("S02")),
        "route_resolver": build_route_resolver(base_urls.get("S25")),
        "submitter": build_submitter(base_urls.get("S26")),
        "artifact_check": build_artifact_check(base_urls.get("S28")),
        "analysis_probe": build_analysis_probe(base_urls.get("S42")),
        "budget_check": build_budget_check(base_urls.get("S23")),
        "canceler": build_canceler(base_urls.get("S26")),
        "edl_provider": build_edl_provider(base_urls.get("S41")),
        "scene_check": build_scene_check(base_urls.get("S06")),
    }
