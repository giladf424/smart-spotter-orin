# Orin Inference Node — Measured Results (2026-08-04)

Scope: detection model, TensorRT engine, Orin inference pipeline.
All numbers below were measured on this machine on 2026-08-04 unless stated.
Anything not measured is marked **NOT MEASURED**. No value here is estimated
or reconstructed.

Environment for every run: JetPack 7.2 / L4T r39.2, TensorRT 10.16.2,
container `smart-spotter-orin:dev`, engine `/models/model.engine` (FP16, 640),
nvpmodel **MAXN_SUPER (mode 2)**, `jetson_clocks` **OFF** (governor `schedutil`).

> **Threshold caveat — applies to every detection count in this document.**
> `app/config.py:42` reads `CONFIDENCE_THRESHOLD = 0.35` (file mtime
> 2026-07-06 17:55). `CLAUDE.md` and the 2026-07-05 tuning notes both state
> 0.25. The change is not traceable in any surviving transcript. Runs marked
> "at 0.25" used an explicit override. The 2026-07-05 outdoor figures were
> taken at a **different** threshold than anything run after 2026-07-06.

---

## A1 — Offline replay throughput

`models/live_capture.h265` is **0 bytes** (so is `range_walk_4.h265`) and
cannot be replayed. Substituted `range_walk_3.h265`.

```bash
docker run --rm --runtime nvidia -e NVIDIA_VISIBLE_DEVICES=nvidia.com/gpu=all \
  --network host \
  -v ~/smart_spotter/orin/models:/models -v ~/smart_spotter/orin/app:/app \
  --entrypoint python3 smart-spotter-orin:dev /app/infer.py \
  --engine /models/model.engine --input-size 640 \
  --source file --file /models/range_walk_3.h265
```

| metric | value |
|---|---|
| frames decoded | 565 |
| frame_ids recovered | **565 / 565** |
| parse_fail | **0** |
| total detections | 1568 (at threshold 0.35) |
| wall clock | 50.85 s (incl. container start) |
| first→last frame span | 35.99 s |
| **sustained throughput** | **15.67 fps** |

Independent corroboration: the AU splitter finds exactly **565 access units**
in this file, mean 83,135 B, **2 keyframes** — matching the 2026-07-06 handoff
note ("exactly 2 keyframes").

---

## A2 — Per-stage timing

Measured with a harness that wraps the production modules without modifying
them. GPU segments split with CUDA events (no extra host syncs).
Source: loopback RTP replay of `range_walk_3.h265` paced at **2.4 fps** (the
Pi's real rate), corrected frame_id pairing, 241 frames.

| stage | mean | median | p95 | max | min |
|---|---|---|---|---|---|
| SEI pad probe (h265parse src) | 24.49 | 24.39 | 31.85 | 50.73 | 10.82 |
| NVDEC decode (parser→appsink) | 65.45 | 66.70 | 71.75 | 185.98 | 44.24 |
| preprocess CPU (letterbox/resize/normalize) | 9.86 | 9.39 | 13.44 | 16.29 | 5.28 |
| H2D copy | 1.74 | 1.76 | 1.97 | 2.36 | 0.81 |
| **TensorRT execute** | **39.88** | 40.06 | 45.17 | 111.51 | 32.18 |
| D2H copy | 0.19 | 0.14 | 0.37 | 5.29 | 0.08 |
| postprocess (filter + un-letterbox) | 0.29 | 0.28 | 0.38 | 0.44 | 0.22 |
| ZMQ serialize + send | 0.28 | 0.25 | 0.33 | 5.52 | 0.17 |
| per-frame compute total | 52.68 | 52.64 | 58.77 | 162.09 | 42.65 |

End-to-end:

| path | mean | median | p95 | max |
|---|---|---|---|---|
| **udpsrc first byte → ZMQ push** | **495.0** | 531.3 | 547.5 | 561.1 |
| parser output → ZMQ push (Orin compute only) | 142.6 | 142.2 | 156.6 | 363.9 |

Same pipeline fed at **10 fps**: end-to-end **221.5 mean / 251.4 p95**;
parser→ZMQ 151.5 mean / 179.0 p95.

### Dominant latency term: h265parse AU-close delay

The udpsrc→parser segment scales with the inter-frame gap, because
`h265parse` cannot close an access unit until the next one begins (this stream
carries no AUD NALs):

| send rate | frame interval | udpsrc→parser | ratio |
|---|---|---|---|
| 2.4 fps | 417 ms | 376.8 ms | 0.90× |
| 10 fps | 100 ms | 91.7 ms | 0.92× |

This is the largest single contributor to end-to-end latency and is a direct
function of the Pi's frame rate. Whether AUD NALs from the Pi (or a receiver
alignment change) would remove it is **NOT MEASURED**.

