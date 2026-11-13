"""S45 的上游，全部是注入式 callback，不是 import。

本服务只有一个上游：**S01**。它的形状和方案 `plans/S45-audit.md` §10.2 假设的
不完全一样，差异吸收在这里，**不让内核看到**：

- S01 答的是 `GET /authorization-context?scope_kind=&scope_id=&principal_id=&purpose=`，
  返回 `permissions` / `denied` / `revocation_epoch`，**不返回角色**。本服务
  要的正是"这个人此刻能不能对这个租户做这件事"——正好是它答的题。
- 缺的是**权限键本身**：注册表 40 个键里一个 `audit.*` 都没有（NEW-1）。
  所以 `REGISTERED_PERMISSIONS` 是空集，四个动作（read / export / destroy /
  retention.change）在真实 S01 上都会 503 `PERMISSION_KEY_UNREGISTERED`。
  这是**如实反映契约现状**，不是实现漏了：拿一个能读审计的凭证去销毁审计，
  不应该因为"读得了"就放行。

**摄入路径不问 S01。** 这是本服务"可降级"的核心：§2.4 说写入方自己已经判定
过动作是否被允许，S45 只记录它。如果 S45 在摄入时也去问 S01，那么 S01 一抖动
所有业务写入都跟着停——正好是 S19 方案里明说不允许的事。所以
`authorization_source=None` 时读/导出/销毁 503，摄入照常。

未配置 base url 时返回必然失败的 callback。没有"忘了配就放行"的路径。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .schema import REGISTERED_PERMISSIONS  # noqa: F401  (导出给测试核对)
from .service import ServiceError

# §4.1 第 3 步给授权 800ms 预算，超时即 503，不落库。
AUTHORIZATION_DEADLINE_SECONDS = 0.8


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
        # 超时/不可达要和"上游说了不行"分开：前者是 503 可重试，后者是 4xx。
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


def build_authorization_source(base_url: str | None, *,
                               deadline_seconds: float = AUTHORIZATION_DEADLINE_SECONDS):
    """返回 `(tenant_id, principal_id, scope_kind, scope_id, permission) -> dict`。

    dict 至少含 `allowed: bool`。`allowed` 只在 S01 明确把该权限列进
    `permissions` 且没列进 `denied` 时为真——什么都不说就是不许。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "DEPENDENCY_UNAVAILABLE",
                "S01_BASE_URL is not configured; refusing to serve audit "
                "reads, exports, retention changes or destruction without an "
                "authorization context")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _authorize(tenant_id: str, principal_id: str, scope_kind: str,
                   scope_id: str, permission: str) -> dict:
        # 键没注册就不必问 S01：它永远不会把未注册的键列进 permissions，
        # 而"因为没有这个键所以拒"和"这个人没这个权限"必须分开报。
        if permission not in REGISTERED_PERMISSIONS:
            raise ServiceError(
                503, "PERMISSION_KEY_UNREGISTERED",
                f"permission {permission!r} is not in "
                "contracts/identity/permission-registry.v2.json; the audit "
                "domain has no registered keys yet (contract gap NEW-1)")
        query = urllib.parse.urlencode({
            "scope_kind": scope_kind, "scope_id": scope_id,
            "principal_id": principal_id, "purpose": permission,
        })
        try:
            result = _request(
                "GET", f"{prefix}/authorization-context?{query}",
                headers={"X-Tenant-Id": tenant_id,
                         "X-Principal-Id": principal_id},
                timeout=deadline_seconds)
        except ConnectionError as exc:
            raise ServiceError(503, "DEPENDENCY_UNAVAILABLE", str(exc),
                               retryable=True) from exc
        granted = permission in (result.get("permissions") or [])
        denied = permission in (result.get("denied") or [])
        return {**result, "allowed": granted and not denied,
                "permission": permission}

    return _authorize


def build_redaction_scanner(base_url: str | None, *,
                            deadline_seconds: float = 0.5):
    """外部 `secret-scanner.v1`。没配就返回 None，由内核退到本地实现。

    契约（`redaction-policy.v2.json`）要求扫描器**可用性**失败时
    逐字符串截断 8 字符 + `REDACTED:scanner_unavailable`，而不是跳过扫描。
    所以这里的 None 不是"不做脱敏"，是"用内建规则做脱敏"；真正的
    "认不出"由内核的失败闭合分支处理。
    """
    if not base_url:
        return None

    prefix = base_url.rstrip("/")

    def _scan(text: str, path: str, key: str | None,
              secret_key) -> tuple[str, list[str]]:
        try:
            result = _request("POST", f"{prefix}/scan",
                              body={"text": text, "path": path, "field": key},
                              timeout=deadline_seconds)
        except ConnectionError as exc:
            # 失败闭合：内核会看到这个 ConnectionError 并截断处理。
            raise
        except ServiceError:
            raise
        return result.get("text", text), result.get("classes") or []

    return _scan
