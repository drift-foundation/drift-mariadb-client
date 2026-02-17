#!/usr/bin/env python3
"""
Extract deterministic MariaDB packet fixtures from wire-capture proxy output.

Input:
  tests/fixtures/scenarios/bin/<scenario>/<run-id>/
    - events.jsonl
    - 0000_c2s.bin, 0001_s2c.bin, ...

Output:
  tests/fixtures/packetized/<scenario>/<run-id>/
    - manifest.json
    - c2s_stream.bin
    - s2c_stream.bin
    - c2s_packets.json
    - s2c_packets.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _read_events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError("events.jsonl line is not an object")
        out.append(obj)
    out.sort(key=lambda e: int(e["index"]))
    return out


def _build_stream(run_dir: Path, events: list[dict[str, Any]], direction: str) -> bytes:
    chunks: list[bytes] = []
    for ev in events:
        if ev.get("direction") != direction:
            continue
        fname = ev.get("file")
        if not isinstance(fname, str):
            raise ValueError(f"invalid event file entry: {ev}")
        data = (run_dir / fname).read_bytes()
        chunks.append(data)
    return b"".join(chunks)


def _looks_like_tls_record_prefix(stream: bytes, off: int) -> bool:
    if off + 5 <= len(stream):
        content_type = stream[off]
        major = stream[off + 1]
        minor = stream[off + 2]
        if content_type in (20, 21, 22, 23) and major == 3 and minor in (0, 1, 2, 3, 4):
            return True
    # Common case when TLS bytes were mis-read as a MariaDB packet header:
    # parser advanced by 4 bytes, so TLS record starts at off-4.
    if off >= 4 and off + 1 <= len(stream):
        pos = off - 4
        if pos + 5 > len(stream):
            return False
        content_type = stream[pos]
        major = stream[pos + 1]
        minor = stream[pos + 2]
        if content_type in (20, 21, 22, 23) and major == 3 and minor in (0, 1, 2, 3, 4):
            return True
        return False
    return False


def _packetize(stream: bytes, direction: str) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    off = 0
    while off < len(stream):
        if off + 4 > len(stream):
            raise ValueError(f"truncated header at offset {off}")
        b0 = stream[off]
        b1 = stream[off + 1]
        b2 = stream[off + 2]
        seq = stream[off + 3]
        payload_len = b0 + (b1 << 8) + (b2 << 16)
        off += 4
        if off + payload_len > len(stream):
            if _looks_like_tls_record_prefix(stream, off):
                raise ValueError(
                    f"non-plain stream detected in {direction} at offset {off} (looks like TLS record). "
                    "Capture with TLS disabled for packet replay (for mariadb CLI use --ssl=OFF)."
                )
            raise ValueError(
                f"truncated payload at offset {off} in {direction}: need {payload_len}, have {len(stream) - off}"
            )
        payload = stream[off : off + payload_len]
        off += payload_len
        packets.append(
            {
                "sequence_id": seq,
                "payload_len": payload_len,
                "payload_hex": payload.hex(),
            }
        )
    return packets


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Convert captured wire chunks into packetized fixture files"
    )
    p.add_argument("--scenario", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument(
        "--scenarios-root", default="tests/fixtures/scenarios/bin", help="capture root"
    )
    p.add_argument(
        "--output-root",
        default="tests/fixtures/packetized",
        help="packetized output root",
    )
    args = p.parse_args()

    run_dir = Path(args.scenarios_root) / args.scenario / args.run_id
    if not run_dir.exists():
        print(f"error: missing run dir: {run_dir}", file=sys.stderr)
        return 2
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        print(f"error: missing events file: {events_path}", file=sys.stderr)
        return 2

    try:
        events = _read_events(events_path)
        c2s_stream = _build_stream(run_dir, events, "c2s")
        s2c_stream = _build_stream(run_dir, events, "s2c")
        c2s_packets = _packetize(c2s_stream, "c2s")
        s2c_packets = _packetize(s2c_stream, "s2c")
    except Exception as ex:
        print(f"error: extract failed: {ex}", file=sys.stderr)
        return 1

    out_dir = Path(args.output_root) / args.scenario / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "c2s_stream.bin").write_bytes(c2s_stream)
    (out_dir / "s2c_stream.bin").write_bytes(s2c_stream)
    _write_json(out_dir / "c2s_packets.json", {"packets": c2s_packets})
    _write_json(out_dir / "s2c_packets.json", {"packets": s2c_packets})
    _write_json(
        out_dir / "manifest.json",
        {
            "scenario": args.scenario,
            "run_id": args.run_id,
            "source_run_dir": str(run_dir),
            "c2s_packets": len(c2s_packets),
            "s2c_packets": len(s2c_packets),
        },
    )
    print(f"[wire-fixture] wrote {out_dir}")
    print(f"[wire-fixture] c2s packets: {len(c2s_packets)}")
    print(f"[wire-fixture] s2c packets: {len(s2c_packets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
