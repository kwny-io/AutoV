# S38 · 字幕与本地化 —— 实现方案

状态：`design_only`。QCReport v2 规范在 [S35 §2](S35-technical-qc.md)。

---

## 1. 职责

S38 拥有 `SubtitleRevision / 字幕文本排版 / 语言 / 时间码 / 安全区与交付格式`。

它做两件在架构上必须分开的事：

1. **生成**：把 S37 的字级时间码 + 对白文本 → SRT/VTT/ASS 轨道
2. **校验**：产物进 QCReport（`SUB.*`），供 S39 消费

它**不做**：

- 不在视频模型里画字（service.yaml acceptance："字幕后期合成，不交给视频模型画字"；S35 的 `TECH.SUBTITLE.BURNED_IN` 就是抓这条被违反）
- 不拥有对白原文（S04/S06）
- 不拥有发音词典（S15）
- 不拥有译文（译文本体由本地化作业产出，但**版本由 S38 管理**）
- 不感知 EDL 的最终输出时间（EDL 归 S41；S38 只在收到 timeline 事件后重算）

---

## 2. 三种文本，三个版本（硬约束）

"原句 / 发音词典 / 译文分别版本化，不能混成一个字段"——这条在实现上意味着**三张独立的表、三条独立的 revision 链**。

| 对象 | 权威服务 | S38 的角色 | 版本标识 |
|---|---|---|---|
| **原句**（source text） | S04 剧本 / S06 ShotSpec | 只读引用 | `text_revision`（S06 的） |
| **发音词典**（pronunciation lexicon） | S15 | 只读引用 | `lexicon_revision` |
| **译文**（localized text） | S38 自己（本地化作业产物） | **拥有者** | `translation_revision` |
| 字幕版式/时码 | S38 | 拥有者 | `subtitle_revision` |

### 2.1 为什么不能合并

同一句"你终于来了"：
- 原句可能在 V3 改成"你总算来了"
- 发音词典可能在 V2 补了"终"的读音（多音字）
- 日文译文可能在 V5 从"やっと来たね"改成"やっと来たのね"

三者变化频率、审批人、失效条件都不同。合并成一个 `text` 字段的后果：

1. 原句改一个字，译文整个失效（但通常只有个别句受影响）
2. 词典改读音，译文不该变（但合并后无法区分）
3. 审计时答不出"这句洋文对应哪版中文"

### 2.2 数据模型

```sql
-- 字幕轨道（一次交付的容器）
CREATE TABLE subtitles.track (
  track_id              text PRIMARY KEY,
  tenant_id             text NOT NULL,
  branch_id             text NOT NULL,
  episode_id            text NOT NULL,
  timeline_id           text NOT NULL,          -- S41
  timeline_revision     integer NOT NULL,
  target_locale         text NOT NULL,          -- zh-CN / en-US / ja-JP ...
  subtitle_revision     integer NOT NULL,
  ...state, created_at
);

-- 单条字幕：三个文本字段各带自己的 revision，且都是外键
CREATE TABLE subtitles.cue (
  track_id              text NOT NULL REFERENCES subtitles.track(track_id),
  cue_id                text NOT NULL,           -- 稳定 ID，跨 revision 不变
  cue_index             integer NOT NULL,
  -- 原句
  script_line_id        text NOT NULL,           -- S06 的对白行 ID
  source_text_revision  integer NOT NULL,        -- S06 该行 revision
  source_text_hash      text NOT NULL,
  -- 发音词典
  lexicon_ref_id        text,                    -- S15; null = 未使用
  lexicon_revision      integer,
  lexicon_hash          text,
  -- 译文
  translation_id        text,                    -- null = target_locale == source_locale
  translation_revision  integer,
  translation_hash      text,
  -- 渲染文本（由三者派生，不独立版本化）
  display_text          text NOT NULL,
  -- 时间
  start_seconds         numeric(10,6) NOT NULL,  -- 输出时间（已按 EDL 转换）
  end_seconds           numeric(10,6) NOT NULL,
  alignment_report_id   text,                    -- S37 report
  align_unit_start      integer,                 -- S37 unit index 范围
  align_unit_end        integer,
  -- 版式
  line_count            integer NOT NULL,
  max_chars_per_line    integer NOT NULL,
  style_ref            text NOT NULL,            -- ASS style id
  safe_area_ok          boolean NOT NULL,
  PRIMARY KEY (track_id, cue_id)
);
CREATE UNIQUE INDEX ON subtitles.cue (track_id, cue_index);

-- 译文本体（S38 拥有）
CREATE TABLE subtitles.translation (
  translation_id     text NOT NULL,
  target_locale      text NOT NULL,
  revision           integer NOT NULL,
  source_text_revision integer NOT NULL,
  source_text_hash   text NOT NULL,
  content_hash       text NOT NULL,
  status             text NOT NULL CHECK (status IN ('draft','reviewed','accepted','stale')),
  translated_by      jsonb NOT NULL,             -- human / model + snapshot
  reviewer           jsonb,
  created_at         timestamptz NOT NULL,
  PRIMARY KEY (translation_id, revision)
);

CREATE TABLE subtitles.cue_text (
  translation_id     text NOT NULL,
  revision           integer NOT NULL,
  cue_id             text NOT NULL,
  text               text NOT NULL,
  PRIMARY KEY (translation_id, revision, cue_id)
);

-- 渲染产物目录：字节在 S28，这里只有指针（§3.0）
CREATE TABLE subtitles.rendition (
  track_id          text NOT NULL,
  subtitle_revision integer NOT NULL,
  format            text NOT NULL CHECK (format IN ('srt','vtt','ass')),
  artifact_id       text NOT NULL,              -- S28 的 artifact_id
  content_hash      char(64) NOT NULL,          -- 与 S28 manifest 一致
  byte_size         bigint NOT NULL,
  rendered_at       timestamptz NOT NULL,
  PRIMARY KEY (track_id, subtitle_revision, format),
  UNIQUE (artifact_id)
);
```

