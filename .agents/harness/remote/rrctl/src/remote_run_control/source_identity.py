"""Stable source-content identity independent from transport packaging."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import RRCError
from .jsonutil import sha256_json
from .models import SourceSpec


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RRCError(
            "source_identity_git",
            "failed to resolve reviewed source identity",
            "source_identity",
            details={"git_args": list(args), "stderr": result.stderr.strip()[-2000:]},
        )
    return result.stdout.strip()


def source_content_sha256(repo: Path, source: SourceSpec) -> str:
    """Hash reviewed Git content and its controlled post-commit manifest.

    Run identifiers, branch labels, staging refs and bundle bytes are deliberately
    excluded. The commit and tree are both retained so retry identity remains tied
    to the exact reviewed revision rather than merely an equivalent checkout.
    """

    commit = _git(repo, "rev-parse", "--verify", f"{source.commit}^{{commit}}")
    tree = _git(repo, "show", "-s", "--format=%T", commit)
    return sha256_json(
        {
            "schema_version": "rrctl.source-content.v1",
            "commit": commit,
            "tree": tree,
            "allowed_post_commit_paths": sorted(set(source.allowed_post_commit_paths)),
        }
    )
