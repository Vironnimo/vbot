# Contributing to vBot

Thanks for your interest in vBot. It is alpha software maintained by a small team, so the most helpful contributions are clear bug reports, concrete ideas, and focused pull requests.

## Where things go

| You want to | Go to |
|---|---|
| Ask a question or get help with your setup | [Discussions → Q&A](https://github.com/Vironnimo/vbot/discussions/categories/q-a) |
| Suggest an idea or discuss a bigger change | [Discussions → Ideas](https://github.com/Vironnimo/vbot/discussions/categories/ideas) |
| Show what you built with vBot | [Discussions → Show and tell](https://github.com/Vironnimo/vbot/discussions/categories/show-and-tell) |
| Report something that is broken | [New bug report](https://github.com/Vironnimo/vbot/issues/new?template=bug_report.yml) |
| Propose a specific, well-scoped feature | [New feature request](https://github.com/Vironnimo/vbot/issues/new?template=feature_request.yml) |
| Report a security vulnerability | Privately, as described in [SECURITY.md](SECURITY.md), never in a public issue |

## Pull requests

- **Open an issue or discussion first** for anything larger than a small fix, so we can agree on the approach before you invest time.
- **Keep a pull request to one change.** Unrelated fixes belong in separate pull requests.
- **Add or update tests** for changed behavior. Tests cover contracts at a module's public interface, not implementation details; a bug fix usually extends the existing test for that behavior.
- **Update the documentation** your change affects: [README.md](README.md), [USAGE.md](USAGE.md), or the files under `resources/skills/vbot-cli/references/`.
- **Commit messages** use the conventional format `<type>(<scope>): <what>`, lowercase, at most 72 characters, no trailing period. Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`. Example: `fix(calendar): keep reminders on the configured time zone`.
- Contributions are licensed under the project's [Apache-2.0 license](LICENSE).

## Development setup

Requirements: Python 3.14 (the version the packages bundle; vBot supports no other), Node.js 22 or newer with npm, and Git.

```bash
git clone https://github.com/Vironnimo/vbot.git
cd vbot
pip install -e ".[dev]"
python -m cli.search_runtime
git config core.hooksPath .githooks
cd webui
npm ci
npm run build
cd ..
```

`cli.search_runtime` provisions the ripgrep executable that the search Tools need. The Git hooks format, lint and type-check your staged files.

Start a development server with its own data directory and port, so it never touches an installed vBot at `~/.vbot` on port 8420:

```bash
python server/main.py --data-dir ~/.vbot-dev --port 8421
```

Then open [http://127.0.0.1:8421/](http://127.0.0.1:8421/). Rebuild the WebUI with `npm run build` in `webui/` after frontend changes.

## Running checks

Run the tests that cover your change while you work:

```bash
python -m pytest tests/core/tools
python -m ruff check --fix <paths>
python -m ruff format <paths>
python -m mypy
cd webui
npx vitest run src/lib
npm run lint
```

CI runs the complete suites on Linux and Windows on every push to `main` and for every release. More details, including the Vite development server and Windows packaging, are in [Development and verification](USAGE.md#development-and-verification).
