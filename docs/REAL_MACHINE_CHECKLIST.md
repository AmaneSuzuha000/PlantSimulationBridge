# Live Plant Simulation Verification Checklist

This checklist tracks COM and SimTalk compatibility verification against a live Windows installation of Siemens Tecnomatix Plant Simulation. Automated offline tests (`python -m pytest -q`) mock the COM boundary; live behavior is confirmed using the scripts under `scripts/` and opt-in real-COM tests (`PSBRIDGE_REAL_COM=1`).

## 0. Environment prerequisites

- Windows 10/11 with a licensed Tecnomatix Plant Simulation installation (verified on Plant Simulation 13).
- Python 3.10+ with `pywin32>=306` matching Plant Simulation's bitness (`python -m pip install -e .[windows,dev]`).
- `PSBRIDGE_TEMPLATE` set to an existing `.spp` model file, for example:

```powershell
$env:PSBRIDGE_TEMPLATE = "C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"
```

## 1. COM lifecycle and STA safety

- `Dispatch("Tecnomatix.PlantSimulation.RemoteControl")` launches an isolated hidden instance; `Quit()` shuts down backend-owned instances cleanly without lingering processes.
- `SetNoMessageBox(True)` is called immediately on connect so SimTalk syntax or runtime errors surface as COM exceptions rather than modal popups.
- `LoadModel` requires the `.spp` suffix; `NewModel()` produces an unaddressable frame on Plant Simulation 13, so live workflows open and clone an existing template file.
- `ComExecutor` executes all COM calls on a single STA thread and enforces exclusive operation scopes.

## 2. Modeling, attributes, and read-back contracts

Verified via `python scripts/verify_contract.py` (57 checks):

- Every class in `validation.DEVICE_CLASS_ALLOWLIST` is creatable via `<class>.createObject(<frame>, x, y)` and discoverable via `NumNodes` / `node(i)`.
- Non-existent class paths (`.MaterialFlow.Station`, `.MaterialFlow.Conveyor`, `.Models.Method`, `.Resources.Worker`, etc.) raise errors instead of silently succeeding.
- Every writable attribute in `validation.CLASS_ATTRIBUTE_SPECS` writes cleanly and reads back via `set_value(..., verify=True)`.
- Time attributes (`procTime`, `Interval`, `Start`, `Stop`, `End`) accept numeric seconds via SimTalk (`SetValue` rejects time strings).
- Connections use `.MaterialFlow.Connector.connect(source, target)` and verify `NumPred` increase after linking.
- Information-flow objects (`&Method`, `&TableFile`, `&Variable`) are addressed via `<frame>.node(k)` handles; multi-line `Method` source is written via `Method.load(...)` and verified byte-for-byte through `<handle>.Program`.

## 3. Simulation control and asynchronous timing

- `EventController.StartStat` defaults to 3600 s in new controllers; `start_simulation` and `run_simulation` reset `statistics_start=0.0` by default unless `None` is passed.
- `EventController.start` is asynchronous and takes ~20–40 ms before `isRunning` flips to `True`; `wait_until_finished()` and `SafeOps.run_simulation()` apply a bounded `startup_grace` window so initial `False` polls do not return prematurely.
- `Win32ComBackend.status()` queries `isRunning` before `simTime` to prevent torn reads when a run finishes mid-poll.

## 4. End-to-end live verification commands

Run all live verification checks against `PSBRIDGE_TEMPLATE`:

```powershell
$env:PSBRIDGE_REAL_COM = "1"
$env:PSBRIDGE_TEMPLATE = "C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"

python -m pytest -q
python scripts/verify_contract.py
python scripts/all_tools_smoke.py
python scripts/real_machine_dispatch_smoke.py
python scripts/real_machine_jobshop_smoke.py
python scripts/mcp_stdio_smoke.py
```

## 5. Table-driven dispatching & heterogeneous job-shop parity

Verified live on Siemens Plant Simulation 13 (`tests/test_simtalk_templates.py` under `PSBRIDGE_REAL_COM=1` and `scripts/real_machine_smoke.py --execute`):

- **Model & table generation (`psbridge.simtalk.templates`)**: Derives `.MUs.PsEntity` (`JobID`, `Step`, `OpDuration`), creates `DT_<job>` `Delivery Table`s (1-based `time`/`object`/`integer`/`string` columns) for exact `t >= 0` release timing, formats 1-based `[col, row]` `TableFile`s (`ColumnIndex := true`, `cutRow`/`cutColumn` without `setDimensions`), creates `SingleProc`/`ParallelProc` stations, and loads SimTalk 1.0 `Dispatch` + `OnSourceExit` / `OnStationEnter` / `OnStationExit` methods via `Method.load`.
- **Policy parity**: Live runs of the 5x3 demo instance under `fifo`, `spt`, and `edd` (plus mixed `due_date=None` instances and `capacity > 1` `ParallelProc` stations) match `reference_schedule` bit-for-bit (`makespan=24.0` for `fifo`/`spt`, `29.0` for `edd`) while keeping the template `.spp` SHA-256 unchanged.
- **Heterogeneous job-shop durations (`psbridge.simtalk.jobshop`)**: Multi-duration machines stamp `@.OpDuration` during routing and set `?.ProcTime := @.OpDuration` via `EntranceCtrlBeforeActions := true`, verified live alongside uniform-duration crossover lines.
