"""A small lint for the kit's Python (the standard library only): syntax, unused imports,
trailing whitespace, tabs and over-long lines."""

import ast
import unittest

import support

FILES = sorted(p for d in ("tools", "tests", "bin") for p in (support.ROOT / d).rglob("*")
               if p.is_file() and (p.suffix == ".py" or p.name == "kit")
               and "__pycache__" not in p.parts)
MAX = 120


class Lint(unittest.TestCase):
    def test_files(self):
        self.assertGreater(len(FILES), 10)
        problems = []
        for p in FILES:
            text = p.read_text(encoding="utf-8")
            rel = p.relative_to(support.ROOT)
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
            used |= {n.value.id for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
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
