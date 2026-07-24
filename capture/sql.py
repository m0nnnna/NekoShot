"""
Database dumps for PostgreSQL, MySQL/MariaDB, Microsoft SQL Server and SQLite.

Databases are found rather than configured: containers are matched by image name
and their credentials read from the container's own environment, so nothing has
to be written down in NekoShot's config. Native servers are dumped through the
local socket, which is normally trusted for root.
"""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logging import get_logger
from utils.progress import PhaseTracker, format_bytes
from config import SqlConfig, SQL_IMAGE_PATTERNS

logger = get_logger('capture.sql')

# System databases: dumping them back onto another server does more harm than good
MSSQL_SYSTEM_DBS = {'master', 'tempdb', 'model', 'msdb'}
MYSQL_SYSTEM_DBS = {'information_schema', 'performance_schema', 'sys'}

# sqlcmd moved between image generations, and the 18+ build needs -C to accept
# the server's self-signed certificate
SQLCMD_PATHS = [
    '/opt/mssql-tools18/bin/sqlcmd',
    '/opt/mssql-tools/bin/sqlcmd',
    '/usr/bin/sqlcmd',
    'sqlcmd',
]


class SqlTarget:
    """One database server to dump."""

    def __init__(self, engine: str, name: str, container: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None, path: Optional[str] = None):
        self.engine = engine
        self.name = name
        self.container = container
        self.env = env or {}
        self.path = path

    @property
    def where(self) -> str:
        return f'container {self.container}' if self.container else 'host'

    def __repr__(self) -> str:
        return f'<SqlTarget {self.engine}:{self.name} ({self.where})>'


