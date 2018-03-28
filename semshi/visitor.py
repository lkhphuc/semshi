from ast import (AST, AsyncFunctionDef, Attribute, ClassDef, DictComp, Eq,
                 FunctionDef, GeneratorExp, Global, Gt, GtE, Import,
                 ImportFrom, Lambda, ListComp, Load, Lt, LtE, Module, Name,
                 NameConstant, NotEq, Num, SetComp, Store, Str, Try, arg,
                 arguments, comprehension)
import io
from itertools import chain, count
from token import NAME, OP, tok_name
from tokenize import tokenize

from .node import Node
from .util import debug_time, logger


# Node types which introduce a new scope
BLOCKS = (Module, FunctionDef, AsyncFunctionDef, ClassDef, ListComp, DictComp,
          SetComp, GeneratorExp, Lambda)


def tokenize_lines(lines):
    return tokenize((bytes(line + '\n', 'utf-8') for line in lines).__next__)


def advance(tokens, s=None, type=NAME):
    """Advance token stream."""
    if s is None:
        return next(t for t in tokens if t.type == type)
    if isinstance(s, str):
        return next(t for t in tokens if t.type == type and t.string == s)
    else:
        return next(t for t in tokens if t.type == type and t.string in s)


@debug_time
def visitor(lines, symtable_root, ast_root):
    visitor = Visitor(lines, symtable_root)
    return visitor(ast_root)


