from types import SimpleNamespace
import re

from linux_plus_plus.tools.lexer import LinuxppRegexLexer


def test_lexer_simple_tokens():
    # simple mapping: numbers and operators
    mapping = {
        re.compile(r'^\d+(?:\.\d+)?'): 'NUM',
        re.compile(r'^[+\-*/()]'): 'OP',
        re.compile(r'^\s+'): '',
    }

    lexer = LinuxppRegexLexer(mapping)
    doc = SimpleNamespace(text="12 + 24")
    lex_fn = lexer.lex_document(doc)
    tokens = lex_fn(0)

    # Expect tokens: number, whitespace skipped (no style), operator, whitespace, number
    # Unmatched whitespace yields '' style entries; implementation may emit single-char tokens
    assert any(t[1].strip() == '12' for t in tokens)
    assert any(t[1].strip() == '+' for t in tokens)
    assert any(t[1].strip() == '24' for t in tokens)
