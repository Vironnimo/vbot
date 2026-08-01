---
name: pdf
description: Create, restyle, inspect, and visually verify polished PDF documents. Use when the user asks to create a PDF, turn supplied content into a PDF, improve an existing PDF, review PDF layout, or deliver a professional report, brief, handout, or printable document as a .pdf file.
---

# PDF

Produce a finished document, not merely a file that opens. The quality loop is mandatory: generate, inspect structurally, render every page, inspect the rendered pages with vision, revise, and repeat.

## Tools and paths

- Use vBot's `write` or `edit` Tool for source/spec files, `bash` for the bundled scripts, and `read` for every rendered PNG page.
- Resolve `{baseDir}` to this Skill's absolute directory when invoking bundled scripts.
- Keep intermediates under `tmp/pdfs/<task-name>/` in the effective cwd.
- Keep final documents under `output/pdf/` in the effective cwd unless the user requested another location.
- Preserve every source PDF. Write edits to a new descriptive filename.
- Never install packages or system binaries without the user's permission.

## Dependency probe

Before creating a new PDF, run:

```text
python -c "import reportlab; print('reportlab ready')"
```

Before rendering, run the bundled renderer with `--help`, then render normally. It accepts either the `pdftoppm` binary or the `pypdfium2` Python package. If creation or rendering dependencies are missing, name the missing dependency and ask before installing anything. Do not silently switch to an unverified text-only result.

## Creation workflow

1. Establish the document goal, audience, page size, language, visual tone, required content, and any brand assets. Infer ordinary choices from context instead of running a long intake interview.
2. For reports, briefs, memos, handouts, and other conventional documents, write a UTF-8 JSON specification and run `python {baseDir}/scripts/create_pdf.py <spec.json> <output.pdf>`. The supported block types are `paragraph`, `heading`, `bullets`, `table`, `callout`, `image`, `spacer`, and `page_break`.
3. For a genuinely custom layout that the JSON builder cannot express, write a task-local ReportLab builder under the task's intermediate directory. Keep the same inspection and rendering loop.
4. Use a restrained hierarchy: one dominant title, clear section headings, readable body text, consistent spacing, and one accent color. Prefer whitespace over decoration.
5. Use a discovered TrueType font when the content contains non-ASCII text. The bundled builder searches common Windows, Linux, and macOS font locations and accepts explicit `--font-regular` and `--font-bold` paths.
6. Make tables wrap text, repeat header rows, and split across pages. Never force a whole long section or table onto one page.
7. Keep citations, labels, legends, footers, and page numbers human-readable. Never leave placeholders, Tool tokens, debug text, or raw markup in the document.

Minimal specification shape:

```json
{
  "title": "Quarterly Review",
  "subtitle": "Decisions and next steps",
  "author": "Operations",
  "summary": "A concise summary shown below the title.",
  "theme": {"accent": "#2563EB"},
  "sections": [
    {
      "heading": "Overview",
      "blocks": [
        {"type": "paragraph", "text": "Opening paragraph."},
        {"type": "bullets", "items": ["First point", "Second point"]},
        {"type": "callout", "title": "Decision", "text": "The agreed direction."}
      ]
    }
  ]
}
```

## Mandatory verification loop

1. Run `python {baseDir}/scripts/inspect_pdf.py <output.pdf>` and resolve every reported structural error.
2. Run `python {baseDir}/scripts/render_pdf.py <output.pdf> <render-directory> --dpi 170`.
3. Call vBot's `read` Tool on every rendered PNG, in page order. For long documents, inspect pages in concurrent batches of at most four.
4. Check every page for clipped or overlapping text, broken glyphs, weak hierarchy, cramped margins, awkward page breaks, orphaned headings, split callouts, table overflow, inconsistent alignment, low-resolution images, accidental blank pages, and unreadable headers or footers.
5. Revise the JSON/specification or builder, regenerate, rerun structural inspection, and rerender. Never reuse PNGs from an older PDF revision.
6. Stop only when the latest rendered pages show no visible defect. If the current Model cannot receive images, say explicitly that structural checks passed but visual verification was not possible; never describe the PDF as visually verified.

## Existing PDFs

- Inspect before modifying. Use `read` for bounded text extraction and `inspect_pdf.py` for structure, then render all relevant pages.
- Preserve page size, orientation, metadata, links, and form behavior unless the requested change requires otherwise.
- For fillable forms, validate both the canonical field tree and page widgets after writing. A rendered value alone does not prove that the stored field value is correct.
- Do not flatten an interactive or signed PDF unless the user explicitly asks for a static result and accepts the consequence.

## Delivery

- Confirm the final filename, absolute path, page count, and whether visual verification completed.
- In a Channel Session, deliver the file with `channel_send` and include the final path in `file_paths`; do not rely on a textual path as delivery.
- In an interactive Session without a file-publishing Tool, provide the absolute path clearly.
- Remove task-local rendered PNGs and disposable builders only after the final PDF is safely written and the user no longer needs the intermediates.
