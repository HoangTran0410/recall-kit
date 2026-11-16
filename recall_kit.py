#!/usr/bin/env python3
"""recall-kit: project memory recall for coding agents.

Given a prompt, find the few most relevant notes in a project's own knowledge
files (decision logs, docs, memory notes) and print them compactly. Prints
nothing when nothing matches well.

    recall "why did we drop the retry queue"   # search
    recall --hook                              # Claude Code UserPromptSubmit hook (stdin JSON)
    recall init                                # write a starter .recall.json
    recall stats                               # what is indexed, how long it takes
    recall mcp                                 # MCP stdio server (recall_search, recall_get)
    recall embed                               # optional: build the vector cache

Standard library only. This single file can also be copied into a repo and run
as `python3 recall_kit.py ...`.
"""
from __future__ import annotations

import datetime
import glob
import json
import math
import os
import re
import sys
import unicodedata
from fnmatch import fnmatch
from typing import NamedTuple

__version__ = "0.1.0"

CONFIG_NAME = ".recall.json"
HEAD_CHARS = 8000        # `file` notes: only the head of the file is read
BODY_CHARS = 700         # match text kept per bullet / section
MAX_GET_CHARS = 20000    # MCP recall_get cap
STATUSES = ("active", "superseded", "cold", "draft")
CLAUDE_MEMORY = "claude-memory"   # source name: Claude Code's per-project auto-memory folder
MEMORY_PREFIX = "memory/"         # how those notes are shown (their real folder is never printed)
MEMORY_INDEX = "MEMORY.md"        # the index file, already in the agent's context: not a note

DEFAULTS = {
    "sources": [],
    "exclude": ["**/node_modules/**"],
    "synonyms": {},
    "stopwords": [],
    "keep_words": [],
    "top": 3,
    "max_bytes": 1000,
    "summary_chars": 160,
    "min_words": 4,
    "min_hits": 2,
    "min_score": 5.0,
    "recency_floor": 0.85,
    "recency_days": 365,
    "kind_weights": {},
    "hook_skip_kinds": [],
    "skip_prefixes": [],
    "summary_headings": ["conclusion", "summary", "decision", "outcome", "tl;dr", "ket luan"],
    "header": "Possibly relevant project notes (read the note before redoing the work):",
    "embedding": {},
}

STOP_EN = """a an and are as at be been being but by can could did do does done for from had has
have he her here him his how i if in into is it its just me my no not of on or our out she so
than that the their them then there these they this those to too up us was we were what when
where which who whom why will with would you your yes ok okay please thanks thank hi hey also
very about again all any both each few more most other some such only own same should shall may
might must am vs via etc""".split()

# Vietnamese function words, accent-folded (prompts are folded before matching).
STOP_VI = """va la cua co khong cho nay do thi ma voi cac nhung duoc di lam gi sao nhu nao vay roi
the mot hai ba nua luon cung con dang da se bi tai vi neu khi thay xem giup minh toi em anh chi
ban nha nhe ve ra vao len xuong trong ngoai tren duoi qua lai moi het nhieu it hon rat tu den
theo bang hay hoac nen can phai muon biet thu o day kia ay""".split()

