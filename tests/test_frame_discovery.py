"""Tests proving the model root comes from the name inside the file, not the file name.

Loading DeliveryConcept.spp renamed to zzz_renamed.spp still roots the model
at .Models.DeliveryConcept rather than .Models.zzz_renamed.
"""
from __future__ import annotations


class _DiscoveryApp:
    """Minimal COM stand-in that answers NumNodes and node(i).Name."""

    def __init__(self, names):
        self.names = names
        self.calls = []

    def GetValue(self, path):
        self.calls.append(path)
        if path == ".Models.NumNodes":
            return len(self.names)
        raise RuntimeError("unexpected " + path)

    def ExecuteSimTalk(self, source):
        self.calls.append(source)


def test_frame_is_discovered_not_derived_from_the_file(monkeypatch) -> None:
    """A file called anything at all still roots at its internal model name."""
    from psbridge.com import win32 as win

    backend = object.__new__(win.Win32ComBackend)
    backend._model_path = r"C:\models\zzz_renamed.spp"
    backend._discovered_frame = None
    backend._last_error = None
    app = _DiscoveryApp(["DeliveryConcept", "Factory1"])
    backend._app = app
    backend._connected = True
    monkeypatch.setattr(win.Win32ComBackend, "_read_number",
                        lambda self, path: len(app.names))
    monkeypatch.setattr(win.Win32ComBackend, "evaluate",
                        lambda self, expr: "DeliveryConcept" if "node(1)" in expr else "Factory1")
    assert backend.default_frame_path == ".Models.DeliveryConcept"
    # and it is cached, so listing the root does not happen twice
    assert backend.default_frame_path == ".Models.DeliveryConcept"


def test_frame_falls_back_when_nothing_is_loaded() -> None:
    from psbridge.com import win32 as win

    backend = object.__new__(win.Win32ComBackend)
    backend._model_path = None
    backend._discovered_frame = None
    backend._last_error = None
    assert backend.default_frame_path == win.DEFAULT_FRAME_PATH


def test_controller_follows_the_discovered_frame() -> None:
    """The controller had the identical file-stem bug and shares the fix."""
    from psbridge.com import win32 as win

    backend = object.__new__(win.Win32ComBackend)
    backend._model_path = r"C:\models\zzz_renamed.spp"
    backend._discovered_frame = ".Models.DeliveryConcept"
    backend._controller_path = win.DEFAULT_CONTROLLER_PATH
    backend._controller_path_explicit = False
    assert backend._controller() == ".Models.DeliveryConcept.EventController"


def test_an_explicit_controller_is_never_overridden() -> None:
    from psbridge.com import win32 as win

    backend = object.__new__(win.Win32ComBackend)
    backend._model_path = r"C:\models\zzz.spp"
    backend._discovered_frame = ".Models.DeliveryConcept"
    backend._controller_path = ".Models.Other.EventController"
    backend._controller_path_explicit = True
    assert backend._controller() == ".Models.Other.EventController"