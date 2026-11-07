# check-registry 缺字段清单 · 2026-11-05

范围：`plans/S35`–`S38` 四份 QC 方案里的 **99 条** `check_id`，逐条对照
`contracts/quality/check-registry.v2.schema.json` 的 10 个必填字段。

**这不是 `check-registry.v2.json`。** 它不给值，只回答两个问题：这条 check 缺哪个字段；
缺的那个字段在方案里**有没有出处**。有出处的列出来源行号；没出处的一律写 `NEEDS_OWNER`。

第二版（2026-11-05 复核）另加三节，仍然**不写 registry、不填推测值**，只是把决策面整理到 owner 能直接拍板：

- **§9** `window_scope` 按 `defect_type` 分族的候选区间（97 条压成 14 个问题，逐个说明为什么还不能收敛）；
- **§10** `escalation_rule` 的散文全量盘点（原文 + 缺哪个子字段）；
- **§11** `severity_max` 缺省规则的可签字条文（覆盖 87 条、不覆盖 12 条、否决后的四个备选）。

数字复核记录在 **§8.1**。

`contracts/README.md:84-86` 的禁令是这份文档存在的理由：

> **不做的事**：不从散文推断这些值。……猜错会让 S39 把一个 20 帧的连续窗口交给逐帧修补。

---

## 0. 结论

| 项 | 结果 |
|---|---|
| check 总数 | **99**（TECH 23 / VIS 32 / AUD 20 / SUB 24）。验收报告原写 74，漏算一整份方案的表，**99 是对的** |
| 逐条都能从表格机械提取、无歧义 | **0 条** |
| `window_scope` 可提取 | **0 / 99** —— 四份表都没有这一列。正文里唯一沾到"修复范围"的一句是 [S38:339](../../plans/S38-subtitles.md)「字幕没有帧/窗口概念」，它排除 `frame`/`window`，但没有在 `track` 与 `all` 之间选（§9.1 #10） |
| `escalation_rule` 可提取 | **0 / 99** —— 带**数值阈值**的升级口径只有 **2 处**（[S37:110](../../plans/S37-audio-alignment.md) 逐条、[S36:174](../../plans/S36-semantic-qc.md) 族级），**两处都缺 `escalate_to`**；另有 3 处"处理顺序耗尽后转人审"（§10.2 A3–A5），连阈值都没有。99 条里 **55 条**在四份方案中没有任何散文（§10.7） |
| `allowed_actions` 可提取 | **21 / 99**（只有 S35 有这一列，其中 2 条不可用） |
| `default_action` 可机械得出 | **14 / 99**（仅当 `allowed_actions` 恰好一个成员，取之；这 14 条全在 S35 的 `technical` 族内） |
| `recheck_scope` 枚举内可提取 | **13 / 99**（只有 S35；其余 10 条是自由文本或 `—`） |
| `defect_type` | **97 / 99**（2 条是非缺陷记录） |
| `severity_default` | **87 / 99** 无歧义；8 条带条件前缀，1 条二值（两个阈值对应两档），3 条为空（非缺陷行：S36 `VIS.KNOWLEDGE.SCOPE_RENDER`、S37 `AUD.TEXT.SCRIPT_CONFLICT` / `AUD.LIPSYNC.FACE_NOT_FOUND`） |
| 命名不合规的 `check_id` | **3 条**，全在 S35（§4） |
| 顺带查实的另外 3 个缺陷 | §5 |
| 按 `defect_type` 分族的 `window_scope` 候选 | **97 条 → 14 个待拍问题**（2 条非缺陷行不参与），见 §9 |
| `severity_max` 缺省规则可一次覆盖 | **87 条**；不覆盖 12 条（8 带条件 + 1 二值 + 3 空），见 §11 |

**一句话**：99 条里没有一条能"抄"进 registry。能推进一步的是 S35 的 **23 条**——它有动作列和复检列；
剩下 **76 条**（S36 32 / S37 20 / S38 24）连列都没有。

---

## 1. 判定标准（可复核）

这四条是我划"能提取 / 不能提取"的边界，任何人可以拿它逐条复算。

### 1.1 什么算"有依据"

**字段的取值必须写在方案正文里，且指向这条 check 本身。** 不能是：
- 从其他 check 的结论类推过来的；
- 从族表的兜底值推出来的（族表说的是"这个 type 兜底去哪个动作"，不是"这条 check 的起点动作是什么"）；
- 从"同类问题通常怎么做"推出来的。

### 1.2 什么是"机械提取"，什么是"语义解释"

| 判定 | 含义 | 例 |
|---|---|---|
| **可提取** | 表格单元格的值**逐字**就是 schema 的枚举成员，无需转换 | `allowed_actions = reingest, transcode`（列头就叫"允许动作"，值全在 12 值枚举内） |
| **可机械导出** | 一个确定规则能从可提取值算出唯一答案 | `allowed_actions` 只有 1 个成员 ⇒ `default_action` 就是它（S39 §3.1 的"起点"语义没有别的解释） |
| **NEEDS_OWNER** | 需要人决定，方案里没有 | 单元格写"连续窗口 ±0.5s"，而枚举是 `technical/semantic/alignment/subtitle/...` —— 转换需要判断 |

### 1.3 `window_scope` 为什么一条都不能提取（关键判断）

`schema` 里 `window_scope` 的定义是：

> `"How much of the artifact the repair touches, and therefore how much must be re-checked."`

`schema` 的 `window_max_frames` 定义是：

> `"When window_scope is frame or window: the largest window this check may report. Gates keyframe_edit, whose precondition is <= 6."`

**它说的是"修复要动多大范围"，不是"检测时抽多宽的窗"。** 这条区分有三处独立佐证：

- [S35:164](../../plans/S35-technical-qc.md) — `window_scope` … 「决定 `keyframe_edit` 是否可用、复检范围」
- [S39:99](../../plans/S39-repair-planning.md) — 「`frame`/`window` 且 ≤ 6 帧才允许 `keyframe_edit`；决定复检范围」
- [S39:112](../../plans/S39-repair-planning.md) — 「禁止逐帧修连续缺陷」

所以下面这些**都不构成** `window_scope` 的依据：

| 看起来像依据 | 为什么不是 | 出处 |
|---|---|---|
| S36 §4 的采样表（`dialogue` 每 1.0s 一窗、`action` 每 0.5s 一窗、`transition` 切点前后各 0.5s） | 这是**检测**窗口，决定抽多少帧看多久；`window_scope` 决定**修**的时候动多大 | [S36:163-170](../../plans/S36-semantic-qc.md) |
| S35「复检范围」列（`连续窗口 ±0.5s` / `全轨` / `全片抽检`） | 这是**复检**范围，是 `recheck_scope` 的素材，不是修复范围 | [S35:759](../../plans/S35-technical-qc.md) 列头 |
| S37「口型 check 的 `planned_windows` = 对白区间帧数」 | `planned_windows` 是覆盖率统计口径 | [S37:104](../../plans/S37-audio-alignment.md) |
| `TECH.FLASH_FRAME` 判定写"单帧亮度" | "命中是单帧" ≠ "修复只动单帧"；很可能要多带前后帧才能重编码 | [S35:775](../../plans/S35-technical-qc.md) |

**若从这里推断**，最危险的后果正好是 README 点名的那条：给 `TECH.BLACK_FRAME` / `TECH.FREEZE_FRAME`
写 `window = 连续窗口 ±0.5s`，S39 就会把一个可能 20 帧的连续缺陷判定为"可以 ≤6 帧逐帧修"。
`keyframe_edit` 对连续视频缺陷正是 docs/11 §2 明令禁止的默认动作。

**结论：`window_scope` 99/99 `NEEDS_OWNER`。** 这不是保守，是这三份文件的字段语义本来就没有交集。

### 1.4 `escalation_rule` 为什么也几乎全部不能提取

schema 要求四个子字段齐全：`metric` / `comparator` / `threshold` / `escalate_to`。
全文（四份方案）里能找到的"连续 N 窗口/超阈值就升级"口径只有 **2 处**，都在 §10 逐条列出：

| # | 出处 | 口径 | 已知子字段 | 缺 |
|---|---|---|---|---|
| 1 | [S37:110](../../plans/S37-audio-alignment.md) `AUD.VOICE.EMBEDDING_DRIFT` | 连续 3 窗口命中才升级 | `metric=consecutive_windows` / `comparator=gte` / `threshold=3` | **`escalate_to`** |
| 2 | [S36:174](../../plans/S36-semantic-qc.md) 全 `VIS.*` 族级 | 连续 ≥2 窗口命中才升级 | `metric=consecutive_windows` / `comparator=gte` / `threshold=2` | **`escalate_to`**；另外它点名字段 `promotion_windows` 不存在（§5.3） |

两处的 `escalate_to` 都写不出：`voice` 族的链是 `re_synthesize_speech → regenerate_video`，
但"升级"到底指前者跳到后者，还是指从 `repair` 跳到 `human_review`，方案没说；
S36 那句更模糊——它说的是"不直接升级为 **blocker**"，那是**严重度**升级，不是**动作**升级，
和 `escalation_rule` 的 `escalate_to`（动作枚举）根本不是同一个维度。

按 §1.1，缺 `escalate_to` 就不是完整依据，**仍标 `NEEDS_OWNER`**，但把三个已知子字段写进清单，
让 owner 只需要拍一个值。

**注意另一类"连续"不是 `escalation_rule`**：S35 §2.7 的决策算法里有 4 个阈值
（`min_coverage_ratio` 0.90 / `tau_low` 0.40 / `tau_abstain` 0.35 / `tau_disagreement` 0.30，
[S35:697-700](../../plans/S35-technical-qc.md)），S36 也复用 `tau_disagreement > 0.30`
（[S36:262](../../plans/S36-semantic-qc.md)）。这些是**报告级 policy**，由 S02 下发，
写进报告的 `policy_version`，不是 per-check 的 `escalation_rule`。把它们抄进 registry 是错的——
registry 的 `threshold` 是数字，policy 的阈值要能按租户/项目下发。

---

## 2. 99 条逐条清单

图例：`✓` 有依据；`✗` `NEEDS_OWNER`；`—` 该行本就不是缺陷。
`acts` / `rechk` 列的原文照抄，用来核对我的 ✓ 判定。

### 2.1 TECH（S35，23 条）

S35 是唯一有「允许动作」和「复检范围」两列的方案，所以它 23 条里 21 条能拿到
`allowed_actions`。但注意：**这两列都不是 schema 字段**，是方案自己的表述。

| # | line | check_id | type | sev | acts（原文） | rechk（原文） |
|---|---|---|---|---|---|---|
| 1 | 761 | `TECH.CONTAINER.PARSE` | ✓ | ✓ | ✓ `reingest, transcode` | ✓ `technical+hash` |
| 2 | 762 | `TECH.FILE.EMPTY` | ✓ | ✓ | ✓ `reingest` | ✓ `technical+hash` |
| 3 | 763 | `TECH.FILE.TRUNCATED` | ✓ | ✓ | ✓ `reingest, transcode` | ✓ `technical+hash` |
| 4 | 764 | `TECH.DECODE.PACKET_ERROR` | ✓ | ✓ | ✓ `transcode` | ✓ `technical+hash` |
| 5 | 765 | `TECH.DURATION.MISMATCH` | ✓ | ✓ | ✓ `regenerate_video, temporal_edit` | ✓ `technical` |
| 6 | 766 | `TECH.DURATION.ZERO` | ✓ | ✓ | ✓ `reingest` | ✓ `technical+hash` |
| 7 | 767 | `TECH.RESOLUTION.BELOW_SPEC` | ✓ | ✓ | ✓ `regenerate_video` | ✓ `technical` |
| 8 | 768 | `TECH.RESOLUTION.MISMATCH` | ✓ | ✓ | ✓ `regenerate_video, transcode` | ✓ `technical` |
| 9 | 769 | `TECH.FPS.MISMATCH` | ✓ | ✓ | ✓ `transcode` | ✓ `technical` |
| 10 | 770 | `TECH.FPS.VFR` | ✓ | ✓ | ✓ `transcode` | ✓ `technical` |
| 11 | 771 | `TECH.CODEC.UNSUPPORTED` | ✓ | ✓ | ✓ `transcode` | ✓ `technical` |
| 12 | 772 | `TECH.COLOR.RANGE_MISMATCH` | ✓ | ✓ | ✓ `transcode` | ✓ `technical` |
| 13 | 773 | `TECH.BLACK_FRAME` ⚠ | ✓ | ✓ 带条件 | ✓ `temporal_edit, regenerate_video` | ✗ `连续窗口 ±0.5s` |
| 14 | 774 | `TECH.FREEZE_FRAME` ⚠ | ✓ | ✓ | ✓ `temporal_edit, regenerate_video` | ✗ `连续窗口 ±0.5s` |
| 15 | 775 | `TECH.FLASH_FRAME` ⚠ | ✓ | ✓ | ✓ `temporal_edit`（单成员） | ✗ `窗口 ±0.5s` |
| 16 | 776 | `TECH.AUDIO.MISSING` | ✓ | ✓ 带条件 | ✓ `regenerate_video, remix` | ✓ `technical+audio` |
| 17 | 777 | `TECH.AUDIO.OFFSET` | ✓ | ✓ | ✗ `remux→transcode` | ✗ `audio+前后 0.5s` |
| 18 | 778 | `TECH.AUDIO.SILENCE_HEAD_TAIL` | ✓ | ✓ | ✓ `temporal_edit`（单成员） | ✗ `窗口` |
| 19 | 779 | `TECH.LOUDNESS.LUFS` | ✓ | ✓ 带条件 | ✓ `remix`（单成员） | ✗ `全轨` |
| 20 | 780 | `TECH.LOUDNESS.TRUE_PEAK` | ✓ | ✓ 带条件 | ✓ `remix`（单成员） | ✗ `全轨` |
| 21 | 781 | `TECH.AUDIO.CLIPPING` | ✓ | ✓ | ✓ `remix`（单成员） | ✗ `全轨` |
| 22 | 782 | `TECH.SUBTITLE.BURNED_IN` | ✓ | ✓ | ✓ `regenerate_video`（单成员） | ✗ `全片抽检` |
| 23 | 783 | `TECH.META.MISSING` | ✓ | ✓ | ✗ `—` | ✗ `—` |

