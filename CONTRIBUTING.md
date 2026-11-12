# Contributing

Issues and pull requests are welcome.

## Development setup

```powershell
git clone https://github.com/AmaneSuzuha000/PlantSimulationBridge.git
cd PlantSimulationBridge
python -m pip install -e .[windows,dev]
```

On macOS or Linux, omit `windows` and run the offline suite:

```bash
python -m pip install -e .[dev]
```

## Running checks

```bash
python -m ruff check .
python -m pytest -q
```

The offline test suite exercises schemas, validation, `FakeBackend`, `FakeDispatchEngine`, oracle cross-checking, `SafeOps`, COM STA executor isolation, SimTalk code generators, and the MCP server without requiring Plant Simulation or `pywin32`.

### Live Plant Simulation verification (Windows)

When making changes under `psbridge/com/win32.py`, `psbridge/core/`, `psbridge/simtalk/dispatch.py`, or `psbridge/simtalk/jobshop.py`, validate against a real Plant Simulation installation using an isolated copy of a template `.spp` model:

```powershell
$env:PSBRIDGE_REAL_COM = "1"
$env:PSBRIDGE_TEMPLATE = "<PathToPlantSimulation>\Examples\Demo\DeliveryConcept.spp"
python -m pytest -q
python scripts/verify_contract.py
python scripts/all_tools_smoke.py
python scripts/real_machine_dispatch_smoke.py
python scripts/real_machine_jobshop_smoke.py
```

Include your Plant Simulation version and observed outputs in your pull request description.

## Style guidelines

- Python 3.10+, standard library first. Keep `pythoncom` and `win32com` imports lazy inside COM connection paths so offline workflows stay cross-platform.
- Write all documentation, docstrings, comments, and error messages in English.
- Preserve safety invariants: never overwrite uncloned `.spp` files, never silently swallow mismatches or unsupported attributes, and always maintain the `NOT_VERIFIED` markers in `psbridge.simtalk.templates` until live-verified.
