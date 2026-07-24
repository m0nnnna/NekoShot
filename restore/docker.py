import subprocess
import json
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional

from utils.logging import get_logger
from utils.progress import PhaseTracker, poll_read_offset, format_bytes
from config import RestoreConfig

logger = get_logger('restore.docker')


class DockerRestore:
    """Handles Docker state restoration from backup."""

    def __init__(self, config: RestoreConfig):
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
        """Check if Docker restore is possible."""
        return self._docker_available

    def restore(self, image_dir: Path, tracker: Optional[PhaseTracker] = None) -> Dict[str, Any]:
        """
        Restore Docker state from the image directory.

        Args:
            image_dir: Path to the image directory
            tracker: Phase tracker for progress reporting

        Returns:
            Dictionary with restore statistics
        """
        if not self._docker_available:
            logger.warning('Docker not available, skipping Docker restore')
            return {'skipped': True, 'reason': 'Docker not available'}

        docker_dir = Path(image_dir) / 'docker'

        if not docker_dir.exists():
            logger.warning('No Docker backup found in image')
            return {'skipped': True, 'reason': 'No Docker backup found'}

        stats = {
            'images_loaded': 0,
            'volumes_restored': 0,
            'networks_created': 0,
        }

        stats['images_loaded'] = self._restore_images(docker_dir, tracker)
        stats['volumes_restored'] = self._restore_volumes(docker_dir, tracker)
        stats['networks_created'] = self._restore_networks(docker_dir)
        self._restore_compose_files(docker_dir)

        logger.info(f'Docker restore complete ~nya: {stats}')
        return stats

    def _restore_images(self, docker_dir: Path, tracker: Optional[PhaseTracker] = None) -> int:
        """Load Docker images from backup."""
        images_file = docker_dir / 'images.tar'

        if not images_file.exists():
            logger.info('No images.tar found, skipping image restore')
            if tracker:
                tracker.phase('Loading Docker images').finish(summary='nothing to load')
            return 0

        logger.info(f'Loading Docker images from {images_file}...')

        total = images_file.stat().st_size
        bar = tracker.phase('Loading Docker images', total=total) if tracker else None

        # `docker load` says nothing useful without a TTY, so track how far it has
        # read into the archive instead
        proc = subprocess.Popen(
            ['docker', 'load', '-i', str(images_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        if bar:
            with poll_read_offset(bar, proc.pid, images_file):
                stdout, stderr = proc.communicate()
        else:
            stdout, stderr = proc.communicate()

        if proc.returncode != 0:
            if bar:
                bar.abandon()
            logger.error(f'Failed to load images: {stderr}')
            return 0

        loaded = stdout.count('Loaded image')

        if bar:
            bar.finish(summary=f'{loaded} images, {format_bytes(total)}')

        logger.info(f'Loaded {loaded} images')
        return loaded

    def _restore_volumes(self, docker_dir: Path, tracker: Optional[PhaseTracker] = None) -> int:
        """Restore Docker volumes from backup."""
        volumes_dir = docker_dir / 'volumes'

        if not volumes_dir.exists():
            logger.info('No volumes backup found')
            if tracker:
                tracker.phase('Restoring Docker volumes').finish(summary='nothing to restore')
            return 0

        volumes_info_file = docker_dir / 'volumes.json'
        volume_metadata = {}

        if volumes_info_file.exists():
            with open(volumes_info_file, 'r') as f:
                volume_list = json.load(f)
                for vol in volume_list:
                    volume_metadata[vol.get('Name')] = vol

        restored = 0

        volume_tars = sorted(volumes_dir.glob('*.tar'))
        # Every tar has to be read in full, so their combined size is the total
        total = sum(tar.stat().st_size for tar in volume_tars) or None
        bar = tracker.phase(f'Restoring {len(volume_tars)} Docker volumes', total=total) if tracker else None
        banked = 0

        for volume_tar in volume_tars:
            volume_name = volume_tar.stem

            logger.info(f'Restoring volume: {volume_name}')
            if bar:
                # tar runs inside the container, out of reach of the host's /proc,
                # so this advances a whole volume at a time
                bar.describe(f'Restoring volume {volume_name}')

            check_result = subprocess.run(
                ['docker', 'volume', 'inspect', volume_name],
                capture_output=True
            )

            if check_result.returncode != 0:
                create_cmd = ['docker', 'volume', 'create', volume_name]

                if volume_name in volume_metadata:
                    vol_info = volume_metadata[volume_name]
                    driver = vol_info.get('Driver', 'local')
                    if driver != 'local':
                        create_cmd.extend(['--driver', driver])

                subprocess.run(create_cmd, capture_output=True)

            result = subprocess.run([
                'docker', 'run', '--rm',
                '-v', f'{volume_name}:/target',
                '-v', f'{volume_tar}:/backup.tar:ro',
                'alpine',
                'sh', '-c', 'rm -rf /target/* && tar -xf /backup.tar -C /target'
            ], capture_output=True, text=True)

            if result.returncode == 0:
                restored += 1
                logger.info(f'Restored volume: {volume_name}')
            else:
                logger.warning(f'Failed to restore volume {volume_name}: {result.stderr}')

            banked += volume_tar.stat().st_size
            if bar:
                bar.update(banked)

        if bar:
            bar.finish(summary=f'{restored}/{len(volume_tars)} volumes')

        return restored

    def _restore_networks(self, docker_dir: Path) -> int:
        """Recreate custom Docker networks."""
        networks_file = docker_dir / 'config' / 'networks.json'

        if not networks_file.exists():
            return 0

        with open(networks_file, 'r') as f:
            networks = json.load(f)

        created = 0
        skip_networks = ['bridge', 'host', 'none']

        for network in networks:
            name = network.get('Name', '')

            if name in skip_networks or name.startswith('docker_'):
                continue

            check_result = subprocess.run(
                ['docker', 'network', 'inspect', name],
                capture_output=True
            )

            if check_result.returncode == 0:
                logger.debug(f'Network {name} already exists')
                continue

            driver = network.get('Driver', 'bridge')

            result = subprocess.run(
                ['docker', 'network', 'create', '--driver', driver, name],
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                created += 1
                logger.info(f'Created network: {name}')

        return created

    def _restore_compose_files(self, docker_dir: Path):
        """Restore docker-compose files to their original locations."""
        compose_dir = docker_dir / 'compose'

        if not compose_dir.exists():
            return

        logger.info('Docker compose files are available in the backup.')
        logger.info(f'Location: {compose_dir}')
        logger.info('Review and manually copy compose files to appropriate locations.')

        for item in compose_dir.iterdir():
            if item.is_dir():
                logger.info(f'  - {item.name}/')

    def show_container_info(self, image_dir: Path):
        """Display information about backed up containers."""
        containers_file = Path(image_dir) / 'docker' / 'containers.json'

        if not containers_file.exists():
            logger.info('No container metadata found')
            return

        with open(containers_file, 'r') as f:
            containers = json.load(f)

        logger.info(f'\nBacked up containers ({len(containers)}):')
        logger.info('-' * 60)

        for container in containers:
            name = container.get('Name', '').lstrip('/')
            image = container.get('Config', {}).get('Image', 'unknown')
            state = container.get('State', {}).get('Status', 'unknown')

            logger.info(f'  {name}')
            logger.info(f'    Image: {image}')
            logger.info(f'    State at backup: {state}')

            mounts = container.get('Mounts', [])
            if mounts:
                logger.info(f'    Volumes:')
                for mount in mounts:
                    if mount.get('Type') == 'volume':
                        logger.info(f'      - {mount.get("Name")} -> {mount.get("Destination")}')

        logger.info('-' * 60)
        logger.info('\nTo recreate containers, use your docker-compose files or')
        logger.info('docker run commands. Images and volumes have been restored ~nya!')
