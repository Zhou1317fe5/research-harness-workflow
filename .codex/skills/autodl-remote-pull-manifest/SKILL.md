---
name: autodl-remote-pull-manifest
description: Generate a minimal remote artifact pull manifest (results.json + train/logs + eval logs/metrics/vis) and place files under research_workspace/experiments/<ExpID>/remote_artifacts.
metadata:
  short-description: Remote artifact pull manifest
---

# AutoDL Remote Pull Manifest

Generate a shell script that pulls the **minimal required artifacts** from a remote EXP_ROOT into:
`research_workspace/experiments/<ExpID>/remote_artifacts/`.

## Prerequisites

**Required before running this skill:**
- Remote training/evaluation must be completed
- You must have SSH access to the remote server
- You must know the remote `EXP_ROOT` path (printed during training)

**Consumed by (downstream skills):**
- `exp-results-ingest-local` - Ingests pulled artifacts into 00-实验记录.md
- `exp-analysis-hen` - Analyzes pulled artifacts and generates H→E→N report

## Execution Order

```
1. autodl-remote-run-snippet (generate remote commands)
2. [Execute on remote server]
3. autodl-remote-pull-manifest (this skill) ← Pull artifacts to local
4. exp-results-ingest-local (ingest into 00-实验记录.md)
5. exp-analysis-hen (generate analysis report)
```

## Minimal pull set (fixed)
- `${EXP_ROOT}/results.json` (optional; warn+skip if missing)
- `${EXP_ROOT}/train_console.log` (optional)
- `${EXP_ROOT}/eval_console.log` (optional)
- `${EXP_ROOT}/configs/**` (optional; warn+skip if missing)
- `${EXP_ROOT}/train/logs/**` (optional; warn+skip if missing)
- `${EXP_ROOT}/eval/**/logs/**`
- `${EXP_ROOT}/eval/**/metrics/**`
- `${EXP_ROOT}/eval/**/vis/**`

If `eval/` contains multiple runs and you do not specify `--eval-dir`, the script pulls **all** eval directories under `eval/*/*` (ordered by remote mtime, newest first).
It also excludes `**/vis/_cache/` when pulling `vis/`.

## Configuration

### Using .env File (Recommended)

Create a `.env` file in `.codex/skills/autodl-remote-pull-manifest/scripts/`:

```bash
# Copy from .env.example
cp .codex/skills/autodl-remote-pull-manifest/scripts/.env.example \
   .codex/skills/autodl-remote-pull-manifest/scripts/.env

# Edit with your credentials
nano .codex/skills/autodl-remote-pull-manifest/scripts/.env
```

Example `.env` content:

```bash
# SSH connection string (full command format)
REMOTE_HOST=ssh -p <port> root@your-remote-host

# SSH password (optional, leave empty for key-based auth)
SSH_PASSWORD=your_actual_password
```

**Security:**
- `.env` is automatically ignored by git (see `.gitignore`)
- Set file permissions: `chmod 600 .codex/skills/autodl-remote-pull-manifest/scripts/.env`
- Never commit `.env` to version control

## Usage

### With .env Configuration (Simplest)

Once `.env` is configured, you only need to specify experiment details:

```bash
python3 .codex/skills/autodl-remote-pull-manifest/scripts/generate_manifest.py \
  --expid "E2025xxxx-xx" \
  --exp-root "/root/autodl-tmp/output/experiments/<experiment_name>/<timestamp>" \
  --write-script
```

The script will automatically read `REMOTE_HOST` and `SSH_PASSWORD` from `.env`.

### Key-based Authentication (Override .env)

```bash
python3 .codex/skills/autodl-remote-pull-manifest/scripts/generate_manifest.py \
  --expid "E2025xxxx-xx" \
  --exp-root "/root/autodl-tmp/output/experiments/<experiment_name>/<timestamp>" \
  --remote-host "<user@host>"
```

### Password-based Authentication (Override .env)

For AutoDL platform with changing SSH configs:

```bash
python3 .codex/skills/autodl-remote-pull-manifest/scripts/generate_manifest.py \
  --expid "E2025xxxx-xx" \
  --exp-root "/root/autodl-tmp/output/experiments/<experiment_name>/<timestamp>" \
  --remote-host "ssh -p <port> root@your-remote-host" \
  --password "your_password" \
  --write-script
```

**Security Notes:**
- Password will be stored in the generated script with restricted permissions (chmod 700)
- Requires `sshpass` tool: `apt-get install -y sshpass` or `brew install sshpass`
- Delete the script after use or store in a secure location
- Avoid committing the script to version control

Notes:
- `--remote-host` supports either `user@host` (recommended) or a full ssh form like `ssh -p 36485 user@host` (the generator will extract host + ssh options).
- Command-line arguments override `.env` defaults.
