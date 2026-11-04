# 实现方案统一验收 · 2026-10-30

范围：`plans/` 下 17 份服务实现方案，由 5 个并行会话产出后合并到 `main`。

结论：**13 项通过，1 项冲突已收敛（C1，见 §2），2 项缺口已关闭（G1/G2，见 §3/§4），3 项契约缺口已落（C6/C7/C8，见 §2.4）。** 没有阻断问题。

---

## 1. 自动化检查结果

| 检查 | 方法 | 结果 |
|---|---|---|
| 原始设计包未被改动 | 对 5 个分支逐个 `git diff base..branch -- docs/design-package` | **5/5 零改动** |
| 方案未残留进 `services/` | 扫 `services/**/implementation-plan.md` | **0 处残留** |
| 文件名 ↔ 服务目录 ↔ service_id | 42 项逐一对齐 | **17/17 一致** |
| 内部相对链接 | 解析 67 个链接 | **0 损坏** |
| 服务 ID 引用有效性 | 全量正则扫描对比 catalog 48 项 | **0 越界引用** |
| 幂等键格式 | `tenant_id + command_id + request_hash` | **17/17 一致**，同键异 hash → 409 无歧义 |
| `execution_fence` / `node_epoch` 混用 | 逐文件扫描误用句式 | **0 处混用** |
| defect type 闭集 | 97 个 check 的 `defect_type` 对 14 值闭集 | **97/97 合规** |
| QCReport 规范唯一性 | 检查是否被复述 | **唯一在 S35 §2**，S36/S37/S38 只引用 |
| permit 状态机三方一致 | S22 / S26 / `admission-record.v2.schema.json` | **一致**；S22 的 `EXPIRED` 明确不外露（C22-4 已记录） |
| reservation 状态机 | S23 / 契约枚举 | **一致**；`EXPIRED` 同样不外露（C23-4b 已记录） |
| outbox 覆盖 | 声明 `publishes` 的方案是否含 outbox | **17/17**（S25 已补，见 §3） |
| E01–E40 覆盖 | 全量扫描 | **30/40** 被引用，未覆盖 10 项全部属于尚未写方案的服务 |

未覆盖的 E 编号：E02 三集连续、E06 无人值守失败、E29 恶意媒体/SSRF、E33 Temporal 升级、E35 容量/noisy neighbor、E36 UI 断线、E37 交付、E38 费用重算、E39 审计保留、E40 FDE 移交 —— 对应 S21/S29–S34/S42–S48，属正常待办。

---

## 2. C1（已按方向 1 收敛，2026-11-01）：S39 到底按什么维度做修复路由

### 2.1 冲突原貌

**S35 §2.2 原立场**：S39 按 `type` + `recommended_actions` 决策，**永不按 `check_id`** 决策 —— 这样任一质检服务新增检查项不需要 S39 发版。

**S39 §3.1 原做法**：主映射表第一列就是 check_id 分组（`TECH.CONTAINER.PARSE` / `VIS.IDENTITY.FACE_DRIFT` / `AUD.LIPSYNC.*` …），§3.3 的"不能自动修"清单逐条按 check_id 列举。没有 `type → action` 兜底表，`recommended_actions` 不参与决策。

**为什么两侧都不完整**：

- 照 S39 写 → 新增 check 必须同步发版 S39，"不需要发版"的承诺是假的。
- 照 S35 写 → 14 个 type 表达不出 `keyframe_edit`（仅静态单帧）与 `regenerate_video`（连续）的差别，同一个 `identity` type 在两种窗口下动作不同。

### 2.2 已选方向 1：`type` 定动作族，`check_id` 定窗口与升级

三样东西各管一件事，谁都不越界：

| 层 | 归属 | 管什么 | 变更成本 |
|---|---|---|---|
| `type`（14 值闭集） | `contracts` | 这一族缺陷**可以**用哪几种动作、兜底是什么 | 改闭集 = 全体发版，几乎不做 |
| `check_id` + 登记项 | 各质检服务 | 这一条检查的**最小动作**、**窗口范围**、**何时升级** | 注册即生效，S39 不发版 |
| `recommended_actions` | 质检服务运行时 | 该次检测的**建议**（输入，不是指令） | 无 |

