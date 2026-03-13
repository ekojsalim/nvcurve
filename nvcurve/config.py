"""User-configurable settings with sensible defaults."""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Safety limits
    max_delta_khz: int = 1000_000        # ±1000 MHz hard cap
    auto_snapshot: bool = True          # Save snapshot before every write
    max_snapshots: int = 20             # Maximum snapshots to keep (0 = unlimited)

    # Monitoring
    poll_interval_s: float = 1.0        # WebSocket monitor poll rate

    # API server
    host: str = "127.0.0.1"
    port: int = 8042

    snapshot_dir: str = "/var/cache/nvcurve/snapshots"
    profile_dir: str = "/etc/nvcurve/profiles"


# Module-level default config instance.
default_config = Config()
