# Third-party notices

This repository is distributed under the MIT License (see [`LICENSE`](LICENSE)). All code in `psbridge/`, `scripts/`, `tools/`, and `tests/` is an independent clean-room implementation.

## Runtime dependencies (not distributed here)

- **Siemens Tecnomatix Plant Simulation**: commercial discrete-event simulation software accessed locally on Windows through its COM RemoteControl automation interface. No Siemens binaries, models, or help files are distributed in this repository.
- **pywin32** (Python Software Foundation License): optional Windows COM runtime dependency (`pip install -e .[windows]`).
