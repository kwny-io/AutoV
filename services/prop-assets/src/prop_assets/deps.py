"""S14 的上游。全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

按方案 `plans/S14-prop-assets.md` 的开头，S14 的 `depends_on` 只有 **S28** 和
**S01**。这两个都是**必须**的，外加两个**可选**的：

- **S28 artifact-ingest（必需）**：`GET /artifacts/{artifact_id}` 是 §4.1 第 1 条
  的唯一实现——artifact 存在、且 `content_hash` 与候选声明一致。读的是已实现
  的服务（`services/artifact-ingest`），返回体里有 `content_hash`（当前 revision
  的）与 `state`，见 `artifact_ingest.service.get_artifact`。**没有它，一个
  `artifact_id` 只是一个字符串**，可能是别的租户的、可能根本没落盘。
- **S01 tenant-access（必需，但只用于敏感文字读取）**：`GET
  /authorization-context?scope_kind=tenant&scope_id=...&principal_id=...` 返回
  `{"permissions": [...], "denied": [...], ...}`。只有平台没给签名作用域
  （`X-Principal-Scopes`）时才走这一跳——签名作用域是主路径（§4.3），
  S01 是它的兜底，不是替代。
- **S35/S36 QC（可选）**：仓库里 S35 是 technical-qc、S36 是 semantic-qc，
  但**没有任何一个产出 `holder_feasibility` / `variant_consistency` /
  `text_ocr_pass`**（C14-10 / PRP-6）。所以这一跳默认不配置：结论只能随
  命令内联传入，读不到就记 `null`，绝不用 `true` 兜底。配置了也只读不判。
- **S02 权利（不接）**：`rights_ref` 是指针，S14 只存不判（§2.2、§1.4）。
  接一个"看起来能判权利"的上游会让 S14 变成第二个权利权威，
  正是 AGENTS.md 禁止的那种。

**未配置一律必然失败**（S28 / S01）：这两条 503 分支就是 §5.4 的
`DEPENDENCY_UNAVAILABLE`。唯一的例外是 QC —— 它本来就没被契约定义。
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
        # 404 在这里是最要紧的一种：**S28 说没有**，不是"上游挂了"。
        # 原样上抛成 404，由 service 翻译成 422 ARTIFACT_NOT_FOUND。
        raise ServiceError(exc.code, payload.get("code") or "UPSTREAM_ERROR",
                           payload.get("message") or str(exc),
                           retryable=bool(payload.get("retryable")),
                           details=payload.get("details") or {})
    except (urllib.error.URLError, OSError) as exc:
        raise ConnectionError(f"upstream unreachable: {exc}") from exc


# ---------------------------------------------------------------- S28 资产

def build_artifact_source(base_url: str | None):
    """`(tenant_id, artifact_id) -> artifact 摘要`，`None` 表示"这个租户看不到"。

    §4.1 第 1 条只要求"存在且 hash 一致"。**不做本地缓存**：artifact 可能被
    tombstone（S28 的 `:tombstone`），缓存过的校验等于拿旧事实给新候选背书。

    404 与"上游不可达"必须分开：
    - S28 明确 404 → 返回 `None`，service 报 422 `ARTIFACT_NOT_FOUND`
      （这是调用方的错，不该重试）；
    - 超时/连接失败 → `ConnectionError`，service 报 503 且**不落候选**
      （F13：拿不到结论就存下来，等于把伪造的 id 写进不可变 revision）。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "ARTIFACT_SOURCE_UNAVAILABLE",
                "S28_BASE_URL is not configured; S14 will not accept a "
                "candidate whose artifact it cannot verify, because an "
                "unverified artifact_id may belong to another tenant "
                "(plan §4.1 step 1, §9)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _get(tenant_id: str, artifact_id: str) -> dict | None:
        try:
            return _request(
                "GET", f"{prefix}/artifacts/"
                       f"{urllib.parse.quote(str(artifact_id))}",
                headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        except ServiceError as exc:
            if exc.status == 404:
                # 不存在**和**"存在但你无权"在 S28 是同形，这里照收 ——
                # 区分它们就是泄漏别的租户有什么。
                return None
            raise

    return _get


# ---------------------------------------------------------------- S01 授权

def build_authorizer(base_url: str | None):
    """`(tenant_id, principal_id) -> {"permissions": [...], "denied": [...]}`。

    只用于敏感文字的读取作用域（C14-6）。**不缓存**：S01 的上下文是
    `Cache-Control: no-store`、TTL 5 秒，缓存一次就让离职者多读五分钟的明文。

    `denied` 必须一起返回并参与判断：S01 的语义是 deny 压过 allow，
    只取 `permissions` 会让一条显式 deny 消失。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "AUTHORIZER_UNAVAILABLE",
                "S01_BASE_URL is not configured; S14 will not guess whether a "
                "caller may read sensitive text — the signed "
                "X-Principal-Scopes header is the only other authority "
                "(plan §4.3, C14-6)")
        return _unconfigured

    prefix = base_url.rstrip("/")

    def _context(tenant_id: str, principal_id: str) -> dict:
        # S01 要求一个 scope。敏感文字是道具级的事实，而道具挂在项目下；
        # 这里按 tenant 级取权限：读明文的 scope 是租户内的运维能力，
        # 不是"某个项目里的普通成员"就该有的。
        query = urllib.parse.urlencode({
            "scope_kind": "tenant",
            "scope_id": tenant_id,
            "principal_id": str(principal_id),
            "purpose": "prop.text.sensitive_read",
        })
        result = _request(
            "GET", f"{prefix}/authorization-context?{query}",
            headers={"X-Tenant-Id": tenant_id,
                     "X-Principal-Id": str(principal_id)},
            timeout=10.0)
        return {"permissions": list(result.get("permissions") or []),
                "denied": list(result.get("denied") or [])}

    return _context


# ---------------------------------------------------------------- QC（可选）

def build_quality_gate_source(base_url: str | None):
    """`(tenant_id, quality_gate_ref) -> 结论文档`。

    **默认不配置**，而且配置了也只是"读结论"。仓库里没有产出这三个结论的
    服务（C14-10 / PRP-6）：
    - S35 = technical-qc（画面/音频技术指标）
    - S36 = semantic-qc（叙事/角色一致性）
    - S33 = 音效、S34 = 配乐（方案 §4.2 曾把 QC 写成 S33/S34，那是错的）

    `quality_gate_ref` 的形状契约里没定义，所以在能读到之前，结论只能随
    命令内联传入（`body.quality_gate`）。这个函数存在的意义是：**一旦**
    owner 决定了 QC 服务，接线只改一个环境变量，不用改内核。
    """
    if not base_url:
        def _unconfigured(*_args, **_kwargs):
            raise ServiceError(
                503, "QUALITY_GATE_UNAVAILABLE",
                "QUALITY_GATE_BASE_URL is not configured; pass the conclusion "
                "inline as body.quality_gate instead (C14-10)")

        return _unconfigured

    prefix = base_url.rstrip("/")

    def _fetch(tenant_id: str, ref) -> dict:
        gate_id = ref.get("id") if isinstance(ref, dict) else ref
        if not gate_id:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "quality_gate_ref needs an id")
        revision = (ref.get("revision") if isinstance(ref, dict) else None)
        path = f"{prefix}/quality-gates/{urllib.parse.quote(str(gate_id))}"
        if revision is not None:
            path += f"/revisions/{int(revision)}"
        return _request("GET", path, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)

    return _fetch
