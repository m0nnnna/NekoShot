import subprocess
import shutil
from pathlib import Path
from typing import Optional, List
from .logging import get_logger
from .progress import (
    PhaseTracker, directory_size, poll_read_offset,
    watch_tar_checkpoints, format_bytes
)

logger = get_logger('compression')

# Emit a tar checkpoint every ~1MB of input (100 records x 10240 bytes)
TAR_CHECKPOINT_RECORDS = 100

# How much hashing to do between progress updates
CHECKSUM_REPORT_BYTES = 4 * 1024 * 1024


def check_zstd_available() -> bool:
    """Check if zstd is available on the system."""
    return shutil.which('zstd') is not None


def check_pigz_available() -> bool:
    """Check if pigz (parallel gzip) is available."""
    return shutil.which('pigz') is not None


def compress_directory(
    source_dir: Path,
    output_file: Path,
    compression: str = 'zstd',
    level: int = 3,
    exclude_patterns: Optional[List[str]] = None,
    tracker: Optional[PhaseTracker] = None
) -> Path:
    """
    Compress a directory into a tar archive with specified compression.

    Args:
        source_dir: Directory to compress
        output_file: Output archive path (extension added automatically)
        compression: Compression type ('zstd', 'gzip', 'none')
        level: Compression level (1-19 for zstd, 1-9 for gzip)
        exclude_patterns: List of patterns to exclude
        tracker: Phase tracker for progress reporting

    Returns:
        Path to the created archive
    """
    source_dir = Path(source_dir)
    output_file = Path(output_file)

    if compression == 'zstd':
        if not check_zstd_available():
            logger.warning('zstd not available, falling back to gzip')
            compression = 'gzip'
        else:
            if not str(output_file).endswith('.tar.zst'):
                output_file = Path(str(output_file) + '.tar.zst')

    if compression == 'gzip':
        if not str(output_file).endswith('.tar.gz'):
            output_file = Path(str(output_file) + '.tar.gz')
    elif compression == 'none':
        if not str(output_file).endswith('.tar'):
            output_file = Path(str(output_file) + '.tar')

    output_file.parent.mkdir(parents=True, exist_ok=True)

    # tar reports the records it has read, which is the only cheap way to see how
    # far a tar|compressor pipeline has got. Total is the staged directory's size.
    tar_cmd = [
        'tar', '-cf', '-',
        f'--checkpoint={TAR_CHECKPOINT_RECORDS}',
        '--checkpoint-action=echo',
    ]

    if exclude_patterns:
        for pattern in exclude_patterns:
            tar_cmd.extend(['--exclude', pattern])

    tar_cmd.extend(['-C', str(source_dir.parent), source_dir.name])

    total = directory_size(source_dir)
    bar = tracker.phase('Squishing into archive', total=total) if tracker else None

    if compression == 'zstd':
        compress_cmd = ['zstd', f'-{level}', '-T0', '-o', str(output_file)]
    elif compression == 'gzip':
        if check_pigz_available():
            compress_cmd = ['pigz', f'-{level}', '-c']
        else:
            compress_cmd = ['gzip', f'-{level}', '-c']
    else:
        compress_cmd = None

    logger.info(f'Compressing {source_dir} to {output_file}')

    try:
        if compress_cmd and compression != 'zstd':
            tar_proc = subprocess.Popen(
                tar_cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True
            )
            watcher = watch_tar_checkpoints(tar_proc.stderr, bar)
            with open(output_file, 'wb') as f:
                compress_proc = subprocess.Popen(
                    compress_cmd, stdin=tar_proc.stdout, stdout=f
                )
            tar_proc.stdout.close()
            compress_proc.wait()
            tar_proc.wait()
            watcher.join(timeout=1.0)

            if tar_proc.returncode != 0 or compress_proc.returncode != 0:
                raise RuntimeError('Compression failed')
        elif compression == 'zstd':
            tar_proc = subprocess.Popen(
                tar_cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True
            )
            watcher = watch_tar_checkpoints(tar_proc.stderr, bar)
            zstd_proc = subprocess.Popen(
                compress_cmd, stdin=tar_proc.stdout
            )
            tar_proc.stdout.close()
            zstd_proc.wait()
            tar_proc.wait()
            watcher.join(timeout=1.0)

            if tar_proc.returncode != 0 or zstd_proc.returncode != 0:
                raise RuntimeError('Compression failed')
        else:
            with open(output_file, 'wb') as f:
                tar_proc = subprocess.Popen(
                    tar_cmd, stdout=f,
                    stderr=subprocess.PIPE, universal_newlines=True
                )
                watcher = watch_tar_checkpoints(tar_proc.stderr, bar)
                tar_proc.wait()
            watcher.join(timeout=1.0)

            if tar_proc.returncode != 0:
                raise RuntimeError('Compression failed')

        if bar:
            bar.finish(summary=format_bytes(output_file.stat().st_size))

        logger.info(f'Squished into: {output_file} ({output_file.stat().st_size / 1024 / 1024:.1f} MB)')
        return output_file

    except (subprocess.CalledProcessError, RuntimeError) as e:
        if bar:
            bar.abandon()
        logger.error(f'Compression failed: {e}')
        raise RuntimeError(f'Failed to compress {source_dir}: {e}')


