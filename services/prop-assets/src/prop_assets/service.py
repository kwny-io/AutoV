"""S14 内核。方案：`plans/S14-prop-assets.md`。

一句话边界：**S14 是"这个东西长什么样、上面写了什么字、开着关着破着分别是什么样"
的唯一权威；它不知道也不能知道"现在归谁、现在是什么状态"。**

三条结构性事实，代码里每一处判断都从它们推出来：

1. **变体是能力，不是事实**（§1.1）。`variant_axes` 回答"我能画出哪几种"，
   永远不回答"现在是哪一种"。读不到请求的变体时**绝不退回默认**（§4.3）——
   S16 说"钥匙开着"、S14 退回 `closed`，S17 就会拿默认图判"通过"，
   剧情线索当场断掉，而没有任何一层会报错。
2. **文字变更是最贵的变更**（§1.5、§3.3）。`canonical_text` 一变，
   所有依赖它的特写、读信、仲裁证据全废。所以文字变更必须 `human_approved`，
   `policy_auto` 在这里是禁止的而不是"不推荐"。
3. **canonical ≠ 最新**（§1.5）。`prop.canonical_revision` 与
   `prop.current_revision` 分开存，接受新版本不自动移动指针，
   `:set-canonical` 能不发事件地移动它（C14-7）。

事务形状与 S25 同构：`BEGIN IMMEDIATE` → 业务事实 → `outbox.append`
→ 同一次 `COMMIT`。事件先发后写或先写后发都会留下一致性空洞。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from event_sdk import Outbox, new_event

from .schema import CONTRACT_GAPS, ID_PREFIX, SCHEMA

SOURCE = "S14"
SCHEMA_VERSION = "2.0"

EVENT_PROP_REVISION_ACCEPTED = "asset.prop.revision.accepted"
TOPIC_PROP = "drama.prop-assets.v2"

# §2.5：候选默认 30 天过期。过期不删，改成 EXPIRED —— "曾经提过又被放过"
# 本身是审计信息。
CANDIDATE_TTL = timedelta(days=30)

# §5.4 / C14-15：方案给了 429 但没说触发条件。按道具的 PENDING 候选数封顶，
# 只做本地背压，不是业务配额（配额归 S23）。
MAX_PENDING_CANDIDATES = 64

# §4.1 第 4 条的真实单位边界（cm / g）。
MAX_DIMENSION_CM = 500.0
MAX_WEIGHT_G = 50000.0

LEGIBILITIES = ("must_be_readable", "may_be_blurred", "background_only")
PROP_KINDS = ("hero_prop", "story_prop", "set_dressing", "background_prop",
              "wearable_prop")
STATUSES = ("active", "deprecated", "tombstoned")
SOURCE_KINDS = ("manual_upload", "generated", "imported", "derived")
DECISION_KINDS = ("human_approved", "policy_auto", "initial_import")

# §4.3 / C14-6：敏感文字的读取作用域。它必须由平台签名注入
# （`X-Principal-Scopes`），**绝不**从请求体读任何 is_admin / redact=false。
SENSITIVE_READ_SCOPE = "prop.text.sensitive_read"

_TEXT_FIELDS = ("surface", "content_kind", "canonical_text", "language",
                "layout", "legibility", "sensitive")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _jcs(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(kind: str) -> str:
    return ID_PREFIX[kind] + uuid.uuid4().hex[:20]


def _row(row) -> dict | None:
    return None if row is None else dict(row)


def _loads(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class Transaction:
    """`BEGIN IMMEDIATE` + 显式提交。不嵌套：一个命令一个事务。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


# ------------------------------------------------------------ 纯函数：文字

def text_fingerprint(text_details) -> str | None:
    """文字指纹。`None` 表示**这一版没有文字**（§2.2 行 199 的 `NULL = 无文字`）。

    方案三处说法不一致（C14-9 / PRP-3），这里把算法写死，使任何实现都能复算：

    - 没有可读文字 → `None`
    - 只有一个面有文字 → `sha256(canonical_text)`，与 §2.1 行 189 的定义一致
    - 多个面有文字 → 按 `surface` 排序后对 `[{"surface","text"}]` 取 JCS 再
      sha256。排序是必须的：不排序的话同一内容换个面的顺序就报"文字变了"，
      而 §1.3 的面本就是无序的。

    只统计 `canonical_text` 非空的面。`may_be_blurred` / `background_only`
    的文字也算——它们同样进了图，改了一样要人审。
    """
    faces = []
    for detail in text_details or []:
        text = detail.get("canonical_text")
        if not isinstance(text, str) or text == "":
            continue
        faces.append({"surface": str(detail.get("surface") or ""),
                      "text": text})
    if not faces:
        return None
    if len(faces) == 1:
        return _sha256_hex(faces[0]["text"])
    faces.sort(key=lambda face: (face["surface"], face["text"]))
    return _sha256_hex(_jcs(faces))


def text_changed(old_fingerprint, new_fingerprint) -> bool:
    """`IS DISTINCT FROM` 语义，**不是** SQL 的 `!=`。

    §4.2 行 446 的伪代码是 `new_fp != prop.text_fingerprint`。如果照 SQL 三值
    逻辑实现，`x != NULL` 求值为 NULL，于是"给一把光面钥匙刻上编号"——最常见
    的那类文字变更——会被判成"没变"，绕过人审（PRP-3 第 (1) 条）。这里用
    Python 的不等号：`None` 与非 `None` 互相都算变更。
    """
    return old_fingerprint != new_fingerprint


def content_hash(payload: dict) -> str:
    """§2.2 `content_hash`：**纯内容**的规范化 sha256，不含 `prop_id`。

    不含 `prop_id` 是方案的索引决定的（`pr_hash_uniq (tenant_id, content_hash)`，
    C14-11）：同一租户里两个道具内容一字不差会被当成"重复"，而不是两条记录。
    不含 `accepted_at` / `revision`：那是每次接受都不同的坐标，放进去就没有
    "同内容重复接受"可判了（F11）。

    敏感面在投影里用 `text_hash` 代替原文：hash 仍然区分不同文字，但落进
    `content_hash` 的字节里没有明文——`content_hash` 会被写进事件与日志。
    """
    sanitized = []
    for detail in payload.get("text_details") or []:
        item = {key: detail.get(key) for key in
                ("surface", "content_kind", "language", "layout", "legibility")}
        item["sensitive"] = bool(detail.get("sensitive"))
        if item["sensitive"]:
            item["text_hash"] = _text_hash_of(detail)
        else:
            item["canonical_text"] = detail.get("canonical_text")
        sanitized.append(item)
    sanitized.sort(key=lambda item: (str(item.get("surface") or ""),
                                     str(item.get("canonical_text")
                                         or item.get("text_hash") or "")))
    projection = {
        "physical": payload.get("physical"),
        "appearance": payload.get("appearance"),
        "text_details": sanitized,
        "variant_manifest": payload.get("variant_manifest"),
        "variant_axes": payload.get("variant_axes"),
        "rights_ref": payload.get("rights_ref"),
        "source_kind": payload.get("source_kind"),
        "parent_revision": payload.get("parent_revision"),
    }
    return _sha256_hex(_jcs(projection))


