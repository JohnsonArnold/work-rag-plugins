"""Filesystem operations used by the local library's publication protocol."""

from contextlib import contextmanager
import os
from pathlib import Path
from typing import BinaryIO


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt
    import time

    _MOVEFILE_WRITE_THROUGH = 0x8
    _ERROR_LOCK_VIOLATION = 33
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file = _kernel32.LockFile
    _unlock_file = _kernel32.UnlockFile
    _lock_file.argtypes = _unlock_file.argtypes = (wintypes.HANDLE,) + (wintypes.DWORD,) * 4
    _lock_file.restype = _unlock_file.restype = wintypes.BOOL
    _move_file = _kernel32.MoveFileExW
    _move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    _move_file.restype = wintypes.BOOL

    def _move_without_replacing(source: Path, destination: Path) -> None:
        if not _move_file(str(source), str(destination), _MOVEFILE_WRITE_THROUGH):
            raise ctypes.WinError(ctypes.get_last_error())

    @contextmanager
    def exclusive_file_lock(handle: BinaryIO):
        # The lock covers byte zero even if the persistent lock file is empty.
        file_handle = msvcrt.get_osfhandle(handle.fileno())
        while not _lock_file(file_handle, 0, 0, 1, 0):
            error = ctypes.get_last_error()
            if error != _ERROR_LOCK_VIOLATION:
                raise ctypes.WinError(error)
            time.sleep(0.02)
        try:
            yield
        finally:
            if not _unlock_file(file_handle, 0, 0, 1, 0):
                raise ctypes.WinError(ctypes.get_last_error())

    def fsync_dir(path: Path) -> None:
        # Windows flushes file contents before publication and uses a
        # write-through move for published blobs and complete backup trees.
        # It has no unprivileged POSIX-style directory fsync.
        return

    def publish_blob(staging: Path, destination: Path) -> None:
        staging.chmod(0o444)
        _move_without_replacing(staging, destination)

    def remove_staging(path: Path) -> None:
        # A successful move consumes the staging file. On duplicate or failed
        # publication the read-only attribute must be cleared before deletion.
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            return
        path.unlink(missing_ok=True)

    publish_directory = _move_without_replacing

else:
    import fcntl

    @contextmanager
    def exclusive_file_lock(handle: BinaryIO):
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    def fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish_blob(staging: Path, destination: Path) -> None:
        staging.chmod(0o444)
        os.link(staging, destination)

    def remove_staging(path: Path) -> None:
        path.unlink(missing_ok=True)

    def publish_directory(source: Path, destination: Path) -> None:
        # Reserve the path and use a single rename over the empty directory.
        publication_target = destination.resolve()
        destination.mkdir()
        try:
            source.rename(publication_target)
        except OSError:
            if destination.resolve() != publication_target:
                raise RuntimeError("Copy destination changed during publication")
            destination.rmdir()
            raise
