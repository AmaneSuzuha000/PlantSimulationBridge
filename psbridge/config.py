"""Runtime configuration for the bridge.

The defaults are chosen for research safety:

* auto_save_on_disconnect is False - closing the bridge never silently
  overwrites the .spp model on disk.
* allow_model_write is True but the experiment runner only ever writes to
  an isolated clone; the original model path is recorded as read-only.
* timeouts are mandatory; there is no "wait forever" mode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from psbridge.errors import ConfigError

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_OPERATION_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class BridgeConfig:
    """Validated, immutable bridge configuration."""

    work_dir: Path = Path(".psbridge-work")
    model_root: Path | None = None
    com_timeout_s: float = DEFAULT_TIMEOUT_S
    operation_timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S
    allow_model_write: bool = True
    auto_save_on_disconnect: bool = False
    backend: str = "fake"
    max_table_read_cells: int = 100_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_dir", Path(self.work_dir))
        if self.model_root is not None:
            object.__setattr__(self, "model_root", Path(self.model_root))
        if self.backend not in {"fake", "win32"}:
            raise ConfigError("backend must be 'fake' or 'win32'", details={"backend": self.backend})
        for name, value in (
            ("com_timeout_s", self.com_timeout_s),
            ("operation_timeout_s", self.operation_timeout_s),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ConfigError(name + " must be a positive finite number", details={name: value})
        if self.operation_timeout_s < self.com_timeout_s:
            raise ConfigError(
                "operation_timeout_s must be >= com_timeout_s",
                details={"com_timeout_s": self.com_timeout_s, "operation_timeout_s": self.operation_timeout_s},
            )
        if isinstance(self.max_table_read_cells, bool) or not isinstance(self.max_table_read_cells, int) or self.max_table_read_cells <= 0:
            raise ConfigError("max_table_read_cells must be a positive integer")
        if self.backend == "win32" and self.model_root is None:
            raise ConfigError("backend 'win32' requires model_root to be set")

    def resolve_work_dir(self) -> Path:
        return self.work_dir.expanduser().resolve()

    def ensure_work_dirs(self) -> dict[str, Path]:
        """Create and return the standard work directories."""
        root = self.resolve_work_dir()
        dirs = {
            "root": root,
            "models": root / "models",
            "results": root / "results",
            "logs": root / "logs",
        }
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        return dirs

    def require_model_root(self) -> Path:
        if self.model_root is None:
            raise ConfigError("model_root is not configured")
        root = self.model_root.expanduser().resolve()
        if not root.exists():
            raise ConfigError("model_root does not exist", details={"model_root": str(root)})
        return root
