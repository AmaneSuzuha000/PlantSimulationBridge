# PlantSimulationBridge

Control Siemens Tecnomatix Plant Simulation from Python and MCP, run reproducible dispatching experiments offline with independent oracle cross-checking, and drive live Plant Simulation COM models through a single-STA safety gateway.

```text
instance.json ─► psbridge (offline) ──► validate_instance / compare_policies / reproduce_run (FakeDispatchEngine + Oracle)
                                    ──► generate_simtalk (SimTalk templates) / demo

             ┌─► psbridge.mcp (stdio) ──► ComExecutor (single STA thread + op lock)
             │
             └─► SafeOps ───────────────► Win32ComBackend ──► Tecnomatix.PlantSimulation.RemoteControl
                    │                       ├─► model discovery (.Models.<name>)
                    │                       ├─► clone-first SHA-256 original protection
                    │                       ├─► property / table / method read-back verification
                    │                       └─► EventController reset / start / isRunning polling
```

## Why

Plant Simulation exposes its automation interface (`Tecnomatix.PlantSimulation.RemoteControl`) as an out-of-process COM server with strict Single-Threaded Apartment (STA) affinity and asymmetric SimTalk conventions:

- Creating objects requires class-receiver syntax (`<class>.createObject(<frame>, x, y)`), and enumeration uses `NumNodes` / `node(i)` rather than `NumChildren`.
- Model roots are discovered from inside the loaded `.spp` file (`.Models.<ModelName>` via `.Models.node(i).Name`), not guessed from the filename.
- Information-flow classes require the `&` prefix (`.InformationFlow.&Method`, `&TableFile`), multi-line methods load via `Method.load`, links are created via `.MaterialFlow.Connector.connect(a, b)`, `for` loops close with `next`, and `EventController.start` is asynchronous with a startup transition window and a 1-hour default `StartStat` reset.

**PlantSimulationBridge (`psbridge`)** encodes these verified conventions behind typed validation allow-lists, read-back verification, clone-first model protection, and a dedicated STA COM executor — alongside an offline discrete-event scheduling layer so policies, metrics, and experiment pipelines can be tested cross-platform without a live Plant Simulation installation.

## Features

- **30 MCP tools over stdio JSON-RPC 2.0**: Run `python -m psbridge.mcp` to expose 9 offline experiment/SimTalk tools and 21 live model/simulation/method tools with strict JSON Schema validation and non-throwing structured error envelopes.
- **Single-STA COM gateway**: `ComExecutor` marshals every COM call onto one dedicated STA worker thread (`CoInitialize` / `CoUninitialize` paired) with operation-level locking so multi-step read-modify-verify sequences never interleave.
- **Safety boundaries**:
  - `model_root` directory confinement on model paths.
  - `protect_original` + SHA-256 cloning before mutating research `.spp` files, with `auto_save_on_disconnect=False`.
  - Allow-listed classes and per-class attributes with mandatory read-back verification (`VerificationFailed` on mismatch).
  - Unconditional `SetNoMessageBox(True)` on connect to prevent modal COM stalls, and automatic session cleanup (`Quit()` for backend-owned instances) on exit.
- **Verified SimTalk dispatch, job-shop & table-driven generators**: `psbridge.simtalk.dispatch`, `psbridge.simtalk.jobshop` (supporting both uniform and heterogeneous per-operation durations via `@.OpDuration`), and `psbridge.simtalk.templates` (`FIFO`/`SPT`/`EDD` table-driven routing, `Delivery Table` releases, `SingleProc`/`ParallelProc` support, verified bit-for-bit against the reference oracle on Plant Simulation 13).
- **Offline scheduling experiments & RL environment**: `FakeDispatchEngine`, `FIFO`/`SPT`/`EDD` policies, a completely independent reference oracle (`tests/test_crosscheck.py`), exact time-weighted WIP and tardiness metrics, `SchedulingEnv` (`reset` / `step` / `action_mask`), and crash-tolerant JSONL result storage.
- **Help index & COM surface tooling**: `tools/pshelp_index.py`, `tools/api_surface.py`, and `psbridge.apiquery` build searchable JSON inventories from local Plant Simulation HTML help files and inspect live COM readability.

## Requirements

- **Cross-platform offline mode** (experiments, validation, oracle cross-check, SimTalk generation, unit suite):
  - Python 3.10+ (standard library only for runtime; `pytest` for tests)
- **Live Plant Simulation COM mode**:
  - Windows 10/11
  - Licensed Siemens Tecnomatix Plant Simulation (verified against Plant Simulation 13)
  - `pywin32>=306` matching your Plant Simulation COM architecture