`rendition` 是 §3.0 的落库形态：一个 `subtitle_revision` 最多三种格式，每行只指向 S28。S38 的**数据库里不存任何 `.srt` 字节**，也不生成自己的下载 URL。

关键点：

- `cue` 里三个 hash 是**并列的三列**，不是拼成一个 JSON。数据库层面就强制它们独立。
- `display_text` 是**派生**的（`localize(source, lexicon, translation)`），不参与版本化——派生值有版本就会和输入版本冲突。
- `translation.status='stale'` 是核心状态：源句 revision 变了，已有译文自动标 stale（由消费 `S06` 对白 revision 事件触发），但**不删除**。stale 译文可预览，不可进正式交付。

### 2.3 失效传播

```text
S06 对白 revision V3 → V4
  └─ S38 收到事件，比对 source_text_hash
       ├─ 未变的 cue：不动
       └─ 变的 cue：translation.status → 'stale'
                     cue.source_text_revision → 4
                     cue.translation_revision 保留（不自动重译）
```

- **不自动重译**。重译要花钱（走 S26），要人验收（`service.yaml` recovery: "翻译新 revision 需验收"）。
- 交付门：`track` 里任何 cue 的译文 status 为 `stale` 或 `draft` → 该 track 不可进 `final_master` 门（S02 `required_gates`）。

词典变化同理：`lexicon_revision` 变了 → 该 cue 的 `display_text` 需重算，但**译文不变**。这是三个版本分开最直接的好处。

---

## 3. 流水线

```text
POST /subtitle-jobs
  │
  ├─ 1. 取输入：S37 字级时间码、S06 对白行、S15 词典、S41 timeline revision
  ├─ 2. 切分（segmentation）：
  │      按语义/标点/停顿把长句切成 cue；每 cue 挂 source_text_revision
  ├─ 3. 时码分配：
  │      cue.start = 首字 start - lead_in
  │      cue.end   = 末字 end + lead_out
  │      └─ 用 S37 的 CI 区间，不能只用点估计
  ├─ 4. 版式：每行字数/行数/阅读速度（CPS）约束下断行
  ├─ 5. 安全区：按目标画幅 + 平台规格算 safe area（纯几何，不看像素）
  ├─ 6. 本地化（可选）：target_locale ≠ source → 翻译作业（走 S26）→ draft
  ├─ 7. 渲染：SRT / VTT / ASS（ASS style 来自项目模板）
  │      └─ 产物**字节**交 S28 归档，S38 只存 artifact_id + content_hash
  ├─ 8. 自校验（SUB.* checks）→ QCReport(qc_layer=subtitle)
  └─ 9. 落库 + Outbox + post.subtitle.ready
```

### 3.0 为什么 depends_on 里有 S28

`service.yaml` 的 `depends_on` 是 `S37, S41, S28`。前两个来自输入链（时码、EDL），**S28 来自输出侧**：

