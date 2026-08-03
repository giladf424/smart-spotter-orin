"""
H.265 frame ingest: GStreamer NVDEC receive pipeline + frame_id recovery.

Pipeline:
  udpsrc -> rtpjitterbuffer -> rtph265depay -> h265parse -> nvv4l2decoder
         -> nvvidconv -> appsink (BGRx, CPU)

frame_id recovery (the key design point):
  The frame_id lives in an SEI NAL in the ENCODED stream. Once nvv4l2decoder
  produces a raw frame the SEI is gone. So we DO NOT try to read SEI from the
  decoded frame. Instead a pad probe on h265parse's SRC pad sees each encoded
  access unit, runs sei.extract over its NALs, and pushes the recovered frame_id
  onto an ordered FIFO. At appsink we pop the FIFO in order and pair it with the
  decoded frame.

  This is correct because the encoder runs bframes=0 / zerolatency, so decode
  order == display order and the mapping is exactly 1 AU in -> 1 frame out, in
  order. The pad probe runs before the decoder, so whether NVDEC preserves or
  strips SEI is irrelevant to us — we never depend on it.

Stage counters (live mode): pad probes on udpsrc (RTP packets in), rtph265depay
(NAL buffers out), and h265parse (AUs out) feed a periodic heartbeat line, so a
stalled stage is visible directly instead of inferred from a silent run.

This module also drives the offline probe: pointed at the sample .hevc via
filesrc instead of udpsrc, it proves the frame_id<->frame association end to end
without the live link.
"""

import collections
import threading

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

import numpy as np  # noqa: E402

import sei  # noqa: E402

Gst.init(None)


# Reference receive caps (from the Pi): RTP/H265, pt=96, clock-rate=90000.
_RTP_CAPS = (
    "application/x-rtp,media=video,encoding-name=H265,"
    "clock-rate=90000,payload=96"
)

# Heartbeat period for the stage-counter log line, seconds.
_HEARTBEAT_SEC = 5


