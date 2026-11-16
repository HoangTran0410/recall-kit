"""Tests on synthetic notes only. Run: python -m unittest discover -s tests -v"""
import contextlib
import datetime
import io
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recall_kit as rk  # noqa: E402

TODAY = datetime.date(2030, 6, 1)

DECISIONS = """# Decisions: storage

Intro paragraph, not a note.

## Decided
- Cache invalidation uses a version stamp per tenant, not timestamps (2030-03-02): clock skew broke the timestamp approach.
- Retry queue dropped: the idempotent writer made it redundant, measured 0 lost writes in a week (2030-04-11)
  continuation line that mentions the quokka migration.
* Star bullets count too: thumbnails are rendered lazily on first view.
- tiny

## Rejected
- Sharding the ledger table by region was rejected 2029-12-01, the join cost doubled.
  - nested bullet stays inside its parent
- ~~Use a bloom filter for the ledger lookup~~ replaced by the covering index.

Plain paragraph ends the bullet above.
"""

FLOW = """---
status: active
date: 2030-05-20
topic: export
tags: [csv, download]
---
# Export pipeline

How the nightly export moves rows to cold storage.

## Steps
1. Snapshot.

## Conclusion
Streaming the export in chunks of 500 rows keeps memory flat.
"""

GUIDE = """# Operator guide

General intro.

## Rotating the signing certificate
Run the rotate script, then restart the gateway. Takes two minutes.

### Detail
Nested detail belongs to the section above.

## Restoring a backup (2030-02-14)
Pick the snapshot, run restore, verify the ledger checksum.
"""


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def filler(root, n=30):
    """Unrelated notes so IDF has a corpus to work with."""
    rnd = random.Random(7)
    vocab = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "grove", "harbor", "island", "jasper"]
    for i in range(n):
        body = " ".join(rnd.choice(vocab) for _ in range(12))
        write(root, "notes/filler-%02d.md" % i, "# Filler %d\n\n%s server\n" % (i, body))


class Project(unittest.TestCase):
    config = {
        "sources": [
            {"glob": "docs/decisions/*.md", "kind": "decision", "split": "bullet"},
            {"glob": "docs/guide.md", "kind": "guide", "split": "heading"},
            {"glob": "docs/**/*.md", "kind": "doc", "split": "file"},
            {"glob": "notes/*.md", "kind": "note", "split": "file"},
        ],
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        write(self.root, "docs/decisions/storage.md", DECISIONS)
        write(self.root, "docs/flows/export.md", FLOW)
        write(self.root, "docs/guide.md", GUIDE)
        filler(self.root)
        self.save(self.config)

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, config):
        write(self.root, rk.CONFIG_NAME, json.dumps(config))
        self.cfg = rk.load_config(self.root)
        self.notes = rk.load_notes(self.root, self.cfg)

    def ids(self, query, **kw):
        cur, old = rk.search(query, self.notes, self.cfg, today=TODAY, **kw)
        return [n.id for _, n in cur], [n.id for _, n in old]

    def cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        old_in, sys.stdin = sys.stdin, io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rk.main(["--root", self.root] + list(argv))
        finally:
            sys.stdin = old_in
        return code, out.getvalue(), err.getvalue()

    def hook(self, prompt, session=""):
        payload = {"prompt": prompt, "session_id": session, "cwd": self.root}
        return self.cli("--hook", stdin=json.dumps(payload))[1]


