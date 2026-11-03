"""Passphrase encryption for snapshots (gpg symmetric, AES256).

gpg is on every Ubuntu and on live ISOs, and takes the passphrase from a file, so
scheduled runs need no prompt. The passphrase file should be root-only (0600); keep
a second copy somewhere safe: without it an encrypted snapshot cannot be opened.
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

ENCRYPTED_SUFFIX = '.gpg'
PASSPHRASE_ENV = 'NEKOSHOT_PASSPHRASE_FILE'


def resolve_passphrase_file(configured: Optional[str] = None) -> Optional[Path]:
    """Explicit argument/config first, then $NEKOSHOT_PASSPHRASE_FILE."""
    value = configured or os.environ.get(PASSPHRASE_ENV)
    return Path(value) if value else None


def check_passphrase_file(path: Optional[Path]) -> None:
    """Raise a readable error if the passphrase file is unusable."""
    if not shutil.which('gpg'):
        raise RuntimeError('gpg is required for encrypted snapshots (apt install gnupg)')
    if path is None:
        raise RuntimeError(f'No passphrase file: pass --passphrase-file or set ${PASSPHRASE_ENV}')
    if not path.is_file() or not path.read_text().strip():
        raise RuntimeError(f'Passphrase file is missing or empty: {path}')
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f'Passphrase file {path} is readable by others: chmod 600 {path}')


def _gpg_base(passphrase_file: Path) -> List[str]:
    return ['gpg', '--batch', '--yes', '--quiet', '--pinentry-mode', 'loopback',
            '--passphrase-file', str(passphrase_file), '--no-symkey-cache']


def encrypt_file(src: Path, passphrase_file: Path) -> Path:
    """Encrypt src to src+'.gpg', delete src, return the new path."""
    check_passphrase_file(passphrase_file)
    dst = Path(str(src) + ENCRYPTED_SUFFIX)
    cmd = _gpg_base(passphrase_file) + ['--symmetric', '--cipher-algo', 'AES256',
                                        '--compress-algo', 'none', '-o', str(dst), str(src)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f'Encryption failed: {result.stderr.strip()}')
    src.unlink()
    return dst


def decrypt_command(archive: Path, passphrase_file: Path) -> List[str]:
    """gpg command that writes the decrypted archive to stdout."""
    return _gpg_base(passphrase_file) + ['--decrypt', str(archive)]


def inner_name(archive_name: str) -> str:
    """'host-1.tar.zst.gpg' -> 'host-1.tar.zst'."""
    return archive_name[:-len(ENCRYPTED_SUFFIX)] if archive_name.endswith(ENCRYPTED_SUFFIX) else archive_name
