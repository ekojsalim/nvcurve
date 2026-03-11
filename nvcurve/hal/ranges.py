"""Clock boost range queries."""

import struct
from typing import Optional

from ..nvapi.bootstrap import nvcall
from ..nvapi.constants import FUNC, RANGES_SIZE


def get_clock_ranges(gpu) -> tuple[Optional[dict], str]:
    """Read clock domain min/max offset ranges via GetClockBoostRanges.

    Returns ({"num_domains": int, "domains": [[int, ...], ...]}, "OK")
    or (None, error).

    On RTX 5090: GPU core ±1000 MHz, memory -1000/+3000 MHz.
    """
    d, err = nvcall(FUNC["GetClockBoostRanges"], gpu, RANGES_SIZE, ver=1)
    if not d:
        return None, err

    num = struct.unpack_from("<I", d, 4)[0]
    domains = []
    for i in range(min(num, 32)):
        base = 0x08 + i * 0x48
        if base + 0x48 > len(d):
            break
        words = [struct.unpack_from("<i", d, base + j)[0] for j in range(0, 0x48, 4)]
        domains.append(words)

    return {"num_domains": num, "domains": domains}, "OK"
