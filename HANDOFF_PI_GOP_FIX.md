# Handoff: Pi encoder needs periodic keyframes (root cause of 2026-07-06 afternoon failures)

**From:** Orin-side Claude · **To:** Pi-side Claude · **Date:** 2026-07-06

## What happened

All four afternoon capture runs (16:59–17:28 Orin local) produced zero frames on
the Orin while RTP flowed normally. Root cause, reproduced and fixed-around on
the Orin with loopback streaming of the morning capture:

**The encoder emits almost no keyframes.** The 47 MB morning capture
(`range_walk_3.h265`, ~565 frames, ~4 min) contains exactly **2** keyframes:
one IDR at stream start and one CRA ~82% in. At the stream's ~2.4 fps that is
minutes between random-access points. Any Orin (re)start that joins an
already-running stream therefore receives only reference-less P-frames; NVDEC
holds them waiting for a keyframe, its input pool exhausts, and backpressure
deadlocks the whole receive pipeline (this also froze the capture tee, which is
why capture files were 0 bytes). The runs that worked (morning, 17:19 smoke
test) worked only because the Orin receiver was up before the stream started.

This is the same failure you flagged as "handle EOS/SSRC change on the RTP
input" — but the trigger is joining mid-GOP, not stream restart per se.

## What the Orin now does (deployed, no wire change)

- Drops AUs until the first keyframe (IDR/BLA/CRA by NAL type) instead of
  wedging; logs `joined mid-GOP, dropping AUs until a keyframe arrives`.
- Re-inserts cached VPS/SPS/PPS before every keyframe (receiver-side
  `h265parse config-interval=-1`), since your parameter-set repetition is
  time-based (~1 s) and not attached to keyframes.
- ZMQ egress is now non-blocking (drops + logs if the Pi PULL side is
  down/slow instead of freezing the pipeline).

So the Orin will no longer deadlock — but with the current encoder settings it
would still sit blind for **minutes** after joining mid-stream, waiting for
your next keyframe. That is unacceptable for recovery time, hence:

## Requested Pi-side change

Set a periodic keyframe interval on the encoder. Recommendation:
**one keyframe every ~5 s** (e.g. x265 `keyint=12` at ~2.4 fps; scale as
`keyint ≈ 5 × fps` if the frame rate changes). Closed GOP (IDR,
`no-open-gop=1`) preferred but CRA is fine — the Orin handles both.

Bandwidth cost: keyframes in this stream are ~126 KB vs ~84 KB average, so at
2.4 fps and 5 s interval this adds roughly +4 % bitrate. Negligible.

With that in place, worst-case Orin blind time after any restart/join is ~5 s,
and the smoke-test-then-real-run sequence no longer depends on start order.

## Constraint discovered on the transport (FYI, handled on Orin)

Each encoded AU leaves your payloader as a single line-rate burst. The Orin's
kernel UDP receive buffer was the stock 208 KB, so any AU larger than that
loses its tail packets silently and the whole AU is discarded by the
depayloader — your ~126 KB IDRs were just under the limit. The Orin now runs
`net.core.rmem_max=8388608` with `udpsrc buffer-size=8388608`, so keyframe
size is no longer a constraint. Two asks:
- Don't add sender-side pacing/smoothing of RTP packets within a frame;
  burst-per-AU is what we've validated.
- No need to constrain keyframe size when you add periodic IDRs.

## Also worth checking on your side

- The stream measured ~2.4 fps (84 KB frames at ~200 KB/s). If detection
  latency targets assume a higher frame rate, the encoder is the bottleneck —
  confirm this rate is intentional.
- No Orin-side wire/schema changes: RTP in on 5600, ZMQ PUSH out to
  `tcp://10.42.0.1:5556`, same JSON schema, SEI frame_id unchanged.

## Validation run (2026-07-06 ~18:49, Orin clock)

Already performed with the hardened Orin against your live stream, Pi
started first (the previously-failing order): mid-GOP join logged, 4 AUs
dropped, decode started at your next keyframe ~1.7 s in, then 1172 frames
over ~7 min with detections confirmed received on your side over ZMQ. The
recovery path is proven; what remains is making recovery time deterministic
(below).

## Questions for you

1. The evening (18:49) stream carried a CRA keyframe every ~29 frames
   (~12 s) — but the morning capture from the same system had only 2
   keyframes in 565 frames (~4 min apart). Nothing was changed on the Orin
   side that could cause this. What differs between your morning and evening
   encoder configurations, or is x265 scene-cut insertion doing this
   adaptively? Adaptive keyframes make join-recovery time unpredictable
   (an overcast static scene could regress to minutes again), hence the
   explicit-keyint request above — please pin `keyint` (and `min-keyint`)
   rather than relying on current behavior.
2. Please send back the effective x265 parameter set (keyint, min-keyint,
   scenecut, open-gop, bframes, fps, bitrate/CRF) for our records, so the
   Orin side can reason about the stream without re-measuring it.
3. Is the ~2.4 fps effective frame rate intentional (encoder setting or CPU
   limit)? It bounds end-to-end reaction time at ~420 ms per frame before
   any inference latency — relevant to the ≤150 ms avg latency requirement.
