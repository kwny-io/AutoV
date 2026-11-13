"""S38 字幕与本地化的数据面（方案 §2–§5）。

这个模块只有三样东西：**表**、**闭集**、**检查项注册数据**。没有 IO，没有
业务分支。把它们放在一起是因为方案 §2.2 / §5 是同一份规格的两半：`SUB.*`
的每一条检查都要落进某张表或某条状态，反过来每张表都要能解释它凭什么被
判 pass。

## 三份文本，三条版本链

字幕最容易犯的错是把「一段文本」当成一个版本。S38 里同时存在三种文本，
各有各的版本、各有各的写者：

| 文本 | 谁写 | 版本落点 |
|---|---|---|
| 源台词 | S06（脚本） | `cue.source_text_revision` / `source_text_hash` |
| 发音词典 | S15 | `cue.lexicon_revision` / `lexicon_hash`（+ `lexicon_ref_id`） |
| 译文 | S38 自己（`subtitles.translation`） | `cue.translation_revision` / `translation_hash` |

三组各占独立列，**没有** `text_versions jsonb` 这种合并列。方案 §2.2 的
意图是「任意一条链变了，S38 都能只让相关的那条失效」；合并成一个 blob 就
没法回答"是哪条变了"。`cue.display_text` 是派生的
`localize(source, lexicon, translation)`，因此**不版本化**——它是三份输入
的函数，存版本号只会多出第四个真相。

## 字节不在这个库里

`.srt` / `.vtt` / `.ass` 的字节由 S28 持有，S38 只存
`subtitles.rendition.artifact_id + content_hash`（§3.0）。这条边界不是风格
问题：一旦 S38 也存一份字节，导出时就有两个可能不一致的副本，而"哪份是
交付的"会变成按查询时间决定。V6 测试直接断言库里没有任何字幕文件。

## 与 S37 的差异

S37 的注册表在 `registry.py`，因为那个服务的检查项就是它的全部业务。S38
的检查项横跨时间码、排版、文本版本、封装格式四组，但本服务的写入范围只
允许 `schema.py` / `service.py` / `deps.py`，所以注册数据落在本文件末尾，
形状与 `contracts/quality/check-registry.v2.schema.json` 一致，测试里直接
喂给 `contracts/quality/validate_registry.py`。

## 已知缺口

见 `CONTRACT_GAPS`。每一条都是「契约里没有，但实现必须做选择」，且都写了
选择本身与它为什么不是所有权决定。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 标识与版本

#: 本服务在所有事件与报告里的 `service_id`。
SOURCE = "S38"

#: 落库对象的前缀。`qc_` 是 `qc-report.v2` 强制的（`^qc_[a-z0-9]{26}$`）。
ID_PREFIX = {
    "track": "trk_",
    "cue": "cue_",
    "translation": "trn_",
    "rendition": "rnd_",
    "report": "qc_",          # qc-report.v2 的 report_id 形状
    "defect": "df_",          # qc-report.v2 的 defect_id 形状
}

SCHEMA_VERSION = "2.0"
EVENT_READY = "post.subtitle.ready"
EVENT_TIMELINE = "post.timeline.revision.created"
EVENT_SCRIPT = "narrative.script.revision.created"
TOPIC = "drama.subtitles.v2"

#: 本服务消费的 inbox 名字。
CONSUMER_TIMELINE = "s38.timeline-revision"
CONSUMER_SCRIPT = "s38.script-revision"
CONSUMER_TRANSLATION = "s38.translation-review"

#: QC 层。`qc-report.v2` 的 `qc_layer` 只有四个值，字幕是其中之一。
QC_LAYER = "subtitle"

#: 渲染/排版口径版本。改了排版算法就要跟着改它，因为它决定同一份 cue 会
#: 不会产出同样的字节（见 §3.0 的"不重渲染"约束）。
RENDER_VERSION = "s38-subtitle-render.v1"
SAMPLING_VERSION = "s38-subtitle-layout.v1"

# ---------------------------------------------------------------- 闭集

#: `subtitles.track.state`（方案 §9.1）。
TRACK_STATES = (
    "draft",                    # 时间码/排版已算，尚未落字节
    "rendered",                 # 三种格式都已归档到 S28
    "rendered_pending_archive",  # 渲染完成但 S28 不可达（§3.0）
    "archived",                 # 已交付，等待 QC
    "qc_passed",
    "accepted",                 # 终态由 S08 决定，S38 不自行推进
    "repair_pending",           # 自检不过，等 S39 的修复
)

#: 允许 `post.subtitle.ready` 的终态。判据不是"结论好不好"，而是"字节在不在
#: S28"。方案 §7 的事件示例里 `qc_decision` 就是 `"repair"`，所以
#: `repair_pending` 也必须发——S39 正是靠这条事件拿到待修的字幕。
#: `rendered_pending_archive` 不在里面：没有 `artifact_id`，事件发不出去（§3.0）。
READY_STATES = ("rendered", "archived", "qc_passed", "repair_pending")

#: `subtitles.translation.status`（§9.2）。`stale` 不是失败，是"源台词变了"。
TRANSLATION_STATUSES = ("draft", "reviewed", "accepted", "stale")

#: 命令里允许声明的译文状态。`accepted` 不在这里：验收是 S08/人的事，
#: 见 CONTRACT_GAPS 的 `S38-C6`。
COMMAND_TRANSLATION_STATUSES = ("draft", "reviewed")

#: 交付格式（§2.2、post.subtitle-ready.v2）。
FORMATS = ("srt", "vtt", "ass")

#: 决策四值，与 `qc-report.v2` 一致。
DECISIONS = ("pass", "repair", "human_review", "abstain")

#: `qc-report.v2` 的 defect `type` 闭集（14 值）。**S38 的 24 条只用
#: `subtitle`**，包括时间码类。理由不是"字幕天然是 subtitle"，而是动作族：
#: `defect_type` 唯一的作用是选修复族（action-families.v2 的原话），而
#: `temporal` 族的链是 `["temporal_edit","regenerate_video"]`——把一条字幕
#: 时间码缺陷登记成 `temporal`，会让 S39 有权重生成画面，而 subtitle 族
#: 明令禁止 `regenerate_video`（"pixels are correct"）。像素没问题，错的
#: 是叠在上面的字。登记成 `subtitle` 才能把修复锁在 realign / re_render。
DEFECT_TYPES = (
    "identity", "anatomy", "hands", "costume", "prop", "continuity",
    "camera_adherence", "action_adherence", "temporal", "subtitle", "lipsync",
    "voice", "technical", "audio_mix",
)
SUBTITLE_DEFECT_TYPES = ("subtitle",)

#: `qc-report.v2` 的 recommended_actions 闭集（12 值）。
RECOMMENDED_ACTIONS = (
    "reingest", "transcode", "keyframe_edit", "regenerate_video",
    "temporal_edit", "redo_lipsync", "realign_subtitles",
    "re_render_subtitles", "remix", "re_synthesize_speech", "human_review",
    "change_spec",
)

SEVERITIES = ("info", "minor", "major", "blocker")
SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "blocker": 3}

#: `qc-report.v2` 的 check manifest 状态四值。**不是 pass/fail**：manifest 的
#: 语义是"这条检查有没有得出结论"，不是"有没有过"。`evaluated` 的结论写在
#: defect 里；`failed` 表示检查器自己出错。把 manifest 写成 pass/fail 会把
#: "有一项没跑"读成"跑过的都没问题"。
CHECK_STATUSES = ("evaluated", "not_applicable", "not_evaluated", "failed")

#: `qc-report.v2` 的 `abstain.reason_code` 七值闭集。
ABSTAIN_REASON_CODES = (
    "incomplete_coverage", "evaluator_timeout", "evaluator_error",
    "model_disagreement", "low_confidence", "dependency_stale",
    "asset_unavailable",
)

#: 允许出现的"正当 not_applicable"理由，不计入 abstain。
JUSTIFIED_NOT_APPLICABLE = frozenset({
    "no_dialogue", "no_such_content", "sampling_not_selected",
    "policy_disabled",
})

#: 命令里出现的易变上下文键。同一个请求重投时它们会变，不能进 request_hash。
VOLATILE_CONTEXT_KEYS = ("trace_id", "traceparent", "timestamp",
                         "requested_at", "issued_at", "server_time")

# ---------------------------------------------------------------- 时间码参数

#: 内部时间单位：秒，`numeric(10,6)`（§4）。所有对外格式都在渲染时取整。
TIMEBASE = "seconds"
MAX_SECONDS = 10 ** 4 - 1          # numeric(10,6) 的整数位上限

#: §4.2 的提前/延后量。上限，不是固定值：`lead_in = min(80ms, 前一个字的
#: 间隙)`，`lead_out = min(120ms, 后一个字的间隙)`。拿固定值会在连续台词
#: 上造出重叠。
MAX_LEAD_IN_MS = 80
MAX_LEAD_OUT_MS = 120

#: §4.3。
MIN_DURATION_MS = 833
MAX_DURATION_MS = 7000
MIN_GAP_MS = 40

#: §4.4 的 CPS / 行长上限。命令里的 `profile` 可以收紧，不能放宽。
MAX_CPS = {"zh": 9, "en": 17, "ja": 8}
MAX_CHARS_PER_LINE = {"zh": 16, "en": 42, "ja": 20}
MAX_LINES = 2

#: 与 S37 对齐结果比对的容差（§5，SUB.TIMING.DRIFT_VS_ALIGNMENT）。
DRIFT_TOLERANCE_MS = 100

#: 译文长度相对源文的漂移阈值（§5，TRANSLATION_LENGTH_DRIFT）。
TRANSLATION_LENGTH_DRIFT = 0.5

# ---------------------------------------------------------------- 表结构

#: 方案 §2.2 的 DDL，加两张方案漏写但 §6/事件契约强制要有的表。
#:
#: * `subtitles.qc_report` / `subtitles.qc_check` / `subtitles.qc_defect`：
#:   `post.subtitle.ready.qc_report_ref` 指向 S38 自己的报告，而 §2.2 的
#:   五张表里没有任何一张装得下它。见 CONTRACT_GAPS `S38-C1`。
#: * `subtitles.clip_ref`：§3.1 的 EDL 增量要比较 in/out/speed 的旧值，
#:   事件里没有 clips，只能自己留一份构建时的快照。见 `S38-C3`。
#: * `subtitles.pending_render`：渲染成功、归档失败时的意图记录。只存
#:   content_hash 与 byte_size，**不存字节**（§3.0）。
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS track (
    track_id            TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    branch_id           TEXT NOT NULL,
    episode_id          TEXT NOT NULL,
    timeline_id         TEXT NOT NULL,
    timeline_revision   INTEGER NOT NULL CHECK (timeline_revision >= 1),
    target_locale       TEXT NOT NULL,
    source_locale       TEXT NOT NULL,
    subtitle_revision   INTEGER NOT NULL CHECK (subtitle_revision >= 1),
    state               TEXT NOT NULL CHECK (state IN (
                            'draft','rendered','rendered_pending_archive',
                            'archived','qc_passed','accepted','repair_pending')),
    -- AGENTS.md：execution_fence 作用于 run/branch；node_epoch 作用于单个
    -- logical_node。两列分开存，永不互相赋值、永不比较（S38-C4）。
    execution_fence     INTEGER NOT NULL CHECK (execution_fence >= 1),
    node_epoch          INTEGER NOT NULL CHECK (node_epoch >= 1),
    profile_json        TEXT NOT NULL,
    alignment_report_id TEXT NOT NULL,
    alignment_revision  INTEGER NOT NULL CHECK (alignment_revision >= 1),
    alignment_hash      TEXT NOT NULL CHECK (length(alignment_hash) = 64),
    dialogue_id         TEXT NOT NULL,
    dialogue_revision   INTEGER NOT NULL CHECK (dialogue_revision >= 1),
    dialogue_hash       TEXT NOT NULL CHECK (length(dialogue_hash) = 64),
    qc_report_id        TEXT,
    qc_decision         TEXT CHECK (qc_decision IN (
                            'pass','repair','human_review','abstain')),
    -- 换字幕 revision 时旧值留档，便于审计"这一版是被谁替换的"。
    supersedes_revision INTEGER CHECK (supersedes_revision IS NULL
                                       OR supersedes_revision >= 1),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (tenant_id, track_id)
);

CREATE TABLE IF NOT EXISTS cue (
    track_id             TEXT NOT NULL
                         REFERENCES track(track_id) ON DELETE CASCADE,
    cue_id               TEXT NOT NULL,
    cue_index            INTEGER NOT NULL CHECK (cue_index >= 0),
    clip_id              TEXT NOT NULL,
    script_line_id       TEXT,
    -- 三份文本，三组列（见模块 docstring）。
    source_text_revision INTEGER CHECK (source_text_revision IS NULL
                                        OR source_text_revision >= 1),
    source_text_hash     TEXT CHECK (source_text_hash IS NULL
                                     OR length(source_text_hash) = 64),
    lexicon_ref_id       TEXT,
    lexicon_revision     INTEGER CHECK (lexicon_revision IS NULL
                                        OR lexicon_revision >= 1),
    lexicon_hash         TEXT CHECK (lexicon_hash IS NULL
                                     OR length(lexicon_hash) = 64),
    translation_id       TEXT,
    translation_revision INTEGER CHECK (translation_revision IS NULL
                                        OR translation_revision >= 1),
    translation_hash     TEXT CHECK (translation_hash IS NULL
                                     OR length(translation_hash) = 64),
    -- 派生文本：localize(source, lexicon, translation)。不版本化。
    display_text         TEXT NOT NULL,
    start_seconds        REAL NOT NULL,
    end_seconds          REAL NOT NULL,
    alignment_report_id  TEXT NOT NULL,
    align_unit_start     INTEGER NOT NULL CHECK (align_unit_start >= 0),
    align_unit_end       INTEGER NOT NULL CHECK (align_unit_end >= 0),
    -- 单个 logical_node 的局部重生成版本。与 track.execution_fence 无关。
    node_epoch           INTEGER NOT NULL CHECK (node_epoch >= 1),
    line_count           INTEGER NOT NULL CHECK (line_count >= 1),
    max_chars_per_line   INTEGER NOT NULL CHECK (max_chars_per_line >= 1),
    -- §2.2 原文是 `style_ref text NOT NULL`。内核在命令没给模板时填
    -- `tpl_sub_default`（§7 示例值），而不是留 null——留 null 会让
    -- SUB.FORMAT.ASS_STYLE_MISSING 永远无法判定"引用了什么"。
    style_ref            TEXT NOT NULL,
    safe_area_ok         INTEGER NOT NULL CHECK (safe_area_ok IN (0, 1)),
    PRIMARY KEY (track_id, cue_id),
    CHECK (end_seconds >= start_seconds),
    CHECK (align_unit_end >= align_unit_start)
);

CREATE UNIQUE INDEX IF NOT EXISTS cue_index_unique
    ON cue(track_id, cue_index);
CREATE INDEX IF NOT EXISTS cue_clip ON cue(track_id, clip_id);

CREATE TABLE IF NOT EXISTS translation (
    translation_id      TEXT NOT NULL,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    tenant_id           TEXT NOT NULL,
    target_locale       TEXT NOT NULL,
    source_text_revision INTEGER NOT NULL CHECK (source_text_revision >= 1),
    source_text_hash    TEXT NOT NULL CHECK (length(source_text_hash) = 64),
    content_hash        TEXT NOT NULL CHECK (length(content_hash) = 64),
    status              TEXT NOT NULL CHECK (status IN (
                            'draft','reviewed','accepted','stale')),
    translated_by       TEXT,
    reviewer            TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (translation_id, revision)
);

CREATE TABLE IF NOT EXISTS cue_text (
    translation_id      TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    cue_id              TEXT NOT NULL,
    text                TEXT NOT NULL,
    PRIMARY KEY (translation_id, revision, cue_id),
    FOREIGN KEY (translation_id, revision)
        REFERENCES translation(translation_id, revision)
        ON DELETE CASCADE
);

-- 字节归属 S28（§3.0）：这里只有指针与摘要。
CREATE TABLE IF NOT EXISTS rendition (
    track_id            TEXT NOT NULL
                        REFERENCES track(track_id) ON DELETE CASCADE,
    subtitle_revision   INTEGER NOT NULL CHECK (subtitle_revision >= 1),
    format              TEXT NOT NULL CHECK (format IN ('srt','vtt','ass')),
    artifact_id         TEXT NOT NULL,
    content_hash        TEXT NOT NULL CHECK (length(content_hash) = 64),
    byte_size           INTEGER NOT NULL CHECK (byte_size > 0),
    rendered_at         TEXT NOT NULL,
    PRIMARY KEY (track_id, subtitle_revision, format),
    UNIQUE (artifact_id)
);

-- 渲染完成但归档失败。**没有 bytes 列**，这是刻意的（§3.0）。
CREATE TABLE IF NOT EXISTS pending_render (
    track_id            TEXT NOT NULL
                        REFERENCES track(track_id) ON DELETE CASCADE,
    subtitle_revision   INTEGER NOT NULL CHECK (subtitle_revision >= 1),
    format              TEXT NOT NULL CHECK (format IN ('srt','vtt','ass')),
    content_hash        TEXT NOT NULL CHECK (length(content_hash) = 64),
    byte_size           INTEGER NOT NULL CHECK (byte_size > 0),
    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error          TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (track_id, subtitle_revision, format)
);

-- §3.1 的 EDL 增量需要"构建时的 clip 参数"作基准。事件不带 clips，所以
-- 留一份快照；它只是缓存，权威仍是 S41。
CREATE TABLE IF NOT EXISTS clip_ref (
    track_id            TEXT NOT NULL
                        REFERENCES track(track_id) ON DELETE CASCADE,
    clip_id             TEXT NOT NULL,
    in_ticks            INTEGER NOT NULL CHECK (in_ticks >= 0),
    out_ticks           INTEGER NOT NULL CHECK (out_ticks >= 0),
    speed_ratio         REAL NOT NULL CHECK (speed_ratio > 0),
    tick_rate           INTEGER NOT NULL CHECK (tick_rate >= 1),
    PRIMARY KEY (track_id, clip_id),
    CHECK (out_ticks > in_ticks)
);

-- S38 自己的 QC 报告。方案 §2.2 没有这张表，但事件契约要求它的 id。
CREATE TABLE IF NOT EXISTS qc_report (
    report_id           TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    track_id            TEXT NOT NULL,
    subtitle_revision   INTEGER NOT NULL CHECK (subtitle_revision >= 1),
    report_generation   INTEGER NOT NULL CHECK (report_generation >= 1),
    decision            TEXT NOT NULL CHECK (decision IN (
                            'pass','repair','human_review','abstain')),
    report_content_hash TEXT NOT NULL CHECK (length(report_content_hash) = 64),
    payload_json        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (tenant_id, report_id)
);

CREATE TABLE IF NOT EXISTS qc_check (
    report_id           TEXT NOT NULL
                        REFERENCES qc_report(report_id)
                        ON DELETE CASCADE,
    check_id            TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN (
                            'evaluated','not_applicable','not_evaluated',
                            'failed')),
    reason_code         TEXT NOT NULL,
    PRIMARY KEY (report_id, check_id)
);

CREATE TABLE IF NOT EXISTS qc_defect (
    report_id           TEXT NOT NULL
                        REFERENCES qc_report(report_id)
                        ON DELETE CASCADE,
    defect_id           TEXT NOT NULL,
    defect_fingerprint  TEXT NOT NULL CHECK (length(defect_fingerprint) = 32),
    check_id            TEXT NOT NULL,
    PRIMARY KEY (report_id, defect_id)
);

-- S38 自己的 inbox：比平台 SDK 的 inbox 多存投影需要的坐标。平台 SDK 的
-- Inbox 只会写 (consumer, event_id, received_at)；这里另有一张同名列的表，
-- 所以内核不用平台 Inbox 类（见 service.S38Inbox 的说明）。
CREATE TABLE IF NOT EXISTS inbox (
    consumer            TEXT NOT NULL,
    event_id            TEXT NOT NULL,
    tenant_id           TEXT,
    event_type          TEXT,
    aggregate_id        TEXT,
    sequence            INTEGER,
    received_at         TEXT NOT NULL,
    PRIMARY KEY (consumer, event_id)
);
"""