### File-replay note

In `--source file` mode `filesrc` runs unpaced, so the parser races ahead of
the consumer. Decode/end-to-end figures measured that way (~1.1 s) are **queue
backlog, not latency**, and are excluded from this document.

---

## A3 — Pure inference benchmark

```bash
# 200 consecutive executions, 20 warm-up excluded, fixed 640x640 input
python3 /bench/bench_engine.py /models/model.engine 200
trtexec --loadEngine=/models/model.engine --iterations=200 --avgRuns=200 --warmUp=500
```

| | mean | median | p95 | p99 | min | max |
|---|---|---|---|---|---|---|
| Python wrapper (production path) | 15.93 | 15.59 | 17.45 | 17.53 | 15.30 | 21.16 |
| trtexec GPU compute | 14.80 | 14.64 | 16.07 | 16.27 | 14.26 | 16.33 |
| trtexec end-to-end latency | 15.23 | 15.03 | 16.60 | 16.85 | 14.64 | 16.96 |

trtexec throughput 67.2 qps; H2D 0.425 ms, D2H 0.007 ms. trtexec warns GPU
compute is unstable (coefficient of variance 3.92%) — consistent with
`jetson_clocks` off.

**Important:** the same engine measures **15.9 ms back-to-back but ~39.9 ms
inside the live pipeline at 2.4 fps** (A2). The GPU clocks down between frames
at low duty cycle. Quoting "16 ms inference" for the live system overstates it
by ~2.5×.

---

## A4 — FP16 vs FP32

Both rebuilt from the same ONNX to separate paths; the cached production
engine was never touched.

| | FP16 | FP32 |
|---|---|---|
| inference mean (200 iters) | **15.75 ms** | 30.77 ms |
| p95 | 17.30 ms | 31.08 ms |
| throughput | 63.5 /s | 32.5 /s |
| engine file size | 44,300,388 B | 83,522,452 B |
| build wall clock | **8 min 39 s** | **3 min 31 s** |

Detections on `models/frame_00347.jpg` (threshold 0.35):

| | FP16 | FP32 |
|---|---|---|
| count | 2 | 2 |
| confidences | 0.9238, 0.9126 | 0.9238, 0.9126 (**identical**) |
| bbox #1 | x=350, y=273, w=167, h=451 | x=349, y=273, w=167, h=452 |
| bbox #2 | x=972, y=202, w=163, h=536 | identical |

**FP16 is 1.95× faster at no measurable accuracy cost** (max deviation 1 px).
FP16 is the *slower* build (more precision-tactic search).

Build times are **derived from timestamps** (container start → engine mtime),
not stopwatch: `bc` is absent from the image so the in-container timer printed
empty. Accurate to ~2 s.

---

## A5 — Live run with the Pi — **NOT MEASURED**

The Pi streamed briefly mid-session (512 KB/s observed from 10.42.0.1) and had
stopped before the live run started; that run recorded `rtp=0` packets over
120 s. Not a pipeline fault — no input. Link itself is healthy: ping 0.58 ms
avg, ARP reachable, NIC **1000 Mb/s full duplex, autoneg on**.

---

## B — Resource envelope (tegrastats @ 1 Hz)

| | idle (31 s) | live 2.4 fps (129 s) | file replay, max rate (50 s) |
|---|---|---|---|
| RAM used | 1994 MB | 3351 MB (peak 3476) | 2563 MB (peak 2692) |
| GPU util | 0% | 9.2% mean / 99% peak | 27.1% mean / 99% peak |
| CPU (avg of 6 cores) | 11.0% | 13.1% (peak 24.5) | 22.5% (peak 28.2) |
| GPU temp peak | 49.4 °C | 50.6 °C | 52.9 °C |
| tj peak | 50.1 °C | 51.4 °C | 53.6 °C |
| VDD_IN power | 3.72 W | 4.52 W | 7.10 W |

**B2:** nvpmodel MAXN_SUPER (mode 2); `jetson_clocks` **off**.
**B3:** idle→live delta = +1357 MB RAM, +9.2 pp GPU, +0.80 W.
Thermals and power are far from any limit; the board is heavily under-utilised
at the current stream rate.

---

## C — Engine and model facts

**C1.** Fingerprint inputs used by the build-or-cache logic
(`docker/entrypoint.sh:33`) — sha256(ONNX) + `libnvinfer10` package version +
input edge + precision:

