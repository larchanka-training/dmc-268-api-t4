"""TreeSitterSymbolIndex against inline fixtures, one block per language ([C2]).

CPU-only: no network, no I/O — the index parses the strings it is given.
"""

import pytest

from adapters.context.tree_sitter_index import TreeSitterSymbolIndex
from domain.errors import SymbolParseError

PY_SOURCE = "\n".join(
    [
        "import os",  # 1
        "",  # 2
        "",  # 3
        "@app.route('/x')",  # 4  <- span starts at the decorator
        "class Handler:",  # 5
        "    '''Handle things.'''",  # 6
        "",  # 7
        "    def run(self, value):",  # 8
        "        '''Run it.'''",  # 9  <- inside the signature
        "        return helper(value) + os.getpid()",  # 10
        "",  # 11
        "",  # 12
        "def helper(x):",  # 13
        "    '''Help.'''",  # 14
        "    return x + 1",  # 15
    ]
)

TS_SOURCE = "\n".join(
    [
        "/** Runs the thing. */",  # 1  <- jsdoc extends the span upward
        "export const run = (x: number) => helper(x);",  # 2
        "",  # 3
        "function helper(x: number) {",  # 4
        "  return x + 1;",  # 5
        "}",  # 6
        "",  # 7
        "class Svc {",  # 8
        "  constructor(private a: number) {}",  # 9
        "  get value(): number {",  # 10
        "    return this.a;",  # 11
        "  }",  # 12
        "}",  # 13
        "",  # 14
        "interface Repo {",  # 15
        "  find(id: string): Svc;",  # 16
        "}",  # 17
    ]
)

GO_SOURCE = "\n".join(
    [
        "package main",  # 1
        "",  # 2
        "// Add sums things up.",  # 3  <- doc comment extends the span
        "func Add(x int) int {",  # 4
        "\treturn x + 1",  # 5
        "}",  # 6
        "",  # 7
        "func (s *Svc) Run() int {",  # 8
        "\treturn s.other()",  # 9
        "}",  # 10
        "",  # 11
        "type Svc struct {",  # 12
        "\tName string",  # 13
        "}",  # 14
    ]
)


@pytest.fixture(scope="module")
def index() -> TreeSitterSymbolIndex:
    return TreeSitterSymbolIndex()


# --- Python -------------------------------------------------------------------


def test_python_enclosing_is_the_innermost_method(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.py", PY_SOURCE, [10])
    assert (span.name, span.kind) == ("run", "method")
    assert (span.start_line, span.end_line) == (8, 10)
    # The signature covers the header and the docstring.
    assert span.signature_end_line == 9


def test_python_decorated_class_starts_at_the_decorator(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.py", PY_SOURCE, [6])
    assert (span.name, span.kind) == ("Handler", "class")
    assert span.start_line == 4  # the decorator row, not the `class` row


def test_python_enclosing_picks_the_innermost_not_the_class(index: TreeSitterSymbolIndex) -> None:
    symbols = index.enclosing("app/x.py", PY_SOURCE, [6])
    assert [symbol.name for symbol in symbols] == ["Handler"]
    assert symbols[0].kind == "class"
    assert symbols[0].signature_end_line == 6  # the class docstring


def test_python_called_from_finds_the_same_file_function(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.called_from("app/x.py", PY_SOURCE, [10])
    assert (span.name, span.kind) == ("helper", "function")
    assert span.start_line == 13
    assert span.signature_end_line == 14  # def + docstring, body not included
    assert span.end_line == 15


def test_python_attribute_call_resolves_the_method_name(index: TreeSitterSymbolIndex) -> None:
    source = "class A:\n    def m(self):\n        return 1\n\n\ndef caller():\n    return A().m()\n"
    spans = index.called_from("app/y.py", source, [7])
    # The class constructor and the method are both same-file symbols on the line.
    assert {span.name for span in spans} == {"A", "m"}


def test_python_parse_error_at_the_line_raises(index: TreeSitterSymbolIndex) -> None:
    broken = "def ok():\n    return 1\n\ndef broken(:\n    pass\n"
    with pytest.raises(SymbolParseError):
        index.enclosing("app/z.py", broken, [4])


# --- TypeScript / JavaScript ----------------------------------------------------


def test_ts_arrow_assignment_is_a_function_with_jsdoc(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.ts", TS_SOURCE, [2])
    assert (span.name, span.kind) == ("run", "function")
    assert span.start_line == 1  # the jsdoc line
    assert span.end_line == 2


def test_ts_class_and_methods(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.ts", TS_SOURCE, [11])
    assert (span.name, span.kind) == ("value", "method")
    assert span.start_line == 10
    assert span.signature_end_line == 10  # header row, body starts after


def test_ts_interface_counts_as_class(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.ts", TS_SOURCE, [16])
    assert (span.name, span.kind) == ("find", "method")


def test_ts_member_call_resolves_the_property(index: TreeSitterSymbolIndex) -> None:
    source = (
        "class Svc {\n  other(): number {\n    return 1\n  }\n}"
        "\nconst s = new Svc()\nconst n = s.other()\n"
    )
    (span,) = index.called_from("app/w.ts", source, [7])
    assert span.name == "other"
    assert span.kind == "method"


def test_js_function_and_call(index: TreeSitterSymbolIndex) -> None:
    source = "function helper(x) {\n  return x\n}\nconst out = helper(1)\n"
    # Module level: the call line sits in no symbol, so nothing encloses it.
    assert index.enclosing("app/m.js", source, [4]) == ()
    (called,) = index.called_from("app/m.js", source, [4])
    assert called.name == "helper"
    assert called.signature_end_line == 1  # header only, body not in tier 2


# --- Go --------------------------------------------------------------------------


def test_go_function_with_doc_comment(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.go", GO_SOURCE, [5])
    assert (span.name, span.kind) == ("Add", "function")
    assert span.start_line == 3  # the doc comment
    assert span.end_line == 6
    assert span.signature_end_line == 4


def test_go_method_and_selector_call(index: TreeSitterSymbolIndex) -> None:
    source = (
        "package main\n"
        "\n"
        "type Svc struct{}\n"
        "\n"
        "func (s *Svc) Other() int {\n"
        "\treturn 1\n"
        "}\n"
        "\n"
        "func Run(s *Svc) int {\n"
        "\treturn s.Other()\n"
        "}\n"
    )
    (span,) = index.enclosing("app/y.go", source, [6])
    assert (span.name, span.kind) == ("Other", "method")
    (called,) = index.called_from("app/y.go", source, [10])
    assert called.name == "Other"
    assert called.kind == "method"


def test_go_struct_counts_as_class(index: TreeSitterSymbolIndex) -> None:
    (span,) = index.enclosing("app/x.go", GO_SOURCE, [13])
    assert (span.name, span.kind) == ("Svc", "class")
    assert (span.start_line, span.end_line) == (12, 14)


# --- dispatch ----------------------------------------------------------------------


def test_unsupported_extension_is_not_supported(index: TreeSitterSymbolIndex) -> None:
    assert index.supports("app/x.rb") is False
    assert index.supports("app/x.py") is True
    assert index.supports("app/x.tsx") is True
    assert index.supports("README") is False
