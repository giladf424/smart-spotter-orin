# Orin inference service — Smart Spotter detection pipeline
# Base: generic SBSA CUDA+cuDNN runtime (confirmed pullable on JetPack 7.2 / L4T r39.2).
# TensorRT and the in-container engine builder come from the Jetson r39.2 apt repo,
# pinned to the exact host version so the on-device-built engine is driver-compatible.
# The L4T multimedia / NVDEC GStreamer plugins are NOT installed here — they are
# mounted from the host by `--runtime nvidia` (confirmed in the runtime CSVs).
FROM nvcr.io/nvidia/cuda:13.2.1-cudnn-runtime-ubuntu24.04

ARG DEBIAN_FRONTEND=noninteractive
# Pin to the exact host TRT build. If the host pin file dictates otherwise we mirror it (see below).
ARG TRT_VERSION=10.16.2.10-1+cuda13.2

# --- 0. Remove CUDA forward-compat libraries ---------------------------------
# The base image ships /usr/local/cuda*/compat with forward-compat driver libs
# (libcuda.so.595.*, etc.). On Orin / JetPack 7.2 the nvidia-container-toolkit
# cudacompat CDI hook panics parsing their ELF header, so the container never
# starts under --runtime nvidia. These libs are pointless here: the container's
# CUDA 13.2 already matches the host R595 driver, so there is nothing to be
# forward-compatible with. Removing them makes the hook a no-op. The real driver
# libs are still injected from the host at runtime by --runtime nvidia.
RUN rm -rf /usr/local/cuda/compat \
           /usr/local/cuda-13/compat \
           /usr/local/cuda-13.2/compat 2>/dev/null || true

# --- 1. Jetson r39.2 apt repo + signing key (mirrors the host setup) ---------
# Generic tools needed to add the repo and fetch the key.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates gnupg wget \
    && rm -rf /var/lib/apt/lists/*

# Host key lives at /etc/apt/trusted.gpg.d/jetson-ota-public.asc; fetch the same key.
RUN wget -qO /etc/apt/trusted.gpg.d/jetson-ota-public.asc \
        https://repo.download.nvidia.com/jetson/jetson-ota-public.asc

# Same three source lines as the host's nvidia-l4t-apt-source.list.
RUN echo "deb https://repo.download.nvidia.com/jetson/common r39.2 main"  >  /etc/apt/sources.list.d/nvidia-l4t-apt-source.list \
 && echo "deb https://repo.download.nvidia.com/jetson/som r39.2 main"     >> /etc/apt/sources.list.d/nvidia-l4t-apt-source.list \
 && echo "deb https://repo.download.nvidia.com/jetson/ffmpeg r39.2 main"  >> /etc/apt/sources.list.d/nvidia-l4t-apt-source.list

# Mirror the host apt pin (origin "*.nvidia.com" priority 600) so transitive
# deps resolve to the same origin the host used. Explicit version pins on the
# TRT packages below still take precedence regardless.
RUN printf 'Package: *\nPin: origin "*.nvidia.com"\nPin-Priority: 600\n' \
        > /etc/apt/preferences.d/nvidia-repo-pin

# --- 2. TensorRT (engine builder + runtime + Python bindings), version-pinned --
# libnvinfer-bin provides trtexec. python3-libnvinfer provides the Python API.
# These pull libnvinfer10 / libnvinfer-plugin10 as deps at the same version.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnvinfer-bin=${TRT_VERSION} \
        libnvinfer10=${TRT_VERSION} \
        libnvinfer-plugin10=${TRT_VERSION} \
        python3-libnvinfer=${TRT_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# --- 3. Generic GStreamer framework (NOT the L4T plugins) --------------------
# Provides the registry + RTP/RTSP depay + appsink. The NVIDIA NVDEC plugins
# (nvv4l2decoder, nvvidconv) are mounted from the host at runtime and loaded
# into this registry automatically. Do NOT install nvidia-l4t-gstreamer here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-libav \
        libgstreamer1.0-0 \
        libgstreamer-plugins-base1.0-0 \
        python3-gi \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
    && rm -rf /var/lib/apt/lists/*

# --- 4. Python deps for the inference service --------------------------------
# python3-numpy / python3-opencv from Ubuntu repo (cv2 only for appsink/debug,
# not the GPU path). pyzmq for the PUSH socket. --extra-index-url covers the
# sbsa/cu132 index gaps noted for JetPack 7.2.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-numpy \
        python3-opencv \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
        --extra-index-url https://pypi.org/simple \
        pyzmq \
        cuda-python

# --- 5. trtexec on PATH ------------------------------------------------------
# libnvinfer-bin installs trtexec to /usr/src/tensorrt/bin on some layouts and
# /usr/bin on others; ensure it's reachable.
ENV PATH="/usr/src/tensorrt/bin:${PATH}"

# --- 6. App layout -----------------------------------------------------------
# Code and model artifacts are MOUNTED at runtime (-v), not baked. /app holds
# the inference service source; /models holds best.onnx + the cached engine.
WORKDIR /app
RUN mkdir -p /app /models

# Entrypoint: build-or-load the TRT engine (cached), then run the service.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
