# NekoShot

```
    /\_/\
   ( o.o )  NekoShot
    > ^ <   Purrfect System Snapshots ~nya!
   /|   |\
  (_|   |_)
```

A neko-powered live system imaging tool for Alpine and Ubuntu servers running Docker. Capture purrfect system snapshots while the system is running, and restore them to the same or different machines ~nya!

## Features

- **Live Capture** - Pounce on running systems without downtime
- **Live Restore (Default)** - Restore directly to a running system, no live USB needed
- **Docker-Aware** - Backs up Docker images, volumes, and compose files
- **Docker-Only Mode** - Snapshot just your Docker stack, skipping the rest of the system
- **Database Dumps** - Finds and dumps PostgreSQL, MySQL/MariaDB, SQL Server and SQLite
- **Interactive Menu** - Run `nekoshot` with no arguments and pick what you want
- **Hostname-Dated Archives** - Snapshots named with hostname and timestamp for easy identification
- **Incremental Backups** - Save space by only stashing changes since last snapshot
- **Fast Compression** - Uses zstd to squish snapshots efficiently
- **Progress and ETAs** - Per-phase progress bars with live throughput and time remaining
- **Headless Backups** - Detach and walk away; check progress from any terminal with `nekoshot status`
- **Bootloader Repair** - Automatically reinstalls GRUB or syslinux on chroot restore
- **Automation Ready** - JSON output, exit codes, and hook support for scripting

## Requirements

- Python 3.8+
- rsync
- zstd (recommended) or gzip
- tar
- Root privileges for capture/restore operations

## Installation

### Ubuntu/Debian

```bash
git clone <repository> nekoshot
cd nekoshot
sudo bash install-ubuntu.sh
```

### Alpine Linux

```bash
git clone <repository> nekoshot
cd nekoshot
sudo sh install-alpine.sh
```

### Manual Installation

```bash
# Install dependencies
# Ubuntu/Debian:
sudo apt install python3 rsync zstd tar

# Alpine:
sudo apk add python3 rsync zstd tar

# Copy files
sudo mkdir -p /opt/nekoshot
sudo cp -r *.py capture/ restore/ utils/ /opt/nekoshot/
sudo chmod +x /opt/nekoshot/nekoshot.py

# Create symlink
sudo ln -s /opt/nekoshot/nekoshot.py /usr/local/bin/nekoshot
```

## Quick Start

### Just Run It

Not sure which command you want? Run NekoShot with no arguments and pick:

```
$ sudo nekoshot

    /\_/\
   ( o.o )  NekoShot
    > ^ <   What shall we do, nya?

  What would you like to do?

    1) Back up now
    2) Set up a scheduled backup
    3) Show or remove scheduled backups
    4) Check on a running backup
    5) List snapshots
    6) Restore a snapshot
    0) Quit

  Choice: 1

  What would you like to back up?

    1) The whole system (files, Docker, metadata)
    2) Docker only (images, volumes, compose files)
    3) Databases only (Postgres, MySQL, SQL Server, SQLite)
    0) Back

  Choice: 3
  Where should it go? (each kind keeps its own folder) [/backup/nekoshot/sql]:
  Any SQLite files to include? (comma separated, blank for none):
  Run it in the background so you can close this terminal? [y/N]: y

  Running: nekoshot sql --output /backup/nekoshot/sql --headless
```

The menu builds an ordinary NekoShot command and shows it to you before running it,
so you can learn the flag once and skip the menu next time. Scheduling uses the same
commands, installed into cron.

The menu only appears on a terminal - piped or scripted invocations print help as
before, so nothing hangs waiting for input.

### Capture a Snapshot

```bash
# Basic capture to /backup directory
sudo nekoshot capture --output /backup/

# Capture with Docker containers pawsed (for consistency)
sudo nekoshot capture --output /backup/ --pause-docker

# Capture without Docker
sudo nekoshot capture --output /backup/ --no-docker

# Capture ONLY Docker (images, volumes, compose) - no system files
sudo nekoshot capture --output /backup/ --docker-only

# Trim junk images first so the backup is smaller
sudo nekoshot capture --output /backup/ --docker-only --prune-images       # dangling only
sudo nekoshot capture --output /backup/ --docker-only --prune-images all    # every unused image
```

