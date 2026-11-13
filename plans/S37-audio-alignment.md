# S37 · 音频理解与对齐质检 —— 实现方案

状态：`design_only`。QCReport v2 规范在 [S35 §2](S35-technical-qc.md)。

---

## 1. 职责与数据所有权

S37 拥有 `ASR / forced alignment / 字音素时间码 / 说话人 / 音画偏移与音色评估`。

它回答三个不同的问题，**必须分开出证据**（service.yaml acceptance: "错读/漏词与字幕偏移有独立证据"）：

| 问题 | 产出 | 消费方 |
|---|---|---|
| 说的词对不对 | ASR 文本 vs 脚本对白 | S39（`voice` 缺陷） |
| 每个字在什么时间 | 字级/音素级时间码 + 置信区间 | S38（字幕时码）、S32（口型驱动） |
| 嘴和声音对不对得上 | 音画偏移、口型开合与音素匹配 | S39（`lipsync` 缺陷） |

三者共用一个 artifact，但**必须产生彼此独立的 defect**。把它们合成一个"音频得分"会让 S39 无法选动作（重配 vs 重对齐 vs 重口型是完全不同的修复）。

S37 **不拥有对白文本**。对白原始文本的权威在 S06（ShotSpec）/ S04（剧本），发音词典在 S15，字幕文本在 S38。S37 只负责"这段话在时间轴上长什么样"。

---

## 2. 流水线

```text
POST /alignment-jobs
  │
  ├─ 1. 校验上下文；取 artifact（音轨/视频）、ShotSpec 对白、S15 发音词典
  ├─ 2. 分离音轨（视频输入时）：保留原始时间基准，记录 stream offset
  ├─ 3. ASR（走 S26 understanding job）
  │      └─ 多语言/多说话人；输出 token 级 posterior
  ├─ 4. 说话人分离（本地，无模型 or 走 S26，取决于部署）
  ├─ 5. Forced alignment：ASR/脚本文本 × 音频 → 字/音素时间码
  │      ├─ 有发音词典 → 词典约束对齐（更准）
  │      └─ 无词典 → 声学对齐 + 置信区间
  ├─ 6. 口型评估（有视频时）：face track + 唇部 ROI × 音素时间码
  ├─ 7. 音色评估：与 S15 voice binding 的 embedding 距离
  ├─ 8. 组装 defects：voice / lipsync / temporal / subtitle-offset
  ├─ 9. ReportValidator → decide() → 落库 + Outbox
  └─ 10. quality.alignment.completed
```

### 2.1 时间基准（最容易出错的地方）

全链路只能有一个时间基准：

```text
基准 = 容器 presentation timeline（PTS），单位秒，浮点
       start_seconds 相对该轨道的第一个呈现时刻
```

- 视频输入时：`TECH.AUDIO.OFFSET` 已经由 S35 报过 stream 偏移。S37 若是复检，必须扣除已知偏移；不能把 S35 已报的偏移再报一次。
- S38 消费 S37 时间码时，也在这个基准上，最后由 S38 按 EDL ripple 转成输出时间。**S37 不感知 EDL**。

`sound.sample_ts` 与视频帧的换算：

```text
frame_index = round(presentation_seconds * fps_num / fps_den)
```

VFR 素材（S35 已报 `TECH.FPS.VFR`）→ S37 拒绝做口型检查，abstain(`evaluator_error`)，理由 `vfr_unsupported`，因为帧-时间映射不唯一。

---

## 3. 检查项注册表（S37，前缀 `AUD.`）

### 3.1 语音内容

| check_id | type | 严重度 | 判定 | 证据 |
|---|---|---|---|---|
| `AUD.TEXT.MISSING_WORD` | voice | major | 脚本有、ASR 无（且非 `not_applicable` 场景） | 漏词区间 + ASR token |
| `AUD.TEXT.EXTRA_WORD` | voice | major | ASR 有、脚本无 | 区间 + token |
| `AUD.TEXT.MISPRONOUNCE` | voice | major | 字面一致但音素序列与 S15 词典不符（或 ASR 置信度下仍判不同音） | 音素对比 |
| `AUD.TEXT.SUBSTITUTION` | voice | **blocker**（关键台词） | 念成另一个词且改变语义 | 双端 token + 语义差异确认 |
| `AUD.TEXT.PROSODY_FLAT` | voice | minor | 情感/重音与 ShotSpec 情绪标签严重不符 | 韵律特征 |
| `AUD.TEXT.SCRIPT_CONFLICT` | — | — | **不产生 defect**：文本冲突不自动改剧本（service.yaml recovery） | 只记 info dimension |

