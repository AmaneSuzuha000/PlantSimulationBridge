# Security policy

## Scope

`PlantSimulationBridge` drives a locally installed, licensed copy of Siemens Tecnomatix Plant Simulation over its Windows COM automation interface (`Tecnomatix.PlantSimulation.RemoteControl`), and runs offline scheduling simulations locally on disk.

It does **not**:

- make outbound network connections,
- store or transmit credentials,
- expose raw `ExecuteSimTalk` directly through MCP tools.

## Built-in safety controls

- **Directory confinement (`model_root`)**: live model sessions require `model_root` and reject paths outside that directory (`PathNotAllowed`).
- **Original model protection (`protect_original`)**: `SafeOps` records the SHA-256 hash of a loaded `.spp` file and refuses mutations or in-place saves until the model is cloned into an experiment work directory.
- **Validation before COM execution**: object classes and attribute names must pass allow-lists (`psbridge.core.validation`), control characters are rejected before quoting SimTalk strings, and destructive deletes require explicit `confirm=true`.
- **No modal hangs or auto-save**: `SetNoMessageBox(True)` is called on connect, and `auto_save_on_disconnect` defaults to `False`.

## Reporting

Please open a private security advisory on GitHub (`Security -> Advisories`) rather than a public issue for anything security-sensitive, including reproduction steps and your Plant Simulation / Python versions.