class SplitTest(Project):
    def by_id(self, note_id):
        return [n for n in self.notes if n.id == note_id][0]

    def test_bullets(self):
        got = [n for n in self.notes if n.kind == "decision"]
        self.assertEqual([n.id.split(":")[1] for n in got], ["6", "7", "9", "13", "15"])
        self.assertEqual(got[0].date, "2030-03-02")
        self.assertEqual(got[1].date, "2030-04-11")
        self.assertIn("quokka", got[1].text)          # continuation line joined
        self.assertEqual((got[1].line, got[1].end), (7, 8))
        self.assertEqual(got[2].date, "")
        self.assertEqual(got[3].date, "2029-12-01")
        self.assertIn("nested bullet", got[3].text)
        self.assertNotIn("Plain paragraph", got[4].text)
        self.assertEqual(got[4].status, "superseded")  # struck-through bullet
        self.assertNotIn("~~", got[4].summary)

    def test_file(self):
        n = self.by_id("docs/flows/export.md")
        self.assertEqual(n.title, "Export pipeline")
        self.assertTrue(n.summary.startswith("Streaming the export in chunks"))
        self.assertEqual((n.status, n.date, n.topic), ("active", "2030-05-20", "export"))
        self.assertIn("csv", n.text)

    def test_file_falls_back_to_first_paragraph_and_file_name(self):
        write(self.root, "notes/pool-sizing.md", "Keep the pool at 20 connections.\nSecond line.\n")
        self.save(self.config)
        n = self.by_id("notes/pool-sizing.md")
        self.assertEqual(n.title, "pool sizing")
        self.assertEqual(n.summary, "Keep the pool at 20 connections. Second line.")

    def test_headings(self):
        got = [n for n in self.notes if n.kind == "guide"]
        self.assertEqual([n.title for n in got],
                         ["Rotating the signing certificate", "Restoring a backup (2030-02-14)"])
        self.assertEqual(got[0].id, "docs/guide.md:5")
        self.assertTrue(got[0].summary.startswith("Run the rotate script"))
        self.assertIn("nested detail", got[0].text.lower())
        self.assertEqual(got[1].date, "2030-02-14")
        self.assertEqual((got[0].line, got[0].end), (5, 10))

    def test_first_source_wins_and_exclude(self):
        self.assertEqual(len([n for n in self.notes if n.path == "docs/guide.md" and n.kind == "doc"]), 0)
        cfg = dict(self.config, exclude=["**/filler-*.md"])
        self.save(cfg)
        self.assertFalse([n for n in self.notes if "filler" in n.path])

    def test_glob_cannot_leave_the_project(self):
        write(self.root, rk.CONFIG_NAME, json.dumps({"sources": [{"glob": "../*.md"}]}))
        with self.assertRaises(rk.ConfigError):
            rk.load_notes(self.root, rk.load_config(self.root))


class RankTest(Project):
    def test_rare_word_beats_common(self):
        # "server" is in every filler note, "idempotent" in one.
        cur, _ = self.ids("is the idempotent writer on the server why the retry queue")
        self.assertEqual(cur[0], "docs/decisions/storage.md:7")
        self.assertFalse([i for i in cur if "filler" in i])

    def test_common_words_alone_print_nothing(self):
        self.assertEqual(self.ids("restart the server and check the filler"), ([], []))

    def test_one_hit_is_not_enough(self):
        self.assertEqual(self.ids("idempotent"), ([], []))

    def test_accent_folding_and_plural(self):
        cur, _ = self.ids("thumbnàils rendéred lazily")
        self.assertEqual(cur[0], "docs/decisions/storage.md:9")
        cur, _ = self.ids("ledger tables sharding regions")
        self.assertEqual(cur[0], "docs/decisions/storage.md:13")
        self.assertEqual(rk.fold("Đường trợ năng"), "duong tro nang")

    def test_synonyms(self):
        prompt = "bộ nhớ đệm mất hiệu lực theo tenant"
        self.assertEqual(self.ids(prompt), ([], []))
        self.save(dict(self.config, synonyms={"bo nho dem": "cache", "mat hieu luc": ["invalidation"],
                                              "nho": "retry queue"}))
        cur, _ = self.ids(prompt)
        self.assertEqual(cur[0], "docs/decisions/storage.md:6")
        # word boundaries: "nho" must match as a whole word, and it did ("bo nho dem"), but
        # "tenan" would not match "tenant".
        self.assertNotIn("tenan", rk.query_words("x", dict(self.cfg, _synonyms=[(" tenan ", "zzz")])))
        self.assertNotIn("zzz", rk.query_words("tenant", dict(self.cfg, _synonyms=[(" tenan ", "zzz")])))

    def test_synonyms_from_file(self):
        write(self.root, "syn.json", json.dumps({"_comment": "ignored", "hàng đợi": "queue"}))
        self.save(dict(self.config, synonyms="syn.json"))
        self.assertIn("queue", rk.query_words("hàng đợi thử lại", self.cfg))

    def test_recency_prefers_newer(self):
        write(self.root, "notes/a.md", "---\ndate: 2028-01-01\n---\n# Kestrel throttle budget\n\nOld numbers.\n")
        write(self.root, "notes/b.md", "---\ndate: 2030-05-30\n---\n# Kestrel throttle budget\n\nNew numbers.\n")
        self.save(self.config)
        cur, _ = self.ids("kestrel throttle budget numbers")
        self.assertEqual(cur[:2], ["notes/b.md", "notes/a.md"])

    def test_status_and_replacement(self):
        write(self.root, "notes/old-plan.md",
              "---\nstatus: superseded\nsuperseded_by: new-plan\n---\n# Walrus importer plan\n\nPoll every minute.\n")
        write(self.root, "notes/new-plan.md", "# Importer v2\n\nPush based, no polling.\n")
        write(self.root, "notes/attic.md", "---\nstatus: cold\n---\n# Walrus importer polling history\n\nOld log.\n")
        write(self.root, "notes/wip.md", "---\nstatus: draft\n---\n# Walrus importer draft\n\nMaybe.\n")
        self.save(self.config)
        cur, old = self.ids("walrus importer draft plan polling")
        self.assertIn("notes/wip.md", cur)
        self.assertEqual(set(old), {"notes/old-plan.md", "notes/attic.md"})
        cur, old = self.ids("walrus importer draft plan polling", hook=True)
        self.assertEqual(old, [])
        self.assertIn("notes/new-plan.md", cur)
        self.assertNotIn("notes/old-plan.md", cur)
        self.assertNotIn("notes/attic.md", cur)
        self.assertIn("notes/wip.md (draft)", self.hook("what was the walrus importer draft plan again"))
        out = self.hook("does the walrus importer still poll every minute")
        self.assertIn("notes/new-plan.md (replaces superseded notes/old-plan.md)", out)
        self.assertNotIn("- notes/old-plan.md", out)
        self.assertIn("superseded by new-plan", self.cli("walrus importer plan")[1])

    def test_same_topic_keeps_newest(self):
        for day in ("2030-01-05", "2030-02-05", "2030-03-05"):
            write(self.root, "notes/pelican-%s.md" % day,
                  "---\ntopic: pelican\ndate: %s\n---\n# Pelican indexer benchmark\n\nRun of %s.\n" % (day, day))
        self.save(self.config)
        cur, _ = self.ids("pelican indexer benchmark results", hook=True)
        self.assertEqual(cur, ["notes/pelican-2030-03-05.md"])
        cur, _ = self.ids("pelican indexer benchmark results", top=6)
        self.assertEqual(len(cur), 3)

    def test_hook_skip_kinds(self):
        self.save(dict(self.config, hook_skip_kinds=["decision"]))
        cur, _ = self.ids("idempotent writer retry queue", hook=True)
        self.assertEqual(cur, [])