最后一行是纪律：ASR 说"我听到的是 B"，脚本说"A"，S37 不下结论谁对，只上报观测。人类或 S39 决定是重配还是改稿。

### 3.2 时间与对齐

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `AUD.ALIGN.LOW_CONFIDENCE` | temporal | major | 对齐路径存在置信区间宽于 `max_align_ci_ms`（默认 120ms）的字 |
| `AUD.ALIGN.GAP` | temporal | minor | 字间出现非语音间隙而脚本无停顿标记 |
| `AUD.ALIGN.OVERLAP` | temporal | major | 同一说话人 token 时间重叠（对齐失败信号） |
| `AUD.ALIGN.SPEAKER_SWITCH` | voice | major | 说话人变更点与脚本说话人标记不符 |
| `AUD.ALIGN.DURATION_VS_SPEC` | temporal | major | 对白实际时长与 ShotSpec `dialogue_duration` 偏差 > `dialogue_duration_tolerance_ms`（默认 400ms） |

`AUD.ALIGN.DURATION_VS_SPEC` 是 S38 与 S39 之间的关键信号：台词比镜头长，修复选项是"缩台词/加快语速/加长镜头/拆镜"，属于**创作决策**，不能自动选。S37 只报偏差，S39 据此判 `human_review`（见 [S39 §3.3](S39-repair-planning.md)）。

### 3.3 口型（需要视频 + face track）

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `AUD.LIPSYNC.OFFSET` | lipsync | **blocker**（>120ms）/ major（>60ms） | 唇部闭合事件与音素爆破点的时间差 |
| `AUD.LIPSYNC.OPENNESS_MISMATCH` | lipsync | major | 张口幅度与元音开口度相关性低于阈值 |
| `AUD.LIPSYNC.MOUTH_MOVES_SILENT` | lipsync | major | 静音段嘴部持续运动 |
| `AUD.LIPSYNC.SPEAKS_CLOSED` | lipsync | **blocker** | 有声段嘴部完全静止（明显没在对口型） |
| `AUD.LIPSYNC.FACE_NOT_FOUND` | lipsync | — | `not_applicable`，reason=`no_face` |

**必须用连续音视频**（service.yaml acceptance: "口型检查使用连续音视频"）。抽帧比对只能发现 `SPEAKS_CLOSED` 这种粗粒度问题，`OFFSET` 必须逐帧。因此口型 check 的 `planned_windows` = 对白区间的帧数，`coverage_ratio` 按帧算，且这条 check 的 `required_coverage_min` 默认 0.95（比其他 check 严）。

### 3.4 音色

| check_id | type | 严重度 | 判定 |
|---|---|---|---|
| `AUD.VOICE.EMBEDDING_DRIFT` | voice | **blocker** | 与 S15 binding 的音色 embedding 距离 > τ；连续 3 窗口命中才升级 |
| `AUD.VOICE.CHARACTER_MIXUP` | voice | **blocker** | 同一角色在不同镜头音色显著不同（跨镜） |
| `AUD.VOICE.EMOTION_MISMATCH` | voice | minor | 情绪与 ShotSpec 不符 |
| `AUD.VOICE.AUTHORIZATION` | voice | **blocker** | 音色授权已撤销（S15 事件投影）却仍被使用 |

`AUD.VOICE.AUTHORIZATION` 的特殊性：它不是内容缺陷，是权利缺陷。命中时 `recommended_actions` 只能是 `human_review`（不能自动重生成——重生成可能用同一个已撤销音色）。

---

## 4. ASR 与对齐的置信度传播

S37 的输出有两个层次的置信度，必须分开：

```text
token_confidence      = ASR posterior（逐 token）
alignment_confidence  = forced alignment 的路径置信度（逐音素）
defect.confidence     = 该 defect 的证据链最弱环节
```

规则：

1. `defect.confidence = min(所有支撑证据的置信度)`。不允许"多数证据高置信"就抬高。
2. `alignment_confidence < 0.5` 的字，不参与 `AUD.TEXT.MISPRONOUNCE` 判定（宁可 `not_applicable` 不误判）。
3. 若被跳过的 token 超过该镜头的 `max_unjudged_token_ratio`（默认 10%），该 check 整体 `failed`，reason=`evaluator_error` → abstain。

第 3 条防止"每个字都不确定，所以一个字都没判，最后 pass"。

