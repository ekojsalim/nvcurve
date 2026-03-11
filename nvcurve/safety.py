"""Centralized safety validation for all write operations.

Every write path calls validate_write() before touching hardware.
"""

from .nvapi.constants import CT_POINTS, IDLE_POINT


def validate_write(
    point_deltas: dict[int, int],
    max_delta_khz: int,
    allow_idle: bool = False,
) -> list[str]:
    """Validate a proposed write request.

    Args:
        point_deltas: {point_index: delta_kHz}
        max_delta_khz: absolute delta limit (e.g. 300_000 for ±300 MHz)
        allow_idle: if True, skip the idle-point block

    Returns a list of error message strings. Empty list means the request is safe.
    """
    errors = []
    for point, delta_khz in point_deltas.items():
        if point < 0 or point >= CT_POINTS:
            errors.append(f"Point {point} out of range (0–{CT_POINTS - 1})")
            continue

        if point == IDLE_POINT and not allow_idle:
            errors.append(
                f"Point {IDLE_POINT} is the reserved idle/low-power entry. "
                "Modifying it could prevent the GPU from entering low-power states. "
                "Use --force-idle to override."
            )
            continue

        if abs(delta_khz) > max_delta_khz:
            errors.append(
                f"Delta {delta_khz / 1000:+.0f} MHz for point {point} exceeds "
                f"safety limit of ±{max_delta_khz / 1000:.0f} MHz. "
                "Use --max-delta to raise the limit if needed."
            )

    return errors
