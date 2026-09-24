"""Single source of truth for the independent scientific review model.

The reviewer model is part of the project's research contract: every gate
(PRERUN verdict, post-run result analysis, closing review) validates the
requested and observed model against these constants. Switching models (for
example when quota is exhausted) is an explicit, auditable change:

1. edit REVIEW_MODEL / EXEC_MODEL / RUNTIME_MODEL_RE below;
2. sync the mirrors (.codex and .claude skill trees are hardlinked to
   .agents, so most consumers follow automatically; re-copy if a mirror was
   materialized);
3. update the display values in the skill docs that cite the model;
4. run the mission contract tests.

Do not add environment-variable or per-run overrides: the whole point is that
the model identity can only change through a reviewed commit.
"""

from __future__ import annotations

import re

# Provider-prefixed canonical identity recorded in CSV notes, the analysis
# index, and reviewer job verdicts.
REVIEW_MODEL = "openai-codex/gpt-5.6-sol"

# Model name passed to `codex exec -m` (CLI takes the bare name).
EXEC_MODEL = "gpt-5.6-sol"

# Requested thinking level for reviewer sessions.
REVIEW_THINKING = "high"

# Full reviewer invocation identity used by the PRERUN reviewer job.
REVIEW_JOB_MODEL = f"{REVIEW_MODEL}:{REVIEW_THINKING}"

# Runtime identities accepted from session metadata / event streams. Test
# fixtures historically use :max; both suffixes keep the exact model match.
RUNTIME_MODEL_SUFFIXES = ("high", "max")
RUNTIME_MODEL_RE = re.compile(
    r"^" + re.escape(REVIEW_MODEL) + r"(?::(?:" + "|".join(RUNTIME_MODEL_SUFFIXES) + r"))?$"
)

# Suffix-stripped comparison for verdict fields recorded by reviewer_job.
def normalize_model_identity(value: str) -> str:
    base = value.strip()
    for suffix in RUNTIME_MODEL_SUFFIXES:
        tail = f":{suffix}"
        if base.endswith(tail):
            return base[: -len(tail)]
    return base
