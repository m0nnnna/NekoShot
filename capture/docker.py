import subprocess
import json
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import contextmanager

from utils.logging import get_logger
from utils.progress import (
    PhaseTracker, poll_file_size, pulse, directory_size, format_bytes
)
from config import CaptureConfig, DOCKER_COMPOSE_LOCATIONS

logger = get_logger('capture.docker')


class DockerCapture:
    """Handles Docker-specific capture including images, volumes, and configs."""

    def __init__(self, config: CaptureConfig):
        self.config = config
        self._docker_available = self._check_docker()

    def _check_docker(self) -> bool:
        """Check if Docker is available and running."""
        try:
            result = subprocess.run(
                ['docker', 'info'],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def is_available(self) -> bool:
        """Check if Docker capture is possible."""
        return self._docker_available

    def capture(self, image_dir: Path, tracker: Optional[PhaseTracker] = None) -> Dict[str, Any]:
        """
        Capture all Docker data to the image directory.

        Args:
            image_dir: Directory to store Docker backup
            tracker: Phase tracker for progress reporting

        Returns:
            Dictionary with capture statistics
        """
        if not self._docker_available:
            logger.warning('Docker not found, skipping ~nya')
            return {'skipped': True, 'reason': 'Docker not available'}

        docker_dir = Path(image_dir) / 'docker'
        docker_dir.mkdir(parents=True, exist_ok=True)

        stats = {
            'images': 0,
            'volumes': 0,
            'containers': 0,
            'compose_files': 0,
            'pruned': None,
        }

        # Prune before anything else so the whole snapshot reflects the trimmed
        # state. Done outside the pause window - it doesn't need containers paused,
        # and it only ever removes images no container references.
        if self.config.prune_images:
            stats['pruned'] = self._prune_images(self.config.prune_images)

        with self._pause_containers() if self.config.pause_docker else _noop_context():
            stats['containers'] = self._capture_container_metadata(docker_dir)
            stats['images'] = self._capture_images(docker_dir, tracker)
            stats['volumes'] = self._capture_volumes(docker_dir, tracker)
            stats['compose_files'] = self._capture_compose_files(docker_dir, tracker)

        self._capture_docker_config(docker_dir)

        logger.info(f'Docker capture complete ~nya: {stats}')
        return stats

    def _prune_images(self, level: str) -> Dict[str, Any]:
        """
        Remove unused images before saving, to shrink the backup.

        'dangling' removes only untagged leftover layers (the usual rebuild
        cruft - safe). 'all' removes every image not used by a container, running
        or stopped, which is aggressive and may force a re-pull later.

        This deletes from the live Docker, not just from the archive - it's the
        `docker image prune` you would run by hand, so it is strictly opt-in.
        """
        cmd = ['docker', 'image', 'prune', '-f']
        if level == 'all':
            cmd.append('-a')

        label = 'all unused' if level == 'all' else 'dangling'
        logger.info(f'Pruning {label} images before saving (this deletes them from Docker)...')

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            # A failed prune must not sink the backup - carry on and save what's there
            logger.warning(f'Image prune failed, continuing without it: {result.stderr.strip()}')
            return {'level': level, 'ok': False, 'error': result.stderr.strip()}

        reclaimed = ''
        for line in result.stdout.splitlines():
            if 'reclaimed' in line.lower():
                reclaimed = line.strip()

        logger.info(reclaimed if reclaimed else 'Nothing to prune ~nya')
        return {'level': level, 'ok': True, 'reclaimed': reclaimed}

    @contextmanager
    def _pause_containers(self):
        """Context manager to pause and unpause running containers."""
        running = self._get_running_containers()

        if running:
            logger.info(f'Pawsing {len(running)} containers for consistent capture')
            for container_id in running:
                subprocess.run(['docker', 'pause', container_id], capture_output=True)

        try:
            yield
        finally:
            if running:
                logger.info(f'Unpawsing {len(running)} containers')
                for container_id in running:
                    subprocess.run(['docker', 'unpause', container_id], capture_output=True)

    def _get_running_containers(self) -> List[str]:
        """Get list of running container IDs."""
        result = subprocess.run(
            ['docker', 'ps', '-q'],
            capture_output=True,
            text=True
        )
        return [c.strip() for c in result.stdout.strip().split('\n') if c.strip()]

    def _capture_container_metadata(self, docker_dir: Path) -> int:
        """Capture metadata about all containers."""
        result = subprocess.run(
            ['docker', 'ps', '-a', '--format', '{{json .}}'],
            capture_output=True,
            text=True
        )

        containers = []
        for line in result.stdout.strip().split('\n'):
            if line:
                containers.append(json.loads(line))

        container_details = []
        for container in containers:
            inspect_result = subprocess.run(
                ['docker', 'inspect', container.get('ID', container.get('Names', ''))],
                capture_output=True,
                text=True
            )
            if inspect_result.returncode == 0:
                details = json.loads(inspect_result.stdout)
                if details:
                    container_details.append(details[0])

        metadata_file = docker_dir / 'containers.json'
        with open(metadata_file, 'w') as f:
            json.dump(container_details, f, indent=2)

        logger.info(f'Captured metadata for {len(containers)} containers')
        return len(containers)

    def _capture_images(self, docker_dir: Path, tracker: Optional[PhaseTracker] = None) -> int:
        """Save all Docker images."""
        result = subprocess.run(
            ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
            capture_output=True,
            text=True
        )

        images = [img for img in result.stdout.strip().split('\n')
                  if img and img != '<none>:<none>']

        if not images:
            logger.info('No Docker images to capture')
            if tracker:
                tracker.phase('Saving Docker images').finish(summary='none to save')
            return 0

        images_file = docker_dir / 'images.tar'

        logger.info(f'Saving {len(images)} Docker images...')

        # No usable total here: docker save writes compressed blobs, so the sizes
        # from `docker images` overstate the archive by ~2.5x. Better to show
        # bytes and rate with no ETA than an ETA built on a bad total.
        bar = tracker.phase(f'Saving {len(images)} Docker images') if tracker else None

        # Stream to a file we open ourselves rather than using `docker save -o`:
        # with -o the archive only appears once the save finishes, so there is
        # nothing to watch while it runs.
        with open(images_file, 'wb') as handle:
            process = subprocess.Popen(
                ['docker', 'save'] + images,
                stdout=handle,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            if bar:
                with poll_file_size(bar, images_file):
                    _stdout, stderr = process.communicate()
            else:
                _stdout, stderr = process.communicate()

        if process.returncode != 0:
            if bar:
                bar.abandon()
            logger.error(f'Failed to save images: {stderr}')
            # A half-written archive would load as corrupt on restore
            images_file.unlink(missing_ok=True)
            return 0

        if bar:
            bar.finish()

        image_list_file = docker_dir / 'images.txt'
        with open(image_list_file, 'w') as f:
            f.write('\n'.join(images))

        logger.info(f'Saved {len(images)} images to {images_file}')
        return len(images)

    def _volume_size(self, volume: str) -> Optional[int]:
        """
        Size a volume by measuring its mountpoint.

        Readable as root on a normal Docker host; on setups where the daemon
        lives elsewhere (Docker Desktop, remote daemons) the path isn't visible
        and the volume simply goes unsized.
        """
        result = subprocess.run(
            ['docker', 'volume', 'inspect', '-f', '{{.Mountpoint}}', volume],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return None

        mountpoint = Path(result.stdout.strip())
        if not mountpoint.is_dir():
            return None

        return directory_size(mountpoint)

    def _capture_volumes(self, docker_dir: Path, tracker: Optional[PhaseTracker] = None) -> int:
        """Backup named Docker volumes."""
        result = subprocess.run(
            ['docker', 'volume', 'ls', '-q'],
            capture_output=True,
            text=True
        )

        volumes = [v.strip() for v in result.stdout.strip().split('\n') if v.strip()]

        if not volumes:
            logger.info('No Docker volumes to capture')
            if tracker:
                tracker.phase('Backing up Docker volumes').finish(summary='none to back up')
            return 0

        # Volume data whose name starts with '_' is skipped, but its metadata is
        # still recorded further down
        backup_volumes = [v for v in volumes if not v.startswith('_')]

        volumes_dir = docker_dir / 'volumes'
        volumes_dir.mkdir(exist_ok=True)

        sizes = {volume: self._volume_size(volume) for volume in backup_volumes}
        known = [size for size in sizes.values() if size]
        # Only claim a total when every volume could be sized, so the ETA
        # isn't built on a total that's missing whole volumes
        total = sum(known) if len(known) == len(backup_volumes) else None

        bar = tracker.phase(f'Backing up {len(backup_volumes)} Docker volumes', total=total) if tracker else None

        captured = 0
        banked = 0

        for volume in backup_volumes:
            logger.info(f'Backing up volume: {volume}')

            volume_file = volumes_dir / f'{volume}.tar'

            docker_cmd = [
                'docker', 'run', '--rm',
                '-v', f'{volume}:/source:ro',
                '-v', f'{volumes_dir}:/backup',
                'alpine',
                'tar', '-cf', f'/backup/{volume}.tar', '-C', '/source', '.'
            ]

            if bar:
                with poll_file_size(bar, volume_file, base=banked):
                    result = subprocess.run(docker_cmd, capture_output=True, text=True)
            else:
                result = subprocess.run(docker_cmd, capture_output=True, text=True)

            if result.returncode == 0:
                captured += 1
            else:
                logger.warning(f'Failed to backup volume {volume}: {result.stderr}')

            try:
                banked += volume_file.stat().st_size
            except OSError:
                pass
            if bar:
                bar.update(banked)

        if bar:
            bar.finish(summary=f'{captured}/{len(backup_volumes)} volumes, {format_bytes(banked)}')

        volume_info = []
        for volume in volumes:
            inspect_result = subprocess.run(
                ['docker', 'volume', 'inspect', volume],
                capture_output=True,
                text=True
            )
            if inspect_result.returncode == 0:
                volume_info.extend(json.loads(inspect_result.stdout))

        with open(docker_dir / 'volumes.json', 'w') as f:
            json.dump(volume_info, f, indent=2)

        logger.info(f'Backed up {captured}/{len(volumes)} volumes')
        return captured

    def _capture_compose_files(self, docker_dir: Path, tracker: Optional[PhaseTracker] = None) -> int:
        """Find and copy docker-compose files."""
        compose_dir = docker_dir / 'compose'
        compose_dir.mkdir(exist_ok=True)

        patterns = [
            'docker-compose.yml',
            'docker-compose.yaml',
            'compose.yml',
            'compose.yaml',
            '.env',
        ]

        # This walk has no measurable total, and on a host with a large /home it
        # can run for minutes (see the compose-scan performance bug), so the bar
        # is pulsed to show it is working rather than hung
        bar = tracker.phase('Scanning for compose files', unit='count') if tracker else None

        found = 0
        try:
            with pulse(bar) if bar else _noop_context():
                for search_path in self.config.docker_compose_paths:
                    search_path = Path(search_path)
                    if not search_path.exists():
                        continue

                    if bar:
                        bar.describe(f'Scanning {search_path} for compose files')

                    for pattern in patterns:
                        for match in search_path.rglob(pattern):
                            if '.git' in str(match) or 'node_modules' in str(match):
                                continue

                            rel_path = match.relative_to(search_path)
                            dest = compose_dir / search_path.name / rel_path
                            dest.parent.mkdir(parents=True, exist_ok=True)

                            shutil.copy2(match, dest)
                            found += 1
                            if bar:
                                bar.advance()
                            logger.debug(f'Copied compose file: {match}')
        finally:
            if bar:
                bar.finish(summary=f'{found} files')

        logger.info(f'Found and copied {found} compose-related files')
        return found

    def _capture_docker_config(self, docker_dir: Path):
        """Capture Docker daemon configuration."""
        config_sources = [
            Path('/etc/docker/daemon.json'),
            Path('/var/lib/docker/config.json'),
            Path.home() / '.docker' / 'config.json',
        ]

        config_dir = docker_dir / 'config'
        config_dir.mkdir(exist_ok=True)

        for src in config_sources:
            if src.exists():
                dest = config_dir / src.name
                shutil.copy2(src, dest)
                logger.debug(f'Copied Docker config: {src}')

        network_result = subprocess.run(
            ['docker', 'network', 'ls', '--format', '{{json .}}'],
            capture_output=True,
            text=True
        )

        networks = []
        for line in network_result.stdout.strip().split('\n'):
            if line:
                networks.append(json.loads(line))

        with open(config_dir / 'networks.json', 'w') as f:
            json.dump(networks, f, indent=2)


@contextmanager
def _noop_context():
    """No-operation context manager."""
    yield
