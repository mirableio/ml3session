"""Convert Claude and Codex records into readable conversation and tool events."""
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

def stringify(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


IMAGE_NOTE = re.compile(r"^\[Image: source: (.+?)(?:, original |\]$)")
CLAUDE_SHELL_BLOCK = re.compile(r"<(bash-input|bash-stdout|bash-stderr)>.*?</\1>", re.DOTALL)
DATA_IMAGE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def existing_image(value, cwd):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value.startswith("file://"):
        url = urlsplit(value)
        if url.netloc not in ("", "localhost"):
            return None
        value = unquote(url.path)
    path = Path(value).expanduser()
    if not path.is_absolute():
        if not cwd:
            return None
        path = Path(cwd) / path
    try:
        return str(path) if path.is_file() else None
    except (ValueError, OSError):
        return None


def without_images(value, cwd, paths, hint=None):
    if isinstance(value, list):
        cleaned = []
        for index, item in enumerate(value):
            sibling = value[index + 1] if index + 1 < len(value) else None
            note = IMAGE_NOTE.match(sibling.get("text") or "") if isinstance(sibling, dict) else None
            cleaned.append(without_images(item, cwd, paths, note.group(1) if note else hint))
        return cleaned
    if isinstance(value, dict):
        kind = value.get("type")
        if kind in ("image", "input_image", "output_image", "image_url", "local_image"):
            source = value.get("source") or {}
            if not isinstance(source, dict):
                source = {}
            url = value.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            for candidate in (value.get("path"), source.get("path"), value.get("file_path"),
                              source.get("url"), url, hint):
                path = existing_image(candidate, cwd)
                if path:
                    paths.append(path)
                    return f"[image: {path}]"
            return f"[{source.get('media_type') or value.get('mimeType') or 'image'}; no local file]"
        return {k: without_images(v, cwd, paths, hint) for k, v in value.items()}
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")) and '"image"' in value:
        try:
            decoded = json.loads(value)
        except ValueError:
            pass
        else:
            filtered = without_images(decoded, cwd, paths, hint)
            if filtered != decoded:
                return stringify(filtered)
    if isinstance(value, str) and "data:image/" in value:
        return DATA_IMAGE.sub("[embedded image]", value)
    return value


def tool_text(block, role, cwd, full, hint=None):
    paths = []
    cleaned = without_images(block, cwd, paths, hint)
    if full:
        return stringify(cleaned)
    identifier = block.get("tool_use_id") or block.get("call_id") or block.get("id") or ""
    name = block.get("name") or block.get("type", role)
    content = next((cleaned[k] for k in ("output", "content", "input", "arguments")
                    if k in cleaned), cleaned)
    text = stringify(content)
    preview = text if len(text) <= 320 else f"{text[:160]} … [{len(text)} chars] … {text[-160:]}"
    if cleaned.get("is_error") or cleaned.get("status") in ("failed", "error"):
        name += " ERROR"
    missing = [path for path in dict.fromkeys(paths) if path not in preview]
    return " ".join(filter(None, (name, identifier, preview, *missing)))


def blocks(role, content, cwd, full, hint=None, include_tools=True):
    if isinstance(content, str):
        yield role, content
        return
    for index, b in enumerate(content or []):
        t = b.get("type", "")
        if t in ("text", "input_text", "output_text"):
            yield role, b.get("text", "")
        elif t in ("tool_use", "tool_result"):
            if include_tools:
                yield t, tool_text(b, t, cwd, full, hint)
        elif t not in ("thinking", "redacted_thinking"):
            sibling = content[index + 1] if index + 1 < len(content) else {}
            note = IMAGE_NOTE.match(sibling.get("text") or "")
            cleaned = without_images(b, cwd, [], note.group(1) if note else hint)
            yield role, cleaned if isinstance(cleaned, str) else f"[{t} attachment]"


def queued_prompt(record):
    attachment = record.get("attachment")
    if (record.get("type") == "attachment" and isinstance(attachment, dict)
            and attachment.get("type") == "queued_command" and attachment.get("commandMode") == "prompt"):
        origin = attachment.get("origin") or {}
        kind = origin.get("kind") if isinstance(origin, dict) else None
        if kind == "human":
            return "user", attachment.get("prompt")
        if kind == "peer":
            return "peer", attachment.get("prompt")
        if not kind:
            return "unknown", attachment.get("prompt")
    return None


def codex_item(record):
    return record.get("payload") if record.get("type") == "response_item" else record


def visible_codex_item(item):
    if not isinstance(item, dict):
        return False
    kind = item.get("type") or ""
    return ((kind == "message" and item.get("role") in ("user", "assistant"))
            or kind.endswith(("_call", "_call_output")) or kind == "tool_search_output")


def events(source, r, full=False, outline=False, cwd=None, include_tools=True):
    cwd = r.get("cwd") or cwd
    if source == "claude":
        role = r.get("type")
        if r.get("isCompactSummary") or r.get("isMeta"):
            return
        queued = queued_prompt(r)
        if queued:
            role, content = queued
            msg = {"content": content}
        elif role in ("user", "assistant"):
            msg = r.get("message", {})
        else:
            return
        result = r.get("toolUseResult")
        file = result.get("file") if isinstance(result, dict) else None
        hint = file.get("filePath") if isinstance(file, dict) else None
    else:
        msg = codex_item(r)
        if not visible_codex_item(msg):
            return
        role = msg.get("role")
        t = msg.get("type", "")
        if t != "message":
            if include_tools and not outline and (t.endswith(("_call", "_call_output")) or t == "tool_search_output"):
                role = "tool_result" if t.endswith("output") else "tool_use"
                block = {k: v for k, v in msg.items() if k != "internal_chat_message_metadata_passthrough"}
                yield role, tool_text(block, role, cwd, full)
            return
        if role not in ("user", "assistant"):
            return
        hint = None
    content = msg.get("content")
    if outline:
        if role != "user":
            return
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content or [] if b.get("type") in ("text", "input_text"))
        text = re.sub(r"^\s*<system-reminder>\s*Linked sessions now\b.*?</system-reminder>\s*",
                      "", text, flags=re.DOTALL)
        if source == "claude":
            text = CLAUDE_SHELL_BLOCK.sub(" ", text)
        if text.strip() in ("[Request interrupted by user]", "[Request interrupted by user for tool use]"):
            return
        if text.strip() and not text.lstrip().startswith(("<environment_context>", "<external_codex_apps_",
                                                          "<task-notification>", "<local-command-", "<command-name>", "# AGENTS.md instructions")):
            yield role, " ".join(text.split())[:240]
    else:
        yield from blocks(role, content, cwd, full, hint, include_tools)
