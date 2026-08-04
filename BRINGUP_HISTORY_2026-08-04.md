# Orin Node — Bring-up History and Design Provenance

Companion to `MEASUREMENTS_2026-08-04.md`. Compiled 2026-08-04.

## Evidence base and its limits — read this first

Everything below is sourced from artefacts that still exist on this machine:
file contents and mtimes, `/etc/` config diffs, `~/.bash_history` (**no
timestamps** — order is preserved, wall-clock is not), committed docs, and
Claude session transcripts in `~/.claude/projects/`.

**Critical gap:** the surviving session transcripts begin **2026-07-05**. The
sessions covering 2026-06-28 → 2026-07-01 — when the board was provisioned,
the container was built, the CDI problem was solved, and the SEI/ingest design
was made — **no longer exist**. `app/sei.py`, `probe.py` and the first
`ingest.py` were all written 2026-06-30/07-01, inside that lost window.

Consequently several questions below are answered **NOT ESTABLISHED**. Code
comments describing a design are treated as statements of intent, *not* as
evidence that a bug occurred or that an alternative was tried and failed.

**No records anywhere on this machine establish how many working hours any
issue consumed.** Date spans are given where files or docs are dated; effort
is not inferred from them.

---

## E1 — TNSPEC / nvpmodel board identity

**Status: fully evidenced by a config diff.**

**What was changed** — `/etc/nv_boot_control.conf`, backup 2026-06-28 12:29,
edited file 2026-06-28 12:53. Exact diff (`.bak` → live):

```
- TNSPEC 3767-301-0005-M.1-1-1-jetson-orin-nano-devkit-
- COMPATIBLE_SPEC 3767--0005--1--jetson-orin-nano-devkit-
+ TNSPEC 3767-301-0005-M.1-1-1-jetson-orin-nano-devkit-super-
+ COMPATIBLE_SPEC 3767--0005--1--jetson-orin-nano-devkit-super-
```

i.e. the board-identity string was suffixed `-super-` in both fields, so
`nvpmodel` would accept the Super power-model table for this board.

**Observable symptom** — not recorded verbatim. What the command record shows
is a sustained attempt to reach a power mode the board would not accept:
repeated `nvpmodel -q`, inspection of `/etc/nvpmodel.conf`, and symlinking it
in turn to `nvpmodel_p3767_0000_super.conf`, `p3767_0003.conf`,
`p3767_0004_super.conf`, `p3767_0003_super.conf`, each followed by
`nvpmodel -m 0` / `-m 2` and a re-query.

**How it was diagnosed** — the record shows the search moving outward from the
power-model config to board identity: `nvpmodel.service` and
`/etc/systemd/nvpmodel.sh` were read, then `nvbootctrl dump-slots-info`,
`/proc/device-tree/nvidia,dtsfilename`, `/proc/device-tree/compatible`,
`dpkg -l nvidia-l4t-bootloader`, and finally `cat /etc/nv_boot_control.conf`
and `grep -n "devkit" /etc/nv_boot_control.conf` — immediately before the
backup and edit. `sudo nvpmodel -m 2 --verbose --force` follows the edit.

**Outcome (verified today)** — `/etc/nvpmodel.conf` →
`nvpmodel_p3767_0003_super.conf` (mtime 2026-06-28 13:00);
`nvpmodel -q` reports **MAXN_SUPER, mode 2**;
`/proc/device-tree/model` reads *NVIDIA Jetson Orin Nano Engineering Reference
Developer Kit Super*.

**Cost** — the observable window runs 2026-06-28 01:39 (first nvpmodel config
backup) to 13:00 (final symlink). That span includes an overnight gap and is
**not** an effort figure. Working hours: **NOT ESTABLISHED**.

---

## E2 — CDI / cuda-compat

These were **two distinct failures**, and the report should not merge them.

### (a) The `enable-cuda-compat` hook panic — circa 2026-06-29

`docker/strip_compat.py` mtime 2026-06-29 19:50. nvidia-container-toolkit
1.19.1 injects a `createContainer` hook that panics parsing an ELF header,
breaking every GPU container.

