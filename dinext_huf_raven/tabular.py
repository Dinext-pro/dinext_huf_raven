"""Bounded data analysis over the employee's in-memory record projection."""
import ast
from contextlib import closing
import json
import operator
import re
import sqlite3

MAX_ROWS = 500
MAX_COLUMNS = 24
MAX_OUTPUT = 100
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,63}\Z")
SQL_FUNCTIONS = frozenset("""
    abs avg ceil coalesce count date datetime floor group_concat ifnull
    instr length lower ltrim max min nullif replace round rtrim strftime
    substr sum total trim upper
""".split())


def scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4096]


def sql(tables, statement):
    if not isinstance(statement, str) or len(statement) > 6000:
        raise ValueError("Supply a SELECT query of up to 6000 characters")
    with closing(sqlite3.connect(":memory:")) as db:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256 * 1024)
        db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        db.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 30)
        for name, (fields, rows) in tables.items():
            if not IDENTIFIER.fullmatch(name) or any(not IDENTIFIER.fullmatch(f) for f in fields):
                raise ValueError("Invalid table or column name")
            columns = ", ".join(f'"{f}"' for f in fields)
            slots = ", ".join("?" for _ in fields)
            db.execute(f'CREATE TABLE "{name}" ({columns})')
            db.executemany(f'INSERT INTO "{name}" VALUES ({slots})',
                           ([scalar(row.get(f)) for f in fields] for row in rows))
        def allowed(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and first in tables and database in ("main", None):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in SQL_FUNCTIONS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        budget = 500
        def stop():
            nonlocal budget
            budget -= 1
            return budget <= 0
        db.set_authorizer(allowed)
        db.set_progress_handler(stop, 1000)
        cursor = db.execute(statement)
        if cursor.description is None:
            raise ValueError("Expected a result table")
        rows = cursor.fetchmany(MAX_OUTPUT + 1)
        return {"columns": [col[0] for col in cursor.description],
                "rows": [[scalar(cell) for cell in row] for row in rows[:MAX_OUTPUT]],
                "truncated": len(rows) > MAX_OUTPUT}


def python(tables, code):
    """Evaluate positive-listed DataFrame operations with Python syntax."""
    import pandas as pd

    if not isinstance(code, str) or len(code) > 4000:
        raise ValueError("Supply a Python analysis of up to 4000 characters")
    program = ast.parse(code)
    if len(list(ast.walk(program))) > 180 or len(program.body) > 12:
        raise ValueError("Analysis exceeds the expression limit")
    names = {key: pd.DataFrame(rows, columns=fields) for key, (fields, rows) in tables.items()}
    if "records" in names:
        names["df"] = names["records"]
    comparisons = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Gt: operator.gt,
                   ast.Lt: operator.lt, ast.GtE: operator.ge, ast.LtE: operator.le}
    aggregates = {"sum", "mean", "median", "min", "max", "count", "nunique", "std"}
    def value(node):
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool, type(None))):
            if isinstance(node.value, int) and abs(node.value) > 10**12:
                raise ValueError("Integer exceeds limit")
            return node.value
        if isinstance(node, (ast.Tuple, ast.List)):
            return [value(item) for item in node.elts]
        if isinstance(node, ast.Subscript):
            obj, key = value(node.value), value(node.slice)
            if isinstance(key, (str, list, pd.Series)):
                return obj[key]
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in comparisons:
            return comparisons[type(node.ops[0])](value(node.left), value(node.comparators[0]))
        if isinstance(node, ast.Call) and not node.keywords:
            args = [value(a) for a in node.args]
            if isinstance(node.func, ast.Name) and node.func.id == "len" and len(args) == 1:
                return len(args[0])
            if isinstance(node.func, ast.Attribute):
                obj, method = value(node.func.value), node.func.attr
                if method in aggregates | {"describe", "value_counts", "reset_index", "dropna", "size"} and not args:
                    fn = getattr(obj, method)
                    return fn() if callable(fn) else fn
                if method in {"head", "tail", "round"} and len(args) <= 1 and all(type(a) is int and 0 <= a <= MAX_OUTPUT for a in args):
                    return getattr(obj, method)(*args)
                if method in {"groupby", "sort_values"} and len(args) == 1:
                    cols = args[0] if isinstance(args[0], list) else [args[0]]
                    if isinstance(obj, pd.DataFrame) and all(isinstance(f, str) and f in obj.columns for f in cols):
                        return getattr(obj, method)(args[0])
                if method == "agg" and len(args) == 1 and isinstance(args[0], list) and all(a in aggregates for a in args[0]):
                    return obj.agg(args[0])
        raise ValueError("Unsupported Python operation. Use column selection, filters, groupby and aggregates.")
    result = None
    for line in program.body:
        if isinstance(line, ast.Assign) and len(line.targets) == 1 and isinstance(line.targets[0], ast.Name):
            result = names[line.targets[0].id] = value(line.value)
        elif isinstance(line, ast.Expr):
            result = value(line.value)
        else:
            raise ValueError("Only assignments and analytical expressions are supported")
    if isinstance(result, pd.Series):
        result = result.to_frame(name=result.name or "value")
    if isinstance(result, pd.DataFrame):
        if any(n is not None for n in result.index.names):
            result = result.reset_index()
        return {"rows": json.loads(result.head(MAX_OUTPUT).to_json(orient="records", date_format="iso")),
                "truncated": len(result) > MAX_OUTPUT}
    if hasattr(result, "item"):
        result = result.item()
    if not isinstance(result, (int, float, str, bool, type(None))):
        raise ValueError("Return a scalar or an aggregated table")
    return {"value": scalar(result)}
