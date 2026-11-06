#!/usr/bin/env python3
"""
NekoShot - Purrfect Live System Imaging Tool for Docker Servers

A neko-powered tool for capturing and restoring complete system snapshots
of Alpine/Ubuntu servers running Docker. Designed for automation and
backup workflows ~nya!
"""

import argparse
import sys
import os
import json
import shlex
import shutil
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from utils.crypto import resolve_passphrase_file, check_passphrase_file, encrypt_file, PASSPHRASE_ENV
from config import load_config, NekoShotConfig, CaptureConfig, RestoreConfig, apply_profile, PROFILES
from utils.logging import setup_logging, get_logger
from utils.compression import compress_directory, decompress_archive, create_checksum, verify_checksum
from utils import progress
from utils.progress import PhaseTracker
from utils import status
from utils.status import StatusPublisher, STATE_COMPLETED, STATE_FAILED
from utils import schedule
import menu
from capture.system import SystemCapture, get_hostname, create_image_dir
from capture.docker import DockerCapture
from capture.metadata import MetadataCapture
from capture.sql import SqlCapture
from restore.system import SystemRestore
from restore.docker import DockerRestore
from restore.bootloader import BootloaderRestore


NEKO_BANNER = r"""
    /\_/\
   ( o.o )  NekoShot
    > ^ <   Purrfect System Snapshots ~nya!
   /|   |\
  (_|   |_)
"""

NEKO_CAPTURE_BANNER = r"""
    /\_/\  ~~~
   ( ^.^ ) < Capturing your system, hold still!
    > ~ <
"""

NEKO_RESTORE_BANNER = r"""
    /\_/\  !!!
   ( >.<) < Restoring everything, stay pawsitive!
    > ~ <
"""

NEKO_DONE_BANNER = r"""
    /\_/\
   ( ^w^ ) < All done! Purrfect!
    > ~ <
"""

NEKO_DOCKER_BANNER = r"""
    /\_/\  <>
   ( o.o ) < Docker-only snapshot, skipping the rest of the system!
    > ~ <
"""

NEKO_SQL_BANNER = r"""
    /\_/\  ___
   ( -.o ) < Sniffing out databases to dump!
    > ~ <
"""

ARCHIVE_EXTENSIONS = ('.tar.zst.gpg', '.tar.gz.gpg', '.tar.gpg', '.tar.zst', '.tar.gz', '.tar')

# Banners are charm for humans and noise for parsers - under --json they would
# sit in front of the payload and stop `nekoshot status --json | jq` working
_banners_enabled = True


def set_banners(enabled: bool) -> None:
    global _banners_enabled
    _banners_enabled = enabled


def show_banner(text: str) -> None:
    """Print a banner, unless output has to stay machine-readable."""
    if _banners_enabled:
        print(text)

# Suffixes marking what a snapshot holds, kept after the timestamp so archive
# names still sort by date: hostname-YYYYMMDD-HHMMSS.docker.tar.zst
DOCKER_ONLY_MARKER = '.docker'
SQL_ONLY_MARKER = '.sql'

MODE_MARKERS = {
    'docker-only': DOCKER_ONLY_MARKER,
    'sql-only': SQL_ONLY_MARKER,
}


def check_root():
    """Verify running as root."""
    if os.geteuid() != 0:
        print('Nyaa~! This kitty needs root privileges (sudo) to work!')
        sys.exit(1)


def _default_log_file(prefix: str) -> Path:
    """Pick a log file for a headless run."""
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    log_dir = Path('/var/log/nekoshot')
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f'{prefix}-{timestamp}.log'
    except OSError:
        return Path.cwd() / f'nekoshot-{prefix}-{timestamp}.log'


def daemonize(log_file: Path) -> Optional[int]:
    """
    Detach into the background, logging to a file.

    Returns the daemon's pid in the original process, or None in the daemon
    itself. The double fork is what stops the daemon from ever reacquiring a
    controlling terminal; the pipe carries the final pid back out, since the
    middle process exits before the parent could learn it.
    """
    read_fd, write_fd = os.pipe()

    if os.fork() > 0:
        # Original process: wait for the grandchild's pid, then let the caller exit
        os.close(write_fd)
        with os.fdopen(read_fd) as pipe:
            reported = pipe.read().strip()
        return int(reported) if reported else None

    # Middle process: become a session leader, then fork the real worker away
    os.close(read_fd)
    os.setsid()

    if os.fork() > 0:
        os._exit(0)

    # Daemon proper
    with os.fdopen(write_fd, 'w') as pipe:
        pipe.write(str(os.getpid()))

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_file, 'a', buffering=1)

    devnull = open(os.devnull, 'r')
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(log_handle.fileno(), sys.stdout.fileno())
    os.dup2(log_handle.fileno(), sys.stderr.fileno())

    return None


def _is_archive_file(path: Path) -> bool:
    """Check if a path is a nekoshot archive file."""
    name = path.name
    return path.is_file() and any(name.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def _find_archives(backup_path: Path) -> List[Path]:
    """Find all nekoshot archive files in a directory."""
    archives = []
    for item in backup_path.iterdir():
        if _is_archive_file(item):
            archives.append(item)
    return sorted(archives, key=lambda x: x.name, reverse=True)


def _archive_stem(name: str) -> str:
    """Strip the archive extension from a snapshot filename."""
    for ext in ARCHIVE_EXTENSIONS:
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _archive_mode(path: Path) -> str:
    """
    Determine a snapshot's mode without unpacking it.

    Archive files carry the mode in their name; legacy image directories can be
    inspected directly.
    """
    if path.is_dir():
        return _snapshot_mode(path)

    stem = _archive_stem(path.name)
    for mode, marker in MODE_MARKERS.items():
        if stem.endswith(marker):
            return mode
    return 'full'


def _snapshot_mode(image_dir: Path) -> str:
    """
    Determine whether an extracted snapshot is 'full' or 'docker-only'.

    Prefers the manifest, falling back to the layout for snapshots taken before
    the manifest carried a mode.
    """
    manifest_file = Path(image_dir) / 'manifest.json'

    if manifest_file.exists():
        try:
            with open(manifest_file, 'r') as f:
                mode = json.load(f).get('mode')
            if mode:
                return mode
        except (json.JSONDecodeError, OSError):
            pass

    has_system = (Path(image_dir) / 'system').exists()
    if not has_system and (Path(image_dir) / 'sql').exists():
        return 'sql-only'
    if not has_system and (Path(image_dir) / 'docker').exists():
        return 'docker-only'

    return 'full'


def _extract_to_temp(archive_path: Path, logger, tracker: Optional[PhaseTracker] = None, passphrase_file: Optional[Path] = None) -> Path:
    """Extract a snapshot archive to a temp directory for restore.

    Extracts next to the archive file itself to avoid /tmp tmpfs limits
    (Alpine mounts /tmp in RAM which fills up fast with docker images).
    """
    extract_dir = archive_path.parent / '.nekoshot_restore_tmp'
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)

    logger.info(f'Unpacking snapshot archive to {extract_dir}...')
    decompress_archive(archive_path, extract_dir, strip_components=1, tracker=tracker, passphrase_file=passphrase_file)
    return extract_dir


