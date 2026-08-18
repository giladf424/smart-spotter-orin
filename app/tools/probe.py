#!/usr/bin/env python3
"""
Decode probe: validates NVDEC decode + frame_id association end to end.

Two modes:
  --file <path.hevc>   decode the captured sample offline (no Pi needed)
  --live [--port N]    decode the live RTP/H265 stream from the Pi

For each decoded frame it records (frame_id, width, height). At EOS (file mode)
it prints a summary and checks the recovered frame_id sequence against the
expectation that it is contiguous and 1:1 with frames — proving:
  (a) NVDEC decodes the Pi's Main/L4.0/420 stream,
  (b) we recover every frame_id by our UUID (x265's SEI rejected),
  (c) the encoded-side SEI <-> decoded-frame ordering holds with no drift.

This closes the Pi's Q6 (SEI survives our path) and Q7 (format negotiates).
"""

import argparse
import sys
from pathlib import Path

# Scripts here run with tools/ on sys.path, not the app root. Add the root so
# the pipeline packages resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.pipeline import FrameIngest  # noqa: E402


class _Probe:
    def __init__(self):
        self.records = []   # (frame_id, w, h)

    def on_frame(self, frame_id, frame_bgr):
        h, w = frame_bgr.shape[:2]
        self.records.append((frame_id, w, h))
        # Per-frame trace on stderr, so piped stdout stays clean.
        print(f"[probe] frame {len(self.records):>4}  "
              f"frame_id={frame_id}  {w}x{h}", file=sys.stderr)

    def summary(self):
        n = len(self.records)
        ids = [r[0] for r in self.records]
        missing = sum(1 for i in ids if i is None)
        dims = {(w, h) for _, w, h in self.records}
        print("\n=== probe summary ===")
        print(f"frames decoded:      {n}")
        print(f"frame_ids recovered: {n - missing}/{n}  (missing={missing})")
        print(f"frame dimensions:    {dims}")
        if ids and all(i is not None for i in ids):
            contiguous = ids == list(range(ids[0], ids[0] + n))
            print(f"ids: first={ids[0]} last={ids[-1]} "
                  f"contiguous={contiguous}")
            if not contiguous:
                # Show where it breaks for debugging.
                for k in range(1, len(ids)):
                    if ids[k] != ids[k - 1] + 1:
                        print(f"  break at frame {k}: {ids[k-1]} -> {ids[k]}")
                        break
        print("=====================")


def main(argv=None):
    p = argparse.ArgumentParser(description="NVDEC + frame_id decode probe.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="decode a captured .hevc sample offline")
    g.add_argument("--live", action="store_true",
                   help="decode the live RTP stream")
    p.add_argument("--port", type=int, default=5600,
                   help="UDP port for --live")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    args = p.parse_args(argv)

    probe = _Probe()
    if args.file:
        ing = FrameIngest(probe.on_frame, source="file", file_path=args.file,
                          width=args.width, height=args.height)
    else:
        ing = FrameIngest(probe.on_frame, source="live", udp_port=args.port,
                          width=args.width, height=args.height)

    try:
        ing.run()
    except KeyboardInterrupt:
        ing.stop()
    probe.summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
