"""ISO-BMFF (fragmented MP4) and MPEG-TS container parsing used by the
live tail endpoint."""

_TS_PACKET_SIZE = 188

def _fmp4_init_end(f):
    # Parse ISO-BMFF top-level boxes from the start and return the offset just
    # past the 'moov' box (the init segment = ftyp + ... + moov). None if not
    # found within a sane number of boxes.
    f.seek(0)
    offset = 0
    for _ in range(64):
        header = f.read(8)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[0:4], 'big')
        box_type = header[4:8]
        if size == 1:  # 64-bit largesize
            ext = f.read(8)
            if len(ext) < 8:
                return None
            size = int.from_bytes(ext, 'big')
        if size < 8:
            return None
        if box_type == b'moov':
            return offset + size
        offset += size
        f.seek(offset)
    return None


def _fmp4_last_moof(window, base):
    # Find the absolute offset of the last 'moof' box in `window` that is
    # immediately followed by an 'mdat' box (a self-contained CMAF fragment).
    i = 0
    last = None
    while True:
        j = window.find(b'moof', i)
        if j < 0:
            break
        if j >= 4:
            size = int.from_bytes(window[j - 4:j], 'big')
            nxt = (j - 4) + size
            if 8 <= size and nxt + 8 <= len(window) and window[nxt + 4:nxt + 8] == b'mdat':
                last = base + (j - 4)
        i = j + 4
    return last
