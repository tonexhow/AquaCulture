"""Safe, filesystem-backed storage for AquaCulture generated images and ZIP exports.

SQLite retains relative paths only. Paths are never accepted from an external URL.
Legacy BLOBs can be migrated with migrate_legacy_media().
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DATABASE_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = DATABASE_DIR / 'media'
SPECIES_DIR = MEDIA_ROOT / 'species'
PREDICTIONS_DIR = MEDIA_ROOT / 'predictions'
THUMBNAILS_DIR = MEDIA_ROOT / 'thumbnails'
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def image_type(blob: bytes) -> tuple[str, str]:
    if blob.startswith(b'\xff\xd8\xff'):
        return '.jpg', 'image/jpeg'
    if blob.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png', 'image/png'
    if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        return '.webp', 'image/webp'
    if blob[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif', 'image/gif'
    if len(blob) > 12 and blob[4:8] == b'ftyp' and blob[8:12] in (b'avif', b'avis'):
        return '.avif', 'image/avif'
    raise ValueError('Unsupported image. Use JPEG, PNG, WebP, GIF or AVIF.')


def resolve_media_path(path: str | None, *, kind: str) -> Path | None:
    """Return trusted path only when it is strictly beneath a managed folder."""
    if not path or not isinstance(path, str):
        return None
    allowed = SPECIES_DIR if kind == 'species' else PREDICTIONS_DIR if kind == 'prediction' else None
    if allowed is None:
        return None
    try:
        candidate = (DATABASE_DIR / path).resolve(strict=False)
        if not candidate.is_relative_to(allowed.resolve(strict=False)):
            return None
        return candidate
    except (OSError, ValueError):
        return None


def save_media(blob: bytes, *, kind: str, record_id: int, legacy: bool = False) -> str:
    if not isinstance(blob, bytes) or not blob:
        raise ValueError('Empty media content.')
    if kind == 'species':
        if len(blob) > MAX_IMAGE_BYTES and not legacy:
            raise ValueError('Image exceeds 12 MB.')
        ext, _ = image_type(blob)
        directory, prefix = SPECIES_DIR, 'species'
    elif kind == 'prediction':
        if not zipfile.is_zipfile(io.BytesIO(blob)):
            raise ValueError('Prediction result is not a ZIP archive.')
        ext, directory, prefix = '.zip', PREDICTIONS_DIR, 'prediction'
    else:
        raise ValueError('Unknown storage type.')
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(blob).hexdigest()
    filename = f'{prefix}-{int(record_id):05d}-{digest[:16]}{ext}'
    target = directory / filename
    if target.exists():
        if target.stat().st_size == len(blob) and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            return target.relative_to(DATABASE_DIR).as_posix()
        raise ValueError('Existing media checksum mismatch; refusing to overwrite.')
    descriptor, tmp_name = tempfile.mkstemp(prefix='.upload-', suffix='.tmp', dir=str(directory))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        if hashlib.sha256(Path(tmp_name).read_bytes()).hexdigest() != digest:
            raise IOError('Media checksum verification failed.')
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target.relative_to(DATABASE_DIR).as_posix()


def migrate_legacy_media(db_path: Path) -> dict[str, int | bool]:
    """One-time non-destructive BLOB migration. Rebuilds prediction schema if NOT NULL blocks NULL.

    Files are fully written + hashed *before* the corresponding BLOB is released.
    Media paths stay relative to database/ to survive moving the project folder.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return {'species_images': 0, 'prediction_zips': 0, 'changed': False}
    counts = {'species_images': 0, 'prediction_zips': 0, 'changed': False}
    with closing(sqlite3.connect(db_path, timeout=45)) as conn, conn:
        conn.execute('PRAGMA busy_timeout=45000')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'species' not in tables or 'prediction' not in tables:
            return counts
        species_columns = {row[1] for row in conn.execute('PRAGMA table_info(species)')}
        prediction_columns = {row[1] for row in conn.execute('PRAGMA table_info(prediction)')}
        if 'image_path' not in species_columns:
            conn.execute('ALTER TABLE species ADD COLUMN image_path TEXT')
            counts['changed'] = True
        if 'data_path' not in prediction_columns:
            conn.execute('ALTER TABLE prediction ADD COLUMN data_path TEXT')
            counts['changed'] = True
        conn.commit()
        # Saving and verifying each media file is deliberately performed before releasing any database BLOB.
        for row_id, blob, existing in conn.execute('SELECT id, image, image_path FROM species WHERE image IS NOT NULL'):
            if not isinstance(blob, bytes) or not blob:
                continue
            path = save_media(blob, kind='species', record_id=row_id, legacy=True)
            conn.execute('UPDATE species SET image_path=?, image=NULL WHERE id=?', (path, row_id))
            counts['species_images'] += 1
        conn.commit()
        for row_id, blob, existing in conn.execute('SELECT id, data, data_path FROM prediction WHERE data IS NOT NULL'):
            if not isinstance(blob, bytes) or not blob:
                continue
            path = save_media(blob, kind='prediction', record_id=row_id)
            conn.execute('UPDATE prediction SET data_path=? WHERE id=?', (path, row_id))
            counts['prediction_zips'] += 1
        conn.commit()
        cols = {row[1]: row for row in conn.execute('PRAGMA table_info(prediction)')}
        # Legacy schema defines data BLOB NOT NULL. Rebuild atomically so path-only saves work.
        if cols['data'][3]:
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute('''CREATE TABLE prediction_storage_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    data BLOB,
                    data_path TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )''')
                conn.execute('''INSERT INTO prediction_storage_new (id, session_id, data, data_path, created_at)
                    SELECT id, session_id,
                           CASE WHEN data_path IS NOT NULL THEN NULL ELSE data END,
                           data_path, created_at FROM prediction''')
                conn.execute('DROP TABLE prediction')
                conn.execute('ALTER TABLE prediction_storage_new RENAME TO prediction')
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            counts['changed'] = True
        else:
            conn.execute('UPDATE prediction SET data=NULL WHERE data IS NOT NULL AND data_path IS NOT NULL')
            conn.commit()
        if counts['species_images'] or counts['prediction_zips']:
            counts['changed'] = True
        status = conn.execute('PRAGMA integrity_check').fetchone()[0]
        if status != 'ok':
            raise RuntimeError(f'SQLite integrity check failed: {status}')
        # Shrink file only when content was migrated, never every time the server boots.
        if counts['changed']:
            conn.execute('VACUUM')
            conn.execute('ANALYZE')
        return counts


