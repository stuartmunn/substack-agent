"""Exclusive file lock with a bounded wait, for read-modify-write of state files."""

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.05


class LockTimeoutError(Exception):
    """Another process held the lock for longer than the timeout."""


@contextmanager
def file_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive lock on ``lock_path``, waiting at most ``timeout`` seconds."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"{lock_path.name} is held by another process"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
