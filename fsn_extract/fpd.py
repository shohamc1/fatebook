"""FPD pack reader for Fate/stay night Remaster (.bin packs).

Format (per FatePackageManager + FSNr_tools):
  Header (0x38 bytes, big-endian):
    0x00: magic 'FPD\\x00'
    0x04: version (int32)
    0x08: fileCount (int64)
    0x10: dataStartPos (int64)  -- header size, entries+names live before it
    0x18: 32 bytes reserved (zeros)
  Then (XOR-scrambled with 64KB repeating keystream):
    fileCount entries x 0x20 bytes: nameOffset, dataOffset, dataLength, fullLength (all BE int64)
    zlib-compressed name buffer
  Then file data at dataStartPos+offset, each file XOR-scrambled individually
  from keystream index 0; if fullLength != 0 the content is zlib-compressed.
"""
import io
import zlib

HDR_SIZE = 0x38
ENTRY_SIZE = 0x20


class FPD:
    def __init__(self, path, keystream):
        self.path = path
        self.key = keystream
        with open(path, 'rb') as f:
            raw = f.read()
        self.raw = raw
        assert raw[:4] == b'FPD\x00', 'bad magic'
        self.version = int.from_bytes(raw[4:8], 'big')
        self.entry_count = int.from_bytes(raw[8:16], 'big')
        self.data_start = int.from_bytes(raw[16:24], 'big')
        block_size = self.data_start - HDR_SIZE

        buf = self._xor(raw[HDR_SIZE:HDR_SIZE + block_size], 0)

        # entries
        entries = []
        pos = 0
        for _ in range(self.entry_count):
            name_off, data_off, data_len, full_len = (
                int.from_bytes(buf[pos + i * 8: pos + (i + 1) * 8], 'big')
                for i in range(4)
            )
            entries.append([name_off, data_off, data_len, full_len])
            pos += ENTRY_SIZE

        names = zlib.decompress(buf[pos:])
        self.entries = []
        for name_off, data_off, data_len, full_len in entries:
            end = names.index(b'\x00', name_off)
            name = names[name_off:end].decode('utf-8')
            self.entries.append((name, data_off, data_len, full_len))

    @staticmethod
    def _xor(data, offset):
        key_len = 65536
        out = bytearray(data)
        for i in range(len(out)):
            out[i] ^= KEY_CACHE[i % key_len]
        return bytes(out)

    def read_entry(self, index):
        name, off, length, full_len = self.entries[index]
        data = self._xor(self.raw[self.data_start + off: self.data_start + off + length], 0)
        if full_len != 0:
            data = zlib.decompress(data)
        return name, data

    def find(self, substring):
        return [(i, e[0]) for i, e in enumerate(self.entries) if substring in e[0]]


KEY_CACHE = b''


def load_key(path):
    global KEY_CACHE
    with open(path, 'rb') as f:
        KEY_CACHE = f.read()
    assert len(KEY_CACHE) == 65536
    return KEY_CACHE


if __name__ == '__main__':
    import sys
    from config import KEY_BIN
    load_key(sys.argv[2] if len(sys.argv) > 2 else KEY_BIN)
    fpd = FPD(sys.argv[1], None)
    print(f'version={fpd.version} entries={fpd.entry_count} data_start={hex(fpd.data_start)}')
    for i, (name, off, ln, fl) in enumerate(fpd.entries[:20]):
        print(i, name, hex(off), hex(ln), hex(fl))
