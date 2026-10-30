# 05 · 完整跨集记忆域

## 1. 三个独立服务

S16 Story Memory持有正史；S18 Context Retrieval负责权限与时间过滤后的检索、组装；S17 Continuity负责发现逻辑/视觉/声音连续性冲突。S09保留记忆声明到上下文、剧本、镜头、成片的依赖。S19中的记忆抽取Agent只是受约束计算任务，无权把草稿直接写成正史。

## 2. 记忆数据与时间

| 类型 | 内容 | 关键字段 |
|---|---|---|
| WorldRule | 世界规则、年代、不可违反设定 | rule_id、scope、exceptions |
| CharacterProfile | 人物背景、性格、目标、能力 | entity_id、profile_revision |
| StoryEvent | 发生事件、参与者、因果 | event_id、participants、causes、story_time |
| EntityState | 伤势、服装状态、道具归属、位置 | entity_id、predicate、value、valid interval |
| Relationship | 信任、亲属、敌对、债务 | subject/object、relation、valid interval |
| KnowledgeState | 谁知道/相信/误解哪个命题 | knower_id、claim_id、belief_type、reveal_event |
| PlotThread | 伏笔、悬念、目标、回收条件 | thread_id、open/advanced/resolved、due_story_range |
| EpisodeSummary | 接受剧本与成片的分层摘要 | episode_manifest_ref、intent/observed、claim_refs |
| CreativePreference | 导演偏好与已接受例外 | owner/scope、explicit/inferred、expiry |
| CanonBranch | 正史分支、实验改稿与分叉 | branch_id、parent_checkpoint、merge_status |

双时间：valid_story_time记录故事中何时成立，recorded_at/commit_sequence记录系统何时知道。另有episode_order、audience_reveal_scope、character_knowledge_scope，分别解决闪回、先拍后播、观众已知而角色未知等问题。不可仅按created_at取最新。

Claim带稳定ID、不可变revision、subject/predicate/object、来源service/id/revision/hash、source_kind、branch、accepted_manifest、valid_from/to、supersedes、confidence与review状态。记忆只包含可证据化声明；推测保存candidate，不能与accepted事实混用。

## 3. 写入与正式接受

剧本新版本 → 抽取candidate claims → 结构规则/冲突检查 → 按项目模式人工或自动接受剧本 → S08发布manifest accepted事件 → S16核验来源并原子提交MemoryCommit → 发布canon.committed → S18/S09推进索引与依赖水位。

S16 commit按series+branch序列化，提供expected_checkpoint做CAS；提交批次中必须含完整来源。重复接受事件不重复生成事实。剧本意图与成片观测分层存储：成片中道具错误不直接覆盖剧本正史，形成continuity defect与待决策冲突。合法即兴改动要经过明确接受事件才更新正史。

节点可用的上下文由`memory_checkpoint + source_manifest_set + branch`固定。后续集生成前声明required canon checkpoint；投影未达到时等候/回源，不能凭“检索没找到”认定事实不存在。

## 4. 检索与Agent上下文

输入：租户/series/branch、目标episode、故事时间区间、角色集合、角色视角或全知视角、任务类型、token预算、最低checkpoint。

过滤顺序：认证与项目权限 → 分支与接受状态 → 故事有效期 → 信息揭示/角色知情限制 → 相关性检索 → 冲突标记 → 摘要。硬事实用结构化查询；语义检索用于关联背景与伏笔。向量库/全文索引均可重建，非事实源。

ContextPack返回fact refs、plot thread refs、asset refs、prohibited assumptions、unresolved conflicts、source watermark、token usage和hash。输入事实来自不可信文本时按数据封装，不允许“记忆里写了忽略预算”改变工具权限。

编剧包偏向人物关系和伏笔；分镜包偏向动作、道具与空间；Prompt包偏向身份与可见状态；QC包包含预期状态与允许例外。不能把整剧全部文本无界传给每次模型调用。

只读检索/预览只组装已有事实和已生成摘要，不隐式发起付费推理。需要新摘要或复杂推理时提交显式plan:compute任务，经S19/S26预算准入后保存版本，再供检索读取。

## 5. 改前集、分支与记忆失效

例：第2集原来“林岚左手受伤，将钥匙交陈默，仍不知道陈默身份”。第3集应保持伤势、钥匙归属和知情边界。

若第2集改为“藏起钥匙”，S07提交新剧本manifest；S16创建superseding claims与新checkpoint；S09沿claim→context_pack→script→shot→artifact依赖定位后续集。计划区分必须改剧情、仅重做QC、可复用资产。未明确迁移前，已交付集保留原checkpoint和历史证据，不悄悄重写。

改稿分支采用copy-on-write引用父checkpoint，实验分支事实不污染正史。合并输出冲突清单（同一谓词相同valid interval的不同值、互斥事件、因果破坏、重复回收伏笔），由规则或有权限人工裁决。回滚追加新commit，不删除历史claim。

## 6. 记忆专项验收

必须覆盖跨3集状态、角色知情范围、观众先知、误导性对白、闪回、时间跳跃、候选未接受、前集改稿传播、分支隔离与合并、索引滞后、重复事件、记忆抽取幻觉与来源撤销。自动化模式也不能允许无证据推测成为accepted正史。
