"""S18 的唯一上游依赖：S16 的正史查询。

注入式 callback，不是 import。理由和 S17 一样：

- AGENTS.md 禁止互相 import 业务 ORM / 跨库查询 → 只能走服务边界。
- 不许变成第二权威 → 每次组装都重新问 S16，不缓存事实。

S16 的 `query` 自己执行分支、故事有效期、角色知情、观众揭示这四步过滤（见
`services/story-memory/src/story_memory/service.py` 的 `query` 与
`_visible_to_character`）。S18 **原样把这些参数透传**，不重新判 —— 判定可见性
的公式只能有一份。

未配置 base url 时返回一个必然失败的 callback：`503 MEMORY_AUTHORITY_UNCONFIGURED`。
没有"忘了配就返回空 pack"的路径 —— 空 pack 会被下游当成"这些就是全部事实"。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .service import ServiceError


def _post(url: str, body: dict, timeout: float = 15.0) -> dict:
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
    except (urllib.error.URLError, OSError) as exc:
        # §5.1 C 情形：S16 不可达 → 503，不降级。
        raise ServiceError(503, "MEMORY_UNAVAILABLE",
                           f"memory authority is unreachable: {exc}",
                           retryable=True)


def build_memory_query(base_url: str | None):
    """S16 `POST /series/{series_id}/memory/query`。"""
    if not base_url:
        def _unconfigured(tenant_id: str, series_id: str, body: dict) -> dict:
            raise ServiceError(
                503, "MEMORY_AUTHORITY_UNCONFIGURED",
                "S16_BASE_URL is not configured; packs cannot be assembled")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _query(tenant_id: str, series_id: str, body: dict) -> dict:
        return _post(f"{prefix}/series/{series_id}/memory/query", body)
    return _query
