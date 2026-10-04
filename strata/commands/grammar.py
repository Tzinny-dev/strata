"""Grammar command: grammar"""

import sys
from typing import Any


def cmd_grammar(args: Any) -> int:
    """`strata grammar`: emit the Strata grammar as llama.cpp GBNF for constrained decoding.

    The grammar is grammar-as-code (strata/grammar.py) kept in lockstep with the
    lexer by tests/test_grammar.py; this command validates it fail-loud (exit 2)
    and then prints the GBNF text ready to feed a constrained sampler. --doc
    annotates every rule with its spec sentence for human supervision. --check
    runs the independent consumer engine (strata/gbnf.py) over a program,
    proving the emitted grammar covers it (exit 0) or not (exit 2).
    """
    from .. import grammar
    problems = grammar.validate()
    if problems:
        for pr in problems:
            print(f"error: grammar inconsistency: {pr}", file=sys.stderr)
        return 2
    if args.check:
        from .. import gbnf, lexer
        g = gbnf.GbnfGrammar.from_text(grammar.emit_gbnf())
        from pathlib import Path
        toks = lexer.Lexer(Path(args.check).read_text()).tokenize()
        if gbnf.accepts_program(toks, g):
            print(f"{args.check}: accepted by the GBNF grammar (independent consumer)")
            return 0
        print(f"{args.check}: REJECTED by the GBNF grammar", file=sys.stderr)
        return 2
    if not args.doc:
        sys.stdout.write(grammar.emit_gbnf())
        return 0
    lines = ["# Strata GBNF grammar (spec/grammar.md, parser-authoritative)", ""]
    for name, (doc, _alts) in grammar.RULES.items():
        lines.append(f"# {name.replace('-', '_')}: {doc}")
    lines.append("")
    sys.stdout.write("\n".join(lines))
    return 0