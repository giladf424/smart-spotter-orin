# CLAUDE.md — Smart Spotter, Orin Inference Node

## What this machine is

This Jetson Orin Nano Super Developer Kit is the **stateless GPU inference node** of the Smart Sniper Spotter System — an Afeka College software engineering capstone (2025). The full system is an autonomous pan/tilt surveillance device: a motorized camera performs serpentine grid scans, detects persons, locks on with servos, localizes noise sources with a 6-mic array, and reports to an Android app. Team: Gilad Faibish (CV/detection — the person you're talking to), Dolev Telem (Android app), Emil Glatter (embedded/Pi-side).

The system has two compute nodes:
- **Raspberry Pi 5** (Emil's domain, separate Claude instance) — hub, owns the camera (Pi HQ IMX477, 16mm lens ~24° HFOV), servo control (2× DFRobot SER0049 pan/tilt), all geometry/aiming, sensors, app communication.
- **This Orin** — receives video, runs detection, returns results. **Intentionally stateless**: no pose, no servo state, no cross-frame tracking. This is a locked architectural decision — do not add state here; tracking (ByteTrack/Norfair) belongs on the Pi because motion-compensated association requires pose.

## Pi ↔ Orin protocol (locked — do not relitigate)

- Direct GigE cable, subnet 10.42.0.0/24. Orin = 10.42.0.2 on `enP8p1s0`, Pi = 10.42.0.1.
- Pi streams **H.265/RTP over UDP** (not RTSP) to the Orin.
- Pi stamps each frame with `frame_id` via **SEI NAL**; Orin extracts it and echoes it back with detections. Pi keeps a `frame_id→pose` ring buffer (~10–15 frames) and does all geometry locally.
- Detections egress: **ZMQ PUSH (Orin) → PULL (Pi)** at `tcp://10.42.0.1:5556`.
- JSON wire schema: `{type, frame_id, timestamp_ms, detections: [{id, class, confidence, bbox}]}`. Detection `id`s are 1-based strings, per-frame only — Pi owns identity across frames.
- Confidence threshold placeholder: 0.25 (tuning for high recall is pending).

## Inference pipeline on this box

Code lives in `~/smart_spotter/orin/app/`, mounted at `/app` in the container:
- **Ingest (Half 1):** `sei.py`, `ingest.py`, `probe.py` — GStreamer pipeline with NVDEC (`nvv4l2decoder`, `nvvidconv`). SEI frame_id extracted via pad probe on the `h265parse` **src pad** (encoded side, before decoder — deliberate, so the design doesn't depend on NVDEC preserving SEI). UUID filtering rejects x265's competing type-39/payloadType-5 SEI.
- **Inference + egress (Half 2):** `config.py`, `engine.py`, `postprocess.py`, `zmq_sink.py`, `infer.py`. TensorRT engine built on-device from ONNX, with build-or-cache logic fingerprinted by onnx-hash + TRT version + input dims + precision.
- Validated end-to-end pre-container-incident: 435 frames decoded, parse_fail=0, 373 detections, pose_join 434/1 hit/miss, HUMAN detections at 0.88–0.95 confidence in full 1920×1080 space, frame_ids echoing correctly.

## Commands

```bash
# Build the container (from docker/)
docker build -t smart-spotter-orin:dev ~/smart_spotter/orin/docker

# Run the full service (entrypoint builds/caches TRT engine, then launches infer.py)
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  -e PRECISION=fp16 -e DETECTOR_INPUT=640 \
  --network host \
  -v ~/smart_spotter/orin/models:/models \
  -v ~/smart_spotter/orin/app:/app \
  smart-spotter-orin:dev

# Dev iteration: bypass entrypoint, run infer.py directly against the cached engine
docker run --rm -it --runtime nvidia \
  -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --network host \
  -v ~/smart_spotter/orin/models:/models \
  -v ~/smart_spotter/orin/app:/app \
  --entrypoint python3 smart-spotter-orin:dev \
  /app/infer.py --engine /models/model.engine --input-size 640 --source live

# GPU sanity check inside the container (first thing to run after any reboot)
docker run --rm --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --entrypoint python3 smart-spotter-orin:dev \
  -c "from cuda.bindings import runtime as cudart; print(cudart.cudaGetDeviceCount())"
```

`infer.py` modes (all run inside the container): `--test-image <jpg>` single still → JSON to stdout; `--source file --file /models/live_capture.h265` offline replay of a captured stream; `--source live [--port 5600]` live RTP from the Pi with ZMQ push (implicit; `--push` forces it in file mode). Engine input edge is authoritative over `--input-size`. Entrypoint knobs via `-e`: `PRECISION` (fp16|fp32), `DETECTOR_INPUT`, `ONNX_PATH`, `ENGINE_PATH`. Force an engine rebuild by deleting `models/model.engine.fingerprint`.

There is no test suite or linter; validation is running the pipeline against the captured samples in `models/` (`live_capture.h265`, `pi_sei_sample.hevc`, `frame_00347.jpg`) and checking parse/detection counts.

## Model (current)

YOLO26-m, **person-only** (nc=1), trained on merged COCO 2017 outdoor-person + WiderPerson (~73K train / ~7K val). P=0.82, R=0.71, mAP50=0.81. ONNX: input `images [1,3,640,640]`, output `output0 [1,300,6]` — **NMS is baked in** (YOLO26 exports end-to-end regardless of `nms=False`), boxes in xyxy 640px input space, class map `{0: person}`. Filter on confidence column (index 4) only — no manual NMS.

Next model iteration (trained off-device on Kaggle/Colab): dual-class (person=0, drone=1) at 960×960 — expect same output layout. When it arrives, only the ONNX, input dims, and class map change; pipeline structure stays.

Detection requirements driving tuning: range 5–40m guaranteed / 40–50m best-effort, min person height ≥60px desired / ≥40px critical, recall ≥0.9, precision ≥0.8, latency ≤150ms avg / ≤250ms edge, ~10–15 simultaneous targets.

## Environment & hard-won constraints

- JetPack 7.2 / L4T r39.2, Ubuntu 24.04, kernel 6.8.12-1021-tegra, driver 595.78, CUDA 13.2, cuDNN 9.20, TensorRT 10.16.2, Python 3.12.3, Docker 29.6.1. MAXN_SUPER (nvpmodel mode 2) unlocked via TNSPEC board-identity fix — do not touch `/etc/nv_boot_control.conf`.
- **Never run `apt upgrade` blindly** — L4T-pinned package risk.
- Container: `smart-spotter-orin:dev`, base `nvcr.io/nvidia/cuda:13.2.1-cudnn-runtime-ubuntu24.04` (SBSA). Runtime is forced CDI mode (`mode=cdi` in `/etc/nvidia-container-runtime/config.toml`), GPU via `-e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all`.
- **CDI spec regeneration:** toolkit 1.19.1 has a broken `enable-cuda-compat` hook (ELF-header parse panic); `~/smart_spotter/orin/docker/strip_compat.py` strips it from `/etc/cdi/nvidia.yaml`. The nvgpu char-device majors in the spec are **boot-specific** — a stale spec causes `CUDA_ERROR_NO_DEVICE` inside containers while the host is healthy (we lost days to this once; dmesg DCE lines like `dce_admin_setup_clients_ipc: Get queue info failed for [2]` are benign headless noise, not a GPU fault). If containers lose the GPU after a reboot: regenerate spec (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`), re-strip, verify majors match `ls -la /dev/nvgpu/igpu0/`. Full runbook: `docker/CDI-GPU-FIX.md`.
- `cuda-python` uses 13.x layout: `from cuda.bindings import runtime as cudart`.
- Access: SSH/VS Code Remote-SSH, host `snipeit.local`, user `snipeit`, Wi-Fi 192.168.7.10.

## Working conventions with Gilad

- Iterative, output-first: he runs commands/cells and pastes output; respond with the next concrete action or patch — no re-explanation of established context.
- Provide **complete file/cell replacements**, not diffs.
- Code comments in English, describing current code only — never narrate fixes in comments; fixes go in conversation text.
- Decisions stated as locked are locked.
- When uncertain, pause and verify against primary sources (NVIDIA docs/forums) before proceeding — but note NVIDIA doc pages sometimes serve stale cached content; if a fetched doc contradicts observed behavior, ask Gilad to paste from his live browser.
- Give honest, unbiased assessments — surface risks and disagreements directly; do not agree to be agreeable.
- Concise answers.
- Cross-node changes (anything touching the wire protocol, stream format, or Pi behavior) are relayed to Emil's Pi-side Claude instance via structured handoff docs Gilad passes between you — flag explicitly when a change requires one.
