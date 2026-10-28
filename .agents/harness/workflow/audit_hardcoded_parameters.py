#!/usr/bin/env python3
"""静态审计科研代码中硬编码的可调参数（数据集白名单、集数断言、超参数常量、固定设备号）。

用于工作流初始化与仓库接入阶段，辅助将写死参数重构为带默认值的 CLI 动态参数，
避免跨数据集实验频繁修改源码与引入回归缺陷。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# 重点关注的超参数与控制变量命名模式（小写匹配）
PARAM_KEYWORDS = {
    "lr",
    "learning_rate",
    "actta_lr",
    "base_lr",
    "alpha",
    "beta",
    "gamma",
    "tau",
    "weight_decay",
    "momentum",
    "episodes",
    "expected_episodes",
    "max_episodes",
    "eval_episodes",
    "batch_size",
    "shot",
    "fold",
    "seed",
    "dataset",
    "datasets",
    "benchmark",
    "target_domain",
    "device",
    "gpu_id",
}

# 常见需要排除扫描的目录
DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".pi",
    ".codex",
    ".claude",
    ".agents",
    ".hermes",
    ".venv",
    "venv",
    "miniconda3",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    "tests",
}


@dataclass
class Finding:
    file: str
    line: int
    variable_or_pattern: str
    current_value: str
    category: str  # 'hyperparameter', 'dataset_whitelist', 'fixed_assertion', 'device_pin'
    suggested_cli_arg: str
    description: str


class ParameterVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str):
        self.relative_path = relative_path
        self.findings: list[Finding] = []

    def _check_name_match(self, name: str) -> bool:
        low = name.lower()
        if low in PARAM_KEYWORDS:
            return True
        for kw in PARAM_KEYWORDS:
            if kw in low and ("_" + kw in low or kw + "_" in low):
                return True
        return False

    def visit_Assign(self, node: ast.Assign) -> None:
        # 检查赋值语句：如 ALPHA = 0.05, DATASETS = ['deepglobe'], device = 'cuda:0'
        for target in node.targets:
            if isinstance(target, ast.Name):
                var_name = target.id
                if self._check_name_match(var_name):
                    val_str = self._format_value(node.value)
                    if val_str is not None:
                        cat = self._categorize(var_name, node.value)
                        cli_arg = "--" + var_name.lower().replace("_", "-")
                        self.findings.append(
                            Finding(
                                file=self.relative_path,
                                line=node.lineno,
                                variable_or_pattern=var_name,
                                current_value=val_str,
                                category=cat,
                                suggested_cli_arg=cli_arg,
                                description=f"变量 '{var_name}' 硬编码为 '{val_str}'，建议暴露为 CLI 启动参数并将当前值设为 default。",
                            )
                        )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # 检查比较语句：如 if dataset == "deepglobe" 或 if dataset not in ["deepglobe", "isic"]
        left_str = self._node_to_str(node.left)
        if left_str and self._check_name_match(left_str):
            for comparator in node.comparators:
                val_str = self._format_value(comparator)
                if val_str is not None and isinstance(comparator, (ast.Constant, ast.List, ast.Tuple, ast.Set)):
                    cli_arg = "--" + left_str.lower().replace("_", "-")
                    self.findings.append(
                        Finding(
                            file=self.relative_path,
                            line=node.lineno,
                            variable_or_pattern=f"Compare({left_str})",
                            current_value=val_str,
                            category="dataset_whitelist" if "data" in left_str.lower() else "fixed_assertion",
                            suggested_cli_arg=cli_arg,
                            description=f"条件分支直接与硬编码值 '{val_str}' 比较，可能阻碍跨域/动态调度。",
                        )
                    )
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        # 检查断言：如 assert expected_episodes == 600
        test = node.test
        if isinstance(test, ast.Compare):
            left_str = self._node_to_str(test.left)
            if left_str and self._check_name_match(left_str):
                val_str = (self._format_value(test.comparators[0]) if test.comparators else "") or ""
                self.findings.append(
                    Finding(
                        file=self.relative_path,
                        line=node.lineno,
                        variable_or_pattern=f"Assert({left_str})",
                        current_value=val_str,
                        category="fixed_assertion",
                        suggested_cli_arg="--" + left_str.lower().replace("_", "-"),
                        description=f"代码存在针对 '{left_str}' 的硬断言 (== {val_str})，在多数据集/多档位实验中易引发误报阻断。",
                    )
                )
        self.generic_visit(node)

    def _categorize(self, name: str, value_node: ast.AST) -> str:
        low = name.lower()
        if "device" in low or "cuda" in str(getattr(value_node, "value", "")).lower():
            return "device_pin"
        if any(k in low for k in ("data", "domain", "benchmark")):
            return "dataset_whitelist"
        if any(k in low for k in ("episode", "step", "count")):
            return "fixed_assertion"
        return "hyperparameter"

    def _node_to_str(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._node_to_str(node.value)}.{node.attr}"
        return None

    def _format_value(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant):
            return repr(node.value)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            formatted: list[str] = []
            for e in node.elts:
                val = self._format_value(e)
                if val is None:
                    return None
                formatted.append(val)
            bracket = "[]" if isinstance(node, ast.List) else "()" if isinstance(node, ast.Tuple) else "{}"
            return bracket[0] + ", ".join(formatted) + bracket[1]
        return None


def audit_repository(root: Path, target_files: list[str] | None = None) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []

    if target_files:
        files_to_scan = [root / f for f in target_files]
    else:
        files_to_scan = []
        for path in root.rglob("*.py"):
            parts = set(path.relative_to(root).parts)
            if parts & DEFAULT_EXCLUDE_DIRS:
                continue
            # 优先关注核心入口与算法实现文件
            name = path.name.lower()
            if any(k in name for k in ("main", "eval", "train", "scope", "config", "model", "probe", "adapter", "util")):
                files_to_scan.append(path)

    for py_file in sorted(files_to_scan):
        if not py_file.is_file():
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        rel_path = py_file.relative_to(root).as_posix()
        visitor = ParameterVisitor(rel_path)
        visitor.visit(tree)
        findings.extend(visitor.findings)

    return findings


def render_report(findings: list[Finding], output_format: str = "text") -> str:
    if output_format == "json":
        return json.dumps([asdict(f) for f in findings], indent=2, ensure_ascii=False)

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("            [工作流初始化] 代码硬编码参数与动态契约体检报告")
    lines.append("=" * 80)

    if not findings:
        lines.append("\n✅ 未检测到显式的硬编码可调参数或白名单阻断，代码具备良好的动态传参解耦性！\n")
        lines.append("=" * 80)
        return "\n".join(lines)

    lines.append(f"\n共发现 {len(findings)} 处潜在硬编码可调参数/白名单限制：\n")
    for idx, f in enumerate(findings, 1):
        lines.append(f"[{idx}] {f.file}:{f.line}  (类别: {f.category})")
        lines.append(f"    - 参数/模式: {f.variable_or_pattern} = {f.current_value}")
        lines.append(f"    - 建议 CLI 传参: {f.suggested_cli_arg} (default={f.current_value})")
        lines.append(f"    - 诊断: {f.description}")
        lines.append("")

    lines.append("-" * 80)
    lines.append("【重构建议与用户决策说明】（推荐改）：")
    lines.append("1. 为什么推荐改：")
    lines.append("   将硬编码参数重构为带默认值的 CLI 参数（通过 argparse/click 暴露），可以让")
    lines.append("   跨数据集（如 ISIC/Chest/FSS）和调参实验完全无需改动 Python 源码，消除")
    lines.append("   因源码改动而引发的多轮 PRERUN 审查与代码回归风险。")
    lines.append("2. 耗时预估：")
    lines.append("   全自动/半自动重构与等价性验证约需 3~5 分钟。")
    lines.append("3. 风险与安全保障：")
    lines.append("   - 零破坏原则：所有重构出的 CLI 参数其 default 必须严格等于原硬编码值；")
    lines.append("   - 等价性验证：重构后立即运行默认参数单测或 1-step 前向探针，确保输出张量 bitwise 一致。")
    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("."), help="目标仓库根目录 (默认: 当前目录)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="输出格式 (text 或 json)")
    parser.add_argument("files", nargs="*", help="指定要扫描的文件相对路径 (若不传则全仓启发式扫描)")
    args = parser.parse_args()

    findings = audit_repository(args.path, target_files=args.files if args.files else None)
    report = render_report(findings, output_format=args.format)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
