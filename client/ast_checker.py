"""
HardTruth Polyglot Anti-Stubbing Engine (Rule 3)
Deterministic analysis of source code to detect vacuous stubs (pass, NotImplementedError, dummy returns, TODOs),
while suppressing false positives on abstract methods, protocols, overloads, and exception class bodies.
Supports Python (.py), TypeScript/JavaScript (.ts, .tsx, .js, .jsx), Rust (.rs), and Go (.go).
"""

from __future__ import annotations
import ast
import os
import re
from typing import List, Optional, Set

class PythonStubVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, modified_lines: Optional[Set[int]] = None):
        self.filename = os.path.basename(filename)
        self.violations: List[str] = []
        self._class_stack: List[ast.ClassDef] = []
        self.modified_lines = modified_lines

    def visit_ClassDef(self, node: ast.ClassDef):
        self._class_stack.append(node)
        self.generic_visit(node)
        self._class_stack.pop()

    def _is_enclosing_class_protocol(self) -> bool:
        if not self._class_stack:
            return False
        cls = self._class_stack[-1]
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id == "Protocol":
                return True
            elif isinstance(base, ast.Attribute) and base.attr == "Protocol":
                return True
            elif isinstance(base, ast.Subscript):
                val = base.value
                if isinstance(val, ast.Name) and val.id == "Protocol":
                    return True
                elif isinstance(val, ast.Attribute) and val.attr == "Protocol":
                    return True
        return False

    def _is_enclosing_class_exception(self) -> bool:
        if not self._class_stack:
            return False
        cls = self._class_stack[-1]
        for base in cls.bases:
            if isinstance(base, ast.Name) and ("Error" in base.id or "Exception" in base.id):
                return True
            elif isinstance(base, ast.Attribute) and ("Error" in base.attr or "Exception" in base.attr):
                return True
        return False

    def _is_exempt_decorator(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for dec in node.decorator_list:
            dec_name = None
            if isinstance(dec, ast.Name):
                dec_name = dec.id
            elif isinstance(dec, ast.Attribute):
                dec_name = dec.attr
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    dec_name = dec.func.id
                elif isinstance(dec.func, ast.Attribute):
                    dec_name = dec.func.attr

            if dec_name in ("abstractmethod", "overload"):
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        if self._is_enclosing_class_protocol():
            return
        if self._is_exempt_decorator(node):
            return

        body = node.body
        if not body:
            return

        # Filter out leading docstring if present
        statements = body
        if len(statements) >= 1 and isinstance(statements[0], ast.Expr):
            if isinstance(statements[0].value, ast.Constant) and isinstance(statements[0].value.value, str):
                statements = statements[1:]

        is_stub = False
        stub_type = ""

        if not statements:
            is_stub = True
            stub_type = "empty body"
        else:
            stub_evals = [self._is_stmt_stub(s, node) for s in statements]
            if all(is_s for is_s, _ in stub_evals):
                is_stub = True
                stub_type = ", ".join(dict.fromkeys(t for _, t in stub_evals if t))

        if is_stub:
            node_start = getattr(node, "lineno", 1)
            node_end = getattr(node, "end_lineno", node_start)
            if self.modified_lines is not None:
                node_lines = set(range(node_start, node_end + 1))
                if not (node_lines & self.modified_lines):
                    return
            self.violations.append(
                f"Function '{node.name}' in {self.filename} is an empty stub ({stub_type}). Write actual working implementation before completing."
            )

    def _is_stmt_stub(self, stmt: ast.AST, node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, str]:
        # 1. pass
        if isinstance(stmt, ast.Pass):
            if not self._is_enclosing_class_exception():
                if node.name in ("setUp", "tearDown", "close", "cleanup", "__init__") or node.name.startswith("on_"):
                    return False, ""
                return True, "pass"
        # 2. raise NotImplementedError / raise NotImplementedError(...)
        elif isinstance(stmt, ast.Raise):
            exc = stmt.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True, "NotImplementedError"
            elif isinstance(exc, ast.Call):
                func = exc.func
                if isinstance(func, ast.Name) and func.id == "NotImplementedError":
                    return True, "NotImplementedError()"
                elif isinstance(func, ast.Attribute) and func.attr == "NotImplementedError":
                    return True, "NotImplementedError()"
            elif isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
                return True, "NotImplementedError"
        # 3. ... (Ellipsis)
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                return True, "..."
        # 4. return None, bare return, return True/False/empty in non-interface methods
        elif isinstance(stmt, ast.Return):
            if node.name in ("close", "cleanup", "setUp", "tearDown", "reset") or node.name.startswith(("is_", "has_", "can_", "should_", "supports_", "get_", "__")):
                return False, ""
            val = stmt.value
            if val is None:
                return True, "return None"
            elif isinstance(val, ast.Constant):
                if val.value is None:
                    return True, "return None"
                elif val.value in (True, False, 0, ""):
                    return True, f"return {val.value!r}"
            elif isinstance(val, (ast.Dict, ast.List, ast.Set)):
                elts = getattr(val, "keys", getattr(val, "elts", []))
                if len(elts) == 0:
                    return True, "return empty container"
        return False, ""


def check_ast_stubs(filepath: str, modified_lines: Optional[Set[int]] = None) -> List[str]:
    """
    Polyglot stub detector:
    - Python: Rejects empty stubs (pass, NotImplementedError, Ellipsis, return True/None).
              Surfaces syntax errors rather than silently swallowing them.
    - TypeScript/JavaScript: Rejects 'throw new Error("Not implemented")', TODO markers in empty functions.
    - Rust: Rejects todo!(), unimplemented!(), panic!("not implemented").
    - Go: Rejects panic("not implemented"), panic("TODO").
    """
    if not os.path.exists(filepath):
        return []

    if modified_lines is not None and len(modified_lines) == 0:
        return []

    ext = os.path.splitext(filepath)[1].lower()
    base = os.path.basename(filepath)

    # 1. Python analysis
    if ext == ".py":
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            tree = ast.parse(content, filename=filepath)
            visitor = PythonStubVisitor(filepath, modified_lines=modified_lines)
            visitor.visit(tree)
            return visitor.violations
        except SyntaxError as se:
            return [f"Syntax error in {base} line {se.lineno}: {se.msg}. Fix syntax errors before completing."]
        except Exception:
            return []

    # 2. TypeScript / JavaScript analysis (.ts, .tsx, .js, .jsx)
    elif ext in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]:
        violations = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for idx, line in enumerate(lines, 1):
                if modified_lines and idx not in modified_lines:
                    continue
                # Check for explicit unimplemented throws
                if re.search(r"throw\s+new\s+Error\s*\(\s*['\"](?:not implemented|todo|unimplemented)['\"]", line, re.IGNORECASE):
                    violations.append(f"Unimplemented throw detected in {base} line {idx}. Write actual implementation before completing.")
                # Check for TODO stubs in functions
                elif re.search(r"//\s*TODO:?\s*(?:implement|add logic|fill in|stub)", line, re.IGNORECASE):
                    violations.append(f"Unimplemented TODO stub detected in {base} line {idx}. Complete implementation before stopping.")
            return violations
        except Exception:
            return []

    # 3. Rust analysis (.rs)
    elif ext == ".rs":
        violations = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for idx, line in enumerate(lines, 1):
                if modified_lines and idx not in modified_lines:
                    continue
                if re.search(r"\b(todo!|unimplemented!)\s*\(", line):
                    violations.append(f"Rust macro '{re.search(r'(todo!|unimplemented!)', line).group(1)}' detected in {base} line {idx}. Implement real logic before completing.")
                elif re.search(r'panic!\s*\(\s*["\'](?:not implemented|todo)["\']', line, re.IGNORECASE):
                    violations.append(f"Unimplemented panic detected in {base} line {idx}. Implement real logic before completing.")
            return violations
        except Exception:
            return []

    # 4. Go analysis (.go)
    elif ext == ".go":
        violations = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for idx, line in enumerate(lines, 1):
                if modified_lines and idx not in modified_lines:
                    continue
                if re.search(r'panic\s*\(\s*["\'](?:not implemented|TODO)["\']', line, re.IGNORECASE):
                    violations.append(f"Unimplemented Go panic detected in {base} line {idx}. Implement real logic before completing.")
            return violations
        except Exception:
            return []

    return []
