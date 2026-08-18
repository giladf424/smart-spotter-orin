#!/usr/bin/env python3
"""
Inference orchestrator: wires ingest, engine, pre/post and the ZMQ sink.

Modes:
  --test-image <path>       one still through the chain; prints JSON
  --source file --file <p>  decode a captured .hevc and run the full chain
                            offline (frame -> detections -> message)
  --source live             decode the live RTP stream from the Pi and push
                            detections over ZMQ

Both streaming modes take (frame_id, frame_bgr) pairs from
ingest.pipeline.FrameIngest and run preprocess -> engine -> postprocess on
each. Every decoded frame produces one message carrying the frame_id echoed
from the stream's SEI, which the Pi joins back to the pose it recorded at
capture time.

Frames whose SEI yielded no frame_id are sent with frame_id 0, which the Pi
cannot tell apart from a genuine frame 0. Those frames are counted separately
as frames_without_frame_id in the end-of-run line.

The entrypoint launches this as:
    python3 /app/infer.py --engine ... --input-size "${DETECTOR_INPUT}" "$@"
so --engine and --input-size override the config defaults.

timestamp_ms is monotonic milliseconds: it has an arbitrary epoch, so it is
only meaningful for measuring intervals between Orin messages, not against
any clock on the Pi.
"""

import argparse
import json
import sys
import time

import config
import cv2
from detect.engine import TRTEngine
from detect.postprocess import postprocess, preprocess
from egress.zmq_sink import ZmqSink


def _now_ms():
    return int(time.monotonic() * 1000)


def _resolve_input_size(args, engine):
    """Engine is authoritative for input edge; warn on mismatch."""
    requested = args.input_size if args.input_size else config.INPUT_SIZE
    eng_n = engine.input_shape[-1]
    if eng_n != requested:
        print(
            f"WARNING: engine input edge {eng_n} != requested {requested}. "
            f"Using engine's {eng_n}.",
            file=sys.stderr,
        )
    return eng_n


def run_inference(engine, frame_bgr, input_size, frame_id):
    """Run one frame through the chain and build its message.

    Returns (message, n_detections). Shared by all modes."""
    tensor, transform = preprocess(frame_bgr, input_size)
    raw = engine.infer(tensor)
    ts = _now_ms()
    detections = postprocess(
        raw, transform, config.CLASS_MAP, config.CONFIDENCE_THRESHOLD,
    )
    message = ZmqSink.build_message(
        frame_id=frame_id if frame_id is not None else 0,
        timestamp_ms=ts, detections=detections,
    )
    return message, len(detections)


# --- single still -----------------------------------------------------------
def run_test_image(args):
    frame = cv2.imread(args.test_image, cv2.IMREAD_COLOR)
    if frame is None:
        print(f"ERROR: could not read image: {args.test_image}",
              file=sys.stderr)
        return 2

    src_h, src_w = frame.shape[:2]
    if (src_w, src_h) != (config.SOURCE_WIDTH, config.SOURCE_HEIGHT):
        print(
            f"WARNING: test image is {src_w}x{src_h}, not "
            f"{config.SOURCE_WIDTH}x{config.SOURCE_HEIGHT}. Treating its "
            f"native dimensions as the source space.",
            file=sys.stderr,
        )

    with TRTEngine(args.engine) as eng:
        input_size = _resolve_input_size(args, eng)
        message, n = run_inference(eng, frame, input_size, args.frame_id)

    print(json.dumps(message, indent=2))
    print(f"[infer] {n} detection(s) kept "
          f"(conf >= {config.CONFIDENCE_THRESHOLD}).", file=sys.stderr)

    if args.push:
        with ZmqSink(endpoint=args.endpoint) as sink:
            sink.send(message)
        print(f"[infer] pushed to {args.endpoint}", file=sys.stderr)
    return 0


# --- streaming (file or live) ----------------------------------------------
def run_stream(args):
    # Imported here so --test-image works on hosts without GStreamer/gi.
    from ingest.pipeline import FrameIngest

    eng = TRTEngine(args.engine)
    input_size = _resolve_input_size(args, eng)

    # Sink: connect for live and for file+--push; otherwise print-only.
    do_push = args.push or (args.source == "live")
    sink = (ZmqSink(endpoint=args.endpoint, connect=do_push)
            if do_push else None)

    stats = {"frames": 0, "dets": 0, "no_fid": 0}

    def on_frame(frame_id, frame_bgr):
        if frame_id is None:
            stats["no_fid"] += 1
        message, n = run_inference(eng, frame_bgr, input_size, frame_id)
        stats["frames"] += 1
        stats["dets"] += n
        if sink is not None:
            sink.send(message)
        # Per-frame trace to stderr; JSON only when not pushing (avoid spam).
        print(f"[infer] frame {stats['frames']:>5} frame_id={frame_id} "
              f"dets={n}", file=sys.stderr)
        if not do_push:
            print(json.dumps(message))

    if args.source == "file":
        ing = FrameIngest(on_frame, source="file", file_path=args.file,
                          width=args.width, height=args.height)
    else:
        ing = FrameIngest(on_frame, source="live", udp_port=args.port,
                          width=args.width, height=args.height,
                          capture_path=args.capture)
        if args.capture:
            print(f"[infer] capturing encoded stream to {args.capture}",
                  file=sys.stderr)

    try:
        ing.run()
    except KeyboardInterrupt:
        ing.stop()
    finally:
        eng.close()
        if sink is not None:
            sink.close()

    print(f"\n[infer] stream ended: frames={stats['frames']} "
          f"total_dets={stats['dets']} "
          f"frames_without_frame_id={stats['no_fid']}", file=sys.stderr)
    return 0


def build_arg_parser():
    p = argparse.ArgumentParser(description="Orin inference pipeline.")
    p.add_argument("--engine", default=config.ENGINE_PATH)
    p.add_argument("--input-size", type=int, default=None)
    # Single-image mode.
    p.add_argument("--test-image", default=None,
                   help="run one still through the chain and print JSON")
    p.add_argument("--frame-id", type=int, default=0,
                   help="synthetic frame_id for --test-image")
    # Streaming mode.
    p.add_argument("--source", choices=["file", "live"], default=None,
                   help="stream source: a .hevc (file) or live RTP (live)")
    p.add_argument("--file", default=None,
                   help="path to .hevc for --source file")
    p.add_argument("--port", type=int, default=5600,
                   help="UDP port for --source live (default 5600)")
    p.add_argument("--capture", default=None,
                   help="also write the encoded stream (with SEI) to this "
                        ".h265 file for offline replay (--source live only)")
    p.add_argument("--width", type=int, default=config.SOURCE_WIDTH)
    p.add_argument("--height", type=int, default=config.SOURCE_HEIGHT)
    # Shared egress.
    p.add_argument("--push", action="store_true",
                   help="push messages over ZMQ (implicit for --source live)")
    p.add_argument("--endpoint", default=config.ZMQ_ENDPOINT)
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if args.test_image:
        return run_test_image(args)
    if args.source:
        if args.source == "file" and not args.file:
            print("ERROR: --source file requires --file <path>",
                  file=sys.stderr)
            return 2
        return run_stream(args)

    print(
        "Nothing to do. Use --test-image <path>, or --source file --file <p>, "
        "or --source live.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
