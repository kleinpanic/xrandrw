# xrandrw

[![PyPI version](https://img.shields.io/pypi/v/xrandrw.svg)](https://pypi.org/project/xrandrw/)
[![Python versions](https://img.shields.io/pypi/pyversions/xrandrw.svg)](https://pypi.org/project/xrandrw/)
[![License: MIT](https://img.shields.io/pypi/l/xrandrw.svg)](https://github.com/kleinpanic/xrandrw/blob/main/LICENSE)

Zero-config, self-healing X11 display-layout daemon for dwm/i3 and other
bare-X11 window managers. It watches for monitor hotplug/unplug, remembers each
display by its EDID identity, and auto-places externals relative to a primary
using a persistent attach-order policy — so your monitors end up where you left
them, every time.

Pure Python standard library (optional `systemd-python` for journald logging).

## Install

End-user (isolated, lands the `xrandrw` console-script at `~/.local/bin` — never
touches system Python):

```bash
pipx install xrandrw            # from PyPI
# or, from a checkout: pipx install .
```

Optional journald logging extra:

```bash
pipx install "xrandrw[journald]"
```

Development — always in a virtualenv, never a bare/root `pip`:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"         # editable + ruff, vulture, pytest, build
```

## Usage

`xrandrw` exposes six modes:

| Command | Description |
|---------|-------------|
| `xrandrw --apply` | Apply the layout once (default when no flag is given). |
| `xrandrw --watch` | Poll topology and re-apply on change. |
| `xrandrw --daemon` | Watch for display changes via event-driven RandR notifications and re-apply (systemd entry point). |
| `xrandrw --print` | Print `xrandr --query` output and exit. |
| `xrandrw --set-pref OUTPUT_OR_ID SIDE` | Set a display's preferred side: `right-of`, `left-of`, `above`, `below`. |
| `xrandrw --list-state` | Dump the placement state JSON. |

## Configuration

Config resolves from (lowest to highest precedence): built-in defaults →
`/etc/xdg/xrandrw.conf` → `~/.config/xrandrw.conf` → process environment. Each
key can be set in a config file (`KEY=value` lines) or via an environment
variable of the same name.

| Key | Purpose |
|-----|---------|
| `USE_XWALLPAPER` | `0` = feh/fehbg, `1` = xwallpaper. |
| `WALL` | Wallpaper image path. |
| `HIDPI_WIDTH` | Treat internal panel as HiDPI when preferred-mode width ≥ this. |
| `POLL_INTERVAL` | Watch-loop poll interval in seconds. |
| `LOG_LEVEL` | `none`, `err`, `info`, `notice`, `debug`. |
| `LOG_FILE` | Optional JSON-lines log file (unset = journald/stderr). |
| `LOCKFILE` | Apply-lock path. |
| `PREF_DEFAULT_SIDE` | Default side for new/unknown monitors. |
| `EXCESS_WINDOW_SEC` / `EXCESS_THRESHOLD` | Churn-backoff window and threshold. |

See [`xrandrw.conf.sample`](xrandrw.conf.sample) for an annotated template —
copy it to `~/.config/xrandrw.conf` and edit.

## systemd user service

The repo ships a `--user` unit that runs `xrandrw --daemon` with `Restart=always`
and `sd_notify` integration. Install and enable it via the Makefile:

```bash
make install    # pipx install . + copy conf sample + unit
make enable     # systemctl --user daemon-reload && enable --now
```

`make install` uses `pipx install --force .`, which places the console-script at
`~/.local/bin/xrandrw` — exactly the `ExecStart` path the unit expects. Run
`systemctl --user daemon-reload` after any reinstall.

## License

MIT — see [LICENSE](LICENSE).
