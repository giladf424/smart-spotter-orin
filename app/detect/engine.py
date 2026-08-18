"""
TensorRT engine wrapper: pure engine I/O.

Loads a serialized .engine, allocates its device and host buffers once, and
runs inference on a preprocessed NCHW FP32 array. It knows nothing about
images, letterboxing or detection semantics — FP32 NCHW in, raw array out.

Uses the native TensorRT Python API (python3-libnvinfer) plus cuda-python
(cudart) for device memory and stream management. Both come from the image.
"""

import numpy as np
import tensorrt as trt

try:
    from cuda.bindings import runtime as cudart  # cuda-python >= 12.6 / 13.x
except ImportError:
    from cuda import cudart  # legacy layout


def _check(err, msg=""):
    """Raise on a non-success cudart error code."""
    if isinstance(err, tuple):
        # cudart calls return (err, *results); split the status off.
        status, *rest = err
        if status != cudart.cudaError_t.cudaSuccess:
            raise RuntimeError(f"CUDA error {status} {msg}")
        return rest[0] if len(rest) == 1 else rest
    if err != cudart.cudaError_t.cudaSuccess:
        raise RuntimeError(f"CUDA error {err} {msg}")
    return None


class TRTEngine:
    """Load a TRT engine and run single-input/single-output inference.

    Assumes, as the current YOLO26 export does:
      - exactly one input tensor and one output tensor
      - static shapes, so nothing to set per call
      - input  : 'images'  [1,3,N,N] FP32 NCHW
      - output : 'output0' [1,300,6] FP32
    """

    def __init__(self, engine_path):
        self._logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(self._logger)

        with open(engine_path, "rb") as f:
            self._engine = self._runtime.deserialize_cuda_engine(f.read())
        if self._engine is None:
            raise RuntimeError(f"failed to deserialize engine: {engine_path}")

        self._context = self._engine.create_execution_context()

        # Keeps the last input and the last output it finds, so an engine with
        # several of either binds the wrong tensor rather than failing here.
        self._input_name = None
        self._output_name = None
        for i in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(i)
            mode = self._engine.get_tensor_mode(name)
            if mode == trt.TensorIOMode.INPUT:
                self._input_name = name
            else:
                self._output_name = name
        if self._input_name is None or self._output_name is None:
            raise RuntimeError("engine needs at least one input and output")

        # Static shapes — read them once.
        self._input_shape = tuple(
            self._context.get_tensor_shape(self._input_name))
        self._output_shape = tuple(
            self._context.get_tensor_shape(self._output_name))

        self._input_dtype = trt.nptype(
            self._engine.get_tensor_dtype(self._input_name))
        self._output_dtype = trt.nptype(
            self._engine.get_tensor_dtype(self._output_name))

        self._input_nbytes = (int(np.prod(self._input_shape))
                              * np.dtype(self._input_dtype).itemsize)
        self._output_nbytes = (int(np.prod(self._output_shape))
                               * np.dtype(self._output_dtype).itemsize)

        # Device buffers and the host output buffer, allocated once and reused.
        self._d_input = _check(cudart.cudaMalloc(self._input_nbytes),
                               "cudaMalloc input")
        self._d_output = _check(cudart.cudaMalloc(self._output_nbytes),
                                "cudaMalloc output")
        self._h_output = np.empty(self._output_shape, dtype=self._output_dtype)

        self._stream = _check(cudart.cudaStreamCreate(), "cudaStreamCreate")

        # Bind device addresses to tensor names (TRT 10 tensor-address API).
        self._context.set_tensor_address(self._input_name,
                                         int(self._d_input))
        self._context.set_tensor_address(self._output_name,
                                         int(self._d_output))

    @property
    def input_shape(self):
        return self._input_shape

    def infer(self, input_array):
        """Run one forward pass.

        input_array: np.ndarray shaped like input_shape (1,3,N,N), FP32, NCHW,
                     contiguous. preprocess() guarantees this.
        returns:     np.ndarray copy of the raw output ([1,300,6]). A COPY, so
                     the caller may keep it past the next infer() call.
        """
        arr = np.ascontiguousarray(input_array, dtype=self._input_dtype)
        if arr.shape != self._input_shape:
            raise ValueError(
                f"input shape {arr.shape} != engine input {self._input_shape}"
            )

        _check(
            cudart.cudaMemcpyAsync(
                self._d_input,
                arr.ctypes.data,
                self._input_nbytes,
                cudart.cudaMemcpyKind.cudaMemcpyHostToDevice,
                self._stream,
            ),
            "memcpy H2D",
        )
        if not self._context.execute_async_v3(stream_handle=int(self._stream)):
            raise RuntimeError("TRT execute_async_v3 failed")
        _check(
            cudart.cudaMemcpyAsync(
                self._h_output.ctypes.data,
                self._d_output,
                self._output_nbytes,
                cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost,
                self._stream,
            ),
            "memcpy D2H",
        )
        _check(cudart.cudaStreamSynchronize(self._stream), "stream sync")

        return self._h_output.copy()

    def close(self):
        """Free the device buffers and stream. Safe to call more than once.

        The TensorRT context, engine and runtime are left to refcounting."""
        for attr in ("_d_input", "_d_output"):
            ptr = getattr(self, attr, None)
            if ptr is not None:
                cudart.cudaFree(ptr)
                setattr(self, attr, None)
        stream = getattr(self, "_stream", None)
        if stream is not None:
            cudart.cudaStreamDestroy(stream)
            self._stream = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