## Install

```powershell
git clone https://github.com/AmaneSuzuha000/PlantSimulationBridge.git
cd PlantSimulationBridge

# Offline experiment & MCP layer
python -m pip install -e .

# Full install with pywin32 (Windows COM) and dev tooling
python -m pip install -e .[windows,dev]
```

To point live smoke/verification scripts at your local template `.spp` model, set `PSBRIDGE_TEMPLATE`:

```powershell
$env:PSBRIDGE_TEMPLATE = "C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"
```

## Quick start

### 1. Offline scheduling experiment comparison

```bash
python -m psbridge.demo
python -m psbridge.demo --json
```

Compare `FIFO`, `SPT`, and `EDD` across multiple random seeds with jitter, verify every schedule against the independent reference oracle, and persist reproducible JSONL records under `.psbridge-work/`.

### 2. MCP stdio server

Launch the server directly or configure your MCP client as shown in `mcp.json.example`:

```bash
python -m psbridge.mcp
```

Smoke-test the subprocess JSON-RPC transport:

```bash
python scripts/mcp_stdio_smoke.py
```

### 3. Live Plant Simulation verification (Windows)

```powershell
$env:PSBRIDGE_TEMPLATE = "<PathToPlantSimulation>\Examples\Demo\DeliveryConcept.spp"

python scripts/verify_contract.py
python scripts/all_tools_smoke.py
python scripts/real_machine_dispatch_smoke.py
python scripts/real_machine_jobshop_smoke.py
python scripts/real_machine_smoke.py --instance .psbridge-work/demo_instance.json --policy fifo --model-root "<PathToPlantSimulation>\Examples\Demo" --model-name DeliveryConcept.spp --execute --confirm-real-machine
```

## MCP tools

Every tool returns a JSON object with `"ok": true | false` and a typed `"error"` payload (`code`, `message`, `details`) on failure.

### Experiment & SimTalk tools (no Plant Simulation required)

| Tool | Purpose |
|---|---|
| `bridge_info` | Bridge version, available policies/backends, and verification flags |
| `validate_instance` | Validate scheduling instance schema, structure, and engine compatibility |
| `compare_policies` | Run policies across seeds, cross-check against oracle, and summarize metrics |
| `run_experiment` | Run one policy over seeds and append records to the JSONL result store |
| `reproduce_run` | Repeat a seeded run multiple times and verify bit-identical schedules/metrics |
| `generate_simtalk` | Render live-verified model, table, dispatch, control, and result SimTalk templates |
| `schedule_vs_reference` | Diff `FakeDispatchEngine` output directly against the independent reference oracle |
| `list_runs` | List stored experiment records from the JSONL result store |
| `experiment_summary` | Aggregate counts, validity statistics, and metric summaries across runs |

### Model, simulation, method & counter tools (live COM via `SafeOps`)

| Tool | Purpose |
|---|---|
| `model_status` | Report COM session state and currently loaded `.spp` path |
| `model_open` | Load a `.spp` model inside `model_root` and record its SHA-256 fingerprint |
| `model_list_objects` | Enumerate objects in a frame using `NumNodes` / `node(i)` and real class paths |
| `model_object_info` | Inspect one object's name and class path |
| `model_create_device` | Derive a device from the allow-list without overwriting existing names |
| `model_delete_device` | Delete an object (requires `confirm=true`; refuses frames and root paths) |
| `model_connect_objects` | Connect two objects via `.MaterialFlow.Connector.connect` and verify `NumPred` |
| `model_get_property` | Read a validated object attribute |
| `model_set_property` | Validate attribute applicability, write safely (seconds for time types), and read back |
| `model_read_counters` | Read `StatNumIn`, `StatNumOut`, and `StatWorkingTime` for named objects |
| `simulation_status` | Read `isRunning` (first, to avoid torn reads), `simTime`, `End`, and derived state |
| `simulation_start` | Set `StartStat` (default `0`), optional `End`, and trigger async `EventController.start` |
| `simulation_stop` | Stop the running controller and report status |
| `simulation_reset` | Reset the EventController without altering model structure |
| `simulation_run` | Start and block until stopped, honoring startup grace window (`startup_grace`) |
| `simulation_set_source` | Configure a `Source` (`MuSelection`, `Path`, `TimeOfGeneration`, `Number`, `Interval`, `Start`) |
| `method_create` | Create an information-flow `&Method` object, populate its source, and return its `node(k)` handle |
| `method_set_source` | Replace a Method's SimTalk source (via `Method.load` for multiline) with read-back |
| `method_get_source` | Read back a Method's `Program` source |
| `object_set_control` | Attach a Method handle to `EntranceCtrl`, `ExitCtrl`, or `InitCtrl` with read-back |
| `mu_create_attribute` | Declare a custom typed attribute on `.MUs.Entity` |