# ---------------------------------------------------------------- 检查项注册表
#
# 方案 §5 的 24 行。`layer` 一律 `subtitle`、`owner_service` 一律 `S38`、
# 前缀一律 `SUB.`——`contracts/quality/validate_registry.py` 会逐条核对。
#
# 动作链只有一族：`["realign_subtitles", "re_render_subtitles"]`。方案 §5 里
# 几条写着「转人工」，但注册表的 `default_action` 不能填 `human_review`
# （它是族兜底，不是起点，校验器直接拒）。所以这些检查登记
# `never_auto_repair=true` + `no_auto_reason`，由内核 `decide()` 转
# `human_review`——这与 S37 对 `AUD.VOICE.AUTHORIZATION` 的做法一致。
#
# 另外：`regenerate_video` 在 subtitle 族里被**永久禁止**（action-families
# 的原话是"pixels are correct"）。子系统里换了字，重生成视频只会得到同一
# 段画面再被画一次字。

REGISTRY_VERSION = 1

#: `window_scope` → `qc-report.v2` 的 `scope`（四值闭集）。
SCOPE_MAP = {
    "frame": "frame",
    "window": "window",
    "shot": "segment",
    "track": "whole_artifact",
    "all": "whole_artifact",
}

#: 缺陷的 `expected_source.service_id`：S38 知道基准来自谁，不问模型。
#: text=台词/译文基准，timing=对齐基准，layout/format=本服务自己的规格。
_SOURCE_DEFAULT = {
    "SUB.TEXT.*": "S06",
    "SUB.TIMING.DRIFT_VS_ALIGNMENT": "S37",
}