S35 逐字段统计：

| 字段 | 可提取 | 说明 |
|---|---|---|
| `defect_type` | 23 / 23 | |
| `severity_default` | 19 无歧义 + 4 带条件 | #13 / #16 / #19 / #20 的"（头尾>阈值）""（有对白镜头）""（master 时 blocker）""（master）"是**条件严重度**，schema 只有一个标量 |
| `severity_max` | **0** | schema 要上下界两个值，表里只有一列。§2.2.2 说「`severity_default` / `severity_max` 严重度上下界」——这句话是**字段作用说明**，不是给 4 个带条件行的映射规则；无条件下两个值是否相等，四份文件都没说。全 99 条同此（见 §6 末） |
| `allowed_actions` | 21 / 23 | #17 `remux` 不在枚举（见 §5.1）；#23 是 `—` |
| `default_action` | 14 / 23 | 仅单成员可取；其余 9 条需 owner 定起点（7 条多成员、1 条枚举外 `remux→transcode`、1 条为 `—`） |
| `recheck_scope` | 13 / 23 | 其余 10 条不可取：#13–#15、#17–#22 是自由文本，**其中 #18 的「窗口」是 2 字但不在枚举**，#23 是 `—` |
| `window_scope` | **0 / 23** | §1.3 |
| `escalation_rule` | **0 / 23** | §1.4 |

**S35 的关键点：它有 7 条多动作 check，`allowed_actions` 能提，但 `default_action` 提不了。**
「允许动作」列给的是"可以走哪几条路"，不是"先走哪条"。[S39:98](../../plans/S39-repair-planning.md)
的缺省是 `family[0]`，但那是**缺省**，不是这条 check 的登记值。按 §1.1 不能拿缺省冒充数据。

### 2.2 VIS（S36，32 条）

表头是 `check_id | type | 严重度 | 判定 | 依据` —— **没有动作列，没有复检列**。

| # | line | check_id | type | sev | 路由字段 |
|---|---|---|---|---|---|
| 24 | 186 | `VIS.IDENTITY.FACE_DRIFT` | ✓ | ✓ | 全 ✗ |
| 25 | 187 | `VIS.IDENTITY.FACE_SWAP_WITHIN_SHOT` | ✓ | ✓ | 全 ✗ |
| 26 | 188 | `VIS.IDENTITY.BODY_PROPORTION` | ✓ | ✓ | 全 ✗ |
| 27 | 189 | `VIS.IDENTITY.OUTFIT_REVISION` | ✓ | ✓ | 全 ✗ |
| 28 | 190 | `VIS.IDENTITY.ACCESSORY_MISSING` | ✓ | ✓ | 全 ✗ |
| 29 | 191 | `VIS.IDENTITY.ACCESSORY_EXTRA` | ✓ | ✓ | 全 ✗ |
| 30 | 197 | `VIS.ANATOMY.LIMB_COUNT` | ✓ | ✓ | 全 ✗ |
| 31 | 198 | `VIS.ANATOMY.JOINT_ARTIFACT` | ✓ | ✓ | 全 ✗ |
| 32 | 199 | `VIS.HANDS.FINGER_DEFORM` | ✓ | ✓ | 全 ✗ |
| 33 | 200 | `VIS.HANDS.OCCLUSION_ARTIFACT` | ✓ | ✓ | 全 ✗ |
| 34 | 201 | `VIS.HANDS.INTERACTION` | ✓ | ✓ | 全 ✗ |
| 35 | 209 | `VIS.SPACE.PERSON_COUNT` | ✓ | ✓ | 全 ✗ |
| 36 | 210 | `VIS.SPACE.RELATIVE_POSITION` | ✓ | ✓ | 全 ✗ |
| 37 | 211 | `VIS.SPACE.PROP_STATE` | ✓ | ✓ | 全 ✗ |
| 38 | 212 | `VIS.SPACE.PROP_OWNERSHIP` | ✓ | ✓ | 全 ✗ |
| 39 | 213 | `VIS.SPACE.BACKGROUND_JUMP` | ✓ | ✓ | 全 ✗ |
| 40 | 219 | `VIS.CAMERA.SHOT_TYPE` | ✓ | ✓ | 全 ✗ |
| 41 | 220 | `VIS.CAMERA.ANGLE` | ✓ | ✓ | 全 ✗ |
| 42 | 221 | `VIS.CAMERA.MOTION` | ✓ | ✓ | 全 ✗ |
| 43 | 222 | `VIS.ACTION.BEAT_MISSING` | ✓ | ✓ | 全 ✗ |
| 44 | 223 | `VIS.ACTION.BEAT_EXTRA` | ✓ | ✓ | 全 ✗ |
| 45 | 224 | `VIS.ACTION.TIMING` | ✓ | ✓ | 全 ✗ |
| 46 | 230 | `VIS.TEMPORAL.FLICKER` | ✓ | ✓ | 全 ✗ |
| 47 | 231 | `VIS.TEMPORAL.MORPH_ARTIFACT` | ✓ | ✓ | 全 ✗ |
| 48 | 232 | `VIS.TEMPORAL.BOUNDARY_POP` | ✓ | ✓ | 全 ✗ |
| 49 | 233 | `VIS.CONTINUITY.SHOT_BOUNDARY` | ✓ | ✓ | 全 ✗ |
| 50 | 234 | `VIS.CONTINUITY.AXIS_CROSSING` | ✓ | ✓ 带条件 | 全 ✗（**§3 有跨文档依据，见下**） |
| 51 | 235 | `VIS.CONTINUITY.LIGHT_DIRECTION` | ✓ | ✓ | 全 ✗ |
| 52 | 236 | `VIS.SAFE_AREA.TEXT_OVERLAP` | ✓ | ✓ | 全 ✗ |
| 53 | 242 | `VIS.STATE.EXPECTED_VISIBLE` | ✓ | ✓ | 全 ✗ |
| 54 | 243 | `VIS.STATE.UNEXPECTED_VISIBLE` | ✓ | ✓ | 全 ✗ |
| 55 | 244 | `VIS.KNOWLEDGE.SCOPE_RENDER` | ✗ `—` | ✗ `不存在` | **非缺陷记录**，见 §11.5 |

### 2.3 AUD（S37，20 条）

表头是 `check_id | type | 严重度 | 判定 | 证据`（§3.2/§3.3/§3.4 是 4 列，无证据列）——
**没有动作列，没有复检列**。

| # | line | check_id | type | sev | 路由字段 |
|---|---|---|---|---|---|
| 56 | 73 | `AUD.TEXT.MISSING_WORD` | ✓ | ✓ | 全 ✗ |
| 57 | 74 | `AUD.TEXT.EXTRA_WORD` | ✓ | ✓ | 全 ✗ |
| 58 | 75 | `AUD.TEXT.MISPRONOUNCE` | ✓ | ✓ | 全 ✗ |
| 59 | 76 | `AUD.TEXT.SUBSTITUTION` | ✓ | ✓ 带条件 | 全 ✗（**§3 有跨文档依据**） |
| 60 | 77 | `AUD.TEXT.PROSODY_FLAT` | ✓ | ✓ | 全 ✗ |
| 61 | 78 | `AUD.TEXT.SCRIPT_CONFLICT` | ✗ `—` | ✗ `—` | **非缺陷记录**，见 §11.5 |
| 62 | 86 | `AUD.ALIGN.LOW_CONFIDENCE` | ✓ | ✓ | 全 ✗ |
| 63 | 87 | `AUD.ALIGN.GAP` | ✓ | ✓ | 全 ✗ |
| 64 | 88 | `AUD.ALIGN.OVERLAP` | ✓ | ✓ | 全 ✗ |
| 65 | 89 | `AUD.ALIGN.SPEAKER_SWITCH` | ✓ | ✓ | 全 ✗ |
| 66 | 90 | `AUD.ALIGN.DURATION_VS_SPEC` | ✓ | ✓ | 全 ✗（**§3 有同文档依据**） |
| 67 | 98 | `AUD.LIPSYNC.OFFSET` | ✓ | ✗ **二值** | 全 ✗ |
| 68 | 99 | `AUD.LIPSYNC.OPENNESS_MISMATCH` | ✓ | ✓ | 全 ✗ |
| 69 | 100 | `AUD.LIPSYNC.MOUTH_MOVES_SILENT` | ✓ | ✓ | 全 ✗ |
| 70 | 101 | `AUD.LIPSYNC.SPEAKS_CLOSED` | ✓ | ✓ | 全 ✗ |
| 71 | 102 | `AUD.LIPSYNC.FACE_NOT_FOUND` | ✓ | ✗ `—` | **非缺陷记录**，见 §11.5（注意：这条**仍需登记**） |
| 72 | 110 | `AUD.VOICE.EMBEDDING_DRIFT` | ✓ | ✓ | `escalation_rule` **部分 ✓**，见 §3 |
| 73 | 111 | `AUD.VOICE.CHARACTER_MIXUP` | ✓ | ✓ | 全 ✗ |
| 74 | 112 | `AUD.VOICE.EMOTION_MISMATCH` | ✓ | ✓ | 全 ✗ |
| 75 | 113 | `AUD.VOICE.AUTHORIZATION` | ✓ | ✓ | **§3 有依据：`never_auto_repair`** |

`AUD.LIPSYNC.OFFSET`（#67）的严重度写的是 **`blocker（>120ms）/ major（>60ms）`** ——
两个值配两个阈值。schema 的 `severity_default` / `severity_max` 是标量，装不下。
这条需要 owner 决定怎么映射（例如 default=`major`, max=`blocker`，把阈值放进 `escalation_rule`），
**但这正是 `escalation_rule` 该长什么样的样板，值得单独拍**。

### 2.4 SUB（S38，24 条）

表头是 `check_id | type | 严重度 | 判定` —— **只有 4 列**。