### 4.1 时间码精度

对 S38 的承诺：字级时间码精度 `±40ms`（对齐置信度 ≥0.8 时），音素级 `±20ms`。S38 需要按此设计字幕卡拉长规则（[S38 §4](S38-subtitles.md)）。

置信度区间必须**随报告一起交给 S38**，不能让 S38 猜。

---

## 5. 数据模型（schema `audio_alignment`）

```sql
CREATE TABLE audio_alignment.report (
  report_id           text PRIMARY KEY,
  report_generation   integer NOT NULL,
  tenant_id           text NOT NULL,
  shot_lineage_id     text NOT NULL,
  shot_id             text NOT NULL,
  episode_id          text NOT NULL,
  branch_id           text NOT NULL,
  artifact_sha256     text NOT NULL,
  audio_track_hash    text NOT NULL,
  manifest_id         text NOT NULL,
  manifest_revision   integer NOT NULL,
  run_id              text NOT NULL,
  execution_fence     integer NOT NULL,
  node_epoch          integer NOT NULL,
  language            text NOT NULL,
  asr_model_snapshot  text NOT NULL,
  align_model_version text NOT NULL,
  lexicon_revision    integer,              -- S15 发音词典 revision，null = 未使用
  voice_binding_ref   jsonb,
  evaluator_version   text NOT NULL,
  policy_version      text NOT NULL,
  decision            text NOT NULL,
  coverage_ratio      numeric(5,4) NOT NULL,
  attempt_slot_id     text,
  payload             jsonb NOT NULL,
  content_hash        text NOT NULL,
  created_at          timestamptz NOT NULL,
  UNIQUE (report_id, report_generation)
);
CREATE INDEX ON audio_alignment.report (tenant_id, branch_id, shot_lineage_id, created_at DESC);
CREATE INDEX ON audio_alignment.report (artifact_sha256);

-- 字级时间码：S38 的主输入，独立表便于按区间查询
CREATE TABLE audio_alignment.alignment_unit (
  report_id        text NOT NULL,
  unit_index       integer NOT NULL,
  utterance_index  integer NOT NULL,
  speaker_id       text,
  char_offset      integer NOT NULL,        -- 在 utterance 文本中的偏移
  char_length      integer NOT NULL,
  start_seconds    numeric(10,6) NOT NULL,
  end_seconds      numeric(10,6) NOT NULL,
  ci_low_seconds   numeric(10,6),
  ci_high_seconds  numeric(10,6),
  confidence       numeric(5,4) NOT NULL,
  phoneme_seq      text[],                  -- 可选，口型评估用
  PRIMARY KEY (report_id, unit_index)
);
CREATE INDEX ON audio_alignment.alignment_unit (report_id, start_seconds);

CREATE TABLE audio_alignment.speaker_segment (
  report_id     text NOT NULL,
  segment_index integer NOT NULL,
  speaker_id    text NOT NULL,
  start_seconds numeric(10,6) NOT NULL,
  end_seconds   numeric(10,6) NOT NULL,
  confidence    numeric(5,4) NOT NULL,
  PRIMARY KEY (report_id, segment_index)
);

CREATE TABLE audio_alignment.evaluator_call (
  call_id        uuid PRIMARY KEY,
  report_id      text NOT NULL,
  check_id       text NOT NULL,
  window_index   integer,
  model_snapshot text NOT NULL,
  prompt_hash    char(64) NOT NULL,
  tokens_in      integer NOT NULL,
  tokens_out     integer NOT NULL,
  latency_ms     integer NOT NULL,
  outcome        text NOT NULL CHECK (outcome IN ('OK', 'ERROR', 'TIMEOUT')),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audio_alignment.idempotency_record (
  tenant_id    text NOT NULL,
  command_id   text NOT NULL,
  request_hash char(64) NOT NULL,
  command_type text NOT NULL,
  state        text NOT NULL CHECK (state IN ('IN_PROGRESS', 'DONE')),
  response_code integer,
  response_body jsonb,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, command_id)
);

CREATE TABLE audio_alignment.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,
  aggregate_id       text NOT NULL,             -- report_id
  aggregate_sequence bigint NOT NULL,
  event_type         text NOT NULL,
  partition_key      text NOT NULL,          -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON audio_alignment.outbox (id) WHERE published_at IS NULL;

CREATE TABLE audio_alignment.inbox (
  consumer    text NOT NULL,
  event_id    text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
```

`aggregate_id = report_id`，`aggregate_sequence = report_revision`。

