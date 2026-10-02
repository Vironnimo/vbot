# Designing a document

How to choose a design that fits the occasion, and how to build it in HTML for `pdf.py create`.

Contents:
1. Choose the direction
2. Directions by kind of document
3. Text and facts
4. Typography
5. Colour
6. Layout and images
7. Page mechanics in Chromium
8. What makes a document look generic
9. Country conventions: Germany

## 1. Choose the direction

A good PDF looks like it was made for its occasion: a letter to the tax office is sober, an invitation to a summer party is warm and loud, a project report is calm and easy to scan. Answer these questions from the request and the content, then pick a direction in section 2:

- What is it, and what does the reader do with it: pay it, sign it, decide, learn, come to an event, file it?
- Who reads it: an authority, a customer, colleagues, family, the public?
- How is it read: printed and posted, printed at home, on a screen, projected, pinned to a wall from three metres?
- What tone fits: formal, businesslike, friendly, festive, urgent?
- What is fixed: a brand colour or logo the user gave, a legal form, a page size, a page limit?

If the user gave a logo, colours or an older document as a model, follow them over the defaults below.

## 2. Directions by kind of document

The templates are in the Skill's `assets/` folder. Each starts with a comment on how to fill it.

| Document | Start from | Direction |
|---|---|---|
| Business or formal letter, letter to an authority, cover letter for a job | `letter.html` | Sober. One muted accent at most, 10–11 pt text, no decoration. A cover letter takes the accent and fonts of the CV it goes with. |
| Invoice, quote, credit note, order confirmation | `invoice.html` | Numbers first: aligned columns, the total stands out, payment details easy to copy. |
| Report, concept, documentation, analysis, minutes, white paper | `report.html` | Calm and structured: summary first, numbered sections, tables with thin rules. |
| Handout, guide, instructions, checklist | `report.html` | Larger text (11–12 pt), numbered steps, one idea per paragraph, pictures next to the step they show. |
| CV | `cv.html` | Quiet and precise; personality through one accent and the serif name, not through graphics or skill bars. |
| Flyer, poster, notice for a noticeboard, event invitation | `notice.html` | One message readable from a distance, facts (when, where, what) in large type, little text. |
| Presentation, pitch, results for a screen | `slides.html` | 16:9, one idea per slide, headlines that state the point, at least 20 pt text. |
| Certificate, award, voucher, greeting card | write new HTML | A4 landscape or A5, centred composition, a serif display face, generous white space, thin frame or ornament line, signature and date lines. |
| Menu, programme, agenda, price list | write new HTML | A4 or A5 portrait, elegant serif or a clean sans, items with right-aligned prices or times joined by a dotted line, clear groups. |
| Large data tables, lists, schedules | `report.html` with `@page { size: A4 landscape; }` | 8.5–9.5 pt, light row rules or soft zebra rows, header repeated on each page, numbers right-aligned. |

For a private occasion (birthday, wedding, club event), choose a warmer accent and more white space, and set the headline larger; one well-chosen emoji or symbol can carry the mood, a row of them cannot.

## 3. Text and facts

- Write for the reader: put the answer, request or result first, details after it.
- Never invent facts the document states: amounts, names, addresses, bank details, tax numbers, dates, quotes, figures in charts. If the request lacks one, leave a visible gap such as `________` and list each gap in your report.
- Calculate every sum, tax and percentage with a command, such as a short python call, never in your head, and check that the rows add up to the total.
- Write in the language of the request unless the user asks for another, and set `lang` on the `html` element to it.
- Use the language's typography: German „Anführungszeichen" and 1.234,50 €, English "quotes" and €1,234.50; an en dash for ranges (9–17 Uhr, 2024–2026); a non-breaking space between a number and its unit (`10&nbsp;%`, `5&nbsp;km`, `12&nbsp;€`).
- Keep one date format throughout the document.

## 4. Typography

- Text size: 10–11.5 pt for printed documents, 9–10 pt for dense tables and footnotes, at least 20 pt on slides, at least 14 pt for body text on posters.
- Line length: 60–80 characters. On A4 with 20 mm margins and 10.5 pt, a full-width paragraph is near the upper end; for larger text, add margin or a `max-width`.
- Line height: 1.4–1.6 for text, 1.05–1.25 for headings.
- Hierarchy: at most four sizes per document. Make headings different by size and weight, not by colour alone. Emphasise with bold or italic sparingly; never underline for emphasis; use capitals only for short labels, with `letter-spacing: 0.08em` or more.
- Typefaces: one family, or one serif paired with one sans. Each template starts with a font stack that works on Windows, macOS and Linux; the first installed font in the stack is used.
- The `Fonts:` line of `create` names the fonts actually embedded. If it shows a fallback such as DejaVu or Liberation where you wanted another face, the face is not installed on this host.
- Web fonts work when the host can reach the internet: add `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@400;600&display=swap">` in the head and put the family first in the stack. Check the `Fonts:` line to see whether it loaded. Good pairings: Inter or Source Sans 3 for text with Source Serif 4 or Fraunces for headings; IBM Plex Sans with IBM Plex Serif; Playfair Display for festive headlines only.
- Numbers in tables: `font-variant-numeric: tabular-nums`, right-aligned, the same number of decimals per column.
- Text that the fonts cannot show appears as empty boxes; `create` reports the characters. Replace them, or ask the user to install fonts (the message names the package).

## 5. Colour

