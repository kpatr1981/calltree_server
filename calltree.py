#!/usr/bin/env python3
"""Static call tree for Bash scripts.

Follows `source`/`.` includes, finds function definitions and prints the
functions each one calls, in execution order, with file:line locations and
the control-flow context each call sits in ([then], [case backup], [||], ...).

Examples (run from examples/backup):
  calltree.py backup.sh                              # whole tree from top level
  calltree.py backup.sh -e main                      # start at one function
  calltree.py backup.sh -e main -m prune_old,delete_archive
  calltree.py backup.sh --to delete_archive          # only paths reaching it
  calltree.py backup.sh --callers die                # who calls it
  calltree.py backup.sh --list                       # function index
  calltree.py backup.sh --names                      # function names and count
  calltree.py backup.sh -I '^(log|warn|die)$'        # leave out matching names
  calltree.py backup.sh --var NOTIFIER=plugins/notify-mail.sh   # value for `source "$NOTIFIER"`

Exports (write files instead of printing the tree; the filters above apply):
  calltree.py backup.sh -e main --graph calls.svg     # also .png .pdf .dot
  calltree.py backup.sh --yaml calls.yaml --json calls.json
  calltree.py backup.sh --to delete_archive --subset paths.sh
  calltree.py backup.sh -e main -d 2 --animate run.gif
Without -e or -t an export covers the whole program. --animate steps through
the calls in the order they run, one per --delay seconds: the current call in
red, calls already made in blue. Images and GIFs need Graphviz `dot`.

Reading the tree:
  :170           call site, in the same file as the enclosing function
  -> file:N      where the called function is defined
  (via run_step) function name passed as an argument to another function
  [then] [||]    control flow around the call; a trailing ? marks a condition
  ^ see :N       already expanded at that call site (use --full to repeat)
This is a static read: it does not evaluate conditions or variables.
"""
from __future__ import annotations

import argparse
import difflib
import glob
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

META = set(' \t\r\n;&|()<>')
OPS = ['&>>', ';;&', '<<<', '<<-', '&&', '||', ';;', ';&', '|&', '&>', '>>',
       '>|', '>&', '<&', '<>', '<<', ';', '&', '|', '<', '>']
REDIRS = {'&>>', '<<<', '&>', '>>', '>|', '>&', '<&', '<>', '<', '>'}
RESERVED = {'if', 'then', 'elif', 'else', 'fi', 'do', 'done', 'while', 'until',
            'for', 'select', 'case', 'esac', 'in', 'function', '{', '}', '!',
            '[[', ']]', 'time', 'coproc'}
PREFIX_COMMANDS = {'command', 'builtin', 'exec', 'nohup', 'time'}
ASSIGNMENT = re.compile(r'[A-Za-z_]\w*(\[[^]]*\])?\+?=')
FUNC_NAME = re.compile(r'[A-Za-z_][\w:.@-]*')


@dataclass
class Tok:
    kind: str  # WORD OP NL REDIR HEREDOC SOPEN SCLOSE ARITH
    val: str
    line: int
    lit: str | None = None  # value without quotes; None when it expands anything


class Lexer:
    def __init__(self, text: str, line: int = 1):
        self.s, self.i, self.line = text, 0, line
        self.out: list[Tok] = []
        self.heredocs: list[tuple[str, bool]] = []

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < len(self.s) else ''

    def emit(self, kind, val, line=None, lit=None):
        self.out.append(Tok(kind, val, self.line if line is None else line, lit))

    def run(self) -> list[Tok]:
        self.lex(None)
        return self.out

    def lex(self, stop):
        s, depth = self.s, 0
        while self.i < len(s):
            c = s[self.i]
            if c == '\n':
                self.emit('NL', c)
                self.i += 1
                self.line += 1
                self.read_heredocs()
            elif c in ' \t\r':
                self.i += 1
            elif c == '\\' and self.peek(1) == '\n':
                self.i += 2
                self.line += 1
            elif c == '#':
                while self.i < len(s) and s[self.i] != '\n':
                    self.i += 1
            elif c == ')' and stop == ')' and depth == 0:
                self.i += 1
                return
            elif c == '(' and self.peek(1) == '(':
                line = self.line
                self.emit('ARITH', self.scan_balanced('(', ')'), line)
            elif c == '(':
                depth += 1
                self.emit('OP', c)
                self.i += 1
            elif c == ')':
                depth -= 1
                self.emit('OP', c)
                self.i += 1
            elif c in '<>' and self.peek(1) == '(':
                self.i += 1
                self.subst_paren()
                self.emit('WORD', c + '(…)')
            elif not self.match_op():
                self.read_word()

    def match_op(self) -> bool:
        for op in OPS:
            if self.s.startswith(op, self.i):
                self.i += len(op)
                if op in ('<<', '<<-'):
                    while self.peek() in (' ', '\t'):
                        self.i += 1
                    raw, _ = self.read_word(emit=False)
                    self.heredocs.append((re.sub(r'[\'"\\]', '', raw), op == '<<-'))
                    self.emit('HEREDOC', op)
                else:
                    self.emit('REDIR' if op in REDIRS else 'OP', op)
                return True
        return False

    def read_heredocs(self):
        for delim, strip_tabs in self.heredocs:
            while self.i < len(self.s):
                j = self.s.find('\n', self.i)
                j = len(self.s) if j < 0 else j
                text = self.s[self.i:j]
                self.i = j + 1
                self.line += 1
                if (text.lstrip('\t') if strip_tabs else text) == delim:
                    break
        self.heredocs = []

    def scan_balanced(self, open_, close) -> str:
        s, start, depth = self.s, self.i, 0
        while self.i < len(s):
            c = s[self.i]
            if c == '\\':
                self.line += s[self.i:self.i + 2].count('\n')
                self.i += 2
                continue
            if c in '\'"':
                j = self.i + 1
                while j < len(s) and s[j] != c:
                    j += 2 if s[j] == '\\' and c == '"' else 1
                self.line += s[self.i:j].count('\n')
                self.i = j + 1
                continue
            if c == '\n':
                self.line += 1
            elif c == open_:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    break
            self.i += 1
        return s[start:self.i]

    def subst_paren(self):
        # self.i is at the '(' of $( or <( ; the inner text is lexed as commands.
        self.i += 1
        self.emit('SOPEN', '$(')
        self.lex(')')
        self.emit('SCLOSE', ')')

    def brace_param(self):
        s, depth = self.s, 1
        self.i += 1
        while self.i < len(s):
            c = s[self.i]
            if c == '\\':
                self.i += 2
                continue
            if c in '$`' and self.dollar():
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return
            elif c == '\n':
                self.line += 1
            self.i += 1

    def dollar(self) -> bool:
        """Consume an expansion at self.i. False means a literal '$'."""
        s, c, n = self.s, self.s[self.i], self.peek(1)
        if c == '`':
            j = self.i + 1
            while j < len(s) and s[j] != '`':
                j += 2 if s[j] == '\\' else 1
            inner = s[self.i + 1:j]
            self.emit('SOPEN', '`')
            self.out.extend(Lexer(inner, self.line).run())
            self.emit('SCLOSE', '`')
            self.line += inner.count('\n')
            self.i = j + 1
            return True
        if n == '(':
            self.i += 1
            if self.peek(1) == '(':
                self.scan_balanced('(', ')')
            else:
                self.subst_paren()
            return True
        if n == '{':
            self.i += 1
            self.brace_param()
            return True
        if n == "'":
            j = self.i + 2
            while j < len(s) and s[j] != "'":
                j += 2 if s[j] == '\\' else 1
            self.line += s[self.i:j].count('\n')
            self.i = j + 1
            return True
        if n and (n.isalnum() or n in '_@*#?$!-'):
            self.i += 2
            if n.isalpha() or n == '_':
                while self.i < len(s) and (s[self.i].isalnum() or s[self.i] == '_'):
                    self.i += 1
            return True
        return False

    def read_word(self, emit=True):
        s, start, line = self.s, self.i, self.line
        literal, buf = True, []
        while self.i < len(s):
            c = s[self.i]
            if c in META:
                # name=( array ) and extglob @( ... ) stay part of the word.
                if c == '(' and (ASSIGNMENT.fullmatch(s[start:self.i]) or
                                 (self.i > start and s[self.i - 1] in '@!+*?')):
                    self.scan_balanced('(', ')')
                    literal = False
                    continue
                break
            if c == '\\':
                if self.peek(1) == '\n':
                    self.line += 1
                else:
                    buf.append(self.peek(1))
                self.i += 2
            elif c == "'":
                j = s.find("'", self.i + 1)
                j = len(s) if j < 0 else j
                buf.append(s[self.i + 1:j])
                self.line += s[self.i:j].count('\n')
                self.i = j + 1
            elif c == '"':
                self.i += 1
                while self.i < len(s) and s[self.i] != '"':
                    ch = s[self.i]
                    if ch == '\\':
                        nxt = self.peek(1)
                        self.line += nxt == '\n'
                        buf.append(nxt if nxt in '$`"\\\n' else ch + nxt)
                        self.i += 2
                        continue
                    if ch in '$`' and self.dollar():
                        literal = False
                        continue
                    self.line += ch == '\n'
                    buf.append(ch)
                    self.i += 1
                self.i += 1
            elif c in '$`' and self.dollar():
                literal = False
            else:
                buf.append(c)
                self.i += 1
        raw = s[start:self.i]
        lit = ''.join(buf) if literal else None
        if emit:
            self.emit('WORD', raw, line, lit)
        return raw, lit