`alignment_unit` 单独的表的理由：字幕作业要按频繁的时间区间查询，塞在 `payload` JSON 里既慢又难保证精度约束。且这些数据会被 S38 引用——**通过契约读取，不是跨库 JOIN**（S38 调 `GET /alignment-reports/{id}/units?start=…&end=…`）。

---

## 6. API

### `POST /alignment-jobs`

```json
{
  "command_id": "cmd_…",
  "execution_context": { "...": "execution-context v2" },
  "artifact": { "service_id": "S28", "id": "clip_x", "revision": 1, "content_hash": "…" },
  "audio_source": { "mode": "embedded", "stream_index": 1 },
  "shot_spec_ref": { "service_id": "S06", "id": "shot_3_2", "revision": 4, "content_hash": "…" },
  "dialogue": {
    "language": "zh-CN",
    "utterances": [
      { "utterance_id": "u1", "speaker_character_id": "char_linlan",
        "text": "你终于来了。", "text_revision": 2,
        "lexicon_ref": { "service_id": "S15", "id": "voice_linlan", "revision": 2, "content_hash": "…" },
        "expected_duration_ms": 1200 }
    ]
  },
  "voice_binding_ref": { "service_id": "S15", "id": "voice_linlan", "revision": 2, "content_hash": "…" },
  "checks": { "include": ["AUD.TEXT.*", "AUD.ALIGN.*", "AUD.LIPSYNC.*", "AUD.VOICE.*"] },
  "known_audio_offset_seconds": 0.021,
  "repair_context": null,
  "deadline": "..."
}
```

`audio_source.mode`：

- `embedded`：从视频容器取音轨（口型检查必须用这个，因为要与视频同基准）
- `external`：独立音频 artifact（配音 stem，用于 S38 前的预对齐）

`known_audio_offset_seconds`：S35 已报的容器偏移，S37 扣除后再评估，避免重复报同一缺陷。

### `GET /alignment-reports/{id}` — 报告

### `GET /alignment-reports/{id}/units?start=1.2&end=3.4` — 字级时间码

返回：

```json
{
  "report_id": "qc_…",
  "time_base": "presentation_pts",
  "units": [
    { "index": 0, "char_offset": 0, "char_length": 1, "start": 1.230, "end": 1.395,
      "ci_low": 1.215, "ci_high": 1.410, "confidence": 0.94 }
  ],
  "speaker_segments": [ { "speaker_id": "char_linlan", "start": 1.210, "end": 3.180, "confidence": 0.91 } ]
}
```

这个接口是 S38 的唯一时间码来源。

---

## 7. 事件 `quality.alignment.completed`

```json
{
  "data": {
    "report_id": "qc_…", "qc_layer": "alignment",
    "artifact_sha256": "…", "audio_track_hash": "…",
    "shot_id": "shot_3_2", "shot_lineage_id": "lin_…",
    "decision": "repair",
    "defect_count": 2, "blocker_count": 1,
    "coverage_ratio": 0.97,
    "language": "zh-CN",
    "max_lipsync_offset_ms": 142,
    "voice_embedding_distance": 0.08,
    "alignment_unit_count": 47,
    "attempt_slot_id": null
  },
  "data_ref": { "service_id": "S37", "id": "qc_…", "revision": 1, "content_hash": "…" }
}
```

S38 依赖这个事件触发字幕作业（`service.yaml` 的 `depends_on: S37`）。所以 `data` 里必须带够信息让 S38 判断要不要等：`decision` 为 `pass`/`repair` 都可以先出字幕草稿，`abstain` 时 S38 必须等复检或标草稿。

---

## 8. 幂等、隔离与恢复

| 项 | 做法 |
|---|---|
| 幂等键 | `tenant_id + command_id + request_hash`；报告派生键含 `audio_track_hash`（音轨单独 hash，视频重编码不改音轨时不重算 ASR） |
| S26 调用幂等 | `idempotency_key = qc:s37:{audio_track_hash}:{utterance_id}:{asr_model_snapshot}` |
| 音轨单独 hash | 让"视频重新编码但音轨没变"能复用 ASR 结果，省一大笔 VLM/ASR 费用 |
| 跨租户 | RLS + 404 不泄漏 |
| 坏音频 | 无声/损坏 → `abstain(asset_unavailable)`；不产生"全静音 = 全漏词"的假 defects |
| 恢复 | 对齐结果可重算（确定性算法），ASR 结果按 `audio_track_hash` 缓存复用 |