`docs/01` 的唯一权威表：「制品bytes、hash、存储位置 → S28；业务域只保存 artifact_id 和内容 hash」。渲染出的 SRT/VTT/ASS 就是制品字节，与视频帧、音轨、探针截图同一类东西。S38 若把字幕文件写进自己的库或自己的对象存储路径，就成了第二个字节权威 —— 这正是 `AGENTS.md` 第一条禁止的事。

| 东西 | 谁存 | S38 存什么 |
|---|---|---|
| cue 文本、时码、三者版本引用 | S38 | 全部（§2.2 `subtitles.cue`） |
| 渲染后的 `.srt` / `.vtt` / `.ass` 字节 | **S28** | `artifact_id` + `content_hash` |
| QC 证据（diff 快照、字符级对比） | **S28** | `evidence_artifact_ids` |

`GET /subtitle-tracks/{id}/revisions/{rev}?format=…` 返回的签名 URL 指向 S28，不是 S38 自己发的 URL。取法同 S35：S28 manifest + 短签名 URL + 禁内网/SSRF。

**S28 不可达时。** 渲染可以完成，但**不发布 `post.subtitle.ready`** —— 事件要带 `artifact_ref`，没有 `artifact_id` 就发不出去。作业停在 `RENDERED_PENDING_ARCHIVE`，按 S28 的导入重试策略退避重试，**不重新渲染**（`docs/09`「下载失败不重生成」同理）。

### 3.1 EDL 变化只重算相关时码

`service.yaml` recovery："EDL 变化只重算相关时码"。

```text
收到 post.timeline.revision.created (S41)
  ├─ 计算 timeline 差分：哪些 clip 的 in/out/speed 变了
  ├─ 映射到受影响 cue 集合（按 clip 覆盖的 cue 时间区间）
  ├─ 只对受影响 cue 重算 start/end
  │    new_start = f(old_start, clip_old_in, clip_new_in, speed_ratio, ripple_offset)
  └─ 未受影响 cue 的时码原样保留（不重新对齐，不重新翻译）
```

`speed_ratio ≠ 1` 的 clip 会挤压对白 → 需要重新检查 `SUB.TIMING.CPS` 与 `SUB.TIMING.OVERLAP`，可能触发 `human_review`（语速容不下）。这条与 S37 的 `AUD.ALIGN.DURATION_VS_SPEC` 是同一类问题在不同层的表现，S39 归并。

---

## 4. 时间码规则

### 4.1 精度与取整

- 内部全部用 `numeric(10,6)` 秒。
- 输出 SRT：`HH:MM:SS,mmm`（毫秒，四舍五入）。
- 输出 VTT：`HH:MM:SS.mmm`。
- 输出 ASS：`H:MM:SS.cc`（厘秒）。**注意 ASS 只有厘秒精度**，四舍五入后必须重新检查重叠——这是最常见的生产事故来源。

### 4.2 lead-in / lead-out

```text
lead_in  = min(80ms, ci_low_start_gap)     // 不早于前一条 cue 结束
lead_out = min(120ms, ci_high_end_gap)
min_duration = 833ms                        // 20 CPS × 16 字符下限
max_duration = 7000ms                       // 超过则必须再切
```

`ci_low`/`ci_high` 来自 S37 的对齐置信区间，不用点估计——否则对齐抖动会让字幕"闪"。

### 4.3 CPS 与行数

```text
cps = len(display_text, 按 locale 计宽) / duration_seconds
限制：cps ≤ locale_profile.max_cps（中文 9，英文 17，日文 8，默认）
     line_count ≤ locale_profile.max_lines（默认 2）
     chars_per_line ≤ locale_profile.max_chars_per_line（中文 16，英文 42）
```

超限的处理顺序（**不许静默缩字**）：

1. 重新切分（拆成两条 cue）
2. 延长 cue 到 `max_duration` 边界（不越过相邻 cue）
3. 仍超 → `SUB.LAYOUT.CPS_EXCEEDED` 缺陷，推荐动作 `human_review`（可能是台词太长，属创作决策）

---

## 5. 检查项注册表（S38，前缀 `SUB.`）

