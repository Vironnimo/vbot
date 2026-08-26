# Setting Up a Free-Capable Provider

Walk the user through this interactively: explain the step in plain language, let the
user perform everything that touches their accounts, and run only the vBot commands
yourself. Never invent, request, or echo an API key in chat output; the user pastes keys
only where they belong. If the user declines setup, accept it, note that free delegation
stays unavailable, and finish the task normally.

## OpenRouter — broadest free lineup

1. Explain the deal: OpenRouter aggregates many upstream Providers; model ids ending in
   `:free` cost nothing within caps — 20 requests/minute, 50 requests/day, raised to
   1000/day once at least $10 in credits has ever been purchased. Buying $10 once is
   optional and permanent for the cap; it is the user's call.
2. User creates an account at `https://openrouter.ai` and creates an API key under API
   keys in their account settings.
3. Connect it (you run):
   ```bash
   vbot provider set-key openrouter <key-from-user> --refresh-models
   ```
   The key lands in the data-dir `.env` via server RPC; the command prints only the
   connection and env-key name.
4. Optionally, point the user to the privacy settings in their OpenRouter account to
   review whether endpoints that may train on inputs are allowed — free endpoint
   policies differ per upstream.
5. Verify (you run): `vbot provider status openrouter`, then
   `vbot model list --provider openrouter --task chat`, then spot-check one candidate
   with `vbot model show <id>` for tool support.

## OpenCode Zen — curated coding-strong free group

Two connection types exist; let the user pick.

- **API key:** the user signs in at `https://opencode.ai/auth`, adds billing details,
  and copies the API key. Tell the user plainly that Zen asks for billing details even
  for the free group and supports auto-reload below a low balance — anyone who wants a
  guaranteed zero spend should disable auto-reload in their Zen workspace settings.
  Then run:
  ```bash
  vbot provider set-key opencode-zen <key-from-user> --refresh-models
  ```
- **OAuth device flow:** run `vbot provider connect opencode-zen`, relay the printed
  user code and verification URL to the user, let them complete it in the browser, then
  poll `vbot provider connect-status opencode-zen` until it reports connected.

Verify exactly as for OpenRouter, using `--provider opencode-zen`.

## After setup

Re-run the inventory (`vbot provider list`, `vbot model list --task chat`), then return
to the free-models workflow to pick candidates and match task fit. Report what was
configured, what the caps mean in practice, and which free Models are ready to use.
