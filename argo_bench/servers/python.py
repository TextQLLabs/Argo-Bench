"""`run_python`: one persistent Python interpreter per run, as an MCP tool.

The interpreter is a child process (`argo_bench/sandbox/kernel.py`), never this server's
own Python: a cell past its time limit can be killed and the interpreter restarted without
taking the server down, and the interpreter starts without the credentials the warehouse
server and the model provider hold. Where it runs is the ``--backend``:

``local``   a process on this machine with a scrubbed environment, sharing the run's
            working directory. On macOS it runs under Seatbelt (``sandbox/local.sb``): it
            can read only its working directory, its interpreter and the kernel, and write
            only there and to the temp directories.
``docker``  a container of the sandbox image (``sandbox/Dockerfile``), no network.
``k8s``     a pod of the sandbox image (``k8s/templates/sandbox-pod.yaml``): gVisor, no network, no
            service-account token, reached through ``kubectl exec``.

A container shares no filesystem with the host, so `SyncedKernel` sends the working
directory's new files (the console, ``results/*.parquet`` from ``run_sql``) with each cell
and brings back what the Mission Control journal gained. The journal in the host's working
directory is the run's record of what it filed, whatever the backend.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer

from argo_bench.sandbox.kernel import JOURNAL, format_cell

SANDBOX = Path(__file__).resolve().parents[1] / "sandbox"
KERNEL = SANDBOX / "kernel.py"
SEATBELT_PROFILE = SANDBOX / "local.sb"
#: Where the sandbox image keeps the kernel (sandbox/Dockerfile).
IMAGE_KERNEL = "/opt/sandbox/kernel.py"
REPO = Path(__file__).resolve().parents[2]
POD_TEMPLATE = REPO / "k8s" / "templates" / "sandbox-pod.yaml"

RESTARTED = ("[the previous interpreter is gone; this is a fresh one — "
             "re-run imports and reload data]\n")
EXITED = ("The interpreter exited while running this cell (it may have run out of memory). "
          "Its state is gone; the next call starts a fresh one.")

#: The only variables a local kernel inherits. Everything else it gets is passed explicitly.
_INHERITED = ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE",
              "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")

DESCRIPTION = """Run Python in a persistent interpreter: variables, imports and loaded \
data carry over between calls, as in a notebook. Returns everything printed, the value of \
a final bare expression, and any traceback.

The working directory holds `results/`, where every `run_sql` result is saved as Parquet \
(`pandas.read_parquet("results/sql_0003.parquet")`), and `mission_control.py`, so \
`from mission_control import MissionControl, Reason, get_sandbox_id` works here. pandas, \
numpy, polars, scipy, statsmodels, scikit-learn, matplotlib, seaborn and Google OR-Tools \
(`ortools`: linear, integer and constraint programming, routing) are installed."""


class Kernel:
    """A kernel process and the line protocol to it. One call at a time."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str], timeout_s: float,
                 max_output: int) -> None:
        self.argv, self.cwd, self.env = argv, cwd, env
        self.timeout_s, self.max_output = timeout_s, max_output
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue | None = None
        self._lock = threading.Lock()
        self._count = 0
        self._starts = 0

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            bufsize=1, start_new_session=True)
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()
        self._starts += 1
        ready = self._read(120)
        if not ready or not ready.get("ready"):
            self.kill()
            raise RuntimeError(f"the Python kernel did not start ({ready!r}); command: "
                               f"{shlex.join(self.argv)}")

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def _read(self, timeout_s: float) -> dict | None:
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if line is None:
            return {"exited": True}
        try:
            return json.loads(line)
        except ValueError:
            return {"exited": True, "garbage": line[:200]}

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def kill(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=10)
        self._proc = None

    def execute(self, code: str) -> str:
        with self._lock:
            notice = ""
            if not self.alive():
                if self._starts:
                    notice = RESTARTED
                self._start()
            self._count += 1
            reply = self._roundtrip({"id": self._count, "code": code}, self.timeout_s)
            if reply is None:
                self.kill()
                return notice + (f"Timed out after {self.timeout_s:.0f}s. The interpreter "
                                 f"was stopped and its state (variables, imports, loaded "
                                 f"data) is gone.")
            if reply.get("exited"):
                self.kill()
                return notice + EXITED
            return notice + format_cell(reply.get("output") or "", reply.get("error"),
                                        self.max_output)

    def _roundtrip(self, request: dict, timeout_s: float) -> dict | None:
        self._proc.stdin.write(json.dumps(request) + "\n")
        self._proc.stdin.flush()
        return self._read(timeout_s)

    def close(self) -> None:
        self.kill()