### 5.1 时间

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `SUB.TIMING.OVERLAP` | subtitle | **blocker** | 同一 track 内 cue 时间重叠（含取整后重叠） |
| `SUB.TIMING.NEGATIVE_DURATION` | subtitle | **blocker** | end ≤ start |
| `SUB.TIMING.TOO_SHORT` | subtitle | major | duration < `min_duration` |
| `SUB.TIMING.TOO_LONG` | subtitle | minor | duration > `max_duration` |
| `SUB.TIMING.CPS_EXCEEDED` | subtitle | major | cps > profile 上限 |
| `SUB.TIMING.OUT_OF_RANGE` | subtitle | **blocker** | cue 落在 timeline 之外（EDL 变更残留） |
| `SUB.TIMING.DRIFT_VS_ALIGNMENT` | subtitle | major | cue 边界与 S37 对齐结果偏差 > 100ms（EDL 变换后应当一致） |
| `SUB.TIMING.FRAME_BOUNDARY` | subtitle | info | cue 起点不在帧边界上（某些交付规格要求） |

### 5.2 版式

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `SUB.LAYOUT.LINE_OVERFLOW` | subtitle | major | 单行字符数超 profile |
| `SUB.LAYOUT.TOO_MANY_LINES` | subtitle | major | 行数超 profile |
| `SUB.LAYOUT.SAFE_AREA` | subtitle | major | 文本几何框超出安全区 |
| `SUB.LAYOUT.ORPHAN_WORD` | subtitle | info | 末行只有 1 个词/字 |
| `SUB.LAYOUT.FONT_MISSING` | subtitle | **blocker** | ASS 引用的字体不在交付白名单 |

`SUB.LAYOUT.SAFE_AREA` 只做几何计算（看坐标）；**渲染后是否压到画面元素由 S36 的 `VIS.SAFE_AREA.TEXT_OVERLAP` 判**。两者不重复：一个算坐标，一个看像素。

### 5.3 文本与版本

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `SUB.TEXT.SOURCE_STALE` | subtitle | **blocker**（正式交付） | `source_text_revision` 落后于 S06 当前 revision |
| `SUB.TEXT.TRANSLATION_STALE` | subtitle | **blocker**（正式交付） | `translation.status='stale'` |
| `SUB.TEXT.TRANSLATION_DRAFT` | subtitle | major | `translation.status='draft'` |
| `SUB.TEXT.TRANSLATION_MISSING` | subtitle | **blocker** | target_locale ≠ source 但无译文 |
| `SUB.TEXT.TRANSLATION_LENGTH_DRIFT` | subtitle | major | 译文长度 / 源文长度 超出 locale 对阈值（中日英之间的正常比例之外） |
| `SUB.TEXT.LEXICON_UNUSED` | subtitle | minor | 存在多音字但未引用词典 revision |
| `SUB.TEXT.SCRIPT_CONFLICT` | subtitle | info | 与 S37 `AUD.TEXT.SCRIPT_CONFLICT` 关联时只记录，不判错 |

`SUB.TEXT.SOURCE_STALE` 是"原句版本化"的落地检查点：它把"字幕和剧本不同步"变成可机检的阻断项，而不是靠人眼。

### 5.4 格式

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `SUB.FORMAT.SRT_SYNTAX` | subtitle | **blocker** | SRT 结构不合法（序号/时间行/空行） |
| `SUB.FORMAT.ENCODING` | subtitle | **blocker** | 非 UTF-8（或目标平台要求的编码） |
| `SUB.FORMAT.ASS_STYLE_MISSING` | subtitle | **blocker** | 引用了未定义的 style |
| `SUB.FORMAT.BOM` | subtitle | info | 存在意外 BOM |

---

## 6. 字幕 QC 报告（`qc_layer="subtitle"`）

S38 的 QCReport 与 S35/S36/S37 **同一 schema**。特殊之处：

- `subject.artifact_kind = "subtitle_track"`，`subject.media.video/audio = null`，`duration_seconds` 取 timeline 时长。
- `coverage`：字幕没有帧/窗口概念，用如下映射
  - `frames_sampled = 0`
  - `planned_windows = cue 总数`，`temporal_windows_evaluated = 已校验 cue 数`
  - `coverage_ratio = 已校验 / 总数`
  - `audio_analyzed = false`
- `defect.time_range` 填 cue 的输出时间区间；`region_bbox` 填安全区几何框（画幅归一化）或 null。
- `defect.evidence_artifact_ids` 指向渲染后的轨道 artifact 与 diff 快照。

这样 S39 用**一个解析器**就能读四类报告。这是"统一 QCReport"的直接收益，也是为什么 §2 的契约只写一份。

---

## 7. API 与事件

### `POST /subtitle-jobs`

