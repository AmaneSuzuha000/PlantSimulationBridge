"""Compare what the help documents against what COM actually exposes.

The help index says which attributes a class is supposed to have. This tool
creates a real object of that class and asks COM, one attribute at a time,
whether the attribute can be read. The difference between the two sets is the
interesting part: it is where a bridge built from the documentation alone would
fail.

Two measured constraints shape the code:

* Material-flow objects can be created and then renamed, and the new name
  resolves.
* Information-flow objects (Method, Variable, TableFile) CANNOT be addressed by
  name at all -- renaming reports success, yet <frame>.<Name> still fails.
  They are reachable only through the node() accessor, which is not a valid
  identifier and therefore has to live inside an expression. That is what
  Win32ComBackend.evaluate is for.
* A Connector is not created with createObject; it comes from
  Connector.connect(a, b), so it has no entry here.

Usage:
    python tools/api_surface.py --classes Source,SingleProc,Drain --out surface.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psbridge.com.win32 import Win32ComBackend

_DEFAULT_TEMPLATES = (
    Path(r"C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
    Path(r"E:\Software\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
)


def resolve_template(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_val = os.environ.get("PSBRIDGE_TEMPLATE")
    if env_val:
        candidates.append(Path(env_val))
    candidates.extend(_DEFAULT_TEMPLATES)
    for path in candidates:
        if path.exists():
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError("Plant Simulation template .spp not found; set PSBRIDGE_TEMPLATE (checked: " + searched + ")")


CLASSES = {
    # Verified creatable class paths against Plant Simulation 13. Notably,
    # Assembly stations use .MaterialFlow.Assembly (not AssemblyStation), and
    # MU classes (.MUs.Entity) cannot be created on frames directly via
    # createObject -- only referenced by Source.Path / Delivery Tables.
    "Source": ".MaterialFlow.Source",
    "Drain": ".MaterialFlow.Drain",
    "SingleProc": ".MaterialFlow.SingleProc",
    "ParallelProc": ".MaterialFlow.ParallelProc",
    "Assembly Station": ".MaterialFlow.Assembly",
    "Buffer": ".MaterialFlow.Buffer",
    "PlaceBuffer": ".MaterialFlow.PlaceBuffer",
    "Sorter": ".MaterialFlow.Sorter",
    "Object Cycle": ".MaterialFlow.Cycle",
    "Track": ".MaterialFlow.Track",
    "TwoLaneTrack": ".MaterialFlow.TwoLaneTrack",
    "Turntable": ".MaterialFlow.Turntable",
    "Store": ".MaterialFlow.Store",
    "Line": ".MaterialFlow.Line",
    "DismantleStation": ".MaterialFlow.DismantleStation",
    "Interface": ".MaterialFlow.Interface",
    "Method": ".InformationFlow.&Method",
    "Variable": ".InformationFlow.&Variable",
    "TableFile": ".InformationFlow.&TableFile",
    "CardFile": ".InformationFlow.&CardFile",
    "FileLink": ".InformationFlow.&FileLink",
    "Comment": ".InformationFlow.&Comment",
    "ShiftCalendar": ".InformationFlow.&ShiftCalendar",
    "AttributeExplorer": ".InformationFlow.&AttributeExplorer",
    "Generator": ".InformationFlow.&Generator",
    "Chart": ".UserInterface.Chart",
    "HtmlReport": ".UserInterface.HtmlReport",
    "Dialog": ".UserInterface.Dialog",
    "Button": ".UserInterface.Button",
    "Checkbox": ".UserInterface.Checkbox",
    "Drop-Down List": ".UserInterface.DropDownList",
}

# Objects that already exist in the template model: probing them avoids
# creating something whose semantics differ from the real one.
EXISTING = ("EventController", "Frame")

SKIP = {
    "start", "stop", "reset", "init", "quit", "close", "open", "load", "save",
    "delete", "move", "copy", "execute", "run",
}

# Classes whose instances cannot be addressed by name on this build. Measured:
# renaming such an object reports success, yet <frame>.<Name> still fails, so
# the sweep has to keep the node() handle captured at creation time.
INFOFLOW = {"Method", "Variable", "TableFile", "CardFile", "FileLink", "Comment",
            "ShiftCalendar", "AttributeExplorer", "Generator"}

# Members the product refuses to answer until the object has been configured.
# Note that the switch that unlocks indexXDim is ColumnIndex, NOT the
# dialog checkbox ShowColumnIndex -- setting ShowColumnIndex alone leaves the
# member unreadable. Same for RowIndex vs ShowRowIndex.
PRECONDITION = {
    ("TableFile", "indexXDim"): "needs ColumnIndex := true",
    ("TableFile", "indexYDim"): "needs RowIndex := true",
    ("TableFile", "FormatStringRowIndex"):
        "needs a format-string row; no way found to create one through SetValue",
    ("Variable", "rollDice"): 'needs DataType := "randtime"',
}


def split_precondition(owner: str, failures: list) -> tuple:
    """Separate 'refuses until configured' from 'could not be read at all'."""
    pre, real = [], []
    for item in failures:
        if item["name"] == "<budget>":
            real.append(item)
        elif (owner, item["name"]) in PRECONDITION:
            pre.append({"name": item["name"],
                        "why": PRECONDITION[(owner, item["name"])]})
        else:
            real.append(item)
    return pre, real


def load_index(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def attributes_for(index: dict, owner: str) -> list:
    names = []
    for entry in index["entries"]:
        if entry["owner"] != owner:
            continue
        if entry["kind"] not in ("attribute", "readonly"):
            continue
        title = entry["title"].strip()
        if not title or " " in title:
            continue
        if title.lower() in SKIP:
            continue
        names.append(title)
    return sorted(set(names))


def probe_name(owner: str) -> str:
    """A SimTalk-safe object name for a help owner, which may contain spaces."""
    return "Probe_" + re.sub(r"[^A-Za-z0-9_]", "_", owner)


def read_attribute(backend, handle, name):
    """Try the read strategies that cover the attribute shapes the help has.

    Scalar attributes answer to to_str(). Object-valued ones (ExitCtrl,
    EntranceCtrl, ...) cannot be stringified, and ref() does not help either --
    measured on TwoLaneTrack: EntranceCtrl, ExitCtrl and even Length fail all
    three forms while Name and Class succeed.

    Those last ones are not a channel limit after all: the help gives their
    syntax as <Path>.A.Length / <Path>.B.Length, i.e. a lane selector sits
    between the object and the member. 32 help entries carry that .A. form and
    every one of them belongs to TwoLaneTrack. So the lane forms are tried too,
    and a member only counts as unreadable when all five fail.
    """
    attempts = (
        "to_str(" + handle + "." + name + ")",
        "to_str(ref(" + handle + ")." + name + ")",
        "to_str(" + handle + "." + name + ".Name)",
        "to_str(" + handle + ".A." + name + ")",
        "to_str(" + handle + ".B." + name + ")",
    )
    last = ""
    for expression in attempts:
        try:
            return True, backend.evaluate(expression), expression
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:140]
    return False, None, last


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=os.environ.get("PSBRIDGE_API_INDEX", ""))
    parser.add_argument("--template", default=None, help="path to base .spp model (or set PSBRIDGE_TEMPLATE)")
    parser.add_argument("--classes", default="all",
                        help="comma-separated owner names, or 'all' for the whole catalog")
    parser.add_argument("--out", default="api_surface.json")
    parser.add_argument("--budget", type=float, default=90.0)
    args = parser.parse_args(argv)

    if not args.index:
        parser.error("pass --index or set PSBRIDGE_API_INDEX to api_index.json")
    template = resolve_template(args.template)
    index = load_index(Path(args.index))
    if args.classes.strip().lower() == "all":
        wanted = list(CLASSES) + list(EXISTING)
    else:
        wanted = [name.strip() for name in args.classes.split(",") if name.strip()]

    workdir = Path(tempfile.mkdtemp(prefix="psbridge-surface-"))
    target = workdir / template.name
    shutil.copyfile(template, target)

    backend = Win32ComBackend(model_root=workdir, quit_on_disconnect=True, prefer_running_instance=False)
    report = {"template": str(template), "classes": {}}
    try:
        backend.connect()
        backend.load_model(str(target))
        root_frame = backend.default_frame_path
        frame = root_frame + ".Frame" if backend.object_exists(root_frame + ".Frame") else root_frame

        for owner in wanted:
            if owner in EXISTING:
                handle = root_frame if owner == "Frame" and not backend.object_exists(root_frame + ".Frame") else root_frame + "." + owner
                documented = attributes_for(index, owner)
                readable, failed = [], []
                started = time.monotonic()
                for name in documented:
                    if time.monotonic() - started > args.budget:
                        failed.append({"name": "<budget>", "error": "sweep budget exhausted"})
                        break
                    ok, _value, detail = read_attribute(backend, handle, name)
                    (readable if ok else failed).append(name if ok else {"name": name, "error": detail})
                pre, real = split_precondition(owner, failed)
                report["classes"][owner] = {
                    "handle": handle, "documented": len(documented), "readable": len(readable),
                    "failed": len(real), "precondition": pre,
                    "readable_names": readable, "failed_attributes": real,
                }
                continue
            create = CLASSES.get(owner)
            if not create:
                report["classes"][owner] = {"error": "no create path known"}
                continue
            try:
                backend.execute_simtalk(create + ".createObject(" + frame + ", 200, 200);")
                index_now = int(str(backend.get_value(frame, "NumNodes")))
                if owner in INFOFLOW:
                    handle = frame + ".node(" + str(index_now) + ")"
                else:
                    backend.execute_simtalk(
                        frame + ".node(" + frame + ".NumNodes).Name := " + json.dumps(probe_name(owner)) + ";"
                    )
                    handle = frame + "." + probe_name(owner)
            except Exception as exc:  # noqa: BLE001
                report["classes"][owner] = {"error": "create failed: " + str(exc)[:160]}
                continue

            documented = attributes_for(index, owner)
            readable, failed = [], []
            started = time.monotonic()
            for name in documented:
                if time.monotonic() - started > args.budget:
                    failed.append({"name": "<budget>", "error": "sweep budget exhausted"})
                    break
                ok, _value, detail = read_attribute(backend, handle, name)
                if ok:
                    readable.append(name)
                else:
                    failed.append({"name": name, "error": detail})
            pre, real = split_precondition(owner, failed)
            report["classes"][owner] = {
                "handle": handle,
                "documented": len(documented),
                "readable": len(readable),
                "failed": len(real),
                "precondition": pre,
                "readable_names": readable,
                "failed_attributes": real,
            }
    finally:
        with contextlib.suppress(Exception):
            backend.disconnect()
        shutil.rmtree(workdir, ignore_errors=True)

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for owner, data in report["classes"].items():
        if "error" in data:
            print(f"{owner:<14} ERROR {data['error'][:70]}")
        else:
            pre_count = len(data.get("precondition") or [])
            print(
                f"{owner:<14} documented={data['documented']:3d} "
                f"readable={data['readable']:3d} failed={data['failed']:3d} precondition={pre_count:d}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())