| # | line | check_id | type | sev | 路由字段 |
|---|---|---|---|---|---|
| 76 | 288 | `SUB.TIMING.OVERLAP` | ✓ | ✓ | 全 ✗ |
| 77 | 289 | `SUB.TIMING.NEGATIVE_DURATION` | ✓ | ✓ | 全 ✗ |
| 78 | 290 | `SUB.TIMING.TOO_SHORT` | ✓ | ✓ | 全 ✗ |
| 79 | 291 | `SUB.TIMING.TOO_LONG` | ✓ | ✓ | 全 ✗ |
| 80 | 292 | `SUB.TIMING.CPS_EXCEEDED` | ✓ | ✓ | **§3 有同文档依据** |
| 81 | 293 | `SUB.TIMING.OUT_OF_RANGE` | ✓ | ✓ | 全 ✗ |
| 82 | 294 | `SUB.TIMING.DRIFT_VS_ALIGNMENT` | ✓ | ✓ | 全 ✗ |
| 83 | 295 | `SUB.TIMING.FRAME_BOUNDARY` | ✓ | ✓ | 全 ✗ |
| 84 | 301 | `SUB.LAYOUT.LINE_OVERFLOW` | ✓ | ✓ | 全 ✗ |
| 85 | 302 | `SUB.LAYOUT.TOO_MANY_LINES` | ✓ | ✓ | 全 ✗ |
| 86 | 303 | `SUB.LAYOUT.SAFE_AREA` | ✓ | ✓ | 全 ✗ |
| 87 | 304 | `SUB.LAYOUT.ORPHAN_WORD` | ✓ | ✓ | 全 ✗ |
| 88 | 305 | `SUB.LAYOUT.FONT_MISSING` | ✓ | ✓ | 全 ✗ |
| 89 | 313 | `SUB.TEXT.SOURCE_STALE` | ✓ | ✓ 带条件 | 全 ✗ |
| 90 | 314 | `SUB.TEXT.TRANSLATION_STALE` | ✓ | ✓ 带条件 | 全 ✗ |
| 91 | 315 | `SUB.TEXT.TRANSLATION_DRAFT` | ✓ | ✓ | 全 ✗ |
| 92 | 316 | `SUB.TEXT.TRANSLATION_MISSING` | ✓ | ✓ | 全 ✗ |
| 93 | 317 | `SUB.TEXT.TRANSLATION_LENGTH_DRIFT` | ✓ | ✓ | 全 ✗ |
| 94 | 318 | `SUB.TEXT.LEXICON_UNUSED` | ✓ | ✓ | 全 ✗ |
| 95 | 319 | `SUB.TEXT.SCRIPT_CONFLICT` | ✓ | ✓ | 全 ✗ |
| 96 | 327 | `SUB.FORMAT.SRT_SYNTAX` | ✓ | ✓ | 全 ✗ |
| 97 | 328 | `SUB.FORMAT.ENCODING` | ✓ | ✓ | 全 ✗ |
| 98 | 329 | `SUB.FORMAT.ASS_STYLE_MISSING` | ✓ | ✓ | 全 ✗ |
| 99 | 330 | `SUB.FORMAT.BOM` | ✓ | ✓ | 全 ✗ |

---

## 3. 方案里**已经写了依据**的（可据此登记，但需 owner 确认口径）

这一节是全文档唯一有"新增信息"的部分。每条都给原文出处，判断依据按 §1.1。

### 3.1 同文档、本表格之外但明确指向该 check 的

| check | 字段 | 值 | 出处 | 强度 |
|---|---|---|---|---|
| `AUD.VOICE.AUTHORIZATION` | `never_auto_repair` | `true` | [S37:115](../../plans/S37-audio-alignment.md)「命中时 `recommended_actions` 只能是 `human_review`（不能自动重生成——重生成可能用同一个已撤销音色）」 | **强**。同一句既否定自动重生成，又给了理由。 |
| `AUD.VOICE.AUTHORIZATION` | `no_auto_reason` | `rights_authorization` | [S39:157](../../plans/S39-repair-planning.md) 把这条 check 逐字列在 `rights_authorization` 判据下 | **强**。S39 的 §3.3 表就是这三值的权威分配。 |
| `AUD.ALIGN.DURATION_VS_SPEC` | `never_auto_repair` | `true` | [S37:92](../../plans/S37-audio-alignment.md)「属于**创作决策**，不能自动选。S37 只报偏差，S39 据此判 `human_review`」 | **强**。"不能自动选"+"判 human_review"是同一句。 |
| `AUD.ALIGN.DURATION_VS_SPEC` | `no_auto_reason` | `creative_decision` | [S39:156](../../plans/S39-repair-planning.md) 逐字列在 `creative_decision` 下，理由「台词比镜头长，怎么消解是导演的」 | **强**。 |
| `AUD.VOICE.EMBEDDING_DRIFT` | `escalation_rule` 的三个子字段 | `metric=consecutive_windows`, `comparator=gte`, `threshold=3` | [S37:110](../../plans/S37-audio-alignment.md)「连续 3 窗口命中才升级」 | **中**。缺 `escalate_to`（见 §1.4）。 |
| `SUB.TIMING.CPS_EXCEEDED` | `never_auto_repair` | `true` | [S38:278](../../plans/S38-subtitles.md)「仍超 → … 推荐动作 `human_review`（可能是台词太长，属创作决策）」 | **中**。这句话的 check 名写错了（§5.2），但语义唯一指向 `SUB.TIMING.CPS_EXCEEDED`——表里只有它带 cps 判定。 |
| `SUB.TIMING.CPS_EXCEEDED` | `no_auto_reason` | `creative_decision` | 同上「属创作决策」 | **中**。 |
| 全部 `SUB.TEXT.*_STALE` | 同族动作禁止 | `subtitle` 族不含 `regenerate_video` | [S35:152](../../plans/S35-technical-qc.md)「`SUB.TEXT.*_STALE` 命中时走 `human_review`」；[S35:133](../../plans/S35-technical-qc.md) subtitle 族「不得触发视频重生成；**不自动重译**」 | **中**。这是族级约束，`default_action` 仍缺。 |

### 3.2 跨文档依据（S39 指定，S36/S37 未复述）

| check | 字段 | 值 | 出处 | 强度 |
|---|---|---|---|---|
| `VIS.CONTINUITY.AXIS_CROSSING` | `never_auto_repair` | `true` | [S39:156](../../plans/S39-repair-planning.md) 逐字列在 `creative_decision`，「改分镜还是重拍整场」 | **中**。S36 自己没写；但 S39 是消费方，它的归类表就是这份数据的规格来源之一。 |
| `VIS.CONTINUITY.AXIS_CROSSING` | `no_auto_reason` | `creative_decision` | 同上 | **中**。 |
| `AUD.TEXT.SUBSTITUTION` | `never_auto_repair` | `true` | [S39:156](../../plans/S39-repair-planning.md) 逐字列在 `creative_decision`，「换词改语义」 | **中**。 |
| `AUD.TEXT.SUBSTITUTION` | `no_auto_reason` | `creative_decision` | 同上 | **中**。 |

**为什么"中"不是"强"**：S39:152 自己说了「**示例，不是穷举**——清单在 registry 里，S39 不维护」。
所以 S39 的列表可以当**提案**，但不能当**已确认的登记值**。owner 确认时的动作很轻
（"是的，这三条按人审"），但这一步不能省，否则又是把某处散文当数据。

### 3.3 这一节的净结论

- **能直接落、只差 owner 点头的：5 条 check 的 `never_auto_repair` + `no_auto_reason`** ——
  `AUD.VOICE.AUTHORIZATION` / `AUD.ALIGN.DURATION_VS_SPEC` / `SUB.TIMING.CPS_EXCEEDED` /
  `VIS.CONTINUITY.AXIS_CROSSING` / `AUD.TEXT.SUBSTITUTION`。五条都只差一个"确认"，不缺依据。
- **能落 3/4 子字段的：1 条**（`AUD.VOICE.EMBEDDING_DRIFT`，缺 `escalate_to`）。
- 其余 **94 条**的 `window_scope` / `escalation_rule` / `default_action` / `recheck_scope`
  **全部 `NEEDS_OWNER`**（上面这 5 条也缺这几项——`never_auto_repair` 只解决其中一个字段）。

  为什么是 94 不是 99−5：上述 5 条 **+ `AUD.VOICE.EMBEDDING_DRIFT`** 都已经"有依据"了，
  但它仅覆盖 `escalation_rule` 的 3/4 个子字段，`escalate_to` 和另外三个字段仍缺，
  所以它**不退出待办**。退出待办的只有 `never_auto_repair` + `no_auto_reason` 这两个字段本身，
  没有任何一条 check 因为这一节的结论而整条办完。99 − 5 = 94 是巧合：
  5 条办完两个字段，第 6 条办完 0.75 个字段，**没有一条 check 是完整的**。

---

## 4. 3 条 `check_id` 命名不合规（现状 · 建议 · 后果）

S35:157 定的规约：`<LAYER>.<GROUP>.<NAME>`，全大写下划线，全局唯一。
schema 的 `check_id` pattern 也强制三段：

```
^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$
```

三行只有两段，**首轮跑 `validate_registry.py` 会被拒**（这是我在上一条任务里实测到的：
`REJECTED: 3 problem(s)`，三条都是 `does not match`）。

| 现状 id | 行 | 建议 id | 为什么建议这个分组 | 不改的后果 |
|---|---|---|---|---|
| `TECH.BLACK_FRAME` | [773](../../plans/S35-technical-qc.md) | `TECH.FRAME.BLACK` | 判定是「连续全黑帧 ≥ `black_min_frames`」，证据是「黑帧区间 + 拼板」——**帧级时间域缺陷**，缺陷的载体是帧序列 | ① 按规约写的解析器（含 schema 的 `pattern`）漏掉它；② registry 按 `<GROUP>` 分组时无处归组；③ 它恰好是 README「console 计数偏小」的成因之一——按三段式正则数会少 3 行 |
| `TECH.FREEZE_FRAME` | [774](../../plans/S35-technical-qc.md) | `TECH.FRAME.FREEZE` | 判定是「连续相同帧 ≥ `freeze_min_seconds`」，证据是「区间 + 首末帧」——同上是**帧级时间域** | 同上 |
| `TECH.FLASH_FRAME` | [775](../../plans/S35-technical-qc.md) | `TECH.FRAME.FLASH` | 判定是「**单帧**亮度超 `flash_luma_delta`」，证据是「帧号 + 亮度曲线」——单帧，但仍是帧级时间域 | 同上 |

**为什么是 `FRAME` 而不是别的分组**：S35 §2 的层内分组看 `check_id` 第二段
（`CONTAINER` / `FILE` / `DECODE` / `DURATION` / `RESOLUTION` / `FPS` / `CODEC` / `COLOR` /
`AUDIO` / `LOUDNESS` / `SUBTITLE` / `META`）。这三条的共同点是**缺陷定义在帧的时间序列上**，
而不是容器、文件、编码参数。`FRAME` 是唯一能覆盖三者且不与现有分组重名的名字。

**这不只是改名。** 改名要同步：
- S35 §4 表（773–775）；
- S35 §12 的 E24-T4 / E24-T5（[978](../../plans/S35-technical-qc.md) / [979](../../plans/S35-technical-qc.md) 的验收向量，引用旧名）；
- 任何已按旧名建了判定的 S39 测试向量。

我全仓 grep 过旧名。除 S35 这三行与两条 E24 向量外，它们还以**允许名单**的形式出现在
`tools/check_all.py` 的 `KNOWN_BAD_CHECK_IDS`（`check_check_ids` 用）——那是这份清单
写完之后才落地的，见 §8.1 第 12 处。**但改名是契约变更，本清单只做提案，不动 S35 的行。**

---

## 5. 核对时顺带查实的另外 3 个缺陷

这三条不在原任务范围内，但都会影响 registry 能否落地，一并列出。**都不由我改。**

### 5.1 `TECH.AUDIO.OFFSET` 的动作值 `remux` 不在枚举里

[S35:777](../../plans/S35-technical-qc.md) 写的是 `remux→transcode`。
`schema` 的 12 值动作枚举里**没有 `remux`**（最接近的是 `transcode` / `remix`）。
按 §1.1 这条不可提取。

现在只有两个选择：① 登记成 `transcode`（丢掉"只换容器不重编码"的语义，可能让 S39 花不必要的重编码钱）；
② 给 `action-families.v2.json` 与 schema 加 `remux`（契约变更 + 全体 QC 服务发版）。
**这是契约层的决定，不是提取问题。**

顺带：`contracts/README.md` 原写「S35 中 3 处动作、11 处复检范围」在枚举外。
**这两个数都是转义管道符误解析的产物，正确值是 2 处动作、10 处复检范围。**

（这两个数改了两轮才对：第一轮修成「1 处 / 10 处」，动作那格又少了 `TECH.META.MISSING`
（[783](../../plans/S35-technical-qc.md)）的 `—`——复检那格已经把 `—` 算进 10 处，动作这格
却只数了 `remux→transcode`。两格必须用同一口径：**非枚举值一律计数，`—` 也是非枚举值**。
第二轮已改回 2 / 10。）

成因：`TECH.DURATION.MISMATCH`（[765](../../plans/S35-technical-qc.md)）的判定列含
`\|actual − ShotSpec.duration\|`。朴素 `split('|')` 把这一行切成 **9 列**（其余 22 行都是 7 列），
于是：

