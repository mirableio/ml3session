"""Locate transcripts and decode numbered records in their original order."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cache, cached_property
import io
import json
import mmap
import os
from pathlib import Path
import re
import sys

from .transcript import codex_item, events, queued_prompt, visible_codex_item

if sys.version_info >= (3, 14):
    from compression.zstd import open as open_zstd
else:
    import zstandard

    def open_zstd(path, mode):
        return io.BufferedReader(zstandard.open(path, mode))


UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
FIRST_UUID = re.compile(UUID.pattern[:-1])


def transcript_stem(path):
    return path.name.removesuffix(".zst").removesuffix(".jsonl")


@dataclass
class ReadStats:
    ranges: list = field(default_factory=list)

    def skip(self, number):
        if self.ranges and self.ranges[-1][1] == number - 1:
            self.ranges[-1][1] = number
        else:
            self.ranges.append([number, number])


def validate_record(record):
    if not isinstance(record, dict):
        raise ValueError("expected a JSON object")
    kind = record.get("type")
    if kind in ("session_meta", "response_item"):
        message = record.get("payload")
    elif kind in ("user", "assistant"):
        message = record.get("message")
    elif kind == "message":
        message = record
    elif kind == "attachment" and queued_prompt(record):
        message = {"content": record["attachment"].get("prompt")}
    else:
        return
    if not isinstance(message, dict):
        raise ValueError("expected a message or payload object")
    if kind == "session_meta" or (kind == "response_item" and message.get("type") != "message"):
        return
    content = message.get("content")
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise ValueError("expected a list of message blocks")
    for block in content:
        if block.get("type") in ("text", "input_text", "output_text") and not isinstance(block.get("text"), str):
            raise ValueError("expected a string in a text block")


@dataclass
class Session:
    source: str
    id: str
    path: Path
    modified: float
    # Codex continues a thread in further rollout files after an edited turn.
    parts: tuple = ()
    aliases: tuple = ()

    @property
    def key(self):
        return f"{self.source}:{self.id}"

    @property
    def files(self):
        return self.parts or (self.path,)

    @cached_property
    def segments(self):
        """Pair each rollout file with the byte length its successor retained."""
        if len(self.files) == 1:
            return [(self.path, None)]
        kept = []
        for path in self.files:
            base = {}
            if kept:
                try:
                    with (open_zstd if path.suffix == ".zst" else open)(path, "rb") as stream:
                        payload = json.loads(stream.readline()).get("payload")
                    base = payload.get("history_base")
                except (OSError, EOFError, ValueError, AttributeError):
                    pass
            if base is None:
                # Editing the first prompt restarts the thread with nothing retained.
                kept.clear()
            elif isinstance(base, dict) and isinstance(base.get("end_byte_offset"), int):
                # The edited turn at the end of the continued file was replaced.
                for index in range(len(kept) - 1, -1, -1):
                    if kept[index][0] == base.get("thread_id"):
                        del kept[index + 1:]
                        kept[index][2] = base["end_byte_offset"]
                        break
            kept.append([UUID.search(transcript_stem(path)).group().lower(), path, None])
        return [(path, limit) for _, path, limit in kept]

    def lines(self):
        no = 0
        for path, limit in self.segments:
            with (open_zstd if path.suffix == ".zst" else open)(path, "rb") as stream:
                read = 0
                for line in stream:
                    # A writer may still be appending the last record.
                    if not line.endswith(b"\n") or (limit is not None and read >= limit):
                        break
                    read += len(line)
                    no += 1
                    yield no, line

    def records(self, *, start=1, stop=None, tolerant=False, stats=None, warn=True):
        warned = False
        for no, line in self.lines():
            if no < start:
                continue
            if stop and no > stop:
                break
            try:
                record = json.loads(line)
                validate_record(record)
            except ValueError as e:
                if not tolerant:
                    raise ValueError(f"{self.path}:{no}: {e}") from e
                if stats is not None:
                    stats.skip(no)
                if warn and not warned:
                    print(f"{self.path}:{no}: skipping malformed lines ({e})", file=sys.stderr)
                    warned = True
                continue
            yield no, line, record


    def metadata(self):
        return meta(self)

    def child_sessions(self):
        return children(self)

    def view_events(self, record, **options):
        yield from events(self.source, record, **options)

    @cached_property
    def snapshot_line(self):
        """Use replacement history only when no original conversation items survive."""
        if self.source != "codex":
            return None
        latest = None
        for number, _, record in self.records(tolerant=True, warn=False):
            if visible_codex_item(codex_item(record)):
                return None
            if record.get("type") == "compacted":
                payload = record.get("payload") or {}
                history = payload.get("replacement_history") if isinstance(payload, dict) else None
                if isinstance(history, list) and any(visible_codex_item(item) for item in history):
                    latest = number
        return latest


    def conversation_records(self, start=None, stop=None, **options):
        snapshot = self.snapshot_line
        for no, line, record in self.records(start=start[0] if start else 1,
                                              stop=stop[0] if stop else None, **options):
            if no != snapshot:
                pos = (no, 0)
                if (not start or start <= pos) and (not stop or pos <= stop):
                    yield pos, line, record
                continue
            for index, item in enumerate(record["payload"]["replacement_history"], 1):
                pos = (no, index)
                if (start and pos < start) or (stop and pos > stop) or not visible_codex_item(item):
                    continue
                try:
                    validate_record(item)
                except ValueError as exc:
                    raise ValueError(f"{self.path}:{no}: invalid snapshot item {index}: {exc}") from exc
                yield pos, line, item


def desktop_session_dir():
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        root = Path(appdata) / "Claude"
    else:
        root = Path.home() / "Library" / "Application Support" / "Claude"
    return root / "claude-code-sessions"


def desktop_rewinds():
    """Collect replaced CLI IDs from Desktop rewind edges, preserving every current chat."""
    directory = desktop_session_dir()
    if directory is None:
        return {}, {}
    current = set()
    owners = {}
    links = {}
    for path in directory.glob("*/*/local_*.json"):
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        active = record.get("cliSessionId")
        if not isinstance(active, str) or not UUID.fullmatch(active):
            continue
        current.add(active.lower())
        edges = record.get("rewindEdges")
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            # Either endpoint can be inactive after Undo rewind.
            for key in ("parent", "child"):
                endpoint = edge.get(key)
                if isinstance(endpoint, str) and UUID.fullmatch(endpoint):
                    owners[endpoint.lower()] = active.lower()
            child, parent, point = (edge.get(k) for k in ("child", "parent", "forkPoint"))
            if all(isinstance(v, str) and UUID.fullmatch(v) for v in (child, parent, point)):
                links[child.lower()] = (parent.lower(), point.lower())
    return {old: owner for old, owner in owners.items() if old not in current}, links


def session_root(source):
    variable = "CLAUDE_CONFIG_DIR" if source == "claude" else "CODEX_HOME"
    return Path(os.getenv(variable, Path.home() / f".{source}"))


def inventory(source=None, *, include_rewound=False):
    patterns = {"claude": ["projects/*/*.jsonl"], "codex": [
        "sessions/**/rollout-*.jsonl*", "archived_sessions/**/rollout-*.jsonl*"]}
    found = {}
    for kind in patterns:
        if source and kind != source:
            continue
        hidden, links = desktop_rewinds() if kind == "claude" and not include_rewound else ({}, {})
        threads = {}
        for pattern in patterns[kind]:
            for path in session_root(kind).glob(pattern):
                name = transcript_stem(path)
                # A Codex continuation is named THREAD_SEGMENT; a fork has its own thread ID.
                match = FIRST_UUID.search(name) if UUID.search(name) else None
                if match and path.name.endswith((".jsonl", ".jsonl.zst")):
                    files = threads.setdefault(match.group().lower(), {})
                    modified = path.stat().st_mtime
                    if name not in files or files[name][1] < modified:
                        files[name] = (path, modified)
        for session_id, files in threads.items():
            parts = tuple(path for _, (path, _) in sorted(files.items()))
            aliases = tuple(UUID.search(name).group().lower() for name in sorted(files))[1:]
            found[f"{kind}:{session_id}"] = Session(
                kind, session_id, parts[0], max(modified for _, modified in files.values()),
                parts if len(parts) > 1 else (), aliases)
        if hidden:
            from .rewind import RewindSession
            for key, session in list(found.items()):
                if session.source != "claude":
                    continue
                if session.id in hidden:
                    continue
                lineage = []
                seen = {session.id}
                ancestor = session.id
                while ancestor in links:
                    ancestor, point = links[ancestor]
                    predecessor = found.get("claude:" + ancestor)
                    if not predecessor or ancestor in seen:
                        break
                    seen.add(ancestor)
                    lineage.append((predecessor, point))
                if lineage:
                    found[key] = RewindSession(session, lineage)
            for ident in hidden:
                found.pop("claude:" + ident, None)
    return sorted(found.values(), key=lambda x: x.modified, reverse=True)


@cache
def codex_titles():
    index = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "session_index.jsonl"
    titles = {}
    if index.is_file():
        with index.open() as stream:
            for line in stream:
                if line.endswith("\n"):
                    row = json.loads(line)
                    titles[row.get("id")] = row.get("thread_name", "")
    return titles


def children(s):
    directory = s.path.parent / s.id / "subagents"
    return [Session("claude", f"{s.id}/{p.relative_to(directory).with_suffix('').as_posix()}", p, p.stat().st_mtime)
            for p in sorted(directory.rglob("agent-*.jsonl"))]


def resolve(sessions, key, *, live_claude=False):
    parent, sep, child = key.partition("/")
    parent = parent.lower()
    matches = [s for s in sessions if (s.key if ":" in parent else s.id).startswith(parent)]
    if not matches and ":" not in parent:
        # The ID of one continuation file opens the thread it belongs to.
        matches = [s for s in sessions if any(a.startswith(parent) for a in getattr(s, "aliases", ()))]
    if len(matches) == 1 and sep:
        matches = [s for s in matches[0].child_sessions() if s.id.partition("/")[2].startswith(child)]
    if not matches and live_claude:
        owners, _ = desktop_rewinds()
        targets = {owner for old, owner in owners.items() if old.startswith(parent.removeprefix("claude:"))}
        if len(targets) == 1:
            raise ValueError(f"{key!r} was superseded; use current chat {targets.pop()} or --include-rewound")
    if len(matches) != 1:
        raise ValueError(f"{len(matches)} sessions match {key!r}; use list or a longer ID")
    return matches[0]


@cache
def desktop_titles():
    """Map each chat's active transcript ID to the title Claude Desktop shows for it."""
    directory = desktop_session_dir()
    titles = {}
    for path in directory.glob("*/*/local_*.json") if directory else ():
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            active, title = record.get("cliSessionId"), record.get("title")
            if isinstance(active, str) and isinstance(title, str) and title:
                titles[active.lower()] = title
    return titles


