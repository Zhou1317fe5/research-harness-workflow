"""Single source of truth for the independent scientific review model.

The reviewer model is part of the project's research contract: every gate
(PRERUN verdict, post-run result analysis, closing review) validates the
requested and observed model against these constants. Switching models (for
example when quota is exhausted) is an explicit, auditable change: edit the
values below, sync materialized mirrors, run the mission contract tests, and
commit. Do not add environment-variable or per-run overrides.

Two namespaces exist because the same logical model is addressed through
different host registries:

- ``pi`` covers Pi-launched sessions (the `scientific-reviewer` sub-agent and
  `pi --model`), whose registry uses provider-prefixed names;
- ``codex`` covers `codex exec -m`, whose registry/provider configuration
  resolves bare names.

Records (CSV notes, the analysis index, reviewer verdicts) store the
provider-prefixed canonical identity so historical evidence stays comparable
across hosts.
"""

from __future__ import annotations

import re

# Logical reviewer model key.
MODEL_KEY = "gpt-5.6-sol"

# Model names passed to each host's launcher / agent registry.
MODELS = {
    "pi": f"openai-codex/{MODEL_KEY}",
    "codex": MODEL_KEY,
}

# Requested thinking level for reviewer sessions.
REVIEW_THINKING = "high"


def model_for_host(host: str) -> str:
    try:
        return MODELS[host]
    except KeyError:
        raise ValueError(f"unknown review host: {host}") from None


def review_job_model(host: str) -> str:
    """Reviewer invocation identity including the requested thinking level."""
    return f"{model_for_host(host)}:{REVIEW_THINKING}"


# Canonical identity recorded in CSV notes, the analysis index, and verdicts.
RECORDED_MODEL = model_for_host("pi")

# Runtime identities accepted from session metadata / event streams, for every
# configured host plus the canonical recorded identity. Test fixtures
# historically use :max; suffixes keep the exact model match.
RUNTIME_MODEL_SUFFIXES = ("high", "max")
_ACCEPTED_BASES = {RECORDED_MODEL, *MODELS.values()}
RUNTIME_MODEL_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(base) for base in sorted(_ACCEPTED_BASES))
    + r")(?::(?:"
    + "|".join(RUNTIME_MODEL_SUFFIXES)
    + r"))?$"
)

# Backward-compatible aliases for consumers written before MODEL_KEY existed.
REVIEW_MODEL = RECORDED_MODEL
EXEC_MODEL = model_for_host("codex")
REVIEW_JOB_MODEL = review_job_model("pi")


def normalize_model_identity(value: str) -> str:
    """Strip the thinking suffix; returns the bare (possibly prefixed) name."""
    base = value.strip()
    for suffix in RUNTIME_MODEL_SUFFIXES:
        tail = f":{suffix}"
        if base.endswith(tail):
            return base[: -len(tail)]
    return base


def is_accepted_model_identity(value: str) -> bool:
    """True if the runtime identity names the configured model on any host."""
    return normalize_model_identity(value) in _ACCEPTED_BASES