class Visitor:
    """The visitor visits the AST recursively to extract relevant name nodes in
    their context.
    """

    def __init__(self, lines, root_table):
        self._lines = lines
        self._table_stack = [root_table]
        self._env = []
        # Holds a copy of the current environment to avoid multiple copies
        self._cur_env = None
        self.names = []

    def __call__(self, root_node):
        self._visit(root_node)
        return self.names

    def _visit(self, node):
        """Recursively visit the node to build a list of names in their scopes.

        In some contexts, nodes appear in a different order than the scopes are
        nested. In that case attributes of a node might be visitied before
        creating a new scope and deleted afterwards so they are not revisited
        later.
        """
        # Use type() because it's faster than the more idiomatic isinstance()
        type_ = type(node)
        if type_ is Name:
            self._new_name(node)
            return
        elif type_ is Attribute:
            self._add_attribute(node)
            self._visit(node.value)
            return
        elif type_ in (NameConstant, Str, Num, Store, Load, Eq, Lt, Gt, NotEq,
                       LtE, GtE):
            return
        elif type_ is Try:
            self._visit_try(node)
        elif type_ in (Import, ImportFrom):
            self._visit_import(node)
        elif type_ is arg:
            self._visit_arg(node)
        elif type_ in (AsyncFunctionDef, FunctionDef, Lambda):
            self._visit_arg_defaults(node)
        elif type_ in (DictComp, SetComp, ListComp, GeneratorExp):
            self._visit_comp(node)
        elif type_ is ClassDef:
            self._visit_class_meta(node)
        if type_ in (AsyncFunctionDef, FunctionDef):
            self._visit_args(node)
        if type_ in (AsyncFunctionDef, ClassDef, FunctionDef):
            self._visit_class_function_definition(node)
        # Either make a new block scope...
        if type_ in BLOCKS:
            self._visit_block(node)
        # ...or just iterate through node attributes
        else:
            self._iter_node(node)

    def _new_name(self, node):
        # Using __dict__.get() is faster than getattr()
        target = node.__dict__.get('self_target')
        self.names.append(Node(node.id, node.lineno, node.col_offset,
                               self._cur_env, None, target))

    def _visit_arg(self, node):
        """Visit argument."""
        self.names.append(Node(node.arg, node.lineno, node.col_offset,
                               self._cur_env))

    def _visit_arg_defaults(self, node):
        """Visit argument default values."""
        for arg_ in node.args.defaults + node.args.kw_defaults:
            self._visit(arg_)
        del node.args.defaults
        del node.args.kw_defaults

    def _visit_block(self, node):
        """Visit block and create new scope."""
        current_table = self._table_stack.pop()
        self._table_stack += reversed(current_table.get_children())
        self._env.append(current_table)
        self._cur_env = self._env[:]
        self._iter_node(node)
        self._env.pop()
        self._cur_env = self._env[:]

    def _visit_try(self, node):
        """Visit try-except."""
        for child in node.body:
            self._visit(child)
        del node.body
        for child in node.orelse:
            self._visit(child)
        del node.orelse

    def _visit_comp(self, node):
        """Visit set/dict/list comprehension or generator expression."""
        generator = node.generators[0]
        self._visit(generator.iter)
        del generator.iter

    def _visit_class_meta(self, node):
        """Visit class bases and keywords."""
        for base in node.bases:
            self._visit(base)
        del node.bases
        for keyword in node.keywords:
            self._visit(keyword)
        del node.keywords

    def _visit_args(self, node):
        """Visit function arguments."""
        args = node.args
        for arg in args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
            if arg is None:
                continue
            self._visit(arg.annotation)
            del arg.annotation
        self._visit(node.returns)
        del node.returns
        self._mark_self(node)

    def _visit_import(self, node):
        """Visit import statement.

        Unlike other nodes in the AST, names in import statements don't come
        with a specified line number and column. Therefore, we need to use the
        tokenizer on that part of the code to get the exact position. Since
        using the tokenize module is slow, we only use it where absoultely
        necessary.
        """
        line_idx = node.lineno - 1
        lines = chain(
            # Let first line start at the col_offset of the import statement
            # in case multiple imports are chained in one line with semicolons.
            [node.col_offset * ' ' + self._lines[line_idx][node.col_offset:]],
            (self._lines[i] + '\n' for i in count(node.lineno)),
        )
        tokens = tokenize_lines(lines)
        # Advance to "import" keyword
        advance(tokens, 'import')
        for alias, remaining in zip(node.names, count(len(node.names)-1, -1)):
            if alias.name == '*':
                continue # TODO Handle star import
            # If it's an "as" alias import...
            if alias.asname is not None:
                # ...advance to "as" keyword.
                advance(tokens, 'as')
            token = advance(tokens)
            self.names.append(Node(
                token.string,
                token.start[0] + line_idx,
                token.start[1],
                self._cur_env,
            ))
            # If there are more imports in that import statement...
            if remaining:
                # ...they must be comma-separated, so advance to next comma.
                advance(tokens, ',', OP)

    def _visit_class_function_definition(self, node):
        """Visit class or function definition.

        The AST does not include the line and column of the names in class and
        function definitions, so we need to determine it using tokenize.
        """
        decorators = node.decorator_list
        for decorator in decorators:
            self._visit(decorator)
        del node.decorator_list
        line_idx = node.lineno - 1
        # Guess offset of the name (length of the keyword + 1)
        start = node.col_offset + (6 if type(node) is ClassDef else 4)
        stop = start + len(node.name)
        # If the node has no decorators and its name appears directly after the
        # definition keyword, we found its position and don't need to tokenize.
        if not decorators and self._lines[line_idx][start:stop] == node.name:
            lineno = node.lineno
            column = start
        else:
            tokens = tokenize_lines(self._lines[i] + '\n' for i in count(line_idx))
            advance(tokens, ('class', 'def'))
            token = advance(tokens)
            lineno = token.start[0] + line_idx
            column = token.start[1]
        self.names.append(Node(node.name, lineno, column, self._cur_env))

    def _mark_self(self, node):
        """Mark self/cls argument if the current function has one.

        Determine if an argument is a self argument (the first argument of a
        method called "self" or "cls") and add a reference in the function's
        symtable.
        """
        # The first argument...
        try:
            arg = node.args.args[0]
        except IndexError:
            return
        # ...with a special name...
        if arg.arg not in ['self', 'cls']:
            return
        # ...and a class as parent scope is a self_param.
        if not self._env[-1].get_type() == 'class':
            return
        # Let the table for the current function scope remember the param
        self._table_stack[-1].self_param = arg.arg

    def _add_attribute(self, node):
        """Add node as an attribute.

        The only interesting attributes are attributes to self or cls in a
        method (e.g. "self._name").
        """
        # TODO this doesn't check if we're inside a class
        # Only attributes of names matter. (foo.attr, but not [].attr)
        if type(node.value) is not Name:
            return
        if node.value.id not in ('self', 'cls'):
            return
        if node.value.id != getattr(self._env[-1], 'self_param', None):
            return
        new_node = Node(
            node.attr,
            node.value.lineno,
            node.value.col_offset + len(node.value.id) + 1,
            self._env[:-1],
            True,
        )
        node.value.self_target = new_node # TODO
        self.names.append(new_node)

    def _iter_node(self, node):
        """Iterate through fields of the node."""
        if node is None:
            return
        for field in node._fields:
            value = node.__dict__.get(field, None)
            if value is None:
                continue
            value_type = type(value)
            if value_type is list:
                for item in value:
                    if type(item) == str:
                        continue
                    self._visit(item)
            # We would want to use isinstance(value, AST) here. Not sure how
            # much more expensive that is, though.
            elif value_type not in (str, int, bytes):
                self._visit(value)