Recorded command sequence, in order:

1. `nvidia-ctk config --in-place --set features.disable-cuda-compat-lib-hook=true`
2. re-run the container probe — **still failing**
3. `grep -rn 'cudacompat|cuda-compat|forward-compat' /etc/cdi/ /var/run/cdi/`
4. `nvidia-ctk cdi generate --output=/var/run/cdi/nvidia.yaml`
5. `grep -c 'cuda-compat' … && echo "STILL PRESENT"`
6. hand-written regex strip of the hook block; re-test
7. `echo "=== is the hook back in the file we edited? ==="`
8. `echo "=== timestamp — was it regenerated after our edit? ==="`
9. `find / -name '*.yaml' -path '*cdi*'` → discovery of **two competing specs**
   (`/etc/cdi/` and `/var/run/cdi/`)
10. `systemctl status nvidia-cdi-refresh.service` → the regenerator
11. runtime switched to `mode=cdi`; spec consolidated to `/etc/cdi/`;
    `nvidia-cdi-refresh.service`/`.path` **masked and disabled**

**On the question "was the config-edit-had-no-effect reasoning the actual
turning point, or a later rationalization?"**

Partly the former, but the framing needs correcting. Steps 1–2 *are*
contemporaneous evidence that a config edit was made, retested, and found to
change nothing. Steps 7–8 are contemporaneous evidence — the operator's own
echo statements, written at the time — of the decisive question being asked.

But the reasoning those lines actually record is **not** "the file is not on
the active code path". It is *"the file we edited is being **regenerated**"* —
and the turning point that follows is the discovery at step 9 that **two
competing CDI specs existed in different directories**, with a systemd unit
rewriting one of them. That is a materially different insight, and it is the
one the record supports. The Claude session transcript that would show the
verbatim reasoning is **gone**, so intent beyond these command-line artefacts
is **NOT ESTABLISHED**.

### (b) Stale CDI device majors — documented 2026-07-02

Root cause per `docker/CDI-GPU-FIX.md`: `/etc/cdi/nvidia.yaml` records device
major/minor numbers **at generation time**, and the `nvgpu` nodes use
**dynamically allocated majors that change across reboots**. `/dev/nvidia*`
uses static major 195, which is why host-level checks (`nvidia-smi`, `cuInit`)
kept passing while containers got `CUDA_ERROR_NO_DEVICE` or segfaulted.

**This was caused by fix (a):** `nvidia-cdi-refresh` — the service that exists
to regenerate the spec — had been disabled in step 11 above, so the spec was
never refreshed and every reboot could re-break containers.

Durable fix (2026-07-02): service and `.path` unit re-enabled, output pinned to
`/etc/cdi/nvidia.yaml` via `NVIDIA_CTK_CDI_OUTPUT_FILE_PATH`, and an
`ExecStartPost` drop-in re-strips the hook after every regeneration. The strip
script must live outside `/home` because the unit runs without
`CAP_DAC_OVERRIDE` and cannot traverse the 750-mode home directory — a failure
there would leave a freshly generated **hooked** spec live.

**Red herring, recorded explicitly:** the DCE boot errors
(`dce_admin_setup_clients_ipc: Get queue info failed for [2]`, and 0x73xxxx
`NVRM rpcRmApiControl_dce` failures) appear on **every** boot since first
provisioning, including every period when inference worked. They are display-
class calls on a headless board and have zero effect on CUDA compute.

**Dates and cost** — (a) circa 2026-06-29 (script mtime), (b) resolved
2026-07-02 (doc date, and a `.bak-20260702` spec backup). Calendar span
2026-06-29 → 2026-07-02. **Working days: NOT ESTABLISHED** — no record on this
machine measures effort. The command history does show numerous reboot cycles
through this period, consistent with a reboot-dependent fault.

---

## E3 — Other issues

Ranked by strength of evidence. Effort is not claimed for any of them.

### Confirmed, well documented

