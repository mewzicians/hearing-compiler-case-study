"""Bounded, ephemeral authorization for native streamed workspace downloads."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import secrets
import threading
import time

from .pipeline import InputError
from .workspace_backup import create_backup, verify_backup

COOKIE = "hearing_backup_download"
DOWNLOAD_PATH = "/api/workspace/backup-download"


class BackupDownloads:
    def __init__(self, store, *, clock=time.monotonic, ttl=600, maximum=2):
        self.store, self.clock, self.ttl, self.maximum = store, clock, ttl, maximum
        self.lock = threading.RLock()
        self.pending = {}
        self.active = set()
        self.cleanup_pending = set()
        self.reserved = 0
        self.closing = False

    def prune(self):
        with self.lock:
            for path in list(self.cleanup_pending):
                path.unlink(missing_ok=True)
                self.cleanup_pending.discard(path)
            expired = [key for key, item in self.pending.items() if item["expires"] <= self.clock()]
            for key in expired:
                self.pending[key]["path"].unlink(missing_ok=True)
                del self.pending[key]

    def prepare(self):
        with self.lock:
            self.prune()
            if self.closing:
                raise InputError("The workspace is stopping; prepare the backup after reopening.")
            if len(self.pending) + len(self.active) + len(self.cleanup_pending) + self.reserved >= self.maximum:
                raise InputError("Finish the pending backup downloads before preparing another.")
            self.reserved += 1
        path = None
        failed = False
        def own(created):
            nonlocal path
            path = created
        try:
            path = create_backup(self.store, on_created=own)
            with path.open("rb", buffering=0) as source:
                sha = hashlib.file_digest(source, "sha256").hexdigest()
                source.seek(0)
                verify_backup(source)
                source.seek(0)
                if hashlib.file_digest(source, "sha256").hexdigest() != sha:
                    raise InputError("The backup changed during verification. Prepare a new snapshot.")
            with self.lock:
                if self.closing:
                    raise InputError("The workspace stopped while preparing its backup.")
                key = secrets.token_urlsafe(32)
                self.pending[key] = {"path": path, "sha256": sha, "expires": self.clock() + self.ttl}
                path = None  # Ownership has moved into the bounded registry.
            return key
        except BaseException:
            failed = True
            raise
        finally:
            try:
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        with self.lock:
                            self.cleanup_pending.add(path)
                        if not failed:
                            raise
                        logging.exception("Prepared backup cleanup deferred after its preparation failure")
            finally:
                with self.lock:
                    self.reserved -= 1

    @contextmanager
    def take(self, key):
        with self.lock:
            self.prune()
            if self.closing or key not in self.pending:
                raise InputError("Prepare a new backup in this app; the download is missing, expired or already used.")
            item = self.pending.pop(key)
            path = item["path"]
            self.active.add(path)
        failed = False
        try:
            with path.open("rb", buffering=0) as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != item["sha256"]:
                    raise InputError("The prepared backup changed. Prepare a new verified snapshot.")
                source.seek(0)
                import os
                yield source, os.fstat(source.fileno()).st_size
        except BaseException:
            failed = True
            raise
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                with self.lock:
                    self.cleanup_pending.add(path)
                # The ZIP headers may already have been sent. Retain the
                # primary failure and never append another HTTP response.
                logging.exception("Prepared backup cleanup deferred%s", " after a transfer error" if failed else "")
            finally:
                with self.lock:
                    self.active.discard(path)

    def close(self):
        with self.lock:
            self.closing = True
            if self.active or self.reserved:
                raise RuntimeError("Backup handlers must drain before their snapshot registry closes.")
            self.prune()
            for key in list(self.pending):
                self.pending[key]["path"].unlink(missing_ok=True)
                del self.pending[key]

    def stop_accepting(self):
        with self.lock:
            self.closing = True


def download_cookie(key="", *, expires=False):
    # Cookies keep the one-use capability out of URLs, logs and JavaScript.
    age = 0 if expires else 600
    return f"{COOKIE}={key}; Path={DOWNLOAD_PATH}; Max-Age={age}; HttpOnly; SameSite=Strict"
