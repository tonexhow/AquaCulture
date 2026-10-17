#!/usr/bin/env bash
# Validate an overwrite-only camera package; update only the existing web service.
set -Eeuo pipefail
if command -v python3 >/dev/null 2>&1; then
  CAMERA_FIX_PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  CAMERA_FIX_PYTHON="$(command -v python)"
else
  printf 'Python 3 is required (existing system Python is sufficient).\n' >&2
  exit 1
fi
exec "$CAMERA_FIX_PYTHON" - "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROJECT = Path('/home/mike/AquaCulture')
PYTHON = PROJECT / '.venv/bin/python'
WEB = 'aquaculture-web.service'
BLE = 'aquaculture-ble.service'
BACKUPS = Path('/var/backups/aquaculture-camera-fix')
MANIFEST = 'startup/camera-fix-manifest.json'
EXPECTED = frozenset('''
.env.example
client/static/index/index.js
database/catalog_migration.py
database/create_database.py
database/maintenance.py
database/media_storage.py
server/__init__.py
server/auth.py
server/api_manager.py
server/app.py
server/camera_devices.py
server/live_streaming.py
server/scanner/fish_recommendation.py
server/scanner/size_detection.py
server/sensors.py
server/settings/admin_password.py
server/settings/database_tables.py
server/settings/password_recovery.py
server/settings/species.py
server/settings/notification/reading_worker.py
server/settings/notification/summary_scheduler.py
startup/deploy-camera-fix.sh
startup/rollback-camera-fix.sh
startup/CAMERA_FIX_REPORT.md
startup/CAMERA_FIX_DEPLOYMENT.md
startup/build_camera_fix.py
'''.split())
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
_deployment_lock = None


def fail(message):
    raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail('Duplicate JSON field: ' + str(key))
        result[key] = value
    return result


def load_json(data):
    return json.loads(data, object_pairs_hook=unique_object)


def relative(value, *, manifest=False):
    allowed = EXPECTED | ({MANIFEST} if manifest else set())
    if not isinstance(value, str) or value not in allowed:
        fail('Package/backup contains an unlisted target.')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in {'', '.', '..'} for p in value.split('/')):
        fail('Unsafe relative path.')
    if '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        fail('Unsafe path characters.')
    forbidden = {'.git', '.venv', 'venv', 'models', 'media', 'network', 'systemd'}
    if any(part.lower() in forbidden for part in path.parts) or value == '.env' or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.service', '.socket', '.timer'}:
        fail('Runtime data or service configuration is forbidden.')
    return path


