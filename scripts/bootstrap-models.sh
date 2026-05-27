#!/usr/bin/env bash
# Download BIRDEYE pretrained weights from Hugging Face into pipeline/models/
# and point the `latest` symlink at them.
#
# Usage:   ./scripts/bootstrap-models.sh
# Source:  https://huggingface.co/Shanit/BIRDEYE (main branch)
#
# Idempotent — safe to re-run. The destination version dir is read
# from the downloaded meta.json (e.g. pipeline/models/v_20260430_165141).

set -euo pipefail

REPO="Shanit/BIRDEYE"
BASE_URL="https://huggingface.co/${REPO}/resolve/main"
FILES=(
  presence_classifier.pt
  face_detector.pt
  eye_state_classifier.pt
  meta.json
)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="${ROOT}/pipeline/models"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

command -v curl    >/dev/null || { echo "ERROR: curl is required"    >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 is required" >&2; exit 1; }

echo "Downloading BIRDEYE weights from huggingface.co/${REPO} ..."
for f in "${FILES[@]}"; do
  printf "  %s ... " "$f"
  curl -fLsS -o "${TMP_DIR}/${f}" "${BASE_URL}/${f}"
  bytes=$(wc -c <"${TMP_DIR}/${f}" | tr -d ' ')
  printf "%s bytes\n" "$bytes"
done

VERSION="$(python3 -c "import json; print(json.load(open('${TMP_DIR}/meta.json'))['deployed_version'])")"
DEST="${MODELS_DIR}/${VERSION}"

mkdir -p "${DEST}"
for f in "${FILES[@]}"; do
  mv -f "${TMP_DIR}/${f}" "${DEST}/${f}"
done

( cd "${MODELS_DIR}" && ln -sfn "${VERSION}" latest )

echo
echo "OK: weights installed at ${DEST}"
echo "OK: pipeline/models/latest -> ${VERSION}"
