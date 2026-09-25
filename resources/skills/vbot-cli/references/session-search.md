# Extended Session search and transcripts

Start with `session_search` for a focused question about a past conversation. Answer directly when the returned evidence is sufficient; partial context alone does not require reading the whole Session. Use this reference when evidence is missing, exact or complete wording is requested, or you need to list Sessions or inspect a Tool Result. Narrowing a search finds other excerpts; it does not expand one excerpt into a full Message. A later Message may revise an earlier decision.

## Locate and select

If the available Tools cannot retrieve missing text, use only the evidence you can read and state what remains unknown. Search refinements should use supported clues; do not try to reconstruct unseen wording by repeatedly guessing unrelated words.

`vbot session list <agent-id> --all` lists an Agent's Sessions; use `<agent-id>@<project-id>` for a Project Agent. For direct reads, run `vbot home` on the server machine and use its exact `data_dir`. The database is `<data_dir>/sessions.db`. Do not infer a remote server's data directory from a local CLI invocation.

The following Python snippets use only the standard library. Run the setup and the relevant query in one script through the shell Tool. Replace the example path and identifiers with the actual target. Open read-only: never write the database or make a copy just to search it.

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

# The Session's current history as "view": its own current entries plus the
# history a fork shares with the Session it was forked from. Start each query
# with this text; its three parameters are the scope.
view_sql = """
WITH target AS (
  SELECT session_key, generation_id, session_id FROM sessions
  WHERE project_id = ? AND agent_id = ? AND session_id = ? AND state = 'live'
), view AS (
  SELECT t.generation_id, t.session_id, e.entry_key, e.seq, e.entry_id, e.role, e.created_at
  FROM target AS t JOIN entries AS e ON e.session_key = t.session_key
  WHERE e.superseded_at_seq IS NULL
  UNION ALL
  SELECT t.generation_id, t.session_id, e.entry_key, e.seq, e.entry_id, e.role, e.created_at
  FROM target AS t JOIN session_lineage AS l ON l.session_key = t.session_key
  JOIN entries AS e ON e.session_key = l.ancestor_key
   AND e.seq >= l.from_seq AND e.seq < l.upto_seq
   AND (e.superseded_at_seq IS NULL OR e.superseded_at_seq >= l.as_of_seq)
)
"""
```

Scope mapping matters: a Tool result may use `project_id: null` for Identity Sessions. SQLite stores that scope as the empty string `""`, **not SQL NULL**; use `project_id = ?` with `""`, never `IS NULL`.

`sessions` contains live and archived generations; `view_sql` matches the full address and `state = 'live'`. `entries` holds every history item of a Session in `seq` order, `entry_text` holds its text (`content`, or Content Blocks as `blocks_json` with their text in `search_text`), and `tool_calls` links each invocation to its result entry. An edit keeps the history it replaced but marks it superseded, and a fork reads the history it shares with its origin Session instead of copying it. The `view` part of `view_sql` applies both rules, so read `view` rather than `entries` directly. `seq` is canonical order and disambiguates repeated Message ids. Compaction summaries have role `compaction_checkpoint`; they are summaries, not verbatim User or Assistant text. Do not confuse a same-named Session in another Agent or Project with the selected target.

## Read User and Assistant Messages

This reads the entire current conversation in order, including earlier Messages retained through Compaction. Content Blocks are preserved as a list rather than silently dropping attachments or file mentions. For long Sessions, add `AND v.seq > ?` to the `WHERE` conditions and `LIMIT ?` after `ORDER BY`, passing the last returned `seq` and a page size, or write the transcript to a local output file and inspect relevant sections.

```python
transcript_sql = (
    view_sql
    + """
SELECT v.generation_id, v.seq, v.entry_id AS message_id, v.role, v.created_at AS timestamp,
       x.content, x.blocks_json
FROM view AS v LEFT JOIN entry_text AS x ON x.entry_key = v.entry_key
WHERE v.role IN ('user', 'assistant')
ORDER BY v.seq
"""
)
for row in db.execute(transcript_sql, scope):
    item = dict(row)
    blocks = item.pop("blocks_json")
    if blocks is not None:
        item["content"] = json.loads(blocks)
    print(json.dumps(item, ensure_ascii=False))
```

## Search exact text, including Tool Results

For a known Session, a simple substring search is useful when the ordinary search excludes the evidence you need. This searches canonical content, extracted Content Block text and Tool Result content; it does not search reasoning or Tool arguments. Change the role filter deliberately for a narrower search. `lower()` in stock SQLite handles ASCII case folding; use Python `casefold()` when Unicode-insensitive comparison matters.

```python
query = "exact fragment"
search_sql = (
    view_sql
    + """
SELECT v.generation_id, v.seq, v.entry_id AS message_id, v.role, v.created_at AS timestamp,
       substr(COALESCE(x.content, x.search_text, ''),
              max(1, instr(lower(COALESCE(x.content, x.search_text, '')), lower(?)) - 300),
              2000) AS preview
FROM view AS v JOIN entry_text AS x ON x.entry_key = v.entry_key
WHERE v.role IN ('user', 'assistant', 'tool')
  AND instr(lower(COALESCE(x.content, x.search_text, '')), lower(?)) > 0
ORDER BY v.seq LIMIT 50
"""
)
for row in db.execute(search_sql, (*scope, query, query)):
    print(json.dumps(dict(row), ensure_ascii=False))
```

The preview surrounds the match and is bounded, as is the 50-row limit; an omitted row is not evidence of absence. Narrow the query or continue by `seq`. To search across this Agent's Sessions, remove `AND session_id = ?` from `view_sql` and its parameter, order by `v.session_id, v.seq`, and include `v.session_id` in the result. A fork then repeats the history it shares with its origin Session, so the same Message id can appear under both. Direct SQL can also reveal internal or delegated Sessions that ordinary search hides, so keep the scope relevant to the user's request.

For an exact Tool Result, select its returned `seq` in the same Session and generation:

```python
sequence = 123
generation_id = "generation-id"
result_sql = (
    view_sql
    + """
SELECT v.seq, v.entry_id AS message_id, c.name, x.content AS result_content
FROM view AS v JOIN tool_calls AS c ON c.result_entry_key = v.entry_key
LEFT JOIN entry_text AS x ON x.entry_key = v.entry_key
WHERE v.generation_id = ? AND v.seq = ?
"""
)
row = db.execute(result_sql, (*scope, generation_id, sequence)).fetchone()
print(json.dumps(None if row is None else dict(row), ensure_ascii=False))
db.close()
```

Use the generation returned by the search or transcript query. If the generation changed or the selected Message is gone, locate it again rather than treating the same sequence in a replacement Session as the original evidence. For archived history, explicitly inspect the desired generation instead of broadening every query to all archives.
