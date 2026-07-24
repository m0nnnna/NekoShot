"""
The interactive menu.

The menu never does the work itself: it builds a NekoShot command and hands it
back to be run now or installed into cron. One code path does the work whether
you came from the menu or typed the command, and the menu can show you the
command it built - so it teaches the CLI rather than hiding it.
"""

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import NekoShotConfig
from utils import schedule

ACTION_RUN = 'run'
ACTION_SCHEDULE = 'schedule'
ACTION_QUIT = 'quit'

NEKO_MENU_BANNER = r"""
    /\_/\
   ( o.o )  NekoShot
    > ^ <   What shall we do, nya?
"""


def _prompt(question: str, default: Optional[str] = None) -> str:
    suffix = f' [{default}]' if default else ''
    try:
        answer = input(f'{question}{suffix}: ').strip()
    except EOFError:
        raise KeyboardInterrupt
    return answer or (default or '')


def _confirm(question: str, default: bool = False) -> bool:
    hint = 'Y/n' if default else 'y/N'
    answer = _prompt(f'{question} [{hint}]').strip().lower()
    if not answer:
        return default
    return answer.startswith('y')


def _choose(title: str, options: List[Tuple[str, str]], allow_back: bool = True) -> Optional[str]:
    """
    Show a numbered list and return the chosen key.

    Returns None when the user backs out.
    """
    print()
    print(f'  {title}')
    print()
    for index, (_key, label) in enumerate(options, start=1):
        print(f'    {index}) {label}')
    print(f'    0) {"Back" if allow_back else "Quit"}')
    print()

    while True:
        answer = _prompt('  Choice')
        if answer in ('0', 'q', 'quit', 'exit'):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print('  Nyaa~? Pick one of the numbers above.')


def _pick_location(config: NekoShotConfig, kind: str) -> str:
    """Ask where this kind of backup should be stashed."""
    default = config.location_for(kind)
    location = _prompt(f'  Where should it go? (each kind keeps its own folder)', default)
    return location


def _backup_kind_menu(config: NekoShotConfig) -> Optional[List[str]]:
    """Build the argv for a backup, whichever kind is chosen."""
    kind = _choose('What would you like to back up?', [
        ('system', 'The whole system (files, Docker, metadata)'),
        ('docker', 'Docker only (images, volumes, compose files)'),
        ('sql', 'Databases only (Postgres, MySQL, SQL Server, SQLite)'),
    ])
    if kind is None:
        return None

    if kind == 'sql':
        argv = ['sql']
        location = _pick_location(config, 'sql')
        argv.extend(['--output', location])

        sqlite = _prompt('  Any SQLite files to include? (comma separated, blank for none)')
        for path in [p.strip() for p in sqlite.split(',') if p.strip()]:
            argv.extend(['--sqlite', path])
        return argv

    argv = ['capture']
    location = _pick_location(config, kind)
    argv.extend(['--output', location])

    if kind == 'docker':
        argv.append('--docker-only')
        if _confirm('  Pause containers while copying volumes? (more consistent, brief downtime)'):
            argv.append('--pause-docker')

        prune = _choose('Prune unused images first? (smaller backup, but deletes them from Docker)', [
            ('none', 'No - keep every image'),
            ('dangling', 'Remove dangling images (untagged rebuild leftovers) - safe'),
            ('all', 'Remove ALL images not used by a container - DELETES locally-built images too (cannot be re-pulled)'),
        ])
        if prune == 'all':
            print()
            print('  ! "all" deletes every image no container uses, including ones you built')
            print('    locally and cannot pull again. Make sure you can rebuild them.')
            if not _confirm('  Really prune all unused images?', default=False):
                prune = 'none'
        if prune in ('dangling', 'all'):
            argv.extend(['--prune-images', prune])

    return argv


def _run_now_menu(config: NekoShotConfig) -> Optional[Tuple[str, List[str]]]:
    argv = _backup_kind_menu(config)
    if argv is None:
        return None

    if _confirm('  Run it in the background so you can close this terminal?', default=False):
        argv.append('--headless')

    return ACTION_RUN, argv


