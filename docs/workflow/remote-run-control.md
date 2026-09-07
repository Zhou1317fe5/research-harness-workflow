# Remote Run Control

`rrctl` is a daemonless, fail-closed control plane for reproducible remote workloads. It freezes one Git commit, validates external anchors, stages a deterministic standard-library worker over SSH, launches the workload in tmux, records authoritative lifecycle state, runs generic and project-adapter health checks, and pulls only manifest-declared artifacts.

It does not schedule GPUs, interpret scientific metrics, update experiment ledgers, or import project code.

## v1 Scope

- Linux local and remote hosts
- OpenSSH, including optional `sshpass` compatibility
- Git bundle source staging
- tmux sessions
- conda workload environments
- external JSON adapters
- first-step, periodic, and completion health
- SHA-bound artifact pull and recovery

## Install

From the research workflow project root:

```bash
python -m pip install -e .agents/harness/remote/rrctl
rrctl --help
```

The package has no runtime dependencies outside the Python standard library.
It remains an independent Python package: the distribution is `remote-run-control`,
the import name is `remote_run_control`, and the command is `rrctl`.
Reinstall an editable installation if it points to the previous source directory.

## Profiles

Copy the shape in `.agents/harness/remote/rrctl/examples/profiles.example.json` to `~/.config/rrctl/profiles.json` and set mode `0600` when a password environment reference is present. RunSpec files contain only the profile name, never credentials.

## RunSpec

A `rrctl.run.v1` document binds:

- run/project identity;
- local repository, branch, full commit, stable `source_content_sha256`, and bookkeeping-only drift allowlist;
- remote stage/repository/control/output roots;
- tmux and conda settings;
- workload argv;
- input anchors and expected SHA256;
- three health phase contracts, `required | advisory | disabled` GPU telemetry policy, and optional adapter argv;
- artifact allowlist and local pull root.

Workload and adapter commands are argv arrays. Shell command strings are rejected by the parser.

## CLI

```bash
rrctl ready run_spec.json
rrctl launch run_spec.json
rrctl inspect <run-id>
rrctl health <run-id> --phase periodic
rrctl wait <run-id>
rrctl pull <run-id>
rrctl pull <run-id> --diagnostic
rrctl resume --profile <name> --control-path <remote-path>
rrctl abort <run-id> --yes
```

Use `--json` for compact machine-readable output. `launch` includes the first-step gate. `wait`, `inspect`, and `resume` are observers: cancellation, connection loss, or periodic `unhealthy` telemetry reports attention while preserving the remote workload. Stopping always requires an explicit `abort`; first-step failure remains fail-closed because the run has not entered accepted long-running service. Missing or short-window telemetry is `degraded`. `abort` verifies the bound PID run marker and tmux session before stopping anything.

`pull --diagnostic` also works for failed, aborted, or running workloads. It snapshots control logs/status and declared text outputs without changing workload state. Text is limited to the last 1 MiB per file and 16 MiB total; truncation is recorded in `snapshot.json`. Snapshots live under `<local_pull_root>/<run-id>/diagnostics/<snapshot-id>/` and coexist with a later normal pull. They are diagnostic evidence, not completion evidence. Normal pulls retain their declared-file size/SHA checks.

`local_pull_root` is the parent directory: rrctl appends the RunID exactly once. The research template sets it to `remote_artifacts/<ExpID>/`.

## Adapter Protocol

An adapter reads `rrctl.adapter.context.v1` JSON from stdin and emits one JSON object:

```json
{
  "protocol": "rrctl.adapter.v1",
  "healthy": true,
  "complete": false,
  "progress": {"completed": 10, "total": 100},
  "observations": {},
  "artifacts": []
}
```

Adapters run as isolated processes inside the project conda environment. Invalid JSON, nonzero exit, timeout, protocol mismatch, or inconsistent verdict fails closed.

## State

The remote control directory is authoritative and contains immutable RunSpec/binding evidence, atomic `status.json`, append-only events/health, console output, recovery metadata, and the final artifact manifest. Bindings separate stable source identity (`source_content_sha256`) from transferred bytes (`transport_bundle_sha256`), so a retry cannot become new reviewed source merely because RunID or packaging changed. Local state under `~/.local/state/rrctl/runs/` is only a rebuildable index.

## Development

Run these commands inside `.agents/harness/remote/rrctl/`:

```bash
python -m pip install -e .
ruff check .
python -m build
```

This distribution contains runtime code and contract validators. Project-specific completion semantics belong in project adapters.
