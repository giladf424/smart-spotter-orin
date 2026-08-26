"""
H.265 frame ingest: GStreamer NVDEC receive pipeline + frame_id recovery.

Pipeline:
  udpsrc -> rtpjitterbuffer -> rtph265depay -> h265parse -> nvv4l2decoder
         -> nvvidconv -> appsink (BGRx, CPU)

frame_id recovery, the key design point:
  The frame_id lives in an SEI NAL in the encoded stream, and is gone once
  nvv4l2decoder produces a raw frame. So we never read SEI from the decoded
  frame. A pad probe on h265parse's src pad sees each encoded access unit,
  parses the frame_id out of its NALs, and appends it to an ordered FIFO;
  appsink pops that FIFO in order and pairs each id with a decoded frame.

  This rests on two things. The sender must not reorder frames (no B-frames),
  so decode order equals display order; and each parser buffer must be one
  whole access unit, so ids and frames stay in step. Check both with
  `tools/probe.py --live`, which reports frame_ids recovered and whether they
  are contiguous. Any drift shows up there immediately.

  Because the probe sits ahead of the decoder, it does not matter whether
  NVDEC preserves or strips SEI.

Stage counters (live mode): pad probes on udpsrc, rtph265depay and h265parse
feed a periodic heartbeat line, so a stalled stage shows up in the log instead
of the run just going quiet.

Pointing this at a .hevc file with filesrc instead of udpsrc runs the same
association offline, without the Pi.
"""

import collections
import threading

import gi

gi.require_version("Gst", "1.0")
import numpy as np  # noqa: E402
from gi.repository import GLib, Gst  # noqa: E402

from ingest import sei  # noqa: E402

Gst.init(None)


# Receive caps forced on udpsrc; must match what the Pi sends.
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
        frame_id is None when no matching SEI was found for that frame; we
        pass that through rather than guess an id.
        capture_path: if set, the encoded stream is also written there via a
        tee after the parser, so a live session can be replayed later with
        source='file'."""
        self._on_frame = on_frame
        self._width = width
        self._height = height

        # Ordered FIFO of frame_ids parsed from encoded AUs, filled by the pad
        # probe (h265parse src) and drained at appsink. Lock guards both ends.
        self._fid_queue = collections.deque()
        self._fid_lock = threading.Lock()

        # Stage counters: [count, bytes] per stage, written on the streaming
        # threads and read by the heartbeat on the main loop. Unsynchronised,
        # which the GIL makes good enough for a diagnostic counter.
        self._stat_rtp = [0, 0]
        self._stat_depay = [0, 0]
        self._stat_au = [0, 0]
        self._live = source == "live"

        # Join guard: drop access units until the first keyframe arrives.
        # Decode must start on a keyframe. A P-frame whose references the
        # decoder never saw cannot be decoded, and holds an input buffer that
        # is never released.
        self._await_key = True
        self._dropped_pre_key = 0

        self._loop = GLib.MainLoop()
        self._pipeline = self._build_pipeline(source, udp_port, file_path,
                                              capture_path)

    # --- pipeline construction ---------------------------------------------
    def _build_pipeline(self, source, udp_port, file_path, capture_path=None):
        if source == "live":
            # buffer-size must hold at least one whole keyframe. The sender
            # can burst an access unit faster than we drain it, and an
            # undersized socket buffer drops the tail packets, leaving the
            # depayloader to discard an incomplete unit. The kernel caps a
            # socket buffer at net.core.rmem_max, which the host raises to
            # this same value in /etc/sysctl.d/90-smart-spotter-udp.conf.
            src = (
                f"udpsrc name=rtpsrc port={udp_port} buffer-size=8388608 "
                f"caps=\"{_RTP_CAPS}\" "
                f"! rtpjitterbuffer latency=50 "
                f"! rtph265depay name=depay "
                # config-interval=-1 re-inserts the cached VPS, SPS and PPS
                # before every keyframe. The decoder can only start on a
                # keyframe that carries them.
                f"! h265parse name=parser config-interval=-1"
            )
        elif source == "file":
            if not file_path:
                raise ValueError("source='file' requires file_path")
            # Annex-B elementary stream straight into the parser, with the
            # same config-interval reasoning as the live path: a mid-stream
            # capture may open on a keyframe that lacks parameter sets.
            src = (f"filesrc location={file_path} "
                   f"! h265parse name=parser config-interval=-1")
        else:
            raise ValueError(f"unknown source: {source}")

        # Pin one access unit per buffer on the parser's src pad. The frame_id
        # FIFO holds one entry per access unit, so anything else would put the
        # ids out of step with the frames.
        src += " ! video/x-h265,stream-format=byte-stream,alignment=au"

        # Optional capture: tee the parsed byte-stream (encoded side, SEI
        # intact) to a file alongside the decode branch. Both tee branches get
        # a queue so a slow disk cannot stall the decoder.
        if capture_path:
            src += (
                " ! tee name=cap_tee "
                f"cap_tee. ! queue ! filesink location={capture_path} "
                "sync=false "
                "cap_tee. ! queue"
            )

        # nvvidconv pulls NVMM->CPU and converts to BGRx; appsink hands us raw
        # bytes. drop=true keeps only the newest frames when we fall behind.
        desc = (
            f"{src} "
            f"! nvv4l2decoder "
            f"! nvvidconv "
            f"! video/x-raw,format=BGRx,"
            f"width={self._width},height={self._height} "
            f"! appsink name=sink emit-signals=true max-buffers=2 "
            f"drop=true sync=false"
        )
        pipeline = Gst.parse_launch(desc)

        # Pad probe on the parser src pad. The caps above pin one access unit
        # per buffer, which is what keeps the FIFO in step with the frames.
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
        """Runs once per encoded access unit: drop until the first keyframe,
        then parse the frame_id and queue it in order.

        Keyframes are identified by NAL type rather than by the DELTA_UNIT
        buffer flag, which is not reliable for non-IDR keyframes. Types 16-21
        cover every IRAP type HEVC currently defines (BLA 16-18, IDR 19-20,
        CRA 21, with 22-23 reserved). Dropped units reach neither the
        decoder, the capture tee, nor the FIFO, so one unit still yields one
        frame."""
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
        # One entry per access unit, in order, even if the id is None.
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
            # BGRx is 4 bytes per pixel and nvvidconv emits tightly packed
            # rows, so the buffer reshapes directly. The x byte is dropped.
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
