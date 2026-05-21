# Publishing to PyPI

## Prerequisites

```bash
pip install build twine
```

API tokens: [pypi.org](https://pypi.org) and optionally [test.pypi.org](https://test.pypi.org).

## Build

```bash
pip install build
python -m build
```

Or run tests and build in one step:

```bash
chmod +x scripts/build-release.sh
./scripts/build-release.sh
```

Artifacts are written to `dist/`.

## Test install

```bash
pip install dist/yugabyte_decolocate_tables-*.whl
decolocate-tables --help
```

## Upload

TestPyPI:

```bash
twine upload --repository testpypi dist/*
```

Production:

```bash
twine upload dist/*
```

## Release checklist

1. Bump `__version__` in `src/decolocate_tables/__init__.py`
2. Add a `CHANGELOG.md` entry
3. Commit and tag: `git tag v0.1.0 && git push origin v0.1.0`
4. `./scripts/build-release.sh`
5. `twine upload dist/*` (or push tag `v0.2.0` to trigger `.github/workflows/release.yml`,
   which attaches `dist/*` to the GitHub Release)
6. Create a [GitHub release](https://github.com/ajcaldera1/yugabyte-decolocate-tables/releases) from the tag if not using the workflow
