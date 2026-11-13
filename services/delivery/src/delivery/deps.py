"""S43 的上游，全部是注入式 callback，不是 import（AGENTS.md 禁止跨库）。

每条是 `build_*(base_url)`，未配置时返回一个**必然抛 503** 的桩：
`ServiceError(503, "<CODE>_UNAVAILABLE", ...)`。没有"默认值兜底"，也没有
"空结果假装查过"——交付包的每一道门都必须有真实回音才能通过（§2.3：
「任何一项无法判定（依赖 503）→ 整体失败，**不是门通过**」）。

已实现的上游：

- **S08** `services/revision-snapshot`：`GET /manifests/{id}`（取
  `execution_fence` / `accepted_revision`）+ `GET /manifests/{id}/
  accepted-pointers`（取 node 级接受指针）。两次调用合成一份回答，因为
  G1 要同时看「接受的是不是这个 artifact」和「fence 有没有推进」。
- **S28** `services/artifact-ingest`：`GET /artifacts/{id}` 或
  `/artifacts/{id}/revisions/{revision}`。只读元数据，**不下载字节**。
- **S41** `services/editorial`：`GET /timelines/{id}/revisions/{r}/
  eligibility`（`formal_master_eligible`）。
- **S35 / S36 / S37**：技术 / 语义 / 音频报告，按 ref 里的 `service_id`
  选路。`decision` 与 `defects` 归一成同一种形状，但**不重算**结论。
- **S15** `services/voice-assets`：`POST /voices/{id}:check-eligibility`。
- **S16** `services/story-memory`：`POST /series/{id}/memory/query`。
- **S09** `services/asset-catalog`：`GET /binding-sets/{id}/revisions/{r}`。
- **S23** `services/budget-ledger`：`GET /episodes/{id}/cost`（按币种分开，
  从不跨币种求和）。
- **S45** `services/audit`：`POST /audit-records`。

未实现（design_only 或接口不存在）的上游，同样以 `build_*` 出现，未配置即
503；测试注入桩：

- **S02**：没有 `:evaluate`，也没有按 decision_id 取判定的接口（S43-2）。
- **S42**：`services/composition` 是 design_only，没有读取候选的路径。
- **S38**：`services/subtitles` 是 design_only。
- **签名**：KMS/HSM 不在 48 服务表里（§11 C6）。
- **渠道连接器**：`connectors/*/*` 是部署件，不是本仓的 Python 包。
- **回执验签**：渠道侧的签名算法由渠道决定，本服务不猜。

`manual_handoff` 渠道**不走连接器**：它是人工拷贝，本服务只落 PENDING 与
回执，不假装"已送达"。真实渠道外发需要实施会话授权（AGENTS.md）。
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
        raise ServiceError(503, code,
                           f"{var} is not configured; {why}")
    # 与 `service._missing_source` 同一约定：桩自报身份，`/health/readiness`
    # 才能如实说"哪道门现在判不了"，而不是去猜哪个 env 没配。
    _stub._unavailable_code = code
    return _stub


# ------------------------------------------------------------- S08 accepted

def build_accepted_master_source(base_url: str | None):
    """`(tenant_id, manifest_id) -> {"manifest", "pointers"}`。

    `manifest` 来自 `GET /manifests/{id}`，`pointers` 来自
    `/manifests/{id}/accepted-pointers`。S08 没有「按候选查接受状态」的接口
    （S43-3），所以 G1 只能拿 node 级接受指针比对。
    """
    if not base_url:
        return _unconfigured_env(
            "S08_BASE_URL", "ACCEPTED_MASTER_SOURCE_UNAVAILABLE",
            "S43 will not claim a master is accepted without asking S08 "
            "(plan §2.3 G1)")

    prefix = base_url.rstrip("/")

    def _accepted(tenant_id: str, manifest_id: str) -> dict:
        quoted = urllib.parse.quote(str(manifest_id))
        headers = {"X-Tenant-Id": tenant_id}
        manifest = _request("GET", f"{prefix}/manifests/{quoted}",
                            headers=headers, timeout=10.0)
        pointers = _request("GET",
                            f"{prefix}/manifests/{quoted}/accepted-pointers",
                            headers=headers, timeout=10.0)
        return {"manifest": manifest, "pointers": pointers}
    return _accepted


# ------------------------------------------------------------ S28 artifacts

def build_artifact_source(base_url: str | None):
    """`(tenant_id, ref) -> S28 artifact 元数据`。

    `ref` 形如 `{"id", "revision"?, "content_hash"?}`。404 / 410 原样抛出，
    由 G2 折成 `ARTIFACT_MISSING`；网络失败抛 `ConnectionError`，由命令折成
    503。**永不返回"看起来存在"的默认值。**
    """
    if not base_url:
        return _unconfigured_env(
            "S28_BASE_URL", "ARTIFACT_SOURCE_UNAVAILABLE",
            "S43 will not seal a package whose artifacts it cannot verify "
            "(plan §2.3 G2)")

    prefix = base_url.rstrip("/")

    def _artifact(tenant_id: str, ref: dict) -> dict:
        artifact_id = urllib.parse.quote(str(ref["id"]))
        revision = ref.get("revision")
        if revision is None:
            url = f"{prefix}/artifacts/{artifact_id}"
        else:
            url = f"{prefix}/artifacts/{artifact_id}/revisions/{int(revision)}"
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)
    return _artifact


# ------------------------------------------------------------- S41 timeline

def build_timeline_source(base_url: str | None):
    """`(tenant_id, timeline_ref) -> {eligible, formal_master_eligible, ...}`。"""
    if not base_url:
        return _unconfigured_env(
            "S41_BASE_URL", "TIMELINE_SOURCE_UNAVAILABLE",
            "S43 will not claim a timeline is complete without asking S41 "
            "(plan §2.3 G3)")

    prefix = base_url.rstrip("/")

    def _timeline(tenant_id: str, ref: dict) -> dict:
        timeline_id = urllib.parse.quote(str(ref["id"]))
        url = (f"{prefix}/timelines/{timeline_id}/revisions/"
               f"{int(ref['revision'])}/eligibility")
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)
    return _timeline


# ---------------------------------------------------- S42 candidate evidence

def build_candidate_source(base_url: str | None):
    """`(tenant_id, candidate_ref) -> 候选与渲染证据`。

    S42 `services/composition` 目前是 design_only，没有读面（S43-4）。协议
    （`GET {prefix}/master-candidates/{id}`）是本实现**自己定的**，待 S42 落地
    后对齐；在那之前生产环境只能是 503。
    """
    if not base_url:
        return _unconfigured_env(
            "S42_BASE_URL", "CANDIDATE_SOURCE_UNAVAILABLE",
            "S42 is design_only; S43 cannot verify candidate deliverability "
            "(plan §2.3 G3/G5, S43-4)")

    prefix = base_url.rstrip("/")

    def _candidate(tenant_id: str, ref: dict) -> dict:
        candidate_id = urllib.parse.quote(str(ref["id"]))
        return _request("GET", f"{prefix}/master-candidates/{candidate_id}",
                        headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _candidate


# ---------------------------------------------------------------- QC reports

_QC_PATH = {
    "S35": "/technical-reports/{id}",
    "S36": "/semantic-reports/{id}",
    "S37": "/alignment-reports/{id}",
}


def build_qc_source(base_urls: dict | None):
    """`(tenant_id, ref) -> {decision, blocking_count, defects}`。

    `base_urls` 是 `{"S35": url, "S36": url, "S37": url}`；缺哪个，那个服务的
    报告就取不到。全部缺失时返回 503 桩。**结论不在这里重算**：`decision`
    原样搬运，`blocking_count` 只是把 `defects` 里 `blocking=true` 或
    `severity="blocker"` 的条目数出来。
    """
    base_urls = {k: v for k, v in (base_urls or {}).items() if v}
    if not base_urls:
        def _stub(*_args, **_kwargs):
            raise ServiceError(
                503, "QC_SOURCE_UNAVAILABLE",
                "no QC report source is configured; S43 will not treat "
                "missing QC evidence as a pass (plan §2.3 G4)")
        return _stub

    prefixes = {k: v.rstrip("/") for k, v in base_urls.items()}

    def _qc(tenant_id: str, ref: dict) -> dict:
        service_id = str(ref.get("service_id") or "")
        if service_id not in prefixes:
            raise ServiceError(
                503, "QC_SOURCE_UNAVAILABLE",
                f"no base URL for QC service {service_id!r}")
        template = _QC_PATH.get(service_id)
        if template is None:
            raise ServiceError(422, "UNSUPPORTED_QC_SERVICE",
                               f"{service_id} does not publish QC reports")
        url = prefixes[service_id] + template.format(
            id=urllib.parse.quote(str(ref["report_id"])))
        query = {}
        if ref.get("generation") is not None:
            query["generation"] = ref["generation"]
        if query:
            url += "?" + urllib.parse.urlencode(query)
        result = _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                          timeout=10.0)
        payload = result.get("payload") if isinstance(result.get("payload"),
                                                      dict) else {}
        defects = payload.get("defects") or result.get("defects") or []
        blocking = 0
        for defect in defects:
            if not isinstance(defect, dict):
                continue
            if defect.get("blocking") is True or defect.get("severity") == "blocker":
                blocking += 1
        return {"service_id": service_id,
                "report_id": ref["report_id"],
                "decision": result.get("decision") or payload.get("decision"),
                "blocking_count": blocking,
                "defect_count": len(defects),
                "payload_ref": {"service_id": service_id,
                                "report_id": ref["report_id"],
                                "generation": ref.get("generation")}}
    return _qc


# ------------------------------------------------------------- S38 subtitles

def build_subtitle_source(base_url: str | None):
    """`(tenant_id, query) -> {artifact_ref, timeline_revision, ...}`。"""
    if not base_url:
        return _unconfigured_env(
            "S38_BASE_URL", "SUBTITLE_SOURCE_UNAVAILABLE",
            "S38 is design_only; S43 cannot verify subtitle freshness "
            "(plan §2.3 G6, S43-4)")

    prefix = base_url.rstrip("/")

    def _subtitle(tenant_id: str, query: dict) -> dict:
        params = {k: v for k, v in query.items() if v is not None}
        url = f"{prefix}/subtitle-tracks"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request("GET", url, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)
    return _subtitle


# ----------------------------------------------------------------- S15 rights

def build_rights_source(base_url: str | None):
    """`(tenant_id, query) -> {allowed, denied[]}`。

    对每个 `voice_refs` 里的条目调 `POST /voices/{id}:check-eligibility`。
    每次外发都**实时**问，不读本服务的 `voice_authorization_projection`
    （投影只能用来发现撤回，不能用来放行）。
    """
    if not base_url:
        return _unconfigured_env(
            "S15_BASE_URL", "RIGHTS_SOURCE_UNAVAILABLE",
            "S43 will not publish without a live rights check "
            "(plan §2.3 G7)")

    prefix = base_url.rstrip("/")

    def _rights(tenant_id: str, query: dict) -> dict:
        denied = []
        for voice in query.get("voice_refs") or []:
            voice_id = urllib.parse.quote(str(voice["id"]))
            body = {"voice_revision": voice.get("revision"),
                    "requested_use": "external_publish"}
            if voice.get("if_state_seq") is not None:
                body["if_state_seq"] = voice["if_state_seq"]
            result = _request("POST", f"{prefix}/voices/{voice_id}"
                              f":check-eligibility", body=body,
                              headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
            if not result.get("eligible"):
                denied.append({"ref": {"service_id": "S15",
                                       "id": voice["id"]},
                               "reason_code": result.get("deny_reason")
                               or "RIGHTS_NOT_ELIGIBLE",
                               "state": result.get("authorization_state")})
        return {"allowed": not denied, "denied": denied}
    return _rights


# ---------------------------------------------------------------- S02 policy

def build_policy_source(base_url: str | None):
    """`(tenant_id, request) -> 判定`。

    `request.op`：

    - `"lookup"`：按 `decision_id` 取判定（E27-S43-02 伪造 id 的落点）。
    - `"evaluate"`：实时评估 `external_publish`（E27-S43-01/04/05）。

    S02 目前两者都没有（S43-2）。路径按方案 §6.3 记为
    `{prefix}/policy-decisions:lookup` 与 `:evaluate`，等 S02 落地后对齐。
    """
    if not base_url:
        return _unconfigured_env(
            "S02_BASE_URL", "POLICY_SOURCE_UNAVAILABLE",
            "S02 is the only authority for publish authorization; S43 will "
            "not cache or invent an allow (plan §2.2)")

    prefix = base_url.rstrip("/")

    def _policy(tenant_id: str, request: dict) -> dict:
        op = request.get("op")
        if op == "lookup":
            decision_id = urllib.parse.quote(str(request["decision_id"]))
            return _request("GET",
                            f"{prefix}/policy-decisions/{decision_id}",
                            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        if op == "evaluate":
            return _request("POST", f"{prefix}/policy-decisions:evaluate",
                            body={k: v for k, v in request.items()
                                  if k != "op"},
                            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        raise ServiceError(422, "UNSUPPORTED_POLICY_OP",
                           f"unknown policy op {op!r}")
    return _policy


# ------------------------------------------------------------------ S16 memory

def build_memory_source(base_url: str | None):
    """`(tenant_id, query) -> memory checkpoint`（S16 的 query 是 POST 纯读）。"""
    if not base_url:
        return _unconfigured_env(
            "S16_BASE_URL", "MEMORY_SOURCE_UNAVAILABLE",
            "the formal package requires a memory checkpoint (plan §2.4)")

    prefix = base_url.rstrip("/")

    def _memory(tenant_id: str, query: dict) -> dict:
        series_id = urllib.parse.quote(str(query["series_id"]))
        body = {"branch_id": query.get("branch_id"),
                "minimum_checkpoint": query.get("minimum_checkpoint", 0),
                "audience_as_of_episode_order": None}
        return _request("POST", f"{prefix}/series/{series_id}/memory/query",
                        body=body, headers={"X-Tenant-Id": tenant_id},
                        timeout=10.0)
    return _memory


# ------------------------------------------------------------ S09 bindings

def build_binding_source(base_url: str | None):
    """`(tenant_id, query) -> 资产绑定版本`。"""
    if not base_url:
        return _unconfigured_env(
            "S09_BASE_URL", "BINDING_SOURCE_UNAVAILABLE",
            "the formal package requires asset bindings (plan §2.4)")

    prefix = base_url.rstrip("/")

    def _bindings(tenant_id: str, query: dict) -> dict:
        binding_set_id = urllib.parse.quote(str(query["binding_set_id"]))
        revision = int(query["revision"])
        return _request(
            "GET", f"{prefix}/binding-sets/{binding_set_id}/revisions/{revision}",
            headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _bindings


# ----------------------------------------------------------------- S23 cost

def build_cost_source(base_url: str | None):
    """`(tenant_id, episode_id) -> 截至此刻的成本快照`（不是实时余额）。"""
    if not base_url:
        return _unconfigured_env(
            "S23_BASE_URL", "COST_SOURCE_UNAVAILABLE",
            "the formal package requires a cost point-in-time (plan §2.4)")

    prefix = base_url.rstrip("/")

    def _cost(tenant_id: str, episode_id: str) -> dict:
        episode = urllib.parse.quote(str(episode_id))
        return _request("GET", f"{prefix}/episodes/{episode}/cost",
                        headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
    return _cost


# ------------------------------------------------------------------- S45 audit

def build_audit_sink(base_url: str | None):
    """`(tenant_id, intent) -> {record_id}`。

    **不降级、不吞异常**：S43 的审计意图与 `agent-runtime` 的补投台账不同，
    它要能回答 E39「这次交付发了什么」。意图本身先落本库（`audit_intent`），
    外发失败会把 `delivered=0` 如实留在库里并出现在 readiness / evidence 里，
    但不会让一次已经成功的封包回滚。
    """
    if not base_url:
        return _unconfigured_env(
            "S45_BASE_URL", "AUDIT_SINK_UNAVAILABLE",
            "S43 will not claim an audit record exists when it could not "
            "reach S45")

    prefix = base_url.rstrip("/")

    def _sink(tenant_id: str, intent: dict) -> dict:
        return _request("POST", f"{prefix}/audit-records", body=intent,
                        headers={"X-Tenant-Id": tenant_id,
                                 "X-Producer-Id": "S43"}, timeout=10.0)
    return _sink


# ----------------------------------------------------------------- signing

def build_signer(base_url: str | None):
    """`(tenant_id, manifest_hash, key_ref) -> 签名`。

    `key_ref` 里带 `tenant_id`，跨租户绝不共用密钥（E28-S43-03）。响应必须含
    `signature_b64`、`algorithm`、`key_ref` 与 `verified`；`verified=false` 的
    签名不阻止封包，但会让 G9 失败、`deliverable=false`（§2.1）。
    """
    if not base_url:
        return _unconfigured_env(
            "KMS_BASE_URL", "SIGNING_UNAVAILABLE",
            "signing requires an external KMS/HSM (plan §2.1, §11 C6)")

    prefix = base_url.rstrip("/")

    def _sign(tenant_id: str, manifest_hash: str, key_ref: dict) -> dict:
        return _request("POST", f"{prefix}/sign", body={
            "tenant_id": tenant_id,
            "manifest_hash": manifest_hash,
            "key_ref": key_ref,
            "algorithm": key_ref.get("algorithm") or "ed25519",
        }, headers={"X-Tenant-Id": tenant_id}, timeout=30.0)
    return _sign


# -------------------------------------------------------------- connector

def build_connector(base_url: str | None):
    """`(tenant_id, request) -> 外发结果`。

    S43 **不持有**渠道凭据、不直连第三方（§4.3 第 5 步）。请求体：
    `{channel_id, kind, endpoint_ref, package_id, attempt_id, manifest_hash,
    items[{role, artifact_ref, byte_size}], total_bytes}`；响应：
    `{outcome: succeeded|failed|unknown, remote_id, bytes_sent,
    failure_code, failure_detail, retryable, receipt}`。
    """
    if not base_url:
        return _unconfigured_env(
            "CONNECTOR_BASE_URL", "CONNECTOR_UNAVAILABLE",
            "external delivery must go through a channel connector; S43 has "
            "no direct outbound path (plan §4.3)")

    prefix = base_url.rstrip("/")

    def _send(tenant_id: str, request: dict) -> dict:
        return _request("POST", f"{prefix}/deliveries", body=request,
                        headers={"X-Tenant-Id": tenant_id}, timeout=300.0)
    return _send


def build_receipt_verifier(base_url: str | None):
    """`(tenant_id, package_id, attempt_id, receipt) -> bool`。

    验不了就是 `False`：**收不到可信回执就不算送达**（§4.6）。未配置时抛
    503，由 `ingest_receipt` 折成拒绝并记审计（E17-S43-03）。
    """
    if not base_url:
        return _unconfigured_env(
            "RECEIPT_VERIFIER_BASE_URL", "RECEIPT_VERIFIER_UNAVAILABLE",
            "receipt signatures can only be verified by the channel's own "
            "verifier; S43 will not accept an unverified receipt")

    prefix = base_url.rstrip("/")

    def _verify(tenant_id: str, package_id: str, attempt_id: str,
                receipt: dict) -> bool:
        result = _request("POST", f"{prefix}/receipts:verify", body={
            "tenant_id": tenant_id,
            "package_id": package_id,
            "attempt_id": attempt_id,
            "receipt": receipt,
        }, headers={"X-Tenant-Id": tenant_id}, timeout=10.0)
        return bool(result.get("valid"))
    return _verify
