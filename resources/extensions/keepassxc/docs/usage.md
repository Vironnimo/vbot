# KeePassXC setup

The `keepassxc` Extension makes entries of the user's password manager a source for Agent Secrets; it adds no Tools. The user binds a Secret to an entry and unlocks the source in Settings -> Secrets, which also shows whether the source is unavailable, locked or available, and why.

KeePassXC reads one local database through `keepassxc-cli`, which comes with KeePassXC. Set the database file, `key_file` only when the database has a key file, and `cli_path` only when `keepassxc-cli` is neither on PATH nor in the KeePassXC installation folder:

```bash
vbot extensions set keepassxc database "C:\Users\<user>\Documents\Passwords.kdbx"
```

Replace the path with the user's database. The master password has no command: the user enters it at each unlock in Settings -> Secrets, and vBot keeps it in memory until the source is locked or the server restarts.
