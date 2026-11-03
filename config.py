import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

DEFAULT_EXCLUDES = [
    '/proc/*',
    '/sys/*',
    '/dev/*',
    '/run/*',
    '/tmp/*',
    '/mnt/*',
    '/media/*',
    '/var/cache/*',
    '/var/tmp/*',
    '/var/run/*',
    '/var/lock/*',
    '/lost+found',
    '*.swp',
    '*.swap',
    '.cache/*',
    '/root/.cache/*',
    '/home/*/.cache/*',
]

# Added on top of DEFAULT_EXCLUDES by the 'desktop' profile: a whole-system backup of a
# desktop PC. Games and package caches are big and can be downloaded again; snap and
# flatpak runtime mounts are loop mounts that must not be walked now that the
# profile crosses filesystem boundaries to pick up /boot/efi and a separate /home.
DESKTOP_EXCLUDES = [
    '/snap/*',
    '/var/lib/snapd/snaps/*',
    '/var/lib/flatpak/repo/*',
    '/var/lib/docker/*',
    '/swapfile',
    '/swap.img',
    '/home/*/.local/share/Steam/steamapps/common/*',
    '/home/*/.local/share/Steam/steamapps/shadercache/*',
    '/home/*/.local/share/Steam/steamapps/compatdata/*/pfx/drive_c/windows/*',
    '/home/*/.steam/steam/steamapps/common/*',
    '/home/*/.local/share/Trash/*',
    '/home/*/.var/app/*/cache/*',
    '/home/*/.local/share/flatpak/repo/*',
    '/home/*/.npm/_cacache/*',
    '/home/*/.cargo/registry/*',
]

PROFILES = ('server', 'desktop')

DOCKER_COMPOSE_LOCATIONS = [
    '/opt',
    '/home',
    '/root',
    '/srv',
    '/var/docker',
]

# Container images that mean "there is a database in here". Matched as substrings
# of the image name, so bitnami/postgresql, postgis/postgis and pgvector all land
# on the postgres engine.
SQL_IMAGE_PATTERNS = {
    'postgres': ['postgres', 'postgis', 'timescale', 'pgvector', 'pgautoupgrade'],
    'mysql': ['mysql', 'mariadb', 'percona'],
    'mssql': ['mssql', 'azure-sql-edge', 'sqlserver'],
}

SQL_ENGINES = ['postgres', 'mysql', 'mssql', 'sqlite']


@dataclass
class CaptureConfig:
    """Configuration for system capture."""
    output_dir: str = '/backup/nekoshot'
    compression: str = 'zstd'
    compression_level: int = 3
    exclude_patterns: List[str] = field(default_factory=lambda: DEFAULT_EXCLUDES.copy())
    # 'server' keeps rsync on one filesystem (the historic behaviour). 'desktop' crosses
    # filesystems so /boot/efi and a separate /home are captured, and drops Docker.
    profile: str = 'server'
    # Encrypt the finished archive (gpg, AES256) with the passphrase in this root-only file.
    encrypt: bool = False
    passphrase_file: str = ''
    one_file_system: bool = True
    include_docker: bool = True
    docker_only: bool = False
    pause_docker: bool = False
    # '' = off, 'dangling' = untagged leftovers only, 'all' = every image not used
    # by a container. Deletes from the live Docker before saving, to shrink the backup.
    prune_images: str = ''
    docker_compose_paths: List[str] = field(default_factory=lambda: DOCKER_COMPOSE_LOCATIONS.copy())


@dataclass
class LocationsConfig:
    """
    Where each kind of snapshot is stashed, so they sort themselves.

    Empty means "derive from capture.output_dir", which keeps a configured
    output_dir authoritative instead of being silently overridden by a default.
    """
    system: str = ''
    docker: str = ''
    sql: str = ''


@dataclass
class SqlConfig:
    """Configuration for database dumps."""
    output_dir: str = ''
    include_docker: bool = True
    include_native: bool = True
    sqlite_paths: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    compression: str = 'zstd'
    compression_level: int = 3


@dataclass
class RestoreConfig:
    """Configuration for system restore."""
    target_dir: str = '/mnt/restore'
    fix_bootloader: bool = True
    bootloader_device: Optional[str] = None
    restore_docker: bool = True
    update_fstab: bool = True


@dataclass
class RetentionConfig:
    """Configuration for backup retention."""
    keep_count: int = 5
    keep_days: Optional[int] = None


@dataclass
class HooksConfig:
    """Pre/post operation hooks."""
    pre_capture: Optional[str] = None
    post_capture: Optional[str] = None
    pre_restore: Optional[str] = None
    post_restore: Optional[str] = None


