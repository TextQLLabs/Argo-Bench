"""A persistent Python interpreter driven over stdin/stdout — what `run_python` executes in.

Stdlib only, so it runs under any interpreter: the sandbox image's Python 3.11 or a local
venv. The parent writes one JSON request per line; the kernel answers each with one JSON
line and keeps every variable, import and loaded dataframe for the next request, the way a
notebook does.

The protocol owns the process's real stdin and stdout; user code gets neither. At startup
the kernel duplicates both descriptors for itself, then points fd 0 at /dev/null and fds 1
and 2 at a log file. A C extension or a subprocess that writes straight to a descriptor
therefore cannot corrupt a reply, and code that calls ``input()`` reads EOF rather than the
next request. Python-level output is captured per request, stdout and stderr interleaved
in the order they were written.

A kernel in a container has no filesystem in common with the host, so files travel in the
protocol itself (`argo_bench.servers.python.SyncedKernel`): a request may carry ``files``
(relative path -> base64 content) written before the cell runs, and ``journal_from`` (a byte
offset) asks for what the Mission Control journal gained since, returned as ``journal``.
"""

from __future__ import annotations

import ast
import base64
import builtins
import contextlib
import io
import json
import linecache
import os
import sys
import traceback

LOG_FILE = os.environ.get("KERNEL_LOG", "kernel.log")
JOURNAL = "mission_control_actions.jsonl"


def _cell_frames(tb):
    """Drop the kernel's own frames, so a traceback starts at the agent's code."""
    while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
        tb = tb.tb_next
    return tb


def run_cell(code: str, namespace: dict, name: str) -> str | None:
    """Execute one cell. Returns a formatted traceback, or None when it succeeded.

    A trailing bare expression is evaluated and its repr printed — `df.head()` as the last
    line shows the frame, exactly as it would in a notebook.
    """
    linecache.cache[name] = (len(code), None, code.splitlines(keepends=True), name)
    try:
        tree = ast.parse(code, filename=name, mode="exec")
        tail = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tail = ast.Expression(tree.body.pop().value)
        exec(compile(tree, name, "exec"), namespace)
        if tail is not None:
            value = eval(compile(tail, name, "eval"), namespace)
            if value is not None:
                print(repr(value))
        return None
    except BaseException as exc:  # SystemExit included: exit() must not end the session
        if isinstance(exc, KeyboardInterrupt):
            raise
        return "".join(traceback.format_exception(type(exc), exc, _cell_frames(
            exc.__traceback__)))


def format_cell(output: str, error: str | None, max_output: int) -> str:
    """What `run_python` returns for one cell: its output, then its traceback, with the
    middle omitted past ``max_output`` characters."""
    text = output + (("\n" if output and not output.endswith("\n") else "") + error
                     if error else "")
    if not text.strip():
        return "(no output)"
    if len(text) > max_output:
        head, tail = max_output // 3, max_output - max_output // 3
        omitted = len(text) - head - tail
        text = (f"{text[:head]}\n[... {omitted:,} characters of output omitted ...]\n"
                f"{text[-tail:]}")
    return text


def write_files(files: dict) -> None:
    """Files the host sent with a request, each under the working directory."""
    root = os.path.realpath(os.getcwd())
    for rel, content in (files or {}).items():
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(root + os.sep):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as handle:
            handle.write(base64.b64decode(content))
        os.replace(path + ".part", path)


def journal_since(offset: int) -> tuple[str, int]:
    """What the journal gained past ``offset`` (whole lines only), and the new offset."""
    try:
        with open(JOURNAL, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return "", offset
    data = data[:data.rfind(b"\n") + 1]
    return data.decode("utf-8", "replace"), offset + len(data)


def main() -> int:
    requests = os.fdopen(os.dup(0), "r", encoding="utf-8")
    replies = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - process lifetime
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdin = io.StringIO("")

    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    namespace: dict = {"__name__": "__main__", "__builtins__": builtins}

    replies.write(json.dumps({"ready": True, "python": sys.version.split()[0],
                              "cwd": os.getcwd()}) + "\n")
    count = 0
    for line in requests:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            replies.write(json.dumps({"error": "malformed request"}) + "\n")
            continue
        count += 1
        write_files(request.get("files"))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            error = run_cell(str(request.get("code") or ""), namespace, f"<cell {count}>")
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        reply = {"id": request.get("id"), "output": buffer.getvalue(), "error": error}
        if "journal_from" in request:
            reply["journal"], reply["journal_to"] = journal_since(int(request["journal_from"]))
        replies.write(json.dumps(reply, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
