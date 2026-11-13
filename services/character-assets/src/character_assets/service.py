"""S11 内核。方案：`plans/S11-character-assets.md`。

一句话边界：**S11 是"这个角色长什么样、哪一版算数、它的身份特征是什么"的
唯一权威；它不知道也不能知道"穿什么、在哪、什么伤、什么声音、能不能用"。**

四条结构性事实，代码里每一处判断都从它们推出来：

1. **canonical ≠ 最新**（§1.2）。`character.canonical_revision` 与
   `current_revision` 分开存；`set_canonical=false` 的接受让 revision 前进而
   指针不动。合成一列就等于"最新生成图自动成为全剧身份基准"——docs/10 原句
   禁止的那种漂移污染（§9 反模式 1）。
2. **候选在结构上不可绑定**（§1.3）。候选不在 `character_revision` 表，
   没有 `revision` / `canonical_eligible` / `accepted_at`。S10 的 `resolve`
   只走 revision 路径，所以"隔离"不是一句 if。
3. **漂移门只在 `policy_auto` 上开**（§2.5、§4.3 第 3 步）。
   `drift_class='major'` + `policy_auto` → 422。人审可以接受一次换人，
   策略不行——策略不接受"评审"这个环节，它接受的是"没人看也安全"。
4. **S11 不判定权利**（§0、§9 反模式 5）。`rights_ref` 是指针；
   `rights_recheck_required` 恒为 true。读路径永不缓存 S02 的结论。

事务形状与 S14/S25 同构：`BEGIN IMMEDIATE` → 业务事实 → `outbox.append`
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

SOURCE = "S11"
SCHEMA_VERSION = "2.0"

EVENT_CHARACTER_REVISION_ACCEPTED = "asset.character.revision.accepted"
# §4.2 第 7 步的本地事件：**不发布到契约 topic**（topics 里没有它）。
EVENT_CANDIDATE_CREATED = "asset.character.candidate.created"
TOPIC_CHARACTER = "drama.character-assets.v2"

# §2.4：候选默认 30 天过期。过期不删，改成 EXPIRED——"曾经提过又被放过"
# 本身是审计信息（§4.4）。
CANDIDATE_TTL = timedelta(days=30)

# §5.4 / C11-13：方案给了 429 但没说触发条件。按角色的 PENDING 候选数封顶，
# 只做本地背压，不是业务配额（配额归 S23）。
MAX_PENDING_CANDIDATES = 64

IDENTITY_KINDS = ("fictional", "actor_likeness", "real_person")
# §2.1 行 117：真人肖像类**必须**有 rights_ref。fictional 可以没有。
RIGHTED_IDENTITY_KINDS = ("actor_likeness", "real_person")
STATUSES = ("active", "deprecated", "tombstoned")
SOURCE_KINDS = ("manual_upload", "generated", "imported", "derived")
DECISION_KINDS = ("human_approved", "policy_auto", "initial_import")
CANDIDATE_STATES = ("PENDING", "ACCEPTED", "REJECTED", "EXPIRED")
ALIAS_KINDS = ("script_name", "nickname", "legacy_id", "import_alias")
REF_ROLES = ("base_for_lora", "derived_from", "merged_from")

# §4.2 第 2 步：front 永远必需；`require_full_turnaround` 时其余三个也要。
REQUIRED_VIEWS = ("front",)
FULL_TURNAROUND_VIEWS = ("side_left", "back", "three_quarter")
ALL_VIEWS = REQUIRED_VIEWS + FULL_TURNAROUND_VIEWS
# §2.2 行 141 的 view_manifest 形状。只认这四个键：多出来的键（比如
# `injured: true`）会让 S11 变成第二个剧情状态权威（§0.2 第 3 条）。
VIEW_NAMES = set(ALL_VIEWS)

# §2.3：分档字段的合法取值。**硬/软**的划分决定漂移等级（C11-23）：
# 硬锚点变了就是"换人"；软分档变了只是"同一人换了个镜头"。
IDENTITY_ENUMS = {
    "face_shape": ("oval", "round", "square", "heart", "long"),
    "jaw": ("soft", "defined", "angular"),
    "eye_shape": ("almond", "round", "hooded", "monolid"),
    "brow": ("straight", "arched", "thick", "thin"),
    "nose_bridge": ("low", "medium", "high"),
    "lip_fullness": ("thin", "medium", "full"),
    "skin_tone_band": ("1", "2", "3", "4", "5", "6"),
    "age_band": ("teen", "20s", "30s", "40s", "50s+"),
    "build": ("slim", "athletic", "average", "heavy"),
}
HAIR_ENUMS = {
    "length": ("short", "medium", "long"),
    "texture": ("straight", "wavy", "curly"),
    "hairline": ("straight", "widows_peak", "rounded"),
}
# 硬锚点：换掉之后模型认不出是同一个人（§1.1 的判据）。
HARD_IDENTITY_FIELDS = ("face_shape", "jaw", "eye_shape", "brow",
                        "nose_bridge", "lip_fullness", "hair",
                        "distinguishing_marks")
# 软分档：同一人的自然变化。只有这些变了是 minor，不是 major。
SOFT_IDENTITY_FIELDS = ("eye_distance_ratio", "skin_tone_band", "age_band",
                        "build", "height_cm_band")


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


# ------------------------------------------------------------ 纯函数：内容

def content_hash(payload: dict) -> str:
    """§2.2 行 139：`sha256(JCS(payload))`。

    不含 `character_id` / `revision` / `accepted_at`：内容寻址的意义正是
    "同样的内容无论何时、给谁接受，hash 都一样"。索引是租户级的
    （`cr_hash_uniq`），所以同租户里两个角色内容一字不差会被判为重复
    （C11-16）——这是方案的索引决定的，不是这里的选择。
    """
    return _sha256_hex(_jcs(payload))


def identity_feature_hash(features: dict) -> str:
    """§2.2 行 145：`sha256(JCS(identity_features))`。——漂移检测的锚。

    `distinguishing_marks` 按 `(kind, location)` 排序后再算：§1.3 说标记是
    身份锚，而"同一组痣换个数组顺序"不是身份变化。不排序的话，同一个角色
    的第一版与第二版会仅仅因为客户端枚举顺序不同就报 major 漂移。
    """
    normalized = dict(features)
    marks = normalized.get("distinguishing_marks")
    if isinstance(marks, list):
        normalized["distinguishing_marks"] = sorted(
            marks, key=lambda mark: (str(mark.get("kind") or ""),
                                     str(mark.get("location") or ""),
                                     str(mark.get("size_mm_band") or "")))
    return _sha256_hex(_jcs(normalized))


def classify_drift(old_features, new_features) -> dict:
    """§2.5 的 `drift_class`：`none` / `minor` / `major`（C11-23）。

    方案只说"要结构化、可用 hash 比对"，没给硬/软划分与阈值。这里把算法
    写死在纯函数里，使任何实现都能复算，也使它可被测试穷举：

    - 任一**硬锚点**（`HARD_IDENTITY_FIELDS`）不同 → `major`。硬锚点是
      §1.1 判据下"换了就认不出"的那几项。
    - 只有**软分档**（`SOFT_IDENTITY_FIELDS`）不同 → `minor`。
    - 没有差异 → `none`。

    不引入任何数值阈值：§2.3 明确要求分档而非精确浮点，加阈值会让
    "0.31 vs 0.315"变成一场争议。
    """
    old = old_features or {}
    new = new_features or {}
    changed = sorted(key for key in set(old) | set(new)
                     if _comparable(old.get(key)) != _comparable(new.get(key)))
    if not changed:
        return {"drift_class": "none", "changed_fields": []}
    hard = [key for key in changed if key in HARD_IDENTITY_FIELDS]
    soft = [key for key in changed if key in SOFT_IDENTITY_FIELDS]
    unknown = [key for key in changed
               if key not in HARD_IDENTITY_FIELDS
               and key not in SOFT_IDENTITY_FIELDS]
    if hard:
        return {"drift_class": "major", "changed_fields": changed,
                "hard_fields": hard}
    if unknown:
        # 未知字段（§2.3 允许新增可选字段）不能自动当成 soft：一个未来的
        # "骨骼点云 hash"字段落在 soft 里，就等于把换人放行成 minor。
        # 保守判 major，并由人审决定——这不是对 owner 的决定代答，
        # 是 §9 反模式 7/1 在未知输入上的安全缺省。
        return {"drift_class": "major", "changed_fields": changed,
                "unknown_fields": unknown}
    return {"drift_class": "minor", "changed_fields": changed,
            "soft_fields": soft}


def _comparable(value):
    """比较用的规范化：list 顺序无关，dict 按 JCS。"""
    if isinstance(value, list):
        return sorted((_comparable(item) for item in value),
                      key=lambda item: _jcs(item) if not isinstance(
                          item, str) else item)
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items()}
    return value


# ------------------------------------------------------------ 服务

class CharacterAssetsService:
    def __init__(self, conn: sqlite3.Connection, *, artifact_source,
                 quality_gate_source=None,
                 max_pending_candidates: int = MAX_PENDING_CANDIDATES):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # `Outbox.__init__` 会 executescript 建表，而 sqlite3 的 executescript
        # 隐式提交当前事务。只能在事务外建一次，之后复用同一实例，否则命令
        # 事务会在第一条事件写入时被悄悄提交，后面的 ROLLBACK 失效。
        self.outbox = Outbox(self.conn)
        # S28：**必需**。没有 artifact 校验就等于没有资产正确性（F1、E29-B2）。
        self.artifact_source = artifact_source
        # S35/S36 的 QC 结论：**可选**。仓库里没有任何服务产出方案 §4.2
        # 说的"身份一致性、多角度一致性"结论（C11-11），所以缺省不配置：
        # 结论随命令内联传入，读不到就记空对象，绝不用 pass=true 兜底。
        self.quality_gate_source = quality_gate_source
        self.max_pending_candidates = max_pending_candidates
        # 没有 authorizer、没有模型连接器、没有 S02 客户端：
        # §4.1 行 379 "S11 不生成图片"、§9 反模式 5/11。依赖表里少一个
        # 能力，是最强的"它不做这件事"声明。

    # ------------------------------------------------------------ 事务助手
    def _sweep_expired(self, tenant_id: str, character_id: str | None = None) -> int:
        """把过期的 PENDING 候选落成 EXPIRED（§3.1 的 `ttl──► EXPIRED`）。

        **必须走自己的事务**，不能塞进命令事务里"顺便标一下"：命令随后
        抛 409，`Transaction` 会回滚，那条 UPDATE 也就没了。告警说候选
        过期了、库里却永远还是 PENDING——下一次 draft 的 64 上限照样数它，
        用户会因为一堆看不见的死候选被 429。

        放在 `draft_candidate` 开头（事务外）而不是后台线程：这个服务没有
        调度器，而"要新增候选"正是最该回收旧位的时刻。
        """
        sql = ("UPDATE character_candidate SET state='EXPIRED'"
               " WHERE tenant_id=? AND state='PENDING' AND expires_at<=?")
        params: tuple = (tenant_id, _now())
        if character_id is not None:
            sql += " AND character_id=?"
            params += (character_id,)
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
        """上游不可达 → 503。**绝不拿缓存的结论兜底**（F10）。

        资产比路由更不该凑合：一个校验不到的 artifact 就是一张可能画错的
        身份参考图，而它会被 S26 拿去生成全剧的所有镜头。
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
    def _load_character(self, tenant_id: str, character_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM character WHERE tenant_id=? AND character_id=?",
            (tenant_id, character_id)).fetchone())
        if row is None:
            # 跨租户与不存在同形（F11/E28）：确认"存在但你没权限"本身就是泄漏。
            # 消息里只回显调用方**自己传进来**的那个 id，不带任何本租户字段。
            raise ServiceError(404, "NOT_FOUND", f"no character {character_id}")
        return row

    def _cas(self, character: dict, expected) -> None:
        if expected is None:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "expected_current_revision is required: this service never "
                "overwrites a revision it has not seen")
        if int(expected) != int(character["current_revision"]):
            raise ServiceError(
                409, "VERSION_CONFLICT",
                f"character {character['character_id']} is at revision "
                f"{character['current_revision']}, expected {expected}",
                retryable=True,
                details={"expected": int(expected),
                         "current": int(character["current_revision"])})

    @staticmethod
    def _require(condition, message: str) -> None:
        if not condition:
            raise ServiceError(400, "MALFORMED_REQUEST", message)

    def _require_tombstoned_ok(self, character: dict) -> None:
        if character["status"] == "tombstoned":
            # §3.3：已下线角色不应有新的身份基准（F9）。
            raise ServiceError(
                422, "CHARACTER_TOMBSTONED",
                f"character {character['character_id']} is tombstoned")

    def _validate_identity_features(self, features) -> dict:
        """§2.3：结构化的分档值，**不是自由文本**（§9 反模式 7）。

        校验规则：
        - 已知字段的取值必须落在枚举里；
        - `eye_distance_ratio` 必须是 0–1 的数值；
        - `distinguishing_marks` 的每一项必须是对象，且带 `kind` 与
          `location`（"有一颗痣"没有位置就不是身份锚）；
        - **未知字段原样保留**（§2.3：新增字段必须可选，旧 revision 才能解析；
          E01-B1 靠这一条）。但它们会参与漂移判定并被保守判为 major（C11-23）。
        """
        if not isinstance(features, dict) or not features:
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                "identity_features must be a non-empty object (structured, "
                "banded values; free text cannot be compared or regressed)")
        known = (set(IDENTITY_ENUMS)
                 | {"hair", "distinguishing_marks", "eye_distance_ratio",
                    "height_cm_band"})
        if not (set(features) & known):
            # {"description": "一个漂亮的女孩"} 结构上是个对象，但它身上没有
            # 任何可比对的锚点：这类候选一旦接受，漂移检测只能永远返回 none。
            # §2.3 要的是结构化特征，§9 反模式 7 点名自由文本——所以至少要有
            # 一个已知字段。未知字段**可以**共存（E01-B1），但不能只有未知字段。
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                f"identity_features has none of the recognized identity fields "
                f"{sorted(known)}; a free-text description is not an identity "
                f"feature (plan §2.3, §9 anti-pattern 7)")
        unknown = sorted(key for key in features if key not in known)
        for key, allowed in IDENTITY_ENUMS.items():
            if key not in features:
                continue
            value = features[key]
            if value not in allowed:
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"identity_features.{key} must be one of {list(allowed)}, "
                    f"got {value!r} (banded values only, plan §2.3)",
                    details={"field": key})
        ratio = features.get("eye_distance_ratio")
        if ratio is not None:
            if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                    or not (0 <= float(ratio) <= 1)):
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    "identity_features.eye_distance_ratio must be a number in "
                    "[0, 1]")
        hair = features.get("hair")
        if hair is not None:
            if not isinstance(hair, dict):
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   "identity_features.hair must be an object")
            for key, allowed in HAIR_ENUMS.items():
                if key in hair and hair[key] not in allowed:
                    raise ServiceError(
                        400, "MALFORMED_REQUEST",
                        f"identity_features.hair.{key} must be one of "
                        f"{list(allowed)}")
        height_band = features.get("height_cm_band")
        if height_band is not None and not isinstance(height_band, str):
            # 区间是字符串（`"160-165"`）：§2.3 要分档，精确浮点会诱导
            # 像素级比对。
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "identity_features.height_cm_band must be a "
                               "band string such as '160-165'")
        marks = features.get("distinguishing_marks")
        if marks is not None:
            if not isinstance(marks, list):
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    "identity_features.distinguishing_marks must be a list")
            for mark in marks:
                if (not isinstance(mark, dict) or not mark.get("kind")
                        or not mark.get("location")):
                    raise ServiceError(
                        400, "MALFORMED_REQUEST",
                        "each distinguishing_mark needs kind and location "
                        "(a mark without a location is not an identity anchor)")
        # 未知字段保留，但列表里记下名字，便于 runbook 审计（不影响返回值）。
        _ = unknown
        return features

    def _validate_view_manifest(self, view_manifest,
                                require_full_turnaround: bool) -> dict:
        """§4.2 第 2 步：front 必有；全转面时其余三个也要。"""
        if not isinstance(view_manifest, dict) or not view_manifest:
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "view_manifest must be a non-empty object")
        unknown = sorted(set(view_manifest) - VIEW_NAMES)
        if unknown:
            # §0.2 第 3 条：`injured` 这类剧情键进 view_manifest，就是 S11
            # 在记剧情状态。宁可拒，也不猜它是不是一个"新视图"。
            raise ServiceError(
                400, "MALFORMED_REQUEST",
                f"view_manifest has unknown keys {unknown}; only "
                f"{sorted(VIEW_NAMES)} are identity views "
                f"(costume/story state belong to S12/S16)",
                details={"unknown_views": unknown})
        required = list(REQUIRED_VIEWS)
        if require_full_turnaround:
            required += list(FULL_TURNAROUND_VIEWS)
        missing = [name for name in required if name not in view_manifest]
        if missing:
            # F3 / §4.2 第 2 步。
            raise ServiceError(
                422, "VIEW_MANIFEST_INCOMPLETE",
                f"view_manifest is missing required views {missing}",
                details={"missing": missing})
        out: dict[str, dict] = {}
        for name in ALL_VIEWS:
            if name not in view_manifest:
                continue
            out[name] = _artifact_ref(view_manifest[name])
        return out

    def _validate_artifacts(self, tenant_id: str, refs) -> None:
        """§4.2 第 1 步：每个 artifact 存在且 hash 一致。

        S28 超时 → 503 且**不落候选**（F10）。拿不到校验结论就存下来，
        等于把一个可能是伪造的 artifact_id 写进不可变 revision。
        """
        for label, ref in refs:
            artifact_id = ref.get("artifact_id")
            if not artifact_id:
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    f"{label} has no artifact_id")
            try:
                artifact = self._upstream(
                    lambda aid=artifact_id: self.artifact_source(tenant_id, aid))
            except ServiceError as exc:
                if exc.status == 404:
                    # 适配器把 404 翻成 None；但如果上游把 404 原样抛出来，
                    # 语义是同一个："这个租户看不到这个 artifact"。两种形状
                    # 必须落成同一个 422，否则结果取决于适配器写法。
                    artifact = None
                else:
                    raise
            if not artifact:
                # F1 / E29-B2。不存在与"存在但你无权"同形——区分就是泄漏。
                raise ServiceError(
                    422, "ARTIFACT_NOT_FOUND",
                    f"artifact {artifact_id!r} does not exist (or is not "
                    f"visible in this tenant)",
                    details={"ref": label, "artifact_id": artifact_id})
            actual = artifact.get("content_hash") or artifact.get("sha256")
            declared = ref.get("content_hash")
            if declared and actual and str(declared) != str(actual):
                # F2
                raise ServiceError(
                    422, "ARTIFACT_HASH_MISMATCH",
                    f"artifact {artifact_id!r} has hash {actual}, the "
                    f"candidate declared {declared}",
                    details={"ref": label, "artifact_id": artifact_id,
                             "expected": declared, "actual": actual})
            ref["content_hash"] = actual or declared

    def _validate_rights_ref(self, identity_kind: str, rights_ref) -> dict:
        """§2.1 行 117 / §4.2 第 3 步：真人必须有 rights_ref。

        **只校验形态，不判定有效性**（§9 反模式 5）。verdict 归 S02。
        """
        if rights_ref is None:
            rights_ref = {}
        if not isinstance(rights_ref, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "rights_ref must be an object")
        if identity_kind in RIGHTED_IDENTITY_KINDS and not rights_ref.get(
                "rights_id"):
            # F4 / E30-B2。这一步必须在这里挡住：等接受时再挡，候选已经
            # 落库，管理面会显示一个"看起来可以接受"的真人角色版本。
            raise ServiceError(
                422, "RIGHTS_REF_REQUIRED",
                f"identity_kind={identity_kind!r} requires a rights_ref with "
                f"a rights_id (S11 stores the pointer; the verdict is S02's)",
                details={"identity_kind": identity_kind})
        return rights_ref

    def _validate_lora_refs(self, lora_refs) -> list[dict]:
        """§2.2 行 147：`[{artifact_id, base_snapshot, revision}]`，来自 S48。

        **不自动接受**（§5.3 消费表）。这里只是把它们存进候选，等有人
        接受 revision 时才成为这一版的引用。
        """
        if lora_refs is None:
            return []
        if not isinstance(lora_refs, list):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "lora_refs must be a list")
        out = []
        for ref in lora_refs:
            if not isinstance(ref, dict) or not ref.get("artifact_id"):
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    "each lora_ref needs an artifact_id")
            leaf = _artifact_ref(ref)
            # §2.2 行 147 的形状是 `[{artifact_id, base_snapshot, revision}]`。
            # `_artifact_ref` 只留 id/hash（它服务于 view_manifest），所以这里
            # 要把 LoRA 特有的两键补回去——否则接受时写不出 base_for_lora 边
            # （C11-22），LoRA 就退化成 payload 里一段无法引用的字符串。
            if ref.get("revision") is not None:
                leaf["revision"] = ref["revision"]
            if ref.get("base_snapshot") is not None:
                leaf["base_snapshot"] = ref["base_snapshot"]
            out.append(leaf)
        return out

    def _validate_expressions(self, expression_manifest) -> dict:
        """§2.2 行 142：`{neutral: artifact_ref, ...}`。

        枚举**不收敛**（C11-20 / §11.2 第 6 条）：这个服务不发明表情名，
        只要求键非空、值是可解析的 artifact 引用。
        """
        if expression_manifest is None:
            return {}
        if not isinstance(expression_manifest, dict):
            raise ServiceError(400, "MALFORMED_REQUEST",
                               "expression_manifest must be an object")
        out = {}
        for name, ref in expression_manifest.items():
            if not isinstance(name, str) or not name:
                raise ServiceError(
                    400, "MALFORMED_REQUEST",
                    "expression names must be non-empty strings")
            out[name] = _artifact_ref(ref)
        return out

    def _quality_gate(self, tenant_id: str, body: dict) -> dict:
        """读 QC 结论。**只读，不判**（§9 反模式 5 的同一逻辑）。

        优先级：命令内联的 `quality_gate` → 配置了的 QC 源。都没有就返回 `{}`。
        结论为失败时起草直接 422，不落候选（E24-B1）。
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
    def create_character(self, tenant_id: str, command_id: str,
                         body: dict) -> dict:
        """`POST /characters`。§6.1 的自然幂等键是 (series_scope, display_name)。"""
        project_id = body.get("project_id")
        series_scope = body.get("series_scope")
        display_name = body.get("display_name")
        identity_kind = body.get("identity_kind")
        self._require(isinstance(project_id, str) and project_id,
                      "project_id is required")
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        self._require(isinstance(display_name, str) and display_name,
                      "display_name is required")
        self._require(identity_kind in IDENTITY_KINDS,
                      f"identity_kind must be one of {list(IDENTITY_KINDS)}")
        principal = str(body.get("principal_id") or tenant_id)
        # 登记时允许携带 rights_ref：真人角色的权利指针**在登记那一刻**
        # 就要有，不能等到起草候选才发现缺（F4 在起草时仍会复核）。
        rights_ref = body.get("rights_ref") or {}

        def _produce():
            existing = _row(self.conn.execute(
                "SELECT * FROM character WHERE tenant_id=? AND series_scope=?"
                " AND display_name=?", (tenant_id, series_scope,
                                        display_name)).fetchone())
            if existing is not None:
                # E12-B3：重复登记返回既有 ID。两条同名记录会让 :lookup
                # 无解，也会让 S10 的引用分成两边。
                return _character_out(existing, created=False)
            character_id = _new_id("character")
            self.conn.execute(
                "INSERT INTO character (tenant_id, character_id, project_id,"
                " series_scope, display_name, identity_kind, status,"
                " canonical_revision, current_revision, series_of_record,"
                " created_by, created_at)"
                " VALUES (?,?,?,?,?,?, 'active', NULL, 0, ?, ?, ?)",
                (tenant_id, character_id, project_id, series_scope,
                 display_name, identity_kind, series_scope,
                 _jcs({"principal_id": principal, "rights_ref": rights_ref}),
                 _now()))
            return _character_out(self._load_character(tenant_id, character_id),
                                  created=True)

        return self._command(tenant_id, command_id, _produce)

    # ------------------------------------------------------------ 命令：候选
    def draft_candidate(self, tenant_id: str, command_id: str,
                        character_id: str, body: dict) -> dict:
        """`POST /characters/{id}/candidates`（§4.2）。事务外校验，事务内 CAS + 写。"""
        character = self._load_character(tenant_id, character_id)
        self._require_tombstoned_ok(character)
        self._cas(character, body.get("expected_current_revision"))
        # 事务外先回收过期位：否则下面数到的 pending 里混着早已过期的死候选，
        # 用户会因为"看不见的 64 条"被 429（§3.1 的 ttl──► EXPIRED）。
        self._sweep_expired(tenant_id, character_id)

        source_kind = body.get("source_kind")
        self._require(source_kind in SOURCE_KINDS,
                      f"source_kind must be one of {list(SOURCE_KINDS)}")
        parent_revision = body.get("parent_revision")
        if parent_revision is not None:
            self._require(isinstance(parent_revision, int)
                          and not isinstance(parent_revision, bool),
                          "parent_revision must be an integer")
            exists = self.conn.execute(
                "SELECT 1 FROM character_revision WHERE tenant_id=?"
                " AND character_id=? AND revision=?",
                (tenant_id, character_id, parent_revision)).fetchone()
            if exists is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"parent revision {parent_revision} does not exist")

        features = self._validate_identity_features(
            body.get("identity_features"))
        body_profile = body.get("body_profile")
        self._require(isinstance(body_profile, dict),
                      "body_profile must be an object "
                      "{height_cm_band, build, age_band}")
        view_manifest = self._validate_view_manifest(
            body.get("view_manifest"),
            bool(body.get("require_full_turnaround")))
        expression_manifest = self._validate_expressions(
            body.get("expression_manifest"))
        rights_ref = self._validate_rights_ref(character["identity_kind"],
                                               body.get("rights_ref"))
        lora_refs = self._validate_lora_refs(body.get("lora_refs"))
        quality_gate_ref = body.get("quality_gate_ref")
        self._require(quality_gate_ref is not None,
                      "quality_gate_ref is required")

        # 事务外：S28。它慢、会超时，且失败时不该占着写锁（F10）。
        refs = [(f"view:{name}", ref) for name, ref in view_manifest.items()]
        refs += [(f"expression:{name}", ref)
                 for name, ref in expression_manifest.items()]
        self._validate_artifacts(tenant_id, refs)

        gate = self._quality_gate(tenant_id, body)
        if gate.get("identity_consistency") == "fail":
            # E24-B1：结论由 QC 给，S11 只读并拒。
            raise ServiceError(
                422, "CANDIDATE_QC_FAILED",
                "the quality gate reports the candidate is not the same "
                "person; S11 does not judge this itself (plan §1.1)",
                details={"identity_consistency": "fail"})
        inconsistent = gate.get("view_consistency")
        if inconsistent == "fail" or (isinstance(inconsistent, dict)
                                      and inconsistent.get("views")):
            # E24-B2：多角度一致性失败时必须指明**哪个视图**，否则人审
            # 只能靠肉眼在四张图里找不同。
            bad = (inconsistent.get("views")
                   if isinstance(inconsistent, dict) else None)
            raise ServiceError(
                422, "IDENTITY_VIEW_INCONSISTENT",
                "the quality gate reports the views do not depict the same "
                "identity",
                details={"views": list(bad or [])})

        payload = {
            "view_manifest": view_manifest,
            "expression_manifest": expression_manifest,
            "body_profile": body_profile,
            "identity_features": features,
            "rights_ref": rights_ref,
            "lora_refs": lora_refs,
            "source_kind": source_kind,
            "parent_revision": parent_revision,
            "quality_gate_ref": quality_gate_ref,
        }
        digest = content_hash(payload)
        feature_hash = identity_feature_hash(features)
        canonical_features = self._canonical_identity_features(
            tenant_id, character_id)
        drift = classify_drift(canonical_features, features) if \
            canonical_features is not None else {
                "drift_class": "none", "changed_fields": [],
                "reason": "no_canonical_yet"}
        candidate_id = _new_id("candidate")
        now = _now()
        expires_at = (_now_dt() + CANDIDATE_TTL).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        def _produce():
            self._cas(self._load_character(tenant_id, character_id),
                      body.get("expected_current_revision"))
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM character_candidate WHERE tenant_id=?"
                " AND character_id=? AND state='PENDING'",
                (tenant_id, character_id)).fetchone()[0]
            if pending >= self.max_pending_candidates:
                # C11-13：本地背压，不是业务配额。配额归 S23。
                raise ServiceError(
                    429, "WRITE_BACKPRESSURE",
                    f"character {character_id} already has {pending} pending "
                    f"candidates (limit {self.max_pending_candidates})",
                    retryable=True,
                    details={"pending": pending,
                             "limit": self.max_pending_candidates})
            self.conn.execute(
                "INSERT INTO character_candidate (tenant_id, candidate_id,"
                " character_id, content_hash, payload, evaluation,"
                " identity_drift, state, created_by, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?, 'PENDING', ?, ?, ?)",
                (tenant_id, candidate_id, character_id, digest,
                 _dumps(payload), _dumps({"quality_gate": gate}),
                 _dumps(drift),
                 _jcs({"principal_id": str(body.get("principal_id")
                                           or tenant_id)}),
                 now, expires_at))
            # §4.2 第 7 步：本地事件，**不发布**到契约 topic（topics 里
            # 没有它，见 C11-4）。写 outbox 是为了重启后能补审计，
            # partition 语义与正式事件一致。
            self.outbox.append(new_event(
                source=SOURCE, type=EVENT_CANDIDATE_CREATED,
                subject=f"character/{character_id}", tenant_id=tenant_id,
                home_cell=str(body.get("home_cell") or "unknown"),
                aggregate_id=character_id,
                sequence=int(self._load_character(tenant_id, character_id
                                                  )["current_revision"]),
                correlation_id=str(body.get("correlation_id") or command_id),
                causation_id=command_id,
                data={"schema_version": SCHEMA_VERSION,
                      "candidate_id": candidate_id,
                      "character_id": character_id,
                      "content_hash": digest,
                      "identity_feature_hash": feature_hash,
                      "drift_class": drift["drift_class"]},
                schema_version=SCHEMA_VERSION))
            return {
                "candidate_id": candidate_id,
                "character_id": character_id,
                "content_hash": digest,
                "identity_feature_hash": feature_hash,
                "identity_drift": drift,
                "state": "PENDING",
                "expires_at": expires_at,
            }

        return self._command(tenant_id, command_id, _produce)

    def _canonical_identity_features(self, tenant_id: str,
                                     character_id: str):
        """当前 canonical 那一版的 identity_features；没有 canonical 则 None。"""
        pointer = _row(self.conn.execute(
            "SELECT revision FROM canonical_pointer WHERE tenant_id=?"
            " AND character_id=? AND superseded_at IS NULL",
            (tenant_id, character_id)).fetchone())
        if pointer is None:
            return None
        row = self.conn.execute(
            "SELECT identity_features FROM character_revision WHERE"
            " tenant_id=? AND character_id=? AND revision=?",
            (tenant_id, character_id, pointer["revision"])).fetchone()
        if row is None:
            return None
        return _loads(row["identity_features"], {})

    def reject_candidate(self, tenant_id: str, command_id: str,
                         character_id: str, candidate_id: str,
                         body: dict) -> dict:
        self._load_character(tenant_id, character_id)
        # 过期落库必须在这个命令事务**之前**完成。写在这里面然后抛 409，
        # 回滚会把 EXPIRED 一起撤掉——§3.1 的 ttl 边永远不成立。
        self._sweep_expired(tenant_id, character_id)

        def _produce():
            candidate = self._load_candidate(tenant_id, character_id,
                                             candidate_id)
            if candidate["state"] != "PENDING":
                raise ServiceError(
                    409, "CANDIDATE_NOT_PENDING",
                    f"candidate {candidate_id} is {candidate['state']}",
                    details={"candidate_id": candidate_id,
                             "state": candidate["state"]})
            self.conn.execute(
                "UPDATE character_candidate SET state='REJECTED' WHERE"
                " tenant_id=? AND candidate_id=?", (tenant_id, candidate_id))
            return {"candidate_id": candidate_id,
                    "character_id": character_id, "state": "REJECTED",
                    "reason_text": body.get("reason_text")}

        return self._command(tenant_id, command_id, _produce)

    def accept_candidate(self, tenant_id: str, command_id: str,
                         character_id: str, candidate_id: str,
                         body: dict) -> dict:
        """`POST /characters/{id}/candidates/{cid}:accept`（§4.3 的八步）。

        锁顺序按 §6.3：character → candidate → canonical_pointer → revision
        → reference → outbox。但**语句顺序**按 §4.3：revision 必须先于
        canonical_pointer 插入——指针有指向 revision 的外键（C11-15）。
        """
        self._load_character(tenant_id, character_id)
        # 同 reject：过期落库先于命令事务，否则 409 一回滚，候选永远 PENDING。
        self._sweep_expired(tenant_id, character_id)
        decision_kind = body.get("decision_kind") or "human_approved"
        self._require(decision_kind in DECISION_KINDS,
                      f"decision_kind must be one of {list(DECISION_KINDS)}")
        set_canonical = bool(body.get("set_canonical"))
        principal = str(body.get("principal_id") or tenant_id)
        home_cell = str(body.get("home_cell") or "unknown")
        correlation_id = str(body.get("correlation_id") or command_id)

        def _produce():
            # 1) 锁 character + CAS
            character = self._load_character(tenant_id, character_id)
            self._require_tombstoned_ok(character)
            self._cas(character, body.get("expected_current_revision"))

            # 2) 锁 candidate
            candidate = self._load_candidate(tenant_id, character_id,
                                             candidate_id)
            if candidate["state"] != "PENDING":
                raise ServiceError(
                    409, "CANDIDATE_NOT_PENDING",
                    f"candidate {candidate_id} is {candidate['state']}",
                    details={"candidate_id": candidate_id,
                             "state": candidate["state"]})
            if _expired(candidate["expires_at"]):
                # 上面 `_sweep_expired` 已在本事务之前落过库；能走到这里说明
                # 扫描之后、加锁之前刚好跨过截止线，直接拒掉即可。这里**不再
                # 写库**——在本事务里写然后抛 409 只会一起回滚。
                raise ServiceError(409, "CANDIDATE_NOT_PENDING",
                                   f"candidate {candidate_id} has expired",
                                   details={"candidate_id": candidate_id,
                                            "state": "EXPIRED"})

            payload = _loads(candidate["payload"], {})
            drift = _loads(candidate["identity_drift"], {})
            features = payload.get("identity_features") or {}
            feature_hash = identity_feature_hash(features)

            # 3) 漂移门（§4.3 第 3 步）：只在升 canonical 时判。
            #    补图（set_canonical=false）改 identity_features 本身就是
            #    一次身份变化，但那一版不会成为基准，所以不挡；等真有人把它
            #    设成 canonical 时再挡（set_canonical 走同一条门）。
            if set_canonical:
                if (drift.get("drift_class") == "major"
                        and decision_kind == "policy_auto"):
                    # F7。人审可以接受一次换人；策略不行。
                    raise ServiceError(
                        422, "IDENTITY_DRIFT_REQUIRES_HUMAN",
                        "this candidate changes identity anchors "
                        f"({drift.get('changed_fields')}); policy_auto may not "
                        f"move the canonical pointer across a major drift",
                        details={"drift_class": "major",
                                 "changed_fields": drift.get("changed_fields")})
                prior_canonical = self._canonical_identity_features(
                    tenant_id, character_id)
                if (decision_kind == "policy_auto"
                        and prior_canonical is not None
                        and identity_feature_hash(prior_canonical)
                        != feature_hash):
                    # §2.5 行的第二条条件：hash 必须完全相同。漂移算法说
                    # "minor"（比如皮肤分档变了）不构成策略放行的理由。
                    raise ServiceError(
                        422, "IDENTITY_DRIFT_REQUIRES_HUMAN",
                        "policy_auto requires an identical identity_feature_hash",
                        details={"prior": identity_feature_hash(prior_canonical),
                                 "candidate": feature_hash})

            revision = int(character["current_revision"]) + 1

            # 4) 插 revision。hash 冲突 = 同内容重复接受（F6）。
            try:
                self.conn.execute(
                    "INSERT INTO character_revision (tenant_id, character_id,"
                    " revision, content_hash, payload, view_manifest,"
                    " expression_manifest, body_profile, identity_features,"
                    " identity_feature_hash, rights_ref, lora_refs,"
                    " source_kind, parent_revision, quality_gate_ref,"
                    " accepted_at, accepted_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant_id, character_id, revision,
                     candidate["content_hash"], _dumps(payload),
                     _dumps(payload.get("view_manifest") or {}),
                     _dumps(payload.get("expression_manifest") or {}),
                     _dumps(payload.get("body_profile") or {}),
                     _dumps(features), feature_hash,
                     _dumps(payload.get("rights_ref") or {}),
                     _dumps(payload.get("lora_refs") or []),
                     payload.get("source_kind"),
                     payload.get("parent_revision"),
                     _dumps(payload.get("quality_gate_ref") or {}),
                     _now(), _jcs({"principal_id": principal})))
            except sqlite3.IntegrityError as exc:
                if "character_revision.content_hash" in str(exc):
                    existing = self.conn.execute(
                        "SELECT revision FROM character_revision WHERE"
                        " tenant_id=? AND content_hash=?",
                        (tenant_id, candidate["content_hash"])).fetchone()
                    # F6 的 details 要带 existing_revision（契约写明）。
                    raise ServiceError(
                        409, "DUPLICATE_CHARACTER_CONTENT",
                        "this content has already been accepted for this "
                        "tenant (content_hash is unique per tenant)",
                        details={"content_hash": candidate["content_hash"],
                                 "existing_revision": (existing[0]
                                                       if existing else None)}
                    ) from exc
                raise

            # 5) 派生引用（§2.6，只写可推导的出边；C11-22）。
            if payload.get("parent_revision") is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO revision_reference (tenant_id,"
                    " character_id, revision, ref_role, target_service,"
                    " target_id, target_revision, target_hash)"
                    " VALUES (?,?,?, 'derived_from', 'S11', ?, ?, NULL)",
                    (tenant_id, character_id, revision, character_id,
                     payload["parent_revision"]))
            for lora in payload.get("lora_refs") or []:
                lora_revision = lora.get("revision")
                if not isinstance(lora_revision, int):
                    # target_revision NOT NULL：缺 revision 的 LoRA 引用
                    # 写不进边，只留在 payload 里（C11-22）。
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO revision_reference (tenant_id,"
                    " character_id, revision, ref_role, target_service,"
                    " target_id, target_revision, target_hash)"
                    " VALUES (?,?,?, 'base_for_lora', 'S48', ?, ?, ?)",
                    (tenant_id, character_id, revision,
                     lora.get("artifact_id"), lora_revision,
                     lora.get("content_hash")))

            canonical_revision = character["canonical_revision"]
            became_canonical = False
            # 6) 移动 canonical 指针（只在 set_canonical 时）
            if set_canonical:
                self.conn.execute(
                    "UPDATE canonical_pointer SET superseded_at=?"
                    " WHERE tenant_id=? AND character_id=?"
                    " AND superseded_at IS NULL",
                    (_now(), tenant_id, character_id))
                self.conn.execute(
                    "INSERT INTO canonical_pointer (tenant_id, character_id,"
                    " revision, decided_at, decided_by, decision_kind,"
                    " reason_text, prior_canonical, superseded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (tenant_id, character_id, revision, _now(),
                     _jcs({"principal_id": principal}), decision_kind,
                     body.get("reason_text"), character["canonical_revision"]))
                canonical_revision = revision
                became_canonical = True

            # 7) 头行的两个游标
            self.conn.execute(
                "UPDATE character SET current_revision=?, canonical_revision=?"
                " WHERE tenant_id=? AND character_id=?",
                (revision, canonical_revision, tenant_id, character_id))

            # 8) 候选状态
            self.conn.execute(
                "UPDATE character_candidate SET state='ACCEPTED' WHERE"
                " tenant_id=? AND candidate_id=?", (tenant_id, candidate_id))

            # 9) 事件与事实同事务（§5.3：契约的 8 个 data 键，一个不多）
            self.outbox.append(new_event(
                source=SOURCE, type=EVENT_CHARACTER_REVISION_ACCEPTED,
                subject=f"character/{character_id}", tenant_id=tenant_id,
                home_cell=home_cell, aggregate_id=character_id,
                sequence=revision, correlation_id=correlation_id,
                causation_id=command_id,
                data={
                    "schema_version": SCHEMA_VERSION,
                    "character_ref": {"service_id": SOURCE,
                                      "id": character_id,
                                      "revision": revision,
                                      "content_hash": candidate["content_hash"]},
                    "identity_feature_hash": feature_hash,
                    "became_canonical": became_canonical,
                    "canonical_revision": canonical_revision,
                    "rights_ref": payload.get("rights_ref") or {},
                    # CRC-2 / C11-8：契约要求 view_count ≥ 1，方案没定义口径。
                    # 取 view_manifest 的视图数（front 必填，故 ≥ 1）。
                    "view_count": len(payload.get("view_manifest") or {}),
                    "source_kind": payload.get("source_kind"),
                },
                data_ref={"service_id": SOURCE, "id": character_id,
                          "revision": revision,
                          "content_hash": candidate["content_hash"]},
                schema_version=SCHEMA_VERSION))

            return {
                "character_id": character_id,
                "revision": revision,
                "candidate_id": candidate_id,
                "content_hash": candidate["content_hash"],
                "identity_feature_hash": feature_hash,
                "became_canonical": became_canonical,
                "canonical_revision": canonical_revision,
                "state": "ACCEPTED",
            }

        return self._command(tenant_id, command_id, _produce)

    def _load_candidate(self, tenant_id: str, character_id: str,
                        candidate_id: str) -> dict:
        row = _row(self.conn.execute(
            "SELECT * FROM character_candidate WHERE tenant_id=?"
            " AND character_id=? AND candidate_id=?",
            (tenant_id, character_id, candidate_id)).fetchone())
        if row is None:
            raise ServiceError(
                404, "NOT_FOUND",
                f"no candidate {candidate_id} for character {character_id}")
        return row

    # ------------------------------------------------------------ 命令：指针与状态
    def set_canonical(self, tenant_id: str, command_id: str,
                      character_id: str, body: dict) -> dict:
        """`POST /characters/{id}:set-canonical`（§5.1：**不发事件**）。

        用于回退（把 canonical 从 12 指回 9）。必须带 `reason_text` 留审计
        （§5.1 行 529）；缺它返回 400（C11-19：契约没有专门的码）。

        **不发事件**是契约的事实（topic 里没有 canonical 变更事件，C11-3）：
        按事件缓存 canonical 的消费方必须回读 `GET /characters/{id}/canonical`。
        """
        revision = body.get("revision")
        decision_kind = body.get("decision_kind") or "human_approved"
        self._require(isinstance(revision, int)
                      and not isinstance(revision, bool),
                      "revision must be an integer")
        self._require(decision_kind in DECISION_KINDS,
                      f"decision_kind must be one of {list(DECISION_KINDS)}")
        if decision_kind == "policy_auto":
            # §2.5：policy_auto 的三个条件（none / 同 hash / 只改视图）在
            # 回退路径上没有"新内容"可判——回退就是把基准换成另一个已有
            # revision，那是人的决定。这里直接拒，不是替 owner 选边。
            raise ServiceError(
                422, "IDENTITY_DRIFT_REQUIRES_HUMAN",
                "policy_auto is not allowed on an explicit rollback: moving "
                "the canonical pointer without new content is a human decision "
                "(plan §2.5, §5.1)", details={"decision_kind": decision_kind})
        reason_text = body.get("reason_text")
        self._require(isinstance(reason_text, str) and reason_text.strip() != "",
                      "reason_text is required when moving the canonical "
                      "pointer (plan §5.1: rollback must leave an audit trail)")
        principal = str(body.get("principal_id") or tenant_id)

        def _produce():
            character = self._load_character(tenant_id, character_id)
            self._require_tombstoned_ok(character)
            target = _row(self.conn.execute(
                "SELECT * FROM character_revision WHERE tenant_id=?"
                " AND character_id=? AND revision=?",
                (tenant_id, character_id, revision)).fetchone())
            if target is None:
                raise ServiceError(
                    404, "NOT_FOUND",
                    f"no revision {revision} for character {character_id}")
            if character["canonical_revision"] == revision:
                # 幂等：已经在指的这一版，不重写也不报错。
                return {"character_id": character_id,
                        "canonical_revision": revision,
                        "changed": False}
            self.conn.execute(
                "UPDATE canonical_pointer SET superseded_at=? WHERE tenant_id=?"
                " AND character_id=? AND superseded_at IS NULL",
                (_now(), tenant_id, character_id))
            # §2.5 的主键是 (tenant_id, character_id, revision)，所以"回退到
            # 一个**曾经是** canonical 的 revision"（E08-B2 的 12→9）会撞上
            # 旧的那一行——那张行是它当初被 supersede 时留下的。这一版的
            # PK 装不下同一 revision 的两条审计，所以回退**更新**那一行的
            # 决定字段并把它重新置为有效；被顶下来的那一行仍然留着，
            # E08-B2 的"审计两条"因此仍然可查（见 C11-26）。
            self.conn.execute(
                "INSERT INTO canonical_pointer (tenant_id, character_id,"
                " revision, decided_at, decided_by, decision_kind,"
                " reason_text, prior_canonical, superseded_at)"
                " VALUES (?,?,?,?,?,?,?,?,NULL)"
                " ON CONFLICT (tenant_id, character_id, revision) DO UPDATE SET"
                " decided_at=excluded.decided_at,"
                " decided_by=excluded.decided_by,"
                " decision_kind=excluded.decision_kind,"
                " reason_text=excluded.reason_text,"
                " prior_canonical=excluded.prior_canonical,"
                " superseded_at=NULL",
                (tenant_id, character_id, revision, _now(),
                 _jcs({"principal_id": principal}), decision_kind,
                 reason_text, character["canonical_revision"]))
            self.conn.execute(
                "UPDATE character SET canonical_revision=?"
                " WHERE tenant_id=? AND character_id=?",
                (revision, tenant_id, character_id))
            return {"character_id": character_id,
                    "canonical_revision": revision,
                    "prior_canonical": character["canonical_revision"],
                    "changed": True}

        return self._command(tenant_id, command_id, _produce)

    def change_status(self, tenant_id: str, command_id: str,
                      character_id: str, target: str, body: dict) -> dict:
        """`:deprecate` / `:tombstone`。§3.3 的直线。

        - `deprecate` 只从 `active` 走；
        - `tombstone` 只从 `deprecated` 走（C11-21：图里没有
          active→tombstoned 这条边，一步下线要 owner 在 §3.3 上补）；
        - `tombstoned` 不能复活：复活走新 character_id，否则历史 manifest
          指向的东西变了（§3.3 非法转移表）。
        """
        self._require(target in ("deprecated", "tombstoned"),
                      "target status must be deprecated or tombstoned")
        allowed_from = {"deprecated": ("active",),
                        "tombstoned": ("deprecated",)}[target]

        def _produce():
            character = self._load_character(tenant_id, character_id)
            current = character["status"]
            if current not in allowed_from:
                raise ServiceError(
                    409, "INVALID_STATUS_TRANSITION",
                    f"cannot move character {character_id} from {current} to "
                    f"{target} (plan §3.3)",
                    details={"from": current, "to": target,
                             "allowed_from": list(allowed_from)})
            self.conn.execute(
                "UPDATE character SET status=? WHERE tenant_id=?"
                " AND character_id=?", (target, tenant_id, character_id))
            return {"character_id": character_id, "status": target,
                    "previous_status": current}

        return self._command(tenant_id, command_id, _produce)

    def add_alias(self, tenant_id: str, command_id: str,
                  character_id: str, body: dict) -> dict:
        """`POST /characters/{id}/aliases`（§2.1）。

        §6.2：主键冲突 → 视为幂等成功（别名并发写不该报错）。歧义**不在
        这里解析**——见 C11-25。
        """
        alias_text = body.get("alias_text")
        alias_kind = body.get("alias_kind")
        source = body.get("source")
        self._require(isinstance(alias_text, str) and alias_text,
                      "alias_text is required")
        self._require(alias_kind in ALIAS_KINDS,
                      f"alias_kind must be one of {list(ALIAS_KINDS)}")
        self._require(isinstance(source, str) and source,
                      "source is required (S03|manual|import_batch)")

        def _produce():
            character = self._load_character(tenant_id, character_id)
            self._require_tombstoned_ok(character)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO character_alias (tenant_id,"
                " character_id, alias_text, alias_kind, source)"
                " VALUES (?,?,?,?,?)",
                (tenant_id, character_id, alias_text, alias_kind, source))
            return {"character_id": character_id, "alias_text": alias_text,
                    "alias_kind": alias_kind, "created": cur.rowcount == 1}

        return self._command(tenant_id, command_id, _produce)

    # ------------------------------------------------------------ 读
    def get_character(self, tenant_id: str, character_id: str) -> dict:
        character = self._load_character(tenant_id, character_id)
        pointer = _row(self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=?"
            " AND character_id=? AND superseded_at IS NULL",
            (tenant_id, character_id)).fetchone())
        aliases = [dict(r) for r in self.conn.execute(
            "SELECT alias_text, alias_kind, source FROM character_alias"
            " WHERE tenant_id=? AND character_id=? ORDER BY alias_text,"
            " alias_kind", (tenant_id, character_id)).fetchall()]
        return {
            "character": _character_out(character, created=None),
            "canonical": _pointer_out(pointer),
            "aliases": aliases,
            # E30-B1：S11 从不缓存权利状态，读就要复查。
            "rights_recheck_required": True,
        }

    def get_revision(self, tenant_id: str, character_id: str,
                     revision: int) -> dict:
        """§4.5：响应带 `ETag = content_hash`，且 **artifact 引用不含 URL**
        （§4.5 硬约束 3、§9 反模式 6）。"""
        character = self._load_character(tenant_id, character_id)
        row = _row(self.conn.execute(
            "SELECT * FROM character_revision WHERE tenant_id=?"
            " AND character_id=? AND revision=?",
            (tenant_id, character_id, revision)).fetchone())
        if row is None:
            raise ServiceError(404, "NOT_FOUND",
                               f"no revision {revision} for character "
                               f"{character_id}")
        view_manifest = _loads(row["view_manifest"], {})
        return {
            "character_ref": {"service_id": SOURCE, "id": character_id,
                              "revision": revision,
                              "content_hash": row["content_hash"]},
            "identity_feature_hash": row["identity_feature_hash"],
            "identity_features": _loads(row["identity_features"], {}),
            "view_manifest": {name: _manifest_ref(ref) for name, ref
                              in view_manifest.items()},
            "expression_manifest": {name: _manifest_ref(ref) for name, ref
                                    in _loads(row["expression_manifest"],
                                              {}).items()},
            "body_profile": _loads(row["body_profile"], {}),
            "rights_ref": _loads(row["rights_ref"], {}),
            "lora_refs": _loads(row["lora_refs"], []),
            "source_kind": row["source_kind"],
            "parent_revision": row["parent_revision"],
            # §4.5 硬约束 1：这两个**必须同时返回**。只给 is_canonical=false
            # 的调用方会以为"这个角色没有 canonical"。
            "is_canonical": character["canonical_revision"] == revision,
            "canonical_revision": character["canonical_revision"],
            "accepted_at": row["accepted_at"],
            "etag": row["content_hash"],
            # §4.5 硬约束 2：恒为 true。
            "rights_recheck_required": True,
        }

    def list_revisions(self, tenant_id: str, character_id: str, *,
                       after: int = 0, limit: int = 50) -> dict:
        self._load_character(tenant_id, character_id)
        self._require(isinstance(after, int) and not isinstance(after, bool),
                      "after must be an integer")
        rows = self.conn.execute(
            "SELECT revision, content_hash, identity_feature_hash,"
            " source_kind, accepted_at FROM character_revision"
            " WHERE tenant_id=? AND character_id=? AND revision>?"
            " ORDER BY revision LIMIT ?",
            (tenant_id, character_id, int(after),
             max(1, min(int(limit), 200)))).fetchall()
        return {"items": [dict(r) for r in rows]}

    def get_canonical(self, tenant_id: str, character_id: str) -> dict:
        self._load_character(tenant_id, character_id)
        pointer = _row(self.conn.execute(
            "SELECT * FROM canonical_pointer WHERE tenant_id=?"
            " AND character_id=? AND superseded_at IS NULL",
            (tenant_id, character_id)).fetchone())
        if pointer is None:
            # §4.5 硬约束 5：404 `CANONICAL_NOT_SET`，**不是** 200 + null。
            # 后者会被调用方静默当成空对象。
            raise ServiceError(
                404, "CANONICAL_NOT_SET",
                f"character {character_id} has no canonical revision")
        row = _row(self.conn.execute(
            "SELECT content_hash, identity_feature_hash FROM character_revision"
            " WHERE tenant_id=? AND character_id=? AND revision=?",
            (tenant_id, character_id, pointer["revision"])).fetchone())
        return {"character_ref": {"service_id": SOURCE, "id": character_id,
                                  "revision": pointer["revision"],
                                  "content_hash": (row or {}).get(
                                      "content_hash")},
                "identity_feature_hash": (row or {}).get(
                    "identity_feature_hash"),
                "canonical": _pointer_out(pointer),
                "rights_recheck_required": True}

    def drift(self, tenant_id: str, character_id: str, *,
              from_revision: int, to_revision: int) -> dict:
        """§5.2：两个 revision 的身份漂移对比（供 S17/S33 用）。"""
        self._load_character(tenant_id, character_id)
        rows = {}
        for label, revision in (("from", from_revision), ("to", to_revision)):
            row = _row(self.conn.execute(
                "SELECT * FROM character_revision WHERE tenant_id=?"
                " AND character_id=? AND revision=?",
                (tenant_id, character_id, revision)).fetchone())
            if row is None:
                raise ServiceError(404, "NOT_FOUND",
                                   f"no revision {revision} for character "
                                   f"{character_id}")
            rows[label] = row
        old = _loads(rows["from"]["identity_features"], {})
        new = _loads(rows["to"]["identity_features"], {})
        result = classify_drift(old, new)
        return {"character_id": character_id,
                "from_revision": from_revision, "to_revision": to_revision,
                "from_identity_feature_hash": rows["from"][
                    "identity_feature_hash"],
                "to_identity_feature_hash": rows["to"]["identity_feature_hash"],
                **result}

    def lookup(self, tenant_id: str, *, series_scope: str,
               alias: str) -> dict:
        """`GET /characters:lookup`（C11-25）。

        同时按 `display_name` 与 `alias_text` 在本 series 内查。命中 0 → 404；
        命中 >1 → **422 AMBIGUOUS_ALIAS** 并列出候选。§2.1 行 130 的原话：
        "同名两个角色时，导入流程必须报 AMBIGUOUS_ALIAS 让人选，不能取第一个"。
        这是全仓唯一会遇到歧义的地方，所以检查放在这里。
        """
        self._require(isinstance(series_scope, str) and series_scope,
                      "series_scope is required")
        self._require(isinstance(alias, str) and alias, "alias is required")
        rows = self.conn.execute(
            "SELECT DISTINCT c.* FROM character c"
            " LEFT JOIN character_alias a ON a.tenant_id=c.tenant_id"
            " AND a.character_id=c.character_id AND a.alias_text=?"
            " WHERE c.tenant_id=? AND c.series_scope=?"
            " AND (c.display_name=? OR a.alias_text IS NOT NULL)"
            " ORDER BY c.character_id", (alias, tenant_id, series_scope,
                                         alias)).fetchall()
        if not rows:
            raise ServiceError(404, "NOT_FOUND",
                               f"no character named {alias!r} in "
                               f"{series_scope!r}")
        if len(rows) > 1:
            ids = [r["character_id"] for r in rows]
            raise ServiceError(
                422, "AMBIGUOUS_ALIAS",
                f"{alias!r} matches {len(ids)} characters in {series_scope!r}; "
                f"pick one explicitly (plan §2.1: never take the first)",
                details={"alias": alias, "series_scope": series_scope,
                         "candidates": ids})
        return {"character": _character_out(dict(rows[0]), created=None),
                "rights_recheck_required": True}

    def list_candidates(self, tenant_id: str, character_id: str, *,
                        state: str | None = None) -> dict:
        """`GET /characters/{id}/candidates?state=`（§5.2，仅内部/管理面）。"""
        self._load_character(tenant_id, character_id)
        clause = ["tenant_id=?", "character_id=?"]
        params: list = [tenant_id, character_id]
        if state:
            if state not in CANDIDATE_STATES:
                raise ServiceError(400, "MALFORMED_REQUEST",
                                   f"state must be one of "
                                   f"{list(CANDIDATE_STATES)}")
            clause.append("state=?")
            params.append(state)
        rows = self.conn.execute(
            "SELECT candidate_id, state, content_hash, identity_drift,"
            " created_at, expires_at FROM character_candidate WHERE "
            + " AND ".join(clause) + " ORDER BY created_at DESC, candidate_id",
            params).fetchall()
        return {"items": [{"candidate_id": r["candidate_id"],
                           "state": r["state"],
                           "content_hash": r["content_hash"],
                           "identity_drift": _loads(r["identity_drift"], {}),
                           "created_at": r["created_at"],
                           "expires_at": r["expires_at"]} for r in rows]}


# ------------------------------------------------------------ 纯函数：装配

def _artifact_ref(ref) -> dict:
    """`view_manifest` / `expression_manifest` 的叶子。

    允许 `"art_1"` 或 `{"artifact_id":..., "content_hash":...}`。
    **只留 id 与 hash**：URL 由 S28 短签名发放，S11 不存也不签发
    （§4.5 硬约束 3、§9 反模式 6）。多出来的键（`url`、`signed_url`）
    在这里被丢弃，不是"透传"。
    """
    if isinstance(ref, str):
        return {"artifact_id": ref, "content_hash": None}
    if isinstance(ref, dict):
        artifact_id = ref.get("artifact_id") or ref.get("id")
        return {"artifact_id": artifact_id,
                "content_hash": ref.get("content_hash") or ref.get("sha256")}
    return {"artifact_id": None, "content_hash": None}


def _manifest_ref(ref: dict) -> dict:
    """读路径的 artifact 引用：补上 `service_id: S28`（§4.5 响应示例）。"""
    return {"service_id": "S28", "id": ref.get("artifact_id"),
            "content_hash": ref.get("content_hash")}


def _character_out(character: dict, *, created) -> dict:
    out = {
        "character_id": character["character_id"],
        "project_id": character["project_id"],
        "series_scope": character["series_scope"],
        "display_name": character["display_name"],
        "identity_kind": character["identity_kind"],
        "status": character["status"],
        "canonical_revision": character["canonical_revision"],
        "current_revision": character["current_revision"],
        "series_of_record": character["series_of_record"],
        "created_at": character["created_at"],
    }
    if created is not None:
        out["created"] = created
    return out


def _pointer_out(pointer) -> dict | None:
    if pointer is None:
        return None
    return {
        "revision": pointer["revision"],
        "decided_at": pointer["decided_at"],
        "decided_by": _loads(pointer["decided_by"], {}),
        "decision_kind": pointer["decision_kind"],
        "reason_text": pointer["reason_text"],
        "prior_canonical": pointer["prior_canonical"],
    }


def _expired(expires_at: str) -> bool:
    try:
        deadline = datetime.fromisoformat(
            str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return deadline <= _now_dt()
