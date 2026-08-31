"""Exclusive project-file ownership and external-change detection."""
from __future__ import annotations

import hashlib
import os

from PyQt6.QtCore import QLockFile


def file_signature(path: str):
    """Return a content-aware signature, or None when the file is absent."""
    try:
        stat = os.stat(path)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return stat.st_size, stat.st_mtime_ns, digest.hexdigest()
    except OSError:
        return None


class ProjectFileGuard:
    def __init__(self):
        self.path = None
        self._lock = None
        self._signature = None
        self.last_lock_owner = None

    def acquire(self, path: str) -> bool:
        target = os.path.abspath(path)
        if self.path == target and self._lock is not None:
            return True
        candidate = QLockFile(target + ".lock")
        candidate.setStaleLockTime(0)
        if not candidate.tryLock(0):
            ok, pid, host, app = candidate.getLockInfo()
            self.last_lock_owner = {
                "pid": int(pid) if ok else None,
                "host": host if ok else "",
                "app": app if ok else "",
            }
            return False
        self.release()
        self.path = target
        self._lock = candidate
        self._signature = file_signature(target)
        self.last_lock_owner = None
        return True

    def changed_on_disk(self) -> bool:
        return bool(self.path) and file_signature(self.path) != self._signature

    def refresh(self):
        if self.path:
            self._signature = file_signature(self.path)

    def release(self):
        if self._lock is not None:
            self._lock.unlock()
        self.path = None
        self._lock = None
        self._signature = None

