# S35 · 媒体技术质检 —— 实现方案

状态：`design_only`。本文是实现方案，不含业务代码。SLO 未实测。

本文有两重身份：

1. S35 自身的实现方案；
2. **质量域共享契约 `QCReport v2` 的唯一规范位置**（§2）。S36 / S37 / S38 只做扩展，不复述；S39 只做消费。

改这条契约要同时改五个服务的实现，所以它的规范只写一份。

---

## 0. 本域要回答的两个硬问题

| 问题 | 答案在哪 |
|---|---|
| 三个质检服务怎么保证输出一致 | §2.1 单一 schema + §2.7 决策算法 + §2.8 一致性闸门（CI 与 S39 双重拒绝） |
| 返工上限怎么跨 Run 累计 | [S39 §5](S39-repair-planning.md) —— 计数器键里没有 run/manifest/epoch/model |

---

## 1. 质量域闭环

```text
                 ┌──────────── S17 连续性（期望，不读像素）
                 │  expected_state_set / ContinuityReport
                 ▼
S28 artifact ──┬─▶ S35 技术质检   ──┐
               ├─▶ S36 语义质检   ──┼─▶ QCReport v2（同一 schema）
               ├─▶ S37 音频对齐   ──┤        │
               └─▶ S38 字幕校验   ──┘        ▼
                                          S39 RepairPlan
                                             │   （只决策，不执行）
                                             ▼
                                     S21 编排 → S26 付费准入 → S27 执行
                                             │
                                             ▼
                                 新 artifact hash → 复检（回到本图顶部）
```

五条不可协商的规则：

1. **报告不可变**。报告绑定 `artifact_sha256`；hash 变了旧报告作废，不得复用到新 hash（docs/11 §2 末句）。
2. **期望归 S17，观测归 S36**。任何判定需要看像素，只在 S36；任何判定需要跨集状态/知情/伏笔，只在 S17。细则见 [S36 §2](S36-semantic-qc.md)。
3. **未知不能当 pass**。评估不完整 → `abstain`，且必须在 `check_manifest` 里显式列出没评估的检查项。
4. **模型调用只走 S26**。S36 的 VLM、S37 的 ASR/对齐模型均无供应商凭证，只有 S26 的 job 引用。
5. **S39 是唯一缺陷消费者**。S35/S36/S37/S38 都不直接触发修复，只发报告事件。

### 1.1 谁该报哪一种缺陷

| 现象 | 归属 | 依据 |
|---|---|---|
| 文件损坏、截断、时长不符 | S35 | 容器/解码测量 |
| 黑帧、静帧、闪白 | S35 | 帧级确定性检测 |
| 响度超限、削波、对白被 BGM 盖住 | S35（测量）/ S39 修 `remix` | LUFS、True Peak |
| 画面被烧录了字幕 | S35 | 后期合成约定（service.yaml acceptance） |
| 同一镜内身份漂移、手部异常、人数错误 | S36 | 与 ShotSpec binding 比对 |
| 服装与绑定 revision 不符 | S36 | binding 比对 |
| 服装状态（撕破/湿）与故事时间不符 | S17 出期望 → S36 出视觉证据 | EntityState valid interval |
| 钥匙在谁手里与正史冲突 | S17 出期望 → S36 出观测 | S16 prop ownership |
| 跨集发型/伤疤不一致 | S17 出期望 → S36 出观测 | CharacterProfile |
| 口型偏移、漏词、错读 | S37 | 连续音视频 + ASR 对脚本 |
| 字幕条时码重叠、超 CPS、译文与对白版本脱钩 | S38 | 字幕自身合规 |
| 字幕渲染后压字/出安全区 | S36 `VIS.SAFE_AREA`（看画面）；S38 只在生成期算几何 | 一个看像素，一个算坐标 |

去重规则：同一条缺陷可能同时被两处命中。S39 用 `defect_fingerprint`（§2.4）归并，指纹相同的多条只保留一条、证据合并，并按 `source_priority`（S17 期望 > S36 观测 > S35 测量）决定主来源。

---

## 2. QCReport v2（唯一规范）

### 2.1 为什么需要 v2

`contracts/domain-v1/qc-report.v1.schema.json` 缺四类东西，直接实现会导致三个服务各自长歪：

| 缺口 | 后果 |
|---|---|
| 没有执行上下文（manifest/epoch/run/lineage） | 报告无法绑定"哪个版本"的候选，S08 无法做接受门 |
| 缺陷只有一个大 `type` 枚举 | 技术层要表达"第 3 秒有这个 packet 错误"，只能塞 `technical`，S39 无法据此选动作 |
| 没有 `check_manifest` | 无法证明"这一层该查的都查了"，abstain 无处落脚 |
| 没有 `supersedes` / `repair_context` | 复检无法关联上一次尝试，E26 无法计数 |

v2 是 v1 字段的**严格超集**：v1 能表达的 v2 都能表达，v1 报告可以无损上抬（补必填字段即可）。v1 的 `type` / `severity` / `decision` 枚举语义**一个字都不改**。

### 2.2 三层设计：envelope / defect / check_id

一致性靠这个分层，不靠三份文档互相抄：

```text
QCReport v2 envelope      ← contracts 拥有，三个服务共用一份 schema
  ├─ type（14 值闭集）     ← contracts 拥有，S39 的唯一路由依据
  └─ check_id（命名空间开放）← 各服务私有，前缀 TECH./VIS./AUD./SUB.
```

关键取舍：**`type` 决定动作族，`check_id` 只决定窗口与升级条件，S39 永不按 `check_id` 分支。**

三个维度各有唯一的拥有者，谁都不能越界：

| 维度 | 拥有者 | 决定什么 | 改动代价 |
|---|---|---|---|
| `type`（14 值闭集） | contracts | 这一族缺陷**可以**用哪几种动作、兜底是什么 | 改闭集 = 全体发版，几乎不做 |
| `check_id` + 其登记项 | 各质检服务 | 这一条检查的**最小动作**、**窗口范围**、**何时升级** | 注册即生效，S39 不发版 |
| `recommended_actions` | 质检服务运行时 | 该次检测的**建议**（输入，不是指令） | 无 |

这样拆的理由：只按 `type` 路由表达不出"同样的 `identity`，单帧可用 `keyframe_edit`、连续只能 `regenerate_video`"；只按 `check_id` 路由则每加一条检查都要 S39 发版。把**族**锁在闭集、把**窗口**放进 registry 数据，两个问题同时消失——新增 check 只需在 registry 写一行，S39 读数据不改代码。

`type` 闭集（沿用 v1，不扩不缩）：

```text
identity  anatomy  hands  costume  prop  continuity  camera_adherence
action_adherence  temporal  subtitle  lipsync  voice  technical  audio_mix
```

### 2.2.1 `type` → 动作族（S39 的路由维度）

`action_family[type]` 是**有序候选链**：从最便宜、影响面最小的动作开始，逐级升级；全部不可达或有禁忌时落到该族的兜底。

`human_review` **不出现在任何族里**——它是所有族的共同终点（兜底列里的 `human_review` 表示"该族的自动动作走不通时转人审"）。把它写进族会让"选不到自动动作"和"主动选择人审"混为一谈。

