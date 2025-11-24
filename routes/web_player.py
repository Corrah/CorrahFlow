import logging
import aiohttp
import re
import urllib.parse
import gzip
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from aiohttp import web
from typing import List, Dict, Optional
import asyncio

logger = logging.getLogger(__name__)

# Simple in-memory cache for EPGs: {url: {data: ET.Element, timestamp: float}}
EPG_CACHE = {}

web_player_bp = web.RouteTableDef()

async def _fetch_and_parse_epg(session: aiohttp.ClientSession, url: str) -> Optional[ET.Element]:
    """Fetches, parses, and caches an EPG from a given URL."""
    try:
        # Check cache first
        cached = EPG_CACHE.get(url)
        if cached and (datetime.now().timestamp() - cached['timestamp'] < 3600):
            return cached['data']

        async with session.get(url) as response:
            if response.status == 200:
                content = await response.read()
                
                # Handle GZIP
                if url.endswith('.gz'):
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(content)) as f:
                            xml_content = f.read()
                    except (gzip.BadGzipFile, OSError):
                        xml_content = content  # Fallback if not a valid gzip
                else:
                    xml_content = content
                    
                root = ET.fromstring(xml_content)
                EPG_CACHE[url] = {'data': root, 'timestamp': datetime.now().timestamp()}
                logger.info(f"Successfully fetched and cached EPG from {url}")
                return root
            else:
                logger.error(f"Failed to fetch EPG from {url}: status {response.status}")
                return None
    except Exception as e:
        logger.error(f"Error fetching/parsing EPG {url}: {e}")
        return None

async def preload_epg_data(epg_urls: List[str]):
    """Pre-fetches and caches EPG data from a list of URLs."""
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_and_parse_epg(session, url) for url in epg_urls]
        await asyncio.gather(*tasks)
    logger.info("EPG pre-loading process completed.")


@web_player_bp.get('/player')
async def player_page(request):
    """Renders the player HTML page."""
    try:
        with open('templates/player.html', 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type='text/html')
    except Exception as e:
        logger.error(f"Error serving player page: {e}")
        return web.Response(text="Error loading player page", status=500)

@web_player_bp.post('/player/parse')
async def parse_playlist(request):
    """Parses a playlist URL and returns channels and EPGs (POST)."""
    try:
        data = await request.json()
        playlist_url = data.get('url')
        use_proxy = data.get('proxy', False)
        return await _parse_playlist_logic(request, playlist_url, use_proxy)
    except Exception as e:
        logger.error(f"Error parsing playlist: {e}")
        return web.json_response({'error': str(e)}, status=500)

