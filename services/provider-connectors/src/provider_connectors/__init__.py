"""S27 provider-connectors：参数映射器 + 协议处理器 + 观测上报者。

八条协议方法见 `mappers.SubmitPayloadMapper`；语义见
`plans/S27-provider-connectors.md`。

**这个包不 import os、不发网络、不 import 别的服务的业务模块。** 出站 HTTP 与
S26 的 claim 都是注入的端口，真实实现在 `api/` 与部署层。
"""

from provider_connectors.mappers import (
    CALLBACK_SKEW_SECONDS,
    DURATION_TIERS_S,
    OBSERVATION_STATUSES,
    SUBMIT_OUTCOME_MARKER,
    SUBMIT_OUTCOMES,
    FakeChannel,
    SubmitContext,
    SubmitPayloadMapper,
)
from provider_connectors.schema import SCHEMA
from provider_connectors.service import (
    BREAKER_OPEN_MS,
    BREAKER_THRESHOLD,
    COMMAND_EVENTS,
    OBSERVATION_RECEIVED,
    REQUIRE_DISPATCH_GRANT,
    SUBMIT_HTTP_TIMEOUT_SECONDS,
    ClaimUnavailable,
    ProviderConnectorsService,
    ServiceError,
    TransportError,
    TransportResult,
    UnknownChannel,
    classify_submit,
    initialise,
)

__all__ = [
    "BREAKER_OPEN_MS",
    "BREAKER_THRESHOLD",
    "CALLBACK_SKEW_SECONDS",
    "COMMAND_EVENTS",
    "ClaimUnavailable",
    "DURATION_TIERS_S",
    "FakeChannel",
    "OBSERVATION_RECEIVED",
    "OBSERVATION_STATUSES",
    "ProviderConnectorsService",
    "REQUIRE_DISPATCH_GRANT",
    "SCHEMA",
    "SUBMIT_HTTP_TIMEOUT_SECONDS",
    "SUBMIT_OUTCOME_MARKER",
    "SUBMIT_OUTCOMES",
    "ServiceError",
    "SubmitContext",
    "SubmitPayloadMapper",
    "TransportError",
    "TransportResult",
    "UnknownChannel",
    "classify_submit",
    "initialise",
]
