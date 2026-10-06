#!/usr/bin/env bash
# Publish agenthub to TestPyPI (then PyPI) with uv.
#
# Credentials: uv reads UV_PUBLISH_TOKEN. To avoid pasting a token in a shell
# history or a chat, keep it in a file and source it:
#
#   install -m 600 /dev/null ~/.config/agenthub/testpypi_token
#   # paste the token (starts with pypi-) into that file, then:
#   export UV_PUBLISH_TOKEN="$(cat ~/.config/agenthub/testpypi_token)"
#
# TestPyPI does NOT accept tokens issued by pypi.org: create a separate token
# at https://test.pypi.org/manage/account/token/
set -euo pipefail

TARGET="${1:-testpypi}"
cd "$(dirname "$0")/.."

echo "[publish] running tests"
uv run pytest tests/ -q

echo "[publish] building sdist + wheel"
rm -rf dist/
uv build

case "$TARGET" in
  testpypi)
    TOKEN_FILE="${TESTPYPI_TOKEN_FILE:-$HOME/.config/agenthub/testpypi_token}"
    if [[ -z "${UV_PUBLISH_TOKEN:-}" && -f "$TOKEN_FILE" ]]; then
      UV_PUBLISH_TOKEN="$(cat "$TOKEN_FILE")"
      export UV_PUBLISH_TOKEN
    fi
    if [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
      echo "[publish] no token: export UV_PUBLISH_TOKEN or write $TOKEN_FILE" >&2
      exit 1
    fi
    echo "[publish] uploading to TestPyPI"
    uv publish --publish-url https://test.pypi.org/legacy/ \
      --username __token__ --password "$UV_PUBLISH_TOKEN" dist/*
    echo "[publish] verify install:"
    echo "  uv venv /tmp/agenthub-tp && uv pip install --python /tmp/agenthub-tp \\"
    echo "    --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ agenthub"
    ;;
  pypi)
    if [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
      echo "[publish] export UV_PUBLISH_TOKEN (pypi.org token)" >&2
      exit 1
    fi
    echo "[publish] uploading to PyPI"
    uv publish --username __token__ --password "$UV_PUBLISH_TOKEN" dist/*
    ;;
  *)
    echo "usage: $0 [testpypi|pypi]" >&2
    exit 2
    ;;
esac
