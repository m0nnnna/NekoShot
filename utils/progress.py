"""
Progress bars and ETAs for the long phases of a capture or restore.

Rendering only happens on a TTY. Under --json, in cron, or with --no-progress the
same updates degrade to periodic log lines, so redirected output stays readable.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .logging import get_logger

logger = get_logger('progress')

# GNU tar counts checkpoints in records, and a record is the 20-block default
# blocking factor: 20 * 512 bytes. Verified against tar's checkpoint output.
TAR_RECORD_BYTES = 20 * 512

_enabled: Optional[bool] = None


def configure(enabled: Optional[bool] = None) -> None:
    """
    Set whether progress bars render.

    Args:
        enabled: True/False to force, None to auto-detect from the terminal
    """
    global _enabled
    _enabled = enabled


def is_enabled() -> bool:
    """Whether bars should render right now."""
    if _enabled is not None:
        return _enabled
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def format_bytes(n: float) -> str:
    """Render a byte count in human units."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def format_duration(seconds: float) -> str:
    """Render a duration as MM:SS, or HH:MM:SS past an hour."""
    if seconds is None or seconds != seconds or seconds < 0:
        return '--:--'
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours:d}:{minutes:02d}:{secs:02d}'
    return f'{minutes:02d}:{secs:02d}'


