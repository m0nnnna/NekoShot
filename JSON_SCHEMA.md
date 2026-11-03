# NekoShot `--json` output (schema 1)

Banners and progress are suppressed under `--json`; stdout carries one JSON document.

- `list`: array of `{name, path, created, hostname, mode, encrypted, distro, size_bytes, size_human}`
- `verify`: `{schema, status: success|failed, verified, image, encrypted, error?}`; exit code 0 on success. Verifying checks the `.sha256` sidecar, so no passphrase is needed for `.gpg` snapshots.
- `prune --force`: `{status, deleted, remaining}` (needs root)
- `schedule list`: `{cron_running, cron_file, jobs: [{name, schedule, command}]}` (`cron_file` is `/etc/systemd/system` with the timer backend)
- `status`: see `cmd_status` (reads `/run/nekoshot/<pid>.json`)

New fields are only ever added; existing fields keep their meaning.