def _schedule_menu(config: NekoShotConfig) -> Optional[Tuple[str, List[str]]]:
    argv = _backup_kind_menu(config)
    if argv is None:
        return None

    when = _choose('How often?', [
        ('daily', 'Every day at 02:00'),
        ('nightly-except-sunday', 'Monday to Saturday at 02:00'),
        ('weekly', 'Every Sunday at 03:00'),
        ('monthly', 'On the 1st of each month at 04:00'),
        ('hourly', 'Every hour'),
        ('custom', 'Custom (enter a cron expression)'),
    ])
    if when is None:
        return None

    if when == 'custom':
        expression = _prompt('  Cron expression (min hour day month weekday)', '0 2 * * *')
    else:
        expression = schedule.SCHEDULE_PRESETS[when][0]

    kind = 'sql' if argv[0] == 'sql' else ('docker' if '--docker-only' in argv else 'system')
    name = _prompt('  Name for this job', f'{kind}-{when}')

    return ACTION_SCHEDULE, [name, expression] + argv


def _scheduled_jobs_menu() -> None:
    jobs = schedule.list_jobs()

    print()
    if not jobs:
        print('  No scheduled backups yet ~nya')
        print()
        return

    print(f'  Scheduled backups (from {schedule.cron_file()}):')
    print()
    for job in jobs:
        print(f'    {job["name"]}')
        print(f'      when: {schedule.describe(job["schedule"])}   ({job["schedule"]})')
        print(f'      runs: {job["command"]}')
    print()

    if not schedule.cron_running():
        print('  Careful: no cron daemon seems to be running, so these will not fire.')
        print('    Alpine:  rc-service crond start && rc-update add crond')
        print('    Ubuntu:  systemctl enable --now cron')
        print()

    if jobs and _confirm('  Remove one?'):
        name = _prompt('  Which job (by name)?')
        if name and schedule.remove_job(name):
            print(f'  Removed "{name}" ~nya')
        else:
            print(f'  No job named "{name}"')


def run_menu(config: NekoShotConfig) -> Optional[Tuple[str, List[str]]]:
    """
    Drive the menu until the user picks something to do.

    Returns (action, argv) or None to quit.
    """
    print(NEKO_MENU_BANNER)

    while True:
        choice = _choose('What would you like to do?', [
            ('backup', 'Back up now'),
            ('schedule', 'Set up a scheduled backup'),
            ('jobs', 'Show or remove scheduled backups'),
            ('status', 'Check on a running backup'),
            ('list', 'List snapshots'),
            ('restore', 'Restore a snapshot'),
        ], allow_back=False)

        if choice is None:
            return None

        if choice == 'backup':
            result = _run_now_menu(config)
            if result:
                return result

        elif choice == 'schedule':
            result = _schedule_menu(config)
            if result:
                return result

        elif choice == 'jobs':
            _scheduled_jobs_menu()

        elif choice == 'status':
            return ACTION_RUN, ['status']

        elif choice == 'list':
            kind = _choose('Which snapshots?', [
                ('system', 'System snapshots'),
                ('docker', 'Docker-only snapshots'),
                ('sql', 'Database snapshots'),
                ('base', 'Everything (the whole stash)'),
            ])
            if kind is None:
                continue
            path = config.capture.output_dir if kind == 'base' else config.location_for(kind)
            return ACTION_RUN, ['list', '--path', path]

        elif choice == 'restore':
            image = _prompt('  Path to the snapshot to restore')
            if not image:
                continue
            return ACTION_RUN, ['restore', '--image', image]


def format_command(argv: List[str]) -> str:
    """Render argv as the command a person would have typed."""
    parts = []
    for item in argv:
        parts.append(f'"{item}"' if ' ' in item else item)
    return 'nekoshot ' + ' '.join(parts)
