"""S28 业务逻辑：制品身份、不可变版本、租户内去重、导入受理、访问票据、tombstone。

本服务是「媒体字节与 hash」的唯一权威（docs/01 权威表）。它**不**写业务身份、
接受状态、任务状态、金额或审计结论，只以 `service_id/id/revision/content_hash`
的形式对外引用，从不复制内容。

本轮实现的是「受理 + 发布 + 访问 + tombstone」这一段。**没有**实现的部分（都在
plans/S28-artifact-ingest.md 里有明确位置）：
  - §4.2 的 DNS 解析与 IP pinning、实际 HTTP 取字节（需要真实网络与 allowlist 配置）
  - §4.2/§8.2 的媒体沙箱探测（需要无网沙箱运行时）
  - §4.8 的删除编排（deletion_job / 引用检查 / BYTES_DELETE）
  - §4.6 的派生（proxy / 缩略图转码）
方案 §11.1 把安全链排在发布之前，这里刻意先做发布：取字节、沙箱、对象存储都要
真实基础设施，本地造一套 mock 只能得到「绿但什么都没证明」的测试。这一偏离与
未实现项已登记在 §12（C28-12）。

`publish` 是唯一的发布点（方案 §4.1 第 12 步）。它把 blob、revision、artifact、
outbox、import 终态放在**同一个显式事务**里提交。这里必须显式 `BEGIN`：连接以
`isolation_level=None` 打开时 `with conn:` 并不开启事务，`sqlite3` 的上下文管理器
只在 `isolation_level` 不是 None 时才有提交/回滚语义。
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from event_sdk import Outbox, new_event

from .schema import (
    CONTENT_CLASSES,
    DERIVATIVE_KINDS,
    EVENT_TYPE,
    PURPOSES,
    SOURCE_KINDS,
)

SOURCE = "s28.artifact-ingest"

# 方案 §4.2 的 content_class 上限，单位字节。生产里来自配置，这里给默认值。
DEFAULT_SIZE_CAPS = {
    "generated_video": 8 * 1024 ** 3,
    "generated_image": 200 * 1024 ** 2,
    "generated_audio": 1 * 1024 ** 3,
    "rendered_master": 64 * 1024 ** 3,
    "uploaded_reference": 2 * 1024 ** 3,
}
DEFAULT_SIZE_CAP = 2 * 1024 ** 3

MAX_TICKET_TTL_SECONDS = 24 * 3600

TOMBSTONE_REASONS = (
    "USER_REQUEST", "RETENTION_EXPIRED", "LEGAL", "SECURITY", "ORPHAN",
)

# content_class → 默认派生提示（§4.6 的白名单）
DEFAULT_DERIVATIVE_HINTS = {
    "generated_video": ["proxy", "thumbnail"],
    "rendered_master": ["proxy", "thumbnail"],
    "uploaded_reference": ["thumbnail"],
    "generated_image": ["thumbnail"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _loads(text):
    return None if text is None else json.loads(text)


def _is_hash(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _is_non_negative_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """显式事务。

    `sqlite3.Connection.__enter__` 在 `isolation_level=None`（autocommit）下不开启
    事务，只返回连接本身——用它包住「同事务写事实与 outbox」会得到一组各自立即
    提交的语句。这里显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


class ImportAuthorizer:
    """S01 的替身（方案 §11.1 第 7 条：先用 mock `GET /authorization-context`）。

    真实实现必须换成对 S01 的调用，并且**不可达时 fail closed**（§4.10：
    503 `AUTH_DEPENDENCY_UNAVAILABLE`，不接收导入）。本类不做任何真实授权判断，
    绝不能被当成「授权已通过」的理由。
    """

    def __call__(self, tenant_id: str, action: str, scope_ref, owner_ref) -> None:
        return None


class LocalUrlSigner:
    """对象存储短签名的本地替身。

    产出 `local://` 开头的串，刻意不是 https：它不可能被误当成真实的预签名 URL，
    也不指向任何真实存储。生产部署必须注入真实签名器。
    """

    def sign(self, storage_key: str, expires_at: str) -> str:
        return f"local://{storage_key}?expires={expires_at}"


