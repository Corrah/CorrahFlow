import argparse
import struct
import sys
from typing import Optional, Union, List, Tuple
from Crypto.Cipher import AES
from collections import namedtuple
import array

# Struttura dati per le info di cifratura (CENC)
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
        """Serializza l'atomo in bytes."""
        return struct.pack(">I", self.size) + self.atom_type + self.data


class MP4Parser:
    """Parser sequenziale per stream di dati MP4."""
    
    def __init__(self,  memoryview):
        self.data = data
        self.position = 0

    def read_atom(self) -> Optional[MP4Atom]:
        pos = self.position
        if pos + 8 > len(self.data):
            return None

        size, atom_type = struct.unpack_from(">I4s", self.data, pos)
        pos += 8

        if size == 1: # Large size (64-bit)
            if pos + 8 > len(self.data):
                return None
            size = struct.unpack_from(">Q", self.data, pos)[0]
            pos += 8

        if size < 8 or pos + size - 8 > len(self.data):
            return None

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

    def _read_atom_at(self, pos: int, end: int) -> Optional[MP4Atom]:
        if pos + 8 > end: return None
        size, atom_type = struct.unpack_from(">I4s", self.data, pos)
        pos += 8
        if size == 1:
            if pos + 8 > end: return None
            size = struct.unpack_from(">Q", self.data, pos)[0]
            pos += 8
        if size < 8 or pos + size - 8 > end: return None
        return MP4Atom(atom_type, size, self.data[pos : pos + size - 8])