Snapshots are named with your hostname and timestamp: `myserver-20240120-143022.tar.zst`

Docker-only snapshots get a `.docker` marker: `myserver-20240120-143022.docker.tar.zst`

### Restore a Snapshot

**Live restore (default)** - restores directly to the running system:

```bash
# Restore to the running system (default behavior)
sudo nekoshot restore --image /backup/image-20240115-120000/
```

**Chroot restore** - for bare-metal recovery from live USB:

```bash
# 1. Boot from live USB/rescue mode
# 2. Mount target disk
sudo mount /dev/sda2 /mnt/restore
sudo mount /dev/sda1 /mnt/restore/boot  # if separate boot partition

# 3. Restore the image with --target
sudo nekoshot restore \
  --image /backup/image-20240115-120000/ \
  --target /mnt/restore \
  --fix-bootloader /dev/sda

# 4. Unmount and reboot
sudo umount -R /mnt/restore
sudo reboot
```

## Progress and ETAs

Capture and restore report progress per phase, with throughput and time remaining:

```
    /\_/\  ~~~
   ( ^.^ ) < Capturing your system, hold still!
    > ~ <

[1/6] Capturing system files: done in 04:31 (12.4 GB)   |   overall  17%, elapsed 04:31
[2/6] Saving 3 Docker images   [-------#----]         2.1 GB   64.2 MB/s
[3/6] Backing up 4 Docker~ [#########---]   74%   3.2 GB / 4.4 GB   88.1 MB/s   ETA 00:14
```

Each phase measures what it can actually see, so the numbers mean something:

| Phase | Progress source | ETA? |
|-------|-----------------|------|
| System files (rsync) | rsync's own byte counter | Yes |
| Saving Docker images | bytes written to `images.tar` | No - see below |
| Docker volumes | volume sizes measured from their mountpoints | Yes |
| Compose scan | pulses while walking; counts files found | No |
| Compression | input bytes consumed, via tar checkpoints | Yes |
| Checksum | bytes hashed against the archive size | Yes |
| Restore: unpacking | how far the archive has been read | Yes |
| Restore: loading images | how far `docker load` has read `images.tar` | Yes |
| Restore: volumes | advances per volume restored | Rough |

`docker save` writes compressed blobs, so the sizes reported by `docker images`
overstate the resulting archive by roughly 2.5x. Rather than show an ETA built on a
total that's wrong, that phase reports bytes written and throughput only.

**Not a terminal?** Bars need a TTY. Under `--json`, when output is redirected to a
file, or with `--no-progress`, the same updates become periodic log lines instead, so
cron logs stay readable:

```
2026-07-16 02:15:04 [INFO] nekoshot.progress: [5/6] Squishing into archive: starting (27.0 GB)
2026-07-16 02:15:19 [INFO] nekoshot.progress: [5/6] Squishing into archive: 46%   12.4 GB / 27.0 GB   118.3 MB/s   ETA 02:04
2026-07-16 02:17:26 [INFO] nekoshot.progress: [5/6] Squishing into archive: done in 02:22 (27.0 GB)   |   overall  83%, elapsed 12:04
```

| Option | Description |
|--------|-------------|
| `--no-progress` | Never render bars; log periodic progress lines instead |
| `--json` | Implies `--no-progress` (bars would corrupt the JSON stream) |

## Trimming Images Before a Backup

A busy Docker host accumulates images you no longer need - leftover layers from
rebuilds, old tags, images for containers you've since removed. They bloat the
`docker save` (the largest part of a Docker backup) and can even be what makes a save
stall. `--prune-images` clears them out first:

```bash
sudo nekoshot capture --docker-only --prune-images        # dangling (untagged) only
sudo nekoshot capture --docker-only --prune-images all    # every image no container uses
```

| Level | Runs | Removes |
|-------|------|---------|
| `--prune-images` | `docker image prune -f` | Dangling (untagged) images - the usual rebuild cruft. Safe. |
| `--prune-images all` | `docker image prune -a -f` | Every image not used by a container, running **or** stopped. Aggressive. |