MARKS_RE = re.compile("[̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯]")
WORD_RE = re.compile(r"[^\W_]+")
DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
FRONT_RE = re.compile(r"﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.S)
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# Hybrid ranking (only when an embedding endpoint is configured).
RRF_K = 10
W_LEX = 0.5
W_VEC = 1.0


class Note(NamedTuple):
    id: str          # path, or path:line for bullet / heading notes
    path: str        # relative to the project root, forward slashes
    kind: str        # label from the source config
    title: str
    summary: str
    text: str        # everything the lexical match looks at
    status: str = "active"
    by: str = ""     # superseded_by
    date: str = ""   # YYYY-MM-DD
    topic: str = ""  # same topic => only the newest note is suggested (hook)
    line: int = 0    # first line (1-based); 0 = whole file
    end: int = 0     # last line
    remark: str = ""  # extra label in the output line
    ext: bool = False  # lives in the Claude Code memory folder outside the project


class ConfigError(Exception):
    pass


# ---- text ------------------------------------------------------------------

def fold(s: str) -> str:
    """Lowercase and strip accents (NFD, combining marks dropped, d-with-stroke -> d)."""
    s = s.lower()
    if s.isascii():
        return s
    s = unicodedata.normalize("NFD", s).replace("đ", "d")
    return MARKS_RE.sub("", s)


def stem(w: str) -> str:
    """Tiny plural fold so `widgets` matches `widget`. Not a real stemmer."""
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def raw_words(s: str) -> list:
    return WORD_RE.findall(fold(s))


def content_words(s: str, stop: frozenset) -> list:
    return [stem(w) for w in raw_words(s) if len(w) >= 2 and w not in stop]


def clean(t: str) -> str:
    """One Markdown line -> plain text."""
    t = LINK_RE.sub(r"\1", t)
    t = re.sub(r"[*`]|~~", "", t)
    return re.sub(r"^(?:[>\-+]\s+|\d+[.)]\s+)+", "", t.strip()).strip()


def valid_date(s: str) -> str:
    m = DATE_RE.search(s or "")
    if not m:
        return ""
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    return m.group(0)


def parse_front(text: str) -> tuple:
    """(front-matter dict, body, number of lines the front matter took)."""
    m = FRONT_RE.match(text)
    if not m:
        return {}, text, 0
    fm, parent = {}, ""
    for ln in m.group(1).splitlines():
        k, sep, v = ln.partition(":")
        k, v = k.strip().lower(), v.strip().strip("\"'")
        if not sep or not k or k.startswith("#"):
            continue
        if not ln.startswith((" ", "\t")):
            fm[k] = v
            parent = "" if v else k
        elif parent:  # one nested level: `metadata:` / `  type: x` -> metadata.type
            fm[parent + "." + k] = v
    return fm, text[m.end():], text[:m.end()].count("\n")


# ---- config ----------------------------------------------------------------

def load_config(root: str, path: str = "") -> dict:
    path = path or os.path.join(root, CONFIG_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except OSError:
        raise ConfigError("no %s found (run `recall init`)" % CONFIG_NAME)
    except ValueError as e:
        raise ConfigError("%s is not valid JSON: %s" % (os.path.basename(path), e))
    if not isinstance(user, dict):
        raise ConfigError("%s must be a JSON object" % os.path.basename(path))
    cfg = dict(DEFAULTS)
    cfg.update(user)
    for src in cfg["sources"]:
        if not isinstance(src, dict) or not (src.get("glob") or src.get("source")):
            raise ConfigError("every source needs a \"glob\" (or \"source\": \"%s\")" % CLAUDE_MEMORY)
        if src.get("source") not in (None, CLAUDE_MEMORY):
            raise ConfigError("unknown source: %s" % src.get("source"))
        if src.get("split", "file") not in ("file", "bullet", "heading", "memory"):
            raise ConfigError("source split must be file, bullet, heading or memory: %s" % src.get("glob"))
    syn = cfg["synonyms"]
    if isinstance(syn, str):
        try:
            with open(os.path.join(root, syn), encoding="utf-8") as f:
                syn = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError("cannot read synonyms file %s: %s" % (syn, type(e).__name__))
    pairs = []
    for phrase, target in (syn or {}).items():
        if phrase.startswith("_"):
            continue
        target = " ".join(target) if isinstance(target, list) else str(target)
        pairs.append((" " + " ".join(raw_words(phrase)) + " ", target))
    cfg["_synonyms"] = pairs
    stop = set(STOP_EN) | set(STOP_VI) | set(fold(w) for w in cfg["stopwords"])
    cfg["_stop"] = frozenset(stop - set(fold(w) for w in cfg["keep_words"]))
    return cfg


def find_root(start: str = "", explicit: str = "") -> str:
    """Project root: --root, else $CLAUDE_PROJECT_DIR (if it has a config), else walk up from cwd."""
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env and os.path.isfile(os.path.join(env, CONFIG_NAME)):
        return os.path.abspath(env)
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, CONFIG_NAME)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


# ---- notes -----------------------------------------------------------------

def excluded(rel: str, patterns: list) -> bool:
    base = os.path.basename(rel)
    for p in patterns:
        short = p[3:] if p.startswith("**/") else p
        if fnmatch(rel, p) or fnmatch(rel, short) or fnmatch(base, p):
            return True
    return False


def source_files(root: str, src: dict, global_exclude: list) -> list:
    pattern = src["glob"]
    if os.path.isabs(pattern) or ".." in pattern.replace("\\", "/").split("/"):
        raise ConfigError("source glob must stay inside the project: %s" % pattern)
    skip = list(global_exclude) + list(src.get("exclude", []))
    out = []
    for p in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        if not rel.startswith("../") and not excluded(rel, skip):
            out.append(rel)
    return out


def memory_dir(root: str) -> str:
    """Claude Code keeps a memory folder per project outside the repo:
    <config dir>/projects/<slug>/memory, slug = absolute root with non-alphanumerics as `-`.
    Resolved at run time; the result is used for reading only and never printed."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(root))
    return os.path.join(base, "projects", slug, "memory")


def source_entries(root: str, src: dict, global_exclude: list) -> list:
    """[(shown path, path to open)] for one source."""
    if src.get("source") != CLAUDE_MEMORY:
        return [(rel, os.path.join(root, rel)) for rel in source_files(root, src, global_exclude)]
    folder = memory_dir(root)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    skip = list(src.get("exclude", []))
    return [(MEMORY_PREFIX + name, os.path.join(folder, name)) for name in names
            if name.endswith(".md") and name != MEMORY_INDEX and not excluded(name, skip)]


def paragraph(lines: list, i: int) -> str:
    """Plain text of the paragraph starting at line i (at most ~300 chars)."""
    out = []
    for ln in lines[i:i + 6]:
        t = ln.strip()
        if not t or t.startswith(("#", "```", "|")):
            break
        out.append(clean(t))
        if sum(len(x) for x in out) > 300:
            break
    return " ".join(out)[:320]


def summarize(lines: list, heads: list) -> tuple:
    """(first `# ` title, first paragraph of a conclusion-like section, else first paragraph)."""
    title, first, in_sum, code = "", "", False, False
    for i, ln in enumerate(lines):
        t = ln.strip()
        if t.startswith("```"):
            code = not code
            continue
        if code or not t or t.startswith(("<!--", "|", "---")):
            continue
        if t.startswith("#"):
            h = t.lstrip("#").strip()
            if not title and t.startswith("# "):
                title = clean(h)
            in_sum = any(k in fold(h) for k in heads)
            continue
        if in_sum:
            return title, paragraph(lines, i)
        if not first:
            first = paragraph(lines, i)
    return title, first


def meta_of(fm: dict) -> tuple:
    status = fm.get("status", "active").lower() or "active"
    if status not in STATUSES:
        status = "active"
    extra = " ".join(fm.get(k, "") for k in ("topic", "tags", "keywords", "area"))
    return status, fm.get("superseded_by", ""), valid_date(fm.get("date", "")), fm.get("topic", ""), extra


def notes_from_file(rel: str, kind: str, text: str, heads: list) -> list:
    fm, body, _ = parse_front(text)
    status, by, date, topic, extra = meta_of(fm)
    title, summary = summarize(body.splitlines(), heads)
    stem_name = os.path.splitext(os.path.basename(rel))[0].replace("-", " ").replace("_", " ")
    title = title or fm.get("title", "") or fm.get("name", "") or stem_name
    summary = clean(fm.get("description", ""))[:320] or summary
    match = " ".join((rel, title, summary, extra))
    return [Note(rel, rel, kind, title, summary, match, status, by, date, topic)]


def notes_from_memory(rel: str, kind: str, text: str, ext: bool) -> list:
    """Agent memory file: front-matter `name` + `description`, then a short body whose first
    lines (Why / How to apply) carry the point."""
    fm, body, _ = parse_front(text)
    status, by, date, topic, extra = meta_of(fm)
    lines = [clean(ln) for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith(("```", "<!--"))]
    stem_name = os.path.splitext(os.path.basename(rel))[0].replace("-", " ").replace("_", " ")
    title = fm.get("name", "") or fm.get("title", "") or stem_name
    if lines and body.lstrip().startswith("# "):
        title = fm.get("name", "") or lines[0].lstrip("# ")
        lines = lines[1:]
    desc = clean(fm.get("description", ""))
    summary = (desc or " ".join(lines[:2]))[:320]
    kind_of = fm.get("metadata.type", "") or fm.get("type", "")
    match = " ".join((os.path.basename(rel), title, desc, " ".join(lines[:8])[:BODY_CHARS], kind_of, extra))
    return [Note(rel, rel, kind, title, summary, match, status, by, date, topic, ext=ext)]


def notes_from_bullets(rel: str, kind: str, text: str) -> list:
    fm, body, off = parse_front(text)
    status, by, fdate, topic, extra = meta_of(fm)
    h1, head, code = "", "", False
    items = []  # [first line, last line, raw first line, text]
    cur = None
    for i, ln in enumerate(body.splitlines(), off + 1):
        if ln.lstrip().startswith("```"):
            code = not code
        if code:
            continue
        if ln.startswith("#"):
            head = clean(ln.lstrip("#"))
            h1 = h1 or head
            cur = None
        elif ln[:2] in ("- ", "* "):
            cur = [i, i, ln, ln[2:].strip()]
            items.append(cur)
        elif cur and ln[:1] in (" ", "\t") and ln.strip():
            cur[1] = i
            if len(cur[3]) < BODY_CHARS:
                cur[3] += " " + ln.strip()
        elif ln.strip():
            cur = None
    out = []
    for first, last, raw, body_text in items:
        plain = clean(body_text)
        if len(plain) < 8:
            continue
        st = "superseded" if body_text.startswith("~~") and status == "active" else status
        match = " ".join((rel, h1, head if head != h1 else "", plain[:BODY_CHARS], extra))
        out.append(Note("%s:%d" % (rel, first), rel, kind, "", plain[:320], match, st, by,
                        valid_date(raw) or fdate, topic, first, last))
    return out


def notes_from_headings(rel: str, kind: str, text: str, level: int) -> list:
    fm, body, off = parse_front(text)
    status, by, fdate, topic, extra = meta_of(fm)
    lines = body.splitlines()
    marks, h1, code = [], "", False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            code = not code
        if code or not ln.startswith("#"):
            continue
        depth = len(ln) - len(ln.lstrip("#"))
        if depth == 1 and not h1:
            h1 = clean(ln[1:])
        if depth <= level:
            marks.append((i, depth))
    out = []
    for n, (i, depth) in enumerate(marks):
        if depth != level:
            continue
        stop = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        title = clean(lines[i].lstrip("#"))
        section = lines[i + 1:stop]
        summary = ""
        for j, ln in enumerate(section):
            t = ln.strip()
            if t and not t.startswith(("#", "```", "|", "<!--")):
                summary = paragraph(section, j)
                break
        excerpt = " ".join(clean(x) for x in section if x.strip())[:BODY_CHARS]
        match = " ".join((rel, h1, title, excerpt, extra))
        out.append(Note("%s:%d" % (rel, off + i + 1), rel, kind, title, summary, match, status, by,
                        valid_date(lines[i]) or fdate, topic, off + i + 1, off + stop))
    return out


def load_notes(root: str, cfg: dict) -> list:
    notes, seen = [], set()
    for src in cfg["sources"]:
        ext = src.get("source") == CLAUDE_MEMORY
        split = "memory" if ext else src.get("split", "file")
        kind = src.get("kind", "memory" if split == "memory" else "note")
        for rel, full in source_entries(root, src, cfg["exclude"]):
            if rel in seen:
                continue
            seen.add(rel)
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    text = f.read(HEAD_CHARS) if split in ("file", "memory") else f.read()
            except OSError:
                continue
            if split == "file":
                notes += notes_from_file(rel, kind, text, cfg["summary_headings"])
            elif split == "memory":
                notes += notes_from_memory(rel, kind, text, ext)
            elif split == "bullet":
                notes += notes_from_bullets(rel, kind, text)
            else:
                notes += notes_from_headings(rel, kind, text, int(src.get("level", 2)))
    return notes


# ---- ranking ---------------------------------------------------------------

def query_words(query: str, cfg: dict) -> list:
    """Content words of the prompt plus synonym targets, de-duplicated, order kept."""
    padded = " " + " ".join(raw_words(query)) + " "
    extra = " ".join(target for phrase, target in cfg["_synonyms"] if phrase in padded)
    out = []
    for w in content_words(query + " " + extra, cfg["_stop"]):
        if w not in out:
            out.append(w)
    return out


def lex_rank(qwords: list, notes: list) -> list:
    """[(idf score, hits, note index)] for every note matching >= 1 query word, best first."""
    df = dict.fromkeys(qwords, 0)
    found = []
    for i, note in enumerate(notes):
        folded = fold(note.text)
        if not any(w in folded for w in qwords):
            continue
        have = set(stem(w) for w in WORD_RE.findall(folded))
        hit = [w for w in qwords if w in have]
        if hit:
            for w in hit:
                df[w] += 1
            found.append((i, hit))
    n = len(notes) or 1
    ranked = sorted((-sum(math.log(n / df[w]) for w in hit), i, len(hit)) for i, hit in found)
    return [(-neg, nhit, i) for neg, i, nhit in ranked]


def recency(note: Note, cfg: dict, today: datetime.date = None) -> float:
    """1.0 for a note dated today, sliding to `recency_floor` at `recency_days` old. Undated = 1.0."""
    if not note.date:
        return 1.0
    try:
        day = datetime.date.fromisoformat(note.date)
    except ValueError:
        return 1.0
    age = ((today or datetime.date.today()) - day).days
    frac = min(1.0, max(0.0, age / float(max(1, cfg["recency_days"]))))
    floor = float(cfg["recency_floor"])
    return 1.0 - (1.0 - floor) * frac


def find_ref(ref: str, notes: list):
    """Resolve `superseded_by`: a note id, a path, a file name, or a file-name prefix."""
    ref = ref.strip()
    if not ref:
        return None
    for test in (lambda n: n.id == ref,
                 lambda n: n.path == ref,
                 lambda n: os.path.basename(n.path) == ref,
                 lambda n: os.path.splitext(os.path.basename(n.path))[0] == ref,
                 lambda n: os.path.basename(n.path).startswith(ref + "-")):
        for n in notes:
            if test(n):
                return n
    return None


def hook_filter(cur: list, old: list, notes: list, cfg: dict) -> list:
    """Strict selection for automatic injection: no cold notes, a superseded note is swapped
    for its replacement, and notes sharing a topic collapse to the newest one."""
    seen = set(n.id for _, n in cur)
    pool = list(cur)
    for score, n in old:
        rep = find_ref(n.by, notes) if n.status == "superseded" else None
        if rep and rep.status not in ("superseded", "cold") and rep.id not in seen:
            seen.add(rep.id)
            pool.append((score, rep._replace(remark="replaces superseded %s" % n.id)))
    pool = [(s, n) for _, _, s, n in sorted((-s, i, s, n) for i, (s, n) in enumerate(pool))]
    newest = {}
    for score, n in pool:
        if n.topic and (n.topic not in newest or (n.date, score) > newest[n.topic][:2]):
            newest[n.topic] = (n.date, score, n.id)
    skip = set(cfg["hook_skip_kinds"])
    return [(s, n) for s, n in pool
            if n.kind not in skip and not (n.topic and newest[n.topic][2] != n.id)]


def search(query: str, notes: list, cfg: dict, top: int = 0, hook: bool = False,
           vec: list = None, today: datetime.date = None) -> tuple:
    """(current notes, superseded/cold notes), each a list of (score, Note), best first.

    `vec` is an optional [(cosine, note index)] ranking from `vec_rank`."""
    top = top or cfg["top"]
    qwords = query_words(query, cfg)
    if not qwords or not notes:
        return [], []
    # In a small corpus no word can reach min_score, so the bar scales down with log(N).
    bar = min(float(cfg["min_score"]), 1.5 * math.log(max(2, len(notes))))
    gated = [(s, i) for s, nhit, i in lex_rank(qwords, notes) if nhit >= cfg["min_hits"] and s >= bar]
    weights = cfg["kind_weights"]
    scored = []
    if not vec:
        scored = gated
    else:
        fused, passed = {}, set(i for _, i in gated)
        for r, (_, i) in enumerate(gated[:60]):
            fused[i] = fused.get(i, 0.0) + W_LEX / (RRF_K + r)
        min_cos = float(cfg["embedding"].get("min_cos", 0.6))
        for r, (cos, i) in enumerate(vec):
            fused[i] = fused.get(i, 0.0) + W_VEC / (RRF_K + r)
            if cos >= min_cos:
                passed.add(i)
        scored = [(s, i) for i, s in fused.items() if i in passed]
    cur, old = [], []
    for s, i in scored:
        n = notes[i]
        s *= recency(n, cfg, today) * float(weights.get(n.kind, 1.0))
        (old if n.status in ("superseded", "cold") else cur).append((-s, i, n))
    cur = [(-neg, n) for neg, _, n in sorted(cur)]
    old = [(-neg, n) for neg, _, n in sorted(old)]
    if hook:
        return hook_filter(cur, old, notes, cfg)[:top], []
    return cur[:top], old[:2]


def label(n: Note) -> str:
    if n.status == "superseded":
        return "%s (superseded by %s)" % (n.id, n.by or "?")
    if n.status == "cold":
        return "%s (cold, history only)" % n.id
    if n.status == "draft":
        return "%s (draft)" % n.id
    return "%s (%s)" % (n.id, n.remark) if n.remark else n.id


def render(cur: list, old: list, cfg: dict) -> str:
    """Compact text, never longer than `max_bytes` UTF-8 bytes. Empty when there is nothing."""
    if not cur and not old:
        return ""
    lines = [cfg["header"]]
    left = int(cfg["max_bytes"]) - len(lines[0].encode("utf-8"))
    for _, n in cur + old:
        head = "- %s%s" % (label(n), " · " + n.date if n.date else "")
        body = n.summary
        if len(body) > cfg["summary_chars"]:
            body = body[:cfg["summary_chars"]].rstrip() + "…"
        body = (n.title + ": " + body) if n.title and body else (n.title or body)
        line = head + (" — " + body if body else "")
        while len(line.encode("utf-8")) + 1 > left and len(line) > len(head):
            line = line[:max(len(head), len(line) - 12)]
        if len(line.encode("utf-8")) + 1 > left:
            break
        lines.append(line)
        left -= len(line.encode("utf-8")) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


# ---- session dedupe (hook) -------------------------------------------------

def _seen_file(session_id: str) -> str:
    import tempfile

    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:64]
    return os.path.join(tempfile.gettempdir(), "recall-kit-%s.seen" % safe)


def seen_ids(session_id: str) -> set:
    try:
        with open(_seen_file(session_id), encoding="utf-8") as f:
            return set(f.read().split("\n"))
    except OSError:
        return set()


def mark_seen(session_id: str, ids: list) -> None:
    try:
        with open(_seen_file(session_id), "a", encoding="utf-8") as f:
            f.write("".join(i + "\n" for i in ids))
    except OSError:
        pass


# ---- optional vector search ------------------------------------------------
# Off unless an endpoint AND a model are configured. Nothing leaves the machine otherwise.

def embed_settings(cfg: dict):
    e = dict(cfg.get("embedding") or {})
    e["url"] = os.environ.get("RECALL_EMBED_URL") or e.get("url", "")
    e["model"] = os.environ.get("RECALL_EMBED_MODEL") or e.get("model", "")
    if os.environ.get("RECALL_NO_VEC") or not e["url"] or not e["model"]:
        return None
    return e


def embed_texts(texts: list, e: dict, timeout: float) -> list:
    """POST to an OpenAI-compatible embeddings endpoint; returns unit-length vectors."""
    import urllib.request

    headers = {"Content-Type": "application/json"}
    secret = os.environ.get(e.get("api_key_env") or "RECALL_EMBED_API_KEY", "")
    if secret:
        headers["Authorization"] = "Bearer " + secret
    data = json.dumps({"model": e["model"], "input": texts}).encode("utf-8")
    req = urllib.request.Request(e["url"], data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = json.load(resp)["data"]
    out = []
    for row in sorted((r.get("index", i), i, r["embedding"]) for i, r in enumerate(rows)):
        v = row[2]
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


def note_hash(n: Note, model: str) -> str:
    import hashlib

    return hashlib.sha1((model + "\n" + n.text).encode("utf-8")).hexdigest()[:20]


def _cache_paths(root: str, e: dict) -> tuple:
    d = os.path.join(root, e.get("cache", ".recall-cache"))
    return d, os.path.join(d, "index.json"), os.path.join(d, "vectors.bin")


def load_vectors(root: str, e: dict) -> tuple:
    """({hash: row}, flat float array, dim) or ({}, None, 0)."""
    from array import array

    _, index_path, bin_path = _cache_paths(root, e)
    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        if index.get("model") != e["model"]:
            return {}, None, 0
        flat = array("f")
        with open(bin_path, "rb") as f:
            flat.fromfile(f, len(index["hashes"]) * index["dim"])
        return dict((h, i) for i, h in enumerate(index["hashes"])), flat, index["dim"]
    except (OSError, ValueError, KeyError, EOFError):
        return {}, None, 0


def build_vectors(root: str, cfg: dict, notes: list, batch: int = 32) -> tuple:
    """Embed notes that are not cached yet. Returns (new, reused)."""
    from array import array

    e = embed_settings(cfg)
    if not e:
        raise ConfigError("embedding is off: set embedding.url and embedding.model first")
    rows, flat, dim = load_vectors(root, e)
    hashes, out, todo = [], array("f"), []
    for n in notes:
        h = note_hash(n, e["model"])
        if h in hashes:
            continue
        hashes.append(h)
        todo.append((h, n))
    vectors, new = {}, 0
    missing = [(h, n) for h, n in todo if h not in rows]
    for k in range(0, len(missing), batch):
        part = missing[k:k + batch]
        for (h, _), v in zip(part, embed_texts([n.text[:2000] for _, n in part], e, 60.0)):
            vectors[h] = v
            new += 1
    for h in hashes:
        if h in vectors:
            out.extend(vectors[h])
        else:
            out.extend(flat[rows[h] * dim:(rows[h] + 1) * dim])
    dim = dim or (len(out) // len(hashes) if hashes else 0)
    d, index_path, bin_path = _cache_paths(root, e)
    os.makedirs(d, exist_ok=True)
    with open(bin_path, "wb") as f:
        out.tofile(f)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({"model": e["model"], "dim": dim, "hashes": hashes}, f)
    return new, len(hashes) - new


def vec_rank(query: str, notes: list, root: str, cfg: dict, k: int = 40):
    """[(cosine, note index)] best first, or None when vectors are off / unavailable."""
    e = embed_settings(cfg)
    if not e:
        return None
    rows, flat, dim = load_vectors(root, e)
    if not rows:
        return None
    try:
        q = embed_texts([query], e, float(e.get("timeout", 1.5)))[0]
    except Exception:
        return None
    if len(q) != dim:
        return None
    from operator import mul

    scored = []
    for i, n in enumerate(notes):
        row = rows.get(note_hash(n, e["model"]))
        if row is not None:
            scored.append((-sum(map(mul, q, flat[row * dim:(row + 1) * dim])), i))
    return [(-neg, i) for neg, i in sorted(scored)[:k]]


# ---- MCP stdio server ------------------------------------------------------

MCP_TOOLS = [
    {"name": "recall_search",
     "description": "Search this project's notes (decisions, docs, memory) for a question. "
                    "Returns note ids with a one-line summary.",
     "inputSchema": {"type": "object", "required": ["query"],
                     "properties": {"query": {"type": "string"},
                                    "k": {"type": "integer", "default": 6}}}},
    {"name": "recall_get",
     "description": "Read one note by the id returned from recall_search.",
     "inputSchema": {"type": "object", "required": ["id"],
                     "properties": {"id": {"type": "string"}}}},
]


def note_body(root: str, n: Note) -> str:
    full = os.path.join(root, n.path)
    if n.ext:
        full = os.path.join(memory_dir(root), os.path.basename(n.path))
    with open(full, encoding="utf-8", errors="ignore") as f:
        if not n.line:
            return f.read(MAX_GET_CHARS)
        lines = f.read().splitlines()
    return "\n".join(lines[n.line - 1:n.end])[:MAX_GET_CHARS]


def mcp_call(root: str, cfg: dict, name: str, args: dict) -> str:
    notes = load_notes(root, cfg)
    if name == "recall_search":
        query = str(args.get("query", ""))
        cur, old = search(query, notes, cfg, top=max(1, min(20, int(args.get("k", 6)))),
                          vec=vec_rank(query, notes, root, cfg))
        big = dict(cfg, max_bytes=6000, summary_chars=240)
        return render(cur, old, big) or "No sufficiently relevant notes."
    if name == "recall_get":
        wanted = str(args.get("id", "")).strip()
        for n in notes:
            if n.id == wanted or (n.path == wanted and not n.line):
                return note_body(root, n)
        return "Unknown note id. Use an id returned by recall_search."
    raise KeyError(name)


def mcp_serve(root: str, config_path: str = "") -> None:
    def reply(mid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": mid}
        if error:
            msg["error"] = error
        else:
            msg["result"] = result
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        if mid is None:
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "recall-kit", "version": __version__}})
        elif method == "tools/list":
            reply(mid, {"tools": MCP_TOOLS})
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/call":
            try:
                text = mcp_call(root, load_config(root, config_path), params.get("name"),
                                params.get("arguments") or {})
                reply(mid, {"content": [{"type": "text", "text": text}]})
            except KeyError:
                reply(mid, error={"code": -32602, "message": "unknown tool"})
            except Exception as e:
                reply(mid, {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True})
        else:
            reply(mid, error={"code": -32601, "message": "method not found"})


# ---- CLI -------------------------------------------------------------------

STARTER = {
    "sources": [
        {"glob": "docs/decisions/*.md", "kind": "decision", "split": "bullet"},
        {"glob": "docs/**/*.md", "kind": "doc", "split": "file", "exclude": ["**/README.md"]},
    ],
    "synonyms": {},
    "top": 3,
}

HOOK_SNIPPET = """Add this to .claude/settings.json to recall notes on every prompt:

  {"hooks": {"UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": "recall --hook", "timeout": 5}
  ]}]}}
