# Extended Session search and transcripts

This covers what `session_search` cannot give you: a list of Sessions, the complete wording of a Message, a Session's whole transcript, Messages before a Compaction, and exact Tool Results. Answer from `session_search` excerpts when they hold the evidence. Narrowing a search finds other excerpts; it does not expand one excerpt into a full Message. A later Message can revise an earlier decision.

## List, inspect and measure Sessions

Replace `<session-id>` with an exact id from `session list` or a Summary's Sub-Agent list. These commands call the server and need no data-directory access.

```bash
vbot session list --limit 5
vbot session list --agent coder@my-repo
vbot session usage <session-id> <another-session-id>
vbot session show <session-id> --summary
vbot session show <session-id>
vbot session show <session-id> --entry <seq>
```

- `list` spans Agents and includes Sub-Agents and Cron, newest activity first. It names each Session's owner, type and activity state. `--agent <agent-or-agent@project>` filters by owner; `--parent <session-id>` lists direct Sub-Agent Sessions. Follow `Next:` when the target is on another page.
- `usage` accepts 1 to 20 ids and reports own consumption, Sub-Agents at every depth and tree totals. The combined total counts each Session once, even when you select both a Parent and its child. It covers requests assigned to the Sessions' own Runs, including failed attempts; inherited history is excluded. Reported USD costs and API-equivalent estimates stay separate; unknown costs are not zero or subscription charges. `--json` carries all counters and cost provenance.
- `show --summary` is a deterministic compact activity view: the initial task, latest Assistant text, chronological activity with Tool outcomes, and direct Sub-Agent Sessions with ids, titles and activity states. It makes no Model request. Summarize its evidence in your own words; a pending call is not a completed effect. `recorded-result=running` with a Command id means the Tool returned while that command continued; it is the recorded result, not a live process check.
- `show` without `--summary` reads transcript text, Tool arguments and Results. Both views start with the newest bounded page and print `Next:` for older entries. `--run <run-id>` limits the view to a Run named in the output. Follow `Next:` when the requested work spans older entries; the initial task and latest Assistant text in a Summary cover its whole selected history.
- `[seq]` identifies an entry in this Session's current history. `show --entry <seq>` reads its text and, for a Tool Result, its arguments. Follow `Next:` for remaining characters. Shortened text is marked; it is not a complete quotation. Both views exclude Reasoning and internal Notes and retain original conversation before Compaction.
- Session ids need no Agent argument. When an id matches several live Sessions, the error names their owners; repeat with `--agent <owner>` for the intended one. For an ambiguous `list --parent` target, use `--parent-agent <owner>`.
- A Summary lists up to 10 direct children and prints a `session list --parent` command when more exist. Read a child's own Summary for its work and descendants. Archived Sessions are accessed through `vbot archive list` and `vbot archive show <entry-id>`.

## Extended search script

Use the script below for substring searches, generation-bound exact Results from its search hits, or all resultless Tool declarations. Normal transcripts and activity reads use the CLI above.

The script `scripts/sessions.py` of this Skill reads the Session database read-only on the server's computer. Run it with `python`; replace `<skill-directory>` with the directory the `skill` Tool reported when it loaded this Skill. Its first output line is a summary; each further line is one JSON object.

```bash
python <skill-directory>/scripts/sessions.py transcript
python <skill-directory>/scripts/sessions.py search "exact fragment"
python <skill-directory>/scripts/sessions.py search "exact fragment" --all-sessions
python <skill-directory>/scripts/sessions.py result --seq 123 --generation <generation-id>
python <skill-directory>/scripts/sessions.py transcript --agent coder@my-repo --session <session-id>
```

- Without `--agent` and `--session` it reads the Session of your current Run, including the Messages before its Compaction checkpoints. For another Session, pass both; `--agent` takes `agent` or `agent@project`.
- `transcript` prints the User and Assistant Messages in order, 200 per page; Content Blocks stay a list, and each Assistant Message lists its Tool calls with the `result_seq` that `result` takes. When the summary says more exist, repeat with the `--after-seq` it names.
- `search` finds text in Messages and Tool Results, ignoring case, and prints up to 50 matches with a 2,000-character preview around each. It does not search reasoning or Tool arguments. `--all-sessions` searches every live Session of the Agent; a fork then repeats the history it shares with its origin. `--role user|assistant|tool` narrows it. A match missing from a bounded result is not evidence that the text is absent.
- `result` prints one complete Tool Result. Pass the `seq` and `generation_id` that `search` or `transcript` printed; the generation names the Session, so `--agent` and `--session` are not needed. When the Session was replaced since, search again instead of reusing the seq.
- The script finds the data directory through `VBOT_DATA_DIR` or `vbot home`; otherwise pass `--data-dir` with the `data_dir` that `vbot home` prints on the server's computer.
- `transcript` leaves out Compaction summaries and prints the original Messages they replaced in your context.
- Archived Sessions are not read. They belong to archive entries: `vbot archive list` lists them and `vbot archive show <entry-id>` names an entry's Sessions.

Direct reads can reveal internal or delegated Sessions that `session_search` hides; keep the scope to what the user's request needs.