def decompress_archive(
    archive_path: Path,
    output_dir: Path,
    strip_components: int = 0,
    tracker: Optional[PhaseTracker] = None
) -> Path:
    """
    Decompress a tar archive to the specified directory.

    Progress is measured by how far the archive has been read, since the
    uncompressed size isn't known ahead of time.

    Args:
        archive_path: Path to the archive
        output_dir: Directory to extract to
        strip_components: Number of leading path components to strip
        tracker: Phase tracker for progress reporting

    Returns:
        Path to the output directory
    """
    archive_path = Path(archive_path)
    output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    archive_size = archive_path.stat().st_size
    bar = tracker.phase(f'Unpacking {archive_path.name}', total=archive_size) if tracker else None

    archive_str = str(archive_path)
    if archive_str.endswith('.tar.zst') or archive_str.endswith('.zst'):
        if not check_zstd_available():
            if bar:
                bar.abandon()
            raise RuntimeError('zstd is required to decompress this archive')
        decompress_cmd = ['zstd', '-d', '-c', str(archive_path)]
        tar_cmd = ['tar', '-xf', '-', '-C', str(output_dir)]
        if strip_components > 0:
            tar_cmd.extend(['--strip-components', str(strip_components)])

        logger.info(f'Decompressing {archive_path} ({format_bytes(archive_size)}) to {output_dir}')

        zstd_proc = subprocess.Popen(decompress_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tar_proc = subprocess.Popen(tar_cmd, stdin=zstd_proc.stdout)
        zstd_proc.stdout.close()

        if bar:
            # Watch how far zstd has read into the archive
            with poll_read_offset(bar, zstd_proc.pid, archive_path):
                tar_proc.wait()
                zstd_proc.wait()
        else:
            tar_proc.wait()
            zstd_proc.wait()

        if zstd_proc.returncode != 0 or tar_proc.returncode != 0:
            if bar:
                bar.abandon()
            zstd_err = zstd_proc.stderr.read().decode() if zstd_proc.stderr else ''
            error_msg = f'Decompression failed (zstd={zstd_proc.returncode}, tar={tar_proc.returncode})'
            if zstd_err:
                error_msg += f'\nzstd errors: {zstd_err}'
            raise RuntimeError(error_msg)

    elif archive_str.endswith('.tar.gz') or archive_str.endswith('.tgz') or archive_str.endswith('.tar'):
        gzipped = not archive_str.endswith('.tar')
        tar_cmd = ['tar', '-xzf' if gzipped else '-xf', str(archive_path), '-C', str(output_dir)]
        if strip_components > 0:
            tar_cmd.extend(['--strip-components', str(strip_components)])

        logger.info(f'{"Decompressing" if gzipped else "Extracting"} {archive_path} to {output_dir}')

        proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

        if bar:
            # tar reads the archive itself here, so watch tar's own read offset
            with poll_read_offset(bar, proc.pid, archive_path):
                _out, err = proc.communicate()
        else:
            _out, err = proc.communicate()

        if proc.returncode != 0:
            if bar:
                bar.abandon()
            verb = 'Decompression' if gzipped else 'Extraction'
            raise RuntimeError(f'{verb} failed (tar={proc.returncode})\ntar errors: {err}')

    else:
        if bar:
            bar.abandon()
        raise ValueError(f'Unknown archive format: {archive_path}')

    if bar:
        bar.finish(summary=format_bytes(archive_size))

    logger.info(f'Extracted to: {output_dir}')
    return output_dir


def create_checksum(
    file_path: Path,
    algorithm: str = 'sha256',
    tracker: Optional[PhaseTracker] = None
) -> str:
    """
    Create a checksum for a file.

    Args:
        file_path: Path to the file
        algorithm: Hash algorithm to use
        tracker: Phase tracker for progress reporting

    Returns:
        Hex digest of the checksum
    """
    import hashlib

    hash_func = hashlib.new(algorithm)
    file_path = Path(file_path)

    try:
        total = file_path.stat().st_size
    except OSError:
        total = None

    bar = tracker.phase(f'Checksumming ({algorithm})', total=total) if tracker else None
    done = 0
    reported = 0

    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            hash_func.update(chunk)
            if not bar:
                continue
            # Report every few MB rather than every 8KB chunk
            done += len(chunk)
            if done - reported >= CHECKSUM_REPORT_BYTES:
                bar.update(done)
                reported = done

    if bar:
        bar.finish()

    return hash_func.hexdigest()


def verify_checksum(file_path: Path, expected: str, algorithm: str = 'sha256') -> bool:
    """
    Verify a file's checksum.

    Args:
        file_path: Path to the file
        expected: Expected checksum value
        algorithm: Hash algorithm used

    Returns:
        True if checksum matches
    """
    actual = create_checksum(file_path, algorithm)
    return actual.lower() == expected.lower()