class FrameIngest:
    """Drives the GStreamer pipeline and yields (frame_id, frame_bgr) pairs.

    Two sources:
      - live: udpsrc on a UDP port (RTP/H265 in)
      - file: filesrc on a .hevc Annex-B elementary stream (offline probe)

    Frames are delivered to a user callback on the GStreamer streaming thread.
    The caller runs the GLib main loop via run().
    """

    def __init__(self, on_frame, source="live", udp_port=5600,
                 file_path=None, width=1920, height=1080, capture_path=None):
        """on_frame(frame_id_or_None, frame_bgr): called per decoded frame.
        frame_id is None if no matching SEI was found for that frame (should not
        happen on this stream, but we surface it rather than guess).
        capture_path: if set, the encoded Annex-B stream (SEI included) is also
        written to this file via a tee after the parser, so a live session can
        be replayed later with source='file'."""
        self._on_frame = on_frame
        self._width = width
        self._height = height

        # Ordered FIFO of frame_ids parsed from encoded AUs, filled by the pad
        # probe (h265parse src) and drained at appsink. Lock guards both ends.
        self._fid_queue = collections.deque()
        self._fid_lock = threading.Lock()

        # Stage counters: [count, bytes] per stage, written on the streaming
        # threads, read by the heartbeat on the main loop. int updates are
        # GIL-atomic enough for a diagnostic counter.
        self._stat_rtp = [0, 0]
        self._stat_depay = [0, 0]
        self._stat_au = [0, 0]
        self._live = source == "live"

        # Mid-stream join guard: until the first keyframe AU arrives, delta
        # AUs are dropped at the parser. Feeding reference-less P-frames to
        # nvv4l2decoder exhausts its input pool and deadlocks the pipeline;
        # dropping them lets decode (and the capture file) start cleanly at
        # the first keyframe.
        self._await_key = True
        self._dropped_pre_key = 0

        self._loop = GLib.MainLoop()
        self._pipeline = self._build_pipeline(source, udp_port, file_path,
                                              capture_path)

    # --- pipeline construction ---------------------------------------------
    def _build_pipeline(self, source, udp_port, file_path, capture_path=None):
        if source == "live":
            # buffer-size: the Pi bursts each encoded AU at GigE line rate, so
            # the kernel socket buffer must hold at least one whole keyframe
            # (~130-220 KB) or its tail packets are silently dropped and the
            # depayloader discards the incomplete FU. Requires
            # net.core.rmem_max >= this value, else the kernel clamps it.
            src = (
                f"udpsrc name=rtpsrc port={udp_port} buffer-size=8388608 "
                f"caps=\"{_RTP_CAPS}\" "
                f"! rtpjitterbuffer latency=50 "
                f"! rtph265depay name=depay "
                # config-interval=-1: re-insert cached VPS/SPS/PPS before every
                # keyframe. The Pi repeats parameter sets on a time interval,
                # not per keyframe, so a keyframe AU is not otherwise
                # guaranteed to be self-contained — and the decoder can only
                # start on a keyframe that is.
                f"! h265parse name=parser config-interval=-1"
            )
        elif source == "file":
            if not file_path:
                raise ValueError("source='file' requires file_path")
            # Annex-B elementary stream straight into the parser. Same
            # config-interval=-1 rationale as the live path (replay of a
            # mid-stream capture may open on a keyframe without params).
            src = (f"filesrc location={file_path} "
                   f"! h265parse name=parser config-interval=-1")
        else:
            raise ValueError(f"unknown source: {source}")

        # Optional capture: tee the parsed byte-stream (encoded side, SEI
        # intact) to a file alongside the decode branch. Both tee branches get
        # a queue so a slow disk cannot stall the decoder.
        if capture_path:
            src += (
                " ! video/x-h265,stream-format=byte-stream,alignment=au "
                "! tee name=cap_tee "
                f"cap_tee. ! queue ! filesink location={capture_path} sync=false "
                "cap_tee. ! queue"
            )

        # nvvidconv pulls NVMM->CPU and converts to BGRx; appsink hands us
        # raw bytes. We keep only the newest frames if the consumer lags.
        desc = (
            f"{src} "
            f"! nvv4l2decoder "
            f"! nvvidconv "
            f"! video/x-raw,format=BGRx,width={self._width},height={self._height} "
            f"! appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        )
        pipeline = Gst.parse_launch(desc)

        # Pad probe on the parser SRC pad: every buffer here is one encoded AU.
        parser = pipeline.get_by_name("parser")
        srcpad = parser.get_static_pad("src")
        srcpad.add_probe(Gst.PadProbeType.BUFFER, self._on_au_probe, None)

        # Live mode: counting probes on udpsrc and depay src pads, plus a
        # heartbeat that prints all stage counters so a dead stage shows up in
        # the log within one period.
        if self._live:
            for name, stat in (("rtpsrc", self._stat_rtp),
                               ("depay", self._stat_depay)):
                pad = pipeline.get_by_name(name).get_static_pad("src")
                pad.add_probe(Gst.PadProbeType.BUFFER, self._on_count_probe,
                              (name, stat))
            GLib.timeout_add_seconds(_HEARTBEAT_SEC, self._on_heartbeat)

        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        return pipeline

    # --- stage counters ------------------------------------------------------
    def _on_count_probe(self, pad, info, user):
        name, stat = user
        if stat[0] == 0:
            print(f"[ingest] first buffer at {name} "
                  f"({info.get_buffer().get_size()} bytes)", flush=True)
        stat[0] += 1
        stat[1] += info.get_buffer().get_size()
        return Gst.PadProbeReturn.OK

    def _on_heartbeat(self):
        print(f"[ingest] stages: rtp={self._stat_rtp[0]} pkts/"
              f"{self._stat_rtp[1]}B  depay={self._stat_depay[0]} bufs/"
              f"{self._stat_depay[1]}B  parsed_au={self._stat_au[0]}/"
              f"{self._stat_au[1]}B", flush=True)
        return True  # keep the timeout firing

    # --- encoded-side SEI extraction ---------------------------------------
    def _on_au_probe(self, pad, info, _user):
        """Runs per encoded access unit. Drops AUs until the first keyframe
        (mid-stream join), then: parse SEI, enqueue frame_id in order.

        Keyframes are detected from the NAL types (IRAP range 16-21: IDR,
        BLA, CRA) rather than the DELTA_UNIT buffer flag, because h265parse
        only clears the flag for IDR and the encoder may emit open-GOP CRA
        keyframes. Dropped AUs never reach the decoder, the capture tee, or
        the frame_id FIFO, so the 1 AU -> 1 frame pairing is preserved."""
        buf = info.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)

        frame_id = None
        is_keyframe = False
        for nal_type, payload in sei.iter_nal_units(data):
            if 16 <= nal_type <= 21:
                is_keyframe = True
            if frame_id is None:
                fid = sei.extract_frame_id_from_nal(nal_type, payload)
                if fid is not None:
                    frame_id = fid

        if self._await_key:
            if not is_keyframe:
                self._dropped_pre_key += 1
                if self._dropped_pre_key == 1:
                    print("[ingest] joined mid-GOP, dropping AUs until a "
                          "keyframe arrives", flush=True)
                return Gst.PadProbeReturn.DROP
            self._await_key = False
            if self._dropped_pre_key:
                print(f"[ingest] keyframe arrived after "
                      f"{self._dropped_pre_key} dropped AUs, starting decode",
                      flush=True)

        self._stat_au[0] += 1
        self._stat_au[1] += buf.get_size()
        # Enqueue exactly one entry per AU (frame_id or None), preserving order.
        with self._fid_lock:
            self._fid_queue.append(frame_id)
        return Gst.PadProbeReturn.OK

    # --- decoded-side frame delivery ---------------------------------------
    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        s = caps.get_structure(0)
        width = s.get_value("width")
        height = s.get_value("height")

        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            # BGRx = 4 bytes/pixel. Build an (h, w, 4) view, drop alpha -> BGR.
            arr = np.frombuffer(mapinfo.data, dtype=np.uint8)
            arr = arr[: width * height * 4].reshape(height, width, 4)
            frame_bgr = np.ascontiguousarray(arr[:, :, :3])
        finally:
            buf.unmap(mapinfo)

        with self._fid_lock:
            frame_id = self._fid_queue.popleft() if self._fid_queue else None

        self._on_frame(frame_id, frame_bgr)
        return Gst.FlowReturn.OK

    # --- bus / lifecycle ---------------------------------------------------
    def _on_bus_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            self._loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[ingest] ERROR: {err} | {dbg}")
            self._loop.quit()

    def run(self):
        self._pipeline.set_state(Gst.State.PLAYING)
        try:
            self._loop.run()
        finally:
            self._pipeline.set_state(Gst.State.NULL)

    def stop(self):
        self._loop.quit()
