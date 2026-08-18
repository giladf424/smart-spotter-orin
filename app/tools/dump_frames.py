#!/usr/bin/env python3
"""Draw a replay's detections onto the frames they came from, for visual audit.

Decodes the chosen frame_ids out of a captured .h265 and writes one annotated
JPEG per frame. Runs inside the container:

    python3 /app/tools/dump_frames.py <capture.h265> <picks.json> <out_dir>

picks.json maps frame_id (string) to a detections list in the wire format
({"confidence": ..., "bbox": {"x","y","width","height"}, ...}) — entries taken
straight from a replay's per-frame JSON output.
"""

import argparse
import json
import sys
from pathlib import Path

import cv2

# Scripts here run with tools/ on sys.path, not the app root. Add the root so
# the pipeline packages resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.pipeline import FrameIngest  # noqa: E402

# Box colour bands, BGR. The lower bound is the detector's confidence
# threshold, so anything red would have been dropped by the live pipeline.
_CONF_STRONG = 0.5
_CONF_MARGINAL = 0.35
_COLOR_STRONG = (0, 255, 0)
_COLOR_MARGINAL = (0, 255, 255)
_COLOR_WEAK = (0, 0, 255)


def _band_color(confidence):
    if confidence >= _CONF_STRONG:
        return _COLOR_STRONG
    if confidence >= _CONF_MARGINAL:
        return _COLOR_MARGINAL
    return _COLOR_WEAK


def _annotate(frame_bgr, detections):
    """Return a copy of the frame with one labelled box per detection."""
    img = frame_bgr.copy()
    for det in detections:
        box = det["bbox"]
        x, y, w, h = box["x"], box["y"], box["width"], box["height"]
        conf = det["confidence"]
        color = _band_color(conf)
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)
        cv2.putText(img, f"{conf:.2f}", (x, max(y - 8, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    return img


class _Dumper:
    """Writes an annotated JPEG per wanted frame_id, then stops decoding."""

    def __init__(self, picks, out_dir):
        self.picks = picks
        self.remaining = set(picks)
        self.out_dir = out_dir
        self.ingest = None

    def on_frame(self, frame_id, frame_bgr):
        if frame_id not in self.remaining:
            return
        self.remaining.discard(frame_id)
        out_path = self.out_dir / f"frame_{frame_id}.jpg"
        cv2.imwrite(str(out_path), _annotate(frame_bgr, self.picks[frame_id]))
        print(f"saved {out_path}")
        if not self.remaining:
            self.ingest.stop()

    def run(self, capture_path):
        self.ingest = FrameIngest(self.on_frame, source="file",
                                  file_path=capture_path)
        self.ingest.run()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Save annotated JPEGs for chosen frame_ids of a capture.")
    parser.add_argument("capture", help="captured .h265 to decode")
    parser.add_argument("picks", help="JSON mapping frame_id -> detections")
    parser.add_argument("out_dir", help="directory to write JPEGs into")
    args = parser.parse_args(argv)

    with open(args.picks) as f:
        picks = {int(k): v for k, v in json.load(f).items()}

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dumper = _Dumper(picks, out_dir)
    dumper.run(args.capture)

    if dumper.remaining:
        print(f"not found in capture: {sorted(dumper.remaining)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