class SqlCapture:
    """Finds database servers and dumps them."""

    # A busy host can make the daemon slow to answer; blinding ourselves to every
    # containerised database over a couple of seconds would be a poor trade
    DOCKER_PROBE_TIMEOUT = 30

    def __init__(self, config: SqlConfig):
        self.config = config
        self.docker_unavailable = False

    # ------------------------------------------------------------------ discovery

    def discover(self) -> List[SqlTarget]:
        """Find every database server worth dumping."""
        targets: List[SqlTarget] = []
        self.docker_unavailable = False

        if self.config.include_docker:
            if self._docker_available():
                targets.extend(self._discover_containers())
            else:
                # Never pass over containers quietly: a snapshot that silently
                # contains none of your databases is worse than a failed one,
                # because it looks like a backup
                self.docker_unavailable = True
                logger.warning('Docker is not responding, so NO databases in containers were backed up!')
                logger.warning('Only databases on the host (if any) are in this snapshot ~nya')

        if self.config.include_native:
            targets.extend(self._discover_native())

        targets.extend(self._discover_sqlite())

        excluded = set(self.config.exclude)
        kept = [t for t in targets if t.name not in excluded and (t.container or '') not in excluded]

        for target in kept:
            logger.info(f'Found {target.engine} on {target.where}: {target.name}')

        skipped = len(targets) - len(kept)
        if skipped:
            logger.info(f'Skipping {skipped} excluded target(s)')

        return kept

    def _docker_available(self) -> bool:
        try:
            result = subprocess.run(['docker', 'info'], capture_output=True,
                                    timeout=self.DOCKER_PROBE_TIMEOUT)
            return result.returncode == 0
        except FileNotFoundError:
            return False  # Docker isn't installed; nothing to look for
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f'Docker did not answer in {self.DOCKER_PROBE_TIMEOUT}s: {e}')
            return False

    def _discover_containers(self) -> List[SqlTarget]:
        """Match running containers against known database images."""
        result = subprocess.run(
            ['docker', 'ps', '--format', '{{.ID}} {{.Image}} {{.Names}}'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return []

        targets = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue

            parts = line.split(None, 2)
            if len(parts) < 3:
                continue

            container_id, image, names = parts
            engine = self._engine_for_image(image)
            if not engine:
                continue

            targets.append(SqlTarget(
                engine=engine,
                name=names.split(',')[0],
                container=container_id,
                env=self._container_env(container_id),
            ))

        return targets

    def _engine_for_image(self, image: str) -> Optional[str]:
        """Which engine an image name implies, if any."""
        lowered = image.lower()
        for engine, patterns in SQL_IMAGE_PATTERNS.items():
            if any(pattern in lowered for pattern in patterns):
                return engine
        return None

    def _container_env(self, container: str) -> Dict[str, str]:
        """
        Read a container's environment.

        This is where database passwords come from: the container already has
        them, so NekoShot never needs its own copy.
        """
        result = subprocess.run(
            ['docker', 'inspect', '-f', '{{json .Config.Env}}', container],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return {}

        try:
            entries = json.loads(result.stdout) or []
        except json.JSONDecodeError:
            return {}

        env = {}
        for entry in entries:
            key, _, value = entry.partition('=')
            env[key] = value
        return env

    def _discover_native(self) -> List[SqlTarget]:
        """Find database servers installed on the host itself."""
        targets = []

        # Postgres: pg_isready confirms a server is actually up, not just installed
        if shutil.which('pg_dumpall') and self._command_ok(['pg_isready', '-q']):
            targets.append(SqlTarget('postgres', 'postgres@host', env=dict(os.environ)))

        if shutil.which('mysqldump') and self._command_ok(['mysqladmin', 'ping', '--silent']):
            targets.append(SqlTarget('mysql', 'mysql@host', env=dict(os.environ)))

        if self._native_sqlcmd():
            targets.append(SqlTarget('mssql', 'mssql@host', env=dict(os.environ)))

        return targets

    def _native_sqlcmd(self) -> Optional[str]:
        for candidate in SQLCMD_PATHS:
            if Path(candidate).exists() or shutil.which(candidate):
                return candidate
        return None

    def _command_ok(self, cmd: List[str]) -> bool:
        try:
            return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _discover_sqlite(self) -> List[SqlTarget]:
        """
        SQLite files named in the config.

        Not auto-discovered: a .db file could be anything, and scanning the disk
        for them would be both slow and wrong.
        """
        targets = []
        for raw in self.config.sqlite_paths:
            path = Path(raw)
            if not path.exists():
                logger.warning(f'SQLite file not found, skipping: {path}')
                continue
            targets.append(SqlTarget('sqlite', path.name, path=str(path)))
        return targets

    # ------------------------------------------------------------------- dumping

    def capture(self, image_dir: Path, tracker: Optional[PhaseTracker] = None) -> Dict[str, Any]:
        """
        Dump every discovered database into image_dir/sql/.

        Returns stats; a failure of one database does not abandon the others.
        """
        targets = self.discover()

        sql_dir = Path(image_dir) / 'sql'
        sql_dir.mkdir(parents=True, exist_ok=True)

        stats = {
            'targets': len(targets), 'dumped': 0, 'failed': 0, 'bytes': 0,
            'databases': [], 'docker_unavailable': self.docker_unavailable,
        }

        if not targets:
            logger.warning('No database servers found ~nya')
            if tracker:
                tracker.phase('Dumping databases').finish(summary='none found')
            return stats

        bar = tracker.phase(f'Dumping {len(targets)} database server(s)', total=len(targets),
                            unit='count') if tracker else None

        dumpers = {
            'postgres': self._dump_postgres,
            'mysql': self._dump_mysql,
            'mssql': self._dump_mssql,
            'sqlite': self._dump_sqlite,
        }

        for index, target in enumerate(targets):
            if bar:
                bar.describe(f'Dumping {target.engine}: {target.name}')

            try:
                produced = dumpers[target.engine](target, sql_dir)
            except Exception as e:
                logger.error(f'Failed to dump {target.name}: {e}')
                produced = []

            if produced:
                stats['dumped'] += 1
                for path in produced:
                    size = path.stat().st_size
                    stats['bytes'] += size
                    stats['databases'].append({
                        'engine': target.engine,
                        'server': target.name,
                        'where': target.where,
                        'file': path.name,
                        'size_bytes': size,
                    })
                    logger.info(f'Dumped {target.name} -> {path.name} ({format_bytes(size)})')
            else:
                stats['failed'] += 1

            if bar:
                bar.update(index + 1)

        with open(sql_dir / 'databases.json', 'w') as f:
            json.dump(stats['databases'], f, indent=2)

        if bar:
            bar.finish(summary=f'{stats["dumped"]}/{len(targets)} servers, {format_bytes(stats["bytes"])}')

        logger.info(f'SQL capture complete ~nya: {stats["dumped"]} dumped, {stats["failed"]} failed')
        return stats

    def _write_dump(self, cmd: List[str], dest: Path, env_extra: Optional[Dict[str, str]] = None) -> Optional[Path]:
        """
        Run a dump command, streaming stdout to dest.

        A dump that fails leaves no file behind: a half-written .sql restored
        later would be worse than no backup at all.
        """
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)

        logger.debug(f'Running: {" ".join(cmd[:3])}...')

        with open(dest, 'wb') as handle:
            process = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.PIPE, env=env)
            _stdout, stderr = process.communicate()

        if process.returncode != 0:
            dest.unlink(missing_ok=True)
            message = (stderr or b'').decode(errors='replace').strip().split('\n')[-1] if stderr else ''
            raise RuntimeError(f'exit {process.returncode}: {message}')

        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError('dump was empty')

        return dest

    def _docker_exec(self, target: SqlTarget, inner: List[str],
                     env_vars: Optional[Dict[str, str]] = None) -> List[str]:
        """Build a `docker exec` command, passing secrets as container env."""
        cmd = ['docker', 'exec']
        for key, value in (env_vars or {}).items():
            cmd.extend(['-e', f'{key}={value}'])
        cmd.append(target.container)
        cmd.extend(inner)
        return cmd

    def _dump_postgres(self, target: SqlTarget, sql_dir: Path) -> List[Path]:
        """pg_dumpall captures every database plus roles in one file."""
        user = target.env.get('POSTGRES_USER', 'postgres')
        password = target.env.get('POSTGRES_PASSWORD', target.env.get('PGPASSWORD', ''))
        dest = sql_dir / f'{_safe(target.name)}-postgres-all.sql'

        if target.container:
            cmd = self._docker_exec(
                target,
                ['pg_dumpall', '-U', user, '--clean', '--if-exists'],
                {'PGPASSWORD': password} if password else None,
            )
            return [self._write_dump(cmd, dest)]

        # Native: run as the postgres user, which the local socket trusts
        cmd = ['pg_dumpall', '-U', user, '--clean', '--if-exists']
        if os.geteuid() == 0 and shutil.which('sudo'):
            cmd = ['sudo', '-u', 'postgres'] + cmd
        return [self._write_dump(cmd, dest, {'PGPASSWORD': password} if password else None)]

    def _dump_mysql(self, target: SqlTarget, sql_dir: Path) -> List[Path]:
        """mysqldump --all-databases, with MariaDB's renamed binary as a fallback."""
        password = (target.env.get('MYSQL_ROOT_PASSWORD')
                    or target.env.get('MARIADB_ROOT_PASSWORD')
                    or target.env.get('MYSQL_PWD', ''))
        user = 'root' if password else target.env.get('MYSQL_USER', 'root')
        if not password:
            password = target.env.get('MYSQL_PASSWORD', '')

        dest = sql_dir / f'{_safe(target.name)}-mysql-all.sql'
        args = ['-u', user, '--all-databases', '--single-transaction', '--routines', '--triggers', '--events']

        last_error = None
        for binary in ['mysqldump', 'mariadb-dump']:
            try:
                if target.container:
                    cmd = self._docker_exec(target, [binary] + args,
                                            {'MYSQL_PWD': password} if password else None)
                else:
                    cmd = [binary] + args
                return [self._write_dump(cmd, dest, {'MYSQL_PWD': password} if password else None)]
            except RuntimeError as e:
                last_error = e
                continue

        raise RuntimeError(f'mysqldump and mariadb-dump both failed ({last_error})')

    def _dump_mssql(self, target: SqlTarget, sql_dir: Path) -> List[Path]:
        """
        BACKUP DATABASE per database, then copy the .bak files out.

        SQL Server has no pg_dumpall equivalent: it writes backups server-side,
        so each file has to be fetched afterwards.
        """
        password = (target.env.get('MSSQL_SA_PASSWORD')
                    or target.env.get('SA_PASSWORD', ''))
        if not password:
            raise RuntimeError(
                'no SA password available (set MSSQL_SA_PASSWORD in the container/environment)'
            )

        sqlcmd = self._find_sqlcmd(target)
        if not sqlcmd:
            raise RuntimeError('sqlcmd not found')

        databases = self._mssql_databases(target, sqlcmd, password)
        if not databases:
            raise RuntimeError('no user databases found')

        produced = []
        backup_dir = '/var/opt/mssql/backup'

        for database in databases:
            remote = f'{backup_dir}/{database}.bak'
            query = f"BACKUP DATABASE [{database}] TO DISK = N'{remote}' WITH INIT, FORMAT"

            self._run_sqlcmd(target, sqlcmd, password, query, make_dir=backup_dir)

            dest = sql_dir / f'{_safe(target.name)}-{_safe(database)}.bak'
            if target.container:
                copy = subprocess.run(
                    ['docker', 'cp', f'{target.container}:{remote}', str(dest)],
                    capture_output=True, text=True
                )
                if copy.returncode != 0:
                    raise RuntimeError(f'docker cp failed: {copy.stderr.strip()}')
                # Don't leave backups piling up inside the container
                subprocess.run(self._docker_exec(target, ['rm', '-f', remote]), capture_output=True)
            else:
                shutil.move(remote, dest)

            produced.append(dest)

        return produced

    def _find_sqlcmd(self, target: SqlTarget) -> Optional[str]:
        if not target.container:
            return self._native_sqlcmd()

        for candidate in SQLCMD_PATHS:
            check = subprocess.run(
                self._docker_exec(target, ['test', '-x', candidate]),
                capture_output=True
            )
            if check.returncode == 0:
                return candidate
        return None

    def _run_sqlcmd(self, target: SqlTarget, sqlcmd: str, password: str, query: str,
                    make_dir: Optional[str] = None, extra: Optional[List[str]] = None) -> str:
        # -C trusts the server certificate, which sqlcmd 18+ requires by default
        inner = [sqlcmd, '-S', 'localhost', '-U', 'sa', '-P', password, '-C', '-b']
        inner.extend(extra or [])
        inner.extend(['-Q', query])

        if target.container:
            if make_dir:
                subprocess.run(self._docker_exec(target, ['mkdir', '-p', make_dir]), capture_output=True)
            cmd = self._docker_exec(target, inner)
        else:
            if make_dir:
                Path(make_dir).mkdir(parents=True, exist_ok=True)
            cmd = inner

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f'sqlcmd failed: {_sqlcmd_error(result.stderr, result.stdout)}')
        return result.stdout

    def _mssql_databases(self, target: SqlTarget, sqlcmd: str, password: str) -> List[str]:
        # -h -1 drops the header row and its separator, -W trims padding. Without
        # them the column header ("name") parses as a database, and backing up a
        # database that doesn't exist fails the whole server.
        output = self._run_sqlcmd(
            target, sqlcmd, password,
            'SET NOCOUNT ON; SELECT name FROM sys.databases WHERE database_id > 4',
            extra=['-h', '-1', '-W'],
        )

        databases = []
        for line in output.split('\n'):
            name = line.strip()
            if not name or name.startswith('-') or 'rows affected' in name:
                continue
            if name.lower() in MSSQL_SYSTEM_DBS:
                continue
            databases.append(name)
        return databases

    def _dump_sqlite(self, target: SqlTarget, sql_dir: Path) -> List[Path]:
        """
        Copy the database through SQLite's online backup API.

        Uses Python's own sqlite3 module rather than the sqlite3 binary: it is
        always present, and it takes a consistent snapshot even while the
        application is writing. A plain file copy could catch a torn
        transaction.
        """
        source = Path(target.path)
        dest = sql_dir / f'{_safe(source.stem)}.sqlite'

        # Read-only URI so a backup can never modify the live database
        source_conn = sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=30)
        try:
            dest_conn = sqlite3.connect(str(dest))
            try:
                source_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source_conn.close()

        return [dest]


def _safe(name: str) -> str:
    """Make a name safe to use as a filename."""
    return ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name)


def _sqlcmd_error(stderr: str, stdout: str) -> str:
    """
    Pull the useful part out of a sqlcmd failure.

    sqlcmd reports the cause first and the generic "BACKUP DATABASE is
    terminating abnormally" last, so quoting the last line explains nothing.
    """
    lines = [line.strip() for line in ((stderr or '') + '\n' + (stdout or '')).split('\n') if line.strip()]
    if not lines:
        return 'unknown error'

    explanatory = [
        line for line in lines
        if not line.startswith('Msg ')
        and 'terminating abnormally' not in line
        and not line.startswith('Sqlcmd: Error:')
    ]
    return (explanatory or lines)[0][:300]