_FAMILY = ["realign_subtitles", "re_render_subtitles"]


def _check(check_id, defect_type, severity, *, scope, default_action=None,
           allowed=None, recheck="subtitle", never_auto=False,
           no_auto_reason=None, expected=None, notes=None):
    """构造一条注册项（形状与 `check-registry.v2` 的 `$defs.check` 一致）。

    默认动作取族链首项；`severity_max` 登记为"再上一档"，blocker 已到顶。
    """
    if allowed is None:
        allowed = list(_FAMILY)
    if default_action is None:
        default_action = allowed[0]
    if default_action not in allowed:
        raise ValueError(f"{check_id}: default_action not in allowed_actions")
    entry = {
        "check_id": check_id,
        "layer": "subtitle",
        "owner_service": SOURCE,
        "defect_type": defect_type,
        "default_action": default_action,
        "allowed_actions": list(allowed),
        "window_scope": scope,
        "severity_default": severity,
        "severity_max": {"info": "minor", "minor": "major",
                         "major": "blocker", "blocker": "blocker"}[severity],
        "recheck_scope": recheck,
        "_expected": expected or "S06",
    }
    # schema 对 frame/window 强制 window_max_frames。字幕的 window 是**一条
    # cue 的区间**，不是帧数，所以最小值 1 是唯一诚实的填法：它表示"窗口不
    # 可再扩"，避免 S39 把 window 解释成一段可跨 cue 的连续帧。keyframe_edit
    # 从来不在 subtitle 族里，这个上限不会被用到。
    if scope in ("frame", "window"):
        entry["window_max_frames"] = 1
    if never_auto:
        entry["never_auto_repair"] = True
        entry["no_auto_reason"] = no_auto_reason
    if notes:
        entry["notes"] = notes
    return entry


