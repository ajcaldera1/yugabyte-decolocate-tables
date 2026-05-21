#!/usr/bin/env bash
# Build sdist + wheel for PyPI or GitHub Releases.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Running unit tests"
python -m pytest -q

echo "==> Cleaning prior artifacts"
rm -rf dist build *.egg-info src/*.egg-info

echo "==> Building release artifacts"
python -m pip install -q build
python -m build

echo "==> Built:"
ls -la dist/

VERSION="$(grep -E '^__version__' src/decolocate_tables/__init__.py | sed 's/.*= "\(.*\)"/\1/')"
echo ""
echo "Version: ${VERSION}"
echo "Test install: pip install dist/yugabyte_decolocate_tables-${VERSION}-py3-none-any.whl"
echo "Upload:       twine upload dist/*"
