# Copyright 2026 LanEx Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""KLayout in worker processes, so the server never freezes while it draws.

KLayout's Python module keeps the interpreter lock for the whole of a call
into its C++ (a render, a DEF read, a merge): two threads rendering two views
take as long as one after the other, and while one renders no other request of
the server runs, not even a live run's event stream. A lock per session cannot
fix that; a separate process can. So ``layoutview`` sends its work to a worker
process (``python -m lanex.controller.layoutworker``) and waits on a pipe,
which leaves the interpreter free for every other request.

Protocol: length-prefixed pickles over the worker's stdin and a private copy
of its stdout. Its fd 1 is pointed at the log (fd 2) at start, so anything
KLayout prints lands in ``~/.lanex/logs/layout-<role>.log`` instead of
corrupting the protocol. The worker exits when the server closes the pipe.
"""

from __future__ import annotations

import os
import pickle
import select
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_HEAD = struct.Struct(">I")
_LOG_CAP = 2 * 1024 * 1024


class WorkerError(RuntimeError):
    """The worker process stopped (crashed or was killed) during a call."""


class Cancelled(RuntimeError):
    """The call was cancelled on purpose (the worker was stopped for it)."""


def _read_exact(f: Any, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise EOFError("pipe closed")
        buf += chunk
    return bytes(buf)


def send(f: Any, obj: Any) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    f.write(_HEAD.pack(len(data)) + data)
    try:
        f.flush()
    except Exception:
        pass


def recv(f: Any) -> Any:
    (n,) = _HEAD.unpack(_read_exact(f, _HEAD.size))
    return pickle.loads(_read_exact(f, n))


# Exceptions that keep their type across the pipe (the callers tell them apart).
_KINDS = {"ValueError": ValueError, "LookupError": LookupError, "KeyError": KeyError,
          "FileNotFoundError": FileNotFoundError, "TimeoutError": TimeoutError}


def _rebuild(err: Tuple[str, str]) -> BaseException:
    kind, msg = err
    return _KINDS.get(kind, RuntimeError)(msg)


def _log_path(role: str) -> Path:
    try:
        from . import platform_env
        d = platform_env.home() / "logs"
    except Exception:
        d = Path.home() / ".lanex" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("layout-" + role + ".log")


class Worker:
    """One worker process. Calls are serialised (the worker is one thread);
    a dead worker is started again by the next call."""

    def __init__(self, role: str):
        self.role = role
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._busy: Optional[str] = None
        self._busy_key: Any = None
        self._cancelled = False
        self.started = 0
        self.log = _log_path(role)

    # -- process
    def _spawn(self) -> subprocess.Popen:
        try:
            if self.log.is_file() and self.log.stat().st_size > _LOG_CAP:
                self.log.write_text("", encoding="utf-8")
        except OSError:
            pass
        logf = open(self.log, "ab")
        try:
            logf.write(("--- layout worker '%s' started %s\n" % (self.role, time.strftime("%Y-%m-%d %H:%M:%S"))).encode())
            logf.flush()
            env = os.environ.copy()
            env["LANEX_LAYOUT_ROLE"] = self.role
            # The parent's import path (a checkout on PYTHONPATH, the venv).
            env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p) if not env.get("PYTHONPATH") \
                else env["PYTHONPATH"]
            proc = subprocess.Popen(  # noqa: S603 - our own module, our own interpreter
                [sys.executable, "-m", "lanex.controller.layoutworker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf,
                bufsize=0, env=env, close_fds=True)
        finally:
            logf.close()
        self.started = time.time()
        return proc

    def _ensure(self) -> subprocess.Popen:
        p = self._proc
        if p is None or p.poll() is not None:
            p = self._proc = self._spawn()
        return p

    def _drop(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass

    # -- calls
    def call(self, op: str, kw: Dict[str, Any], timeout: float = 120.0, key: Any = None) -> Any:
        with self._lock:
            proc = self._ensure()
            self._busy, self._busy_key, self._cancelled = op, key, False
            try:
                send(proc.stdin, (op, kw))
                deadline = time.time() + timeout
                while True:
                    left = deadline - time.time()
                    if left <= 0:
                        self._drop()
                        raise TimeoutError(op + " took longer than %d s" % int(timeout))
                    r, _w, _x = select.select([proc.stdout], [], [], min(left, 1.0))
                    if r:
                        break
                    if proc.poll() is not None:
                        raise EOFError("worker exited")
                ok, val = recv(proc.stdout)
            except (EOFError, BrokenPipeError, OSError, pickle.UnpicklingError, struct.error):
                cancelled = self._cancelled
                self._drop()
                if cancelled:
                    raise Cancelled(op + " was cancelled")
                raise WorkerError("the layout engine stopped while working on this (its log is " + str(self.log) + ")")
            finally:
                self._busy, self._busy_key = None, None
        if ok:
            return val
        raise _rebuild(val)

    def busy(self) -> Optional[Tuple[str, Any]]:
        return (self._busy, self._busy_key) if self._busy else None

    def cancel(self, op: Optional[str] = None, key: Any = None) -> bool:
        """Stop the call in flight (when it is *op* for *key*, if given) by
        killing the process; its sessions are reopened on the next call."""
        if not self._busy or (op and self._busy != op) or (key is not None and self._busy_key != key):
            return False
        self._cancelled = True
        p = self._proc
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
        return True

    def stop(self) -> None:
        with self._lock:
            p, self._proc = self._proc, None
        if p is not None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def main() -> int:
    """The worker loop: one call at a time, answers in order."""
    inp = os.fdopen(0, "rb", buffering=0)
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)            # stray prints (KLayout's warnings) go to the log
    sys.stdout = sys.stderr
    from lanex.controller import layoutview as lv

    while True:
        try:
            op, kw = recv(inp)
        except EOFError:
            return 0
        try:
            res: Tuple[bool, Any] = (True, lv._serve(op, kw))
        except BaseException as ex:  # noqa: BLE001 - every error goes back to the caller
            res = (False, (type(ex).__name__, str(ex)))
        try:
            send(out, res)
        except (BrokenPipeError, OSError):
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
