# CLAUDE.md — Smart Spotter, Orin Inference Node

## What this machine is

This Jetson Orin Nano Super Developer Kit is the **stateless GPU inference
node** of the Smart Sniper Spotter System — an Afeka College software
engineering capstone (2025). The full system is an autonomous pan/tilt
surveillance device: a motorized camera performs serpentine grid scans, detects
persons, locks on with servos, localizes noise sources with a 6-mic array, and
reports to an Android app. Team: Gilad Faibish (CV/detection — the person you're
talking to), Dolev Telem (Android app), Emil Glatter (embedded/Pi-side).

Two compute nodes:
- **Raspberry Pi 5** (Emil's domain, separate Claude instance) — hub, owns the
  camera (Pi HQ IMX477, 16mm lens ~24° HFOV), servo control (2× DFRobot
  SER0049 pan/tilt), all geometry/aiming, sensors, app communication.
- **This Orin** — receives video, runs detection, returns results.
  **Intentionally stateless**: no pose, no servo state, no cross-frame
  tracking. Locked decision — do not add state here; tracking belongs on the
  Pi, because motion-compensated association needs pose.

## Pi ↔ Orin protocol (locked — do not relitigate)

- Direct GigE cable, subnet 10.42.0.0/24. Orin = 10.42.0.2 on `enP8p1s0`,
  Pi = 10.42.0.1.
- Pi streams **H.265/RTP over UDP** (not RTSP) to port 5600.
- Pi stamps each frame with `frame_id` via **SEI NAL**; the Orin extracts it and
  echoes it back with detections. The Pi keeps a `frame_id→pose` ring buffer and
  does all geometry locally.
- Detections egress: **ZMQ PUSH (Orin) → PULL (Pi)** at `tcp://10.42.0.1:5556`.
- Wire schema: `{type, frame_id, timestamp_ms, detections: [{id, class,
  confidence, bbox}]}`. Detection `id`s are 1-based strings, per-frame only —
  the Pi owns identity across frames. Capped at 32 detections per message.

### Two open wire-contract questions (handoff sent 2026-08-18, no reply yet)

- Frames whose SEI yields no frame_id are sent as `frame_id: 0`, which the Pi
  cannot distinguish from a real frame 0.
- `timestamp_ms` is `time.monotonic()` — arbitrary epoch, not comparable to any
  Pi clock.

Do not change either without Emil. Details in the deferred-fix memories.

## Layout

```
app/
  config.py            all tunables; the one file a model swap touches
  infer.py             orchestrator + CLI
  ingest/  sei.py      frame_id extraction from the H.265 bitstream
           pipeline.py GStreamer/NVDEC receive + frame_id↔frame pairing
  detect/  engine.py   TensorRT engine I/O
           postprocess.py letterbox + box math
  egress/  zmq_sink.py wire contract and PUSH socket
  tools/   probe.py, dump_frames.py — diagnostics, not the service
docker/                Dockerfile, entrypoint, host CDI strip script
docs/environment/      host/container runbooks (not pipeline code)
models/                ONNX, engines, captures — all gitignored except one JPEG
```

Scripts in `app/tools/` add the app root to `sys.path` themselves, since
running a file in a subdirectory does not put its parent on the path.

## Commands

```bash
# Build the image
docker build -t smart-spotter-orin:dev ~/smart_spotter/orin/docker

# Full service (entrypoint builds-or-reuses the engine, then runs infer.py)
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  -e PRECISION=fp16 -e DETECTOR_INPUT=640 \
  --network host \
  -v ~/smart_spotter/orin/models:/models \
  -v ~/smart_spotter/orin/app:/app \
  smart-spotter-orin:dev

# Dev iteration: skip the entrypoint, use the cached engine
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all --network host \
  -v ~/smart_spotter/orin/models:/models -v ~/smart_spotter/orin/app:/app \
  --entrypoint python3 smart-spotter-orin:dev \
  /app/infer.py --engine /models/model.engine --source live

# GPU sanity check — first thing after any reboot
docker run --rm --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --entrypoint python3 smart-spotter-orin:dev \
  -c "from cuda.bindings import runtime as cudart; print(cudart.cudaGetDeviceCount())"
#   want: (<cudaError_t.cudaSuccess: 0>, 1)
```

Add `-e PYTHONDONTWRITEBYTECODE=1` to any dev run: the container runs as root,
so it otherwise leaves root-owned `__pycache__` dirs in the mounted source that
you then need sudo to delete.

`infer.py` modes: `--test-image <jpg>` (one still → JSON on stdout);
`--source file --file <path>` (offline replay); `--source live [--port 5600]`
(live RTP, ZMQ push implicit; `--push` forces it in file mode). The engine's
input edge wins over `--input-size`. Entrypoint knobs via `-e`: `PRECISION`
(fp16|fp32), `DETECTOR_INPUT`, `ONNX_PATH`, `ENGINE_PATH`. Force a rebuild by
deleting `models/model.engine.fingerprint`.

## Verifying a change

No test suite. Validation is running the pipeline and checking counts:

```bash
ruff check .                                    # host, via pipx

# detection path — expect 2 HUMAN boxes at 0.9238 and 0.9126
... /app/infer.py --engine /models/model.engine \
    --test-image /models/frame_00347.jpg

# decode + SEI path — expect 42/42 recovered, contiguous=True
... /app/tools/probe.py --file /models/pi_sei_sample.hevc
```

`pi_sei_sample.hevc` is an SMPTE colour-bar test pattern, so it has zero
detections by design — it validates decode and frame_id recovery, not
detection. `live_capture.h265` and `range_walk_4.h265` are **0 bytes** and
cannot be replayed.

Container startup noise that is **not** a fault: `lsmod: not found` and
`(Argus) ... nvargus-daemon failed` — there is no CSI camera on this box and
the pipeline does not use Argus.

## Model (current)

YOLO26-m, **person-only** (nc=1), trained off-device on merged COCO 2017
outdoor-person + WiderPerson (~73K train / ~7K val). P=0.82, R=0.71,
mAP50=0.81 (Gilad's training run; not re-measured on this box).

Engine, read off `models/model.engine`: input `images [1,3,640,640]` FP32 NCHW
→ output `output0 [1,300,6]` FP32. **NMS is baked in** (YOLO26 exports
end-to-end regardless of `nms=False`); boxes are xyxy in the 640 letterboxed
space. Filter on the confidence column (index 4) only — never add manual NMS.

`CONFIDENCE_THRESHOLD = 0.35` in `app/config.py`. Not yet tuned against a
false-positive review.

Next iteration: dual-class (person=0, drone=1) at 960×960, same output layout.
A swap needs the new ONNX, `CLASS_MAP` and `INPUT_SIZE` in config.py, and
`DETECTOR_INPUT` on the entrypoint. The ONNX decides the input size — changing
`INPUT_SIZE` alone only produces a warning on stderr.

Detection requirements: range 5–40m guaranteed / 40–50m best-effort, min person
height ≥60px desired / ≥40px critical, recall ≥0.9, precision ≥0.8, latency
≤150ms avg / ≤250ms edge, ~10–15 simultaneous targets.

## Environment

Verified 2026-08-18:

| | |
|---|---|
| JetPack / L4T | 7.2 / r39.2 (R39 rev 2.0) |
| OS / kernel | Ubuntu 24.04.4, 6.8.12-1021-tegra |
| Driver | r595 open kernel module |
| CUDA / cuDNN / TensorRT | 13.2.1 / 9.20.0 / 10.16.2 |
| Python | 3.12.3 (host and container) |
| Docker | 29.6.1 |
| Power mode | MAXN_SUPER (nvpmodel 2), governor `schedutil` |
| Image | `smart-spotter-orin:dev`, from `nvcr.io/nvidia/cuda:13.2.1-cudnn-runtime-ubuntu24.04` |

- **Never run `apt upgrade` blindly** — L4T-pinned package risk.
- MAXN_SUPER was unlocked via a TNSPEC board-identity fix. Do not touch
  `/etc/nv_boot_control.conf`. See `docs/environment/ORIN_MAXN_SUPER_FIX.md`.
- GPU access in containers runs through **CDI** (`mode=cdi`), and needs
  `-e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all` on every run. Two distinct
  failure modes, with a triage table:
  **`docs/environment/CDI-GPU-ACCESS.md`**. Read it before diagnosing any
  "GPU disappeared" symptom.
- `docker/strip_compat.py` has an installed copy at
  `/usr/local/sbin/strip-cuda-compat.py` that does **not** track the repo.
  Re-install it after editing.
- `cuda-python` uses the 13.x layout: `from cuda.bindings import runtime as cudart`.
- No passwordless sudo. Diagnose unprivileged and hand Gilad `! sudo …`
  one-liners — note that `!` has no TTY, so anything needing a password must be
  run in a real terminal.
- Access: SSH / VS Code Remote-SSH, host `snipeit.local`, user `snipeit`,
  Wi-Fi 192.168.7.10.

## Code style

- `ruff check .` must pass. Config in `ruff.toml`: lint only at 79 columns.
  **Do not run `ruff format`** — it rewrites the aligned hand-wrapping into one
  argument per line.
- Comments describe current code only. Never narrate a fix or a past bug in a
  comment; that goes in conversation. Keep the constraint, drop the history.
- Do not assert anything a reader cannot verify from the code or a named,
  repeatable measurement.
- Plain English. Short unless there is a reason to elaborate. No comment that
  merely restates the code.

## Working conventions with Gilad

- Iterative, output-first: he runs commands and pastes output; reply with the
  next concrete action or patch, not a re-explanation of settled context.
- Provide **complete file/cell replacements**, not diffs.
- Decisions stated as locked are locked.
- Give honest assessments — surface risks and disagreements directly; say
  explicitly whether you agree or disagree before describing what you changed.
- Verify against **the code**, not against comments, docs, or memories. Earlier
  documents in this project asserted confident things traceable to nothing, and
  that cost real time.
- Commits: small and atomic, Conventional Commits, imperative, summary under
  ~50 chars, body only when it adds something. You draft the message, Gilad
  commits and pushes, you wait for confirmation before the next step.
- Cross-node changes (wire protocol, stream format, Pi behaviour) need a
  structured handoff doc that Gilad passes to Emil's Claude — flag explicitly
  when a change requires one.
