import re
import math
import logging
import os
import urllib.parse
from urllib.parse import urljoin
from datetime import datetime, timedelta

# Tenta di usare lxml per prestazioni (molto consigliato), fallback su xml standard
try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET

logger = logging.getLogger("EasyProxy.MPD")

class MPDToHLSConverter:
    """
    Convertitore MPD -> HLS ottimizzato per 'EasyProxy'.
    Fix: Buffering, Loop su segmenti, Sincronizzazione AppleCoreMedia.
    """
    
    def __init__(self):
        self.ns = {
            'mpd': 'urn:mpeg:dash:schema:mpd:2011',
            'cenc': 'urn:mpeg:cenc:2013',
            'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
        }
        # Regex pre-compilate per velocità
        self.re_duration = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(\.\d+)?)S)?')
        self.re_template_bw = re.compile(r'\$Bandwidth(?s:%.[^$]+)?\$')
        self.re_template_rep = re.compile(r'\$RepresentationID(?s:%.[^$]+)?\$')
        self.re_template_num = re.compile(r'\$Number(?:(%[^$]+))?\$')
        self.re_template_time = re.compile(r'\$Time(?:(%[^$]+))?\$')

    def _parse_iso8601_duration(self, duration_str: str) -> float:
        """Converte durata ISO8601 (es. PT10.5S) in secondi."""
        if not duration_str: return 0.0
        match = self.re_duration.match(duration_str)
        if not match: return 0.0
        h = float(match.group(1) or 0)
        m = float(match.group(2) or 0)
        s = float(match.group(3) or 0)
        return (h * 3600) + (m * 60) + s

    def _parse_iso8601_datetime(self, date_str: str) -> datetime:
        """Converte data stringa in oggetto datetime UTC."""
        if not date_str: return datetime.utcnow()
        try:
            clean_str = date_str.replace('Z', '')
            if '.' in clean_str:
                return datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S.%f")
            return datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        except:
            return datetime.utcnow()

    def _process_template(self, url_template: str, rep_id: str, number: int = None, time: int = None, bandwidth: int = None) -> str:
        """Gestisce i template DASH ($Number$, $Time$, ecc.) con formattazione."""
        url = url_template
        if bandwidth is not None:
            url = self.re_template_bw.sub(str(bandwidth), url)
        url = self.re_template_rep.sub(str(rep_id), url)
        if number is not None:
            def repl_num(m): return (m.group(1) % number) if m.group(1) else str(number)
            url = self.re_template_num.sub(repl_num, url)
        if time is not None:
            def repl_time(m): return (m.group(1) % time) if m.group(1) else str(time)
            url = self.re_template_time.sub(repl_time, url)
        return url

    def convert_master_playlist(self, manifest_content: str, proxy_base: str, original_url: str, params: str) -> str:
        """Genera la Master Playlist (Varianti)."""
        try:
            if isinstance(manifest_content, str): manifest_content = manifest_content.encode('utf-8')
            if b'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace(b'<MPD', b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
            
            root = ET.fromstring(manifest_content)
            # HLS Version 6 è la più stabile per compatibilità mista
            lines = ['#EXTM3U', '#EXT-X-VERSION:6']
            
            video_sets = []
            audio_sets = []
            
            for aset in root.findall('.//mpd:AdaptationSet', self.ns):
                mime = aset.get('mimeType', '')
                content = aset.get('contentType', '')
                if 'video' in mime or 'video' in content: video_sets.append(aset)
                elif 'audio' in mime or 'audio' in content: audio_sets.append(aset)
                else:
                    # Fallback per manifest incompleti
                    if aset.find('mpd:Representation[@mimeType="video/mp4"]', self.ns) is not None: video_sets.append(aset)
                    elif aset.find('mpd:Representation[@mimeType="audio/mp4"]', self.ns) is not None: audio_sets.append(aset)

            # Gestione Audio
            audio_group = 'audio'
            has_audio = False
            for i, aset in enumerate(audio_sets):
                lang = aset.get('lang', 'und')
                rep = aset.find('mpd:Representation', self.ns)
                if rep is None: continue
                rep_id = rep.get('id')
                
                # Encode URL per il proxy
                enc_url = urllib.parse.quote(original_url, safe='')
                uri = f"{proxy_base}/proxy/hls/manifest.m3u8?d={enc_url}&format=hls&rep_id={rep_id}{params}"
                
                name = f"Audio {lang}"
                default = "YES" if i == 0 else "NO"
                lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group}",NAME="{name}",LANGUAGE="{lang}",DEFAULT={default},AUTOSELECT=YES,URI="{uri}"')
                has_audio = True

            # Gestione Video
            for aset in video_sets:
                base_codecs = aset.get('codecs')
                for rep in aset.findall('mpd:Representation', self.ns):
                    rep_id = rep.get('id')
                    bw = rep.get('bandwidth', '0')
                    w = rep.get('width')
                    h = rep.get('height')
                    fps = rep.get('frameRate')
                    codecs = rep.get('codecs') or base_codecs
                    
                    enc_url = urllib.parse.quote(original_url, safe='')
                    uri = f"{proxy_base}/proxy/hls/manifest.m3u8?d={enc_url}&format=hls&rep_id={rep_id}{params}"
                    
                    inf = [f'BANDWIDTH={bw}']
                    if w and h: inf.append(f'RESOLUTION={w}x{h}')
                    if fps: inf.append(f'FRAME-RATE={fps}')
                    if codecs: inf.append(f'CODECS="{codecs}"')
                    if has_audio: inf.append(f'AUDIO="{audio_group}"')
                    
                    lines.append(f'#EXT-X-STREAM-INF:{",".join(inf)}')
                    lines.append(uri)
            
            return '\n'.join(lines)
        except Exception as e:
            logger.error(f"Errore Master Playlist: {e}")
            return f"#EXTM3U\n#EXT-X-ERROR: {e}"

    def convert_media_playlist(self, manifest_content: str, rep_id: str, proxy_base: str, original_url: str, params: str, clearkey_param: str = None) -> str:
        """Genera la Media Playlist con logica ANTI-BUFFERING."""
        try:
            if isinstance(manifest_content, str): manifest_content = manifest_content.encode('utf-8')
            if b'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace(b'<MPD', b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
                
            root = ET.fromstring(manifest_content)
            mpd_type = root.get('type', 'static')
            is_live = mpd_type.lower() == 'dynamic'

            # DATI TEMPORALI ASSOLUTI (Fondamentale per il fix)
            ast_str = root.get('availabilityStartTime')
            availability_start_time = self._parse_iso8601_datetime(ast_str)

            # Trova Representation
            rep = None
            aset = None
            for a in root.findall('.//mpd:AdaptationSet', self.ns):
                r = a.find(f'mpd:Representation[@id="{rep_id}"]', self.ns)
                if r is not None:
                    rep = r
                    aset = a
                    break
            
            if rep is None: return "#EXTM3U\n#EXT-X-ERROR: Rep not found"
            
            bw = int(rep.get('bandwidth', 0))
            lines = ['#EXTM3U', '#EXT-X-VERSION:6', '#EXT-X-INDEPENDENT-SEGMENTS']

            # Setup Decrittazione
            server_side_decryption = False
            dec_query = ""
            if clearkey_param:
                try:
                    kid, key = clearkey_param.split(':')
                    server_side_decryption = True
                    dec_query = f"&key={key}&key_id={kid}"
                except: pass

            # Base URL
            base_node = root.find('mpd:BaseURL', self.ns)
            base_url = urljoin(original_url, base_node.text) if (base_node is not None and base_node.text) else os.path.dirname(original_url)
            if not base_url.endswith('/'): base_url += '/'

            # Parsing Template
            tmpl = rep.find('mpd:SegmentTemplate', self.ns) or aset.find('mpd:SegmentTemplate', self.ns)
            if not tmpl: return "#EXTM3U\n#EXT-X-ERROR: No Template"

            timescale = int(tmpl.get('timescale', '1'))
            media_tmpl = tmpl.get('media')
            init_tmpl = tmpl.get('initialization')
            start_num = int(tmpl.get('startNumber', '1'))

            # 1. Init Segment (EXT-X-MAP)
            encoded_init = ""
            if init_tmpl:
                init_url = self._process_template(init_tmpl, rep_id, bandwidth=bw)
                full_init = urljoin(base_url, init_url)
                encoded_init = urllib.parse.quote(full_init, safe='')
                
                # Se decrittazione server, passiamo endpoint decrypt
                uri = f"{proxy_base}/decrypt/init.mp4?url={encoded_init}{dec_query}{params}" if server_side_decryption else f"{proxy_base}/segment/init.mp4?base_url={encoded_init}{params}"
                lines.append(f'#EXT-X-MAP:URI="{uri}"')

            # 2. Timeline Logic
            segments = []
            timeline = tmpl.find('mpd:SegmentTimeline', self.ns)
            
            if timeline is not None:
                current_time = 0
                current_seq = start_num
                
                for s in timeline.findall('mpd:S', self.ns):
                    t = s.get('t')
                    d = int(s.get('d'))
                    r = int(s.get('r', '0'))
                    
                    if t is not None: current_time = int(t)
                    
                    dur_sec = d / timescale
                    count = r + 1
                    
                    for _ in range(count):
                        # CALCOLO PROGRAM DATE TIME (Il segreto per la stabilità)
                        # Somma availabilityStartTime + (timestamp corrente / timescale)
                        pdt = availability_start_time + timedelta(seconds=(current_time / timescale))
                        
                        segments.append({
                            'number': current_seq,
                            'time': current_time,
                            'duration': dur_sec,
                            'pdt': pdt.strftime('%Y-%m-%dT%H:%M:%S.%fZ') # Formato ISO8601 completo
                        })
                        current_time += d
                        current_seq += 1
            else:
                # Fallback VOD
                dur = int(tmpl.get('duration', 0))
                if dur > 0:
                    dur_sec = dur / timescale
                    count = 100 # Default limit
                    if not is_live:
                         period = root.find('mpd:Period', self.ns)
                         if period and period.get('duration'):
                             total = self._parse_iso8601_duration(period.get('duration'))
                             count = int(total / dur_sec)
                    
                    for i in range(count):
                        segments.append({
                            'number': start_num + i,
                            'time': (start_num + i) * dur,
                            'duration': dur_sec,
                            'pdt': None
                        })

            # 3. FILTRO LIVE (Buffer Window + Safety Hold-Back)
            if is_live and segments:
                # Finestra di 3 minuti
                window = 180.0
                total_dur = sum(s['duration'] for s in segments)
                
                # Se abbiamo troppi segmenti (più della finestra), rimuoviamo i vecchi dall'inizio
                if total_dur > window:
                    keep = []
                    acc = 0.0
                    for s in reversed(segments):
                        keep.insert(0, s)
                        acc += s['duration']
                        if acc >= window: break
                    segments = keep

                # --- HOLD-BACK DI SICUREZZA (CRUCIALE) ---
                # Rimuovi gli ultimi 3 segmenti.
                # Questo impedisce al player di chiedere file che la CDN sta ancora scrivendo (Errori 404).
                if len(segments) > 3:
                    segments = segments[:-3]

                if segments:
                    # Sequence Number corretto allineato con DASH
                    lines.append(f'#EXT-X-MEDIA-SEQUENCE:{segments[0]["number"]}')

            # 4. Finalizzazione
            # Target Duration arrotondata sempre per eccesso
            max_dur = math.ceil(max(s['duration'] for s in segments)) if segments else 6
            lines.insert(2, f'#EXT-X-TARGETDURATION:{max_dur}')
            
            if not is_live: lines.append('#EXT-X-PLAYLIST-TYPE:VOD')

            for seg in segments:
                # SCRIVI PROGRAM DATE TIME
                if seg.get('pdt'):
                    lines.append(f'#EXT-X-PROGRAM-DATE-TIME:{seg["pdt"]}')
                
                # Genera URL segmento
                name = self._process_template(media_tmpl, rep_id, number=seg['number'], time=seg['time'], bandwidth=bw)
                full = urljoin(base_url, name)
                enc = urllib.parse.quote(full, safe='')
                
                uri = f"{proxy_base}/decrypt/segment.mp4?url={enc}&init_url={encoded_init}{dec_query}{params}" if server_side_decryption else f"{proxy_base}/segment/{name}?base_url={enc}{params}"
                
                lines.append(f'#EXTINF:{seg["duration"]:.5f},')
                lines.append(uri)

            if not is_live: lines.append('#EXT-X-ENDLIST')

            return '\n'.join(lines)

        except Exception as e:
            logger.error(f"Playlist Error: {e}")
            return f"#EXTM3U\n#EXT-X-ERROR: {e}"
