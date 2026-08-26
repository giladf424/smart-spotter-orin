"""
ZeroMQ detection egress: owns the Orin->Pi wire contract.

The Orin PUSHes and connects; the Pi PULLs and binds (see config.ZMQ_ENDPOINT).
PUSH/connect means the Orin can restart freely without the Pi reconfiguring.

Message schema (locked by the Pi-side contract):
  {
    "type": "target_detection",
    "frame_id": <uint32>,
    "timestamp_ms": <int>,
    "detections": [
      {"id":"1","class":"HUMAN","confidence":0.85,
       "bbox":{"x":..,"y":..,"width":..,"height":..}}
    ]
  }
Detection count is capped by config.MAX_DETECTIONS_PER_MSG. timestamp_ms is a
monotonic reading from this box and is informational. frame_id is the join key
both sides use.
"""

import json

import config
import zmq


class ZmqSink:
    """PUSH socket that connects to the Pi's PULL bind and sends detections."""

    def __init__(self, endpoint=config.ZMQ_ENDPOINT, connect=True):
        """endpoint: tcp://<pi-ip>:<port>. connect=False builds the socket
        without connecting, leaving send() to raise."""
        self._endpoint = endpoint
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.PUSH)
        self._sock.setsockopt(zmq.LINGER, 0)
        # send() runs on the GStreamer streaming thread, so it must never
        # block. A bounded queue plus a non-blocking send keeps decode running
        # at its own rate whatever the network is doing.
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

        detections must already be sorted by confidence, descending: the list
        is trimmed to max_detections, so the order decides which survive.
        Ids are per-frame index strings starting at id_start, and bbox values
        are rounded to whole source-frame pixels.
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
        """Serialise and PUSH one message, best-effort: a full send queue
        drops the message rather than blocking. A dropped message means the Pi
        gets no detections at all for that frame."""
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