def cmd_capture(args, config: NekoShotConfig):
    """Execute capture command."""
    logger = get_logger('cli')

    capture_config = config.capture
    capture_config.compression = args.compression or capture_config.compression
    capture_config.pause_docker = args.pause_docker or capture_config.pause_docker
    capture_config.docker_only = args.docker_only or capture_config.docker_only

    if getattr(args, 'profile', None) or capture_config.profile != 'server':
        apply_profile(capture_config, getattr(args, 'profile', None) or capture_config.profile)

    if args.no_docker:
        capture_config.include_docker = False

    if getattr(args, 'encrypt', False):
        capture_config.encrypt = True
    if getattr(args, 'passphrase_file', None):
        capture_config.passphrase_file = args.passphrase_file
    passphrase_file = None
    if capture_config.encrypt:
        passphrase_file = resolve_passphrase_file(capture_config.passphrase_file)
        try:
            check_passphrase_file(passphrase_file)
        except RuntimeError as e:
            logger.error(f'Nyaa~! {e}')
            return 1

    if getattr(args, 'prune_images', None):
        capture_config.prune_images = args.prune_images

    if capture_config.docker_only and not capture_config.include_docker:
        logger.error('Nyaa~! --docker-only and --no-docker cancel each other out - pick one!')
        return 1

    if capture_config.prune_images and not capture_config.include_docker:
        logger.warning('--prune-images has nothing to do with Docker skipped, ignoring ~nya')
        capture_config.prune_images = ''

    if args.exclude:
        capture_config.exclude_patterns.extend(args.exclude)

    docker_only = capture_config.docker_only
    mode = 'docker-only' if docker_only else 'full'

    # Each kind of snapshot sorts into its own directory unless told otherwise
    output_dir = Path(args.output or config.location_for('docker' if docker_only else 'system')).resolve()
    capture_config.output_dir = str(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # A full-system backup holds /etc and every home: only root may look inside,
    # whatever umask the daemon was started with.
    os.chmod(output_dir, 0o700)

    # Detach only once the arguments are known good, so mistakes are still
    # reported to the terminal rather than buried in a log nobody is watching
    log_file = None
    if args.headless:
        log_file = Path(args.log).resolve() if args.log else _default_log_file('capture')
        daemon_pid = daemonize(log_file)

        if daemon_pid is not None:
            if args.json:
                print(json.dumps({
                    'status': 'started',
                    'pid': daemon_pid,
                    'log_file': str(log_file),
                    'mode': mode,
                }))
            else:
                print(f'Off it goes ~nya! Backup running headless with pid {daemon_pid}')
                print(f'  log:      {log_file}')
                print(f'  progress: nekoshot status')
            return 0

        # Past here we are the daemon; bars are pointless without a terminal
        progress.configure(enabled=False)

    publisher = StatusPublisher(
        operation='capture',
        mode=mode,
        output=str(output_dir),
        log_file=str(log_file) if log_file else None,
    )

    show_banner(NEKO_DOCKER_BANNER if docker_only else NEKO_CAPTURE_BANNER)
    logger.info('='*60)
    logger.info('NekoShot ~ Docker-Only Capture' if docker_only else 'NekoShot ~ System Capture')
    logger.info('='*60)
    logger.info(f'Stashing snapshots in: {output_dir}')
    logger.info(f'Compression: {capture_config.compression}')
    logger.info(f'Include Docker: {capture_config.include_docker}')

    if docker_only and args.incremental:
        logger.warning('Incremental mode only applies to system files, ignoring for docker-only ~nya')

    if config.hooks.pre_capture:
        logger.info(f'Running pre-capture hook ~nya: {config.hooks.pre_capture}')
        os.system(config.hooks.pre_capture)

    # Probe Docker before counting phases so the numbering and overall percentage
    # reflect what will actually run. Done after the pre-capture hook, which may
    # be what starts Docker in the first place.
    docker_capture = DockerCapture(capture_config) if capture_config.include_docker else None
    docker_available = docker_capture.is_available() if docker_capture else False

    if docker_only and not docker_available:
        # Nothing else would go in this snapshot, so don't create one at all
        logger.error('Nyaa~! Docker is not available, so there is nothing to capture!')
        publisher.finish(STATE_FAILED, error='Docker is not available')
        return 1

    # Phases: [system files] + [images, volumes, compose] + metadata, archive, checksum
    phases = 3 + (1 if capture_config.encrypt else 0)
    if not docker_only:
        phases += 1
    if docker_available:
        phases += 3

    tracker = PhaseTracker(phases, publisher=publisher)

    with publisher.track():
        # Step 1: Capture system files (rsync to working directory).
        # Docker-only snapshots skip this entirely - no rsync, no rootfs.
        if docker_only:
            image_dir = create_image_dir(output_dir)
        else:
            system_capture = SystemCapture(capture_config)

            incremental_base = None
            if args.incremental and args.link_dest:
                incremental_base = Path(args.link_dest)
                if not incremental_base.exists():
                    logger.warning(f'Incremental base not found: {incremental_base}')
                    incremental_base = None

            image_dir = system_capture.capture(
                output_dir=output_dir,
                incremental_base=incremental_base,
                extra_excludes=args.exclude,
                tracker=tracker
            )

        # Step 2: Capture Docker state
        if capture_config.include_docker:
            logger.info('')
            if docker_available:
                logger.info('Pouncing on Docker state...')
                docker_capture.capture(image_dir, tracker=tracker)
            else:
                logger.warning('Docker not found, skipping ~nya')

        # Step 3: Capture system metadata
        logger.info('')
        logger.info('Collecting system metadata...')
        metadata_bar = tracker.phase('Collecting system metadata', unit='count')
        metadata_capture = MetadataCapture()
        metadata_capture.capture(image_dir, mode=mode)
        metadata_bar.finish(summary='done')

        # Step 4: Compress everything into a single archive
        logger.info('')
        logger.info('Squishing everything into a single archive...')
        hostname = get_hostname()
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        marker = DOCKER_ONLY_MARKER if docker_only else ''
        archive_name = f'{hostname}-{timestamp}{marker}'

        archive_path = compress_directory(
            source_dir=image_dir,
            output_file=output_dir / archive_name,
            compression=capture_config.compression,
            level=capture_config.compression_level,
            tracker=tracker
        )

        # Step 4b: Encrypt the archive; the checksum then covers the encrypted file
        if capture_config.encrypt:
            enc_bar = tracker.phase('Encrypting archive', unit='count')
            archive_path = encrypt_file(archive_path, passphrase_file)
            enc_bar.finish(summary='AES256')

        # Step 5: Create checksum sidecar
        checksum = create_checksum(archive_path, tracker=tracker)
        checksum_file = Path(str(archive_path) + '.sha256')
        with open(checksum_file, 'w') as f:
            f.write(f'{checksum}  {archive_path.name}\n')

        # Step 6: Clean up working directory
        shutil.rmtree(image_dir)

    if config.hooks.post_capture:
        logger.info(f'Running post-capture hook ~nya: {config.hooks.post_capture}')
        os.system(config.hooks.post_capture)

    archive_size = archive_path.stat().st_size

    publisher.finish(
        STATE_COMPLETED,
        archive=str(archive_path),
        size_bytes=archive_size,
        checksum=checksum,
        overall_percent=100,
        elapsed_seconds=round(tracker.elapsed(), 1),
    )

    show_banner(NEKO_DONE_BANNER)
    logger.info('='*60)
    logger.info('Docker-Only Capture Complete ~nya!' if docker_only else 'Capture Complete ~nya!')
    logger.info('='*60)
    logger.info(f'Snapshot: {archive_path}')
    logger.info(f'Size: {archive_size / 1024 / 1024:.1f} MB')
    logger.info(f'Checksum: {checksum_file}')
    logger.info(f'Took: {progress.format_duration(tracker.elapsed())}')

    if docker_only:
        logger.info('')
        logger.info('This snapshot holds Docker images, volumes and compose files only -')
        logger.info('restoring it will not touch the host system ~nya')

    if args.json:
        print(json.dumps({
            'status': 'success',
            'archive': str(archive_path),
            'mode': mode,
            'size_bytes': archive_size,
            'checksum': checksum,
        }))

    return 0


def _count_restore_phases(args, config, image_path: Path, mode: str) -> int:
    """
    How many phases this restore will run.

    The snapshot's mode is readable from its name, so the phase count is known
    before anything is unpacked.
    """
    phases = 1 if _is_archive_file(image_path) else 0  # unpacking the archive

    if mode != 'docker-only':
        phases += 1  # system files

    if config.restore.restore_docker and not args.skip_docker:
        phases += 2  # loading images, restoring volumes

    return max(1, phases)


def cmd_restore(args, config: NekoShotConfig):
    """Execute restore command."""
    logger = get_logger('cli')

    image_path = Path(args.image)

    if not image_path.exists():
        logger.error(f'Nyaa~! Snapshot not found: {image_path}')
        return 1

    if not _is_archive_file(image_path) and not image_path.is_dir():
        logger.error(f'Not a valid snapshot archive or directory: {image_path}')
        return 1

    tracker = PhaseTracker(_count_restore_phases(args, config, image_path, _archive_mode(image_path)))

    # If it's an archive file, extract to temp dir first
    extract_dir = None
    if _is_archive_file(image_path):
        pf = None
        if image_path.name.endswith('.gpg'):
            pf = resolve_passphrase_file(getattr(args, 'passphrase_file', None))
            try:
                check_passphrase_file(pf)
            except RuntimeError as e:
                logger.error(f'Nyaa~! {e}')
                return 1
        extract_dir = _extract_to_temp(image_path, logger, tracker=tracker, passphrase_file=pf)
        image_dir = extract_dir
    else:
        image_dir = image_path

    try:
        return _do_restore(args, config, image_dir, logger, tracker)
    finally:
        if extract_dir and extract_dir.exists():
            logger.info('Tidying up extracted files...')
            shutil.rmtree(extract_dir)


def _do_docker_only_restore(args, config, image_dir, logger, tracker=None):
    """Restore a docker-only snapshot: Docker state only, host system untouched."""

    if args.target:
        logger.error('Nyaa~! This is a docker-only snapshot - there are no system files to put in a target.')
        logger.error('Run without --target to load its Docker state into the running system.')
        return 1

    if args.skip_docker:
        logger.error('Nyaa~! --skip-docker on a docker-only snapshot leaves nothing to restore.')
        return 1

    show_banner(NEKO_DOCKER_BANNER)
    logger.info('='*60)
    logger.info('NekoShot ~ Docker-Only Restore')
    logger.info('='*60)
    logger.info(f'Snapshot: {image_dir}')
    logger.info('Target: Docker on the running system (host files untouched)')
    logger.warning('')
    logger.warning('CAREFUL! Volumes in the snapshot will overwrite existing ones with the same name ~nya.')
    logger.warning('')

    response = input('Proceed with Docker restore? [y/N]: ')
    if response.lower() != 'y':
        logger.info('Aborted ~nya')
        return 0

    if config.hooks.pre_restore:
        logger.info(f'Running pre-restore hook ~nya: {config.hooks.pre_restore}')
        os.system(config.hooks.pre_restore)

    restore_config = config.restore

    docker_restore = DockerRestore(restore_config)
    if not docker_restore.is_available():
        logger.error('Nyaa~! Docker is not available on this system, cannot restore.')
        return 1

    docker_restore.restore(image_dir, tracker=tracker)
    docker_restore.show_container_info(image_dir)

    if config.hooks.post_restore:
        logger.info(f'Running post-restore hook ~nya: {config.hooks.post_restore}')
        os.system(config.hooks.post_restore)

    show_banner(NEKO_DONE_BANNER)
    logger.info('='*60)
    logger.info('Docker-Only Restore Complete ~nya!')
    logger.info('='*60)
    logger.info('')
    logger.info('No reboot needed - bring your containers back up with docker-compose ~nya')

    if args.json:
        print(json.dumps({
            'status': 'success',
            'mode': 'docker-only',
            'target': 'docker',
        }))

    return 0


def _do_sql_restore_notice(image_dir, logger) -> int:
    """
    Explain how to restore a database snapshot rather than doing it.

    Loading a dump means overwriting live databases, and every engine wants it
    done differently. Better to hand over the files and the exact commands than
    to guess and wreck someone's data.
    """
    manifest = Path(image_dir) / 'sql' / 'databases.json'
    entries = []
    if manifest.exists():
        try:
            with open(manifest, 'r') as f:
                entries = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    logger.info('='*60)
    logger.info('This is a database snapshot ~nya')
    logger.info('='*60)
    logger.info('')
    logger.info('NekoShot will not load dumps back on its own: that would overwrite live')
    logger.info('databases, and each engine wants it done its own way. Unpack it and')
    logger.info('restore the dumps yourself:')
    logger.info('')
    logger.info(f'  tar -xf <snapshot>.sql.tar.zst        # or: zstd -d -c <snapshot> | tar -x')
    logger.info('')

    if entries:
        logger.info('It holds:')
        for entry in entries:
            logger.info(f'  {entry["file"]}   ({entry["engine"]} from {entry["server"]})')
        logger.info('')

    logger.info('Then, per engine:')
    logger.info('  postgres:  psql -U postgres -f <dump>.sql')
    logger.info('             (in a container: docker exec -i <c> psql -U postgres < <dump>.sql)')
    logger.info('  mysql:     mysql -u root -p < <dump>.sql')
    logger.info('             (in a container: docker exec -i <c> mariadb -u root -p<pw> < <dump>.sql)')
    logger.info('  sqlite:    cp <file>.sqlite /path/to/app.db')
    logger.info('')
    logger.info('  sqlserver, in a container (the chown matters - docker cp lands the')
    logger.info('  file as root and SQL Server cannot read it):')
    logger.info('    docker cp <file>.bak <c>:/var/opt/mssql/backup/restore.bak')
    logger.info('    docker exec -u 0 <c> chown mssql /var/opt/mssql/backup/restore.bak')
    logger.info('    docker exec <c> /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P <pw> -C \\')
    logger.info('      -Q "RESTORE DATABASE [x] FROM DISK=N\'/var/opt/mssql/backup/restore.bak\' WITH REPLACE"')

    return 0


def _do_restore(args, config, image_dir, logger, tracker=None):
    """Perform the actual restore from an extracted image directory."""

    mode = _snapshot_mode(image_dir)

    if mode == 'sql-only':
        return _do_sql_restore_notice(image_dir, logger)

    if mode == 'docker-only':
        return _do_docker_only_restore(args, config, image_dir, logger, tracker)

    # If --target is specified, do chroot restore to mounted target
    if args.target:
        target_dir = Path(args.target)

        if not target_dir.exists():
            logger.error(f'Target directory not found: {target_dir}')
            logger.error('Mount your target disk first: mount /dev/sdXN /mnt/restore')
            return 1

        show_banner(NEKO_RESTORE_BANNER)
        logger.info('='*60)
        logger.info('NekoShot ~ Chroot Restore')
        logger.info('='*60)
        logger.info(f'Snapshot: {image_dir}')
        logger.info(f'Target: {target_dir}')

        if config.hooks.pre_restore:
            logger.info(f'Running pre-restore hook ~nya: {config.hooks.pre_restore}')
            os.system(config.hooks.pre_restore)

        restore_config = config.restore
        restore_config.target_dir = str(target_dir)

        logger.info('')
        logger.info('Unpacking system files...')
        system_restore = SystemRestore(restore_config)
        system_restore.restore(image_dir, target_dir, tracker=tracker)
        system_restore.create_required_dirs(target_dir)

        if args.update_fstab:
            logger.info('Updating fstab...')
            system_restore.update_fstab(target_dir, image_dir)

        if restore_config.restore_docker and not args.skip_docker:
            logger.info('')
            logger.info('Restoring Docker state...')
            docker_restore = DockerRestore(restore_config)
            if docker_restore.is_available():
                docker_restore.restore(image_dir, tracker=tracker)
                docker_restore.show_container_info(image_dir)
            else:
                if tracker:
                    tracker.set_phases(tracker.phases - 2)
                logger.info('Docker not here right meow, skipping restore')
                logger.info('Docker images/volumes will be available after boot')

        if args.fix_bootloader:
            logger.info('')
            logger.info(f'Installing bootloader to {args.fix_bootloader}...')
            bootloader = BootloaderRestore(restore_config)
            bootloader.restore(target_dir, args.fix_bootloader, args.bootloader_type)

            if args.update_initramfs:
                bootloader.update_initramfs(target_dir)

        if config.hooks.post_restore:
            logger.info(f'Running post-restore hook ~nya: {config.hooks.post_restore}')
            os.system(config.hooks.post_restore)

        show_banner(NEKO_DONE_BANNER)
        logger.info('='*60)
        logger.info('Chroot Restore Complete ~nya!')
        logger.info('='*60)
        logger.info('')
        logger.info('Next steps:')
        logger.info('1. Review /etc/fstab for correct UUIDs')
        logger.info('2. Unmount the target: umount -R /mnt/restore')
        logger.info('3. Reboot into the restored system')

        if args.json:
            print(json.dumps({
                'status': 'success',
                'target': str(target_dir),
            }))

        return 0

    # Default: live restore to running system
    show_banner(NEKO_RESTORE_BANNER)
    logger.info('='*60)
    logger.info('NekoShot ~ LIVE System Restore')
    logger.info('='*60)
    logger.info(f'Snapshot: {image_dir}')
    logger.info(f'Target: / (live system)')
    logger.warning('')
    logger.warning('CAREFUL! This kitty is about to overwrite files on your running system!')
    logger.warning('Make sure the snapshot is from a matching OS version ~nya.')
    logger.warning('')

    response = input('Proceed with live restore? [y/N]: ')
    if response.lower() != 'y':
        logger.info('Aborted ~nya')
        return 0

    if config.hooks.pre_restore:
        logger.info(f'Running pre-restore hook ~nya: {config.hooks.pre_restore}')
        os.system(config.hooks.pre_restore)

    restore_config = config.restore
    restore_config.target_dir = '/'

    logger.info('')
    logger.info('Starting live restore...')
    system_restore = SystemRestore(restore_config)
    system_restore.live_restore(image_dir, tracker=tracker)

    if restore_config.restore_docker and not args.skip_docker:
        logger.info('')
        logger.info('Restoring Docker state...')
        docker_restore = DockerRestore(restore_config)
        if docker_restore.is_available():
            docker_restore.restore(image_dir, tracker=tracker)
        else:
            if tracker:
                tracker.set_phases(tracker.phases - 2)
            logger.info('Docker will wake up after reboot ~nya')

    if config.hooks.post_restore:
        logger.info(f'Running post-restore hook ~nya: {config.hooks.post_restore}')
        os.system(config.hooks.post_restore)

    show_banner(NEKO_DONE_BANNER)
    logger.info('='*60)
    logger.info('Live Restore Complete ~nya!')
    logger.info('='*60)
    logger.info('')
    logger.info('A reboot is needed to apply all changes.')

    response = input('Reboot meow? [y/N]: ')
    if response.lower() == 'y':
        logger.info('Rebooting ~nya!')
        os.system('reboot')

    if args.json:
        print(json.dumps({
            'status': 'success',
            'target': '/',
        }))

    return 0


def cmd_sql(args, config: NekoShotConfig):
    """Dump databases into their own snapshot."""
    logger = get_logger('cli')

    sql_config = config.sql
    sql_config.compression = args.compression or sql_config.compression

    if args.no_docker:
        sql_config.include_docker = False
    if args.no_native:
        sql_config.include_native = False
    if args.sqlite:
        sql_config.sqlite_paths.extend(args.sqlite)
    if args.exclude:
        sql_config.exclude.extend(args.exclude)

    if not sql_config.include_docker and not sql_config.include_native and not sql_config.sqlite_paths:
        logger.error('Nyaa~! --no-docker and --no-native leave nothing to look in!')
        return 1

    output_dir = Path(args.output or config.location_for('sql')).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log_file = None
    if args.headless:
        log_file = Path(args.log).resolve() if args.log else _default_log_file('sql')
        daemon_pid = daemonize(log_file)

        if daemon_pid is not None:
            if args.json:
                print(json.dumps({
                    'status': 'started', 'pid': daemon_pid,
                    'log_file': str(log_file), 'mode': 'sql-only',
                }))
            else:
                print(f'Off it goes ~nya! Database backup running headless with pid {daemon_pid}')
                print(f'  log:      {log_file}')
                print(f'  progress: nekoshot status')
            return 0

        progress.configure(enabled=False)

    publisher = StatusPublisher(
        operation='sql', mode='sql-only', output=str(output_dir),
        log_file=str(log_file) if log_file else None,
    )

    show_banner(NEKO_SQL_BANNER)
    logger.info('='*60)
    logger.info('NekoShot ~ Database Backup')
    logger.info('='*60)
    logger.info(f'Stashing dumps in: {output_dir}')
    logger.info(f'Looking in: {"containers " if sql_config.include_docker else ""}'
                f'{"host " if sql_config.include_native else ""}'
                f'{f"+ {len(sql_config.sqlite_paths)} sqlite file(s)" if sql_config.sqlite_paths else ""}')

    if config.hooks.pre_capture:
        logger.info(f'Running pre-capture hook ~nya: {config.hooks.pre_capture}')
        os.system(config.hooks.pre_capture)

    # Phases: dump, metadata, archive, checksum
    tracker = PhaseTracker(4, publisher=publisher)

    with publisher.track():
        image_dir = create_image_dir(output_dir)

        sql_capture = SqlCapture(sql_config)
        stats = sql_capture.capture(image_dir, tracker=tracker)

        if not stats['dumped']:
            logger.error('Nyaa~! Nothing was dumped, so there is no snapshot to keep.')
            shutil.rmtree(image_dir, ignore_errors=True)
            publisher.finish(STATE_FAILED, error='No databases were dumped')
            return 1

        logger.info('')
        logger.info('Collecting system metadata...')
        metadata_bar = tracker.phase('Collecting system metadata', unit='count')
        MetadataCapture().capture(image_dir, mode='sql-only')
        metadata_bar.finish(summary='done')

        logger.info('')
        logger.info('Squishing dumps into a single archive...')
        hostname = get_hostname()
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        archive_name = f'{hostname}-{timestamp}{SQL_ONLY_MARKER}'

        archive_path = compress_directory(
            source_dir=image_dir,
            output_file=output_dir / archive_name,
            compression=sql_config.compression,
            level=sql_config.compression_level,
            tracker=tracker,
        )

        checksum = create_checksum(archive_path, tracker=tracker)
        checksum_file = Path(str(archive_path) + '.sha256')
        with open(checksum_file, 'w') as f:
            f.write(f'{checksum}  {archive_path.name}\n')

        shutil.rmtree(image_dir)

    if config.hooks.post_capture:
        logger.info(f'Running post-capture hook ~nya: {config.hooks.post_capture}')
        os.system(config.hooks.post_capture)

    archive_size = archive_path.stat().st_size

    publisher.finish(
        STATE_COMPLETED, archive=str(archive_path), size_bytes=archive_size,
        checksum=checksum, overall_percent=100,
        elapsed_seconds=round(tracker.elapsed(), 1),
    )

    show_banner(NEKO_DONE_BANNER)
    logger.info('='*60)
    logger.info('Database Backup Complete ~nya!')
    logger.info('='*60)
    logger.info(f'Snapshot: {archive_path}')
    logger.info(f'Size: {archive_size / 1024 / 1024:.1f} MB')
    logger.info(f'Databases: {stats["dumped"]} server(s), {len(stats["databases"])} dump file(s)')
    if stats['failed']:
        logger.warning(f'{stats["failed"]} server(s) could not be dumped - see the log above')
    if stats['docker_unavailable']:
        logger.warning('Docker was unreachable: this snapshot has NO databases from containers!')
    logger.info(f'Took: {progress.format_duration(tracker.elapsed())}')

    if args.json:
        print(json.dumps({
            'status': 'success',
            'archive': str(archive_path),
            'mode': 'sql-only',
            'size_bytes': archive_size,
            'checksum': checksum,
            'servers_dumped': stats['dumped'],
            'servers_failed': stats['failed'],
            'docker_unavailable': stats['docker_unavailable'],
            'databases': stats['databases'],
        }))

    return 0


def _render_status(record: dict) -> List[str]:
    """Format one run for the status display."""
    labels = {
        status.STATE_RUNNING: 'RUNNING',
        status.STATE_COMPLETED: 'DONE',
        status.STATE_FAILED: 'FAILED',
        status.STATE_STALE: 'STALE',
    }
    state = record.get('state', 'unknown')
    operation = record.get('operation', 'capture')
    mode = record.get('mode')
    elapsed = progress.format_duration(record.get('elapsed_seconds') or 0)

    lines = []
    lines.append(
        f'  {operation}{f" ({mode})" if mode else ""} on {record.get("hostname", "?")}'
        f'   {labels.get(state, state.upper())}   pid {record.get("pid", "?")}'
    )

    phase = record.get('phase')
    if phase:
        bar_width = 24
        percent = phase.get('percent')
        if percent is not None:
            filled = int(bar_width * percent / 100)
            bar = '[' + '#' * filled + '-' * (bar_width - filled) + ']'
            pct = f'{percent:>3}%'
        else:
            bar = '[' + '-' * bar_width + ']'
            pct = '    '

        detail = phase.get('detail') or phase.get('description')
        parts = [f'  [{phase.get("index")}/{phase.get("count")}] {detail}', f'  {bar} {pct}']

        amount = progress.format_bytes if phase.get('unit') == 'bytes' else (lambda n: f'{int(n):,}')

        # Phases without a total (docker save) still have real work done to show
        if phase.get('total'):
            parts.append(f'  {amount(phase["current"])} / {amount(phase["total"])}')
        elif phase.get('current'):
            parts.append(f'  {amount(phase["current"])}')
        if phase.get('rate'):
            parts.append(f'  {amount(phase["rate"])}/s')
        if phase.get('eta_seconds') is not None:
            parts.append(f'  ETA {progress.format_duration(phase["eta_seconds"])}')

        lines.append(''.join(parts))

    summary = f'  overall {record.get("overall_percent", 0):>3}%   elapsed {elapsed}   started {record.get("started_at", "?")}'
    lines.append(summary)

    if state == status.STATE_COMPLETED and record.get('archive'):
        size = record.get('size_bytes')
        size_text = f' ({progress.format_bytes(size)})' if size else ''
        lines.append(f'  snapshot: {record["archive"]}{size_text}')
    elif state == status.STATE_FAILED and record.get('error'):
        lines.append(f'  error: {record["error"]}')
    elif state == status.STATE_STALE:
        lines.append('  the process is gone - it was killed or the machine rebooted mid-backup')

    if record.get('output'):
        lines.append(f'  output: {record["output"]}')
    if record.get('log_file'):
        lines.append(f'  log: {record["log_file"]}')

    return lines


def cmd_status(args, config: NekoShotConfig):
    """Show progress of running (or most recent) backups."""
    records = status.read_all()

    if not args.all:
        running = [r for r in records if r.get('state') == status.STATE_RUNNING]
        # Nothing running: the last run's outcome is the useful answer
        records = running if running else records[:1]

    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if not k.startswith('_')} for r in records], indent=2))
        return 0 if not any(r.get('state') in (status.STATE_FAILED, status.STATE_STALE) for r in records) else 1

    if not records:
        print('\n  /\\_/\\')
        print(' ( -.- )  No backups running, and none finished recently ~nya')
        print('  > ^ <   Start one with: nekoshot capture --headless --output /backup/')
        print()
        return 0

    print('\n  /\\_/\\')
    print(' ( o.o )  NekoShot Status')
    print('  > ^ <')
    print()

    for record in records:
        for line in _render_status(record):
            print(line)
        print()

    return 1 if any(r.get('state') in (status.STATE_FAILED, status.STATE_STALE) for r in records) else 0