```
onnx=1ea9d1e05a4b19a3d6f5fd5d685786e783b4c8bd62ce226e29117030bb5f5aad;trt=10.16.2.10-1+cuda13.2;in=640;prec=fp16
```

Production engine `models/model.engine`: 44,995,684 B, built 2026-06-29 20:52.
(A fresh FP16 rebuild of the same ONNX today produced 44,300,388 B — TRT builds
are not byte-reproducible.)

**C2. Cannot confirm.** Every occurrence of "exhibition" / "exhibit" /
"demo day" / "presentation" on this machine originates from the 2026-08-04
request itself and the searches run to answer it. No log, capture, config file,
or session transcript references such an event. The configuration that ran at a
July exhibition is therefore **NOT ESTABLISHED**.

**C3.** Verbatim ZMQ message, unedited, from the loopback live-mode run
(241 messages captured on a PULL/bind collector):

```json
{"type":"target_detection","frame_id":1,"timestamp_ms":18048939,"detections":[{"id":"1","class":"HUMAN","confidence":0.9292,"bbox":{"x":878,"y":412,"width":212,"height":476}},{"id":"2","class":"HUMAN","confidence":0.9053,"bbox":{"x":2,"y":354,"width":181,"height":588}}]}
```

---

## D — Detection quality evidence

Threshold **0.25** for both runs (explicit override, to expose marginal
detections). All boxes in original 1920×1080 space.

### Height distribution

| capture | frames | detections | ≥60 px | 40–60 px | <40 px | min height |
|---|---|---|---|---|---|---|
| `outdoor_capture.h265` | 364 | 394 | 388 (98.5%) | 4 (1.0%) | 2 (0.5%) | 39.0 px |
| `range_walk_5.h265` | 1171 | 720 | 720 (100%) | 0 | 0 | 177.0 px |

`outdoor_capture` confidence: min 0.251, median 0.756, max 0.946.
The 364 frames / 394 detections exactly reproduce the 2026-07-05 figures —
replay is deterministic.

### D1 — clean multi-person, mid range (`D1_frame263.png`)

`outdoor_capture.h265`, frame_id 263, 10 simultaneous detections, all real
people (campus courtyard):

| # | conf | height px | width px | bbox (x,y,w,h) |
|---|---|---|---|---|
| 1 | 0.9351 | 642.8 | 356.2 | 258,430,356,643 |
| 2 | 0.8242 | 187.5 | 61.5 | 1264,494,62,188 |
| 3 | 0.8032 | 438.0 | 80.2 | 0,494,80,438 |
| 4 | 0.7812 | 183.8 | 55.5 | 1206,497,56,184 |
| 5 | 0.7559 | 213.8 | 66.0 | 1317,468,66,214 |
| 6 | 0.7334 | 222.0 | 72.9 | 57,500,73,222 |
| 7 | 0.7041 | 181.5 | 46.3 | 227,488,46,182 |
| 8 | 0.5435 | 224.2 | 62.9 | 185,491,63,224 |
| 9 | 0.4971 | 221.2 | 60.3 | 186,492,60,221 |
| 10 | 0.2549 | 158.2 | 30.0 | 1168,505,30,158 |

Supports the 10–15 simultaneous-target requirement. All ≥60 px.

### D2 — marginal case — **INCONCLUSIVE**

No confirmed *true* person below 60 px exists in any capture on this machine.

- `outdoor_capture` smallest: 39.0 px, conf 0.3784, bbox 83,731,37,39
  (frame 229). Visual inspection: the box sits on a dark patch beside a
  doorway. **Not confirmable as a person** — plausibly a false positive.
  Requires Gilad's adjudication.
- `range_walk_5` smallest: 177.0 px, conf 0.5396, bbox 1000,899,613,177
  (frame 1021) — 613 px wide × 177 px tall, a head at the bottom frame edge
  beside a parked car. Near-field, not a distant target.

**The `range_walk_*` captures do not contain a range walk with distant
subjects.** Every detection in `range_walk_5` is ≥177 px. They should not be
cited as range evidence.

Consequence: the design envelope (**≥60 px desired / ≥40 px critical**) is
**untested in the band it describes**.

### D3 — failure case (`D3lowconf_frame126.png`)

`outdoor_capture.h265`, frame_id 126. Detections #4 and #5 are two overlapping
boxes on the **same** partially-occluded person at the right frame edge
(IoU ≈ 0.48) — a duplicate that survived the model's baked-in NMS. A duplicate
counts as a false positive for precision.

