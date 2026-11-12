"""S27 的协议映射层：`SubmitPayloadMapper` 与本地开发用的 `FakeChannel`。

`plans/S27-provider-connectors.md` §4.1 定义了映射器接口，§3 定义了八个方法。
这个模块只放**纯函数式**的翻译逻辑：不碰数据库、不发网络、读不到环境变量。

## 四条硬规则（§4.1 明文要求"代码检查，不是文档"）

1. `to_native` **不得**读取任何环境变量或配置来决定 `model`。模型只能来自
   `payload["model_id"]` / `payload["model_snapshot"]`。本模块不 import `os`
   （T27-25 用 `ast` 断言）。
2. `to_native` **不得**丢弃 `required_capabilities` 里的任何一项。表达不了的，
   `validate` 必须返回错误 —— 不能静默忽略（T27-24）。
3. `from_native_status` 必须把供应商状态映射到 6 个平台状态之一，**未知值报
   `inconclusive`，不能猜**（§4 行 361）。
4. `from_native_response` 的结论必须是 `accepted|rejected|unknown|not_sent`
   四选一，且由 socket 层事实决定（§5.3）—— 所以它只回答"响应说了什么"，
   真正的四态判定在 `service.classify_submit` 里，因为它还要看 `phase`。

## 为什么 `FakeChannel` 在 src/ 里而不是 tests/ 里

§16 第 1 步：「`SubmitPayloadMapper` 接口 + `FakeChannel`（用于本地开发与契约
测试）」。它是**协议的参考实现**，不是测试夹具：它展示一个渠道适配器
需要提供哪些东西、返回什么形状。真实渠道接进来时照着它写，而不是照着测试写。
`connectors/*/*/connector.yaml` 里九个渠道全部 `disabled`，所以这个仓库目前
只有这一个渠道实现。

## 与方案的偏离（C27-7 / C27-9）

- **C27-7：`status` 只发七个值。** `connector.observation-received.v2.schema.json`
  故意不给 `status` 收 enum（PCR-1：拒绝收窄供应商词汇）。但平台侧的消费方
  （S26 的 `PROGRESS_RANK`）需要有限的词汇才能判"倒退"。S27 因此承诺
  **发出**的值只在 `OBSERVATION_STATUSES` 里，未知的供应商状态一律降成
  `inconclusive`，绝不把供应商的原文塞进 `status`（原文进
  `provider_status_raw`）。
- **C27-9：`provider_usage` 由实现提到平级。** §3.5 行 260 的响应示例把 usage
  嵌在 `output` 内（PCR-3），而 §6.5 行 606 的事件里它在与 `output` 平级的位置。
  两种写法都过 schema，所以必须由实现唯一定下来。取"提出来"：否则 S23 要为了
  读用量去解 `output`，而 `output` 的形状是供应商定义的，不该被平台解释。
  事件契约里**没有** `error` / `retry_after_ms` 两个键，所以 §3.5 示例里的它们
  只留在本地回执（C27-6）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# §3.5 行 241 + §4 行 361。`inconclusive` 是第 7 个：供应商给了我们没见过的
# 状态值时必须报它，不能猜一个六值。契约里 `status` 故意不收 enum（PCR-1），
# 所以这里也不收 —— 但**发出的值只从这七个里取**（C27-7）。
OBSERVATION_STATUSES = (
    "queued", "running", "succeeded", "failed", "canceled", "not_found",
    "inconclusive",
)

# 这些值不是"供应商状态"，而是 S27 自己的提交结论（§5.3）。S26 用
# `provider_status_raw == "SUBMIT_OUTCOME"` 把它们和供应商状态区分开。
SUBMIT_OUTCOMES = ("accepted", "rejected", "unknown", "not_sent")
SUBMIT_OUTCOME_MARKER = "SUBMIT_OUTCOME"

# 回调验签的时间窗（§6.3）。
CALLBACK_SKEW_SECONDS = 300


@dataclass(frozen=True)
class SubmitContext:
    """`to_native` 需要的、来自派单而不是来自业务输入的那部分。

    全部来自 `connector.submit.requested` 的 data 或它的信封。**没有一个字段
    能让映射器换模型、改预算或挑账户** —— 这不是巧合，是 §1.2 的结构化落点。
    """

    dispatch_id: str
    provider_job_id: str
    tenant_id: str
    stable_client_key: str
    fencing_token: int
    deadline: str
    callback_url: str = ""
    traceparent: str = ""


@runtime_checkable
class SubmitPayloadMapper(Protocol):
    """§4.1。每个渠道适配器实现这一个接口。"""

    channel_route: str
    idempotency_class: str          # supported | client_lookup_only | unsupported
    cancel_semantics: str           # immediate | request_only | unsupported
    refresh_url_supported: bool
    supports_callback: bool
    supported_modes: frozenset[str]

    def capabilities(self) -> dict:
        ...

    def validate(self, payload: dict) -> list[dict]:
        ...

    def estimate(self, payload: dict) -> dict:
        ...

    def to_native(self, payload: dict, ctx: SubmitContext) -> dict:
        ...

    def provider_ref_from(self, body: dict) -> str | None:
        ...

    def classify_rejection(self, http_status: int, body: dict) -> str | None:
        ...

    def from_native_status(self, body: dict) -> dict:
        ...

    def parse_callback(self, body: dict) -> dict:
        ...

    def verify_callback(self, raw_body: bytes, headers: dict,
                        secret: bytes, *, now: float) -> dict:
        ...

    def native_cancel(self, provider_job_ref: str) -> dict:
        ...

    def native_manifest(self, provider_job_ref: str,
                        refresh_urls: bool) -> dict:
        ...


# ---------------------------------------------------------------- 常量

# §4.1 的时长档。供应商按档收费，档外值必须拒 —— 吸附是 S25 的事。
DURATION_TIERS_S = (5, 10)

# FakeChannel 能表达的能力。`required_capabilities` 里出现别的一律 validate 失败
# （规则 2）。真实渠道按自己的算子集填。
EXPRESSIBLE_CAPABILITIES = frozenset({
    "text_to_video", "image_to_video", "first_last_frame",
    "reference_to_video", "preserve_identity", "native_audio",
})

# 供应商原生模式 ↔ 平台模式。写死在映射表里，**没有"默认模式"**（规则 1 的邻居）。
NATIVE_MODES = {
    "text_to_video": "txt2video",
    "image_to_video": "img2video",
    "first_last_frame": "flf2v",
    "reference_to_video": "ref2video",
}
PLATFORM_MODES = {v: k for k, v in NATIVE_MODES.items()}

# 供应商原生状态 → 平台状态（规则 3）。**不在表里的一律 inconclusive。**
NATIVE_STATUS = {
    "QUEUED": "queued", "PENDING": "queued", "SUBMITTED": "queued",
    "RUNNING": "running", "IN_PROGRESS": "running", "PROCESSING": "running",
    "SUCCEEDED": "succeeded", "SUCCESS": "succeeded", "COMPLETED": "succeeded",
    "FAILED": "failed", "ERROR": "failed",
    "CANCELED": "canceled", "CANCELLED": "canceled",
    "NOT_FOUND": "not_found", "ABSENT": "not_found",
}

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})

# 4xx 里"明确未受理"的供应商错误码（规则 4 的 rejected 分支）。不在这里的
# 4xx 一律按 unknown 处理 —— 理由见 `service.classify_submit`。
REJECTION_CODES = {
    "invalid_parameter": "invalid_input",
    "invalid_request": "invalid_input",
    "policy_violation": "policy",
    "content_policy": "policy",
    "unauthorized": "auth",
    "forbidden": "auth",
}


# ---------------------------------------------------------------- FakeChannel

@dataclass
class FakeChannel:
    """一个够用、能跑通全部契约的渠道实现。

    协议（`native` 侧）刻意做得像真实供应商：提交返回一个任务 id，查询返回
    一个大写状态词 + 可选的 output/usage。HTTP 传输不在这里 —— 由 `service`
    注入的 `transport` 端口负责，这样四态判定才测得到 connect/read 的差别。
    """

    channel_route: str = "fake/channel"
    idempotency_class: str = "client_lookup_only"
    cancel_semantics: str = "immediate"
    refresh_url_supported: bool = True
    supports_callback: bool = True
    supported_modes: frozenset[str] = frozenset(NATIVE_MODES)
    # `provider_usage` 是**观测原文**，不声明任何计价语义（PCR-4 / x-needs-owner
    # 第三条）。这个渠道回什么，S27 就报什么，不做乘算。
    usage_units: tuple[str, ...] = ("generated_seconds", "currency")

    # ---- 无副作用的方法 ------------------------------------------------

    def capabilities(self) -> dict:
        """§3.1。这是**部署配置里声明的映射表**，不是实时探测。"""
        return {
            "channel_route": self.channel_route,
            "model_id": "fake",
            "model_snapshot": "v1",
            "modes": sorted(self.supported_modes),
            "execution_kind": "poll",
            "supports_callback": self.supports_callback,
            "callback_scheme": "hmac_sha256",
            "native_audio": False,
            "output": {
                "aspect_ratios": ["9:16", "16:9", "1:1"],
                "max_resolution": {"width": 1080, "height": 1920},
                "duration_tiers_s": list(DURATION_TIERS_S),
                "trims_to_nearest": False,
            },
            "input_limits": {"prompt_chars": 2000, "reference_images": 4,
                             "video_refs": 1},
            # §3.1 末段：必须来自**实测**，不是文档推断。这里是本地开发渠道，
            # 它的"实测"就是它自己声明的行为。
            "idempotency": self.idempotency_class,
            "cancel_supported": self.cancel_semantics != "unsupported",
            "url_ttl_seconds": 86400,
            "job_ttl_seconds": 604800,
            "pricing": {"billing_units": [{"unit": "generated_second",
                                           "tiers_s": list(DURATION_TIERS_S)}],
                        "currency": "USD"},
            "evidence": {"source": "local_fake", "fetched_at": None,
                         "doc_hash": None},
        }

    def validate(self, payload: dict) -> list[dict]:
        """§3.2。纯本地、零网络。**表达不了的 required capability 必须报错**。"""
        errors: list[dict] = []
        mode = payload.get("mode")
        if mode not in self.supported_modes:
            errors.append({"code": "MODE_UNSUPPORTED", "path": "mode",
                           "detail": f"{mode!r} is not one of "
                                     f"{sorted(self.supported_modes)}"})
        cap_mode = NATIVE_MODES.get(mode) if mode else None
        if mode is not None and cap_mode is None:
            errors.append({"code": "MODE_UNMAPPABLE", "path": "mode",
                           "detail": f"{mode!r} has no native spelling"})
        duration = payload.get("duration_seconds")
        if duration is not None and duration not in DURATION_TIERS_S:
            # 不吸附：档位吸附是 S25 的责任（§2.3），这里只报不一致。
            errors.append({"code": "DURATION_TIER_MISMATCH",
                           "path": "duration_seconds",
                           "detail": f"{duration} is not one of "
                                     f"{list(DURATION_TIERS_S)}"})
        prompt = (payload.get("prompt") or {}).get("text") or ""
        limit = 2000
        if len(prompt) > limit:
            errors.append({"code": "PROMPT_TOO_LONG", "path": "prompt.text",
                           "detail": f"{len(prompt)} > {limit}"})
        for cap in payload.get("required_capabilities") or []:
            if cap not in EXPRESSIBLE_CAPABILITIES:
                # 规则 2：不能静默忽略。
                errors.append({"code": "CAPABILITY_UNEXPRESSIBLE",
                               "path": "required_capabilities",
                               "detail": f"{cap!r} cannot be expressed on "
                                         f"{self.channel_route}"})
        return errors

    def estimate(self, payload: dict) -> dict:
        """§3.3。纯计算，不产生费用。金额单位是 micro，与 S23 一致。"""
        seconds = int(payload.get("duration_seconds") or 5)
        per_second_micro = 180_000
        amount = per_second_micro * seconds
        return {
            "estimated_units": {"generated_second": seconds},
            "estimated_cost_micro": amount,
            "currency": "USD",
            "worst_case_cost_micro": amount * 2,
            "breakdown": [{"component": "generation",
                           "amount_micro": amount}],
            "confidence": "documented",
        }

    # ---- 翻译 ----------------------------------------------------------

    def to_native(self, payload: dict, ctx: SubmitContext) -> dict:
        """平台规范输入 → 供应商请求。

        规则 1 的落点：`model` 只来自 `payload`，本函数没有第二个来源可读。
        """
        mode = payload.get("mode")
        native = {
            "model": payload.get("model_id"),
            "model_version": payload.get("model_snapshot"),
            "mode": NATIVE_MODES.get(mode, mode),
            "prompt": (payload.get("prompt") or {}).get("text"),
            "negative_prompt": (payload.get("prompt") or {}).get("negative"),
            "duration": payload.get("duration_seconds"),
            "ratio": payload.get("aspect_ratio"),
            "resolution": payload.get("resolution"),
            "seed": payload.get("seed"),
            # §5.4：`client_lookup_only` / `supported` 都把这个 key 交给供应商，
            # 供后续查询（前者）或去重（后者）。`unsupported` 仍然放进 metadata，
            # 因为放进去没有坏处；真正的执行点在本地索引表。
            "client_key": ctx.stable_client_key,
            "callback_url": ctx.callback_url or None,
            "metadata": {
                "provider_job_id": ctx.provider_job_id,
                "dispatch_id": ctx.dispatch_id,
                "tenant_id": ctx.tenant_id,
            },
            "references": [
                {"role": r.get("role"), "url": r.get("url"),
                 "sha256": r.get("sha256")}
                for r in (payload.get("references") or [])
            ],
        }
        return {k: v for k, v in native.items() if v is not None}

    def provider_ref_from(self, body: Any) -> str | None:
        if isinstance(body, dict):
            for key in ("id", "task_id", "job_id", "provider_job_ref"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def classify_rejection(self, http_status: int, body: Any) -> str | None:
        """4xx 里哪些能证明"供应商未受理"。判不了就返回 None → unknown。"""
        if not isinstance(body, dict):
            return None
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(err.get("code") or "").lower()
        return REJECTION_CODES.get(code)

    def from_native_status(self, body: Any) -> dict:
        """§3.5 响应 → 平台观测的字段。**未知状态值报 inconclusive**。"""
        body = body if isinstance(body, dict) else {}
        raw = str(body.get("state") or body.get("status") or "").strip().upper()
        status = NATIVE_STATUS.get(raw, "inconclusive")
        if status not in OBSERVATION_STATUSES:          # 防御：表写错了也不能发出去
            status = "inconclusive"
        return {
            "provider_job_ref": self.provider_ref_from(body),
            # 原样保留供应商的取值，**不转小写不翻译** —— 排障要看原文。
            "provider_status_raw": raw or "UNKNOWN",
            "status": status,
            "terminal": status in TERMINAL_STATUSES,
            "progress": body.get("progress"),
            # §3.5：usage 在响应里嵌在 output 内（PCR-3），这里**提出来**放到
            # 平级，因为事件契约把它定义在平级。两种位置都过 schema，所以必须
            # 由实现唯一定下来（C27-9）。
            "output": body.get("output"),
            "provider_usage": body.get("usage"),
        }

    def parse_callback(self, body: Any) -> dict:
        """供应商回调 → 关联键 + 观测字段（§6.4）。"""
        body = body if isinstance(body, dict) else {}
        task = body.get("data") if isinstance(body.get("data"), dict) else body
        return {
            "client_key": body.get("client_key") or task.get("client_key"),
            "provider_job_ref": self.provider_ref_from(task),
            "observations": [self.from_native_status(task)],
        }

    def verify_callback(self, raw_body: bytes, headers: dict,
                        secret: bytes, *, now: float) -> dict:
        """§6.3。`signed = ts + '.' + rawBody`，HMAC-SHA256，常数时间比较。"""
        def _get(name: str) -> str:
            for k, v in headers.items():
                if k.lower() == name:
                    return str(v)
            return ""

        ts = _get("x-timestamp")
        got = _get("x-signature")
        if not ts or not got:
            return {"ok": False, "reason": "missing_headers"}
        try:
            skew = abs(now - float(ts))
        except ValueError:
            return {"ok": False, "reason": "bad_timestamp"}
        if skew > CALLBACK_SKEW_SECONDS:
            return {"ok": False, "reason": "timestamp_out_of_window"}
        signed = ts.encode("ascii") + b"." + raw_body
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        # 长度不等时 compare_digest 会返回 False 但比较时间仍随长度变化，
        # 先拒掉长度不符的，再走定长比较。
        if len(expected) != len(got):
            return {"ok": False, "reason": "signature_mismatch"}
        if not hmac.compare_digest(expected.encode("ascii"), got.encode("ascii")):
            return {"ok": False, "reason": "signature_mismatch"}
        return {"ok": True}

    def native_cancel(self, provider_job_ref: str) -> dict:
        return {"task_id": provider_job_ref}

    def native_manifest(self, provider_job_ref: str, refresh_urls: bool) -> dict:
        return {"task_id": provider_job_ref, "refresh": bool(refresh_urls)}
