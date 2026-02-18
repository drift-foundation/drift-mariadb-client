#!/usr/bin/env python3
"""
Write reproducible SQL transcripts for packetized wire fixtures.

For each packetized run directory, this tool reads c2s_packets.json and extracts
COM_QUERY payloads (command byte 0x03), then writes scenario.sql in:
  - the packetized run dir
  - the source scenario run dir (if present in manifest.json)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def decode_query_from_payload_hex(payload_hex: str) -> str | None:
    if len(payload_hex) < 2:
        return None
    try:
        b = bytes.fromhex(payload_hex)
    except ValueError:
        return None
    if not b or b[0] != 0x03:
        return None
    query = b[1:].decode("utf-8", errors="strict").strip()
    if not query:
        return None
    return query


def render_sql(scenario: str, run_id: str, queries: list[str]) -> str:
    out: list[str] = []
    out.append(f"-- Scenario: {scenario}")
    out.append(f"-- Run: {run_id}")
    out.append("-- Extracted from COM_QUERY packets in fixture capture.")
    out.append("")
    if not queries:
        out.append("-- No COM_QUERY packets found in this scenario.")
        out.append("-- This is expected for handshake-only captures.")
        out.append("")
        return "\n".join(out)
    for q in queries:
        # Keep explicit statement form to make replay easy with mariadb -e or input script.
        if q.endswith(";"):
            out.append(q)
        else:
            out.append(q + ";")
    out.append("")
    return "\n".join(out)


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def process_run_dir(run_dir: Path) -> tuple[int, int]:
    c2s_path = run_dir / "c2s_packets.json"
    manifest_path = run_dir / "manifest.json"
    if not c2s_path.exists() or not manifest_path.exists():
        return (0, 0)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scenario = str(manifest.get("scenario", run_dir.parent.name))
    run_id = str(manifest.get("run_id", run_dir.name))

    c2s = json.loads(c2s_path.read_text(encoding="utf-8"))
    packets = c2s.get("packets", [])
    queries: list[str] = []
    for p in packets:
        payload_hex = p.get("payload_hex")
        if not isinstance(payload_hex, str):
            continue
        q = decode_query_from_payload_hex(payload_hex)
        if q is not None:
            queries.append(q)

    content = render_sql(scenario, run_id, queries)
    writes = 0
    targets = [run_dir / "scenario.sql"]

    source_run_dir = manifest.get("source_run_dir")
    if isinstance(source_run_dir, str) and source_run_dir.strip():
        targets.append(Path(source_run_dir) / "scenario.sql")

    for t in targets:
        t.parent.mkdir(parents=True, exist_ok=True)
        if write_if_changed(t, content):
            writes += 1
    return (1, writes)


def iter_run_dirs(packetized_root: Path, scenario: str | None, run_id: str | None):
    if scenario and run_id:
        yield packetized_root / scenario / run_id
        return
    if scenario:
        scen_dir = packetized_root / scenario
        if not scen_dir.exists():
            return
        for d in sorted(p for p in scen_dir.iterdir() if p.is_dir()):
            yield d
        return
    for scen_dir in sorted(p for p in packetized_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in scen_dir.iterdir() if p.is_dir()):
            yield run_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packetized-root", default="tests/fixtures/packetized")
    ap.add_argument("--scenario")
    ap.add_argument("--run-id")
    args = ap.parse_args()

    packetized_root = Path(args.packetized_root)
    if not packetized_root.exists():
        print(f"error: missing packetized root: {packetized_root}")
        return 2

    scanned = 0
    writes = 0
    for run_dir in iter_run_dirs(packetized_root, args.scenario, args.run_id):
        s, w = process_run_dir(run_dir)
        scanned += s
        writes += w
    print(f"[write-scenario-sql] scanned runs: {scanned}")
    print(f"[write-scenario-sql] files written: {writes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
