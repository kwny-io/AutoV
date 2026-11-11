"""EventEnvelope v2 与 Outbox / Inbox。

字段集见 docs/04 §4：CloudEvents 1.0 加领域上下文
（schema_version、tenant、home_cell、aggregate_id/sequence、
correlation_id/causation_id、traceparent、data、data_ref）。

Outbox 与本地事实同事务；消费者按 `consumer + event_id` 去重。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


SPECVERSION = "1.0"


@dataclass(frozen=True)
class EventEnvelope:
    id: str
    source: str
    type: str
    subject: str
    time: str
    specversion: str
    schema_version: str
    tenant_id: str
    home_cell: str
    aggregate_id: str
    sequence: int
    correlation_id: str
    causation_id: str | None
    traceparent: str
    data: dict[str, Any]
    data_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "subject": self.subject,
            "time": self.time,
            "specversion": self.specversion,
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "home_cell": self.home_cell,
            "aggregate_id": self.aggregate_id,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "traceparent": self.traceparent,
            "data": self.data,
            "data_ref": self.data_ref,
        }
        return {**out, **self.extra}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox (
  event_id        TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  source          TEXT NOT NULL,
  event_type      TEXT NOT NULL,
  subject         TEXT NOT NULL,
  aggregate_id    TEXT NOT NULL,
  sequence        INTEGER NOT NULL,
  payload         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  published_at    TEXT
);
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox (published_at) WHERE published_at IS NULL;
"""

INBOX_DDL = """
CREATE TABLE IF NOT EXISTS inbox (
  consumer     TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  PRIMARY KEY (consumer, event_id)
);
"""


class Outbox:
    """写侧：与业务事实同一事务调用 `append`，事务提交即事实与事件一起落盘。"""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(OUTBOX_DDL)

    def append(self, envelope: EventEnvelope) -> str:
        self.conn.execute(
            "INSERT INTO outbox (event_id, tenant_id, source, event_type, subject,"
            " aggregate_id, sequence, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope.id,
                envelope.tenant_id,
                envelope.source,
                envelope.type,
                envelope.subject,
                envelope.aggregate_id,
                envelope.sequence,
                envelope.to_json(),
                _now(),
            ),
        )
        return envelope.id

    def unpublished(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM outbox WHERE published_at IS NULL "
            "ORDER BY created_at, sequence LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def mark_published(self, event_id: str) -> None:
        self.conn.execute(
            "UPDATE outbox SET published_at = ? WHERE event_id = ?", (_now(), event_id)
        )


class Inbox:
    """读侧：至少一次投递下去重。`consume` 返回 True 表示首次见到该 event_id。"""

    def __init__(self, conn):
        self.conn = conn
        self.conn.executescript(INBOX_DDL)

    def consume(self, consumer: str, event_id: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inbox (consumer, event_id, received_at) VALUES (?, ?, ?)",
            (consumer, event_id, _now()),
        )
        return cur.rowcount == 1


def new_event(
    *,
    source: str,
    type: str,
    subject: str,
    tenant_id: str,
    home_cell: str,
    aggregate_id: str,
    sequence: int,
    correlation_id: str,
    data: dict[str, Any],
    causation_id: str | None = None,
    traceparent: str = "00-" + "0" * 32 + "-" + "0" * 16 + "-01",
    data_ref: str | None = None,
    schema_version: str = "2.0",
) -> EventEnvelope:
    return EventEnvelope(
        id=str(uuid.uuid4()),
        source=source,
        type=type,
        subject=subject,
        time=_now(),
        specversion=SPECVERSION,
        schema_version=schema_version,
        tenant_id=tenant_id,
        home_cell=home_cell,
        aggregate_id=aggregate_id,
        sequence=sequence,
        correlation_id=correlation_id,
        causation_id=causation_id,
        traceparent=traceparent,
        data=data,
        data_ref=data_ref,
    )
