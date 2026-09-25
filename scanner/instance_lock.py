"""Cross-platform, crash-released guard for one Edge installation.

The file contents are diagnostic only. The OS byte/flock lock is the authority.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class ScannerInstanceLock:
    def __init__(self, path: Path, mode: str) -> None:
        self.path = Path(path)
        self.mode = mode
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.fstat(fd).st_size == 0:
                handle.write(b"\0")
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.seek(1)
                owner = handle.read(100).decode("ascii", errors="replace").strip() or "unknown owner"
                raise RuntimeError(f"Edge Scanner sibling already running ({owner}); {self.mode} will exit") from exc
            self._file = handle
            handle.seek(1)
            handle.write(f"PID {os.getpid()} mode={self.mode}".encode("ascii"))
            handle.truncate()
        except BaseException:
            if self._file is None:
                handle.close()
            raise

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ScannerInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
