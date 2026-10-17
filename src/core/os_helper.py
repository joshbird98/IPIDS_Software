import logging
import os

log = logging.getLogger("os_helper")


def harden_windows_process(limit_working_set=True):
    """Programmatically shields the calling background microservice from Windows
    foreground boosting CPU starvation and memory working set trimming by
    explicitly enforcing native 64-bit Win32 API type compliance.

    limit_working_set: apply the 50/200 MB working-set bounds. Pass False for
    processes that legitimately need more than 200 MB resident (e.g. the data
    viewer, whose live ring buffer alone is ~270 MB) - otherwise, under memory
    pressure, Windows trims them towards 200 MB and they page heavily.
    """
    if os.name != 'nt':
        return

    from src.core.paths import simulation_enabled
    if simulation_enabled():
        # Simulated devices on a laptop: don't run a dozen spin-waiting processes at HIGH priority
        log.info("Simulation mode: leaving process priority and working set at Windows defaults.")
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
        log.info("Process priority class elevated to HIGH_PRIORITY_CLASS.")
    else:
        error_code = kernel32.GetLastError()
        log.warning("Failed to elevate process priority class. Win32 error code: %s", error_code)

    if not limit_working_set:
        return

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
            log.info("Physical RAM working set locked (50MB/200MB boundaries).")
        else:
            error_code = kernel32.GetLastError()
            log.warning("SetProcessWorkingSetSize failed. Win32 error code: %s", error_code)
        kernel32.CloseHandle(process_handle)
    else:
        error_code = kernel32.GetLastError()
        log.warning("Security descriptor denied handle access for quota modifications. Win32 error code: %s",
                    error_code)