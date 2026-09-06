"""_assert_write_context 的回归测试。

对应 v7 会话的两次 false completion：工作发生在平行目录、修复提交落到无关分支。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import csv_state as cs  # noqa: E402

ROW = {"id": "ISSUE-01", "branch": "feature/target", "notes": ""}


def _repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    (path / "m.csv").write_text("id\nISSUE-01\n", encoding="utf-8")
    return path


def test_branch_match_passes(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r", "feature/target")
    monkeypatch.chdir(repo)
    cs._assert_write_context(repo / "m.csv", ROW)


def test_branch_mismatch_rejected(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r", "feature/other")
    monkeypatch.chdir(repo)
    with pytest.raises(cs.StateUpdateError, match="write_context_branch_mismatch"):
        cs._assert_write_context(repo / "m.csv", ROW)


def test_empty_branch_skips(tmp_path, monkeypatch):
    """branch 未声明时不做无根据的断言。"""
    repo = _repo(tmp_path / "r", "feature/other")
    monkeypatch.chdir(repo)
    cs._assert_write_context(repo / "m.csv", {**ROW, "branch": ""})


def test_parallel_repo_rejected(tmp_path, monkeypatch):
    """cwd 与 CSV 属于不同仓库——v7 第一次 false completion 的形态。"""
    here = _repo(tmp_path / "here", "feature/target")
    other = _repo(tmp_path / "other", "feature/target")
    monkeypatch.chdir(here)
    with pytest.raises(cs.StateUpdateError, match="write_context_repo_mismatch"):
        cs._assert_write_context(other / "m.csv", ROW)


def test_non_repo_skips(tmp_path):
    """不在 git 仓库内时跳过：前提本就不成立，不是本函数能判的。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "m.csv").write_text("id\n", encoding="utf-8")
    cs._assert_write_context(plain / "m.csv", ROW)
