"""RFC 8785 规范化的 request_hash，以及 `(tenant_id, command_id)` 键的两种终态。

`request_hash = sha256(JCS(body 去掉 command_id 与 request_hash))`，定义见
plans/S08-revision-snapshot.md §2.1 与 docs/04 §1。同键同 body 返回首次结果；
同键不同 body 是 409 而不是覆盖——这是 AGENTS.md 里「同键不同 body 拒绝（409）」
的代码形态。

JCS 的完整实现（数字规范化、字符串转义）超出本 SDK 的范围；这里用
`json.dumps(sort_keys=True, separators=..., ensure_ascii=False)` 加显式的
非有限数拒绝，覆盖本仓库契约里实际出现的值类型，并在遇到未知类型时抛错
而不是悄悄产生一个稳定的错误 hash。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any


class CommandConflict(Exception):
    """同 `(tenant_id, command_id)` 已经用另一份 body 执行过：409，不可重试。"""

    code = "IDEMPOTENCY_KEY_REUSED"
    retryable = False

    def __init__(self, command_id: str, stored_hash: str, incoming_hash: str):
        super().__init__(
            f"command_id {command_id!r} was used with request_hash {stored_hash}, "
            f"this body hashes to {incoming_hash}"
        )
        self.command_id = command_id
        self.stored_hash = stored_hash
        self.incoming_hash = incoming_hash


class CommandInProgress(Exception):
    """同键同 body，但首次执行还没落结果：409，可重试。"""

    code = "COMMAND_IN_PROGRESS"
    retryable = True

    def __init__(self, command_id: str):
        super().__init__(f"command_id {command_id!r} is still in progress")
        self.command_id = command_id


def _normalize(value: Any) -> Any:
    # RFC 8785 §3.2.2.3: 非有限数不合法，不能静默变成 NaN/Infinity 参与 hash。
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float {value!r} cannot be canonicalized")
        if value == 0.0:
            return 0
        return value
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def canonical_request_hash(body: dict[str, Any]) -> str:
    """对去掉 `command_id` / `request_hash` 的 body 求 JCS 摘要。"""
    payload = {k: v for k, v in body.items() if k not in ("command_id", "request_hash")}
    canonical = json.dumps(
        _normalize(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CommandRecord:
    tenant_id: str
    command_id: str
    request_hash: str
    result: str | None  # None = 正在执行


DDL = """
CREATE TABLE IF NOT EXISTS idempotency_record (
  tenant_id     TEXT NOT NULL,
  command_id    TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  result        TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (tenant_id, command_id)
);
"""


class SqliteIdempotencyStore:
    """`(tenant_id, command_id)` 主键的幂等表，跑在服务自己的 schema 里。

    必须在业务写入的同一个 SQLite 事务中调用，才能保证「结果与事实一起提交」。
    """

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(DDL)

    def begin(self, tenant_id: str, command_id: str, request_hash: str) -> CommandRecord:
        row = self.conn.execute(
            "SELECT request_hash, result FROM idempotency_record "
            "WHERE tenant_id = ? AND command_id = ?",
            (tenant_id, command_id),
        ).fetchone()
        if row is not None:
            stored_hash, result = row
            if stored_hash != request_hash:
                raise CommandConflict(command_id, stored_hash, request_hash)
            if result is None:
                raise CommandInProgress(command_id)
            return CommandRecord(tenant_id, command_id, stored_hash, result)
        self.conn.execute(
            "INSERT INTO idempotency_record "
            "(tenant_id, command_id, request_hash, result, created_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (tenant_id, command_id, request_hash, _utcnow()),
        )
        return CommandRecord(tenant_id, command_id, request_hash, None)

    def finish(self, tenant_id: str, command_id: str, result: str) -> None:
        self.conn.execute(
            "UPDATE idempotency_record SET result = ? "
            "WHERE tenant_id = ? AND command_id = ?",
            (result, tenant_id, command_id),
        )

    def abort(self, tenant_id: str, command_id: str) -> None:
        self.conn.execute(
            "DELETE FROM idempotency_record WHERE tenant_id = ? AND command_id = ?",
            (tenant_id, command_id),
        )


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
