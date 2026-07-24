import logging
import sys
from pathlib import Path
from typing import Optional


_loggers = {}


def setup_logging(
    level: str = 'INFO',
    log_file: Optional[Path] = None,
    json_output: bool = False
) -> logging.Logger:
    """
    Configure logging for nekoshot.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for log output
        json_output: If True, output logs in JSON format

    Returns:
        Configured root logger
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger('nekoshot')
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    if json_output:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.

    Args:
        name: Logger name (will be prefixed with 'nekoshot.')

    Returns:
        Logger instance
    """
    full_name = f'nekoshot.{name}' if not name.startswith('nekoshot') else name

    if full_name not in _loggers:
        _loggers[full_name] = logging.getLogger(full_name)

    return _loggers[full_name]


class JsonFormatter(logging.Formatter):
    """Format log records as JSON for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime

        log_data = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }

        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)

        if hasattr(record, 'extra_data'):
            log_data['data'] = record.extra_data

        return json.dumps(log_data)


class ProgressLogger:
    """Helper for logging progress of long operations."""

    def __init__(self, logger: logging.Logger, total: int, description: str = ''):
        self.logger = logger
        self.total = total
        self.current = 0
        self.description = description
        self._last_percent = -1

    def update(self, amount: int = 1):
        """Update progress by the given amount."""
        self.current += amount
        percent = int((self.current / self.total) * 100) if self.total > 0 else 100

        if percent != self._last_percent and percent % 10 == 0:
            self.logger.info(f'{self.description}: {percent}% ({self.current}/{self.total})')
            self._last_percent = percent

    def finish(self):
        """Mark progress as complete."""
        self.current = self.total
        self.logger.info(f'{self.description}: Complete ({self.total}/{self.total})')
