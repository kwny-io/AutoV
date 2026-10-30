# Temporal 工作流

定义见 [docs/07-production-workflows.md](../docs/07-production-workflows.md)。

| 工作流 | 职责 |
|---|---|
| `episode` | 整集生产：SeriesBatch → Episode → Scene/Shot |
| `shot` | 镜头级：ProductionStage → ProviderJobWait → Media → QC |
| `memory-migration` | 记忆迁移与正史 checkpoint 推进 |
| `admission` | 付费准入 Saga |
| `repair` | 缺陷修复计划与有界返工 |

## Temporal 是执行历史的权威

执行历史归 Temporal，不是自研服务。但**当前生产状态归 S08**，两者不要混。

## Activity 必须业务幂等

Temporal 的 Activity 可能执行多次。观察到完成一次**不等于**执行一次。

不能把"我看它成功了"当成幂等保证。

## Continue-As-New

Signal / Update 去重的作用域受 Continue-As-New 影响，跨代之后要去重逻辑仍然成立。

## 停止语义

层级停止有明确规则。`execution_fence` 管控整个 run / branch 的取消与故障切换；
`node_epoch` 只管单个节点的局部重生成。**两者不能混用。**

旧 epoch 的结果永远不能推进当前接受指针。

## 准入工作流

```
S26 建 attempt(attempt_id + request_hash + quote)
  → S23 预留预算
  → S22 申请 quota permit
  → 两者都确认 → 落 SubmissionIntent + Outbox
  → S27 拿 fenced dispatch grant 才真正提交
  → 观测回执 → accrue / settle → 释放 permit
```

`SUBMISSION_UNKNOWN` / `SUBSESSION_UNKNOWN` 状态下**禁止盲目重提**，必须先对账
（`must_reconcile_before_resubmit`）。
