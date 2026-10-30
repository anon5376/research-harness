"""Exclusive, non-blocking file locks using the platform's native descriptor API."""
import errno
import os

_WINDOWS = os.name == 'nt'
if _WINDOWS:
    import msvcrt
else:
    import fcntl


class Locked(Exception):
    """Another process holds the lock."""


def try_lock(path) -> int:
    """Open or create path, take an exclusive non-blocking lock, return the fd. Raise Locked if it is held."""
    flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) if _WINDOWS else os.O_RDONLY
    fd = os.open(path, flags | os.O_CREAT, 0o666)
    try:
        if _WINDOWS:
            # Use a real first byte for the Windows byte-range lock.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'\0')
            os.lseek(fd, 0, os.SEEK_SET)
        try:
            if _WINDOWS:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise Locked() from None
            raise
    except BaseException:
        os.close(fd)
        raise
    return fd


def unlock(fd: int) -> None:
    """Release the lock and close the fd."""
    try:
        if _WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
