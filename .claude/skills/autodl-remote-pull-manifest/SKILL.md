---
name: autodl-remote-pull-manifest
description: Generate a minimal remote artifact pull manifest (results.json + train/logs + eval logs/metrics/vis) and place files under research_workspace/experiments/<ExpID>/remote_artifacts.
---

# AutoDL Remote Pull Manifest

This is a mirror of the Codex skill at `.codex/skills/autodl-remote-pull-manifest/SKILL.md`.

Artifacts are pulled into `research_workspace/experiments/<ExpID>/remote_artifacts/`.

## Usage

### Key-based Authentication (Default)

```bash
python3 .codex/skills/autodl-remote-pull-manifest/scripts/generate_manifest.py \
  --expid "E2025xxxx-xx" \
  --exp-root "/root/autodl-tmp/output/experiments/<experiment_name>/<timestamp>" \
  --remote-host "<user@host>"
```

### Password-based Authentication (AutoDL Platform)

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

## References
- `docs/SKILLs/agent-skills-standard.md`
