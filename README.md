# ml3session

Suppose you debugged a failing test with Claude Code yesterday. Today you want a second opinion from Codex. With `ml3session`, Codex can read what you asked, what Claude tried, and what the test printed. You can also ask Claude to look at a Codex conversation, or read either conversation yourself in a terminal.

A **session** is one saved conversation. The CLI reads session files on the computer where it runs. To use it from an agent, that agent needs shell access to the command and read access to the saved conversation.

## Install and try it

Get a copy of this repository and open a terminal in the folder containing `pyproject.toml`. With uv installed, run:

```sh
uv tool install .
ml3session --help
```

The CLI supports Python 3.9 and newer. If your shell cannot find the installed command, run `uv tool update-shell` and open a new terminal. You can also run it from the repository with `uv run ml3session --help`. If you use pip, activate a Python virtual environment and run `python -m pip install .` there.

**Try one conversation.** List your three most recently active Claude Code sessions:

```sh
ml3session claude list --limit 3
```

If your previous conversation was in Codex, use `ml3session codex list --limit 3`. The source always names the app that **saved** the conversation. If the list is empty, check that you've used that app on this machine under the same user account.

Copy the value after `session=` from a row you recognize. Then give the reviewing agent this prompt, replacing `SESSION_ID` with that value and `claude` with `codex` if appropriate:

> Use `ml3session claude read SESSION_ID --last 1` to read from my most recent prompt onward. Follow any remaining pages and tell me what we tried, what worked, and what is still open.

That is enough for a first handoff. You can change the question to suit the job. If you prefer to try it yourself, run `ml3session claude read SESSION_ID` in the terminal to see the first page from the beginning of the conversation.

## Find a session from a clue

Use `list` when you remember the working directory or chat title. Use `search` when you remember a phrase from the conversation. Replace the example phrases with your own:

```sh
ml3session claude list --project my-app
ml3session codex list --title "login bug"
ml3session claude search "connection refused"
```

A search hit gives you the session ID, timestamp, speaker, matching excerpt, and a **cursor** for the record containing the match. Search is literal and case-insensitive. Add `--tools` to search recorded tool inputs and outputs. Both `list` and `search` visit recently modified sessions first and return up to 20 rows by default. Use `--limit N` to change that.

When Desktop rewind metadata is available, list shows the current chat. Reads and searches on that chat follow its recorded rewind points and restore retained ancestor messages across compaction. Copied message UUIDs appear once; discarded turns stay in their physical transcript. Searching an earlier retained message returns a cursor you can open against the current chat. Separate forks remain discoverable. To inspect an old version explicitly, add `--include-rewound` to `list`, `search`, or `read`. Without Desktop metadata, discovery falls back to the transcript files.

Claude search includes subagents. A child hit gives you its own session ID and the parent ID, so you can read the delegated work or return to the main conversation. To browse a parent's agents, use `ml3session claude list --children SESSION`. To search only top-level chats, add `--no-children`.

## Read the exchange

If you already have a session ID, start here. In the examples below, replace `SESSION` with the returned ID or a unique prefix, and copy cursors verbatim.

```sh
ml3session claude read SESSION --outline
ml3session claude read SESSION --since PROMPT_CURSOR
```

`--last 1` starts at the latest prompt listed in the outline. Use `--last 2` when the latest prompt is a follow-up and you need the preceding exchange. If fewer than N prompts exist, the read starts at the earliest available prompt; a session with no matching prompts yields an empty page. Once you have reviewed a page, use its cursor for subsequent reads.

The outline shows user prompt excerpts with timestamps and cursors. Claude Code shell commands and their output stay visible in the exchange. Only the written request outside those shell blocks counts toward `--last`. Pick a prompt and use `--since` to read it together with the replies and tool activity that followed. You can also start at a search hit. To stop at a particular record, add `--until END_CURSOR`, which includes that record. If you know the time window, use `--from` and `--to` with timezone-qualified timestamps, such as `2000-01-01T00:00:00Z`. Both time boundaries are inclusive.

Conversation text is preserved. Long tool calls and results show their beginning and end. If a preview omits a detail you need, expand that one record:

```sh
ml3session claude read SESSION --at RESULT_CURSOR --tools full
```

Image blocks show a local pathname when the transcript identifies an existing file, or a placeholder otherwise. An agent can open the file if the image matters. Referenced artifacts are read separately.

## Continue where you left off