| 按朴素解析取的格 | 实际内容 | 被误判为 |
|---|---|---|
| `c[5]`（动作列） | `> 1 帧` | 第 3 处枚举外动作 |
| `c[6]`（复检列） | `实测时长` | 第 11 处枚举外复检范围 |

（这两格的值以朴素 `split('|')` 后按位置取的第 6、第 7 个元素为准。整行 9 格是
`0 ID / 1 technical / 2 blocker / 3 "\" / 4 "actual − ShotSpec.duration\" / 5 "> 1 帧" /
6 实测时长 / 7 "regenerate_video, temporal_edit" / 8 technical` —— 复检列实际落到的
`c[6]` 是 `实测时长`，不是 `regenerate_video`（后者在动作格 `c[7]` 里，那一格是
`regenerate_video, temporal_edit` 两个动作）。原写 `regenerate_video` 把右移一格的落点又往前
挪了一格；三个候选值（`实测时长` / `regenerate_video` / `regenerate_video, temporal_edit`）
都不在 `recheck_scope` 枚举内，所以 3/11 与 2/10 的结论不受影响，错的是这张示意表的最后一格。
见 §5.1 更正记录。）

用 `(?<!\\)\|` 切分后这一行仍是 7 列，正确结果是：

- **动作列枚举外 = 2 处**：#17 的 `remux→transcode`、#23 的 `—`。
- **复检列枚举外 = 10 处**：9 条自由文本（#13/#14/#15/#17/#18/#19/#20/#21/#22，其中 #18 写的是「窗口」）
  + #23 的 `—`。

也就是说，README 那格里写的两处偏差**互相是同一个 bug 的两个症状**，而它把一张 22 行正常的表
变成了"23 行里 3 处动作错"。这条教训要用在下游：**任何按列位读这四份表的脚本，
必须先处理转义管道符，否则列会整体右移一格。**

### 5.2 S38:278 引用了一个不存在的 `check_id`

[S38:278](../../plans/S38-subtitles.md) 写「仍超 → `SUB.LAYOUT.CPS_EXCEEDED` 缺陷」，
但 §5 的注册表里只有 `SUB.TIMING.CPS_EXCEEDED`（[292](../../plans/S38-subtitles.md)），
另有 `SUB.LAYOUT.*` 四条（`LINE_OVERFLOW` / `TOO_MANY_LINES` / `SAFE_AREA` / `ORPHAN_WORD`），
**没有 `SUB.LAYOUT.CPS_EXCEEDED`**。

按 [`contracts/README.md`](../../contracts/README.md) 和 schema，registry 的键必须与报告里发出的
`check_id` 一致。若实现照 §4.3 发 `SUB.LAYOUT.CPS_EXCEEDED`，而 registry 按 §5 登记
`SUB.TIMING.CPS_EXCEEDED`，S39 会走 §3.1.1 的「registry 缺失该 `check_id`」分支 →
`human_review` + `registry_entry_invalid_total{check_id}` 告警。**结果偏保守，但每天在报警。**

建议改 §4.3 的文案（`SUB.LAYOUT` → `SUB.TIMING`），不是改表。**同样要 owner 确认。**

### 5.3 `promotion_windows` 不是 schema 字段

[S36:174](../../plans/S36-semantic-qc.md) 写「阈值记在 `check-registry` 的 `promotion_windows`」，
但 `check-registry.v2.schema.json` 的属性列表里**没有 `promotion_windows`**
（schema 是 `additionalProperties: false`，加不了未声明字段）。

这句话描述的功能——"连续 ≥2 窗口命中才升级"——正好该由 `escalation_rule` 的
`metric=consecutive_windows` 表达。所以要么 S36 改文案为「记在 `escalation_rule`」，
要么 schema 加字段。**前者更小，但仍是契约层拍板。**

---

## 6. 这 99 条离 registry 还差什么（按 owner 分组）

这份清单交付后，四个 QC 服务可以各自认领。**每人要填的字段数不同**：

| owner | 条数 | 已可提取 | 需 owner 决定 | 阻塞实现吗 |
|---|---|---|---|---|
| S35 | 23 | `defect_type` 23、`severity_default` 19+4 带条件、`allowed_actions` 21、`recheck_scope` 13 | `window_scope` 23、`escalation_rule` 23、`severity_max` 23、`default_action` 9、`recheck_scope` 10、`allowed_actions` 2 | 是 |
| S36 | 32 | `defect_type` 31、`severity_default` 30+1 带条件（32 条里 1 条无严重度） | `window_scope` 32、`escalation_rule` 32、`allowed_actions` 32、`default_action` 32、`recheck_scope` 32、`severity_max` 32（**外加 §5.3**）；另 1 条 `never_auto_repair` 可从 S39 取（§3.2） | 是 |
| S37 | 20 | `defect_type` 19（1 条非缺陷：`AUD.TEXT.SCRIPT_CONFLICT`，`type=—`）、`severity_default` 16 无歧义 + 1 带条件 + 1 二值（`AUD.LIPSYNC.OFFSET`）+ 2 空 | `window_scope` 20、`escalation_rule` 20、`allowed_actions` 20、`default_action` 20、`recheck_scope` 20、`severity_max` 20；另 **3 条** check 的 `never_auto_repair` 可从散文取（§3：`AUD.VOICE.AUTHORIZATION` / `AUD.ALIGN.DURATION_VS_SPEC` / `AUD.TEXT.SUBSTITUTION`） | 是 |
| S38 | 24 | `defect_type` 24、`severity_default` 22+2 带条件 | `window_scope` 24、`escalation_rule` 24、`allowed_actions` 24、`default_action` 24、`recheck_scope` 24、`severity_max` 24；另 1 条 `never_auto_repair`（§3，需先修 §5.2） | 是 |

**一个共同的卡点**：`severity_max` 四份表都没有。"严重度"列只有一档。§2.2.2 写它是"上下界"，
但没说不带条件的行是否两个值相等。**可签字的条文写在 §11**：一次规则覆盖 87 条，
不覆盖的 12 条另有处置，否决后也有备选。不在这里重复。

**建议的推进顺序**（不是我可以单方面定的，提给 owner）：
1. **先拍 3 条改名（§4）**——它不改，`validate_registry.py` 直接拒载，后面填什么都进不去。
   这是唯一一个"阻塞一切"的动作，成本却最小（现只影响 S35 三行 + E24-T4/T5 两条向量）。
2. 再拍 `severity_max` 的缺省规则（§11）→ 一次解决 99 条里的一个字段。
3. 再按族逐个拍 `window_scope`（§9）——按 14 个 `defect_type` 分组比按 99 条逐条快。
4. 最后逐条 `default_action` / `escalation_rule`（§10）。

---

## 7. 产出位置与理由

放在 `delivery/acceptance/`，不放 `plans/`。

| 候选 | 为什么选 / 不选 |
|---|---|
| `delivery/acceptance/` ✅ | 同目录下已有 [plans-consistency-2026-10-30.md](plans-consistency-2026-10-30.md)，它的 §2.4 末尾和 §7 第 6 条正是这份清单的上游——那份报告写"99 条 check 的登记项字段仍缺……已派给 S35–S39 会话产出缺字段清单"。清单是那份验收报告的**下游待办**，放一起才对得上。 |
| `plans/` ❌ | `plans/` 放的是 48 份服务实现方案，每份对应一个服务。这份清单跨 S35–S38 四个服务，且它不含设计决策——只是核对结果。按 AGENTS.md「`plans/<服务ID>-<slug>.md`」的命名约定，它没有合法的服务 ID 可用。 |
| `contracts/` ❌ | 不是契约。README 已明说数据文件落不了，再塞一份清单进 `contracts/` 会让"这里应该是已定稿的东西"这条规矩变松。 |
| `docs/design-package/` ❌ | 只读原始输入。 |

---

## 8. 复核方法

我用一个 15 行的 Python 脚本逐行解析四份表，输出见本节末。

**注意转义管道符**：`TECH.DURATION.MISMATCH` 的判定列含 `\|actual − ShotSpec.duration\|`。
朴素 `split('|')` 会把这行切成 9 列，把判定列的后半段挤进动作列——这正是"3 处动作在枚举外"
这个误判的来源（实际只有 2 处）。必须用 `(?<!\\)\|` 切分。

复算口径：

```python
SPLIT = re.compile(r'(?<!\\)\|')
def cells(line): ...          # 去首尾 |，按未转义 | 切，单元格里的 \| 还原成 |
pat = re.compile(r'^\|\s*`(TECH|VIS|AUD|SUB)\.[A-Z0-9_.]+`\s*\|')
```

| 方案 | 行号范围 | 条数 |
|---|---|---|
| S35 | 761–783 | 23 |
| S36 | 186–244 | 32 |
| S37 | 73–113 | 20 |
| S38 | 288–330 | 24 |
| **合计** | | **99** |

按三段式正则 `^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$` 数只能得到 **96**,
少掉的正是 §4 那三行。**这两个数都要能复现，才说明数对了。**

### 8.1 数字复核（2026-11-05 第二版）

上一版正文里的数字是逐个手写的，不是脚本输出。这次全部用同一口径重算，**发现 15 处对不上**
（第 9、10 处是第二轮复核出来的：第 9 处来自加 §9 之后，第 10 处在 `contracts/README.md`；
第 11–13 处是 2026-11-08 的第三次复核，其中第 13 处是复盘第 12 处时发现的、错在所填的提交号；
第 14、15 处是 2026-11-08 的第四次复核，均无数字偏移，只改标签/族数的写法），
已就地改正（另有一组复核确认为真，列在后面的对照组）。记录格式：原写 → 正确 → 错因。

