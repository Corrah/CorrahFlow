import argparse
import struct
import sys
from typing import Optional, Union, List, Tuple
from Crypto.Cipher import AES
from collections import namedtuple
import array

CENCSampleAuxiliaryDataFormat = namedtuple("CENCSampleAuxiliaryDataFormat", ["is_encrypted", "iv", "sub_samples"])

class MP4Atom:
    """Rappresenta un 'box' (atom) MP4."""
    __slots__ = ("atom_type", "size", "data")

    def __init__(self, atom_type: bytes, size: int,  Union[memoryview, bytearray]):
        self.atom_type = atom_type
        self.size = size
        self.data = data

    def __repr__(self):
        return f"<MP4Atom type={self.atom_type}, size={self.size}>"

    def pack(self):
        return struct.pack(">I", self.size) + self.atom_type + self.data


class MP4Parser:
    def __init__(self,  memoryview):
        self.data = data
        self.position = 0

    def read_atom(self) -> Optional[MP4Atom]:
        pos = self.position
        if pos + 8 > len(self.data): return None

        size, atom_type = struct.unpack_from(">I4s", self.data, pos)
        pos += 8

        if size == 1:
            if pos + 8 > len(self.data): return None
            size = struct.unpack_from(">Q", self.data, pos)[0]
            pos += 8

        if size < 8 or pos + size - 8 > len(self.data): return None

        atom_data = self.data[pos : pos + size - 8]
        self.position = pos + size - 8
        return MP4Atom(atom_type, size, atom_data)

    def list_atoms(self) -> list[MP4Atom]:
        atoms = []
        original_position = self.position
        self.position = 0
        while self.position + 8 <= len(self.data):
            atom = self.read_atom()
            if not atom: break
            atoms.append(atom)
        self.position = original_position
        return atoms


