---
name: pdf
description: Create well-designed PDFs (letters, invoices, reports, CVs, flyers, slides), edit PDFs (merge, split, rotate, stamp, compress, passwords), fill PDF forms and read scanned PDFs. Use when a task creates, changes, fills or checks a PDF file.
---

# PDF

Create PDF documents that suit their occasion and pass a visual check, and change, fill or read existing PDFs. To only read a PDF that has a text layer, use the `read` Tool; this Skill is not needed. Word, Excel and image files as output are outside this Skill.

## Tools

Every operation is one command of `pdf.py`:

```
python {baseDir}/scripts/pdf.py <command> <arguments>
```

- On Linux and macOS, use `python3` instead of `python`. `python {baseDir}/scripts/pdf.py <command> --help` lists all options of a command.
- The first output line is the result. A failure starts with `Failed:` and says how to fix it.
- Commands that write a file never overwrite their input: give a new output name.
- Pages count from 1; ranges look like `1-3,7,9-`, where `9-` means page 9 to the end.
- Run only `pdf.py`. The folder `pdf_tools` next to it holds its modules (`__init__.py`, `cli.py`, `common.py`, `create.py`, `edit.py`, `env.py`, `fonts.py`, `forms.py`, `inspection.py`, `markdown.py`).

If a command fails with `Missing Python packages`, run the `setup --install` command that the message names, then repeat the failed command. It installs pypdf, pypdfium2, Pillow, ReportLab and cryptography into a private environment of these tools, without administrator rights and without changing any other Python installation. Mention the installation in your report.

HTML documents need a Chromium-based browser: Edge, Chrome or Chromium. `create` finds an installed one. If `create` fails with `No browser can print HTML here`, build the document from Markdown as described in `references/without-browser.md`. In your report, name the install commands from the message; for a letter for a window envelope, an invoice, a flyer, a poster or slides, add that a browser gives the document its proper layout.

## Authority

- On your own: install the Python packages with `setup --install`; create work files in `pdf-work/` and output files.
- Only when the user asked for it: `--flatten` on forms, `--break-signatures`, removing a password, replacing or deleting a file the user gave you.
- A request to change a file, such as adding pages to it or stamping it, asks for a changed copy: write a new file and name it in your report. Replace the original only when the user says to overwrite or replace it.
- Only after the user agreed: installing a browser (also with `setup --install-browser`), system packages or fonts.

## Create a document

1. Collect the content and the facts. Never invent amounts, names, addresses, bank details, tax numbers or dates: if the request lacks one, put `________` in its place and list it in your report. Done when every fact in the document comes from the user, the request or a source you read.
2. Read `references/design.md` and choose the design by its sections 1 and 2. Done when you have chosen the template or direction, the page size, the accent colour and the document's language.
3. Write the HTML in a work folder `pdf-work/<document-name>/` in the working directory. If a template fits, copy it from `{baseDir}/assets/` into the work folder as `<document-name>.html`, replace every `{{...}}` placeholder, translate the lines marked `LABEL`, and delete the blocks the document does not need. The templates are `report.html`, `letter.html`, `invoice.html`, `cv.html`, `notice.html` and `slides.html`. If none fits, write the HTML yourself by sections 4 to 7 of `references/design.md`. Put images next to the HTML file. Done when the HTML contains no `{{`.
4. Create the PDF. Unless the user named a place, write it to the working directory, named for its content in the user's language, such as `Rechnung-2026-014.pdf`:
   ```
   python {baseDir}/scripts/pdf.py create pdf-work/<document-name>/<document-name>.html <output.pdf>
   ```
   The output lists the pages, the embedded fonts and problems:
   ```
   Created <output.pdf>: <pages> page(s), A4 portrait, <size> (msedge.exe).
   File: <absolute path>
   Fonts: <embedded fonts>
   No structural problems found.
   ```
   Done when the first line starts with `Created` and you fixed every line under `Problems:`.
