"""Recover the ancestry of a Desktop rewind without exposing discarded turns."""
from collections import defaultdict
from contextlib import ExitStack
from functools import cached_property
from heapq import merge
import json
import mmap

from .sessions import Session, children


def record_time(path, uuid):
    """Timestamp of one record, found without parsing the whole transcript."""
    try:
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            needle = b'"' + uuid.encode() + b'"'
            end = len(data)
            # Fork points sit near the end of the file they were recorded in.
            while True:
                hit = data.rfind(needle, 0, end)
                if hit < 0:
                    break
                begin = data.rfind(b"\n", 0, hit) + 1
                stop = data.find(b"\n", hit)
                try:
                    record = json.loads(data[begin:stop if stop >= 0 else len(data)])
                except ValueError:
                    record = None
                # Other records name this ID as their parent.
                if isinstance(record, dict) and record.get("uuid") == uuid:
                    return record.get("timestamp") or ""
                end = begin
    except (OSError, ValueError):
        pass
    return ""


def first_time(path):
    try:
        with path.open("rb") as stream:
            for raw in stream:
                stamp = json.loads(raw).get("timestamp")
                if stamp:
                    return stamp
    except (OSError, ValueError, AttributeError):
        pass
    return ""


class RewindSession(Session):
    def __init__(self, current, lineage):
        super().__init__(current.source, current.id, current.path, current.modified)
        self.lineage = lineage

    @cached_property
    def insertions(self):
        # Each ancestor file contributes its records, in file order, as far as
        # its recorded fork point. Parent links are not followed: parallel tool
        # calls sit off that chain, and repeated record IDs make it loop.
        # Store offsets rather than retaining potentially large tool payloads.
        usable = []
        for predecessor, point in self.lineage:
            rows = []
            offset = 0
            for _, raw in predecessor.lines():
                try:
                    record = json.loads(raw)
                except ValueError:
                    record = None
                if isinstance(record, dict) and record.get("uuid"):
                    rows.append((record["uuid"], predecessor.path, offset))
                    if record["uuid"] == point:
                        usable.append(rows)
                        break
                offset += len(raw)
            else:
                break

        # Insert missing ancestors immediately before the next shared record.
        # Current-file positions (and previously issued current cursors) stay intact.
        anchors = {}
        for n, _, r in self.records(tolerant=True, warn=False):
            if r.get("uuid"):
                anchors.setdefault(r["uuid"], n)
        groups = defaultdict(list)
        pending = []
        seen = set()
        last_anchor = 0
        for rows in reversed(usable):
            for uid, path, offset in rows:
                if uid in seen:
                    continue  # Copied forward from an older file, or repeated in this one.
                seen.add(uid)
                if uid in anchors:
                    last_anchor = max(last_anchor, anchors[uid])
                    if pending:
                        groups[last_anchor].extend(pending)
                        pending = []
                else:
                    pending.append((path, offset))
        if pending:
            groups[last_anchor + 1].extend(pending)
        return groups

    def child_sessions(self):
        found = {child.id: child for child in children(self)}
        for predecessor, point in self.lineage:
            limit = record_time(predecessor.path, point)
            for child in children(predecessor):
                ident = self.id + "/" + child.id.partition("/")[2]
                # Agents started after the fork point belong to the discarded turns.
                if ident not in found and not (limit and first_time(child.path) > limit):
                    found[ident] = Session("claude", ident, child.path, child.modified)
        return list(found.values())

    def inherited_records(self, start, stop):
        with ExitStack() as stack:
            streams = {}
            for anchor, group in sorted(self.insertions.items()):
                for offset_in_group, (path, offset) in enumerate(group, -len(group)):
                    pos = (anchor, offset_in_group)
                    if (start and pos < start) or (stop and pos > stop):
                        continue
                    if path not in streams:
                        streams[path] = stack.enter_context(path.open("rb"))
                    stream = streams[path]
                    stream.seek(offset)
                    raw = stream.readline()
                    yield pos, raw, json.loads(raw)

    def conversation_records(self, start=None, stop=None, **options):
        local = super().conversation_records(start, stop, **options)
        yield from merge(self.inherited_records(start, stop), local, key=lambda row: row[0])
