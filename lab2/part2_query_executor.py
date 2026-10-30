#!/usr/bin/env python3
import argparse
import json
import re
import sys
from collections import namedtuple
from dataclasses import dataclass
from typing import Any, List, Optional


class QueryError(Exception):
    """Any user-facing problem: bad syntax, unknown attribute, incompatible relations..."""


# =====================================================================
# 1. RELATION  (a table = list of column descriptors + list of row tuples)
# =====================================================================
def dedupe(rows):
    """Relations are SETS: drop duplicate rows but keep first-seen order."""
    try:
        return list(dict.fromkeys(rows))
    except TypeError:
        raise QueryError("cell values must be numbers, strings or null")


class Relation:
    """cols: list of (relation_name, attribute_name); rows: list of tuples.
    Remembering the relation name of each column lets us tell
    Employee.Name from Department.Name after a join."""

    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows

    @classmethod
    def from_records(cls, name, records):
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise QueryError(f"table '{name}' must be a list of JSON objects")
        attrs = []
        for rec in records:
            for key in rec:
                if key not in attrs:
                    attrs.append(key)
        rows = [tuple(rec.get(a) for a in attrs) for rec in records]
        return cls([(name, a) for a in attrs], dedupe(rows))

    def headers(self):
        """Display names: bare attribute, or Rel.attr when the bare name is not unique."""
        counts = {}
        for _, a in self.cols:
            counts[a] = counts.get(a, 0) + 1
        return [a if counts[a] == 1 else f"{r}.{a}" for r, a in self.cols]

    def index_of(self, ref):
        """Resolve 'Attr' or 'Rel.Attr' to a column position."""
        if "." in ref:
            rel, attr = ref.split(".", 1)
            hits = [i for i, (r, a) in enumerate(self.cols) if r == rel and a == attr]
        else:
            hits = [i for i, (_, a) in enumerate(self.cols) if a == ref]
        if not hits:
            raise QueryError(f"unknown attribute '{ref}' (available: {', '.join(self.headers())})")
        if len(hits) > 1:
            raise QueryError(f"attribute '{ref}' is ambiguous - write it as Relation.{ref}")
        return hits[0]


def fmt(value):
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(round(value, 4))
    return str(value)


