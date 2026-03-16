# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] - 2026-03-16

### Added
- **Automated Setup Check**: Introduced the `nvcurve setup` command that performs a consolidated 4-step hardware compatibility check (NvAPI diagnostics, V/F curve baseline read, non-destructive write-verify test on point 80, and automatic state restore).
- **Auto-Load Profile Support**: Added the ability to apply a specific profile automatically on server startup.
  - CLI usage: `--auto-load-profile <name>` when starting the server or installing the systemd service.
  - Configuration safely persists via `/etc/nvcurve/config.json`.
  - The web UI now features a new Timer badge and inline toggle for auto-load configuration directly within the Profile Panel.
- **Curve Flattening (Anchor Points)**: Introduced anchor point tracking for multi-point operations.
  - The last explicitly Shift/Ctrl-clicked point is now visually highlighted with an amber halo on the curve graph.
  - Selecting two or more points displays a new "Flatten to [anchor]" toolbar action to instantly align all selected points to the anchor point's frequency offset.

### Changed
- **Improved Diagnostics**: The internal diagnostic engine now provides much deeper hardware state analysis, including active driver version, VRAM totals, power limits, raw clock offsets, memory ranges, and raw boost masks.
- **Offline Snapshots**: The CLI commands for managing snapshots (`nvcurve snapshot save`, `restore`, `list`) now intelligently bypass the server and fall back to direct HAL operations if the background daemon is not currently running.
