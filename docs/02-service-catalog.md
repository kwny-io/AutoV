# 独立服务目录

48个业务服务边界；Gateway、BFF、Studio与托管基础设施另外独立部署。每个链接均有职责、数据、命令、事件、扩容、故障与专项验收。

| ID | 服务 | 域 | 数据所有者schema | 负责团队 |
|---|---|---|---|---|
| S01 | [租户与访问](../services/tenant-access/service.md) | enterprise | `tenant_access` | platform |
| S02 | [治理与自动化策略](../services/governance-policy/service.md) | enterprise | `governance_policy` | platform |
| S03 | [项目与剧集目录](../services/project-catalog/service.md) | creation | `project_catalog` | creation |
| S04 | [创意与剧本](../services/narrative/service.md) | creation | `narrative` | creation |
| S05 | [分集分场](../services/breakdown/service.md) | creation | `breakdown` | creation |
| S06 | [分镜与导演规范](../services/storyboard/service.md) | creation | `storyboard` | creation |
| S07 | [对话修改与变更计划](../services/change-management/service.md) | control | `change_management` | workflow |
| S08 | [版本快照与接受状态](../services/revision-snapshot/service.md) | control | `revision_snapshot` | workflow |
| S09 | [依赖溯源与影响分析](../services/lineage-impact/service.md) | control | `lineage_impact` | workflow |
| S10 | [资产目录与绑定解析](../services/asset-catalog/service.md) | assets | `asset_catalog` | assets |
| S11 | [角色身份资产](../services/character-assets/service.md) | assets | `character_assets` | assets |
| S12 | [服装与妆造资产](../services/costume-assets/service.md) | assets | `costume_assets` | assets |
| S13 | [场景与空间资产](../services/scene-assets/service.md) | assets | `scene_assets` | assets |
| S14 | [道具资产](../services/prop-assets/service.md) | assets | `prop_assets` | assets |
| S15 | [音色与声音授权](../services/voice-assets/service.md) | assets | `voice_assets` | assets |
| S16 | [跨集记忆与正史](../services/story-memory/service.md) | memory | `story_memory` | memory |
| S17 | [连续性校验](../services/continuity/service.md) | memory | `continuity` | memory |
| S18 | [上下文检索与组装](../services/context-retrieval/service.md) | memory | `context_retrieval` | memory |
| S19 | [Agent运行与工具沙箱](../services/agent-runtime/service.md) | agents | `agent_runtime` | agents |
| S20 | [Prompt模板与编译](../services/prompt-compiler/service.md) | agents | `prompt_compiler` | agents |
| S21 | [长任务编排](../services/orchestration/service.md) | control | `orchestration` | workflow |
| S22 | [调度与供应商配额](../services/scheduler-quota/service.md) | execution | `scheduler_quota` | execution |
| S23 | [预算与成本账本](../services/budget-ledger/service.md) | finance | `budget_ledger` | finance |
| S24 | [模型能力与价格注册](../services/model-registry/service.md) | models | `model_registry` | models |
| S25 | [模型路由与估价](../services/model-routing/service.md) | models | `model_routing` | models |
| S26 | [统一生成任务协调](../services/generation-jobs/service.md) | execution | `generation_jobs` | execution |
| S27 | [供应商连接器](../services/provider-connectors/service.md) | execution | `provider_connectors` | execution |
| S28 | [素材归档与制品目录](../services/artifact-ingest/service.md) | media | `artifact_ingest` | media |
| S29 | [图像与关键帧生产](../services/image-production/service.md) | production | `image_production` | generation |
| S30 | [视频片段生产](../services/video-production/service.md) | production | `video_production` | generation |
| S31 | [配音生产](../services/speech-production/service.md) | production | `speech_production` | generation |
| S32 | [口型生产](../services/lipsync/service.md) | production | `lipsync` | generation |
| S33 | [音效生产](../services/sound-effects/service.md) | production | `sound_effects` | generation |
| S34 | [BGM与音乐](../services/music/service.md) | production | `music` | generation |
| S35 | [媒体技术质检](../services/technical-qc/service.md) | quality | `technical_qc` | quality |
| S36 | [视觉语义与时序质检](../services/semantic-qc/service.md) | quality | `semantic_qc` | quality |
| S37 | [音频理解与对齐质检](../services/audio-alignment/service.md) | quality | `audio_alignment` | quality |
| S38 | [字幕与本地化](../services/subtitles/service.md) | post | `subtitles` | post |
| S39 | [返工与修复决策](../services/repair-planning/service.md) | quality | `repair_planning` | quality |
| S40 | [人工审核与接管](../services/human-review/service.md) | governance | `human_review` | platform |
| S41 | [剪辑与时间线](../services/editorial/service.md) | post | `editorial` | post |
| S42 | [混音与合成渲染](../services/composition/service.md) | post | `composition` | post |
| S43 | [交付与外部分发](../services/delivery/service.md) | post | `delivery` | post |
| S44 | [效果评测与经营分析](../services/evaluation-analytics/service.md) | analytics | `evaluation_analytics` | data |
| S45 | [审计与证据保全](../services/audit/service.md) | enterprise | `audit` | platform |
| S46 | [开发者API与MCP](../services/developer-integration/service.md) | experience | `developer_integration` | experience |
| S47 | [实时状态与通知投影](../services/realtime-projection/service.md) | experience | `realtime_projection` | experience |
| S48 | [LoRA与模型适配训练](../services/adaptation-training/service.md) | assets | `adaptation_training` | assets |
