# Current Conclusions

> 本文件只回答一个问题：**经过这些实验，我们现在相信什么、否定什么。**
> Experiment 是证据，Conclusion 才是跨实验判断。旧实验不因被推翻而删除。
> 发现：`OPEN | SUPPORTED | MIXED | REJECTED | SUPERSEDED`；推断只用 `OPEN | REJECTED | SUPERSEDED`。
> 用户决定：`ACTIVE | PROPOSED | SUPERSEDED`；执行事实：`OBSERVED | RETRACTED | SUPERSEDED`。
> 生效决定与已验证成绩分开记录；条目注明类型、范围、来源和取代关系。

---

## 一、基线定义与设计决定 (Decisions)
<!-- 记录算法基线、评测口径与人工批准的冻结决定，状态多为 ACTIVE / SUPERSEDED -->

### C01 — <基线或决定标题>
Type: decision
Status: **ACTIVE**
Scope: <对象与适用范围>

<当前决定内容，例如基线锁定版本、评测协议等>

Source: <来源事件 ID、用户指令或文档引用>

---

## 二、核心假设与实验发现 (Findings & Hypotheses)
<!-- 记录跨实验验证的科学假设与指标涨跌发现，状态为 SUPPORTED / REJECTED / OPEN -->

### C02 — <假设或发现标题>
Type: hypothesis
Status: **OPEN**
Scope: <对象、任务与适用范围>

<当前结论>

支持证据：<ExpID>
反例 / 限制：<ExpID>
Source: <来源事件 ID 或可回查的文档引用>

---

## 三、已关闭路径 (Closed Paths)

| 路径 | 关闭理由 | 证据 |
|---|---|---|

---

> 维护规则：认知**真正改变**时才更新。新增结论给 C 编号；被取代的标 `SUPERSEDED`
> 并写明被谁取代，不删除。`REJECTED` 条目是失败教训的召回入口。
