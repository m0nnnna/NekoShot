import subprocess
import shutil
from pathlib import Path
from typing import Optional

from utils.logging import get_logger
from config import RestoreConfig, detect_distro

logger = get_logger('restore.bootloader')


class BootloaderRestore:
    """Handles bootloader reinstallation after system restore."""

    def __init__(self, config: RestoreConfig):
        self.config = config
        self.distro = detect_distro()

    def restore(
        self,
        target_dir: Path,
        device: str,
        bootloader_type: Optional[str] = None
    ) -> bool:
        """
        Reinstall bootloader on the target system.

        Args:
            target_dir: Mount point of the restored system
            device: Device to install bootloader (e.g., /dev/sda)
            bootloader_type: 'grub', 'syslinux', or None for auto-detect

        Returns:
            True if successful
        """
        target_dir = Path(target_dir)

        if not target_dir.exists():
            raise ValueError(f'Target directory not found: {target_dir}')

        if not Path(device).exists() and not device.startswith('/dev/'):
            device = f'/dev/{device}'

        if not bootloader_type:
            bootloader_type = self._detect_bootloader(target_dir)

        logger.info(f'Installing {bootloader_type} bootloader to {device}')

        self._mount_virtual_filesystems(target_dir)

        try:
            if bootloader_type == 'grub':
                return self._install_grub(target_dir, device)
            elif bootloader_type == 'syslinux':
                return self._install_syslinux(target_dir, device)
            else:
                logger.error(f'Unknown bootloader type: {bootloader_type}')
                return False
        finally:
            self._unmount_virtual_filesystems(target_dir)

    def _detect_bootloader(self, target_dir: Path) -> str:
        """Detect which bootloader the system uses."""
        if (target_dir / 'boot' / 'grub').exists():
            return 'grub'
        if (target_dir / 'boot' / 'grub2').exists():
            return 'grub'
        if (target_dir / 'boot' / 'syslinux').exists():
            return 'syslinux'
        if (target_dir / 'boot' / 'extlinux').exists():
            return 'syslinux'

        return 'grub'

    def _mount_virtual_filesystems(self, target_dir: Path):
        """Mount virtual filesystems required for chroot."""
        mounts = [
            ('proc', '-t proc', 'proc'),
            ('sys', '-t sysfs', 'sys'),
            ('dev', '--bind', 'dev'),
            ('dev/pts', '--bind', 'dev/pts'),
            ('run', '--bind', 'run'),
        ]

        for src, opts, dest in mounts:
            mount_point = target_dir / dest
            mount_point.mkdir(parents=True, exist_ok=True)

            if '--bind' in opts:
                cmd = ['mount', '--bind', f'/{src}', str(mount_point)]
            else:
                cmd = ['mount'] + opts.split() + [src, str(mount_point)]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                logger.warning(f'Failed to mount {dest}: {result.stderr.decode()}')

        efi_dir = Path('/sys/firmware/efi')
        if efi_dir.exists():
            boot_efi = target_dir / 'boot' / 'efi'
            if boot_efi.exists():
                efi_part = self._find_efi_partition()
                if efi_part:
                    subprocess.run(
                        ['mount', efi_part, str(boot_efi)],
                        capture_output=True
                    )

    def _unmount_virtual_filesystems(self, target_dir: Path):
        """Unmount virtual filesystems."""
        mounts = ['dev/pts', 'dev', 'proc', 'sys', 'run', 'boot/efi']

        for mount in mounts:
            mount_point = target_dir / mount
            if mount_point.exists():
                subprocess.run(
                    ['umount', '-l', str(mount_point)],
                    capture_output=True
                )

    def _find_efi_partition(self) -> Optional[str]:
        """Find the EFI system partition."""
        result = subprocess.run(
            ['lsblk', '-o', 'NAME,PARTTYPE', '-n', '-l'],
            capture_output=True,
            text=True
        )

        efi_guid = 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b'

        for line in result.stdout.strip().split('\n'):
            parts = line.split()
            if len(parts) >= 2 and parts[1].lower() == efi_guid:
                return f'/dev/{parts[0]}'

        return None

    def _install_grub(self, target_dir: Path, device: str) -> bool:
        """Install GRUB bootloader."""
        is_efi = Path('/sys/firmware/efi').exists()

        if is_efi:
            logger.info('Detected EFI system, installing GRUB for EFI')
            grub_install_cmd = [
                'chroot', str(target_dir),
                'grub-install',
                '--target=x86_64-efi',
                '--efi-directory=/boot/efi',
                '--bootloader-id=GRUB',
                '--recheck'
            ]
        else:
            logger.info('Detected BIOS system, installing GRUB for BIOS')
            grub_install_cmd = [
                'chroot', str(target_dir),
                'grub-install',
                '--target=i386-pc',
                '--recheck',
                device
            ]

        result = subprocess.run(
            grub_install_cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.error(f'grub-install failed: {result.stderr}')
            return False

        logger.info('Regenerating GRUB configuration...')

        grub_mkconfig_cmd = [
            'chroot', str(target_dir),
            'grub-mkconfig', '-o', '/boot/grub/grub.cfg'
        ]

        result = subprocess.run(
            grub_mkconfig_cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.warning(f'grub-mkconfig warning: {result.stderr}')

        logger.info('GRUB installation complete ~nya')
        return True

    def _install_syslinux(self, target_dir: Path, device: str) -> bool:
        """Install syslinux/extlinux bootloader."""
        boot_dir = target_dir / 'boot'

        if (boot_dir / 'extlinux').exists():
            install_cmd = [
                'chroot', str(target_dir),
                'extlinux', '--install', '/boot/extlinux'
            ]
        else:
            install_cmd = [
                'chroot', str(target_dir),
                'syslinux', '--install', '/boot/syslinux'
            ]

        result = subprocess.run(
            install_cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.error(f'syslinux install failed: {result.stderr}')
            return False

        logger.info('Installing MBR...')

        mbr_paths = [
            '/usr/lib/syslinux/mbr/mbr.bin',
            '/usr/share/syslinux/mbr.bin',
            '/usr/lib/syslinux/bios/mbr.bin',
        ]

        mbr_src = None
        for path in mbr_paths:
            full_path = target_dir / path.lstrip('/')
            if full_path.exists():
                mbr_src = full_path
                break

        if mbr_src:
            dd_cmd = ['dd', f'if={mbr_src}', f'of={device}', 'bs=440', 'count=1']
            result = subprocess.run(dd_cmd, capture_output=True)

            if result.returncode != 0:
                logger.warning('MBR installation may have failed')
        else:
            logger.warning('MBR file not found, skipping MBR installation')

        logger.info('Syslinux installation complete ~nya')
        return True

    def update_initramfs(self, target_dir: Path) -> bool:
        """Regenerate initramfs on the target system."""
        target_dir = Path(target_dir)

        self._mount_virtual_filesystems(target_dir)

        try:
            if self.distro in ['ubuntu', 'debian']:
                cmd = ['chroot', str(target_dir), 'update-initramfs', '-u', '-k', 'all']
            elif self.distro == 'alpine':
                cmd = ['chroot', str(target_dir), 'mkinitfs']
            else:
                kernel_version = self._get_kernel_version(target_dir)
                cmd = ['chroot', str(target_dir), 'dracut', '-f', '--kver', kernel_version]

            logger.info('Regenerating initramfs...')
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                logger.warning(f'initramfs regeneration warning: {result.stderr}')

            return True

        finally:
            self._unmount_virtual_filesystems(target_dir)

    def _get_kernel_version(self, target_dir: Path) -> str:
        """Get installed kernel version."""
        modules_dir = target_dir / 'lib' / 'modules'

        if modules_dir.exists():
            versions = sorted(modules_dir.iterdir(), reverse=True)
            if versions:
                return versions[0].name

        return ''