class HookTest(Project):
    def test_prints_compact_relative_output(self):
        out = self.hook("why did we drop the retry queue for the idempotent writer")
        lines = out.strip().split("\n")
        self.assertEqual(lines[0], self.cfg["header"])
        self.assertTrue(lines[1].startswith("- docs/decisions/storage.md:7 · 2030-04-11 — Retry queue dropped"))
        self.assertLessEqual(len(lines) - 1, 3)
        self.assertLessEqual(len(out.encode("utf-8")), 1000)

    def test_no_absolute_paths(self):
        outs = [self.hook("why did we drop the retry queue for the idempotent writer"),
                self.cli("streaming export chunks memory")[1],
                self.cli("--json", "rotate signing certificate gateway")[1],
                self.cli("stats")[1]]
        for out in outs:
            self.assertTrue(out)
            self.assertNotIn(self.root, out)
            self.assertNotIn(tempfile.gettempdir(), out)
            self.assertNotIn(os.path.expanduser("~"), out)

    def test_short_or_unrelated_prompt_prints_nothing(self):
        self.assertEqual(self.hook("ok do it"), "")
        self.assertEqual(self.hook("retry queue"), "")
        self.assertEqual(self.hook("please write a haiku about the autumn moon tonight"), "")
        self.assertEqual(self.cli("--hook", stdin="not json")[1], "")
        self.assertEqual(self.cli("--hook", stdin="{}")[1], "")

    def test_skip_prefixes(self):
        self.save(dict(self.config, skip_prefixes=["/loop"]))
        self.assertEqual(self.hook("/loop why did we drop the retry queue idempotent writer"), "")

    def test_session_dedupe(self):
        sid = "test-" + uuid.uuid4().hex
        prompt = "why did we drop the retry queue for the idempotent writer"
        try:
            self.assertIn("storage.md:7", self.hook(prompt, sid))
            self.assertNotIn("storage.md:7", self.hook(prompt, sid))
            self.assertIn("storage.md:7", self.hook(prompt, "other-" + sid))
            self.assertIn("storage.md:7", self.hook(prompt))  # no session id: no dedupe
        finally:
            for s in (sid, "other-" + sid):
                with contextlib.suppress(OSError):
                    os.remove(rk._seen_file(s))

    def test_byte_budget(self):
        long = "xé " * 400
        for i in range(5):
            write(self.root, "notes/narwhal-%d.md" % i, "# Narwhal gateway limits %d\n\n%s\n" % (i, long))
        self.save(dict(self.config, top=5, max_bytes=600, summary_chars=500))
        out = self.hook("what are the narwhal gateway limits today")
        self.assertTrue(out)
        self.assertLessEqual(len(out.encode("utf-8")), 601)

    def test_root_from_env_and_no_config(self):
        elsewhere = tempfile.mkdtemp()
        cwd, old_env = os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")
        try:
            os.chdir(elsewhere)
            os.environ["CLAUDE_PROJECT_DIR"] = self.root
            self.assertEqual(rk.find_root(), self.root)
            os.environ.pop("CLAUDE_PROJECT_DIR")
            self.assertEqual(rk.find_root(), "")
            self.assertEqual(rk.find_root(os.path.join(self.root, "docs", "flows")), self.root)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(rk.main(["some", "question"]), 2)
            self.assertIn("recall init", err.getvalue())
            with contextlib.redirect_stdout(out):
                self.assertEqual(rk.main(["--root", elsewhere, "init"]), 0)
            starter = rk.load_config(elsewhere)
            self.assertTrue(starter["sources"])
        finally:
            os.chdir(cwd)
            if old_env is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = old_env
            os.remove(os.path.join(elsewhere, rk.CONFIG_NAME))
            os.rmdir(elsewhere)


