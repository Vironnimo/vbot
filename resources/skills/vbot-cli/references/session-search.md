# Extended Session search and transcripts

Start with `session_search` for a focused question about a past conversation. Answer directly when the returned evidence is sufficient; partial context alone does not require reading the whole Session. Use this reference when evidence is missing, exact or complete wording is requested, or you need to list Sessions or inspect a Tool Result. Narrowing a search finds other excerpts; it does not expand one excerpt into a full Message. A later Message may revise an earlier decision.

## Locate and select

If the available Tools cannot retrieve missing text, use only the evidence you can read and state what remains unknown. Search refinements should use supported clues; do not try to reconstruct unseen wording by repeatedly guessing unrelated words.

`vbot session list <agent-id> --all` lists an Agent's Sessions; use `<agent-id>@<project-id>` for a Project Agent. For direct reads, run `vbot home` on the server machine and use its exact `data_dir`. The database is `<data_dir>/sessions.db`. Do not infer a remote server's data directory from a local CLI invocation.

The following Python snippets use only the standard library. Run the setup and the relevant query in one script through Bash. Replace the example path and identifiers with the actual target. Open read-only: never write the database or make a copy just to search it.

```python
import json
import sqlite3
from pathlib import Path

database = Path("/absolute/data_dir/sessions.db")
db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
db.execute("PRAGMA query_only = ON")
project_id = ""  # Empty for Identity Sessions; exact Project id otherwise.
agent_id = "agent-id"
session_id = "session-id"
scope = (project_id, agent_id, session_id)
```

Scope mapping matters: a Tool result may use `project_id: null` for Identity Sessions. SQLite stores that scope as the empty string `""`, **not SQL NULL**; use `s.project_id = ?` with `""`, never `IS NULL`.

`sessions` contains live and archived generations. Match the full address and `status = 'live'`; join Messages by `session_key`. `messages` stores User and Assistant content; `tool_calls` stores each invocation with its result. The read-only `history_records` view combines these entities in Timeline order. `m.active = 1` excludes history replaced by an edit. `m.seq` is canonical order and disambiguates repeated Message ids. Compaction summaries have role `compaction_checkpoint`; they are summaries, not verbatim User or Assistant text. Do not confuse a same-named Session in another Agent or Project with the selected target.

## Read User and Assistant Messages

This reads the entire active conversation in order, including earlier Messages retained through Compaction. Content Blocks are preserved as a list rather than silently dropping attachments or file mentions. For long Sessions, add `AND m.seq > ?` to the `WHERE` conditions and `LIMIT ?` after `ORDER BY`, passing the last returned `seq` and a page size, or write the transcript to a local output file and inspect relevant sections.

```python
transcript_sql = """
SELECT s.generation_id, m.seq, m.message_id, m.role, m.timestamp,
       m.content, m.content_blocks_json
FROM sessions s JOIN messages m ON m.session_key = s.session_key
WHERE s.project_id = ? AND s.agent_id = ? AND s.session_id = ?
  AND s.status = 'live' AND m.active = 1 AND m.role IN ('user', 'assistant')
ORDER BY m.seq
"""
for row in db.execute(transcript_sql, scope):
    item = dict(row)
    blocks = item.pop("content_blocks_json")
    if blocks is not None:
        item["content"] = json.loads(blocks)
    print(json.dumps(item, ensure_ascii=False))
```

## Search exact text, including Tool Results

For a known Session, a simple substring search is useful when the ordinary search excludes the evidence you need. This searches canonical content, extracted Content Block text and Tool Result content; it does not search reasoning or Tool arguments. Change the role filter deliberately for a narrower search. `lower()` in stock SQLite handles ASCII case folding; use Python `casefold()` when Unicode-insensitive comparison matters.

```python
query = "exact fragment"
search_sql = """
SELECT s.generation_id, m.seq, m.message_id, m.role, m.timestamp,
       substr(COALESCE(m.content, m.content_search, t.result_content, ''),
              max(1, instr(lower(COALESCE(m.content, m.content_search, t.result_content, '')),
                           lower(?)) - 300), 2000) AS preview
FROM sessions s JOIN history_records m ON m.session_key = s.session_key
LEFT JOIN tool_calls t ON t.result_key = m.message_key
WHERE s.project_id = ? AND s.agent_id = ? AND s.session_id = ?
  AND s.status = 'live' AND m.active = 1
  AND m.role IN ('user', 'assistant', 'tool')
  AND instr(lower(COALESCE(m.content, m.content_search, t.result_content, '')), lower(?)) > 0
ORDER BY m.seq LIMIT 50
"""
for row in db.execute(search_sql, (query, *scope, query)):
    print(json.dumps(dict(row), ensure_ascii=False))
```

The preview surrounds the match and is bounded, as is the 50-row limit; an omitted row is not evidence of absence. Narrow the query or continue by `seq`. To search across this Agent's Sessions, remove `s.session_id = ?` and its parameter, and include `s.session_id` in the result. Direct SQL can also reveal internal or delegated Sessions that ordinary search hides, so keep the scope relevant to the user's request.

For an exact Tool Result, select its returned `seq` in the same Session and generation:

```python
sequence = 123
generation_id = "generation-id"
result_sql = """
SELECT m.seq, m.message_id, t.name, t.result_content
FROM sessions s JOIN history_records m ON m.session_key = s.session_key
JOIN tool_calls t ON t.result_key = m.message_key
WHERE s.project_id = ? AND s.agent_id = ? AND s.session_id = ?
  AND s.status = 'live' AND s.generation_id = ? AND m.active = 1 AND m.seq = ?
"""
row = db.execute(result_sql, (*scope, generation_id, sequence)).fetchone()
print(json.dumps(None if row is None else dict(row), ensure_ascii=False))
db.close()
```

Use the generation returned by the search or transcript query. If the generation changed or the selected Message is gone, locate it again rather than treating the same sequence in a replacement Session as the original evidence. For archived history, explicitly inspect the desired generation instead of broadening every query to all archives.