```json
{
  "command_id": "cmd_…",
  "execution_context": { "...": "execution-context v2" },
  "timeline_ref": { "service_id": "S41", "id": "tl_ep3", "revision": 9, "content_hash": "…" },
  "alignment_ref": { "service_id": "S37", "id": "qc_…", "revision": 1, "content_hash": "…" },
  "dialogue_ref": { "service_id": "S06", "id": "shot_3_2", "revision": 4, "content_hash": "…" },
  "target_locale": "zh-CN",
  "source_locale": "zh-CN",
  "profile": {
    "max_cps": 9, "max_lines": 2, "max_chars_per_line": 16,
    "format": ["srt", "vtt", "ass"],
    "safe_area": { "bottom_pct": 0.10, "side_pct": 0.05 },
    "style_template_ref": { "id": "tpl_sub_default", "revision": 2 }
  },
  "localization": { "translate": false, "reviewer_required": true },
  "repair_context": null,
  "deadline": "..."
}
```

### `GET /subtitle-tracks/{id}`

返回 track + cue 列表 + 三版本引用 + 当前 QCReport 引用。

### `GET /subtitle-tracks/{id}/revisions/{revision}?format=srt|vtt|ass`

返回渲染产物（通过签名 URL 或流）。

### 事件 `post.subtitle.ready`

```json
{
  "data": {
    "track_id": "sub_…", "subtitle_revision": 3,
    "episode_id": "ep3", "branch_id": "canon",
    "timeline_ref": { "id": "tl_ep3", "revision": 9 },
    "target_locale": "zh-CN",
    "cue_count": 142,
    "formats": ["srt", "vtt", "ass"],
    "artifacts": [                                   // 字节在 S28，这里只有指针
      { "format": "srt", "artifact_id": "art_…", "content_hash": "…" },
      { "format": "vtt", "artifact_id": "art_…", "content_hash": "…" },
      { "format": "ass", "artifact_id": "art_…", "content_hash": "…" }
    ],
    "qc_report_ref": { "service_id": "S38", "id": "qc_…", "revision": 1, "content_hash": "…" },
    "qc_decision": "repair",
    "defect_count": 2,
    "stale_translation_count": 0
  },
  "data_ref": { "service_id": "S38", "id": "sub_…", "revision": 3, "content_hash": "…" }
}
```

`event_type` 名字是 `post.subtitle.ready`（既有 catalog），但语义包含"已自校验"。**不新增 topic**：缺陷随 `data.qc_report_ref` 携带，S39 直接读。这是 [S35 §12 C4](S35-technical-qc.md) 的答案。

---

## 8. 版本化对象与查询

S38 需要从 S15 读发音词典，但 `contracts` 里没有词典的查询契约。按 AGENTS.md 应报告而不自行补：

**需要的 query**：`GET /voices/{id}/lexicons?revision=N` 返回词条列表（词、音素或拼音、语言、生效范围）。

在补齐之前：S38 以 `lexicon_ref` 为不可解析引用，`SUB.TEXT.LEXICON_UNUSED` 记 `dependency_unavailable`，不自己维护一份词典（那是第二权威）。

---

## 9. 状态机

```text
track.state:
  draft ──(渲染完成)──▶ rendered ──(S28 归档成功)──▶ archived
    │                      │
    │                      └──(S28 不可达)──▶ rendered_pending_archive ──(重试成功)──▶ archived
    │
  archived ──(自校验 pass)──▶ qc_passed
    │                          │
    │                          ├──(S02 门通过)──▶ accepted
    │                          └──(S39 修复)────▶ repair_pending ──▶ draft
    └──(自校验 fail)──────────▶ repair_pending

translation.status:
  draft ──(reviewer approve)──▶ reviewed ──(accept)──▶ accepted
    │                                                      │
    └────────────(source_text_revision 变化)──────────────▶ stale
```

交付门只认 `track.state='accepted'` 且无 `stale`/`draft` 译文（对应 `SUB.TEXT.*_STALE` blocker）。

---

## 10. 测试清单

### 10.1 E24 —— 字幕缺陷、证据时间段、coverage、复检

