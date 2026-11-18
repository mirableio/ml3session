"""Select ranges, verify cursors and paginate complete transcript records."""
from collections import deque
from datetime import datetime
import hashlib
import json
import re

from .sessions import ReadStats


def cursor(s, pos, line):
    number, index = pos
    suffix = f"~{index}" if index else ""
    return f"{s.key}@{number}.{hashlib.sha256(line).hexdigest()[:16]}{suffix}"


def boundary(s, value):
    if not value:
        return None
    key, sep, token = value.partition("@")
    match = re.fullmatch(r"([1-9]\d*)\.[0-9a-f]{16}(?:~(-?[1-9]\d*))?", token)
    if key != s.key or not sep or not match:
        raise ValueError(f"invalid cursor for {s.key}: {value}")
    return int(match[1]), int(match[2] or 0)


def instant(value):
    dt = datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
    if dt.utcoffset() is None:
        raise ValueError("timestamps require a timezone (Z or ±HH:MM)")
    return dt


JSON_LINE_ESCAPES = str.maketrans({"\u0085": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"})


def render(a, obj):
    if a.json:
        return json.dumps(obj, ensure_ascii=False).translate(JSON_LINE_ESCAPES)
    elif "text" in obj:
        parent = f" parent={obj['parent']}" if obj.get("parent") else ""
        history = " compacted" if obj.get("history_source") else ""
        return f"[{obj['timestamp']} {obj['role']}{history}] {obj['cursor']}{parent}\n{obj['text']}\n"
    else:
        return "  ".join(f"{k}={v}" for k, v in obj.items() if v or v == 0)


def recent_prompt_window(session, count):
    """Find the first selected prompt and the end of this snapshot."""
    prompts = deque()
    last_line = (0, 0)
    stats = ReadStats()
    for number, line, record in session.conversation_records(tolerant=True, stats=stats):
        last_line = number
        if next(session.view_events(record, outline=True), None) is not None:
            prompts.append(cursor(session, number, line))
            if len(prompts) > count:
                prompts.popleft()
    if stats.ranges:
        last_line = max(last_line, (stats.ranges[-1][1], 0))
    return (prompts[0] if prompts else None), last_line


def scan(a, s, searching=False):
    first = getattr(a, "at", None) or getattr(a, "since", None) or getattr(a, "after", None)
    stop_token = getattr(a, "until", None)
    stop = boundary(s, stop_token)
    no_prompts = False
    if getattr(a, "last", None):
        first, stop = recent_prompt_window(s, a.last)
        no_prompts = first is None
    start = boundary(s, first)
    if start and stop and start > stop:
        raise ValueError("--until precedes the start cursor")
    expected = {n: token for n, token in ((start, first), (stop, stop_token)) if token}
    if stop_token and start == stop and first != stop_token:
        raise ValueError("conflicting cursor digests")
    last = getattr(a, "after", None)
    hits = used = 0
    more = False
    budget = getattr(a, "max_chars", None)
    cwd = s.metadata()["project"] if s.source == "codex" else None
    stats = ReadStats()
    skipped_at_cursor = 0
    for n, line, r in s.conversation_records(start=start, stop=stop,
                                tolerant=not getattr(a, "strict", False), stats=stats):
        if n in expected and cursor(s, n, line) != expected.pop(n):
            raise ValueError("cursor record changed")
        if no_prompts or (n == start and getattr(a, "after", None)):
            continue
        stamp = r.get("timestamp")
        if a.start or a.end:
            if not stamp:
                continue
            at = instant(stamp)
            if (a.start and at < a.start) or (a.end and at > a.end):
                continue
        rows = list(s.view_events(r, full=a.tools if searching else a.tools == "full",
                           outline=getattr(a, "outline", False), cwd=cwd,
                           include_tools=not searching or a.tools))
        if not rows:
            continue
        c = cursor(s, n, line)
        page_record = []
        for role, body in rows:
            if searching:
                if role.startswith("tool_") and not a.tools:
                    continue
                match = a.pattern.search(body)
                if not match:
                    continue
                body = body[max(0, match.start()-80):match.end()+100].replace("\n", " ")
            item = dict(source=s.source, session=s.id, cursor=c, timestamp=stamp, role=role, text=body)
            if n[1] > 0:
                item["history_source"] = "compacted"
            if searching and "/" in s.id:
                item["parent"] = s.id.partition("/")[0]
            row = render(a, item)
            if searching:
                print(row)
                hits += 1
                if hits >= a.remaining:
                    return hits
            else:
                page_record.append(row)
        size = sum(len(row) + 1 for row in page_record)
        if budget and hits and used + size > budget:
            more = True
            break
        for row in page_record:
            print(row)
        hits += len(page_record)
        used += size
        last = c
        skipped_at_cursor = len(stats.ranges)
    if more and expected:
        # Validate a requested end cursor beyond this page without parsing unrelated records.
        for pos, token in list(expected.items()):
            for n, line, _ in s.conversation_records(start=pos, stop=pos, tolerant=True):
                if cursor(s, n, line) != token:
                    raise ValueError("cursor record changed")
                del expected[n]
    if expected:
        raise ValueError("cursor record missing from transcript")
    if not searching:
        ranges = stats.ranges[:skipped_at_cursor] if more else stats.ranges
        print(render(a, dict(source=s.source, session=s.id, next_cursor=last,
                             records_emitted=hits, has_more=more,
                             skipped_lines=sum(end - begin + 1 for begin, end in ranges),
                             skipped_line_ranges=ranges)))
    return hits