def format_table(rel):
    heads = rel.headers()
    body = [[fmt(v) for v in row] for row in rel.rows]
    widths = [max([len(h)] + [len(r[i]) for r in body]) for i, h in enumerate(heads)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    def row_text(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    out = [line, row_text(heads), line] + [row_text(r) for r in body] + [line]
    out.append(f"({len(body)} row{'s' if len(body) != 1 else ''})")
    return "\n".join(out)


# =====================================================================
# 2. CONDITIONS  (used by SELECT and the theta / outer joins)
# =====================================================================
COMPARATORS = {"=", "!=", "<", "<=", ">", ">="}
_ORDER = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
          ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def compare(op, a, b):
    if a is None or b is None:        # NULL compared with anything is never true
        return False
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    try:
        return _ORDER[op](a, b)
    except TypeError:
        raise QueryError(f"cannot compare {a!r} with {b!r}")


def compile_operand(node, rel):
    if node[0] == "const":
        value = node[1]
        return lambda row: value
    i = rel.index_of(node[1])         # resolve the attribute ONCE, not per row
    return lambda row: row[i]


def compile_condition(node, rel):
    """Turn a condition tree into a Python function  row -> bool."""
    kind = node[0]
    if kind == "or":
        f, g = compile_condition(node[1], rel), compile_condition(node[2], rel)
        return lambda row: f(row) or g(row)
    if kind == "and":
        f, g = compile_condition(node[1], rel), compile_condition(node[2], rel)
        return lambda row: f(row) and g(row)
    if kind == "not":
        f = compile_condition(node[1], rel)
        return lambda row: not f(row)
    if kind == "isnull":
        get, negate = compile_operand(node[1], rel), node[2]
        return lambda row: (get(row) is None) != negate
    _, op, left, right = node          # ("cmp", op, left, right)
    get_l, get_r = compile_operand(left, rel), compile_operand(right, rel)
    return lambda row: compare(op, get_l(row), get_r(row))


# =====================================================================
# 3. THE OPERATORS  (each takes Relation(s), returns a new Relation)
# =====================================================================
def op_select(rel, predicate):
    return Relation(rel.cols, [row for row in rel.rows if predicate(row)])


def op_project(rel, refs):
    idx = [rel.index_of(r) for r in refs]
    if len(set(idx)) != len(idx):
        raise QueryError("PROJECT lists the same attribute twice")
    rows = [tuple(row[i] for i in idx) for row in rel.rows]
    return Relation([rel.cols[i] for i in idx], dedupe(rows))


def op_rename(rel, new_name, new_attrs):
    if new_attrs is not None and len(new_attrs) != len(rel.cols):
        raise QueryError(f"RENAME gives {len(new_attrs)} names for {len(rel.cols)} attributes")
    cols = [(new_name or r, new_attrs[i] if new_attrs else a) for i, (r, a) in enumerate(rel.cols)]
    if len(set(cols)) != len(cols):
        raise QueryError("RENAME would create two attributes with the same name")
    return Relation(cols, rel.rows)


def check_no_clash(left, right, what):
    clash = set(left.cols) & set(right.cols)
    if clash:
        r, a = sorted(clash)[0]
        raise QueryError(f"{what}: both sides contain {r}.{a} - RENAME one side first")


def op_cross(left, right):
    check_no_clash(left, right, "CROSS")
    return Relation(left.cols + right.cols, [l + r for l in left.rows for r in right.rows])


def op_theta_join(left, right, cond):
    check_no_clash(left, right, "JOIN")
    combined = Relation(left.cols + right.cols, [])
    pred = compile_condition(cond, combined)
    rows = []
    for l in left.rows:
        for r in right.rows:
            if pred(l + r):
                rows.append(l + r)
    return Relation(combined.cols, rows)


def op_natural_join(left, right):
    pairs = [(i, j) for i, (_, a) in enumerate(left.cols)
             for j, (_, b) in enumerate(right.cols) if a == b]
    if not pairs:
        return op_cross(left, right)
    right_keep = [j for j in range(len(right.cols)) if j not in {j for _, j in pairs}]
    rows = []
    for l in left.rows:
        for r in right.rows:
            if all(l[i] == r[j] and l[i] is not None for i, j in pairs):
                rows.append(l + tuple(r[j] for j in right_keep))
    return Relation(left.cols + [right.cols[j] for j in right_keep], dedupe(rows))


def op_outer_join(left, right, cond, kind):
    check_no_clash(left, right, "OUTER JOIN")
    combined = Relation(left.cols + right.cols, [])
    pred = compile_condition(cond, combined)
    null_l, null_r = (None,) * len(left.cols), (None,) * len(right.cols)
    rows, matched_right = [], set()
    for l in left.rows:
        found = False
        for j, r in enumerate(right.rows):
            if pred(l + r):
                rows.append(l + r)
                matched_right.add(j)
                found = True
        if not found and kind in ("left", "full"):
            rows.append(l + null_r)           # pad the right side with NULLs
    if kind in ("right", "full"):
        for j, r in enumerate(right.rows):
            if j not in matched_right:
                rows.append(null_l + r)       # pad the left side with NULLs
    return Relation(combined.cols, rows)


def value_kinds(rel, i):
    return {"number" if isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
            else type(r[i]).__name__ for r in rel.rows if r[i] is not None}


def check_compatible(left, right, op):
    if len(left.cols) != len(right.cols):
        raise QueryError(f"{op}: not union compatible "
                         f"({len(left.cols)} vs {len(right.cols)} attributes)")
    lh, rh = left.headers(), right.headers()
    for i in range(len(left.cols)):
        a, b = value_kinds(left, i), value_kinds(right, i)
        if a and b and a != b:
            raise QueryError(f"{op}: attributes '{lh[i]}' and '{rh[i]}' have different domains")


def op_union(left, right):
    check_compatible(left, right, "UNION")
    return Relation(left.cols, dedupe(left.rows + right.rows))   # names come from the FIRST relation


def op_intersect(left, right):
    check_compatible(left, right, "INTERSECT")
    keep = set(right.rows)
    return Relation(left.cols, [r for r in left.rows if r in keep])


def op_difference(left, right):
    check_compatible(left, right, "DIFFERENCE")
    drop = set(right.rows)
    return Relation(left.cols, [r for r in left.rows if r not in drop])


def op_outer_union(left, right):
    """Partially compatible relations: keep every attribute, pad the missing ones with NULL."""
    names = [a for _, a in left.cols]
    extra = [c for c in right.cols if c[1] not in names]
    cols = left.cols + extra
    r_pos = {a: i for i, (_, a) in enumerate(right.cols)}
    rows = [l + (None,) * len(extra) for l in left.rows]
    for r in right.rows:
        rows.append(tuple(r[r_pos[a]] if a in r_pos else None for _, a in cols))
    return Relation(cols, dedupe(rows))


def op_divide(left, right):
    """T(Y) = R(Z) / S(X): Y-values of R that appear with EVERY tuple of S."""
    x_idx = []
    for _, a in right.cols:
        try:
            x_idx.append(left.index_of(a))
        except QueryError:
            raise QueryError(f"DIVIDE: divisor attribute '{a}' is not in the dividend")
    y_idx = [i for i in range(len(left.cols)) if i not in x_idx]
    if not y_idx:
        raise QueryError("DIVIDE: the dividend must have attributes beyond the divisor's")
    needed = set(right.rows)
    seen = {}
    for row in left.rows:
        seen.setdefault(tuple(row[i] for i in y_idx), set()).add(tuple(row[i] for i in x_idx))
    rows = [y for y, xs in seen.items() if needed <= xs]
    return Relation([left.cols[i] for i in y_idx], rows)


AGG_FUNCS = {"COUNT": ("COUNT", "Count"), "SUM": ("SUM", "Sum"),
             "AVG": ("AVG", "Average"), "AVERAGE": ("AVG", "Average"),
             "MIN": ("MIN", "Min"), "MINIMUM": ("MIN", "Min"),
             "MAX": ("MAX", "Max"), "MAXIMUM": ("MAX", "Max")}


def compute_aggregate(func, values):
    if func == "COUNT":
        return len(values)
    if not values:
        return None
    if func in ("SUM", "AVG") and not all(isinstance(v, (int, float)) for v in values):
        raise QueryError(f"{func} needs numeric values")
    if func == "SUM":
        return sum(values)
    if func == "AVG":
        return sum(values) / len(values)
    return min(values) if func == "MIN" else max(values)


def op_aggregate(rel, group_refs, items):
    """items: list of (func, argument or '*', output_name)."""
    g_idx = [rel.index_of(r) for r in group_refs]
    a_idx = [None if arg == "*" else rel.index_of(arg) for _, arg, _ in items]
    groups = {}
    for row in rel.rows:
        groups.setdefault(tuple(row[i] for i in g_idx), []).append(row)
    if not g_idx and not groups:
        groups[()] = []                    # aggregate over an empty relation = one row
    rows = []
    for key, members in groups.items():
        out = list(key)
        for (func, _, _), ai in zip(items, a_idx):
            values = members if ai is None else [m[ai] for m in members if m[ai] is not None]
            out.append(compute_aggregate(func, values))
        rows.append(tuple(out))
    cols = [rel.cols[i] for i in g_idx] + [("", name) for _, _, name in items]
    return Relation(cols, rows)


def op_closure(rel, src_ref, dst_ref):
    """RECURSIVE query: transitive closure of the edges  src -> dst.
    Returns (src, dst, Level): dst is reachable from src in `Level` steps."""
    i, j = rel.index_of(src_ref), rel.index_of(dst_ref)
    edges = {}                                      # src value -> list of dst values
    level = {}                                      # (src, dst) -> first level reached
    frontier = []
    for row in rel.rows:
        a, b = row[i], row[j]
        if a is None or b is None:
            continue
        edges.setdefault(a, []).append(b)
        if (a, b) not in level:
            level[(a, b)] = 1
            frontier.append((a, b))
    depth = 1
    while frontier:                                 # fixpoint: stop when nothing new appears
        depth += 1
        new_pairs = []
        for a, b in frontier:
            for c in edges.get(b, []):
                if (a, c) not in level:
                    level[(a, c)] = depth
                    new_pairs.append((a, c))
        frontier = new_pairs
    cols = [rel.cols[i], rel.cols[j], (rel.cols[i][0], "Level")]
    return Relation(cols, [(a, c, lv) for (a, c), lv in level.items()])


# =====================================================================
# 4. TOKENIZER
# =====================================================================
Token = namedtuple("Token", "kind value pos")      # kind: NUM STR ID SYM END

TOKEN_RE = re.compile(r'''
    (?P<ws>[ \t\r]+)
  | (?P<comment>\#[^\n]*)
  | (?P<nl>\n)
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<id>[^\W\d]\w*(?:\.[^\W\d]\w*)?)
  | (?P<sym><-|:=|<=|>=|!=|<>|==|[-=<>(),;\[\]*]|[∪∩−–×÷⋈←≤≥≠])
''', re.X)

WORD_ALIASES = {"σ": "SELECT", "π": "PROJECT", "Π": "PROJECT", "ρ": "RENAME",
                "ℱ": "AGGREGATE", "ℑ": "AGGREGATE"}
KEYWORD_SYMBOLS = {"∪": "UNION", "∩": "INTERSECT", "×": "CROSS", "÷": "DIVIDE", "⋈": "JOIN"}
SYMBOL_ALIASES = {"←": "<-", ":=": "<-", "≤": "<=", "≥": ">=", "≠": "!=",
                  "<>": "!=", "==": "=", "−": "-", "–": "-"}


def tokenize(text):
    tokens, pos, depth = [], 0, 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise QueryError(f"unexpected character {text[pos]!r} at position {pos}")
        kind, value, start = m.lastgroup, m.group(), pos
        pos = m.end()
        if kind in ("ws", "comment"):
            continue
        if kind == "nl":                            # a newline ends a statement
            if depth == 0:                          # ...unless we are inside ( ) or [ ]
                tokens.append(Token("SYM", ";", start))
        elif kind == "num":
            tokens.append(Token("NUM", float(value) if "." in value else int(value), start))
        elif kind == "str":
            tokens.append(Token("STR", value[1:-1], start))
        elif kind == "id":
            tokens.append(Token("ID", WORD_ALIASES.get(value, value), start))
        elif value in KEYWORD_SYMBOLS:
            tokens.append(Token("ID", KEYWORD_SYMBOLS[value], start))
        else:
            value = SYMBOL_ALIASES.get(value, value)
            if value in "([":
                depth += 1
            elif value in ")]":
                depth = max(0, depth - 1)
            tokens.append(Token("SYM", value, start))
    tokens.append(Token("END", None, len(text)))
    return tokens


# =====================================================================
# 5. SYNTAX TREE
# =====================================================================
@dataclass
class RelRef:   name: str
@dataclass
class Select:   cond: Any; child: Any
@dataclass
class Project:  refs: List[str]; child: Any
@dataclass
class Rename:   name: Optional[str]; attrs: Optional[List[str]]; child: Any
@dataclass
class Aggregate: group: List[str]; items: list; child: Any
@dataclass
class Closure:  src: str; dst: str; child: Any
@dataclass
class Join:     kind: str; left: Any; right: Any; cond: Any = None
@dataclass
class SetOp:    op: str; left: Any; right: Any
@dataclass
class Assign:   name: str; attrs: Optional[List[str]]; expr: Any
@dataclass
class ExprStmt: expr: Any


# =====================================================================
# 6. PARSER  
# =====================================================================
UNARY_WORDS = {"SELECT", "PROJECT", "RENAME", "AGGREGATE", "CLOSURE"}


class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.pos = 0

    # ---- token helpers
    def peek(self, k=0):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self):
        tok = self.toks[self.pos]
        if tok.kind != "END":
            self.pos += 1
        return tok

    def is_sym(self, value, k=0):
        t = self.peek(k)
        return t.kind == "SYM" and t.value == value

    def is_kw(self, *words, k=0):
        t = self.peek(k)
        return t.kind == "ID" and t.value.upper() in words

    def accept_sym(self, value):
        if self.is_sym(value):
            self.next()
            return True
        return False

    def error(self, msg):
        t = self.peek()
        near = "end of input" if t.kind == "END" else repr(t.value)
        raise QueryError(f"{msg} (at position {t.pos}, near {near})")

    def expect_sym(self, value):
        if not self.accept_sym(value):
            self.error(f"expected '{value}'")

    def expect_id(self, what="a name"):
        if self.peek().kind != "ID":
            self.error(f"expected {what}")
        return self.next().value

    def parse_name_list(self):
        names = [self.expect_id("an attribute name")]
        while self.accept_sym(","):
            names.append(self.expect_id("an attribute name"))
        return names

    # ---- statements
    def parse_program(self):
        stmts = []
        while True:
            while self.accept_sym(";"):
                pass
            if self.peek().kind == "END":
                return stmts
            stmts.append(self.parse_statement())
            if not (self.peek().kind == "END" or self.is_sym(";")):
                self.error("unexpected input after the end of a statement")

    def parse_statement(self):
        if self.peek().kind == "ID":
            save = self.pos
            name = self.next().value
            attrs = None
            try:
                if self.accept_sym("("):
                    attrs = self.parse_name_list()
                    self.expect_sym(")")
                if self.accept_sym("<-"):
                    return Assign(name, attrs, self.parse_set_expr())
            except QueryError:
                pass
            self.pos = save                         # not an assignment: re-read as an expression
        return ExprStmt(self.parse_set_expr())

    # ---- expressions
    def parse_set_expr(self):
        left = self.parse_join_expr()
        while True:
            if self.is_kw("UNION"):
                op = "union"
            elif self.is_kw("INTERSECT"):
                op = "intersect"
            elif self.is_kw("DIFFERENCE", "MINUS") or self.is_sym("-"):
                op = "difference"
            elif self.is_kw("OUTER") and self.is_kw("UNION", k=1):
                op = "outer_union"
                self.next()
            else:
                return left
            self.next()
            left = SetOp(op, left, self.parse_join_expr())

    def parse_optional_condition(self):
        if self.accept_sym("["):
            cond = self.parse_condition()
            self.expect_sym("]")
            return cond
        return None

    def parse_join_expr(self):
        left = self.parse_primary()
        while True:
            cond = None
            if self.accept_sym("*"):
                kind = "natural"
            elif self.is_kw("NATURAL"):
                self.next()
                if not self.is_kw("JOIN"):
                    self.error("expected JOIN after NATURAL")
                self.next()
                kind = "natural"
            elif self.is_kw("JOIN"):
                self.next()
                cond = self.parse_optional_condition()
                kind = "theta" if cond else "natural"
            elif self.is_kw("CROSS"):
                self.next()
                if self.is_kw("JOIN"):
                    self.next()
                kind = "cross"
            elif self.is_kw("DIVIDE"):
                self.next()
                kind = "divide"
            elif self.is_kw("LEFT", "RIGHT", "FULL"):
                kind = self.next().value.lower()
                if self.is_kw("OUTER"):
                    self.next()
                if not self.is_kw("JOIN"):
                    self.error("expected JOIN")
                self.next()
                cond = self.parse_optional_condition()
                if cond is None:
                    self.error("an outer join needs a condition, e.g. LEFT JOIN[A = B]")
            else:
                return left
            left = Join(kind, left, self.parse_primary(), cond)

    def parse_primary(self):
        t = self.peek()
        if self.accept_sym("("):
            expr = self.parse_set_expr()
            self.expect_sym(")")
            return expr
        if t.kind == "ID" and t.value.upper() in UNARY_WORDS and self.is_sym("[", k=1):
            return self.parse_unary()
        if t.kind == "ID":
            return RelRef(self.next().value)
        self.error("expected a relation name, '(' or an operator such as SELECT[...]")

    def parse_unary(self):
        word = self.next().value.upper()
        self.expect_sym("[")
        if word == "SELECT":
            args = (self.parse_condition(),)
        elif word == "PROJECT":
            args = (self.parse_name_list(),)
        elif word == "RENAME":
            args = self.parse_rename_spec()
        elif word == "AGGREGATE":
            args = self.parse_aggregate_spec()
        else:                                       # CLOSURE
            src = self.expect_id("the 'from' attribute")
            self.expect_sym(",")
            args = (src, self.expect_id("the 'to' attribute"))
        self.expect_sym("]")
        self.expect_sym("(")
        child = self.parse_set_expr()
        self.expect_sym(")")
        node = {"SELECT": Select, "PROJECT": Project, "RENAME": Rename,
                "AGGREGATE": Aggregate, "CLOSURE": Closure}[word]
        return node(*args, child)

    def parse_rename_spec(self):                    # S  |  S(a,b,c)  |  (a,b,c)
        name = attrs = None
        if self.peek().kind == "ID":
            name = self.next().value
        if self.accept_sym("("):
            attrs = self.parse_name_list()
            self.expect_sym(")")
        if name is None and attrs is None:
            self.error("RENAME needs a new name and/or a list of new attribute names")
        return name, attrs

    def semicolon_ahead(self):
        i = self.pos
        while not (self.toks[i].kind == "END" or (self.toks[i].kind == "SYM" and self.toks[i].value == "]")):
            if self.toks[i].kind == "SYM" and self.toks[i].value == ";":
                return True
            i += 1
        return False

    def parse_aggregate_spec(self):                 # [ group_attrs ; COUNT x, AVG y AS z ]
        group = []
        if self.semicolon_ahead():
            group = [] if self.is_sym(";") else self.parse_name_list()
            self.expect_sym(";")
        items = []
        while True:
            word = self.expect_id("an aggregate function (COUNT, SUM, AVG, MIN, MAX)")
            if word.upper() not in AGG_FUNCS:
                self.error(f"unknown aggregate function '{word}'")
            func, label = AGG_FUNCS[word.upper()]
            paren = self.accept_sym("(")
            arg = "*" if self.accept_sym("*") else self.expect_id("an attribute name")
            if paren:
                self.expect_sym(")")
            if arg == "*" and func != "COUNT":
                self.error(f"{func} cannot be applied to *")
            name = label if arg == "*" else f"{label}_{arg.split('.')[-1]}"
            if self.is_kw("AS"):
                self.next()
                name = self.expect_id("an output name")
            items.append((func, arg, name))
            if not self.accept_sym(","):
                return group, items

    # ---- conditions:  or > and > not > comparison
    def parse_condition(self):
        left = self.parse_and()
        while self.is_kw("OR"):
            self.next()
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.is_kw("AND"):
            self.next()
            left = ("and", left, self.parse_not())
        return left

    def parse_not(self):
        if self.is_kw("NOT"):
            self.next()
            return ("not", self.parse_not())
        if self.accept_sym("("):
            cond = self.parse_condition()
            self.expect_sym(")")
            return cond
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_operand()
        if self.is_kw("IS"):                        # attr IS [NOT] NULL
            self.next()
            negate = self.is_kw("NOT")
            if negate:
                self.next()
            if not self.is_kw("NULL"):
                self.error("expected NULL")
            self.next()
            return ("isnull", left, negate)
        t = self.peek()
        if t.kind != "SYM" or t.value not in COMPARATORS:
            self.error("expected a comparison operator (=, !=, <, <=, >, >=)")
        self.next()
        return ("cmp", t.value, left, self.parse_operand())

    def parse_operand(self):
        t = self.peek()
        if self.is_sym("-") and self.peek(1).kind == "NUM":
            self.next()
            return ("const", -self.next().value)
        if t.kind in ("NUM", "STR"):
            self.next()
            return ("const", t.value)
        if t.kind == "ID":
            self.next()
            return ("const", None) if t.value.upper() == "NULL" else ("attr", t.value)
        self.error("expected an attribute name or a constant")


# =====================================================================
# 7. EVALUATOR  (walk the tree, call the operators)
# =====================================================================
class Evaluator:
    def __init__(self, db):
        self.db = db                                # name -> Relation (assignments add to it)

    def run_text(self, text):
        """Run every statement; return a list of (label, Relation)."""
        results = []
        for stmt in Parser(tokenize(text)).parse_program():
            if isinstance(stmt, Assign):
                rel = op_rename(self.evaluate(stmt.expr), stmt.name, stmt.attrs)
                self.db[stmt.name] = rel
                results.append((stmt.name, rel))
            else:
                results.append(("result", self.evaluate(stmt.expr)))
        return results

    def evaluate(self, node):
        return getattr(self, "eval_" + type(node).__name__)(node)

    def eval_RelRef(self, n):
        if n.name in self.db:                       # exact match first
            return self.db[n.name]
        same = [k for k in self.db if k.lower() == n.name.lower()]
        if len(same) == 1:                          # lecture writes EMPLOYEE, data says Employee
            return self.db[same[0]]
        raise QueryError(f"unknown relation '{n.name}' (available: {', '.join(self.db)})")

    def eval_Select(self, n):
        rel = self.evaluate(n.child)
        return op_select(rel, compile_condition(n.cond, rel))

    def eval_Project(self, n):
        return op_project(self.evaluate(n.child), n.refs)

    def eval_Rename(self, n):
        return op_rename(self.evaluate(n.child), n.name, n.attrs)

    def eval_Aggregate(self, n):
        return op_aggregate(self.evaluate(n.child), n.group, n.items)

    def eval_Closure(self, n):
        return op_closure(self.evaluate(n.child), n.src, n.dst)

    def eval_Join(self, n):
        left, right = self.evaluate(n.left), self.evaluate(n.right)
        if n.kind == "natural":
            return op_natural_join(left, right)
        if n.kind == "cross":
            return op_cross(left, right)
        if n.kind == "theta":
            return op_theta_join(left, right, n.cond)
        if n.kind == "divide":
            return op_divide(left, right)
        return op_outer_join(left, right, n.cond, n.kind)      # left / right / full

    def eval_SetOp(self, n):
        left, right = self.evaluate(n.left), self.evaluate(n.right)
        return {"union": op_union, "intersect": op_intersect,
                "difference": op_difference, "outer_union": op_outer_union}[n.op](left, right)


# =====================================================================
# 8. COMMAND LINE
# =====================================================================
def load_database(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise QueryError("the data file must be a JSON object: {table_name: [rows...]}")
    return {name: Relation.from_records(name, recs) for name, recs in raw.items()}


def show(results, verbose):
    for label, rel in (results if verbose else results[-1:]):
        if verbose:
            print(f"\n{label}:")
        print(format_table(rel))


def repl(evaluator):
    print("Relational algebra prompt.  'tables' lists relations, 'exit' quits.")
    while True:
        try:
            line = input("ra> ").strip()
        except EOFError:
            break
        if line.lower() in ("exit", "quit"):
            break
        if line.lower() == "tables":
            print(", ".join(evaluator.db))
        elif line:
            try:
                show(evaluator.run_text(line), verbose=False)
            except QueryError as err:
                print("error:", err)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Relational algebra query executor")
    ap.add_argument("data", help="JSON file: {table: [ {col: value}, ... ]}")
    ap.add_argument("-q", "--query", help="query text to run")
    ap.add_argument("-f", "--file", help="file containing one or more statements")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every statement's result")
    args = ap.parse_args(argv)
    try:
        evaluator = Evaluator(load_database(args.data))
        if args.query or args.file:
            text = args.query
            if args.file:
                with open(args.file, encoding="utf-8") as f:
                    text = f.read()
            show(evaluator.run_text(text), args.verbose)
        else:
            repl(evaluator)
    except (OSError, json.JSONDecodeError, QueryError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
