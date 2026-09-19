"""
HardTruth AST Stub Detection Engine
Deterministic analysis of Python ASTs to detect vacuous stubs (pass, NotImplementedError, dummy returns),
while suppressing false positives on abstract methods, protocols, and overloads.
"""

import ast
import os
from typing import List

class StubVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = os.path.basename(filename)
        self.violations: List[str] = []
        self._class_stack: List[ast.ClassDef] = []

    def visit_ClassDef(self, node: ast.ClassDef):
        self._class_stack.append(node)
        self.generic_visit(node)
        self._class_stack.pop()

    def _is_enclosing_class_protocol(self) -> bool:
        if not self._class_stack:
            return False
        cls = self._class_stack[-1]
        for base in cls.bases:
            # Check Protocol or typing.Protocol or Protocol[T]
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

        if len(statements) != 1:
            return

        stmt = statements[0]
        is_stub = False
        stub_type = ""

        # 1. pass
        if isinstance(stmt, ast.Pass):
            is_stub = True
            stub_type = "pass"

        # 2. raise NotImplementedError / raise NotImplementedError(...)
        elif isinstance(stmt, ast.Raise):
            exc = stmt.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                is_stub = True
                stub_type = "NotImplementedError"
            elif isinstance(exc, ast.Call):
                func = exc.func
                if isinstance(func, ast.Name) and func.id == "NotImplementedError":
                    is_stub = True
                    stub_type = "NotImplementedError()"
                elif isinstance(func, ast.Attribute) and func.attr == "NotImplementedError":
                    is_stub = True
                    stub_type = "NotImplementedError()"
            elif isinstance(exc, ast.Attribute) and exc.attr == "NotImplementedError":
                is_stub = True
                stub_type = "NotImplementedError"

        # 3. ... (Ellipsis)
        elif isinstance(stmt, ast.Expr):
            if isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                is_stub = True
                stub_type = "..."

        # 4. return True, return None, bare return
        elif isinstance(stmt, ast.Return):
            val = stmt.value
            if val is None:
                is_stub = True
                stub_type = "return"
            elif isinstance(val, ast.Constant):
                if val.value is None:
                    is_stub = True
                    stub_type = "return None"
                elif val.value is True:
                    is_stub = True
                    stub_type = "return True"

        if is_stub:
            self.violations.append(
                f"Function '{node.name}' in {self.filename} is an empty stub ({stub_type}). Write actual working implementation before completing."
            )

def check_ast_stubs(filepath: str) -> List[str]:
    """
    Deterministic AST check: rejects empty stubs (pass, NotImplementedError, Ellipsis, return True/None).
    Suppresses legitimate stubs in Protocol classes, @abstractmethod, and @overload.
    """
    if not os.path.exists(filepath) or not filepath.endswith(".py"):
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        tree = ast.parse(content, filename=filepath)
        visitor = StubVisitor(filepath)
        visitor.visit(tree)
        return visitor.violations
    except Exception:
        return []
