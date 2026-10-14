"""A small lint for the benchmark harness's Python (standard library only): syntax,
unused imports, trailing whitespace, tabs and over-long lines."""

import ast
import unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
FILES = sorted([p for p in BENCH.rglob("*.py") if "__pycache__" not in p.parts]
               + [BENCH.parent / "bin" / "bench"])
MAX = 160


class Lint(unittest.TestCase):
    def test_files(self):
        self.assertGreater(len(FILES), 8)
        problems = []
        for p in FILES:
            text = p.read_text(encoding="utf-8")
            rel = p.relative_to(BENCH.parent)
            tree = ast.parse(text, filename=str(p))
            imported = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        imported[(a.asname or a.name).split(".")[0]] = n.lineno
                elif isinstance(n, ast.ImportFrom):
                    for a in n.names:
                        imported[a.asname or a.name] = n.lineno
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            lines = text.split("\n")
            for name, ln in imported.items():
                noqa = "noqa" in lines[ln - 1]
                if name not in used and name != "annotations" and not noqa and p.name != "__init__.py":
                    problems.append(f"{rel}:{ln}: unused import {name}")
            for i, line in enumerate(lines, 1):
                if line != line.rstrip():
                    problems.append(f"{rel}:{i}: trailing whitespace")
                if "\t" in line:
                    problems.append(f"{rel}:{i}: tab")
                if len(line) > MAX:
                    problems.append(f"{rel}:{i}: line longer than {MAX}")
            if not text.endswith("\n"):
                problems.append(f"{rel}: no final newline")
        self.assertEqual("\n".join(problems), "")


if __name__ == "__main__":
    unittest.main()
