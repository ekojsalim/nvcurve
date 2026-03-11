<div align="center">
  <img src="frontend/public/logo.svg" alt="NVCurve Logo" width="128" />
  <h1>NVCurve</h1>
  <p><strong>A native Linux NVIDIA GPU V/F Curve Editor & Overclocking Tool</strong></p>
</div>

---

NVCurve is a Linux-native GPU overclocking tool, providing MSI Afterburner-like per-point voltage-frequency curve control for NVIDIA GPUs. It uses undocumented NvAPI functions via `libnvidia-api.so` for exact hardware-level tuning.

It features both a robust Python CLI and a modern React web interface for interactive curve editing, profile management, and live hardware monitoring.

> **Note:** This tool relies on undocumented NvAPI features. While read operations are safe, write operations adjust GPU frequency offsets at a hardware level. Use at your own risk.

## Features

- **Per-Point Curve Editing**: Independently adjust the frequency offset for any point on the GPU's voltage-frequency curve.
- **Modern Web UI**: Interactive visual curve editor, point table, and real-time monitoring dashboard.
- **Live Monitoring**: Tracks GPU voltage, clock speed, temperature, and power draw using both NvAPI and NVML.
- **Profile Management**: Save and load clock settings.
- **CLI Support**: Full terminal interface for scripting and headless use.

## Prerequisites

- **OS**: Linux
- **GPU**: NVIDIA GPU (Tested on RTX 5090 Blackwell architecture)
- **Driver**: Proprietary NVIDIA drivers (Tested on 580.126.18)
- **Python**: 3.12+
- **Privileges**: NVCurve requires `root` access for hardware interactions. The CLI will automatically prompt for elevated privileges via `sudo` when needed.

## Installation

NVCurve is distributed as a Python tool and uses `uv` for dependency management.

```bash
# Install globally via uv tool
uv tool install nvcurve

# Alternatively, run directly from source
uv run -m nvcurve serve
```

*Note: For the web UI frontend development, use `pnpm install` and `pnpm run dev` in the `frontend` directory.*

## Usage

NVCurve gracefully handles privilege escalation. If you run a command as a standard user, it will automatically prompt for your `sudo` password to interface directly with the NVIDIA driver.

### Starting the Web Interface
To start the API server and serve the web UI:
```bash
nvcurve serve
```
Then navigate to `http://localhost:8042` in your browser.

### Command Line Interface
NVCurve offers a full-featured CLI:

```bash
# Read the current curve
nvcurve read --full

# Live terminal monitoring
nvcurve monitor

# Set a global +100 MHz offset
nvcurve write --global --delta 100

# Set a +50 MHz offset for a specific V/F point (e.g., point 80)
nvcurve write --point 80 --delta 50

# Reset the curve to default
nvcurve write --reset

# Save the current state to a profile
nvcurve profile save my_profile
```

## Architecture

NVCurve is split into two primary components:
1. **Python API Backend (`nvcurve/`)**: Interfaces directly with `libnvidia-api.so` (via ctypes) and `libnvidia-ml.so` to read/write hardware states. It exposes these mechanisms via a FastAPI REST+WebSocket server to provide privilege isolation.
2. **React Frontend (`frontend/`)**: A rich interactive GUI running in the browser, communicating with the root backend remotely. 

For full technical specifications surrounding the NvAPI buffer layouts and function endpoints utilized, please refer to the internal documentation.

## Disclaimer
These functions are undocumented and unsupported by NVIDIA. They may alter or break between driver versions. Write functions can alter GPU behavior and should be used with extreme caution. The authors accept no responsibility for hardware damage resulting from the use of this software.
