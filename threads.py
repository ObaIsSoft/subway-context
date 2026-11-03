"""Append-only thread log: one JSON line per event, so a thread graph can be built from records.

A thread is everything that grows from one initiator (a chat id, or a worker started from the HUD).
Records are stored at <base_dir>/threads/<safe_name(thread_id)>.jsonl. This module imports only the
standard library and never imports server, so either side can change without a cycle.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional

try:
    import fcntl
except ImportError:  # Windows: each write is still one atomic append, but not locked across processes.
    fcntl = None  # type: ignore[assignment]

THREADS_SUBDIR = "threads"
KINDS = {
    "spawned", "followed", "rerouted", "approved", "rejected", "started", "stopped", "finished",
    "escalated", "permission_asked", "permission_answered", "note", "terminal_run",
}
EDGE_KINDS = {"spawned", "followed", "rerouted", "note", "terminal_run"}

STRING_CAP = 500
LIST_CAP = 50


def safe_name(thread_id: str) -> str:
    """A file name for any thread id: everything outside [A-Za-z0-9_-] becomes _, plus a hash of the id."""
    tid = str(thread_id or "")
    if not tid:
        raise ValueError("thread id is required")
    digest = hashlib.sha1(tid.encode("utf-8")).hexdigest()[:8]
    return f"{re.sub(r'[^A-Za-z0-9_-]', '_', tid)[:80]}-{digest}"


def _threads_dir(base_dir: str) -> str:
    path = os.path.join(base_dir, THREADS_SUBDIR)
    os.makedirs(path, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def thread_path(base_dir: str, thread_id: str) -> str:
    return os.path.join(_threads_dir(base_dir), f"{safe_name(thread_id)}.jsonl")


def _cap_string(value: str) -> str:
    return value if len(value) <= STRING_CAP else value[:STRING_CAP] + "…"


def _capped(value: Any, _depth: int = 0) -> Any:
    """Copy data with every nested string capped and every list trimmed, so one record stays small."""
    if _depth > 8:
        return "…"
    if isinstance(value, str):
        return _cap_string(value)
    if isinstance(value, dict):
        return {str(k)[:200]: _capped(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_capped(v, _depth + 1) for v in list(value)[:LIST_CAP]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _cap_string(str(value))


def record(base_dir: str, thread_id: str, kind: str, node: str, peer: str = "", data: Optional[Dict[str, Any]] = None,
           at: Optional[float] = None) -> None:
    """Append one record to the thread's log. Raises ValueError for an unknown kind or an empty thread id."""
    if kind not in KINDS:
        raise ValueError(f"unknown thread record kind '{kind}'")
    line = json.dumps({
        "v": 1,
        "thread": str(thread_id),
        "at": at if at is not None else time.time(),
        "kind": kind,
        "node": str(node or ""),
        "peer": str(peer or ""),
        "data": _capped(dict(data or {})),
    }, ensure_ascii=False) + "\n"
    payload = line.encode("utf-8")
    path = thread_path(base_dir, thread_id)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, payload)
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_lines(path: str) -> List[Dict[str, Any]]:
    """Every valid record in file order. Blank, partial or corrupt lines are skipped, never raised."""
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "rb") as f:
            chunks = f.read().split(b"\n")
    except OSError:
        return out
    for raw in chunks:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _empty(thread_id: str) -> Dict[str, Any]:
    return {"thread": str(thread_id), "records": [], "nodes": {}, "edges": []}


def read_thread(base_dir: str, thread_id: str, limit: int = 5000) -> Dict[str, Any]:
    """The last `limit` records, the node summary and the edges (records that point at a peer)."""
    if not str(thread_id or ""):
        raise ValueError("thread id is required")
    recs = _read_lines(os.path.join(base_dir, THREADS_SUBDIR, f"{safe_name(thread_id)}.jsonl"))
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 5000
    recs = recs[-limit:]
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    for r in recs:
        node = str(r.get("node") or "")
        at = r.get("at")
        kinds = r.get("kind")
        if node:
            entry = nodes.setdefault(node, {"first_at": at, "last_at": at, "kinds": []})
            entry["last_at"] = at
            if kinds not in entry["kinds"]:
                entry["kinds"].append(kinds)
        peer = str(r.get("peer") or "")
        if peer and kinds in EDGE_KINDS:
            nodes.setdefault(peer, {"first_at": at, "last_at": at, "kinds": []})
            edges.append({"kind": kinds, "from": node, "to": peer, "at": at, "data": r.get("data") or {}})
    return {"thread": str(thread_id), "records": recs, "nodes": nodes, "edges": edges}


def list_threads(base_dir: str) -> List[Dict[str, Any]]:
    """One entry per thread file, newest first. The thread id comes from the first valid line."""
    folder = os.path.join(base_dir, THREADS_SUBDIR)
    if not os.path.isdir(folder):
        return []
    out: List[Dict[str, Any]] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(folder, name)
        recs = _read_lines(path)
        if not recs:
            continue
        thread_id = ""
        for r in recs:
            if isinstance(r.get("thread"), str) and r["thread"]:
                thread_id = r["thread"]
                break
        if not thread_id:
            continue
        updated = max((r.get("at") for r in recs if isinstance(r.get("at"), (int, float))), default=0.0)
        out.append({"thread": thread_id, "records": len(recs), "updated_at": updated})
    out.sort(key=lambda t: (t["updated_at"], t["thread"]), reverse=True)
    return out
