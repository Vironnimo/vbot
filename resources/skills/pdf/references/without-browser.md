# Creating a PDF without a browser

How to build a document from Markdown when `create` reports that no browser can print HTML. The result is a clean, single-column document with a title block, page numbers and bookmarks; the layout choices of the HTML templates are not available.

## What the Markdown builder can and cannot do

It can: a title block (title, subtitle, author, date), headings in four levels, paragraphs, bold, italic, strikethrough, inline code, links, bulleted, numbered, nested and task lists, tables with column alignment, quotes and coloured notes, code blocks, images with captions, horizontal rules, page breaks, a table of contents with page numbers, an accent colour, page numbers in the document's language, and a running header from page 2.

It cannot: place an address in an envelope window, set columns, side-by-side boxes, backgrounds, custom fonts or sizes, or full-bleed pages. Letters, invoices, flyers and slides therefore come out as plain documents. Say so in your report and name the browser install command from the `create` message.

## Write the file

Save the Markdown as UTF-8 with the extension `.md`, next to its images. Create the PDF with the same `create` command as for HTML, giving the `.md` file as the source:

```
python <skill-dir>/scripts/pdf.py create <document.md> <document.pdf>
```

`<skill-dir>` is the directory in the commands of the Skill's instructions; on Linux and macOS, use `python3`. Then check the result with `render` as for every created document.

The optional front matter between `---` lines sets:

| Key | Values | Default |
|---|---|---|
| `title` | the title in the title block and the PDF properties | the first `#` heading |
| `subtitle` | a line below the title | none |
| `author` | shown below the title and in the PDF properties | none |
| `date` | shown next to the author | none |
| `lang` | language code such as `de` or `en`; sets the page labels | `en` |
| `page` | `A4`, `A5`, `Letter` or `Legal` | `A4` |
| `orientation` | `portrait` or `landscape` | `portrait` |
| `accent` | hex colour for the title bar, links, table rules and bullets | `#1F4E79` |
| `toc` | `true` adds a table of contents with page numbers after the title | none |

Syntax beyond standard Markdown:
- Page break: a line with only `\pagebreak`.
- Coloured note: a quote whose first line is `[!NOTE]`, `[!TIP]`, `[!IMPORTANT]`, `[!WARNING]` or `[!CAUTION]`, optionally followed by a title.
- Image with caption: a line with only `![Caption](picture.png)`. Paths are relative to the Markdown file. Images are scaled to the text width and at most 120 mm high.
- Task list: `- [ ] open` and `- [x] done`.
- Table column alignment: `:---` left, `:---:` centred, `---:` right in the separator row.

## Example

```markdown
---
title: Umzug der Geschäftsstelle
subtitle: Ablauf, Termine und Zuständigkeiten
author: Verwaltung
date: 2. Oktober 2026
lang: de
accent: "#0F4C5C"
---

## Das Wichtigste

Die Geschäftsstelle zieht am **14. November 2026** in die Bahnhofstraße 12. Am 13. und 14. November bleibt sie geschlossen.

> [!IMPORTANT] Bitte bis 6. November erledigen
> Persönliche Unterlagen in die beschrifteten Kisten packen.

## Termine

| Datum | Schritt | Zuständig |
|:---|:---|:---|
| 6. November | Kisten gepackt | alle |
| 13. November | Möbel und Technik | Umzugsfirma |
| 17. November | Erster Arbeitstag | alle |

## Checkliste

- [x] Mietvertrag unterschrieben
- [ ] Internetanschluss bestellt

\pagebreak

## Anfahrt

![Lageplan der neuen Geschäftsstelle](lageplan.png)
```

## Characters and fonts

The builder uses TrueType fonts installed on the host and embeds them. It looks for a fallback font for each character the main font lacks. Characters no font covers print as empty boxes; `create` lists them under `Problems:`. Replace them with words, or ask the user to install a font with TrueType outlines that covers them. Emoji print in one colour at best; leave them out.
