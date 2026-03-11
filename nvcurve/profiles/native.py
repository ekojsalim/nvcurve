"""Native profile storage and schema."""

import json
import os
import glob
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List


@dataclass
class ProfileData:
    name: str
    gpu_name: str
    curve_deltas: Dict[str, int]  # { "index": delta_khz }
    mem_offset_mhz: Optional[int] = None
    power_limit_w: Optional[int] = None


def save_profile(profile_dir: str, data: ProfileData) -> str:
    """Save profile to JSON, sanitising the filename."""
    os.makedirs(profile_dir, exist_ok=True)
    safe_name = "".join(c for c in data.name if c.isalnum() or c in " _-()").strip()
    if not safe_name:
        safe_name = "Unnamed"
        
    filepath = os.path.join(profile_dir, f"{safe_name}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(asdict(data), f, indent=2)
    return filepath


def load_profile(filepath: str) -> ProfileData:
    """Load profile from JSON."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Migrate old field names.
    if "vram_p0_offset_mhz" in data and "mem_offset_mhz" not in data:
        data["mem_offset_mhz"] = data.pop("vram_p0_offset_mhz")
    # Drop removed fields so old profiles don't cause TypeError.
    for obsolete in ("gpu_locked_min_mhz", "gpu_locked_max_mhz", "vram_p0_offset_mhz"):
        data.pop(obsolete, None)
    return ProfileData(**data)


def list_profiles(profile_dir: str) -> List[ProfileData]:
    """Return a list of all safely readable profiles."""
    if not os.path.exists(profile_dir):
        return []
    profiles = []
    for fp in glob.glob(os.path.join(profile_dir, "*.json")):
        try:
            profiles.append(load_profile(fp))
        except Exception as e:
            # log warning ideally, but swallowing for robustness
            pass
    # Sort alphabetically by name
    profiles.sort(key=lambda p: p.name.lower())
    return profiles


def rename_profile(profile_dir: str, old_name: str, new_name: str) -> bool:
    """Rename a profile: update the name field and move the file."""
    old_safe = "".join(c for c in old_name if c.isalnum() or c in " _-()").strip()
    new_safe = "".join(c for c in new_name if c.isalnum() or c in " _-()").strip()
    if not new_safe:
        return False
    old_path = os.path.join(profile_dir, f"{old_safe}.json")
    new_path = os.path.join(profile_dir, f"{new_safe}.json")
    if not os.path.exists(old_path):
        return False
    try:
        profile = load_profile(old_path)
        profile.name = new_name
        with open(new_path, "w", encoding="utf-8") as f:
            json.dump(asdict(profile), f, indent=2)
        if old_path != new_path:
            os.remove(old_path)
        return True
    except Exception:
        return False


def delete_profile(profile_dir: str, name: str) -> bool:
    """Delete a profile by name."""
    safe_name = "".join(c for c in name if c.isalnum() or c in " _-()").strip()
    filepath = os.path.join(profile_dir, f"{safe_name}.json")
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            return True
        except Exception:
            return False
    return False
