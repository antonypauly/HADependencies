#!/usr/bin/env bash
# usage: list_wheels.sh OWNER/REPO OUT.tsv
#
# Writes one line per wheel: <tag>\t<filename>\t<download url>
# covering every ha-wheels-<version> release (newest release first).
# The ha-wheels-test release is skipped so test wheels never count as "built"
# and never reach the index.
#
# Needs GH_TOKEN in the environment (gh api).
set -euo pipefail

REPO="$1"
OUT="$2"
: > "$OUT"

# Assets are fetched per release via the paginated assets endpoint rather than
# from the "assets" array embedded in the release list, which can be truncated
# for releases with many wheels.
gh api --paginate "repos/${REPO}/releases?per_page=100" \
  --jq '.[]
        | select(.tag_name | startswith("ha-wheels-"))
        | select(.tag_name != "ha-wheels-test")
        | [.id, .tag_name] | @tsv' \
| while IFS=$'\t' read -r id tag; do
    gh api --paginate "repos/${REPO}/releases/${id}/assets?per_page=100" \
      --jq ".[]
            | select(.name | endswith(\".whl\"))
            | [\"${tag}\", .name, .browser_download_url] | @tsv" >> "$OUT"
  done

echo "Found $(wc -l < "$OUT") wheels across all releases" >&2
