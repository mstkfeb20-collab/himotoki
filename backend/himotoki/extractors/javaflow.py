"""Where the values of a Java method go: a per-method summary for value-flow analysis.

A value is a set of *atoms* `base|path`:

* `p0`, `p1` … parameters; `v:name` locals (all assignments merged); `ret` the return
  value; `cK` the value of call K (resolved, or passed through, at link time); `nK` an
  object made here;
* `TH|field…` a field of this class; `T:<type>|field…` a field of an object of a declared
  type (field-based: every object of the type shares it, which is what lets a value
  cross layers without following every call);
* `S|key` a session / request attribute; `Q|name` a request parameter.

The summary lists flows (`src -> dst`), calls with the atoms of their receiver and
arguments, and *sinks*: SQL parameter markers bound to values (JDBC `ps.setXxx(n, v)`,
JdbcTemplate arguments, named parameters, iBATIS / MyBatis statement ids). Everything is
taken from the source text; nothing is executed. What cannot be told (a map key built at
run time, reflection) simply produces no flow.
"""
from __future__ import annotations

import re

_SQL_HEAD = re.compile(r"(?is)^\s*(select|insert|update|delete|merge|with|\{\s*call|call)\b")
_PS_SET = re.compile(r"^set(String|Int|Long|BigDecimal|Date|Timestamp|Object|Double|Float|Short|Bytes|Time|Boolean|"
                     r"NString|Byte|Clob|Blob|CharacterStream|AsciiStream|Array)$")
_JDBC_RUN = {"update", "batchUpdate", "query", "queryForObject", "queryForList", "queryForMap", "queryForInt",
             "queryForLong", "queryForRowSet", "queryForStream", "execute"}
_STMT_RUN = {"insert", "update", "delete", "queryForObject", "queryForList", "queryForMap", "queryForPaginatedList",
             "selectOne", "selectList", "selectMap", "selectCursor", "queryWithRowHandler"}
_ADD = {"add", "addAll", "offer", "push", "addElement", "addFirst", "addLast", "set"}
_PUT = {"put", "addValue", "setAttribute", "setParameter", "putValue", "setProperty"}
_GETKEY = {"get", "getAttribute", "getValue", "getProperty", "getString", "getOrDefault", "remove"}
_COPY = {"copyProperties", "copy"}
MAX_PATH = 4
_PLAIN = re.compile(r"^(String|Object|Integer|Long|Short|Byte|Double|Float|Boolean|Character|BigDecimal|BigInteger|"
                    r"Date|Timestamp|Time|Calendar|StringBuffer|StringBuilder|Connection|PreparedStatement|"
                    r"CallableStatement|ResultSet|Statement|HttpServletRequest|HttpServletResponse|HttpSession|"
                    r"ActionForm|ActionMapping|ActionForward|ActionErrors|ActionMessages|Exception|Throwable|"
                    r"(Array|Linked)?List|(Hash|Tree|Linked)?Map|(Hash|Tree)?Set|Collection|Iterator|Vector|"
                    r"Hashtable|Properties|Model|ModelMap|BindingResult|Locale|Logger|Log)\b")