def latest_title(path):
    """Return the newest title in a Claude transcript; forks begin with the parent's title."""
    try:
        with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for key in ("customTitle", "aiTitle"):
                needle = b'"' + key.encode() + b'"'
                end = len(data)
                while True:
                    hit = data.rfind(needle, 0, end)
                    if hit < 0:
                        break
                    begin = data.rfind(b"\n", 0, hit) + 1
                    stop = data.find(b"\n", hit)
                    try:
                        record = json.loads(data[begin:stop]) if stop >= 0 else None
                    except ValueError:
                        record = None
                    # The key can also occur inside quoted conversation text.
                    if isinstance(record, dict) and isinstance(record.get(key), str) and record[key]:
                        return record[key]
                    end = begin
    except (OSError, ValueError):
        pass
    return ""


def meta(s):
    project = parent = ""
    title = codex_titles().get(s.id, "") if s.source == "codex" else ""
    for no, _, r in s.records(tolerant=True):
        if r.get("type") == "session_meta":
            p = r["payload"]
            project = p.get("cwd", "")
            title = codex_titles().get(s.id, "")
            # history_base can name one continuation file of the parent thread.
            parent = p.get("forked_from_id") or (p.get("history_base") or {}).get("thread_id")
            break
        project = project or r.get("cwd", "")
        if not project and s.source == "codex" and r.get("type") == "message" and r.get("role") == "user":
            for _, text in events("codex", r):
                if text.lstrip().startswith("<environment_context>"):
                    match = re.search(r"<cwd>([^<]+)</cwd>", text)
                    if match:
                        project = match.group(1)
                        break
        title = title or r.get("customTitle") or r.get("aiTitle", "")
        if (project and title) or no >= 32:
            break
    if s.source == "claude" and s.path.suffix == ".jsonl":
        # Desktop renames are not always written back to the transcript.
        title = desktop_titles().get(s.id) or latest_title(s.path) or title
    extra = {}
    if "/" in s.id:
        parent = s.id.split("/")[0]
        sidecar = s.path.with_suffix(".meta.json")
        if sidecar.exists():
            try:
                metadata = json.loads(sidecar.read_bytes())
                if not isinstance(metadata, dict):
                    raise ValueError("expected a metadata object")
                title = metadata.get("description") or title
                extra["agent_type"] = metadata.get("agentType")
            except ValueError as e:
                print(f"{sidecar}: {e}", file=sys.stderr)
        if not title:
            for _, _, record in s.records(tolerant=True):
                prompt = next(events("claude", record, outline=True), None)
                if prompt:
                    extra["prompt_preview"] = prompt[1]
                    break
    elif s.source == "claude":
        extra["child_count"] = len(children(s))
    return dict(source=s.source, session=s.id, project=project, title=title, parent=parent,
                modified=datetime.fromtimestamp(s.modified, timezone.utc).isoformat(), **extra)
