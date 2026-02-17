#!/usr/bin/env python3
"""
Minimal TCP MITM capture proxy for MariaDB/MySQL wire traffic.

It accepts one client connection, forwards bytes to the target server, and
records every forwarded chunk as a binary file with ordered metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import time
from pathlib import Path


def _now_ns() -> int:
    return time.time_ns()


def _mkdir_run_dir(root: Path, scenario: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    run_id = f"{ts}-{os.getpid()}"
    run_dir = root / scenario / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _write_jsonl_line(path: Path, obj: object) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def _record_chunk(run_dir: Path, idx: int, direction: str, data: bytes, t_ns: int) -> int:
    name = f"{idx:04d}_{direction}.bin"
    out_path = run_dir / name
    out_path.write_bytes(data)
    _write_jsonl_line(
        run_dir / "events.jsonl",
        {
            "index": idx,
            "direction": direction,
            "bytes": len(data),
            "ts_ns": t_ns,
            "file": name,
        },
    )
    return idx + 1


def run_proxy(
    listen_host: str,
    listen_port: int,
    target_host: str,
    target_port: int,
    run_dir: Path,
) -> int:
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_sock.bind((listen_host, listen_port))
    listen_sock.listen(1)
    print(f"[wire-capture] listening on {listen_host}:{listen_port}", flush=True)
    print(f"[wire-capture] forwarding to {target_host}:{target_port}", flush=True)
    print("[wire-capture] waiting for one client connection...", flush=True)

    client_sock, client_addr = listen_sock.accept()
    print(f"[wire-capture] client connected: {client_addr[0]}:{client_addr[1]}", flush=True)
    server_sock = socket.create_connection((target_host, target_port), timeout=10)
    print("[wire-capture] target connected", flush=True)

    client_sock.setblocking(False)
    server_sock.setblocking(False)
    listen_sock.close()

    idx = 0
    total_c2s = 0
    total_s2c = 0
    start_ns = _now_ns()
    exit_code = 0

    try:
        while True:
            readable, _, _ = select.select([client_sock, server_sock], [], [], 0.5)
            if not readable:
                continue
            for sock in readable:
                if sock is client_sock:
                    data = client_sock.recv(65536)
                    if not data:
                        return 0
                    server_sock.sendall(data)
                    t_ns = _now_ns()
                    idx = _record_chunk(run_dir, idx, "c2s", data, t_ns)
                    total_c2s += len(data)
                else:
                    data = server_sock.recv(65536)
                    if not data:
                        return 0
                    client_sock.sendall(data)
                    t_ns = _now_ns()
                    idx = _record_chunk(run_dir, idx, "s2c", data, t_ns)
                    total_s2c += len(data)
    except KeyboardInterrupt:
        print("[wire-capture] interrupted", flush=True)
        exit_code = 130
    finally:
        end_ns = _now_ns()
        try:
            client_sock.close()
        except Exception:
            pass
        try:
            server_sock.close()
        except Exception:
            pass
        _write_json(
            run_dir / "summary.json",
            {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_ms": (end_ns - start_ns) // 1_000_000,
                "chunks": idx,
                "bytes_c2s": total_c2s,
                "bytes_s2c": total_s2c,
                "status": "ok" if exit_code == 0 else "interrupted",
            },
        )
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description="Capture MariaDB wire bytes via TCP proxy")
    p.add_argument("--scenario", required=True, help="Scenario name (folder under output root)")
    p.add_argument("--listen-host", default="127.0.0.1")
    p.add_argument("--listen-port", required=True, type=int)
    p.add_argument("--target-host", default="127.0.0.1")
    p.add_argument("--target-port", required=True, type=int)
    p.add_argument("--output-root", default="tests/fixtures/scenarios/bin")
    args = p.parse_args()

    out_root = Path(args.output_root)
    run_dir = _mkdir_run_dir(out_root, args.scenario)
    _write_json(
        run_dir / "manifest.json",
        {
            "scenario": args.scenario,
            "listen": {"host": args.listen_host, "port": args.listen_port},
            "target": {"host": args.target_host, "port": args.target_port},
            "created_at_ns": _now_ns(),
            "format_version": 1,
            "notes": "Chunk-level TCP capture; files are ordered by events.jsonl index.",
        },
    )
    print(f"[wire-capture] output: {run_dir}", flush=True)
    rc = run_proxy(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        target_host=args.target_host,
        target_port=args.target_port,
        run_dir=run_dir,
    )
    print(f"[wire-capture] done (rc={rc})", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

