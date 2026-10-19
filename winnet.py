"""
winnet - status & counter adapter langsung dari Windows API (iphlpapi.GetIfEntry2).
Tanpa memanggil PowerShell/proses lain, jadi sangat ringan untuk polling.
Copyright (c) 2026 Bayu Saputra. MIT License.
"""

import ctypes
from ctypes import wintypes

_u64 = ctypes.c_uint64


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", _u64),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", ctypes.c_byte * 16),
        ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", ctypes.c_byte * 16),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", _u64),
        ("ReceiveLinkSpeed", _u64),
    ] + [(n, _u64) for n in (
        "InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards", "InErrors",
        "InUnknownProtos", "InUcastOctets", "InMulticastOctets", "InBroadcastOctets",
        "OutOctets", "OutUcastPkts", "OutNUcastPkts", "OutDiscards", "OutErrors",
        "OutUcastOctets", "OutMulticastOctets", "OutBroadcastOctets", "OutQLen")]


try:
    _ip = ctypes.WinDLL("iphlpapi")
    _ip.GetIfEntry2.argtypes = [ctypes.POINTER(MIB_IF_ROW2)]
    _ip.ConvertInterfaceAliasToLuid.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_u64)]
except (OSError, AttributeError):
    _ip = None

_luid_cache = {}


def _luid(alias):
    if alias in _luid_cache:
        return _luid_cache[alias]
    v = _u64()
    if _ip is None or _ip.ConvertInterfaceAliasToLuid(alias, ctypes.byref(v)) != 0:
        return None
    _luid_cache[alias] = v.value
    return v.value


def row(alias):
    """MIB_IF_ROW2 untuk adapter, atau None."""
    luid = _luid(alias)
    if luid is None:
        return None
    r = MIB_IF_ROW2()
    r.InterfaceLuid = luid
    if _ip.GetIfEntry2(ctypes.byref(r)) != 0:
        _luid_cache.pop(alias, None)
        return None
    return r


def is_up(alias):
    r = row(alias)
    return bool(r and r.OperStatus == 1)


def link_speed(alias):
    r = row(alias)
    return r.ReceiveLinkSpeed if r and r.OperStatus == 1 else 0


assert ctypes.sizeof(MIB_IF_ROW2) == 1352, ctypes.sizeof(MIB_IF_ROW2)