5. Render the pages:
   ```
   python {baseDir}/scripts/pdf.py render <output.pdf> pdf-work/<document-name>/pages
   ```
   Read every overview sheet it names, then the full-size image of page 1 and of each page where a sheet shows something doubtful. For a document of up to 3 pages, read every page image. Check each page for:
   - text cut off, overlapping, or running out of its box or table cell;
   - empty boxes in place of characters;
   - a heading alone at the bottom of a page, a single line alone at the top, a nearly empty last page, blank pages;
   - content that belongs on one page spilling onto a second;
   - weak hierarchy, cramped or uneven spacing, edges that do not line up;
   - images blurry, stretched or missing;
   - page numbers, headers and footers missing or wrong;
   - for letters and invoices: the address inside its field, the footer complete.
   To see a detail up close, render only that part of the page with `--area <left>,<top>,<width>,<height>` in millimetres from the top-left corner. Done when you checked every rendered page against this list.
6. If a check in step 5 failed, change the HTML, then run `create` and `render` again and read the new images; images rendered before the last `create` show the old version. Done when images rendered after the last `create` show none of the defects.
7. If you cannot see images: if an `analyze_image` Tool is available, give it the overview sheets and page images with the list from step 5. Otherwise, state in your report that the PDF passed the structural checks but its look was not checked.

## Change an existing PDF

Run `inspect <file.pdf>` first: it reports pages, page sizes, fonts, form fields, signatures, password protection and defects. Then use:

| Goal | Command |
|---|---|
| Join files | `merge <a.pdf> <b.pdf> -o <joined.pdf>`; `--bookmarks` adds one bookmark per file |
| Keep, remove or reorder pages | `select <in.pdf> <out.pdf> --pages 3,1-2,5-`; pages left out are removed |
| Rotate pages | `select <in.pdf> <out.pdf> --rotate 2:90`; clockwise, `--rotate 90` turns all pages |
| One file per page or per range | `split <in.pdf> <folder>` or `split <in.pdf> <folder> --ranges 1-2,3-` |
| Watermark | `stamp <in.pdf> <out.pdf> --text ENTWURF` |
| Short note in a corner | `stamp <in.pdf> <out.pdf> --text "<note>" --position top-right` |
| Letterhead from another PDF behind the content | `stamp <in.pdf> <out.pdf> --overlay <letterhead.pdf> --under` |
| Smaller file | `compress <in.pdf> <out.pdf>`; for more reduction add `--quality 60 --max-pixels 1600` |
| Password protection | `encrypt <in.pdf> <out.pdf> --password <password>` |
| Remove a password | `decrypt <in.pdf> <out.pdf> --password <password>` |

- If the user asks for password protection without giving a password, create a random one of 16 characters and give it in your report.
- After `stamp`, a rotation or `compress`, render the changed pages and check them.
- These commands cannot change words, numbers or layout inside a page. For such a change, rebuild the document: read its text with the `read` Tool, render its pages to see the look, write it as HTML, and create a new PDF by "Create a document".

## Fill a form

1. List the fields and write a values file:
   ```
   python {baseDir}/scripts/pdf.py fields <form.pdf> --template pdf-work/<form-name>/values.json
   ```
   Done when you know which field takes which fact. If it reports no fillable fields, fill the form with an overlay from `assets/form-overlay.html` as described in `references/flat-forms.md`, instead of the next steps.
2. Edit `values.json`: text as strings, checkboxes as `true` or `false`, radio buttons and lists as one of the options that `fields` listed. Remove the fields you leave unchanged. Done when every value comes from the user or the request.
3. Fill the form:
   ```
   python {baseDir}/scripts/pdf.py fill <form.pdf> pdf-work/<form-name>/values.json <filled.pdf>
   ```
   If it prints `Nothing was written`, fix each listed value and run it again. Done when the first line confirms the filled fields.
4. Render the filled pages and check that each value sits in its field and fits it.

Add `--flatten` only when the user wants a final copy that can no longer be edited. A signed PDF loses its signatures when filled; `fill` refuses it unless you add `--break-signatures`.

## Read a scanned PDF

If `inspect` reports `No text layer on pages ...`, render those pages with `--pages <pages> --dpi 200` and read the page images in page order. Transcribe what the user needs; mark words you cannot read as `[unreadable]`.

## Deliver

- In a chat Channel, send the PDF with `channel_send` in `file_paths`. Elsewhere, write `file:<absolute path of the PDF>` in your reply.
- Delete the `pages` folder of renders after delivery. Keep the HTML in `pdf-work/`, so later changes start from it.
- Report in the user's language, in this order: the file with page count and size; the design you chose, in one sentence; each missing fact you marked; each installation; whether the look was checked on rendered pages; problems left.
