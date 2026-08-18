#!/usr/bin/env python3
"""
Decode probe: checks NVDEC decode and frame_id pairing end to end.

Two modes:
  --file <path.hevc>   decode a captured sample offline (no Pi needed)
  --live [--port N]    decode the live RTP/H265 stream from the Pi

Records (frame_id, width, height) per decoded frame and prints a summary: how
many frame_ids were recovered, the frame dimensions, and whether the ids are
contiguous. That covers NVDEC decoding the stream, the SEI parser picking our
ids out rather than x265's, and encoded order still matching decoded order.

File mode ends at EOS and prints the summary. Live mode never sees EOS: the
KeyboardInterrupt path below does not fire, because the GLib loop does not
hand SIGINT back to Python, so a killed live run prints no summary — read the
per-frame trace and the heartbeat counters instead.
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
