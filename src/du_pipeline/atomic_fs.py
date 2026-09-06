"""Atomic local-filesystem publication primitives.

Linux ``renameat2(RENAME_NOREPLACE)`` is required.  We deliberately fail closed
when the kernel/filesystem does not support it: emulating this with an existence
check followed by rename would reintroduce the overwrite race.
"""
import ctypes
import errno
import os
from pathlib import Path

AT_FDCWD = -100
RENAME_NOREPLACE = 1


def rename_noreplace(source, destination):
    """Atomically rename *source* only if *destination* is absent.

    Both paths must be on a Linux local filesystem supporting renameat2.  EXDEV,
    ENOSYS and EINVAL are surfaced; there is intentionally no unsafe copy/link
    fallback.  On collision FileExistsError is raised and both files are intact.
    """
    source, destination = Path(source), Path(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE)
    if result:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), str(destination))
        raise OSError(error, os.strerror(error), str(destination))