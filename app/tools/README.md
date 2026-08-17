# app/tools

Diagnostic scripts, not part of the inference service. Run them directly inside
the container; they are scripts rather than an importable package, so there is no
`__init__.py` here.

- **`probe.py`** — decodes a stream and reports whether every frame carried a
  frame_id and whether the ids are contiguous. The instrument for checking that
  SEI extraction and AU-to-frame pairing hold.

  ```bash
  python3 /app/tools/probe.py --live            # live RTP from the Pi
  python3 /app/tools/probe.py --file /models/pi_sei_sample.hevc
  ```

- **`dump_frames.py`** — decodes chosen frame_ids from a capture and writes JPEGs
  with detection boxes drawn on them, for visual audit of a replay's output.

  ```bash
  python3 /app/tools/dump_frames.py <capture.h265> <picks.json> <out_dir>
  ```
