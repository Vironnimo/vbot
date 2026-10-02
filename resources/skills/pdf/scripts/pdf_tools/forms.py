"""List and fill the interactive form fields (AcroForm) of a PDF."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from .common import (
    AtomicOutput,
    CommandError,
    describe_pages,
    existing_pdf,
    human_size,
    open_reader,
    output_path,
    require_modules,
    tool_command,
)

READ_ONLY = 1
REQUIRED = 2
MULTILINE = 1 << 12
RADIO = 1 << 15
PUSH_BUTTON = 1 << 16
COMBO = 1 << 17
EDITABLE = 1 << 18
TRUE_WORDS = {"true", "yes", "on", "1", "x", "checked", "ja", "oui", "si", "sí"}
FALSE_WORDS = {"false", "no", "off", "0", "", "unchecked", "nein", "non"}


class Field:
    def __init__(self, name: str, data: Any) -> None:
        self.name = name
        self.data = data
        flags = int(data.get("/Ff", 0) or 0)
        self.flags = flags
        kind = data.get("/FT")
        if kind == "/Btn":
            self.kind = (
                "push button" if flags & PUSH_BUTTON else ("radio" if flags & RADIO else "checkbox")
            )
        elif kind == "/Ch":
            self.kind = "dropdown" if flags & COMBO else "list"
        elif kind == "/Sig":
            self.kind = "signature"
        else:
            self.kind = "text"
        self.label = str(data.get("/TU", "") or "")
        self.pages: list[int] = []
        self.max_length = int(data.get("/MaxLen", 0) or 0)
        self.states: list[str] = []
        self.options: list[str] = []
        for option in data.get("/Opt", []) or []:
            option = option.get_object() if hasattr(option, "get_object") else option
            if isinstance(option, list) and option:
                self.options.append(str(option[0]))
            else:
                self.options.append(str(option))
        for state in data.get("/_States_", []) or []:
            if str(state) != "/Off":
                self.states.append(str(state).lstrip("/"))

    @property
    def fillable(self) -> bool:
        return self.kind not in ("push button", "signature") and not self.flags & READ_ONLY

    @property
    def value(self) -> Any:
        value = self.data.get("/V")
        if value is None:
            return False if self.kind == "checkbox" else ""
        if self.kind == "checkbox":
            return str(value) not in ("/Off", "")
        if self.kind == "radio":
            return "" if str(value) == "/Off" else str(value).lstrip("/")
        if isinstance(value, list):
            return [str(item) for item in value]
        return str(value)

    def describe(self) -> str:
        parts = [f'"{self.name}" {self.kind}']
        if self.pages:
            parts.append(f"page {describe_pages(sorted(set(self.pages)))}")
        line = ", ".join(parts) + f": {json.dumps(self.value, ensure_ascii=False)}"
        details = []
        if self.label:
            details.append(f"label: {self.label}")
        if self.kind == "checkbox" and self.states:
            details.append(f"checked value: {self.states[0]}")
        if self.kind == "radio" and self.states:
            details.append("options: " + ", ".join(self.states))
        if self.kind in ("dropdown", "list") and self.options:
            shown = ", ".join(self.options[:15])
            more = f" (+{len(self.options) - 15} more)" if len(self.options) > 15 else ""
            details.append(f"options: {shown}{more}")
        if self.flags & READ_ONLY:
            details.append("read-only")
        if self.flags & REQUIRED:
            details.append("required")
        if self.kind == "text" and self.max_length:
            details.append(f"at most {self.max_length} characters")
        if self.kind == "text" and self.flags & MULTILINE:
            details.append("multi-line")
        return line + (f" ({'; '.join(details)})" if details else "")


def _fields(reader: Any) -> dict[str, Field]:
    raw = reader.get_fields() or {}
    fields = {name: Field(name, data) for name, data in raw.items() if data.get("/FT")}
    for index, page in enumerate(reader.pages):
        for annotation in page.get("/Annots", []) or []:
            try:
                widget = annotation.get_object()
            except Exception:
                continue
            if widget.get("/Subtype") != "/Widget":
                continue
            name = _qualified_name(widget)
            if name in fields:
                field = fields[name]
                field.pages.append(index)
                if not field.max_length:
                    field.max_length = _inherited_int(widget, "/MaxLen")
    return fields


def _inherited_int(widget: Any, key: str) -> int:
    node = widget
    for _ in range(32):
        if node is None:
            return 0
        if key in node:
            try:
                return int(node[key])
            except (TypeError, ValueError):
                return 0
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return 0


def _qualified_name(widget: Any) -> str:
    parts: list[str] = []
    node = widget
    for _ in range(32):
        if node is None:
            break
        if "/T" in node:
            parts.append(str(node["/T"]))
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return ".".join(reversed(parts))


def list_fields(value: str, template: str | None, password: str | None) -> list[str]:
    require_modules("pypdf")
    source = existing_pdf(value)
    reader = open_reader(source, password)
    fields = _fields(reader)
    acroform = reader.trailer["/Root"].get("/AcroForm")
    xfa = bool(acroform and "/XFA" in acroform.get_object())
    if not fields:
        if xfa:
            raise CommandError(
                f"{source.name} is a pure XFA form; its fields cannot be filled with these tools, "
                "and most viewers other than Adobe Acrobat Reader show no content for it. Tell "
                "the user to fill it in Adobe Acrobat Reader."
            )
        return [
            f"{source.name} has no fillable fields. Fill it by placing the text on an overlay "
            "page, as described in the Skill's references/flat-forms.md."
        ]
    fillable = [field for field in fields.values() if field.fillable]
    pages = sorted({page for field in fields.values() for page in field.pages})
    where = f"page{'s' if len(pages) != 1 else ''} {describe_pages(pages)}"
    lines = [f"{source.name}: {len(fillable)} fillable fields of {len(fields)} on {where}."]
    if xfa:
        lines.append(
            "The form also has an XFA layer; fill removes it so viewers show the filled fields."
        )
    lines.extend(f"- {field.describe()}" for field in fields.values())
    if template:
        target = Path(template).expanduser().resolve()
        if target.suffix.lower() != ".json":
            raise CommandError("--template must name a .json file.")
        values = {field.name: field.value for field in fillable}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines.append(
            f"Wrote the fillable fields with their current values to {target}. Edit the values, "
            "then run "
            f"`{tool_command(f'fill {source.as_posix()} {target.as_posix()} <filled.pdf>')}`."
        )
    return lines


def fill(
    value: str,
    values_value: str,
    target_value: str,
    flatten: bool,
    break_signatures: bool,
    password: str | None,
) -> list[str]:
    require_modules("pypdf")
    from pypdf import PdfWriter
    from pypdf.generic import NameObject

    source = existing_pdf(value)
    target = output_path(target_value, inputs=[source])
    values = _load_values(values_value)
    reader = open_reader(source, password)
    fields = _fields(reader)
    if not fields:
        raise CommandError(f"{source.name} has no fillable fields; run `fields` for details.")
    signed = [
        name for name, field in fields.items() if field.kind == "signature" and field.data.get("/V")
    ]
    if signed and not break_signatures:
        raise CommandError(
            f"{source.name} is digitally signed ({', '.join(signed)}); changing it invalidates the "
            "signature. Only if the user accepts that, repeat with --break-signatures."
        )
    prepared = _prepare(values, fields, source)
    writer = PdfWriter(clone_from=reader)
    acroform = writer._root_object.get("/AcroForm")
    notes: list[str] = []
    if acroform is not None and "/XFA" in acroform.get_object():
        del acroform.get_object()[NameObject("/XFA")]
        notes.append("Removed the XFA layer so viewers show the filled fields.")
    writer.set_need_appearances_writer(True)
    writer.update_page_form_field_values(None, prepared, auto_regenerate=False)
    with AtomicOutput(target) as output:
        with open(output.path, "wb") as handle:
            writer.write(handle)
        if flatten:
            _flatten(output.path)
    lines = [
        f"Filled {len(prepared)} fields -> {target.name}"
        + (" (flattened: values are now fixed page content)" if flatten else "")
        + f", {human_size(target.stat().st_size)}.",
        f"File: {target}",
        *notes,
    ]
    if not flatten:
        mismatches = _verify(target, prepared, fields)
        if mismatches:
            lines.append("Problems:")
            lines.extend(f"- {problem}" for problem in mismatches)
        else:
            lines.append("Every value reads back from the saved file as intended.")
    empty_required = [
        field.name
        for field in fields.values()
        if field.flags & REQUIRED
        and field.fillable
        and field.name not in values
        and not field.value
    ]
    if empty_required:
        lines.append("Required fields still empty: " + ", ".join(empty_required))
    lines.append(
        "Next: render the filled pages and check that every value is visible and fits its box."
    )
    return lines


def _flatten(path: Path) -> None:
    """Draw every field's appearance into the page content and drop the form (PDFium)."""
    require_modules("pypdfium2")
    import pypdfium2 as pdfium  # type: ignore[import-untyped]
    import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]

    flattened = path.with_name(path.name + ".flat")
    document = pdfium.PdfDocument(str(path))
    try:
        document.init_forms()
        for index in range(len(document)):
            page = document[index]
            try:
                if pdfium_raw.FPDFPage_Flatten(page.raw, pdfium_raw.FLAT_PRINT) == 0:
                    raise CommandError(f"Flattening page {index + 1} failed.")
            finally:
                page.close()
        document.save(str(flattened))
    finally:
        document.close()
    flattened.replace(path)