@dataclass
class Func:
    name: str
    file: str
    line: int
    end: int = 0
    calls: list = field(default_factory=list)


@dataclass
class Call:
    name: str | None  # None for dynamic commands such as "$@"
    file: str
    line: int
    words: list  # [(raw, lit)] arguments
    tags: list
    paths: list = field(default_factory=list)  # resolved files for source/.


class DB:
    def __init__(self, variables):
        self.funcs: dict[str, Func] = {}
        self.dupes: list[tuple[Func, Func]] = []
        self.tops: dict[str, Func] = {}
        self.vars = dict(variables)
        self.sources: list[tuple[Call, str]] = []
        self.warnings: list[str] = []

    def define(self, f: Func):
        if f.name in self.funcs:
            self.dupes.append((self.funcs[f.name], f))
        self.funcs[f.name] = f


class Parser:
    def __init__(self, db: DB, file: str, toks: list[Tok], owner: Func, base_tags=()):
        self.db, self.file, self.toks, self.k = db, file, toks, 0
        self.stack = [{'t': 'func', 'f': owner, 'open': None, 'root': True}]
        self.base = list(base_tags)
        self.cmd_pos, self.chain, self.cmd = True, None, None
        self.continued = False  # last token was && || | so a newline continues
        self.saved, self.subst = [], 0
        self.expect_target, self.cond = False, False

    def run(self):
        while self.k < len(self.toks):
            t = self.toks[self.k]
            self.k += 1
            continued = self.continued
            self.continued = t.kind == 'OP' and t.val in ('&&', '||', '|', '|&')
            if t.kind == 'NL' and continued:
                self.continued = True
                continue
            getattr(self, 'on_' + t.kind)(t)
        self.end_cmd()

    # -- helpers ---------------------------------------------------------
    def owner(self) -> Func:
        return next(e['f'] for e in reversed(self.stack) if e['t'] == 'func')

    def top(self):
        return self.stack[-1]

    def in_case(self, *phases):
        return self.top()['t'] == 'case' and self.top()['phase'] in phases

    def innermost(self, kind):
        """Innermost open block of this kind, with everything opened inside it
        dropped.  Matched by position: two open ifs can be equal dicts."""
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]['t'] == kind:
                del self.stack[i + 1:]
                return self.stack[i]
        return None

    def pop_to(self, kinds):
        while len(self.stack) > 1:
            e = self.stack.pop()
            if e['t'] in kinds:
                return e
        return None

    def tags(self):
        out = []
        for e in reversed(self.stack):
            if e['t'] == 'func':
                break
            if e['t'] == 'if':
                out.append(e['phase'])
            elif e['t'] == 'case':
                out.append('case ' + e['pat'])
            elif e['t'] == 'loop':
                out.append(e['kw'])
        out.reverse()
        if self.subst:
            out.append('$()')
        if self.chain:
            out.append(self.chain)
        return self.base + out

    def new_line(self, chain=None):
        self.end_cmd()
        self.cmd_pos, self.chain = True, chain

    # -- tokens ----------------------------------------------------------
    def on_NL(self, t):
        if not self.cond and not self.in_case('subject', 'pattern'):
            self.new_line()

    def on_HEREDOC(self, t):
        pass

    def on_REDIR(self, t):
        if not self.cond:
            self.expect_target = True

    def on_ARITH(self, t):
        self.cmd_pos = False

    def on_SOPEN(self, t):
        self.saved.append((self.cmd, self.cmd_pos, self.chain, self.expect_target,
                           self.cond, len(self.stack)))
        self.cmd, self.cmd_pos, self.chain = None, True, None
        self.expect_target, self.cond = False, False
        self.subst += 1

    def on_SCLOSE(self, t):
        self.end_cmd()
        self.subst -= 1
        (self.cmd, self.cmd_pos, self.chain, self.expect_target,
         self.cond, depth) = self.saved.pop()
        del self.stack[depth:]

    def on_OP(self, t):
        v = t.val
        if self.cond:
            return
        if self.in_case('subject', 'pattern'):
            if v == ')' and self.top()['phase'] == 'pattern':
                self.top()['phase'] = 'body'
                self.cmd_pos, self.chain = True, None
            return
        if v in (';', '&', '|', '|&', '&&', '||'):
            self.end_cmd(background=v == '&')
            self.cmd_pos = True
            if v in ('&&', '||'):
                self.chain = v
            elif v in (';', '&'):
                self.chain = None
        elif v in (';;', ';&', ';;&'):
            self.end_cmd()
            e = self.innermost('case')
            if e:
                e['phase'], e['pat'] = 'pattern', ''
            self.cmd_pos, self.chain = False, None
        elif v == '(':
            if self.cmd_pos:
                self.stack.append({'t': 'sub'})
        elif v == ')':
            self.end_cmd()
            top = self.top()
            if top['t'] == 'sub' or (top['t'] == 'func' and top['open'] == '('):
                self.close_func(self.stack.pop(), t.line)
            self.cmd_pos = False

    def close_func(self, e, line):
        if e and e['t'] == 'func':
            e['f'].end = line

    def on_WORD(self, t):
        if self.expect_target:
            self.expect_target = False
            return
        if self.cond:
            if t.val == ']]' and t.lit == ']]':
                self.cond = False
            elif self.cmd:
                self.cmd['words'].append((t.val, t.lit))
            return
        top = self.top()
        if top['t'] == 'case' and top['phase'] == 'subject':
            if t.val == 'in':
                top['phase'] = 'pattern'
            return
        if top['t'] == 'case' and top['phase'] == 'pattern':
            if t.val == 'esac':
                self.stack.pop()
                self.cmd_pos = False
            else:
                top['pat'] += ('|' if top['pat'] else '') + t.val
            return
        if not self.cmd_pos:
            if self.cmd is not None:
                self.cmd['words'].append((t.val, t.lit))
            return
        if t.lit == t.val and t.val in RESERVED:
            return self.reserved(t)
        if ASSIGNMENT.match(t.val):
            self.assignment(t.val)
            return
        nxt = self.toks[self.k:self.k + 2]
        if (t.lit and FUNC_NAME.fullmatch(t.lit) and len(nxt) == 2 and
                [x.val for x in nxt] == ['(', ')'] and all(x.kind == 'OP' for x in nxt)):
            self.k += 2
            return self.define(t.lit, t.line)
        self.cmd = {'name': t.lit, 'line': t.line, 'words': [], 'tags': self.tags()}
        self.cmd_pos = False

    def reserved(self, t):
        v = t.val
        if v == 'if':
            self.stack.append({'t': 'if', 'phase': 'if?'})
        elif v in ('then', 'elif', 'else'):
            e = self.innermost('if')
            if e:
                e['phase'] = {'then': 'then', 'elif': 'elif?', 'else': 'else'}[v]
        elif v == 'fi':
            self.end_cmd()
            self.pop_to({'if'})
            self.cmd_pos = False
            return
        elif v in ('while', 'until'):
            self.stack.append({'t': 'loop', 'kw': v})
        elif v in ('for', 'select'):
            self.stack.append({'t': 'loop', 'kw': v})
            self.cmd_pos = False
            return
        elif v == 'done':
            self.end_cmd()
            self.pop_to({'loop'})
            self.cmd_pos = False
            return
        elif v == 'case':
            self.stack.append({'t': 'case', 'phase': 'subject', 'pat': ''})
            self.cmd_pos = False
            return
        elif v == 'esac':
            self.end_cmd()
            self.pop_to({'case'})
            self.cmd_pos = False
            return
        elif v == 'function':
            name = self.toks[self.k] if self.k < len(self.toks) else None
            if name and name.kind == 'WORD' and name.lit:
                self.k += 1
                nxt = self.toks[self.k:self.k + 2]
                if [x.val for x in nxt] == ['(', ')']:
                    self.k += 2
                return self.define(name.lit, name.line)
            return
        elif v == '{':
            self.stack.append({'t': 'group'})
        elif v == '}':
            self.end_cmd()
            while len(self.stack) > 1:
                e = self.stack.pop()
                if e['t'] == 'group' or (e['t'] == 'func' and e['open'] == '{'):
                    self.close_func(e, t.line)
                    break
            self.cmd_pos = False
            return
        elif v == '[[':
            self.cmd = {'name': '[[', 'line': t.line, 'words': [], 'tags': self.tags()}
            self.cond, self.cmd_pos = True, False
            return
        elif v in ('in', ']]'):
            return
        self.cmd_pos, self.chain = True, None

    def define(self, name, line):
        while self.k < len(self.toks) and self.toks[self.k].kind == 'NL':
            self.k += 1
        f = Func(name, self.file, line)
        self.db.define(f)
        body = self.toks[self.k] if self.k < len(self.toks) else None
        if body and ((body.kind == 'WORD' and body.val == '{') or
                     (body.kind == 'OP' and body.val == '(')):
            self.k += 1
            self.stack.append({'t': 'func', 'f': f, 'open': body.val})
        self.cmd_pos, self.chain = True, None

    def assignment(self, word):
        if self.owner().name != '<top>':
            return
        name, _, value = word.partition('=')
        value = resolve_text(value, self.file, self.db.vars)
        if value is not None and name not in self.db.vars:
            self.db.vars[name.rstrip('+')] = value

    def end_cmd(self, background=False):
        c, self.cmd = self.cmd, None
        if c is None:
            return
        name, words = c['name'], c['words']
        if background:
            c['tags'] = c['tags'] + ['&']
        if name in PREFIX_COMMANDS and words and words[0][1]:
            name, words = words[0][1], words[1:]
        elif name == 'env':
            i = 0
            while i < len(words) and (ASSIGNMENT.match(words[i][0]) or words[i][0].startswith('-')):
                i += 2 if words[i][0] in ('-u', '--unset', '-C', '--chdir', '-S') else 1
            if i < len(words) and words[i][1]:
                name, words = words[i][1], words[i + 1:]
        owner = self.owner()
        if name in ('trap', 'eval') and words and words[0][1] and not words[0][1].startswith('-'):
            label = 'trap ' + ' '.join(w[0] for w in words[1:]) if name == 'trap' else 'eval'
            sub = Parser(self.db, self.file, Lexer(words[0][1], c['line']).run(),
                         owner, c['tags'] + [label])
            sub.run()
            return
        call = Call(name, self.file, c['line'], words, c['tags'])
        owner.calls.append(call)
        if name in ('source', '.') and words:
            self.db.sources.append((call, words[0][0]))


