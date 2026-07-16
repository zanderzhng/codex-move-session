from __future__ import annotations

import os
from pathlib import Path


def _unsupported() -> OSError:
    return OSError("Windows file-handle operations are unavailable on this platform")


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _SYNCHRONIZE = 0x00100000
    _FILE_LIST_DIRECTORY = 0x00000001
    _FILE_ADD_FILE = 0x00000002
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_CREATE = 2
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x00000040

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class _IoStatusBlockUnion(ctypes.Union):
        _fields_ = [("status", wintypes.LONG), ("pointer", wintypes.LPVOID)]

    class _IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("value", _IoStatusBlockUnion), ("information", ctypes.c_size_t)]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _set_file_information = _kernel32.SetFileInformationByHandle
    _set_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _set_file_information.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _get_file_information.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll")
    _nt_create_file = _ntdll.NtCreateFile
    _nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _nt_create_file.restype = wintypes.LONG
    _rtl_nt_status_to_dos_error = _ntdll.RtlNtStatusToDosError
    _rtl_nt_status_to_dos_error.argtypes = (wintypes.LONG,)
    _rtl_nt_status_to_dos_error.restype = wintypes.ULONG
    _invalid_handle = wintypes.HANDLE(-1).value


def _open_file(path: Path, access: int, share: int, crt_flags: int) -> int:
    handle = _create_file(
        str(path),
        access,
        share,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, crt_flags | getattr(os, "O_BINARY", 0))
    except BaseException:
        _close_handle(handle)
        raise


def open_file_for_delete(path: Path) -> int:
    if os.name != "nt":
        raise _unsupported()
    return _open_file(
        path,
        _GENERIC_READ | _DELETE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        os.O_RDONLY,
    )


def open_file_for_update(path: Path) -> int:
    if os.name != "nt":
        raise _unsupported()
    return _open_file(path, _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ, os.O_RDWR)


def create_file_exclusive_at(
    parent: Path, name: str, expected_parent_identity: tuple[int, int]
) -> int:
    if os.name != "nt":
        raise _unsupported()
    parent_handle = _create_file(
        str(parent),
        _FILE_LIST_DIRECTORY | _FILE_ADD_FILE | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if parent_handle == _invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    child_handle = wintypes.HANDLE()
    try:
        information = _ByHandleFileInformation()
        if not _get_file_information(parent_handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(f"refusing reparse-point parent: {parent}")
        file_index = (information.file_index_high << 32) | information.file_index_low
        if file_index != expected_parent_identity[1]:
            raise OSError(f"parent identity changed before restore: {parent}")

        name_buffer = ctypes.create_unicode_buffer(name)
        name_bytes = len(name.encode("utf-16-le"))
        unicode_name = _UnicodeString(
            name_bytes,
            name_bytes + 2,
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent_handle,
            ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        status = _nt_create_file(
            ctypes.byref(child_handle),
            _GENERIC_WRITE | _DELETE | _SYNCHRONIZE,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_DELETE,
            _FILE_CREATE,
            _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
            None,
            0,
        )
        if status < 0:
            code = _rtl_nt_status_to_dos_error(status)
            raise ctypes.WinError(code)
        try:
            return msvcrt.open_osfhandle(
                child_handle.value, os.O_WRONLY | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            _close_handle(child_handle)
            raise
    finally:
        _close_handle(parent_handle)


def write_file_by_fd(fd: int, content: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to Windows file handle")
        view = view[written:]
    os.fsync(fd)


def delete_file_by_fd(fd: int) -> None:
    if os.name != "nt":
        raise _unsupported()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    disposition = _FileDispositionInfo(True)
    if not _set_file_information(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
