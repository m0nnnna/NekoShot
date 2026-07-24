"""
Installing NekoShot backups into cron.

Alpine and Ubuntu disagree about where cron entries live and whether a user
column exists, so both layouts are handled here rather than at the call sites.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .logging import get_logger

logger = get_logger('schedule')

# Ubuntu/Debian: drop-in files, each line carrying the user to run as
CRON_D_PATH = Path('/etc/cron.d/nekoshot')

# Alpine (busybox crond): a per-user crontab, no user column
BUSYBOX_CRONTAB = Path('/etc/crontabs/root')

MARKER = '# nekoshot:'

SCHEDULE_PRESETS = {
    'hourly': ('0 * * * *', 'every hour'),
    'daily': ('0 2 * * *', 'every day at 02:00'),
    'nightly-except-sunday': ('0 2 * * 1-6', 'Monday to Saturday at 02:00'),
    'weekly': ('0 3 * * 0', 'every Sunday at 03:00'),
    'monthly': ('0 4 1 * *', 'on the 1st of each month at 04:00'),
}


def uses_cron_d() -> bool:
    """
    Whether this host takes /etc/cron.d drop-ins.

    busybox crond (Alpine) ignores /etc/cron.d, so entries written there would
    look installed and never run.
    """
    return Path('/etc/cron.d').is_dir() and not _is_busybox_cron()


def _is_busybox_cron() -> bool:
    crond = shutil.which('crond')
    if not crond:
        return False
    try:
        # busybox's crond is a symlink into busybox itself
        return 'busybox' in Path(crond).resolve().name.lower()
    except OSError:
        return False


def cron_file() -> Path:
    return CRON_D_PATH if uses_cron_d() else BUSYBOX_CRONTAB


def _format_entry(name: str, schedule: str, command: str) -> List[str]:
    """One labelled cron entry, in this host's dialect."""
    if uses_cron_d():
        return [f'{MARKER}{name}', f'{schedule} root {command}']
    return [f'{MARKER}{name}', f'{schedule} {command}']


def _read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def list_jobs() -> List[Dict[str, str]]:
    """Every NekoShot job currently installed."""
    lines = _read_lines(cron_file())
    jobs = []

    for index, line in enumerate(lines):
        if not line.startswith(MARKER):
            continue

        name = line[len(MARKER):].strip()
        if index + 1 >= len(lines):
            continue

        entry = lines[index + 1].strip()
        if not entry or entry.startswith('#'):
            continue

        # "m h dom mon dow [user] command"
        fields = entry.split(None, 5)
        if len(fields) < 6:
            continue

        schedule = ' '.join(fields[:5])
        remainder = fields[5]

        if uses_cron_d():
            # /etc/cron.d entries carry a user column before the command
            parts = remainder.split(None, 1)
            command = parts[1] if len(parts) > 1 else ''
        else:
            command = remainder

        jobs.append({'name': name, 'schedule': schedule, 'command': command})

    return jobs


def add_job(name: str, schedule: str, command: str) -> Path:
    """
    Install (or replace) a named job.

    Replacing by name keeps re-running the menu from stacking duplicate entries.
    """
    path = cron_file()
    remove_job(name, quiet=True)

    lines = _read_lines(path)

    if not lines and uses_cron_d():
        lines = [
            '# NekoShot scheduled backups ~nya',
            '# Managed by `nekoshot schedule` - hand edits are kept, but entries',
            '# with a "# nekoshot:<name>" marker are rewritten by name.',
            'SHELL=/bin/sh',
            'PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin',
            '',
        ]

    lines.extend(_format_entry(name, schedule, command))

    path.parent.mkdir(parents=True, exist_ok=True)
    # cron ignores a crontab whose last line has no newline
    path.write_text('\n'.join(lines) + '\n')
    path.chmod(0o644 if uses_cron_d() else 0o600)

    _reload_cron()
    logger.info(f'Scheduled "{name}" in {path}')
    return path


def remove_job(name: str, quiet: bool = False) -> bool:
    """Remove a job by name. Returns whether anything was removed."""
    path = cron_file()
    lines = _read_lines(path)
    if not lines:
        return False

    kept = []
    removed = False
    skip_next = False

    for line in lines:
        if skip_next:
            skip_next = False
            removed = True
            continue

        if line.startswith(MARKER) and line[len(MARKER):].strip() == name:
            skip_next = True  # drop the marker and the entry under it
            continue

        kept.append(line)

    if removed:
        path.write_text('\n'.join(kept).rstrip('\n') + '\n')
        _reload_cron()
        if not quiet:
            logger.info(f'Removed scheduled job "{name}"')

    return removed


def _reload_cron() -> None:
    """
    Nudge cron to re-read its files.

    Vixie cron picks up /etc/cron.d by itself; busybox crond needs a reload, and
    a failure here is not worth failing the whole operation over.
    """
    if uses_cron_d():
        return

    for cmd in (['rc-service', 'crond', 'reload'], ['killall', '-HUP', 'crond']):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue


def cron_running() -> bool:
    """Whether a cron daemon is actually running to execute these jobs."""
    for name in ('crond', 'cron'):
        try:
            if subprocess.run(['pgrep', '-x', name], capture_output=True, timeout=5).returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


def describe(schedule: str) -> str:
    """Turn a cron expression back into something readable."""
    for _name, (expression, description) in SCHEDULE_PRESETS.items():
        if expression == schedule:
            return description
    return schedule


def nekoshot_command() -> str:
    """
    How cron should invoke NekoShot.

    Prefers the installed entry point; falls back to this checkout so a job
    scheduled from a git clone still runs.
    """
    installed = shutil.which('nekoshot')
    if installed:
        return installed

    script = Path(__file__).resolve().parent.parent / 'nekoshot.py'
    return f'{shutil.which("python3") or "python3"} {script}'
