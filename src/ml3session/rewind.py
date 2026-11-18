"""Recover the ancestry of a Desktop rewind without exposing discarded turns."""
from collections import defaultdict
from contextlib import ExitStack
from functools import cached_property
from heapq import merge
import json

from .sessions import Session


class RewindSession(Session):
    def __init__(self, current, lineage):
        super().__init__(current.source, current.id, current.path, current.modified)
        self.lineage = lineage

    @cached_property
    def insertions(self):
        # Each ancestor file contributes only as far as its recorded fork point.
        # Store offsets rather than retaining potentially large tool payloads.
        nodes = {}
        for predecessor, point in self.lineage:
            indexed = {}
            previous = None
            offset = 0
            for _, raw in predecessor.lines():
                try:
                    record = json.loads(raw)
                except ValueError:
                    record = None
                if isinstance(record, dict) and record.get("uuid"):
                    uid = record["uuid"]
                    parent = record.get("parentUuid") or record.get("logicalParentUuid")
                    # Some compaction boundaries point to an intermediate event
                    # that was never persisted. The preceding main-chain record
                    # is the end of the pre-compaction segment in this bounded file.
                    fallback = previous if record.get("subtype") == "compact_boundary" else None
                    indexed[uid] = (parent, fallback, predecessor.path, offset)
                    if not record.get("isSidechain"):
                        previous = uid
                    if uid == point:
                        nodes.update(indexed)
                        break
                offset += len(raw)
            else:
                break

        order = []
        seen = set()
        uid = self.lineage[0][1]
        while uid in nodes and uid not in seen:
            seen.add(uid)
            order.append(uid)
            parent, fallback = nodes[uid][:2]
            uid = parent if parent in nodes else fallback

        # Insert missing ancestors immediately before the next shared record.
        # Current-file positions (and previously issued current cursors) stay intact.
        anchors = {r["uuid"]: n for n, _, r in self.records(tolerant=True, warn=False)
                   if r.get("uuid") in seen}
        groups = defaultdict(list)
        pending = []
        last_anchor = 0
        for uid in reversed(order):
            if uid in anchors:
                last_anchor = max(last_anchor, anchors[uid])
                if pending:
                    groups[last_anchor].extend(pending)
                    pending = []
            else:
                pending.append(nodes[uid][2:])
        if pending:
            groups[last_anchor + 1].extend(pending)
        return groups

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
