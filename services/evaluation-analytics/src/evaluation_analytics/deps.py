"""S44 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

每条是 `build_*(base_url)`，未配置时返回一个**必然抛 503** 的桩：
`ServiceError(503, "<CODE>_UNAVAILABLE", ...)`。没有「默认值兜底」，也没有
「空结果假装查过」——读模型最危险的错误就是把「没查到」渲染成 0
（方案 §0 第 3 条、§2.4 「无数据不是 0」）。

已实现的上游：

- **S23** `services/budget-ledger`：`GET /episodes/{id}/cost-breakdown?cohort=`。
  只读原始条目，S44 不在本地复制账本。
- **S35 / S36 / S37**：技术 / 语义 / 音频 QC 报告，按 ref 的 `service_id` 选路。
- **S40** `services/human-review`：`GET /review-decisions`（人工决定）。
- **S43** `services/delivery`：`GET /delivery-packages?episode_id=`（发布时间，
  K4 的分子）。
- **评测执行网关**（S21→S26）：`POST /evaluation-executions`。本服务
  **不自行发起付费生成**：网关未配置或开关未打开时 503。

未实现 / design_only 的上游同样以 `build_*` 出现，未配置即 503；测试注入
桩。`execution_fence` / `node_epoch` 只在这里被**校验**，不进任何查询参数：
本服务没有任何以它们为坐标的读面。
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


def _unconfigured_env(var: str, code: str, why: str):
    def _stub(*_args, **_kwargs):
        raise ServiceError(503, code, f"{var} is not configured; {why}")
    # 与 `service._missing_source` 同一约定：桩自报身份，`/health/readiness`
    # 才能如实报出「哪些源现在根本读不了」，而不是去猜哪个 env 没配。
    _stub._unavailable_code = code
    return _stub


# ------------------------------------------------------------- S23 raw cost

def build_cost_source(base_url: str | None):
    """`(tenant_id, query) -> {"entries": [...], "watermark": int}`。

    取数路径按 S23 方案：`GET /episodes/{id}/cost-breakdown?cohort=`；
    `episode_id` 为 `*` 时走 `GET /cost-breakdown?cohort=`（按 cohort 列账）。
    条目形状：`{cost_entry_id, amount_micro, currency, cost_type,
    attribution{project_id, series_id, episode_id, shot_id, minutes},
    as_of_watermark}`。金额是**整数 micro** 与币种；S44 从不跨币种求和。

    这是 S23 方案里的读面；S23 尚未实现时，生产环境只能是 503。
    """
    if not base_url:
        return _unconfigured_env(
            "S23_BASE_URL", "COST_SOURCE_UNAVAILABLE",
            "S44 will not report a cost KPI without S23's raw entries "
            "(plan §0: S23 is the only cost authority)")

    prefix = base_url.rstrip("/")

    def _cost(tenant_id: str, query: dict) -> dict:
        cohort = json.dumps(query.get("cohort") or {}, ensure_ascii=False,
                            separators=(",", ":"))
        episode_id = query.get("episode_id")
        if episode_id and episode_id != "*":
            path = f"/episodes/{urllib.parse.quote(str(episode_id))}" \
                   "/cost-breakdown"
        else:
            path = "/cost-breakdown"
        url = f"{prefix}{path}?" + urllib.parse.urlencode({"cohort": cohort})
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=30.0)
    return _cost


# ---------------------------------------------------------------- QC reports

_QC_PATH = {
    "S35": "/technical-reports/{id}",
    "S36": "/semantic-reports/{id}",
    "S37": "/alignment-reports/{id}",
}


def build_qc_source(base_urls: dict | None):
    """`(tenant_id, request) -> 报告或报告清单`。

    `request.op`：

    - `"report"`：按 `{service_id, report_id}` 取单份报告（引用式）。
    - `"query"`：按 episode/shot 列报告，返回 `{rows, watermark}`。

    两者都把 `decision` 与 `defects` 原样搬运，**不重算**；`abstain` 保持为
    `abstain` —— 它不是 pass（E25-S44-01/02）。
    """
    base_urls = {k: v for k, v in (base_urls or {}).items() if v}
    if not base_urls:
        return _unconfigured_env(
            "QC_BASE_URL", "QC_SOURCE_UNAVAILABLE",
            "S44 will not compute a quality KPI without the QC reports "
            "(plan §4.1 step 3)")

    prefixes = {k: v.rstrip("/") for k, v in base_urls.items()}

    def _qc(tenant_id: str, request: dict) -> dict:
        op = request.get("op") or "report"
        if op == "query":
            rows: list[dict] = []
            watermarks = []
            for service_id, prefix in sorted(prefixes.items()):
                params = {k: v for k, v in request.items()
                          if k not in ("op",) and v is not None}
                url = f"{prefix}/reports?" + urllib.parse.urlencode(params,
                                                                    doseq=True)
                result = _request("GET", url,
                                  headers={"X-Tenant-Id": tenant_id},
                                  timeout=30.0)
                for row in result.get("rows") or []:
                    rows.append({**row, "service_id": service_id})
                if isinstance(result.get("watermark"), int):
                    watermarks.append(result["watermark"])
            return {"rows": rows,
                    "watermark": min(watermarks) if watermarks else None}

        service_id = str(request.get("service_id") or "")
        template = _QC_PATH.get(service_id)
        if template is None or service_id not in prefixes:
            raise ServiceError(
                503, "QC_SOURCE_UNAVAILABLE",
                f"no report source for QC service {service_id!r}")
        url = prefixes[service_id] + template.format(
            id=urllib.parse.quote(str(request["report_id"])))
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=15.0)
        payload = result.get("payload") if isinstance(result.get("payload"),
                                                      dict) else {}
        return {
            "service_id": service_id,
            "report_id": request["report_id"],
            "decision": result.get("decision") or payload.get("decision"),
            "defects": payload.get("defects") or result.get("defects") or [],
            "watermark": result.get("watermark"),
        }
    return _qc


# ------------------------------------------------------------ S40 decisions

def build_review_source(base_url: str | None):
    """`(tenant_id, query) -> {"decisions": [...], "watermark": int}`。"""
    if not base_url:
        return _unconfigured_env(
            "S40_BASE_URL", "REVIEW_SOURCE_UNAVAILABLE",
            "S44 will not compute an intervention/memory KPI without S40's "
            "decisions (plan §4.1 step 3)")

    prefix = base_url.rstrip("/")

    def _reviews(tenant_id: str, query: dict) -> dict:
        params = {k: v for k, v in query.items() if v is not None}
        url = f"{prefix}/review-decisions"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=15.0)
    return _reviews


# ------------------------------------------------------- S43 delivery records

def build_delivery_source(base_url: str | None):
    """`(tenant_id, query) -> {"packages": [...], "watermark": int}`。"""
    if not base_url:
        return _unconfigured_env(
            "S43_BASE_URL", "DELIVERY_SOURCE_UNAVAILABLE",
            "S44 will not compute delivery lead time without S43's publish "
            "records (plan §4.1 step 3)")

    prefix = base_url.rstrip("/")

    def _delivery(tenant_id: str, query: dict) -> dict:
        params = {k: v for k, v in query.items() if v is not None}
        url = f"{prefix}/delivery-packages"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=15.0)
    return _delivery


# ------------------------------------------------------------ S26 job facts

def build_job_source(base_url: str | None):
    """`(tenant_id, query) -> {"episodes": [...], "watermark": int}`。

    §4.1 第 3 步要读 S26 的任务与尝试（含失败、取消、未知）。服务清单的
    `depends_on` 里没有 S26（S44-14），但 K2/K4/K5/K6 的分母都在那里；
    未配置即 503，不拿别的源凑。
    """
    if not base_url:
        return _unconfigured_env(
            "S26_BASE_URL", "JOB_SOURCE_UNAVAILABLE",
            "S44 will not compute a shot/node KPI without S26's run facts "
            "(plan §4.1 step 3)")

    prefix = base_url.rstrip("/")

    def _jobs(tenant_id: str, query: dict) -> dict:
        params = {k: v for k, v in query.items() if v is not None}
        url = f"{prefix}/runs"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=30.0)
    return _jobs


# --------------------------------------------------------- S08 accepted facts

def build_accepted_source(base_url: str | None):
    """`(tenant_id, query) -> {"episodes": [...], "watermark": int}`。

    每集带 `{episode_id, entered_formal_render, accepted, duration_seconds}`。
    `accepted` 只来自 S08 的接受指针；本服务不读 S23 也不推断「没拒绝就是
    接受」。未配置即 503（K1 与 K3 的分母都要它）。
    """
    if not base_url:
        return _unconfigured_env(
            "S08_BASE_URL", "ACCEPTED_SOURCE_UNAVAILABLE",
            "S44 will not claim an episode is usable without S08's accepted "
            "pointer (plan §2.3 K1)")

    prefix = base_url.rstrip("/")

    def _accepted(tenant_id: str, query: dict) -> dict:
        params = {k: v for k, v in query.items() if v is not None}
        url = f"{prefix}/accepted-masters"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=30.0)
    return _accepted


# ------------------------------------------------------ evaluation execution

def build_evaluator_source(base_url: str | None):
    """`(tenant_id, request) -> {"gateway_ref": ..., "items": [...], ...}`。

    评测若需要生成新样本，必须走 S21 编排 → S26 准入（§1 末段）。本服务
    只发**评测请求**，不持有预算、不直连模型、不算钱。网关未配置即 503；
    即使配置了，`evaluation_execution_enabled` 默认 false，调用方仍会拿到
    503 `EVALUATION_EXECUTION_DISABLED`（AGENTS.md 的停止条件）。
    """
    if not base_url:
        return _unconfigured_env(
            "EVALUATOR_BASE_URL", "EVALUATOR_UNAVAILABLE",
            "evaluation execution must go through S21->S26; S44 has no direct "
            "paid path (plan §1, §10 step 8)")

    prefix = base_url.rstrip("/")

    def _evaluate(tenant_id: str, request: dict) -> dict:
        return _request("POST", f"{prefix}/evaluation-executions",
                        body=request,
                        headers={"X-Tenant-Id": tenant_id,
                                 "X-Producer-Id": SOURCE_ID},
                        timeout=300.0)
    return _evaluate


SOURCE_ID = "S44"


# ------------------------------------------------------ execution context

CONTEXT_SCHEMA_PATH = "contracts/execution-context.v2.schema.json"

# 契约要求的 18 个键。这里重复一份是为了在 `jsonschema` 不可用时仍然
# fail-closed：缺任何一个都 422，不是「校验器没装就放行」。
REQUIRED_CONTEXT_KEYS = (
    "schema_version", "tenant_id", "project_id", "series_id", "branch_id",
    "episode_id", "home_region", "home_cell", "manifest_ref", "node_id",
    "node_epoch", "execution_fence", "memory_checkpoint", "policy_ref",
    "actor", "command_id", "request_hash", "trace_id",
)


def _load_context_schema(root=None) -> dict | None:
    import os
    base = root or os.getcwd()
    path = os.path.join(base, CONTEXT_SCHEMA_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def validate_execution_context(context, root=None) -> dict:
    """按 `contracts/execution-context.v2.schema.json` 校验，失败即 422。

    两条纪律：

    1. **缺契约文件不是放行理由。** 读不到 schema 时用内置的 18 键清单与
       少量类型检查兜底，仍然拒绝。
    2. **`execution_fence` 与 `node_epoch` 各自校验，从不互相比较。**
       fence 属于 run/branch，epoch 属于单个 `logical_node`（AGENTS.md）；
       本服务只把这两个值当作入参形状的一部分，不拿任何一个推进指针。
    """
    if not isinstance(context, dict):
        raise ServiceError(422, "INVALID_EXECUTION_CONTEXT",
                           "execution_context must be an object")
    missing = [key for key in REQUIRED_CONTEXT_KEYS if key not in context]
    if missing:
        raise ServiceError(422, "INVALID_EXECUTION_CONTEXT",
                           "execution_context is missing required keys",
                           details={"missing": missing})
    for key in ("node_epoch", "execution_fence"):
        value = context.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ServiceError(422, "INVALID_EXECUTION_CONTEXT",
                               f"{key} must be an integer >= 1",
                               details={"field": key})
    actor = context.get("actor")
    if not isinstance(actor, dict) or not actor.get("principal_id") \
            or not actor.get("delegation_id"):
        raise ServiceError(422, "INVALID_EXECUTION_CONTEXT",
                           "actor needs principal_id and delegation_id")
    if context.get("schema_version") != "2.0":
        raise ServiceError(422, "INVALID_EXECUTION_CONTEXT",
                           "schema_version must be '2.0'")

    schema = _load_context_schema(root)
    if schema is None:
        return context
    try:
        import jsonschema  # noqa: F401
    except ImportError:  # 内建检查已经在上面做过；不多信也不多拒
        return context
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(context),
                    key=lambda error: list(error.path))
    if errors:
        raise ServiceError(
            422, "INVALID_EXECUTION_CONTEXT",
            "execution_context does not match execution-context.v2",
            details={"violations": [error.message for error in errors[:8]]})
    return context
