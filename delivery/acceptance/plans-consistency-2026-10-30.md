# 实现方案统一验收 · 2026-10-30

范围：`plans/` 下 17 份服务实现方案，由 5 个并行会话产出后合并到 `main`。

结论：**13 项通过，1 项冲突待裁决，2 项缺口待补。** 没有阻断问题，但下面 C1 是两套方案的设计前提互相矛盾，实现前必须先定。

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
| outbox 覆盖 | 声明 `publishes` 的方案是否含 outbox | **16/17**（S25 缺，见 G1） |
| E01–E40 覆盖 | 全量扫描 | **30/40** 被引用，未覆盖 10 项全部属于尚未写方案的服务 |

未覆盖的 E 编号：E02 三集连续、E06 无人值守失败、E29 恶意媒体/SSRF、E33 Temporal 升级、E35 容量/noisy neighbor、E36 UI 断线、E37 交付、E38 费用重算、E39 审计保留、E40 FDE 移交 —— 对应 S21/S29–S34/S42–S48，属正常待办。

---

## 2. C1（待裁决）：S39 到底按什么维度做修复路由

这是两份方案的核心设计前提互相矛盾，不能靠实现时"看着办"。

**S35 §2.2 的立场**（`plans/S35-technical-qc.md:97`）：

> **S39 按 `type` + `recommended_actions` 决策，永不按 `check_id` 决策。** 这样任一质检服务新增一个检查项，不需要 S39 发版；同时新增检查项也不能改变修复路由，因为路由维度被锁在闭集里。

**S39 §3.1 的实际做法**（`plans/S39-repair-planning.md:78-106`）：

主映射表第一列就是 check_id 分组（`TECH.CONTAINER.PARSE` / `VIS.IDENTITY.FACE_DRIFT` / `AUD.LIPSYNC.*` …），§3.3 的"不能自动修"清单更是逐条按 check_id 列举。全文**没有** `type → action` 的兜底表，也没有 `recommended_actions` 参与决策的逻辑。

**为什么这是实质冲突而不是措辞差异**：

- 若照 S39 写，S35/S36/S37/S38 任何一个新增 check，S39 必须同步发版，否则新 check 落到默认分支（而默认分支在 S39 里不存在）。S35 那句"不需要 S39 发版"就是假的。
- 若照 S35 写，S39 §3.1 整张表和 §3.3 的清单都要重构成 type 维度，而 14 个 type 表达不出 `keyframe_edit`（仅静态单帧）与 `regenerate_video`（连续）的差别 —— 同一个 `identity` type 在两种窗口下动作不同。

**两侧都有道理**，所以按 `AGENTS.md`「契约与文档冲突 → 停下，报告冲突，不要自行选边」，这里不选。

可选的收敛方向（供决策，非结论）：

1. **type 定动作族，check_id 定窗口与升级条件。** 保留 S35 的"新增 check 不发版"承诺，同时允许 registry 的 `severity_max` / `recheck_scope` 字段表达差异。
2. **承认 S39 必须随质检服务发版**，把 S35 那句改成"新增 check 需在 registry 登记，S39 读 registry 而非硬编码"。这条能落地，但要删掉 S35 现在的强承诺。
3. 引入第三种维度（如 `repair_class`），由 check 登记时声明，路由键固定在 `repair_class × window_scope` 上。

无论选哪条，**S35 §2.2 和 S39 §3.1 必须改一处**，不能两边都留着。

---

## 3. G1（待补）：S25 声明发布事件但方案里没有 outbox

`services/model-routing/service.yaml` 声明：

```yaml
publishes:
  - event_type: model.route.selected
    topic: drama.model-routing.v2
    retention_days: 14
```

`plans/S25-model-routing.md`（563 行）全文**没有** `outbox` 字样，数据模型只有 4 张表（policy_snapshot / route_decision / route_candidate / degradation_option），没有 outbox 表，也没描述何时发 `model.route.selected`。

其余 16 份方案都有 outbox，所以这不是"本服务不需要事件"的有意选择。

影响：`RouteDecision` 写入与事件发布之间没有事务保证。下游按事件驱动的路径会漏掉路由决策，只能轮询 `GET /route-decisions/{id}` 兜底 —— 这违反 README 第 4 条硬约束。

补法：加 outbox 表 + 在与 `route_decision` 同一本地事务里写事件行，与 S24 的写法保持一致。

---

## 4. G2（待确认）：S38 声明依赖 S28 但方案未提

`services/subtitles/service.yaml` 的 `depends_on` 含 `S28`（媒体字节权威），但 `plans/S38-subtitles.md` 全文没有出现 `S28`。

S38 的输入链是「S37 字级时间码 + 对白文本」，看起来不需要直接读媒体字节。可能的情况：

- service.yaml 的依赖声明过宽（实际经 S37/S41 间接获得所需信息），应改 catalog；
- 或者 S38 确实需要从 S28 取音轨做 `SUB.TIMING` 对齐，方案漏写了这一环。

两种处理不同（改 catalog vs 补方案），需要确认。按 `AGENTS.md`「依赖服务未实现 → 用 mock 按契约走」，实现时若不确定就先按依赖存在来 mock。

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

1. 裁决 C1（S39 路由维度），改 S35 或 S39 其中之一。
2. 补 S25 的 outbox。
3. 确认 G2（S38 是否真依赖 S28），改 catalog 或补方案。
4. 剩余 31 个服务（S01/S03–S07/S09–S15/S19–S21/S28–S34/S41–S48）尚无方案。

方案是设计稿。以上检查证明的是**文档之间自洽**，不是**设计正确**。E01–E40 里真正要证明的东西（跨租户拒绝、重复投递幂等、Saga 补偿、fencing、重启恢复）只能靠实现加测试证明 —— `docs/16` 的原话是「schema 校验通过 ≠ 分布式正确性」。
