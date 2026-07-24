# NekoShot on nekOS

nekOS is a custom desktop environment (Void Linux, running nested inside WSL2 via
Xephyr) — see [nekOS](https://github.com/m0nnnna/nekos). NekoShot runs there as an
ordinary CLI backup tool, installed the same way as the Alpine/Ubuntu targets but using
Void's `xbps` package manager instead of `apk`/`apt`.

## Install

```bash
git clone https://github.com/m0nnnna/NekoShot.git nekoshot
cd nekoshot
sudo sh nekos/install-nekos.sh
```

This installs to `/opt/nekoshot` and symlinks `nekoshot` onto `$PATH`, identical in
behavior to the Alpine/Ubuntu installers — see the main [README](../README.md) for usage,
commands, and configuration.

## How nekOS wires this in

nekOS's own repo (`provision/install-nekoshot.sh`) does the PATH symlink step itself: it
looks for `/opt/nekoshot/nekoshot.py`, and if present, `chmod +x`'s it and symlinks it to
`/usr/local/bin/nekoshot`. The expected flow is:

1. From the Windows host: sync this repo into the nekOS WSL distro at `/opt/nekoshot`
   (`robocopy N:\projects\nekoshot \\wsl.localhost\nekos-void\opt\nekoshot /E`).
2. nekOS's `start-session.sh` runs `install-nekoshot.sh` on every session start, which
   picks up the synced copy and wires the `nekoshot` command into `PATH`.

This `nekos/` folder's `install-nekos.sh` is the standalone equivalent for anyone running
NekoShot on Void Linux directly (nekOS or otherwise) without going through nekOS's own
session scripts.

**Note:** nekOS also ships a native GUI app called "Backups" (`nekos-shot`, a separate
small C/XCB program in the nekOS repo's own `shot/` folder). That is a different,
unrelated project that happens to share a similar name — NekoShot (this repo) is the
full-system/Docker/database imaging CLI tool documented in the main README.
