"""
ZeroMQ detection egress: owns the Orin->Pi wire contract.

The Orin PUSHes and connects; the Pi PULLs and binds at tcp://<pi-ip>:5556.
PUSH/connect means the Orin can restart freely without the Pi reconfiguring.

Message schema (locked, from PI_SIDE_BRIEF.md):
  {
    "type": "target_detection",
    "frame_id": <uint32>,
    "timestamp_ms": <int>,
    "detections": [
      {"id":"1","class":"HUMAN","confidence":0.85,
       "bbox":{"x":..,"y":..,"width":..,"height":..}}
    ]
  }
The Pi caps at 32 detections per message and ignores unknown fields.
"""

import json

import config
import zmq


class ZmqSink:
    """PUSH socket that connects to the Pi's PULL bind and sends detections."""

    def __init__(self, endpoint=config.ZMQ_ENDPOINT, connect=True):
        """endpoint: tcp://<pi-ip>:<port>. connect=False builds the socket but
        does not connect (used by --test-image when not pushing)."""
        self._endpoint = endpoint
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.PUSH)
        self._sock.setsockopt(zmq.LINGER, 0)
        # Bounded queue + non-blocking send: if the Pi is down or slow, we
        # drop messages instead of stalling the pipeline (send() runs inside
        # the GStreamer streaming thread, so a blocking send freezes decode).
        self._sock.setsockopt(zmq.SNDHWM, 100)
        self._dropped = 0
        self._connected = False
        if connect:
            self._sock.connect(self._endpoint)
            self._connected = True

    @staticmethod
    def build_message(frame_id, timestamp_ms, detections,
                      id_start=config.ID_START,
                      max_detections=config.MAX_DETECTIONS_PER_MSG):
        """Build the wire dict from a list of postprocess.Detection.

        detections are assumed already sorted by confidence desc; we trim to
        max_detections (the highest-confidence ones survive the Pi's 32 cap).
        Per-frame index ids are assigned here as strings starting at id_start.
        bbox values are rounded to ints (source-frame pixels).
        """
        trimmed = detections[:max_detections]
        det_list = []
        for i, d in enumerate(trimmed):
            det_list.append({
                "id": str(id_start + i),
                "class": d.label,
                "confidence": round(float(d.confidence), 4),
                "bbox": {
                    "x": int(round(d.x)),
                    "y": int(round(d.y)),
                    "width": int(round(d.width)),
                    "height": int(round(d.height)),
                },
            })
        return {
            "type": "target_detection",
            "frame_id": int(frame_id),
            "timestamp_ms": int(timestamp_ms),
            "detections": det_list,
        }

    def send(self, message):
        """Serialize and PUSH one message dict, best-effort: if the send queue
        is full (Pi down/slow), the message is dropped rather than blocking.
        The Pi joins by frame_id, so gaps are equivalent to dropped frames."""
        if not self._connected:
            raise RuntimeError("ZmqSink.send called on a non-connected sink")
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        try:
            self._sock.send(payload, zmq.DONTWAIT)
        except zmq.Again:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                print(f"[zmq_sink] send queue full, dropped "
                      f"{self._dropped} messages so far", flush=True)

    def close(self):
        sock = getattr(self, "_sock", None)
        if sock is not None:
            sock.close(linger=0)
            self._sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