# -- source path resolution ----------------------------------------------
def resolve_text(text, file, variables):
    """Best-effort static value of a shell word; None if it can't be known."""
    here = os.path.dirname(os.path.abspath(file))
    out, i = [], 0
    while i < len(text):
        if text.startswith('$(', i):
            depth, j = 0, i + 1
            while j < len(text):
                depth += {'(': 1, ')': -1}.get(text[j], 0)
                if depth == 0:
                    break
                j += 1
            inner = text[i:j + 1]
            if 'dirname' in inner and ('BASH_SOURCE' in inner or '$0' in inner):
                out.append(here)
            else:
                return None
            i = j + 1
            continue
        m = re.match(r'\$\{?([A-Za-z_]\w*)\}?', text[i:])
        if m:
            if m.group(1) not in variables:
                return None
            out.append(variables[m.group(1)])
            i += m.end()
            continue
        if text[i] == '$':
            return None
        if text[i] not in '"\'':
            out.append(text[i])
        i += 1
    return ''.join(out)


def parse_file(db: DB, path: str):
    top = Func('<top>', path, 0)
    db.tops[path] = top
    with open(path, encoding='utf-8', errors='replace') as fh:
        text = fh.read()
    Parser(db, path, Lexer(text).run(), top).run()


def load(entry, extra, variables):
    db = DB(variables)
    queue, seen, done = [os.path.abspath(p) for p in [entry] + extra], set(), 0
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        parse_file(db, path)
        for call, raw in db.sources[done:]:
            value = resolve_text(raw, call.file, db.vars)
            if value is not None and not os.path.isabs(value):
                value = os.path.join(os.path.dirname(call.file), value)
            matches = sorted(glob.glob(value)) if value else []
            if not matches:
                db.warnings.append(f'could not resolve `source {raw}` at {rel(call.file)}:{call.line}'
                                   ' (pass the file as an extra argument or use --var NAME=VALUE)')
            call.paths = [os.path.abspath(m) for m in matches]
            queue.extend(call.paths)
        done = len(db.sources)
    return db


