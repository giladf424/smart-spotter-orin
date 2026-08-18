"""
H.265 SEI frame_id extraction.

Parses Annex-B HEVC, finds prefix-SEI NALs (nal_unit_type=39), and extracts the
frame_id from OUR user_data_unregistered SEI, identified by our UUID. This is
the elementary-stream parsing logic; it is decoder-independent, so it can be
validated on a captured .hevc file offline and then reused on whatever encoded
buffer GStreamer hands us (e.g. via a pad probe ahead of the decoder).

Facts this depends on, checkable against models/pi_sei_sample.hevc:
  - Our SEI is NAL type 39, payloadType 5, payload = 16-byte UUID followed by
    a 4-byte big-endian frame_id. The UUID is SNIPE_UUID below.
  - x265 emits its own type-39 / payloadType-5 SEI carrying a version string,
    under a different UUID. Matching on type and payloadType alone would read
    that ASCII as a frame_id, so the UUID check is what makes this correct.
  - Emulation-prevention bytes (00 00 03) appear in the raw NAL and must be
    removed before reading the payload.
"""

import struct

# Our frame_id SEI UUID (user_data_unregistered payload prefix).
SNIPE_UUID = bytes([
    0x53, 0x6e, 0x69, 0x70, 0x65, 0x49, 0x74, 0x46,
    0x72, 0x6d, 0x49, 0x44, 0x00, 0x00, 0x00, 0x01,
])

# SEI payloadType for user_data_unregistered.
_PT_USER_DATA_UNREGISTERED = 5
# HEVC prefix-SEI NAL unit type.
_NAL_PREFIX_SEI = 39


def iter_nal_units(data):
    """Yield (nal_type, nal_payload_bytes) for each Annex-B NAL in `data`.

    nal_payload_bytes is the NAL after its 2-byte header, still EPB-coded.
    Handles both 3-byte (00 00 01) and 4-byte (00 00 00 01) start codes.
    """
    n = len(data)
    i = 0
    # Collect every start-code offset first, so each NAL's end is known.
    starts = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, 3))
                i += 3
                continue
            if data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1:
                starts.append((i, 4))
                i += 4
                continue
        i += 1

    for idx, (pos, sc_len) in enumerate(starts):
        nal_start = pos + sc_len
        nal_end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        nal = data[nal_start:nal_end]
        if len(nal) < 2:
            continue
        # HEVC NAL header: forbidden_zero(1) | nal_unit_type(6) |
        # layer_id(6) | tid(3)
        nal_type = (nal[0] >> 1) & 0x3F
        yield nal_type, nal[2:]  # payload after 2-byte header


def remove_epb(rbsp_in):
    """Remove HEVC emulation-prevention bytes: 00 00 03 -> 00 00 (drop the 03
    only when it follows two zero bytes and precedes 00/01/02/03)."""
    out = bytearray()
    zeros = 0
    i = 0
    n = len(rbsp_in)
    while i < n:
        b = rbsp_in[i]
        if zeros >= 2 and b == 0x03 and i + 1 < n and rbsp_in[i + 1] <= 0x03:
            zeros = 0
            i += 1
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
        i += 1
    return bytes(out)


def parse_sei_frame_id(sei_payload_epb):
    """Given a prefix-SEI NAL payload (EPB-coded, header already stripped),
    return frame_id (int) if it carries OUR UUID, else None.

    Parses possibly-multiple SEI messages in the NAL; returns the first one
    matching our UUID.
    """
    rbsp = remove_epb(sei_payload_epb)
    i = 0
    n = len(rbsp)
    while i < n:
        # payloadType (ff-extended)
        payload_type = 0
        while i < n and rbsp[i] == 0xFF:
            payload_type += 255
            i += 1
        if i >= n:
            break
        payload_type += rbsp[i]
        i += 1
        # payloadSize (ff-extended)
        payload_size = 0
        while i < n and rbsp[i] == 0xFF:
            payload_size += 255
            i += 1
        if i >= n:
            break
        payload_size += rbsp[i]
        i += 1

        if i + payload_size > n:
            break
        payload = rbsp[i:i + payload_size]
        i += payload_size

        if (payload_type == _PT_USER_DATA_UNREGISTERED
                and len(payload) >= 20
                and payload[:16] == SNIPE_UUID):
            return struct.unpack(">I", payload[16:20])[0]
        # Anything else (e.g. x265's SEI) is not ours — keep scanning.

        # After a message, an rbsp trailing byte (0x80) may follow; if the next
        # byte is the stop bit and we're at the tail, stop.
        if i < n and rbsp[i] == 0x80 and i == n - 1:
            break
    return None


def extract_frame_id_from_nal(nal_type, nal_payload_epb):
    """Convenience: return frame_id if this NAL is a prefix-SEI carrying our
    UUID, else None."""
    if nal_type != _NAL_PREFIX_SEI:
        return None
    return parse_sei_frame_id(nal_payload_epb)
