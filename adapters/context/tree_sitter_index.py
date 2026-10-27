"""tree-sitter implementation of the `SymbolIndex` port ([C2]).

CPU-only and free of I/O: every method works on the source text it is given, so
tests run on fixtures without a network. Grammars are chosen by file extension;
an extension without a grammar answers `supports` False and the caller falls
back to a window. Parse errors overlapping the requested lines raise
`SymbolParseError` — again a fallback, never a failure (review-context spec).

Names and spans come from the syntax tree only; nothing here is logged.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os.path import splitext

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Tree

from domain.errors import SymbolParseError
from domain.ports import SymbolSpan


def default_languages() -> dict[str, Language]:
    """The first-tier languages of [C2]: Python, TypeScript/JavaScript, Go."""
    return {
        ".py": Language(tree_sitter_python.language()),
        ".go": Language(tree_sitter_go.language()),
        ".js": Language(tree_sitter_javascript.language()),
        ".jsx": Language(tree_sitter_javascript.language()),
        ".mjs": Language(tree_sitter_javascript.language()),
        ".cjs": Language(tree_sitter_javascript.language()),
        ".ts": Language(tree_sitter_typescript.language_typescript()),
        ".tsx": Language(tree_sitter_typescript.language_tsx()),
    }


def _extension(path: str) -> str:
    return splitext(path.rsplit("/", 1)[-1])[1].lower()


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class _RawSymbol:
    """One symbol with 0-based rows, before the 1-based conversion to SymbolSpan."""

    name: str
    kind: str
    start_row: int
    end_row: int
    signature_end_row: int

    @property
    def size(self) -> int:
        return self.end_row - self.start_row

    def to_span(self) -> SymbolSpan:
        return SymbolSpan(
            name=self.name,
            kind=self.kind,
            start_line=self.start_row + 1,
            end_line=self.end_row + 1,
            signature_end_line=self.signature_end_row + 1,
        )


def _extend_over_line_comments(source_lines: Sequence[str], start_row: int) -> int:
    """Go `//` doc comments directly above an item ([C2])."""
    row = start_row - 1
    while row >= 0 and source_lines[row].lstrip().startswith("//"):
        row -= 1
    return row + 1


def _extend_over_jsdoc(source_lines: Sequence[str], start_row: int) -> int:
    """A `/** ... */` block ending on the line directly above the symbol."""
    above = start_row - 1
    if above < 0 or not source_lines[above].rstrip().endswith("*/"):
        return start_row
    row = above
    while row >= 0:
        if source_lines[row].lstrip().startswith("/**"):
            return row
        row -= 1
    return start_row


def _header_end(body: Node | None, own_start_row: int, fallback_end: int) -> int:
    """The last row of a definition header: the row the body starts on, minus one.

    `own_start_row` is the declaration's own start, not the span extended over
    doc comments: the clamp only guards a body that starts on the header's row.
    """
    if body is None:
        return fallback_end
    return max(own_start_row, body.start_point.row - 1)


class TreeSitterSymbolIndex:
    """Implements `domain.ports.SymbolIndex`; safe to share across jobs."""

    def __init__(self, languages: Mapping[str, Language] | None = None) -> None:
        chosen = default_languages() if languages is None else dict(languages)
        self._parsers: dict[str, Parser] = {ext: Parser(lang) for ext, lang in chosen.items()}

    def supports(self, path: str) -> bool:
        return _extension(path) in self._parsers

    def enclosing(self, path: str, source: str, lines: Sequence[int]) -> tuple[SymbolSpan, ...]:
        rows = _anchor_rows(lines)
        tree = self._parse(path, source)
        _raise_on_errors_at(tree, rows)
        symbols = self._symbols(path, source, tree)
        chosen: dict[int, _RawSymbol] = {}
        for row in rows:
            innermost: _RawSymbol | None = None
            for symbol in symbols:
                contained = symbol.start_row <= row <= symbol.end_row
                if contained and (innermost is None or symbol.size < innermost.size):
                    innermost = symbol
            if innermost is not None:
                chosen[id(innermost)] = innermost
        ordered = sorted(chosen.values(), key=lambda s: (s.start_row, s.name))
        return tuple(symbol.to_span() for symbol in ordered)

    def called_from(self, path: str, source: str, lines: Sequence[int]) -> tuple[SymbolSpan, ...]:
        rows = _anchor_rows(lines)
        tree = self._parse(path, source)
        _raise_on_errors_at(tree, rows)
        symbols = self._symbols(path, source, tree)
        definitions: dict[str, _RawSymbol] = {}
        for symbol in symbols:
            definitions.setdefault(symbol.name, symbol)
        called: set[str] = set()
        stack: list[Node] = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in ("call", "call_expression") and node.start_point.row in rows:
                name = _callee_name(node)
                if name is not None and name in definitions:
                    called.add(name)
            stack.extend(node.named_children)
        ordered = sorted(called, key=lambda name: definitions[name].start_row)
        return tuple(definitions[name].to_span() for name in ordered)

    # --- plumbing -------------------------------------------------------------

    def _parse(self, path: str, source: str) -> Tree:
        parser = self._parsers.get(_extension(path))
        if parser is None:
            raise SymbolParseError(f"no grammar for {path!r}")
        return parser.parse(source.encode("utf-8", errors="replace"))

    def _symbols(self, path: str, source: str, tree: Tree) -> tuple[_RawSymbol, ...]:
        source_lines = source.split("\n")
        family = _extension(path)
        out: list[_RawSymbol] = []
        if family == ".py":
            _collect_python(tree.root_node, source_lines, in_class=False, out=out)
        elif family in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            _collect_ts(tree.root_node, source_lines, out=out)
        elif family == ".go":
            _collect_go(tree.root_node, source_lines, out=out)
        return tuple(out)


def _anchor_rows(lines: Sequence[int]) -> frozenset[int]:
    return frozenset(line - 1 for line in lines if line >= 1)


def _raise_on_errors_at(tree: Tree, rows: frozenset[int]) -> None:
    """[C2] fallback trigger: the tree has ERROR/missing nodes at the requested lines."""
    if not tree.root_node.has_error:
        return
    if not rows:
        return
    lowest, highest = min(rows), max(rows)
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        error_at_lines = node.is_error or node.is_missing
        if error_at_lines and node.start_point.row <= highest and node.end_point.row >= lowest:
            raise SymbolParseError("syntax errors at the requested lines")
        stack.extend(node.children)


def _callee_name(call_node: Node) -> str | None:
    if call_node.type == "call":  # python
        function = next((child for child in call_node.named_children if child.is_named), None)
        if function is None:
            return None
        if function.type == "identifier":
            return _text(function)
        if function.type == "attribute":
            attribute = function.child_by_field_name("attribute")
            return _text(attribute)
        return None
    function = call_node.child_by_field_name("function")  # TypeScript/JavaScript, Go
    if function is None:
        return None
    if function.type == "identifier":
        return _text(function)
    # obj.method(...) / pkg.Func(...): the same-file symbol is the last part.
    part = function.child_by_field_name("property") or function.child_by_field_name("field")
    return _text(part)


# --- Python -------------------------------------------------------------------


def _python_docstring_end(body: Node) -> int | None:
    """The end row of the body's first string statement, when it is a docstring."""
    first = next((child for child in body.named_children if child.is_named), None)
    if first is not None and first.type == "expression_statement":
        inner = next((child for child in first.named_children if child.is_named), None)
        if inner is not None and inner.type == "string":
            return first.end_point.row
    return None