def _load_values(value: str) -> dict[str, Any]:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise CommandError(f"Values file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise CommandError(f"The values file is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise CommandError('The values file must be a JSON object: {"Field name": "value", ...}.')
    return data


def _prepare(values: dict[str, Any], fields: dict[str, Field], source: Path) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    errors: list[str] = []
    for name, raw in values.items():
        field = fields.get(name)
        if field is None:
            close = difflib.get_close_matches(name, list(fields), n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            errors.append(f'No field "{name}".{hint}')
            continue
        if not field.fillable:
            errors.append(
                f'"{name}" is a {field.kind}'
                f"{' (read-only)' if field.flags & READ_ONLY else ''} and cannot be filled."
            )
            continue
        converted, error = _convert(field, raw)
        if error:
            errors.append(error)
        else:
            prepared[name] = converted
    if errors:
        raise CommandError(
            "Nothing was written. Fix the values file:\n"
            + "\n".join(f"- {error}" for error in errors)
            + f"\nList the fields with `{tool_command(f'fields {source.as_posix()}')}`."
        )
    return prepared


def _convert(field: Field, raw: Any) -> tuple[Any, str | None]:
    if field.kind == "checkbox":
        on_state = field.states[0] if field.states else "Yes"
        text = (
            str(raw).strip().lower() if not isinstance(raw, bool) else ("true" if raw else "false")
        )
        if text in TRUE_WORDS or text == on_state.lower():
            return f"/{on_state}", None
        if text in FALSE_WORDS:
            return "/Off", None
        return None, f'"{field.name}" is a checkbox: use true or false, not {json.dumps(raw)}.'
    if field.kind == "radio":
        text = str(raw).lstrip("/")
        if text == "" or text.lower() == "off":
            return "/Off", None
        match = [state for state in field.states if state.lower() == text.lower()]
        if not match:
            return (
                None,
                f'"{field.name}" accepts one of: {", ".join(field.states)}; not {json.dumps(raw)}.',
            )
        return f"/{match[0]}", None
    if field.kind in ("dropdown", "list"):
        text = str(raw)
        if field.options and text not in field.options and not field.flags & EDITABLE:
            match = [option for option in field.options if option.lower() == text.lower()]
            if not match:
                close = difflib.get_close_matches(text, field.options, n=3, cutoff=0.4)
                return None, (
                    f'"{field.name}" accepts only its options; '
                    f"{json.dumps(raw)} is not one of them."
                    + (f" Closest: {', '.join(close)}." if close else "")
                )
            text = match[0]
        return text, None
    if isinstance(raw, bool):
        raw = "X" if raw else ""
    text = "" if raw is None else str(raw)
    if field.max_length and len(text) > field.max_length:
        return (
            None,
            f'"{field.name}" holds at most {field.max_length} characters; '
            f"the value has {len(text)}.",
        )
    return text, None


def _verify(target: Path, prepared: dict[str, Any], fields: dict[str, Field]) -> list[str]:
    reader = open_reader(target)
    written = _fields(reader)
    problems = []
    for name, expected in prepared.items():
        before, after = fields[name], written.get(name)
        if after is None:
            problems.append(f'"{name}" is missing in the saved file.')
            continue
        actual = after.data.get("/V")
        if before.kind in ("checkbox", "radio"):
            same = str(actual or "/Off") == str(expected)
        else:
            same = str(actual if actual is not None else "") == str(expected)
        if not same:
            problems.append(
                f'"{name}" reads back as {json.dumps(str(actual))}, '
                f"expected {json.dumps(str(expected))}."
            )
    return problems