### 8.1 无对白镜头

ShotSpec 声明 `dialogue: []` → **显式 no-op**（docs/07 §1.5：不创建空音频假成功）。报告仍然生成：`check_manifest` 全部 `not_applicable`, `reason_code=no_dialogue`，`decision=pass`，`coverage.audio_analyzed=false`。

这与"未知当 pass"不冲突——它是有可验证理由的、注册表允许的 `not_applicable`，与 `not_evaluated` 严格区分。

---

## 9. 测试清单

### 9.1 E24 —— 口型与字幕偏移缺陷、证据时间段、coverage

| # | 用例 | 断言 |
|---|---|---|
| E24-A1 | 口型整体偏移 150ms | `AUD.LIPSYNC.OFFSET` blocker；`measured={142, ms, 120, gt}`；`time_range` 覆盖全对白段 |
| E24-A2 | 口型偏移 80ms | major，不 blocker |
| E24-A3 | 有声段嘴完全不动 | `AUD.LIPSYNC.SPEAKS_CLOSED` blocker |
| E24-A4 | 静音段嘴在动 | `AUD.LIPSYNC.MOUTH_MOVES_SILENT` major |
| E24-A5 | 台词漏一个词 | `AUD.TEXT.MISSING_WORD`；区间精确到该词；ASR token 作证据 |
| E24-A6 | 台词念错音（多音字） | `AUD.TEXT.MISPRONOUNCE`；与 S15 lexicon revision 关联 |
| E24-A7 | 台词比镜头长 800ms | `AUD.ALIGN.DURATION_VS_SPEC` major；S39 转 human_review |
| E24-A8 | 只有 60% 帧做了口型检查 | `coverage_ratio=0.60 < required 0.95` → abstain |
| E24-A9 | VFR 视频 | abstain(`evaluator_error`, `vfr_unsupported`)，不硬算 |
| E24-A10 | 无对白镜头 | 全部 `not_applicable`，`decision=pass`，`audio_analyzed=false` |
| E24-A11 | 修复后复检通过 | 新报告 `pass`；`repair_context` 指向上次 slot；时间码重新生成 |

### 9.2 E25 —— 评估器超时与冲突

| # | 用例 | 断言 |
|---|---|---|
| E25-A1 | ASR 超时 | abstain(`evaluator_timeout`)；`evaluator_call.outcome='timeout'` |
| E25-A2 | 对齐置信度全线低（>10% token 被跳过） | check failed → abstain |
| E25-A3 | ASR 与脚本冲突（ASR 说 B，脚本 A） | 只产生观测，不出"改剧本"建议；`AUD.TEXT.SCRIPT_CONFLICT` 记 dimension |
| E25-A4 | S15 不可达 | `AUD.VOICE.*` = `dependency_unavailable` → abstain |

### 9.3 E26 —— 跨 Run 返工

| # | 用例 | 断言 |
|---|---|---|
| E26-A1 | Run 2 重做口型 | 新报告 `shot_lineage_id` 不变；`defect_fingerprint` 若仍同 → S39 计数 +1 |
| E26-A2 | 音轨未变、视频重编码 | ASR 结果从缓存复用（`audio_track_hash` 相同），不重复计费 |

### 9.4 独立性验收

| # | 用例 | 断言 |
|---|---|---|
| I1 | S36 与 S37 对同一镜头的口型结论 | 以 S37 为准；S36 不注册任何 `lipsync` check（registry 断言） |
| I2 | S38 取时间码 | 只走 `GET /alignment-reports/{id}/units`，不读 S37 DB |
| I3 | 音色授权撤销后新作业 | `AUD.VOICE.AUTHORIZATION` blocker，动作仅 `human_review` |

---

## 10. 契约缺口

| # | 缺口 | 建议 |
|---|---|---|
| C1 | 无 `contracts/quality/alignment-units.v2.schema.json` | 新增；S38 依赖它 |
| C2 | QCReport v2 的 `subject.media.audio` 有 `stream_index` 但无 `audio_track_hash` | 补 `audio_track_hash`（S37 幂等与复用的基础） |
| C3 | S15 的 `lexicon_ref` 在 ShotSpec 里无处安放 | 见 [S38 §8](S38-subtitles.md)：发音词典是独立版本化对象，需 S15 提供 query |
| C4 | `AUD.TEXT.SCRIPT_CONFLICT` 无对应 defect type | 刻意不映射到 defect；若需上报，需在 registry 定义 `severity_max=info` 的观测型 check |