def _collect_python(
    node: Node,
    source_lines: Sequence[str],
    *,
    in_class: bool,
    out: list[_RawSymbol],
    start_row: int | None = None,
) -> None:
    if node.type == "decorated_definition":
        # The span starts at the first decorator, not at the def.
        inner = node.named_children[-1] if node.named_children else None
        if inner is not None:
            _collect_python(
                inner, source_lines, in_class=in_class, out=out, start_row=node.start_point.row
            )
        return
    if node.type == "function_definition":
        name = _text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        if name is not None and body is not None:
            start = start_row if start_row is not None else node.start_point.row
            signature_end = _header_end(body, node.start_point.row, node.end_point.row)
            docstring_end = _python_docstring_end(body)
            if docstring_end is not None:
                signature_end = docstring_end
            out.append(
                _RawSymbol(
                    name=name,
                    kind="method" if in_class else "function",
                    start_row=start,
                    end_row=node.end_point.row,
                    signature_end_row=signature_end,
                )
            )
        for child in node.named_children:
            _collect_python(child, source_lines, in_class=in_class, out=out)
        return
    if node.type == "class_definition":
        name = _text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        if name is not None and body is not None:
            start = start_row if start_row is not None else node.start_point.row
            signature_end = _header_end(body, node.start_point.row, node.end_point.row)
            docstring_end = _python_docstring_end(body)
            if docstring_end is not None:
                signature_end = docstring_end
            out.append(_RawSymbol(name, "class", start, node.end_point.row, signature_end))
        for child in node.named_children:
            _collect_python(child, source_lines, in_class=True, out=out)
        return
    for child in node.named_children:
        _collect_python(child, source_lines, in_class=in_class, out=out)


