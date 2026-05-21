# Publishing to PyPI

## Prerequisites

```bash
pip install build twine
```

API tokens: [pypi.org](https://pypi.org) and optionally [test.pypi.org](https://test.pypi.org).

## Build

```bash
python -m build
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
4. `python -m build && twine upload dist/*`
5. Create a [GitHub release](https://github.com/ajcaldera1/yugabyte-decolocate-tables/releases) from the tag
