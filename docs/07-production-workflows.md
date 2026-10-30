# 07 · 企业级生产、修改与恢复流程

## 1. 整集生产

1. S46/BFF经S01鉴权，S02解析模式，S03定位项目，S08创建Run使用的manifest与epoch；API返回operation，不阻塞等待生成。
2. S18构建固定checkpoint上下文；S04/S05/S06分别执行创意剧本、分场、分镜；S19各角色池负责有界推理，输出交给各内容服务校验与保存。
3. S10解析资产与权利版本；缺资产交S11–S15/S29准备，按策略门接受canonical；S48可选训练，独立预算与GPU池。
4. S08接受剧本后S16提交正史；后续依赖集等required checkpoint。S17检查知情、状态、伏笔与视觉连续性。
5. S21扩展shot子工作流，S31配音与S37字级对齐确定对白长度，S29准备关键帧。无对白分支使用显式no-op，不创建空音频假成功。
6. S20/S25/S24完成Prompt、候选路由与能力校验，S26经S23/S22准入，由S27独立渠道执行。所有LLM/VLM和创作推理同样受预算与配额约束。
7. S28及时归档，S35/S36技术与语义时序QC；S32必要时做口型，再S37/S36检查音画与边界。失败交S39修复计划，新attempt受累计上限约束。
8. S02判定自动接受或S40审核，S08只接受当前epoch候选。S41创建EDL，S33/S34准备音效音乐，S38生成字幕，S42混音合成与标准化输出。
9. 全片QC及配置的最终门通过，S08接受master，S43生成交付包；外部分发单独授权。S44/47更新经营和实时状态；S45保存证据。

## 2. 对话局部修改

“第3场第2镜改仰拍、缩4秒”：S07用明确集上下文解析稳定shot_id → S06准备新DSL与结尾停顿派生改动 → S09按字段边找受影响子图 → S17/S37验证对白与连续性 → S25估算增量成本 → S02判断自动应用 → S08 CAS提交新manifest/epoch → S21只运行受影响子图。

重做关键帧、视频、必要口型与QC；复用未改文本/音色与足够短的TTS；重算EDL、字幕时码、音效/BGM切点、混音与master。旧任务结果归历史，实际花费仍结算。没有集上下文或4秒无法容纳对白时返回具体澄清选项，自动化档位不授权随意删台词。

## 3.跨集改稿

修改已接受的前集剧情需同时创建memory impact plan；S16的新事实checkpoint与旧manifest保留映射，S09定位后续集所依赖的claim。S07先展示跨集影响，不用修改一个字段导致全剧悄悄重做。已发布成片若不迁移，仍标记其历史canon checkpoint。

## 4. 执行层级与停止

Temporal层级为SeriesBatch → Episode → Scene/Shot → ProductionStage → ProviderJobWait/Media/QC。每层扇出上限、deadline、cancel scope与预算scope明确；过大history用子工作流和Continue-As-New，业务idempotency跨Run持久化。

Workflow纯确定性控制流，业务I/O、推理、存储、非确定时钟在Activity。Activity deadline、heartbeat与重试策略按任务类型配置；等待模型使用callback signal+持久timer对账，不在API进程sleep。

暂停停止新准入，继续已有任务归档结算；取消可取消渠道任务，其余discard_on_completion；恢复先查已存在provider_job。缺失素材/必要节点失败导致INCOMPLETE或WAITING_*，不能将其当SUCCEEDED。无人值守结束时给独立分支结果与失败清单。

## 5. 标准输出

Master/clean master、字幕SRT/VTT/ASS、对白/音效/BGM stems、EDL/Timeline JSON、资产版本manifest、Prompt/模型与政策引用、QC与审核证据、成本报告、跨集记忆checkpoint、交付完整性签名。具体编码/色彩/响度由客户项目规格确定。