class MP4Decrypter:
    def __init__(self, key_map: dict[bytes, bytes]):
        self.key_map = key_map
        self.current_key = None
        self.trun_sample_sizes = array.array("I")
        self.current_sample_info = []
        self.encryption_overhead = 0

    def decrypt_segment(self, combined_segment: bytes) -> bytes:
        data = memoryview(combined_segment)
        parser = MP4Parser(data)
        atoms = parser.list_atoms()
        atom_process_order = [b"moov", b"moof", b"sidx", b"mdat"]
        
        processed_atoms_map = {}
        for atom in atoms:
            if atom.atom_type in atom_process_order:
                processed = self._process_atom(atom.atom_type, atom)
                processed_atoms_map[id(atom)] = processed

        result = bytearray()
        for atom in atoms:
            if id(atom) in processed_atoms_map:
                result.extend(processed_atoms_map[id(atom)].pack())
            else:
                result.extend(atom.pack())
        return bytes(result)

    def _process_atom(self, atom_type: bytes, atom: MP4Atom) -> MP4Atom:
        if atom_type == b"moov": return self._process_moov(atom)
        elif atom_type == b"moof": return self._process_moof(atom)
        elif atom_type == b"sidx": return self._process_sidx(atom)
        elif atom_type == b"mdat": return self._decrypt_mdat(atom)
        return atom

    def _process_moov(self, moov: MP4Atom) -> MP4Atom:
        parser = MP4Parser(moov.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"trak":
                new_data.extend(self._process_trak(atom).pack())
            elif atom.atom_type not in {b"pssh", b"uuid"}:
                new_data.extend(atom.pack())
        return MP4Atom(b"moov", len(new_data) + 8, new_data)

    def _process_moof(self, moof: MP4Atom) -> MP4Atom:
        parser = MP4Parser(moof.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"traf":
                new_data.extend(self._process_traf(atom).pack())
            else:
                new_data.extend(atom.pack())
        return MP4Atom(b"moof", len(new_data) + 8, new_data)

    def _process_traf(self, traf: MP4Atom) -> MP4Atom:
        parser = MP4Parser(traf.data)
        atoms = parser.list_atoms()
        new_data = bytearray()
        tfhd = None
        sample_count = 0
        enc_boxes = {b"senc", b"saiz", b"saio", b"uuid"}
        
        removed_size = sum(a.size for a in atoms if a.atom_type in enc_boxes)
        self.encryption_overhead = removed_size

        for atom in atoms:
            if atom.atom_type == b"tfhd": tfhd = atom
            elif atom.atom_type == b"trun": sample_count = self._process_trun(atom)
            elif atom.atom_type == b"senc": self.current_sample_info = self._parse_senc(atom, sample_count)

        if tfhd:
            try:
                tid = struct.unpack_from(">I", tfhd.data, 4)[0]
                self.current_key = self._get_key_for_track(tid)
            except: pass

        for atom in atoms:
            if atom.atom_type in enc_boxes: continue
            if atom.atom_type == b"trun":
                new_data.extend(self._modify_trun(atom, removed_size).pack())
            else:
                new_data.extend(atom.pack())

        return MP4Atom(b"traf", len(new_data) + 8, new_data)

    def _process_trun(self, trun: MP4Atom) -> int:
        flags = struct.unpack_from(">I", trun.data, 0)[0] & 0xFFFFFF
        sample_count = struct.unpack_from(">I", trun.data, 4)[0]
        offset = 8
        if flags & 0x01: offset += 4
        if flags & 0x04: offset += 4
        self.trun_sample_sizes = array.array("I")
        for _ in range(sample_count):
            if flags & 0x100: offset += 4
            if flags & 0x200:
                sz = struct.unpack_from(">I", trun.data, offset)[0]
                self.trun_sample_sizes.append(sz)
                offset += 4
            else:
                self.trun_sample_sizes.append(0)
            if flags & 0x400: offset += 4
            if flags & 0x800: offset += 4
        return sample_count

    def _modify_trun(self, trun: MP4Atom, removed: int) -> MP4Atom:
        data = bytearray(trun.data)
        flags = struct.unpack_from(">I", data, 0)[0] & 0xFFFFFF
        if flags & 0x01:
            curr = struct.unpack_from(">i", data, 8)[0]
            struct.pack_into(">i", data, 8, curr - removed)
        return MP4Atom(b"trun", len(data) + 8, data)

    def _process_sidx(self, sidx: MP4Atom) -> MP4Atom:
        data = bytearray(sidx.data)
        if len(data) > 36:
            curr = struct.unpack_from(">I", data, 32)[0]
            ref_type = curr >> 31
            ref_size = curr & 0x7FFFFFFF
            packed = (ref_type << 31) | (ref_size - self.encryption_overhead)
            struct.pack_into(">I", data, 32, packed)
        return MP4Atom(b"sidx", len(data) + 8, data)

    def _decrypt_mdat(self, mdat: MP4Atom) -> MP4Atom:
        if not self.current_key or not self.current_sample_info: return mdat
        decrypted = bytearray()
        src = mdat.data
        pos = 0
        for i, info in enumerate(self.current_sample_info):
            size = self.trun_sample_sizes[i] if i < len(self.trun_sample_sizes) and self.trun_sample_sizes[i] > 0 else len(src) - pos
            if pos + size > len(src): break
            decrypted.extend(self._process_sample(src[pos:pos+size], info, self.current_key))
            pos += size
        if pos < len(src): decrypted.extend(src[pos:])
        return MP4Atom(b"mdat", len(decrypted) + 8, decrypted)

    def _parse_senc(self, senc: MP4Atom, count: int) -> list:
        data = memoryview(senc.data)
        flags = struct.unpack_from(">I", data, 0)[0] & 0xFFFFFF
        pos = 4
        if count == 0:
            count = struct.unpack_from(">I", data, pos)[0]
            pos += 4
        info = []
        for _ in range(count):
            if pos + 8 > len(data): break
            iv = data[pos:pos+8].tobytes()
            pos += 8
            subs = []
            if flags & 0x02:
                if pos + 2 > len(data): break
                sc = struct.unpack_from(">H", data, pos)[0]
                pos += 2
                for _ in range(sc):
                    if pos + 6 > len(data): break
                    subs.append(struct.unpack_from(">HI", data, pos))
                    pos += 6
            info.append(CENCSampleAuxiliaryDataFormat(True, iv, subs))
        return info

    @staticmethod
    def _process_sample(sample: memoryview, info: CENCSampleAuxiliaryDataFormat, key: bytes) -> bytes:
        if not info.is_encrypted: return sample
        iv = info.iv + b"\x00" * (16 - len(info.iv))
        cipher = AES.new(key, AES.MODE_CTR, initial_value=iv, nonce=b"")
        if not info.sub_samples: return cipher.decrypt(sample)
        res = bytearray()
        off = 0
        for clear_n, enc_n in info.sub_samples:
            res.extend(sample[off:off+clear_n])
            off += clear_n
            res.extend(cipher.decrypt(sample[off:off+enc_n]))
            off += enc_n
        if off < len(sample): res.extend(cipher.decrypt(sample[off:]))
        return res

    def _get_key_for_track(self, tid: int) -> bytes:
        if len(self.key_map) == 1: return next(iter(self.key_map.values()))
        tid_b = tid.pack(4, "big")
        return self.key_map.get(tid_b, list(self.key_map.values())[0])

    def _process_trak(self, trak: MP4Atom) -> MP4Atom:
        parser = MP4Parser(trak.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"mdia": new_data.extend(self._process_mdia(atom).pack())
            else: new_data.extend(atom.pack())
        return MP4Atom(b"trak", len(new_data) + 8, new_data)

    def _process_mdia(self, mdia: MP4Atom) -> MP4Atom:
        parser = MP4Parser(mdia.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"minf": new_data.extend(self._process_minf(atom).pack())
            else: new_data.extend(atom.pack())
        return MP4Atom(b"mdia", len(new_data) + 8, new_data)

    def _process_minf(self, minf: MP4Atom) -> MP4Atom:
        parser = MP4Parser(minf.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"stbl": new_data.extend(self._process_stbl(atom).pack())
            else: new_data.extend(atom.pack())
        return MP4Atom(b"minf", len(new_data) + 8, new_data)

    def _process_stbl(self, stbl: MP4Atom) -> MP4Atom:
        parser = MP4Parser(stbl.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"stsd": new_data.extend(self._process_stsd(atom).pack())
            else: new_data.extend(atom.pack())
        return MP4Atom(b"stbl", len(new_data) + 8, new_data)

    def _process_stsd(self, stsd: MP4Atom) -> MP4Atom:
        data = stsd.data
        count = struct.unpack_from(">I", data, 4)[0]
        new_data = bytearray(data[:8])
        parser = MP4Parser(data[8:])
        for _ in range(count):
            entry = parser.read_atom()
            if not entry: break
            new_data.extend(self._process_sample_entry(entry).pack())
        return MP4Atom(b"stsd", len(new_data) + 8, new_data)

    def _process_sample_entry(self, entry: MP4Atom) -> MP4Atom:
        t = entry.atom_type
        hsz = 78 if t in {b"avc1", b"encv", b"hvc1", b"hev1"} else 28 if t in {b"mp4a", b"enca"} else 16
        new_data = bytearray(entry.data[:hsz])
        parser = MP4Parser(entry.data[hsz:])
        real_fmt = None
        for atom in iter(parser.read_atom, None):
            if atom.atom_type in {b"sinf", b"schi", b"tenc", b"schm"}:
                if atom.atom_type == b"sinf": real_fmt = self._extract_real_format(atom)
                continue
            new_data.extend(atom.pack())
        final_type = real_fmt if real_fmt else t
        if final_type == b"encv": final_type = b"avc1"
        if final_type == b"enca": final_type = b"mp4a"
        return MP4Atom(final_type, len(new_data) + 8, new_data)

    def _extract_real_format(self, sinf: MP4Atom) -> Optional[bytes]:
        parser = MP4Parser(sinf.data)
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"frma": return atom.data
        return None

def decrypt_segment(init_segment: bytes, segment_content: bytes, key_id: str, key: str) -> bytes:
    try:
        kid = bytes.fromhex(key_id)
        k = bytes.fromhex(key)
    except:
        kid = key_id.encode() if isinstance(key_id, str) else key_id
        k = key.encode() if isinstance(key, str) else key
    return MP4Decrypter({kid: k}).decrypt_segment(init_segment + segment_content)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--key_id", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.init, "rb") as f: i = f.read()
    with open(args.segment, "rb") as f: s = f.read()
    o = decrypt_segment(i, s, args.key_id, args.key)
    with open(args.output, "wb") as f: f.write(o)
