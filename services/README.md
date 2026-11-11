# 服务索引

48 个独立服务。每个服务的元数据在 `services/<slug>/service.yaml`，语义与验收在其 `service.md`。

完整定义见 [docs/02-service-catalog.md](../docs/02-service-catalog.md)。

**44 个服务为 `design_only`；S01 `tenant-access`、S08 `revision-snapshot`、S09 `lineage-impact`、S28 `artifact-ingest` 为 `implemented`。** 依赖关系是运行协作关系，不是启动拓扑要求；异步环通过事件拆解。


## 平台（4）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S01 | [租户与访问](tenant-access/service.md) | enterprise | POST /access-grants | tenant-access |
| S02 | [治理与自动化策略](governance-policy/service.md) | enterprise | POST /policies/{id}/revisions | governance-policy |
| S40 | [人工审核与接管](human-review/service.md) | governance | POST /review-tasks/{id}/decisions | human-review |
| S45 | [审计与证据保全](audit/service.md) | enterprise | POST /audit-records | audit |

## 创作（4）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S03 | [项目与剧集目录](project-catalog/service.md) | creation | POST /projects/{id}/episodes | project-catalog |
| S04 | [创意与剧本](narrative/service.md) | creation | POST /scripts/{id}/revisions | narrative |
| S05 | [分集分场](breakdown/service.md) | creation | POST /breakdowns | breakdown |
| S06 | [分镜与导演规范](storyboard/service.md) | creation | POST /storyboards/{id}/revisions | storyboard |

## 工作流（4）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S07 | [对话修改与变更计划](change-management/service.md) | control | POST /change-plans/{id}:apply | change-management |
| S08 | [版本快照与接受状态](revision-snapshot/service.md) | control | POST /manifests/{id}:commit | revision-snapshot |
| S09 | [依赖溯源与影响分析](lineage-impact/service.md) | control | POST /lineage-edges | lineage-impact |
| S21 | [长任务编排](orchestration/service.md) | control | POST /runs | orchestration |

## 资产（7）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S10 | [资产目录与绑定解析](asset-catalog/service.md) | assets | POST /binding-sets | asset-catalog |
| S11 | [角色身份资产](character-assets/service.md) | assets | POST /characters/{id}/revisions | character-assets |
| S12 | [服装与妆造资产](costume-assets/service.md) | assets | POST /costumes/{id}/revisions | costume-assets |
| S13 | [场景与空间资产](scene-assets/service.md) | assets | POST /scene-assets/{id}/revisions | scene-assets |
| S14 | [道具资产](prop-assets/service.md) | assets | POST /props/{id}/revisions | prop-assets |
| S15 | [音色与声音授权](voice-assets/service.md) | assets | POST /voices/{id}/revisions | voice-assets |
| S48 | [LoRA与模型适配训练](adaptation-training/service.md) | assets | POST /training-jobs | adaptation-training |

## 记忆（3）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S16 | [跨集记忆与正史](story-memory/service.md) | memory | POST /memory-commits | story-memory |
| S17 | [连续性校验](continuity/service.md) | memory | POST /continuity-checks | continuity |
| S18 | [上下文检索与组装](context-retrieval/service.md) | memory | POST /context-packs | context-retrieval |

## Agent（2）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S19 | [Agent运行与工具沙箱](agent-runtime/service.md) | agents | POST /agent-tasks | agent-runtime |
| S20 | [Prompt模板与编译](prompt-compiler/service.md) | agents | POST /prompt-compilations | prompt-compiler |

## 执行（3）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S22 | [调度与供应商配额](scheduler-quota/service.md) | execution | POST /permits:acquire | scheduler-quota |
| S26 | [统一生成任务协调](generation-jobs/service.md) | execution | POST /generation-jobs | generation-jobs |
| S27 | [供应商连接器](provider-connectors/service.md) | execution | POST /connector-commands | provider-connectors |

## 财务（1）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S23 | [预算与成本账本](budget-ledger/service.md) | finance | POST /budget-authorizations | budget-ledger |

## 模型（2）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S24 | [模型能力与价格注册](model-registry/service.md) | models | POST /capability-snapshots | model-registry |
| S25 | [模型路由与估价](model-routing/service.md) | models | POST /route-decisions | model-routing |

## 生成（6）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S29 | [图像与关键帧生产](image-production/service.md) | production | POST /image-jobs | image-production |
| S30 | [视频片段生产](video-production/service.md) | production | POST /video-jobs | video-production |
| S31 | [配音生产](speech-production/service.md) | production | POST /speech-jobs | speech-production |
| S32 | [口型生产](lipsync/service.md) | production | POST /lipsync-jobs | lipsync |
| S33 | [音效生产](sound-effects/service.md) | production | POST /sfx-jobs | sound-effects |
| S34 | [BGM与音乐](music/service.md) | production | POST /music-jobs | music |

## 媒体（1）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S28 | [素材归档与制品目录](artifact-ingest/service.md) | media | POST /artifact-imports | artifact-ingest |

## 质量（4）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S35 | [媒体技术质检](technical-qc/service.md) | quality | POST /technical-checks | technical-qc |
| S36 | [视觉语义与时序质检](semantic-qc/service.md) | quality | POST /semantic-checks | semantic-qc |
| S37 | [音频理解与对齐质检](audio-alignment/service.md) | quality | POST /alignment-jobs | audio-alignment |
| S39 | [返工与修复决策](repair-planning/service.md) | quality | POST /repair-plans | repair-planning |

## 后期（4）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S38 | [字幕与本地化](subtitles/service.md) | post | POST /subtitle-jobs | subtitles |
| S41 | [剪辑与时间线](editorial/service.md) | post | POST /timelines/{id}/revisions | editorial |
| S42 | [混音与合成渲染](composition/service.md) | post | POST /render-jobs | composition |
| S43 | [交付与外部分发](delivery/service.md) | post | POST /delivery-packages | delivery |

## 数据（1）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S44 | [效果评测与经营分析](evaluation-analytics/service.md) | analytics | POST /evaluation-runs | evaluation-analytics |

## 体验（2）

| ID | 服务 | 域 | 命令入口 | 部署单元 |
|---|---|---|---|---|
| S46 | [开发者API与MCP](developer-integration/service.md) | experience | POST /integrations/commands | developer-integration |
| S47 | [实时状态与通知投影](realtime-projection/service.md) | experience | POST /subscriptions | realtime-projection |