# --- TypeScript / JavaScript ----------------------------------------------------


def _collect_ts(node: Node, source_lines: Sequence[str], out: list[_RawSymbol]) -> None:
    kind = node.type
    if kind in (
        "function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "enum_declaration",
    ):
        name = _text(node.child_by_field_name("name"))
        if name is not None:
            start = _extend_over_jsdoc(source_lines, node.start_point.row)
            body = node.child_by_field_name("body")
            out.append(
                _RawSymbol(
                    name=name,
                    kind="class"
                    if "class" in kind or kind.startswith(("interface", "enum"))
                    else "function",
                    start_row=start,
                    end_row=node.end_point.row,
                    signature_end_row=_header_end(body, node.start_point.row, node.end_point.row),
                )
            )
        for child in node.named_children:
            _collect_ts(child, source_lines, out)
        return
    if kind == "method_definition":
        name = _text(node.child_by_field_name("name"))
        if name is not None:
            start = _extend_over_jsdoc(source_lines, node.start_point.row)
            body = node.child_by_field_name("body")
            out.append(
                _RawSymbol(
                    name,
                    "method",
                    start,
                    node.end_point.row,
                    _header_end(body, node.start_point.row, node.end_point.row),
                )
            )
        return
    if kind == "method_signature":
        name = _text(node.child_by_field_name("name"))
        if name is not None:
            start = _extend_over_jsdoc(source_lines, node.start_point.row)
            out.append(_RawSymbol(name, "method", start, node.end_point.row, node.end_point.row))
        return
    if kind == "lexical_declaration":
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            value = declarator.child_by_field_name("value")
            if value is None or value.type not in ("arrow_function", "function_expression"):
                continue
            name = _text(declarator.child_by_field_name("name"))
            if name is None:
                continue
            start = _extend_over_jsdoc(source_lines, node.start_point.row)
            body = value.child_by_field_name("body")
            out.append(
                _RawSymbol(
                    name,
                    "function",
                    start,
                    node.end_point.row,
                    _header_end(body, node.start_point.row, node.end_point.row),
                )
            )
        return
    for child in node.named_children:
        _collect_ts(child, source_lines, out)


# --- Go --------------------------------------------------------------------------


def _go_name(node: Node) -> str | None:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named)
    # A method's name is a bare field_identifier child of method_declaration.
    for child in node.named_children:
        if child.type == "field_identifier":
            return _text(child)
    return None


def _collect_go(node: Node, source_lines: Sequence[str], out: list[_RawSymbol]) -> None:
    if node.type in ("function_declaration", "method_declaration"):
        name = _go_name(node)
        body = node.child_by_field_name("body")
        if name is not None and body is not None:
            start = _extend_over_line_comments(source_lines, node.start_point.row)
            out.append(
                _RawSymbol(
                    name=name,
                    kind="method" if node.type == "method_declaration" else "function",
                    start_row=start,
                    end_row=node.end_point.row,
                    signature_end_row=_header_end(body, node.start_point.row, node.end_point.row),
                )
            )
        return
    if node.type == "type_declaration":
        for spec in node.named_children:
            if spec.type != "type_spec":
                continue
            inner = spec.child_by_field_name("type")
            if inner is None or inner.type not in ("struct_type", "interface_type"):
                continue
            name = _text(spec.child_by_field_name("name"))
            if name is None:
                continue
            start = _extend_over_line_comments(source_lines, node.start_point.row)
            out.append(_RawSymbol(name, "class", start, spec.end_point.row, spec.end_point.row))
        return
    for child in node.named_children:
        _collect_go(child, source_lines, out)
