"""NvAPI bootstrap — library loading, function resolution, versioned struct calls."""

import ctypes
import struct
import sys

from .errors import NVAPI_ERRORS


def load_nvapi() -> ctypes.CDLL:
    """Load libnvidia-api.so from the NVIDIA driver."""
    for name in ("libnvidia-api.so", "libnvidia-api.so.1"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    print("Error: Cannot load libnvidia-api.so")
    print("Ensure the NVIDIA proprietary driver is installed.")
    sys.exit(1)


# Module-level library handle and QueryInterface function pointer.
_nvapi = load_nvapi()
_QI = _nvapi.nvapi_QueryInterface
_QI.restype = ctypes.c_void_p
_QI.argtypes = [ctypes.c_uint32]


def query_interface(fid: int, nargs: int = 2):
    """Resolve an NvAPI function pointer by its 32-bit ID.

    Returns a callable ctypes function, or None if the driver doesn't expose it.
    """
    ptr = _QI(fid)
    if not ptr:
        return None
    return ctypes.CFUNCTYPE(ctypes.c_int32, *[ctypes.c_void_p] * nargs)(ptr)


def nvcall(
    fid: int,
    gpu,
    size: int,
    ver: int = 1,
    pre_fill=None,
) -> tuple[bytes | None, str]:
    """Call an NvAPI function with a versioned struct buffer.

    Allocates a buffer of `size` bytes, writes the version word
    ``(ver << 16) | size`` at offset 0, optionally calls ``pre_fill(buf)``
    to populate request fields, then invokes the function.

    Returns ``(bytes, "OK")`` on success or ``(None, error_description)`` on
    failure.
    """
    func = query_interface(fid)
    if not func:
        return None, "function pointer not found (driver too old?)"
    buf = ctypes.create_string_buffer(size)
    struct.pack_into("<I", buf, 0, (ver << 16) | size)
    if pre_fill:
        pre_fill(buf)
    ret = func(gpu, buf)
    if ret != 0:
        return None, f"error {ret} ({NVAPI_ERRORS.get(ret, 'unknown')})"
    return bytes(buf), "OK"


def nvcall_raw(fid: int, gpu, buf: ctypes.Array) -> tuple[int, str]:
    """Call an NvAPI function with a pre-built mutable buffer.

    Used for write operations where the caller needs full control over the
    buffer contents (e.g. SetClockBoostTable).

    Returns ``(return_code, description)``.
    """
    func = query_interface(fid)
    if not func:
        return -999, "function pointer not found (driver too old?)"
    ret = func(gpu, buf)
    return ret, NVAPI_ERRORS.get(ret, f"unknown ({ret})")
