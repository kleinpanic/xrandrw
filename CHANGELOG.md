# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-05

Initial packaged baseline. This tags the still-hardcoded, as-yet-untested code
as an honest v0.1.0 starting point so the versioning scheme is established
immediately; later releases bump toward v1.0 as hardening, native X11, device
profiles, and tests land.

### Added
- PEP 621 `pyproject.toml` (setuptools backend, src-layout) declaring the
  `xrandrw = xrandrw.cli:main` console-script.
- MIT `LICENSE`.
- `README.md` documenting install, the six CLI modes, config keys, and the
  systemd user-service setup.
- `journald` optional-dependency extra for `systemd-python`.

### Changed
- `xrandrw.py` monolith split into the `src/xrandrw/` package (8 submodules).
- Makefile `install` target now runs `pipx install --force .` instead of copying
  a binary into `~/.local/bin`.
