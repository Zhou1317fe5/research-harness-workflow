# 日常实验使用指南：双会话极简闭环

科研实验的标准操作采用**双会话解耦**模式：一个会话用高级强模型做方案设计与决策，另一个会话用高额度/低成本模型做代码实现与实验调度。

---

## 核心分工

```text
【会话 1：方案与决策】
模型：高级强模型（如 gpt-6-astra / gpt-6.1-sol / claude-opus-5 / claude-fable-5.1）
流程：提出新想法或分析历史结果 → 讨论收敛 →「给出spec方案」→「转csv」→ 获得 CSV 路径
                                  ↓
                        issues/.../*.csv
                                  ↓
【会话 2：代码与执行】
模型：执行模型 / 高额度模型（如 gpt luna / deepseek）
流程：输入「mission issues/.../*.csv」
     自动完成：代码修改 → PRERUN 审查 → 托管训练 (rrctl) → 证据拉回 → 输出 review.md
```

---

## 交互流转（四步闭环）

### 1. 方案会话：讨论与定方案
在强模型会话中，开展方案讨论。

* **场景 A：全新实验方案**
  直接说明研究意图与基线对比：
  ```text
  我想在现有基线上验证 [某假设/新机制]，沿用现有数据划分与评测口径。
  请和我讨论对照设计、主指标与预算，先形成实验方案，暂不实现。
  ```

* **场景 B：基于已有实验迭代**
  将上一轮产出的 review 总结提供给模型：
  ```text
  issues/<上一轮目录>/<任务名>.review.md 实验已完成，请分析结果并讨论下一步方向。
  ```

讨论收敛后，直接要求生成方案：
```text
ok 给出spec方案
```

### 2. 方案会话：转任务清单并取路径
审阅 Spec 方案无误后，直接指令转 CSV 并索取路径：
```text
批准，转csv
```
模型生成并在回复中输出唯一的 CSV 路径（如 `issues/<任务目录>/<任务清单>.csv`）。方案阶段交接完成。

### 3. 执行会话：直接运行 CSV
新开一个执行会话（使用 `gpt luna` / `deepseek`），直接输入上一步拿到的路径：
```text
mission issues/<任务目录>/<任务清单>.csv
```
执行模型将全自动推进：
1. **代码修改与本地检查**：实现改动，检查参数传递与语法。
2. **short-smoke 与 PRERUN**：涉及核心逻辑改动时，自动跑 1~100 步短 smoke，由独立只读会话完成运行前审查。
3. **GPU 托管与首步验收**：提交 `rrctl`，30s~2min 内确认显存占用、step >= 1 及有限 Loss。过门后静默挂机。
4. **拉回证据与交付**：训练完成自动拉回产物与指标，生成本次实验的 `review.md`（位于 `issues/<任务目录>/<任务名>.review.md`）。

### 4. 循环迭代：带回 review.md 继续推进
将执行会话生成的 `review.md` 路径复制回【会话 1】，进入下一轮迭代：
```text
issues/<本次目录>/<任务名>.review.md 实验已完成。请总结分析指标变化与发现，下一步做什么？
```

---

## 中断与断点恢复

若执行过程中遇到网络中断或退出会话，重开执行会话后直接再次输入原 CSV 路径：
```text
mission issues/<任务目录>/<任务清单>.csv
```
或者直接输入 `mission`，Agent 会自动从 `issues/.missions.json` 读取活跃指针，沿 CSV 中未完成的任务行精准恢复，绝不重复启动已完成的实验。

---

## 产物与索引速查

| 想看什么 | 位置 |
|---|---|
| 本轮执行交付与复盘 | `issues/<任务目录>/<任务名>.review.md` |
| 当前全局研究主线 | `research_workspace/STATE.md` |
| 已验证的有效结论 | `research_workspace/CONCLUSIONS.md` |
| 历史实验索引台账 | `research_workspace/EXPERIMENTS.csv` |
| 单次实验机器事实与分析 | `research_workspace/experiments/<ExpID>/analysis/analysis.md` |
| 远程原始运行产物 | `remote_artifacts/<ExpID>/<RunID>/` |