class ProgressBar:
    """
    A single-line progress bar with a smoothed rate and ETA.

    Totals may be unknown (or arrive late via set_total), in which case the bar
    reports what has been done so far without inventing a percentage.
    """

    RATE_WINDOW = 30.0
    MIN_REDRAW = 0.1

    def __init__(
        self,
        description: str,
        total: Optional[int] = None,
        phase: Optional[int] = None,
        phases: Optional[int] = None,
        unit: str = 'bytes',
        log_interval: float = 15.0,
        tracker: Optional['PhaseTracker'] = None,
    ):
        self.description = description
        self._phase_name = description
        self.total = total if total and total > 0 else None
        self.phase = phase
        self.phases = phases
        self.unit = unit
        self.log_interval = log_interval
        self.tracker = tracker

        self.current = 0
        self.start = time.monotonic()
        self._samples = deque()
        self._last_draw = 0.0
        self._last_log = self.start
        self._done = False
        self._live = is_enabled()

        self._sample(0)
        if not self._live:
            logger.info(f'{self._label()}: starting{self._of_total()}')

    def _label(self, description: Optional[str] = None) -> str:
        description = self.description if description is None else description
        if self.phase and self.phases:
            return f'[{self.phase}/{self.phases}] {description}'
        return description

    def _of_total(self) -> str:
        if not self.total:
            return ''
        return f' ({self._amount(self.total)})'

    def _amount(self, n: float) -> str:
        return format_bytes(n) if self.unit == 'bytes' else f'{int(n):,}'

    def _sample(self, value: int) -> None:
        now = time.monotonic()
        self._samples.append((now, value))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.RATE_WINDOW:
            self._samples.popleft()

    def _rate(self) -> Optional[float]:
        """Bytes (or items) per second over the recent window."""
        if len(self._samples) < 2:
            return None
        (t0, v0), (t1, v1) = self._samples[0], self._samples[-1]
        elapsed = t1 - t0
        if elapsed <= 0 or v1 <= v0:
            return None
        return (v1 - v0) / elapsed

    def _eta(self) -> Optional[float]:
        rate = self._rate()
        if not rate or not self.total:
            return None
        remaining = self.total - self.current
        return remaining / rate if remaining > 0 else 0

    def rate(self) -> Optional[float]:
        """Current throughput per second, or None if not yet measurable."""
        return self._rate()

    def eta(self) -> Optional[float]:
        """Seconds remaining, or None when the total or rate is unknown."""
        return self._eta()

    def percent(self) -> Optional[int]:
        """Completion of this phase, or None when the total is unknown."""
        if not self.total:
            return None
        return int(min(1.0, self.current / self.total) * 100)

    def set_total(self, total: Optional[int]) -> None:
        """Set or correct the total once it becomes known."""
        self.total = total if total and total > 0 else None

    def update(self, current: int) -> None:
        """Set absolute progress."""
        if self._done or current < self.current:
            return
        self.current = current
        self._sample(current)
        if self.tracker:
            self.tracker.publish()
        self._emit()

    def advance(self, amount: int = 1) -> None:
        """Add to progress."""
        self.update(self.current + amount)

    def touch(self) -> None:
        """Redraw without changing progress, so an idle phase still looks alive."""
        if not self._done:
            self._emit()

    def describe(self, description: str) -> None:
        """Change the label mid-phase (e.g. to name the directory being scanned)."""
        self.description = description
        self._emit()

    def _emit(self) -> None:
        now = time.monotonic()
        if self._live:
            if now - self._last_draw < self.MIN_REDRAW:
                return
            self._last_draw = now
            sys.stdout.write('\r' + self._render())
            sys.stdout.flush()
        elif now - self._last_log >= self.log_interval:
            self._last_log = now
            logger.info(self._plain())

    def _stats(self, show_rate: bool = True, show_amounts: bool = True) -> str:
        parts = []
        if show_amounts:
            if self.total:
                parts.append(f'{self._amount(self.current)} / {self._amount(self.total)}')
            else:
                parts.append(self._amount(self.current))

        rate = self._rate()
        if show_rate and rate:
            parts.append(f'{self._amount(rate)}/s')

        eta = self._eta()
        if eta is not None:
            parts.append(f'ETA {format_duration(eta)}')
        return '   '.join(parts)

    def _plain(self) -> str:
        pct = f'{int(self.current / self.total * 100)}%   ' if self.total else ''
        return f'{self._label()}: {pct}{self._stats()}'

    # Terminal width -> (label column, bar width, show rate, show amounts).
    # Fixed tiers rather than 'whatever space is left': the leftover approach
    # squeezed the bar to nothing on an 80-column terminal once the byte counts
    # grew. Pinning the label to a column also stops the bar sliding around as
    # the numbers change width.
    # Each tier is sized so the widest realistic stats still fit without the
    # ETA being clipped off the end.
    _LAYOUTS = [
        (110, 24, 24, True, True),
        (90, 22, 18, False, True),
        (80, 18, 12, False, True),
        (70, 28, 12, False, False),
        (0, 16, 0, False, False),
    ]

    def _layout(self, width: int):
        for min_width, label_col, bar_width, show_rate, show_amounts in self._LAYOUTS:
            if width >= min_width:
                return label_col, bar_width, show_rate, show_amounts
        return 16, 0, False, False

    def _render(self) -> str:
        width = max(24, shutil.get_terminal_size((80, 20)).columns)
        label_col, bar_width, show_rate, show_amounts = self._layout(width)
        stats = self._stats(show_rate=show_rate, show_amounts=show_amounts)

        if self.total:
            fraction = min(1.0, self.current / self.total)
            pct = f'{int(fraction * 100):>3}%'
        else:
            fraction = None
            pct = '    '

        if bar_width and fraction is not None:
            filled = int(bar_width * fraction)
            bar = '[' + '#' * filled + '-' * (bar_width - filled) + ']  '
        elif bar_width:
            # Unknown total: sweep a marker so the line still looks alive
            pos = int(time.monotonic() * 8) % bar_width
            bar = '[' + '-' * pos + '#' + '-' * (bar_width - pos - 1) + ']  '
        else:
            bar = ''

        label = self._label()
        if len(label) > label_col:
            label = label[:label_col - 1] + '~'

        line = f'{label:<{label_col}} {bar}{pct}   {stats}'.rstrip()
        return line[:width - 1].ljust(width - 1)

    def finish(self, summary: Optional[str] = None) -> float:
        """Complete the bar and print a one-line result. Returns elapsed seconds."""
        if self._done:
            return time.monotonic() - self.start

        self._done = True
        elapsed = time.monotonic() - self.start

        if self.total and self.current < self.total:
            self.current = self.total

        # Retire the phase before reading the overall figure, or it reports the
        # phase as still in flight and the percentage lands one phase behind
        if self.tracker:
            self.tracker.complete_phase()

        done = summary if summary is not None else self._amount(self.current)
        # Report the phase's own name, not whatever describe() last set it to
        tail = f'{self._label(self._phase_name)}: done in {format_duration(elapsed)} ({done})'
        if self.tracker:
            tail += f'   |   overall {self.tracker.overall_percent():>3}%, elapsed {format_duration(self.tracker.elapsed())}'

        if self._live:
            width = max(24, shutil.get_terminal_size((80, 20)).columns)
            sys.stdout.write('\r' + tail[:width - 1].ljust(width - 1) + '\n')
            sys.stdout.flush()
        else:
            logger.info(tail)

        return elapsed

    def abandon(self) -> None:
        """End a bar whose phase failed, without claiming it completed."""
        if self._done:
            return
        self._done = True
        if self._live:
            sys.stdout.write('\n')
            sys.stdout.flush()
        if self.tracker:
            self.tracker.complete_phase()

    def __enter__(self) -> 'ProgressBar':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.abandon()