def thumbnail_for(original: Path, *, max_size: int = 640) -> Path:
    """Compatibility helper: every display now uses the single original image.

    Older callers may still request a thumbnail. Never generate or reuse a
    second stored copy; existing legacy thumbnails are left untouched.
    """
    return original


def _library_directories() -> dict[str, Path]:
    """List managed media folders without exposing database/configuration files."""
    directories = {'species': SPECIES_DIR, 'predictions': PREDICTIONS_DIR}
    if MEDIA_ROOT.is_dir():
        root = MEDIA_ROOT.resolve()
        for directory in sorted(MEDIA_ROOT.iterdir(), key=lambda item: item.name.casefold()):
            if (directory.name.startswith('.') or directory.is_symlink()
                    or not directory.is_dir() or not directory.resolve().is_relative_to(root)):
                continue
            directories[directory.name] = directory
    return directories


def resolve_library_file(folder: str, filename: str) -> Path | None:
    """Resolve a Library selection strictly inside its managed media folder."""
    directory = _library_directories().get(folder)
    if directory is None or not filename or '\\' in filename or ':' in filename or '\x00' in filename:
        return None
    parts = filename.split('/')
    if any(not part or part.startswith('.') for part in parts):
        return None
    try:
        root = MEDIA_ROOT.resolve()
        allowed = directory.resolve()
        candidate = (directory / filename).resolve()
        if (not allowed.is_relative_to(root) or not candidate.is_relative_to(allowed)
                or not candidate.is_file()):
            return None
        return candidate
    except (OSError, ValueError):
        return None


def list_media_library(folder: str = 'species') -> dict[str, object]:
    """Read folder summaries and files for the authenticated Settings Library."""
    directories = _library_directories()
    if folder not in directories:
        raise ValueError('Library folder was not found.')
    labels = {'species': 'Images', 'predictions': 'ZIPs', 'thumbnails': 'Legacy thumbnails'}
    folders: list[dict[str, object]] = []
    selected_files: list[dict[str, object]] = []
    for folder_id, directory in directories.items():
        files: list[dict[str, object]] = []
        if directory.is_dir():
            for entry in directory.rglob('*'):
                try:
                    relative_name = entry.relative_to(directory).as_posix()
                    path = resolve_library_file(folder_id, relative_name)
                    if path is None or entry.is_symlink():
                        continue
                    stat = path.stat()
                    mime_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                    previewable = mime_type in {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'}
                    kind = 'image' if previewable else 'zip' if path.suffix.lower() == '.zip' else 'file'
                    url = f'/api/admin/library/{quote(folder_id, safe="")}/{quote(relative_name, safe="/")}'
                    files.append({
                        'name': entry.name, 'path': entry.relative_to(DATABASE_DIR).as_posix(),
                        'size': stat.st_size, 'size_bytes': stat.st_size,
                        'modified_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds'),
                        'kind': kind, 'mime_type': mime_type, 'previewable': previewable,
                        'url': url, 'download_url': url + '?download=1',
                    })
                except (OSError, ValueError):
                    # A concurrent upload/removal must not break the whole Library.
                    continue
        files.sort(key=lambda item: str(item['path']).casefold())
        label = labels.get(folder_id, folder_id.replace('_', ' ').title())
        folders.append({
            'id': folder_id, 'name': folder_id, 'label': label,
            'path': directory.relative_to(DATABASE_DIR).as_posix(),
            'kind': 'image' if folder_id in {'species', 'thumbnails'} else 'zip' if folder_id == 'predictions' else 'folder',
            'exists': directory.is_dir(), 'file_count': len(files),
            'total_bytes': sum(int(item['size']) for item in files), 'legacy': folder_id == 'thumbnails',
        })
        if folder_id == folder:
            selected_files = files
    return {
        'ok': True, 'folder': folder, 'folders': folders, 'files': selected_files,
        'total_files': sum(int(item['file_count']) for item in folders),
        'total_bytes': sum(int(item['total_bytes']) for item in folders),
    }
