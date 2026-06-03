import os


def harden_windows_process():
    """Programmatically shields the calling background microservice from Windows
    foreground boosting CPU starvation and memory working set trimming by
    explicitly enforcing native 64-bit Win32 API type compliance.
    """
    if os.name != 'nt':
        return

    import ctypes
    from ctypes import wintypes

    # Win32 API Priority & Access Constants
    HIGH_PRIORITY_CLASS = 0x00000080
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400

    kernel32 = ctypes.windll.kernel32

    # --- Strict Ctypes Type Gating for 64-bit Architecture Compatibility ---
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.GetLastError.restype = wintypes.DWORD

    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
    kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # ----------------------------------------------------------------------

    # Extract compile-safe 64-bit process pseudo-handle
    current_process = kernel32.GetCurrentProcess()

    # 1. Elevate Process Priority Class to HIGH
    if kernel32.SetPriorityClass(current_process, HIGH_PRIORITY_CLASS):
        print("[SYS_HARDEN] Process priority class elevated to HIGH_PRIORITY_CLASS.")
    else:
        error_code = kernel32.GetLastError()
        print(f"[SYS_HARDEN] WARNING: Failed to elevate process priority class. Win32 Error Code: {error_code}")

    # 2. Lock Working Set footprint to prevent paging memory blocks to storage
    min_size = ctypes.c_size_t(50 * 1024 * 1024)  # 50 MB
    max_size = ctypes.c_size_t(200 * 1024 * 1024)  # 200 MB

    process_id = kernel32.GetCurrentProcessId()
    process_handle = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION,
        False,
        process_id
    )

    if process_handle:
        if kernel32.SetProcessWorkingSetSize(process_handle, min_size, max_size):
            print("[SYS_HARDEN] Physical RAM Working Set locked (50MB/200MB boundaries).")
        else:
            error_code = kernel32.GetLastError()
            print(f"[SYS_HARDEN] WARNING: SetProcessWorkingSetSize allocation failed. Win32 Error Code: {error_code}")
        kernel32.CloseHandle(process_handle)
    else:
        error_code = kernel32.GetLastError()
        print(
            f"[SYS_HARDEN] WARNING: Security descriptor denied handle access for quota modifications. Win32 Error Code: {error_code}")