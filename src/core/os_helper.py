import os

def harden_windows_process():
    """Programmatically shields the calling background microservice from Windows
    foreground boosting CPU starvation and memory working set trimming.
    """
    if os.name != 'nt':
        return

    import ctypes

    # Win32 API Constants
    HIGH_PRIORITY_CLASS = 0x00000080
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400

    kernel32 = ctypes.windll.kernel32
    current_process = kernel32.GetCurrentProcess()

    # 1. Elevate Process Priority Class to HIGH
    if kernel32.SetPriorityClass(current_process, HIGH_PRIORITY_CLASS):
        print("[SYS_HARDEN] Process priority class elevated to HIGH_PRIORITY_CLASS.")
    else:
        print("[SYS_HARDEN] WARNING: Failed to elevate process priority class.")

    # 2. Lock Working Set footprint to prevent paging memory blocks to pagefile.sys
    min_size = ctypes.c_size_t(50 * 1024 * 1024)  # 50 MB
    max_size = ctypes.c_size_t(200 * 1024 * 1024)  # 200 MB

    process_handle = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION,
        False,
        kernel32.GetCurrentProcessId()
    )

    if process_handle:
        if kernel32.SetProcessWorkingSetSize(process_handle, min_size, max_size):
            print("[SYS_HARDEN] Physical RAM Working Set locked (50MB/200MB boundaries).")
        else:
            print("[SYS_HARDEN] WARNING: SetProcessWorkingSetSize allocation failed.")
        kernel32.CloseHandle(process_handle)
    else:
        print("[SYS_HARDEN] WARNING: Security descriptor denied handle access for quota modifications.")