"""


def cmd_init(root: str) -> int:
    path = os.path.join(root, CONFIG_NAME)
    if os.path.exists(path):
        print("%s already exists, not touching it" % CONFIG_NAME, file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(STARTER, f, indent=2)
        f.write("\n")
    print("wrote %s: edit \"sources\" to point at your notes, then try `recall stats`.\n" % CONFIG_NAME)
    print(HOOK_SNIPPET)
    return 0


def cmd_stats(root: str, cfg: dict) -> int:
    import time

    t0 = time.perf_counter()
    notes = load_notes(root, cfg)
    search("warm up the ranking path with a few words", notes, cfg)
    ms = (time.perf_counter() - t0) * 1000
    for src in cfg["sources"]:
        ext = src.get("source") == CLAUDE_MEMORY
        files = set(rel for rel, _ in source_entries(root, src, cfg["exclude"]))
        count = sum(1 for n in notes if n.path in files and n.ext == ext)
        print("%-8s %-40s %4d files %5d notes" % ("memory" if ext else src.get("split", "file"),
                                                  CLAUDE_MEMORY if ext else src["glob"], len(files), count))
    print("total: %d notes, load + search %.0f ms, vector search %s"
          % (len(notes), ms, "on" if embed_settings(cfg) else "off"))
    return 0


def run_hook(args) -> None:
    """UserPromptSubmit: stdin {"prompt", "session_id", "cwd"}. Any problem => print nothing."""
    try:
        data = json.load(sys.stdin)
        query, sid = str(data.get("prompt") or ""), str(data.get("session_id") or "")
        root = find_root(str(data.get("cwd") or ""), args.root)
        if not query.strip() or not root:
            return
        cfg = load_config(root, args.config)
        low = fold(query).strip()
        if any(low.startswith(fold(p)) for p in cfg["skip_prefixes"]):
            return
        if len(query_words(query, cfg)) < cfg["min_words"]:
            return
        notes = load_notes(root, cfg)
        cur, _ = search(query, notes, cfg, top=args.top, hook=True, vec=vec_rank(query, notes, root, cfg))
        if sid:
            seen = seen_ids(sid)
            cur = [x for x in cur if x[1].id not in seen]
        text = render(cur, [], cfg)
        if text:
            if sid:
                mark_seen(sid, [n.id for _, n in cur])
            print(text)
    except Exception:
        return


def main(argv: list = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="recall", description="Recall relevant project notes for a prompt.",
                                 epilog="Commands: recall init | stats | mcp | embed")
    ap.add_argument("words", nargs="*", help="the question, or a command: init, stats, mcp, embed")
    ap.add_argument("--hook", action="store_true", help="Claude Code UserPromptSubmit mode (stdin JSON)")
    ap.add_argument("--root", default="", help="project root (default: found from the config file)")
    ap.add_argument("--config", default="", help="config file (default: <root>/%s)" % CONFIG_NAME)
    ap.add_argument("--top", type=int, default=0, help="max notes to print")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--version", action="version", version="recall-kit " + __version__)
    args = ap.parse_args(argv)
    if args.config and not args.root:
        args.root = os.path.dirname(os.path.abspath(args.config))
    if args.hook:
        run_hook(args)
        return 0
    command = args.words[0] if len(args.words) == 1 else ""
    if command == "init":
        return cmd_init(args.root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if not args.words:
        ap.print_help()
        return 2
    try:
        root = find_root("", args.root)
        if not root:
            raise ConfigError("no %s found here or in any parent folder (run `recall init`)" % CONFIG_NAME)
        cfg = load_config(root, args.config)
        if command == "mcp":
            mcp_serve(root, args.config)
            return 0
        if command == "stats":
            return cmd_stats(root, cfg)
        notes = load_notes(root, cfg)
        if command == "embed":
            new, reused = build_vectors(root, cfg, notes)
            print("embedded %d notes, reused %d from the cache" % (new, reused))
            return 0
        query = " ".join(args.words)
        cur, old = search(query, notes, cfg, top=args.top or 6, vec=vec_rank(query, notes, root, cfg))
    except ConfigError as e:
        print("recall: %s" % e, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([{"id": n.id, "kind": n.kind, "status": n.status, "date": n.date,
                           "score": round(s, 3), "title": n.title, "summary": n.summary}
                          for s, n in cur + old], ensure_ascii=False, indent=1))
        return 0
    text = render(cur, old, dict(cfg, max_bytes=max(4000, cfg["max_bytes"])))
    if text:
        print(text)
    else:
        print("recall: no relevant notes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
