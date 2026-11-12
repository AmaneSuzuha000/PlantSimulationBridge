"""Query the help index, with what was measured on a real machine attached.

The index answers "what does the help say exists". The evidence files answer
"what did this machine actually do". Keeping them apart made every question a
grep exercise, so this module joins them on (owner, name) and offers one
lookup surface.

Statuses, in the vocabulary the probes use:
  documented   the help lists it, nothing was measured
  readable     read successfully on a real object
  callable     called with no arguments and returned
  needs-args   answered "wrong number of arguments" -- it exists
  absent       answered "not a valid path" -- it does not exist
  error        reached the product and was refused for another reason
  hung         the probe had to be killed; the name comes from its log
  unmapped     no object of a suitable class could be built to test it
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Member:
    name: str
    owner: str
    kind: str
    page: str = ""
    syntax: str = ""
    status: str = "documented"
    detail: str = ""

    @property
    def exists(self) -> bool:
        """True when the machine proved this member is there."""
        return self.status in ("readable", "callable", "needs-args")

    def as_dict(self) -> dict:
        return {"name": self.name, "owner": self.owner, "kind": self.kind,
                "status": self.status, "exists": self.exists, "detail": self.detail}


class ApiIndex:
    """The documented surface, optionally annotated with measurements."""

    def __init__(self, members: Sequence[Member]) -> None:
        self._members = list(members)

    @classmethod
    def load(cls, index_path: str | Path, *evidence_paths: str | Path) -> ApiIndex:
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
        members = [
            Member(name=e["title"], owner=e["owner"], kind=e["kind"],
                   page=e.get("page", ""), syntax=e.get("syntax", ""))
            for e in data["entries"]
        ]
        for path in evidence_paths:
            p = Path(path)
            if not p.exists():
                continue
            payload = json.loads(p.read_text(encoding="utf-8"))
            members = _apply(members, payload.get("results", []))
        return cls(members)

    def __len__(self) -> int:
        return len(self._members)

    def search(self, text: str, *, kind: str | None = None,
               status: str | None = None, exists: bool | None = None,
               limit: int = 50) -> list[Member]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"limit must be an int, got {type(limit).__name__}")
        if limit <= 0:
            return []
        needle = text.strip().casefold()
        hits = []
        for member in self._members:
            if needle and needle not in member.name.casefold():
                continue
            if kind is not None and member.kind != kind:
                continue
            if status is not None and member.status != status:
                continue
            if exists is not None and member.exists is not exists:
                continue
            hits.append(member)
            if len(hits) >= limit:
                break
        return hits

    def by_owner(self, owner: str) -> list[Member]:
        wanted = owner.strip().casefold()
        return [m for m in self._members if m.owner.casefold() == wanted]

    def owners(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for member in self._members:
            counts[member.owner] = counts.get(member.owner, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for member in self._members:
            by_status[member.status] = by_status.get(member.status, 0) + 1
            by_kind[member.kind] = by_kind.get(member.kind, 0) + 1
        verified = sum(1 for m in self._members if m.exists)
        return {"members": len(self._members), "verified": verified,
                "by_status": dict(sorted(by_status.items())),
                "by_kind": dict(sorted(by_kind.items()))}


# Statuses that record something the machine actually observed. Everything
# else is bookkeeping: "documented" (only the help mentions it), "not-executed"
# and "unmapped" (nobody tried).
MEASURED_STATUSES = frozenset({
    "readable", "callable", "needs-args", "absent", "error", "hung", "timed-out",
})
UNMEASURED_STATUSES = frozenset({"", "documented", "not-executed", "unmapped"})

# Normalise probe status spellings so "exists-needs-args" and "needs-args"
# map consistently when merging measurement outputs.
STATUS_ALIASES = {"exists-needs-args": "needs-args"}


def normalise_status(status: str) -> str:
    return STATUS_ALIASES.get(status, status)


def _apply(members: Sequence[Member], results: Iterable[dict]) -> list[Member]:
    """Attach measured statuses, never letting bookkeeping erase a measurement.

    Evidence files overlap: api_surface_by_owner.json lists every member it saw,
    including the ones it never called, as "not-executed". Applying it after
    method_results.json therefore overwrote 197 measured methods, and the
    combined report claimed 5 verified methods where there were 202.
    """
    status: dict[tuple[str, str], dict] = {}
    for row in results:
        key = (row.get("owner", ""), row.get("name", ""))
        incoming = normalise_status(row.get("status", "documented"))
        previous = status.get(key)
        if previous is not None:
            kept = previous["status"]
            if kept in MEASURED_STATUSES and incoming not in MEASURED_STATUSES:
                continue
        status[key] = {**row, "status": incoming}
    out = []
    for member in members:
        row = status.get((member.owner, member.name))
        if row is None:
            out.append(member)
            continue
        incoming = row.get("status", "documented")
        # Compare against the status this member already carries, not just
        # against the other rows of this file: by the second evidence file the
        # earlier measurement is already on the member.
        if member.status in MEASURED_STATUSES and incoming not in MEASURED_STATUSES:
            out.append(member)
            continue
        out.append(replace(member, status=incoming,
                           detail=row.get("detail", row.get("error", ""))))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="apiquery", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("search", "owner", "owners", "stats"))
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("--kind")
    parser.add_argument("--status")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--index", default=os.environ.get("PSBRIDGE_API_INDEX", ""))
    parser.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args(argv)

    if not args.index:
        parser.error("pass --index or set PSBRIDGE_API_INDEX to api_index.json")
    index = ApiIndex.load(args.index, *args.evidence)

    if args.command == "stats":
        print(json.dumps(index.stats(), ensure_ascii=False, indent=1))
    elif args.command == "owners":
        for owner, count in index.owners()[: max(0, args.limit)]:
            print(f"{count:4d}  {owner or '(no owner)'}")
    elif args.command == "owner":
        for member in index.by_owner(args.value):
            print(f"{member.kind:<9} {member.status:<10} {member.name}")
    else:
        for member in index.search(args.value, kind=args.kind, status=args.status,
                                   limit=args.limit):
            print(f"{member.kind:<9} {member.status:<10} {member.owner or '-':<28} {member.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
