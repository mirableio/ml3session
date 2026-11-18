"""Command-line arguments, usage and dispatch."""
import argparse
import os
import re
import sys

from .archive import load_archive
from .backup import backup
from .reader import instant, render, scan
from .sessions import inventory, resolve
from zipfile import BadZipFile

def main():
    guide = """Commands
  backup [ZIP]   Back up all local sessions, including archived and rewound history.
                 Default: compact decision traces. Use --native for original files.
  list           List session IDs, projects and titles, newest activity first.
  search QUERY   Find a literal phrase in conversation text, ignoring case.
                 Quote multi-word queries. Hits include an excerpt and cursor.
  read SESSION   Read messages, tool calls and results in recorded order.
                 Use a session UUID or a prefix unique within the source.

Options (place after the command)
  --archive ZIP    List, search or read the saved physical sessions in a trace ZIP.
  --json           All commands. Print one JSON object per line.
  --project TEXT   List or search. Match part of the working directory,
                   ignoring case.
  --title TEXT     List. Match part of the session title, ignoring case.
  --include-rewound  Claude commands. Include older transcripts replaced by a Desktop rewind.
  --no-children    Claude search. Restrict the default parent-and-subagent search
                   to top-level sessions. Child hits include the parent ID.
  --limit N        List or search. Return up to N sessions or hits (default 20).
  --children ID    Claude list. Show subagent IDs for a parent session.
                   Read a child using its SESSION/agent-id.
  --tools          Search. Include tool calls and results in the search.
  --tools MODE     Read. compact (default) previews long tool text; full expands it.
                   Images show an existing local path when recorded, else a marker.
  --last N         Read from the Nth most recent outline prompt through the end.
                   Use without other range selectors. Normal paging applies.
  --outline        Read. Show user prompt excerpts with timestamps and cursors.
  --since CURSOR   Read. Include the selected record and everything after it.
  --strict         Read. Fail at a malformed record; otherwise report skipped_lines and line ranges.
  --unlimited      Read. Output the entire selected range in one page.
  --max-chars N    Read. Soft page budget (default 32,000 chars, footer excluded).
                   Keeps records whole; resume with --after if has_more is true.
  --from TIME      Search or read. Include records at or after TIME.
  --to TIME        Search or read. Include records at or before TIME.
                   Use an ISO timestamp with Z or an offset such as +02:00.
  --after CURSOR   Read. Start immediately after this record.
  --until CURSOR   Read. Include this record and stop. Can pair with --after.
  --at CURSOR      Read. Show just this record. Use alone as the range selector.

Examples
  ml3session claude list --project my-app
  ml3session codex search "connection refused" --json
  ml3session codex read SESSION --last 1
  ml3session codex read SESSION --after CURSOR

Copy SESSION and CURSOR from a hit. To resume later, use the next_cursor
printed at the end of the last successful read.

Sources default to ~/.claude (CLAUDE_CONFIG_DIR) and ~/.codex (CODEX_HOME)."""
    details = {
        "backup": ("Create a ZIP of local session history.",
                   """Run from the directory where you want to save the ZIP, or give an output path.
Default filenames include the source and UTC timestamp. Existing files are never overwritten.
Trace mode keeps messages, recorded reasoning text and compact tool evidence.
Native mode keeps original transcripts and available session metadata. See README for restoration.

Examples
  ml3session claude backup
  ml3session codex backup /path/to/backup.zip --native"""),
        "list": ("Find session IDs by project and recent activity.",
                 """Results are ordered by transcript modification time, newest first.
Each row gives the source, session ID and available project, title and parent
information. Untitled Claude children show a prompt preview when available. Pass the session ID to read under the same source.

Examples
  ml3session claude list --limit 5
  ml3session codex list --project my-app --json"""),
        "search": ("Find a remembered phrase in conversation text.",
                   """Search matches literal text case-insensitively within each content block.
Sessions are searched newest-modified first. Each hit contains a session ID,
record cursor, timestamp, role and excerpt. Use read --at with the hit cursor
for its record (add --tools full for full tool text), or --after for subsequent records. No hits means empty
output and success. Use --project to narrow a large archive.

Examples
  ml3session claude search "connection refused" --project my-app
  ml3session codex search "pytest" --tools --from 2000-01-01T00:00:00Z"""),
        "read": ("Read a session or a precise part of it in recorded order.",
                 """Read user and assistant text plus recorded tool calls and results. With no
range, start at the beginning with compact tool output. Session IDs accept unique prefixes.
A cursor identifies one record, which can contain multiple content blocks.
Copy cursor values verbatim, including the source prefix.

Examples (replace SESSION and CURSOR with values from list, search or read)
  ml3session claude read SESSION --from 2000-01-01T00:00:00Z
  ml3session codex read SESSION --at CURSOR
  ml3session codex read SESSION --after CURSOR --json

Each content row carries session, cursor, timestamp, role and text. The final
row reports records_emitted (blocks), skipped_lines, skipped_line_ranges, has_more and next_cursor. For incremental
reads, save next_cursor per chat after a successful command. If nothing new
arrived, the previous cursor is returned. A stale or mismatched cursor is an
error. Discard partial output on failure.

Reads cover the selected transcript. Referenced files, separate subagent logs
and inherited Codex history may require separate reads. Internal reasoning and
telemetry are skipped; non-text message attachments are indicated by type."""),
    }
    parser = argparse.ArgumentParser(
        description="Retrieve local Claude Code or Codex conversations as context for another "
                    "chat or script. Commands only read history; you decide how to use it.",
        epilog=guide, formatter_class=argparse.RawDescriptionHelpFormatter)
    if len(sys.argv) > 1 and sys.argv[1] in details:
        parser.exit(2, f"Choose the source first: ml3session <claude|codex> {sys.argv[1]} ...\n")
    sources = parser.add_subparsers(dest="source", required=True)
    for source, label in (("claude", "Read local Claude Code sessions."),
                          ("codex", "Read local Codex sessions.")):
        parent = sources.add_parser(source, help=label, description=label)
        subs = parent.add_subparsers(dest="command", required=True)
        for cmd, (summary, examples) in details.items():
            sub = subs.add_parser(cmd, help=summary, description=summary, epilog=examples,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
            if source == "claude" and cmd != "backup":
                sub.add_argument("--include-rewound", action="store_true",
                                 help="also include superseded Claude Desktop rewind transcripts")
            sub.add_argument("--json", action="store_true", help="emit JSON Lines for scripts and agents")
            if cmd == "backup":
                sub.add_argument("zip", nargs="?", help="output ZIP path (default: timestamped file here)")
                sub.add_argument("--native", action="store_true", help="archive original transcripts and metadata")
                continue
            sub.add_argument("--archive", metavar="ZIP", help="use a trace ZIP instead of live session files")
            if cmd != "read":
                sub.add_argument("--project", help="case-insensitive substring of the working directory")
                sub.add_argument("--limit", type=int, default=20, metavar="N",
                                 help="maximum number of result rows (default: 20)")
            if cmd != "list":
                sub.add_argument("--from", dest="start", type=instant, metavar="TIME",
                                 help="include records at or after ISO time with Z or ±HH:MM offset")
                sub.add_argument("--to", dest="end", type=instant, metavar="TIME",
                                 help="include records at or before ISO time with Z or ±HH:MM offset")
            if cmd == "list":
                sub.add_argument("--title", help="case-insensitive substring of the session title")
            if cmd == "search" and source == "claude":
                sub.add_argument("--no-children", action="store_true", help="exclude Claude subagent transcripts from search")
            if cmd == "list" and source == "claude":
                sub.add_argument("--children", metavar="SESSION", help="list direct and workflow Claude subagent transcripts of this session")
            if cmd == "search":
                sub.add_argument("query", help="literal phrase; quote spaces in the shell")
                sub.add_argument("--tools", action="store_true", help="also search tool calls and results")
            if cmd == "read":
                sub.add_argument("session", help="session UUID or unique prefix from list or search")
                sub.add_argument("--tools", choices=("compact", "full"), default="compact",
                                 help="tool text detail; images use file references in either mode")
                sub.add_argument("--outline", action="store_true", help="only user prompt excerpts and cursors")
                sub.add_argument("--strict", action="store_true", help="fail on malformed records in the range")
                budget = sub.add_mutually_exclusive_group()
                budget.add_argument("--unlimited", action="store_const", dest="max_chars", const=None,
                                    help="emit all selected records in one page")
                budget.add_argument("--max-chars", type=int, metavar="N",
                                 help="soft page budget (default 32,000); whole records, footer excluded")
                sub.set_defaults(max_chars=32000)
                starts = sub.add_mutually_exclusive_group()
                starts.add_argument("--last", type=int, metavar="N",
                                    help="start at the Nth most recent user prompt; use alone as the range selector")
                starts.add_argument("--since", metavar="CURSOR", help="start at this record, inclusive")
                starts.add_argument("--after", metavar="CURSOR", help="start immediately after this record")
                group = sub.add_mutually_exclusive_group()
                group.add_argument("--until", metavar="CURSOR", help="stop after including this record")
                group.add_argument("--at", metavar="CURSOR", help="show only this record; use without range options")
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = sys.argv[1:]
    if args in (["claude"], ["codex"]):
        args.append("--help")
    a = parser.parse_args(args)
    try:
        if a.command == "backup":
            print(render(a, backup(a.source, a.zip, a.native)))
            return
        if getattr(a, "start", None) and a.end and a.start > a.end:
            raise ValueError("--to precedes --from")
        if a.archive:
            sessions = load_archive(a.archive, a.source)
        else:
            sessions = inventory(a.source, include_rewound=getattr(a, "include_rewound", False))
        live_claude = a.source == "claude" and not a.archive
        if a.command == "read":
            if a.last is not None:
                if a.last < 1:
                    raise ValueError("--last must be positive")
                if a.at or a.until or a.start or a.end:
                    raise ValueError("--last cannot be combined with another range selector")
            if a.at:
                if a.after or a.since or a.start or a.end:
                    raise ValueError("--at takes a single record and cannot be combined with ranges")
                a.until = a.at
            if a.max_chars is not None and a.max_chars < 1:
                raise ValueError("--max-chars must be positive")
            scan(a, resolve(sessions, a.session, live_claude=live_claude))
            return
        if getattr(a, "children", None):
            sessions = resolve(sessions, a.children, live_claude=live_claude).child_sessions()
            sessions.sort(key=lambda child: child.modified, reverse=True)
        if a.command == "search" and a.source == "claude" and not a.no_children:
            descendants = [child for session in sessions for child in session.child_sessions()]
            sessions = sorted(sessions + descendants, key=lambda item: item.modified, reverse=True)
        if a.limit < 1:
            raise ValueError("--limit must be positive")
        a.remaining = a.limit
        if a.command == "search":
            a.pattern = re.compile(re.escape(a.query), re.IGNORECASE)
        for s in sessions:
            if a.command == "list" or a.project:
                info = s.metadata()
                if a.project and a.project.casefold() not in info["project"].casefold():
                    continue
            if a.command == "list":
                if a.title and a.title.casefold() not in info["title"].casefold():
                    continue
                print(render(a, info))
                a.remaining -= 1
            else:
                a.remaining -= scan(a, s, True)
            if not a.remaining:
                break
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")
    except (ValueError, OSError, EOFError, BadZipFile) as e:
        parser.exit(2, f"ml3session: {e}\n")