class SyncedKernel(Kernel):
    """A kernel in a container: files travel in the protocol (see `kernel.py`).

    ``launch(name)`` returns the command that attaches to a fresh interpreter called
    ``name`` (creating its container or pod first if need be); ``stop(name)`` removes it.
    A restarted interpreter is a new container, so everything is sent to it again.
    """

    NOT_SENT = frozenset({JOURNAL, "kernel.log"})

    def __init__(self, cwd: Path, env: dict[str, str], timeout_s: float, max_output: int,
                 prefix: str, launch: Callable[[str], list[str]],
                 stop: Callable[[str], None]) -> None:
        super().__init__([], cwd, env, timeout_s, max_output)
        self.prefix, self.launch, self.stop = prefix, launch, stop
        self.name = ""
        self._sent: dict[str, tuple[int, int]] = {}
        self._journal_at = 0

    def _start(self) -> None:
        self.name = f"{self.prefix}-{self._starts + 1}"
        self._sent, self._journal_at = {}, 0
        try:
            self.argv = self.launch(self.name)
        except Exception:
            self._remove()
            raise
        super()._start()

    def _changed(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(self.cwd.rglob("*")):
            rel = path.relative_to(self.cwd).as_posix()
            if not path.is_file() or rel in self.NOT_SENT:
                continue
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            if self._sent.get(rel) != key:
                out[rel] = base64.b64encode(path.read_bytes()).decode("ascii")
                self._sent[rel] = key
        return out

    def _roundtrip(self, request: dict, timeout_s: float) -> dict | None:
        request = {**request, "files": self._changed(), "journal_from": self._journal_at}
        reply = super()._roundtrip(request, timeout_s)
        if reply and reply.get("journal"):
            with open(self.cwd / JOURNAL, "a", encoding="utf-8") as handle:
                handle.write(reply["journal"])
        if reply and "journal_to" in reply:
            self._journal_at = int(reply["journal_to"])
        return reply

    def _remove(self) -> None:
        if self.name:
            with contextlib.suppress(Exception):
                self.stop(self.name)
            self.name = ""

    def kill(self) -> None:
        super().kill()
        self._remove()


# ------------------------------------------------------------------------ backends
def local_kernel(args, cwd: Path, given: dict[str, str]) -> Kernel:
    env = {k: os.environ[k] for k in _INHERITED if k in os.environ}
    env.update({"HOME": str(cwd), "PYTHONPATH": str(cwd), "PYTHONUNBUFFERED": "1",
                "MPLBACKEND": "Agg", "KERNEL_LOG": str(cwd / "kernel.log"), **given})
    argv = [args.python or sys.executable, str(KERNEL)]
    available = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
    if args.seatbelt == "on" and not available:
        raise SystemExit("--seatbelt on: sandbox-exec is not available on this machine")
    if args.seatbelt == "on" or (args.seatbelt == "auto" and available):
        python = Path(argv[0]).absolute()
        params = {"WORKDIR": os.path.realpath(cwd),
                  # The venv is the directory above bin/, taken before resolving symlinks.
                  "VENV": os.path.realpath(python.parent.parent),
                  "KERNEL": os.path.realpath(KERNEL),
                  "CA_FILE": os.path.realpath(env["SSL_CERT_FILE"])
                  if env.get("SSL_CERT_FILE") else "/dev/null"}
        argv = ["sandbox-exec", "-f", str(SEATBELT_PROFILE),
                *(arg for key, value in params.items() for arg in ("-D", f"{key}={value}")),
                *argv]
    return Kernel(argv, cwd, env, args.timeout, args.max_output)


def docker_kernel(args, cwd: Path, given: dict[str, str], prefix: str) -> Kernel:
    def launch(name: str) -> list[str]:
        return ["docker", "run", "-i", "--rm", "--name", name, "--network", "none",
                "--cpus", args.cpus, "--memory", args.memory,
                *(f"-e{key}={value}" for key, value in given.items()), args.image]

    def stop(name: str) -> None:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60,
                       check=False)

    return SyncedKernel(cwd, dict(os.environ), args.timeout, args.max_output, prefix,
                        launch, stop)