| # | 位置 | 原写 | 正确 | 错因 |
|---|---|---|---|---|
| 1 | §0 「一句话」 | 「能推进一步的是 S35 的 **35 条**……剩下 **64 条**（S36/S37/S38）」 | S35 **23 条**；其余 **76 条**（S36 32 / S37 20 / S38 24） | 35 与 64 相加正好 99，所以没被察觉。但 35 既不是 S35 的条数（23），也不是任何可解释的分组；`64` 是 99−35 的余数。§2.1 的标题和 §6 的表当时已经写的是 23，同一份文档里两个值并存 |
| 2 | §3.3 | 「其余 **93 条**」 | **94 条** | 99 − 5（有 `never_auto_repair` 依据的 5 条）= 94。原值把 §3.3 上一行的 `AUD.VOICE.EMBEDDING_DRIFT` 也扣掉了，但它只解决 `escalation_rule` 的 3/4 个子字段，整条 check 并未办完，不应退出分母。改后另加了一句说明为什么是 94 而不是 99−5 的巧合 |
| 3 | §6 S37 行 | 「另 **4 条** check 的 `never_auto_repair` 可从散文取（§3）」 | **3 条**：`AUD.VOICE.AUTHORIZATION` / `AUD.ALIGN.DURATION_VS_SPEC` / `AUD.TEXT.SUBSTITUTION` | 把 `AUD.VOICE.EMBEDDING_DRIFT` 也算进去了。它是 §3.1 的"3/4 子字段"案例，给的是 `escalation_rule`，不是 `never_auto_repair` |
| 4 | §6 S37 行 | 「`defect_type` **19**」→ **18** → **19**（三次） | 第二版改成 18 是**改错了**：[S37:102](../../plans/S37-audio-alignment.md) `AUD.LIPSYNC.FACE_NOT_FOUND` 的 `type` 是 `lipsync`，只有严重度是 `—`，应计入 `defect_type`。S37 真正的非缺陷行只有 [S37:78](../../plans/S37-audio-alignment.md) `AUD.TEXT.SCRIPT_CONFLICT`（`type=—`）。本文件 §8.1 对照组写的 97/99 才是对的（99 − 2 条 `type=—`），§6 与它自相矛盾。第三版已改回 19 |
| 5 | §0 / §1.4 | 「只有 **1 条**散文给了"连续 3 窗口"的口径」 | **2 处**：[S37:110](../../plans/S37-audio-alignment.md) 逐条 + [S36:174](../../plans/S36-semantic-qc.md) 族级（"连续 ≥2 窗口"） | 只 grep 了 `连续 \d+ 窗口`，S36 写的是「连续 **≥2** 窗口」，中间有字符，漏了。且它用了「升级为 blocker」这个说法，字面上不像动作升级，容易跳过。这条不只在 §0：§1.4 整节是按"只有 1 条"写的，已一并改写（现在 §1.4 + §10 覆盖 2 处，并新增了 A3–A5 三条"S38 的处理顺序"） |
| 6 | §10.7 盘点合计 F 行 | 「其余 **94** 条 check，散文里一个字都没有」 | **55 条**（14 行被点名 + 30 行仅被族级覆盖 + 55 行无散文 = 99） | 94 是拿"99 − 5（A 类散文条目）"减出来的，但 A–E 数的是**散文条目**，不是**行**：一条族级散文（[S36:174](../../plans/S36-semantic-qc.md)）落在 30 行上，一条测试断言（[S38:456](../../plans/S38-subtitles.md)）落在 0 行上，A3/A4/A5 里 A4 根本不在 99 行内。分子分母口径不同，相减无意义。这类"条目数减行数"的错误和 §8.1 第 1 处的"减法漂移"是同源 |
| 7 | §9.3 「其余 9 个族 / 62 条」 | 9 个族、**62** 条 | **10 个族、65 条**（97 − 32） | 两个错各自独立：族数少算 1（13 个非空族 − 3 个可按族拍定 = 10，原来写成 9），条数则既与族数不自洽（62 ≠ 97 − 32 = 65），也和 §9.3 下一段自己写的"另外 10 个族（合计 65 条）"直接冲突——同一节里两个数并存，和 §8.1 第 1 处是同一种毛病。已在表下补上 65 的逐族加总，以后可以逐行验 |
| 8 | §9.3 末段「正文完全没提」的族清单 | 列了 `costume`/`prop`/`anatomy`/`hands`/`action_adherence`/`camera_adherence`，末尾接「之外的几个」 | 恰好 **6 个族**，没有"之外的"；且 `camera_adherence` 不能出现在这里 | 这句话把 `camera_adherence` 同时写进了两个互斥集合——上一段刚说它"可以按族拍定（3 条）"，末段又说它"正文完全没提"。它确实没提，但"没提"不影响它能否拍定（候选值 `shot` 无争议）。真正既没提、又要逐族拍的 6 个族是 `anatomy`/`hands`/`costume`/`prop`/`action_adherence`/`camera_adherence`，"之外的几个"是一句没算过的收尾。已删掉该短语 |
| 9 | §9.3 表下「65 的逐族加总」 | 「identity 3 + anatomy 2 + hands 3 + costume 3 + prop 2 + continuity 8 + action_adherence 3 + temporal 7 + voice 10 + technical 23 = 65」 | 这十项相加是 **64**；第 65 条是 [S36:236](../../plans/S36-semantic-qc.md) `VIS.SAFE_AREA.TEXT_OVERLAP`（`defect_type=subtitle`，但查画面） | 第 7 处修完后按承诺"逐行验"，验出加总比被加数少 1。写加总时按"其余 10 个族"取族，而 S36:236 虽是 `subtitle` 类型却不在 S38 那 24 条里，被两边都漏掉——**它正好是 §9.1 #10 行末自己点名的那一条**。已在加总里显式列出。同一处还补完了第 8 处没做完的修改：末段族清单里的 `camera_adherence` 当时只删了收尾短语、没从清单里删掉，与"32 条含 camera_adherence"仍冲突 |
| 10 | `contracts/README.md` 缺口表第 4 行 | 「S35 中 **3 处**动作 / 11 处复检；实为 **1 处** / 10 处」 | **2 处**动作 / 10 处复检 | 转义管道符那个 bug 修对了（3→2），但修的时候两格用了不同口径：复检格把 `—` 算进 10 处，动作格只数了 `remux→transcode`。`TECH.META.MISSING`（[S35:783](../../plans/S35-technical-qc.md)）的 `—` 也是枚举外的值，必须计。口径写死为：**非枚举值一律计数，`—` 也是非枚举值**，两格同进同出。§5.1 正文里的 2 / 10 一直是对的，只有 README 那格错 |
| 11 | §5.1 示意表「`c[6]`（复检列）」 | 实际内容 `regenerate_video` | `实测时长`（`regenerate_video` 在动作格 `c[7]`，且那一格是 `regenerate_video, temporal_edit` 两个动作） | 2026-11-08 自查。写这张示意表时是按"右移一格"推的：判定列含转义管道的 `\|actual − ShotSpec.duration\|` 多切出 2 格，后续每格右移 2 位而不是 1 位，`c[6]` 落到 `实测时长` 而不是 `regenerate_video`。**这段文字本身就是"用推理代替实测"的产物**——它的上一行正是责备别人没回到源表，自己却没把那行按位置打出来。3/11 与 2/10 两个结论不受影响（三个候选值都不在 `recheck_scope` 枚举内），但示意格错了。已把整行 9 格逐格列出。**这一处的第一版更正仍不精确**：它说 `regenerate_video` 是 `c[7]`，仿佛那格就这一个值，实际是 `regenerate_video, temporal_edit`——同一轮里先修"落点错一格"、再修"落点格的取值只写了一半"。 |
| 12 | §4 末句「我全仓 grep 过旧名，目前只出现在 S35 这三行与两条 E24 向量里」 | 只列 S35 三行 + E24-T4/T5 | 还有第三处：`tools/check_all.py` 的 `KNOWN_BAD_CHECK_IDS` 把这三个 id 按名允许（`check_check_ids` 跳过它们），并带一条反向断言——一旦改名落地、这三个 id 不再以两段式出现，同一个 `check_check_ids` 会把这条允许也判为失败。这句是**写好时真、后来变假**的：它随 `4254bdb`（2026-11-06 02:15）进 main 时 `tools/check_all.py` 还不存在（`git cat-file -e 4254bdb:tools/check_all.py` 报 ABSENT），`889d31c` / `9f90c2e`（2026-11-06 04:40 / 04:42）才落地，而 9f90c2e 的提交说明整段就在讲这三个 id | 两类错因叠加：(a) 声明"我全仓 grep 过"却没有把 `tools/` 放进搜索面——`.md` / `.json` / `.py` 里我漏了 `.py`；(b) 结论写成"目前"却是过去时判断，之后没人回头复核。**这一处的实际影响比字面大**：改名提案若不提 `check_all.py`，照办的人改完 S35 三行 + 两条 E24 向量后 `check_all.py` 会立刻判失败，而它报的是"这条允许已不再命中、请删除"，读起来像工具自己的问题。已在 §4 末句点名该文件与 `§8.1` 第 12 处 |
| 13 | §8.1 第 12 处的「写下时」提交号 | `717e7c1`（2026-11-05 10:16） | `4254bdb`（2026-11-06 02:15） | 2026-11-08 写第 12 处时用 `git log -S` 取"最早引入这句话的提交"，拿到 `717e7c1` 就填了，没问它是否在 main 上。它只在 `claude/check-registry-data` 分支：`git merge-base --is-ancestor 717e7c1 HEAD` 为否，`git branch --contains 717e7c1` 只列出那条分支。main 上首次出现这句的是 `4254bdb`。**结论（写好时真、后来变假）不受影响**——`4254bdb` 时 `tools/check_all.py` 同样 ABSENT，两个提交相隔 16 小时而 check_all.py 都在其后。但"哪个提交"是这个更正表唯一的可核锚点，填一个不在 main 上的提交，读者按 main 去查会查不到 |
| 14 | §10.1 第 3 条末句「A3/A4 写了 `human_review` 也不能直接登记：那三处…」 | 只点了 `A3/A4` 两条，却写「那三处」 | **A3/A4/A5** 三条 | 2026-11-08 第四次复核。§10.2 的表把 `human_review` 写在 A3、A4、A5 三行（A5 是 `S38:241`「可能触发 `human_review`」），§0、§10.7、§10.2 结论段也都写 A3–A5 三条，只有这一句漏了 A5。同句前半「A3/A4」与后半「三处」自相矛盾 |
| 15 | §9.0 末段「剩下 **11 个族**可以按"复检成本"排优先级」 | 11 个族 | **12 个族** | 2026-11-08 第四次复核。94 条 = 97 − `identity` 3，分布在 13 个非空族里除 `identity` 外的全部 12 族上；同节 §9.4（第 673 行）自己写的是「其余 **12 个族**的正文…」。11 与 12 同节并存，和 §8.1 第 1、7 处是同一种"两个值并存"的毛病。注：`audio_mix` 是 0 条，所以这里既不能写成 13，也不能只数"有 check 的族"再漏一个 |

**复核确认为正确、未改动的数字**（同样逐条复算过，作为对照组）：

