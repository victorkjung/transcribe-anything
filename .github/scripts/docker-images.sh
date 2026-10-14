#!/usr/bin/env bash
# Select only registries this repository is configured to publish to.
set -euo pipefail
: "${GITHUB_REPOSITORY:?repository required}"
: "${GITHUB_OUTPUT:?output file required}"
dockerhub_enabled=false
if [[ "${GITHUB_EVENT_NAME:-}" != pull_request && -n "${DOCKER_USERNAME:-}" && -n "${DOCKER_PASSWORD:-}" ]]; then
  # A Docker Hub username is public, but must not inject additional output lines.
  [[ "$DOCKER_USERNAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { printf 'Invalid Docker Hub namespace\n' >&2; exit 1; }
  dockerhub_enabled=true
fi
{
  printf 'dockerhub_enabled=%s\n' "$dockerhub_enabled"
  printf 'images<<CONTAINER_IMAGES\n'
  printf 'ghcr.io/%s\n' "${GITHUB_REPOSITORY,,}"
  if [[ "$dockerhub_enabled" == true ]]; then
    printf '%s/%s\n' "$DOCKER_USERNAME" "${GITHUB_REPOSITORY##*/}"
  fi
  printf 'CONTAINER_IMAGES\n'
} >> "$GITHUB_OUTPUT"
