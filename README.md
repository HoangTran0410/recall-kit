# recall-kit

Project memory recall for coding agents.

Your repo already holds the answers: decision logs, design docs, runbooks, memory notes. The agent
just does not know they exist, so it re-measures, re-debates and re-breaks things that were settled
months ago. `recall` reads the prompt, finds the few notes that match it, and prints one compact
line per note. The agent then opens the note it needs.

- At most 3 notes and 1 KB per prompt (configurable).
- Prints **nothing** when nothing matches well. No match is the common case and costs zero context.
- Fast: about 20 ms to load and rank 1,000 notes, about 50 ms including interpreter start.
- Python standard library only. No index to build, no daemon, no network.
- Works as a Claude Code hook, a CLI and a small MCP server.

```
$ recall "why did we drop the retry queue"
Possibly relevant project notes (read the note before redoing the work):
- docs/decisions/storage.md:7 · 2030-04-11 — Retry queue dropped: the idempotent writer made it redundant, measured 0 lost writes in a week
```

## Setup in 2 minutes

1. Install (Python 3.9+):

   ```
   pipx install git+https://github.com/<owner>/recall-kit
   ```

   Or copy one file: put `recall_kit.py` anywhere in your repo (for example `tools/recall_kit.py`)
   and run it as `python3 tools/recall_kit.py` wherever this README says `recall`.

2. In your project root:

   ```
   recall init      # writes .recall.json
   ```

   Edit `sources` so the globs point at your notes (see below), then check what was picked up:

   ```
   recall stats
   recall "a question you know has an answer in the notes"
   ```

3. Hook it into Claude Code. Add to `.claude/settings.json`:

   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         {"hooks": [{"type": "command", "command": "recall --hook", "timeout": 5}]}
       ]
     }
   }
   ```

   Copy-one-file variant of the command: `python3 "$CLAUDE_PROJECT_DIR/tools/recall_kit.py" --hook`.

   The hook reads `{"prompt", "session_id", "cwd"}` from stdin and prints matching notes, which
   Claude Code adds to the context of that prompt. A note is suggested once per session. Any error
   (no config, bad JSON) means no output; the hook never blocks a prompt.

Optional MCP server (tools `recall_search` and `recall_get`), in `.mcp.json`:

```json
{"mcpServers": {"recall": {"command": "recall", "args": ["mcp"]}}}
```

## Config: `.recall.json`

The file sits in the project root. The root is found from `--root`, else `$CLAUDE_PROJECT_DIR`
(if it holds a config), else the nearest parent folder with a `.recall.json`. All paths in the
config and in the output are relative to that root.

```json
{
  "sources": [
    {"glob": "docs/decisions/*.md", "kind": "decision", "split": "bullet"},
    {"glob": "docs/runbooks/*.md", "kind": "runbook", "split": "heading"},
    {"glob": "docs/**/*.md", "kind": "doc", "split": "file", "exclude": ["**/README.md"]}
  ],
  "synonyms": "recall-synonyms.json"
}
```

A file belongs to the first source that matches it. A full example with every option is in
[`examples/recall.json`](examples/recall.json).

### Sources

| Field | Meaning |
|---|---|
| `glob` | Files to read, relative to the root. `**` is recursive. Must stay inside the project. |
| `source` | Instead of `glob`: `"claude-memory"` reads Claude Code's per-project memory folder (below). |
| `kind` | A label of your choice (`decision`, `runbook`, ...). Used by `kind_weights` and `hook_skip_kinds`. |
| `split` | How a file becomes notes: `file`, `bullet`, `heading` or `memory` (below). Default `file`. |
| `level` | `heading` only: which heading level starts a note. Default `2` (`##`). |
| `exclude` | Globs to skip for this source. |