def cmd_watch(args, config: NekoShotConfig):
    """Re-render status until the running backup finishes."""
    try:
        while True:
            records = status.read_all()
            running = [r for r in records if r.get('state') == status.STATE_RUNNING]

            if sys.stdout.isatty():
                sys.stdout.write('\033[H\033[2J')  # home + clear

            cmd_status(args, config)

            if not running:
                return 0

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\nStopped watching ~nya (the backup keeps running)')
        return 0


def cmd_schedule(args, config: NekoShotConfig) -> int:
    """
    Scriptable equivalent of the menu's "Set up" / "Show or remove" scheduled
    backup screens -- exists so a non-interactive caller (a GUI, a script) can
    manage cron jobs the same way the menu does, without going through it.
    """
    action = getattr(args, 'schedule_action', None)

    if action == 'list':
        jobs = schedule.list_jobs()
        running = schedule.cron_running()
        if args.json:
            print(json.dumps({
                'jobs': [{**job, 'description': schedule.describe(job['schedule'])} for job in jobs],
                'cron_running': running,
                'cron_file': str(schedule.cron_file()),
            }, indent=2))
            return 0

        if not jobs:
            print('No scheduled backups yet ~nya')
            return 0
        print(f'Scheduled backups (from {schedule.cron_file()}):')
        for job in jobs:
            print(f'  {job["name"]}')
            print(f'    when: {schedule.describe(job["schedule"])}   ({job["schedule"]})')
            print(f'    runs: {job["command"]}')
        if not running:
            print('Careful: no cron daemon appears to be running, so these will not fire.')
        return 0

    if action == 'add':
        check_root()
        argv = args.command_argv
        if argv and argv[0] == '--':
            argv = argv[1:]
        if not argv:
            print('Nyaa~! Need a nekoshot command to schedule, e.g.: capture --output /backup')
            return 1
        # The command is stored in a crontab (run by sh) and in systemd ExecStart (its own
        # parser): quote every argument so paths with spaces survive either.
        command = f'{schedule.nekoshot_command()} {" ".join(shlex.quote(a) for a in argv)}'
        try:
            schedule.add_job(args.name, args.cron, command)
        except OSError as e:
            if args.json:
                print(json.dumps({'ok': False, 'error': str(e)}))
            else:
                print(f'Could not write the cron entry: {e}')
            return 1
        if args.json:
            print(json.dumps({'ok': True, 'name': args.name, 'schedule': args.cron, 'command': command}))
        else:
            print(f'Scheduled "{args.name}" - runs {schedule.describe(args.cron)} ~nya')
        return 0

    if action == 'remove':
        check_root()
        removed = schedule.remove_job(args.name)
        if args.json:
            print(json.dumps({'ok': removed, 'name': args.name}))
        else:
            print(f'Removed "{args.name}" ~nya' if removed else f'No job named "{args.name}"')
        return 0 if removed else 1

    print('Usage: nekoshot schedule {list|add|remove} ...')
    return 1


