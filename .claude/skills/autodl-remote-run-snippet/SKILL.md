---
name: autodl-remote-run-snippet
description: Generate a copy-paste remote (AutoDL/tmux) run snippet (git fetch/checkout/pull + scripts/train_eval_shutdown_autodl.sh). Use when you need a remote train→eval command block and must manually choose train/eval scripts from scripts/.
---

# AutoDL Remote Run Snippet

This is a mirror of the Codex skill at `.codex/skills/autodl-remote-run-snippet/SKILL.md`.

## Usage
```bash
# List candidate scripts (manual selection required)
python3 .codex/skills/autodl-remote-run-snippet/scripts/generate_snippet.py

# Generate the snippet after choosing scripts
python3 .codex/skills/autodl-remote-run-snippet/scripts/generate_snippet.py \
  --branch "<branch>" \
  --train-script "scripts/<train_*.sh>" \
  --eval-script "scripts/<eval_*.sh>"
```

## References
- `docs/SKILLs/agent-skills-standard.md`
