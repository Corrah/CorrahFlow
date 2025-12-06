import re
import math
import logging
import os
import urllib.parse
from urllib.parse import urljoin
from typing import List, Dict, Optional, Any

# Tenta di usare lxml per prestazioni migliori (parsing XML 5-10x più veloce),
# altrimenti usa la libreria standard xml.etree.ElementTree
try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET

# Configurazione Logging
logger = logging.getLogger(__name__)

class MPDToHLSConverter:
    """
    Converte manifest MPD (DASH) in playlist HLS (m3u8) on-the-fly.
    Ottimizzato per stabilità live stream, gestione buffer e corretta formattazione dei template.
    """
    
    def __init__(self):
        # Namespace XML standard per DASH e protezione contenuti
        self.ns = {
            'mpd': 'urn:mpeg:dash:schema:mpd:2011',
            'cenc': 'urn:mpeg:cenc:2013',
            'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
        }
        
        # Regex pre-compilate per massimizzare le prestazioni durante le richieste frequenti
        # Parsing durata ISO8601 (es. PT1H2M3.5S)
        self.re_duration = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(\.\d+)?)S)?')
        
        # Regex per template DASH (es. $Number%05d$)
        self.re_template_bw = re.compile(r'\$Bandwidth(?s:%.[^$]+)?\$')
        self.re_template_rep = re.compile(r'\$RepresentationID(?s:%.[^$]+)?\$')
        self.re_template_num = re.compile(r'\$Number(?:(%[^$]+))?\$')
        self.re_template_time = re.compile(r'\$Time(?:(%[^$]+))?\$')

    def _parse_iso8601(self, duration_str: str) -> float:
        """Converte una durata in formato ISO8601 (stringa) in secondi (float)."""
        if not duration_str: 
            return 0.0
        match = self.re_duration.match(duration_str)
        if not match: 
            return 0.0
        
        h = float(match.group(1) or 0)
        m = float(match.group(2) or 0)
        s = float(match.group(3) or 0)
        return (h * 3600) + (m * 60) + s

    def _process_template(self, url_template: str, rep_id: str, number: int = None, time: int = None, bandwidth: int = None) -> str:
        """
        Sostituisce i placeholder DASH ($Number$, $Time$, ecc.) con i valori reali,
        rispettando eventuali formattazioni printf-style (es. %05d).
        """
        url = url_template
        
        # Sostituzione Bandwidth
        if bandwidth is not None:
            url = self.re_template_bw.sub(str(bandwidth), url)
            
        # Sostituzione ID
        url = self.re_template_rep.sub(str(rep_id), url)

        # Sostituzione Number
        if number is not None:
            def repl_num(m):
                fmt = m.group(1)
                # Se c'è un formato (es. %05d), lo applica, altrimenti usa str() semplice
                return fmt % number if fmt else str(number)
            url = self.re_template_num.sub(repl_num, url)

        # Sostituzione Time
        if time is not None:
            def repl_time(m):
                fmt = m.group(1)
                return fmt % time if fmt else str(time)
            url = self.re_template_time.sub(repl_time, url)
            
        return url

    def convert_master_playlist(self, manifest_content: str, proxy_base: str, original_url: str, params: str) -> str:
        """
        Genera la Master Playlist HLS (elenco delle varianti Audio/Video).
        """
        try:
            # Gestione encoding e fix namespace mancante
            if isinstance(manifest_content, str):
                manifest_content = manifest_content.encode('utf-8')
            
            if b'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace(b'<MPD', b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
            
            root = ET.fromstring(manifest_content)
            
            lines = ['#EXTM3U', '#EXT-X-VERSION:3']
            
            video_sets = []
            audio_sets = []
            subtitle_sets = []
            
            # Classificazione AdaptationSet
            for adaptation_set in root.findall('.//mpd:AdaptationSet', self.ns):
                mime = adaptation_set.get('mimeType', '')
                content = adaptation_set.get('contentType', '')
                
                if 'video' in mime or 'video' in content:
                    video_sets.append(adaptation_set)
                elif 'audio' in mime or 'audio' in content:
                    audio_sets.append(adaptation_set)
                elif 'text' in mime or 'subtitles' in content or 'application/ttml+xml' in mime:
                    subtitle_sets.append(adaptation_set)
                else:
                    # Fallback detection basata sulle representation figlie
                    if adaptation_set.find('mpd:Representation[@mimeType="video/mp4"]', self.ns) is not None:
                        video_sets.append(adaptation_set)
                    elif adaptation_set.find('mpd:Representation[@mimeType="audio/mp4"]', self.ns) is not None:
                        audio_sets.append(adaptation_set)

            # --- GESTIONE AUDIO ---
            audio_group_id = 'audio'
            has_audio = False
            
            for i, aset in enumerate(audio_sets):
                lang = aset.get('lang', 'und')
                # Prende la prima representation per i dettagli tecnici
                rep = aset.find('mpd:Representation', self.ns)
                if rep is None: continue
                
                rep_id = rep.get('id')
                bw = rep.get('bandwidth', '128000')
                
                encoded_url = urllib.parse.quote(original_url, safe='')
                media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{params}"
                
                # Nome leggibile per il menu audio
                name = f"Audio {lang.upper()} ({int(bw)//1000}k)"
                is_default = "YES" if i == 0 else "NO"
                
                lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group_id}",NAME="{name}",LANGUAGE="{lang}",DEFAULT={is_default},AUTOSELECT=YES,URI="{media_url}"')
                has_audio = True

            # --- GESTIONE SOTTOTITOLI ---
            subs_group_id = 'subs'
            has_subs = False
            
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

            # --- GESTIONE VIDEO ---
            for aset in video_sets:
                # Codec a livello di AdaptationSet (fallback)
                aset_codecs = aset.get('codecs')
                
                for rep in aset.findall('mpd:Representation', self.ns):
                    rep_id = rep.get('id')
                    bw = int(rep.get('bandwidth', 0))
                    width = rep.get('width')
                    height = rep.get('height')
                    fps = rep.get('frameRate')
                    codecs = rep.get('codecs') or aset_codecs
                    
                    encoded_url = urllib.parse.quote(original_url, safe='')
                    media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={rep_id}{params}"
                    
                    # Costruzione attributi EXT-X-STREAM-INF
                    inf_parts = [f'BANDWIDTH={bw}']
                    if width and height: 
                        inf_parts.append(f'RESOLUTION={width}x{height}')
                    if fps: 
                        inf_parts.append(f'FRAME-RATE={fps}')
                    if codecs: 
                        inf_parts.append(f'CODECS="{codecs}"')
                    
                    # Associazione Audio/Sottotitoli
                    if has_audio: 
                        inf_parts.append(f'AUDIO="{audio_group_id}"')
                    if has_subs: 
                        inf_parts.append(f'SUBTITLES="{subs_group_id}"')
                    
                    lines.append(f'#EXT-X-STREAM-INF:{",".join(inf_parts)}')
                    lines.append(media_url)
            
            return '\n'.join(lines)

        except Exception as e:
            logger.exception("Errore generazione Master Playlist")
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)

    def convert_media_playlist(self, manifest_content: str, rep_id: str, proxy_base: str, original_url: str, params: str, clearkey_param: str = None) -> str:
        """
        Genera la Media Playlist HLS per una specifica traccia (Audio/Video).
        Include logica anti-buffering per flussi LIVE.
        """
        try:
            if isinstance(manifest_content, str):
                manifest_content = manifest_content.encode('utf-8')
                
            # Parsing XML
            if b'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace(b'<MPD', b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
                
            root = ET.fromstring(manifest_content)
            
            # Rilevamento tipo stream
            mpd_type = root.get('type', 'static')
            is_live = mpd_type.lower() == 'dynamic'
            
            # Ricerca Representation specifica
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
            
            # Inizio Playlist - Versione 7 necessaria per fMP4
            lines = ['#EXTM3U', '#EXT-X-VERSION:7']
            
            # --- GESTIONE DECRITTAZIONE (ClearKey) ---
            server_side_decryption = False
            decryption_query = ""
            if clearkey_param:
                try:
                    # Formato atteso: kid_hex:key_hex
                    parts = clearkey_param.split(':')
                    if len(parts) == 2:
                        kid, key = parts
                        server_side_decryption = True
                        decryption_query = f"&key={key}&key_id={kid}"
                except Exception as e:
                    logger.error(f"Errore parsing clearkey: {e}")

            # --- RISOLUZIONE BASE URL ---
            base_url_node = root.find('mpd:BaseURL', self.ns)
            if base_url_node is not None and base_url_node.text:
                 base_url = urljoin(original_url, base_url_node.text)
            else:
                 base_url = os.path.dirname(original_url)
            
            if not base_url.endswith('/'): 
                base_url += '/'

            # --- LETTURA TEMPLATE ---
            segment_template = representation.find('mpd:SegmentTemplate', self.ns)
            if segment_template is None:
                # Fallback sull'AdaptationSet
                segment_template = adaptation_set.find('mpd:SegmentTemplate', self.ns)
            
            if not segment_template:
                return "#EXTM3U\n#EXT-X-ERROR: SegmentTemplate required (SegmentList not supported in this mode)"

            timescale = int(segment_template.get('timescale', '1'))
            init_template = segment_template.get('initialization')
            media_template = segment_template.get('media')
            start_number = int(segment_template.get('startNumber', '1'))
            
            # --- 1. EXT-X-MAP (INIT SEGMENT) ---
            # Fondamentale per fMP4: contiene i metadati (moov) per decodificare lo stream.
            if init_template:
                init_url = self._process_template(init_template, rep_id, bandwidth=bandwidth)
                full_init_url = urljoin(base_url, init_url)
                encoded_init_url = urllib.parse.quote(full_init_url, safe='')
                
                if server_side_decryption:
                    # Se decrittiamo, dobbiamo passare anche l'init al decryptor
                    map_uri = f"{proxy_base}/decrypt/init.mp4?url={encoded_init_url}{decryption_query}{params}"
                else:
                    map_uri = f"{proxy_base}/segment/init.mp4?base_url={encoded_init_url}{params}"
                
                lines.append(f'#EXT-X-MAP:URI="{map_uri}"')

            # --- 2. COSTRUZIONE LISTA SEGMENTI ---
            segments = []
            timeline = segment_template.find('mpd:SegmentTimeline', self.ns)
            
            if timeline is not None:
                # Modalità Timeline (precisa)
                current_time = 0
                current_seq = start_number
                
                for s in timeline.findall('mpd:S', self.ns):
                    t = s.get('t')
                    d = int(s.get('d'))
                    r = int(s.get('r', '0'))
                    
                    if t is not None:
                        current_time = int(t)
                    
                    duration_sec = d / timescale
                    # Espansione delle ripetizioni (r)
                    count = r + 1
                    
                    for _ in range(count):
                        segments.append({
                            'number': current_seq,
                            'time': current_time,
                            'duration': duration_sec
                        })
                        current_time += d
                        current_seq += 1
            else:
                # Modalità Duration fissa (Fallback)
                duration = int(segment_template.get('duration', '0'))
                if duration > 0:
                    seg_duration = duration / timescale
                    # Stima segmenti (per Live senza timeline è rischioso, ma ok per VOD)
                    num_segments = 100 # Placeholder
                    
                    # Se VOD, prova a calcolare il totale reale
                    if not is_live:
                        period = root.find('mpd:Period', self.ns)
                        if period is not None and period.get('duration'):
                            total_dur = self._parse_iso8601(period.get('duration'))
                            num_segments = int(total_dur / seg_duration)
                    
                    for i in range(num_segments):
                        segments.append({
                            'number': start_number + i,
                            'time': (start_number + i) * duration,
                            'duration': seg_duration
                        })

            # --- 3. LOGICA LIVE EDGE (ANTI-BUFFERING) ---
            if is_live and segments:
                # A. Gestione Finestra DVR
                dvr_depth_str = root.get('timeShiftBufferDepth')
                # Se non specificato, usiamo una finestra sicura di 3 minuti
                dvr_window = self._parse_iso8601(dvr_depth_str) if dvr_depth_str else 180.0
                
                total_available_dur = sum(s['duration'] for s in segments)
                
                # Taglia i segmenti troppo vecchi che escono dalla finestra DVR
                if total_available_dur > dvr_window:
                    kept_segments = []
                    accumulated = 0.0
                    for seg in reversed(segments):
                        kept_segments.insert(0, seg)
                        accumulated += seg['duration']
                        if accumulated >= dvr_window:
                            break
                    segments = kept_segments
                
                # B. Safety Hold-back (Cruciale per evitare 404)
                # Rimuove gli ultimi 2 segmenti dalla lista per garantire che 
                # siano effettivamente stati scritti e propagati sulla CDN origine.
                if len(segments) > 2:
                    segments = segments[:-2]
                
                # Imposta Media Sequence
                if segments:
                    lines.append(f'#EXT-X-MEDIA-SEQUENCE:{segments[0]["number"]}')
            
            # --- 4. CALCOLO TARGET DURATION ---
            if segments:
                max_duration = max(s['duration'] for s in segments)
                # Arrotondamento per eccesso obbligatorio per standard HLS
                target_duration = math.ceil(max_duration)
                lines.insert(2, f'#EXT-X-TARGETDURATION:{target_duration}')
            else:
                lines.insert(2, '#EXT-X-TARGETDURATION:6')

            if not is_live:
                lines.append('#EXT-X-PLAYLIST-TYPE:VOD')

            # --- 5. SCRITTURA SEGMENTI IN PLAYLIST ---
            for seg in segments:
                # Genera nome file dal template
                seg_name = self._process_template(
                    media_template, 
                    rep_id, 
                    number=seg['number'], 
                    time=seg['time'], 
                    bandwidth=bandwidth
                )
                
                full_seg_url = urljoin(base_url, seg_name)
                encoded_seg_url = urllib.parse.quote(full_seg_url, safe='')
                
                # Durata precisa
                lines.append(f'#EXTINF:{seg["duration"]:.6f},')
                
                if server_side_decryption:
                    # Endpoint decrypt
                    lines.append(f"{proxy_base}/decrypt/segment.mp4?url={encoded_seg_url}&init_url={encoded_init_url}{decryption_query}{params}")
                else:
                    # Endpoint proxy standard
                    lines.append(f"{proxy_base}/segment/{seg_name}?base_url={encoded_seg_url}{params}")

            if not is_live:
                lines.append('#EXT-X-ENDLIST')

            return '\n'.join(lines)

        except Exception as e:
            logger.exception(f"Errore critico Media Playlist rep_id={rep_id}")
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)
