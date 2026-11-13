"""S06 的上游，全部注入式 callback，不 import 任何服务的业务模块（AGENTS.md）。

五条上游（+ 一个可选策略源），形状都是**读上游代码核对过的**：

- **S05** `GET /breakdowns/{id}/revisions/{revision}` 给分场 revision
  （`state` / `content_hash` / `scenes[]`，每场带 `line_span`）。S06 的分场、
  场序、行跨度全部以它为准（§3.1 校验①）。
- **S04** `GET /scripts/{id}/revisions/{revision}` 给元数据，
  `GET .../lines` 给分页行列表。两个都要读：只读元数据拿不到台词正文，只读
  正文拿不到 `state` 与 `content_hash`。分页用 `next_after_line_id` 游标。
- **S10** `GET /binding-sets/{id}/revisions/{revision}` 给 BindingSet（只用于
  核对 pin 的 hash），`GET /assets/{id}` 给资产是否存在（§3.1 校验⑥）。
  两者都不是「当前绑定是什么」的权威——那是 S10 的 binding set，S06 不复制。
- **S18** `GET /context-packs/{pack_id}` 返回 `{pack: {...}}`，`pack.stale`
  为真时不给「差不多新」的降级。
- **S19** `GET /agent-tasks/{id}/result` 返回 `{proposal: {...}}`。S06 只读这一
  条结果，**不读任务状态**：「任务成功」不等于「这份分镜可以落库」。

未配置一律 503，没有「忘了配就给默认值」的路径（§7.5 / 本仓 S04 同一约定）。
例外是 S18 与 S10：内核按方案把「没配 S18」降级成 `context_coverage=partial`、
把「没配 S10」理解成「不核资产存在性」——但这两个降级都只发生在**内核**，
deps 不会替内核决定，它只把「连不上」如实抛成 503。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .service import ServiceError

# S04 的 `list_revision_lines` 单页上限（`narrative.schema.MAX_LIST_LIMIT`）。
LINE_PAGE_SIZE = 200
# 防御性上限：分页死循环时及时停，不把一个残缺行集合当成整版剧本。
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


def _unconfigured(code: str, message: str):
    def _raise(*_args, **_kwargs):
        raise ServiceError(503, code, message)
    return _raise


# ---------------------------------------------------------------- S05 分场

def build_breakdown_source(base_url: str | None):
    """`(tenant_id, breakdown_id, revision) -> {state, content_hash, scenes[]}`。

    上游 404 原样上抛，内核转成 404（E28：跨租户与不存在同码）。
    """
    if not base_url:
        return _unconfigured(
            "BREAKDOWN_SOURCE_UNAVAILABLE",
            "S05_BASE_URL is not configured; S06 will not storyboard a scene "
            "list it cannot read (plan §7.5)")
    prefix = base_url.rstrip("/")

    def _breakdown(tenant_id: str, breakdown_id: str, revision: int) -> dict:
        quoted = urllib.parse.quote(str(breakdown_id))
        doc = _request(
            "GET", f"{prefix}/breakdowns/{quoted}/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        if not isinstance(doc, dict) or "scenes" not in doc:
            raise ServiceError(
                503, "BREAKDOWN_SOURCE_UNAVAILABLE",
                "S05 returned a revision without scenes; S06 will not "
                "storyboard an unverifiable breakdown")
        return doc
    return _breakdown


# ---------------------------------------------------------------- S04 剧本

def build_script_source(base_url: str | None):
    """`(tenant_id, script_id, revision) -> {state, content_hash, lines[]}`。

    行列表**整版**返回：防漂移与行覆盖都要以「S04 实际给出的行集合」为分母，
    只取第一页会让漏行校验把没读到的行全判成缺失。
    """
    if not base_url:
        return _unconfigured(
            "SCRIPT_SOURCE_UNAVAILABLE",
            "S04_BASE_URL is not configured; S06 will not accept dialogue it "
            "cannot verify (plan §7.5)")
    prefix = base_url.rstrip("/")

    def _script(tenant_id: str, script_id: str, revision: int) -> dict:
        quoted = urllib.parse.quote(str(script_id))
        headers = {"X-Tenant-Id": tenant_id}
        meta = _request(
            "GET", f"{prefix}/scripts/{quoted}/revisions/{int(revision)}",
            headers=headers, timeout=10.0)
        if not isinstance(meta, dict) or "state" not in meta:
            raise ServiceError(
                503, "SCRIPT_SOURCE_UNAVAILABLE",
                "S04 returned revision metadata without state; S06 will not "
                "treat an unverifiable revision as authoritative")

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
                f"{MAX_LINE_PAGES} pages; refusing to verify a partial line set")

        return {
            "script_id": meta.get("script_id") or script_id,
            "revision": int(meta.get("revision") or revision),
            "state": meta.get("state"),
            "content_hash": meta.get("content_hash"),
            "line_set_hash": meta.get("line_set_hash"),
            "lines": lines,
        }
    return _script


# ---------------------------------------------------------------- S10 资产

def build_binding_set_source(base_url: str | None):
    """`(tenant_id, binding_set_id, revision) -> {...}`。

    S06 只用它核对调用方 pin 的 hash，**不从 BindingSet 推导
    `ShotSpec.asset_bindings`**——方案没给这个映射（S06-G02），编一份等于把
    S10 的绑定语义复制到 S06，然后两份各走各的。
    """
    if not base_url:
        return _unconfigured(
            "BINDING_SET_SOURCE_UNAVAILABLE",
            "S10_BASE_URL is not configured; S06 cannot verify the pinned "
            "binding set (plan §7.5)")
    prefix = base_url.rstrip("/")

    def _binding_set(tenant_id: str, binding_set_id: str, revision: int) -> dict:
        quoted = urllib.parse.quote(str(binding_set_id))
        doc = _request(
            "GET",
            f"{prefix}/binding-sets/{quoted}/revisions/{int(revision)}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        if not isinstance(doc, dict):
            raise ServiceError(
                503, "BINDING_SET_SOURCE_UNAVAILABLE",
                "S10 returned no binding set body")
        return doc
    return _binding_set


def build_asset_source(base_url: str | None):
    """`(asset_kind, asset_id, revision) -> bool`。

    S06 只需要「这个 revision 存在吗」。**不读资产内容、不读当前版本、不缓存**
    ——资产的内容与生命周期是 S10 的权威（§3.1 校验⑥）。

    `revision` 在这里只做参数完整性检查：S10 的 `GET /assets/{id}` 不暴露
    revision 列表（资产版本由 owner 服务管），所以能证明的是「资产存在且未被
    tombstone」，不是「第 n 版存在」。这条差异登记在 S06-G14。
    """
    if not base_url:
        return _unconfigured(
            "ASSET_SOURCE_UNAVAILABLE",
            "S10_BASE_URL is not configured; S06 cannot verify asset bindings")
    prefix = base_url.rstrip("/")

    def _asset(asset_kind: str, asset_id: str, revision: int) -> bool:
        quoted = urllib.parse.quote(str(asset_id))
        doc = _request(
            "GET", f"{prefix}/assets/{quoted}",
            headers={"X-Tenant-Id": ""}, timeout=15.0)
        if not isinstance(doc, dict):
            raise ServiceError(503, "ASSET_SOURCE_UNAVAILABLE",
                               "S10 returned no asset body")
        # tombstone 的资产视同不存在：已经删掉的东西不能被新分镜引用。
        return doc.get("status") not in ("tombstoned", "deleted")
    return _asset


# ---------------------------------------------------------------- S18 上下文包

def build_context_pack_source(base_url: str | None):
    """`(tenant_id, pack_id) -> {content_hash, stale, ...}`。

    未配置时返回一个会 503 的闭包，而不是 `None`：内核把 503 降级成
    `partial`，但保留了「配了却连不上」与「根本没配」在日志里的区别。
    """
    if not base_url:
        return _unconfigured(
            "CONTEXT_PACK_SOURCE_UNAVAILABLE",
            "S18_BASE_URL is not configured; the revision is recorded as "
            "context_coverage='partial' (plan §7.1)")
    prefix = base_url.rstrip("/")

    def _pack(tenant_id: str, pack_id: str) -> dict:
        result = _request(
            "GET", f"{prefix}/context-packs/{urllib.parse.quote(str(pack_id))}",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        pack = result.get("pack") if isinstance(result, dict) else None
        if isinstance(pack, dict):
            return pack
        if isinstance(result, dict) and "content_hash" in result:
            return result
        raise ServiceError(
            503, "CONTEXT_PACK_SOURCE_UNAVAILABLE",
            "S18 returned no pack body; S06 will not record a context it "
            "cannot identify")
    return _pack


# ---------------------------------------------------------------- S19 提案

def build_agent_task_source(base_url: str | None):
    """`(tenant_id, task_id) -> {proposal: {...}}`。

    S06 只读 `result.proposal`。**不把任务状态当放行依据**：AGENTS.md 的
    「审核已通过不等于放行」同理——「S19 说任务成功」不等于「这份分镜可以
    落库」。落库与否由 S06 的确定性校验决定。
    """
    if not base_url:
        return _unconfigured(
            "AGENT_TASK_SOURCE_UNAVAILABLE",
            "S19_BASE_URL is not configured; S06 cannot fetch the proposal it "
            "was asked to persist (plan §7.5)")
    prefix = base_url.rstrip("/")

    def _result(tenant_id: str, task_id: str) -> dict:
        result = _request(
            "GET",
            f"{prefix}/agent-tasks/{urllib.parse.quote(str(task_id))}/result",
            headers={"X-Tenant-Id": tenant_id}, timeout=15.0)
        if not isinstance(result, dict):
            raise ServiceError(503, "AGENT_TASK_SOURCE_UNAVAILABLE",
                               "S19 returned no result body")
        return result
    return _result


# ---------------------------------------------------------------- S02 策略

def build_policy_source(base_url: str | None):
    """`(tenant_id, project_id) -> {...}`。**只读，且当前不参与任何判定**。

    S06 现在不需要策略：分镜的可行性是确定性的（§3.5），没有「按策略放宽」
    的路径。留这个接线是为了让「某天要策略」的时候不用改内核签名——但它
    现在**不执行 anything**，也不在响应里写「已按策略校验」。
    """
    if not base_url:
        return None
    prefix = base_url.rstrip("/")

    def _effective(tenant_id: str, project_id: str) -> dict:
        return _request(
            "GET",
            f"{prefix}/projects/{urllib.parse.quote(str(project_id))}"
            f"/effective-policy",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _effective
