"""项目专属 rrctl adapter 的契约注册表。

模板默认为空：所有 adapter 走 build_rrctl_runspec.py 的通用契约校验
（ADAPTER_CONTRACT_FIELDS）。

项目需要专属 adapter 时，在 .agents/harness/remote/adapters/ 下实现，然后在此登记它的
契约字段集。builder 通过注册表查找，**不需要修改 builder 本体**。

登记格式：

    MY_ADAPTER = ["python", ".agents/harness/remote/adapters/my_adapter.py"]

    PROJECT_ADAPTERS = {
        tuple(MY_ADAPTER): {
            # schema_version -> 该版本要求的字段集；None 键为无版本时的默认
            "contracts": {
                "my.adapter-contract.v1": {"schema_version", "progress_path", ...},
            },
            # 该 adapter 是否要求 source.bundle_sha256
            "requires_bundle_sha256": False,
        },
    }
"""
from __future__ import annotations

from typing import Any

PROJECT_ADAPTERS: dict[tuple[str, ...], dict[str, Any]] = {}
