import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile


class BackupTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = dict(os.environ, HOME=str(self.root), APPDATA=str(self.root),
                        CLAUDE_CONFIG_DIR=str(self.root / "claude"), CODEX_HOME=str(self.root / "codex"),
                        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        self.id = "00000000-0000-4000-8000-000000000001"

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, "-m", "ml3session", *args, "--json"],
                              env=self.env, cwd=self.root, check=check, capture_output=True, text=True)

    def test_claude_trace_and_native_restore(self):
        path = self.root / "claude/projects/demo" / (self.id + ".jsonl")
        path.parent.mkdir(parents=True)
        record = {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "Recorded explanation"},
            {"type": "text", "text": "Decision"},
            {"type": "tool_result", "content": [{"type": "image", "source": {"type": "base64", "data": "IMAGE_BYTES"}}]}]}}
        prefix = json.dumps(record) + "\n"
        path.write_text(prefix + '{"partial":')
        out = self.root / "trace.zip"
        self.cli("claude", "backup", str(out))
        with ZipFile(out) as archive:
            self.assertIsNone(archive.testzip())
            entry, = json.loads(archive.read("manifest.json"))["sessions"]
            trace = archive.read(entry["file"]).decode()
            self.assertIn("Recorded explanation", trace)
            self.assertIn("Decision", trace)
            self.assertNotIn("IMAGE_BYTES", trace)
        self.assertNotEqual(self.cli("claude", "backup", str(out), check=False).returncode, 0)
        result = json.loads(self.cli("claude", "backup", "--native").stdout)
        with ZipFile(result["archive"]) as archive:
            self.assertEqual(archive.read("claude/projects/demo/" + path.name).decode(), prefix)
            archive.extractall(self.root / "restored")
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "restored/claude")
        self.assertIn("Decision", self.cli("claude", "read", self.id).stdout)

    def test_codex_trace_and_native_database(self):
        root = self.root / "codex"
        path = root / "archived_sessions" / ("rollout-" + self.id + ".jsonl")
        path.parent.mkdir(parents=True)
        records = [{"type": "response_item", "payload": {"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "Recorded summary"}], "encrypted_content": "OPAQUE"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Question"}}]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        (root / "auth.json").write_text("SECRET")
        db = sqlite3.connect(root / "state_1.sqlite")
        self.addCleanup(db.close)
        db.execute("pragma journal_mode=wal")
        db.execute("create table titles (name)")
        db.execute("insert into titles values ('example')")
        db.commit()
        for mode in ("trace", "native"):
            dest = self.root / (mode + ".zip")
            self.cli("codex", "backup", str(dest), *(["--native"] if mode == "native" else []))
            with ZipFile(dest) as archive:
                self.assertIsNone(archive.testzip())
                self.assertNotIn("codex/auth.json", archive.namelist())
                entry, = json.loads(archive.read("manifest.json"))["sessions"]
                content = archive.read(entry["file"]).decode()
                if mode == "trace":
                    self.assertIn("Recorded summary", content)
                    self.assertNotIn("OPAQUE", content)
                else:
                    self.assertEqual(content, path.read_text())
                    restored = self.root / "restored"
                    archive.extractall(restored)
                    read_db = sqlite3.connect(restored / "codex/state_1.sqlite")
                    self.addCleanup(read_db.close)
                    self.assertEqual(read_db.execute("select name from titles").fetchone(), ("example",))
                    self.env["CODEX_HOME"] = str(restored / "codex")
                    self.assertIn("Question", self.cli("codex", "read", self.id).stdout)

    def test_trace_archive_is_readable_without_live_source(self):
        path = self.root / "claude/projects/demo" / (self.id + ".jsonl")
        path.parent.mkdir(parents=True)
        rows = [{"type": "user", "message": {"content": "Find decision"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "thinking", "thinking": " "}, {"type": "thinking", "thinking": "Because X"},
                    {"type": "text", "text": "Choose Y"}]}}]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        child_dir = path.with_suffix("") / "subagents"
        child_dir.mkdir(parents=True)
        (child_dir / "agent-abc.jsonl").write_text(json.dumps(rows[0]) + "\n")
        dest = self.root / "history.zip"
        self.cli("claude", "backup", str(dest))
        with ZipFile(dest) as archive:
            data = archive.read(f"traces/{self.id}.jsonl")
            self.assertEqual(sum(b'"role": "thinking"' in line for line in data.splitlines()), 1)
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "empty")
        def archived(*args):
            return list(map(json.loads, self.cli("claude", *args, "--archive", str(dest)).stdout.splitlines()))
        self.assertEqual(archived("list")[0]["session"], self.id)
        self.assertEqual(archived("list", "--children", self.id)[0]["session"], self.id + "/agent-abc")
        hit, = archived("search", "Choose Y")
        self.assertEqual([r["text"] for r in archived("read", self.id, "--at", hit["cursor"])[:-1]],
                         ["Because X", "Choose Y"])
        page = archived("read", self.id, "--max-chars", "1")
        self.assertTrue(page[-1]["has_more"])
        self.assertEqual(archived("read", self.id, "--after", page[-1]["next_cursor"])[0]["text"], "Because X")
        self.assertEqual(archived("read", self.id, "--last", "1")[0]["text"], "Find decision")

    def test_no_hardlink_publication_fallback(self):
        from ml3session.backup import publish_zip
        from unittest.mock import patch
        import errno
        temp = self.root / "finished"
        output = self.root / "final.zip"
        temp.write_bytes(b"finished archive")
        with patch("ml3session.backup.os.link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with patch("ml3session.backup.os.replace", side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    publish_zip(temp, output)
            self.assertFalse(output.exists())
            publish_zip(temp, output)
            self.assertEqual(output.read_bytes(), b"finished archive")
            temp.write_bytes(b"new archive")
            with self.assertRaises(FileExistsError):
                publish_zip(temp, output)
            self.assertEqual(output.read_bytes(), b"finished archive")
