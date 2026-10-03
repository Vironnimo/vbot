# Bundled pdf Skill

How the bundled `pdf` Skill (`resources/skills/pdf/`) is built: its command-line tool, the environment and browser it uses, its templates and references, and the decisions behind its Agent-facing text.

## Layout

- `SKILL.md` - procedures for creating, changing, filling and reading PDFs, the authority rules and delivery.
- `scripts/pdf.py` - the only entry point Agents run. A thin launcher that puts `scripts/` on `sys.path` and calls `pdf_tools.cli.main()`.
- `scripts/pdf_tools/` - a package with relative imports: `cli` (argparse, command dispatch), `common` (errors, atomic output, page ranges), `create` (HTML through a browser, Markdown through ReportLab, the post-create report), `edit` (merge, split, select, rotate, stamp, compress, encrypt, decrypt), `env` (private environment, browser discovery, font coverage), `fonts` (TrueType fonts for ReportLab), `forms` (AcroForm fields and fill), `inspection` (`inspect`, `render`, overview sheets, grid), `markdown` (the Markdown fallback). A package rather than sibling `_pdf_*.py` modules, because mypy otherwise saw one file under two module names.
- `assets/` - HTML templates: `report`, `letter` (DIN 5008 form B), `invoice`, `notice` (one-page flyer), `cv`, `slides` (16:9), `form-overlay` (answers on a form without fields). Every template uses `{{...}}` placeholders, `LABEL` comments on words to translate, and one `:root` accent colour.
- `references/design.md` (choosing a design direction, typography, colour, Chromium page mechanics, German letter and invoice rules), `references/without-browser.md` (the Markdown fallback), `references/flat-forms.md` (overlay procedure for forms without fields).

## Contracts

- Every command prints its result on the first line, `Failed: ...` plus exit code 1 on errors, and never overwrites its input; output is written atomically. `create` and `inspect` report `Fonts:`, placeholders left as `{{...}}`, a missing title or language, characters no installed font covers, and end with a `Next:` line.
- Scripts run on the host's `python` (3.9+, ruff target `py39` for `resources/skills/*/scripts/**/*.py`) and import only the standard library at startup. Third-party packages (`pypdf`, `pypdfium2`, `pillow`, `reportlab`, `cryptography`; `env.PACKAGES`) come from a private environment that `setup --install` creates with `venv` (falling back to `uv`) at `<cache>/vbot/pdf-tools/env` (`%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, `$XDG_CACHE_HOME` or `~/.cache` elsewhere). When that environment exists, `pdf.py` re-runs itself inside it; `VBOT_PDF_TOOLS_REEXEC=1` stops the re-run (tests set it).
- HTML is printed by a headless Chromium-family browser (`--print-to-pdf`, temporary profile). Discovery (`env.find_browser`) prefers an already-installed Playwright Chromium headless shell (newest revision first), then Edge, Chrome, Chromium on Windows; Chrome, Edge, Chromium, Brave apps on macOS; system binaries on Linux except Snap ones. Other Playwright browser-cache binaries follow (`PLAYWRIGHT_BROWSERS_PATH` or the default cache), then Snap Chromium last because Snap confines file access. A candidate whose shared libraries are missing (`ldd`) is skipped and its problem reported (Linux only). Automatic printing tries another candidate after a browser failure within one shared `--timeout` budget; explicit `--browser` never substitutes another executable. Each attempt gets its own temporary profile and atomic output; a failed attempt leaves the destination unchanged. `setup --install-browser` installs Playwright's Chromium into the private environment; it needs the user's agreement, like system packages and fonts.
- Without a browser, `.md` sources build through ReportLab with front matter (title, lang, page, accent, toc, ...), callouts, task lists and images; it covers reports and notes, not letters, invoices, flyers or slides.

## Chromium print facts the templates rely on

Verified with Edge; `references/design.md` teaches them to Agents. `@page` size, margins, `:first` and margin boxes with `counter(page)`/`counter(pages)` work; margin boxes need their own `font`. Content placed into the page margin is clipped, so fold marks are an `@page :first` background. `position: fixed` is unreliable. Horizontal overflow makes Chromium scale the whole page down (seen ~0.94), which breaks DIN 5008 positions. Backgrounds need `print-color-adjust: exact`. No `target-counter`, so tables of contents carry no page numbers.

## Gotchas

- ReportLab starts each page and each table cell in Helvetica; `markdown.py` sets `initialFontName` and every table's `FONTNAME`, or `Fonts:` lists an unused Helvetica.
- pypdf clones of encoded streams have no `/Length`; `edit._recompress` reads the encoded size from `_data`.
- Importing the package in tests writes `__pycache__` into the Skill unless `sys.dont_write_bytecode` is set; the Skill package must stay free of it.

## Agent-facing text

| Text | Reason |
|---|---|
| Description: `Create well-designed PDFs (letters, invoices, reports, CVs, flyers, slides), edit PDFs (merge, split, rotate, stamp, compress, passwords), fill PDF forms and read scanned PDFs. Use when a task creates, changes, fills or checks a PDF file.` | Names the document kinds users ask for, so the Skill loads for "write a letter as PDF" as well as for PDF operations. |
| Authority: Python packages on the Agent's own; browser, system packages and fonts only after the user agrees; `--flatten`, `--break-signatures`, removing a password only on request. | User decision: installing the private environment changes nothing outside the cache; system installs and irreversible document changes stay with the user. |
| Browser missing: build from Markdown via `references/without-browser.md`, name the install commands in the report, and say which document kinds need a browser for their layout. | The procedure finishes without the user; the user decides about the install afterwards. User decision: a maintained fallback beside the browser path. |
| Create steps: collect facts and never invent them (`________` for gaps), read `design.md`, start from a template, `render` and read the pages against a checklist, use `analyze_image` without vision. | User requirement: documents that fit their purpose and look good, not one generic look; checking rendered pages is what catches layout faults. |
| `Calculate every amount with a command, such as a short python call, never in your head` (`assets/invoice.html`, `references/design.md`) | In the 2026-10 eval one of 12 invoices carried a wrong VAT and gross total; the run had "calculated" in its head. |
| Step 6 and the `create` result after replacing a PDF: `page images rendered before show the old version. Render again ...` | Eval Agents re-ran `create` after a fix and read the earlier PNGs, so they checked a stale look. |
| `render --area LEFT,TOP,WIDTH,HEIGHT` (mm, grid labels stay in page coordinates), named in step 5 and `references/flat-forms.md` | Eval Agents checking checkbox and line positions cropped renders by hand with PIL, which failed where the host Python lacks Pillow. |
| Authority: `A request to change a file, such as adding pages to it or stamping it, asks for a changed copy: write a new file ... Replace the original only when the user says to overwrite or replace it.` | In the eval, one Model replaced `vertrag.pdf` in all 3 runs of "append anlage.pdf to vertrag.pdf". Decided for the user at their request: a wrong new file costs a rename, a wrong replacement loses the original. |
| Password error: `Repeat the command with --password <password> ... If you do not have the password, ask the user for it.` | Agents that had just set the password were told to ask the user for it. |

Tests: `tests/resources/skills/test_pdf_skill.py` runs the commands in-process; only the HTML test starts a browser and skips without one.
