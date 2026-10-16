"""Shared local Admin credential store and restart-safe rate limits; no Pi imports."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
GENERIC_RECOVERY = "If the account exists, a password reset link has been sent."


class AuthUnavailable(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, seconds):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__("Authentication temporarily limited. Try again later.")


def default_auth_path():
    if os.environ.get("AQUACULTURE_AUTH_DB"):
        return Path(os.environ["AQUACULTURE_AUTH_DB"])
    shared = Path("/var/lib/aquaculture-auth/admin.sqlite3")
    installed = Path("/etc/aquaculture-auth/installed")
    return shared if sys.platform == "linux" and (installed.exists() or shared.parent.exists()) else ROOT / "database/admin_auth.sqlite3"


def email_address(value):
    if not isinstance(value, str):
        raise ValueError("Enter a valid email address.")
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise ValueError("Enter a valid email address.")
    return value


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or not value.strip() or "\x00" in value:
        raise ValueError("Use a password of 12–128 characters. Spaces are allowed.")
    return value


@contextmanager
def connection(path, readonly=False):
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5) if readonly else sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    except sqlite3.Error:
        raise AuthUnavailable("Local authentication store is unavailable.") from None


class CredentialStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_auth_path()

    def initialize(self, legacy_config=None, *, password=None, email=None):
        """Import the existing password once; never install a guessed default."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640))
        except FileExistsError:
            pass
        with connection(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS admin (id INTEGER PRIMARY KEY CHECK(id=1),
                    password_hash TEXT NOT NULL, email TEXT, revision TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reset_tokens (digest TEXT PRIMARY KEY,
                    revision TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL);
                CREATE TABLE IF NOT EXISTS security_events (id INTEGER PRIMARY KEY,
                    occurred_at REAL NOT NULL, event TEXT NOT NULL);
            """)
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM admin WHERE id=1").fetchone():
                if password is None and legacy_config:
                    try:
                        password = json.loads(Path(legacy_config).read_text(encoding="utf-8")).get("password")
                    except (OSError, ValueError, AttributeError):
                        raise AuthUnavailable("Initialize the local Admin store from a trusted console.") from None
                if not isinstance(password, str) or not password or len(password) > 128:
                    raise AuthUnavailable("No configured Admin credential is available.")
                conn.execute("INSERT INTO admin VALUES (1,?,?,?,?,?)", (generate_password_hash(password), email_address(email) if email else None, secrets.token_hex(16), 1, time.time()))
                self.event(conn, "credential_store_initialized")
        if legacy_config:
            self.remove_plaintext(Path(legacy_config))

    @staticmethod
    def remove_plaintext(path):
        if not path.exists():
            return
        original_stat = path.stat()
        config = json.loads(path.read_text(encoding="utf-8"))
        if "password" not in config:
            return
        del config["password"]
        config["admin_credentials_migrated"] = True
        fd, temporary = tempfile.mkstemp(prefix=".auth-migration-", suffix=".tmp", dir=path.parent)
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.fchown(fd, original_stat.st_uid, original_stat.st_gid)
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2, ensure_ascii=False)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def event(conn, event):
        conn.execute("INSERT INTO security_events (occurred_at,event) VALUES (?,?)", (time.time(), event))
        conn.execute("DELETE FROM security_events WHERE id NOT IN (SELECT id FROM security_events ORDER BY id DESC LIMIT 1000)")

    def state(self):
        with connection(self.path, True) as conn:
            row = conn.execute("SELECT revision,enabled,email FROM admin WHERE id=1").fetchone()
        if not row:
            raise AuthUnavailable("Admin credential is not initialized.")
        return dict(row)

    def verify(self, password):
        with connection(self.path, True) as conn:
            row = conn.execute("SELECT password_hash,revision,enabled FROM admin WHERE id=1").fetchone()
        if not row or not row["enabled"] or not isinstance(password, str) or not 1 <= len(password) <= 128:
            return None
        return row["revision"] if check_password_hash(row["password_hash"], password) else None

    def change(self, current, new_password):
        hashed = generate_password_hash(validate_password(new_password))
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT password_hash,enabled FROM admin WHERE id=1").fetchone()
            if not row or not row["enabled"] or not isinstance(current, str) or not 1 <= len(current) <= 128 or not check_password_hash(row["password_hash"], current):
                return False
            self.set_password(conn, hashed, "password_changed")
        return True

    def set_recovery_email(self, current, email):
        email = email_address(email)
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT password_hash,enabled FROM admin WHERE id=1").fetchone()
            if not row or not row["enabled"] or not isinstance(current, str) or not 1 <= len(current) <= 128 or not check_password_hash(row["password_hash"], current):
                return False
            conn.execute("UPDATE admin SET email=? WHERE id=1", (email,))
            conn.execute("DELETE FROM reset_tokens")
            self.event(conn, "recovery_email_changed")
        return True

    def issue_reset(self, email, *, now=None, ttl=900):
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT email,enabled,revision FROM admin WHERE id=1").fetchone()
            conn.execute("DELETE FROM reset_tokens WHERE expires_at<=? OR used_at IS NOT NULL", (now,))
            if not row or not row["enabled"] or not hmac.compare_digest(row["email"] or "", email):
                return None
            conn.execute("INSERT INTO reset_tokens VALUES (?,?,?,NULL)", (hashlib.sha256(token.encode()).hexdigest(), row["revision"], now + ttl))
            self.event(conn, "password_reset_requested")
        return token

    def discard_reset(self, token):
        with connection(self.path) as conn:
            conn.execute("DELETE FROM reset_tokens WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))
            self.event(conn, "password_reset_delivery_failed")

    def reset(self, token, password, confirmation, *, now=None):
        validate_password(password)
        if password != confirmation:
            raise ValueError("Passwords do not match.")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return False
        now = time.time() if now is None else now
        hashed = generate_password_hash(password)
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            valid = conn.execute("""SELECT t.digest FROM reset_tokens t JOIN admin a ON a.id=1
                WHERE t.digest=? AND t.expires_at>? AND t.used_at IS NULL
                AND a.enabled=1 AND t.revision=a.revision""", (hashlib.sha256(token.encode()).hexdigest(), now)).fetchone()
            if not valid:
                return False
            self.set_password(conn, hashed, "password_reset_completed")
        return True

    def set_password(self, conn, hashed, event):
        conn.execute("UPDATE admin SET password_hash=?,revision=?,updated_at=? WHERE id=1", (hashed, secrets.token_hex(16), time.time()))
        conn.execute("UPDATE reset_tokens SET used_at=? WHERE used_at IS NULL", (time.time(),))
        self.event(conn, event)


class DurableLimiter:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with connection(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS auth_attempts (scope TEXT, request_id TEXT,
                    client TEXT NOT NULL, stamp REAL NOT NULL, failed INTEGER,
                    PRIMARY KEY(scope,request_id));
                CREATE TABLE IF NOT EXISTS auth_clock (id INTEGER PRIMARY KEY, stamp REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_delay (scope TEXT PRIMARY KEY, until_at REAL NOT NULL);
            """)

    def reserve(self, scope, client, request_id, *, now=None, daily=100, failure_limit=5, short_window=900, hour_limit=None):
        now = time.time() if now is None else now
        key = hashlib.sha256(str(client).encode()).hexdigest()
        rid = hashlib.sha256(str(request_id).encode()).hexdigest()
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            high = conn.execute("SELECT stamp FROM auth_clock WHERE id=1").fetchone()
            now = max(now, high[0] if high else now)
            conn.execute("INSERT OR REPLACE INTO auth_clock VALUES (1,?)", (now,))
            conn.execute("DELETE FROM auth_attempts WHERE stamp<=?", (now - 86400,))
            if conn.execute("SELECT 1 FROM auth_attempts WHERE scope=? AND request_id=?", (scope, rid)).fetchone():
                raise ValueError("Duplicate authentication request.")
            stamps = [r[0] for r in conn.execute("SELECT stamp FROM auth_attempts WHERE scope=? ORDER BY stamp", (scope,))]
            wait = stamps[-daily] + 86400 - now if len(stamps) >= daily else 0
            recent = [r[0] for r in conn.execute("SELECT stamp FROM auth_attempts WHERE scope=? AND (failed=1 OR failed IS NULL) AND stamp>? ORDER BY stamp", (scope, now - short_window))]
            if len(recent) >= failure_limit:
                wait = max(wait, recent[-failure_limit] + short_window - now)
            delay = conn.execute("SELECT until_at FROM auth_delay WHERE scope=?", (scope,)).fetchone()
            wait = max(wait, delay[0] - now) if delay else wait
            if hour_limit:
                hourly = [r[0] for r in conn.execute("SELECT stamp FROM auth_attempts WHERE scope=? AND client=? AND stamp>? ORDER BY stamp", (scope, key, now - 3600))]
                if len(hourly) >= hour_limit:
                    wait = max(wait, hourly[-hour_limit] + 3600 - now)
            if wait > 0:
                raise RateLimited(wait)
            conn.execute("INSERT INTO auth_attempts VALUES (?,?,?,?,NULL)", (scope, rid, key, now))
        return rid

    def finish(self, scope, reservation, success, *, now=None):
        now = time.time() if now is None else now
        with connection(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            high = conn.execute("SELECT stamp FROM auth_clock WHERE id=1").fetchone()
            now = max(now, high[0] if high else now)
            conn.execute("UPDATE auth_attempts SET failed=? WHERE scope=? AND request_id=?", (int(not success), scope, reservation))
            failures = conn.execute("SELECT count(*) FROM auth_attempts WHERE scope=? AND failed=1 AND stamp>?", (scope, now - 900)).fetchone()[0]
            delay = min(300, 2 ** min(failures - 1, 8)) if not success else 0
            if delay:
                conn.execute("INSERT OR REPLACE INTO auth_delay VALUES (?,?)", (scope, now + delay))
            return delay

    def clear_local(self, scope):
        with connection(self.path) as conn:
            conn.execute("DELETE FROM auth_attempts WHERE scope=?", (scope,))
            conn.execute("DELETE FROM auth_delay WHERE scope=?", (scope,))
            if not conn.execute("SELECT 1 FROM auth_attempts LIMIT 1").fetchone():
                conn.execute("DELETE FROM auth_clock")


def main():
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="Trusted local Admin credential setup/recovery")
    parser.add_argument("--database", default=str(default_auth_path()))
    parser.add_argument("--legacy-config")
    parser.add_argument("--import-existing")
    parser.add_argument("--email")
    parser.add_argument("--set-password", action="store_true")
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SystemExit("Run this local administrative command with sudo.")
    store = CredentialStore(args.database)
    if args.import_existing and not store.path.exists() and Path(args.import_existing).exists():
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with connection(args.import_existing, True) as old, connection(store.path) as new:
            old.backup(new)
        os.chmod(store.path, 0o640)
    if args.set_password:
        password = validate_password(getpass.getpass("New Admin password: "))
        if password != getpass.getpass("Confirm new Admin password: "):
            raise SystemExit("Passwords do not match.")
        try:
            store.state()
            initialized = True
        except AuthUnavailable:
            initialized = False
        if initialized:
            with connection(store.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                store.set_password(conn, generate_password_hash(password), "local_password_recovery")
        else:
            store.initialize(password=password, email=args.email)
    else:
        store.initialize(args.legacy_config, email=args.email)
    print("Local Admin credential store ready. No password or hash has been printed.")


if __name__ == "__main__":
    main()