| `type` | 动作族（按最小→最大） | 兜底 | 禁忌 |
|---|---|---|---|
| `technical` | `reingest` → `transcode` → `remix` → `temporal_edit` → `regenerate_video` | `regenerate_video` | 音频类缺陷不得走 `reingest`（源没坏） |
| `identity` | `keyframe_edit`（仅 `window_scope ≤ 6 帧`）→ `temporal_edit` → `regenerate_video` | `regenerate_video` | 连续窗口**禁止** `keyframe_edit` |
| `anatomy` | `temporal_edit` → `regenerate_video` | `regenerate_video` | 无时序能力时直接回退，不得逐帧重绘 |
| `hands` | `temporal_edit` → `regenerate_video` | `regenerate_video` | 同上 |
| `costume` | `regenerate_video` → `change_spec` | `human_review` | 涉及角色设定变更必须 `human_review` |
| `prop` | `regenerate_video` → `change_spec` | `human_review` | 同上 |
| `continuity` | `regenerate_video` → `change_spec` | `human_review` | 跨集事实冲突默认 `human_review` |
| `camera_adherence` | `regenerate_video` → `change_spec` | `human_review` | 需改分镜时走 `change_spec`，不是重生成 |
| `action_adherence` | `regenerate_video` → `change_spec` | `human_review` | 同上 |
| `temporal` | `temporal_edit` → `regenerate_video` | `regenerate_video` | — |
| `subtitle` | `realign_subtitles`（时码，用 S37 对齐结果）→ `re_render_subtitles`（版式/格式，S38 内部重建，零生成费用） | `human_review` | 不得触发视频重生成；**不自动重译**（见下） |
| `lipsync` | `redo_lipsync` | `human_review` | 音频本身有问题时先修音频；不自动重做 TTS（见下） |
| `voice` | `re_synthesize_speech` → `regenerate_video` | `human_review` | 音色授权失效时**禁止**自动重生成 |
| `audio_mix` | `remix` → `regenerate_video` | `remix` | 不得 `reingest`（源的混音问题不在容器） |

`audio_mix` 目前没有检查项指向它——闭集不能缩，保留给后续音频层使用。

**族里的每个动作必须是 `recommended_actions` 枚举的成员**，否则族没法机器可读。上面用到的 `re_synthesize_speech` 与 `re_render_subtitles` 是本次新增的枚举值（见 §12 C7）——S37/S38/S39 原先都用"重新 TTS"、"重排版"这类自然语言描述，实现时没有对应值。

新值与既有值的分工，避免误用：

| 值 | 做什么 | 谁执行 | 花钱 |
|---|---|---|---|
| `realign_subtitles` | 按 S37 对齐结果**改时码**，文本不动 | S38 | 否 |
| `re_render_subtitles` | 版式/格式重排、重渲染轨道，**时码与文本都不动** | S38 | 否 |
| `re_synthesize_speech` | 重新做语音合成（新音频） | S26 `speech` | 是 |

**两处刻意不进族**：

- **字幕重译**走 S26 翻译，要花钱且译文需人验收（S38 `service.yaml` recovery）。它不在 `subtitle` 族里，`SUB.TEXT.*_STALE` 命中时走 `human_review`。
- **口型缺陷的 TTS 重做**：`lipsync` 族只到 `redo_lipsync`。音频本身有问题时应由 `voice` 类的缺陷单独报出来先修，而不是让 S39 从口型缺陷推断出"重做语音"。这条防的是跨族越权。

### 2.2.2 registry 里必须登记的字段

`check_id` 命名规约：`<LAYER>.<GROUP>.<NAME>`，全大写下划线，全局唯一，登记在 `contracts/quality/check-registry.v2.json`。每条登记：

| 字段 | 作用 | S39 怎么用 |
|---|---|---|
| `layer` | `technical` / `semantic` / `alignment` / `subtitle` | 归类与复检层选择 |
| `defect_type` | 落在 14 值闭集内 | **选中动作族**（唯一的路由入口） |
| `default_action` | 该 check 的最小动作 | 族的起点；必须是 `action_family[defect_type]` 的成员 |
| `window_scope` | `frame` / `window` / `shot` / `track` / `all` | 决定 `keyframe_edit` 是否可用、复检范围 |
| `escalation_rule` | 数值阈值 + 升级后动作 | 族内的升级判定 |
| `severity_default` / `severity_max` | 严重度上下界 | 门禁判定 |
| `allowed_actions` | 该 check 允许的动作（族的子集） | 交叉校验，防止登记错 |

两条硬约束，实现时必须有测试守着：

1. **`default_action` 和 `allowed_actions` 必须是该 `defect_type` 动作族的子集。** 登记时校验，越界直接拒绝注册——否则 registry 会成为绕过闭集的旁路。
2. **严重度由登记表决定，模型只能往上提，不能往下压**——防止被注入的 prompt 或幻觉把 blocker 说成 minor。

S39 解析一条缺陷的动作候选，等于：

```text
candidates = action_family[defect.defect_type]           # 闭集，contracts 拥有
             ∩ registry[defect.check_id].allowed_actions  # 数据，质检服务拥有
             ∩ policy.allowed_actions                    # 数据，S02 下发
```

交集为空 → `human_review`（`reason=no_capable_route`），**不是**猜一个动作。起点取 `registry[check_id].default_action`，按 `escalation_rule` 与能力可达性在交集内逐级升。

新增一条 check 只改 registry 数据：`action_family` 没变，S39 的代码路径没变，所以不需要发版。