def _t(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _ext(atom: str, prop: str) -> str:
    base, _, path = atom.partition("|")
    parts = [p for p in path.split(".") if p] + [prop]
    return base + "|" + ".".join(parts[:MAX_PATH])


def _key(v: str | None) -> str | None:
    """A map / attribute key usable as a path element: a literal, or a constant resolved at
    link time (SklConst.KEY_SOKO_CD -> `@C@SklConst@KEY_SOKO_CD`)."""
    if not v:
        return None
    m = re.fullmatch(r"\{\{C:([\w.$]+)\}\}", v)
    if m:
        return "@C@" + m.group(1).replace(".", "@")
    if "{{" in v or not re.fullmatch(r"[\w\-]+", v):
        return None          # a key built at run time cannot be followed
    return v


def _prop(name: str, prefix: str) -> str:
    s = name[len(prefix):]
    return s[:1].lower() + s[1:] if s else ""


class MethodFlow:
    def __init__(self, params: list, cls_consts: dict, imports: list, field_names: set):
        self.pnames = {pn: i for i, (_, pn) in enumerate(params)}
        self.vtype: dict[str, str] = {pn: pt for pt, pn in params}
        self.locals: set[str] = set()
        self.consts = cls_consts
        self.fields = field_names
        self.spring_copy = any(i.startswith("org.springframework") and i.endswith("BeanUtils") for i in imports)
        self.flows: list = []
        self.calls: list = []
        self.sinks: list = []
        self.strs: dict[str, list[str]] = {}    # local -> SQL text pieces in source order
        self.stmts: dict[str, str] = {}         # PreparedStatement local -> SQL
        self.lists: dict[str, list] = {}        # list local -> atoms added, in order
        self.seq = 0

    # ------------------------------------------------------------ helpers
    def flow(self, src: set, dst: set, line: int):
        for s in src:
            for d in dst:
                if s != d and len(self.flows) < 4000:
                    self.flows.append([s, d, line])

    def var_atom(self, name: str) -> str | None:
        if name in self.pnames:
            return f"p{self.pnames[name]}|"
        if name in self.locals or name in self.vtype:
            return f"v:{name}|"
        if name in self.fields:
            return f"TH|{name}"
        return None

    def typeof(self, node) -> str | None:
        if node is None:
            return None
        t = node.type
        if t == "identifier":
            n = _t(node)
            if n in self.vtype:
                return self.vtype[n]
            return f"TF:{n}" if n in self.fields else None
        if t == "field_access":
            o = node.child_by_field_name("object")
            if o is not None and o.type == "this":
                return f"TF:{_t(node.child_by_field_name('field'))}"
        if t == "cast_expression":
            return _t(node.child_by_field_name("type"))
        if t == "parenthesized_expression" and node.named_children:
            return self.typeof(node.named_children[0])
        if t == "object_creation_expression":
            return _t(node.child_by_field_name("type"))
        return None

    def typed(self, node, prop: str) -> set:
        ty = self.typeof(node)
        if not ty or ty in ("String", "Object", "Map", "HashMap", "List", "ArrayList"):
            return set()
        ty = re.sub(r"<.*>", "", ty).strip()
        return {f"T:{ty}|{prop}"}

    def sval(self, node) -> str | None:
        """Text of a string expression as far as the source tells (unknown parts are dropped)."""
        if node is None:
            return None
        t = node.type
        if t == "string_literal":
            s = _t(node)
            if s.startswith('"""'):
                return s[3:-3]
            return bytes(s[1:-1], "utf-8").decode("unicode_escape", "ignore") if "\\" in s else s[1:-1]
        if t == "binary_expression" and _t(node.child_by_field_name("operator")) == "+":
            a, b = self.sval(node.child_by_field_name("left")), self.sval(node.child_by_field_name("right"))
            return (a or "") + (b or "") if a is not None or b is not None else None
        if t == "parenthesized_expression" and node.named_children:
            return self.sval(node.named_children[0])
        if t == "identifier":
            n = _t(node)
            if n in self.strs:
                return "".join(self.strs[n])
            if n in self.consts:
                return self.consts[n]
            if n not in self.vtype and re.fullmatch(r"[A-Z][A-Z0-9_]*", n):
                return "{{C:" + n + "}}"
            return None
        if t == "field_access":
            return "{{C:" + _t(node).replace(" ", "") + "}}"
        if t == "method_invocation":
            name = _t(node.child_by_field_name("name"))
            obj = node.child_by_field_name("object")
            if name == "toString" and obj is not None:
                return self.sval(self._chain_root(obj)) if obj.type == "method_invocation" else self.sval(obj)
        if t == "ternary_expression":
            return self.sval(node.child_by_field_name("consequence"))
        return None

    def _chain_root(self, node):
        while node is not None and node.type == "method_invocation" and node.child_by_field_name("object") is not None:
            node = node.child_by_field_name("object")
        return node

    def _str_append(self, name: str, piece: str | None):
        if piece is not None and name in self.strs:
            self.strs[name].append(piece)

    # ------------------------------------------------------------ statements
    def visit(self, node):
        if node is None:
            return
        t = node.type
        line = node.start_point[0] + 1
        if t in ("local_variable_declaration", "resource"):
            ty = _t(node.child_by_field_name("type"))
            decls = [d for d in node.named_children if d.type == "variable_declarator"] if t != "resource" else [node]
            for d in decls:
                nm = _t(d.child_by_field_name("name"))
                v = d.child_by_field_name("value")
                self.locals.add(nm)
                if ty:
                    self.vtype[nm] = ty
                self._assign_local(nm, v, line)
            return
        if t == "expression_statement":
            for c in node.named_children:
                self.eval(c)
            return
        if t == "return_statement":
            for c in node.named_children:
                self.flow(self.eval(c), {"ret|"}, line)
            return
        if t == "enhanced_for_statement":
            nm = _t(node.child_by_field_name("name"))
            self.locals.add(nm)
            self.vtype[nm] = _t(node.child_by_field_name("type"))
            self.flow(self.eval(node.child_by_field_name("value")), {f"v:{nm}|"}, line)
            self.visit(node.child_by_field_name("body"))
            return
        if t in ("class_declaration", "interface_declaration", "enum_declaration"):
            return
        for c in node.named_children:
            if c.type.endswith("_statement") or c.type in ("block", "local_variable_declaration", "switch_block",
                                                         "switch_block_statement_group", "switch_rule",
                                                         "catch_clause", "finally_clause", "resource_specification",
                                                         "resource", "expression_statement", "else"):
                self.visit(c)
            elif c.type.endswith("expression") or c.type in ("method_invocation", "parenthesized_expression",
                                                            "condition", "assignment_expression"):
                self.eval(c)
            else:
                self.visit(c)

    def _assign_local(self, nm: str, v, line: int):
        if v is None:
            return
        if v.type == "method_invocation" and _t(v.child_by_field_name("name")) in ("prepareStatement", "prepareCall"):
            args = v.child_by_field_name("arguments")
            if args is not None and args.named_children:
                self.stmts[nm] = self.sval(args.named_children[0]) or ""
            self.eval(v)
            return
        ty = self.vtype.get(nm, "")
        if ty in ("String", "StringBuffer", "StringBuilder") or nm in self.strs:
            if v.type == "object_creation_expression" and "String" in _t(v.child_by_field_name("type")):
                args = v.child_by_field_name("arguments")
                first = args.named_children[0] if args is not None and args.named_children else None
                s = self.sval(first) if first is not None and first.type != "decimal_integer_literal" else ""
                self.strs[nm] = [s or ""]
            else:
                s = self.sval(v)
                if s is not None:
                    self.strs[nm] = [s]
        if ty.startswith(("List", "ArrayList", "LinkedList", "Collection", "Vector")) and nm not in self.lists:
            self.lists[nm] = []
        self.flow(self.eval(v), {f"v:{nm}|"}, line)

    # ------------------------------------------------------------ expressions
    def eval(self, node) -> set:
        if node is None:
            return set()
        t = node.type
        line = node.start_point[0] + 1
        if t == "identifier":
            a = self.var_atom(_t(node))
            out = {a} if a else set()
            ty = self.vtype.get(_t(node)) if a and not a.startswith("TH|") else None
            if ty and ty[:1].isupper() and not _PLAIN.match(ty):
                out.add(f"T:{re.sub(r'<.*>', '', ty).strip()}|")      # the object, field by field
            return out
        if t == "this":
            return {"TH|"}
        if t == "field_access":
            o = node.child_by_field_name("object")
            fld = _t(node.child_by_field_name("field"))
            if o is not None and o.type == "this":
                return {f"TH|{fld}"}
            return {_ext(a, fld) for a in self.eval(o)} | self.typed(o, fld)
        if t in ("parenthesized_expression", "cast_expression"):
            v = node.child_by_field_name("value") if t == "cast_expression" else (node.named_children[0] if node.named_children else None)
            return self.eval(v)
        if t == "binary_expression":
            op = _t(node.child_by_field_name("operator"))
            a, b = self.eval(node.child_by_field_name("left")), self.eval(node.child_by_field_name("right"))
            return a | b if op == "+" else set()
        if t == "ternary_expression":
            self.eval(node.child_by_field_name("condition"))
            return self.eval(node.child_by_field_name("consequence")) | self.eval(node.child_by_field_name("alternative"))
        if t == "assignment_expression":
            return self._assign(node, line)
        if t == "update_expression":
            return set()
        if t == "array_access":
            self.eval(node.child_by_field_name("index"))
            return self.eval(node.child_by_field_name("array"))
        if t == "array_creation_expression":
            out = set()
            for c in node.named_children:
                if c.type == "array_initializer":
                    for x in c.named_children:
                        out |= self.eval(x)
            return out
        if t == "array_initializer":
            out = set()
            for x in node.named_children:
                out |= self.eval(x)
            return out
        if t == "method_invocation":
            return self._call(node, line)
        if t == "object_creation_expression":
            return self._new(node, line)
        if t in ("lambda_expression",):
            body = node.child_by_field_name("body")
            if body is not None:
                (self.visit if body.type == "block" else self.eval)(body)
            return set()
        if t in ("instanceof_expression", "unary_expression"):
            for c in node.named_children:
                self.eval(c)
            return set()
        return set()

    def _assign(self, node, line):
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        op = _t(node.child_by_field_name("operator"))
        if left is not None and left.type == "identifier":
            nm = _t(left)
            if op == "+=" and nm in self.strs:
                self._str_append(nm, self.sval(right) or "")
            elif op == "=" and (nm in self.strs or self.vtype.get(nm) in ("String", "StringBuffer", "StringBuilder")):
                s = self.sval(right)
                if s is not None and right.type == "binary_expression" and nm in self.strs and \
                        _t(right).startswith(nm):
                    self.strs[nm] = [s]          # sql = sql + "..." keeps what came before
                elif s is not None:
                    self.strs[nm] = [s]
            if right is not None and right.type == "method_invocation" and \
                    _t(right.child_by_field_name("name")) in ("prepareStatement", "prepareCall"):
                args = right.child_by_field_name("arguments")
                if args is not None and args.named_children:
                    self.stmts[nm] = self.sval(args.named_children[0]) or ""
        val = self.eval(right)
        if left is None:
            return val
        if left.type == "identifier":
            a = self.var_atom(_t(left))
            if a is None:
                self.locals.add(_t(left))
                a = f"v:{_t(left)}|"
            self.flow(val, {a}, line)
        elif left.type == "field_access":
            o = left.child_by_field_name("object")
            fld = _t(left.child_by_field_name("field"))
            dst = {f"TH|{fld}"} if o is not None and o.type == "this" else \
                {_ext(a, fld) for a in self.eval(o)} | self.typed(o, fld)
            self.flow(val, dst, line)
        elif left.type == "array_access":
            self.flow(val, self.eval(left.child_by_field_name("array")), line)
        return val

    def _args(self, node):
        a = node.child_by_field_name("arguments")
        return list(a.named_children) if a is not None else []

    def _stringish(self, node) -> bool:
        t = node.type
        if t in ("string_literal", "binary_expression") or t == "field_access":
            return True
        if t == "identifier":
            ty = self.vtype.get(_t(node))
            return ty == "String" or (ty is None and bool(re.fullmatch(r"[A-Z][A-Z0-9_]+", _t(node))))
        if t == "method_invocation":
            return _t(node.child_by_field_name("name")) in ("toString", "concat", "trim", "format", "valueOf")
        return False

    def _receiver_kind(self, obj) -> str:
        nm = _t(obj).lower() if obj is not None else ""
        ty = (self.typeof(obj) or "").lower()
        if "session" in nm or "session" in ty:
            return "session"
        if nm in ("request", "req", "httprequest", "servletrequest") or "servletrequest" in ty:
            return "request"
        if "preparedstatement" in ty or "callablestatement" in ty or re.fullmatch(r"p?stmt\w*|ps\w*|cs\w*|pstm\w*", nm):
            return "ps"
        if "jdbc" in nm or "jdbctemplate" in ty or "template" in nm and "sql" not in nm:
            return "jdbc"
        if "sqlmap" in nm or "sqlmap" in ty or "sqlsession" in nm or "sqlsession" in ty or "sqlmapclient" in ty:
            return "sqlmap"
        return ""

    def _call(self, node, line) -> set:
        name = _t(node.child_by_field_name("name"))
        obj = node.child_by_field_name("object")
        args = self._args(node)
        rk = self._receiver_kind(obj)
        # StringBuffer / StringBuilder appends build SQL text in source order
        if name == "append" and obj is not None:
            root = self._chain_root(obj)
            if root is not None and root.type == "identifier" and self.var_atom(_t(root)):
                if obj.type == "method_invocation":
                    self._call(obj, line)
                self._str_append(_t(root), self.sval(args[0]) if args else None)
                buf = self.var_atom(_t(root))
                for a in args:
                    self.flow(self.eval(a), {buf}, line)
                return {buf}                      # append returns the buffer itself
        recv = self.eval(obj) if obj is not None else set()
        argv = [self.eval(a) for a in args]
        key = _key(self.sval(args[0])) if args else None

        # request parameters, session / request attributes
        if name in ("getParameter", "getParameterValues") and key:
            return {f"Q|{key}"}
        if name == "getAttribute" and key and rk in ("session", "request"):
            return {f"S|{key}"}
        if name == "setAttribute" and key and len(argv) == 2 and rk in ("session", "request"):
            self.flow(argv[1], {f"S|{key}"}, line)
            return set()
        # JDBC PreparedStatement parameters
        if rk == "ps" and _PS_SET.match(name) and len(args) >= 2:
            idx_node = args[0]
            it = _t(idx_node)
            if re.fullmatch(r"\d+", it):
                idx = int(it)
            elif "++" in it or "--" in it:
                idx = "seq"
            else:
                lst = next((n for n in self.lists if re.search(rf"\b{re.escape(n)}\b", _t(args[1]))), None)
                idx = f"list:{lst}" if lst else "seq"
            self.seq += 1
            self.sinks.append({"k": "pos", "stmt": _t(obj), "sql": self.stmts.get(_t(obj)), "idx": idx,
                               "v": sorted(argv[1]), "l": line, "o": self.seq})
            return set()
        # JdbcTemplate / NamedParameterJdbcTemplate
        if name in _JDBC_RUN and args:
            sql = self.sval(args[0])
            if sql and (_SQL_HEAD.match(sql) or "{{C:" in sql):
                rest = args[1:]
                if re.search(r"(?<![:\w]):[A-Za-z_]\w*", sql):
                    for a, v in zip(rest, argv[1:]):
                        if v:
                            self.sinks.append({"k": "named", "sql": sql, "v": sorted(v), "l": line})
                    return {f"c{self._record(node, obj, name, args, recv, argv, line)}|"}
                vals = []
                for a, v in zip(rest, argv[1:]):
                    tt = _t(a)
                    if a.type == "array_creation_expression":
                        init = next((c for c in a.named_children if c.type == "array_initializer"), None)
                        for x in (init.named_children if init is not None else []):
                            vals.append(sorted(self.eval(x)))
                    elif re.search(r"\.toArray\(", tt) and a.type == "method_invocation":
                        lst = _t(self._chain_root(a))
                        if lst in self.lists:
                            vals.append(f"list:{lst}")
                    elif a.type in ("object_creation_expression", "lambda_expression", "class_literal",
                                    "method_reference", "this") or re.search(r"(?i)mapper|rowcallback|extractor|\.class$", tt):
                        continue
                    elif a.type == "identifier" and _t(a) in self.lists:
                        vals.append(f"list:{_t(a)}")
                    else:
                        vals.append(sorted(v))
                self.sinks.append({"k": "jdbc", "sql": sql, "vals": vals, "l": line})
                return {f"c{self._record(node, obj, name, args, recv, argv, line)}|"}
        # iBATIS / MyBatis statement ids: sqlMapClient.insert("Order.insert", dto)
        if name in _STMT_RUN and args and len(args) >= 2 and args[0].type == "string_literal" and \
                (rk == "sqlmap" or re.search(r"(?i)sqlmap|sqlsession|getsqlmap|sqlclient|session\b", _t(obj) if obj is not None else "")):
            sid = self.sval(args[0])
            if sid and re.fullmatch(r"[\w.$]+", sid):
                self.sinks.append({"k": "stmt", "id": sid, "v": sorted(argv[1]), "l": line})
                return set()
        if name in _STMT_RUN and len(args) == 1 and args[0].type == "string_literal" and rk == "sqlmap":
            return set()
        # bean copies
        if name in _COPY and len(argv) >= 2 and (obj is None or re.search(r"(?i)bean|propert|copier|util", _t(obj))):
            src, dst = (argv[0], argv[1]) if self.spring_copy or "spring" in _t(obj).lower() else (argv[1], argv[0])
            sn, dn = (args[0], args[1]) if self.spring_copy else (args[1], args[0])
            self.flow(src, dst, line)
            st, dt = self.typeof(sn), self.typeof(dn)
            if st and dt and not st.startswith("TF:") and not dt.startswith("TF:"):
                self.flow({f"T:{re.sub(r'<.*>', '', st)}|"}, {f"T:{re.sub(r'<.*>', '', dt)}|"}, line)
            return set()
        # PropertyUtils.getSimpleProperty(bean, "x") / setProperty(bean, "x", v)
        if name in ("getProperty", "getSimpleProperty", "getNestedProperty") and len(args) == 2 and obj is not None \
                and re.search(r"(?i)util", _t(obj)):
            k2 = _key(self.sval(args[1]))
            return {_ext(a, k2) for a in argv[0]} if k2 else set()
        if name in ("setProperty", "setSimpleProperty", "setNestedProperty") and len(args) == 3 and obj is not None \
                and re.search(r"(?i)util", _t(obj)):
            k2 = _key(self.sval(args[1]))
            if k2:
                self.flow(argv[2], {_ext(a, k2) for a in argv[0]}, line)
            return set()
        # maps and attribute bags with a constant key
        if name in _PUT and len(argv) == 2 and key is not None and obj is not None:
            self.flow(argv[1], {_ext(a, key) for a in recv}, line)
            return recv
        if name in _GETKEY and key is not None and len(argv) >= 1 and obj is not None and rk not in ("ps",):
            return {_ext(a, key) for a in recv}
        # collections
        if name in _ADD and argv and obj is not None:
            self.flow(argv[-1], recv, line)
            if obj.type == "identifier" and _t(obj) in self.lists:
                self.lists[_t(obj)].append(sorted(argv[-1]))
            return set()
        if name in ("get", "getString", "getAttribute", "remove") and args and key is None and obj is not None \
                and self._stringish(args[0]):
            return set()                       # a key known only at run time: not followed
        if name in ("get", "next", "iterator", "elementAt", "getFirst", "getLast", "poll", "peek", "pop",
                    "values", "toArray", "subList") and obj is not None:
            return recv
        # accessors
        if re.match(r"(get|is)[A-Z]", name) and not args:
            prop = _prop(name, "get" if name.startswith("get") else "is")
            if obj is None:
                return {f"TH|{prop}"}
            return {_ext(a, prop) for a in recv} | self.typed(obj, prop)
        if re.match(r"set[A-Z]", name) and len(args) == 1:
            prop = _prop(name, "set")
            dst = {f"TH|{prop}"} if obj is None else ({_ext(a, prop) for a in recv} | self.typed(obj, prop))
            self.flow(argv[0], dst, line)
            return set()
        return {f"c{self._record(node, obj, name, args, recv, argv, line)}|"}

    def _record(self, node, obj, name, args, recv, argv, line, new=None) -> int:
        from .java import _desc
        k = len(self.calls)
        c = {"k": k, "r": _desc(obj) if obj is not None else None, "n": name, "a": len(args),
             "args": [sorted(v) for v in argv], "recv": sorted(recv), "l": line}
        if new:
            c["new"] = new
        self.calls.append(c)
        return k

    def _new(self, node, line) -> set:
        ty = _t(node.child_by_field_name("type"))
        args = self._args(node)
        argv = [self.eval(a) for a in args]
        body = next((c for c in node.named_children if c.type == "class_body"), None)
        if body is not None:
            # anonymous classes (PreparedStatementSetter, callbacks): their bodies run here
            for m in body.named_children:
                if m.type == "method_declaration":
                    fp = m.child_by_field_name("parameters")
                    for p in (fp.named_children if fp is not None else []):
                        pn = p.child_by_field_name("name")
                        if pn is not None:
                            self.locals.add(_t(pn))
                            self.vtype[_t(pn)] = _t(p.child_by_field_name("type"))
                    self.visit(m.child_by_field_name("body"))
            return set()
        if "ParameterSource" in ty:
            k = len(self.calls)
            base = f"n{k}|"
            self.calls.append({"k": k, "n": "<new>", "a": len(args), "args": [], "recv": [], "l": line})
            if "Bean" in ty and argv:
                self.flow(argv[0], {base}, line)
            elif len(args) == 2:
                key = self.sval(args[0])
                if key:
                    self.flow(argv[1], {_ext(base, key)}, line)
            elif len(args) == 1:
                self.flow(argv[0], {base}, line)
            return {base}
        return {f"c{self._record(node, None, '<init>', args, set(), argv, line, new=ty)}|"}


def method_flow(body, params: list, cls_consts: dict, imports: list, field_names: set) -> dict | None:
    if body is None:
        return None
    mf = MethodFlow(params, cls_consts, imports, field_names)
    mf.visit(body)
    if not (mf.flows or mf.calls or mf.sinks):
        return None
    lists = {k: v for k, v in mf.lists.items() if v}
    out = {"f": mf.flows, "c": mf.calls, "s": mf.sinks}
    if lists:
        out["lists"] = lists
    return out