class MP4Decrypter:
    """
    Gestisce la decrittazione di segmenti MP4 CENC (Common Encryption).
    Rimuove box di encryption e corregge offset dati.
    """

    def __init__(self, key_map: dict[bytes, bytes]):
        self.key_map = key_map
        self.current_key = None
        self.trun_sample_sizes = array.array("I")
        self.current_sample_info = []
        self.encryption_overhead = 0

    def decrypt_segment(self, combined_segment: bytes) -> bytes:
        """Decritta un segmento completo (init + media)."""
        data = memoryview(combined_segment)
        parser = MP4Parser(data)
        atoms = parser.list_atoms()

        # Ordine logico di processamento
        atom_process_order = [b"moov", b"moof", b"sidx", b"mdat"]
        
        processed_atoms_map = {}
        
        # Prima passata: processa i metadati (moov/moof) per estrarre info chiavi
        for atom in atoms:
            if atom.atom_type in atom_process_order:
                processed = self._process_atom(atom.atom_type, atom)
                processed_atoms_map[id(atom)] = processed

        # Ricostruisce il file mantenendo l'ordine originale degli atomi
        result = bytearray()
        for atom in atoms:
            if id(atom) in processed_atoms_map:
                result.extend(processed_atoms_map[id(atom)].pack())
            else:
                # Atomi non toccati (styp, emsg, free, etc.)
                result.extend(atom.pack())

        return bytes(result)

    def _process_atom(self, atom_type: bytes, atom: MP4Atom) -> MP4Atom:
        if atom_type == b"moov":
            return self._process_moov(atom)
        elif atom_type == b"moof":
            return self._process_moof(atom)
        elif atom_type == b"sidx":
            return self._process_sidx(atom)
        elif atom_type == b"mdat":
            return self._decrypt_mdat(atom)
        else:
            return atom

    def _process_moov(self, moov: MP4Atom) -> MP4Atom:
        """Rimuove pssh/uuid da moov e processa i trak."""
        parser = MP4Parser(moov.data)
        new_data = bytearray()

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"trak":
                new_trak = self._process_trak(atom)
                new_data.extend(new_trak.pack())
            elif atom.atom_type not in {b"pssh", b"uuid"}:
                # Rimuoviamo pssh e uuid (spesso usati per DRM)
                new_data.extend(atom.pack())

        return MP4Atom(b"moov", len(new_data) + 8, new_data)

    def _process_moof(self, moof: MP4Atom) -> MP4Atom:
        """Processa il fragment header."""
        parser = MP4Parser(moof.data)
        new_data = bytearray()

        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"traf":
                new_traf = self._process_traf(atom)
                new_data.extend(new_traf.pack())
            else:
                new_data.extend(atom.pack())

        return MP4Atom(b"moof", len(new_data) + 8, new_data)

    def _process_traf(self, traf: MP4Atom) -> MP4Atom:
        """
        Processa Track Fragment.
        - Estrae info da senc
        - Rimuove senc, saiz, saio
        - Corregge trun offset
        """
        parser = MP4Parser(traf.data)
        atoms = parser.list_atoms()
        
        new_data = bytearray()
        tfhd = None
        sample_count = 0
        
        # Box da rimuovere
        enc_boxes = {b"senc", b"saiz", b"saio", b"uuid"}
        
        # Calcola quanto spazio rimuoviamo per correggere gli offset
        removed_size = sum(a.size for a in atoms if a.atom_type in enc_boxes)
        self.encryption_overhead = removed_size

        # Passaggio 1: Analisi
        for atom in atoms:
            if atom.atom_type == b"tfhd":
                tfhd = atom
            elif atom.atom_type == b"trun":
                sample_count = self._process_trun(atom)
            elif atom.atom_type == b"senc":
                # Parsa le info di cifratura ma NON includere il box
                self.current_sample_info = self._parse_senc(atom, sample_count)

        # Setup chiave
        if tfhd:
            try:
                tid = struct.unpack_from(">I", tfhd.data, 4)[0]
                self.current_key = self._get_key_for_track(tid)
            except: pass

        # Passaggio 2: Ricostruzione
        for atom in atoms:
            if atom.atom_type in enc_boxes:
                continue # Skip
            
            if atom.atom_type == b"trun":
                # Applica correzione offset
                new_trun = self._modify_trun(atom, removed_size)
                new_data.extend(new_trun.pack())
            else:
                new_data.extend(atom.pack())

        return MP4Atom(b"traf", len(new_data) + 8, new_data)

    def _process_trun(self, trun: MP4Atom) -> int:
        """Legge le dimensioni dei campioni dal TRUN."""
        flags = struct.unpack_from(">I", trun.data, 0)[0] & 0xFFFFFF
        sample_count = struct.unpack_from(">I", trun.data, 4)[0]
        
        offset = 8 # Flags(4) + Count(4)
        if flags & 0x01: offset += 4 # Data offset present
        if flags & 0x04: offset += 4 # First sample flags present

        self.trun_sample_sizes = array.array("I")

        for _ in range(sample_count):
            if flags & 0x100: offset += 4 # Duration
            
            if flags & 0x200: # Size present
                sz = struct.unpack_from(">I", trun.data, offset)[0]
                self.trun_sample_sizes.append(sz)
                offset += 4
            else:
                self.trun_sample_sizes.append(0)
                
            if flags & 0x400: offset += 4 # Flags
            if flags & 0x800: offset += 4 # CTO

        return sample_count

    def _modify_trun(self, trun: MP4Atom, removed_overhead: int) -> MP4Atom:
        """Aggiorna il data_offset nel TRUN sottraendo la dimensione dei box rimossi."""
        data = bytearray(trun.data)
        flags = struct.unpack_from(">I", data, 0)[0] & 0xFFFFFF

        # Se data-offset-present (0x01) è settato
        if flags & 0x01:
            # Data offset è a byte 8 (dopo flags e sample_count)
            curr_offset = struct.unpack_from(">i", data, 8)[0]
            new_offset = curr_offset - removed_overhead
            struct.pack_into(">i", data, 8, new_offset)

        return MP4Atom(b"trun", len(data) + 8, data)

    def _process_sidx(self, sidx: MP4Atom) -> MP4Atom:
        """Aggiorna la dimensione referenziata nel SIDX (se presente)."""
        data = bytearray(sidx.data)
        # Version 0 (32-bit) vs Version 1 (64-bit) - assumiamo V0 per semplicità comune
        # Offset referenziato inizia a byte 32 in SIDX v0
        if len(data) > 36:
            curr = struct.unpack_from(">I", data, 32)[0]
            ref_type = curr >> 31
            ref_size = curr & 0x7FFFFFFF
            
            # Riduciamo la dimensione del segmento referenziato
            # Nota: Questo è euristico, encryption_overhead è calcolato sul moof
            # ma sidx punta al (moof+mdat). Se riduciamo moof, la size totale cala.
            new_size = ref_size - self.encryption_overhead
            packed = (ref_type << 31) | new_size
            struct.pack_into(">I", data, 32, packed)
            
        return MP4Atom(b"sidx", len(data) + 8, data)

    def _decrypt_mdat(self, mdat: MP4Atom) -> MP4Atom:
        """Decritta il payload media."""
        if not self.current_key or not self.current_sample_info:
            return mdat

        decrypted = bytearray()
        src_data = mdat.data
        pos = 0

        for i, info in enumerate(self.current_sample_info):
            # Determina dimensione campione
            if i < len(self.trun_sample_sizes) and self.trun_sample_sizes[i] > 0:
                size = self.trun_sample_sizes[i]
            else:
                size = len(src_data) - pos # Fallback: resto del file

            if pos + size > len(src_data): break

            chunk = src_data[pos : pos + size]
            dec_chunk = self._process_sample(chunk, info, self.current_key)
            decrypted.extend(dec_chunk)
            pos += size
        
        # Se avanzano dati (padding o altro), copiali
        if pos < len(src_data):
            decrypted.extend(src_data[pos:])

        return MP4Atom(b"mdat", len(decrypted) + 8, decrypted)

    def _parse_senc(self, senc: MP4Atom, sample_count: int) -> list[CENCSampleAuxiliaryDataFormat]:
        data = memoryview(senc.data)
        vf = struct.unpack_from(">I", data, 0)[0]
        flags = vf & 0xFFFFFF
        pos = 4

        if sample_count == 0:
            sample_count = struct.unpack_from(">I", data, pos)[0]
            pos += 4

        info_list = []
        for _ in range(sample_count):
            if pos + 8 > len(data): break
            
            iv = data[pos : pos + 8].tobytes()
            pos += 8
            
            subsamples = []
            if flags & 0x02: # Subsample info present
                if pos + 2 > len(data): break
                sc = struct.unpack_from(">H", data, pos)[0]
                pos += 2
                for _ in range(sc):
                    if pos + 6 > len(data): break
                    clear, enc = struct.unpack_from(">HI", data, pos)
                    subsamples.append((clear, enc))
                    pos += 6
            
            info_list.append(CENCSampleAuxiliaryDataFormat(True, iv, subsamples))
            
        return info_list

    @staticmethod
    def _process_sample(sample: memoryview, info: CENCSampleAuxiliaryDataFormat, key: bytes) -> bytes:
        if not info.is_encrypted: return sample

        iv = info.iv + b"\x00" * (16 - len(info.iv))
        cipher = AES.new(key, AES.MODE_CTR, initial_value=iv, nonce=b"")

        if not info.sub_samples:
            return cipher.decrypt(sample)

        res = bytearray()
        off = 0
        for clear_n, enc_n in info.sub_samples:
            res.extend(sample[off : off + clear_n]) # Clear bytes
            off += clear_n
            res.extend(cipher.decrypt(sample[off : off + enc_n])) # Encrypted bytes
            off += enc_n
        
        if off < len(sample):
            res.extend(cipher.decrypt(sample[off:]))

        return res

    def _get_key_for_track(self, tid: int) -> bytes:
        # Fallback: se c'è una sola chiave, usa quella a prescindere dal TID
        if len(self.key_map) == 1:
            return next(iter(self.key_map.values()))
        
        # Cerca per TID (4 byte big endian)
        tid_bytes = tid.pack(4, "big") if isinstance(tid, int) else tid
        if tid_bytes in self.key_map:
            return self.key_map[tid_bytes]
            
        raise ValueError(f"Key not found for track {tid}")

    # --- Metodi per processare il MOOV (Track Header) ---
    def _process_trak(self, trak: MP4Atom) -> MP4Atom:
        parser = MP4Parser(trak.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"mdia":
                new_data.extend(self._process_mdia(atom).pack())
            else:
                new_data.extend(atom.pack())
        return MP4Atom(b"trak", len(new_data) + 8, new_data)

    def _process_mdia(self, mdia: MP4Atom) -> MP4Atom:
        parser = MP4Parser(mdia.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"minf":
                new_data.extend(self._process_minf(atom).pack())
            else:
                new_data.extend(atom.pack())
        return MP4Atom(b"mdia", len(new_data) + 8, new_data)

    def _process_minf(self, minf: MP4Atom) -> MP4Atom:
        parser = MP4Parser(minf.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"stbl":
                new_data.extend(self._process_stbl(atom).pack())
            else:
                new_data.extend(atom.pack())
        return MP4Atom(b"minf", len(new_data) + 8, new_data)

    def _process_stbl(self, stbl: MP4Atom) -> MP4Atom:
        parser = MP4Parser(stbl.data)
        new_data = bytearray()
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"stsd":
                new_data.extend(self._process_stsd(atom).pack())
            else:
                new_data.extend(atom.pack())
        return MP4Atom(b"stbl", len(new_data) + 8, new_data)

    def _process_stsd(self, stsd: MP4Atom) -> MP4Atom:
        # STSD contiene le descrizioni dei codec (avc1, mp4a, encv...)
        # Dobbiamo convertire encv -> avc1 e enca -> mp4a
        data = stsd.data
        entry_count = struct.unpack_from(">I", data, 4)[0]
        new_data = bytearray(data[:8]) # Ver/Flags + Count
        
        parser = MP4Parser(data[8:])
        for _ in range(entry_count):
            entry = parser.read_atom()
            if not entry: break
            new_entry = self._process_sample_entry(entry)
            new_data.extend(new_entry.pack())
            
        return MP4Atom(b"stsd", len(new_data) + 8, new_data)

    def _process_sample_entry(self, entry: MP4Atom) -> MP4Atom:
        # Trasforma Entry protetta (encv) in clear (avc1)
        orig_type = entry.atom_type
        
        # Determina header size
        if orig_type in {b"mp4a", b"enca"}:
            head_sz = 28
        elif orig_type in {b"mp4v", b"encv", b"avc1", b"hvc1", b"hev1"}:
            head_sz = 78
        else:
            head_sz = 16 # Generic

        new_data = bytearray(entry.data[:head_sz])
        parser = MP4Parser(entry.data[head_sz:])
        
        real_format = None

        for atom in iter(parser.read_atom, None):
            # Rimuovi atomi di protezione
            if atom.atom_type in {b"sinf", b"schi", b"tenc", b"schm"}:
                # Estrai il formato reale da sinf->frma se presente
                if atom.atom_type == b"sinf":
                    real_format = self._extract_real_format(atom)
                continue 
            new_data.extend(atom.pack())

        # Se era encv/enca, cambia il tipo con quello reale (avc1/mp4a)
        final_type = real_format if real_format else orig_type
        if final_type == b"encv": final_type = b"avc1"
        if final_type == b"enca": final_type = b"mp4a"

        return MP4Atom(final_type, len(new_data) + 8, new_data)

    def _extract_real_format(self, sinf: MP4Atom) -> Optional[bytes]:
        parser = MP4Parser(sinf.data)
        for atom in iter(parser.read_atom, None):
            if atom.atom_type == b"frma":
                return atom.data # es. avc1
        return None


def decrypt_segment(init_segment: bytes, segment_content: bytes, key_id: str, key: str) -> bytes:
    """Funzione helper per l'uso esterno."""
    try:
        kid_bytes = bytes.fromhex(key_id)
        k_bytes = bytes.fromhex(key)
    except ValueError:
        # Fallback per chiavi non-hex (raw)
        kid_bytes = key_id.encode() if isinstance(key_id, str) else key_id
        k_bytes = key.encode() if isinstance(key, str) else key

    decrypter = MP4Decrypter({kid_bytes: k_bytes})
    return decrypter.decrypt_segment(init_segment + segment_content)


# CLI per test rapidi
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--key_id", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.init, "rb") as f: init = f.read()
    with open(args.segment, "rb") as f: seg = f.read()
    
    out = decrypt_segment(init, seg, args.key_id, args.key)
    
    with open(args.output, "wb") as f: f.write(out)
    print(f"Decrypted: {args.output}")