**Mid-GOP join deadlock (2026-07-06)** — four consecutive field capture runs
produced `frames=0` with 0-byte capture files while RTP flowed normally.
Root cause chain, reproduced via loopback: the Pi stream carried almost no
keyframes (**2 in 565 frames**); any Orin restart joining a running stream
received only reference-less P-frames; `nvv4l2decoder` held them, its input
pool exhausted, and backpressure deadlocked the whole graph including the
capture tee — hence the 0-byte files. Two further faults surfaced in the same
investigation:
- `zmq_sink` used a blocking send with `SNDHWM=100`, so a down/slow Pi froze
  the pipeline at exactly 100 frames. Now `DONTWAIT` + drop-and-log.
- Kernel UDP receive buffer was the stock ~208 KB while each AU arrives as a
  single line-rate burst, so any AU larger than that lost its tail packets
  silently and was discarded by the depayloader. Now `rmem_max=8388608` plus
  `udpsrc buffer-size=8388608`.

Orin-side fixes: drop AUs until the first keyframe (by NAL type 16–21 —
`h265parse` only clears the `DELTA_UNIT` flag for IDR, not CRA, so the flag
check is insufficient), and `h265parse config-interval=-1` to re-insert cached
parameter sets. Pi side then pinned a closed 12-frame GOP
(`keyint=12 min-keyint=12 scenecut=0 open-gop=0`). Documented in
`HANDOFF_PI_GOP_FIX.md` and `models/PI_REPLY_GOP_FIX.md`.

**Pi hotspot subnet collision (2026-07-02)** — symptom was "link dead but ARP
works". Cause was Pi-side routing: a NetworkManager shared profile on `usb0`
claiming `10.42.0.0/24`, colliding with the GigE link subnet. Fixed on the Pi
by moving it to `10.43.0.1/24`. Not an Orin fault.

**NIC link diagnosis fallout (2026-07-05)** — during that investigation the
link was forced to 100 Mb/s with EEE disabled. The revert was issued but never
verified at the time. **Verified resolved 2026-08-04:** link reads
1000 Mb/s full duplex, autoneg on.

### Partially evidenced

**Container base image selection** — the command record shows real comparative
investigation: `docker manifest inspect` against
`nvcr.io/nvidia/l4t-jetpack:r39.2`, then
`nvcr.io/nvidia/cuda:13.2.1-cudnn-runtime-ubuntu24.04` and the 13.2.0 variant;
inspection of `/etc/nvidia-container-runtime/host-files-for-container.d/*.csv`
for `nvbuf|nvv4l2|nvmedia|nvdec|gstreamer` entries; and locating
`libgstnvvideo4linux2.so` / `libnvbufsurface*` on the host. The resulting
`docker/Dockerfile` encodes the conclusion explicitly: install the **generic**
GStreamer framework and **not** `nvidia-l4t-gstreamer`, because the NVIDIA
NVDEC plugins are mounted from the host at runtime. A considered decision with
supporting checks; time cost **NOT ESTABLISHED**.

**cuda-python 13.x bindings layout** — `app/engine.py` imports
`from cuda.bindings import runtime as cudart` with a fallback to the legacy
`from cuda import cudart`, and `CLAUDE.md` records the 13.x layout as a
constraint. This documents a real API difference. There is **no evidence of a
failure event** — the fallback is defensive, and a try/except is not proof a
crash occurred.

### Not confirmed

**MPLBACKEND crashes in subprocesses** — **no occurrence of `MPLBACKEND`
anywhere** in any surviving transcript, script, or config on this machine.
`matplotlib` appears 6 times across three transcripts. I cannot confirm this
event happened, and I am not going to describe it. If it is real it happened in
the lost 2026-06-28 → 07-01 window, and you are the only remaining source.

---

## F — Design provenance

### F1 — Why the SEI probe sits on the h265parse **src** pad (encoded side)

**Was a decoded-side extraction attempted first and found to fail?
NOT ESTABLISHED.**

`app/ingest.py` states the rationale as a design position:

> "Once `nvv4l2decoder` produces a raw frame the SEI is gone. So we DO NOT try
> to read SEI from the decoded frame."

and

> "The pad probe runs before the decoder, so whether NVDEC preserves or strips
> SEI is irrelevant to us — we never depend on it."

