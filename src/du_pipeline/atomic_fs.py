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

def open_dir_beneath(root, relative=Path('.'), create=False, durable=False):
    """Pin a directory below root while refusing symlinks in every component."""
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts: raise PermissionError('path escapes managed artifact root')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    # Pin every absolute component as well: O_NOFOLLOW on root alone does not
    # protect an ancestor of root from being replaced by a symlink.
    root = Path(root).absolute()
    if '..' in root.parts: raise PermissionError('invalid managed root')
    fd = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    try:
        for part in relative.parts:
            if part in ('', '.'): continue
            if create:
                try: os.mkdir(part, dir_fd=fd)
                except FileExistsError: pass
            child = os.open(part, flags, dir_fd=fd)
            try:
                if durable:
                    os.fsync(child)
                    os.fsync(fd)
            except BaseException:
                os.close(child)
                raise
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd); raise


def fsync_file_at(fd, name, identity):
    """Flush the proven regular inode, refusing a substituted/symlink leaf."""
    import stat
    leaf = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        if not stat.S_ISREG(os.fstat(leaf).st_mode) or file_identity(os.fstat(leaf)) != tuple(identity):
            raise PermissionError('file identity changed before fsync')
        os.fsync(leaf)
        assert_identity_at(fd, name, identity)
    finally:
        os.close(leaf)


def file_identity(st):
    """Stable inode ownership plus content-change detection (rename changes ctime)."""
    return (st.st_dev, st.st_ino, st.st_uid, st.st_gid, st.st_mode,
            st.st_size, st.st_mtime_ns)


def snapshot_at(fd, name):
    """Read only a regular no-follow leaf; reject replacement during hashing."""
    import hashlib
    import stat
    if not name or Path(name).name != name or name in ('.', '..'):
        raise PermissionError('a single leaf name is required')
    leaf = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(leaf)
        if not stat.S_ISREG(before.st_mode):
            raise PermissionError('not a regular owned file')
        digest = hashlib.sha256()
        while True:
            chunk = os.read(leaf, 1024 * 1024)
            if not chunk: break
            digest.update(chunk)
        identity = file_identity(before)
        if identity != file_identity(os.fstat(leaf)) or before.st_ctime_ns != os.fstat(leaf).st_ctime_ns:
            raise PermissionError('file changed while hashing')
        assert_identity_at(fd, name, identity)
        return identity, digest.hexdigest()
    finally:
        os.close(leaf)


def assert_identity_at(fd, name, identity):
    if file_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != tuple(identity):
        raise PermissionError('file identity changed')


def walk_dirs_at(fd, relative=Path('.')):
    """Yield borrowed pinned directory FDs; no pathname-based walk/reopen."""
    yield relative, fd
    for name in sorted(os.listdir(fd)):
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
        except OSError:
            continue
        try:
            yield from walk_dirs_at(child, relative / name)
        finally:
            os.close(child)