@web_player_bp.get('/api/parse-playlist')
async def api_parse_playlist(request):
    """Parses a playlist URL and returns channels and EPGs (GET)."""
    try:
        playlist_url = request.query.get('url')
        use_proxy = True # Default to true for GET endpoint as per frontend behavior
        return await _parse_playlist_logic(request, playlist_url, use_proxy)
    except Exception as e:
        logger.error(f"Error parsing playlist: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def _parse_playlist_logic(request, playlist_url, use_proxy):
    if not playlist_url:
        return web.json_response({'error': 'Missing URL'}, status=400)

    # Download playlist with timeout
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(playlist_url) as response:
                if response.status != 200:
                    return web.json_response({'error': f'Failed to download playlist: {response.status}'}, status=400)
                content = await response.text()
        except asyncio.TimeoutError:
            return web.json_response({'error': 'Timeout downloading playlist'}, status=408)
        except Exception as e:
            return web.json_response({'error': f'Error downloading playlist: {str(e)}'}, status=500)

    lines = content.splitlines()
    channels = []
    epg_urls = []
    
    # Parse EPG URLs from #EXTM3U header
    if lines and lines[0].startswith('#EXTM3U'):
        header = lines[0]
        tvg_match = re.search(r'url-tvg="([^"]+)"', header)
        if tvg_match:
            epg_urls = [url.strip() for url in tvg_match.group(1).split(',')]

    # Parse Channels
    current_channel = {}
    
    # Base URL for proxying
    scheme = request.url.scheme
    host = request.url.host
    port = request.url.port
    base_url = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"

    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith('#EXTINF:'):
            info_part = line[8:]
            
            if ',' in info_part:
                meta, name = info_part.rsplit(',', 1)
                current_channel['name'] = name.strip()
            else:
                meta = info_part
                current_channel['name'] = "Unknown Channel"
            
            # Extract Logo
            logo_match = re.search(r'tvg-logo="([^"]+)"', meta)
            if logo_match:
                current_channel['logo'] = logo_match.group(1)
            
            # Extract Group
            group_match = re.search(r'group-title="([^"]+)"', meta)
            if group_match:
                current_channel['group'] = group_match.group(1)
            else:
                current_channel['group'] = "Uncategorized"

            # Extract TVG-ID
            id_match = re.search(r'tvg-id="([^"]+)"', meta)
            if id_match:
                current_channel['id'] = id_match.group(1)
            else:
                current_channel['id'] = current_channel['name']
        
        elif line.startswith('#KODIPROP:'):
            # Extract license key
            if 'inputstream.adaptive.license_key=' in line:
                key = line.split('inputstream.adaptive.license_key=', 1)[1].strip()
                current_channel['clearkey'] = key

        elif not line.startswith('#'):
            # URL line
            original_url = line
            
            # Always return the original URL in the 'url' field because the frontend
            # constructs the proxy URL itself using the proxy toggle state.
            current_channel['url'] = original_url
            
            if 'name' in current_channel:
                channels.append(current_channel)
            
            current_channel = {}

    return web.json_response({
        'channels': channels,
        'epg_urls': epg_urls
    })

@web_player_bp.post('/player/epg/all')
async def get_all_epg(request):
    """Fetches all EPG info from given EPG URLs."""
    try:
        data = await request.json()
        epg_urls = data.get('epg_urls', [])

        if not epg_urls:
            return web.json_response({'error': 'No EPG URLs provided'}, status=400)

        all_programs = {}
        now = datetime.now(timezone.utc)

        def parse_xmltv_time(time_str):
            try:
                if ' ' in time_str:
                    dt_part, tz_part = time_str.split(' ')
                    dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S")
                    return dt.replace(tzinfo=timezone.utc)
                else:
                    return datetime.strptime(time_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except:
                return None

        async with aiohttp.ClientSession() as session:
            # Fetch all EPG URLs in parallel
            tasks = [_fetch_and_parse_epg(session, url) for url in epg_urls]
            roots = await asyncio.gather(*tasks, return_exceptions=True)

            for root in roots:
                if isinstance(root, Exception):
                    logger.error(f"Error fetching EPG: {root}")
                    continue
                if root:
                    for programme in root.findall('programme'):
                        tvg_id = programme.get('channel')
                        if tvg_id:
                            start_str = programme.get('start')
                            stop_str = programme.get('stop')

                            start = parse_xmltv_time(start_str)
                            stop = parse_xmltv_time(stop_str)

                            if start and stop and start <= now <= stop:
                                if tvg_id not in all_programs:
                                    title_elem = programme.find('title')
                                    desc_elem = programme.find('desc')

                                    all_programs[tvg_id] = {
                                        'title': title_elem.text if title_elem is not None else "No Title",
                                        'description': desc_elem.text if desc_elem is not None else "No Description",
                                        'start': start.strftime("%H:%M"),
                                        'stop': stop.strftime("%H:%M")
                                    }
        return web.json_response(all_programs)

    except Exception as e:
        logger.error(f"Error in all_epg endpoint: {e}")
        return web.json_response({'error': str(e)}, status=500)

@web_player_bp.post('/player/epg')
async def get_epg(request):
    """Fetches EPG info for a specific channel."""
    try:
        data = await request.json()
        epg_urls = data.get('epg_urls', [])
        tvg_id = data.get('tvg_id')
        
        if not tvg_id:
            return web.json_response({'error': 'Missing tvg_id'}, status=400)
            
        if not epg_urls:
            return web.json_response({'error': 'No EPG URLs provided'}, status=400)

        current_program = None
        
        # Helper to parse time format "YYYYMMDDhhmmss +0000"
        def parse_xmltv_time(time_str):
            try:
                # Remove space and timezone offset for simple parsing if needed, 
                # but better to handle it. XMLTV usually is "20241124203000 +0000"
                # For simplicity, we'll just take the first part and assume UTC if +0000
                # or try to parse it fully.
                # Let's try a robust approach.
                if ' ' in time_str:
                    dt_part, tz_part = time_str.split(' ')
                    dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S")
                    # Ideally handle timezone, but for now assume UTC or ignore
                    return dt.replace(tzinfo=timezone.utc)
                else:
                    return datetime.strptime(time_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except:
                return None

        now = datetime.now(timezone.utc)

        async with aiohttp.ClientSession() as session:
            for url in epg_urls:
                try:
                    root = await _fetch_and_parse_epg(session, url)
                    
                    if root:
                        # Find program for this channel and time
                        # XMLTV format: <programme start="..." stop="..." channel="...">
                        for programme in root.findall('programme'):
                            if programme.get('channel') == tvg_id:
                                start_str = programme.get('start')
                                stop_str = programme.get('stop')
                                
                                start = parse_xmltv_time(start_str)
                                stop = parse_xmltv_time(stop_str)
                                
                                if start and stop and start <= now <= stop:
                                    title_elem = programme.find('title')
                                    desc_elem = programme.find('desc')
                                    
                                    current_program = {
                                        'title': title_elem.text if title_elem is not None else "No Title",
                                        'description': desc_elem.text if desc_elem is not None else "No Description",
                                        'start': start.strftime("%H:%M"),
                                        'stop': stop.strftime("%H:%M")
                                    }
                                    break
                        
                        if current_program:
                            break # Found it, stop searching other EPGs

                except Exception as e:
                    logger.error(f"Error fetching/parsing EPG {url}: {e}")
                    continue

        if current_program:
            return web.json_response(current_program)
        else:
            return web.json_response({'error': 'Program not found'}, status=404)

    except Exception as e:
        logger.error(f"Error in EPG endpoint: {e}")
        return web.json_response({'error': str(e)}, status=500)