def k8s_kernel(args, cwd: Path, given: dict[str, str], prefix: str) -> Kernel:
    kubectl = ["kubectl", "-n", args.k8s_namespace]
    template = string.Template(Path(args.k8s_pod_template).read_text(encoding="utf-8"))

    def launch(name: str) -> list[str]:
        manifest = template.substitute(NAME=name, NAMESPACE=args.k8s_namespace,
                                       IMAGE=args.image, CPU=args.cpus, MEMORY=args.memory)
        subprocess.run([*kubectl, "create", "-f", "-"], input=manifest, text=True,
                       capture_output=True, timeout=120, check=True)
        ready = subprocess.run([*kubectl, "wait", "--for=condition=Ready", f"pod/{name}",
                                "--timeout=300s"], capture_output=True, text=True, check=False)
        if ready.returncode != 0:
            raise RuntimeError(f"sandbox pod {name} did not become ready: "
                               f"{ready.stderr.strip()[-400:]}")
        return [*kubectl, "exec", "-i", name, "-c", "kernel", "--", "env",
                *(f"{key}={value}" for key, value in given.items()),
                "python3", "-u", IMAGE_KERNEL]

    def stop(name: str) -> None:
        subprocess.run([*kubectl, "delete", "pod", name, "--wait=false", "--ignore-not-found"],
                       capture_output=True, timeout=60, check=False)

    return SyncedKernel(cwd, dict(os.environ), args.timeout, args.max_output, prefix,
                        launch, stop)


def build_server(kernel: Kernel) -> MCPServer:
    server = MCPServer("python", instructions=(
        "A persistent Python interpreter for analysis. Query the warehouse with run_sql "
        "and load its saved Parquet here."))

    @server.tool(name="run_python", description=DESCRIPTION, structured_output=False)
    async def run_python(code: str) -> str:
        try:
            return await anyio.to_thread.run_sync(kernel.execute, code)
        except RuntimeError as exc:   # the interpreter could not start: say why
            return f"The Python interpreter could not start: {exc}"

    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cwd", default=".", help="the run's working directory")
    ap.add_argument("--backend", default="local", choices=("local", "docker", "k8s"))
    ap.add_argument("--name", default="argo", help="docker/k8s: container / pod name prefix")
    ap.add_argument("--python", default="", help="local: the interpreter (default: this one)")
    ap.add_argument("--seatbelt", default="auto", choices=("auto", "on", "off"),
                    help="local: confine the kernel with sandbox-exec (macOS)")
    ap.add_argument("--image", default="argo-sandbox", help="docker/k8s: the sandbox image")
    ap.add_argument("--cpus", default="1", help="docker/k8s: CPU limit")
    ap.add_argument("--memory", default="8Gi", help="docker/k8s: memory limit")
    ap.add_argument("--k8s-namespace", default="argo-sandbox")
    ap.add_argument("--k8s-pod-template", default=str(POD_TEMPLATE))
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="a variable the kernel gets (repeatable)")
    ap.add_argument("--timeout", type=float, default=900.0, help="per-cell limit, seconds")
    ap.add_argument("--max-output", type=int, default=30_000,
                    help="characters of output returned per cell")
    args = ap.parse_args(argv)

    cwd = Path(args.cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    given = dict(pair.split("=", 1) for pair in args.env if "=" in pair)
    # Container and pod names: lower-case DNS labels.
    prefix = re.sub(r"[^a-z0-9-]+", "-", args.name.lower()).strip("-")[:50] or "argo"
    if args.backend == "docker":
        args.memory = args.memory.replace("Gi", "g").replace("Mi", "m")
        kernel = docker_kernel(args, cwd, given, prefix)
    elif args.backend == "k8s":
        kernel = k8s_kernel(args, cwd, given, prefix)
    else:
        kernel = local_kernel(args, cwd, given)
    # The MCP client closes stdin and then signals: exit through `finally`, so a container
    # or pod never outlives its run.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    try:
        build_server(kernel).run("stdio")
    finally:
        kernel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