| 数字 | 值 | 复算方式 |
|---|---|---|
| check 总数 | 99 | 23+32+20+24，按 `^\|\s*`(TECH\|VIS\|AUD\|SUB)\.` 逐文件数 |
| `defect_type` 可提取 | 97/99 | 2 条 `type=—`（S36:244、S37:78） |
| `allowed_actions` 可提取 | 21/99 | S35 23 行 − `remux→transcode`（777）− `—`（783） |
| `default_action` 可机械得出 | 14/99 | `allowed_actions` 恰好 1 个成员的行数；14 条全在 S35 |
| `recheck_scope` 枚举内 | 13/99 | S35 23 行 − 10 条自由文本/`—` |
| S35 多动作 check | 7 条 | 761 / 763 / 765 / 768 / 773 / 774 / 776 |
| S35 `recheck_scope` 合规 | 13/23，其余 10 条 = 9 条自由文本 + 1 条 `—` | 自由文本是 773/774/775/777/778/779/780/781/782 |
| `severity_default` | 87 无歧义 + 8 带条件 + 1 二值 + 3 空 | 见 §11.2 的逐条表 |
| 三段式正则计数 | 96 | 99 − 3 条两段式 id |

**错因的共性**：前三处都是"用减法推出来的数"（99−X），没有回到源表重新数。
第 14、15 处是另一种：**计数与标签在同一句里打架**（点了两条写三处；族数 11 与 12
同节并存），两个值都像是"数过"的，但至少有一个没回表——和第 1、7 处同源。
凡是从另一个数字减出来的，都可能有这种漂移。这次重算全部改为直接从表格计数，不复用中间结果。
第 5 处是另一种常见漂移：**正则只覆盖了写过的那一种字面形式**（`连续 3 窗口`），
没覆盖同义写法（`连续 ≥2 窗口`）。凡是用 grep 下的"只有 N 处"结论，都要反过来问
"这个词还有别的写法吗"。

---

## 9. `window_scope` 按 `defect_type` 分族的候选区间

### 9.0 一个能降低决策风险的事实，请先看

`schema` 说 `window_scope` 干两件事：决定 `keyframe_edit` 是否可用、决定复检范围
（[S35:164](../../plans/S35-technical-qc.md)、[S39:99](../../plans/S39-repair-planning.md)）。
但**只有 `identity` 族的链里含 `keyframe_edit`**：

| 族 | 链 | 含 `keyframe_edit`？ |
|---|---|---|
| `identity` | `keyframe_edit` → `temporal_edit` → `regenerate_video` | **是** |
| `technical` | `reingest` → `transcode` → `remix` → `temporal_edit` → `regenerate_video` | 否 |
| `anatomy` / `hands` / `temporal` | `temporal_edit` → `regenerate_video` | 否 |
| `costume` / `prop` / `continuity` / `camera_adherence` / `action_adherence` | `regenerate_video` → `change_spec` | 否 |
| `subtitle` | `realign_subtitles` → `re_render_subtitles` | 否 |
| `lipsync` | `redo_lipsync`（单成员） | 否 |
| `voice` | `re_synthesize_speech` → `regenerate_video` | 否 |
| `audio_mix` | `remix` → `regenerate_video` | 否 |

（链与兜底以 `contracts/quality/action-families.v2.json` 为准，已逐族核对。）

**所以 README 点名的那种事故——"把 20 帧的连续窗口交给逐帧修补"——在路由上只可能发生在
`identity` 的 3 条 check 上。** 其余 **94 条**即使把 `window_scope` 填错，`keyframe_edit`
也不在它的族里，路由不会因此变激进；错的只是复检范围（多花钱或少查一截）。

这不是说其余 94 条可以随便填——复检范围错了同样要付代价。但这说明**两边的时间价值不一样**：
`identity` 那 3 条必须先对，剩下 12 个族可以按"复检成本"排优先级。

另外提醒：`window_scope ∈ {frame, window}` 时 `window_max_frames` 变成必填
（schema `allOf[1]`），所以每选一次 `frame`/`window` 都同时要 owner 给一个帧数上限。
这一项在 99 条里**没有任何一处**有现成数字。

### 9.1 分组总表

**覆盖 97 条**（99 条减去 2 条非缺陷记录：[S36:244](../../plans/S36-semantic-qc.md) /
[S37:78](../../plans/S37-audio-alignment.md)；这两条本就不产生 defect，不登记 `window_scope`）。

| # | `defect_type` | 条数 | 族内可用的 `window_scope` 候选 | 正文对"修复范围"说过什么 | 仍需 owner 拍的唯一原因 |
|---|---|---|---|---|---|
| 1 | `identity` | 3 | `window` / `shot` / `track`（**只有这一族 `keyframe_edit` 真可用**） | [S36:186](../../plans/S36-semantic-qc.md)「窗口内人脸嵌入…」、[S36:187](../../plans/S36-semantic-qc.md)「同一 bbox 轨道的…在窗口间跳变」、[S35:124](../../plans/S35-technical-qc.md) 族表「连续窗口**禁止** `keyframe_edit`」 | 两句说的都是**检测**窗口；[S35:124](../../plans/S35-technical-qc.md) 只说了"连续窗口不能逐帧修"，没说"哪几条 check 属于连续窗口"。要拍的是：这 3 条的缺陷天然是单窗口还是跨窗口 |
| 2 | `anatomy` | 2 | `window` / `shot` | 无。[S35:125](../../plans/S35-technical-qc.md) 族表「无时序能力时直接回退，**不得逐帧重绘**」 | 族表只排除 `frame`，`window` 与 `shot` 都合法。判定列（[S36:197](../../plans/S36-semantic-qc.md)「四肢/手指数量异常」/[S36:198](../../plans/S36-semantic-qc.md)「关节反向/扭曲」）描述的是单帧现象，但修复要动的范围与它无关 |
| 3 | `hands` | 3 | `window` / `shot` | [S36:199](../../plans/S36-semantic-qc.md)「手部**特写窗口**手指结构崩坏」 | 同 #2，且"特写窗口"是检测窗口 |
| 4 | `costume` | 3 | `shot` / `track` / `all` | 无 | 服装是角色属性，跨镜一致；但"修一条"的粒度可能是单镜。三条 check 的判断依据都是 `ShotSpec` binding（[S36:189-191](../../plans/S36-semantic-qc.md)），binding 的粒度是镜还是场，方案没写 |
| 5 | `prop` | 2 | `shot` / `track` / `all` | 无 | 同 #4。判定依据是 S17 `expected_state_set`（[S36:211](../../plans/S36-semantic-qc.md)），同一道具的状态可能跨若干镜 |
| 6 | `continuity` | 8 | `shot` / `track` / `all`（**没有值能干净表达"两个镜头"**，见 §9.2） | [S36:213](../../plans/S36-semantic-qc.md)「同场景**前后镜头**背景结构突变」、[S36:233](../../plans/S36-semantic-qc.md)「与**前一镜**的接续关系…断裂」 | 连续性问题天生跨 2 个镜头，而枚举里的 `shot` 是单镜、`all` 是全片。要么接受"用 `all` 表达跨镜"带来的复检放大，要么修改枚举语义 |
| 7 | `camera_adherence` | 3 | `shot` | 无 | 景别/角度/运镜都是镜头级属性，候选值本身没争议；但这 3 条的修复动作是 `regenerate_video` 或 `change_spec`，而 `change_spec` 改的是**分镜**，影响面超过单镜 |
| 8 | `action_adherence` | 3 | `shot` / `window` | 无。[S36:224](../../plans/S36-semantic-qc.md)「节拍发生时间与分镜节拍表偏差 > 0.5s」 | "节拍"可能短于一个镜头（一个镜头有多个 beat）。0.5s 是**检测**容差，不是修复范围 |
| 9 | `temporal` | 7 | `window` / `shot` | [S36:230](../../plans/S36-semantic-qc.md)「亮度/色彩**逐帧**跳变」、[S36:231](../../plans/S36-semantic-qc.md)「**相邻窗口间**非物理形变」、[S36:232](../../plans/S36-semantic-qc.md)「切点前后 **3 帧**内…突变」；S37 侧 4 条（[S37:86-90](../../plans/S37-audio-alignment.md)）无窗口描述 | 这个族**混了两个服务两种语义的 check**：S36 的 3 条是画面时序，S37 的 4 条是音频对齐时间。一个 `window_scope` 值不可能同时讲对两件事。按族拍之前要先决定是否拆组 |
| 10 | `subtitle` | 25 | `track` / `all`（**`frame`/`window` 结构性不适用**） | [S38:339-343](../../plans/S38-subtitles.md)「**字幕没有帧/窗口概念**」+ `planned_windows = cue 总数`、`temporal_windows_evaluated = 已校验 cue 数` | 这是 14 个族里**唯一有结构性证据**的：字幕的载体是 cue 序列，不是帧。但 `track` 与 `all` 仍要选（`all` 会连带复检整份交付物）。注意这 25 条里有 1 条在 S36（[S36:236](../../plans/S36-semantic-qc.md) `VIS.SAFE_AREA.TEXT_OVERLAP`），它的载体是**画面**，不是字幕轨——它跟这 24 条同 type 但不同物 |
| 11 | `lipsync` | 5 | `shot` | [S37:104](../../plans/S37-audio-alignment.md)「`planned_windows` = **对白区间的帧数**，`coverage_ratio` 按帧算」 | 那句话是覆盖率口径（§1.3 已排除）。族只有 `redo_lipsync` 一个成员、且 `keyframe_edit` 不在族里，所以 `window_scope` **对路由完全没有影响**，只影响复检。这条的争议最小 |
| 12 | `voice` | 10 | `window` / `shot` / `track` | [S37:110](../../plans/S37-audio-alignment.md)「连续 3 **窗口**命中才升级」、[S37:111](../../plans/S37-audio-alignment.md)「同一角色在不同**镜头**音色显著不同（跨镜）」 | 两条互相矛盾：一条按窗口算，一条按镜头算。另外 S37 的幂等键是 per-utterance（[S37:351](../../plans/S37-audio-alignment.md) `qc:s37:{audio_track_hash}:{utterance_id}:…`），说明修复的真实单位是**一句台词**，而枚举里没有 utterance 这一档 |
| 13 | `technical` | 23 | `frame` / `window` / `shot` / `all`（**四种都有理由**） | 复检列（[S35:759](../../plans/S35-technical-qc.md) 列头）写了 `连续窗口 ±0.5s`(773/774)、`窗口 ±0.5s`(775)、`窗口`(778)、`全轨`(779/780/781)、`全片抽检`(782)；[S35:748](../../plans/S35-technical-qc.md)「帧级检查（黑帧/静帧/闪白）**全帧解码**」 | 这 23 条横跨容器/文件/解码/时长/分辨率/帧率/编码/色彩/音频/响度/烧录字幕/元数据，**根本不是一个"族"该有的粒度**。复检列的用词是 §1.3 明确排除的素材（复检≠修复），但它至少说明连作者自己都认为是五种不同的范围 |
| 14 | `audio_mix` | **0** | — | [S35:138](../../plans/S35-technical-qc.md)「`audio_mix` 目前没有检查项指向它——闭集不能缩，保留给后续音频层使用」 | 不需要拍，但**要注意别在统计时把它算成"缺 0 条"以外的数**：14 个 type 里它恒为 0 |
| — | 非缺陷记录 | 2 | 不适用 | — | 不产生 defect，不登记 |

合计：13 个非空 type 覆盖 **97 条**；`audio_mix` 0 条；非缺陷 2 条。

**"候选"这一列的口径**：schema 对 `window_scope` **没有任何族约束**——五个值都合法，
validator 也不会拒（`validate_registry.py` 只查 `severity_default ≤ severity_max` 与族链）。
候选值是按"这一族的缺陷载体天然是什么粒度"列的**建议范围**，不是 schema 允许的范围。
owner 完全可以选一个我没列的值；我列的是我认为说得通的。

**另一列的口径**：`frame`/`window` 一旦选中，`window_max_frames` 立刻变成必填
（schema `allOf[1]`）。所以候选里凡出现这两个值的族，owner 每拍一条就要多给一个帧数。

### 9.2 三个必须先解决的结构问题（不解决，分组拍了也会填错）

分组只能把 99 个问题压成 14 个，压不到"填表"的程度。原因是有三个问题**在枚举层面就没有答案**，
不是数据缺失：

**(a) `track` 对视频层的语义未定义。** schema 对 `window_scope` 只有一句
"How much of the artifact the repair touches"。对字幕，`track` 唯一指向"字幕轨"；
对视频 artifacts，`track` 可能是视频轨、音频轨、或一条 SSE 轨道。`continuity` / `costume` /
`prop` / `voice` 这 4 个族（共 23 条）如果要用 `track`，得先定义它指什么。
**这是 schema 措辞问题，不是填值问题。**

**(b) 枚举缺"两个镜头"这一档。** `continuity` 的 8 条里有 4 条明确跨镜
（[S36:213](../../plans/S36-semantic-qc.md) `VIS.SPACE.BACKGROUND_JUMP`「同场景前后镜头」
、[S36:233](../../plans/S36-semantic-qc.md) `VIS.CONTINUITY.SHOT_BOUNDARY`「与前一镜的接续关系」
、[S36:234](../../plans/S36-semantic-qc.md) `VIS.CONTINUITY.AXIS_CROSSING`「越轴」（越轴必须相对前一镜才成立）
、[S36:235](../../plans/S36-semantic-qc.md) `VIS.CONTINUITY.LIGHT_DIRECTION`「与前一镜不符」）。
`shot` 是单镜，`all` 是全片，中间没有"相邻两镜"。
现在只能二选一：用 `all` 换正确性（复检放大到全片），或用 `shot` 换成本（跨镜缺陷修不全）。

**(c) 同一 `defect_type` 下混了不同粒度的 check。** `temporal`（7 条）横跨 S36 的画面时序与
S37 的音频对齐；`technical`（23 条）横跨从"文件解不开"到"响度差 2 LU"。按 type 分组的前提是
"同族粒度相近"，这两个族不满足。若要按组拍，得先决定是否在 type 内再分子组
（例如按 S35 §2 的 `CONTAINER`/`FILE`/`AUDIO`/`LOUDNESS` 第二段分组）。

### 9.3 如果按分组推，会得到什么（供 owner 参考，不是结论）

把 14 个族的候选值收窄成一张"如果同族一致"的表，会是这样：

| 若同族取值一致 | 覆盖条数 | 需要 owner 额外给的 |
|---|---|---|
| `subtitle` = `track` | 24（S38） | 无（[S38:339](../../plans/S38-subtitles.md) 是结构性证据）；但 [S36:236](../../plans/S36-semantic-qc.md) 那 1 条要单独看，它查的是画面 |
| `subtitle` = `all` | 24 | 同上，且复检面更大 |
| `lipsync` = `shot` | 5 | 无（族单成员，路由不受影响） |
| `camera_adherence` = `shot` | 3 | 是否接受 `change_spec` 带来的跨镜影响 |
| `identity` = `window` | 3 | **每条的 `window_max_frames`**（`frame`/`window` 必填） |
| 其余 10 个族 + 1 条跨服务 `subtitle` | 65 | 都还要逐族拍，且 `temporal`/`technical` 内部还要再分组 |

**能"按族一次拍定"的只有 32 条**：subtitle 24 + lipsync 5 + camera_adherence 3
（camera 那条还要先接受 `change_spec` 的跨镜影响）。**剩下 65 条必须逐族甚至逐条拍**，
其中 `identity` 的 3 条虽然候选值只有一个（`window`），但每条都还要另给 `window_max_frames`。

（上表"其余 10 个族 + 1 条 `subtitle` 65 条"就等于 97 − 32。逐项相加：
identity 3 + anatomy 2 + hands 3 + costume 3 + prop 2 + continuity 8 + action_adherence 3
+ temporal 7 + voice 10 + technical 23 **= 64**，再加跨服务的那 1 条
[S36:236](../../plans/S36-semantic-qc.md) `VIS.SAFE_AREA.TEXT_OVERLAP` 才等于 65。
它 `defect_type` 是 `subtitle`，但查的是画面，不能跟着 S38 的 24 条一起拍 ——
这正是 §9.1 #10 那一行的最后一句。不要把它折回 `subtitle` 计数里。）

这是本节能给出的最强结论：**分组不是捷径。** 它有 checks 的 13 个族里，
只有 3 个族可以真正按"同族一致"拍（合计 32 条：`subtitle` 24 + `lipsync` 5 + `camera_adherence` 3）；
另外 10 个族（合计 64 条）加那条跨服务的 `subtitle`（1 条）共 65 条
要么族内混了不同粒度（`temporal`、`technical`），要么跨镜语义枚举表达不了（`continuity`），
要么正文对"修复范围"完全没提（`anatomy`、`hands`、`costume`、`prop`、`action_adherence`）。
**把 99 条压成 14 个问题是真的；但 14 个问题里 10 个并不比原来简单。**

### 9.4 为什么不能由我代填

§1.3 的论证在这里全部适用，而且被这次分族检查**加强**了：13 个非空族里，**只有 `subtitle`
有指向修复范围的结构性证据**（[S38:339](../../plans/S38-subtitles.md)）。其余 12 个族的正文
要么只谈检测窗口（S36 §4 采样表、S37 `planned_windows`），要么只谈复检范围（S35 复检列），
要么完全不提窗口。**没有一族能用"同族通常一致"的理由填值**——
"通常"正是 §1.1 排除的类推。

---

## 10. `escalation_rule` 的散文盘点

### 10.1 判定口径

`schema` 的 `escalation_rule` 是一个四字段对象（`additionalProperties: false`）：

```json
{ "metric": "<string>", "comparator": "lt|lte|gt|gte|eq|ne",
  "threshold": <number>, "escalate_to": "<12 值动作枚举>" }
