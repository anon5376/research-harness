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
    if _WINDOWS:
        return _windows_lock(path)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        try:
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
    if _WINDOWS:
        os.close(fd)
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _windows_lock(path):
    """Deny write/delete sharing until the last inherited handle closes.

    Unlike Windows byte-range locks, sharing restrictions survive the opening
    process's death when a leased step still holds an inherited handle.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(os.fspath(path), 0xC0000000, 1, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        if error.winerror == 32:  # ERROR_SHARING_VIOLATION
            raise Locked() from None
        raise error
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        raise
