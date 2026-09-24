"""Per-host source of truth for the independent scientific review model.

The reviewer model is part of the project's research contract: every gate
(PRERUN verdict, post-run result analysis, closing review) validates the
requested and observed model against the value approved for that gate's host.
Different users run the whole workflow on different hosts (Pi vs Codex), and
each host approves its own reviewer model independently -- one host switching
models (for example when quota is exhausted) never blocks the other.

Switching a host's model is an explicit, auditable change: edit the matching
entry in ``MODELS`` below, sync materialized mirrors, run the mission contract
tests, and commit. Do not add environment-variable or per-run overrides.
"""

from __future__ import annotations

import re

# Approved reviewer model per host launcher / agent registry.
#
# - "pi":    Pi-launched sessions (the `scientific-reviewer` sub-agent and
#            `pi --model`). Pi registry names are provider-prefixed; a Pi user
#            reaches OpenAI Codex login models via the `openai-codex/...`
#            prefix, or any other provider it registers (e.g. newapi/kimi-k3).
# - "codex": `codex exec -m`, which resolves bare names through the Codex
#            CLI's configured provider registry.
#
# To switch a host's reviewer model, change only that host's value. A host the
# project never uses may keep a placeholder; it is only exercised when that
# host's channel actually runs.
MODELS = {
    "pi": "openai-codex/gpt-5.6-sol",
    "codex": "gpt-5.6-sol",
}

# Requested thinking level for reviewer sessions.
REVIEW_THINKING = "high"


def model_for_host(host: str) -> str:
    try:
        value = MODELS[host]
    except KeyError:
        raise ValueError(f"unknown review host: {host}") from None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"review model for host {host!r} is not configured")
    return value


def review_job_model(host: str) -> str:
    """Reviewer invocation identity including the requested thinking level."""
    return f"{model_for_host(host)}:{REVIEW_THINKING}"


# Canonical recorded identity per host, written into CSV notes, the analysis
# index, and verdicts for that host's channel.
RECORDED_MODELS = {host: model_for_host(host) for host in MODELS}

# Default recorded identity used by single-value consumers that predate the
# per-host split (the Pi canonical name, the project's primary host).
RECORDED_MODEL = RECORDED_MODELS["pi"]
REVIEW_MODEL = RECORDED_MODEL  # backward-compatible alias
EXEC_MODEL = model_for_host("codex")  # backward-compatible alias
REVIEW_JOB_MODEL = review_job_model("pi")  # backward-compatible alias

# Runtime identities accepted from session metadata / event streams: every
# configured host's name plus a thinking suffix. Test fixtures historically
# use :max; both suffixes keep the exact model match.
RUNTIME_MODEL_SUFFIXES = ("high", "max")
_ACCEPTED_BASES = frozenset(RECORDED_MODELS.values())
RUNTIME_MODEL_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(base) for base in sorted(_ACCEPTED_BASES))
    + r")(?::(?:"
    + "|".join(RUNTIME_MODEL_SUFFIXES)
    + r"))?$"
)


def normalize_model_identity(value: str) -> str:
    """Strip the thinking suffix; returns the bare (possibly prefixed) name."""
    base = value.strip()
    for suffix in RUNTIME_MODEL_SUFFIXES:
        tail = f":{suffix}"
        if base.endswith(tail):
            return base[: -len(tail)]
    return base


def accepted_model_for_host(host: str) -> str:
    """The exact model identity a channel on this host must request/record."""
    return model_for_host(host)


def is_accepted_model_identity(value: str, host: str | None = None) -> bool:
    """True if the runtime identity matches the approved model.

    With ``host`` set, only that host's approved model is accepted; otherwise
    any configured host's model is accepted.
    """
    base = normalize_model_identity(value)
    if host is not None:
        return base == model_for_host(host)
    return base in _ACCEPTED_BASES


# Host/channel labels used by the gates, mapped to the MODELS key.
CHANNEL_HOST = {
    # post-run analysis channels
    "scientific-reviewer-subagent": "pi",
    "codex-exec-independent": "codex",
    # closing review modes
    "reviewer-subagent": "pi",
    # reviewer_job backends
    "pi": "pi",
    "codex": "codex",
}


def host_for_channel(channel: str) -> str:
    try:
        return CHANNEL_HOST[channel]
    except KeyError:
        raise ValueError(f"unknown review channel: {channel}") from None
