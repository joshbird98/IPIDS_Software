from snap7.util import get_bool, get_int, get_dint, get_dword, get_real, get_string, get_uint, get_time

# ---- type dispatch table ----
TYPE_READERS = {
    "BOOL": lambda data, offset: get_bool(data, int(offset), int(round((offset - int(offset)) * 10))),
    "INT": lambda data, offset: get_int(data, int(offset)),
    "UINT": lambda data, offset: get_uint(data, int(offset)),
    "DINT": lambda data, offset: get_dint(data, int(offset)),
    "UDINT": lambda data, offset: get_uint(data, int(offset)),
    "TIME": lambda data, offset: get_time(data, int(offset)),
    "DWORD": lambda data, offset: get_dword(data, int(offset)),
    "REAL": lambda data, offset: get_real(data, int(offset)),
    "STRING": lambda data, offset: get_string(data, int(offset)),  # adjust max length
}
