# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/Vironnimo/vbot/security/advisories/new)). Do not open a public issue or discussion for a vulnerability.

Include what you found, the affected version (WebUI: **Settings → General → Version**), steps to reproduce, and the impact you expect. You will get an acknowledgement, and we will keep you informed while we work on a fix and agree on the disclosure with you.

## Supported versions

vBot is alpha software. Security fixes go into the latest release; please update before reporting and check whether the problem still occurs.

## Security model

Keep these properties in mind when judging whether something is a vulnerability:

- **Agents act with the operating-system permissions of the account that runs the server.** They can read and write files, run commands and contact external services when their Tools allow it. An Agent doing what its enabled Tools permit is expected behavior.
- **The server has no built-in authentication** and binds to `127.0.0.1` by default. Network access to the server amounts to code execution on the host; remote use belongs behind a VPN, a trusted private network or an authenticated TLS reverse proxy.
- **Trusted Extensions run inside the server process** with the same permissions.
- **`~/.vbot` holds credentials, Sessions and Agent state** and must stay private to that account.

Reports we especially want to hear about: ways to reach the server or its data from outside the configured network boundary, ways around a Tool Access Policy or a Tool permission, credentials leaking into logs, Model requests or other places they should not reach, and flaws in the installer or update verification.
