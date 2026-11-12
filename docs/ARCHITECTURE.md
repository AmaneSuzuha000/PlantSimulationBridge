# Architecture

This document describes the design of `PlantSimulationBridge` (`psbridge`) and the safety, concurrency, and reproducibility guarantees enforced across its layers.

## 1. Layering and dependency direction

```text
Clients (MCP JSON-RPC stdio / Python scripts / CLI)
    │
    ▼
psbridge.tools           Structured tool boundary (JSON in/out; never raises)
    │
    ├─► psbridge.experiment    Instances, batch runner, metrics, validity, JSONL store
    │       ▲
    │       └─ psbridge.scheduling   Policies, independent reference oracle, RL env
    │
    └─► psbridge.core          Input validation, read-back verification, clone protection
            │
            ▼
        psbridge.com           Single-STA ComExecutor + SimBackend protocol (Win32 / Fake)
            │
            ▼
        Siemens Tecnomatix Plant Simulation (COM RemoteControl + SimTalk)
```

Dependencies strictly point downward:
- `psbridge.experiment` never imports COM or `pywin32`.
- `psbridge.scheduling` depends only on schemas and the `DispatchEngine` contract.
- `psbridge.scheduling.reference` (the reference oracle) shares no scheduling code with `psbridge.experiment.fake_engine`.

## 2. Offline experiment engine & oracle cross-check

Experimental metrics and validity gates must remain fast and testable without launching Plant Simulation on every change. `psbridge.experiment.engine` defines the `DispatchEngine` protocol (`reset`, `next_decision`, `apply`, `is_done`, `current_state`, `schedule`, `state_hash`, `event_log`) and behavioral specification `S1`–`S9`:

1. **`FakeDispatchEngine`** implements `S1`–`S9` as an event-driven discrete-event engine.
2. **`reference_schedule`** implements the exact same rules independently via iterative `(time, machine_id)` scanning without sharing code with `FakeDispatchEngine`.

`tests/test_crosscheck.py` verifies across randomized instances that both implementations emit identical `(op_id, machine_id, start, end)` schedules. Every `RunRecord` records `status` (`valid`, `invalid`, or `failed`), refuses to mark invalid/failed runs as valid, and appends crash-tolerant records (`fsync`) through `ResultStore`.

## 3. Clone-first model protection and read-back verification

Production `.spp` simulation models must never be silently corrupted or saved over during automated experiments:

- **`BridgeConfig.auto_save_on_disconnect`** defaults to `False`; `disconnect()` never writes to disk.
- **`SafeOps`** (`protect_original=True` by default in library workflows) hashes the original `.spp` file before loading (`ModelFingerprint`) and blocks in-place mutations (`ModelMutationDenied`) until `clone_model_for_experiment()` creates and activates an isolated copy and confirms the original SHA-256 hash did not change.
- **Explicit delete opt-in**: `delete_device` requires literal `confirm=True`, verifies post-delete absence, and blocks deleting `.`, `.Models`, or the active frame.
- **Non-destructive creation**: `create_device(..., overwrite=False)` raises `ObjectExists` on collision rather than replacing an existing object.
- **Double verification on writes**: `set_property` checks per-class attribute applicability (`PropertyNotApplicable`), dispatches the typed write (SimTalk numeric seconds for `time` attributes, because COM `SetValue` rejects time strings), and reads back the value to verify convergence (`VerificationFailed`).

## 4. Single-STA COM gateway & Plant Simulation semantics

Plant Simulation's COM automation server has strict Single-Threaded Apartment (STA) affinity:

- **`ComExecutor`** runs one dedicated worker thread initialized with paired `CoInitialize()` / `CoUninitialize()`.
- **`OperationScope`** serializes multi-call operations (`clone`, `create_device`, `set_property`, `method_create`, `reset`/`start`) so concurrent callers cannot interleave.
- **Lazy `pywin32` import**: `psbridge.com.win32` imports `pythoncom` / `win32com` only inside `connect()`, keeping all offline layers importable on Linux, macOS, and Windows without `pywin32`.

Measured Plant Simulation 13 COM/SimTalk rules built into `Win32ComBackend` and `psbridge.simtalk`:

| Area | Implemented behavior |
|---|---|
| Modal suppression | `SetNoMessageBox(True)` is called unconditionally right after acquiring the COM app so SimTalk errors return exceptions instead of stalling the STA thread with UI dialogs |
| Root frame discovery | Discovered from `.Models.node(i).Name` inside the loaded model rather than guessed from the `.spp` filename |
| Object creation & listing | Uses `<class>.createObject(<frame>, x, y)` (class as receiver) and enumerates via `NumNodes` / `node(i)` (`NumChildren` stays `0`) |
| Info-flow handles | Methods/tables live under `.InformationFlow.&Method` / `&TableFile`, format columns via `[col, row]` (`ColumnIndex := true`, `cutRow`/`cutColumn`), and resolve via captured `<frame>.node(k)` handles or `str_to_obj(...)` |
| Clone lifecycle | `clone_model()` saves the modified copy, closes, and reloads it so COM `model_path` and disk state match |
| Table-driven dispatching | `psbridge.simtalk.templates` and `psbridge.simtalk.jobshop` support per-operation `ProcTime` assignment (`Dispatch` `stn.ProcTime := bestDuration;` for `SingleProc`/`ParallelProc`, or `@.OpDuration` via `EntranceCtrlBeforeActions`) and `Delivery Table` release schedules |
| Structured reads & writes | Scalar reads go directly through `GetValue(<expr>)`; `TableFile` cells store strings (formatted via `to_str(...)`); `Method` bodies load cleanly byte-for-byte through temp-file `Method.load(...)` |
| Simulation timing & stats | `EventController.StartStat` is reset to `0` by default; `status()` reads `isRunning` before `simTime` to prevent torn state pairs; `run_simulation()` and `wait_until_finished()` enforce a startup grace window (`startup_grace`) past the ~20–40 ms post-`start` transition |
| Instance cleanup | `disconnect()` calls `Quit()` only on COM instances started by the backend (`owns_instance=True`) and leaves external instances untouched |

## 5. MCP tool & transport split

`psbridge.tools` defines all 30 tool schemas, argument validators, and pure handlers as ordinary Python functions. `psbridge.mcp.server` wraps them cleanly in newline-delimited JSON-RPC 2.0 (`stdio`), isolating the protocol transport from tool logic and automatically tearing down any active COM session in its shutdown `finally` block.