```

注意三点，后面每一条都用得到：

1. `threshold` 的类型是 **`number`**，不是表达式。像"cps > profile 上限"这种**阈值本身是变量**的，
   写不进去（[S38:292](../../plans/S38-subtitles.md) 的判据就是这种）。
2. `escalate_to` 必须是**动作**，不是 decision（`pass`/`repair`/`human_review`/`abstain` 里只有
   `human_review` 同时是动作枚举成员）。"转 abstain"写不进去。
3. `escalate_to` **必须在该 `defect_type` 的族链里**（`x-cross-checks` 第 4 条，
   `validate_registry.py:70-76` 强制），而 `human_review` **不在任何一个族的链里**
   （14 个族逐一核对，命中 0）。它是兜底列的值，不是链成员
   （[S35:119](../../plans/S35-technical-qc.md)「`human_review` **不出现在任何族里**」）。

   **这一条比看起来更硬**：它意味着**任何一条 check 都不可能把 `escalate_to` 登记成
   `human_review`**——validator 会直接拒载。所以"超阈值就转人审"这个最自然的升级语义，
   在现行 schema 下**根本无法表达**。这也是为什么 §10.2 的五条里，
   A3/A4/A5 写了 `human_review` 也不能直接登记：那三处的 `human_review` 是族兜底，
   不是 `escalation_rule.escalate_to`。

### 10.2 真正描述"动作升级"的散文：5 条

| # | 出处 | 原文 | `metric` | `comparator` | `threshold` | `escalate_to` | 缺几个 |
|---|---|---|---|---|---|---|---|
| A1 | [S37:110](../../plans/S37-audio-alignment.md) `AUD.VOICE.EMBEDDING_DRIFT` | 「与 S15 binding 的音色 embedding 距离 > τ；**连续 3 窗口命中才升级**」 | ✓ `consecutive_windows` | ✓ `gte` | ✓ `3` | **✗** | **1** |
| A2 | [S36:174](../../plans/S36-semantic-qc.md) 族级（全 `VIS.*`） | 「单窗口孤立命中不直接升级为 blocker，需要**连续 ≥2 窗口**命中才升级」 | ✓ `consecutive_windows` | ✓ `gte` | ✓ `2` | **✗**（且"升级为 blocker"是严重度不是动作，见 10.3） | **1** |
| A3 | [S38:274-278](../../plans/S38-subtitles.md) CPS 超限 | 「1. 重新切分…2. 延长 cue 到 `max_duration`…3. **仍超 → … 推荐动作 `human_review`**」 | **✗**（"仍超"指什么变量没写死） | **✗** | **✗**（真实阈值是 `locale_profile.max_cps`，变量不是数字） | **✗ 不可登记**（`human_review` 不在 `subtitle` 链里，validator 会拒；只能登记成 `realign_subtitles` 或 `re_render_subtitles`） | **4** |
| A4 | [S38:456](../../plans/S38-subtitles.md) `E24-U4` | 「先切分；**仍不行 → `SUB.LAYOUT.LINE_OVERFLOW` + human_review**」 | **✗** | **✗** | **✗** | **✗ 不可登记**（同上） | **4** |
| A5 | [S38:241](../../plans/S38-subtitles.md) EDL 变速 | 「`speed_ratio ≠ 1` 的 clip 会挤压对白 → 需要重新检查 `SUB.TIMING.CPS` 与 `SUB.TIMING.OVERLAP`，**可能触发 `human_review`**」 | **✗** | **✗** | **✗** | **✗** | **4** |

**结论：5 条动作升级散文里，只有 2 条带可写的数值阈值（A1、A2），四字段齐全的是 0 条。**

上一版正文写的"只有 1 条"，漏掉了 A2。而且这 5 条里 **3 条（A3/A4/A5）即使 owner 肯给
metric/comparator/threshold 也填不了**——它们要跳的目标是 `human_review`，
而 `human_review` 不在任何族链里（§10.1 第 3 条）。**这是 A3–A5 与 A1–A2 的本质区别：
A1/A2 缺的是 owner 的一个决定（跳哪个动作）；A3–A5 缺的是 schema 的表达能力。**

### 10.3 形似而实非：7 条"条件严重度"（不是 `escalation_rule`）

这 8 条带条件的严重度里，有 7 条描述的是**同一 check 在不同场景下的 severity 不同**
（另 1 条是二值，见 10.4）。它们看着像"超过 X 就升级"，但升级的是**严重度**，
而 `escalation_rule.escalate_to` 要的是**动作**。这一维度 schema 没有字段装。

| 出处 | 原文 | 缺什么才能进 registry |
|---|---|---|
| [S35:773](../../plans/S35-technical-qc.md) | `major（头尾>阈值）` | 没有"头尾在片头还是片中"这一输入能表达为 `metric`；且 `threshold` 未给数字（"阈值"是 `black_min_frames` 的兄弟参数，未命名） |
| [S35:776](../../plans/S35-technical-qc.md) | `blocker（有对白镜头）` | 条件是一个布尔（ShotSpec 有对白），不是数值比较 |
| [S35:779](../../plans/S35-technical-qc.md) | `major（master 时 blocker）` | 条件是交付阶段（`master` / 非 master），不是数值 |
| [S35:780](../../plans/S35-technical-qc.md) | `blocker（master）` | 同上 |
| [S36:234](../../plans/S36-semantic-qc.md) | `blocker（对白场）` | 条件是场景类型，不是数值 |
| [S37:76](../../plans/S37-audio-alignment.md) | `blocker（关键台词）` | 条件是台词标记，不是数值 |
| [S38:313](../../plans/S38-subtitles.md) / [S38:314](../../plans/S38-subtitles.md) | `blocker（正式交付）` | 条件是交付阶段，不是数值 |

**这 7 条需要的不是 `escalation_rule`，是一个 schema 里还不存在的"条件严重度"表达**
（例如 `severity_rules: [{when: {...}, severity: "blocker"}]`）。**这是契约缺口，记在这里备查，
不在本次范围内改。**

### 10.4 二值严重度：1 条

[S37:98](../../plans/S37-audio-alignment.md) `AUD.LIPSYNC.OFFSET`：
`**blocker**（>120ms）/ major（>60ms）`。两个阈值、两档严重度、一个标量字段装不下。

它的形状**恰好可以拆给 `severity_default` / `severity_max` + `escalation_rule`**：

```text
severity_default = "major"        # 60ms 起
severity_max     = "blocker"      # 可升到 blocker
escalation_rule  = {metric: "lipsync_offset_ms", comparator: "gt", threshold: 120, escalate_to: "？"}
```

前两个是映射，第三个的 `escalate_to` 仍然缺——`lipsync` 族只有 `redo_lipsync` 一个成员，
`human_review` 是兜底不在链里（`validate_registry.py` 会拒）。
**所以这条也不是"填一下就好"，它需要一个动作层面的决定**：
>120ms 时是"仍然 `redo_lipsync`、只是标 blocker"，还是"直接人审"？前者根本不需要
`escalation_rule`（只需要两档严重度），后者需要一个 schema 允许的 `human_review` 跳转。

### 10.5 形似而实非：报告级 policy（5 条，不能抄进 registry）

| 出处 | 原文 | 为什么不是 `escalation_rule` |
|---|---|---|
| [S35:667-668](../../plans/S35-technical-qc.md) | `coverage_ratio < min_coverage_ratio`（0.90）→ `abstain` | 报告级；`escalate_to` 不能是 `abstain` |
| [S35:678](../../plans/S35-technical-qc.md) | `confidence < tau_low`（0.40）→ `human_review` | 报告级；阈值由 S02 按项目下发，写死在 registry 会锁死 |
| [S35:687](../../plans/S35-technical-qc.md) | `report.confidence < tau_abstain`（0.35）→ `abstain` | 同上 |
| [S35:684](../../plans/S35-technical-qc.md) / [S36:262](../../plans/S36-semantic-qc.md) | `disagreement_rate > tau_disagreement`（0.30）→ `human_review` | 同上；且 S36:265 明说"0.30 是起点，需在 evals 里回归校准"——它**本来就是要变的值** |
| [S35:700](../../plans/S35-technical-qc.md) | 同 `tau_disagreement` | 同上 |

这 5 条是 `decide()` 的输入，走 `policy_version`，不是 per-check 数据。

### 10.6 形似而实非：运行告警与硬覆盖（3 条）

| 出处 | 原文 | 为什么不是 |
|---|---|---|
| [S35:957](../../plans/S35-technical-qc.md) | 「`abstain_ratio > 5%` 持续 15 分钟 → 评估器/沙箱退化，P2」 | 运维告警，对象是**服务**不是 check；"持续 15 分钟"在 schema 里也无字段 |
| [S37:133](../../plans/S37-audio-alignment.md) | 「被跳过的 token 超过 `max_unjudged_token_ratio`（默认 10%），该 check 整体 `failed` → abstain」 | check 级、有 `metric`（skipped_token_ratio）/`comparator`（`gt`）/`threshold`（0.1），但 `escalate_to` 应是 `abstain`，**不在动作枚举里**。它在 `decide()` 的第 3 步生效（[S35:671](../../plans/S35-technical-qc.md)），不是升级 |
| [S35:963](../../plans/S35-technical-qc.md) | 「`TECH.CONTAINER.PARSE` / `TECH.DECODE.PACKET_ERROR` 命中后…S39 只允许 `reingest`」 | 这是**硬覆盖**（收窄到单成员），与 `escalation_rule` 的"达阈值往上跳"方向相反。它落点是 `allowed_actions = [reingest]`，不是 `escalation_rule` |

### 10.7 盘点合计

| 类别 | 条数 | 能否进 registry |
|---|---|---|
| A. 动作升级散文 | **5** | 0 条四字段齐全。A1/A2 缺 1 个字段（owner 可拍）；**A3–A5 缺 4 个，其中 `escalate_to` 是 schema 表达不了** |
| B. 条件严重度 | 7 | 否——需要 schema 新字段 |
| C. 二值严重度 | 1 | 部分（严重度可映射，`escalate_to` 仍需决定） |
| D. 报告级 policy | 5 | 否——走 `policy_version` |
| E. 运行告警 / 硬覆盖 | 3 | 否（1 条落点是 `allowed_actions`） |
| F. 其余 check | **55** | **散文里一个字都没有** |

**F 行的 55 按"落在哪一条 check 上"重新对账**。首先要说清 A–E 五行的计数单位并不统一：
B（7）和 C（1）数的是**表行**；A（5）、D（5）、E（3）数的是**散文条目**——其中 D 的 5 条是报告级
policy，根本不落在任何一条 check 上；E 的 [S35:957](../../plans/S35-technical-qc.md) 是服务级告警，
也不落行。**单位不同的两组数不能相减**，这正是上一版写成 94 的错因。

| 落在 99 行上的覆盖 | 行数 | 明细 |
|---|---|---|
| 点名了具体 check 的散文 | **14** | S35 6（[761](../../plans/S35-technical-qc.md)/[764](../../plans/S35-technical-qc.md) 硬覆盖，[773](../../plans/S35-technical-qc.md)/[776](../../plans/S35-technical-qc.md)/[779](../../plans/S35-technical-qc.md)/[780](../../plans/S35-technical-qc.md) 条件严重度）· S36 1（[234](../../plans/S36-semantic-qc.md)）· S37 3（[76](../../plans/S37-audio-alignment.md)/[98](../../plans/S37-audio-alignment.md)/[110](../../plans/S37-audio-alignment.md)）· S38 4（[288](../../plans/S38-subtitles.md)/[292](../../plans/S38-subtitles.md)/[313](../../plans/S38-subtitles.md)/[314](../../plans/S38-subtitles.md)） |
| 仅被**族级**散文覆盖 | **30** | [S36:174](../../plans/S36-semantic-qc.md)「连续 ≥2 窗口才升级」覆盖全部 `VIS.*`；32 行里扣掉 1 行点名（234）和 1 行非缺陷（244），得 30 |
| 完全无散文 | **55** | 99 − 14 − 30 |
| 合计 | **99** | |

逐文件：S35 6 点名 / 17 无；S36 1 点名 / 30 族级 / 1 无；S37 3 点名 / 17 无；S38 4 点名 / 20 无。

三处口径要说明：

- **A3/A5 的归属**：[S38:278](../../plans/S38-subtitles.md) 点名的是 `CPS_EXCEEDED`（行 292），
  [S38:241](../../plans/S38-subtitles.md) 点名 `CPS` 与 `OVERLAP`（行 292、288）。后者是这 14 行里
  最松的一处归属——它说的是"EDL 变速后要重查这两项"，而不是"这两项超限就升级"。
- **A4（[S38:456](../../plans/S38-subtitles.md) `E24-U4`）不占 99 里任何一行**：它是测试用例的断言，
  描述的是修复动作耗尽后的走向，不是某条 check 的登记数据。所以"5 条散文"与"14 行"不是同一批。
- **A1 的归属**：[S37:110](../../plans/S37-audio-alignment.md) 是 `AUD.VOICE.EMBEDDING_DRIFT` 表行本身，
  既属 A 类也属这 14 行。

`escalation_rule` 的结论不依赖这张对账表，依赖的是 §10.2 的 5 条与 §10.1 的"0/99"。

**回到 §0 的那行**：`escalation_rule` 可提取 = **0 / 99**。
最接近的是 A1、A2 两条，"连续 3 窗口"这个数字经核实**是真的**（而且还有第二条"连续 ≥2 窗口"），
但两条都卡在 `escalate_to` 上——而 `escalate_to` 恰恰是最不能猜的那个：
它决定超阈值之后花多少钱、动多大范围。

**一个比"缺字段"更坏的消息**：即使 owner 愿意拍全部四个字段，只要目标是"转人审"，
就**拍不了**——`human_review` 不在任何族链里，validator 会拒（§10.1 第 3 条）。
`escalation_rule` 现行设计只能表达"往上跳一个更贵的自动动作"，表达不了"超阈值就不自动修"。
**这一条需要 owner 在 schema 层面决定，不是数据层面。**

---

## 11. `severity_max` 缺省规则：供 owner 签字的条文

### 11.1 待签条文（全文，可直接签字或否决）

> **条文 S-M1（`severity_max` 缺省）**
>
> 对于方案表格中"严重度"列**只给出一个无修饰值**的 check，其登记项取
> `severity_max := severity_default`，其中 `severity_default` 就是该列的值。
>
> 该规则**只做复制，不新增判断**：它不改变任何一条 check 的严重度取值，
> 只把"上限等于登记值"这一既有缺省行为写进数据。
>
> 该规则不适用于"严重度"列带条件前缀、带多个值、或为空的 check（共 12 条，见 §11.3），
> 这些 check 各自处置。

签字位置：`☐ 认可` / `☐ 否决`　　签字人：__________　　日期：__________

### 11.2 它覆盖哪 87 条

| 方案 | 行 | 无修饰严重度 | 说明 |
|---|---|---|---|
| S35 | 761–783 | **19** | 剔除 4 条带条件（773/776/779/780） |
| S36 | 186–244 | **30** | 剔除 1 条带条件（234）+ 1 条空（244） |
| S37 | 73–113 | **16** | 剔除 1 条带条件（76）+ 1 条二值（98）+ 2 条空（78/102） |
| S38 | 288–330 | **22** | 剔除 2 条带条件（313/314） |
| **合计** | | **87** | |

逐条复算方式：把严重度单元格去掉 `*` 后，值恰好等于 `blocker`/`major`/`minor`/`info`
之一的，计 1 条。87 条全部满足。（对应 §8.1 复核表里"`severity_default` 87"那一行，
同一批行。）

### 11.3 它不覆盖哪 12 条

| 类别 | 条数 | 清单 | 需要 owner 做什么 |
|---|---|---|---|
| 带条件前缀 | **8** | [S35:773](../../plans/S35-technical-qc.md) `major（头尾>阈值）`、[S35:776](../../plans/S35-technical-qc.md) `blocker（有对白镜头）`、[S35:779](../../plans/S35-technical-qc.md) `major（master 时 blocker）`、[S35:780](../../plans/S35-technical-qc.md) `blocker（master）`、[S36:234](../../plans/S36-semantic-qc.md) `blocker（对白场）`、[S37:76](../../plans/S37-audio-alignment.md) `blocker（关键台词）`、[S38:313](../../plans/S38-subtitles.md)、[S38:314](../../plans/S38-subtitles.md) `blocker（正式交付）` | 二选一：**(i)** 取括号前的值作为 `severity_default`，`severity_max` 取括号里出现过的最高值（773/779 得 `default=major, max=blocker`；其余 6 条得 `default=max`），括号条件**记进 `notes`，不进路由**；**(ii)** 等 §10.3 的"条件严重度"字段进 schema 再填 |
| 二值 | **1** | [S37:98](../../plans/S37-audio-alignment.md) `blocker（>120ms）/ major（>60ms）` | 见 §10.4。最小方案：`severity_default=major, severity_max=blocker`，阈值留空（`escalation_rule=null`），即"一律按 major 起、允许升 blocker"——**语义上是把 120ms 那条规则降级成提示**，需要 owner 明确接受 |
| 空 | **3** | [S36:244](../../plans/S36-semantic-qc.md) `不存在`、[S37:78](../../plans/S37-audio-alignment.md) `—`、[S37:102](../../plans/S37-audio-alignment.md) `—` | 这 3 行**不产生 defect**（[S36:244](../../plans/S36-semantic-qc.md) 明写"S17 全权负责，此行为'不许留空'的显式记录"；[S37:78](../../plans/S37-audio-alignment.md) "不产生 defect"；[S37:102](../../plans/S37-audio-alignment.md) 是 `not_applicable` 的兜底）。**决策是"要不要登记"，不是"填什么值"。** 见 §11.5 |

8 + 1 + 3 = **12**。87 + 12 = 99 ✓

### 11.4 为什么这条规则站得住（三条独立依据）

**(1) 它不是在发明规则，是在把已有的缺省写进数据。**
[S39:102](../../plans/S39-repair-planning.md) 的 registry 字段表里，`severity_max` 的
"缺省行为"一栏写的就是「**取登记的 `severity_default`**」。也就是说：即使 registry 没写
`severity_max`，S39 的行为已经是这样。S-M1 只是把隐含缺省显式化，**改变的是可审计性，
不是行为**。这一点让这条条文的签字风险显著低于其他任何一次填入。

**(2) 结构上恒合法。** `validate_registry.py:98-101` 只拒绝 `severity_default > severity_max`：

```python
if sd in order and sm in order and order[sd] > order[sm]:
    errors.append(f"...: severity_default {sd!r} exceeds severity_max {sm!r}")