def _text_hash_of(detail: dict) -> str:
    """一面的文字摘要。调用方给了 `text_hash` 且与原文算出来的一致就用它，
    否则用算出来的——两者不一致在 `_validate_text_details` 里已经 422 了。"""
    supplied = detail.get("text_hash")
    if isinstance(supplied, str) and len(supplied) == 64:
        return supplied
    text = detail.get("canonical_text")
    return _sha256_hex(text if isinstance(text, str) else "")


# ------------------------------------------------------------ 服务

class PropAssetsService:
    def __init__(self, conn: sqlite3.Connection, *, artifact_source,
                 quality_gate_source=None, authorizer=None,
                 max_pending_candidates: int = MAX_PENDING_CANDIDATES):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        # S28：**必需**。没有 artifact 校验就等于没有资产正确性（F1、E29-E2）。
        self.artifact_source = artifact_source
        # S35/S36 的 QC：**可选**。方案 §4.1 第 6 条把结论的来源写成
        # `quality_gate_ref`，而全仓没有产出它的服务（C14-10 / PRP-6）。
        # 传 None 时结论只能随命令内联传入；读不到就记 null，绝不用 true 兜底。
        self.quality_gate_source = quality_gate_source
        # S01：可选，只用于复核敏感文字的读取作用域。主路径是平台签名的
        # `X-Principal-Scopes`（§4.3）。
        self.authorizer = authorizer
        self.max_pending_candidates = max_pending_candidates

    # ------------------------------------------------------------ 事务助手
    def _sweep_expired(self, tenant_id: str, prop_id: str | None = None) -> int:
        """把过期的 PENDING 候选落成 EXPIRED（§3.1 的 `ttl──► EXPIRED`）。

        **必须走自己的事务**，不能塞进命令事务里"顺便标一下"：命令随后
        抛 409，`Transaction` 会回滚，那条 UPDATE 也就没了。告警说候选
        过期了、库里却永远还是 PENDING——下一次 draft 的 64 上限照样数它，
        用户会因为一堆看不见的死候选被 429。

        放在 `draft_candidate` 开头（事务外）而不是后台线程：这个服务没有
        调度器，而"要新增候选"正是最该回收旧位的时刻。
        """
        sql = ("UPDATE prop_candidate SET state='EXPIRED'"
               " WHERE tenant_id=? AND state='PENDING' AND expires_at<=?")
        params: tuple = (tenant_id, _now())
        if prop_id is not None:
            sql += " AND prop_id=?"
            params += (prop_id,)
        with Transaction(self.conn):
            cur = self.conn.execute(sql, params)
            return cur.rowcount

    def _command(self, tenant_id: str, command_id: str, produce):
        with Transaction(self.conn):
            result = produce()
            self.conn.execute(
                "UPDATE idempotency_record SET result=? "
                "WHERE tenant_id=? AND command_id=?",
                (_jcs(result), tenant_id, command_id))
            return result

    def _upstream(self, call, code: str = "DEPENDENCY_UNAVAILABLE"):
        """上游不可达 → 503。**绝不拿缓存的结论兜底**（F13）。

        资产比路由更不该凑合：一个校验不到的 artifact 就是一张可能画错的参考图。
        """
        try:
            return call()
        except ConnectionError as exc:
            raise ServiceError(503, code, f"upstream unreachable: {exc}",
                               retryable=True) from exc
        except ServiceError as exc:
            if exc.status >= 500:
                raise ServiceError(503, code, str(exc), retryable=True,
                                   details=exc.details) from exc
            raise

    # ------------------------------------------------------------ 通用校验
    def _load_prop(self, tenant_id: str, prop_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM prop WHERE tenant_id=? AND prop_id=?",
            (tenant_id, prop_id)).fetchone())
        if row is None:
            # 跨租户与不存在同形（F14/E28）：确认"存在但你没权限"本身就是泄漏。
            raise ServiceError(404, "NOT_FOUND", f"no prop {prop_id}")
        return row

    def _cas(self, prop: dict, expected) -> None:
        if expected is None:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "expected_current_revision is required: this service never "
                "overwrites a revision it has not seen")
        if int(expected) != int(prop["current_revision"]):
            raise ServiceError(
                409, "VERSION_CONFLICT",
                f"prop {prop['prop_id']} is at revision "
                f"{prop['current_revision']}, expected {expected}",
                retryable=True,
                details={"expected": int(expected),
                         "current": int(prop["current_revision"])})

    @staticmethod
    def _require(condition, message: str) -> None:
        if not condition:
            raise ServiceError(400, "MALFORMED_REQUEST", message)

    def _validate_physical(self, physical) -> dict:
        """§1.4：必须真实厘米/克。`small/medium/large` 无法让 S17 判比例。"""
        if not isinstance(physical, dict):
            raise ServiceError(422, "PHYSICAL_DIMENSION_INVALID",
                               "physical must be an object with real units")
        missing, bad = [], []
        for key, upper in (("length_cm", MAX_DIMENSION_CM),
                           ("width_cm", MAX_DIMENSION_CM),
                           ("height_cm", MAX_DIMENSION_CM),
                           ("weight_g", MAX_WEIGHT_G)):
            value = physical.get(key)
            if value is None:
                missing.append(key)
                continue
            # bool 是 int 的子类，`True` 会被当成 1 —— 明确拒掉。
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                bad.append(key)
                continue
            if not (0 < float(value) <= upper):
                bad.append(key)
        if missing or bad:
            raise ServiceError(
                422, "PHYSICAL_DIMENSION_INVALID",
                "physical dimensions must be real cm/g values in (0, 500] and "
                "weight in (0, 50000] g",
                details={"missing": missing, "out_of_range": bad})
        out = {key: physical[key] for key in
               ("length_cm", "width_cm", "height_cm", "weight_g")}
        # 两个设计标记：`handheld` 决定特写要不要画手，`rigid` 决定挤压/弯折
        # 是否合理。缺省 False 不是"不知道"，是"没这个预期"。
        out["handheld"] = bool(physical.get("handheld"))
        out["rigid"] = bool(physical.get("rigid"))
        return out

    def _validate_text_details(self, text_details) -> list[dict]:
        """§4.1 第 5 条 + §1.3 的三条硬约束。"""
        if text_details is None:
            return []
        if not isinstance(text_details, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "text_details must be a list")
        normalized, seen = [], set()
        for detail in text_details:
            if not isinstance(detail, dict):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "each text_detail must be an object")
            surface = detail.get("surface")
            if not isinstance(surface, str) or surface == "":
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "text_detail.surface is required")
            if surface in seen:
                # 一个面只能有一段文字。两个 text_detail 抢同一个 surface，
                # 读路径就不知道返回哪一个（F5）。
                raise ServiceError(
                    422, "DUPLICATE_TEXT_SURFACE",
                    f"surface {surface!r} appears more than once",
                    details={"surface": surface})
            seen.add(surface)
            legibility = detail.get("legibility")
            if legibility not in LEGIBILITIES:
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"legibility must be one of {list(LEGIBILITIES)}")
            text = detail.get("canonical_text")
            text = "" if text is None else text
            if not isinstance(text, str):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "canonical_text must be a string")
            if legibility == "must_be_readable" and text.strip() == "":
                # 必须可读却没有字：QC 拿什么做 OCR 基准？这是请求自相矛盾，
                # 不是"尺寸不合法"，所以是 400 而不是 422。
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"surface {surface!r} is must_be_readable but has no "
                    f"canonical_text")
            sensitive = bool(detail.get("sensitive"))
            computed = _sha256_hex(text)
            supplied = detail.get("text_hash")
            if (isinstance(supplied, str) and len(supplied) == 64
                    and supplied != computed):
                # 调用方声明的指纹指向另一段文字。派生表里的 hash 会被当作
                # "这段文字的身份"，对不上就是 body 自相矛盾 → 400。
                # 本服务不接受"由调用方定义文字身份"。
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"text_hash for surface {surface!r} does not match "
                    f"sha256(canonical_text)")
            normalized.append({
                "surface": surface,
                "content_kind": str(detail.get("content_kind") or "printed"),
                "canonical_text": text,
                "language": str(detail.get("language") or "und"),
                "layout": detail.get("layout") or {},
                "legibility": legibility,
                "sensitive": sensitive,
                "text_hash": computed,
            })
        normalized.sort(key=lambda item: item["surface"])
        return normalized

    def _validate_variants(self, variant_axes, variant_manifest) -> tuple:
        """§4.1 第 2 条：`variant_axes` 每个取值都要有 artifact。"""
        if not isinstance(variant_axes, dict) or not variant_axes:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "variant_axes must be a non-empty object {axis: [values]} "
                "(the event contract requires at least one axis)")
        if not isinstance(variant_manifest, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "variant_manifest must be an object "
                               "{axis: {value: artifact_ref}}")
        axes: dict[str, list[str]] = {}
        for axis, values in variant_axes.items():
            if not isinstance(axis, str) or not axis:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "variant axis names must be non-empty")
            if not isinstance(values, list) or not values:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"variant axis {axis!r} must be a non-empty "
                                   f"list of values")
            seen = []
            for value in values:
                if not isinstance(value, str) or not value:
                    raise ServiceError(400, "MALFORMED_REQUEST",
                                       f"variant axis {axis!r} has a non-string "
                                       f"value")
                if value not in seen:
                    seen.append(value)
            axes[axis] = seen

        manifest: dict[str, dict[str, dict]] = {}
        for axis, values in variant_manifest.items():
            if not isinstance(values, dict):
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"variant_manifest[{axis!r}] must map value -> artifact")
            manifest[axis] = {}
            for value, ref in values.items():
                manifest[axis][value] = _artifact_ref(ref)

        for axis, values in axes.items():
            missing = [value for value in values
                       if value not in (manifest.get(axis) or {})]
            if missing:
                # 声明了 `broken` 却没有图，等于 S16 说"碎了"、画面画不出来
                # （§2.2 行 224）。
                raise ServiceError(
                    422, "VARIANT_COVERAGE_INCOMPLETE",
                    f"axis {axis!r} declares values with no artifact: "
                    f"{missing}",
                    details={"axis": axis, "missing_values": missing})
        return axes, manifest

    def _validate_artifacts(self, tenant_id: str, manifest: dict) -> None:
        """§4.1 第 1 条：每个 artifact 存在且 hash 一致。

        S28 超时 → 503 且**不落候选**（F13）。拿不到校验结论就存下来，
        等于把一个可能是伪造的 artifact_id 写进不可变 revision。
        """
        for axis in sorted(manifest):
            for value in sorted(manifest[axis]):
                ref = manifest[axis][value]
                artifact_id = ref.get("artifact_id")
                if not artifact_id:
                    raise ServiceError(
                        400, "MALFORMED_REQUEST",
                        f"variant {axis}:{value} has no artifact_id")
                artifact = self._upstream(
                    lambda aid=artifact_id: self.artifact_source(tenant_id, aid))
                if not artifact:
                    raise ServiceError(
                        422, "ARTIFACT_NOT_FOUND",
                        f"artifact {artifact_id!r} does not exist (or is not "
                        f"visible in this tenant)",
                        details={"axis": axis, "value": value,
                                 "artifact_id": artifact_id})
                actual = (artifact.get("content_hash")
                          or artifact.get("sha256"))
                declared = ref.get("content_hash")
                if declared and actual and str(declared) != str(actual):
                    raise ServiceError(
                        422, "ARTIFACT_HASH_MISMATCH",
                        f"artifact {artifact_id!r} has hash {actual}, the "
                        f"candidate declared {declared}",
                        details={"axis": axis, "value": value,
                                 "artifact_id": artifact_id,
                                 "expected": declared, "actual": actual})
                ref["content_hash"] = actual or declared

    def _validate_required_variants(self, tenant_id: str, prop_id: str,
                                    manifest: dict) -> None:
        """§2.3：外部登记的需求必须被本版覆盖。

        这张表的存在意义是让缺口**在资产侧**被发现，而不是等 S17 报画面不符。
        """
        rows = self.conn.execute(
            "SELECT axis, required_value, required_by FROM required_variant"
            " WHERE tenant_id=? AND prop_id=? ORDER BY axis, required_value,"
            " required_by", (tenant_id, prop_id)).fetchall()
        for row in rows:
            if row["required_value"] in (manifest.get(row["axis"]) or {}):
                continue
            raise ServiceError(
                422, "REQUIRED_VARIANT_MISSING",
                f"axis {row['axis']!r} needs {row['required_value']!r} "
                f"(registered by {row['required_by']})",
                details={"axis": row["axis"],
                         "value": row["required_value"],
                         "required_by": row["required_by"]})

    def _quality_gate(self, tenant_id: str, body: dict) -> dict:
        """读 QC 结论。**只读，不判**（§1.4、§9 第 7 条）。

        优先级：命令内联的 `quality_gate` → 配置了的 QC 源。都没有就返回 `{}`，
        于是 `text_ocr_pass` 记为 `null`——**不是 `true`**。这个 null 会在
        story_critical + must_be_readable 的道具升 canonical 时变成 422
        TEXT_OCR_INCOMPLETE（C14-10）。
        """
        inline = body.get("quality_gate")
        if isinstance(inline, dict):
            return inline
        if self.quality_gate_source is None:
            return {}
        ref = body.get("quality_gate_ref")
        if not ref:
            return {}
        return self._upstream(
            lambda: self.quality_gate_source(tenant_id, ref)) or {}

    # ------------------------------------------------------------ 命令：登记
    def create_prop(self, tenant_id: str, command_id: str,
                    body: dict) -> dict:
        """`POST /props`。§6.1 的自然幂等键是 (series_scope, display_name)。"""
        project_id = body.get("project_id")
        series_scope = body.get("series_scope")
        display_name = body.get("display_name")
        prop_kind = body.get("prop_kind")
        self._require(isinstance(project_id, str) and project_id,
                      "project_id is required")
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        self._require(isinstance(display_name, str) and display_name,
                      "display_name is required")
        self._require(prop_kind in PROP_KINDS,
                      f"prop_kind must be one of {list(PROP_KINDS)}")
        story_critical = bool(body.get("story_critical"))
        principal = str(body.get("principal_id") or tenant_id)

        def _produce():
            existing = _row(self.conn.execute(
                "SELECT * FROM prop WHERE tenant_id=? AND series_scope=?"
                " AND display_name=?", (tenant_id, series_scope,
                                        display_name)).fetchone())
            if existing is not None:
                # E12：重复登记返回既有 ID。两条记录同名同租户会让
                # `:lookup` 无解，也会让 S10 的引用分成两边。
                return _prop_out(existing, created=False)
            prop_id = _new_id("prop")
            self.conn.execute(
                "INSERT INTO prop (tenant_id, prop_id, project_id,"
                " series_scope, display_name, prop_kind, story_critical,"
                " status, canonical_revision, current_revision,"
                " text_fingerprint, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?, 'active', NULL, 0, NULL, ?, ?)",
                (tenant_id, prop_id, project_id, series_scope, display_name,
                 prop_kind, 1 if story_critical else 0,
                 _jcs({"principal_id": principal}), _now()))
            return _prop_out(self._load_prop(tenant_id, prop_id), created=True)

        return self._command(tenant_id, command_id, _produce)

    # ------------------------------------------------------------ 命令：候选
    def draft_candidate(self, tenant_id: str, command_id: str, prop_id: str,
                        body: dict) -> dict:
        """`POST /props/{id}/candidates`（§4.1）。事务外校验，事务内 CAS + 写。"""
        prop = self._load_prop(tenant_id, prop_id)
        if prop["status"] == "tombstoned":
            # §3.4：已下线道具不能再有新版本。
            raise ServiceError(422, "PROP_TOMBSTONED",
                               f"prop {prop_id} is tombstoned")
        self._cas(prop, body.get("expected_current_revision"))
        # 事务外先回收过期位：否则下面数到的 pending 里混着早已过期的死候选，
        # 用户会因为"看不见的 64 条"被 429（§3.1 的 ttl──► EXPIRED）。
        self._sweep_expired(tenant_id, prop_id)

        source_kind = body.get("source_kind")
        self._require(source_kind in SOURCE_KINDS,
                      f"source_kind must be one of {list(SOURCE_KINDS)}")
        rights_ref = body.get("rights_ref")
        self._require(isinstance(rights_ref, dict) and rights_ref,
                      "rights_ref must be a non-empty object (S14 stores the "
                      "pointer only; the verdict is S02's)")
        quality_gate_ref = body.get("quality_gate_ref")
        self._require(quality_gate_ref is not None,
                      "quality_gate_ref is required")
        parent_revision = body.get("parent_revision")
        if parent_revision is not None:
            self._require(isinstance(parent_revision, int)
                          and not isinstance(parent_revision, bool),
                          "parent_revision must be an integer")
            exists = self.conn.execute(
                "SELECT 1 FROM prop_revision WHERE tenant_id=? AND prop_id=?"
                " AND revision=?", (tenant_id, prop_id,
                                    parent_revision)).fetchone()
            if exists is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"parent revision {parent_revision} does not exist")

        physical = self._validate_physical(body.get("physical"))
        appearance = body.get("appearance")
        self._require(isinstance(appearance, dict),
                      "appearance must be an object")
        text_details = self._validate_text_details(body.get("text_details"))
        axes, manifest = self._validate_variants(
            body.get("variant_axes"), body.get("variant_manifest"))

        # 事务外：S28。它慢、会超时，且失败时不该占着写锁（F13）。
        self._validate_artifacts(tenant_id, manifest)
        self._validate_required_variants(tenant_id, prop_id, manifest)

        gate = self._quality_gate(tenant_id, body)
        prop_check = _prop_check(physical, gate, text_details)
        if prop_check["holder_feasibility"] == "fail":
            # E24-E2：结论由 QC 给，S14 只读并拒绝。
            raise ServiceError(
                422, "HOLDER_FEASIBILITY_FAIL",
                "the quality gate reports the prop cannot be held as designed; "
                "S14 does not judge this itself (plan §1.4)",
                details={"holder_feasibility": "fail"})
        if prop_check["variant_consistency"] == "fail":
            # E24-E3：broken 与 intact 画的不是同一把钥匙。
            raise ServiceError(
                422, "VARIANT_INCONSISTENT",
                "the quality gate reports the variant artifacts are not the "
                "same object", details={"variant_consistency": "fail"})
        if prop_check["text_ocr_pass"] is False:
            raise ServiceError(
                422, "TEXT_OCR_MISMATCH",
                "a must_be_readable surface does not match canonical_text "
                "according to the quality gate (S14 does not run OCR)",
                details={"surface": prop_check["text_ocr_surface"]})

        payload = {
            "physical": physical,
            "appearance": appearance,
            "text_details": text_details,
            "variant_manifest": manifest,
            "variant_axes": axes,
            "rights_ref": rights_ref,
            "source_kind": source_kind,
            "parent_revision": parent_revision,
        }
        digest = content_hash(payload)
        fingerprint = text_fingerprint(text_details)
        candidate_id = _new_id("candidate")
        now = _now()
        expires_at = (_now_dt() + CANDIDATE_TTL).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        def _produce():
            self._cas(self._load_prop(tenant_id, prop_id),
                      body.get("expected_current_revision"))
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM prop_candidate WHERE tenant_id=?"
                " AND prop_id=? AND state='PENDING'",
                (tenant_id, prop_id)).fetchone()[0]
            if pending >= self.max_pending_candidates:
                # C14-15：本地背压，不是业务配额。配额归 S23。
                raise ServiceError(
                    429, "WRITE_BACKPRESSURE",
                    f"prop {prop_id} already has {pending} pending candidates "
                    f"(limit {self.max_pending_candidates})",
                    retryable=True,
                    details={"pending": pending,
                             "limit": self.max_pending_candidates})
            self.conn.execute(
                "INSERT INTO prop_candidate (tenant_id, candidate_id, prop_id,"
                " content_hash, payload, prop_check, evaluation, state,"
                " created_by, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?, 'PENDING', ?, ?, ?)",
                (tenant_id, candidate_id, prop_id, digest,
                 _dumps(payload), _dumps(prop_check),
                 _dumps({"quality_gate": gate}),
                 _jcs({"principal_id": str(body.get("principal_id")
                                           or tenant_id)}),
                 now, expires_at))
            return {
                "candidate_id": candidate_id,
                "prop_id": prop_id,
                "content_hash": digest,
                "text_fingerprint": fingerprint,
                "prop_check": prop_check,
                "state": "PENDING",
                "expires_at": expires_at,
            }

        return self._command(tenant_id, command_id, _produce)

    def reject_candidate(self, tenant_id: str, command_id: str, prop_id: str,
                         candidate_id: str, body: dict) -> dict:
        self._load_prop(tenant_id, prop_id)
        # 过期落库必须在这个命令事务**之前**完成。写在这里面然后抛 409，
        # 回滚会把 EXPIRED 一起撤掉——§3.1 的 ttl 边永远不成立。
        self._sweep_expired(tenant_id, prop_id)

        def _produce():
            candidate = self._load_candidate(tenant_id, prop_id, candidate_id)
            if candidate["state"] != "PENDING":
                raise ServiceError(
                    409, "CANDIDATE_NOT_PENDING",
                    f"candidate {candidate_id} is {candidate['state']}")
            self.conn.execute(
                "UPDATE prop_candidate SET state='REJECTED' WHERE"
                " tenant_id=? AND candidate_id=?", (tenant_id, candidate_id))
            return {"candidate_id": candidate_id, "prop_id": prop_id,
                    "state": "REJECTED",
                    "reason_text": body.get("reason_text")}

        return self._command(tenant_id, command_id, _produce)

    def accept_candidate(self, tenant_id: str, command_id: str, prop_id: str,
                         candidate_id: str, body: dict) -> dict:
        """`POST /props/{id}/candidates/{cid}:accept`（§4.2 的九步）。

        锁顺序按 §6.3：prop → candidate → canonical_pointer → revision →
        text_detail → outbox。读也按这个顺序，SQLite 的 `BEGIN IMMEDIATE`
        让整段写事务串行。
        """
        self._load_prop(tenant_id, prop_id)
        # 同 reject：过期落库先于命令事务，否则 409 一回滚，候选永远 PENDING。
        self._sweep_expired(tenant_id, prop_id)
        decision_kind = body.get("decision_kind") or "human_approved"
        self._require(decision_kind in DECISION_KINDS,
                      f"decision_kind must be one of {list(DECISION_KINDS)}")
        set_canonical = bool(body.get("set_canonical"))
        impact_plan_ref = body.get("impact_plan_ref")
        reason_text = body.get("reason_text")
        principal = str(body.get("principal_id") or tenant_id)
        home_cell = str(body.get("home_cell") or "unknown")
        correlation_id = str(body.get("correlation_id") or command_id)

        def _produce():
            # 1) 锁 prop + CAS
            prop = self._load_prop(tenant_id, prop_id)
            if prop["status"] == "tombstoned":
                raise ServiceError(422, "PROP_TOMBSTONED",
                                   f"prop {prop_id} is tombstoned")
            self._cas(prop, body.get("expected_current_revision"))

            # 2) 锁 candidate
            candidate = self._load_candidate(tenant_id, prop_id, candidate_id)
            if candidate["state"] != "PENDING":
                raise ServiceError(
                    409, "CANDIDATE_NOT_PENDING",
                    f"candidate {candidate_id} is {candidate['state']}")
            if _expired(candidate["expires_at"]):
                # 上面 `_sweep_expired` 已在本事务之前落过库；能走到这里说明
                # 扫描之后、加锁之前刚好跨过截止线，直接拒掉即可。这里**不再
                # 写库**——在本事务里写然后抛 409 只会一起回滚。
                raise ServiceError(409, "CANDIDATE_NOT_PENDING",
                                   f"candidate {candidate_id} has expired")

            payload = _loads(candidate["payload"], {})
            prop_check = _loads(candidate["prop_check"], {})
            new_fp = text_fingerprint(payload.get("text_details") or [])
            changed = text_changed(prop["text_fingerprint"], new_fp)

            # 3) 文字变更的门。**不看 set_canonical**：§4.2 第 3 步没有这个条件，
            #    而文字一旦进库就已被 S10 引用，与它是不是 canonical 无关。
            if changed and decision_kind != "human_approved":
                raise ServiceError(
                    422, "TEXT_CHANGE_REQUIRES_HUMAN",
                    "changing canonical_text requires human_approved; "
                    "policy_auto is not allowed for text changes (plan §1.5)",
                    details={"previous_fingerprint": prop["text_fingerprint"],
                             "new_fingerprint": new_fp})
            if changed and prop["story_critical"] and impact_plan_ref is None:
                raise ServiceError(
                    422, "IMPACT_PLAN_REQUIRED",
                    "a story_critical prop changed its text; an impact plan "
                    "reference is required",
                    details={"text_fingerprint": new_fp})

            # 4) 升 canonical 且 story_critical：有"必须可读"的面时，OCR 结论
            #    必须是 true。`null`（没读过）也挡——"不知道对不对"不是"对"
            #    （C14-10）。没有可读面的版本不需要 OCR，放行。
            if set_canonical and prop["story_critical"]:
                if (prop_check.get("text_ocr_required")
                        and prop_check.get("text_ocr_pass") is not True):
                    raise ServiceError(
                        422, "TEXT_OCR_INCOMPLETE",
                        "a story_critical prop cannot become canonical without "
                        "a passing OCR conclusion for its readable surfaces",
                        details={"text_ocr_pass": prop_check.get("text_ocr_pass")})

            revision = int(prop["current_revision"]) + 1

            # 5) 插 revision。hash 冲突 = 同内容重复接受（F11）。
            try:
                self.conn.execute(
                    "INSERT INTO prop_revision (tenant_id, prop_id, revision,"
                    " content_hash, text_fingerprint, payload, physical,"
                    " appearance, text_details, variant_manifest, variant_axes,"
                    " rights_ref, source_kind, parent_revision,"
                    " quality_gate_ref, accepted_at, accepted_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, prop_id, revision, candidate["content_hash"],
                     new_fp, _dumps(payload),
                     _dumps(payload.get("physical") or {}),
                     _dumps(payload.get("appearance") or {}),
                     _dumps(payload.get("text_details") or []),
                     _dumps(payload.get("variant_manifest") or {}),
                     _dumps(payload.get("variant_axes") or {}),
                     _dumps(payload.get("rights_ref") or {}),
                     payload.get("source_kind"),
                     payload.get("parent_revision"),
                     _dumps(_loads(candidate["evaluation"], {})
                            .get("quality_gate") or {}),
                     _now(), _jcs({"principal_id": principal})))
            except sqlite3.IntegrityError as exc:
                if "prop_revision.content_hash" in str(exc):
                    raise ServiceError(
                        409, "DUPLICATE_PROP_CONTENT",
                        "this content has already been accepted for this "
                        "tenant (content_hash is unique per tenant)",
                        details={"content_hash": candidate["content_hash"]}
                    ) from exc
                raise

            # 6) 派生表。sensitive 的行**明文恒为 NULL**（§2.4、§9 第 9 条）。
            for detail in payload.get("text_details") or []:
                self.conn.execute(
                    "INSERT INTO prop_text_detail (tenant_id, prop_id,"
                    " revision, surface, content_kind, language, legibility,"
                    " sensitive, text_hash, text_plain) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, prop_id, revision, detail["surface"],
                     detail["content_kind"], detail["language"],
                     detail["legibility"], 1 if detail["sensitive"] else 0,
                     detail["text_hash"],
                     None if detail["sensitive"]
                     else detail["canonical_text"]))

            canonical_revision = prop["canonical_revision"]
            became_canonical = False
            # 7) 移动 canonical 指针（只在 set_canonical 时）
            if set_canonical:
                self.conn.execute(
                    "UPDATE canonical_pointer SET superseded_at=?"
                    " WHERE tenant_id=? AND prop_id=? AND superseded_at IS NULL",
                    (_now(), tenant_id, prop_id))
                self.conn.execute(
                    "INSERT INTO canonical_pointer (tenant_id, prop_id,"
                    " revision, text_fingerprint, decided_at, decided_by,"
                    " decision_kind, reason_text, prior_canonical,"
                    " superseded_at) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (tenant_id, prop_id, revision, new_fp, _now(),
                     _jcs({"principal_id": principal}), decision_kind,
                     reason_text, prop["canonical_revision"]))
                self.conn.execute(
                    "UPDATE prop SET current_revision=?, canonical_revision=?,"
                    " text_fingerprint=? WHERE tenant_id=? AND prop_id=?",
                    (revision, revision, new_fp, tenant_id, prop_id))
                canonical_revision = revision
                became_canonical = True
            else:
                self.conn.execute(
                    "UPDATE prop SET current_revision=? WHERE tenant_id=?"
                    " AND prop_id=?", (revision, tenant_id, prop_id))

            # 8) 候选状态
            self.conn.execute(
                "UPDATE prop_candidate SET state='ACCEPTED' WHERE"
                " tenant_id=? AND candidate_id=?", (tenant_id, candidate_id))

            # 9) 事件与事实同事务（§5.3、PRP-5：12 个键，一个不多）
            self.outbox.append(new_event(
                source=SOURCE, type=EVENT_PROP_REVISION_ACCEPTED,
                subject=f"prop/{prop_id}", tenant_id=tenant_id,
                home_cell=home_cell, aggregate_id=prop_id, sequence=revision,
                correlation_id=correlation_id, causation_id=command_id,
                data={
                    "schema_version": SCHEMA_VERSION,
                    "prop_ref": {"service_id": SOURCE, "id": prop_id,
                                 "revision": revision,
                                 "content_hash": candidate["content_hash"]},
                    "prop_kind": prop["prop_kind"],
                    "story_critical": bool(prop["story_critical"]),
                    "became_canonical": became_canonical,
                    "canonical_revision": canonical_revision,
                    "text_changed": changed,
                    "text_fingerprint": new_fp,
                    "variant_axes": payload.get("variant_axes") or {},
                    # 成功路径上恒为 true：未覆盖在第 4.1 步就 422 了。
                    # 契约已登记它可能是常量（PRP-5），照发不说谎。
                    "required_variants_covered": True,
                    "impact_plan_ref": impact_plan_ref,
                    "rights_ref": payload.get("rights_ref") or {},
                },
                data_ref={"service_id": SOURCE, "id": prop_id,
                          "revision": revision,
                          "content_hash": candidate["content_hash"]},
                schema_version=SCHEMA_VERSION))

            return {
                "prop_id": prop_id,
                "revision": revision,
                "candidate_id": candidate_id,
                "content_hash": candidate["content_hash"],
                "text_fingerprint": new_fp,
                "text_changed": changed,
                "became_canonical": became_canonical,
                "canonical_revision": canonical_revision,
                "state": "ACCEPTED",
            }

        return self._command(tenant_id, command_id, _produce)

    def _load_candidate(self, tenant_id: str, prop_id: str,
                        candidate_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM prop_candidate WHERE tenant_id=? AND prop_id=?"
            " AND candidate_id=?", (tenant_id, prop_id,
                                    candidate_id)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no candidate {candidate_id} for prop {prop_id}")
        return row

    # ------------------------------------------------------------ 命令：指针与状态
    def set_canonical(self, tenant_id: str, command_id: str, prop_id: str,
                      body: dict) -> dict:
        """`POST /props/{id}:set-canonical`（C14-7：**不发事件**）。

        方案把 canonical 的移动做成了独立命令，而 topics 里没有它的主题。
        按事件缓存 canonical 的消费方必须回读 `GET /props/{id}/canonical`——
        这条写在这里，是因为它是这个设计对下游的硬要求。
        """
        revision = body.get("revision")
        decision_kind = body.get("decision_kind") or "human_approved"
        self._require(isinstance(revision, int)
                      and not isinstance(revision, bool),
                      "revision must be an integer")
        self._require(decision_kind in DECISION_KINDS,
                      f"decision_kind must be one of {list(DECISION_KINDS)}")
        principal = str(body.get("principal_id") or tenant_id)

        def _produce():
            prop = self._load_prop(tenant_id, prop_id)
            if prop["status"] == "tombstoned":
                raise ServiceError(422, "PROP_TOMBSTONED",
                                   f"prop {prop_id} is tombstoned")
            target = _row(self.conn.execute(
                "SELECT * FROM prop_revision WHERE tenant_id=? AND prop_id=?"
                " AND revision=?", (tenant_id, prop_id, revision)).fetchone())
            if target is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no revision {revision} for prop {prop_id}")
            # §1.5：移动指针到另一段文字上，就是一次 canonical_text 变更。
            if (text_changed(prop["text_fingerprint"],
                             target["text_fingerprint"])
                    and decision_kind != "human_approved"):
                raise ServiceError(
                    422, "TEXT_CHANGE_REQUIRES_HUMAN",
                    "moving canonical to a revision with different text "
                    "requires human_approved (plan §1.5)",
                    details={"previous_fingerprint": prop["text_fingerprint"],
                             "new_fingerprint": target["text_fingerprint"]})
            if prop["canonical_revision"] == revision:
                # 幂等：已经在指的这一版，不重写也不报错。
                return {"prop_id": prop_id, "canonical_revision": revision,
                        "changed": False}
            self.conn.execute(
                "UPDATE canonical_pointer SET superseded_at=? WHERE tenant_id=?"
                " AND prop_id=? AND superseded_at IS NULL",
                (_now(), tenant_id, prop_id))
            self.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, prop_id, revision,"
                " text_fingerprint, decided_at, decided_by, decision_kind,"
                " reason_text, prior_canonical, superseded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                (tenant_id, prop_id, revision, target["text_fingerprint"],
                 _now(), _jcs({"principal_id": principal}), decision_kind,
                 body.get("reason_text"), prop["canonical_revision"]))
            self.conn.execute(
                "UPDATE prop SET canonical_revision=?, text_fingerprint=?"
                " WHERE tenant_id=? AND prop_id=?",
                (revision, target["text_fingerprint"], tenant_id, prop_id))
            return {"prop_id": prop_id, "canonical_revision": revision,
                    "prior_canonical": prop["canonical_revision"],
                    "text_fingerprint": target["text_fingerprint"],
                    "changed": True}

        return self._command(tenant_id, command_id, _produce)

    def change_status(self, tenant_id: str, command_id: str, prop_id: str,
                      target: str, body: dict) -> dict:
        """`:deprecate` / `:tombstone`。§3.2 的三态直线，`tombstoned` 不能复活
        （§3.4：复活走新 ID，否则旧绑定会指向一个"又活了"的对象）。"""
        self._require(target in ("deprecated", "tombstoned"),
                      "target status must be deprecated or tombstoned")

        def _produce():
            prop = self._load_prop(tenant_id, prop_id)
            current = prop["status"]
            allowed = ({"active"} if target == "deprecated"
                       else {"active", "deprecated"})
            if current not in allowed:
                raise ServiceError(
                    409, "INVALID_STATUS_TRANSITION",
                    f"cannot move prop {prop_id} from {current} to {target}",
                    details={"from": current, "to": target})
            self.conn.execute(
                "UPDATE prop SET status=? WHERE tenant_id=? AND prop_id=?",
                (target, tenant_id, prop_id))
            return {"prop_id": prop_id, "status": target,
                    "previous_status": current}

        return self._command(tenant_id, command_id, _produce)

    def register_required_variant(self, tenant_id: str, command_id: str,
                                  prop_id: str, body: dict) -> dict:
        """`POST /props/{id}/required-variants`（§2.3）。

        §6.1：主键含 `(axis, required_value, required_by)`，重复登记无副作用。
        需求方是谁未定（C14-2 / §11.2 第 1 条），所以 `required_by` 原样存字符串。
        """
        axis = body.get("axis")
        value = body.get("required_value")
        required_by = body.get("required_by")
        for name, item in (("axis", axis), ("required_value", value),
                           ("required_by", required_by)):
            self._require(isinstance(item, str) and item,
                          f"{name} is required")

        def _produce():
            prop = self._load_prop(tenant_id, prop_id)
            if prop["status"] == "tombstoned":
                raise ServiceError(422, "PROP_TOMBSTONED",
                                   f"prop {prop_id} is tombstoned")
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO required_variant (tenant_id, prop_id,"
                " axis, required_value, required_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (tenant_id, prop_id, axis, value, required_by, _now()))
            return {"prop_id": prop_id, "axis": axis, "required_value": value,
                    "required_by": required_by,
                    "created": cur.rowcount == 1}

        return self._command(tenant_id, command_id, _produce)

    # ------------------------------------------------------------ 读
    def get_prop(self, tenant_id: str, prop_id: str) -> dict:
        prop = self._load_prop(tenant_id, prop_id)
        pointer = _row(self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=? AND prop_id=?"
            " AND superseded_at IS NULL", (tenant_id, prop_id)).fetchone())
        required = [dict(r) for r in self.conn.execute(
            "SELECT axis, required_value, required_by, created_at FROM"
            " required_variant WHERE tenant_id=? AND prop_id=? ORDER BY axis,"
            " required_value, required_by", (tenant_id, prop_id)).fetchall()]
        return {
            "prop": _prop_out(prop, created=None),
            "canonical": _pointer_out(pointer),
            "required_variants": required,
            # E30-E1：S14 从不缓存权利状态，读就要复查。
            "rights_recheck_required": True,
        }

    def get_revision(self, tenant_id: str, prop_id: str, revision: int, *,
                     variant: str | None = None, principal: str | None = None,
                     scopes=()) -> dict:
        prop = self._load_prop(tenant_id, prop_id)
        row = _row(self.conn.execute(
            "SELECT * FROM prop_revision WHERE tenant_id=? AND prop_id=?"
            " AND revision=?", (tenant_id, prop_id, revision)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no revision {revision} for prop {prop_id}")
        axes = _loads(row["variant_axes"], {})
        manifest = _loads(row["variant_manifest"], {})
        selector = _parse_variant_selector(variant)
        artifacts: dict[str, dict] = {}
        for axis, value in selector.items():
            available = axes.get(axis)
            chosen = (manifest.get(axis) or {}).get(value)
            if not available or value not in available or chosen is None:
                # §4.3：**422，不静默退回默认**。退回默认会让 S17 拿着一张
                # 关着的钥匙图判"开着"通过。
                raise ServiceError(
                    422, "VARIANT_NOT_AVAILABLE",
                    f"variant {axis}:{value} is not available in revision "
                    f"{revision}",
                    details={"axis": axis, "value": value,
                             "available": available or []})
            artifacts[f"{axis}:{value}"] = chosen
        if not selector:
            for axis, values in sorted(axes.items()):
                for value in values:
                    ref = (manifest.get(axis) or {}).get(value)
                    if ref is not None:
                        artifacts[f"{axis}:{value}"] = ref

        privileged = self._is_privileged(tenant_id, principal, scopes)
        details = _loads(row["text_details"], [])
        return {
            "prop_ref": {"service_id": SOURCE, "id": prop_id,
                         "revision": revision,
                         "content_hash": row["content_hash"]},
            "variant": selector,
            "artifacts": artifacts,
            "physical": _loads(row["physical"], {}),
            "appearance": _loads(row["appearance"], {}),
            "text": [_text_out(d, privileged) for d in details],
            "text_fingerprint": row["text_fingerprint"],
            "variant_axes": axes,
            "rights_ref": _loads(row["rights_ref"], {}),
            "is_canonical": prop["canonical_revision"] == revision,
            "canonical_revision": prop["canonical_revision"],
            "rights_recheck_required": True,
        }

    def read_text(self, tenant_id: str, prop_id: str, revision: int, *,
                  surface: str | None = None, principal: str | None = None,
                  scopes=()) -> dict:
        """`GET /props/{id}/revisions/{revision}/text`（§4.3）。

        `redact=false` 这个查询参数**不构成授权**：特权只认平台签名的作用域。
        一个调用方能自报 `redact=false` 就会把这条脱敏做成装饰。
        """
        self._load_prop(tenant_id, prop_id)
        row = _row(self.conn.execute(
            "SELECT * FROM prop_revision WHERE tenant_id=? AND prop_id=?"
            " AND revision=?", (tenant_id, prop_id, revision)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no revision {revision} for prop {prop_id}")
        privileged = self._is_privileged(tenant_id, principal, scopes)
        details = _loads(row["text_details"], [])
        if surface is not None:
            chosen = [d for d in details if d.get("surface") == surface]
            if not chosen:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no text on surface {surface!r}")
            details = chosen
        return {"prop_id": prop_id, "revision": revision,
                "text_fingerprint": row["text_fingerprint"],
                "items": [_text_out(d, privileged) for d in details]}

    def get_canonical(self, tenant_id: str, prop_id: str) -> dict:
        self._load_prop(tenant_id, prop_id)
        pointer = _row(self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=? AND prop_id=?"
            " AND superseded_at IS NULL", (tenant_id, prop_id)).fetchone())
        if pointer is None:
            # 与"没有这个道具"分开：道具在、只是还没定 canonical。
            raise ServiceError(404, "CANONICAL_NOT_SET",
                               f"prop {prop_id} has no canonical revision")
        row = _row(self.conn.execute(
            "SELECT content_hash FROM prop_revision WHERE tenant_id=?"
            " AND prop_id=? AND revision=?",
            (tenant_id, prop_id, pointer["revision"])).fetchone())
        return {"prop_ref": {"service_id": SOURCE, "id": prop_id,
                             "revision": pointer["revision"],
                             "content_hash": (row or {}).get("content_hash")},
                "canonical": _pointer_out(pointer),
                "rights_recheck_required": True}

    def lookup(self, tenant_id: str, *, series_scope: str,
               alias: str) -> dict:
        """`GET /props:lookup`（C14-14：alias 就是 display_name 的精确匹配）。

        `display_name` 在 `(tenant_id, series_scope)` 内唯一，正好是查找键；
        没有第二张别名表，也没有模糊匹配——模糊匹配会让"两个道具名字很像"
        变成"有时查到这个有时查到那个"。
        """
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        self._require(isinstance(alias, str) and alias, "alias is required")
        row = _row(self.conn.execute(
            "SELECT * FROM prop WHERE tenant_id=? AND series_scope=?"
            " AND display_name=?", (tenant_id, series_scope, alias)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no prop named {alias!r} in {series_scope!r}")
        return {"prop": _prop_out(row, created=None),
                "rights_recheck_required": True}

    def list_props(self, tenant_id: str, *, series_scope: str,
                   status: str | None = None, limit: int = 50) -> dict:
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        clause = ["tenant_id=?", "series_scope=?"]
        params: list = [tenant_id, series_scope]
        if status:
            clause.append("status=?")
            params.append(status)
        params.append(max(1, min(int(limit), 200)))
        rows = self.conn.execute(
            "SELECT * FROM prop WHERE " + " AND ".join(clause)
            + " ORDER BY display_name, prop_id LIMIT ?", params).fetchall()
        return {"items": [_prop_out(dict(r), created=None) for r in rows]}

    # ------------------------------------------------------------ 敏感文字
    def _is_privileged(self, tenant_id: str, principal, scopes) -> bool:
        if SENSITIVE_READ_SCOPE in set(scopes or ()):
            return True
        if self.authorizer is None:
            return False
        # 没有平台签名的作用域时才问 S01。调用失败 → 503：敏感文字读不到
        # 结论时既不能放行也不能假装"没权限"，因为两种都不是已知事实。
        context = self._upstream(
            lambda: self.authorizer(tenant_id, principal)) or {}
        permissions = set(context.get("permissions") or [])
        denied = set(context.get("denied") or [])
        return SENSITIVE_READ_SCOPE in (permissions - denied)


# ------------------------------------------------------------ 纯函数：装配

def _artifact_ref(ref) -> dict:
    """`variant_manifest` 的叶子。允许 `"art_1"` 或 `{"artifact_id":..., "content_hash":...}`。"""
    if isinstance(ref, str):
        return {"artifact_id": ref, "content_hash": None}
    if isinstance(ref, dict):
        artifact_id = ref.get("artifact_id") or ref.get("id")
        return {"artifact_id": artifact_id,
                "content_hash": ref.get("content_hash") or ref.get("sha256")}
    return {"artifact_id": None, "content_hash": None}


def _prop_check(physical: dict, gate: dict, text_details: list[dict]) -> dict:
    """§2.5 的 `prop_check`：四个结论，**只记录、不生成**。

    `text_ocr_pass` 的三种取值都有意义：`True` 通过、`False` 不符（→422）、
    `None` 没有结论（→ 只有 story_critical 升 canonical 时才挡）。用 `None`
    而不是 `True` 兜底，是因为"没检查过"与"检查通过"在事故里是两个故事。
    """
    ocr_items = gate.get("text_ocr") or []
    by_surface: dict[str, object] = {}
    if isinstance(ocr_items, list):
        for item in ocr_items:
            if isinstance(item, dict) and item.get("surface"):
                by_surface[str(item["surface"])] = item
    readable = [d for d in text_details
                if d.get("legibility") == "must_be_readable"]
    pass_value = None
    failed_surface = None
    if not readable:
        # 没有"必须可读"的面，就没有 OCR 要覆盖的对象。这里**不**写
        # `text_ocr_pass = True`——那是"检查通过了"的读数，而实际什么都没检查
        # （一个没跑过的检查不能显示为绿）。改用 `text_ocr_required = False`
        # 表达"这一版不需要 OCR"，门禁据此放行。
        pass_value = None
    else:
        verdicts = []
        for detail in readable:
            item = by_surface.get(detail["surface"])
            if item is None:
                verdicts.append(None)
                continue
            if "pass" in item:
                ok = bool(item["pass"])
            else:
                # 结论以 hash 形式给：比对 canonical_text 的指纹。
                expected = detail["text_hash"]
                got = item.get("text_hash") or item.get("ocr_text_hash")
                ok = (got is not None and str(got) == expected)
            verdicts.append(ok)
            if ok is False:
                failed_surface = detail["surface"]
        if any(v is False for v in verdicts):
            pass_value = False
        elif all(v is True for v in verdicts):
            pass_value = True
        else:
            pass_value = None
    return {
        "text_ocr_pass": pass_value,
        "text_ocr_surface": failed_surface,
        # 本版有没有"必须可读"的面。False 时 `text_ocr_pass` 的 null 是
        # "不需要"而不是"没查"——两者在门禁里的含义相反（§4.2 第 4 步）。
        "text_ocr_required": bool(readable),
        # 尺寸范围已经校验过；这一项记的是"没有用大小档位糊弄"。
        "size_plausible": True,
        "variant_coverage": True,
        "holder_feasibility": gate.get("holder_feasibility"),
        "variant_consistency": gate.get("variant_consistency"),
    }


def _text_out(detail: dict, privileged: bool) -> dict:
    sensitive = bool(detail.get("sensitive"))
    out = {
        "surface": detail.get("surface"),
        "content_kind": detail.get("content_kind"),
        "language": detail.get("language"),
        "layout": detail.get("layout") or {},
        "legibility": detail.get("legibility"),
        "sensitive": sensitive,
        "text_fingerprint": detail.get("text_hash"),
    }
    if sensitive and not privileged:
        # §1.3 第 3 条：非特权调用方只拿得到"文字存在且是这一段"的证明。
        out["text"] = "[redacted]"
        out["redacted"] = True
    else:
        out["text"] = detail.get("canonical_text")
        out["redacted"] = False
    return out


def _prop_out(prop: dict, *, created) -> dict:
    out = {
        "prop_id": prop["prop_id"],
        "project_id": prop["project_id"],
        "series_scope": prop["series_scope"],
        "display_name": prop["display_name"],
        "prop_kind": prop["prop_kind"],
        "story_critical": bool(prop["story_critical"]),
        "status": prop["status"],
        "canonical_revision": prop["canonical_revision"],
        "current_revision": prop["current_revision"],
        "text_fingerprint": prop["text_fingerprint"],
        "created_at": prop["created_at"],
    }
    if created is not None:
        out["created"] = created
    return out


def _pointer_out(pointer) -> dict | None:
    if pointer is None:
        return None
    return {
        "revision": pointer["revision"],
        "text_fingerprint": pointer["text_fingerprint"],
        "decided_at": pointer["decided_at"],
        "decided_by": _loads(pointer["decided_by"], {}),
        "decision_kind": pointer["decision_kind"],
        "reason_text": pointer["reason_text"],
        "prior_canonical": pointer["prior_canonical"],
    }


def _parse_variant_selector(value: str | None) -> dict[str, str]:
    """`condition:broken,open_state:open` → `{"condition":"broken", ...}`。"""
    if not value:
        return {}
    selector: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"variant selector {part!r} must be axis:value")
        axis, _, chosen = part.partition(":")
        axis, chosen = axis.strip(), chosen.strip()
        if not axis or not chosen:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               f"variant selector {part!r} must be axis:value")
        if axis in selector and selector[axis] != chosen:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                f"variant axis {axis!r} was selected twice")
        selector[axis] = chosen
    return selector


def _expired(expires_at: str) -> bool:
    try:
        deadline = datetime.fromisoformat(
            str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return deadline <= _now_dt()
