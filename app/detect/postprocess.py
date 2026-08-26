"""
Pre- and post-processing for the YOLO26 detector.

This module owns all bbox coordinate math. The letterbox applied in
preprocess() and the inverse applied in postprocess() must be exact inverses:
the Pi turns these pixel coordinates into a real-world bearing, so a mismatch
becomes a silent aiming error rather than a visible bug.

Chain:
  preprocess(frame)  -> (nchw_fp32, transform)   # frame: HxWx3 BGR uint8
  engine.infer(...)  -> raw [1,300,6]
  postprocess(raw, transform) -> list[Detection]  # boxes in source pixels

Letterbox: resize keeping aspect ratio, then pad the short side out to a
square of the requested input size. The scale and pad are recorded so
postprocess can undo them exactly. Stretching instead would distort the
geometry the Pi depends on.

YOLO26 output is end-to-end: [1,300,6], each row [x1,y1,x2,y2,conf,class_id]
in xyxy pixels of the letterboxed space. NMS is baked into the graph, so we
run none here. Rows are only filtered by confidence (column 4).
"""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class LetterboxTransform:
    """Records the letterbox so postprocess can invert it exactly.

    A detector-space coordinate (xd, yd) maps back to source space as:
        xs = (xd - pad_x) / scale
        ys = (yd - pad_y) / scale
    """
    scale: float        # uniform resize factor applied to the source frame
    pad_x: float        # left padding added in detector space (pixels)
    pad_y: float        # top padding added in detector space (pixels)
    src_w: int          # source frame width  (for clamping)
    src_h: int          # source frame height (for clamping)


@dataclass
class Detection:
    """One detection in source-frame pixel space (top-left origin, +x right,
    +y down). Coordinates are floats; ZmqSink.build_message rounds them to
    whole pixels when building the wire message."""
    label: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


def preprocess(frame_bgr, input_size):
    """Letterbox a BGR uint8 HxWx3 frame to an NCHW FP32 [1,3,S,S] tensor.

    Returns (tensor, transform). The tensor is RGB, scaled to [0,1] and
    contiguous, matching what Ultralytics YOLO expects, so the current engine
    and any later re-export read their input the same way.
    """
    src_h, src_w = frame_bgr.shape[:2]
    scale = min(input_size / src_w, input_size / src_h)
    new_w = int(round(src_w * scale))
    new_h = int(round(src_h * scale))

    resized = cv2.resize(frame_bgr, (new_w, new_h),
                         interpolation=cv2.INTER_LINEAR)

    # Centre it in the square canvas. 114 is the YOLO pad value, and the -0.1
    # before rounding is Ultralytics' convention for an odd pad: the extra
    # pixel goes to the bottom and the right.
    pad_x = (input_size - new_w) / 2.0
    pad_y = (input_size - new_h) / 2.0
    top = int(round(pad_y - 0.1))
    bottom = input_size - new_h - top
    left = int(round(pad_x - 0.1))
    right = input_size - new_w - left
    canvas = cv2.copyMakeBorder(
        resized, top, bottom, left, right,
        cv2.BORDER_CONSTANT, value=(114, 114, 114),
    )

    # BGR->RGB, HWC->CHW, uint8->float32 [0,1], add batch dim.
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
    chw = rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
    tensor = np.ascontiguousarray(chw[np.newaxis, ...])

    # Record the integer pad actually applied, not the float, so the inverse
    # is exact for boxes the model emits in this canvas.
    transform = LetterboxTransform(
        scale=scale, pad_x=float(left), pad_y=float(top),
        src_w=src_w, src_h=src_h,
    )
    return tensor, transform


def postprocess(raw_output, transform, class_map, conf_threshold):
    """Parse [1,300,6], filter by confidence, un-letterbox to source pixels.

    raw_output: np.ndarray [1,300,6], rows [x1,y1,x2,y2,conf,class_id] in
                detector (letterboxed) pixel space.
    transform:  the LetterboxTransform from preprocess().
    class_map:  {int class_id -> str label}. Detections whose class id is
                missing from the map are dropped.
    conf_threshold: float; rows with conf below this are dropped.

    returns list[Detection] in source pixel space, sorted by confidence desc.
    """
    dets = raw_output.reshape(-1, 6)

    out = []
    inv_scale = 1.0 / transform.scale
    for row in dets:
        conf = float(row[4])
        if conf < conf_threshold:
            continue
        cls_id = int(row[5])
        label = class_map.get(cls_id)
        if label is None:
            continue

        # xyxy in detector space -> source space (exact inverse of letterbox).
        x1 = (float(row[0]) - transform.pad_x) * inv_scale
        y1 = (float(row[1]) - transform.pad_y) * inv_scale
        x2 = (float(row[2]) - transform.pad_x) * inv_scale
        y2 = (float(row[3]) - transform.pad_y) * inv_scale

        # Clamp to source bounds: the model can predict boxes running off the
        # canvas, and the Pi's geometry expects in-frame coordinates.
        x1 = min(max(x1, 0.0), transform.src_w)
        y1 = min(max(y1, 0.0), transform.src_h)
        x2 = min(max(x2, 0.0), transform.src_w)
        y2 = min(max(y2, 0.0), transform.src_h)

        w = x2 - x1
        h = y2 - y1
        if w <= 0.0 or h <= 0.0:
            continue

        out.append(Detection(
            label=label, confidence=conf,
            x=x1, y=y1, width=w, height=h,
        ))

    out.sort(key=lambda d: d.confidence, reverse=True)
    return out