class PhaseTracker:
    """
    Tracks progress across the numbered phases of a capture or restore.

    Phase totals are only known as each phase starts (finding them upfront would
    mean scanning the filesystem first), so 'overall' is measured in phases
    completed plus the fraction of the phase in flight - not in bytes.
    """

    HEARTBEAT_INTERVAL = 2.0

    def __init__(self, phases: int, publisher=None):
        self.phases = phases
        self.index = 0
        self.start = time.monotonic()
        self.publisher = publisher
        self._current: Optional[ProgressBar] = None

        if publisher:
            self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        """
        Keep the published record ticking even when a phase reports nothing.

        Progress updates alone aren't enough: a phase can be genuinely busy and
        silent (docker save before it creates its file, a long directory walk),
        which would freeze the elapsed time and make `nekoshot status` look like
        the backup had died.
        """
        def beat():
            while True:
                time.sleep(self.HEARTBEAT_INTERVAL)
                try:
                    self.publish(force=True)
                except Exception:
                    return  # never let status reporting take down a backup

        threading.Thread(target=beat, daemon=True).start()

    def phase(self, description: str, total: Optional[int] = None, unit: str = 'bytes') -> ProgressBar:
        """Begin the next phase and return its bar."""
        self.index += 1
        self._current = ProgressBar(
            description,
            total=total,
            phase=self.index,
            phases=self.phases,
            unit=unit,
            tracker=self,
        )
        self.publish(force=True)
        return self._current

    def complete_phase(self) -> None:
        self._current = None

    def publish(self, force: bool = False) -> None:
        """
        Hand the current progress to the status publisher, if there is one.

        Driven from update() rather than the render path: without a terminal the
        bar only redraws every log_interval, which would leave `nekoshot status`
        reading numbers up to 15s stale during a headless run.
        """
        if not self.publisher:
            return

        bar = self._current
        phase = None
        if bar:
            phase = {
                'index': bar.phase,
                'count': self.phases,
                'description': bar._phase_name,
                'detail': bar.description if bar.description != bar._phase_name else None,
                'unit': bar.unit,
                'percent': bar.percent(),
                'current': bar.current,
                'total': bar.total,
                'rate': round(bar.rate(), 1) if bar.rate() else None,
                'eta_seconds': round(bar.eta()) if bar.eta() is not None else None,
            }

        self.publisher.publish({
            'overall_percent': self.overall_percent(),
            'elapsed_seconds': round(self.elapsed(), 1),
            'phase': phase,
        }, force=force)

    def set_phases(self, phases: int) -> None:
        """Correct the phase count when it turns out work will be skipped."""
        self.phases = max(self.index, phases)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def overall_percent(self) -> int:
        done = self.index - 1 if self._current else self.index
        fraction = 0.0
        if self._current and self._current.total:
            fraction = min(1.0, self._current.current / self._current.total)
        return int(min(1.0, (done + fraction) / self.phases) * 100)


@contextmanager
def poll_file_size(bar: ProgressBar, path: Path, base: int = 0, interval: float = 0.25):
    """
    Drive a bar from a file that a subprocess is writing.

    Used where the tool underneath reports nothing (docker save, volume tars).
    `base` carries progress already banked by earlier files in the same phase.
    """
    path = Path(path)
    stop = threading.Event()

    def watch():
        while not stop.wait(interval):
            try:
                bar.update(base + path.stat().st_size)
            except OSError:
                pass

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield bar
    finally:
        stop.set()
        thread.join(timeout=1.0)
        try:
            bar.update(base + path.stat().st_size)
        except OSError:
            pass