## Project layout

```text
PlantSimulationBridge/
  psbridge/
    schema.py             Instance / schedule data contracts and strict validation
    errors.py             Typed error hierarchy and stable error codes
    config.py             Runtime configuration (paths, timeouts, safety flags)
    tools.py              30 MCP-ready tool handlers + serialized COM session manager
    demo.py               Offline FIFO / SPT / EDD demonstration CLI
    apiquery.py           Queryable API inventory loader and CLI
    com/
      backend.py          SimBackend protocol and value types
      executor.py         Dedicated single-STA COM worker + OperationScope lock
      win32.py            Win32 COM backend + verified SimTalk snippet builders
      fake.py             Deterministic in-memory FakeBackend with fault injection
    core/
      ops.py              SafeOps verification, clone protection, and lifecycle layer
      validation.py       Identifier, path, class/attribute allow-lists, SimTalk quoting
    experiment/
      engine.py           DispatchEngine protocol and S1-S9 scheduling specification
      fake_engine.py      Discrete-event simulation engine
      metrics.py          Makespan, flow time, tardiness, and exact integral avg WIP
      validity.py         Schedule feasibility and oracle agreement gates
      runner.py           Reproducible seeded batch runner
      store.py            Append-only fsynced JSONL result store
    scheduling/
      policies.py         FIFO, SPT, EDD dispatching rules
      reference.py        Independent reference schedule oracle
      env.py              RL-compatible reset / step / action_mask environment
    simtalk/
      dispatch.py         Live-verified scheduled source and exit-control routing builders
      jobshop.py          Live-verified buffered multi-stage job-shop generator
      model_spec.py       Table-driven model specification types
      templates.py        Live-verified table-driven SimTalk dispatch & control generators
    mcp/
      server.py           stdio JSON-RPC 2.0 MCP server
  scripts/                Contract checks and live smoke harnesses
  tools/                  Help HTML indexer and live COM attribute prober
  docs/                   Architecture notes and live verification checklist
  tests/                  Comprehensive offline suite + opt-in real COM suite
```

## Tests

```bash
# Lint and offline unit/property/cross-check suite (works on Linux & Windows)
python -m ruff check .
python -m pytest -q
```

To include the real Plant Simulation COM test case on Windows (uses a temporary copy of `PSBRIDGE_TEMPLATE`):

```powershell
$env:PSBRIDGE_REAL_COM = "1"
$env:PSBRIDGE_TEMPLATE = "<PathToPlantSimulation>\Examples\Demo\DeliveryConcept.spp"
python -m pytest -q
```

## Known scope and limitations

- **Verified COM & SimTalk scope**: All COM lifecycle calls (`Dispatch`, `LoadModel`, `SaveModel`, `CloseModel`, `NewModel`, `Quit`, `GetValue`, `SetValue`, `ExecuteSimTalk`, `SetNoMessageBox`), dynamic model/controller discovery, `.MaterialFlow.Connector.connect`, `Method.load`, `TableFile` `[col, row]` cell round-trips, `psbridge.simtalk.dispatch`, `psbridge.simtalk.jobshop` (uniform & heterogeneous per-operation durations), and `psbridge.simtalk.templates` (`FIFO`/`SPT`/`EDD` exact schedule parity + `ParallelProc`) are verified against Siemens Tecnomatix Plant Simulation 13.
- **`NewModel()` addressing**: On Plant Simulation 13, a freshly created `NewModel()` frame is not addressable by `.Models` COM paths even after `SaveModel()`. Open and clone an existing template `.spp` file instead.
- **Offline dispatching engine scope**: `FakeDispatchEngine` and `reference_schedule` target deterministic single-capacity machines without flexible multi-candidate routing (`EngineUnsupported` is raised cleanly if passed capacity > 1 or multi-candidate operations offline, while `psbridge.simtalk.templates` maps `capacity > 1` machines to `.MaterialFlow.ParallelProc`).
- **COM session isolation**: `GetActiveObject` does not attach to a user's GUI Plant Simulation session on typical Windows setups; `Dispatch` starts a dedicated hidden instance owned and cleaned up by `Win32ComBackend`.

## License

MIT — see [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