That is a statement of intent written into the code, not a record of an
experiment. The files were authored 2026-06-30/07-01, inside the lost
transcript window. There is no artefact on this machine — no script, log,
capture, or transcript — showing a decoded-side attempt. **I cannot tell you
which it was.** The phrasing "so the design doesn't depend on NVDEC preserving
SEI" reads as risk-avoidance rather than post-failure repair, but that is a
reading of prose, not evidence, and should not go into the report as fact.

### F2 — The UUID filter against x265's SEI

**The collision is real. I verified it today, independently of any code
comment.**

Scanning the actual bitstreams for prefix-SEI (NAL type 39) with
payloadType 5:

| capture | SEI with our UUID | foreign payloadType-5 SEI |
|---|---|---|
| `pi_sei_sample.hevc` | 42 | **1** |
| `range_walk_3.h265` | 98 | **1** |

The foreign one carries uuid `2ca2de09b51747dbbb55a4fe7fc2fc4e` and payload
`x265 (build 199) - 3.5+1-f0c1022b6:[Linux][G…` — x265's version string,
exactly as `app/sei.py` describes. So matching on (type 39 + payloadType 5)
alone genuinely is insufficient, and without the UUID check that ASCII would be
read as a frame_id.

Note the collision occurs **once per stream**, not per frame — so unfiltered it
would corrupt approximately one access unit at stream start, not a stream-wide
failure.

**Whether the filter was added before or after observing this: NOT
ESTABLISHED.** `sei.py`'s header describes these as "Critical facts (from the
Pi, verified against the real bitstream)", which attributes the knowledge to
the Pi side and to bitstream inspection — but does not date it relative to the
code. What is certain is that the filter is **necessary**, not merely
defensive.

### F3 — Alternatives evaluated

| alternative | status |
|---|---|
| **DeepStream** vs hand-built GStreamer | **Zero mentions** in every surviving transcript, script, config and doc on this machine. No evidence it was tested. Cannot rule out discussion in the lost sessions. |
| **ONNX Runtime** vs TensorRT | **Zero mentions**, same basis. No evidence it was tested. |
| **RTSP pull** vs raw RTP/UDP | Not an Orin-side evaluation. `CLAUDE.md` records it under "Pi ↔ Orin protocol (locked — do not relitigate)": *"Pi streams H.265/RTP over UDP (not RTSP)"*. Decided as a protocol constraint, not benchmarked here. The container does install the generic GStreamer RTP/RTSP depayloaders, but nothing shows an RTSP path was ever built or measured. |

Honest summary for the report: **no evidence exists that any of these three
alternatives was empirically tested on this node.** Claiming a comparative
evaluation would not survive scrutiny.

### F4 — YOLO26 exporting with NMS baked in regardless of `nms=False`

**How it was discovered: NOT ESTABLISHED.** Every occurrence of `nms=False`
and "baked in" in the surviving transcripts is a re-read of `CLAUDE.md`, which
already contained the conclusion by 2026-07-02. The discovery predates the
surviving record. **What broke or nearly broke before it was understood is
therefore unknown, and I will not invent it.**

**The claim itself is verified by the artefacts:** the engine's output tensor
is `output0 [1,300,6]` — a fixed 300-row end-to-end detection list with
per-row confidence (col 4) and class id (col 5), not raw anchors.
`app/postprocess.py` applies a confidence filter only, with no NMS anywhere in
the codebase, and produces clean results (1568 detections over 565 frames with
no duplicate storms). One duplicate pair *did* survive in `outdoor_capture`
frame 126 at IoU ≈ 0.48 — consistent with an NMS operator inside the graph at a
fixed IoU threshold we do not control.

---

## What a careful reader should not claim from this document

- That the SEI probe placement was chosen *after* a decoded-side failure.
- That the UUID filter was added *in response to* an observed collision.
- That DeepStream, ONNX Runtime, or RTSP were evaluated.
- Any figure for hours or working days spent on any issue.
- That an MPLBACKEND subprocess crash occurred.
- That a July exhibition demo occurred, or what configuration ran at it.