def cmd_menu(args, config: NekoShotConfig, parser) -> int:
    """Run the interactive menu, then carry out what was chosen."""
    logger = get_logger('cli')

    try:
        choice = menu.run_menu(config)
    except KeyboardInterrupt:
        print('\nBye ~nya!')
        return 0

    if choice is None:
        print('\nBye ~nya!')
        return 0

    action, payload = choice

    if action == menu.ACTION_SCHEDULE:
        name, expression, *argv = payload
        # The command is stored in a crontab (run by sh) and in systemd ExecStart (its own
        # parser): quote every argument so paths with spaces survive either.
        command = f'{schedule.nekoshot_command()} {" ".join(shlex.quote(a) for a in argv)}'

        print()
        print(f'  Installing into {schedule.cron_file()}:')
        print(f'    {expression}  {command}')
        print()

        if not menu._confirm('  Look right?', default=True):
            print('  Nothing scheduled ~nya')
            return 0

        try:
            schedule.add_job(name, expression, command)
        except OSError as e:
            logger.error(f'Could not write the cron entry: {e}')
            return 1

        print(f'  Scheduled "{name}" - it will run {schedule.describe(expression)} ~nya')

        if not schedule.cron_running():
            print()
            print('  Careful: no cron daemon appears to be running, so it will not fire.')
            print('    Alpine:  rc-service crond start && rc-update add crond')
            print('    Ubuntu:  systemctl enable --now cron')

        return 0

    # Run it: re-parse as if it had been typed, so there is one code path
    print()
    print(f'  Running: {menu.format_command(payload)}')
    print()

    sub_args = parser.parse_args(payload)
    _apply_global_flags(sub_args, args)

    if sub_args.command in ROOT_COMMANDS:
        check_root()

    return dispatch(sub_args, config, parser)


