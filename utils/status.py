"""
Publishing and reading the progress of a running backup.

A capture publishes its phase, percentage and ETA to a small JSON file under a
runtime directory; `nekoshot status` reads it from any other terminal. Every run
publishes, so status works whether the backup was started headless, from cron, or
in the foreground.
"""

import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging import get_logger

logger = get_logger('status')

RUNTIME_DIR_ENV = 'NEKOSHOT_RUN_DIR'

# /run is the conventional home for runtime state and is cleared on reboot, which
# is exactly right for "is a backup happening". The rest are fallbacks for systems
# where it isn't available or writable.
RUNTIME_DIR_CANDIDATES = ['/run/nekoshot', '/var/run/nekoshot', '/tmp/nekoshot']

# Records of finished runs stay readable for a while so `status` can report the
# last result, then get cleaned up by the next run
KEEP_FINISHED_HOURS = 24

PUBLISH_INTERVAL = 0.5

STATE_RUNNING = 'running'
STATE_COMPLETED = 'completed'
STATE_FAILED = 'failed'
STATE_STALE = 'stale'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def status_dir_for_write() -> Path:
    """First runtime directory this process can actually write to."""
    override = os.environ.get(RUNTIME_DIR_ENV)
    candidates = [override] if override else RUNTIME_DIR_CANDIDATES

    for candidate in candidates:
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f'.write-test-{os.getpid()}'
            probe.touch()
            probe.unlink()
            return path
        except OSError:
            continue

    raise RuntimeError(f'No writable runtime directory (tried: {", ".join(str(c) for c in candidates)})')


def status_dirs_for_read() -> List[Path]:
    """
    Every runtime directory that exists, most preferred first.

    Reading must not create anything: `status` runs unprivileged, and creating an
    empty /tmp/nekoshot would make it report "no backups" while root's real
    records sit in /run/nekoshot.
    """
    override = os.environ.get(RUNTIME_DIR_ENV)
    candidates = [override] if override else RUNTIME_DIR_CANDIDATES
    return [Path(c) for c in candidates if Path(c).is_dir()]


def process_alive(pid: int) -> bool:
    """Whether a process is still around."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def _resolve_state(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mark records whose process has vanished.

    A backup killed mid-run leaves its last "running" record behind; without this
    it would claim to still be running forever.
    """
    if record.get('state') == STATE_RUNNING and not process_alive(record.get('pid', -1)):
        record['state'] = STATE_STALE
    return record


def read_all() -> List[Dict[str, Any]]:
    """
    Every known run, newest first, with dead processes marked stale.
    """
    records = []
    seen = set()

    for directory in status_dirs_for_read():
        for path in directory.glob('*.json'):
            try:
                with open(path, 'r') as f:
                    record = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue  # a half-written or unreadable file is not worth failing over

            key = (record.get('pid'), record.get('started_at'))
            if key in seen:
                continue
            seen.add(key)

            record['_path'] = str(path)
            records.append(_resolve_state(record))

    records.sort(key=lambda r: r.get('started_at') or '', reverse=True)
    return records


def prune_finished(directory: Path, max_age_hours: int = KEEP_FINISHED_HOURS) -> None:
    """Drop records of runs that ended a while ago."""
    cutoff = time.time() - max_age_hours * 3600

    for path in directory.glob('*.json'):
        try:
            with open(path, 'r') as f:
                record = json.load(f)
            if record.get('state') == STATE_RUNNING and process_alive(record.get('pid', -1)):
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except (json.JSONDecodeError, OSError):
            # Unparseable leftovers are junk; remove them if they're old enough
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


class StatusPublisher:
    """Writes this run's progress where `nekoshot status` can find it."""

    def __init__(
        self,
        operation: str,
        mode: Optional[str] = None,
        output: Optional[str] = None,
        log_file: Optional[str] = None,
    ):
        self.pid = os.getpid()
        self.enabled = True

        try:
            self.dir = status_dir_for_write()
        except RuntimeError as e:
            # Losing status reporting must never cost you the backup itself
            logger.warning(f'Progress status will not be published: {e}')
            self.enabled = False
            return

        self.path = self.dir / f'{self.pid}.json'
        self._last_write = 0.0

        prune_finished(self.dir)

        self._finished = False

        self.record: Dict[str, Any] = {
            'pid': self.pid,
            'state': STATE_RUNNING,
            'operation': operation,
            'mode': mode,
            'output': output,
            'log_file': log_file,
            'hostname': socket.gethostname(),
            'started_at': _utc_now(),
            'updated_at': _utc_now(),
            'elapsed_seconds': 0,
            'overall_percent': 0,
            'phase': None,
        }
        self._write()

    def _write(self) -> None:
        if not self.enabled:
            return

        self.record['updated_at'] = _utc_now()
        payload = {k: v for k, v in self.record.items() if not k.startswith('_')}

        # Write-and-rename so a reader never catches a half-written file
        tmp = self.path.with_suffix('.json.tmp')
        try:
            with open(tmp, 'w') as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)
            self._last_write = time.monotonic()
        except OSError as e:
            logger.debug(f'Could not publish status: {e}')

    def publish(self, fields: Dict[str, Any], force: bool = False) -> None:
        """Update the record, rate-limited unless forced."""
        # Ignore late updates from a heartbeat thread; the final record wins
        if not self.enabled or self._finished:
            return

        self.record.update(fields)

        if not force and time.monotonic() - self._last_write < PUBLISH_INTERVAL:
            return

        self._write()

    def finish(self, state: str, **fields: Any) -> None:
        """Record the final outcome."""
        if not self.enabled:
            return

        self.record['state'] = state
        self.record['phase'] = None
        self.record.update(fields)
        self._write()
        self._finished = True

    @contextmanager
    def track(self):
        """
        Record a failure if the wrapped work raises.

        Without this, a run that dies leaves its last 'running' record behind and
        `nekoshot status` would report a backup that no longer exists. Success is
        recorded by the caller, which knows what it produced.
        """
        try:
            yield self
        except BaseException as e:
            self.finish(STATE_FAILED, error=f'{type(e).__name__}: {e}')
            raise