BASE = os.getcwd()


def rel(path):
    r = os.path.relpath(path, BASE)
    return path if r.startswith('..') else r


# -- rendering -----------------------------------------------------------
class Style:
    def __init__(self, on):
        self.on = on

    def __call__(self, code, text):
        return f'\033[{code}m{text}\033[0m' if self.on and text else text

    def dim(self, t): return self('2', t)
    def loc(self, t): return self('36', t)
    def name(self, t): return self('1', t)
    def mark(self, t): return self('1;33', t)
    def warn(self, t): return self('33', t)


class Renderer:
    def __init__(self, db: DB, opts, style: Style):
        self.db, self.o, self.s = db, opts, style
        self.marks = set(opts.mark) | set(opts.to)
        self.expanded: dict[str, str] = {}
        self.lines = 0
        self.reach_memo: dict[str, bool] = {}

    def out(self, text):
        self.lines += 1
        if self.lines <= self.o.max_lines:
            print(text)

    def is_func(self, name):
        return name in self.db.funcs

    def runs_args(self, name):
        return any(x.name is None for x in self.db.funcs[name].calls)

    def indirect(self, c: Call):
        if not self.is_func(c.name) or not self.runs_args(c.name):
            return []
        return [lit for raw, lit in c.words
                if lit and raw == lit and lit != c.name and self.is_func(lit)]

    def hidden(self, name):
        return name in self.o.hide or bool(self.o.ignore and self.o.ignore.search(name))

    def visible(self, c: Call):
        if c.name is None:
            return False
        if c.name in ('source', '.'):
            return self.o.sources
        if self.hidden(c.name):
            return False
        if self.is_func(c.name):
            return not (self.o.no_leaves and not self.indirect(c) and
                        not any(self.is_func(x.name) and not self.hidden(x.name)
                                for x in self.db.funcs[c.name].calls))
        return self.o.externals or c.name in self.o.show

    def reaches(self, name, stack=()):
        if name in self.o.to:
            return True
        if name in self.reach_memo:
            return self.reach_memo[name]
        if name in stack or not self.is_func(name):
            return False
        hit = any(self.call_reaches(c, stack + (name,)) for c in self.db.funcs[name].calls)
        if not stack or not hit:
            self.reach_memo[name] = hit
        return hit

    def call_reaches(self, c: Call, stack=()):
        if c.name in ('source', '.'):
            return self.o.sources and any(
                any(self.call_reaches(x, stack) for x in self.db.tops[p].calls) for p in c.paths)
        return (c.name is not None and self.reaches(c.name, stack)) or \
            any(self.reaches(t, stack) for t in self.indirect(c))

    def children(self, calls):
        kept = [c for c in calls if self.visible(c)]
        if self.o.to:
            kept = [c for c in kept if self.call_reaches(c)]
        return kept

    def label(self, name):
        text = self.s.name(name)
        return self.s.mark('★ ' + name) if name in self.marks else text

    def where(self, file, line, base_file):
        if file == base_file and not self.o.full_paths:
            return f':{line}'
        return f'{rel(file)}:{line}'

    def command_text(self, c: Call):
        args = []
        for raw, _ in c.words:
            raw = ' '.join(raw.split())
            args.append(raw if len(raw) <= 28 else raw[:27] + '…')
        rest = ' '.join(args)
        room = self.o.width - len(c.name)
        if len(rest) > room:
            rest = rest[:max(room - 1, 0)] + '…'
        return self.label(c.name) + (' ' + rest if rest else '')

    def tag_text(self, c: Call):
        return self.s.dim('  [' + '] ['.join(c.tags) + ']') if c.tags else ''

    def status(self, name, depth, path, site):
        """Return (suffix, expand?) for a function about to be shown."""
        f = self.db.funcs[name]
        kids = self.children(f.calls)
        defined = self.s.dim(f'  -> {rel(f.file)}:{f.line}')
        if not kids:
            return defined, False
        if name in path:
            return defined + self.s.warn('  ↻ recursive'), False
        if name in self.expanded and not self.o.full:
            return defined + self.s.dim(f'  ^ see {self.expanded[name]}'), False
        if depth >= self.o.depth:
            return defined + self.s.dim('  …'), False
        self.expanded[name] = site
        return defined, True

    def calls(self, calls, owner_file, prefix, depth, path):
        kids = self.children(calls)
        for n, c in enumerate(kids):
            self.call(c, owner_file, prefix, n == len(kids) - 1, depth, path)

    def call(self, c: Call, owner_file, prefix, last, depth, path):
        branch, pad = ('└─ ', '    ') if last else ('├─ ', '│   ')
        site = self.where(c.file, c.line, owner_file)
        head = prefix + branch + self.s.loc(site) + '  ' + self.command_text(c) + self.tag_text(c)
        sub = prefix + pad
        if c.name in ('source', '.'):
            self.out(head + self.s.dim('  -> ' + ', '.join(rel(p) for p in c.paths)))
            tops = [x for p in c.paths for x in self.db.tops[p].calls]
            self.calls(tops, None, sub, depth + 1, path)
            return
        targets = self.indirect(c)
        if self.o.to:
            targets = [t for t in targets if self.reaches(t)]
        if targets:
            nodes = []
            if not (c.name in self.expanded and not self.o.full) and \
                    (not self.o.to or self.reaches(c.name)):
                nodes.append((c.name, ''))
            nodes += [(t, f'(via {c.name})') for t in targets]
            self.out(head)
            for n, (name, via) in enumerate(nodes):
                self.func_node(name, via, sub, n == len(nodes) - 1, depth + 1, path,
                               f'{rel(c.file)}:{c.line}')
            return
        if not self.is_func(c.name):
            self.out(head)
            return
        suffix, expand = self.status(c.name, depth, path, f'{rel(c.file)}:{c.line}')
        self.out(head + suffix)
        if expand:
            f = self.db.funcs[c.name]
            self.calls(f.calls, f.file, sub, depth + 1, path | {c.name})

    def func_node(self, name, via, prefix, last, depth, path, site):
        branch, pad = ('└─ ', '    ') if last else ('├─ ', '│   ')
        suffix, expand = self.status(name, depth, path, site)
        self.out(prefix + branch + self.label(name) + '()' +
                 (self.s.dim('  ' + via) if via else '') + suffix)
        if expand:
            f = self.db.funcs[name]
            self.calls(f.calls, f.file, prefix + pad, depth + 1, path | {name})

    def tree(self, entry_file):
        if self.o.entry:
            f = self.db.funcs[self.o.entry]
            self.expanded[f.name] = f'{rel(f.file)}:{f.line}'
            self.out(self.label(f.name) + '()' + self.s.dim(f'  -> {rel(f.file)}:{f.line}'))
            self.calls(f.calls, f.file, '', 1, frozenset({f.name}))
        else:
            top = self.db.tops[entry_file]
            self.out(self.s.name(rel(entry_file)))
            if not self.children(top.calls):
                self.out(self.s.dim('  (no matching top-level calls; try --entry FUNCTION or --list)'))
            self.calls(top.calls, entry_file, '', 1, frozenset())
        if self.lines > self.o.max_lines:
            print(self.s.warn(f'… {self.lines - self.o.max_lines} more lines (raise --max-lines)'))


