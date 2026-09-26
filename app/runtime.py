"""Process lifetime helpers for the polling bot."""

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunningError(RuntimeError):
    """Another process already owns the bot's instance lock."""


def _acquire_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        # Windows permits locking a byte beyond EOF, including an empty file.
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    """Hold an OS lock until context exit or process termination.

    The file stays on disk so all contenders always lock the same file. Its
    contents record the last owner's PID; only the OS lock indicates ownership.
    """
    with lock_path.open("a+b") as lock_file:
        try:
            _acquire_lock(lock_file)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise AlreadyRunningError(
                    f"Another bot process is already running (lock: {lock_path})"
                ) from exc
            raise

        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(f"{os.getpid()}\n".encode("ascii"))
            lock_file.flush()
            yield
        finally:
            _release_lock(lock_file)
