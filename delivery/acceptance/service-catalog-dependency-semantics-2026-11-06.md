# `depends_on` 的语义缺口：7 个服务提出 30 条待补依赖，但没人定义这个字段管什么

日期：2026-11-06　范围：`contracts/service-catalog.v2.json`　状态：**待裁决，未改任何值**

## 0. 一句话

七份方案各自说「`depends_on` 缺 X」，合计 30 条。但 `depends_on` 在仓库里**没有语义定义**——
它既可能是"同步调用依赖"，也可能是"事件消费依赖"，也可能兼有。在这条定下来之前，
补任何一条都是猜口径。**本文不给值，只把这 30 条和它们的口径分歧摆出来。**

## 1. 已核实的现状

- `depends_on` 的全库定义：**零处**。`docs/01`–`17`、`contracts/*.md`、`README.md`、`AGENTS.md`
  里都搜不到这个词的解释。
- 它唯一的消费者是 `tools/scaffold/generate_services.py:104-107`，把数组原样写进
  `services/<slug>/service.yaml` 的 `depends_on:` 列表，不做校验、不做推断。
- 因此 `depends_on` 目前只是**一个字符串数组**，不是契约。

## 2. 关键证据：两份方案对同一字段的口径相反

| 出处 | 说法 | 读法 |
|---|---|---|
| [S13:759](../../plans/S13-scene-assets.md) | 「实际依赖 S09 的影响计划 \| catalog \| 本文只校验 `impact_plan_ref` 形态，**不做同步依赖**；若要让 S13 **阻断式等待**需改 catalog」 | `depends_on` = **阻断式同步等待**。S13 故意不加，因为它是异步消费 |
| [S39:922](../../plans/S39-repair-planning.md) | 「实际依赖 S26（`generation.job.state-changed` 计数）、S23（预算预检）、S24/S25（能力查询）、S40（人审交接）、S17（`ContinuityReport`）\| 补 cata…」 | 这些**全部是事件/查询**，不是同步等待，但方案认为该进 `depends_on` |

同一个字段，S13 明确拒绝把异步依赖写进去，S39 明确要求把异步依赖写进去。
**这不是谁写错了，是字段没有定义。**

## 3. 30 条待补依赖（按服务）

来源逐条可查；「性质」一列是我从方案原文读出的调用形态，**不是我的判断**。

### S29（1 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S25 | 同步：`route_decision_id` 在 `POST /generation-jobs` 里必填 | [S29:1064](../../plans/S29-image-production.md) |

### S30（3 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S25 | 同步：`route_decision_id` 必填 | [S30:1201](../../plans/S30-video-production.md) |
| S31 | 同步：`dialogue_duration_ms` 的来源（S30 §4.4 判"对白主动作不被截断"） | 同上 |
| S06 | 查询：分镜与节拍表 | 同上 |

### S31（3 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S25 | 同步：`route_decision_id` 必填 | [S31:1379](../../plans/S31-speech-production.md) |
| S37 | 查询：`dialogue_span_ms` 与 `AUD.*` 判定来源 | 同上 |
| S06 / S04 | 查询：`utterance` 的文本版本与 `source_ref` | 同上 |

### S32（7 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S30 | 查询：`video_ref` 的来源 | [S32:1453](../../plans/S32-lipsync.md) |
| S37 | 查询：`AUD.TEXT.*` 判定依据 + `AUD.LIPSYNC.*` 复检 | 同上 |
| S25 | 同步：`route_decision_id` 必需 | 同上 |
| S10 / S13 | 查询：人脸身份与授权 | 同上 |
| S36 | 查询：视觉复检（身份一致性、遮挡、边缘） | 同上 |
| S39 | 事件：`RepairAction` 来源 | 同上 |
| S41 | 事件：时间线与镜头删除 | 同上 |

### S33（4 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S25 | 同步：`route_decision_id` | [S33:1431](../../plans/S33-sound-effects.md) |
| S02 | 查询：策略 | 同上 |
| S07 | 查询：变更管理 | 同上 |
| S41 | 事件：时间线 | 同上 |

### S34（6 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S02 | 查询：策略 | [S34:1549](../../plans/S34-music.md) |
| S25 | 同步：`route_decision_id` | 同上 |
| S07 | 查询：变更管理 | 同上 |
| S41 | 事件：时间线 | 同上 |
| S43 | 事件：交付 | 同上 |
| S37 | 查询：`AUD.*` 判定 | 同上 |

### S39（6 条）

| 依赖 | 性质 | 出处 |
|---|---|---|
| S26 | 事件：`generation.job.state-changed` 计数 | [S39:922](../../plans/S39-repair-planning.md) |
| S23 | 同步：预算预检 | 同上 |
| S24 / S25 | 查询：能力查询 | 同上 |
| S40 | 同步：人审交接 | 同上 |
| S17 | 查询：`ContinuityReport` | 同上 |

**按性质粗分**：明确的同步依赖 8 条，事件/查询 22 条。若 `depends_on` 取 S13 的口径
（只记阻断式同步等待），则 22 条不该进；若取 S39 的口径（记全部"实际依赖"），则 30 条都该进。
**两种口径给出的 catalog 完全不同。**

## 4. 加进去会撞到的两件事

### 4.1 已有的 S26 ⇄ S27 环

`S26.depends_on = [S25, S22, S23, S27]`，`S27.depends_on = [S26]`——双向。
[S27 §9](../../plans/S27-provider-connectors.md) 明确这是**有意设计**：两条 Kafka 边异步、
唯一同步边 `:claim` 方向 S27→S26，"没有反向同步依赖"。

但如果按 S39 的口径把事件依赖也写进 `depends_on`，这个环会被当成真实环；
按 S13 的口径，`S26 → S27` 这条（S26 发 `connector.submit.requested` 给 S27）也不该进 catalog。
**两种口径下 S26 ⇄ S27 都有问题，只是问题不同。**

### 4.2 加上 S41 / S43 会形成新的跨域环

S32/S33/S34 要加 S41（时间线），而 `S41.depends_on` 里有 `S08`、`S28`、`S19`，
`S43.depends_on` 里有 `S35`、`S36`、`S37`。生产组已经在 `S26` 的上游，
post 组在生产组的**下游**。把 S41/S43 加进生产组的 `depends_on`，
等于让"下游"变成"上游的依赖"——部署顺序会变成不可排序。

## 5. 需要 owner 拍的三件事

1. **`depends_on` 的定义**：同步调用依赖、事件消费依赖，还是两者分列
   （例如 `depends_on_sync` / `consumes_from`）？
2. **S26 ⇄ S27 的环**：`depends_on` 是声明"会调用"，还是"必须按序启动"？
   前者允许环，后者不允许。
3. **生产组是否该依赖 post 组**：S32/S33/S34 声明的 S41/S43 依赖若成立，
   当前的分层（production → post）需要重画。

## 6. 本文没有做的事

- **没有改 catalog。** 30 条一条都没加。
- **没有从散文推断字段值。** 上表「性质」列是从方案原文读出的调用形态，不是语义结论。
- **没有替 owner 选口径。** §5 的三问是冲突点，按 [AGENTS.md](../../AGENTS.md)
  「契约与文档冲突 → 停下，报告冲突，不要自行选边」处理。

## 7. 复核方式

```bash
python -I -c "
import json
c=json.load(open('contracts/service-catalog.v2.json'))
m={s['service_id']:s for s in c['services']}
for k in ('S29','S30','S31','S32','S33','S34','S39'):
    print(k, m[k]['depends_on'])
"
```

30 条的出处行号见上表，逐条可回溯到方案原文。