def _apply_global_flags(sub_args, args) -> None:
    """Carry the global flags from the original invocation into a menu choice."""
    for flag in ('json', 'no_progress', 'config', 'log_level'):
        if hasattr(args, flag):
            setattr(sub_args, flag, getattr(args, flag))


def _list_search_paths(backup_path: Path) -> List[Path]:
    """
    The directory asked for, plus the per-kind folders beneath it.

    Snapshots sort themselves into system/, docker/ and sql/, so listing the
    stash itself should still find them rather than reporting an empty shelf.
    """
    paths = [backup_path]
    for kind in ('system', 'docker', 'sql'):
        candidate = backup_path / kind
        if candidate.is_dir():
            paths.append(candidate)
    return paths


def cmd_list(args, config: NekoShotConfig):
    """List available snapshots."""
    logger = get_logger('cli')

    backup_path = Path(args.path or config.capture.output_dir)

    if not backup_path.exists():
        logger.error(f'Snapshot stash not found: {backup_path}')
        return 1

    images = []

    search_paths = _list_search_paths(backup_path)
    candidates = sorted(
        (item for path in search_paths for item in path.iterdir()),
        key=lambda p: p.name, reverse=True
    )

    for item in candidates:
        if _is_archive_file(item):
            # Parse hostname and date from filename like hostname-YYYYMMDD-HHMMSS.tar.zst
            # (docker-only and sql snapshots carry a marker before the extension)
            stem = _archive_stem(item.name)

            mode = 'full'
            for candidate, marker in MODE_MARKERS.items():
                if stem.endswith(marker):
                    stem = stem[:-len(marker)]
                    mode = candidate
                    break

            # Try to split hostname-YYYYMMDD-HHMMSS
            parts = stem.rsplit('-', 2)
            if len(parts) >= 3:
                hostname = '-'.join(parts[:-2])
                date_str = parts[-2]
                time_str = parts[-1]
                created = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {time_str[:2]}:{time_str[2:4]}:{time_str[4:]}'
            else:
                hostname = stem
                created = datetime.fromtimestamp(item.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')

            size = item.stat().st_size

            images.append({
                'name': item.name,
                'path': str(item),
                'created': created,
                'hostname': hostname,
                'mode': mode,
                'encrypted': item.name.endswith('.gpg'),
                'distro': '-',
                'size_bytes': size,
                'size_human': f'{size / 1024 / 1024:.1f} MB',
            })

    # Also find legacy image directories (backward compat)
    for item in candidates:
        if item.is_dir() and item.name.startswith('image-'):
            manifest_file = item / 'manifest.json'
            manifest = {}

            if manifest_file.exists():
                with open(manifest_file, 'r') as f:
                    manifest = json.load(f)

            size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())

            images.append({
                'name': item.name,
                'path': str(item),
                'created': manifest.get('created', 'unknown'),
                'hostname': manifest.get('hostname', ''),
                'mode': _snapshot_mode(item),
                'encrypted': False,
                'distro': manifest.get('distro', 'unknown'),
                'size_bytes': size,
                'size_human': f'{size / 1024 / 1024:.1f} MB',
            })

    if args.json:
        print(json.dumps(images, indent=2))
    else:
        if not images:
            print(f'No snapshots found in {backup_path} ~nya')
            return 0

        print(f'\n  /\\_/\\')
        print(f' ( o.o )  NekoShot Snapshot Stash')
        print(f'  > ^ <   {backup_path}')
        print()
        print('-' * 92)
        print(f'{"Name":<40} {"Created":<22} {"Host":<14} {"Type":<12} {"Size"}')
        print('-' * 92)

        for img in images:
            created = img['created'][:19].replace('T', ' ') if 'T' in img['created'] else img['created']
            hostname = img['hostname'][:12] if img['hostname'] else '-'
            print(f'{img["name"]:<40} {created:<22} {hostname:<14} {img["mode"]:<12} {img["size_human"]}')

        print('-' * 92)
        print(f'Total: {len(images)} snapshot(s) ~nya')

    return 0


