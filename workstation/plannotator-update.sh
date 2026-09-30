#!/usr/bin/env bash
# Keep the Plannotator CLI current.
#
# Plannotator has no self-update subcommand; the documented upgrade path is to
# re-run the vendor installer, which re-resolves the latest GitHub release,
# verifies its SHA-256, and atomically replaces the binary.
#
# --minimal touches ONLY the binary (no skills, hooks, agent config), which is
# what we want unattended. Run the installer without --minimal to refresh the
# agent integrations too.
set -uo pipefail

LOG="${PLANNOTATOR_UPDATE_LOG:-$HOME/.local/state/plannotator-update.log}"
BIN="${PLANNOTATOR_BIN:-$HOME/.local/bin/plannotator}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

{
  echo "=== $(date -Is) plannotator-update ==="
  echo "before: $("$BIN" --version 2>&1 | head -1)"
  if curl -fsSL https://plannotator.ai/install.sh | bash -s -- --non-interactive --minimal; then
    echo "after:  $("$BIN" --version 2>&1 | head -1)"
  else
    echo "installer failed with status $?"
  fi
} >>"$LOG" 2>&1
