import subprocess
import json
import shutil
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

from utils.logging import get_logger
from config import detect_distro

logger = get_logger('capture.metadata')


class MetadataCapture:
    """Captures system metadata for restoration reference."""

    def __init__(self):
        self.distro = detect_distro()

    def capture(self, image_dir: Path, mode: str = 'full') -> Dict[str, Any]:
        """
        Capture system metadata to the image directory.

        Args:
            image_dir: Directory to store metadata
            mode: 'full' for a whole-system snapshot; any partial mode records
                  just the manifest, since host-level metadata can't be restored
                  from a snapshot that holds no system files - collecting it
                  would only mislead

        Returns:
            Dictionary with capture statistics
        """
        metadata_dir = Path(image_dir) / 'metadata'
        metadata_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            'version': '1.0',
            'created': datetime.utcnow().isoformat() + 'Z',
            'distro': self.distro,
            'hostname': self._get_hostname(),
            'mode': mode,
        }

        if mode == 'full':
            self._capture_packages(metadata_dir)
            self._capture_partitions(metadata_dir)
            self._capture_fstab(metadata_dir)
            self._capture_network(metadata_dir)
            self._capture_services(metadata_dir)
            self._capture_bootloader(metadata_dir)

        manifest_file = image_dir / 'manifest.json'
        with open(manifest_file, 'w') as f:
            json.dump(manifest, f, indent=2)

        logger.info(f'Metadata all catalogued ~nya: {metadata_dir}')
        return manifest

    def _get_hostname(self) -> str:
        """Get system hostname."""
        try:
            return Path('/etc/hostname').read_text().strip()
        except Exception:
            result = subprocess.run(['hostname'], capture_output=True, text=True)
            return result.stdout.strip()

    def _capture_packages(self, metadata_dir: Path):
        """Capture installed packages list."""
        packages_file = metadata_dir / 'packages.txt'

        if self.distro == 'alpine':
            result = subprocess.run(
                ['apk', 'info', '-v'],
                capture_output=True,
                text=True
            )
        elif self.distro in ['ubuntu', 'debian']:
            result = subprocess.run(
                ['dpkg', '--get-selections'],
                capture_output=True,
                text=True
            )
        else:
            result = subprocess.run(
                ['cat', '/var/log/apt/history.log'],
                capture_output=True,
                text=True
            )

        with open(packages_file, 'w') as f:
            f.write(result.stdout)

        if self.distro in ['ubuntu', 'debian']:
            sources_file = metadata_dir / 'apt-sources.txt'
            result = subprocess.run(
                ['cat', '/etc/apt/sources.list'],
                capture_output=True,
                text=True
            )
            with open(sources_file, 'w') as f:
                f.write(result.stdout)

            sources_dir = Path('/etc/apt/sources.list.d')
            if sources_dir.exists():
                dest_dir = metadata_dir / 'sources.list.d'
                shutil.copytree(sources_dir, dest_dir, dirs_exist_ok=True)

        logger.info(f'Captured package list: {packages_file}')

    def _capture_partitions(self, metadata_dir: Path):
        """Capture partition layout and disk information."""
        partitions = {}

        lsblk_result = subprocess.run(
            ['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,UUID,PARTUUID'],
            capture_output=True,
            text=True
        )

        if lsblk_result.returncode == 0:
            try:
                partitions['lsblk'] = json.loads(lsblk_result.stdout)
            except json.JSONDecodeError:
                partitions['lsblk_raw'] = lsblk_result.stdout

        blkid_result = subprocess.run(
            ['blkid', '-o', 'export'],
            capture_output=True,
            text=True
        )
        partitions['blkid'] = blkid_result.stdout

        fdisk_result = subprocess.run(
            ['fdisk', '-l'],
            capture_output=True,
            text=True
        )
        partitions['fdisk'] = fdisk_result.stdout

        partitions_file = metadata_dir / 'partitions.json'
        with open(partitions_file, 'w') as f:
            json.dump(partitions, f, indent=2)

        logger.info('Captured partition information')

    def _capture_fstab(self, metadata_dir: Path):
        """Copy fstab configuration."""
        fstab_src = Path('/etc/fstab')
        if fstab_src.exists():
            shutil.copy2(fstab_src, metadata_dir / 'fstab')
            logger.info('Captured /etc/fstab')

        crypttab_src = Path('/etc/crypttab')
        if crypttab_src.exists():
            shutil.copy2(crypttab_src, metadata_dir / 'crypttab')

    def _capture_network(self, metadata_dir: Path):
        """Capture network configuration."""
        network_dir = metadata_dir / 'network'
        network_dir.mkdir(exist_ok=True)

        netplan_dir = Path('/etc/netplan')
        if netplan_dir.exists():
            for conf in netplan_dir.glob('*.yaml'):
                shutil.copy2(conf, network_dir / conf.name)

        interfaces_file = Path('/etc/network/interfaces')
        if interfaces_file.exists():
            shutil.copy2(interfaces_file, network_dir / 'interfaces')

        interfaces_dir = Path('/etc/network/interfaces.d')
        if interfaces_dir.exists():
            for conf in interfaces_dir.glob('*'):
                shutil.copy2(conf, network_dir / conf.name)

        resolv_conf = Path('/etc/resolv.conf')
        if resolv_conf.exists() and not resolv_conf.is_symlink():
            shutil.copy2(resolv_conf, network_dir / 'resolv.conf')

        hosts_file = Path('/etc/hosts')
        if hosts_file.exists():
            shutil.copy2(hosts_file, network_dir / 'hosts')

        ip_result = subprocess.run(
            ['ip', '-j', 'addr'],
            capture_output=True,
            text=True
        )
        if ip_result.returncode == 0:
            try:
                with open(network_dir / 'ip-addr.json', 'w') as f:
                    f.write(ip_result.stdout)
            except Exception:
                pass

        logger.info('Captured network configuration')

    def _capture_services(self, metadata_dir: Path):
        """Capture service states."""
        services = {}

        if shutil.which('systemctl'):
            result = subprocess.run(
                ['systemctl', 'list-unit-files', '--type=service', '--no-pager', '--output=json'],
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                try:
                    services['systemd'] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    result = subprocess.run(
                        ['systemctl', 'list-unit-files', '--type=service', '--no-pager'],
                        capture_output=True,
                        text=True
                    )
                    services['systemd_raw'] = result.stdout

        elif shutil.which('rc-status'):
            result = subprocess.run(
                ['rc-status', '-a'],
                capture_output=True,
                text=True
            )
            services['openrc'] = result.stdout

        services_file = metadata_dir / 'services.json'
        with open(services_file, 'w') as f:
            json.dump(services, f, indent=2)

        logger.info('Captured service states')

    def _capture_bootloader(self, metadata_dir: Path):
        """Capture bootloader configuration."""
        boot_dir = metadata_dir / 'boot'
        boot_dir.mkdir(exist_ok=True)

        grub_cfg = Path('/boot/grub/grub.cfg')
        if grub_cfg.exists():
            shutil.copy2(grub_cfg, boot_dir / 'grub.cfg')

        grub_default = Path('/etc/default/grub')
        if grub_default.exists():
            shutil.copy2(grub_default, boot_dir / 'grub_default')

        grub_d = Path('/etc/grub.d')
        if grub_d.exists():
            dest = boot_dir / 'grub.d'
            shutil.copytree(grub_d, dest, dirs_exist_ok=True)

        syslinux_cfg = Path('/boot/syslinux/syslinux.cfg')
        if syslinux_cfg.exists():
            shutil.copy2(syslinux_cfg, boot_dir / 'syslinux.cfg')

        extlinux_cfg = Path('/boot/extlinux/extlinux.conf')
        if extlinux_cfg.exists():
            shutil.copy2(extlinux_cfg, boot_dir / 'extlinux.conf')

        efi_dir = Path('/boot/efi/EFI')
        if efi_dir.exists() and shutil.which('efibootmgr'):
            efibootmgr_result = subprocess.run(
                ['efibootmgr', '-v'],
                capture_output=True,
                text=True
            )
            if efibootmgr_result.returncode == 0:
                with open(boot_dir / 'efibootmgr.txt', 'w') as f:
                    f.write(efibootmgr_result.stdout)

        logger.info('Captured bootloader configuration')
