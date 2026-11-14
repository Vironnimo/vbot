# 1Password setup

The `onepassword` Extension makes entries of the user's password manager a source for Agent Secrets; it adds no Tools. The user binds a Secret to an entry and unlocks the source in Settings -> Secrets, which also shows whether the source is unavailable, locked or available, and why.

1Password reads the vaults of a 1Password service account through the 1Password CLI `op`, which must be installed on the server's computer. The user creates the service account in their 1Password account with read access to the vaults vBot needs, then saves its token in their terminal with `vbot extensions set onepassword token --stdin` or enters it at each unlock in Settings -> Secrets.
