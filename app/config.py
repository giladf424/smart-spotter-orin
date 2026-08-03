"""
Central configuration for the Orin-side inference pipeline.

This is the SINGLE place the interim->final model swap touches. The interim
demo model is YOLO26-m single-class person at 640x640; the final model is
YOLO26-m dual-class (person=0, drone=1) at 960x960. Swapping models must stay
config-only: change INPUT_SIZE and CLASS_MAP here (and rebuild the engine via
the entrypoint with a matching DETECTOR_INPUT), nothing else.

Anything that can be overridden by a CLI arg (engine path, input size) carries
its DEFAULT here so infer.py is runnable standalone; the entrypoint's --engine
/ --input-size override these when present.
"""

# --- Engine -----------------------------------------------------------------
# Default engine path. The entrypoint builds-or-reuses the engine at this exact
# path (/models/model.engine) and passes it to infer.py via --engine, so this
# default is only used when infer.py is invoked directly without the entrypoint.
ENGINE_PATH = "/models/model.engine"

# --- Detector input ---------------------------------------------------------
# Square detector input edge in pixels. 640 interim, 960 final.
# NOTE: this value also lives in the entrypoint (DETECTOR_INPUT env), because
# the entrypoint must know it to BUILD the engine before Python ever runs. The
# two must agree. When infer.py receives --input-size from the entrypoint it
# overrides this; we keep this default so standalone runs work.
INPUT_SIZE = 640

# --- Classes ----------------------------------------------------------------
# Maps the model's integer class id (col index 5 of the [1,300,6] output) to the
# wire-contract label string the Pi expects. Interim: person only. Final: add
# drone. The Pi passes these through to the app/UI.
CLASS_MAP = {
    0: "HUMAN",
    # 1: "DRONE",   # uncomment for the final dual-class model
}

# --- Detection filtering ----------------------------------------------------
# Confidence threshold applied to col index 4 of each [1,300,6] row. This is the
# ONLY postprocessing filter for YOLO26 output (NMS is baked into the graph).
# PLACEHOLDER value — tune against the person model's operating point.
CONFIDENCE_THRESHOLD = 0.35

# --- Source frame geometry --------------------------------------------------
# The Pi sends full 1080p; we un-letterbox detector-space boxes back to this
# space so the Pi's FOV->angle geometry is correct. In --test-image mode the
# actual still's dimensions override these (see infer.py).
SOURCE_WIDTH = 1920
SOURCE_HEIGHT = 1080

# --- Detection id semantics -------------------------------------------------
# detections[].id is a PER-FRAME INDEX (string), not a stable track id. The Orin
# is stateless; the Pi owns tracking. ID_START is the first index value.
# ASSUMPTION (flagged for the Pi): ids start at 1 ("1","2","3",...). If the Pi
# wants 0-based, change this to 0 — it is the only line that controls it.
ID_START = 1

# --- ZeroMQ egress ----------------------------------------------------------
# Orin PUSHes/connects; Pi PULLs/binds at tcp://<pi-ip>:5556. Port is locked by
# the Pi brief. The IP is the Pi's static link-local GigE address, assigned at
# link bring-up — PLACEHOLDER until the GigE link is configured.
ZMQ_PI_IP = "10.42.0.1"             # Pi's static GigE address (eth0)
ZMQ_PORT = 5556
ZMQ_ENDPOINT = f"tcp://{ZMQ_PI_IP}:{ZMQ_PORT}"

# Pi caps at 32 detections per message; trim before sending to respect that.
MAX_DETECTIONS_PER_MSG = 32
