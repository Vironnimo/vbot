# Extended Session search and transcripts

This covers what `session_search` cannot give you: a list of Sessions, the complete wording of a Message, a Session's whole transcript, Messages before a Compaction, and exact Tool Results. Answer from `session_search` excerpts when they hold the evidence. Narrowing a search finds other excerpts; it does not expand one excerpt into a full Message. A later Message can revise an earlier decision.

## Find the Session

`vbot session list <agent-id> --all` lists an Agent's Sessions with ids, titles and last activity; use `<agent-id>@<project-id>` for a Project Agent.

## Read it with the script

The script `scripts/sessions.py` of this Skill reads the Session database read-only on the server's computer. Run it with `python`; replace `<skill-directory>` with the directory the `skill` Tool reported when it loaded this Skill. Its first output line is a summary; each further line is one JSON object.

```bash
python <skill-directory>/scripts/sessions.py transcript
python <skill-directory>/scripts/sessions.py search "exact fragment"
python <skill-directory>/scripts/sessions.py search "exact fragment" --all-sessions
python <skill-directory>/scripts/sessions.py result --seq 123 --generation <generation-id>
python <skill-directory>/scripts/sessions.py transcript --agent coder@my-repo --session <session-id>
```

- Without `--agent` and `--session` it reads the Session of your current Run, including the Messages before its Compaction checkpoints. For another Session, pass both; `--agent` takes `agent` or `agent@project`.
- `transcript` prints the User and Assistant Messages in order, 200 per page; Content Blocks stay a list. When the summary says more exist, repeat with the `--after-seq` it names.
- `search` finds text in Messages and Tool Results, ignoring case, and prints up to 50 matches with a 2,000-character preview around each. It does not search reasoning or Tool arguments. `--all-sessions` searches every live Session of the Agent; a fork then repeats the history it shares with its origin. `--role user|assistant|tool` narrows it. A match missing from a bounded result is not evidence that the text is absent.
- `result` prints one complete Tool Result. Pass the `seq` and `generation_id` that `search` or `transcript` printed; when the Session was replaced since, search again instead of reusing the seq.
- The script finds the data directory through `VBOT_DATA_DIR` or `vbot home`; otherwise pass `--data-dir` with the `data_dir` that `vbot home` prints on the server's computer.
- `transcript` leaves out Compaction summaries and prints the original Messages they replaced in your context.
- Archived Sessions are not read. They belong to archive entries: `vbot archive list` lists them and `vbot archive show <entry-id>` names an entry's Sessions.

Direct reads can reveal internal or delegated Sessions that `session_search` hides; keep the scope to what the user's request needs.
