"""The help is two manuals, and the index has to read both."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_tool():
    path = Path(__file__).resolve().parent.parent / "tools" / "pshelp_index.py"
    spec = importlib.util.spec_from_file_location("pshelp_index", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PAGE = """<html><head><title>{title}</title></head><body>
<div class="WebWorks_Breadcrumbs">{crumbs}</div>
<p>Syntax:</p><p>{syntax}</p>
<p>Assignment</p><p>{title} := value</p>
</body></html>"""


def _write(tmp_path: Path, name: str, crumbs: str, title: str = "procTime",
           syntax: str = "&lt;Path&gt;.procTime") -> Path:
    path = tmp_path / (name + ".html")
    path.write_text(PAGE.format(title=title, crumbs=crumbs, syntax=syntax),
                    encoding="utf-8")
    return path


def test_add_in_pages_get_an_owner(tmp_path: Path) -> None:
    """Measured: 417 of 2,772 members had no owner because every page from the
    separate "Add-Ins Reference Help" manual was skipped. That manual covers
    COM/C/DDE, Kanban, Database, genetic algorithms, ValueStreamMapping,
    ActiveX and Gantt -- real namespaces, not noise."""
    tool = _load_tool()
    page = _write(
        tmp_path, "InterPrInterfaces.42.035",
        "Add-Ins Reference Help &gt; Inter-Process Communication Interfaces"
        " &gt; COM &gt; Functions of the COM Interface &gt; startSimulation",
        title="startSimulation", syntax="eMPlant.startSimulation(PathOfEventController:&lt;string&gt;)",
    )
    entry = tool.parse_page(page)
    assert entry is not None
    assert entry["breadcrumb"].startswith("Add-Ins Reference Help >")
    assert entry["owner"] == "Functions of the COM Interface"


def test_reference_help_pages_still_work(tmp_path: Path) -> None:
    """The fix must not disturb the manual that already parsed."""
    tool = _load_tool()
    page = _write(
        tmp_path, "Ch02UserInterface1.17.240",
        "Reference Help &gt; The Plant Simulation Program Window &gt; Toolbox"
        " &gt; Attributes and Methods of the Toolbox",
        title="Attributes and Methods of the Toolbox",
    )
    entry = tool.parse_page(page)
    assert entry is not None
    assert entry["breadcrumb"].startswith("Reference Help >")
    assert entry["owner"] == "Toolbox"


def test_a_page_with_no_breadcrumb_is_still_returned(tmp_path: Path) -> None:
    """Empty owner is a fact about a page, not a reason to drop it."""
    tool = _load_tool()
    page = _write(tmp_path, "Stray.01.001", "")
    entry = tool.parse_page(page)
    assert entry is not None
    assert entry["owner"] == ""