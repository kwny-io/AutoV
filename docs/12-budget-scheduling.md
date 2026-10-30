# 12 · 企业预算、配额与公平调度

## 1. 预算账本独立

S23是金额唯一权威，支持企业/项目/单集/Run/镜头多级预算、币种和价格版本。reserve/authorize/accrue/settle/release/adjust/refund分开；reservation不等于实际消费。金额用integer minor或明确micro-units，不使用float。FX与时点留痕，不直接相加不同币种。

`available = limit - conservative_consumed - outstanding_reserved`。父预算包含已委派escrow与全部子级承诺，不能重复计算可用额。多级行按固定顺序锁；重复usage事件用业务唯一键去重；对账差额追加adjustment，不篡改历史。

S24 PriceSnapshot包含按请求/生成秒/字符/token/像素等计费单位、失败计费、税币、有效期。无可验证上界的任务不能以“0元估计”准入。报价留安全余量；最终供应商费用可能超报价，需保守暂记与新任务冻结机制，不承诺外部账单绝对不会超预算。

## 2. 配额与调度独立

S22维持tenant/project/provider/account/region/modality/GPU池的额度。RPM/TPM与在途并发是不同计数，HTTP提交结束不释放在途槽。CPU媒体和自托管GPU按资源秒/内存另配，不能混成视频API并发。

租户weighted fair queue、任务priority、aging和企业保底份额组合；在线人工修改可在租户份额内优先，不能无限抢占批量客户。供应商已运行任务通常不可抢占，优先级只影响尚未dispatch的任务。平台队列与供应商队列等待分别展示。

持久Permit：RESERVED→DISPATCH_AUTHORIZED→INFLIGHT→RELEASED；提交前普通租约可到期补偿，已经授权/提交或未知任务必须对账确认终态再释放。Redis只缓存令牌桶，丢失后保守暂停窗口或用持久请求日志重建。

## 3. 企业费用归因

每条费用关联tenant/project/series/episode/run/shot/attempt/provider_job/modality/model/channel/price_snapshot。共享角色训练/设定集可列全剧公共成本或按政策摊销，原始账单保留；同一资产复用不再次虚记生成费。

经营报表同时列生成直接费、失败返工、QC/VLM、训练、媒体计算、存储传输、人工与平台分摊。客户价格与平台供应商成本分账，企业充值/开票若需要应接独立商业计费系统，不能把生产预算ledger冒充法定总账。

## 4. 准入、停止与对账

具体Saga见04。预算增加是独立权限；自动化档位不能提高额度。暂停/取消不意味着供应商退款；不可取消任务继续结算。每天/分钟级扫描未知提交、超期permit、预留泄漏、账单漂移、价格过期；按严重度告警并阻止对应账户池新增调用。

对账接口必须可证明一个provider_job对应哪个attempt；供应商账单延迟时保持pending_reconciliation。跨地域灾备未确认旧执行器停止时不重发付费任务；以安全停止换取防重复费用。
