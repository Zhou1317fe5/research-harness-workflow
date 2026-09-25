"""Per-host review model contract: validation logic plus built-in defaults.

The reviewer model is part of the project's research contract: every gate
(PRERUN verdict, post-run result analysis, closing review) validates the
requested and observed model against the value approved for that gate's host.
Different users run the whole workflow on different hosts (Pi vs Codex), and
each host approves its own reviewer model independently -- one host switching
models (for example when quota is exhausted) never blocks the other.

Authored values live in ``config/review_contract.toml``, which is
project-owned: like ``config/project.toml`` and ``config/.env`` it is excluded
from template sync, so a project's approved models never collide with the
template's. This module keeps the validation, the per-host derivations and the
built-in defaults that apply while no contract file exists. See
``config/review_contract.example.toml`` for the file shape.

Switching a host's model is an explicit, auditable change: edit that host's
entry in the contract, run the mission contract tests, and commit. Do not add
environment-variable or per-run overrides.

Contract shape::

    schema_version = 1

    [models]              # approved reviewer model per host
    pi = "<provider>/<model>"
    codex = "<bare model>"

    [thinking]
    level = "high"        # requested thinking level for reviewer sessions
    suffix_hosts = ["pi"] # hosts that resolve the level inside the model id

The reviewer session runs with extension discovery disabled, so an approved
model must come from a built-in provider, a logged-in provider, or a compatible
endpoint declared in the agent-level ``models.json``. See
``config/review_contract.example.toml``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise ModuleNotFoundError(
            "Python 3.10 requires tomli; install .agents/harness/requirements.txt "
            "in the selected environment"
        ) from exc


# Hosts the workflow knows how to launch. A contract may change a host's values
# but cannot introduce a new host, because launching, recording and validating
# that host would need code that does not exist yet.
KNOWN_HOSTS = ("pi", "codex")

# Built-in defaults, used when no project contract file is present.
# - "pi":    Pi-launched sessions (the `scientific-reviewer` sub-agent and
#            `pi --model`). Pi registry names are provider-prefixed; a Pi user
#            reaches OpenAI Codex login models via the `openai-codex/...`
#            prefix, or any other provider it registers (e.g. newapi/kimi-k3).
# - "codex": `codex exec -m`, which resolves bare names through the Codex
#            CLI's configured provider registry.
DEFAULT_MODELS = {
    "pi": "openai-codex/gpt-5.6-sol",
    "codex": "gpt-5.6-sol",
}
DEFAULT_THINKING = "high"
# Pi accepts `model:thinking`; the codex CLI takes a bare model name and
# configures reasoning separately (the closing review runner passes the bare
# name too).
DEFAULT_SUFFIX_HOSTS = ("pi",)

CONTRACT_PATH = Path(__file__).resolve().parent / "config" / "review_contract.toml"
CONTRACT_SCHEMA_VERSION = 1
_CONTRACT_KEYS = {"schema_version", "models", "thinking"}


class ReviewContract(NamedTuple):
    """Validated per-host reviewer settings."""

    models: dict[str, str]
    thinking: str
    suffix_hosts: tuple[str, ...]


def _invalid(message: str) -> ValueError:
    return ValueError(f"review_contract_invalid:{message}")


def _host_table(data: object, label: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise _invalid(f"{label}:expected_table")
    unknown = sorted(set(data) - set(KNOWN_HOSTS))
    if unknown:
        raise _invalid(f"{label}:unknown_hosts:{','.join(unknown)}")
    return data


def _default_contract() -> ReviewContract:
    return ReviewContract(
        models=dict(DEFAULT_MODELS),
        thinking=DEFAULT_THINKING,
        suffix_hosts=tuple(DEFAULT_SUFFIX_HOSTS),
    )


def load_contract(path: Path = CONTRACT_PATH) -> ReviewContract:
    """Load the project-owned contract, falling back to the built-in defaults.

    A missing file keeps the defaults. A present but malformed file fails closed
    instead of silently reviewing with an unapproved model.
    """
    if not path.is_file():
        return _default_contract()
    with path.open("rb") as stream:
        try:
            data = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise _invalid(f"toml:{exc}") from exc
    if not isinstance(data, dict):
        raise _invalid("root:expected_table")
    unknown = sorted(set(data) - _CONTRACT_KEYS)
    if unknown:
        raise _invalid(f"unknown_keys:{','.join(unknown)}")
    version = data.get("schema_version", CONTRACT_SCHEMA_VERSION)
    if version != CONTRACT_SCHEMA_VERSION:
        raise _invalid(f"schema_version:{version}")

    contract = _default_contract()

    overrides = _host_table(data.get("models", {}), "models")
    for host, value in overrides.items():
        if not isinstance(value, str) or not value.strip():
            raise _invalid(f"models.{host}:expected_non_empty_string")
        contract.models[host] = value.strip()

    thinking = data.get("thinking", {})
    if not isinstance(thinking, dict):
        raise _invalid("thinking:expected_table")
    thinking_unknown = sorted(set(thinking) - {"level", "suffix_hosts"})
    if thinking_unknown:
        raise _invalid(f"thinking:unknown_keys:{','.join(thinking_unknown)}")
    if "level" in thinking:
        level = thinking["level"]
        if not isinstance(level, str) or not level.strip():
            raise _invalid("thinking.level:expected_non_empty_string")
        contract = contract._replace(thinking=level.strip())
    if "suffix_hosts" in thinking:
        hosts = thinking["suffix_hosts"]
        if not isinstance(hosts, list) or any(
            not isinstance(host, str) or host not in KNOWN_HOSTS for host in hosts
        ):
            raise _invalid("thinking.suffix_hosts:expected_known_hosts")
        contract = contract._replace(suffix_hosts=tuple(dict.fromkeys(hosts)))

    for host in KNOWN_HOSTS:
        if not contract.models.get(host):
            raise _invalid(f"models.{host}:missing")
    return contract


CONTRACT = load_contract()

MODELS = CONTRACT.models
REVIEW_THINKING = CONTRACT.thinking
MODEL_TAKES_THINKING_SUFFIX = {host: host in CONTRACT.suffix_hosts for host in MODELS}


def model_for_host(host: str) -> str:
    try:
        value = MODELS[host]
    except KeyError:
        raise ValueError(f"unknown review host: {host}") from None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"review model for host {host!r} is not configured")
    return value


def review_job_model(host: str) -> str:
    """Reviewer invocation identity a host's launcher/runner must pass.

    Launchers omit ``--model`` and inherit this value, so switching a host's
    model is a single edit in ``config/review_contract.toml``.
    """
    base = model_for_host(host)
    if host not in MODEL_TAKES_THINKING_SUFFIX:
        raise ValueError(f"unknown review host: {host}") from None
    return f"{base}:{REVIEW_THINKING}" if MODEL_TAKES_THINKING_SUFFIX[host] else base


# Canonical recorded identity per host, written into CSV notes, the analysis
# index, and verdicts for that host's channel.
RECORDED_MODELS = {host: model_for_host(host) for host in MODELS}

# Default recorded identity used by single-value consumers that predate the
# per-host split (the Pi canonical name, the project's primary host).
RECORDED_MODEL = RECORDED_MODELS["pi"]
REVIEW_MODEL = RECORDED_MODEL  # backward-compatible alias
REVIEW_JOB_MODEL = review_job_model("pi")  # backward-compatible alias

# Runtime identities accepted from session metadata / event streams: every
# configured host's name plus a thinking suffix. Test fixtures historically
# use :max; both suffixes keep the exact model match.
RUNTIME_MODEL_SUFFIXES = tuple(dict.fromkeys((REVIEW_THINKING, "max")))
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