CHECKS = [
    # --- §5.1 时间码 -------------------------------------------------------
    _check("SUB.TIMING.OVERLAP", "subtitle", "blocker", scope="window",
           notes="同一轨相邻 cue 输出区间重叠；ASS 百分秒取整后必须复检"
                 "（E24-U2）"),
    _check("SUB.TIMING.NEGATIVE_DURATION", "subtitle", "blocker",
           scope="window", notes="end < start"),
    _check("SUB.TIMING.TOO_SHORT", "subtitle", "major", scope="window",
           notes=f"时长 < {MIN_DURATION_MS}ms 且无法向邻居扩展"),
    _check("SUB.TIMING.TOO_LONG", "subtitle", "minor", scope="window",
           notes=f"时长 > {MAX_DURATION_MS}ms 且无法再切分"),
    _check("SUB.TIMING.CPS_EXCEEDED", "subtitle", "major", scope="window",
           allowed=["realign_subtitles"], never_auto=True,
           no_auto_reason="CPS 超限时先重断句、再扩到 max_duration；两步都"
                          "用尽后只能人改文案或放宽 profile。自动缩短文字＝"
                          "擅自删台词（§4.4）",
           notes="见 §4.4 的三步顺序；禁止静默缩字"),
    _check("SUB.TIMING.OUT_OF_RANGE", "subtitle", "blocker", scope="window",
           notes="cue 落在 timeline [0, duration] 之外"),
    _check("SUB.TIMING.DRIFT_VS_ALIGNMENT", "subtitle", "major",
           scope="window", expected="S37",
           notes=f"cue 边界与 S37 的词级区间偏差 > {DRIFT_TOLERANCE_MS}ms"),
    _check("SUB.TIMING.FRAME_BOUNDARY", "subtitle", "info", scope="frame",
           notes="边界落在帧中间（仅提示，不阻断）"),

    # --- §5.2 排版 ---------------------------------------------------------
    _check("SUB.LAYOUT.LINE_OVERFLOW", "subtitle", "major", scope="window",
           notes=f"单行超过 {MAX_CHARS_PER_LINE['zh']} 汉字 / "
                 f"{MAX_CHARS_PER_LINE['en']} 拉丁字符"),
    _check("SUB.LAYOUT.TOO_MANY_LINES", "subtitle", "major", scope="window",
           notes=f"行数 > {MAX_LINES}"),
    _check("SUB.LAYOUT.SAFE_AREA", "subtitle", "major", scope="window",
           notes="字幕框超出 profile.safe_area（底部/侧边百分比）"),
    _check("SUB.LAYOUT.ORPHAN_WORD", "subtitle", "info", scope="window",
           notes="末行只剩一个词（仅提示）"),
    _check("SUB.LAYOUT.FONT_MISSING", "subtitle", "blocker", scope="track",
           allowed=["re_render_subtitles"],
           notes="style_template_ref 指定的字体/样式在渲染环境缺失；"
                 "只能换样式重渲染，重排时间码没有意义"),

    # --- §5.3 文本与版本 ---------------------------------------------------
    _check("SUB.TEXT.SOURCE_STALE", "subtitle", "blocker", scope="track",
           allowed=["realign_subtitles"], never_auto=True,
           no_auto_reason="源台词 revision 已前进；重排只能让旧文本对齐得更"
                          "整齐。必须由人确认要跟哪一版台词（S06 是权威）",
           notes="正式交付阻断；预览仍可出"),
    _check("SUB.TEXT.TRANSLATION_STALE", "subtitle", "blocker", scope="track",
           allowed=["realign_subtitles"], never_auto=True,
           no_auto_reason="译文是基于旧源文的。重译是付费 S26 任务，且需要"
                          "人验收；S38 只把状态标 stale，绝不自动重译",
           notes="预览可出、正式交付阻断（§9.2）"),
    _check("SUB.TEXT.TRANSLATION_DRAFT", "subtitle", "major", scope="track",
           allowed=["realign_subtitles"], never_auto=True,
           no_auto_reason="未评审的译文不能进正式交付；评审是人的决定",
           notes="draft 只能预览"),
    _check("SUB.TEXT.TRANSLATION_MISSING", "subtitle", "blocker", scope="window",
           allowed=["realign_subtitles"], never_auto=True,
           no_auto_reason="目标语言缺这段译文；填译文是翻译工作，不是排版",
           notes="命令显式声明缺译时产 defect；依赖不可达则 abstain"),
    _check("SUB.TEXT.TRANSLATION_LENGTH_DRIFT", "subtitle", "major",
           scope="window",
           notes=f"译文长度相对源文偏离 > {int(TRANSLATION_LENGTH_DRIFT * 100)}%"),
    # 基准是词典本身（S15），不是剧本：这里判的是"这条 cue 有没有按它读到的
    # 词典 revision 引用"，内核产缺陷时写的就是 S15（service.py 的
    # `_source("S15", "lexicon", 1)`）。不写 `expected=` 会落到
    # `_SOURCE_DEFAULT` 的 `SUB.TEXT.* → S06`，与缺陷实际携带的基准不符。
    _check("SUB.TEXT.LEXICON_UNUSED", "subtitle", "minor", scope="track",
           expected="S15",
           notes="S15 词典不可达时无法判定是否用对读音；只记 minor 观测"),
    _check("SUB.TEXT.SCRIPT_CONFLICT", "subtitle", "info", scope="window",
           allowed=["realign_subtitles"], never_auto=True,
           no_auto_reason="台词与字幕文本冲突时 S38 只上报观测；谁对谁错是"
                          "创作决定，不由排版服务判定",
           notes="观测型：只写 dimension，永不产 defect"),

    # --- §5.4 封装格式 -----------------------------------------------------
    _check("SUB.FORMAT.SRT_SYNTAX", "subtitle", "blocker",
           scope="track", allowed=["re_render_subtitles"],
           notes="SRT 序号/时间行/空行结构不合法"),
    _check("SUB.FORMAT.ENCODING", "subtitle", "blocker", scope="track",
           allowed=["re_render_subtitles"],
           notes="非 UTF-8 或含不可编码字符"),
    _check("SUB.FORMAT.ASS_STYLE_MISSING", "subtitle", "blocker",
           scope="track", allowed=["re_render_subtitles"],
           notes="ASS 样式段引用的 style 未定义"),
    _check("SUB.FORMAT.BOM", "subtitle", "info", scope="track",
           allowed=["re_render_subtitles"],
           notes="UTF-8 BOM 的存在与否（仅提示；SRT 播放器兼容性差异）"),
]