- One accent colour plus neutral greys. A second accent only for a clear purpose, such as status colours in a table.
- Text in near-black (`#1a1e26`), secondary text in a mid grey (`#5b6372`). Accent text needs strong contrast on its background: as a rule, accents at the dark end of the lists below.
- Accents by mood:

| Mood | Accent |
|---|---|
| Trustworthy, technical | `#1d4ed8` blue, `#1e3a5f` navy |
| Calm, health, environment | `#0f4c5c` petrol, `#0e7490` teal, `#15803d` green |
| Warm, personal, crafted | `#9a3412` terracotta, `#b45309` amber |
| Energetic, sales, events | `#c2410c` orange, `#be185d` raspberry, `#6d28d9` violet |
| Neutral, legal, serious | `#374151` graphite |
| Warning, deadline | `#b91c1c` red, only for the warning itself |

- Large dark or coloured areas suit posters, title slides and screen documents. For documents that people print at home, keep backgrounds white and colour to lines, headings and small areas.
- Never carry meaning by colour alone; add a word or a symbol.

## 6. Layout and images

- Margins at least 20 mm for print, 15 mm for screen-only documents. Align everything to a few clear left edges; centre only titles of posters, certificates and slides.
- Use one spacing scale (for example 2, 4, 6, 9, 14 mm) and the same gap for the same relation everywhere.
- Group what belongs together; separate groups with white space before you add lines or boxes.
- Tables: thin horizontal rules, no vertical lines, header in a smaller muted weight, enough padding.
- Images: at least 150 dpi at the printed size, so a picture 80 mm wide needs about 470 pixels. Give every image a `max-width: 100%` and a caption when it shows data.
- Charts: draw simple bar or line charts as inline SVG in the HTML, so they stay sharp and use the document's colours; label axes and values directly instead of using a legend where possible.
- Logos: use the file the user gave, as SVG or PNG with transparency; never draw a replacement logo.

## 7. Page mechanics in Chromium

- Set the paper with `@page { size: A4; margin: 22mm 22mm 24mm; }`; `A4 landscape`, `A5`, `letter` and sizes such as `338.67mm 190.5mm` work.
- Page numbers and running headers go into `@page` margin boxes such as `@bottom-right { content: "Seite " counter(page) " von " counter(pages); }`. Margin boxes do not inherit the body font: set `font` in each one. `@page :first { ... }` changes the first page.
- Content placed outside the page area, in the margins, is cut off. To draw into the margins, such as fold marks, use a `background` in the `@page` rule, as `letter.html` does.
- Content wider than the page area makes Chromium shrink the whole page. Keep fixed widths inside the page area; for long URLs and words add `overflow-wrap: anywhere`.
- Breaks: `break-before: page` starts a new page; `break-after: avoid` on headings keeps them with the following text; `break-inside: avoid` keeps figures, boxes and table rows together. A table's `thead` repeats on every page.
- `position: fixed` does not repeat reliably on every page; use margin boxes instead.
- A table of contents cannot show page numbers. The PDF gets bookmarks from the headings automatically; for a contents list in the document, list the sections without numbers.
- Backgrounds print only with `print-color-adjust: exact` on `html`; every template sets it.
- A full-bleed page: `@page { margin: 0; }` and one element of exactly the page size, as in `notice.html`.
- Paths to images and fonts are relative to the HTML file. Remote resources and JavaScript load for up to `--wait` seconds (default 5) before printing.

## 8. What makes a document look generic

Avoid these; each one makes a document look machine-made:

- Everything centred, or every block in a box with a shadow.
- Gradients, several bright colours, or decorative icons without a meaning.
- Emoji as bullet points in professional documents.
- A cover page for a document of two or three pages.
- A heading above every paragraph, or bullet lists where sentences carry the argument.
- Bold scattered through sentences.
- Headings like "Executive Summary" or "Conclusion" that a short document does not need.
- Placeholder or filler text, or invented sample numbers.
- Mixed date formats, quotation marks or currencies.
- A heading alone at the bottom of a page, or a single line at the top of the next.

## 9. Country conventions: Germany

Letters (DIN 5008):
- The address field has a small zone at the top for the return address and notes such as "Einschreiben", then up to 6 address lines without empty lines; the town line comes last. For another country, add the country in capitals as the last line.
- The subject line has no word "Betreff" in front and no full stop; it is bold.
- Salutation: "Sehr geehrte Frau Muster," or "Sehr geehrte Damen und Herren,"; the text after it starts with a lower-case letter unless the word is a noun.
- Closing: "Mit freundlichen Grüßen" without a comma, then about three empty lines for the signature, then the name.
- Date in the information block: "2. Oktober 2026" or "02.10.2026".

Invoices (§ 14 UStG) contain:
- full name and address of the seller and the buyer;
- the seller's tax number (Steuernummer) or VAT ID (USt-IdNr.);
- invoice date and a unique, consecutive invoice number;
- quantity and kind of the goods or services, and the date or period of delivery;
- net amount per tax rate, the tax rate and the tax amount, and the total;
- for an exemption, the reason instead of the tax, for example "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet." for small businesses.

Invoices up to 250 € gross (Kleinbetragsrechnung) need only: seller's name and address, date, quantity and kind of the service, and the gross amount with the tax rate.

Between German businesses, e-invoices (XRechnung or ZUGFeRD) become mandatory for issuing: from 1 January 2027 for companies with more than 800,000 € turnover in the previous year, from 1 January 2028 for all others. Small businesses under § 19 UStG and invoices up to 250 € are exempt. A PDF made with this Skill is not an e-invoice. If the invoice goes to a German business after these dates, tell the user.