def run(args, *, timeout=15, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        # Do not echo arbitrary command output, unit environments, or credentials.
        fail('Command failed: ' + Path(args[0]).name + ' ' + str(args[1] if len(args) > 1 else ''))
    return result


def properties(unit):
    fields = ('LoadState', 'User', 'Group', 'WorkingDirectory', 'ExecStart', 'ActiveState', 'UnitFileState', 'MainPID', 'InvocationID')
    output = run(['systemctl', 'show', unit, *['--property=' + p for p in fields]]).stdout
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def enabled(unit):
    result = run(['systemctl', 'is-enabled', unit], check=False)
    return result.returncode == 0 and result.stdout.strip() in {'enabled', 'enabled-runtime'}


def active(unit):
    return run(['systemctl', 'is-active', '--quiet', unit], check=False).returncode == 0


def check_project():
    if PROJECT.resolve() != PROJECT or not PROJECT.is_dir():
        fail('Expected the real project directory /home/mike/AquaCulture, without symlink parents.')
    current = Path(PROJECT.anchor)
    for part in PROJECT.parts[1:]:
        current /= part
        if current.is_symlink():
            fail('Project parents must not be symlinks.')
    for name in ('server', 'client', 'database'):
        target = PROJECT / name
        if not target.is_dir() or target.is_symlink():
            fail('Existing project directory is missing or unsafe: ' + name)


def lock_deployment():
    import fcntl
    global _deployment_lock
    _deployment_lock = os.open('/run/lock/aquaculture-camera-fix.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(_deployment_lock)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        fail('Unsafe deployment lock file.')
    try:
        fcntl.flock(_deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail('Another camera deployment/rollback is already running.')


def check_service():
    import pwd
    account = pwd.getpwnam('mike')
    web = properties(WEB)
    if web.get('LoadState') != 'loaded' or web.get('User') != 'mike' or web.get('WorkingDirectory') != str(PROJECT):
        fail('Existing web service must run as mike in /home/mike/AquaCulture. No unit was changed.')
    executable = re.search(r'(?:^|[ {;])path=([^ ;}]+)', web.get('ExecStart', ''))
    arguments = re.search(r'argv\[\]=([^;]+)', web.get('ExecStart', ''))
    permitted = {str(PYTHON), str(PROJECT / '.venv/bin/python3')}
    argv = shlex.split(arguments.group(1).strip()) if arguments else []
    if not executable or executable.group(1) not in permitted or argv not in [
        [executable.group(1), str(PROJECT / 'server/app.py')],
        [executable.group(1), '-u', str(PROJECT / 'server/app.py')],
    ]:
        fail('Web ExecStart differs from the expected project venv and server/app.py; inspect the unit locally.')
    if not PYTHON.is_file() or not os.access(PYTHON, os.X_OK):
        fail('Existing project .venv/bin/python is unavailable.')
    if web.get('ActiveState') not in {'active', 'inactive', 'failed'}:
        fail('Web service is transitioning; retry when it is stable.')
    if not enabled(WEB) or not active(BLE) or not enabled(BLE):
        fail('Web must already be enabled; BLE must already be active and enabled. No unit will be enabled or repaired here.')
    print('Verified web: user=mike; working directory=/home/mike/AquaCulture; ExecStart=project .venv Python + server/app.py')
    print('Verified BLE active/enabled. Its unit, account, state and runtime directories will be preserved.')
    return account, web, properties(BLE)


def verify_ble(before):
    after = properties(BLE)
    if not active(BLE) or not enabled(BLE):
        fail('BLE is no longer active/enabled; it has not been modified by this helper.')
    for key in ('MainPID', 'InvocationID'):
        if before.get(key) and after.get(key) != before[key]:
            fail('BLE changed during deployment; inspect it separately. This helper never restarts BLE.')
    if not enabled(WEB):
        fail('Web is no longer enabled; unit configuration was not changed by this helper.')


def check_database_schema():
    import sqlite3
    from contextlib import closing
    database = PROJECT / 'database/aquaculture.db'
    if database.is_symlink() or database.resolve() != database or not database.resolve().is_relative_to(PROJECT):
        fail('Live database must be a real file inside the project; no changes made.')
    if not database.is_file():
        fail('Existing database/aquaculture.db is unavailable; no changes made.')
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
            columns = list(connection.execute('PRAGMA table_info(prediction)'))
    except sqlite3.Error:
        fail('Live database schema could not be inspected read-only; no changes made.')
    if any(str(column[1]).lower() == 'data' and column[3] for column in columns):
        fail('Legacy media schema needs a separately reviewed migration (prediction.data is NOT NULL); no changes made.')
    print('Read-only database schema preflight passed; no migration or database write was performed.')


def prepare_package(package):
    import zipfile
    stage = Path(tempfile.mkdtemp(prefix='aquaculture-camera-stage-', dir='/var/tmp' if sys.platform.startswith('linux') else None))
    if stage.resolve().is_relative_to(PROJECT.resolve()):
        fail('Staging must be outside the project.')
    print('Private staging directory: ' + str(stage))
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)) or set(names) != EXPECTED | {MANIFEST}:
            fail('ZIP must contain exactly the expected camera-fix files and manifest, without duplicate or extra entries.')
        if sum(member.file_size for member in members) > MAX_TOTAL:
            fail('Package exceeds the total size limit.')
        for member in members:
            relative(member.filename, manifest=True)
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if member.is_dir() or kind not in {0, stat.S_IFREG} or member.flag_bits & 1 or member.file_size > MAX_FILE:
                fail('ZIP directories, links, special files, encrypted entries and oversized files are refused.')
        raw_manifest = archive.read(MANIFEST)
        manifest = load_json(raw_manifest)
        if not isinstance(manifest, dict) or set(manifest) != {'format', 'files'} or type(manifest.get('format')) is not int or manifest['format'] != 1:
            fail('Unsupported package manifest.')
        entries = manifest['files']
        if not isinstance(entries, list) or len(entries) != len(EXPECTED):
            fail('Manifest has an unexpected target count.')
        hashes = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}:
                fail('Invalid manifest file record.')
            name = str(relative(entry['path']))
            expected_hash = entry['sha256']
            if name in hashes or not isinstance(expected_hash, str) or not re.fullmatch('[0-9a-f]{64}', expected_hash):
                fail('Invalid or duplicate manifest hash.')
            hashes[name] = expected_hash
        if set(hashes) != EXPECTED:
            fail('Manifest does not match the exact camera-fix target set.')
        payload = {}
        for name in sorted(EXPECTED):
            data = archive.read(name)
            if len(data) > MAX_FILE or digest(data) != hashes[name]:
                fail('Package digest mismatch: ' + name)
            if name.endswith('.py'):
                compile(data, name, 'exec')  # In memory; never import the app or create .pyc files.
            path = stage.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if name.endswith('.sh'):
                run(['bash', '-n', str(path)])
            payload[name] = data
        (stage / MANIFEST).write_bytes(raw_manifest)
    print('Verified exact file list, SHA-256 digests, Python syntax and Bash syntax (' + str(len(EXPECTED)) + ' payload files).')
    return stage, payload, hashes, raw_manifest