@dataclass
class NekoShotConfig:
    """Main configuration container."""
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    sql: SqlConfig = field(default_factory=SqlConfig)
    locations: LocationsConfig = field(default_factory=LocationsConfig)
    restore: RestoreConfig = field(default_factory=RestoreConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    log_level: str = 'INFO'
    log_file: Optional[str] = None
    json_output: bool = False

    def location_for(self, kind: str) -> str:
        """
        Where snapshots of a given kind ('system', 'docker', 'sql') belong.

        Falls back to a subdirectory of the base output directory so the kinds
        stay sorted without anyone having to configure three paths.
        """
        configured = getattr(self.locations, kind, '')
        if configured:
            return configured

        if kind == 'sql' and self.sql.output_dir:
            return self.sql.output_dir

        # Joined as text: these are always POSIX paths on the target systems
        return f'{self.capture.output_dir.rstrip("/")}/{kind}'


def load_config(config_path: Optional[Path] = None) -> NekoShotConfig:
    """
    Load configuration from file.

    Args:
        config_path: Path to config file. If None, uses defaults and checks
                     standard locations (/etc/nekoshot.conf, ~/.nekoshot.conf)

    Returns:
        Loaded configuration
    """
    config = NekoShotConfig()

    search_paths = [
        Path('/etc/nekoshot.conf'),
        Path('/etc/nekoshot/config.json'),
        Path.home() / '.nekoshot.conf',
        Path.home() / '.config' / 'nekoshot' / 'config.json',
    ]

    if config_path:
        search_paths.insert(0, Path(config_path))

    for path in search_paths:
        if path.exists():
            config = _load_config_file(path, config)
            break

    return config


def _load_config_file(path: Path, base_config: NekoShotConfig) -> NekoShotConfig:
    """Load and merge configuration from a file."""
    with open(path, 'r') as f:
        data = json.load(f)

    if 'capture' in data:
        for key, value in data['capture'].items():
            if hasattr(base_config.capture, key):
                setattr(base_config.capture, key, value)

    if 'sql' in data:
        for key, value in data['sql'].items():
            if hasattr(base_config.sql, key):
                setattr(base_config.sql, key, value)

    if 'locations' in data:
        for key, value in data['locations'].items():
            if hasattr(base_config.locations, key):
                setattr(base_config.locations, key, value)

    if 'restore' in data:
        for key, value in data['restore'].items():
            if hasattr(base_config.restore, key):
                setattr(base_config.restore, key, value)

    if 'retention' in data:
        for key, value in data['retention'].items():
            if hasattr(base_config.retention, key):
                setattr(base_config.retention, key, value)

    if 'hooks' in data:
        for key, value in data['hooks'].items():
            if hasattr(base_config.hooks, key):
                setattr(base_config.hooks, key, value)

    for key in ['log_level', 'log_file', 'json_output']:
        if key in data:
            setattr(base_config, key, data[key])

    return base_config


def save_config(config: NekoShotConfig, path: Path) -> None:
    """Save configuration to file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        'capture': asdict(config.capture),
        'sql': asdict(config.sql),
        'locations': asdict(config.locations),
        'restore': asdict(config.restore),
        'retention': asdict(config.retention),
        'hooks': asdict(config.hooks),
        'log_level': config.log_level,
        'log_file': config.log_file,
        'json_output': config.json_output,
    }

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def apply_profile(capture: CaptureConfig, profile: str) -> None:
    """Apply a named capture profile to the capture config, in place."""
    if profile not in PROFILES:
        raise ValueError(f'unknown profile {profile!r}, expected one of {", ".join(PROFILES)}')
    capture.profile = profile
    if profile == 'desktop':
        capture.one_file_system = False
        capture.include_docker = False
        for pattern in DESKTOP_EXCLUDES:
            if pattern not in capture.exclude_patterns:
                capture.exclude_patterns.append(pattern)


def get_default_excludes() -> List[str]:
    """Get the default exclusion patterns."""
    return DEFAULT_EXCLUDES.copy()


def detect_distro() -> str:
    """
    Detect the Linux distribution.

    Returns:
        'alpine', 'ubuntu', 'debian', or 'unknown'
    """
    os_release = Path('/etc/os-release')
    if os_release.exists():
        content = os_release.read_text()
        if 'alpine' in content.lower():
            return 'alpine'
        elif 'ubuntu' in content.lower():
            return 'ubuntu'
        elif 'debian' in content.lower():
            return 'debian'

    if Path('/etc/alpine-release').exists():
        return 'alpine'

    return 'unknown'