### 2.3 envelope（完整）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://drama.example.invalid/contracts/quality/qc-report.v2.schema.json",
  "title": "QCReport v2",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "report_id", "report_generation", "qc_layer",
    "context", "subject", "shot_ref",
    "evaluator", "policy_version", "sampling_policy_version",
    "check_manifest", "defects", "dimensions", "coverage",
    "decision", "decision_reason", "abstain", "confidence",
    "supersedes", "repair_context", "evidence_bundle_refs",
    "created_at", "report_content_hash"
  ],
  "properties": {
    "schema_version": { "const": "2.0" },

    "report_id": {
      "type": "string",
      "pattern": "^qc_[a-z0-9]{26}$",
      "description": "ULID 风格；由 §2.6 的派生键确定性生成"
    },
    "report_generation": {
      "type": "integer", "minimum": 1,
      "description": "同一派生键的第几次评估；超时后重评会 +1，旧代数被 supersedes 引用"
    },
    "qc_layer": {
      "type": "string",
      "enum": ["technical", "semantic", "alignment", "subtitle"]
    },

    "context": {
      "type": "object", "additionalProperties": false,
      "required": ["tenant_id", "project_id", "series_id", "branch_id", "episode_id",
                   "home_cell", "manifest_ref", "run_id", "execution_fence",
                   "shot_lineage_id", "policy_ref", "trace_id"],
      "properties": {
        "tenant_id": { "type": "string", "minLength": 1 },
        "project_id": { "type": "string", "minLength": 1 },
        "series_id": { "type": "string", "minLength": 1 },
        "branch_id": { "type": "string", "minLength": 1 },
        "episode_id": { "type": "string", "minLength": 1 },
        "home_cell": { "type": "string", "minLength": 1 },
        "manifest_ref": {
          "type": "object", "additionalProperties": false,
          "required": ["id", "revision", "content_hash"],
          "properties": {
            "id": { "type": "string" }, "revision": { "type": "integer", "minimum": 1 },
            "content_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
          }
        },
        "run_id": { "type": "string", "minLength": 1 },
        "execution_fence": { "type": "integer", "minimum": 1 },
        "shot_lineage_id": {
          "type": "string", "minLength": 1,
          "description": "计数与去重用的稳定镜头血缘根；见 S39 §5.2"
        },
        "policy_ref": {
          "type": "object", "additionalProperties": false,
          "required": ["service_id", "id", "revision", "content_hash"],
          "properties": {
            "service_id": { "const": "S02" }, "id": { "type": "string" },
            "revision": { "type": "integer", "minimum": 1 },
            "content_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
          }
        },
        "memory_checkpoint": {
          "oneOf": [
            { "type": "object", "additionalProperties": false,
              "required": ["branch_id", "commit_sequence", "content_hash"],
              "properties": {
                "branch_id": { "type": "string" },
                "commit_sequence": { "type": "integer", "minimum": 0 },
                "content_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
              } },
            { "type": "null" }
          ]
        },
        "trace_id": { "type": "string", "minLength": 1 }
      }
    },

    "subject": {
      "type": "object", "additionalProperties": false,
      "required": ["artifact_id", "artifact_sha256", "artifact_kind", "media"],
      "properties": {
        "artifact_id": { "type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$" },
        "artifact_sha256": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
        "artifact_kind": {
          "type": "string",
          "enum": ["video", "audio", "image", "subtitle_track", "master"]
        },
        "media": {
          "type": "object", "additionalProperties": false,
          "required": ["duration_seconds", "video", "audio"],
          "properties": {
            "duration_seconds": { "type": "number", "minimum": 0 },
            "video": {
              "oneOf": [
                { "type": "object", "additionalProperties": false,
                  "required": ["width", "height", "fps_num", "fps_den", "codec", "pix_fmt",
                               "color_range", "is_vfr", "frame_count"],
                  "properties": {
                    "width": { "type": "integer", "minimum": 1 },
                    "height": { "type": "integer", "minimum": 1 },
                    "fps_num": { "type": "integer", "minimum": 1 },
                    "fps_den": { "type": "integer", "minimum": 1 },
                    "codec": { "type": "string" },
                    "pix_fmt": { "type": "string" },
                    "color_range": { "type": "string", "enum": ["tv", "pc", "unknown"] },
                    "is_vfr": { "type": "boolean" },
                    "frame_count": { "type": "integer", "minimum": 0 }
                  } },
                { "type": "null" }
              ]
            },
            "audio": {
              "oneOf": [
                { "type": "object", "additionalProperties": false,
                  "required": ["codec", "sample_rate_hz", "channels", "stream_index",
                               "start_offset_seconds"],
                  "properties": {
                    "codec": { "type": "string" },
                    "sample_rate_hz": { "type": "integer", "minimum": 1 },
                    "channels": { "type": "integer", "minimum": 1 },
                    "stream_index": { "type": "integer", "minimum": 0 },
                    "start_offset_seconds": { "type": "number" }
                  } },
                { "type": "null" }
              ]
            }
          }
        }
      }
    },

    "shot_ref": {
      "type": "object", "additionalProperties": false,
      "required": ["shot_id", "shot_revision", "logical_node_id", "node_epoch"],
      "properties": {
        "shot_id": { "type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$" },
        "shot_revision": { "type": "integer", "minimum": 1 },
        "logical_node_id": { "type": "string", "minLength": 1 },
        "node_epoch": { "type": "integer", "minimum": 1 }
      }
    },

    "evaluator": {
      "type": "object", "additionalProperties": false,
      "required": ["name", "version", "image_digest", "model_snapshot"],
      "properties": {
        "name": { "type": "string", "minLength": 1 },
        "version": { "type": "string", "minLength": 1 },
        "image_digest": { "type": "string", "minLength": 1 },
        "model_snapshot": {
          "oneOf": [
            { "type": "object", "additionalProperties": false,
              "required": ["provider", "model_id", "snapshot"],
              "properties": {
                "provider": { "type": "string" },
                "model_id": { "type": "string" },
                "snapshot": { "type": "string" }
              } },
            { "type": "null" }
          ]
        }
      }
    },
    "policy_version": { "type": "string", "minLength": 1 },
    "sampling_policy_version": { "type": "string", "minLength": 1 },

    "check_manifest": {
      "type": "array", "minItems": 1,
      "description": "本层要求的每个 check 恰好出现一次；这是'该查的都查了'的唯一证明",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["check_id", "status", "reason_code"],
        "properties": {
          "check_id": { "type": "string", "pattern": "^(TECH|VIS|AUD|SUB)\\.[A-Z0-9_]+\\.[A-Z0-9_]+$" },
          "status": {
            "type": "string",
            "enum": ["evaluated", "not_applicable", "not_evaluated", "failed"]
          },
          "reason_code": {
            "type": "string",
            "enum": ["ok", "no_such_content", "no_dialogue", "no_face", "asset_unavailable",
                     "evaluator_timeout", "evaluator_error", "dependency_unavailable",
                     "sampling_not_selected", "policy_disabled"]
          },
          "severity_ceiling": {
            "type": "string", "enum": ["blocker", "major", "minor", "info"]
          }
        }
      }
    },

    "defects": {
      "type": "array",
      "items": { "$ref": "#/$defs/defect" }
    },

    "dimensions": {
      "type": "array",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["name", "score", "reason"],
        "properties": {
          "name": { "type": "string", "minLength": 1 },
          "score": { "type": ["number", "null"], "minimum": 0, "maximum": 1 },
          "reason": { "type": "string", "minLength": 1 }
        }
      }
    },

    "coverage": {
      "type": "object", "additionalProperties": false,
      "required": ["media_duration_seconds", "frames_sampled", "temporal_windows_evaluated",
                   "planned_windows", "coverage_ratio", "required_coverage_min",
                   "audio_analyzed", "audio_analyzed_seconds", "sampling_policy_version"],
      "properties": {
        "media_duration_seconds": { "type": "number", "minimum": 0 },
        "frames_sampled": { "type": "integer", "minimum": 0 },
        "temporal_windows_evaluated": { "type": "integer", "minimum": 0 },
        "planned_windows": { "type": "integer", "minimum": 0 },
        "coverage_ratio": {
          "type": "number", "minimum": 0, "maximum": 1,
          "description": "temporal_windows_evaluated / planned_windows；planned=0 时强制为 0"
        },
        "required_coverage_min": { "type": "number", "minimum": 0, "maximum": 1 },
        "audio_analyzed": { "type": "boolean" },
        "audio_analyzed_seconds": { "type": "number", "minimum": 0 },
        "sampling_policy_version": { "type": "string", "minLength": 1 }
      }
    },

    "decision": { "type": "string", "enum": ["pass", "repair", "human_review", "abstain"] },
    "decision_reason": { "type": "string", "minLength": 1 },

    "abstain": {
      "oneOf": [
        { "type": "object", "additionalProperties": false,
          "required": ["reason_code", "failed_check_ids", "detail"],
          "properties": {
            "reason_code": {
              "type": "string",
              "enum": ["incomplete_coverage", "evaluator_timeout", "evaluator_error",
                       "model_disagreement", "low_confidence", "dependency_stale",
                       "asset_unavailable"]
            },
            "failed_check_ids": {
              "type": "array", "minItems": 1,
              "items": { "type": "string" }
            },
            "detail": { "type": "string", "minLength": 1 }
          } },
        { "type": "null" }
      ]
    },

    "confidence": { "type": "number", "minimum": 0, "maximum": 1 },

    "supersedes": {
      "oneOf": [
        { "type": "object", "additionalProperties": false,
          "required": ["report_id", "report_generation"],
          "properties": {
            "report_id": { "type": "string", "pattern": "^qc_[a-z0-9]{26}$" },
            "report_generation": { "type": "integer", "minimum": 1 }
          } },
        { "type": "null" }
      ]
    },

    "repair_context": {
      "oneOf": [
        { "type": "object", "additionalProperties": false,
          "required": ["repair_plan_id", "attempt_slot_id", "attempt_seq", "defect_fingerprints"],
          "properties": {
            "repair_plan_id": { "type": "string" },
            "attempt_slot_id": { "type": "string" },
            "attempt_seq": { "type": "integer", "minimum": 1 },
            "defect_fingerprints": {
              "type": "array", "items": { "type": "string" }
            }
          } },
        { "type": "null" }
      ]
    },

    "evidence_bundle_refs": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["service_id", "id", "revision", "content_hash"],
        "properties": {
          "service_id": { "type": "string", "pattern": "^S[0-9]{2}$" },
          "id": { "type": "string" },
          "revision": { "type": "integer", "minimum": 1 },
          "content_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
        }
      }
    },

    "created_at": { "type": "string", "format": "date-time" },
    "report_content_hash": {
      "type": "string", "pattern": "^[0-9a-f]{64}$",
      "description": "canonical JSON（本节除该字段外）的 sha256"
    }
  },

  "$defs": {
    "defect": {
      "type": "object", "additionalProperties": false,
      "required": [
        "defect_id", "defect_fingerprint", "check_id", "type", "severity",
        "scope", "time_range", "region_bbox", "character_id",
        "expected", "expected_source", "observed", "measured",
        "confidence", "detector", "evidence_artifact_ids", "recommended_actions"
      ],
      "properties": {
        "defect_id": { "type": "string", "pattern": "^df_[a-z0-9]{26}$" },
        "defect_fingerprint": {
          "type": "string", "pattern": "^[0-9a-f]{32}$",
          "description": "跨复检的稳定身份；算法见 §2.4，与 artifact hash 无关"
        },
        "check_id": { "type": "string", "pattern": "^(TECH|VIS|AUD|SUB)\\.[A-Z0-9_]+\\.[A-Z0-9_]+$" },
        "type": {
          "type": "string",
          "enum": ["identity", "anatomy", "hands", "costume", "prop", "continuity",
                   "camera_adherence", "action_adherence", "temporal", "subtitle",
                   "lipsync", "voice", "technical", "audio_mix"]
        },
        "severity": { "type": "string", "enum": ["blocker", "major", "minor", "info"] },
        "scope": { "type": "string", "enum": ["frame", "window", "segment", "whole_artifact"] },
        "time_range": {
          "type": "object", "additionalProperties": false,
          "required": ["start_seconds", "end_seconds"],
          "properties": {
            "start_seconds": { "type": "number", "minimum": 0 },
            "end_seconds": { "type": "number", "exclusiveMinimum": 0 }
          }
        },
        "region_bbox": {
          "oneOf": [
            { "type": "object", "additionalProperties": false,
              "required": ["x", "y", "width", "height"],
              "properties": {
                "x": { "type": "number", "minimum": 0, "maximum": 1 },
                "y": { "type": "number", "minimum": 0, "maximum": 1 },
                "width": { "type": "number", "exclusiveMinimum": 0, "maximum": 1 },
                "height": { "type": "number", "exclusiveMinimum": 0, "maximum": 1 }
              } },
            { "type": "null" }
          ]
        },
        "character_id": {
          "oneOf": [
            { "type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$" },
            { "type": "null" }
          ]
        },
        "expected": { "type": "string", "minLength": 1 },
        "expected_source": {
          "type": "object", "additionalProperties": false,
          "required": ["service_id", "ref_id", "ref_revision"],
          "properties": {
            "service_id": { "type": "string", "pattern": "^S[0-9]{2}$" },
            "ref_id": { "type": "string" },
            "ref_revision": { "type": "integer", "minimum": 1 }
          }
        },
        "observed": { "type": "string", "minLength": 1 },
        "measured": {
          "oneOf": [
            { "type": "object", "additionalProperties": false,
              "required": ["value", "unit", "threshold", "comparator"],
              "properties": {
                "value": { "type": "number" },
                "unit": { "type": "string" },
                "threshold": { "type": ["number", "null"] },
                "comparator": { "type": "string", "enum": ["lt", "lte", "gt", "gte", "eq", "ne", "na"] }
              } },
            { "type": "null" }
          ]
        },
        "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
        "detector": { "type": "string", "enum": ["deterministic", "model"] },
        "evidence_artifact_ids": {
          "type": "array", "minItems": 1,
          "items": { "type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$" }
        },
        "recommended_actions": {
          "type": "array", "minItems": 1,
          "items": {
            "type": "string",
            "enum": ["reingest", "transcode", "keyframe_edit", "regenerate_video",
                     "temporal_edit", "redo_lipsync", "realign_subtitles",
                     "re_render_subtitles", "remix", "re_synthesize_speech",
                     "human_review", "change_spec"]
          }
        }
      }
    }
  }
}
```

v1 兼容性说明：v2 的 `defect` 字段是 v1 的超集，`type` / `severity` 枚举全等。`recommended_actions` **多了三个值**：`change_spec`、`re_synthesize_speech`、`re_render_subtitles`。需要与 v1 最小兼容时，三者都降级为 `human_review`——降级只会让处置变保守，不会变成更激进的动作。`dimensions` / `coverage` 的核心字段名保持 v1 拼写。

### 2.4 defect_fingerprint

复检时要回答"这个缺陷是不是又回来了"，所以指纹必须**与 artifact hash 无关**，否则每次修复都产生新指纹，E26 的"同类重复"永远不成立。

```text
fingerprint = first16hex(sha256(canonical_json({
  qc_layer,
  check_id,
  type,
  character_id,              // null 保留
  severity_bucket: "blocker" | "non_blocker",
  location_bucket: round(time_range.start_seconds / 0.5)   // 0.5s 桶
})))
```

- 时间桶 0.5s：容忍 ±0.5s 的检测抖动，但不把不同时间段的缺陷合并。
- 不含 `observed` 文本：措辞随模型版本变，不能作为身份。
- 不含 `artifact_sha256`：这正是跨尝试追踪的基础。
- 不含 `severity` 精确值：blocker/major 之间抖动不该分裂指纹，但 blocker 与 non_blocker 分裂。

### 2.5 decision 语义（三个服务逐字相同）

| decision | 含义 | 是否可自动接受 | S39 动作 |
|---|---|---|---|
| `pass` | 所有 required check 都已评估且无 blocker/major 缺陷 | 可以（仍受 S02 门与人工盲审抽样约束） | 不生成修复计划，上报"CLEAN" |
| `repair` | 存在可自动修复的缺陷 | 不可以 | 生成 RepairPlan |
| `human_review` | 缺陷存在但无可自动执行动作，或置信度落在灰区 | 不可以 | 创建 S40 ReviewTask |
| `abstain` | 评估没做完或结论不可信 | **绝对不可以** | 按 `exception_behavior` 转人审或隔离 |

`abstain` 的四条硬性要求：

1. `abstain != null` 且 `failed_check_ids` 非空；
2. `check_manifest` 中对应项 status ∈ {`not_evaluated`, `failed`, `not_applicable`}；
3. `decision` 不得为 `pass`（由 §2.8 的 CI 用例强制）；
4. 报告仍然发布——**弃权是有记录的结论，不是没有结论**。

`not_applicable` 与 `not_evaluated` 的区别：前者是"这个素材本来就没有这个内容"（比如无对白镜头没有 `AUD.TEXT.*`），必须在 `reason_code` 给出可验证理由（`no_dialogue`、`no_face`）；后者是"该查没查成"。只有 `not_applicable` 允许不计入 abstain。

### 2.6 确定性 report_id 与幂等

```text
derived_key = sha256(canonical_json({
  tenant_id, artifact_sha256, qc_layer,
  evaluator.name, evaluator.version, evaluator.model_snapshot,
  policy_version, sampling_policy_version,
  repair_context.attempt_slot_id ?? null
}))
report_id        = "qc_" + base32_lower(derived_key)[0:26]
report_generation = 该 derived_key 已有报告数 + 1
```

- 同一输入重复投递 → 同一 `report_id`，数据库唯一约束吞掉，天然幂等，符合 `tenant_id + command_id + request_hash` 契约（`command_id` 不参与派生，因为同一份内容用不同 command 重算应得同一报告）。
- 同键不同 `request_hash`（例如换了采样策略但 `sampling_policy_version` 没升）→ 409。这条能抓到采样策略改了版本号没改的 bug。
- 评估超时后重评 → `report_generation +1`，`supersedes` 指向上一次。S39 只认同一派生键的**最大 generation**。

### 2.7 决策算法（伪代码，三服务共用同一实现）

```text
decide(report, policy, registry):
  # 1. 检查清单完整性：本层 registry 要求的 check 必须恰好各出现一次
  for check in registry.required_for(layer, subject):
      e = manifest.lookup(check)
      if e is None:                      return abstain("incomplete_coverage", [check])
      if e.status == "failed":           failed.append(check)
      if e.status == "not_evaluated":    failed.append(check)
      if e.status == "not_applicable" and not is_justified(e): failed.append(check)

  # 2. 采样覆盖不足
  if coverage.coverage_ratio < policy.min_coverage_ratio:   # 默认 0.90
      return abstain("incomplete_coverage", failed ∪ required_checks)

  # 3. 有 failed 的必查项 → 弃权，不看分数
  if failed.non_empty:                return abstain(reason_for(failed), failed)

  # 4. 硬失败：registry 标记 hard_fail 的 check 命中即为 blocker，置信度不参与
  if any(d.severity == "blocker" for d in defects):        return repair

  # 5. 缺陷分级
  if any(d.severity == "major" for d in defects):
      if any(d.confidence < policy.tau_low for d in defects):        return human_review
      if all(d.recommended_actions ⊆ policy.allowed_actions for d in defects):
                                                                      return repair
      return human_review

  # 6. 模型冲突
  if model_disagreement > policy.tau_disagreement:                  return human_review

  # 7. 整体置信度不足
  if report.confidence < policy.tau_abstain:                        return abstain("low_confidence", [])

  # 8. 只剩 minor / info
  return pass
