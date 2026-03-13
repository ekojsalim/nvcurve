# NvAPI function IDs, struct sizes, offsets, and safety constants.
# All values verified by hex-dump analysis on RTX 5090 (GB202), driver 590.48.01.

# ── Function IDs ────────────────────────────────────────────────────────────

FUNC = {
    # Bootstrap
    "Initialize":          0x0150E828,
    "EnumPhysicalGPUs":    0xE5AC921F,
    "GetFullName":         0xCEEE8E9F,

    # V/F curve (read)
    "GetVFPCurve":         0x21537AD4,  # ClkVfPointsGetStatus
    "GetClockBoostMask":   0x507B4B59,  # ClkVfPointsGetInfo
    "GetClockBoostTable":  0x23F1B133,  # ClkVfPointsGetControl
    "GetCurrentVoltage":   0x465F9BCF,  # ClientVoltRailsGetStatus
    "GetClockBoostRanges": 0x64B43A6A,  # ClkDomainsGetInfo

    # Additional read
    "GetPerfLimits":       0xE440B867,  # PerfClientLimitsGetStatus
    "GetVoltBoostPercent": 0x9DF23CA1,  # ClientVoltRailsGetControl

    # Write
    "SetClockBoostTable":  0x0733E009,  # ClkVfPointsSetControl
}

# ── GetVFPCurve (0x21537AD4) ─────────────────────────────────────────────────
# 128 × 28-byte entries at offset 0x48; freq_kHz at +0x00, volt_uV at +0x04.

VFP_SIZE   = 0x1C28   # 7208 bytes
VFP_BASE   = 0x48
VFP_STRIDE = 0x1C     # 28 bytes
VFP_POINTS = (VFP_SIZE - VFP_BASE) // VFP_STRIDE

# ── Get/SetClockBoostTable (0x23F1B133 / 0x0733E009) ────────────────────────
# 128 × 36-byte entries at offset 0x44; freqDelta (int32, kHz) at entry+0x14.
# Correct layout verified against LACT issue #936 (which had wrong stride 0x48).

CT_SIZE      = 0x2420  # 9248 bytes
CT_BASE      = 0x44
CT_STRIDE    = 0x24    # 36 bytes
CT_DELTA_OFF = 0x14    # freqDelta offset within entry
CT_POINTS    = (CT_SIZE - CT_BASE) // CT_STRIDE

# ── Other struct sizes ───────────────────────────────────────────────────────

MASK_SIZE   = 0x182C   # GetClockBoostMask
VOLT_SIZE   = 0x004C   # GetCurrentVoltage  (voltage µV at offset 0x28)
RANGES_SIZE = 0x0928   # GetClockBoostRanges
PERF_SIZE   = 0x030C   # GetPerfLimits (version 2, not 1)
VBOOST_SIZE = 0x0028   # GetVoltBoostPercent

# ── Safety ───────────────────────────────────────────────────────────────────

MAX_DELTA_KHZ = 1_000_000  # ±1000 MHz hard cap (Blackwell driver limit)
