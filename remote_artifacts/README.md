# remote_artifacts —— 实验原始证据区

**这里只放证据，不放认知。** 科研结论在 `research_workspace/`。

## 布局

```text
remote_artifacts/<ExpID>/<RunID>/
  results.json  summary.json  config.yaml  train.log  eval.log  figures/
```

## 规则

1. **不进 Git。** 项目根 `.gitignore` 已含 `remote_artifacts/*`（README 例外）。
2. **不作 Agent 默认上下文，禁止递归批量读取。** 只在核验具体实验时定点读取单个 ExpID。
3. **`record.json` 引用此处的路径以项目根解析**，形如 `remote_artifacts/<ExpID>/`。
4. checkpoint、tensor、逐样本 trace 留在远端，不拉到这里。

## 下钻顺序

```text
STATE.md → CONCLUSIONS.md → EXPERIMENTS.csv → record.json → analysis/analysis.md
                              ↓ 只有需要核验具体实验时
                    remote_artifacts/<ExpID>/
                              ↓ 仍然不足时
                        远端完整原始数据
```