```

阈值默认值（由 S02 policy 下发，`policy_version` 记录在报告里）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `min_coverage_ratio` | 0.90 | 低于此值弃权 |
| `tau_low` | 0.40 | 低于此值的 major 缺陷不自动修，转人审 |
| `tau_abstain` | 0.35 | 整体置信度低于此值弃权 |
| `tau_disagreement` | 0.30 | 两个评估器分歧率超过此值转人审 |

关键性质：**第 3 步在评分之前**。这是"未知不能当 pass"在代码层面的落点——评估器超时不是低分，是没有结论，不能进入分数比较。

### 2.8 一致性闸门（答案：三个服务凭什么输出一致）

四条机制，缺一条都会出轨：

| # | 机制 | 落点 |
|---|---|---|
| 1 | **单一 schema**：三服务共用 `contracts/quality/qc-report.v2.schema.json`，写报告前必须过 schema 校验 + 语义校验（`report_id` 重算一致、`check_manifest` 完备、`coverage_ratio` 与分子分母一致、`time_range.end > start`） | 服务内 `ReportValidator`，不通过不落库 |
| 2 | **共享决策实现**：`decide()` 只有一份实现，发布为 `platform/qc-sdk`；三服务 pin 同一版本，升级走灰度。禁止各服务自己写 if/else 判定 pass | 代码级 |
| 3 | **契约一致性用例集**：`contracts/quality/conformance/*.json`，同一组输入向量（含超时、冲突、空缺陷、覆盖不足、低置信）跑三服务，输出必须逐字段相同（除 id/时间） | CI 门禁 |
| 4 | **消费侧拒绝**：S39 的 ingest 跑同一 validator；不合规报告进 quarantine 并告警 `qc_report_rejected_total`，**不做 best-effort 解析**。宁可阻断，不可降级理解 | 运行期 |

第 4 条是最硬的一条。它把"schema 漂移"从"下游悄悄算错"变成"上线即被拒"。docs/04 说"最终接受状态由 S08 持有"，这里同理：**S39 不猜**。

---

## 3. S35 架构

### 3.1 职责边界

S35 **只做确定性测量**，不调用任何模型（service.yaml acceptance: "报告不依赖 VLM 主观判断"）。这使 S35 成为整条链的地基：它的报告在评估器升级时也稳定，因此可以作为跨版本对比的基准。

依赖只有 S28，运行期不依赖 S17/S26 —— 这与 S36/S37 不同，S35 的不可用不会拖垮语义层。

### 3.2 流水线

```text
POST /technical-checks
  │
  ├─ 1. 资质与租户校验（S01 上下文 / S02 policy_ref）
  ├─ 2. 取 artifact（S28 manifest，短签名 URL，禁内网/SSRF）
  ├─ 3. 媒体沙箱：非特权、无网络、只读挂载、cgroup 限额、超时可杀
  │      └─ ffprobe → 结构；ffmpeg 解码 → 帧级统计；ebur128 → 响度
  ├─ 4. 逐项 check（§4 注册表），每项产出：
  │      CheckResult{check_id, status, defects[], measured, evidence}
  ├─ 5. 证据固化：探针 JSON、波形图、问题帧缩略图拼板 → S28 归档
  ├─ 6. 组装 check_manifest / coverage / defects
  ├─ 7. ReportValidator → decide() → 落库 + Outbox（同事务）
  └─ 8. quality.technical.completed
```

失败语义：沙箱超时 / 解码崩溃 → 对应 check status=`failed`，`reason_code=evaluator_timeout|evaluator_error`，**不是** "没检出问题"。整个作业若连 ffprobe 都跑不起来，报告仍然生成，`decision=abstain`，`abstain.reason_code=asset_unavailable`。

### 3.3 采样与 coverage

- 帧级检查（黑帧/静帧/闪白）**全帧解码**，`frames_sampled = frame_count`，`coverage_ratio = 1.0`。
- 抽帧检查（缩略图/色域）按 `sampling_policy_version` 定义的间隔抽样。
- `planned_windows` 由采样策略按 `duration / window_seconds` 计算并向上取整；`temporal_windows_evaluated` 是实际完成数。被 kill 的窗口不计入。
- 全帧检查的 `planned_windows = temporal_windows_evaluated = ceil(duration / 1.0)`，保证 `coverage_ratio` 三个服务用同一口径可加可比。

---

## 4. 检查项注册表（S35）

严重度含义：`blocker` = 硬失败，命中即 `decision=repair`，且不可自动接受；`major` = 需修复，可按 policy 例外放行；`minor` = 告警，不阻断；`info` = 只记录。

| check_id | defect type | 严重度 | 判定 | 证据 | 允许动作 | 复检范围 |
|---|---|---|---|---|---|---|
| `TECH.CONTAINER.PARSE` | technical | **blocker** | 容器无法解析 / 无 moov | 探针 JSON | reingest, transcode | technical+hash |
| `TECH.FILE.EMPTY` | technical | **blocker** | 无视频帧或 size=0 | 文件元数据 | reingest | technical+hash |
| `TECH.FILE.TRUNCATED` | technical | **blocker** | 末包不完整 / `frame_count` < 容器声明 / 解码在 EOF 前报错 | 探针 + 末帧 | reingest, transcode | technical+hash |
| `TECH.DECODE.PACKET_ERROR` | technical | **blocker** | 解码错误数 > 0 且发生在可交付段 | 错误帧时间码 | transcode | technical+hash |
| `TECH.DURATION.MISMATCH` | technical | **blocker** | \|actual − ShotSpec.duration\| > 1 帧 | 实测时长 | regenerate_video, temporal_edit | technical |
| `TECH.DURATION.ZERO` | technical | **blocker** | duration ≤ 0.04s | 实测时长 | reingest | technical+hash |
| `TECH.RESOLUTION.BELOW_SPEC` | technical | **blocker** | 短边 < 项目交付下限 | 实测 dims | regenerate_video | technical |
| `TECH.RESOLUTION.MISMATCH` | technical | major | 与 ShotSpec 目标画幅比例不符（非等比裁切） | 实测 dims | regenerate_video, transcode | technical |
| `TECH.FPS.MISMATCH` | technical | major | fps 与项目交付规格不符（VFR 视为不符） | 实测 fps | transcode | technical |
| `TECH.FPS.VFR` | technical | major | `is_vfr=true` | 帧时间戳分布 | transcode | technical |
| `TECH.CODEC.UNSUPPORTED` | technical | major | codec/pix_fmt 不在交付白名单 | 探针 | transcode | technical |
| `TECH.COLOR.RANGE_MISMATCH` | technical | minor | 期望 tv/pc 与实际不符 | 探针 | transcode | technical |
| `TECH.BLACK_FRAME` | technical | major（头尾>阈值） | 连续全黑帧 ≥ `black_min_frames` | 黑帧区间 + 拼板 | temporal_edit, regenerate_video | 连续窗口 ±0.5s |
| `TECH.FREEZE_FRAME` | technical | major | 连续相同帧 ≥ `freeze_min_seconds`（默认 0.5s） | 区间 + 首末帧 | temporal_edit, regenerate_video | 连续窗口 ±0.5s |
| `TECH.FLASH_FRAME` | technical | minor | 单帧亮度超 `flash_luma_delta`（默认 0.6） | 帧号 + 亮度曲线 | temporal_edit | 窗口 ±0.5s |
| `TECH.AUDIO.MISSING` | technical | **blocker**（有对白镜头） | 无音轨或音轨全静音但 ShotSpec 有对白 | 音量包络 | regenerate_video, remix | technical+audio |
| `TECH.AUDIO.OFFSET` | technical | major | 容器 `audio.start_offset − video.start_offset` 超 ±1 帧 | 探针 | remux→transcode | audio+前后 0.5s |
| `TECH.AUDIO.SILENCE_HEAD_TAIL` | technical | minor | 头/尾静音超阈值 | 包络 | temporal_edit | 窗口 |
| `TECH.LOUDNESS.LUFS` | technical | major（master 时 blocker） | 集成响度超出项目目标 ±2 LU | ebur128 报告 | remix | 全轨 |
| `TECH.LOUDNESS.TRUE_PEAK` | technical | **blocker**（master） | True Peak > 上限（默认 −1 dBTP） | ebur128 报告 | remix | 全轨 |
| `TECH.AUDIO.CLIPPING` | technical | major | 削波采样占比 > 阈值 | 削波区间 | remix | 全轨 |
| `TECH.SUBTITLE.BURNED_IN` | technical | **blocker** | 画面检出烧录字幕（OCR 命中字幕样式区域）且项目要求后期合成 | 命中帧 + bbox | regenerate_video | 全片抽检 |
| `TECH.META.MISSING` | technical | info | 缺色彩/旋转等元数据 | 探针 | — | — |

阈值全部来自 `policy_ref` 指向的 `technical_qc_profile`，报告里只记录**实测值 + 阈值 + 比较符**（`measured`），不记录策略快照正文——避免报告随策略膨胀。策略版本在 `policy_version` 里。

关于"硬失败 vs 告警"的分界原则：

- **硬失败** = 这份素材不能进入下一步（解码不了、时长错了、分辨率不够、该有声音没声音、master 响度/峰值越界、被烧了字幕）。这类问题的共同点是：下游任何修复都比重新生成便宜。
- **告警** = 素材可用但未达最佳（色彩范围、静音头尾、闪白、元数据）。可以走例外接受，但要有记录。

`TECH.AUDIO.MISSING` 的"有对白镜头"判定来自 ShotSpec 的 `dialogue` 是否为空，**不来自 ASR**。S35 不调模型，这条必须靠声明判断；若声明缺失，记 `not_applicable`（`reason_code=no_dialogue`）而不是 pass。

---

## 5. 数据模型（schema `technical_qc`）

```sql
CREATE TABLE technical_qc.report (
  report_id            text PRIMARY KEY,
  report_generation    integer NOT NULL,
  tenant_id            text NOT NULL,
  series_id            text NOT NULL,
  episode_id           text NOT NULL,
  branch_id            text NOT NULL,
  shot_id              text NOT NULL,
  shot_lineage_id      text NOT NULL,
  logical_node_id      text NOT NULL,
  node_epoch           integer NOT NULL,
  manifest_id          text NOT NULL,
  manifest_revision    integer NOT NULL,
  run_id               text NOT NULL,
  execution_fence      integer NOT NULL,
  artifact_id          text NOT NULL,
  artifact_sha256      text NOT NULL,
  evaluator_version    text NOT NULL,
  policy_version       text NOT NULL,
  sampling_version     text NOT NULL,
  decision             text NOT NULL CHECK (decision IN ('pass','repair','human_review','abstain')),
  decision_reason      text NOT NULL,
  confidence           numeric(5,4) NOT NULL,
  coverage_ratio       numeric(5,4) NOT NULL,
  attempt_slot_id      text,
  supersedes_report_id text,
  payload              jsonb NOT NULL,        -- 完整 v2 文档
  content_hash         text NOT NULL,
  created_at           timestamptz NOT NULL,
  UNIQUE (report_id, report_generation)
);
CREATE INDEX ON technical_qc.report (tenant_id, series_id, branch_id, shot_lineage_id, created_at DESC);
CREATE INDEX ON technical_qc.report (artifact_sha256);
CREATE INDEX ON technical_qc.report USING gin (payload jsonb_path_ops);

-- 同派生键同代只允许一份（幂等兜底）
CREATE UNIQUE INDEX ON technical_qc.report
  (artifact_sha256, evaluator_version, policy_version, sampling_version,
   COALESCE(attempt_slot_id,'-'), report_generation);

CREATE TABLE technical_qc.outbox (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id          text NOT NULL,        -- 事件信封 required
  aggregate_id       text NOT NULL,        -- report_id
  aggregate_sequence bigint NOT NULL,      -- report_generation
  event_type         text NOT NULL,
  partition_key      text NOT NULL,        -- tenant_id || ':' || aggregate_id
  payload            jsonb NOT NULL,
  headers            jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  published_at       timestamptz,
  UNIQUE (aggregate_id, aggregate_sequence, event_type)
);
CREATE INDEX outbox_unpublished_idx ON technical_qc.outbox (id) WHERE published_at IS NULL;
CREATE TABLE technical_qc.inbox (
  consumer text NOT NULL, event_id text NOT NULL, received_at timestamptz NOT NULL,
  PRIMARY KEY (consumer, event_id)
);
```

隔离：每租户一个 schema 前缀 or 行级 `tenant_id` + RLS。选行级 + RLS，因为 report 量大且查询天然带 tenant。

证据不落本库：探针 JSON / 拼板图都进 S28，本库只存 `evidence_artifact_ids`。

---

## 6. API

### 6.1 `POST /technical-checks`

```json
{
  "command_id": "cmd_01H...",
  "execution_context": { "...": "contracts/execution-context.v2.schema.json 全文" },
  "artifact": { "service_id": "S28", "id": "clip_x", "revision": 1, "content_hash": "…64hex" },
  "shot_spec_ref": { "service_id": "S06", "id": "shot_3_2", "revision": 4, "content_hash": "…" },
  "profile_ref": { "mode": "shot", "target": { "width": 1920, "height": 1080, "fps": "24/1",
                   "loudness_lufs": -16.0, "true_peak_db": -1.0 } },
  "repair_context": null,
  "deadline": "2026-10-30T12:00:00Z"
}
```

响应 `202`：`{ "report_id": "qc_…", "operation_id": "op_…", "status": "accepted" }`。报告是异步产物，命令不阻塞。

错误：`409` 幂等冲突（同键不同 body）；`422` artifact 不可达或 profile 与交付规格矛盾；`403` 越权；`429` 沙箱池饱和。

### 6.2 `GET /technical-reports/{id}`

返回 `payload` + `source_revision` + `projection_watermark`。支持 `?generation=latest`（默认）与 `?generation=N`（审计回看）。

### 6.3 内部查询（给 S39 / S08 投影）

`GET /technical-reports?artifact_sha256=…&manifest_id=…` 返回摘要列表，供 S39 组装 QCSnapshot。

---

## 7. 事件

`quality.technical.completed`，topic `drama.technical-qc.v2`，14 天保留，`replay_side_effects=false`。

```json
{
  "specversion": "1.0", "schema_version": "2.0",
  "id": "evt_…", "source": "S35", "type": "quality.technical.completed",
  "subject": "shot_3_2",
  "time": "2026-10-30T11:00:00Z",
  "tenant_id": "…", "home_cell": "cell_a1",
  "aggregate_id": "qc_…", "aggregate_sequence": 1,
  "correlation_id": "run_…", "causation_id": "cmd_…",
  "traceparent": "00-…-…-01",
  "data": {
    "report_id": "qc_…", "report_generation": 1, "qc_layer": "technical",
    "artifact_sha256": "…", "manifest_ref": { "id": "…", "revision": 12, "content_hash": "…" },
    "shot_id": "shot_3_2", "shot_lineage_id": "lin_…", "node_epoch": 4,
    "run_id": "run_…", "execution_fence": 2,
    "decision": "repair", "decision_reason": "TECH.DURATION.MISMATCH blocker",
    "defect_count": 1, "blocker_count": 1, "coverage_ratio": 1.0,
    "attempt_slot_id": null
  },
  "data_ref": { "service_id": "S35", "id": "qc_…", "revision": 1, "content_hash": "…" }
}
```

事件策略：`data` 只带路由与决策所需字段（轻量，S39 可据此分流），完整报告走 `data_ref` 回查。这样 S39 不必消费大 payload，也符合 docs/04 "完整剧本/媒体不放普通事件"。

---

## 8. 一致性、幂等与隔离

| 项 | 做法 |
|---|---|
| 命令幂等 | `tenant_id + command_id + request_hash`；同键同 body 返回同 `report_id` |
| 报告幂等 | §2.6 派生键 + 唯一索引；重复评估只升 `report_generation` |
| 落库与事件 | 同事务写 `report` + `outbox` |
| 消费 | Inbox 去重；按 `aggregate_id`（report_id）保序 |
| 并发 | 同一 artifact 并发请求不加锁，靠派生键唯一约束收敛；冲突时后到者读取已有报告返回 |
| 跨租户 | 所有查询强制 `tenant_id` 谓词 + RLS；`GET` 不存在与越权返回同一 404，不泄漏存在性（E28） |
| 保留 | 报告按租户合同保留（默认 730 天）；证据在 S28 按媒体保留期 |

---

## 9. 可观测与 runbook

指标：

```text
technical_qc_reports_total{decision, layer="technical"}
technical_qc_defects_total{check_id, severity}
technical_qc_eval_seconds_bucket{media_kind}
technical_qc_sandbox_kill_total{reason}
technical_qc_report_rejected_total{reason}     # 报告未通过 validator，必须为 0
technical_qc_coverage_ratio_bucket
technical_qc_abstain_ratio{reason_code}        # 突升说明评估器或沙箱在退化
```

告警：

- `abstain_ratio > 5%` 持续 15 分钟 → 评估器/沙箱退化，P2。
- `report_rejected_total > 0` → P1，契约漂移，立即停止该版本灰度。
- `sandbox_kill_total` 按 reason 分层，OOM 单独告警。

Runbook 要点：

1. **坏媒体隔离**：`TECH.CONTAINER.PARSE` / `TECH.DECODE.PACKET_ERROR` 命中后，artifact 打 `quarantined` 标记事件，S39 只允许 `reingest`，禁止任何"再试一次同样解码"。
2. **确定性检查可无脑重跑**：同一 hash + 同一 evaluator 重跑结果必须一致；不一致说明有非确定性来源（GPU 抖动、时钟），按事故处理，因为这会破坏 E24 的可复现性。
3. **回滚**：evaluator 版本回退只影响新请求；已发布报告不重算（docs/16 E32/evaluator 灰度约束）。

---

## 10. 测试清单

### 10.1 E24 —— 静态/时序缺陷、证据时间段、coverage、修复后复检、预算上限

| # | 用例 | 断言 |
|---|---|---|
| E24-T1 | 截断 mp4（moov 后截断） | `TECH.FILE.TRUNCATED` blocker；`time_range` 落在截断点；`decision=repair` |
| E24-T2 | 0 字节文件 | `TECH.FILE.EMPTY` blocker；`decision=repair`；允许动作仅 `reingest` |
| E24-T3 | 声明 4.000s，实际 3.958s | `TECH.DURATION.MISMATCH`；`measured={3.958, s, 4.000, ne}` |
| E24-T4 | 中段 8 帧全黑 | `TECH.BLACK_FRAME`；`time_range` 覆盖黑帧区间 ±1 帧；证据含拼板 artifact |
| E24-T5 | 冻帧 0.6s | `TECH.FREEZE_FRAME` major |
| E24-T6 | 有对白但无音轨 | `TECH.AUDIO.MISSING` blocker（不是 `not_applicable`） |
| E24-T7 | 无对白镜头且无音轨 | `TECH.AUDIO.MISSING` status=`not_applicable`, reason=`no_dialogue`；`decision=pass` |
| E24-T8 | 沙箱 OOM 中途被杀 | `check_manifest` 中未完成项 `failed`；`coverage_ratio<1`；`decision=abstain`, reason=`evaluator_error`；**不得 pass** |
| E24-T9 | hash 改变后复用旧报告 | S39 拒收（`REPORT_HASH_MISMATCH`）；不得进入 accept |
| E24-T10 | 修复后复检 | 新报告 `repair_context.attempt_slot_id` 非空，`supersedes` 指向旧 report_id |
| E24-T11 | 复检发现新 hash 下缺陷仍在 | `defect_fingerprint` 与上次相同 → S39 判定"同类重复"，消耗 identical-strategy 计数 |

### 10.2 E25 —— 评估器超时与冲突

| # | 用例 | 断言 |
|---|---|---|
| E25-T1 | ffprobe 超时 | `decision=abstain`，`abstain.reason_code=evaluator_timeout`，`failed_check_ids` 非空 |
| E25-T2 | 两次评估结果不同（非确定性） | 第二次 `report_generation=2` 且 `supersedes` 指向 1；告警触发 |
| E25-T3 | 覆盖不足（采样策略只跑了 70%） | `coverage_ratio=0.70 < 0.90` → abstain |
| E25-T4 | `check_manifest` 缺一个必查项 | ReportValidator 拒绝，报告不落库（服务侧），或 S39 quarantine（消费侧） |

### 10.3 E26 —— 跨 Run 返工累计（S35 侧验证点）

| # | 用例 | 断言 |
|---|---|---|
| E26-T1 | 同一 shot 第 2 个 Run 的复检报告 | `report_id` 派生键含 `attempt_slot_id`，与 Run 1 不同；`shot_lineage_id` 相同 |
| E26-T2 | 报告被 S39 消费两次 | 计数只 +1（slot 唯一约束） |

### 10.3.1 路由不变量（S35 定族，S39 消费 —— 两个仓都要有对应测试）

| # | 用例 | 断言 |
|---|---|---|
| ROUTE-T1 | registry 每条登记都过一遍族校验 | 所有 `default_action` / `allowed_actions` ⊆ `action_family[defect_type]`；有越族则**加载期失败**，不是运行期跳过 |
| ROUTE-T2 | 新增一条 check（只改 registry JSON） | registry validator 接受；S39 无需改代码即可路由 |
| ROUTE-T3 | 给 `technical` 类 check 登记 `redo_lipsync` | 加载期拒绝，报 `registry_cross_family{check_id}` |
| ROUTE-T4 | 给 `window_scope=shot` 的 check 登记 `keyframe_edit` | 加载期拒绝，报 `keyframe_edit_out_of_window{check_id}` |
| ROUTE-T5 | `audio_mix` 族目前无 check 指向 | 允许（闭集不缩）；若某 check 登记 `audio_mix`，其动作必在 `remix`/`regenerate_video` 内 |
| ROUTE-T6 | `never_auto_repair: true` 的登记项带 `no_auto_reason` | 该字段必填，值为三个枚举之一；缺失即加载失败 |

### 10.4 通用验收（docs/16 末段）

跨租户拒绝（含 404 不泄漏）、重复投递、乱序/缺失事件、重启恢复（outbox 重放不重发副作用）、依赖超时（S28 不可达 → abstain）、版本冲突（manifest 换代后的报告不被新 manifest 复用）、审计可追溯（每条报告可从 trace_id 反查命令与 run）、独立升级回滚。

---

## 11. 交付顺序与开关

1. 契约：`contracts/quality/qc-report.v2.schema.json` + `check-registry.v2.json` + `conformance/`（先于任何服务代码）
2. `platform/qc-sdk`：validator + `decide()` + fingerprint，带单测覆盖率门
3. S35 最小闭合：`TECH.CONTAINER.PARSE` / `FILE.EMPTY` / `FILE.TRUNCATED` / `DURATION.*` 四条（就能覆盖 docs/16 对 S35 的验收句）
4. 其余检查项按"阻断度"补齐：先 blocker，后 major，最后 minor/info
5. 全部检查项到齐后接 S39 的 ingest（用 mock S39 不影响顺序）

开关：`TECHNICAL_QC_ENABLED`（默认 false）、`TECHNICAL_QC_CHECKS`（白名单，默认空 = 不查任何项）。provider 开关不适用——本服务无模型调用。

---

## 12. 契约缺口（需要先行修改，不要自行补）

按 docs/16 §6 与 AGENTS.md "契约与文档冲突 → 停下报告"：

| # | 缺口 | 建议 |
|---|---|---|
| C1 | `contracts/domain-v1/qc-report.v1.schema.json` 缺执行上下文、`check_manifest`、`supersedes`、`repair_context` | 新增 `contracts/quality/qc-report.v2.schema.json`（本文 §2.3），v1 保留为 payload 基线 |
| C2 | `contracts/event-topics.v2.json` 的 `quality.*` 事件没有定义 `data` 最小字段集 | 按本文 §7 补 `event-payload/quality-*.v2.schema.json` |
| C3 | `service-catalog.v2.json` 的 S35 `depends_on` 只有 S28，但读 ShotSpec 需要 S06 | 依赖表是运行协作关系而非拓扑，读 ShotSpec 走 query 可接受；若要求显式化，需改 catalog |
| C4 | S38 需要发缺陷但没有 `quality.subtitle.*` 事件 | 见 [S38 §7](S38-subtitles.md)：缺陷随 `post.subtitle.ready` 的 `data_ref` 携带，不新增 topic |
| C5 | 没有 `contracts/quality/check-registry.v2.json` | 新增。**S39 的修复路由依赖它**（§2.2.1 的族是代码，§2.2.2 的登记项是这个文件）；同时 validator 用它的 `severity_max` 与 `allowed_actions` 做交叉校验 |
| C6 | registry 的 `default_action` / `allowed_actions` 必须是 `defect_type` 动作族的子集，但没有任何机制强制这一点 | 在 `platform/qc-sdk` 的 registry validator 里做**加载期校验**：越族即拒绝加载并报警。不要依赖人工 review —— registry 是数据，会漂移 |
| C7 | `recommended_actions` 缺两个实际在用的动作：重新语音合成、重排字幕轨道。S37/S38/S39 只能用自然语言描述 | 新增 `re_synthesize_speech` 与 `re_render_subtitles`（§2.3）。不加这两个值，`voice` 与 `subtitle` 动作族无法机器可读，S39 只能硬编码 check_id 绕过去 |
| C8 | `action_family[type]` 只存在于本文表格里，没有机器可读契约 | 见 [S39 §11 C5b](S39-repair-planning.md)：落到 `contracts/quality/` 下，S39 读数据不硬编码 |