| `split` | One note is | Note id | Good for |
|---|---|---|---|
| `file` | The whole file: title = first `# ` heading (else front-matter `title`/`name`, else the file name); summary = first paragraph of a section named Conclusion / Summary / Decision / Outcome / TL;DR, else front-matter `description`, else the first paragraph. Only the first 8,000 characters are read. | `path` | One report or ADR per file |
| `bullet` | Each top-level `- ` or `* ` line, with its indented continuation lines. Date = first `YYYY-MM-DD` in the bullet line. A bullet that starts with `~~struck text~~` counts as superseded. | `path:line` | Decision logs, "one line per lesson" files |
| `heading` | Each `##` section (or the level you set), including its deeper sub-sections. Date = first `YYYY-MM-DD` in the heading. | `path:line` | Runbooks, FAQs, long design docs |

| `memory` | One agent-memory file: title = front-matter `name`; searchable text = `description` + the first 8 body lines (the **Why:** / **How to apply:** lines) + `metadata.type`. | `path` | Memory notes kept inside the repo |

### Claude Code memory folder

Claude Code keeps per-project memory notes outside the repo, in
`<config dir>/projects/<slug>/memory/*.md`. One source line adds them:

```json
{"source": "claude-memory"}
```

- The folder is resolved at run time: `$CLAUDE_CONFIG_DIR` if set, else `.claude` in your home
  folder; the slug is the absolute project root with every non-alphanumeric character as `-`.
  Nothing about it is stored in the config, so the config stays safe to commit.
- Notes are shown as `memory/<file>.md`. The real folder (it contains your user name) is never
  printed, not by the hook, the CLI, `--json`, `stats` or the MCP tools.
- The `memory` split is always used; the default `kind` is `memory`. Only `*.md` directly in the
  folder are read, and the `MEMORY.md` index is skipped. `exclude` takes file-name globs.
- Hook mode includes memory notes by default and ranks them like any other note: Claude Code
  loads the index lines into context, but not the bodies. To leave them to the CLI and MCP only,
  set `"hook_skip_kinds": ["memory"]`.

### Front matter (optional)

```
---
status: superseded        # active (default) | superseded | cold | draft
superseded_by: 0007-new-cache.md   # a note id, a path, a file name, or a file-name prefix
topic: cache              # notes with the same topic: the hook suggests only the newest
date: 2030-04-11
tags: [redis, eviction]   # tags, keywords, area and topic are searchable
---
```

For `bullet` and `heading` sources the front matter applies to every note in the file.

| Status | CLI and MCP | Hook |
|---|---|---|
| `active` | shown | shown |
| `draft` | shown, labelled `(draft)` | shown, labelled |
| `superseded` | shown last, labelled with its replacement | replaced by the note in `superseded_by` |
| `cold` | shown last, labelled | never shown |

### Other options