S39 的解析变成三步交集：

```text
candidates = action_family[defect.defect_type]
           ∩ registry[defect.check_id].allowed_actions
           ∩ policy.allowed_actions
起点 = registry.default_action      升级 = registry.escalation_rule
窗口 = registry.window_scope       空集 → human_review, reason=no_capable_route
```

`human_review` 不出现在任何动作族里 —— 它是所有族的公共终点，不是某一族的成员。

**改动落点**：

- [S35 §2.2.1](plans/S35-technical-qc.md) 新增 14 行动作族表（含 `keyframe_edit` 仅在 `window_scope ≤ 6 帧` 可用的约束、音频类缺陷不得走 `reingest` 等硬约束）。
- [S35 §2.2.2](plans/S35-technical-qc.md) 定义登记项字段与两条硬约束：登记项的动作必须是族的子集；severity 只能往上抬不能往下压。
- [S35 §10.3.1](plans/S35-technical-qc.md) 新增 ROUTE-T1..T6 路由不变量，核心是「新 check 不需要 S39 改代码」这条被机械验证。
- [S39 §3.1](plans/S39-repair-planning.md) 整张 27 行 check_id 表删除，替换为上述三步交集。
- [S39 §3.3](plans/S39-repair-planning.md) "不能自动修"不再逐条列 check_id，改为读登记项 `never_auto_repair` + `no_auto_reason`。
- [S39 §10.1](plans/S39-repair-planning.md) 补 E24-R13..R18，其中 R13 直接验证「新增 check 不重建 S39」。

### 2.3 收敛带来的连带改动

`recommended_actions` 枚举从 10 值扩到 12 值，新增 `re_synthesize_speech`（S37 重合成语音）和 `re_render_subtitles`（S38 内部重渲染字幕版式，零生成费用）。原表用自然语言写"重排版/重新 TTS"，那些不是合法枚举值，无法实现。

`realign_subtitles` 只改时码，`re_render_subtitles` 才重建版式，两者不可互相替代；重译**不进**任何动作族 —— 翻译内容变更属于 `change_spec` 范畴。

### 2.4 契约缺口（C6 / C7 / C8 —— 已落）

三条缺口指向同一个东西：`action_family` 原来只是 S35 里的一张文档表格，没有任何机器可读形式，
所以「登记项不得越出动作族」这条约束只能靠人守。

| 编号 | 缺口 | 落点 |
|---|---|---|
| C6 | `action_family` 无机器可读契约，族校验无加载期执行点 | `contracts/quality/action-families.v2.json` + `validate_registry.py` |
| C7 | `recommended_actions` 缺 `re_synthesize_speech` / `re_render_subtitles` | `contracts/quality/qc-report.v2.schema.json`（12 值） |
| C8 | S35 C5 / S39 C5b：动作族与登记项字段都要有 schema | `check-registry.v2.schema.json` + `qc-report.v2.schema.json` |

**落地的四个文件：**

| 文件 | 大小 | 内容 |
|---|---|---|
| `contracts/quality/action-families.v2.json` | 7.6 KB | 14 值闭集 → 动作族；每族 `chain` / `fallback` / `forbidden[]`（含理由）；`keyframe_edit` 的 ≤6 帧前置条件；三步交集公式；5 条不变量 |
| `contracts/quality/qc-report.v2.schema.json` | 24 KB | QCReport v2 完整 payload，**从 `plans/S35` §2.3 程序化提取**而非手抄，避免转录漂移 |
| `contracts/quality/check-registry.v2.schema.json` | 8.3 KB | 登记项形状；`check_id` 命名模式；两条 `allOf` 条件（`never_auto_repair` → 必须有理由；窗口类 → 必须有 `window_max_frames`）；`x-cross-checks` 列出 schema 表达不了的 7 条规则 |
| `contracts/quality/validate_registry.py` | 5.3 KB | 跨文件约束的参考实现。退出码 0=可加载 / 1=拒绝 / 2=环境问题 |

