import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReadTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=temp.name, CODEX_HOME=temp.name,
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        self.id = "00000000-0000-4000-8000-000000000001"
        self.log = self.root / "projects" / "demo" / (self.id + ".jsonl")
        self.log.parent.mkdir(parents=True)
        self.image = self.root / "local screenshot.png"
        self.image.touch()

    def write(self, *records):
        with self.log.open("w") as out:
            for record in records:
                print(record if isinstance(record, str) else json.dumps(record), file=out)

    def run_cli(self, *args, source="claude", check=True):
        return subprocess.run([sys.executable, "-m", "ml3session", source, *args, "--json"], env=self.env,
                              text=True, capture_output=True, check=check)

    def rows(self, *args, **kwargs):
        return [json.loads(line) for line in self.run_cli(*args, **kwargs).stdout.splitlines()]

    def test_images_paging_and_outline(self):
        image = {"type": "image", "source": {"type": "base64", "data": "SENSITIVE_IMAGE_BYTES"}}
        note = {"type": "text", "text": f"[Image: source: {self.image}, original 100x100]"}
        tool = {"type": "tool_result", "tool_use_id": "one", "content": [image, note]}
        self.write({"type": "user", "message": {"content": "question"}},
                   {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
                   {"type": "user", "message": {"content": [tool, dict(tool, tool_use_id="two")]}})
        outline = self.rows("read", self.id, "--outline")
        self.assertEqual([row["text"] for row in outline[:-1]], ["question"])
        for mode in ("compact", "full"):
            first = self.rows("read", self.id, "--since", outline[0]["cursor"],
                              "--max-chars", "1", "--tools", mode)
            self.assertTrue(first[-1]["has_more"])
            page = self.rows("read", self.id, "--after", first[-1]["next_cursor"],
                             "--max-chars", "1", "--tools", mode)
            self.assertEqual(len(page[:-1]), 2)
            self.assertFalse(page[-1]["has_more"])
            for row in page[:-1]:
                self.assertIn(str(self.image), row["text"])
                self.assertNotIn("SENSITIVE_IMAGE_BYTES", row["text"])

    def test_corruption_outside_range(self):
        self.write("{bad", "bad}", {"type": "user", "message": {"content": "find me"}}, "bad")
        search = self.run_cli("search", "find me")
        self.assertEqual(len(search.stderr.splitlines()), 1)
        cursor = json.loads(search.stdout)["cursor"]
        self.assertEqual(self.rows("read", self.id, "--at", cursor)[0]["text"], "find me")
        self.assertEqual(self.rows("read", self.id)[-1]["skipped_lines"], 3)
        self.assertEqual(self.rows("read", self.id)[-1]["skipped_line_ranges"], [[1, 2], [4, 4]])
        self.assertNotEqual(self.run_cli("read", self.id, "--strict", check=False).returncode, 0)

    def test_codex_local_image_and_embedded_fallback(self):
        self.log = self.root / "sessions" / ("rollout-" + self.id + ".jsonl")
        self.log.parent.mkdir()
        self.write({"type": "session_meta", "payload": {"cwd": str(self.root), "id": self.id}},
                   {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_image", "image_url": self.image.name},
                                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}})
        rows = self.rows("read", self.id, source="codex")
        self.assertIn(str(self.image), rows[0]["text"])
        self.assertIn("no local file", rows[1]["text"])
        self.assertNotIn("AAAA", rows[1]["text"])

    def test_default_pages_and_verified_until(self):
        self.write(*[{"type": "user", "message": {"content": str(n) * 18000}} for n in range(3)])
        all_rows = self.rows("read", self.id, "--unlimited")
        end = all_rows[2]["cursor"]
        page = self.rows("read", self.id, "--until", end)
        self.assertEqual(page[:-1], all_rows[:1])
        self.assertTrue(page[-1]["has_more"])
        next_page = self.rows("read", self.id, "--after", page[-1]["next_cursor"],
                              "--until", end, "--unlimited")
        self.assertEqual(next_page[:-1], all_rows[1:3])
        wrong = end[:-1] + ("0" if end[-1] != "0" else "1")
        self.assertNotEqual(self.run_cli("read", self.id, "--until", wrong, check=False).returncode, 0)

    def test_children_titles_and_search_scope(self):
        self.write({"type": "custom-title", "customTitle": "Parent title"})
        directory = self.log.with_suffix("") / "subagents" / "workflows" / "wf_A"
        directory.mkdir(parents=True)
        log = directory / "agent-abcd.jsonl"
        log.write_text(json.dumps({"type": "assistant", "message": {"content": "child evidence"}}) + "\n")
        log.with_suffix(".meta.json").write_text(json.dumps({"description": "Trace the error", "agentType": "worker"}))
        (directory / "journal.jsonl").write_text("{}\n")
        self.assertEqual(self.rows("list", "--title", "PARENT")[0]["child_count"], 1)
        child, = self.rows("list", "--children", self.id)
        self.assertEqual(child["title"], "Trace the error")
        self.assertEqual(child["agent_type"], "worker")
        self.assertEqual(self.rows("search", "child evidence", "--no-children"), [])
        hit, = self.rows("search", "child evidence")
        self.assertEqual(hit["parent"], self.id)
        self.assertEqual(hit["session"], child["session"])
        self.assertEqual(self.rows("read", child["session"], "--at", hit["cursor"])[0]["text"], "child evidence")

    def test_fork_and_rename_use_latest_title(self):
        quoted = {"type": "user", "message": {"content": 'mentions {"customTitle": "Quoted"} in text'}}
        self.write({"type": "custom-title", "customTitle": "Parent chat"},
                   {"type": "ai-title", "aiTitle": "Generated"}, quoted,
                   {"type": "custom-title", "customTitle": "Forked chat"},
                   {"type": "ai-title", "aiTitle": "Generated"}, quoted)
        self.assertEqual(self.rows("list")[0]["title"], "Forked chat")
        self.assertEqual(self.rows("list", "--title", "parent"), [])
        self.write({"type": "ai-title", "aiTitle": "First"}, quoted, {"type": "ai-title", "aiTitle": "Second"})
        self.assertEqual(self.rows("list", "--title", "second")[0]["title"], "Second")
        self.env.update(HOME=str(self.root), APPDATA=str(self.root))
        base = self.root if sys.platform == "win32" else self.root / "Library" / "Application Support"
        chat = base / "Claude" / "claude-code-sessions" / "a" / "b" / "local_1.json"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"cliSessionId": self.id, "title": "Renamed in Desktop"}))
        self.assertEqual(self.rows("list")[0]["title"], "Renamed in Desktop")

    def test_codex_continuation_joins_thread_and_fork_stays_separate(self):
        def message(role, text):
            return {"type": "response_item", "payload": {"type": "message", "role": role, "content": text}}

        def rollout(name, meta, *records):
            lines = [json.dumps(r) + "\n" for r in ({"type": "session_meta", "payload": meta}, *records)]
            (self.root / "sessions" / ("rollout-" + name + ".jsonl")).write_text("".join(lines))
            return lines

        (self.root / "sessions").mkdir()
        thread, part, fork = self.id, self.id[:-1] + "2", self.id[:-1] + "3"
        first = rollout("2000-01-01T00-00-00-" + thread, {"id": thread, "cwd": "/demo"},
                        message("user", "kept question"), message("assistant", "kept answer"),
                        message("user", "interrupted prompt"))
        kept = sum(len(line) for line in first[:3])
        rollout("2000-01-02T00-00-00-" + thread + "_" + part,
                {"id": thread, "history_base": {"thread_id": thread, "end_byte_offset": kept}},
                message("user", "edited prompt"), message("assistant", "final answer"))
        rollout("2000-01-03T00-00-00-" + fork,
                {"id": fork, "forked_from_id": thread, "cwd": "/demo",
                 "history_base": {"thread_id": part, "end_byte_offset": 1}},
                message("user", "fork question"))
        listed = {row["session"]: row for row in self.rows("list", source="codex")}
        self.assertEqual(set(listed), {thread, fork})
        self.assertEqual(listed[fork]["parent"], thread)
        rows = self.rows("read", thread, source="codex")
        self.assertEqual([row["text"] for row in rows[:-1]],
                         ["kept question", "kept answer", "edited prompt", "final answer"])
        self.assertEqual(self.rows("read", part, "--last", "1", source="codex")[0]["text"], "edited prompt")
        self.assertEqual(self.rows("search", "interrupted", source="codex"), [])
        hit, = self.rows("search", "final answer", source="codex")
        self.assertEqual(hit["session"], thread)
        self.assertEqual(self.rows("read", thread, "--at", hit["cursor"], source="codex")[0]["text"], "final answer")
        self.assertEqual([row["text"] for row in self.rows("read", fork, source="codex")[:-1]], ["fork question"])
        rollout("2000-01-04T00-00-00-" + thread + "_" + self.id[:-1] + "4", {"id": thread},
                message("user", "rewritten first prompt"))
        self.assertEqual([row["text"] for row in self.rows("read", thread, source="codex")[:-1]],
                         ["rewritten first prompt"])

    def test_tool_tail_and_notifications(self):
        tool = {"type": "tool_result", "content": "begin " + "x" * 600 + " diagnostic tail"}
        self.write({"type": "user", "message": {"content": "<task-notification>done"}},
                   {"type": "user", "message": {"content": [tool]}},
                   {"type": "user", "message": {"content": "actual prompt"}})
        self.assertEqual([r["text"] for r in self.rows("read", self.id, "--outline")[:-1]], ["actual prompt"])
        self.assertEqual(self.rows("search", "diagnostic"), [])
        hit, = self.rows("search", "diagnostic", "--tools")
        result = self.rows("read", self.id, "--at", hit["cursor"])[0]["text"]
        self.assertIn("begin", result)
        self.assertIn("diagnostic tail", result)
        self.assertNotIn("x" * 321, result)

    def test_invalid_shapes_and_changed_cursor(self):
        self.write({"type": "user", "message": {"content": "valid"}})
        cursor = self.rows("read", self.id)[0]["cursor"]
        self.write({"type": "user", "message": {"content": [{"type": "text", "text": None}]}},
                   {"type": "session_meta"}, "[]")
        recovered = self.run_cli("read", self.id)
        self.assertEqual(json.loads(recovered.stdout)["skipped_lines"], 3)
        self.assertIn(str(self.log) + ":1", recovered.stderr)
        strict = self.run_cli("read", self.id, "--strict", check=False)
        self.assertEqual(strict.returncode, 2)
        self.assertNotIn("Traceback", strict.stderr)
        self.assertEqual(self.run_cli("read", self.id, "--after", cursor, check=False).returncode, 2)

    def test_mixed_reminder_and_untitled_child_preview(self):
        self.write({"type": "custom-title", "customTitle": "Parent"})
        directory = self.log.with_suffix("") / "subagents"
        directory.mkdir(parents=True)
        self.log = directory / "agent-prompt.jsonl"
        reminder = "<system-reminder>\nLinked sessions now (status): …\n</system-reminder>"
        self.write(*[{"type": "user", "message": {"content": value}}
                     for value in (reminder, "[Request interrupted by user]", reminder + "\nContinue testing.")])
        child, = self.rows("list", "--children", self.id)
        self.assertEqual(child["title"], "")
        self.assertEqual(child["prompt_preview"], "Continue testing.")
        outline = self.rows("read", child["session"], "--outline")
        self.assertEqual([r["text"] for r in outline[:-1]], ["Continue testing."])
        self.assertEqual(len(self.rows("read", child["session"])), 4)

    def test_omissions_belong_to_page_that_consumes_them(self):
        message = {"type": "user", "message": {"content": "page"}}
        self.write("bad", message, "bad", "bad", message, "bad")
        first = self.rows("read", self.id, "--max-chars", "1")[-1]
        second = self.rows("read", self.id, "--after", first["next_cursor"])[-1]
        self.assertEqual(first["skipped_line_ranges"], [[1, 1]])
        self.assertEqual(second["skipped_line_ranges"], [[3, 4], [6, 6]])

    def test_last_matches_outline_and_resumes(self):
        def message(role, text):
            return {"type": role, "message": {"content": text}}
        self.write(message("user", "Earlier question"), message("assistant", "Earlier reply"),
                   message("user", "Follow up"), message("assistant", "Result"),
                   message("user", "<task-notification>Finished"))
        outline = self.rows("read", self.id, "--outline")
        for count in (1, 2, 9):
            expected = self.rows("read", self.id, "--since", outline[-min(count, 2)-1]["cursor"])
            self.assertEqual(self.rows("read", self.id, "--last", str(count)), expected)
        page = self.rows("read", self.id, "--last", "1", "--max-chars", "1")
        self.assertEqual(page[0]["text"], "Follow up")
        self.assertTrue(page[-1]["has_more"])
        rest = self.rows("read", self.id, "--after", page[-1]["next_cursor"])
        self.assertEqual(rest[0]["text"], "Result")
        self.write(message("assistant", "No prompt here"))
        self.assertEqual(self.rows("read", self.id, "--last", "1")[-1]["records_emitted"], 0)

    def test_last_codex_and_invalid_ranges(self):
        self.log = self.root / "sessions" / ("rollout-" + self.id + ".jsonl")
        self.log.parent.mkdir()
        self.write(*[{"type": "response_item", "payload": {"type": "message", "role": role,
                     "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
                     for role, text in (("user", "Check the result"), ("assistant", "Done"),
                                        ("user", "<environment_context>generated"))])
        page = self.rows("read", self.id, "--last", "1", source="codex")
        self.assertEqual(page[0]["text"], "Check the result")
        for args in (("--last", "0"), ("--last", "1", "--until", page[0]["cursor"])):
            self.assertEqual(self.run_cli("read", self.id, *args, source="codex", check=False).returncode, 2)

    def test_claude_last_preserves_shell_evidence_and_selects_written_prompts(self):
        shell = ("<bash-input>pytest</bash-input><bash-stdout>starting\nfinished</bash-stdout>"
                 "<bash-stderr>failure</bash-stderr>")
        prompt = "Why did the test fail?"
        def user(content):
            return {"type": "user", "message": {"content": content}}

        self.write(user(prompt), user(shell), user("<bash-stdout>more output</bash-stdout>"))
        outline = self.rows("read", self.id, "--outline")
        self.assertEqual([r["text"] for r in outline[:-1]], [prompt])
        exchange = self.rows("read", self.id, "--last", "1")
        self.assertEqual(exchange[0]["cursor"], outline[0]["cursor"])
        self.assertEqual(exchange[1]["text"], shell)
        self.assertEqual(exchange[2]["text"], "<bash-stdout>more output</bash-stdout>")

        follow_up = "Explain that error."
        self.write(user(prompt), user([{"type": "text", "text": shell},
                                       {"type": "text", "text": follow_up}]))
        outline = self.rows("read", self.id, "--outline")
        self.assertEqual([r["text"] for r in outline[:-1]], [prompt, follow_up])
        exchange = self.rows("read", self.id, "--last", "1")
        self.assertEqual(exchange[0]["cursor"], outline[1]["cursor"])
        self.assertEqual([r["text"] for r in exchange[:-1]], [shell, follow_up])

    def test_queued_steering_read_search_outline_and_last(self):
        def queued(prompt, origin=None, mode="prompt"):
            attachment = {"type": "queued_command", "commandMode": mode, "prompt": prompt}
            if origin:
                attachment["origin"] = {"kind": origin}
            return {"type": "attachment", "attachment": attachment}
        self.write(queued("First correction", "human"),
                   queued([{"type": "text", "text": "Second correction"}], "human"),
                   queued("Unattributed correction"), queued("Peer text", "peer"),
                   queued("Automated", "task-notification", "task-notification"))
        rows = self.rows("read", self.id)
        self.assertEqual([(r["role"], r["text"]) for r in rows[:-1]],
                         [("user", "First correction"), ("user", "Second correction"),
                          ("unknown", "Unattributed correction"), ("peer", "Peer text")])
        outline = self.rows("read", self.id, "--outline")
        self.assertEqual([r["text"] for r in outline[:-1]], ["First correction", "Second correction"])
        self.assertEqual(self.rows("read", self.id, "--last", "1")[0]["cursor"], outline[1]["cursor"])
        peer_hit, = self.rows("search", "Peer text")
        self.assertEqual(peer_hit["role"], "peer")
        self.assertEqual(self.rows("read", self.id, "--at", peer_hit["cursor"])[0], rows[3])
        hit, = self.rows("search", "Second correction")
        self.assertEqual(self.rows("read", self.id, "--at", hit["cursor"])[0]["text"], "Second correction")

    def test_legacy_codex_and_json_lines(self):
        self.log = self.root / "sessions" / ("rollout-" + self.id + ".jsonl")
        self.log.parent.mkdir()
        separators = "café\u0085one\u2028two\u2029three"
        self.write({"id": self.id}, {"record_type": "state"},
                   {"type": "message", "role": "user", "content": [
                       {"type": "input_text", "text": "<environment_context><cwd>/demo</cwd></environment_context>"}]},
                   {"type": "message", "role": "user", "content": separators},
                   {"type": "function_call", "call_id": "one", "name": "test", "arguments": "{}"},
                   {"type": "function_call_output", "call_id": "one", "output": "ok"},
                   {"type": "reasoning", "summary": []},
                   {"type": "message", "role": "assistant", "content": "Done"})
        result = self.run_cli("read", self.id, source="codex").stdout
        self.assertIn("café", result)
        self.assertEqual(len(result.splitlines()), 6)
        rows = [json.loads(line) for line in result.splitlines()]
        self.assertEqual(rows[1]["text"], separators)
        self.assertIsNone(rows[1]["timestamp"])
        self.assertEqual([r["role"] for r in rows[:-1]], ["user", "user", "tool_use", "tool_result", "assistant"])
        self.assertEqual(self.rows("list", "--project", "demo", source="codex")[0]["project"], "/demo")
        hit, = self.rows("search", "two", source="codex")
        self.assertEqual(self.rows("read", self.id, "--at", hit["cursor"], source="codex")[0], rows[1])

    def test_snapshot_only_is_pageable_and_never_replays_over_normal_turns(self):
        self.log = self.root / "sessions" / ("rollout-" + self.id + ".jsonl")
        self.log.parent.mkdir()
        history = [{"type": "message", "role": role, "content": text}
                   for role, text in (("user", "Question"), ("assistant", "Answer"), ("user", "Next"))]
        snapshot = {"type": "compacted", "payload": {"replacement_history": history}}
        self.write(snapshot, snapshot)
        def read(*args):
            return self.rows("read", self.id, *args, source="codex")
        all_rows = read()[:-1]
        self.assertEqual([r["text"] for r in all_rows], ["Question", "Answer", "Next"])
        self.assertTrue(all(r["history_source"] == "compacted" for r in all_rows))
        hit, = self.rows("search", "Answer", source="codex")
        self.assertEqual(read("--at", hit["cursor"])[0], all_rows[1])
        page = read("--max-chars", "1", "--until", all_rows[-1]["cursor"])
        self.assertEqual(page[:-1], all_rows[:1])
        self.assertEqual(read("--after", page[-1]["next_cursor"])[:-1], all_rows[1:])
        self.assertEqual(read("--last", "1")[:-1], all_rows[-1:])
        self.write(snapshot, {"type": "response_item", "payload": history[0]})
        self.assertEqual([r["text"] for r in read()[:-1]], ["Question"])
        self.assertNotEqual(self.run_cli("read", self.id, "--at", hit["cursor"], source="codex",
                                         check=False).returncode, 0)

    def test_desktop_rewind_visibility(self):
        self.env.update(HOME=str(self.root), APPDATA=str(self.root))
        app_dir = self.root if sys.platform == "win32" else self.root / "Library/Application Support"
        metadata = app_dir / "Claude/claude-code-sessions/account/workspace"
        metadata.mkdir(parents=True)
        old = self.id
        latest = old[:-1] + "2"
        fork = old[:-1] + "3"
        for ident, text in ((old, "discarded work"), (latest, "new work"), (fork, "fork work")):
            self.log = self.log.with_name(ident + ".jsonl")
            self.write({"type": "custom-title", "customTitle": "Shared title"},
                       {"type": "user", "message": {"content": text}})
        record = {"cliSessionId": latest, "rewindEdges": [{"parent": old, "child": latest}]}
        path = metadata / ("local_" + old + ".json")
        path.write_text(json.dumps(record))
        (metadata / ("local_" + fork + ".json")).write_text(json.dumps(
            {"cliSessionId": fork, "forkedFromSessionId": "local_" + old}))
        self.assertEqual({r["session"] for r in self.rows("list")}, {latest, fork})
        self.assertEqual(self.rows("search", "discarded work"), [])
        self.assertNotEqual(self.run_cli("read", old, check=False).returncode, 0)
        self.assertEqual(self.rows("search", "discarded work", "--include-rewound")[0]["session"], old)
        self.assertEqual(self.rows("read", old, "--include-rewound")[0]["text"], "discarded work")
        record["cliSessionId"] = old
        path.write_text(json.dumps(record))
        self.assertEqual({r["session"] for r in self.rows("list")}, {old, fork})

    def test_retained_history_across_compaction_and_rewind(self):
        self.env.update(HOME=str(self.root), APPDATA=str(self.root))
        directory = self.root if sys.platform == "win32" else self.root / "Library/Application Support"
        index = directory / "Claude/claude-code-sessions/account/workspace"
        index.mkdir(parents=True)
        latest = self.id[:-1] + "2"
        point = self.id[:-1] + "3"
        def prompt(uid, previous, text):
            return {"uuid": uid, "parentUuid": previous, "type": "user", "message": {"content": text}}

        start = prompt("first", None, "Retained early decision")
        boundary = {"uuid": "boundary", "type": "system", "subtype": "compact_boundary",
                    "parentUuid": None, "logicalParentUuid": "unpersisted"}
        anchor = prompt(point, "boundary", "Shared decision")
        self.write(start, boundary, anchor, prompt("old-tail", point, "Discarded decision"))
        self.log = self.log.with_name(latest + ".jsonl")
        self.write(boundary, anchor, prompt("current-tail", point, "Current decision"))
        (index / "local_chat.json").write_text(json.dumps({"cliSessionId": latest, "rewindEdges": [
            {"parent": self.id, "child": latest, "forkPoint": point}]}))
        self.assertEqual([r["session"] for r in self.rows("list")], [latest])
        rows = self.rows("read", latest)
        self.assertEqual([r["text"] for r in rows[:-1]],
                         ["Retained early decision", "Shared decision", "Current decision"])
        hit, = self.rows("search", "Retained early")
        self.assertEqual(hit["session"], latest)
        self.assertEqual(self.rows("read", latest, "--at", hit["cursor"])[0], rows[0])
        self.assertEqual(self.rows("read", latest, "--after", hit["cursor"])[:-1], rows[1:-1])
        self.assertEqual(self.rows("search", "Discarded decision"), [])
        self.assertEqual(self.rows("read", latest, "--last", "2")[:-1], rows[1:-1])
        self.assertIn(latest, self.run_cli("read", self.id, check=False).stderr)
