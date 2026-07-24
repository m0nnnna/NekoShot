import subprocess
import shutil
from pathlib import Path
from typing import Optional, List

from utils.logging import get_logger
from utils.compression import decompress_archive
from utils.progress import PhaseTracker, run_rsync_with_progress
from config import RestoreConfig

logger = get_logger('restore.system')


class SystemRestore:
    """Handles system filesystem restoration."""

    def __init__(self, config: RestoreConfig):
        self.config = config
        self._check_requirements()

    def _check_requirements(self):
        """Verify required tools are available."""
        if not shutil.which('rsync'):
            raise RuntimeError('rsync is required but not found')

    def restore(
        self,
        image_dir: Path,
        target_dir: Path,
        exclude_patterns: Optional[List[str]] = None,
        tracker: Optional[PhaseTracker] = None
    ) -> bool:
        """
        Restore system files from an image to the target directory.

        Args:
            image_dir: Path to the image directory
            target_dir: Target mount point for restoration
            exclude_patterns: Patterns to exclude during restore
            tracker: Phase tracker for progress reporting

        Returns:
            True if successful
        """
        image_dir = Path(image_dir)
        target_dir = Path(target_dir)

        if not image_dir.exists():
            raise ValueError(f'Image directory not found: {image_dir}')

        if not target_dir.exists():
            raise ValueError(f'Target directory not found: {target_dir}')

        system_dir = image_dir / 'system'

        if system_dir.exists():
            # New format: system/ directory (from single archive extraction)
            logger.info(f'Restoring from system directory to {target_dir}')
            self._rsync_to_target(system_dir, target_dir, exclude_patterns, tracker=tracker)
        else:
            # Legacy format: separate system archive
            system_archive = self._find_system_archive(image_dir)
            if system_archive:
                logger.info(f'Unpacking system archive directly to {target_dir}')
                decompress_archive(system_archive, target_dir, strip_components=1, tracker=tracker)
            else:
                raise ValueError(f'No system archive or directory found in {image_dir}')

        logger.info(f'System restore complete to {target_dir} ~nya')
        return True

    def live_restore(self, image_dir: Path, tracker: Optional[PhaseTracker] = None) -> bool:
        """
        Restore to a live running system.

        If system/ directory exists (new single-archive format), rsyncs directly.
        If a system archive exists (legacy format), extracts to staging first.

        Args:
            image_dir: Path to the image directory
            tracker: Phase tracker for progress reporting

        Returns:
            True if successful
        """
        image_dir = Path(image_dir)

        if not image_dir.exists():
            raise ValueError(f'Image directory not found: {image_dir}')

        system_dir = image_dir / 'system'
        system_archive = self._find_system_archive(image_dir)

        # New format: system/ directory already exists (extracted from single archive)
        if system_dir.exists():
            logger.info('Syncing to live system...')
            self._rsync_live(system_dir, tracker=tracker)
            logger.info('Live restore complete ~nya')
            return True

        # Legacy format: extract system archive to staging, then rsync
        if system_archive:
            # Stage next to archive to avoid /tmp tmpfs limits on Alpine
            staging_dir = system_archive.parent / '.nekoshot_live_staging'
            if staging_dir.exists():
                logger.info(f'Cleaning up old napping spot...')
                shutil.rmtree(staging_dir)
            staging_dir.mkdir(parents=True)

            try:
                logger.info(f'Step 1/2: Unpacking to staging area...')
                decompress_archive(system_archive, staging_dir, strip_components=1)

                logger.info('')
                logger.info('Step 2/2: Syncing to live system...')
                self._rsync_live(staging_dir, tracker=tracker)

            finally:
                logger.info('Tidying up staging directory...')
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

            logger.info('Live restore complete ~nya')
            return True

        raise ValueError(f'No system archive or directory found in {image_dir}')

    def _rsync_live(self, source_dir: Path, tracker: Optional[PhaseTracker] = None):
        """
        Rsync from staging to live root filesystem.

        Excludes critical directories that shouldn't be touched on a running system.
        """
        cmd = [
            'rsync',
            '-aAXH',
            '--info=progress2',
            '--numeric-ids',
        ]

        # Exclude directories that must not be overwritten on a live system
        live_excludes = [
            # Virtual/runtime filesystems
            '/proc/*',
            '/sys/*',
            '/dev/*',
            '/run/*',
            '/tmp/*',
            '/mnt/*',
            '/media/*',
            '/lost+found',
            '/swapfile',
            # Exclude the staging directory itself
            '/tmp/nekoshot_live_restore',
            # Boot-critical files with machine-specific UUIDs
            '/boot/grub/grub.cfg',
            '/boot/grub/grubenv',
            '/boot/efi/*',
            '/etc/fstab',
            # Machine identity (causes issues if duplicated)
            '/etc/machine-id',
            '/var/lib/dbus/machine-id',
            # Kernel/initramfs (keep target's versions)
            '/boot/vmlinuz*',
            '/boot/initrd*',
            '/boot/initramfs*',
        ]

        for pattern in live_excludes:
            cmd.extend(['--exclude', pattern])

        source = str(source_dir) + '/'
        cmd.extend([source, '/'])

        logger.info('Syncing files to live system (grab a catnap, this may take a while)...')
        logger.info('Pawserving: bootloader, fstab, kernel, initramfs, machine-id')
        logger.debug(f'Running: {" ".join(cmd)}')

        bar = tracker.phase('Syncing to live system') if tracker else None

        returncode = run_rsync_with_progress(cmd, bar, logger)

        if returncode not in [0, 24]:
            if bar:
                bar.abandon()
            raise RuntimeError(f'rsync failed with code {returncode}')

        if bar:
            bar.finish()

        logger.info('Sync complete ~nya')

    def _find_system_archive(self, image_dir: Path) -> Optional[Path]:
        """Find the system archive in the image directory."""
        # Check legacy naming first (system.tar.zst)
        for ext in ['.tar.zst', '.tar.gz', '.tar']:
            archive = image_dir / f'system{ext}'
            if archive.exists():
                return archive

        # Find hostname-date named archives (new format)
        for ext in ['.tar.zst', '.tar.gz', '.tar']:
            matches = list(image_dir.glob(f'*{ext}'))
            # Filter out docker/metadata archives, only match top-level system archives
            for match in matches:
                if match.parent == image_dir and match.name != f'checksums.sha256':
                    return match

        return None

    def _rsync_to_target(
        self,
        source_dir: Path,
        target_dir: Path,
        exclude_patterns: Optional[List[str]] = None,
        tracker: Optional[PhaseTracker] = None
    ):
        """Rsync files to target with proper options."""
        cmd = [
            'rsync',
            '-aAXH',
            '--info=progress2',
            '--numeric-ids',
        ]

        default_excludes = [
            '/proc/*',
            '/sys/*',
            '/dev/*',
            '/run/*',
            '/tmp/*',
        ]

        for pattern in default_excludes:
            cmd.extend(['--exclude', pattern])

        if exclude_patterns:
            for pattern in exclude_patterns:
                cmd.extend(['--exclude', pattern])

        source = str(source_dir)
        if not source.endswith('/'):
            source = source + '/'

        target = str(target_dir)
        if not target.endswith('/'):
            target = target + '/'

        cmd.extend([source, target])

        logger.debug(f'Running: {" ".join(cmd)}')

        bar = tracker.phase(f'Restoring system files to {target_dir}') if tracker else None

        returncode = run_rsync_with_progress(cmd, bar, logger)

        if returncode not in [0, 24]:
            if bar:
                bar.abandon()
            raise RuntimeError(f'rsync failed with code {returncode}')

        if bar:
            bar.finish()

    def create_required_dirs(self, target_dir: Path):
        """Create required system directories on target."""
        target_dir = Path(target_dir)

        required_dirs = [
            'proc', 'sys', 'dev', 'run', 'tmp', 'mnt', 'media',
            'var/run', 'var/lock', 'var/tmp'
        ]

        for dir_name in required_dirs:
            dir_path = target_dir / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)

        (target_dir / 'tmp').chmod(0o1777)
        (target_dir / 'var' / 'tmp').chmod(0o1777)

        logger.info('Created required system directories ~nya')

    def update_fstab(self, target_dir: Path, image_dir: Path):
        """
        Update fstab with new UUIDs if disk has changed.

        This is a basic implementation - complex setups may need manual adjustment.
        """
        target_fstab = target_dir / 'etc' / 'fstab'
        backup_fstab = image_dir / 'metadata' / 'fstab'

        if not backup_fstab.exists():
            logger.warning('No fstab backup found, skipping fstab update')
            return

        if target_fstab.exists():
            shutil.copy2(target_fstab, str(target_fstab) + '.bak')

        result = subprocess.run(
            ['blkid', '-o', 'export'],
            capture_output=True,
            text=True
        )

        current_uuids = {}
        current_device = None

        for line in result.stdout.split('\n'):
            if line.startswith('DEVNAME='):
                current_device = line.split('=')[1]
            elif line.startswith('UUID=') and current_device:
                current_uuids[current_device] = line.split('=')[1]

        shutil.copy2(backup_fstab, target_fstab)

        logger.info('Restored fstab (may need manual UUID updates for different disks)')

    def chroot_command(self, target_dir: Path, command: List[str]) -> subprocess.CompletedProcess:
        """Run a command in chroot environment."""
        target_dir = Path(target_dir)

        mount_points = [
            ('proc', 'proc', '/proc'),
            ('sysfs', 'sysfs', '/sys'),
            ('devtmpfs', 'udev', '/dev'),
            ('devpts', 'devpts', '/dev/pts'),
        ]

        for fstype, src, mount in mount_points:
            mount_path = target_dir / mount.lstrip('/')
            mount_path.mkdir(parents=True, exist_ok=True)
            subprocess.run(['mount', '-t', fstype, src, str(mount_path)], capture_output=True)

        try:
            result = subprocess.run(
                ['chroot', str(target_dir)] + command,
                capture_output=True,
                text=True
            )
            return result
        finally:
            for _, _, mount in reversed(mount_points):
                mount_path = target_dir / mount.lstrip('/')
                subprocess.run(['umount', str(mount_path)], capture_output=True)