MEMO = """---
name: Flamingo feeder cadence
description: Refill the flamingo feeder every 6 hours, not on demand
metadata:
  type: feedback
---
Use the fixed cadence for the flamingo feeder.

**Why:** on-demand refills starved the night shift twice.
**How to apply:** schedule via the cron helper, never from the request path (zeppelin rule).
"""


class ClaudeMemoryTest(unittest.TestCase):
    """The memory folder lives outside the project, under the user's home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.user = "secretuser%s" % uuid.uuid4().hex[:6]
        home = os.path.join(os.path.realpath(self._tmp.name), self.user)
        self.root = os.path.join(home, "work", "my_app.v2")
        self.saved = dict((k, os.environ.get(k)) for k in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR"))
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.home = home
        write(self.root, "notes/a.md", "# Unrelated\n\nNothing here.\n")
        filler(self.root)
        write(self.root, rk.CONFIG_NAME, json.dumps({"sources": [
            {"source": "claude-memory"}, {"glob": "notes/*.md", "kind": "note"}]}))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def fill(self, base):
        slug = "".join(c if c.isalnum() and c.isascii() else "-" for c in self.root)
        folder = os.path.join(base, "projects", slug, "memory")
        write(folder, "flamingo-feeder.md", MEMO)
        write(folder, "MEMORY.md", "- [Flamingo feeder cadence](flamingo-feeder.md) flamingo feeder cadence refill\n")
        return folder

    def run_cli(self, *argv, stdin=""):
        out = io.StringIO()
        old_in, sys.stdin = sys.stdin, io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rk.main(["--root", self.root] + list(argv))
        finally:
            sys.stdin = old_in
        return out.getvalue()

    def check(self):
        cfg = rk.load_config(self.root)
        notes = rk.load_notes(self.root, cfg)
        memo = [n for n in notes if n.kind == "memory"]
        self.assertEqual([n.id for n in memo], ["memory/flamingo-feeder.md"])   # MEMORY.md skipped
        self.assertEqual(memo[0].title, "Flamingo feeder cadence")
        self.assertTrue(memo[0].summary.startswith("Refill the flamingo feeder"))
        for word in ("starved", "zeppelin", "feedback"):
            self.assertIn(word, memo[0].text)           # Why / How to apply lines + metadata.type
        prompt = "how often should the flamingo feeder refill happen"
        hook = self.run_cli("--hook", stdin=json.dumps({"prompt": prompt, "cwd": self.root}))
        self.assertIn("- memory/flamingo-feeder.md \u2014 Flamingo feeder cadence: Refill", hook)
        get = rk.mcp_call(self.root, cfg, "recall_get", {"id": "memory/flamingo-feeder.md"})
        self.assertIn("**Why:**", get)
        outs = [hook, get, self.run_cli(prompt), self.run_cli("--json", prompt), self.run_cli("stats"),
                rk.mcp_call(self.root, cfg, "recall_search", {"query": prompt}),
                self.run_cli("why zeppelin rule cron helper request path")]
        for out in outs:
            self.assertTrue(out.strip())
            self.assertNotIn(self.user, out)
            self.assertNotIn(self.home, out)
            self.assertNotIn(tempfile.gettempdir(), out)
            self.assertNotIn("projects", out)
        for n in notes:
            self.assertNotIn(self.user, " ".join(str(x) for x in n))
        return prompt

    def test_default_home_folder(self):
        self.fill(os.path.join(self.home, ".claude"))
        self.check()

    def test_config_dir_env_wins(self):
        other = os.path.join(self.home, "elsewhere", "cc")
        os.environ["CLAUDE_CONFIG_DIR"] = other
        self.fill(other)
        self.check()

    def test_hook_can_skip_memory(self):
        self.fill(os.path.join(self.home, ".claude"))
        prompt = self.check()
        write(self.root, rk.CONFIG_NAME, json.dumps({"hook_skip_kinds": ["memory"], "sources": [
            {"source": "claude-memory"}, {"glob": "notes/*.md", "kind": "note"}]}))
        payload = json.dumps({"prompt": prompt, "cwd": self.root})
        self.assertEqual(self.run_cli("--hook", stdin=payload), "")
        self.assertIn("memory/flamingo-feeder.md", self.run_cli(prompt))   # CLI still finds it

    def test_missing_folder_is_fine(self):
        cfg = rk.load_config(self.root)
        self.assertFalse([n for n in rk.load_notes(self.root, cfg) if n.kind == "memory"])

    def test_memory_split_for_files_inside_the_repo(self):
        write(self.root, "agent/memory/flamingo-feeder.md", MEMO)
        write(self.root, rk.CONFIG_NAME, json.dumps({"sources": [
            {"glob": "agent/memory/*.md", "split": "memory"}]}))
        notes = rk.load_notes(self.root, rk.load_config(self.root))
        self.assertEqual((notes[0].id, notes[0].kind, notes[0].ext), ("agent/memory/flamingo-feeder.md", "memory", False))
        self.assertIn("starved", notes[0].text)


class McpTest(Project):
    def test_search_and_get(self):
        found = rk.mcp_call(self.root, self.cfg, "recall_search", {"query": "retry queue idempotent writer"})
        self.assertIn("docs/decisions/storage.md:7", found)
        body = rk.mcp_call(self.root, self.cfg, "recall_get", {"id": "docs/decisions/storage.md:7"})
        self.assertTrue(body.startswith("- Retry queue dropped"))
        self.assertIn("quokka", body)
        section = rk.mcp_call(self.root, self.cfg, "recall_get", {"id": "docs/guide.md:5"})
        self.assertIn("### Detail", section)
        self.assertNotIn("Restoring", section)
        self.assertIn("# Export pipeline", rk.mcp_call(self.root, self.cfg, "recall_get", {"id": "docs/flows/export.md"}))
        for bad in ("../outside.md", os.path.join(self.root, rk.CONFIG_NAME), rk.CONFIG_NAME):
            self.assertIn("Unknown note id", rk.mcp_call(self.root, self.cfg, "recall_get", {"id": bad}))

    def test_stdio_protocol(self):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "recall_search", "arguments": {"query": "streaming export chunks memory"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "nope"}]
        out = self.cli("mcp", stdin="\n".join(json.dumps(r) for r in reqs) + "\n")[1]
        replies = [json.loads(x) for x in out.strip().split("\n")]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "recall-kit")
        self.assertEqual(len(replies[1]["result"]["tools"]), 2)
        self.assertIn("docs/flows/export.md", replies[2]["result"]["content"][0]["text"])
        self.assertIn("error", replies[3])


class FakeEmbeddings(BaseHTTPRequestHandler):
    """Deterministic stand-in for an embeddings endpoint: one axis per known concept."""
    AXES = ["otter", "sql", "moon"]
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeEmbeddings.seen.append(body)
        rows = []
        for i, text in enumerate(body["input"]):
            low = text.lower()
            vec = [1.0 if axis in low else 0.0 for axis in self.AXES]
            # prompts speak in paraphrases the lexical match cannot see
            if "swimming mammal" in low:
                vec[0] = 1.0
            rows.append({"index": i, "embedding": vec + [0.05]})
        data = json.dumps({"data": rows}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class VectorTest(Project):
    def setUp(self):
        super().setUp()
        for name in ("RECALL_EMBED_URL", "RECALL_EMBED_MODEL", "RECALL_NO_VEC"):
            os.environ.pop(name, None)
        write(self.root, "notes/otter.md", "# Otter enclosure sizing\n\nTwelve square metres per pair.\n")
        FakeEmbeddings.seen = []
        self.server = HTTPServer(("localhost", 0), FakeEmbeddings)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://localhost:%d/v1/embeddings" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_off_by_default_sends_nothing(self):
        self.save(self.config)
        self.assertIsNone(rk.embed_settings(self.cfg))
        self.assertIsNone(rk.vec_rank("anything at all", self.notes, self.root, self.cfg))
        self.hook("which swimming mammal needs the bigger enclosure sizing")
        self.assertEqual(self.cli("embed")[0], 2)
        self.assertEqual(FakeEmbeddings.seen, [])

    def test_hybrid_finds_paraphrase(self):
        self.save(dict(self.config, embedding={"url": self.url, "model": "fake-model"}))
        prompt = "how much room does the swimming mammal need"
        self.assertEqual(self.ids(prompt), ([], []))            # lexical alone: nothing
        self.assertIsNone(rk.vec_rank(prompt, self.notes, self.root, self.cfg))  # no cache yet
        code, out, _ = self.cli("embed")
        self.assertEqual(code, 0)
        self.assertIn("embedded %d notes" % len(self.notes), out)
        self.assertIn("embedded 0 notes", self.cli("embed")[1])  # second run reuses the cache
        vec = rk.vec_rank(prompt, self.notes, self.root, self.cfg)
        self.assertEqual(self.notes[vec[0][1]].id, "notes/otter.md")
        self.assertGreater(vec[0][0], 0.9)
        cur, _ = self.ids(prompt, vec=vec)
        self.assertEqual(cur, ["notes/otter.md"])
        self.assertIn("notes/otter.md", self.hook(prompt))
        os.environ["RECALL_NO_VEC"] = "1"
        try:
            self.assertEqual(self.hook(prompt), "")
        finally:
            os.environ.pop("RECALL_NO_VEC")

    def test_endpoint_down_falls_back_to_lexical(self):
        self.save(dict(self.config, embedding={"url": self.url, "model": "fake-model"}))
        self.cli("embed")
        self.save(dict(self.config, embedding={"url": "http://localhost:9/v1/embeddings", "model": "fake-model",
                                               "timeout": 0.3}))
        out = self.hook("why did we drop the retry queue for the idempotent writer")
        self.assertIn("storage.md:7", out)


class SpeedTest(unittest.TestCase):
    def test_1000_notes_under_100ms(self):
        rnd = random.Random(42)
        vocab = ["w%03d" % i for i in range(400)] + ["latency", "budget", "gateway", "queue", "index"]
        with tempfile.TemporaryDirectory() as root:
            for f in range(8):
                lines = ["# Area %d" % f, "", "## Decided"]
                for b in range(75):
                    lines.append("- %s (2030-0%d-1%d)" % (" ".join(rnd.choice(vocab) for _ in range(25)), 1 + b % 9, b % 9))
                write(root, "docs/decisions/area-%d.md" % f, "\n".join(lines) + "\n")
            for f in range(400):
                body = "\n\n".join(" ".join(rnd.choice(vocab) for _ in range(60)) for _ in range(6))
                write(root, "docs/flows/flow-%03d.md" % f, "# Flow %d %s\n\n%s\n" % (f, rnd.choice(vocab), body))
            write(root, "docs/flows/needle.md", "# Zebra crossing timeout\n\nThe zebra crossing timeout is 40 seconds.\n")
            write(root, rk.CONFIG_NAME, json.dumps({"sources": [
                {"glob": "docs/decisions/*.md", "kind": "decision", "split": "bullet"},
                {"glob": "docs/flows/*.md", "kind": "flow", "split": "file"}]}))
            best, count, top = 9e9, 0, ""
            for _ in range(5):
                t0 = time.perf_counter()
                cfg = rk.load_config(root)
                notes = rk.load_notes(root, cfg)
                cur, _ = rk.search("what is the zebra crossing timeout for the gateway queue", notes, cfg, hook=True)
                best = min(best, (time.perf_counter() - t0) * 1000)
                count, top = len(notes), cur[0][1].id
            self.assertGreaterEqual(count, 1000)
            self.assertEqual(top, "docs/flows/needle.md")
            limit = float(os.environ.get("RECALL_SPEED_MS", "100"))
            self.assertLess(best, limit, "load + search took %.1f ms for %d notes" % (best, count))
            print("\n  speed: %d notes, load + search best of 5 = %.1f ms" % (count, best), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
