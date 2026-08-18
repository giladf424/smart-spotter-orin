#!/usr/bin/env bash
# Orin inference service entrypoint.
# Responsibility: ensure a TensorRT engine exists that matches the current
# ONNX + TRT version + input dims + precision, building it on-device if needed
# (engines are non-portable), then launch the inference service.
#
# Cache key = sha256(onnx) + TRT version + input dims + precision.
# The .engine and a sidecar .fingerprint live in the MOUNTED /models dir, so
# they persist across container restarts and are only rebuilt on a real change.
set -euo pipefail

# --- Config (overridable via -e) --------------------------------------------
MODELS_DIR="${MODELS_DIR:-/models}"
ONNX_PATH="${ONNX_PATH:-${MODELS_DIR}/best.onnx}"
ENGINE_PATH="${ENGINE_PATH:-${MODELS_DIR}/model.engine}"
FINGERPRINT_PATH="${ENGINE_PATH}.fingerprint"
PRECISION="${PRECISION:-fp16}"                 # fp16 | fp32
DETECTOR_INPUT="${DETECTOR_INPUT:-640}"        # 640 interim, 960 target
# Service entrypoint, mounted at /app.
APP_ENTRY="${APP_ENTRY:-/app/infer.py}"

log() { echo "[entrypoint] $*"; }

# --- Sanity ------------------------------------------------------------------
if [[ ! -f "${ONNX_PATH}" ]]; then
    log "ERROR: ONNX not found at ${ONNX_PATH}. Mount it into ${MODELS_DIR}."
    exit 1
fi

# --- Compute fingerprint -----------------------------------------------------
TRT_VER="$(dpkg-query -W -f='${Version}' libnvinfer10 2>/dev/null || echo unknown)"
ONNX_HASH="$(sha256sum "${ONNX_PATH}" | awk '{print $1}')"
FINGERPRINT="onnx=${ONNX_HASH};trt=${TRT_VER};in=${DETECTOR_INPUT};prec=${PRECISION}"
log "current fingerprint: ${FINGERPRINT}"

# --- Build-or-reuse ----------------------------------------------------------
NEED_BUILD=1
if [[ -f "${ENGINE_PATH}" && -f "${FINGERPRINT_PATH}" ]]; then
    if [[ "$(cat "${FINGERPRINT_PATH}")" == "${FINGERPRINT}" ]]; then
        log "cached engine matches fingerprint — reusing ${ENGINE_PATH}"
        NEED_BUILD=0
    else
        log "fingerprint changed — rebuilding engine"
        log "  cached: $(cat "${FINGERPRINT_PATH}")"
    fi
else
    log "no cached engine/fingerprint — building"
fi

if [[ "${NEED_BUILD}" -eq 1 ]]; then
    PREC_FLAG=""
    [[ "${PRECISION}" == "fp16" ]] && PREC_FLAG="--fp16"
    # Static-shape ONNX (dynamic=False at export), so no shape flags needed.
    # Build to a temp path then atomically move, so a crash mid-build never
    # leaves a half-written engine that the cache would treat as valid.
    TMP_ENGINE="${ENGINE_PATH}.tmp.$$"
    log "building engine: trtexec --onnx=${ONNX_PATH} ${PREC_FLAG} --saveEngine=${TMP_ENGINE}"
    trtexec --onnx="${ONNX_PATH}" ${PREC_FLAG} --saveEngine="${TMP_ENGINE}"
    mv -f "${TMP_ENGINE}" "${ENGINE_PATH}"
    echo "${FINGERPRINT}" > "${FINGERPRINT_PATH}"
    log "engine built and cached at ${ENGINE_PATH}"
fi

# --- Launch service ----------------------------------------------------------
if [[ ! -f "${APP_ENTRY}" ]]; then
    log "ERROR: service not found at ${APP_ENTRY}. Mount the app directory."
    exit 1
fi

log "starting inference service: ${APP_ENTRY}"
exec python3 "${APP_ENTRY}" \
    --engine "${ENGINE_PATH}" \
    --input-size "${DETECTOR_INPUT}" \
    "$@"