| Option | Default | Meaning |
|---|---|---|
| `exclude` | `["**/node_modules/**"]` | Globs skipped for every source. |
| `synonyms` | `{}` | An object, or the path of a JSON file holding one. See below. |
| `stopwords` | `[]` | Extra words to ignore, added to the built-in English + Vietnamese lists. |
| `keep_words` | `[]` | Built-in stopwords you want to keep searchable. |
| `top` | `3` | Max notes per prompt in hook mode. |
| `max_bytes` | `1000` | Hard cap on the hook output, in UTF-8 bytes. |
| `summary_chars` | `160` | Summary length per note. |
| `min_words` | `4` | Hook only: prompts with fewer content words print nothing ("ok do it"). |
| `min_hits` | `2` | A note must match at least this many distinct prompt words. |
| `min_score` | `5.0` | Minimum IDF score. Raise it for fewer, surer suggestions. |
| `recency_floor`, `recency_days` | `0.85`, `365` | A dated note loses up to 15 % of its score as it ages to one year. `1.0` turns this off. |
| `kind_weights` | `{}` | Score multiplier per kind, for example `{"runbook": 0.9}`. |
| `hook_skip_kinds` | `[]` | Kinds the hook never prints (for example notes that are already in the agent's context). |
| `skip_prefixes` | `[]` | Hook only: prompts starting with one of these print nothing. |
| `summary_headings` | conclusion, summary, ... | Section names that hold the one-line conclusion of a `file` note. |
| `header` | see example | First line of the output. |
| `embedding` | off | Optional vector search, see below. |

### Synonyms: prompts in one language, notes in another

If you write prompts in Vietnamese and keep notes in English, nothing matches by itself. A synonyms
file maps prompt phrases to the words used in the notes:

```json
{"trợ năng": "accessibility", "hàng đợi": "queue", "bộ nhớ đệm": ["cache", "eviction"]}
```

Phrases are matched on accent-folded, lower-cased text with word boundaries, so `tro nang`,
`trợ năng` and `Trợ Năng` are the same phrase and `nang` alone does not trigger it. The target
words are added to the prompt's search words. Starter list: [`examples/synonyms-vi-en.json`](examples/synonyms-vi-en.json).
The same mechanism handles team jargon (`"the big table": "ledger"`).

Accent folding is generic (Unicode NFD, combining marks dropped, `đ` to `d`), so it works for any
Latin-script language. Languages written without spaces are not word-split and will not match well.

## How ranking works

1. **Words.** The prompt is folded (lower case, no accents), split into words, stopwords and
   one-letter words dropped, a trailing plural `s` removed, synonym targets added.
2. **Match text.** For each note: its path, title, summary, tags/topic, and for bullets and
   sections the first 700 characters of body.
3. **IDF score.** Each matched prompt word adds `ln(N / notes containing the word)`. A word found in
   one note of a thousand is worth about 6.9; a word found in half of them about 0.7. Rare words
   decide the ranking, common words barely count.
4. **Gate.** A note needs `min_hits` distinct words and a score of at least `min_score` (scaled down
   for projects with only a few dozen notes). Most prompts pass no note through this gate, and then
   nothing is printed.
5. **Adjust.** Score times the recency factor times the kind weight.
6. **Hook selection.** Drop `cold`; swap `superseded` for its replacement; keep the newest note per
   `topic`; drop notes already shown in this session; keep the `top` best; cut to `max_bytes`.

No index is stored. Every run reads the source files again, so there is nothing to rebuild after
an edit. `recall stats` shows the note count and the time it takes.

## Optional: vector search

Off by default. Lexical matching misses paraphrases ("how much room does it need" vs "enclosure
sizing"). If you run or rent an OpenAI-compatible embeddings endpoint you can add a second ranking:

```json
{"embedding": {"url": "http://localhost:8080/v1/embeddings", "model": "your-model-name",
               "api_key_env": "MY_EMBED_SECRET"}}
```

```
recall embed     # embeds new or changed notes into .recall-cache/ (add it to .gitignore)
```

`RECALL_EMBED_URL` and `RECALL_EMBED_MODEL` override the config; `RECALL_NO_VEC=1` turns it off.
At query time the prompt is embedded (1.5 s timeout), compared with the cached vectors in pure
Python, and fused with the lexical ranking by reciprocal rank fusion. A note found only by vectors
must reach `min_cos` (default 0.6, model dependent). If the endpoint is down or slow, recall falls
back to lexical ranking without an error. Expect roughly +50 ms per 1,000 notes plus the request.

## Privacy

- Without an `embedding` block, recall-kit makes no network calls at all. It reads the files named
  by your globs and writes to stdout.
- With `embedding` set, `recall embed` sends note text, and every search sends the prompt, to the
  URL **you** configured, and nowhere else. There is no default host.
- The secret for the endpoint is read from an environment variable; it is never written to disk.
- Session dedupe keeps one small file per session in the OS temp folder (note ids only).
- Output and the MCP tools only ever use paths relative to the project root (`memory/<file>.md`
  for the Claude Code memory folder). `recall_get` returns only notes that your sources select;
  it is not a general file reader.

## CLI

```
recall "question"           search (up to 6 notes, plus superseded ones, labelled)
recall --json "question"    same, as JSON with scores
recall --hook               Claude Code UserPromptSubmit mode
recall init                 write a starter .recall.json
recall stats                files and notes per source, timing
recall mcp                  MCP stdio server
recall embed                build or refresh the vector cache
  --root DIR  --config FILE  --top N
```

`python -m recall_kit` is the same as `recall`.

## Development

```
python -m unittest discover -s tests -v    # no dependencies; pytest works too
python tools/leak_check.py                 # no private paths, addresses or secrets in the tree
```

## License

MIT