def open_parent(name, *, create=False, account=None, created=None):
    # Anchor every traversal at the project fd; never follow a symlink component.
    parts = relative(name, manifest=True).parts
    fd = os.open(PROJECT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    walked = []
    try:
        for part in parts[:-1]:
            walked.append(part)
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return None, parts[-1]
                os.mkdir(part, 0o755, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.fchown(child, account.pw_uid, account.pw_gid)
                os.fchmod(child, 0o755)
                created.append('/'.join(walked))
            os.close(fd)
            fd = child
        result, fd = fd, None
        return result, parts[-1]
    finally:
        if fd is not None:
            os.close(fd)


def snapshot(name):
    parent, leaf = open_parent(name)
    if parent is None:
        return None, None
    try:
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None, None
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE or info.st_mode & 0o7000:
                fail('Target is not a bounded ordinary file: ' + name)
            data = handle.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                fail('Target is oversized: ' + name)
            return data, {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                          'mtime_ns': info.st_mtime_ns, 'atime_ns': info.st_atime_ns}
    finally:
        os.close(parent)


def save_metadata(folder, metadata):
    temporary = folder / 'metadata.json.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(metadata, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, folder / 'metadata.json')
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(payload, hashes, account, web, ble):
    if BACKUPS.is_symlink():
        fail('Backup directory must not be a symlink.')
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (Path('/var/backups'), BACKUPS):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail('Backup parents must be root-owned and not writable by other users.')
    folder = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%SZ-', time.gmtime()), dir=BACKUPS))
    # Keep the verified helper payload available even if rollback removes newly
    # introduced startup files from the project.
    tools = folder / 'tools'
    tools.mkdir(mode=0o700)
    for name in ('deploy-camera-fix.sh', 'rollback-camera-fix.sh'):
        with (tools / name).open('xb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload['startup/' + name])
            handle.flush()
            os.fsync(handle.fileno())
    records = []
    # The manifest itself is also preserved/restored, although it is not a payload hash member.
    for name in sorted(EXPECTED | {MANIFEST}):
        original, attributes = snapshot(name)
        record = {'path': name, 'existed': original is not None, 'deployment_sha256': hashes[name]}
        if original is not None:
            target = folder / 'files' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            if digest(target.read_bytes()) != digest(original):
                fail('Original file backup verification failed: ' + name)
            record.update(attributes)
            record['sha256'] = digest(original)
        records.append(record)
    metadata = {'format': 1, 'project': str(PROJECT), 'status': 'backed_up', 'created_directories': [],
                'web_was_active': web.get('ActiveState') == 'active', 'ble_before': ble, 'files': records}
    # ble_before contains only selected non-environment systemctl properties.
    metadata['ble_before'].pop('ExecStart', None)
    save_metadata(folder, metadata)
    print('Backup: ' + str(folder))
    print('Manual rollback: sudo bash ' + shlex.quote(str(tools / 'rollback-camera-fix.sh')) + ' ' + shlex.quote(str(folder)))
    return folder, metadata


def atomic_write(name, data, attributes, account, created, *, restore_timestamps=False):
    parent, leaf = open_parent(name, create=True, account=account, created=created)
    temporary = '.camera-fix-' + os.urandom(12).hex()
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fchown(handle.fileno(), attributes.get('uid', account.pw_uid), attributes.get('gid', account.pw_gid))
                os.fchmod(handle.fileno(), attributes.get('mode', 0o755 if name.endswith('.sh') else 0o644))
                os.fsync(handle.fileno())
            if restore_timestamps and 'mtime_ns' in attributes:
                os.utime(temporary, ns=(attributes['atime_ns'], attributes['mtime_ns']), dir_fd=parent, follow_symlinks=False)
            else:
                # Timestamp-based .pyc validation uses whole seconds plus size.
                # A same-size deployment must not retain the prior source second.
                fresh = time.time_ns()
                previous = attributes.get('mtime_ns')
                if previous is not None and fresh // 1_000_000_000 == previous // 1_000_000_000:
                    fresh += 1_000_000_000
                os.utime(temporary, ns=(fresh, fresh), dir_fd=parent, follow_symlinks=False)
            # Recheck the final component; replacement never follows that component.
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    fail('Unsafe target replaced during deployment: ' + name)
            except FileNotFoundError:
                pass
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def wait_web(*, endpoints=True):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    deadline = time.monotonic() + 75
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    while time.monotonic() < deadline:
        if active(WEB):
            if not endpoints:
                return
            try:
                for path in ('/api/health', '/api/camera/status'):
                    with opener.open('http://127.0.0.1:5000' + path, timeout=2) as response:
                        if response.status != 200:
                            raise ValueError('Endpoint is unavailable')
                        result = load_json(response.read(1024 * 1024))
                        if not isinstance(result, dict):
                            raise ValueError('Endpoint did not return a JSON object')
                        if path == '/api/health' and result.get('ok') is not True:
                            raise ValueError('Backend health is not ready')
                print('Web active; /api/health and /api/camera/status respond. Missing camera hardware does not fail deployment.')
                return
            except (OSError, ValueError, urllib.error.URLError):
                pass
        time.sleep(2)
    fail('Web did not become healthy on 127.0.0.1:5000 within 75 seconds.')


def camera_logs():
    result = run(['journalctl', '-u', WEB, '-n', '120', '--no-pager', '-o', 'cat'], check=False)
    # Show camera lifecycle lines only, with a length cap and basic redaction.
    lines = [line for line in result.stdout.splitlines() if re.search(r'camera|V4L|picamera|libcamera|opencv', line, re.I)][-15:]
    print('Recent camera log lines:')
    for line in lines:
        line = re.sub(r'(?i)(password|token|secret|authorization)(\s*[:=]\s*)\S+', r'\1\2[REDACTED]', line)
        print(line[:500])
    if not lines:
        print('(No matching camera entries in the last 120 web log lines.)')


def validate_backup(folder):
    folder = Path(folder).absolute()
    if folder.parent != BACKUPS or folder.resolve() != folder:
        fail('Backup must be a direct, real child of /var/backups/aquaculture-camera-fix.')
    for path in (BACKUPS, folder, folder / 'metadata.json'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            fail('Backup metadata and directories must be root-owned and protected.')
        if path.name == 'metadata.json':
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                fail('Backup metadata must be a bounded ordinary file.')
        elif not stat.S_ISDIR(info.st_mode):
            fail('Backup paths must be ordinary directories.')
    raw = (folder / 'metadata.json').read_bytes()
    if len(raw) > 1024 * 1024:
        fail('Backup metadata is oversized.')
    metadata = load_json(raw)
    if not isinstance(metadata, dict) or set(metadata) != {'format', 'project', 'status', 'created_directories', 'web_was_active', 'ble_before', 'files'}:
        fail('Invalid backup metadata fields.')
    if type(metadata.get('format')) is not int or metadata['format'] != 1 or metadata.get('project') != str(PROJECT) or type(metadata.get('web_was_active')) is not bool:
        fail('Invalid backup identity.')
    if metadata.get('status') not in {'backed_up', 'applying', 'deployed', 'rolled_back'}:
        fail('Unknown backup status.')
    records = metadata.get('files')
    if not isinstance(records, list) or len(records) != len(EXPECTED) + 1:
        fail('Backup target count does not match this camera fix.')
    names = []
    for record in records:
        if not isinstance(record, dict):
            fail('Invalid backup record.')
        name = str(relative(record.get('path'), manifest=True))
        names.append(name)
        if type(record.get('existed')) is not bool or not re.fullmatch('[0-9a-f]{64}', str(record.get('deployment_sha256', ''))):
            fail('Invalid backup file record.')
        if record['existed']:
            for key in ('uid', 'gid', 'mode', 'atime_ns', 'mtime_ns'):
                if type(record.get(key)) is not int or record[key] < 0:
                    fail('Invalid prior file attributes.')
            if record['mode'] > 0o777 or not re.fullmatch('[0-9a-f]{64}', str(record.get('sha256', ''))):
                fail('Unsafe backup mode or hash.')
            target = folder / 'files' / name
            current = folder / 'files'
            if current.is_symlink() or not current.is_dir():
                fail('Unsafe backup file directory.')
            for part in PurePosixPath(name).parts:
                current /= part
                if current.is_symlink():
                    fail('Backup file parents must not be symlinks.')
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > MAX_FILE:
                fail('Unsafe original backup file.')
            if digest(target.read_bytes()) != record['sha256']:
                fail('Original backup digest mismatch: ' + name)
    if len(set(names)) != len(names) or set(names) != EXPECTED | {MANIFEST}:
        fail('Backup contains unexpected or duplicate targets.')
    allowed_parents = {str(parent) for name in EXPECTED | {MANIFEST} for parent in PurePosixPath(name).parents if str(parent) != '.'}
    created = metadata.get('created_directories')
    if not isinstance(created, list) or len(created) != len(set(created)) or any(p not in allowed_parents for p in created):
        fail('Backup has unsafe directory records.')
    return folder, metadata


def restore(folder, metadata, account):
    # Refuse overwriting user edits made after deployment, including newly created files.
    for record in metadata['files']:
        data, _ = snapshot(record['path'])
        acceptable = {record['deployment_sha256']}
        if record['existed']:
            acceptable.add(record['sha256'])
        if data is not None and digest(data) not in acceptable:
            fail('Rollback would overwrite a subsequent edit: ' + record['path'] + '. Review the private backup locally.')
    for record in metadata['files']:
        name = record['path']
        if record['existed']:
            atomic_write(name, (folder / 'files' / name).read_bytes(), record, account, [], restore_timestamps=True)
        else:
            parent, leaf = open_parent(name)
            if parent is not None:
                try:
                    try:
                        os.unlink(leaf, dir_fd=parent)
                        os.fsync(parent)
                    except FileNotFoundError:
                        pass
                finally:
                    os.close(parent)
    for name in sorted(metadata['created_directories'], key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        parts = PurePosixPath(name).parts
        fd = os.open(PROJECT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                fail('Unsafe rollback directory: ' + name)
            try:
                os.rmdir(parts[-1], dir_fd=fd)  # Exact recorded, empty directory only.
            except OSError as error:
                import errno
                if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise
                print('Retained nonempty directory: ' + name)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)
    metadata['status'] = 'rolled_back'
    save_metadata(folder, metadata)


def restore_service(was_active):
    if was_active:
        run(['systemctl', 'restart', WEB], timeout=45)
        wait_web(endpoints=False)
    else:
        run(['systemctl', 'stop', WEB], timeout=45)


def main():
    parser = argparse.ArgumentParser(description='Verified overwrite-only camera update; preserves BLE and all live configuration/data.')
    parser.add_argument('package', nargs='?', default='AquaCulture_Camera_Fix_Overwrite.zip')
    parser.add_argument('--check', '--dry-run', action='store_true', dest='check', help='Validate package and syntax only; no systemd, project writes, or root required')
    parser.add_argument('--rollback', metavar='BACKUP', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check and args.rollback:
        fail('--check cannot be combined with --rollback.')
    if args.check:
        prepare_package(Path(args.package))
        print('CHECK ONLY: no project, services, credentials, environment or databases were changed.')
        return
    if not sys.platform.startswith('linux') or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        fail('Use sudo bash on the Raspberry Pi for deployment/rollback. --check works without sudo.')
    lock_deployment()
    check_project()
    account, web, ble = check_service()
    if args.rollback:
        folder, metadata = validate_backup(args.rollback)
        # Validate every current target before stopping a working service.
        for record in metadata['files']:
            data, _ = snapshot(record['path'])
            accepted = {record['deployment_sha256'], record.get('sha256')}
            if data is not None and digest(data) not in accepted:
                fail('A target has subsequent edits: ' + record['path'])
        run(['systemctl', 'stop', WEB], timeout=45)
        try:
            restore(folder, metadata, account)
        finally:
            restore_service(metadata['web_was_active'])
        verify_ble(ble)
        print('Restored prior target files and prior web active/inactive state. Backup retained: ' + str(folder))
        return
    check_database_schema()
    stage, payload, hashes, raw_manifest = prepare_package(Path(args.package))
    payload[MANIFEST] = raw_manifest
    hashes[MANIFEST] = digest(raw_manifest)
    folder, metadata = create_backup(payload, hashes, account, web, ble)
    validate_backup(folder)
    # Catch source changes or newly appeared files after backup, before stopping web.
    for record in metadata['files']:
        current, _ = snapshot(record['path'])
        if (current is not None) != record['existed'] or current is not None and digest(current) != record['sha256']:
            fail('Target changed while backing up: ' + record['path'])
    stopped, applying = False, False
    try:
        metadata['status'] = 'applying'
        save_metadata(folder, metadata)
        stopped = True
        run(['systemctl', 'stop', WEB], timeout=45)
        applying = True
        for record in metadata['files']:
            atomic_write(record['path'], payload[record['path']], record if record['existed'] else {}, account, metadata['created_directories'])
            save_metadata(folder, metadata)
        run(['systemctl', 'start', WEB], timeout=45)
        wait_web()
        verify_ble(ble)
        metadata['status'] = 'deployed'
        save_metadata(folder, metadata)
        camera_logs()
        print('Deployment complete. BLE, .env, configuration, credentials, live databases, models and systemd units/drop-ins were preserved.')
        print('Backup retained: ' + str(folder))
    except BaseException:
        print('Deployment failed. Attempting web-only rollback.', file=sys.stderr)
        try:
            if stopped:
                run(['systemctl', 'stop', WEB], timeout=45, check=False)
            if applying:
                restore(folder, metadata, account)
            if stopped:
                restore_service(metadata['web_was_active'])
            print('Automatic rollback completed; original web active/inactive state restored.', file=sys.stderr)
        except BaseException as recovery_error:
            print('Automatic rollback needs local review (' + type(recovery_error).__name__ + ').', file=sys.stderr)
        print('Backup: ' + str(folder), file=sys.stderr)
        print('Manual rollback: sudo bash ' + shlex.quote(str(folder / 'tools/rollback-camera-fix.sh')) + ' ' + shlex.quote(str(folder)), file=sys.stderr)
        raise


def interrupted(signum, frame):
    raise KeyboardInterrupt()


signal.signal(signal.SIGTERM, interrupted)
if hasattr(signal, 'SIGHUP'):
    signal.signal(signal.SIGHUP, interrupted)

try:
    main()
except (Exception, KeyboardInterrupt) as error:
    print('ERROR: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__ + ': deployment/check did not complete; inspect the package and backup locally.'), file=sys.stderr)
    sys.exit(1)
PY
