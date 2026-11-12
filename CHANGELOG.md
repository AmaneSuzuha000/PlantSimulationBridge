# Changelog

All notable changes to this project will be documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-12

Initial public release of **PlantSimulationBridge (`psbridge`)**.

### Added

- **COM gateway & Win32 backend (`psbridge.com`)**:
  - Dedicated single-STA `ComExecutor` with paired `CoInitialize`/`CoUninitialize` and exclusive `OperationScope` locking.
  - `Win32ComBackend` supporting `Dispatch`, model-root auto-discovery (`.Models.<name>.Frame`), `<class>.createObject`, `NumNodes`/`node(i)` enumeration, `.MaterialFlow.Connector.connect`, `&Method` handle capture + `Method.load` multiline round-tripping, `TableFile` string-safe cell writes, and `EventController` lifecycle control.
  - `FakeBackend` with deterministic state machine and fault injection for cross-platform testing.
- **Safety & validation layer (`psbridge.core`)**:
  - Strict identifier, dotted path, SimTalk source, class allow-list, and per-class attribute specification validators.
  - `SafeOps` with `model_root` confinement, `protect_original` SHA-256 clone verification, mandatory property/table/method read-back checks, torn-read-free `status()`, and async post-`start` `startup_grace` synchronization.
- **Scheduling, offline experiments & RL environment (`psbridge.experiment`, `psbridge.scheduling`)**:
  - `DispatchEngine` protocol, `FakeDispatchEngine`, independent `reference_schedule` oracle, `FIFO`/`SPT`/`EDD` policies, time-weighted exact average WIP metrics, schedule validity gates, crash-tolerant `ResultStore` (JSONL), and `SchedulingEnv`.
- **SimTalk generators & tooling (`psbridge.simtalk`, `psbridge.apiquery`, `tools/`)**:
  - Live-verified scheduled source, exit-control routing (`dispatch.py`), uniform and heterogeneous buffered job-shop (`jobshop.py`), and full table-driven `FIFO`/`SPT`/`EDD` + `ParallelProc` template generators (`templates.py`) against Plant Simulation 13.
  - Plant Simulation HTML help indexer (`tools/pshelp_index.py`), COM attribute prober (`tools/api_surface.py`), and `ApiIndex` query CLI.
- **MCP server & 30 structured tools (`psbridge.tools`, `psbridge.mcp`)**:
  - 9 offline experiment/SimTalk tools and 21 live model/simulation/method tools served over stdio JSON-RPC 2.0 with automatic COM session teardown on exit.