def called_names(db: DB):
    """Number of call sites naming each function, directly or as an argument."""
    callers: dict[str, int] = {}
    for f in list(db.funcs.values()) + list(db.tops.values()):
        for c in f.calls:
            for name in [c.name] + [lit for raw, lit in c.words if lit and raw == lit]:
                if name in db.funcs and name != f.name:
                    callers[name] = callers.get(name, 0) + 1
    return callers


def command_line(c: Call):
    words = ' '.join(' '.join(r.split()) for r, _ in c.words)
    return (c.name or '') + (' ' + words if words else '')


def list_functions(db: DB, style: Style):
    callers = called_names(db)
    rows = sorted(db.funcs.values(), key=lambda f: (rel(f.file), f.line))
    width = max((len(f.name) for f in rows), default=8)
    print(style.dim(f'{"FUNCTION":<{width}}  {"DEFINED":<28} CALLS  CALLED-FROM'))
    for f in rows:
        out = sum(1 for c in f.calls if c.name in db.funcs)
        loc = f'{rel(f.file)}:{f.line}'
        print(f'{f.name:<{width}}  {style.loc(f"{loc:<28}")} {out:>5}  {callers.get(f.name, 0):>11}')


def list_names(db: DB, style: Style):
    rows = sorted(db.funcs.values(), key=lambda f: (rel(f.file), f.line))
    for f in rows:
        print(f.name)
    print(style.dim(f'{len(rows)} functions'))


def list_callers(db: DB, name: str, style: Style):
    found = 0
    for f in list(db.tops.values()) + list(db.funcs.values()):
        for c in f.calls:
            direct = c.name == name
            via = any(lit == name and raw == lit for raw, lit in c.words) and c.name in db.funcs
            if not (direct or via):
                continue
            found += 1
            owner = rel(f.file) if f.name == '<top>' else f.name + '()'
            text = command_line(c)
            text = text if len(text) <= 70 else text[:69] + '…'
            tags = style.dim('  [' + '] ['.join(c.tags) + ']') if c.tags else ''
            print(f'{style.loc(f"{rel(c.file)}:{c.line}")}  in {style.name(owner)}  {text}{tags}')
    if not found:
        print(f'No calls to {name} found.')


# -- exports -------------------------------------------------------------
@dataclass
class Edge:
    src: tuple  # node key: ('func', name), ('file', path) or ('cmd', name)
    dst: tuple
    call: Call
    order: int  # position among the source node's calls
    via: str | None = None