CHECK_IDS = [c["check_id"] for c in CHECKS]

#: check_id → 注册项。
BY_ID = {c["check_id"]: c for c in CHECKS}

#: 观测型：只写 dimension，永不产 defect。
OBSERVATION_ONLY = frozenset({"SUB.TEXT.SCRIPT_CONFLICT"})

#: 只在整轨级别判定的检查（scope=whole_artifact）。
TRACK_LEVEL = frozenset(c["check_id"] for c in CHECKS
                        if c["window_scope"] == "track")

#: 观测型之外，`never_auto_repair` 的检查：结论只能转人。内核用注册表本身
#: 判定，不另抄一份名单——抄一份就会和注册表分叉。
NEVER_AUTO = frozenset(c["check_id"] for c in CHECKS
                       if c.get("never_auto_repair"))

#: 依赖 S37 对齐报告的检查。S37 abstain / 不可达时它们 not_evaluated。
NEEDS_ALIGNMENT = frozenset({
    "SUB.TIMING.DRIFT_VS_ALIGNMENT",
    "SUB.TIMING.OVERLAP",
    "SUB.TIMING.NEGATIVE_DURATION",
    "SUB.TIMING.TOO_SHORT",
    "SUB.TIMING.TOO_LONG",
    "SUB.TIMING.OUT_OF_RANGE",
})

