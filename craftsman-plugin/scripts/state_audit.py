#!/usr/bin/env python3
"""State-machine checks for the craftsman hooks. Stdlib only.

    state_audit.py scan            hook JSON on stdin (Edit/Write/MultiEdit);
                                   prints what kinds of state write the edit makes,
                                   one per line, or nothing
    state_audit.py record <dir>    note that <dir>'s project was just audited
    state_audit.py due <dir>       print a reminder line when <dir>'s project is
                                   overdue for an audit (at most once a week)

Audit dates live in $CRAFTSMAN_CONFIG_DIR/state-audits.tsv (default
~/.claude/craftsman), one line per project root: path, last audit, last reminder
(epoch seconds; 0 = never). CRAFTSMAN_STATE_AUDIT_DAYS sets how old an audit may
get (default 30); 0 turns the reminder off.

Every failure is silent: a hook must never break the session.
"""

import json
import os
import re
import subprocess
import sys
import time

DAY = 86400
NUDGE_EVERY = 7 * DAY

# What a state write looks like in an edit. Each pattern names one kind; the
# hook reports the kinds, not the lines, so the nudge stays short.
_STATE = r"(?:status|state|stage|phase|lifecycle)"
PATTERNS = [
    # status: 'running' / status = "done" / 'status': 'failed' / state = 'x'
    ("status write", re.compile(
        r"""(?ix) (?:\b|['"]) [a-z_]*""" + _STATE + r"""['"]? \s* (?:=|:|=>) \s* ['"][a-z_ -]{2,}['"]""")),
    # {'status': next.name} / .update({ status, ... }) — a quoted key or a
    # row write, whatever the value is
    ("status write", re.compile(
        r"""(?ix) ['"][a-z_]*""" + _STATE + r"""['"] \s* : \s* [\w(]
            | \b(?:update|insert|upsert|copyWith|copy) \s* \( \s* \{? [^;]{0,200}? (?<!\?\ ) (?<!\?) \b[a-z_]*(?:status|stage|phase)\b \s* [:,=}]""")),
    # status = Status.RUNNING / stage = Stage.Shipped (enum assignment)
    ("status write", re.compile(
        r"""(?x) \b[a-z_]*(?:status|stage|phase)\b \s* = \s* [A-Z]\w*\.[A-Za-z_]+""")),
    # SQL: CHECK (status in (...)), status text ... check, set status = , enum types
    ("status column or constraint", re.compile(
        r"""(?ix) check \s* \( \s* \(? \s* "?[a-z_]*""" + _STATE + r""""? \s* (?:in\b|=\s*any)
            | \b[a-z_]*""" + _STATE + r"""\b \s+ text\b [^;\n]* \bcheck\b
            | \bset \s+ "?[a-z_]*""" + _STATE + r""""? \s* = """)),
    ("lifecycle enum", re.compile(
        r"""(?ix) create \s+ type \s+ \S+ \s+ as \s+ enum
            | \benum \s+ \w*(?:status|state|stage|phase)\b
            | \benum \s+ class \s+ \w*(?:status|state|stage|phase)\b""")),
    # _saving = true / isBusy = false / inFlight = true
    ("busy flag", re.compile(
        r"""(?ix) \b_?(?:is_?)?(?:busy|saving|syncing|submitting|running|in_?flight|working|uploading|downloading)\b
            \s* = \s* (?:true|false|True|False)\b""")),
]

SKIP = re.compile(r"(?i)(\.(md|mdx|txt|rst|json|lock|ya?ml|svg)$|[._-](test|spec)\.|_test\.|/(tests?|__tests__|test_fixtures)/)")


def _edit_text(tool_input):
    parts = [tool_input.get("new_string"), tool_input.get("content")]
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, dict):
            parts.append(edit.get("new_string"))
    return "\n".join(p for p in parts if isinstance(p, str))


def scan(data):
    tool_input = data.get("tool_input") or {}
    path = str(tool_input.get("file_path") or "").replace("\\", "/")
    if not path or SKIP.search(path):
        return []
    text = _edit_text(tool_input)
    kinds = []
    for kind, pattern in PATTERNS:
        if kind not in kinds and pattern.search(text):
            kinds.append(kind)
    return kinds


def project_root(directory):
    try:
        out = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip())
    except Exception:
        pass
    return None


def _store():
    base = os.environ.get("CRAFTSMAN_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "craftsman")
    return os.path.join(base, "state-audits.tsv")


def _load():
    rows = {}
    try:
        with open(_store(), encoding="utf-8") as f:
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) == 3:
                    try:
                        rows[cols[0]] = [int(cols[1]), int(cols[2])]
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _save(rows):
    path = _store()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for root in sorted(rows):
            audited, nudged = rows[root]
            f.write(f"{root}\t{audited}\t{nudged}\n")
    os.replace(tmp, path)


def record(directory, now):
    root = project_root(directory)
    if not root:
        return
    rows = _load()
    rows[root] = [now, rows.get(root, [0, 0])[1]]
    _save(rows)


def due(directory, now):
    try:
        days = int(os.environ.get("CRAFTSMAN_STATE_AUDIT_DAYS", "30"))
    except ValueError:
        days = 30
    if days <= 0:
        return None
    root = project_root(directory)
    if not root:
        return None
    rows = _load()
    audited, nudged = rows.get(root, [0, 0])
    if audited and now - audited < days * DAY:
        return None
    if nudged and now - nudged < NUDGE_EVERY:
        return None
    rows[root] = [audited, now]
    _save(rows)
    name = os.path.basename(root)
    when = f"was last audited {(now - audited) // DAY} days ago" if audited else "has not been audited yet"
    return (
        f"State-machine check: {name} {when}. When the current task is done (or if the user asks for "
        f"a health check), offer /craftsman:state-machines; do not run it unasked. "
        f"This reminder comes at most once a week."
    )


def main(argv):
    if len(argv) < 2:
        return 0
    cmd, now = argv[1], int(time.time())
    if cmd == "scan":
        try:
            data = json.load(sys.stdin)
        except Exception:
            return 0
        for kind in scan(data if isinstance(data, dict) else {}):
            print(kind)
    elif cmd == "record" and len(argv) > 2:
        record(argv[2], now)
    elif cmd == "due" and len(argv) > 2:
        line = due(argv[2], now)
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        sys.exit(0)