def q(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


class Selection:
    """The nodes and calls the tree filters pick, for --graph, --yaml, --json,
    --subset and --animate.

    With --entry or --to it starts where the tree does.  Otherwise it covers the
    whole program: every file's top level plus every function nothing calls.
    Source lines are always followed, so sourced files show up as nodes.
    """

    def __init__(self, db: DB, opts, entry_file):
        o = argparse.Namespace(**vars(opts))
        o.sources = True
        self.db, self.o, self.entry_file = db, o, entry_file
        self.r = Renderer(db, o, Style(False))
        self.nodes: dict[tuple, int] = {}  # key -> depth first reached
        self.out: dict[tuple, list[Edge]] = {}  # expanded node -> its edges in call order
        self.edges: list[Edge] = []
        self.notes: list[str] = []
        self.roots = self.find_roots()
        self.build()

    def find_roots(self):
        if self.o.entry:
            return [('func', self.o.entry)]
        if self.o.to:
            return [('file', self.entry_file)]
        called = called_names(self.db)
        funcs = sorted(self.db.funcs.values(), key=lambda f: (rel(f.file), f.line))
        return [('file', p) for p in self.db.tops] + \
            [('func', f.name) for f in funcs if f.name not in called and not self.r.hidden(f.name)]

    def owner(self, key):
        return self.db.funcs[key[1]] if key[0] == 'func' else self.db.tops[key[1]]

    def targets(self, c: Call):
        r, o = self.r, self.o
        if c.name is None or (o.to and not r.call_reaches(c)):
            return []
        if c.name in ('source', '.'):
            return [(('file', p), None) for p in c.paths]
        if not r.visible(c):
            return []
        via = [t for t in r.indirect(c) if not r.hidden(t) and (not o.to or r.reaches(t))]
        if not r.is_func(c.name):
            out = [(('cmd', c.name), None)]
        elif via and o.to and not r.reaches(c.name):
            out = []
        else:
            out = [(('func', c.name), None)]
        return out + [(('func', t), c.name) for t in via]

    def build(self):
        queue = [(k, 0) for k in self.roots]
        for k in self.roots:
            self.nodes.setdefault(k, 0)
        while queue:
            key, depth = queue.pop(0)
            if key in self.out or key[0] == 'cmd' or depth >= self.o.depth:
                continue
            edges, order = self.out.setdefault(key, []), 0
            for c in self.owner(key).calls:
                hits = self.targets(c)
                order += bool(hits)
                for dst, via in hits:
                    e = Edge(key, dst, c, order, via)
                    edges.append(e)
                    self.edges.append(e)
                    self.nodes.setdefault(dst, depth + 1)
                    queue.append((dst, depth + 1))

    def functions(self):
        names = [k[1] for k in self.nodes if k[0] == 'func']
        return sorted((self.db.funcs[n] for n in names), key=lambda f: (rel(f.file), f.line))

    def name(self, key, parens=True):
        return rel(key[1]) if key[0] == 'file' else key[1] + ('()' if parens and key[0] == 'func' else '')

    # -- graphviz --------------------------------------------------------
    def groups(self):
        out: dict[tuple, list[Edge]] = {}
        for e in self.edges:
            out.setdefault((e.src, e.dst, e.via), []).append(e)
        return out

    def node_line(self, key, visited, current):
        kind, name = key
        attrs = {}
        if kind == 'func':
            f = self.db.funcs[name]
            more = '\n…' if key not in self.out and f.calls else ''
            attrs.update(shape='box', style='rounded', label=f'{name}()\n:{f.line}{more}')
        elif kind == 'file':
            attrs.update(shape='folder', label=rel(name))
        else:
            attrs.update(shape='plaintext', label=name, fontcolor='gray35')
        fill = None
        if kind == 'func' and name in self.r.marks:
            attrs.update(color='#c98a00', penwidth='2')
            fill = '#ffe680'
        if key in visited:
            fill = '#dce8fb'
        if key == current:
            fill = '#ffc9c9'
        if fill:
            attrs['style'] = (attrs.get('style', '') + ',filled').lstrip(',')
            attrs['fillcolor'] = fill
        if self.o.entry and key == self.roots[0]:
            attrs['penwidth'] = '2'
        return q(':'.join(key)) + ' [' + ' '.join(f'{k}={q(v)}' for k, v in attrs.items()) + '];'

    def edge_line(self, gkey, edges, done, current, anim):
        src, dst, via = gkey
        parts = []
        for e in edges[:4]:
            parts.append(f'#{e.order} :{e.call.line}' +
                         (' [' + '] ['.join(e.call.tags) + ']' if e.call.tags else ''))
        if len(edges) > 4:
            parts.append(f'+{len(edges) - 4} more')
        attrs = {'label': ('via ' + via + '\n' if via else '') + '\n'.join(parts)}
        if via:
            attrs['style'] = 'dashed'
        elif dst[0] == 'file':
            attrs['style'] = 'dotted'
        if anim:
            if gkey == current:
                attrs.update(color='#d62728', fontcolor='#d62728', penwidth='2.5')
            elif gkey in done:
                attrs.update(color='#3566c8', fontcolor='#3566c8', penwidth='1.5')
            else:
                attrs.update(color='gray80', fontcolor='gray65')
        return (f'{q(":".join(src))} -> {q(":".join(dst))} [' +
                ' '.join(f'{k}={q(v)}' for k, v in attrs.items()) + '];')

    def dot(self, done=frozenset(), current=None, caption=None, width=0):
        """Graphviz text.  For an animation frame, done holds the edge groups
        already run, current the one running now and caption the frame title,
        drawn at a fixed width so every frame gets the same layout."""
        anim = caption is not None
        visited = {k for g in done for k in g[:2]}
        if current:
            visited.add(current[0])
        current_node = current[1] if current else None
        lines = ['digraph calltree {',
                 '  rankdir=LR; bgcolor="white"; newrank=true;',
                 '  node [fontname="Helvetica" fontsize=10];',
                 '  edge [fontname="Helvetica" fontsize=8 arrowsize=0.7];']
        if anim:
            lines.append('  labelloc=t; labeljust=l; label=<<TABLE BORDER="0"><TR>'
                         f'<TD FIXEDSIZE="TRUE" WIDTH="{width}" HEIGHT="22" ALIGN="LEFT">'
                         f'<FONT FACE="Courier" POINT-SIZE="12">{html.escape(caption)}</FONT>'
                         '</TD></TR></TABLE>>;')
        clusters: dict[str, list] = {}
        for key in self.nodes:
            if key[0] != 'cmd':
                file = key[1] if key[0] == 'file' else self.db.funcs[key[1]].file
                clusters.setdefault(file, []).append(key)
        for n, (file, keys) in enumerate(clusters.items()):
            lines.append(f'  subgraph cluster_{n} {{ label={q(rel(file))}; style="rounded"; '
                         'color="gray75"; fontcolor="gray45"; fontsize=9;')
            lines += ['    ' + self.node_line(k, visited, current_node) for k in keys]
            lines.append('  }')
        lines += ['  ' + self.node_line(k, visited, current_node) for k in self.nodes if k[0] == 'cmd']
        lines += ['  ' + self.edge_line(g, es, done, current, anim) for g, es in self.groups().items()]
        lines.append('}')
        return '\n'.join(lines) + '\n'

    # -- data ------------------------------------------------------------
    def data(self):
        db = self.db
        kinds = {'func': 'function', 'file': 'file', 'cmd': 'command'}

        def at(c):
            return f'{rel(c.file)}:{c.line}'

        def calls(key):
            rows = []
            for e in self.out.get(key, []):
                row = {'order': e.order, 'kind': kinds[e.dst[0]], 'name': self.name(e.dst, False),
                       'at': at(e.call), 'command': command_line(e.call)}
                if e.dst[0] == 'func':
                    f = db.funcs[e.dst[1]]
                    row['defined'] = f'{rel(f.file)}:{f.line}'
                if e.via:
                    row['via'] = e.via
                row['tags'] = list(e.call.tags)
                rows.append(row)
            return rows

        callers: dict[tuple, list] = {}
        for e in self.edges:
            row = {'kind': kinds[e.src[0]], 'name': self.name(e.src, False), 'at': at(e.call)}
            if e.via:
                row['via'] = e.via
            callers.setdefault(e.dst, []).append(row)
        functions = []
        for f in self.functions():
            key = ('func', f.name)
            row = {'name': f.name, 'defined': f'{rel(f.file)}:{f.line}',
                   'end': f'{rel(f.file)}:{f.end}' if f.end else None,
                   'calls': calls(key), 'called_by': callers.get(key, [])}
            if key not in self.out and f.calls:
                row['beyond_depth'] = True
            functions.append(row)
        return {
            'script': rel(self.entry_file),
            'entry': self.o.entry,
            'to': self.o.to,
            'warnings': db.warnings,
            'files': [{'path': rel(k[1]), 'calls': calls(k), 'called_by': callers.get(k, [])}
                      for k in self.nodes if k[0] == 'file'],
            'functions': functions,
            'commands': sorted(k[1] for k in self.nodes if k[0] == 'cmd'),
        }

    def subset(self):
        """Source text of the selected functions, file by file, with the
        comment block above each definition."""
        funcs = self.functions()
        inside = lambda f: any(g is not f and g.file == f.file and g.line < f.line <= g.end
                               for g in funcs)
        texts: dict[str, list[str]] = {}
        out = [f'# calltree: {len(funcs)} functions from {rel(self.entry_file)}'
               ' (static extract for reading; not meant to run as is)']
        current = None
        for f in funcs:
            if inside(f):
                continue
            if f.file not in texts:
                with open(f.file, encoding='utf-8', errors='replace') as fh:
                    texts[f.file] = fh.read().splitlines()
            lines = texts[f.file]
            start = f.line
            while start > 1 and lines[start - 2].lstrip().startswith('#') and \
                    not lines[start - 2].startswith('#!'):
                start -= 1
            if not f.end:
                self.notes.append(f'end of {f.name} not found; subset has its first line only')
            if f.file != current:
                out += ['', f'# ===== {rel(f.file)} =====']
                current = f.file
            out += ['', f'# {rel(f.file)}:{f.line}'] + lines[start - 1:(f.end or f.line)]
        return '\n'.join(out) + '\n'

    # -- animation -------------------------------------------------------
    def steps(self):
        """Edges in the order the code runs them: depth first, following each
        call into the function it runs (each function once, unless --full)."""
        out, seen = [], set()

        def walk(key, path):
            if key in path or (key in seen and not self.o.full):
                return
            seen.add(key)
            for e in self.out.get(key, []):
                out.append(e)
                walk(e.dst, path | {key})

        for root in self.roots:
            walk(root, frozenset())
        return out

    def frames(self):
        steps = self.steps()
        captions = [f'start  {self.name(self.roots[0])}']
        for n, e in enumerate(steps, 1):
            tags = ' [' + '] ['.join(e.call.tags) + ']' if e.call.tags else ''
            via = f' (via {e.via})' if e.via else ''
            captions.append(f'{n}/{len(steps)}  {self.name(e.src)} -> {self.name(e.dst)}{via}'
                            f'  {rel(e.call.file)}:{e.call.line}{tags}')
        captions.append(f'end  {len(steps)} calls')
        width = int(max(len(c) for c in captions) * 7.2) + 12
        groups = [(e.src, e.dst, e.via) for e in steps]
        texts = [self.dot(frozenset(), None, captions[0], width)]
        for n, g in enumerate(groups):
            texts.append(self.dot(frozenset(groups[:n]), g, captions[n + 1], width))
        texts.append(self.dot(frozenset(groups), None, captions[-1], width))
        return texts


def render(text, fmt):
    run = subprocess.run(['dot', '-T' + fmt], input=text.encode(), capture_output=True)
    if run.returncode:
        sys.exit('calltree: dot failed: ' + run.stderr.decode(errors='replace').strip())
    return run.stdout


def gif_image(data: bytes):
    """First image of a GIF: (screen size, descriptor fields, colour table, LZW data)."""
    if data[:3] != b'GIF':
        sys.exit('calltree: dot did not return a GIF')
    w, h, packed = struct.unpack('<HHB', data[6:11])
    pos, table = 13, b''
    if packed & 0x80:
        size = 3 * (2 << (packed & 7))
        table, pos = data[pos:pos + size], pos + size
    while pos < len(data) and data[pos] != 0x3B:
        if data[pos] == 0x21:  # extension: skip its sub-blocks
            pos += 2
            while data[pos]:
                pos += data[pos] + 1
            pos += 1
            continue
        x, y, iw, ih, flags = struct.unpack('<HHHHB', data[pos + 1:pos + 10])
        pos += 10
        if flags & 0x80:
            size = 3 * (2 << (flags & 7))
            table, pos = data[pos:pos + size], pos + size
        start = pos
        pos += 1  # LZW minimum code size
        while data[pos]:
            pos += data[pos] + 1
        return (w, h), (x, y, iw, ih, flags & 0x40), table, data[start:pos + 1]
    sys.exit('calltree: GIF from dot has no image')


def write_gif(path, images, delays):
    """Join single-image GIFs into one looping animation (delays in 1/100 s)."""
    w = max(img[0][0] for img in images)
    h = max(img[0][1] for img in images)
    out = bytearray(b'GIF89a' + struct.pack('<HHBBB', w, h, 0, 0, 0))
    out += b'\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00'  # loop forever
    for (_, (x, y, iw, ih, interlace), table, lzw), delay in zip(images, delays):
        bits = max(len(table) // 3, 2).bit_length() - 2
        out += struct.pack('<BBBBHBB', 0x21, 0xF9, 4, 2 << 2, delay, 0, 0)
        out += struct.pack('<BHHHHB', 0x2C, x, y, iw, ih, 0x80 | interlace | bits)
        out += table.ljust(3 * (2 << bits), b'\0') + lzw
    out += b'\x3b'
    with open(path, 'wb') as fh:
        fh.write(out)
    return (w, h)


GRAPH_FORMATS = {'.dot': None, '.gv': None, '.svg': 'svg', '.png': 'png', '.pdf': 'pdf'}


def check_exports(p, o):
    outputs = [x for x in (o.graph, o.yaml, o.json, o.subset, o.animate) if x]
    if not outputs:
        return False
    if o.names or o.list or o.callers:
        p.error('--graph, --yaml, --json, --subset and --animate cannot be combined '
                'with --list, --names or --callers')
    if outputs.count('-') > 1:
        p.error('only one output can go to stdout (-)')
    ext = os.path.splitext(o.graph)[1].lower() if o.graph and o.graph != '-' else '.dot'
    if ext not in GRAPH_FORMATS:
        p.error(f'--graph: unknown format {ext or "(none)"}; use .dot, .gv, .svg, .png or .pdf')
    if o.animate == '-' or (o.animate and not o.animate.lower().endswith('.gif')):
        p.error('--animate needs a .gif file name')
    if (GRAPH_FORMATS[ext] or o.animate) and not shutil.which('dot'):
        p.error('drawing images needs Graphviz `dot` on PATH (macOS: brew install graphviz); '
                'a .dot graph works without it')
    if o.delay <= 0:
        p.error('--delay must be more than 0')
    return True


def write_text(path, text):
    if path == '-':
        sys.stdout.write(text)
    else:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)


YAML_PLAIN = re.compile(r'[A-Za-z_./][\w./-]*(:\d+)?')
YAML_WORDS = {'y', 'n', 'yes', 'no', 'on', 'off', 'true', 'false', 'null', '~'}


def yaml_scalar(v):
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, dict)):
        return '[]' if isinstance(v, list) else '{}'
    if YAML_PLAIN.fullmatch(v) and v.lower() not in YAML_WORDS:
        return v
    return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid YAML one


