# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] - 2026-03-17

### Added
- **Multi-GPU Support** *(experimental — untested on real multi-GPU hardware)*: The server now manages all detected NVIDIA GPUs simultaneously under a single process. Each GPU gets its own isolated state (write lock, monitor clients, curve clients, active profile). REST endpoints and WebSocket subscriptions accept a `gpu_index` parameter. A new `/api/gpus` endpoint enumerates all GPUs with name, index, UUID, and PCI bus ID.
- **GPU Selector in Web UI**: When multiple GPUs are present, the status bar shows a dropdown to switch the active GPU. Switching resets all pending edits, selection state, and live monitoring for the new target.
- **Default Profile**: Added the ability to designate a profile as the default — it is applied automatically on server startup.
  - CLI: `nvcurve profile default <name>` to set, `nvcurve profile default --clear` to unset.
  - The web UI shows a filled star on the default profile and lets you toggle it with a single click.
  - The setting persists to `/etc/nvcurve/config.json` (created by `service install`). If the config file is absent, the setting is in-memory for the current session only.
- **Curve Flattening**: Selecting two or more points and clicking "Flatten to [anchor]" in the toolbar sets each selected point to a different offset, such that all land on the same effective frequency as the anchor point. The anchor is the last explicitly clicked point (highlighted with an amber halo on the graph); bulk selections (box, range, Ctrl+A) preserve the existing anchor.
- **Server-Optional Profile Commands**: `profile apply`, `profile list`, and `profile default` no longer require the server to be running. When the server is absent they fall back to direct HAL calls or config-file writes, escalating to root via `sudo` automatically — the same pattern already used by snapshot commands.
- **Automated Setup Check**: `nvcurve setup` runs a consolidated 4-step hardware compatibility check: NvAPI function probe → V/F curve baseline read → non-destructive write-verify → automatic state restore. The write-verify defaults to the last GPU-domain point (safe on all GPU generations); override with `--point` and `--delta`. Pass `--full-mask` if writes fail on older GPUs such as Pascal.

### Changed
- **Profile CLI syntax**: Profile commands now take the profile name as a positional argument instead of `--name` (e.g. `nvcurve profile apply balanced` instead of `nvcurve profile apply --name balanced`).
- **Improved Diagnostics**: The `read --diag` engine now reports driver version, VRAM totals, power limits, raw clock offsets, memory offset ranges, and raw boost masks in addition to the NvAPI function probe.
- **Offline Snapshots**: `snapshot save`, `restore`, and `list` bypass the server and fall back to direct HAL operations when the daemon is not running.
