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
    _FILE_RENAME_INFO_CLASS = 3
    _FILE_CREATE = 2
    _FILE_OPEN = 1
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

    class _FileRenameInfoHeader(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOL),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
        ]

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


def adopt_created_handle(
    handle: int,
    flags: int,
    open_handle: object,
    mark_delete: object,
    close_handle: object,
) -> int:
    try:
        return open_handle(handle, flags)
    except BaseException:
        try:
            mark_delete(handle)
        finally:
            close_handle(handle)
        raise


def _mark_delete_handle(handle: int) -> None:
    disposition = _FileDispositionInfo(True)
    if not _set_file_information(
        wintypes.HANDLE(handle),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _handle_identity(handle: int) -> tuple[int, int]:
    information = _ByHandleFileInformation()
    if not _get_file_information(wintypes.HANDLE(handle), ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    file_index = (information.file_index_high << 32) | information.file_index_low
    return information.volume_serial_number, file_index


def _open_parent(parent: Path, expected_identity: tuple[int, int]) -> int:
    handle = _create_file(
        str(parent),
        _FILE_LIST_DIRECTORY | _FILE_ADD_FILE | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _ByHandleFileInformation()
        if not _get_file_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(f"refusing reparse-point parent: {parent}")
        if _handle_identity(handle) != expected_identity:
            raise OSError(f"parent identity changed before mutation: {parent}")
        return handle
    except BaseException:
        _close_handle(handle)
        raise


def _relative_name(name: str) -> tuple[object, object]:
    name_buffer = ctypes.create_unicode_buffer(name)
    name_bytes = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        name_bytes,
        name_bytes + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    return name_buffer, unicode_name


def _create_relative_handle(
    parent_handle: int,
    name: str,
    access: int,
    share: int,
    disposition: int,
) -> int:
    name_buffer, unicode_name = _relative_name(name)
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent_handle,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    child_handle = wintypes.HANDLE()
    status = _nt_create_file(
        ctypes.byref(child_handle),
        access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        share,
        disposition,
        _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT,
        None,
        0,
    )
    del name_buffer
    if status < 0:
        code = _rtl_nt_status_to_dos_error(status)
        raise ctypes.WinError(code)
    return child_handle.value


def _relative_file_identity(parent_handle: int, name: str) -> tuple[int, int]:
    handle = _create_relative_handle(
        parent_handle,
        name,
        _GENERIC_READ | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        _FILE_OPEN,
    )
    try:
        return _handle_identity(handle)
    finally:
        _close_handle(handle)


def _rename_handle(handle: int, parent_handle: int, name: str) -> None:
    encoded_name = name.encode("utf-16-le")
    name_offset = _FileRenameInfoHeader.file_name_length.offset + ctypes.sizeof(wintypes.DWORD)
    buffer = ctypes.create_string_buffer(name_offset + len(encoded_name))
    information = _FileRenameInfoHeader.from_buffer(buffer)
    information.replace_if_exists = True
    information.root_directory = parent_handle
    information.file_name_length = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name))
    if not _set_file_information(
        wintypes.HANDLE(handle),
        _FILE_RENAME_INFO_CLASS,
        buffer,
        len(buffer),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


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
    return _open_file(
        path,
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_DELETE,
        os.O_RDONLY,
    )


def get_handle_identity(fd: int) -> tuple[int, int]:
    if os.name != "nt":
        raise _unsupported()
    return _handle_identity(msvcrt.get_osfhandle(fd))


def get_file_identity(path: Path) -> tuple[int, int]:
    if os.name != "nt":
        raise _unsupported()
    handle = _create_file(
        str(path),
        0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _handle_identity(handle)
    finally:
        _close_handle(handle)


def create_file_exclusive_at(
    parent: Path, name: str, expected_parent_identity: tuple[int, int]
) -> int:
    if os.name != "nt":
        raise _unsupported()
    parent_handle = _open_parent(parent, expected_parent_identity)
    try:
        child_handle = _create_relative_handle(
            parent_handle,
            name,
            _GENERIC_WRITE | _DELETE | _SYNCHRONIZE,
            _FILE_SHARE_READ | _FILE_SHARE_DELETE,
            _FILE_CREATE,
        )
        return adopt_created_handle(
            child_handle,
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
            msvcrt.open_osfhandle,
            _mark_delete_handle,
            _close_handle,
        )
    finally:
        _close_handle(parent_handle)


def atomic_replace_file(
    target_fd: int,
    parent: Path,
    name: str,
    expected_parent_identity: tuple[int, int],
    expected_target_identity: tuple[int, int],
    content: bytes,
    mutation_recorder: object = None,
) -> tuple[int, int]:
    if os.name != "nt":
        raise _unsupported()
    if get_handle_identity(target_fd) != expected_target_identity:
        raise OSError(f"target identity changed before atomic replace: {parent / name}")
    parent_handle = _open_parent(parent, expected_parent_identity)
    temporary_name = f".{name}.{os.getpid()}.{os.urandom(8).hex()}"
    temporary_fd = -1
    renamed = False
    try:
        temporary_handle = _create_relative_handle(
            parent_handle,
            temporary_name,
            _GENERIC_WRITE | _DELETE | _SYNCHRONIZE,
            _FILE_SHARE_READ | _FILE_SHARE_DELETE,
            _FILE_CREATE,
        )
        temporary_fd = adopt_created_handle(
            temporary_handle,
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
            msvcrt.open_osfhandle,
            _mark_delete_handle,
            _close_handle,
        )
        view = memoryview(content)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short write to Windows temporary file")
            view = view[written:]
        os.fsync(temporary_fd)
        temporary_handle = msvcrt.get_osfhandle(temporary_fd)
        intended_identity = _handle_identity(temporary_handle)
        if mutation_recorder is not None:
            mutation_recorder(intended_identity, False)
        if get_handle_identity(target_fd) != expected_target_identity or (
            _relative_file_identity(parent_handle, name) != expected_target_identity
        ):
            raise OSError(f"target identity changed before atomic replace: {parent / name}")
        _rename_handle(temporary_handle, parent_handle, name)
        renamed = True
        if mutation_recorder is not None:
            mutation_recorder(intended_identity, True)
        actual_identity = _relative_file_identity(parent_handle, name)
        if actual_identity != intended_identity:
            raise OSError(
                f"atomic replacement identity changed: expected {intended_identity}, "
                f"found {actual_identity}"
            )
        return intended_identity
    finally:
        if temporary_fd >= 0:
            if not renamed:
                _mark_delete_handle(msvcrt.get_osfhandle(temporary_fd))
            os.close(temporary_fd)
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
    _mark_delete_handle(msvcrt.get_osfhandle(fd))
