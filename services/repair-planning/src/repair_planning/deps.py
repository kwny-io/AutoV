"""S39 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

五条读路径 + 一条可选预检：

- **S02** `services/governance-policy`：`POST /policy-decisions:resolve-effective`，
  请求体 `{"context": {...}}`，拿合并后的 `getEffectivePolicy`。`repair_limits.*`
  的跨层合并（取 `min`）归 S02，S39 只消费合并后的结果，**不自己逐层 apply**
  （§5.4 末段）。未配置必然 503：没有 policy 就没有上限，没有上限就不能出计划。
- **S35 / S36 / S37 / S38**：各自有自己的报告读面，**没有一个统一的
  `/qc-reports/{id}`**（C21）：

  | 服务 | 路径 | 摘取 |
  |---|---|---|
  | S35 | `GET /technical-reports/{id}?generation=` | `payload.payload` |
  | S36 | `GET /semantic-reports/{id}` | `payload.report` |
  | S37 | `GET /alignment-reports/{id}?generation=` | `payload.payload` |
  | S38 | `GET /subtitle-reports/{id}?generation=` | `payload.payload` |

  S36 的读面**没有 generation 参数**（只能取最新一代），这是它自己的形状，
  不在这里补。S38 的 api/ 目录还是空的，所以 `S38_BASE_URL` 目前应当保持
  未配置——配置了也会在运行期失败，不会假装读到报告。
- **S17** `services/continuity`：跨集冲突结论。**可选**依赖：未配置返回
  `None`，跨集冲突层就按「缺层」处理（coverage gap → escalated），不假装查过。
- **S24 / S25** `model-registry` / `model-routing`：`GET /model-candidates`
  候选能力查询（**只查不调用**，§3.2）。未配置必然 503：不能证明「支持时序
  一致性」就不能开 `temporal_edit` / `regenerate_video`。
- **S21** `services/orchestration`：剩余修复额度预检（§5.7 Level 4）。§11 C6
  说这个接口还不存在，所以是**可选**依赖：未配置返回 `None`，计划记
  `run_precheck=unavailable`，由 S21 执行期兜底。

**未配置一律必然失败或明确缺席**，没有「默认值兜底」：S39 的输出是修复决策，
用一份默认 policy 或默认能力表兜底，等于替租户决定能修什么、能花多少。

三处**上游形状与方案不一致**，都登记在 `schema.CONTRACT_GAPS`，不在这里
自行选边（C19 / C20 / C21）。
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
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def _unconfigured(code: str, message: str):
    def _missing(*_args, **_kwargs):
        raise ServiceError(503, code, message)
    # 桩自报身份：`/health/readiness` 才能说清"哪道门现在判不了"，而不是
    # 去猜哪个 env 没配。与 S43/S44 同一约定。
    _missing._unavailable_code = code
    return _missing


# ------------------------------------------------------------ S02 policy

def build_policy_source(base_url: str | None):
    """`(tenant_id, context) -> EffectivePolicy 响应`。

    真实路径与形状来自 `services/governance-policy/api/app.py:118` 与
    `service.py:456`：`POST /policy-decisions:resolve-effective`，请求体
    `{"context": {project_id, episode_id, stage_id, node_id, policy_ref,
    tenant_id}}`，响应嵌套在 `effective.*` 下。

    `repair_limits` 只有 shot 级两项（C2）；缺 episode/run 上限时由内核按
    §5.4 缺省值补并记 `policy_partial`——这是**记录在计划上的降级**，不是
    静默忽略。S39 不做层级合并：`effective` 已经是 S02 合并后的结果。
    """
    if not base_url:
        return _unconfigured(
            "POLICY_SOURCE_UNAVAILABLE",
            "S02_BASE_URL is not configured; S39 cannot read repair_limits "
            "and will not invent attempt caps (plan §5.4)")
    prefix = base_url.rstrip("/")

    def _policy(tenant_id: str, context: dict) -> dict:
        body = {"context": {"tenant_id": tenant_id,
                            **{k: v for k, v in context.items()
                               if k != "tenant_id"}}}
        return _request("POST", f"{prefix}/policy-decisions:resolve-effective",
                        body=body, headers={"X-Tenant-Id": tenant_id})
    return _policy


# ------------------------------------------------------- S35/S36/S37/S38

# 每个层自己的报告读面。S38 的 api/ 还没实现，路径按 S35/S37 的命名推，
# 在实现前 `S38_BASE_URL` 应保持未配置（C21）。
REPORT_PATHS = {
    "S35": ("/technical-reports/{id}", "generation", ("payload",)),
    "S36": ("/semantic-reports/{id}", None, ("report",)),
    "S37": ("/alignment-reports/{id}", "generation", ("payload",)),
    "S38": ("/subtitle-reports/{id}", "generation", ("payload",)),
}


def build_report_source(base_url: str | None, service_id: str):
    """`(tenant_id, ref) -> qc-report.v2 文档`。

    `ref` 是 `qc_snapshot.reports[]` 的一项（`id` / `revision` /
    `content_hash` / `service_id`）。**必须按 revision 读**（S36 除外——它
    的读面没有这个参数）：报告要钉在事件声明的那一代，读「当前」会把已经
    被 supersede 的结论当成输入。
    """
    if not base_url:
        return _unconfigured(
            f"{service_id}_REPORT_SOURCE_UNAVAILABLE",
            f"base URL for {service_id} is not configured; S39 has no QC "
            f"report to plan from and will not fabricate one (plan §4.2)")
    template, revision_param, keys = REPORT_PATHS.get(
        service_id, ("/qc-reports/{id}", "generation", ("payload",)))
    prefix = base_url.rstrip("/")

    def _report(tenant_id: str, ref: dict) -> dict:
        report_id = urllib.parse.quote(str(ref["id"]))
        url = prefix + template.format(id=report_id)
        revision = ref.get("revision")
        if revision_param and revision is not None:
            url += f"?{revision_param}={int(revision)}"
        payload = _request("GET", url,
                           headers={"X-Tenant-Id": tenant_id}, timeout=20.0)
        if not isinstance(payload, dict):
            raise ServiceError(
                503, f"{service_id}_REPORT_SOURCE_UNAVAILABLE",
                "report response is not a JSON object")
        for key in keys:
            if isinstance(payload.get(key), dict):
                return payload[key]
        return payload
    return _report


def build_continuity_source(base_url: str | None):
    """S17 跨集冲突。**可选**：C8 说它既没有 schema 也没有事件名。

    未配置返回 `None`——注意不是「假装通过」：内核在覆盖断言里会把这层算成
    `expected` 的一部分（如果 registry 登记了 S17 拥有的 check），缺口会变成
    `coverage_gap` → escalated。
    """
    if not base_url:
        return None
    prefix = base_url.rstrip("/")

    def _continuity(tenant_id: str, ref: dict) -> dict:
        report_id = urllib.parse.quote(str(ref["id"]))
        url = f"{prefix}/continuity-reports/{report_id}"
        payload = _request("GET", url,
                           headers={"X-Tenant-Id": tenant_id}, timeout=20.0)
        if isinstance(payload, dict):
            for key in ("payload", "report"):
                if isinstance(payload.get(key), dict):
                    return payload[key]
        return payload
    return _continuity


# --------------------------------------------------------- S24 / S25

def build_capability_source(base_url: str | None):
    """`(tenant_id, requirement) -> {candidates, excluded, revision}`（只查不调用）。

    真实形状来自 `services/model-registry/api/app.py:241`：`GET
    /model-candidates?tenant_id=…&modality=…&required_capabilities=a,b`，响应
    `{capability_bundle_revision, projection_watermark, candidates[], excluded[]}`。
    能力过滤在 S24 侧完成（`CAPABILITY_MISSING:{capability}`），本服务只读
    结果——**不自己解释候选的能力声明**，那会变成第二份能力表。

    `max_window_frames` 在 S24 的能力词表里不存在（C19），所以它**不发出去**：
    发一个会被静默忽略的查询参数，比不发更容易让人以为它生效了。
    """
    if not base_url:
        return _unconfigured(
            "CAPABILITY_SOURCE_UNAVAILABLE",
            "S24_BASE_URL is not configured; S39 cannot prove temporal "
            "capability and will not route a video repair on a guess (plan "
            "§3.2)")
    prefix = base_url.rstrip("/")

    def _capabilities(tenant_id: str, requirement: dict) -> dict:
        query = {
            "tenant_id": tenant_id,
            "modality": requirement.get("modality") or "video",
            "required_capabilities": ",".join(
                requirement.get("required_capabilities") or []),
        }
        payload = _request(
            "GET", f"{prefix}/model-candidates?" + urllib.parse.urlencode(query),
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if not isinstance(payload, dict) or not isinstance(
                payload.get("candidates"), list):
            raise ServiceError(
                503, "CAPABILITY_SOURCE_UNAVAILABLE",
                "model-registry response has no candidates list; refusing to "
                "treat an unreadable answer as 'capable'")
        return payload
    return _capabilities


# ---------------------------------------------------------------- S21

def build_run_source(base_url: str | None):
    """`(tenant_id, run_id) -> {remaining_repair_attempts, budget_exhausted}`。

    §11 C6：S21 还没有这个查询接口。所以这里**可选**：未配置返回 `None`，
    计划记 `run_precheck=unavailable`，Level 4 交给 S21 执行期兜底。
    """
    if not base_url:
        return None
    prefix = base_url.rstrip("/")

    def _run(tenant_id: str, run_id: str) -> dict:
        return _request("GET",
                        f"{prefix}/runs/{urllib.parse.quote(str(run_id))}"
                        f"/repair-quota",
                        headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _run
