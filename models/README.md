# models/

Build artifacts and test data. Almost everything here is **gitignored** — a
fresh clone shows only this file and `frame_00347.jpg`, so the directory looks
empty until you populate it.

## What belongs here

| File | Source | Notes |
|---|---|---|
| `best.onnx` | trained off-device (Kaggle/Colab), copied over | The input size is baked in; it decides the engine's input edge |
| `model.engine` | built on this box by the entrypoint | Device-specific, not portable — never commit or copy between machines |
| `model.engine.fingerprint` | written by the entrypoint | `onnx hash + TRT version + input size + precision`; delete it to force a rebuild |
| `*.h265` / `*.hevc` | captured from the Pi's stream | Replay input for `--source file` |
| `frame_00347.jpg` | frame pulled from a capture | Tracked, because it is the fixed input for the detection smoke test |

## Test data

- **`pi_sei_sample.hevc`** (4.2 MB) — SMPTE colour-bar test pattern carrying our
  frame_id SEI. 42 frames, ids 1–42. Use it to validate decode and frame_id
  recovery. It contains **no people**, so zero detections is the correct result.
- **`frame_00347.jpg`** — 1920×1080 frame with two people. The detection smoke
  test; expect 0.9238 and 0.9126 with the current engine at threshold 0.35.
- **`range_walk*.h265`, `outdoor_capture.h265`** — field captures from the
  development phase. Usable for replay, but any measurement taken from them
  predates several pipeline fixes; do not quote figures from them without
  re-measuring.
- **`live_capture.h265` and `range_walk_4.h265` are 0 bytes** and cannot be
  replayed.
- **`bench_fp16.engine`, `bench_fp32.engine`** — leftovers from a precision
  benchmark. Not used by anything; safe to delete.

## Rebuilding the engine

The entrypoint does it automatically when the fingerprint does not match:

```bash
rm models/model.engine.fingerprint
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  -e PRECISION=fp16 -e DETECTOR_INPUT=640 \
  -v ~/smart_spotter/orin/models:/models \
  -v ~/smart_spotter/orin/app:/app \
  smart-spotter-orin:dev
```

Engines are tied to this GPU and this TensorRT build. If TensorRT or the driver
changes, the cached engine is stale — the fingerprint catches the TensorRT case,
but not a driver change.