Every command accepts `--json` for JSON Lines. The last row of a successful read is a footer. When `has_more` is true, copy its `next_cursor` and request the next page:

```sh
ml3session claude read SESSION --after NEXT_CURSOR --json
```

`--after` resumes immediately after that record. Pages default to 32,000 rendered characters and keep records whole, so one large record can exceed the budget. Use `--max-chars N` to adjust the page size or `--unlimited` for a full export.

For “check what happened since last time,” **keep the source, session ID, and last cursor you finished reading in the reviewing chat**. Once you reach `has_more: false`, that cursor is your starting point for future additions. An outline's cursor marks a prompt you located. Save your progress from the actual exchange you read. Each reviewing chat can keep its own place.

## Back up sessions

Run either command once per snapshot. With no output path, it writes a timestamped ZIP in the current directory. An explicit path must have an existing parent directory. Existing backups are never overwritten.

```sh
ml3session claude backup
ml3session codex backup /path/to/codex-traces.zip
```

The default **trace ZIP** contains one readable JSON Lines file per discovered transcript and a `manifest.json` mapping session IDs to files, projects, and titles. It includes archived Codex sessions, Claude subagents, and pre-rewind transcripts. Rows preserve conversation text, available recorded Claude thinking or Codex reasoning summaries, and compact tool previews. Images use references or placeholders; external files and encrypted reasoning payloads are omitted. The manifest lists any malformed lines skipped. Unzip it anywhere to read the traces without either app. You can also read the ZIP directly, even on a machine without the original sessions:

```sh
ml3session claude list --archive history.zip
ml3session claude search "connection refused" --archive history.zip
ml3session claude read SESSION --archive history.zip --last 1
```

Use the source that produced the ZIP. Archive commands use the physical transcripts and tool detail captured at backup time. The manifest identifies the original sessions, and archive search hits can be reopened with archive cursors. Thinking text is available only when the source saved readable text; signature-only blocks do not contribute rows.

For **original transcripts** add `--native`. This can produce a much larger ZIP because original files retain embedded artifacts. Claude archives include available Desktop chat mappings and subagent sidecars. Codex archives include the session index and consistent SQLite copies of the available thread-history and state databases. Separate tool-result files, source repositories, authentication files and app caches are outside the backup scope. Each input is captured separately; for a single quiet point in time, close the source app before running the backup.

To check a native restore, extract into an empty directory, then point the reader at the extracted source folder:

```sh
CLAUDE_CONFIG_DIR=/path/to/extracted/claude ml3session claude list
CODEX_HOME=/path/to/extracted/codex ml3session codex list
```

The `claude/` and `codex/` trees mirror their respective configuration directories. If present, `desktop/` mirrors Claude Desktop’s separate `claude-code-sessions` directory. Reopening restored chats in the original app also depends on that app’s version, account, and workspace. Each command creates one full ZIP; scheduling and retention are separate.

## Local history and incomplete transcripts

- Claude Code history comes from `~/.claude/projects`. Set `CLAUDE_CONFIG_DIR` to use a different Claude configuration directory. Codex history comes from active and archived rollouts under `~/.codex`, or `CODEX_HOME` if set. Compressed Codex rollouts are supported.
- Claude messages queued while a turn is running appear in conversation order. Queued prompts marked as human count toward `--outline` and `--last`. Older queued prompts with no sender label appear as `unknown` in reads and search.
- Older Codex rollouts can lack message timestamps. Their rows show `null` in JSON and can be selected by cursor. Time filters include only timestamped rows.
- When a Codex transcript contains only retained compaction history, the CLI reads the latest available conversation snapshot. Those rows carry `history_source: "compacted"` in JSON and a cursor for each snapshot item. Original message times may be unavailable.
- Each read follows one transcript in recorded order. For a Codex fork, earlier turns may live in the parent session listed by `list`.
- When you edit or resend a prompt, Codex continues the same chat in a further rollout file. The CLI reads those files as one session, without the turn the edit replaced. The ID of a continuation file also opens its chat. A fork has its own session ID and stays separate.
- If a transcript contains malformed lines, the CLI warns on stderr. The read footer gives their count in `skipped_lines` and their inclusive, one-based ranges in `skipped_line_ranges`. Use `--strict` when missing evidence should fail the read.
- Cursors identify a session, physical line, and content digest. A changed or missing cursor causes an error. Discard partial output from a failed command and locate the exchange again with `search` or `--outline`.
