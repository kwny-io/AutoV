# 实现方案

各服务的详细实现方案。**这里是文档目录，不是代码目录。**

## 为什么单独放

`services/<slug>/` 只放代码（`src/ api/ events/ migrations/ tests/ deploy/ runbooks/`）。

实现方案是设计产物，会随着实现推进不断重写，不应该和代码文件混在一起 —— 否则 `ls services/<slug>/` 分不清哪些是要提交的代码、哪些是会过时的文档。

`docs/design-package/` 是**原始设计包存档，只读**。任何新写的文档都不放那里，避免把原始输入和后续产出混为一谈。

## 命名

`plans/<服务ID>-<slug>.md`，与 `services/<slug>/` 一一对应：

```
plans/
  S02-governance-policy.md
  S08-revision-snapshot.md
  S16-story-memory.md
  S24-model-registry.md
  S35-technical-qc.md
  ...
```

## 状态

方案是**设计稿**，不是已实现的功能。写方案时服务仍是 `design_only`，代码一行没有，SLO 未验证。

方案里的表结构、状态机、失败分支是否成立，只能靠实现和测试证明。**文档写完不等于设计对了。**

## 与代码的关系

实现时以方案为起点，但以 `service.yaml`（机器可读元数据）和 `docs/` 下的设计文档为准。

方案与设计文档冲突时，**停下来报告冲突，不要自行选边**（见 [AGENTS.md](../AGENTS.md)）。
