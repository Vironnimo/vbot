"""bash command lines lowered through tree-sitter-bash into a ``Script``.

tree-sitter is imported on first use, so importing the command analysis costs
nothing until a restricted Session runs a bash command. A tree with an error
or a missing node is not analyzable.
"""

from __future__ import annotations

import functools
import itertools
import re
from typing import TYPE_CHECKING

from core.permissions.commands._syntax import Part, Pipeline, RawCommand, Redirect, Script, Word

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

_GLOB = re.compile(r"[*?\[]")
# Words that assign the variables they name (``read X``, ``declare -a Y``).
_ASSIGNING_BUILTINS = frozenset({"read", "mapfile", "readarray", "getopts", "unset", "printf"})
_FILE_REDIRECTS = {
    ">": "write",
    ">>": "write",
    ">|": "write",
    "&>": "write",
    "&>>": "write",
    "<": "read",
    "<>": "write",
}


@functools.cache
def _parser() -> Parser:
    import tree_sitter_bash
    from tree_sitter import Language, Parser

    return Parser(Language(tree_sitter_bash.language()))


def parse_bash(text: str) -> Script:
    """Lower *text*; ``Script.error`` is set when tree-sitter finds a syntax error."""
    source = text.encode("utf-8")
    tree = _parser().parse(source)
    if tree.root_node.has_error:
        return Script(error="bash could not parse the command line")
    lowering = _Lowering(source)
    lowering.statements(tree.root_node, ())
    return lowering.script