```

相等永远不会被拒。所以 S-M1 填出来的 87 条**不可能因为这条规则被 validator 拒载**——
唯一的拒载来源会是 §4 的 3 条两段式 id，与本节无关。

**(3) 语义上是"最紧的上限"，与 schema 的措辞一致。**
schema:105 说 `severity_max` 是
> "Ceiling a model-backed detector may raise to. Registration decides severity; the detector
> may only raise toward this, never lower it."

取 `max = default` 的含义是"**检测器不得上提**"。这是**最保守**的选择：它把最终严重度完全
交给登记值，模型一个子都升不上去。

**顺带说明为什么这对大部分 check 其实没有副作用**：只有 S36 调模型
（[S36:180](../../plans/S36-semantic-qc.md)「严重度上限由 registry 固定；VLM 只能上提不能下压」）。
S35 明写「只做确定性测量，**不调用任何模型**」（[S35:723](../../plans/S35-technical-qc.md)），
S37 走 ASR/embedding、S38 走纯规则，都不存在"模型上提"的通道。
所以对 S35 的 19 条、S37 的 16 条、S38 的 22 条，`severity_max` 实际上是个**不会被读到的字段**——
填 `= default` 是唯一不产生误导的填法。**真正受 S-M1 约束的只有 S36 的 30 条。**

### 11.5 三个非缺陷行：登记还是不登记

这 3 行不是"严重度缺失"，是"没有缺陷"：

| check | 原文 | 建议（需 owner 确认） |
|---|---|---|
| [S36:244](../../plans/S36-semantic-qc.md) `VIS.KNOWLEDGE.SCOPE_RENDER` | 「知情范围**不由 S36 判**；S17 全权负责。此行为"不许留空"的显式记录」 | **不登记**。它不是 check，是"这一格我们故意留空"的注记。登记它会让 registry 出现一条永不命中的条目，并让 §6 的 `required_for(layer, subject)` 完整性断言（[S35:661](../../plans/S35-technical-qc.md)）把它算成必查项 |
| [S37:78](../../plans/S37-audio-alignment.md) `AUD.TEXT.SCRIPT_CONFLICT` | 「**不产生 defect**：文本冲突不自动改剧本（service.yaml recovery）…只记 info dimension」 | **不登记为 check**；它的 info 已由 `dimensions` 承载 |
| [S37:102](../../plans/S37-audio-alignment.md) `AUD.LIPSYNC.FACE_NOT_FOUND` | 「`not_applicable`，reason=`no_face`」 | **必须登记**。这一条与其他两条不同：它是一条真会触发的 check（没人脸的镜头），只是结论是 `not_applicable`。不登记它，`decide()` 第 1 步会把它判成 `incomplete_coverage`（[S35:661](../../plans/S35-technical-qc.md)），**导致所有无人脸镜头的报告全部 abstain**。`severity_default` 需要 owner 给一个值（`info` 最自然，但仍需确认），`severity_max` 按 S-M1 取同值 |

这一格是 S-M1 **唯一需要额外决定**的地方：不是"填什么"，是"这 3 行里的哪几行算 check"。

### 11.6 如果 owner 否决 S-M1，四个备选

按"决策量 / 风险"排序。**第四个是不可行的，列出来是为了说明没有更省的路。**

| 备选 | 决策量 | 后果 | 评价 |
|---|---|---|---|
| **B1 逐条拍** | 87 次 | 每条都可能与 S39 当前的缺省行为不一致，等于**未经论证地改行为** | 成本最高，且相对 S-M1 没有任何胜出点——除非某条 check 确实需要"允许模型上提" |
| **B2 全填 `blocker`** | 1 次 | 结构合法；但 `severity_max` 从此不再约束任何东西，S39 的门禁（"`blocker` 不可自动接受"）变成模型可决定 | **不建议**。它把唯一的安全上限拆了，而这正是 schema 想防的（schema:105 "Ceiling"） |
| **B3 按 `defect_type` 定上限** | 14 次 | 例如 `technical`/`lipsync`/`subtitle` 等确定性族 `max=default`；模型族（`identity`/`hands`/`continuity`/…）允许 `max=blocker` | **可接受的折中**。但要先回答：凭什么某族允许模型上提、另一族不允许？方案里没有这句话，得 owner 现写一条理由 |
| **B4 不填，留空让 S39 走缺省** | 0 次 | 字段是 **required**（schema:28-32），留空直接 schema 校验失败 | **不可行** |

**我的建议是 S-M1**，理由已列在 §11.4：它不改变行为、结构恒合法、对 68 条确定性 check
是唯一不误导的填法。但**这是 owner 的决定，本清单只把决策面整理干净。**

### 11.7 签字后的下一步（不必现在决定）

S-M1 一旦签字，87 条 `severity_max` 可以机械生成，**不需要任何人工判断**；
生成物仍要过 `validate_registry.py`。剩下 12 条的处置见 §11.3，其中 8 条带条件的
若选方案 (i) 也可机械生成（取括号前值 / 括号内最高值），**实际人工决策只剩**：

- 3 条非缺陷行的登记取舍（§11.5）；
- `AUD.LIPSYNC.OFFSET` 的语义取舍（§10.4）。

**也就是说：S-M1 签字 + 上面两个小决定 = `severity_max` 这一列 99 条全部落定。**
这是本清单里唯一一条"签字即解锁一整列"的路径。
