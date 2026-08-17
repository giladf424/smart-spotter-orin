"""Video ingest: receive the Pi's H.265 stream and recover per-frame ids.

sei.py      parses frame_ids out of the encoded bitstream
pipeline.py drives the GStreamer/NVDEC pipeline and pairs ids with frames
"""