class ArtifactIngestService:
    def __init__(self, conn: sqlite3.Connection, *,
                 authorizer=None, signer=None, size_caps=None,
                 host_allowlist=None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.outbox = Outbox(conn)
        self.authorizer = authorizer or ImportAuthorizer()
        self.signer = signer or LocalUrlSigner()
        self.size_caps = dict(DEFAULT_SIZE_CAPS, **(size_caps or {}))
        # §4.2：allowlist 为空 = 全部拒绝（fail closed）
        self.host_allowlist = frozenset(host_allowlist or ())

    # ---- 内部 -----------------------------------------------------------------

    def _cap(self, content_class: str) -> int:
        return self.size_caps.get(content_class, DEFAULT_SIZE_CAP)

    def _artifact_row(self, tenant_id: str, artifact_id: str) -> sqlite3.Row:
        # 跨租户与不存在同码：不泄漏存在性（§8.5）
        row = self.conn.execute(
            "SELECT * FROM artifact WHERE tenant_id = ? AND artifact_id = ?",
            (tenant_id, artifact_id)).fetchone()
        if row is None:
            raise ServiceError(404, "ARTIFACT_NOT_FOUND", "artifact does not exist")
        return row

    def _import_row(self, tenant_id: str, import_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM artifact_import WHERE tenant_id = ? AND import_id = ?",
            (tenant_id, import_id)).fetchone()
        if row is None:
            raise ServiceError(404, "IMPORT_NOT_FOUND", "import does not exist")
        return row

    @staticmethod
    def _scope_covers(caller_scope, artifact_scope) -> bool:
        """调用方的 scope 是否覆盖已有 artifact 的 scope。

        §4.4 的原话是「两处 scope 相同才允许暴露」。这里按**包含**实现：调用方
        声明的字段必须逐一同值覆盖已有 artifact 声明的字段。理由是导入方通常声明
        比 artifact 更宽的范围（`{project}` 覆盖 `{project, series, episode}`），
        按字面「相等」会让最常见的同 scope 情形也被判成不可见。字面语义与这里的
        差别已登记在方案 §12，等 owner 裁决。
        """
        if not isinstance(caller_scope, dict) or not isinstance(artifact_scope, dict):
            return False
        return all(caller_scope.get(k) == v for k, v in artifact_scope.items())

    def _check_source_url(self, url: str) -> str:
        """未出网就挡掉的 URL 校验（§4.2 的离线部分），返回 host。

        只做不需要 DNS 的部分：scheme、host allowlist、IP 字面量。DNS 解析与
        IP pinning 必须在连接时做，不在本轮范围内。
        """
        if not isinstance(url, str) or not url:
            raise ServiceError(400, "INVALID_REQUEST", "provider_url is required")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ServiceError(422, "UNSAFE_MEDIA", "provider_url must use https")
        host = (parts.hostname or "").lower()
        if not host:
            raise ServiceError(400, "INVALID_REQUEST", "provider_url has no host")
        if _is_ip_literal(host):
            # 允许 IP 字面量等于让 allowlist 失去意义
            raise ServiceError(422, "UNSAFE_MEDIA",
                               "provider_url must not be an IP literal")
        if not self.host_allowlist:
            raise ServiceError(422, "UNSAFE_MEDIA",
                               "no host allowlist configured; refusing all hosts")
        if host not in self.host_allowlist:
            raise ServiceError(422, "UNSAFE_MEDIA",
                               "provider_url host is not allowlisted")
        return host

    # ---- 命令：受理导入（§6.1） ------------------------------------------------

    def create_import(self, tenant_id: str, body: dict) -> dict:
        """§6.1 的 8 步校验，然后落 ACCEPTED。导入异步，这里不发布。"""
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_REQUEST", "body must be an object")

        # 2. 租户一致性：租户上下文由平台签名绑定，body 里的只是声明
        declared_tenant = body.get("tenant_id")
        if declared_tenant is not None and declared_tenant != tenant_id:
            raise ServiceError(403, "FORBIDDEN_SCOPE",
                               "tenant_id does not match the authenticated tenant")

        # 3. 闭集
        content_class = body.get("content_class")
        if content_class not in CONTENT_CLASSES:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"content_class must be one of {list(CONTENT_CLASSES)}")
        source = body.get("source")
        if not isinstance(source, dict):
            raise ServiceError(400, "INVALID_REQUEST", "source is required")
        kind = source.get("kind")
        if kind not in SOURCE_KINDS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"source.kind must be one of {list(SOURCE_KINDS)}")

        # 4. expected_sha256 格式
        expected = body.get("expected_sha256")
        if expected is not None and not _is_hash(expected):
            raise ServiceError(400, "INVALID_REQUEST",
                               "expected_sha256 must be 64 lowercase hex chars")

        declared = body.get("declared_size_bytes")
        if declared is not None:
            if not _is_non_negative_int(declared):
                raise ServiceError(400, "INVALID_REQUEST",
                                   "declared_size_bytes must be a non-negative integer")
            if declared > self._cap(content_class):
                raise ServiceError(
                    422, "SIZE_EXCEEDED",
                    f"{content_class} is capped at {self._cap(content_class)} bytes",
                    details={"cap_bytes": self._cap(content_class)})

        # 5. URL TTL —— 未出网就判
        url_host = None
        if kind == "provider_url":
            url_host = self._check_source_url(source.get("provider_url"))
            expires = source.get("provider_url_expires_at")
            if not isinstance(expires, str):
                raise ServiceError(400, "INVALID_REQUEST",
                                   "provider_url_expires_at is required for provider_url")
            try:
                expires_at = _parse_time(expires)
            except ValueError:
                raise ServiceError(
                    400, "INVALID_REQUEST",
                    "provider_url_expires_at must be an ISO-8601 timestamp")
            if expires_at <= datetime.now(timezone.utc):
                raise ServiceError(409, "URL_EXPIRED", "provider_url has already expired")

        # 6/7. 目标 artifact 与 CAS 版本
        target = body.get("target_artifact_id")
        expected_revision = body.get("expected_revision")
        if target is not None:
            row = self._artifact_row(tenant_id, target)
            if row["state"] == "TOMBSTONED":
                raise ServiceError(409, "ARTIFACT_TOMBSTONED",
                                   "target artifact is tombstoned")
            if row["state"] != "AVAILABLE":
                raise ServiceError(409, "ARTIFACT_TOMBSTONED",
                                   f"target artifact is {row['state']}, not AVAILABLE")
            if not _is_positive_int(expected_revision):
                raise ServiceError(400, "INVALID_REQUEST",
                                   "expected_revision is required with target_artifact_id")
            if expected_revision != row["current_revision"]:
                raise ServiceError(
                    409, "REVISION_CONFLICT", "expected_revision does not match",
                    details={"current_revision": row["current_revision"]})
        elif expected_revision is not None:
            raise ServiceError(400, "INVALID_REQUEST",
                               "expected_revision is only valid with target_artifact_id")

        scope_ref = body.get("scope_ref")
        if not isinstance(scope_ref, dict) or not scope_ref:
            # 没有 scope，§4.4 的去重可见性与 §6.6 的列举都无从判断
            raise ServiceError(400, "INVALID_REQUEST", "scope_ref is required")
        owner_ref = body.get("owner_ref")
        if owner_ref is not None and not isinstance(owner_ref, dict):
            raise ServiceError(400, "INVALID_REQUEST", "owner_ref must be an object")

        # §2.4 有 mime_declared 列、§5 的 F6/T28-33 要按「声明 vs 探测」拒绝，
        # 但 §6.1 的请求体没有这个字段。这里接受 `mime_declared`（与列同名），
        # 缺它就无从比较。已登记为方案 §12 的 C28-17。
        declared_mime = body.get("mime_declared")
        if declared_mime is not None and not isinstance(declared_mime, str):
            raise ServiceError(400, "INVALID_REQUEST", "mime_declared must be a string")

        retention = body.get("retention")
        retention_ref = None
        if retention is not None:
            if not isinstance(retention, dict) or not isinstance(
                    retention.get("policy_ref"), str):
                raise ServiceError(400, "INVALID_REQUEST",
                                   "retention.policy_ref must be a string")
            retention_ref = retention["policy_ref"]

        # 8. S01 授权（§4.10）。不可达时由 authorizer 自己抛 503。
        self.authorizer(tenant_id, "artifact.import", scope_ref, owner_ref)

        import_id = _new_id("imp_")
        now = _now()
        with Transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO artifact_import (import_id, tenant_id, command_id, state,"
                    " content_class, target_artifact_id, expected_sha256, expected_revision,"
                    " declared_size_bytes, mime_declared, owner_ref, scope_ref,"
                    " retention_policy_ref, source_kind, source_ref, attempt_id,"
                    " url_host, url_expires_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'ACCEPTED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (import_id, tenant_id, body["command_id"], content_class, target,
                     expected, expected_revision, declared, declared_mime,
                     None if owner_ref is None else _jcs(owner_ref),
                     _jcs(scope_ref), retention_ref, kind,
                     source.get("provider_job_id") or source.get("upload_id")
                     or source.get("object_key"),
                     source.get("attempt_id"), url_host,
                     source.get("provider_url_expires_at") if kind == "provider_url" else None,
                     now, now))
            except sqlite3.IntegrityError:
                # 幂等在 HTTP 层的 command() 里；走到这里说明有人绕过了它。
                # 数据库约束是最后一道，不能把裸 IntegrityError 漏成 500。
                raise ServiceError(409, "IDEMPOTENCY_KEY_REUSED",
                                   "command_id was already used for this tenant")
        return {"import_id": import_id, "state": "ACCEPTED",
                "artifact_id": None, "replayed": False}

    # ---- worker：发布（§4.1 第 12 步） -----------------------------------------

    def publish(self, tenant_id: str, import_id: str, *,
                observed_size_bytes: int, actual_sha256: str,
                storage_key: str, storage_region: str,
                storage_class: str = "STANDARD",
                probe: dict | None = None,
                derivative_hint: list | None = None,
                home_cell: str = "cell_a1") -> dict:
        """取字节与探测的**结果**交进来，这里只做发布事务。

        调用方（worker）负责在沙箱外算出 `actual_sha256`（§4.3：hash 必须由受信
        路径上的字节算出来，不能由沙箱产出）。本方法不读网络、不读沙箱。
        """
        row = self._import_row(tenant_id, import_id)
        if row["state"] == "PUBLISHED":
            # 重复投递：不重复发布，返回首次结果
            existing = self.conn.execute(
                "SELECT artifact_id, revision FROM artifact_revision"
                " WHERE tenant_id = ? AND source_import_id = ?",
                (tenant_id, import_id)).fetchone()
            return {"import_id": import_id, "state": "PUBLISHED",
                    "artifact_id": existing["artifact_id"] if existing else None,
                    "revision": existing["revision"] if existing else None,
                    "dedup": None, "replayed": True}

        self._check_publishable(row, observed_size_bytes)

        if not _is_hash(actual_sha256):
            raise ServiceError(400, "INVALID_REQUEST",
                               "actual_sha256 must be 64 lowercase hex chars")
        expected = row["expected_sha256"]
        if expected is not None and expected != actual_sha256:
            with Transaction(self.conn):
                self._reject_import(import_id, "HASH_MISMATCH",
                                    "actual hash does not match expected_sha256")
            raise ServiceError(422, "HASH_MISMATCH",
                               "actual hash does not match expected_sha256",
                               retryable=True)

        probe = probe or {}
        declared_mime = row["mime_declared"]
        detected = probe.get("mime_type")
        if declared_mime and detected and declared_mime != detected:
            with Transaction(self.conn):
                self._reject_import(import_id, "REJECTED_SECURITY",
                                    "detected mime_type differs from the declared one")
            raise ServiceError(422, "FORMAT_MISMATCH",
                               "detected mime_type differs from the declared one")

        hints = derivative_hint
        if hints is None:
            hints = DEFAULT_DERIVATIVE_HINTS.get(row["content_class"], [])

        with Transaction(self.conn):
            result = self._publish_in_transaction(
                tenant_id, row, observed_size_bytes, actual_sha256,
                storage_key, storage_region, storage_class, probe, hints, home_cell)
        return {**result, "replayed": False}

    def _check_publishable(self, row: sqlite3.Row, observed_size_bytes) -> None:
        if row["state"] not in ("ACCEPTED", "URL_CHECKED"):
            raise ServiceError(409, "INVALID_REQUEST",
                               f"import is {row['state']}, not publishable")
        if not _is_non_negative_int(observed_size_bytes):
            raise ServiceError(400, "INVALID_REQUEST",
                               "observed_size_bytes must be a non-negative integer")
        cap = self._cap(row["content_class"])
        if observed_size_bytes > cap:
            # §4.2：超上限不进入解码
            with Transaction(self.conn):
                self._reject_import(row["import_id"], "REJECTED_SIZE",
                                    f"observed size exceeds the {row['content_class']} cap")
            raise ServiceError(
                422, "SIZE_EXCEEDED",
                f"{row['content_class']} is capped at {cap} bytes",
                details={"cap_bytes": cap})
        expires = row["url_expires_at"]
        if expires and _parse_time(expires) <= datetime.now(timezone.utc):
            with Transaction(self.conn):
                self._reject_import(row["import_id"], "URL_EXPIRED",
                                    "provider_url expired before the bytes arrived")
            raise ServiceError(409, "URL_EXPIRED",
                               "provider_url expired before the bytes arrived")

    def _reject_import(self, import_id: str, state: str, detail: str) -> None:
        now = _now()
        self.conn.execute(
            "UPDATE artifact_import SET state = ?, reject_code = ?, reject_detail = ?,"
            " terminal_at = ?, updated_at = ? WHERE import_id = ?",
            (state, state, detail, now, now, import_id))

    def _publish_in_transaction(self, tenant_id, row, observed_size_bytes,
                                actual_sha256, storage_key, storage_region,
                                storage_class, probe, hints, home_cell) -> dict:
        import_id = row["import_id"]
        now = _now()

        # 去重（§4.4）：唯一键是 (tenant_id, content_hash)，命中就复用同一份字节
        blob = self.conn.execute(
            "SELECT blob_id, storage_key, storage_region, storage_class"
            " FROM content_blob WHERE tenant_id = ? AND content_hash = ?",
            (tenant_id, actual_sha256)).fetchone()
        dedup_hit = blob is not None
        if blob is None:
            self.conn.execute(
                "INSERT INTO content_blob (blob_id, tenant_id, content_hash, size_bytes,"
                " storage_key, storage_region, storage_class, state, ref_count, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'LIVE', 0, ?)",
                (_new_id("blob_"), tenant_id, actual_sha256, observed_size_bytes,
                 storage_key, storage_region, storage_class, now))
        else:
            # 复用已有位置：后到的导入不覆盖先到的存储坐标
            storage_key = blob["storage_key"]
            storage_region = blob["storage_region"]
            storage_class = blob["storage_class"]

        artifact_id, revision = self._publish_artifact(
            tenant_id, row, actual_sha256, observed_size_bytes,
            storage_key, storage_region, storage_class, probe, home_cell)

        self.conn.execute(
            "UPDATE content_blob SET ref_count = ref_count + 1"
            " WHERE tenant_id = ? AND content_hash = ?",
            (tenant_id, actual_sha256))

        # 可见性（§4.4）：只有调用方对已有 artifact 的 scope 有读权限才说 hit
        dedup = {"hit": False}
        if dedup_hit:
            other = self._readable_artifact_for(
                tenant_id, actual_sha256, artifact_id, _loads(row["scope_ref"]))
            if other is not None:
                dedup = {"hit": True, "artifact_id": other}

        event_id = self._emit(tenant_id, row, artifact_id, revision, actual_sha256,
                              observed_size_bytes, probe, dedup, hints, home_cell)

        self.conn.execute(
            "UPDATE artifact_import SET state = 'PUBLISHED', actual_sha256 = ?,"
            " observed_size_bytes = ?, mime_detected = ?, probe_result = ?,"
            " fetch_finished_at = ?, terminal_at = ?, updated_at = ?"
            " WHERE import_id = ?",
            (actual_sha256, observed_size_bytes, probe.get("mime_type"),
             _jcs(probe), now, now, now, import_id))

        return {"import_id": import_id, "state": "PUBLISHED",
                "artifact_id": artifact_id, "revision": revision,
                "dedup": dedup, "outbox_event_id": event_id}

    def _readable_artifact_for(self, tenant_id: str, content_hash: str,
                               exclude_artifact_id: str, caller_scope):
        rows = self.conn.execute(
            "SELECT DISTINCT a.artifact_id, a.scope_ref FROM artifact a"
            " JOIN artifact_revision r ON r.artifact_id = a.artifact_id"
            " WHERE r.tenant_id = ? AND r.content_hash = ? AND a.artifact_id <> ?"
            " AND a.state = 'AVAILABLE'",
            (tenant_id, content_hash, exclude_artifact_id)).fetchall()
        for row in rows:
            if self._scope_covers(caller_scope, _loads(row["scope_ref"])):
                return row["artifact_id"]
        return None

    def _publish_artifact(self, tenant_id, row, content_hash, size_bytes,
                          storage_key, storage_region, storage_class, probe,
                          home_cell):
        now = _now()
        target = row["target_artifact_id"]
        if target is None:
            artifact_id = _new_id("art_")
            self.conn.execute(
                "INSERT INTO artifact (artifact_id, tenant_id, home_region, home_cell,"
                " owner_ref, scope_ref, content_class, current_revision, state,"
                " retention_policy_ref, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'AVAILABLE', ?, ?, ?)",
                (artifact_id, tenant_id, storage_region, home_cell, row["owner_ref"],
                 row["scope_ref"], row["content_class"], row["retention_policy_ref"],
                 now, now))
            revision = 1
        else:
            # §7.3 的 CAS：0 行受影响 = 有人抢先提交了
            cur = self.conn.execute(
                "UPDATE artifact SET current_revision = current_revision + 1,"
                " updated_at = ? WHERE artifact_id = ? AND tenant_id = ?"
                " AND current_revision = ? AND state = 'AVAILABLE'",
                (now, target, tenant_id, row["expected_revision"]))
            if cur.rowcount != 1:
                current = self.conn.execute(
                    "SELECT current_revision, state FROM artifact"
                    " WHERE artifact_id = ? AND tenant_id = ?",
                    (target, tenant_id)).fetchone()
                if current is not None and current["state"] == "TOMBSTONED":
                    raise ServiceError(409, "ARTIFACT_TOMBSTONED",
                                       "artifact was tombstoned during the import")
                raise ServiceError(
                    409, "REVISION_CONFLICT", "artifact revision moved",
                    details={"current_revision": current["current_revision"]
                             if current else None})
            artifact_id = target
            revision = row["expected_revision"] + 1

        try:
            self.conn.execute(
                "INSERT INTO artifact_revision (artifact_id, revision, tenant_id,"
                " content_hash, size_bytes, mime_type, container, duration_ms, width_px,"
                " height_px, frame_rate_num, frame_rate_den, audio_channels,"
                " sample_rate_hz, storage_key, storage_class, storage_region,"
                " source_import_id, probe_ok, published_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, revision, tenant_id, content_hash, size_bytes,
                 probe.get("mime_type"), probe.get("container"), probe.get("duration_ms"),
                 probe.get("width_px"), probe.get("height_px"),
                 probe.get("frame_rate_num"), probe.get("frame_rate_den"),
                 probe.get("audio_channels"), probe.get("sample_rate_hz"),
                 storage_key, storage_class, storage_region, row["import_id"],
                 1 if probe.get("probed") else 0, now))
        except sqlite3.IntegrityError:
            # (artifact_id, content_hash) 唯一：同身份同字节只能是同一个 revision
            raise ServiceError(
                409, "REVISION_CONFLICT",
                "this artifact already has a revision with the same content")
        return artifact_id, revision

    def _emit(self, tenant_id, row, artifact_id, revision, content_hash,
              size_bytes, probe, dedup, hints, home_cell) -> str:
        data = {
            "schema_version": "2.0",
            "artifact_ref": {"service_id": "S28", "id": artifact_id,
                             "revision": revision, "content_hash": content_hash},
            "content_class": row["content_class"],
            "size_bytes": size_bytes,
            "source": self._event_source(row),
            "dedup": dedup,
            "derivative_hint": [h for h in hints if h in DERIVATIVE_KINDS],
        }
        for key in ("mime_type", "container", "duration_ms", "width_px",
                    "height_px", "frame_rate_num", "frame_rate_den",
                    "audio_channels", "sample_rate_hz"):
            if probe.get(key) is not None:
                data[key] = probe[key]
        return self.outbox.append(new_event(
            source=SOURCE,
            type=EVENT_TYPE,
            subject=artifact_id,
            tenant_id=tenant_id,
            home_cell=home_cell,
            aggregate_id=artifact_id,
            sequence=revision,
            correlation_id=row["command_id"],
            causation_id=row["import_id"],
            data=data,
        ))

    @staticmethod
    def _event_source(row) -> dict:
        """§6.8 的 `source`：kind + 按 kind 从 `source_ref` 取出的 id。

        §2.4 把 provider_job_id / upload_id / 派生源混存在一个 `source_ref` 列里，
        类型信息在存储层就丢了（contracts 里记为 AI-5）。这里按 kind 决定键名。
        """
        kind = row["source_kind"]
        source = {"kind": kind}
        ref = row["source_ref"]
        if ref and kind in ("provider_url", "upload_session"):
            source["provider_job_id"] = ref
        attempt = row["attempt_id"]
        if attempt:
            source["attempt_id"] = attempt
        return source

    # ---- 查询 ----------------------------------------------------------------

    def get_import(self, tenant_id: str, import_id: str) -> dict:
        row = self._import_row(tenant_id, import_id)
        published = self.conn.execute(
            "SELECT artifact_id, revision FROM artifact_revision"
            " WHERE tenant_id = ? AND source_import_id = ?",
            (tenant_id, import_id)).fetchone()
        return {
            "import_id": row["import_id"],
            "state": row["state"],
            "content_class": row["content_class"],
            "artifact_id": published["artifact_id"] if published else None,
            "revision": published["revision"] if published else None,
            "reject_code": row["reject_code"],
            "reject_detail": row["reject_detail"],
            "observed_size_bytes": row["observed_size_bytes"],
            # 「命令终态」可重试 ≠ 「制品终态」（§3.1）
            "retryable": row["state"] in ("FETCH_FAILED", "PUBLISH_FAILED"),
            "created_at": row["created_at"],
            "terminal_at": row["terminal_at"],
        }

    def get_artifact(self, tenant_id: str, artifact_id: str) -> dict:
        row = self._artifact_row(tenant_id, artifact_id)
        response = {
            "artifact_id": row["artifact_id"],
            "tenant_id": row["tenant_id"],
            "content_class": row["content_class"],
            "state": row["state"],
            "current_revision": row["current_revision"],
            "source_revision": row["current_revision"],
            "watermark": row["updated_at"],
            "derivatives": self._derivatives(tenant_id, artifact_id, row),
            "retention": {"policy_ref": row["retention_policy_ref"],
                          "retain_until": None,
                          "legal_hold": bool(row["legal_hold"])},
            "provenance": None,
            "permission_filtered": True,
        }
        if row["state"] == "TOMBSTONED":
            response["tombstoned_at"] = row["tombstoned_at"]

        revision = self.conn.execute(
            "SELECT * FROM artifact_revision WHERE tenant_id = ? AND artifact_id = ?"
            " AND revision = ?",
            (tenant_id, artifact_id, row["current_revision"])).fetchone()
        if revision is not None:
            response.update({
                "content_hash": revision["content_hash"],
                "size_bytes": revision["size_bytes"],
                "mime_type": revision["mime_type"],
                "duration_ms": revision["duration_ms"],
                "width_px": revision["width_px"],
                "height_px": revision["height_px"],
                "frame_rate": self._frame_rate(revision),
                "storage": {"region": revision["storage_region"],
                            "class": revision["storage_class"]},
                "provenance": {"source_kind": None,
                               "import_id": revision["source_import_id"]},
            })
        retention = self.conn.execute(
            "SELECT retain_until FROM retention_binding"
            " WHERE tenant_id = ? AND artifact_id = ?",
            (tenant_id, artifact_id)).fetchone()
        if retention is not None:
            response["retention"]["retain_until"] = retention["retain_until"]
        # 响应不含下载 URL（§6.2）：读取元数据与获取字节是两件事
        return response

    def _derivatives(self, tenant_id: str, artifact_id: str, row) -> list:
        rows = self.conn.execute(
            "SELECT kind, artifact_id, state FROM artifact_derivative"
            " WHERE tenant_id = ? AND source_artifact_id = ? AND source_revision = ?"
            " ORDER BY kind",
            (tenant_id, artifact_id, row["current_revision"])).fetchall()
        return [{"kind": r["kind"], "artifact_id": r["artifact_id"],
                 "state": r["state"]} for r in rows]

    @staticmethod
    def _frame_rate(row):
        if not row["frame_rate_num"]:
            return None
        return {"num": row["frame_rate_num"], "den": row["frame_rate_den"]}

    def get_revision(self, tenant_id: str, artifact_id: str, revision: int) -> dict:
        self._artifact_row(tenant_id, artifact_id)
        row = self.conn.execute(
            "SELECT * FROM artifact_revision WHERE tenant_id = ? AND artifact_id = ?"
            " AND revision = ?", (tenant_id, artifact_id, revision)).fetchone()
        if row is None:
            raise ServiceError(404, "ARTIFACT_NOT_FOUND",
                               f"revision {revision} does not exist")
        return {
            "artifact_id": row["artifact_id"],
            "revision": row["revision"],
            "content_hash": row["content_hash"],
            "size_bytes": row["size_bytes"],
            "mime_type": row["mime_type"],
            "container": row["container"],
            "duration_ms": row["duration_ms"],
            "width_px": row["width_px"],
            "height_px": row["height_px"],
            "frame_rate": self._frame_rate(row),
            "audio_channels": row["audio_channels"],
            "sample_rate_hz": row["sample_rate_hz"],
            "storage": {"region": row["storage_region"],
                        "class": row["storage_class"], "deleted": False},
            "source_import_id": row["source_import_id"],
            "published_at": row["published_at"],
        }

    def list_artifacts(self, tenant_id: str, scope_ref: dict | None,
                       limit: int = 50) -> dict:
        """`GET /artifacts?scope_ref=…`：仅租户内，必须带 scope。

        不提供「全租户搜索」给无权限主体（§6.6）：没有 scope 就拒绝，而不是返回
        整个租户的清单。
        """
        if not isinstance(scope_ref, dict) or not scope_ref:
            raise ServiceError(400, "INVALID_REQUEST",
                               "scope_ref is required to list artifacts")
        rows = self.conn.execute(
            "SELECT artifact_id, scope_ref, content_class, state, current_revision,"
            " created_at FROM artifact WHERE tenant_id = ? AND state <> 'DELETED'"
            " ORDER BY created_at DESC LIMIT ?", (tenant_id, limit)).fetchall()
        items = [{"artifact_id": r["artifact_id"], "content_class": r["content_class"],
                  "state": r["state"], "current_revision": r["current_revision"],
                  "created_at": r["created_at"]}
                 for r in rows if self._scope_covers(scope_ref, _loads(r["scope_ref"]))]
        return {"items": items, "truncated": len(items) >= limit}

    # ---- 访问票据（§6.5）与 tombstone（§6.4） -----------------------------------

    def issue_media_access_ticket(self, tenant_id: str, body: dict, *,
                                  principal_id: str,
                                  principal_type: str = "service") -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_REQUEST", "body must be an object")
        ref = body.get("artifact_ref")
        if not isinstance(ref, dict) or not ref.get("id"):
            raise ServiceError(400, "INVALID_REQUEST", "artifact_ref.id is required")
        purpose = body.get("purpose")
        if purpose not in PURPOSES:
            raise ServiceError(422, "UNSAFE_MEDIA",
                               f"purpose must be one of {list(PURPOSES)}")
        ttl = body.get("ttl_seconds")
        if not _is_positive_int(ttl):
            raise ServiceError(400, "INVALID_REQUEST",
                               "ttl_seconds must be a positive integer")
        if ttl > MAX_TICKET_TTL_SECONDS:
            raise ServiceError(422, "UNSAFE_MEDIA",
                               f"ttl_seconds must not exceed {MAX_TICKET_TTL_SECONDS}")

        row = self._artifact_row(tenant_id, ref["id"])
        if row["state"] == "TOMBSTONED":
            # 明确区分「曾经存在、已停用」与「从未存在」（§1.4）
            raise ServiceError(409, "ARTIFACT_TOMBSTONED", "artifact is tombstoned")
        if row["state"] != "AVAILABLE":
            raise ServiceError(409, "ARTIFACT_TOMBSTONED",
                               f"artifact is {row['state']}, not AVAILABLE")

        scope_ref = body.get("scope_ref")
        if not self._scope_covers(scope_ref, _loads(row["scope_ref"])):
            raise ServiceError(403, "FORBIDDEN_SCOPE",
                               "artifact is outside the requested scope")
        self.authorizer(tenant_id, "artifact.read", scope_ref, None)

        revision_no = ref.get("revision", row["current_revision"])
        revision = self.conn.execute(
            "SELECT * FROM artifact_revision WHERE tenant_id = ? AND artifact_id = ?"
            " AND revision = ?", (tenant_id, ref["id"], revision_no)).fetchone()
        if revision is None:
            raise ServiceError(404, "ARTIFACT_NOT_FOUND",
                               f"revision {revision_no} does not exist")
        if ref.get("content_hash") and ref["content_hash"] != revision["content_hash"]:
            # 所指版本已经不是这一份字节：不能让调用方拿错版本
            raise ServiceError(409, "REVISION_CONFLICT",
                               "content_hash does not match the requested revision")

        issued = datetime.now(timezone.utc)
        expires_at = issued + timedelta(seconds=ttl)
        issued_text = issued.isoformat().replace("+00:00", "Z")
        expires_text = expires_at.isoformat().replace("+00:00", "Z")
        ticket_id = _new_id("tkt_")
        with Transaction(self.conn):
            self.conn.execute(
                "INSERT INTO media_access_ticket (ticket_id, tenant_id, artifact_id,"
                " revision, principal_id, principal_type, purpose, scope_ref,"
                " issued_at, expires_at, max_bytes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, tenant_id, ref["id"], revision_no, principal_id,
                 principal_type, purpose, _jcs(scope_ref) if scope_ref else None,
                 issued_text, expires_text, body.get("max_bytes")))
        # URL 不入库（§2.7），只在响应里返回
        return {"ticket_id": ticket_id,
                "url": self.signer.sign(revision["storage_key"], expires_text),
                "expires_at": expires_text, "method": "GET"}

    def is_ticket_live(self, tenant_id: str, ticket_id: str) -> bool:
        row = self.conn.execute(
            "SELECT expires_at, revoked_at FROM media_access_ticket"
            " WHERE tenant_id = ? AND ticket_id = ?",
            (tenant_id, ticket_id)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return False
        return _parse_time(row["expires_at"]) > datetime.now(timezone.utc)

    def tombstone(self, tenant_id: str, artifact_id: str, body: dict, *,
                  requested_by: str) -> dict:
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_REQUEST", "body must be an object")
        reason = body.get("reason_code", "USER_REQUEST")
        if reason not in TOMBSTONE_REASONS:
            raise ServiceError(400, "INVALID_REQUEST",
                               f"reason_code must be one of {list(TOMBSTONE_REASONS)}")
        if body.get("delete_bytes"):
            # 删除编排（§4.8）不在本轮范围。宁可拒绝，也不能返回一个
            # deletion_id 让调用方以为删除已经启动。
            raise ServiceError(
                422, "INVALID_REQUEST",
                "delete_bytes=true requires the deletion orchestrator (S28 §4.8), "
                "which is not implemented in this build")
        row = self._artifact_row(tenant_id, artifact_id)
        if row["legal_hold"]:
            raise ServiceError(409, "LEGAL_HOLD", "artifact is under legal hold")

        now = _now()
        with Transaction(self.conn):
            # TOMBSTONED → AVAILABLE 不允许（§3.5）：更新语句自带 state 谓词
            cur = self.conn.execute(
                "UPDATE artifact SET state = 'TOMBSTONED', tombstoned_at = ?,"
                " updated_at = ? WHERE artifact_id = ? AND tenant_id = ?"
                " AND state = 'AVAILABLE'",
                (now, now, artifact_id, tenant_id))
            if cur.rowcount == 1:
                self.conn.execute(
                    "INSERT INTO artifact_tombstone (artifact_id, tenant_id,"
                    " reason_code, requested_by, requested_via, effective_at, note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (artifact_id, tenant_id, reason, requested_by,
                     body["command_id"], now, body.get("note")))
                # 未到期的 ticket 全部撤销（§7.4）
                self.conn.execute(
                    "UPDATE media_access_ticket SET revoked_at = ?"
                    " WHERE tenant_id = ? AND artifact_id = ? AND revoked_at IS NULL",
                    (now, tenant_id, artifact_id))
        state = self._artifact_row(tenant_id, artifact_id)["state"]
        return {"artifact_id": artifact_id, "state": state, "deletion_id": None}


__all__ = [
    "ArtifactIngestService",
    "ImportAuthorizer",
    "LocalUrlSigner",
    "ServiceError",
    "Transaction",
]