@contextmanager
def pulse(bar: ProgressBar, interval: float = 0.2):
    """
    Keep a bar animating during work that reports nothing at all.

    Without this a phase that makes no callbacks (a long directory walk) leaves
    a frozen line on screen, which is exactly what makes a slow phase look hung.
    """
    stop = threading.Event()

    def tick():
        while not stop.wait(interval):
            bar.touch()

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        yield bar
    finally:
        stop.set()
        thread.join(timeout=1.0)


def _read_offset(pid: int, target: Path) -> Optional[int]:
    """Read how far a process has read into a given file, via /proc."""
    fd_dir = Path(f'/proc/{pid}/fd')
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return None

    for entry in entries:
        try:
            if Path(os.readlink(entry)) != target:
                continue
            info = Path(f'/proc/{pid}/fdinfo/{entry.name}').read_text()
        except OSError:
            continue

        match = re.search(r'^pos:\s*(\d+)', info, re.MULTILINE)
        if match:
            return int(match.group(1))
    return None


@contextmanager
def poll_read_offset(bar: ProgressBar, pid: int, path: Path, interval: float = 0.25):
    """
    Drive a bar from how far a process has read into a file.

    Lets us show real progress for decompression, where the only thing we can
    observe is the archive being consumed. Linux-only; degrades to an
    indeterminate bar wherever /proc isn't available.
    """
    path = Path(path).resolve()
    stop = threading.Event()

    def watch():
        while not stop.wait(interval):
            offset = _read_offset(pid, path)
            if offset is not None:
                bar.update(offset)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield bar
    finally:
        stop.set()
        thread.join(timeout=1.0)


# rsync --info=progress2 lines look like:
#      1,234,567,890  46%  118.34MB/s    0:02:04 (xfr#12, to-chk=903/4021)
_RSYNC_PROGRESS = re.compile(r'^\s*([\d,]+)\s+(\d+)%')


def parse_rsync_progress(line: str):
    """
    Parse an rsync --info=progress2 line into (bytes_done, percent).

    Returns None for any other line.
    """
    match = _RSYNC_PROGRESS.match(line)
    if not match:
        return None
    return int(match.group(1).replace(',', '')), int(match.group(2))


def run_rsync_with_progress(cmd, bar: Optional[ProgressBar], log) -> int:
    """
    Run an rsync command, driving `bar` from its progress output.

    rsync's percentage is relative to what it has discovered so far, so the
    derived total moves around early on and settles as the scan completes. The
    bar's own smoothed rate keeps the ETA from lurching with it.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    for line in process.stdout:
        line = line.strip()
        if not line:
            continue

        parsed = parse_rsync_progress(line)
        if parsed is None:
            log.debug(line)
            continue

        done, percent = parsed
        if bar is None:
            continue
        # Below a few percent the implied total is mostly noise
        if percent >= 2:
            bar.set_total(int(done * 100 / percent))
        bar.update(done)

    process.wait()
    return process.returncode


def watch_tar_checkpoints(
    stream,
    bar: Optional[ProgressBar],
    record_bytes: int = TAR_RECORD_BYTES
) -> threading.Thread:
    """
    Drive a bar from GNU tar --checkpoint output on the given stream.

    tar counts records it has read, which is the one cheap way to see how much
    input a tar|compressor pipeline has actually consumed.

    This always drains the stream, bar or no bar: tar emits a checkpoint line per
    megabyte, so an unread stderr pipe would fill and stall tar. Anything that
    isn't a checkpoint is a real tar message and gets logged.
    """
    pattern = re.compile(r'checkpoint\s+(\d+)', re.IGNORECASE)

    def watch():
        for line in stream:
            line = line.strip()
            if not line:
                continue
            match = pattern.search(line)
            if match:
                if bar:
                    bar.update(int(match.group(1)) * record_bytes)
            else:
                logger.debug(line)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def directory_size(path: Path) -> Optional[int]:
    """Total size of a directory tree, or None if it can't be measured."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    except OSError:
        return None
    return total or None