**This is destructive.** It's the same `docker image prune` you'd run by hand: the
images are deleted from Docker, not merely left out of the archive. It never removes an
image a container is using, and it never touches your containers or volumes.

Be careful with `all`: it also removes **locally-built images that aren't in any
registry** (anything you made with `docker build` and aren't currently running). Those
cannot be re-pulled - if you don't have the Dockerfile to rebuild them, they're gone for
good. Prefer plain `--prune-images` (dangling only) unless you're certain every unused
image can be recreated. The menu asks for an extra confirmation before the `all` level.

## Database Backups

```bash
# Dump every database NekoShot can find
sudo nekoshot sql --output /backup/sql/

# Include SQLite files (they can't be auto-discovered - see below)
sudo nekoshot sql --sqlite /srv/app/data.db --sqlite /srv/other/app.sqlite

# Only containers, skip anything installed on the host
sudo nekoshot sql --no-native

# Skip a particular server
sudo nekoshot sql --exclude staging-postgres
```

Databases are **found, not configured**. Containers are matched by image name and
dumped through `docker exec`; servers installed on the host are dumped through the
local socket.

| Engine | How it's dumped | Matched images |
|--------|-----------------|----------------|
| PostgreSQL | `pg_dumpall --clean --if-exists` (all databases + roles) | postgres, postgis, timescale, pgvector |
| MySQL / MariaDB | `mysqldump --all-databases --single-transaction` (falls back to `mariadb-dump`) | mysql, mariadb, percona |
| SQL Server | `BACKUP DATABASE` per user database, then the `.bak` files are copied out | mssql, azure-sql-edge, sqlserver |
| SQLite | Python's `sqlite3` online backup API - consistent even while the app writes | *(configured paths only)* |

The result is a snapshot just like any other, named with a `.sql` marker:
`myserver-20240120-143022.sql.tar.zst`. It works with `list`, `verify` and `prune`.

### Credentials

NekoShot doesn't store database passwords. It reads them from the place they already
exist - the container's own environment:

| Engine | Read from |
|--------|-----------|
| PostgreSQL | `POSTGRES_USER`, `POSTGRES_PASSWORD` (or `PGPASSWORD`) |
| MySQL / MariaDB | `MYSQL_ROOT_PASSWORD`, `MARIADB_ROOT_PASSWORD`, or `MYSQL_USER` + `MYSQL_PASSWORD` |
| SQL Server | `MSSQL_SA_PASSWORD` or `SA_PASSWORD` |

For servers running on the host, dumps go through the local socket, which normally
trusts root (`sudo -u postgres pg_dumpall`, root over the MySQL socket). Set
`PGPASSWORD` / `MYSQL_PWD` / `MSSQL_SA_PASSWORD` in the environment if yours needs
one. A native SQL Server with no password available is reported and skipped rather
than failing the whole run.

Passwords are passed to the dump process as environment variables and are never
written to the snapshot, the status file, or the log.

### Restoring a dump

`nekoshot restore` will **not** load dumps back automatically. Restoring a database
means overwriting a live one, and every engine wants it done differently - so pointing
`restore` at a `.sql` snapshot prints the commands for each engine it contains and
leaves the doing to you.

```bash
# Unpack the snapshot
tar -xf myserver-20240120-143022.sql.tar.zst      # or: zstd -d -c <snap> | tar -x

# PostgreSQL
docker exec -i my-postgres psql -U postgres < myserver-postgres-all.sql

# MySQL / MariaDB
docker exec -i my-mariadb mariadb -u root -p<pw> < myserver-mysql-all.sql

# SQLite
cp app.sqlite /srv/app/data.db

# SQL Server - the chown is required: `docker cp` puts the file in as root, and
# SQL Server runs as the mssql user, so without it the restore fails with
# "Operating system error 5(Access is denied.)"
docker cp myserver-nekodb.bak my-mssql:/var/opt/mssql/backup/restore.bak
docker exec -u 0 my-mssql chown mssql /var/opt/mssql/backup/restore.bak
docker exec my-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P '<pw>' -C \
  -Q "RESTORE DATABASE [nekodb] FROM DISK=N'/var/opt/mssql/backup/restore.bak' WITH REPLACE"
```

If one server fails to dump, the others still get backed up; the failure is logged and
counted, and the run only fails outright if nothing at all could be dumped.

| Option | Description |
|--------|-------------|
| `--output, -o` | Where to stash the dumps (default: the configured `sql` location) |
| `--sqlite` | Path to a SQLite file (repeatable) |
| `--no-docker` | Don't look for databases in containers |
| `--no-native` | Don't look for databases on the host |
| `--exclude, -e` | Container or server name to skip (repeatable) |
| `--headless` | Run in the background (see [Headless Backups](#headless-backups)) |
| `--compression` | `zstd`, `gzip` or `none` |

## Sorted Backup Locations

Each kind of backup keeps its own folder, so snapshots sort themselves:

```
/backup/nekoshot/
├── system/   myserver-20240120-020000.tar.zst
├── docker/   myserver-20240121-020000.docker.tar.zst
└── sql/      myserver-20240121-013000.sql.tar.zst
```

These are the defaults, derived from `capture.output_dir`. Point any of them somewhere
else in the config:

```json
{
  "capture": { "output_dir": "/backup/nekoshot" },
  "locations": {
    "system": "/mnt/big-disk/system",
    "docker": "/backup/nekoshot/docker",
    "sql": "/mnt/fast-ssd/db-dumps"
  }
}
```

`--output` always wins over both. `nekoshot list --path /backup/nekoshot` looks one
level down, so it finds all three kinds in one listing.

Prune each kind on its own schedule - a plain `--keep` counts every kind together:

```bash
sudo nekoshot prune --path /backup/nekoshot/sql --keep 14 --force
sudo nekoshot prune --path /backup/nekoshot/system --keep 4 --force
```

## Scheduled Backups

The menu writes cron entries for you (option 2), or manage them directly:

```
  Scheduled backups (from /etc/cron.d/nekoshot):

    docker-daily
      when: every day at 02:00   (0 2 * * *)
      runs: /usr/local/bin/nekoshot capture --docker-only --output /backup/nekoshot/docker

    sql-daily
      when: every day at 02:00   (0 2 * * *)
      runs: /usr/local/bin/nekoshot sql --output /backup/nekoshot/sql
```

Entries are written to `/etc/cron.d/nekoshot` on Ubuntu/Debian, or `/etc/crontabs/root`
on Alpine (busybox crond ignores `/etc/cron.d`, so an entry there would look installed
and never run). Each job is labelled with a `# nekoshot:<name>` marker and replaced by
name, so re-running the menu never stacks duplicates. NekoShot also warns you if no
cron daemon is running to execute them.

## Headless Backups

Start a backup, get your prompt back, and check on it whenever you like:

```bash
# Detaches and returns immediately
sudo nekoshot capture --output /backup/ --headless

Off it goes ~nya! Backup running headless with pid 8853
  log:      /var/log/nekoshot/capture-20240120-143022.log
  progress: nekoshot status
```

Then from any terminal - including a different SSH session:

```bash
$ nekoshot status

  /\_/\
 ( o.o )  NekoShot Status
  > ^ <

  capture (docker-only) on myserver   RUNNING   pid 8853
  [5/6] Squishing into archive  [##########--------------]  44%  12.4 GB / 27.0 GB  118.3 MB/s  ETA 02:04
  overall  72%   elapsed 08:31   started 2024-01-20T14:30:22Z
  output: /backup
  log: /var/log/nekoshot/capture-20240120-143022.log
```

`nekoshot status` reports on **any** run, not just headless ones - foreground and cron
backups publish the same progress. It only reads, so it needs no root.

```bash
nekoshot status              # current run, or the last one's result
nekoshot status --watch      # refresh until the backup finishes
nekoshot status --all        # every run from the last 24h
nekoshot --json status       # machine-readable, for monitoring
```

Exit codes: `0` running or completed cleanly, `1` if the most recent run failed or died.

| Option | Description |
|--------|-------------|
| `--headless` | Detach and run in the background (capture only) |
| `--log` | Log file for a headless run (default: `/var/log/nekoshot/capture-TIMESTAMP.log`) |
| `--watch, -w` | Keep refreshing status until the run finishes |
| `--interval, -i` | Seconds between refreshes with `--watch` (default: 2) |
| `--all, -a` | Show all recent runs rather than just the current one |

### Where the status lives

Progress is published to `/run/nekoshot/<pid>.json`, written atomically so a reader
never sees a half-written file. `/run` is the conventional home for runtime state and
is cleared on reboot, which is what you want for "is a backup happening right now".
Records of finished runs stick around for 24 hours so `status` can report the last
result, then the next run cleans them up.

Set `NEKOSHOT_RUN_DIR` to override the location; `/var/run/nekoshot` and
`/tmp/nekoshot` are used as fallbacks if `/run` isn't available.

A backup that is killed (or a machine that reboots mid-backup) leaves a record with no
process behind it. `status` detects this and reports it as `STALE` rather than claiming
a backup is still running:

```
  capture (full) on myserver   STALE   pid 8853
  the process is gone - it was killed or the machine rebooted mid-backup
```

### Monitoring a headless backup

```bash
# Alert if the last run didn't finish cleanly
nekoshot status > /dev/null || echo "NekoShot backup needs attention"

# Pull the current phase and ETA for a dashboard
nekoshot --json status | jq -r '.[0] | "\(.state) \(.overall_percent)% eta=\(.phase.eta_seconds)s"'
```

## Commands

### capture

Capture a live system snapshot.

```bash
sudo nekoshot capture [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--output, -o` | Output directory for the snapshot |
| `--compression` | Compression type: `zstd`, `gzip`, or `none` |
| `--exclude, -e` | Additional patterns to exclude (can be repeated) |
| `--pause-docker` | Pawse Docker containers during capture |
| `--no-docker` | Skip Docker capture entirely |
| `--docker-only` | Capture only Docker state, skipping system files |
| `--prune-images [all]` | Delete unused images before saving to shrink the backup (see [Trimming Images](#trimming-images-before-a-backup)) |
| `--headless` | Run in the background and return immediately (see [Headless Backups](#headless-backups)) |
| `--log` | Log file for a headless run |
| `--incremental` | Create incremental backup |
| `--link-dest` | Previous backup path for incremental mode |

**Examples:**

```bash
# Basic capture
sudo nekoshot capture --output /backup/

# Exclude additional directories
sudo nekoshot capture --output /backup/ \
  --exclude "/home/*/Downloads" \
  --exclude "/var/log/*"

# Incremental backup (only stores changed files)
sudo nekoshot capture --output /backup/ \
  --incremental \
  --link-dest /backup/image-20240115-120000/

# Use gzip instead of zstd
sudo nekoshot capture --output /backup/ --compression gzip

# Docker-only: images, volumes and compose files, no rootfs
sudo nekoshot capture --output /backup/ --docker-only

# Docker-only with containers pawsed for a consistent volume backup
sudo nekoshot capture --output /backup/ --docker-only --pause-docker
```

#### Docker-Only Capture

`--docker-only` snapshots just the Docker stack: images, volumes, compose files,
daemon config and network definitions. No rootfs is rsynced, so these snapshots are
much smaller and faster than a full system capture - handy for frequent backups of a
Docker host whose OS can be rebuilt from scratch.

A docker-only snapshot contains no system files, no packages list and no bootloader
config, so it **cannot** rebuild a machine on its own. Keep taking periodic full
snapshots if you need bare-metal recovery.

Notes:
- `--docker-only` and `--no-docker` are mutually exclusive.
- If Docker isn't available the capture fails rather than writing an empty snapshot.
- `--incremental` only applies to system files and is ignored in docker-only mode.

### restore

Restore a system snapshot. **Live restore is the default** - it restores directly to the running system. Use `--target` for chroot restore to a mounted disk.

Docker-only snapshots are detected automatically: NekoShot restores their Docker state and leaves the host system alone.

```bash
sudo nekoshot restore [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--image, -i` | Path to snapshot directory (required) |
| `--target, -t` | Chroot restore to mounted target. Without this, restores to live system |
| `--fix-bootloader` | Device to install bootloader (e.g., `/dev/sda`) [chroot mode only] |
| `--bootloader-type` | Force bootloader type: `grub` or `syslinux` [chroot mode only] |
| `--skip-docker` | Skip Docker restoration |
| `--update-fstab` | Copy fstab from backup [chroot mode only] |
| `--update-initramfs` | Regenerate initramfs after restore [chroot mode only] |

**Examples:**

```bash
# Live restore to running system (default)
sudo nekoshot restore --image /backup/image-20240115-120000/

# Live restore without Docker
sudo nekoshot restore --image /backup/image-20240115-120000/ --skip-docker

# Restore a docker-only snapshot (Docker state only, host untouched)
sudo nekoshot restore --image /backup/myserver-20240120-143022.docker.tar.zst

# Chroot restore with bootloader repair
sudo nekoshot restore \
  --image /backup/image-20240115-120000/ \
  --target /mnt/restore \
  --fix-bootloader /dev/sda \
  --update-fstab \
  --update-initramfs
```

### status

Show the progress and ETA of a running backup, or the result of the last one.
Reads only, so it needs no root. See [Headless Backups](#headless-backups).

```bash
nekoshot status [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--watch, -w` | Refresh until the backup finishes |
| `--interval, -i` | Seconds between refreshes with `--watch` (default: 2) |
| `--all, -a` | Show every run from the last 24 hours |
| `--json` | Output as JSON (global flag: `nekoshot --json status`) |

### list

List available snapshots.

```bash
nekoshot list [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--path, -p` | Path to snapshot stash |
| `--json` | Output as JSON |

**Example:**

```bash
$ nekoshot list --path /backup/

  /\_/\
 ( o.o )  NekoShot Snapshot Stash
  > ^ <   /backup/

--------------------------------------------------------------------------------------------
Name                                     Created                Host           Type         Size
--------------------------------------------------------------------------------------------
myserver-20240120-143022.docker.tar.zst  2024-01-20 14:30:22    myserver       docker-only  0.4 GB
myserver-20240115-120000.tar.zst         2024-01-15 12:00:00    myserver       full         1.1 GB
myserver-20240110-090000.tar.zst         2024-01-10 09:00:00    myserver       full         1.0 GB
--------------------------------------------------------------------------------------------
Total: 3 snapshot(s) ~nya
```

### verify

Sniff-check snapshot integrity using checksums.

```bash
nekoshot verify --image /backup/image-20240115-120000/
```

### prune

Knock old snapshots off the shelf based on retention policy.

```bash
sudo nekoshot prune [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--path, -p` | Path to snapshot stash |
| `--keep, -k` | Number of snapshots to keep (default: 5) |
| `--mode` | Only prune snapshots of one type: `all`, `full`, or `docker-only` |
| `--force, -f` | Skip confirmation prompt |

**Examples:**

```bash
# Keep last 5 snapshots, prompt before deleting
sudo nekoshot prune --path /backup/ --keep 5

# Keep last 3 snapshots, no prompt
sudo nekoshot prune --path /backup/ --keep 3 --force

# Keep the last 7 docker-only snapshots, leaving full snapshots alone
sudo nekoshot prune --path /backup/ --keep 7 --mode docker-only --force
```

If you mix frequent docker-only snapshots with occasional full ones in the same
directory, use `--mode` when pruning. A plain `--keep 7` counts both types together,
so a run of nightly docker-only snapshots can knock your full snapshots off the shelf.

## Configuration

NekoShot looks for configuration in these locations (in order):
1. Path specified with `--config`
2. `/etc/nekoshot.conf`
3. `/etc/nekoshot/config.json`
4. `~/.nekoshot.conf`
5. `~/.config/nekoshot/config.json`

### Example Configuration

```json
{
  "locations": {
    "system": "/backup/nekoshot/system",
    "docker": "/backup/nekoshot/docker",
    "sql": "/backup/nekoshot/sql"
  },
  "sql": {
    "include_docker": true,
    "include_native": true,
    "sqlite_paths": ["/srv/app/data.db"],
    "exclude": ["staging-postgres"],
    "compression": "zstd"
  },
  "capture": {
    "output_dir": "/backup/nekoshot",
    "compression": "zstd",
    "compression_level": 3,
    "include_docker": true,
    "docker_only": false,
    "pause_docker": false,
    "exclude_patterns": [
      "/proc/*",
      "/sys/*",
      "/dev/*",
      "/run/*",
      "/tmp/*",
      "/var/cache/*"
    ]
  },
  "restore": {
    "fix_bootloader": true,
    "restore_docker": true,
    "update_fstab": true
  },
  "retention": {
    "keep_count": 5
  },
  "hooks": {
    "pre_capture": "/usr/local/bin/pre-backup.sh",
    "post_capture": "/usr/local/bin/post-backup.sh"
  },
  "log_level": "INFO"
}
```

## Snapshot Structure

Each snapshot creates a directory with this structure:

```
image-YYYYMMDD-HHMMSS/
├── manifest.json                    # Snapshot metadata (hostname, distro, timestamp)
├── hostname-YYYYMMDD-HHMMSS.tar.zst # Compressed root filesystem
├── checksums.sha256                 # File integrity checksums
├── docker/
│   ├── images.tar        # Docker images (docker save)
│   ├── images.txt        # List of saved images
│   ├── containers.json   # Container metadata
│   ├── volumes.json      # Volume metadata
│   ├── volumes/          # Volume data backups
│   │   ├── myapp_data.tar
│   │   └── postgres_data.tar
│   ├── compose/          # Docker compose files
│   └── config/           # Docker daemon config
└── metadata/
    ├── packages.txt      # Installed packages
    ├── partitions.json   # Disk layout
    ├── fstab             # Mount configuration
    ├── services.json     # Service states
    ├── network/          # Network configuration
    └── boot/             # Bootloader configuration
```

A docker-only snapshot holds just the Docker payload - no root filesystem, and no
host metadata that couldn't be restored from it anyway:

```
myserver-YYYYMMDD-HHMMSS.docker.tar.zst
├── manifest.json         # "mode": "docker-only", hostname, distro, timestamp
├── docker/               # Same layout as above: images, volumes, compose, config
└── metadata/             # Empty - host metadata is not collected in this mode
```

## Automation

### Cron Job for Daily Snapshots

```bash
# /etc/cron.d/nekoshot
0 2 * * * root /usr/local/bin/nekoshot capture --output /backup/ --json >> /var/log/nekoshot.log 2>&1
0 3 * * 0 root /usr/local/bin/nekoshot prune --path /backup/ --keep 7 --force >> /var/log/nekoshot.log 2>&1
```

Nightly docker-only snapshots with a weekly full system snapshot, pruned separately
so neither type crowds out the other:

```bash
# /etc/cron.d/nekoshot
0 2 * * 1-6 root /usr/local/bin/nekoshot capture --output /backup/ --docker-only --json >> /var/log/nekoshot.log 2>&1
0 2 * * 0   root /usr/local/bin/nekoshot capture --output /backup/ --json >> /var/log/nekoshot.log 2>&1
0 4 * * 0   root /usr/local/bin/nekoshot prune --path /backup/ --keep 6 --mode docker-only --force >> /var/log/nekoshot.log 2>&1
0 5 * * 0   root /usr/local/bin/nekoshot prune --path /backup/ --keep 4 --mode full --force >> /var/log/nekoshot.log 2>&1
```

Or let the menu write these for you - `sudo nekoshot` → *Set up a scheduled backup*.
Databases usually want a tighter schedule than the filesystem, since dumps are small:

```bash
# /etc/cron.d/nekoshot
30 * * * *  root /usr/local/bin/nekoshot sql --json >> /var/log/nekoshot.log 2>&1
0 2 * * 1-6 root /usr/local/bin/nekoshot capture --docker-only --json >> /var/log/nekoshot.log 2>&1
0 2 * * 0   root /usr/local/bin/nekoshot capture --json >> /var/log/nekoshot.log 2>&1
0 5 * * *   root /usr/local/bin/nekoshot prune --path /backup/nekoshot/sql --keep 48 --force >> /var/log/nekoshot.log 2>&1
```

### Backup Script with Notifications

```bash
#!/bin/bash
# /usr/local/bin/backup-system.sh

set -e

BACKUP_DIR="/backup/nekoshot"
WEBHOOK_URL="https://hooks.slack.com/services/xxx"

# Run backup
OUTPUT=$(nekoshot capture --output "$BACKUP_DIR" --json 2>&1)
EXIT_CODE=$?

# Parse result
if [ $EXIT_CODE -eq 0 ]; then
    IMAGE_PATH=$(echo "$OUTPUT" | grep -o '"image_path":"[^"]*"' | cut -d'"' -f4)
    curl -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"Snapshot captured ~nya: $IMAGE_PATH\"}" \
        "$WEBHOOK_URL"
else
    curl -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"Snapshot FAILED! Hissss: $OUTPUT\"}" \
        "$WEBHOOK_URL"
    exit 1
fi

# Prune old snapshots
nekoshot prune --path "$BACKUP_DIR" --keep 5 --force
```

### Pre/Post Hooks

Configure hooks in `/etc/nekoshot/config.json`:

```json
{
  "hooks": {
    "pre_capture": "/usr/local/bin/pre-backup.sh",
    "post_capture": "/usr/local/bin/post-backup.sh",
    "pre_restore": "/usr/local/bin/pre-restore.sh",
    "post_restore": "/usr/local/bin/post-restore.sh"
  }
}
```

Example pre-backup hook to dump databases:

```bash
#!/bin/bash
# /usr/local/bin/pre-backup.sh
docker exec postgres pg_dumpall -U postgres > /var/backups/postgres-dump.sql
docker exec mysql mysqldump --all-databases -u root -p"$MYSQL_ROOT_PASSWORD" > /var/backups/mysql-dump.sql
```

## Restore Scenarios

### Live Restore (Default)

The simplest way to restore - run directly on the target system:

```bash
# Just point at the snapshot and go
sudo nekoshot restore --image /backup/image-20240115-120000/

# The system will ask for confirmation, then restore and offer to reboot
```

Live restore preserves:
- Running kernel and initramfs
- Bootloader configuration
- `/etc/fstab` (disk UUIDs)
- Machine identity files
- Virtual filesystems (proc, sys, dev, etc.)

### Chroot Restore (Bare Metal)

For restoring to a new disk or when the system can't boot:

1. Boot target machine from live USB
2. Partition the disk (match or exceed original sizes)
3. Format partitions:
   ```bash
   mkfs.ext4 /dev/sda2
   mkfs.ext4 /dev/sda1  # boot partition
   ```
4. Mount and restore:
   ```bash
   mount /dev/sda2 /mnt
   mkdir /mnt/boot
   mount /dev/sda1 /mnt/boot
   nekoshot restore \
     --image /path/to/image \
     --target /mnt \
     --fix-bootloader /dev/sda \
     --update-initramfs
   ```
5. Update `/mnt/etc/fstab` with new UUIDs:
   ```bash
   blkid  # get new UUIDs
   nano /mnt/etc/fstab  # update UUIDs
   ```
6. Unmount and reboot

### Restore Docker Only

If you only need to restore Docker data to a running system:

```bash
# Load images
docker load -i /path/to/image/docker/images.tar

# Restore a specific volume
docker volume create myvolume
docker run --rm -v myvolume:/target -v /path/to/image/docker/volumes:/backup alpine \
  tar -xf /backup/myvolume.tar -C /target
```

## Troubleshooting

### Capture fails with "rsync not found"

Install rsync:
```bash
# Ubuntu/Debian
sudo apt install rsync

# Alpine
sudo apk add rsync
```

### Compression fails / zstd not found

Install zstd or use gzip:
```bash
# Install zstd
sudo apt install zstd  # or: apk add zstd

# Or use gzip instead
sudo nekoshot capture --output /backup/ --compression gzip
```

### Restore fails at bootloader

Try manually installing the bootloader:
```bash
# Mount virtual filesystems
mount --bind /dev /mnt/dev
mount --bind /proc /mnt/proc
mount --bind /sys /mnt/sys

# Chroot and install
chroot /mnt
grub-install /dev/sda
grub-mkconfig -o /boot/grub/grub.cfg
exit

# Unmount
umount /mnt/{dev,proc,sys}
```

### Docker volumes not restoring

Ensure Docker is running and you have the alpine image:
```bash
docker pull alpine
```

Then manually restore:
```bash
docker volume create volumename
docker run --rm -v volumename:/target -v /backup/docker/volumes:/src alpine \
  sh -c "tar -xf /src/volumename.tar -C /target"
```

## License

MIT License - See LICENSE file for details.
