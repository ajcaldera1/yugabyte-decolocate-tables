# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-20

### Added

- Initial standalone PyPI package `yugabyte-decolocate-tables`
- CLI entry point `decolocate-tables` and `python -m decolocate_tables`
- Colocated-to-uncollocated migration with view recreation, parallel COPY, resume
- DDL adjustments: `COLOCATION = false`, `SPLIT INTO` tablets, PK `ASC` to `HASH`
