# 06 · 人工参与分层与企业治理

## 1. 四档产品模式

| mode | 行为 | 等待与失败处理 |
|---|---|---|
| guided | 用户逐节点启动，节点内部自动执行 | 完成后等待下一明确命令；仍可预生成获授权的独立准备分支 |
| stage_review | 阶段自动，配置关口人工接受 | 默认剧本/角色canonical/关键分镜/粗剪/最终交付门；关口可按项目选择 |
| exception_assisted | 连续生成、自动QC和有界修复 | 低置信度、冲突、预算不足、修复耗尽时创建人审任务并等待相关分支 |
| unattended_batch | 按预设策略自动选候选、返工、合成 | 无法解决的必要分支标blocked，继续独立分支；Run可结束INCOMPLETE并产失败报告，不伪造成片成功 |

四档影响的是交互策略；权限、授权、数据驻留、预算、质量最低标准是硬规则。guided并不免除审计；unattended不意味着允许删对白、换音色或扩大预算。

## 2. 策略继承与可覆盖边界

企业强制策略 → 项目默认 → 单集/Run覆盖 → 阶段覆盖 → 节点覆盖。低层只能在企业允许范围内提高或调整创作自动化；企业要求的审批、法定保留与预算权限不能被覆盖。resolve输出effective_policy与每字段来源，界面可解释“为什么需要人工”。

独立字段：generation_mode（manual/auto）、acceptance_mode（manual/auto_if_qc_pass）、gate、locked、allowed_actions、repair_limits、budget_scope、reviewer_roles、exception_behavior、expiry、policy_version。自动生成多个候选后等人选、自动选后等人确认正式交付均可配置。

## 3. 门与决定

门类型：创作接受、资产canonical、质量例外、预算增加、音色/资产资格、外部分发。决定权限分开：导演可以接受创作，不自动拥有提高预算权限；财务批准额度不等于批准画面质量。

ReviewTask固定manifest/epoch、artifact hash、policy version、evidence、reason、options、assignee role和deadline。S40记录决定，S02判断权限/条件，S08最终CAS提交；过期任务、旧epoch决定返回stale，不更新当前版。多人审批可配置quorum与职责分离；同一用户重复点击算同command。

超时在guided/stage_review/exception_assisted通常WAITING_REVIEW；unattended按已配置策略隔离或失败，不自动批准。必要内容未通过时允许预览包，正式交付禁止。自动接受也记录PolicyDecision与QC证据，能追溯谁配置了规则。

## 4. 动态切换与人工接管

策略在Run开始固定。运行中切换需授权命令形成新PolicyRevision，在下一安全checkpoint应用，只影响未准入动作；已经提交供应商的任务继续对账。人工锁定节点提升epoch/lock版本，阻止自动覆盖；过期任务结果进入历史候选。

固定的是创作与自动化语义策略。账号撤销、资产/声音授权撤销、企业kill switch、数据驻留禁用和实时预算余额在每次dispatch前重新检查，不能因为Run固定旧policy而继续越权。局部修改提升受影响node_epoch，取消/灾备切主提升execution_fence；见04。

用户可在任意节点暂停、改参数、重写对白、替换媒体、选候选、分支试做、解锁。用户上传同样进行归档、权限与QC。紧急kill switch按tenant/provider/cell停止新dispatch，已有供应商任务是否可取消由渠道能力决定，未确认取消不报退款。

## 5. 产品默认

企业初次项目用stage_review，角色与风格稳定后转exception_assisted；可重复模板的批量任务使用unattended。项目创建向导直接选择四档并展示需要人工的门，单集顶栏显示当前档位，节点检查器显示继承来源。设置变更预览影响的节点和在途任务，不在运行中偷偷改默认。
