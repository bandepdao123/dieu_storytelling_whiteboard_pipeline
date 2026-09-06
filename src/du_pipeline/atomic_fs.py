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

def rename_noreplace_at(source_fd, source_name, destination_fd, destination_name):
    """Atomically rename relative to already-pinned directory descriptors."""
    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None: raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(source_fd, os.fsencode(source_name), destination_fd, os.fsencode(destination_name), RENAME_NOREPLACE):
        error = ctypes.get_errno()
        if error == errno.EEXIST: raise FileExistsError(error, os.strerror(error), os.fspath(destination_name))
        raise OSError(error, os.strerror(error), os.fspath(destination_name))

def open_dir_beneath(root, relative=Path('.'), create=False):
    """Pin a directory below root while refusing symlinks in every component."""
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts: raise PermissionError('path escapes managed artifact root')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in relative.parts:
            if part in ('', '.'): continue
            if create:
                try: os.mkdir(part, dir_fd=fd)
                except FileExistsError: pass
            child = os.open(part, flags, dir_fd=fd); os.close(fd); fd = child
        return fd
    except Exception:
        os.close(fd); raise