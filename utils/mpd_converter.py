import re
import math
import logging
import os
import urllib.parse
from urllib.parse import urljoin
from typing import List, Dict, Optional, Tuple

# Tenta di usare lxml per prestazioni migliori, fallback su standard lib
try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

class MPDToHLSConverter:
    """
    Converte manifest MPD (DASH) in playlist HLS (m3u8) on-the-fly.
    Ottimizzato per MediaFlow Proxy.
    """
    
    def __init__(self):
        self.ns = {
            'mpd': 'urn:mpeg:dash:schema:mpd:2011',
            'cenc': 'urn:mpeg:cenc:2013',
            'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
        }
        # Regex pre-compilate per performance
        self.re_duration = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(\.\d+)?)S)?')
        self.re_template_bw = re.compile(r'\$Bandwidth(?s:%.[^$]+)?\$')
        self.re_template_rep = re.compile(r'\$RepresentationID(?s:%.[^$]+)?\$')
        self.re_template_num = re.compile(r'\$Number(?:(%[^$]+))?\$')
        self.re_template_time = re.compile(r'\$Time(?:(%[^$]+))?\$')

    def _parse_iso8601(self, duration_str: str) -> float:
        """Converte durata ISO8601 (es. PT10.5S) in secondi."""
        if not duration_str: return 0.0
        match = self.re_duration.match(duration_str)
        if not match: return 0.0
        h = float(match.group(1) or 0)
        m = float(match.group(2) or 0)
        s = float(match.group(3) or 0)
        return (h * 3600) + (m * 60) + s

    def _process_template(self, url_template: str, rep_id: str, number: int = None, time: int = None, bandwidth: int = None) -> str:
        """Gestisce i template DASH con formattazione printf-style."""
        url = url_template
        
        if bandwidth is not None:
            url = self.re_template_bw.sub(str(bandwidth), url)
            
        url = self.re_template_rep.sub(str(rep_id), url)

        if number is not None:
            def repl_num(m):
                fmt = m.group(1)
                return fmt % number if fmt else str(number)
            url = self.re_template_num.sub(repl_num, url)

        if time is not None:
            def repl_time(m):
                fmt = m.group(1)
                return fmt % time if fmt else str(time)
            url = self.re_template_time.sub(repl_time, url)
            
        return url

    def convert_master_playlist(self, manifest_content: str, proxy_base: str, original_url: str, params: str) -> str:
        """Genera la Master Playlist HLS dagli AdaptationSet del MPD."""
        try:
            # Fix namespace se assente per evitare errori di parsing
            if b'xmlns' not in manifest_content.encode('utf-8') if isinstance(manifest_content, str) else b'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace('<MPD', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
            
            if isinstance(manifest_content, str):
                manifest_content = manifest_content.encode('utf-8')
                
            root = ET.fromstring(manifest_content)
            lines = ['#EXTM3U', '#EXT-X-VERSION:3']
            
            video_sets = []
            audio_sets = []
            subtitle_sets = []
            
            # Estrazione AdaptationSets
            for adaptation_set in root.findall('.//mpd:AdaptationSet', self.ns):
                mime = adaptation_set.get('mimeType', '')
                content = adaptation_set.get('contentType', '')
                
                if 'video' in mime or 'video' in content:
                    video_sets.append(adaptation_set)
                elif 'audio' in mime or 'audio' in content:
                    audio_sets.append(adaptation_set)
                elif 'text' in mime or 'subtitles' in content or 'application/ttml+xml' in mime:
                    subtitle_sets.append(adaptation_set)
                # Fallback basato sulle representation interne
                else:
                    if adaptation_set.find('mpd:Representation[@mimeType="video/mp4"]', self.ns) is not None:
                        video_sets.append(adaptation_set)
                    elif adaptation_set.find('mpd:Representation[@mimeType="audio/mp4"]', self.ns) is not None:
                        audio_sets.append(adaptation_set)

            # --- AUDIO & SOTTOTITOLI ---
            audio_group_id = 'audio'
            subs_group_id = 'subs'
            has_audio = False
            has_subs = False
            
            # Process Audio
            for i, aset in enumerate(audio_sets):
                lang = aset.get('lang', 'und')
                # Prendi la prima representation valida per i metadati
                rep = aset.find('mpd:Representation', self.ns)
                if rep is None: continue
                
                rep_id = rep.get('id')
                bw = rep.get('bandwidth', '128000')
                
                encoded_url = urllib.parse.quote(original_url, safe='')
                media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{params}"
                
                name = f"Audio {lang.upper()} ({int(bw)//1000}k)"
                is_default = "YES" if i == 0 else "NO"
                
                lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group_id}",NAME="{name}",LANGUAGE="{lang}",DEFAULT={is_default},AUTOSELECT=YES,URI="{media_url}"')
                has_audio = True

            # Process Subtitles
            for i, aset in enumerate(subtitle_sets):
                lang = aset.get('lang', 'und')
                rep = aset.find('mpd:Representation', self.ns)
                if rep is None: continue
                rep_id = rep.get('id')
                
                encoded_url = urllib.parse.quote(original_url, safe='')
                media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{params}"
                name = f"Sub {lang.upper()}"
                
                lines.append(f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{subs_group_id}",NAME="{name}",LANGUAGE="{lang}",AUTOSELECT=YES,URI="{media_url}"')
                has_subs = True

            # --- VIDEO ---
            for aset in video_sets:
                for rep in aset.findall('mpd:Representation', self.ns):
                    rep_id = rep.get('id')
                    bw = int(rep.get('bandwidth', 0))
                    width = rep.get('width')
                    height = rep.get('height')
                    fps = rep.get('frameRate')
                    codecs = rep.get('codecs') or aset.get('codecs')
                    
                    encoded_url = urllib.parse.quote(original_url, safe='')
                    media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{params}"
                    
                    inf_parts = [f'BANDWIDTH={bw}']
                    if width and height: inf_parts.append(f'RESOLUTION={width}x{height}')
                    if fps: inf_parts.append(f'FRAME-RATE={fps}')
                    if codecs: inf_parts.append(f'CODECS="{codecs}"')
                    if has_audio: inf_parts.append(f'AUDIO="{audio_group_id}"')
                    if has_subs: inf_parts.append(f'SUBTITLES="{subs_group_id}"')
                    
                    lines.append(f'#EXT-X-STREAM-INF:{",".join(inf_parts)}')
                    lines.append(media_url)
            
            return '\n'.join(lines)
        except Exception as e:
            logger.exception("Errore Master Playlist")
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)

    def convert_media_playlist(self, manifest_content: str, rep_id: str, proxy_base: str, original_url: str, params: str, clearkey_param: str = None) -> str:
        """Genera la Media Playlist HLS per una specifica Representation."""
        try:
            if isinstance(manifest_content, str):
                manifest_content = manifest_content.encode('utf-8')
                
            # Fix namespace al volo
            if b'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace(b'<MPD', b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
                
            root = ET.fromstring(manifest_content)
            
            mpd_type = root.get('type', 'static')
            is_live = mpd_type.lower() == 'dynamic'
            min_buffer_time = self._parse_iso8601(root.get('minBufferTime', 'PT2S'))
            
            # Trova la Representation target
            representation = None
            adaptation_set = None
            for aset in root.findall('.//mpd:AdaptationSet', self.ns):
                rep = aset.find(f'mpd:Representation[@id="{rep_id}"]', self.ns)
                if rep is not None:
                    representation = rep
                    adaptation_set = aset
                    break
            
            if representation is None:
                return "#EXTM3U\n#EXT-X-ERROR: Representation not found"

            bandwidth = int(representation.get('bandwidth', 0))
            
            # Header HLS
            lines = ['#EXTM3U', '#EXT-X-VERSION:7']
            
            # Configurazione DRM Server-side
            server_side_decryption = False
            decryption_query = ""
            if clearkey_param:
                try:
                    kid, key = clearkey_param.split(':')
                    server_side_decryption = True
                    decryption_query = f"&key={key}&key_id={kid}"
                except: pass

            # Base URL Resolution
            base_url_node = root.find('mpd:BaseURL', self.ns)
            if base_url_node is not None and base_url_node.text:
                 # Gestisce BaseURL relativi e assoluti
                 base_url = urljoin(original_url, base_url_node.text)
            else:
                 base_url = os.path.dirname(original_url)
            
            if not base_url.endswith('/'): base_url += '/'

            # Strategia Template
            segment_template = representation.find('mpd:SegmentTemplate', self.ns)
            if segment_template is None:
                segment_template = adaptation_set.find('mpd:SegmentTemplate', self.ns)
            
            if not segment_template:
                return "#EXTM3U\n#EXT-X-ERROR: SegmentTemplate required (SegmentList not implemented)"

            timescale = int(segment_template.get('timescale', '1'))
            init_template = segment_template.get('initialization')
            media_template = segment_template.get('media')
            start_number = int(segment_template.get('startNumber', '1'))
            
            # --- INITIALIZATION SEGMENT ---
            encoded_init_url = ""
            if init_template:
                init_url = self._process_template(init_template, rep_id, bandwidth=bandwidth)
                full_init_url = urljoin(base_url, init_url)
                encoded_init_url = urllib.parse.quote(full_init_url, safe='')
                
                if not server_side_decryption:
                    lines.append(f'#EXT-X-MAP:URI="{proxy_base}/segment/init.mp4?base_url={encoded_init_url}{params}"')

            # --- SEGMENT GENERATION ---
            segments = []
            timeline = segment_template.find('mpd:SegmentTimeline', self.ns)
            
            # Metodo 1: SegmentTimeline (Più comune e preciso)
            if timeline is not None:
                current_time = 0
                current_seq = start_number
                
                for s in timeline.findall('mpd:S', self.ns):
                    t = s.get('t')
                    d = int(s.get('d'))
                    r = int(s.get('r', '0'))
                    
                    # Gestione Discontinuità Temporali
                    if t is not None:
                        new_time = int(t)
                        # Se c'è un salto significativo (> 1 secondo), segna discontinuità
                        if segments and (new_time - current_time) > timescale:
                             segments[-1]['discontinuity'] = True
                        current_time = new_time
                    
                    # Espandi ripetizioni (r)
                    # r può essere negativo (loop fino alla fine), ma raro in live moderni. Qui gestiamo standard >= 0
                    count = r + 1
                    duration_sec = d / timescale
                    
                    for _ in range(count):
                        segments.append({
                            'number': current_seq,
                            'time': current_time,
                            'duration': duration_sec,
                            'discontinuity': False
                        })
                        current_time += d
                        current_seq += 1
            
            # Metodo 2: Calcolo basato su durata fissa (VOD/Live semplice)
            else:
                duration = int(segment_template.get('duration', '0'))
                if duration > 0:
                    # Stima approssimativa per VOD se non c'è timeline
                    period = root.find('mpd:Period', self.ns)
                    p_dur_str = period.get('duration') if period is not None else None
                    total_duration = self._parse_iso8601(p_dur_str) if p_dur_str else 0
                    
                    seg_duration = duration / timescale
                    num_segments = int(total_duration / seg_duration) if total_duration > 0 else 10  # Fallback
                    
                    for i in range(num_segments):
                        segments.append({
                            'number': start_number + i,
                            'time': (start_number + i) * duration, # time approssimato
                            'duration': seg_duration,
                            'discontinuity': False
                        })

            # --- FILTRO LIVE (DVR Window) ---
            if is_live and segments:
                # Leggi timeShiftBufferDepth (finestra DVR)
                dvr_depth_str = root.get('timeShiftBufferDepth')
                # Default a 300s se manca in live, o infinito (nessun taglio) se non specificato
                dvr_window = self._parse_iso8601(dvr_depth_str) if dvr_depth_str else 300.0
                
                # Calcola durata totale disponibile
                total_available_dur = sum(s['duration'] for s in segments)
                
                # Se il buffer è pieno, taglia i segmenti vecchi
                if total_available_dur > dvr_window:
                    kept_segments = []
                    accumulated = 0.0
                    # Prendi dal fondo finché non riempi la finestra
                    for seg in reversed(segments):
                        kept_segments.insert(0, seg)
                        accumulated += seg['duration']
                        if accumulated >= dvr_window:
                            break
                    segments = kept_segments
                
                # Imposta Sequence Number e Target Duration
                if segments:
                    lines.append(f'#EXT-X-MEDIA-SEQUENCE:{segments[0]["number"]}')
                    # Calcola Target Duration arrotondata per eccesso
                    max_dur = max(s['duration'] for s in segments)
                    lines.insert(2, f'#EXT-X-TARGETDURATION:{math.ceil(max_dur)}')
                else:
                    # Edge case: nessun segmento
                    lines.insert(2, '#EXT-X-TARGETDURATION:6')
                    
            else:
                # VOD
                if segments:
                    max_dur = max(s['duration'] for s in segments)
                    lines.insert(2, f'#EXT-X-TARGETDURATION:{math.ceil(max_dur)}')
                    lines.append(f'#EXT-X-PLAYLIST-TYPE:VOD')

            # --- COSTRUZIONE PLAYLIST ---
            for seg in segments:
                if seg.get('discontinuity'):
                    lines.append('#EXT-X-DISCONTINUITY')
                
                # Genera URL segmento
                seg_name = self._process_template(
                    media_template, 
                    rep_id, 
                    number=seg['number'], 
                    time=seg['time'], 
                    bandwidth=bandwidth
                )
                
                full_seg_url = urljoin(base_url, seg_name)
                encoded_seg_url = urllib.parse.quote(full_seg_url, safe='')
                
                lines.append(f'#EXTINF:{seg["duration"]:.6f},')
                
                if server_side_decryption:
                    # URL per decrypt proxy
                    lines.append(f"{proxy_base}/decrypt/segment.mp4?url={encoded_seg_url}&init_url={encoded_init_url}{decryption_query}{params}")
                else:
                    # URL proxy standard
                    lines.append(f"{proxy_base}/segment/{seg_name}?base_url={encoded_seg_url}{params}")

            if not is_live:
                lines.append('#EXT-X-ENDLIST')

            return '\n'.join(lines)

        except Exception as e:
            logger.exception(f"Errore Media Playlist rep_id={rep_id}")
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)
