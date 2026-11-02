#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="housing-quality-radar"
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
rm -rf "$STAGING_DIR/.git" "$STAGING_DIR/.local-evals" "$STAGING_DIR/runs" "$STAGING_DIR/.pytest_cache"
find "$STAGING_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +

LEGACY_DIR="$TARGET_ROOT/xiaohongshu-housing-radar"
if [[ -e "$LEGACY_DIR" ]]; then
  BACKUP_ROOT="$(dirname "$TARGET_ROOT")/skill-backups"
  mkdir -p "$BACKUP_ROOT"
  LEGACY_BACKUP="$(mktemp -d "$BACKUP_ROOT/xiaohongshu-housing-radar.XXXXXX")"
  rmdir "$LEGACY_BACKUP"
  mv "$LEGACY_DIR" "$LEGACY_BACKUP"
  printf 'Legacy skill name xiaohongshu-housing-radar moved to %s (renamed to %s in v0.3.0)\n' "$LEGACY_BACKUP" "$SKILL_NAME"
fi

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