def yaml_lines(v, pad=''):
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                yield f'{pad}{k}:'
                yield from yaml_lines(x, pad + '  ')
            else:
                yield f'{pad}{k}: {yaml_scalar(x)}'
        return
    for x in v:
        if isinstance(x, dict) and x:
            first, *rest = yaml_lines(x, pad + '  ')
            yield f'{pad}- {first.lstrip()}'
            yield from rest
        else:
            yield f'{pad}- {yaml_scalar(x)}'


def exports(db: DB, o, entry_file, style: Style):
    sel = Selection(db, o, entry_file)
    funcs = sum(1 for k in sel.nodes if k[0] == 'func')
    summary = f'{funcs} functions, {len(sel.edges)} calls'
    say = lambda path, what: path != '-' and print(style.dim(f'wrote {path}: {what}'), file=sys.stderr)
    if o.graph:
        fmt = GRAPH_FORMATS[os.path.splitext(o.graph)[1].lower()] if o.graph != '-' else None
        text = sel.dot()
        if fmt:
            with open(o.graph, 'wb') as fh:
                fh.write(render(text, fmt))
        else:
            write_text(o.graph, text)
        say(o.graph, summary)
    if o.yaml:
        write_text(o.yaml, '\n'.join(yaml_lines(sel.data())) + '\n')
        say(o.yaml, summary)
    if o.json:
        write_text(o.json, json.dumps(sel.data(), indent=2, ensure_ascii=False) + '\n')
        say(o.json, summary)
    if o.subset:
        write_text(o.subset, sel.subset())
        say(o.subset, f'{funcs} functions')
    if o.animate:
        texts = sel.frames()
        if len(texts) <= 2:
            sys.exit('calltree: no calls to animate (check --entry, --to and the hide filters)')
        if len(texts) > 300:
            print(style.warn(f'warning: {len(texts)} frames; narrow it with -e, -d, -t, -H or --ignore '
                             'for a shorter, smaller GIF'), file=sys.stderr)
        with ThreadPoolExecutor(os.cpu_count() or 4) as pool:
            images = [gif_image(g) for g in pool.map(lambda t: render(t, 'gif'), texts)]
        step = max(1, round(o.delay * 100))
        size = write_gif(o.animate, images, [step] * (len(images) - 1) + [step * 3])
        say(o.animate, f'{len(texts) - 2} steps, {len(texts)} frames, {size[0]}x{size[1]}, '
                       f'{o.delay:g}s per step')
    for w in sel.notes:
        print(style.warn('warning: ' + w), file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='calltree', description=__doc__.split('\n\n')[0],
        epilog=__doc__.split('\n\n', 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('script', help='entry script')
    p.add_argument('extra', nargs='*', help='more files to load (when a source path cannot be resolved)')
    p.add_argument('-e', '--entry', help='start the tree at this function')
    p.add_argument('-m', '--mark', default='', help='comma-separated functions to highlight')
    p.add_argument('-t', '--to', default='', help='only show paths that reach these functions')
    p.add_argument('-d', '--depth', type=int, default=10**6, help='maximum depth')
    p.add_argument('-x', '--externals', action='store_true', help='also show external commands and builtins')
    p.add_argument('-s', '--show', default='', help='comma-separated external commands to show (e.g. psql,kubectl)')
    p.add_argument('-H', '--hide', default='', help='comma-separated functions to leave out (e.g. die,say)')
    p.add_argument('-I', '--ignore', metavar='REGEX',
                   help='leave out functions and commands whose name matches REGEX (e.g. "^log_|^say$")')
    p.add_argument('-L', '--no-leaves', action='store_true',
                   help='leave out calls to functions that call nothing shown')
    p.add_argument('--sources', action='store_true', help='show source/. lines')
    p.add_argument('--full', action='store_true', help='re-expand functions already shown')
    p.add_argument('--full-paths', action='store_true', help='always print file paths at call sites')
    p.add_argument('--width', type=int, default=70, help='maximum command text width')
    p.add_argument('--max-lines', type=int, default=5000)
    p.add_argument('--var', action='append', default=[], metavar='NAME=VALUE',
                   help='value for a variable used in source paths')
    p.add_argument('--callers', metavar='FUNCTION', help='list every call site of FUNCTION')
    p.add_argument('--list', action='store_true', help='list all functions')
    p.add_argument('--names', action='store_true', help='list function names only, then their count')
    p.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    x = p.add_argument_group('exports (instead of the tree; FILE - means stdout)')
    x.add_argument('--graph', metavar='FILE', help='draw the call graph: .dot/.gv, or .svg/.png/.pdf via Graphviz')
    x.add_argument('--yaml', metavar='FILE', help='write functions, call sites and callers as YAML')
    x.add_argument('--json', metavar='FILE', help='the same data as JSON')
    x.add_argument('--subset', metavar='FILE', help='write the source of only the selected functions')
    x.add_argument('--animate', metavar='FILE.gif', help='animated GIF stepping through the calls in run order')
    x.add_argument('--delay', type=float, default=1.0, metavar='SECONDS',
                   help='time per animation step (default 1)')
    o = p.parse_args(argv)
    split = lambda v: [x for x in v.split(',') if x]
    o.mark, o.to, o.show = split(o.mark), split(o.to), set(split(o.show))
    o.hide = set(split(o.hide))
    try:
        o.ignore = re.compile(o.ignore) if o.ignore else None
    except re.error as e:
        p.error(f'--ignore: bad regex: {e}')
    exporting = check_exports(p, o)
    if o.to and not exporting:
        o.full = True

    for path in [o.script] + o.extra:
        if not os.path.isfile(path):
            p.error(f'no such file: {path}')
    global BASE
    if os.path.relpath(os.path.abspath(o.script)).startswith('..'):
        BASE = os.path.dirname(os.path.abspath(o.script))
    variables = dict(v.split('=', 1) for v in o.var if '=' in v)
    db = load(o.script, o.extra, variables)
    style = Style(o.color == 'always' or (o.color == 'auto' and sys.stdout.isatty()
                                          and not os.environ.get('NO_COLOR')))
    for w in db.warnings:
        print(style.warn('warning: ' + w), file=sys.stderr)
    for old, new in db.dupes:
        print(style.warn(f'warning: {new.name} defined at {rel(old.file)}:{old.line} and '
                         f'{rel(new.file)}:{new.line}; using the last one'), file=sys.stderr)

    for name in [o.entry, o.callers] + o.mark + o.to:
        if name and name not in db.funcs:
            near = difflib.get_close_matches(name, db.funcs, n=3)
            p.error(f'unknown function: {name}' + (f' (did you mean {", ".join(near)}?)' if near else ''))

    if exporting:
        exports(db, o, os.path.abspath(o.script), style)
    elif o.names:
        list_names(db, style)
    elif o.list:
        list_functions(db, style)
    elif o.callers:
        list_callers(db, o.callers, style)
    else:
        Renderer(db, o, style).tree(os.path.abspath(o.script))


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        sys.stderr.close()
