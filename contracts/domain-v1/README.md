# v1领域负载基线

仅保留5个细化JSON Schema，供v2内部负载兼容与迁移参考。旧部署、旧租户上下文、旧全局epoch与旧OpenAPI均被替代。

ShotSpec/GenerationRequest/QCReport可作为内容payload；外部调用必须使用v2身份、分支、manifest、node_epoch、execution_fence与政策/记忆引用。v1 ChangePlan仅覆盖机位和时长，其它改动由v2变更计划及服务端字段白名单验证。v1事件envelope仅供历史迁移，生产事件使用v2。

跨字段时间、权限、金额、引用存在性与状态迁移仍须领域服务校验，JSON Schema不证明分布式行为已正确。
