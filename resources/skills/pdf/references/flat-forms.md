# Filling a form without fillable fields

How to fill a PDF form whose blanks are only printed lines and boxes, such as a scanned form or a form exported without fields. You write the answers on a transparent overlay page and put it on top of the form; the form itself stays unchanged.

`<skill-dir>` below is the directory in the commands of the Skill's instructions; on Linux and macOS, use `python3`.

1. Run `inspect` on the form. Done when you know the page count and the page size of each page that needs answers.
2. Render those pages with a millimetre grid:
   ```
   python <skill-dir>/scripts/pdf.py render <form.pdf> pdf-work/<form-name>/grid --grid --pages <pages>
   ```
   Read each page image. For every answer, note from the grid labels, in millimetres from the top-left corner: for text on a line, where the text starts and the height of the line; for a box, its top-left corner and width; for a checkbox, its centre. Done when you have a position for every answer.
3. Copy `<skill-dir>/assets/form-overlay.html` to `pdf-work/<form-name>/overlay.html`. Set `@page size` to the form's page size. Write one `.page` block per page of the form, in the same order, with one entry per answer at the noted position; a page without answers stays an empty `.page` block. Delete the sample entries and replace `{{de}}` with the form's language. Done when the file contains no `{{`.
4. Create the overlay and put it on the form:
   ```
   python <skill-dir>/scripts/pdf.py create pdf-work/<form-name>/overlay.html pdf-work/<form-name>/overlay.pdf
   python <skill-dir>/scripts/pdf.py stamp <form.pdf> <filled.pdf> --overlay pdf-work/<form-name>/overlay.pdf
   ```
   Done when `stamp` reports the filled file.
5. Render the filled pages without the grid and read them. Check that every answer stands on its line or inside its box, does not touch printed text, and fits the space. To check a small answer or a checkbox up close, render only its part of the page with `--area <left>,<top>,<width>,<height> --grid` (millimetres from the top-left corner). If an answer is off, change its position in `overlay.html` by the difference you see on the grid, and repeat steps 4 and 5. Done when every answer sits in place.

- For a long answer in a short space, use the class `small` on the entry (9 pt) before you shorten the text; never let text run over the printed form.
- Write dates, numbers and names exactly as the user gave them, in the format the form asks for.
- Leave the signature line empty unless the user gave a signature image; then place it as `<img>` with the class `in-box` and a width in millimetres.
