"""Decode selected frame_ids from a captured .h265 and save JPEGs with the
detection boxes from a prior replay drawn on them. Runs inside the container:
    python3 dump_frames.py <capture.h265> <picks.json> <out_dir>

picks.json maps frame_id (string) -> detections list in the wire format
({"confidence": ..., "bbox": {"x","y","width","height"}, ...}), i.e. entries
taken directly from a replay's per-frame JSON output.
"""
import json
import sys

import cv2

sys.path.insert(0, "/app")
from ingest import FrameIngest

capture, picks_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
picks = {int(k): v for k, v in json.load(open(picks_path)).items()}
remaining = set(picks)


def on_frame(frame_id, frame_bgr):
    if frame_id not in remaining:
        return
    remaining.discard(frame_id)
    img = frame_bgr.copy()
    for d in picks[frame_id]:
        b = d["bbox"]
        x, y, w, h = b["x"], b["y"], b["width"], b["height"]
        # Color by confidence band: green >=0.5, yellow 0.35-0.5, red <0.35,
        # so detected-vs-marginal is visible at a glance in audit images.
        c = d["confidence"]
        color = (0, 255, 0) if c >= 0.5 else (0, 255, 255) if c >= 0.35 else (0, 0, 255)
        cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)
        cv2.putText(img, f"{c:.2f}", (x, max(y - 8, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    out = f"{out_dir}/frame_{frame_id}.jpg"
    cv2.imwrite(out, img)
    print(f"saved {out}")
    if not remaining:
        ing.stop()


ing = FrameIngest(on_frame, source="file", file_path=capture)
ing.run()
