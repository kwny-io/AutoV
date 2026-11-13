"""S36 的依赖闭包。**这里只出现 S26 / S28 / S17 三个上游，没有供应商。**

这不是风格问题，是信任边界（AGENTS.md「不能绕过 S26 直连模型」）：

- 模型只在 S26 后面。本文件里没有任何 `api.openai.com` 之类的地址，也没有
  供应商密钥的读取；想加一条直连都改不动——`build_*` 的 URL 只有一个基址。
- 每个闭包都带 `configured` 属性。`service._require_upstream()` 读它，
  未配置时在**花任何力气之前**返回 503。这样「忘了配 S26」不会退化成
  「没有模型意见，那就当通过」——那正是 docs/16 E25 禁止的。
- 上游 4xx 原样上抛（保住 404 的"不存在"语义），5xx 与连接失败统一成 503 +
  retryable，让调用方知道可以重试而不是以为结论已出。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .service import ServiceError

DEFAULT_TIMEOUT = 15.0


def _request(method: str, url: str, *, body=None, headers=None,
             timeout: float = DEFAULT_TIMEOUT):
    """最小 HTTP 客户端。

    错误映射只做一件事：把上游的**拒绝**与上游的**不可达**分开。
    前者可能带业务码（预算耗尽、跨租户 404），后者一律是「没结论」。
    """
    payload = None
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method=method,
                                     headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {}
        raise ServiceError(
            exc.code,
            (detail or {}).get("code") or "UPSTREAM_ERROR",
            (detail or {}).get("message") or str(exc),
            retryable=bool((detail or {}).get("retryable")),
            details=(detail or {}).get("details") or {}) from exc
    except (urllib.error.URLError, OSError) as exc:
        # 连接层失败不是业务结论。写成 ConnectionError，让 service._upstream
        # 统一翻译成 503。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc
    if not raw:
        return {}
    return json.loads(raw)


def _get(url, *, headers=None):
    return _request("GET", url, headers=headers)


def _post(url, body, *, headers=None):
    return _request("POST", url, body=body, headers=headers)


def _unconfigured(code: str, what: str):
    """未配置的闭包。

    它必须在调用时抛，而不是在构造时——构造发生在 import 期，那时连不上
    任何东西也不该让进程起不来；但一旦有人真来下命令，就必须立刻 503。
    """
    def _raise(*_a, **_kw):
        raise ServiceError(
            503, code,
            f"{what} is not configured; S36 refuses to produce a verdict "
            f"without it (a missing dependency must never read as a pass)")
    _raise.configured = False
    return _raise


# ---------------------------------------------------------------- S28 素材

def build_artifact_source(base_url: str):
    """读素材元数据。**只读**，不写 S28 的库。

    返回字段映射：S28 的 revision 读给容器/时长/宽高/帧率/声道，够 S36 判
    语义；codec/pix_fmt/is_vfr 那几项它不给，`service._artifact_view` 会
    按 SQC-12 标成推断值，且不进技术层判定。
    """
    if not base_url:
        return _unconfigured("S28_UNAVAILABLE", "S28 artifact read")

    def _read(tenant_id: str, ref: dict) -> dict:
        url = (f"{base_url.rstrip('/')}/artifacts/{ref['id']}"
               f"/revisions/{int(ref['revision'])}")
        return _get(url, headers={"X-Tenant-Id": tenant_id})

    _read.configured = True
    return _read


# ---------------------------------------------------------- S17 期望状态

def build_expected_state_source(base_url: str):
    """读 S17 的 continuity report。

    SQC-1：仓库里没有 `s17-expected-state.v2` schema，S17 的真实响应是
    `{report, defects, expected_state, exemption_applications}`。这里只挑
    `expected_state` 行，不改名、不补字段——S36 对它的理解写在
    `service._resolve_expected_state` 一处。
    """
    if not base_url:
        return _unconfigured("S17_UNAVAILABLE", "S17 expected-state read")

    def _read(tenant_id: str, ref: dict) -> dict:
        url = f"{base_url.rstrip('/')}/continuity-reports/{ref['id']}"
        return _get(url, headers={"X-Tenant-Id": tenant_id})

    _read.configured = True
    return _read


# ------------------------------------------------------------- S26 提交

def build_s26_submit_source(base_url: str):
    """把一次 VLM 评估变成 S26 的 generation job。

    `route_decision_id` 是**调用方**给的（命令体 `evaluator_route_decision_id`）。
    S36 的 `depends_on` 里没有 S25，所以它不自己问路由——自己编一条路由
    等于替 S25 做决定，是第二个权威（SQC-9）。
    """
    if not base_url:
        return _unconfigured("S26_UNAVAILABLE", "S26 job submission")

    def _submit(tenant_id: str, command: dict, route_decision_id: str) -> dict:
        body = {
            "command_id": command["idempotency_key"],
            "tenant_id": tenant_id,
            "route_decision_id": route_decision_id,
            "route_decision_ref": {
                "service_id": "S25",
                "id": route_decision_id,
                "revision": 1,
                # 这里没有路由决策的内容哈希可填；S26 只要求它是 64 hex。
                # 用 route id 的占位会伪造一个"已验证"的哈希，所以留空串并在
                # 返回值里不参与任何判定。
                "content_hash": "",
            },
            "generation_command": command,
        }
        result = _post(f"{base_url.rstrip('/')}/generation-jobs", body,
                       headers={"X-Tenant-Id": tenant_id})
        return {
            "provider_job_id": result.get("provider_job_id")
            or result.get("operation_id") or "",
            "operation_id": result.get("operation_id") or "",
            "attempt_id": result.get("attempt_id") or "",
        }

    _submit.configured = True
    return _submit


# --------------------------------------------------------------- S26 查询

def build_s26_job_source(base_url: str):
    """读 S26 job 状态。**只读**——S36 从不重投、从不 reconcile。

    `SUBMISSION_UNKNOWN` / `RECONCILING` 的判断在 `service._invoke` 里：
    那两种状态下我们不知道供应商是否已经收下这次付费调用，所以既不能重提
    也不能判通过，只能让这一项没有结论（AGENTS.md 硬红线）。
    """
    if not base_url:
        return _unconfigured("S26_UNAVAILABLE", "S26 job read")

    def _read(tenant_id: str, provider_job_id: str) -> dict:
        url = f"{base_url.rstrip('/')}/generation-jobs/{provider_job_id}"
        return _get(url, headers={"X-Tenant-Id": tenant_id})

    _read.configured = True
    return _read


def build_default_deps() -> dict:
    """按环境变量装配。全部缺省时三个都未配置——服务会整体 503。

    这是刻意的默认值：一个没有任何上游的 S36 只能编结论，宁可不启动。
    """
    import os
    return {
        "artifact_source": build_artifact_source(os.environ.get("S28_BASE_URL", "")),
        "expected_state_source": build_expected_state_source(
            os.environ.get("S17_BASE_URL", "")),
        "submit_source": build_s26_submit_source(
            os.environ.get("S26_BASE_URL", "")),
        "job_source": build_s26_job_source(os.environ.get("S26_BASE_URL", "")),
    }
