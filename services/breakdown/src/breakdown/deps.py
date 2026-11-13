"""S05 的上游，全部注入式 callback，不 import 任何服务的业务模块（AGENTS.md）。

三条上游，形状都是**读上游代码核对过的**：

- **S04** `GET /scripts/{id}/revisions/{revision}` 给元数据（`state` /
  `content_hash` / `line_set_hash`），`GET .../lines` 给分页行列表。两个都要
  读：只读元数据拿不到行，只读行拿不到 `state` 与两个 hash。分页用
  `next_after_line_id` 游标，**不按 `limit` 猜总行数**（S04 的行序只由 `seq`
  表达，见 C7）。
- **S18** `GET /context-packs/{pack_id}` 返回 `{pack: {...}}`（与 S19 的
  adapter 同形）。`pack.content_hash` 用来核对 pin 的 hash；`pack.stale`
  为真时不给「差不多新」的降级。
- **S19** `GET /agent-tasks/{id}/result` 返回 `{proposal: {...}}`。S05 只读
  这一条结果，不读任务状态 —— 「任务成功」与「结果是可用的场次提案」是两件
  事，后者由 S05 的五条校验判断。

未配置一律 503，没有「忘了配就给默认值」的路径（§7.5 / 本仓 S04 同一约定）。
`ConnectionError` 交给内核的 `_upstream` 转 503；HTTP 错误按上游码原样抛出，
让内核决定哪些码可以降级（S18 的 503 可降级成 `partial`，S04 的不能）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError

# S04 的 `list_revision_lines` 单页上限（`narrative.schema.MAX_LIST_LIMIT`）。
# 这里主动取 200，并用游标翻页；写死一个比上游小很多的值只会让大剧本多绕几圈。
LINE_PAGE_SIZE = 200
# 防御性上限：分页死循环（上游恒返回同一个 `next_after_line_id`）时及时停。
MAX_LINE_PAGES = 500


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
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S04 剧本

def build_script_source(base_url: str | None):
    """`(tenant_id, script_id, revision) -> {state, lines, content_hash, ...}`。

    行列表**整版**返回：S05 的五条校验都要以「S04 实际给出的行集合」为分母，
    分页只取第一页会让漏行校验把没读到的行全判成缺失。

    上游 404 原样上抛，内核转成 `SCRIPT_NOT_FOUND`（E28：与「不存在」同码，
    不泄露跨租户存在性）。上游 `list_revision_lines` 的游标坏了（404
    `LINE_NOT_FOUND`）也是上游不自洽 → 内核按 5xx 处理，不能当成「行读完
    了」。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "S04_BASE_URL is not configured; S05 will not break down a "
                "script it cannot read (plan §7.5, T05-35)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _script(tenant_id: str, script_id: str, revision: int) -> dict:
        quoted = urllib.parse.quote(str(script_id))
        headers = {"X-Tenant-Id": tenant_id}
        meta = _request(
            "GET",
            f"{prefix}/scripts/{quoted}/revisions/{int(revision)}",
            headers=headers, timeout=10.0)
        if not isinstance(meta, dict) or "state" not in meta:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "S04 returned revision metadata without state; S05 will not "
                "treat an unverifiable revision as SEALED")

        lines: list[dict] = []
        cursor = None
        for _page in range(MAX_LINE_PAGES):
            url = (f"{prefix}/scripts/{quoted}/revisions/{int(revision)}/lines"
                   f"?limit={LINE_PAGE_SIZE}")
            if cursor:
                url += "&after_line_id=" + urllib.parse.quote(str(cursor))
            page = _request("GET", url, headers=headers, timeout=30.0)
            batch = page.get("lines")
            if not isinstance(batch, list):
                raise ServiceError(
                    503, "SCRIPT_SOURCE_UNAVAILABLE",
                    "S04 returned a line page without a lines array")
            lines.extend(item for item in batch if isinstance(item, dict))
            cursor = page.get("next_after_line_id")
            if not cursor:
                break
        else:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                f"S04 line pagination did not terminate within "
                f"{MAX_LINE_PAGES} pages; refusing to break down a partial "
                f"line set")

        return {
            "script_id": meta.get("script_id") or script_id,
            "revision": int(meta.get("revision") or revision),
            "state": meta.get("state"),
            "content_hash": meta.get("content_hash"),
            "line_set_hash": meta.get("line_set_hash"),
            "lines": lines,
        }
    return _script


# ---------------------------------------------------------------- S18 上下文包

def build_context_pack_source(base_url: str | None):
    """`(tenant_id, pack_id) -> {content_hash, stale, ...}`。

    未配置时**返回一个会 503 的闭包**，而不是 `None`：内核把 `None` 读成
    「没配 S18 → partial」（§7.1 允许降级）；这里 503 也是同一条降级路径，
    但保留了「配了却连不上」与「根本没配」在日志里的区别 —— 两者都降级，
    不许一个静默变 200 而另一个 500。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "CONTEXT_PACK_UNAVAILABLE",
                "S18_BASE_URL is not configured; the revision is recorded as "
                "context_coverage='partial' (plan §7.1, T05-33)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _pack(tenant_id: str, pack_id: str) -> dict:
        result = _request(
            "GET", f"{prefix}/context-packs/{urllib.parse.quote(str(pack_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        # 与 S19 的 adapter 同形：正文在 `pack` 下。也接受平铺形态，免得
        # 因为上游两个端点长得不一样就把一个有效 pack 判成空的。
        pack = result.get("pack") if isinstance(result, dict) else None
        if isinstance(pack, dict):
            return pack
        if isinstance(result, dict) and "content_hash" in result:
            return result
        raise ServiceError(
            503, "CONTEXT_PACK_UNAVAILABLE",
            "S18 returned no pack body; S05 will not record a context it "
            "cannot identify")
    return _pack


# ---------------------------------------------------------------- S19 提案

def build_agent_task_source(base_url: str | None):
    """`(tenant_id, task_id) -> {proposal: {...}}`。

    S05 只读 `result.proposal`。**不把任务状态当放行依据**：AGENTS.md「把审核
    已通过当成放行依据」是红线，同理「S19 说任务成功」不等于「这份分场可以
    落库」。落库与否由 S05 的五条确定性校验决定。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "S19_BASE_URL is not configured; S05 cannot fetch the proposal "
                "it was asked to persist (plan §7.5)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _result(tenant_id: str, task_id: str) -> dict:
        result = _request(
            "GET",
            f"{prefix}/agent-tasks/{urllib.parse.quote(str(task_id))}/result",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        if not isinstance(result, dict):
            raise ServiceError(
                503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                "S19 returned no result body")
        return result
    return _result
