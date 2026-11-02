#!/usr/bin/env python3
"""MCP server (stdio) exposing tools/calltree.py to Claude Code and other MCP clients.

No dependencies: speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and runs
calltree.py as a subprocess, so nothing it prints can corrupt the protocol stream.

Register with Claude Code:
    claude mcp add calltree -- python3 /abs/path/to/tools/calltree_mcp.py
"""
import json
import os
import subprocess
import sys

CALLTREE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calltree.py')
TIMEOUT = 60

COMMON = {
    'script': {'type': 'string', 'description': 'Path to the entry shell script (e.g. backup.sh)'},
    'vars': {'type': 'object', 'additionalProperties': {'type': 'string'},
             'description': 'Values for variables in source paths, e.g. {"NOTIFIER": "plugins/notify-mail.sh"}'},
}
FILTERS = {
    'entry': {'type': 'string', 'description': 'Start the tree at this function'},
    'to': {'type': 'array', 'items': {'type': 'string'},
           'description': 'Only keep paths that reach these functions'},
    'depth': {'type': 'integer', 'minimum': 1, 'description': 'Maximum depth'},
    'hide': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Functions to leave out'},
    'ignore': {'type': 'string', 'description': 'Regex of function names to leave out'},
    'externals': {'type': 'boolean', 'description': 'Also show external commands and builtins'},
}

TOOLS = [
    {'name': 'calltree_tree',
     'description': 'Call tree of a shell script in execution order, with file:line for each call.',
     'inputSchema': {'type': 'object', 'required': ['script'], 'properties': {**COMMON, **FILTERS}}},
    {'name': 'calltree_callers',
     'description': 'Every call site of one shell function (file:line and command).',
     'inputSchema': {'type': 'object', 'required': ['script', 'function'],
                     'properties': {**COMMON, 'function': {'type': 'string'}}}},
    {'name': 'calltree_functions',
     'description': 'All functions defined by the script and the files it sources, with locations.',
     'inputSchema': {'type': 'object', 'required': ['script'], 'properties': COMMON}},
    {'name': 'calltree_json',
     'description': 'Functions, call sites and callers as JSON, for precise follow-up analysis.',
     'inputSchema': {'type': 'object', 'required': ['script'], 'properties': {**COMMON, **FILTERS}}},
]


def build_argv(tool, a):
    script = os.path.abspath(a['script'])
    argv = [script, '--color', 'never']
    for name, value in (a.get('vars') or {}).items():
        argv += ['--var', f'{name}={value}']
    if tool == 'calltree_callers':
        return argv + ['--callers', a['function']]
    if tool == 'calltree_functions':
        return argv + ['--list']
    if a.get('entry'):
        argv += ['--entry', a['entry']]
    if a.get('to'):
        argv += ['--to', ','.join(a['to'])]
    if a.get('depth'):
        argv += ['--depth', str(a['depth'])]
    if a.get('hide'):
        argv += ['--hide', ','.join(a['hide'])]
    if a.get('ignore'):
        argv += ['--ignore', a['ignore']]
    if a.get('externals'):
        argv.append('--externals')
    if tool == 'calltree_json':
        argv += ['--json', '-']
    return argv


def call_tool(name, args):
    if name not in {t['name'] for t in TOOLS}:
        raise ValueError(f'unknown tool: {name}')
    if not args.get('script'):
        raise ValueError('"script" is required')
    argv = build_argv(name, args)
    # Run from the script's directory so reported paths read like lib/archive.sh:14.
    proc = subprocess.run([sys.executable, CALLTREE, *argv], cwd=os.path.dirname(argv[0]),
                          capture_output=True, text=True, timeout=TIMEOUT)
    text, err = proc.stdout, proc.stderr
    if 'calltree: error:' in err:  # argparse error: keep warnings + the error, drop the usage block
        head, _, msg = err.partition('usage: calltree')
        err = head + 'error:' + msg.split('calltree: error:', 1)[1]
    if name == 'calltree_json' and proc.returncode == 0:
        err = ''  # keep the output valid JSON; warnings are already in its "warnings" field
    if err.strip():  # warnings (unresolved sources, duplicate definitions) or errors
        text += ('\n' if text else '') + err
    return {'content': [{'type': 'text', 'text': text or '(no output)'}],
            'isError': proc.returncode != 0}


def handle(msg):
    method, params = msg.get('method'), msg.get('params') or {}
    if method == 'initialize':
        return {'protocolVersion': params.get('protocolVersion', '2025-06-18'),
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'calltree', 'version': '1.0.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        try:
            return call_tool(params.get('name'), params.get('arguments') or {})
        except (ValueError, subprocess.TimeoutExpired) as e:
            return {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
    raise LookupError(method)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            if 'id' not in msg:  # notification (e.g. notifications/initialized): no reply
                continue
            try:
                reply = {'jsonrpc': '2.0', 'id': msg['id'], 'result': handle(msg)}
            except LookupError as e:
                reply = {'jsonrpc': '2.0', 'id': msg['id'],
                         'error': {'code': -32601, 'message': f'method not found: {e}'}}
        sys.stdout.write(json.dumps(reply) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