**三份枚举已交叉验证一致**（14 个 `defect_type`、12 个动作），且 `human_review` 不出现在任何
`chain` 里。校验器已用一条违规登记项实测：把 `SUB.TIMING.OVERLAP` 的 `default_action` 写成
`regenerate_video`，被拒并退出 1，两条错误分别指出「起点不在族链内」和「登记项不得拓宽族」。

**仍留一个人工把关点**：`check-registry.v2.json` 数据文件本身还没落 —— schema 与校验器有，
99 条 check 仍只在四份 QC 方案的表里。落地前方案与 registry 之间靠人工对照。已记入
[contracts/README](../contracts/README.md) 的设计缺口第 4 条。

---

## 3. G1（已修，2026-11-01）：S25 声明发布事件但方案里没有 outbox

**原问题。** `services/model-routing/service.yaml` 声明：

```yaml
publishes:
  - event_type: model.route.selected
    topic: drama.model-routing.v2
    retention_days: 14
```

但 `plans/S25-model-routing.md` 全文没有 `outbox` 字样，数据模型只有 4 张表（policy_snapshot / route_decision / route_candidate / degradation_option）。其余 16 份方案都有 outbox，所以这不是"本服务不需要事件"的有意选择。

影响：`RouteDecision` 写入与事件发布之间没有事务保证。下游按事件驱动的路径会漏掉路由决策，只能轮询 `GET /route-decisions/{id}` 兜底 —— 这违反 README 第 4 条硬约束。

**修法。** 按原定的补法补齐，并与 S08 §6.4 / S24 §2.7 的既有写法保持一致：

| 补了什么 | 位置 | 内容 |
|---|---|---|
| outbox 表 | §7 数据模型 | `id / aggregate_id / aggregate_seq / event_type / payload / created_at / published_at`，`UNIQUE (aggregate_id, aggregate_seq)` + 部分索引 `WHERE published_at IS NULL` |
| 同事务写入 | §7.1 | 决策头 + 全部候选 + outbox 行三步一个 `COMMIT`；写明为什么必须同事务（两种拆分各留下一种一致性空洞） |
| 事件体 | §7.1 | `data` 只放指针与派单必需字段，**不放 `score_parts`、不放 `rejected[]` 明细**；`feasible=false` 也发事件 |
| relay | §7.2 | `FOR UPDATE SKIP LOCKED` 批量取、按 `tenant_id:aggregate_id` 单线程发布、失败重试、`published_at + 7d` 归档 |
| 幂等语义 | §8 | 幂等回放路径跳过 outbox 插入；至少一次投递，按 `event_id` 去重、按 `(aggregate_id, aggregate_seq)` 判序 |
| 可观测 | §10 | `route_event_outbox_lag_seconds`（> 60s 告警）、`route_event_outbox_depth` |
| 测试 | §11 | T25-24 中继恢复补发、T25-25 重投去重、T25-26 `feasible=false` 仍发事件、T25-27 幂等回放不产生第二条事件 |

**G1 关闭后的 outbox 覆盖：17/17。**

一个仍然保留的边界：事件**不是授权书**。S26 收到 `model.route.selected` 后仍必须回查 `route_decision` 校验 `content_hash` / `quote_valid_to` / `rank`（§12）。这与 `docs/04`「事件不能直接推动 accepted pointer」是同一条纪律。

---

## 4. G2（已关闭，2026-11-01）：S38 声明依赖 S28 但方案未提

**原问题。** `services/subtitles/service.yaml` 的 `depends_on` 含 `S28`（媒体字节权威），但 `plans/S38-subtitles.md` 全文没有出现 `S28`。当时的假设是两条之一：catalog 声明过宽（应改 catalog），或 S38 缺了一环（应补方案）。

**确认结果：是后者，catalog 不改。**

S38 的输入链确实不需要媒体字节（`S37 时间码 + S06 对白 + S15 词典 + S41 EDL` 足够）。漏掉的是**输出侧**：