#: 依赖 S15 词典的检查。词典不可达 → not_evaluated(dependency_unavailable)。
NEEDS_LEXICON = frozenset({"SUB.TEXT.LEXICON_UNUSED"})


def registry_document() -> dict:
    """导出 `check-registry.v2` 形状的文档（丢掉 `_` 前缀的内核字段）。

    schema 是 `additionalProperties:false`，多一个键就加载不了。
    """
    return {
        "schema_version": "2.0",
        "registry_version": REGISTRY_VERSION,
        "checks": [{k: v for k, v in entry.items() if not k.startswith("_")}
                   for entry in CHECKS],
    }


def select(include, exclude) -> list[str]:
    """把 `checks.include` 解析成有序 check_id 列表。

    未知模式直接报错：拼错一个前缀就静默少查一组，是"沉默变绿灯"最常见的
    来源。显式点名不存在的 check 同样报错。
    """
    import fnmatch

    if include is None:
        include = ["SUB.*"]
    if not isinstance(include, (list, tuple)) or not include:
        raise ValueError("checks.include must be a non-empty list")
    for pattern in list(include) + list(exclude or []):
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("checks.include entries must be non-empty strings")
        if not any(fnmatch.fnmatchcase(cid, pattern) for cid in CHECK_IDS):
            raise ValueError(f"checks.include pattern matches no check: {pattern}")

    chosen = []
    for check_id in CHECK_IDS:
        if not any(fnmatch.fnmatchcase(check_id, p) for p in include):
            continue
        if any(fnmatch.fnmatchcase(check_id, p) for p in (exclude or [])):
            continue
        chosen.append(check_id)
    if not chosen:
        raise ValueError("checks.include selects no check")
    return chosen


