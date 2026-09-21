#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="xiaohongshu-housing-radar"
TARGET_ROOT="${1:-${CODEX_HOME:-$HOME/.codex}/skills}"
TARGET_DIR="$TARGET_ROOT/$SKILL_NAME"
STAGING_ROOT="$(mktemp -d)"
STAGING_DIR="$STAGING_ROOT/$SKILL_NAME"

cleanup() {
  rm -rf "$STAGING_ROOT"
}
trap cleanup EXIT

mkdir -p "$TARGET_ROOT"
mkdir -p "$STAGING_DIR"
cp -R "$SOURCE_DIR/." "$STAGING_DIR"
rm -rf "$STAGING_DIR/.git" "$STAGING_DIR/.local-evals" "$STAGING_DIR/runs"

if [[ -e "$TARGET_DIR" ]]; then
  BACKUP_ROOT="$(dirname "$TARGET_ROOT")/skill-backups"
  mkdir -p "$BACKUP_ROOT"
  BACKUP_DIR="$(mktemp -d "$BACKUP_ROOT/$SKILL_NAME.XXXXXX")"
  rmdir "$BACKUP_DIR"
  mv "$TARGET_DIR" "$BACKUP_DIR"
  printf 'Previous installation moved to %s\n' "$BACKUP_DIR"
fi
mv "$STAGING_DIR" "$TARGET_DIR"

printf 'Installed %s to %s\n' "$SKILL_NAME" "$TARGET_DIR"
