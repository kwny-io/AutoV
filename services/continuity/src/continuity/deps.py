"""S17 的两条只读依赖：S16 的正史查询、S06 的镜头读取。

两者都是**注入式**的 callback，不是 import。理由是 AGENTS.md 的两条红线：

- 不许互相 import 业务 ORM，也不许跨库查询 → 只能走服务边界。
- 不许变成第二权威 → S17 每次检查都重新问，不缓存 S16 的事实。

未配置 base url 时返回 `None`。`None` 不是"检查通过"：`shot_source=None` 时
每个镜头都会落成 `MISSING_EVIDENCE`，`memory_query` 是构造函数的必需参数，
所以没有"忘了配就静默放行"的路径。

`memory_query` 抛 `MEMORY_WATERMARK_BEHIND` 时**必须**带着这个码传上去 ——
§6 的整次 abstain 依赖它。所以这里把 S16 的 409 映射回同名的 `ServiceError`，
而不是吞掉或改成别的异常。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .service import ServiceError


def _post(url: str, body: dict, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
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


def _get(url: str, timeout: float = 10.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        raise ServiceError(exc.code, "UPSTREAM_ERROR", str(exc))
    except (urllib.error.URLError, OSError):
        return None


def build_memory_query(base_url: str | None):
    """S16 `POST /memory-queries`。`base_url` 未配置就没有可用的正史源。"""
    if not base_url:
        def _unconfigured(tenant_id: str, series_id: str, body: dict) -> dict:
            raise ServiceError(
                503, "MEMORY_AUTHORITY_UNCONFIGURED",
                "S16_BASE_URL is not configured; continuity checks cannot run")
        return _unconfigured

    url = base_url.rstrip("/") + "/memory-queries"

    def _query(tenant_id: str, series_id: str, body: dict) -> dict:
        return _post(url, {"series_id": series_id, **body},
                     timeout=15.0)
    return _query


def build_shot_source(base_url: str | None):
    """S06 镜头读取。未配置 → 返回 None，于是所有镜头都是 `MISSING_EVIDENCE`。"""
    if not base_url:
        return None
    prefix = base_url.rstrip("/")

    def _shot(tenant_id: str, series_id: str, branch_id: str,
              shot_id: str) -> dict | None:
        return _get(f"{prefix}/shots/{shot_id}?series_id={series_id}"
                    f"&branch_id={branch_id}")
    return _shot