class _Lowering:
    def __init__(self, source: bytes) -> None:
        self.source = source
        self.script = Script()
        self._scopes = itertools.count(1)
        self._pipelines = itertools.count(1)

    def text(self, node: Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def new_scope(self, scope: tuple[int, ...]) -> tuple[int, ...]:
        return (*scope, next(self._scopes))

    # Statements -------------------------------------------------------------

    def statements(self, node: Node, scope: tuple[int, ...]) -> None:
        children = node.children
        for index, child in enumerate(children):
            if not child.is_named:
                continue
            following = children[index + 1] if index + 1 < len(children) else None
            background = following is not None and following.type == "&"
            self.statement(child, self.new_scope(scope) if background else scope)

    def statement(
        self, node: Node, scope: tuple[int, ...], pipeline: Pipeline | None = None
    ) -> None:
        kind = node.type
        if kind == "command":
            self.command(node, scope, pipeline, [])
        elif kind == "redirected_statement":
            self.redirected(node, scope, pipeline)
        elif kind == "pipeline":
            self.pipeline(node, scope)
        elif kind == "subshell":
            self.statements(node, self.new_scope(scope))
        elif kind == "variable_assignment":
            self.assignment(node, scope)
        elif kind == "declaration_command":
            self.declaration(node, scope)
        elif kind == "unset_command":
            for child in node.named_children:
                self.script.assigned.add(self.text(child))
        elif kind in ("for_statement", "select_statement"):
            variable = node.child_by_field_name("variable")
            if variable is not None:
                self.script.assigned.add(self.text(variable))
            self.nested(node, scope)
        elif kind == "function_definition":
            body = node.child_by_field_name("body")
            if body is not None:
                self.statement(body, scope)
        elif kind == "comment":
            return
        else:
            self.nested(node, scope)

    def nested(self, node: Node, scope: tuple[int, ...]) -> None:
        """Statements and expansions anywhere inside a compound statement or expression."""
        for child in node.named_children:
            if child.type in ("command_substitution", "process_substitution"):
                self.statements(child, self.new_scope(scope))
            elif child.type in (
                "word",
                "string",
                "raw_string",
                "concatenation",
                "simple_expansion",
                "expansion",
            ):
                self.word(child, scope)
            else:
                self.statement(child, scope)

    def pipeline(self, node: Node, scope: tuple[int, ...]) -> None:
        elements = [child for child in node.named_children if child.type != "comment"]
        if len(elements) < 2:
            for element in elements:
                self.statement(element, scope)
            return
        identity = next(self._pipelines)
        for index, element in enumerate(elements):
            # Every element of a pipeline runs in a subshell of its own.
            self.statement(element, self.new_scope(scope), Pipeline(identity, index))

    def redirected(self, node: Node, scope: tuple[int, ...], pipeline: Pipeline | None) -> None:
        body = node.child_by_field_name("body")
        redirects: list[Redirect] = []
        for child in node.children_by_field_name("redirect"):
            self.redirect(child, scope, redirects)
        if body is not None and body.type == "command":
            self.command(body, scope, pipeline, redirects)
            return
        if body is not None:
            self.statement(body, scope, pipeline)
        if redirects:
            self.script.commands.append(RawCommand([], self.text(node), redirects, pipeline, scope))

    def redirect(self, node: Node, scope: tuple[int, ...], redirects: list[Redirect]) -> None:
        if node.type == "file_redirect":
            operator = next((child.type for child in node.children if not child.is_named), "")
            destination = node.child_by_field_name("destination")
            if destination is None:
                return
            kind = _FILE_REDIRECTS.get(operator)
            if operator in (">&", "<&", ">&-", "<&-") and destination.type in ("number", "word"):
                # Duplicating a descriptor (2>&1) opens no file; ">&file" does.
                if (
                    destination.type == "number"
                    or self.text(destination).isdigit()
                    or self.text(destination) == "-"
                ):
                    return
                kind = "write" if operator.startswith(">") else "read"
            if kind is not None:
                redirects.append(
                    Redirect("read" if kind == "read" else "write", self.word(destination, scope))
                )
        elif node.type == "heredoc_redirect":
            for child in node.named_children:
                if child.type in ("file_redirect", "heredoc_redirect", "herestring_redirect"):
                    self.redirect(child, scope, redirects)
                elif child.type == "heredoc_body":
                    self.expansions(child, scope)
        elif node.type == "herestring_redirect":
            self.expansions(node, scope)

    def expansions(self, node: Node, scope: tuple[int, ...]) -> None:
        for child in node.named_children:
            if child.type in ("command_substitution", "process_substitution"):
                self.statements(child, self.new_scope(scope))
            else:
                self.expansions(child, scope)

    def assignment(self, node: Node, scope: tuple[int, ...]) -> None:
        name = node.child_by_field_name("name")
        if name is not None:
            self.script.assigned.add(self.text(name).split("[", 1)[0])
        value = node.child_by_field_name("value")
        if value is not None:
            self.word(value, scope)

    def declaration(self, node: Node, scope: tuple[int, ...]) -> None:
        for child in node.named_children:
            if child.type == "variable_assignment":
                self.assignment(child, scope)
            elif (
                child.type == "variable_name"
                or child.type == "word"
                and not self.text(child).startswith(("-", "+"))
            ):
                self.script.assigned.add(self.text(child))
            else:
                self.word(child, scope)

    def command(
        self,
        node: Node,
        scope: tuple[int, ...],
        pipeline: Pipeline | None,
        redirects: list[Redirect],
    ) -> None:
        words: list[Word] = []
        redirects = list(redirects)
        for index, child in enumerate(node.children):
            field = node.field_name_for_child(index)
            if child.type == "variable_assignment":
                # A prefix assignment (X=1 cmd) changes the command's environment.
                self.assignment(child, scope)
            elif child.type in ("file_redirect", "heredoc_redirect", "herestring_redirect"):
                self.redirect(child, scope, redirects)
            elif field == "name":
                words.append(
                    self.word(child.named_children[0] if child.named_children else child, scope)
                )
            elif field == "argument" or (child.is_named and words):
                words.append(self.word(child, scope))
        if not words:
            if redirects:
                self.script.commands.append(
                    RawCommand([], self.text(node), redirects, pipeline, scope)
                )
            return
        name = (
            words[0].parts[0].value
            if len(words[0].parts) == 1 and words[0].parts[0].kind == "text"
            else ""
        )
        if name in _ASSIGNING_BUILTINS:
            for word in words[1:]:
                if (
                    len(word.parts) == 1
                    and word.parts[0].kind == "text"
                    and not word.source.startswith("-")
                ):
                    self.script.assigned.add(word.parts[0].value)
        self.script.commands.append(RawCommand(words, self.text(node), redirects, pipeline, scope))

    # Words ------------------------------------------------------------------

    def word(self, node: Node, scope: tuple[int, ...]) -> Word:
        parts: list[Part] = []
        glob = self.parts(node, scope, parts, quoted=False)
        first = (
            node.named_children[0] if node.type == "concatenation" and node.named_children else node
        )
        tilde = first.type == "word" and self.text(first).startswith("~")
        return Word(tuple(_merge(parts)), self.text(node), glob=glob, tilde=tilde)

    def parts(self, node: Node, scope: tuple[int, ...], parts: list[Part], *, quoted: bool) -> bool:
        """Append *node*'s parts; return whether an unquoted wildcard may expand it."""
        kind = node.type
        text = self.text(node)
        if kind in ("word", "number") and not quoted:
            parts.append(Part("text", _unescape(text)))
            return bool(_GLOB.search(text)) or ("{" in text and ("," in text or ".." in text))
        if kind == "raw_string":
            parts.append(Part("text", text[1:-1]))
            return False
        if kind in ("string", "translated_string"):
            for child in node.named_children:
                self.parts(child, scope, parts, quoted=True)
            return False
        if kind == "string_content":
            parts.append(Part("text", _unescape_quoted(text)))
            return False
        if kind == "concatenation":
            glob = False
            for child in node.named_children:
                glob = self.parts(child, scope, parts, quoted=quoted) or glob
            # "{a,b}" is lowered as separate words around the braces.
            return glob or (not quoted and "{" in text and "," in text)
        if kind == "simple_expansion":
            name = node.named_children[0] if node.named_children else None
            if name is not None and name.type == "variable_name":
                parts.append(Part("variable", self.text(name)))
            else:
                parts.append(Part("dynamic", text))
            return not quoted
        if kind == "expansion":
            named = node.named_children
            if len(named) == 1 and named[0].type == "variable_name" and len(node.children) == 3:
                parts.append(Part("variable", self.text(named[0])))
            else:
                self.expansions(node, scope)
                parts.append(Part("dynamic", text))
            return not quoted
        if kind in ("command_substitution", "process_substitution"):
            self.statements(node, self.new_scope(scope))
            parts.append(Part("dynamic", text))
            return not quoted
        self.expansions(node, scope)
        parts.append(Part("dynamic", text))
        return not quoted and kind != "ansi_c_string"


def _merge(parts: list[Part]) -> list[Part]:
    merged: list[Part] = []
    for part in parts:
        if part.kind == "text" and merged and merged[-1].kind == "text":
            merged[-1] = Part("text", merged[-1].value + part.value)
        elif part.kind != "text" or part.value:
            merged.append(part)
    return merged


def _unescape(text: str) -> str:
    """An unquoted word as bash passes it: a backslash keeps the next character."""
    return re.sub(r"\\(.)", r"\1", text.replace("\\\n", ""), flags=re.DOTALL)


def _unescape_quoted(text: str) -> str:
    """Text inside double quotes: a backslash escapes only $ ` " \\ and a newline."""
    return re.sub(
        r'\\([$`"\\\n])', lambda match: "" if match.group(1) == "\n" else match.group(1), text
    )