| # | 用例 | 断言 |
|---|---|---|
| E24-U1 | 两条 cue 时间重叠 30ms | `SUB.TIMING.OVERLAP` blocker；两条 cue 的 time_range 都在证据里 |
| E24-U2 | ASS 厘秒取整后重叠 | `SUB.TIMING.OVERLAP` 检出（证明取整后复检有效） |
| E24-U3 | 15 字中文 cue 只有 600ms | `SUB.TIMING.CPS_EXCEEDED` major |
| E24-U4 | 台词 40 字塞不进行数限制 | 先切分；仍不行 → `SUB.LAYOUT.LINE_OVERFLOW` + human_review |
| E24-U5 | 原句 V3→V4 后未重出字幕 | `SUB.TEXT.SOURCE_STALE` blocker |
| E24-U6 | 译文缺失但 target≠source | `SUB.TEXT.TRANSLATION_MISSING` blocker |
| E24-U7 | EDL 缩短 1 秒导致 cue 落到 timeline 外 | `SUB.TIMING.OUT_OF_RANGE` blocker |
| E24-U8 | EDL 只改一个 clip，只重算相关 cue | 其他 cue 的 start/end 逐字段不变（对比断言） |
| E24-U9 | 修复时码后复检 | `repair_context` 指向 slot；时间码 defects 消失，`decision=pass` |
| E24-U10 | 字幕被烧进画面 | 由 S35 `TECH.SUBTITLE.BURNED_IN` 报；S38 不重复报 |

### 10.2 E25 —— 超时与冲突

| # | 用例 | 断言 |
|---|---|---|
| E25-U1 | S37 对齐报告 abstain 时请求字幕 | S38 仍出草稿但 `qc_decision=abstain`；不得标 `qc_passed` |
| E25-U2 | 翻译作业超时 | `SUB.TEXT.TRANSLATION_MISSING`（不是"空译文但 pass"） |
| E25-U3 | 词典服务不可达 | `SUB.TEXT.LEXICON_UNUSED` = `dependency_unavailable` |
| E25-U4 | 校验只覆盖 70% cue | `coverage_ratio=0.70` → `decision=abstain` |
| E25-U5 | S28 归档超时 | track 停在 `rendered_pending_archive`；**不发** `post.subtitle.ready`；S28 恢复后补发，**不重新渲染** |

### 10.3 版本独立性（硬约束专项）

| # | 用例 | 断言 |
|---|---|---|
| V1 | 只改原句 revision | `source_text_revision` 变；`lexicon_revision`/`translation_revision` 不变；译文标 stale |
| V2 | 只改词典 revision | `lexicon_revision` 变；`display_text` 可能变；`translation_revision` 与 status 不变 |
| V3 | 只改译文 revision | 前两者不变；`translation.status` → `reviewed` |
| V4 | 数据库层断言 | `cue` 三列均为独立列，无合并的 `text` 字段（schema 测试） |
| V5 | 审计 | 给定 cue 能取出三组 `(id, revision, hash)` 并分别定位来源 |
| V6 | 字节权威 | 扫 S38 库与对象存储，**无任何 `.srt`/`.vtt`/`.ass` 正文**；只有 `rendition.artifact_id` 指向 S28 |

### 10.4 E26 —— 跨 Run

| # | 用例 | 断言 |
|---|---|---|
| E26-U1 | Run 2 重生成字幕 | `shot_lineage_id` 不变；新 track revision |
| E26-U2 | 同字幕缺陷在 Run 2 复现 | `defect_fingerprint` 相同 → S39 计数 +1 |

---

## 11. 契约缺口

| # | 缺口 | 建议 |
|---|---|---|
| C1 | 无 `contracts/quality/subtitle-track.v2.schema.json` | 新增；`cue` 的三版本列必须进 schema，否则只是实现约定 |
| C2 | S15 无发音词典查询契约 | 见 §8；补 `GET /voices/{id}/lexicons?revision=N` |
| C3 | S41 timeline 事件的 payload 未定义差分语义 | 补 `post.timeline.revision.created` 的 payload schema，含 affected clip 列表 |
| C4 | `post.subtitle.ready` 无 payload schema，`artifacts[]` 的 `artifact_id` 归属未写明 | 随 C1 一起补；`artifact_id` 必须是 S28 的 ID 空间，不能是 S38 自定义 ID |

> **G2 已关闭（2026-11-01）**：`depends_on` 里的 S28 不是声明过宽，而是方案漏写了输出侧这一环。S38 渲染出的字幕文件是制品字节，按 `docs/01` 只能由 S28 存。catalog 不改，方案补 §3.0 / `subtitles.rendition` / `artifacts[]` / `V6` / `E25-U5`。
| C4 | `contracts/event-topics.v2.json` 的 `post.subtitle.ready` 无 payload schema | 按 §7 补 |
| C5 | 无 locale profile 契约（CPS/行数/安全区） | 新增 `contracts/quality/locale-profile.v2.json`，按 locale 定义，随 policy 版本化 |