def cmd_verify(args, config: NekoShotConfig):
    """Verify snapshot integrity."""
    logger = get_logger('cli')

    image_path = Path(args.image)

    if not image_path.exists():
        logger.error(f'Nyaa~! Snapshot not found: {image_path}')
        return 1

    # New format: archive file with .sha256 sidecar
    if _is_archive_file(image_path):
        checksum_file = Path(str(image_path) + '.sha256')
        if not checksum_file.exists():
            logger.warning('No .sha256 checksum file found alongside the archive')
            return 1

        logger.info(f'Sniffing snapshot integrity: {image_path.name}')

        with open(checksum_file, 'r') as f:
            line = f.readline().strip()

        parts = line.split('  ', 1)
        if len(parts) != 2:
            logger.error('Malformed checksum file')
            return 1

        expected_hash = parts[0]

        logger.info(f'Checking {image_path.name}...')
        if verify_checksum(image_path, expected_hash):
            logger.info('All checksums match - snapshot is purrfect!')
            if args.json:
                print(json.dumps({'schema': 1, 'status': 'success', 'verified': True,
                                  'image': str(image_path), 'encrypted': image_path.name.endswith('.gpg')}))
            return 0
        else:
            logger.error('FAILED - checksum mismatch!')
            if args.json:
                print(json.dumps({'schema': 1, 'status': 'failed', 'verified': False,
                                  'image': str(image_path), 'error': 'checksum mismatch'}))
            return 1

    # Legacy format: directory with checksums.sha256 inside
    if not image_path.is_dir():
        logger.error(f'Not a valid snapshot: {image_path}')
        return 1

    checksums_file = image_path / 'checksums.sha256'

    if not checksums_file.exists():
        logger.warning('No checksums file found, this kitty can\'t verify integrity')
        return 1

    logger.info(f'Sniffing snapshot integrity: {image_path}')

    errors = []

    with open(checksums_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split('  ', 1)
            if len(parts) != 2:
                continue

            expected_hash, filename = parts
            file_path = image_path / filename

            if not file_path.exists():
                logger.error(f'Missing file: {filename}')
                errors.append(filename)
                continue

            logger.info(f'Checking {filename}...')

            if verify_checksum(file_path, expected_hash):
                logger.info(f'  OK ~nya')
            else:
                logger.error(f'  FAILED - checksum mismatch!')
                errors.append(filename)

    if errors:
        logger.error(f'\nVerification FAILED: {len(errors)} file(s) have issues!')
        return 1

    logger.info('\nAll checksums match - snapshot is purrfect!')

    if args.json:
        print(json.dumps({'schema': 1, 'status': 'success', 'verified': True,
                          'image': str(image_path), 'encrypted': False}))

    return 0


def cmd_prune(args, config: NekoShotConfig):
    """Remove old snapshots based on retention policy."""
    logger = get_logger('cli')

    backup_path = Path(args.path or config.capture.output_dir)
    keep_count = args.keep or config.retention.keep_count

    if not backup_path.exists():
        logger.error(f'Snapshot stash not found: {backup_path}')
        return 1

    # Collect both archive files (new) and directories (legacy)
    snapshots = []

    for item in backup_path.iterdir():
        if _is_archive_file(item):
            snapshots.append(item)
        elif item.is_dir() and item.name.startswith('image-'):
            snapshots.append(item)

    mode_filter = args.mode or 'all'
    if mode_filter != 'all':
        snapshots = [s for s in snapshots if _archive_mode(s) == mode_filter]
        logger.info(f'Only considering {mode_filter} snapshots')

    snapshots.sort(key=lambda x: x.name, reverse=True)

    if len(snapshots) <= keep_count:
        logger.info(f'Found {len(snapshots)} snapshots, keeping {keep_count} - nothing to knock off the shelf ~nya')
        return 0

    to_delete = snapshots[keep_count:]

    logger.info(f'Found {len(snapshots)} snapshots, keeping {keep_count}')
    logger.info(f'Knocking {len(to_delete)} old snapshot(s) off the shelf:')

    for snap in to_delete:
        logger.info(f'  - {snap.name}')

    if not args.force:
        response = input('\nProceed with deletion? [y/N]: ')
        if response.lower() != 'y':
            logger.info('Aborted ~nya')
            return 0

    deleted = 0
    for snap in to_delete:
        try:
            if snap.is_dir():
                shutil.rmtree(snap)
            else:
                snap.unlink()
                # Also delete .sha256 sidecar if present
                sha_file = Path(str(snap) + '.sha256')
                if sha_file.exists():
                    sha_file.unlink()

            logger.info(f'Knocked off: {snap.name}')
            deleted += 1
        except Exception as e:
            logger.error(f'Failed to delete {snap.name}: {e}')

    logger.info(f'\nPruned {deleted} snapshot(s) ~nya')

    if args.json:
        print(json.dumps({
            'status': 'success',
            'deleted': deleted,
            'remaining': keep_count
        }))

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (also used to re-parse commands built by the menu)."""
    parser = argparse.ArgumentParser(
        prog='nekoshot',
        description='Purrfect Live System Imaging Tool for Docker Servers ~nya',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Not sure? Just run it and pick from the menu
  sudo nekoshot

  # Capture a snapshot ~nya
  sudo nekoshot capture --output /backup/

  # Capture only Docker state (images, volumes, compose) - no system files
  sudo nekoshot capture --output /backup/ --docker-only

  # Dump every database it can find (containers and host)
  sudo nekoshot sql --output /backup/sql/

  # Run a backup headless and check on it from anywhere
  sudo nekoshot capture --output /backup/ --headless
  nekoshot status
  nekoshot status --watch

  # Live restore to running system (default)
  sudo nekoshot restore --image /backup/myserver-20240115-120000.tar.zst

  # Chroot restore to mounted target (bare metal / live USB recovery)
  sudo nekoshot restore --image /backup/myserver-20240115-120000.tar.zst --target /mnt/restore --fix-bootloader /dev/sda

  # List available snapshots
  nekoshot list --path /backup/

  # Verify snapshot integrity
  nekoshot verify --image /backup/myserver-20240115-120000.tar.zst

  # Prune old snapshots (keep last 5)
  nekoshot prune --path /backup/ --keep 5
'''
    )

    parser.add_argument('--config', '-c', help='Path to configuration file')
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], default='INFO')
    parser.add_argument('--json', action='store_true', help='Output results as JSON')
    parser.add_argument('--no-progress', action='store_true',
                        help='Disable progress bars (automatic when output is not a terminal)')

    # The same flags again on every subcommand, so `nekoshot sql --json` works as
    # naturally as `nekoshot --json sql`. SUPPRESS keeps a subcommand's unset flag
    # from overwriting one given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='Output results as JSON')
    common.add_argument('--no-progress', action='store_true', default=argparse.SUPPRESS,
                        help='Disable progress bars')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    capture_parser = subparsers.add_parser('capture', parents=[common], help='Capture a system snapshot ~nya')
    capture_parser.add_argument('--output', '-o', help='Output directory for the snapshot')
    capture_parser.add_argument('--compression', choices=['zstd', 'gzip', 'none'], help='Compression type')
    capture_parser.add_argument('--exclude', '-e', action='append', help='Additional patterns to exclude')
    capture_parser.add_argument('--profile', choices=list(PROFILES),
                                help="'desktop': whole PC incl. /boot/efi and separate /home, skips Docker, "
                                     "Steam game files and caches. 'server' (default): one filesystem, Docker included")
    capture_parser.add_argument('--encrypt', action='store_true',
                                help='Encrypt the snapshot (gpg AES256) with the passphrase in --passphrase-file')
    capture_parser.add_argument('--passphrase-file',
                                help=f'Root-only file holding the passphrase (or set ${PASSPHRASE_ENV})')
    capture_parser.add_argument('--pause-docker', action='store_true', help='Pause Docker containers during capture')
    capture_parser.add_argument('--no-docker', action='store_true', help='Skip Docker capture')
    capture_parser.add_argument('--docker-only', action='store_true',
                                help='Capture only Docker state (images, volumes, compose), skipping system files')
    capture_parser.add_argument('--prune-images', nargs='?', const='dangling',
                                choices=['dangling', 'all'], default=None,
                                help='Delete unused images before saving, to shrink the backup. '
                                     '--prune-images removes dangling (untagged) images; '
                                     '--prune-images all removes every image not used by a container, '
                                     'including locally-built images that cannot be re-pulled. '
                                     'Destructive: removes them from Docker, not just the archive.')
    capture_parser.add_argument('--incremental', action='store_true', help='Create incremental backup')
    capture_parser.add_argument('--link-dest', help='Previous backup for incremental mode')
    capture_parser.add_argument('--headless', action='store_true',
                                help='Run in the background and return immediately; check on it with: nekoshot status')
    capture_parser.add_argument('--log', help='Log file for a headless run (default: /var/log/nekoshot/capture-TIMESTAMP.log)')

    restore_parser = subparsers.add_parser('restore', parents=[common], help='Restore system from snapshot (live restore by default)')
    restore_parser.add_argument('--image', '-i', required=True, help='Path to snapshot archive (.tar.zst) or legacy image directory')
    restore_parser.add_argument('--passphrase-file', help='Passphrase file for an encrypted snapshot')
    restore_parser.add_argument('--target', '-t', help='Chroot restore to mounted target (e.g., /mnt/restore). Without this, restores to live system')
    restore_parser.add_argument('--fix-bootloader', help='Device to install bootloader (e.g., /dev/sda) [chroot mode only]')
    restore_parser.add_argument('--bootloader-type', choices=['grub', 'syslinux'], help='Bootloader type [chroot mode only]')
    restore_parser.add_argument('--skip-docker', action='store_true', help='Skip Docker restore')
    restore_parser.add_argument('--update-fstab', action='store_true', help='Update fstab with backup copy [chroot mode only]')
    restore_parser.add_argument('--update-initramfs', action='store_true', help='Regenerate initramfs [chroot mode only]')

    sql_parser = subparsers.add_parser('sql', parents=[common], help='Dump databases (Postgres, MySQL/MariaDB, SQL Server, SQLite)')
    sql_parser.add_argument('--output', '-o', help='Output directory (default: the configured sql location)')
    sql_parser.add_argument('--compression', choices=['zstd', 'gzip', 'none'], help='Compression type')
    sql_parser.add_argument('--no-docker', action='store_true', help='Do not look for databases in containers')
    sql_parser.add_argument('--no-native', action='store_true', help='Do not look for databases on the host')
    sql_parser.add_argument('--sqlite', action='append', help='Path to a SQLite file to back up (repeatable)')
    sql_parser.add_argument('--exclude', '-e', action='append', help='Container or server name to skip (repeatable)')
    sql_parser.add_argument('--headless', action='store_true', help='Run in the background; check with: nekoshot status')
    sql_parser.add_argument('--log', help='Log file for a headless run')

    status_parser = subparsers.add_parser('status', parents=[common], help='Show progress and ETA of a running backup')
    status_parser.add_argument('--all', '-a', action='store_true', help='Show every recent run, not just the current one')
    status_parser.add_argument('--watch', '-w', action='store_true', help='Keep refreshing until the backup finishes')
    status_parser.add_argument('--interval', '-i', type=float, default=2.0, help='Seconds between refreshes with --watch (default: 2)')

    list_parser = subparsers.add_parser('list', parents=[common], help='List available snapshots')
    list_parser.add_argument('--path', '-p', help='Path to snapshot stash')

    verify_parser = subparsers.add_parser('verify', parents=[common], help='Sniff-check snapshot integrity')
    verify_parser.add_argument('--image', '-i', required=True, help='Path to snapshot archive or legacy directory')

    prune_parser = subparsers.add_parser('prune', parents=[common], help='Knock old snapshots off the shelf')
    prune_parser.add_argument('--path', '-p', help='Path to snapshot stash')
    prune_parser.add_argument('--keep', '-k', type=int, help='Number of snapshots to keep')
    prune_parser.add_argument('--mode', choices=['all', 'full', 'docker-only', 'sql-only'], default='all',
                              help='Only prune snapshots of this type (default: all)')
    prune_parser.add_argument('--force', '-f', action='store_true', help='Skip confirmation prompt')

    schedule_parser = subparsers.add_parser('schedule', parents=[common], help='Manage scheduled backups (cron)')
    schedule_sub = schedule_parser.add_subparsers(dest='schedule_action', help='list | add | remove')
    schedule_sub.add_parser('list', parents=[common], help='List scheduled jobs')
    schedule_add_parser = schedule_sub.add_parser('add', parents=[common], help='Add or replace a scheduled job by name')
    schedule_add_parser.add_argument('name', help='Job name')
    schedule_add_parser.add_argument('cron', help='Cron expression, e.g. "0 2 * * *"')
    schedule_add_parser.add_argument('command_argv', nargs=argparse.REMAINDER,
                                     help='The nekoshot subcommand and args to run, e.g. capture --output /backup')
    schedule_remove_parser = schedule_sub.add_parser('remove', parents=[common], help='Remove a scheduled job by name')
    schedule_remove_parser.add_argument('name', help='Job name')

    subparsers.add_parser('menu', parents=[common], help='Interactive menu (also what you get by running nekoshot with no command)')

    return parser


ROOT_COMMANDS = ['capture', 'sql', 'restore', 'prune']


def dispatch(args, config: NekoShotConfig, parser) -> int:
    """Run the chosen command."""
    if args.command == 'menu':
        return cmd_menu(args, config, parser)

    if args.command == 'status' and args.watch:
        return cmd_watch(args, config)

    commands = {
        'capture': cmd_capture,
        'sql': cmd_sql,
        'restore': cmd_restore,
        'status': cmd_status,
        'list': cmd_list,
        'verify': cmd_verify,
        'prune': cmd_prune,
        'schedule': cmd_schedule,
    }

    return commands[args.command](args, config)


def main():
    # Checked against argv rather than parsed args because this prints before
    # argparse runs, which is what keeps the banner on `nekoshot --help`
    if '--json' not in sys.argv:
        print(NEKO_BANNER)

    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    config.json_output = args.json
    set_banners(not args.json)

    setup_logging(
        level=args.log_level,
        json_output=args.json
    )

    # Bars would be noise in a log file or in front of a JSON parser; those runs
    # get periodic log lines instead. Otherwise auto-detect the terminal.
    progress.configure(enabled=False if (args.json or args.no_progress) else None)

    if not args.command:
        # A person at a terminal gets the menu; a script gets the help it expects
        if sys.stdin.isatty() and sys.stdout.isatty() and not args.json:
            args.command = 'menu'
        else:
            parser.print_help()
            return 1

    if args.command in ROOT_COMMANDS:
        check_root()

    return dispatch(args, config, parser)


if __name__ == '__main__':
    sys.exit(main())
