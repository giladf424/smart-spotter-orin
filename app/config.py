"""
Central configuration for the Orin-side inference pipeline.

The current model is YOLO26-m single-class person at 640x640; the planned one
is dual-class (person=0, drone=1) at 960x960.

A model swap touches the ONNX at ONNX_PATH, CLASS_MAP and INPUT_SIZE here,
and DETECTOR_INPUT in the entrypoint.

Values a CLI arg can override (engine path, input size) keep their default
here, so infer.py runs standalone.
"""

# --- Engine -----------------------------------------------------------------
# The entrypoint builds or reuses the engine at this path and passes it to
# infer.py via --engine, so this default only applies to standalone runs.
ENGINE_PATH = "/models/model.engine"

# --- Detector input ---------------------------------------------------------
# Square detector input edge, in pixels. A fallback: infer.py reads the real
# edge off the engine and warns if this disagrees. The entrypoint passes
# DETECTOR_INPUT here as --input-size and into the engine cache fingerprint.
INPUT_SIZE = 640

# --- Classes ----------------------------------------------------------------
# Model class id (column 5 of the [1,300,6] output) -> the label string the Pi
# expects. Detections whose class id is missing here are dropped.
CLASS_MAP = {
    0: "HUMAN",
    # 1: "DRONE",   # uncomment for the dual-class model
}

# --- Detection filtering ----------------------------------------------------
# Applied to column 4 of each output row. This is the only postprocessing
# filter, because YOLO26 bakes NMS into the graph.
# Tuned deliberately low: a missed person costs more than a spurious box.
CONFIDENCE_THRESHOLD = 0.35

# --- Source frame geometry --------------------------------------------------
# Defaults for infer.py's --width/--height, which set the decoder's output
# caps. Boxes come back in whatever size the decoder actually produced, so
# these decide the coordinate space the Pi receives. The Pi sends 1080p.
SOURCE_WIDTH = 1920
SOURCE_HEIGHT = 1080

# --- Detection id semantics -------------------------------------------------
# detections[].id is a per-frame index, not a stable track id: the Orin is
# stateless and the Pi owns tracking. Ids are 1-based strings; this is the only
# line that controls where they start.
ID_START = 1

# --- ZeroMQ egress ----------------------------------------------------------
# The Orin PUSHes and connects; the Pi PULLs and binds. Address and port are
# fixed by the Pi-side contract.
ZMQ_PI_IP = "10.42.0.1"             # Pi's static GigE address
ZMQ_PORT = 5556
ZMQ_ENDPOINT = f"tcp://{ZMQ_PI_IP}:{ZMQ_PORT}"

# Per-message detection cap set by the Pi-side contract. build_message trims
# to this, keeping the highest-confidence ones.
MAX_DETECTIONS_PER_MSG = 32