| # | conf | height px | bbox |
|---|---|---|---|
| 1 | 0.9268 | 387.8 | 588,690,257,388 |
| 2 | 0.9233 | 585.0 | 822,486,484,585 |
| 3 | 0.9170 | 249.0 | 0,826,275,249 |
| 4 | 0.6714 | 147.8 | 1304,932,68,148 |
| 5 | 0.2510 | 150.0 | 1305,928,134,150 |

Zero-detection frames also exist (`outdoor_capture` frame 383,
`range_walk_5` frame 1321) but without ground truth they cannot be confirmed
as misses.

---

## Defect found: live-mode frame_id pairing

Measured, not inferred from code.

On the **live RTP** path, `h265parse` emits **two buffers per picture**: an
SEI-only buffer (31–123 B) carrying the frame_id, followed by a separate VCL
buffer (the picture) carrying none. Instrumented over 40 s:
**60 with-VCL / 60 without-VCL / 60 frames delivered.**

`app/ingest.py` `_on_au_probe` appends one FIFO entry per parser buffer;
`_on_new_sample` pops one per decoded frame. Two pushes per pop desyncs the
queue: measured **`no_frame_id = 130 / 262`**, with the remainder receiving
*stale* ids belonging to earlier pictures. It also corrupts any latency derived
from that FIFO (appeared as 27 s mean growing to 54 s — entirely artifact).

Sample of parser buffers (size, NAL types, has_VCL, frame_id):

```
(105713, [32,33,34,39,39,32,33,34,32,33,34,20], True,  1)   <- IDR, SEI in same buffer
(31,     [39],                                  False, 2)   <- SEI only
(167394, [1],                                   True,  None) <- picture, no id
(123,    [39,32,33,34],                         False, 3)
(85853,  [1],                                   True,  None)
```

**Fix validated in the harness** (carry a frame_id seen on a non-VCL buffer
forward to the next VCL-bearing buffer): **`no_frame_id = 0 / 241`**, latency
stable. **Not applied to `app/`.**

**File replay is unaffected** — filesrc→h265parse aggregates whole AUs, giving
exactly 1:1 pairing (565/565, 0 misses). This is why every offline validation
looked clean.

**NOT CONFIRMED against the real Pi stream.** The previously reported Pi-side
pose join of 434 hit / 1 miss is inconsistent with this failure mode, so either
the real stream groups NALs differently or that validation predates something.
If it reproduces live it is a cross-node correctness bug — detections joined to
the wrong pose means an aiming error — and requires a handoff to Emil.

---

## G — Gaps against stated requirements

| requirement | measured | verdict |
|---|---|---|
| latency ≤150 ms avg | 495 ms mean @2.4 fps; 222 ms mean @10 fps | **FAIL** |
| latency ≤250 ms edge | 548 ms p95 @2.4 fps; 251 ms p95 @10 fps | **FAIL** |
| recall ≥0.9 | no ground truth exists | **NOT MEASURED** |
| precision ≥0.8 | no ground truth exists | **NOT MEASURED** |
| person height ≥60 px desired / ≥40 px critical | no confirmed true person <60 px in any capture | **UNTESTED IN BAND** |
| 10–15 simultaneous targets | 10 real simultaneous detections observed | supported |

Latency causes, in order, **neither of which is inference**:

1. `h265parse` AU-close delay — 0.90× the frame interval (377 ms at 2.4 fps).
2. The Pi's ~2.4 fps software x265 encode — ~420 ms/frame before anything
   reaches the Orin.
3. GPU DVFS at low duty cycle — inference 39.9 ms in-pipeline vs 15.9 ms
   isolated, with `jetson_clocks` off.

Orin compute alone (parser→ZMQ) is 142.6 ms mean / 156.6 ms p95, and the box
runs at 9.2% mean GPU utilisation and 4.5 W. The Orin is not the bottleneck.

---

## Methodology notes

- Harness code lives in the session scratchpad, not in `app/`. Production
  modules were wrapped, never modified.
- The loopback sender replays a real Pi capture through
  `appsrc → h265parse → rtph265pay config-interval=1 → udpsink sync=true`,
  reproducing the Pi's payloader configuration and burst-per-AU behaviour.
  Pacing requires `is-live=false` and explicit PTS; `identity sleep-time` was
  found to have no effect, and `is-live=true` blasts.
- The AU splitter used by the sender was validated against ground truth:
  565 AUs vs 565 decoded frames, 2 keyframes, sequential frame_ids.
