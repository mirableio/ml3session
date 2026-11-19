"""Read a portable trace ZIP through the same session interface as live history."""
from datetime import datetime
from itertools import groupby
import json
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .transcript import events


class TraceSession:
    def __init__(self, path, entry):
        self.path = path
        self.entry = entry
        self.modified = datetime.fromisoformat(entry["modified"]).timestamp()
        self.source = entry["source"]
        self.id = entry["session"]
        self.descendants = []

    @property
    def key(self):
        return f"trace:{self.source}:{self.id}"

    def metadata(self):
        return self.entry

    def child_sessions(self):
        return self.descendants

    def conversation_records(self, start=None, stop=None, **options):
        def numbered_rows(stream):
            for number, raw in enumerate(stream, 1):
                row = json.loads(raw)
                if not isinstance(row, dict) or not isinstance(row.get("text"), str):
                    raise ValueError(f"invalid trace row in {self.entry['file']}:{number}")
                yield number, raw, row

        def source_record(item):
            return item[2]["line"], item[2].get("snapshot_item", 0)

        with ZipFile(self.path) as archive, archive.open(self.entry["file"]) as stream:
            for _, group in groupby(numbered_rows(stream), key=source_record):
                rows = list(group)
                if rows[0][2].get("replaced"):
                    continue  # A turn the user edited away; kept in the ZIP for the record.
                pos = (rows[0][0], 0)
                if start and pos < start:
                    continue
                if stop and pos > stop:
                    break
                raw = b"".join(line for _, line, _ in rows)
                yield pos, raw, {"timestamp": rows[0][2].get("timestamp"),
                                 "rows": [row for _, _, row in rows]}

    def view_events(self, record, *, outline=False, include_tools=True, **options):
        if outline:
            text = [r["text"] for r in record["rows"] if r["role"] == "user"
                    and not r["text"].startswith("[image:")]
            if text:
                message = {"type": "message", "role": "user", "content": " ".join(text)}
                synthetic = {"type": "user", "message": message} if self.source == "claude" else message
                yield from events(self.source, synthetic, outline=True)
            return
        for row in record["rows"]:
            role, text = row["role"], row["text"]
            if role.startswith("tool_") and not include_tools:
                continue
            if role in ("thinking", "reasoning_summary") and not text.strip():
                continue
            yield role, text


def load_archive(path, source):
    path = Path(path).expanduser()
    try:
        with ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("mode") != "trace" or manifest.get("format_version") != 1:
                raise ValueError("expected a version 1 trace ZIP; extract native backups first")
            if manifest["source"] != source:
                raise ValueError(f"archive contains {manifest['source']} sessions; select that source")
            sessions = {e["session"]: TraceSession(path, e) for e in manifest["sessions"]}
            files = set(archive.namelist())
            for session in sessions.values():
                if session.entry["file"] not in files:
                    raise ValueError(f"missing trace member: {session.entry['file']}")
                if "/" in session.id:
                    parent = sessions.get(session.id.partition("/")[0])
                    if parent:
                        parent.descendants.append(session)
    except (BadZipFile, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid trace archive {path}: {exc}") from exc
    return sorted((s for s in sessions.values() if "/" not in s.id),
                  key=lambda s: s.modified, reverse=True)