def severity_after(default: str, proposed: str, ceiling: str) -> str:
    """只能上提不能下压，且封顶在登记上限。"""
    if proposed not in SEVERITY_ORDER:
        raise ValueError(f"unknown severity: {proposed}")
    value = default if SEVERITY_ORDER[default] >= SEVERITY_ORDER[proposed] \
        else proposed
    if SEVERITY_ORDER[value] > SEVERITY_ORDER[ceiling]:
        value = ceiling
    return value


def report_scope(window_scope: str) -> str:
    try:
        return SCOPE_MAP[window_scope]
    except KeyError:
        raise ValueError(f"unknown window_scope: {window_scope!r}") from None


def expected_source(check_id: str) -> str:
    return BY_ID[check_id]["_expected"]


# ---------------------------------------------------------------- 契约缺口
#
# 每一条都必须是「契约里没有，实现必须选」，且写清楚选了什么、为什么这不
# 构成所有权决定。方案 §11 的 C1–C5 在最前面，其余是写代码时才暴露的。

CONTRACT_GAPS = [
    {
        "id": "S38-C1",
        "kind": "conflict",
        "summary": "方案 §2.2 的 DDL 没有 QC 报告表，但 §6 与 "
                   "post.subtitle-ready.v2 要求 qc_report_ref 指向 S38 自己的报告。",
        "handling": "本服务新增 subtitles.qc_report / qc_check / qc_defect 三张表，"
                    "只写自己的报告；不新增任何指针给别的服务。",
    },
    {
        "id": "S38-C2",
        "kind": "gap",
        "summary": "方案 §8：S15 没有「按 revision 查词典条目」的查询契约，"
                   "无法判定某音是否按词典发音。",
        "handling": "deps.build_lexicon_source 未配置时返回 None，"
                    "SUB.TEXT.LEXICON_UNUSED 记 not_evaluated(dependency_unavailable)，"
                    "报告走 abstain；不把'查不到'当成通过。",
    },
    {
        "id": "S38-C3",
        "kind": "gap",
        "summary": "post.timeline.revision.created 不携带 clips/in-out/speed，"
                   "也不带 tick_rate（PTR-2）；§3.1 的增量重算需要它们。",
        "handling": "构建时把 S41 的 clip 参数快照进 subtitles.clip_ref；"
                    "收到 EDL 事件后经 deps.build_timeline_source 读新 revision 的"
                    "clips 与 tick_rate。S41 不可达时事件处理返回 503 让上游重投，"
                    "不猜 tick_rate、不做部分应用。",
    },
    {
        "id": "S38-C4",
        "kind": "conflict",
        "summary": "execution-context.v2 强制 execution_fence 且 node_epoch，"
                   "而 §2.2 的 track DDL 两列都没有。",
        "handling": "track 存 execution_fence，cue 存 node_epoch，两列独立、"
                    "不互相赋值、不比较；测试断言两者可以不同（3 / 7）。",
    },
    {
        "id": "S38-C5",
        "kind": "gap",
        "summary": "契约里没有 locale profile（CPS/行长/安全区）的权威来源；"
                   "§4.4 的数字是方案默认值。",
        "handling": "命令的 profile 是收紧约束，内核再用方案默认值封顶；"
                    "实际使用的 profile 原样写入报告，便于 S39 解释阈值。",
    },
    {
        "id": "S38-C6",
        "kind": "gap",
        "summary": "没有「译文 reviwed/accepted 由谁推进」的契约；命令若可声明"
                   "accepted，就等于让调用方自证审核通过。",
        "handling": "命令只接受 draft/reviewed；accepted 只能由具备该权限的"
                    "内部方法写入（本服务不提供路由）。AGENTS.md：审核通过不是"
                    "放行依据，正式交付前仍要复查。",
    },
    {
        "id": "S38-C7",
        "kind": "gap",
        "summary": "qc-report.v2 的 shot_ref 是必填，但字幕报告是整轨级，"
                   "没有 shot 可言。",
        "handling": "用 track 自身填 shot_ref（shot_id=track_id、"
                    "shot_revision=subtitle_revision、logical_node_id=track_id、"
                    "node_epoch=track.node_epoch），并把这一映射写进报告摘要；"
                    "语义上不冒充镜头级结论。",
    },
    {
        "id": "S38-C8",
        "kind": "gap",
        "summary": "post.subtitle-ready.v2 的 stale_translation_count / cue_count "
                   "只有 x-needs-owner 标注，没有定义（是否含 stale 的 cue？"
                   "cue_count 是重算后还是原始？）。",
        "handling": "cue_count = 本次 revision 实际写入的 cue 数；"
                    "stale_translation_count = 状态为 stale 的译文 revision 所覆盖的"
                    "cue 数。两者都按这一口径写进报告摘要，等 owner 确认后改。",
    },
    {
        "id": "S38-C9",
        "kind": "gap",
        "summary": "契约不强制 artifacts[].artifact_id 属于 S28 的 ID 空间"
                   "（art_<ulid>），但 §3.0 要求字节由 S28 持有。",
        "handling": "内核校验归档回调返回的 artifact_id 必须以 art_ 开头，"
                    "否则 502 ARCHIVE_BAD_RESPONSE；不把任意字符串当 S28 指针。",
    },
    {
        "id": "S38-C10",
        "kind": "conflict",
        "summary": "§3.0 说归档失败后「不要重渲染」，但字节不存 S38，"
                   "重试只能重新生成字节；两句话只有在渲染确定性成立时才相容。",
        "handling": "重试按同一份 cue 与同一 RENDER_VERSION 重新渲染，"
                    "并与 pending_render.content_hash 逐字节比对；"
                    "不一致即 500 RENDER_NOT_DETERMINISTIC，不归档第二份。",
    },
    {
        "id": "S38-C11",
        "kind": "gap",
        "summary": "§7 的命令体没有 episode_id / branch_id / project_id / series_id，"
                   "但轨道与事件都需要它们。",
        "handling": "一律取 execution_context 的签名字段，不从请求体读；"
                    "请求体若另给了这些值则被忽略（不参与哈希、不参与比对）。",
    },
    {
        "id": "S38-C12",
        "kind": "gap",
        "summary": "方案 §7 传 dialogue_ref，但 §5 没有「对齐单元不在台词里」"
                   "这类检查；AUD.TEXT.* 已由 S37 负责。",
        "handling": "dialogue_ref 只用于覆盖度分母与缺陷的 expected_source 溯源，"
                    "S38 不另造检查项，也不越界判台词对错。",
    },
    {
        "id": "S38-C13",
        "kind": "gap",
        "summary": "S38_ENABLED 开关不在方案/契约里；本服务要求默认关闭。",
        "handling": "S38_ENABLED 默认 '0'，关闭时命令 403 SERVICE_DISABLED、"
                    "查询照常可读（只读面不因开关而丢失审计可见性）。",
    },
    {
        "id": "S38-C14",
        "kind": "gap",
        "summary": "契约里没有「译文如何进入 S38」的入口：§7 的命令体没有 "
                   "translations，但 cue 需要 translation_id/revision。",
        "handling": "命令体可选 localization.translations（只接受 draft/reviewed）；"
                    "同一 translation_id+revision 内容不同则 409，不覆盖。"
                    "等契约补入口后改为独立命令。",
    },
    {
        "id": "S38-C15",
        "kind": "gap",
        "summary": "§9.1 的 track 状态机里有 accepted，但没有「谁宣布 accepted」的"
                   "契约；而 AGENTS.md 禁止服务成为第二权威、禁止自行推进接受指针。",
        "handling": "内核没有任何代码路径写 accepted / qc_passed：状态只到 "
                    "rendered / archived / repair_pending。accepted 由 S02/S08 的"
                    "门决定，本服务只记录它的投影；测试断言 DDL 之外没有写入口"
                    "（V6 同类：无余额、无 accepted pointer、无第二份字节）。",
    },
    {
        "id": "S38-C16",
        "kind": "gap",
        "summary": "§6 要求 defect.evidence_artifact_ids 指向「渲染后的轨道 "
                   "artifact 与 diff 快照」，但 S28 不可达时本 revision 没有 "
                   "artifact_id，而 schema 强制 minItems 1。",
        "handling": "证据按优先级取已归档的 rendition artifact_id；一个都没有时"
                    "用 S37 对齐报告 id（它是时间码缺陷的原始证据，且形如 "
                    "qc_…，符合 id pattern）。不编造 art_ id，也不为空数组。",
    },
    {
        "id": "S38-C18",
        "kind": "gap",
        "summary": "S37 的 alignment unit 只带行内 char_offset/char_length，"
                   "没有 utterance 或行 id；契约里没有「unit 属于哪一行」的字段，"
                   "而 §4.1 的时码映射需要它。",
        "handling": "pair_units 按「逐行消费、偏移小于本行长度」把 unit 顺序"
                    "分配到对白行：偏移回退即换 utterance、前移一行；越过当前"
                    "行长即该行没有对齐单元，前移一行。没有单元的行贡献计划数、"
                    "不贡献 cue，于是 coverage_ratio < 1（E25-U4）。"
                    "等 S37 补行/utterance id 后改为直接 join。",
    },
    {
        "id": "S38-C19",
        "kind": "gap",
        "summary": "契约里没有「译文如何按源文片段切分」的规则：translation "
                   "给出的是整行译文，而 cue 是按源文片段切的，两者断句不必对齐。",
        "handling": "按源字符占比切整行译文（_translation_slice）。这是近似；"
                    "另一种做法（每条 cue 都放整行译文）会让相邻 cue 重复同一段"
                    "文字，比偏一点更糟。单段行拿到整句，多段行按比例。",
    },
    {
        "id": "S38-C17",
        "kind": "gap",
        "summary": "契约没有「字幕版式模板」（style_template_ref）的解析契约："
                   "ASS 需要字号/边距/字体，S38 拿不到权威值，而 "
                   "SUB.LAYOUT.FONT_MISSING / SUB.FORMAT.ASS_STYLE_MISSING 需要"
                   "「引用的样式是否存在」的事实。",
        "handling": "style_ref 只存命令给的模板引用原串（未给则填 "
                    "'tpl_sub_default'，与 §7 示例一致）；渲染时 ASS 样式块由 "
                    "模板引用 + profile 确定性生成，字体白名单取 profile 的 "
                    "fonts_available（缺省时 FONT_MISSING 记 "
                    "not_evaluated(dependency_unavailable)，不判通过）。",
    },
]
