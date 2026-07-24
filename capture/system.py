import subprocess
import shutil
import socket
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from utils.logging import get_logger
from utils.progress import PhaseTracker, run_rsync_with_progress
from config import CaptureConfig, get_default_excludes

logger = get_logger('capture.system')


def get_hostname() -> str:
    """Get system hostname for archive naming."""
    try:
        return Path('/etc/hostname').read_text().strip()
    except Exception:
        return socket.gethostname()


def create_image_dir(output_dir: Path) -> Path:
    """Create a timestamped working directory for a snapshot."""
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    image_dir = Path(output_dir) / f'image-{timestamp}'
    image_dir.mkdir(parents=True, exist_ok=True)
    return image_dir


class SystemCapture:
    """Handles system-level filesystem capture using rsync."""

    def __init__(self, config: CaptureConfig):
        self.config = config
        self._check_requirements()

    def _check_requirements(self):
        """Verify required tools are available."""
        if not shutil.which('rsync'):
            raise RuntimeError('rsync is required but not found. Install with: apt install rsync / apk add rsync')

    def capture(
        self,
        output_dir: Path,
        source_root: str = '/',
        incremental_base: Optional[Path] = None,
        extra_excludes: Optional[List[str]] = None,
        tracker: Optional[PhaseTracker] = None
    ) -> Path:
        """
        Capture the system filesystem to a working directory.

        The working directory contains a raw system/ subdirectory.
        Final compression into a single archive is handled by the caller.

        Args:
            output_dir: Directory to store the working image
            source_root: Root directory to capture (default: /)
            incremental_base: Previous backup for incremental mode (hardlinks unchanged files)
            extra_excludes: Additional patterns to exclude
            tracker: Phase tracker for progress reporting

        Returns:
            Path to the created image directory (working dir)
        """
        image_dir = create_image_dir(output_dir)
        system_dir = image_dir / 'system'
        system_dir.mkdir(exist_ok=True)

        excludes = self.config.exclude_patterns.copy()
        if extra_excludes:
            excludes.extend(extra_excludes)

        excludes.append(str(output_dir) + '/*')

        logger.info(f'Pouncing on system files from {source_root}')
        logger.info(f'Excluding {len(excludes)} patterns')

        try:
            self._run_rsync(
                source=source_root,
                dest=system_dir,
                excludes=excludes,
                link_dest=incremental_base / 'system' if incremental_base else None,
                tracker=tracker
            )

            logger.info(f'System capture complete ~nya')
            return image_dir

        except Exception as e:
            logger.error(f'System capture failed! Hissss: {e}')
            if image_dir.exists():
                shutil.rmtree(image_dir)
            raise

    def _run_rsync(
        self,
        source: str,
        dest: Path,
        excludes: List[str],
        link_dest: Optional[Path] = None,
        tracker: Optional[PhaseTracker] = None
    ):
        """Execute rsync with the given parameters."""
        cmd = [
            'rsync',
            '-aAXHx',
            '--info=progress2',
            '--numeric-ids',
        ]

        for pattern in excludes:
            cmd.extend(['--exclude', pattern])

        if link_dest and link_dest.exists():
            cmd.extend(['--link-dest', str(link_dest.resolve())])
            logger.info(f'Using incremental mode with base: {link_dest}')

        if not source.endswith('/'):
            source = source + '/'

        cmd.extend([source, str(dest) + '/'])

        logger.debug(f'Running: {" ".join(cmd)}')

        bar = tracker.phase('Capturing system files') if tracker else None

        returncode = run_rsync_with_progress(cmd, bar, logger)

        if returncode not in [0, 24]:
            if bar:
                bar.abandon()
            raise RuntimeError(f'rsync failed with code {returncode}')

        if bar:
            bar.finish()


def get_disk_usage(path: str = '/') -> dict:
    """Get disk usage statistics for a path."""
    result = subprocess.run(
        ['df', '-B1', path],
        capture_output=True,
        text=True
    )

    lines = result.stdout.strip().split('\n')
    if len(lines) < 2:
        return {}

    parts = lines[1].split()
    return {
        'device': parts[0],
        'total': int(parts[1]),
        'used': int(parts[2]),
        'available': int(parts[3]),
        'percent': parts[4],
        'mount': parts[5] if len(parts) > 5 else path
    }
