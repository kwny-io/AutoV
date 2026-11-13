"""S47 的两个上游。**都是只读，都是 GET。**

§1.4 / §8.3 把"绝不写回"做成了可观测事实：这个模块里没有 `post/put/patch/
delete` 的包装函数，连一个通用的 `_request(method, ...)` 都不提供——只提供
`_get(url, ...)`。想写回得先改这个文件，那是一次显式的、会被 review 的改动，
而不是"顺手传个 method"。

两个上游的形状都是**读上游代码核对过的**，不是从方案里抄的：

- **S21** `GET /runs/{id}`：`services/orchestration/src/orchestration/service.py`
  的 `get_run` 恰好返回 7 个字段——`run_id, manifest_revision, execution_fence,
  state, completed_nodes, blocked_nodes, projection_watermark`。有计划地**没有**
  `total_shots` / `current_node` / `project_id`：那是 C47-15 / C47-16，回源也
  补不齐，只能登记。
- **S01** `GET /authorization-context?scope_kind=&scope_id=&principal_id=&purpose=`：
  返回 `permissions` / `denied` / `revocation_epoch`（不含 `roles`）。**实时**，
  不缓存——E27 要求撤销立即生效，缓存一次就等于让撤销晚一个 TTL。

未配置时两者都**必然失败**（503），没有"忘了配就放行/就给空投影"的路径。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError


def _get(url: str, *, headers: dict | None = None,
         timeout: float = 15.0) -> dict:
    """唯一的出站原语。方法写死在 `Request` 里，调用方无法改成写。"""
    merged = {"Accept": "application/json"}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        payload: dict = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            pass
        raise ServiceError(
            exc.code, payload.get("code") or "UPSTREAM_ERROR",
            payload.get("message") or str(exc),
            retryable=bool(payload.get("retryable")),
            details=payload.get("details") or {})
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S21 回源

def build_run_source(base_url: str | None):
    """`(tenant_id, run_id) -> {state, projection_watermark, ...}`（§3.8）。

    **只返回 S21 真的有的字段**。不在这里补 `total_shots` 之类——回源的目的是
    "对齐水位"，不是"凑出 §4.2 的形状"；凑形状会让前端以为那些值可信。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "PROJECTION_UNAVAILABLE",
                "S21_BASE_URL is not configured; a GAPPED projection stays "
                "GAPPED rather than being filled from a guess (plan §3.8)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _run(tenant_id: str, run_id: str) -> dict:
        result = _get(
            f"{prefix}/runs/{urllib.parse.quote(str(run_id), safe='')}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        watermark = result.get("projection_watermark")
        if not isinstance(watermark, int):
            # 没有水位就没法做 CAS：宁可报错，也不"用当前时间当水位"。
            raise ServiceError(
                502, "UPSTREAM_CONTRACT_VIOLATION",
                "S21 GET /runs/{id} returned no integer projection_watermark")
        return {
            "run_id": result.get("run_id", run_id),
            "state": result.get("state"),
            "manifest_revision": result.get("manifest_revision"),
            "completed_nodes": result.get("completed_nodes"),
            "blocked_nodes": result.get("blocked_nodes"),
            "execution_fence": result.get("execution_fence"),
            "projection_watermark": watermark,
        }
    return _run


# ---------------------------------------------------------------- S01 授权

def build_authorization_source(base_url: str | None):
    """`(tenant_id, principal_id, scope_kind, scope_id) -> context`。

    返回体的 `denied` 是**拒绝清单**：S01 用"列出被拒的权限"表达否定，所以
    判断"能不能看"要看 `permissions` 里有没有对应项，而不是看有没有 `denied`。
    这里把两者都原样带出，由 service 决定，不在 deps 里偷偷做一次简化判断。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AUTHORIZATION_UNAVAILABLE",
                "S01_BASE_URL is not configured; S47 will not assume a "
                "principal may watch a run (plan §4.5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _authz(tenant_id: str, principal_id: str, scope_kind: str,
               scope_id: str) -> dict:
        query = urllib.parse.urlencode({
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "principal_id": principal_id,
            "purpose": "projection.read",
        })
        return _get(
            f"{prefix}/authorization-context?{query}",
            headers={"X-Tenant-Id": tenant_id,
                     "X-Principal-Id": principal_id},
            timeout=5.0)
    return _authz