`docs/01` §唯一权威表写死「制品bytes、hash、存储位置 → S28；业务域只保存 artifact_id 和内容 hash」。S38 渲染出的 `.srt` / `.vtt` / `.ass` 就是制品字节，与视频帧、音轨、探针截图同类。S38 自己存这些文件就是第二个字节权威，触犯 `AGENTS.md` 第一条。

**补法。** 方案侧补五处，catalog 与 service.yaml 一字未改：

| 补了什么 | 位置 |
|---|---|
| 说明 S28 是输出侧依赖，不是输入依赖 | S38 §3.0 |
| `subtitles.rendition` 表：只有 `artifact_id` + `content_hash` + `byte_size`，无正文字节 | S38 §2.2 |
| `post.subtitle.ready` 加 `artifacts[]`（每格式一个 S28 指针） | S38 §7 |
| 状态机加 `rendered` / `rendered_pending_archive` / `archived` | S38 §9 |
| E25-U5（S28 不可达 → 停在待归档、不发事件、不重渲染）、V6（扫库证明无字幕正文字节） | S38 §10 |

**G2 关闭。** 一个附带的契约缺口记入 S38 §11 C4：`post.subtitle.ready` 没有 payload schema，`artifacts[].artifact_id` 必须明确属 S28 的 ID 空间。

---

## 5. 已确认无问题的两处

- **`sha256:` 前缀**：S02/S40 的示例里 `content_hash` 写成 `"sha256:..."`，契约 schema 是 `^[0-9a-f]{64}$`（裸 hex）。这是**示例占位符写法**，不是字段定义冲突 —— 17 份方案在字段定义处都按裸 hex 写。实现时以 schema 为准，示例按 schema 填充。不构成待改项，但实现时注意别把示例照抄进代码。
- **`grant_state` 字段位置**：S26 说 `admission-record` 里有 `grant_state`，实际它在 `dispatch` 子对象内。S26 的表述正确，是我初查时误判。

---

## 6. 合并状态

| 分支 | 提交 | 方案 | 状态 |
|---|---|---|---|
| `claude/keen-brahmagupta-8e2308` | `d01de44` | S02, S40 | 此前已入 main |
| `claude/suspicious-bhabha-944438` | `72310ed` | S16, S17, S18 | 此前已入 main |
| `claude/amazing-bhabha-570e99` | `9d09eed` | S08, S22, S23 | 本次提取入 main |
| `claude/strange-murdock-f3e52f` | `d8f6055` | S24, S25, S26, S27 | 本次提取入 main |
| `claude/magical-mendeleev-14bc20` | `81eaac1` | S35–S39 | 本次提取入 main |

5 个会话分支本身**未推送**，方案内容已全部收进 `main:plans/`。

---

## 7. 下一步

1. ~~补 S25 的 outbox。~~ 已于 2026-11-01 完成，见 §3。
2. ~~确认 G2（S38 是否真依赖 S28）。~~ 已于 2026-11-01 关闭：catalog 正确，方案补了输出侧，见 §4。
3. ~~落 `action_family` 与登记项的机器可读契约（C6/C7/C8），族校验进加载期。~~
   已于 2026-11-02 完成，见 §2.4。族校验的**参考实现**在 `contracts/quality/validate_registry.py`；
   实现期需要把它移植进 `platform/qc-sdk`，并补 `check-registry.v2.json` 数据文件。
4. 剩余 31 个服务（S01/S03–S07/S09–S15/S19–S21/S28–S34/S41–S48）尚无方案 —— **正在并行编写**，
   已落 18 份，剩余 13 个在 3 个会话窗口中。

方案是设计稿。以上检查证明的是**文档之间自洽**，不是**设计正确**。E01–E40 里真正要证明的东西（跨租户拒绝、重复投递幂等、Saga 补偿、fencing、重启恢复）只能靠实现加测试证明 —— `docs/16` 的原话是「schema 校验通过 ≠ 分布式正确性」。
