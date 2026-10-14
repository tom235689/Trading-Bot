"""Advisory file locks that the operating system releases when the process dies."""

import sys
from typing import IO


def lock_file(handle: IO[str], *, lock: bool) -> None:
    """Take (or release) the lock on an open file without waiting; OSError when it is held."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)
