"""Write portable decision traces or original local session files to a ZIP."""
from contextlib import closing
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .reader import JSON_LINE_ESCAPES
from .sessions import ReadStats, children, desktop_session_dir, inventory, meta, session_root
from .transcript import codex_item, events


def decision_events(source, record, cwd):
    yield from events(source, record, cwd=cwd, include_thinking=source == "claude")
    if source == "codex":
        item = codex_item(record)
        if isinstance(item, dict) and item.get("type") == "reasoning":
            for block in item.get("summary") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip():
                    yield "reasoning_summary", block["text"]


def write_trace(archive, session, entry):
    name = f"traces/{session.id}.jsonl"
    stats = ReadStats()
    count = 0
    with archive.open(name, "w", force_zip64=True) as stream:
        for (line, index), _, record in session.conversation_records(tolerant=True, stats=stats):
            for role, text in decision_events(session.source, record, entry["project"]):
                row = dict(line=line, timestamp=record.get("timestamp"), role=role, text=text)
                if index:
                    row["snapshot_item"] = index
                message_id = record.get("uuid") or (codex_item(record) or {}).get("id")
                if message_id:
                    row["message_id"] = message_id
                data = json.dumps(row, ensure_ascii=False).translate(JSON_LINE_ESCAPES)
                stream.write((data + "\n").encode("utf-8"))
                count += 1
    entry.update(file=name, events=count, skipped_line_ranges=stats.ranges)


def write_original(archive, path, name):
    # Bound the copy to the length at open time; an active session can keep growing.
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size and path.suffix == ".jsonl":
            # Exclude an unfinished last record. Earlier malformed lines are preserved.
            end = size
            while end:
                start = max(0, end - 65536)
                source.seek(start)
                edge = source.read(end - start).rfind(b"\n")
                if edge >= 0:
                    break
                end = start
            size = start + edge + 1 if end else 0
            source.seek(0)
        info = ZipInfo(str(name))
        info.compress_type = ZIP_STORED if path.suffix == ".zst" else ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=True) as dest:
            while size:
                block = source.read(min(size, 1024 * 1024))
                if not block:
                    raise OSError(f"file shrank during backup: {path}")
                dest.write(block)
                size -= len(block)


def write_metadata(archive, source, root, temp_dir):
    if source == "claude":
        desktop = desktop_session_dir()
        if desktop and desktop.is_dir():
            for path in desktop.glob("*/*/local_*.json"):
                write_original(archive, path, "desktop/" + path.relative_to(desktop).as_posix())
        return
    index = root / "session_index.jsonl"
    if index.exists():
        write_original(archive, index, "codex/" + index.name)
    for pattern in ("state_*.sqlite", "thread_history_*.sqlite"):
        for path in root.glob(pattern):
            # SQLite's backup API includes committed WAL data in a consistent database.
            snapshot = temp_dir / path.name
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                with closing(sqlite3.connect(snapshot)) as dest:
                    db.backup(dest)
            write_original(archive, snapshot, "codex/" + path.name)
            snapshot.unlink()


def publish_zip(temporary, output):
    try:
        os.link(temporary, output)
        return
    except OSError as exc:
        if exc.errno not in (errno.ENOSYS, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise
    # Reserve the destination exclusively before replacing it on filesystems
    # without hard links. A concurrent invocation cannot claim this name.
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        os.replace(temporary, output)
    except BaseException:
        output.unlink()
        raise


def backup(source, destination=None, native=False):
    now = datetime.now(timezone.utc)
    mode = "native" if native else "trace"
    name = f"ml3session-{source}-{mode}-{now:%Y%m%dT%H%M%S%fZ}.zip"
    output = Path(destination or name).expanduser().absolute()
    if output.exists():
        raise ValueError(f"backup already exists: {output}")
    root = session_root(source)
    sessions = inventory(source, include_rewound=True)
    if source == "claude":
        sessions += [child for parent in sessions for child in children(parent)]
    if not sessions:
        raise ValueError(f"no {source} transcripts found under {root}")
    manifest = dict(format_version=1, source=source, mode=mode, started_at=now.isoformat(), sessions=[])
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".ml3session-") as tmp:
        temp_dir = Path(tmp)
        temporary = temp_dir / "backup.zip"
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for session in sessions:
                entry = meta(session)
                if native:
                    names = [source + "/" + path.relative_to(root).as_posix() for path in session.files]
                    entry["file"] = names[0]
                    if names[1:]:
                        entry["continuation_files"] = names[1:]
                    for path, name in zip(session.files, names):
                        write_original(archive, path, name)
                    sidecar = session.path.with_suffix(".meta.json")
                    if source == "claude" and "/" in session.id and sidecar.exists():
                        write_original(archive, sidecar, source + "/" + sidecar.relative_to(root).as_posix())
                else:
                    write_trace(archive, session, entry)
                manifest["sessions"].append(entry)
            if native:
                write_metadata(archive, source, root, temp_dir)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2))
        temporary.chmod(0o600)
        # Atomically publish only the finished ZIP, without clobbering a concurrent backup.
        publish_zip(temporary, output)
    return dict(source=source, mode=mode, archive=str(output), sessions=len(sessions),
                bytes=output.stat().st_size)
