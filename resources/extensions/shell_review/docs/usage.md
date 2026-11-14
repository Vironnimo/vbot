# Shell Review configuration and preview

The `shell_review` Extension reviews prepared shell commands with vBot's selected Decision Model. It uses the normal permission approval UI when the command needs confirmation, review fails or the Model is unavailable; it does not change Permission Rules.

Inspect the selected Model and the Extension settings:

```bash
vbot task-model status decision
vbot extensions show shell_review
```

The `enabled` setting activates review of shell commands and is off by default. `policy` supplies the review criteria, `allow_threshold` the probability needed for automatic approval and `timeout_seconds` the review deadline. These settings apply live; the probability is a Model estimate, not an accuracy guarantee.

The `review` operation previews a judgment even while the `enabled` setting is off. It neither executes the command nor grants approval:

```bash
vbot extensions run shell_review review --help
```

Pass the request with `--stdin` as a JSON object containing the real `platform`, `shell`, `cwd`, `command` and `user_requests`; the help describes the additional evidence fields. Preserve what the user actually authorized instead of rewriting it to obtain approval. Read the returned judgment and reason before reporting whether automatic approval would apply.
