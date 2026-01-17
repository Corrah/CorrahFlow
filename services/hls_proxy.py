import asyncio
import logging
import re
import sys
import random
import os
import urllib.parse
from urllib.parse import urlparse, urljoin
import base64
import binascii
import json
import ssl
import aiohttp
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector, ClientPayloadError, ServerDisconnectedError, ClientConnectionError
from aiohttp_socks import ProxyConnector

from config import GLOBAL_PROXIES, TRANSPORT_ROUTES, get_proxy_for_url, get_ssl_setting_for_url, API_PASSWORD, check_password, MPD_MODE
from extractors.generic import GenericHLSExtractor, ExtractorError
from services.manifest_rewriter import ManifestRewriter

# Legacy MPD converter (used when MPD_MODE=legacy)
MPDToHLSConverter = None
decrypt_segment = None
if MPD_MODE == "legacy":
    try:
        from utils.mpd_converter import MPDToHLSConverter
        from utils.drm_decrypter import decrypt_segment
        logger = logging.getLogger(__name__)
        logger.info("✅ Legacy MPD modules loaded (mpd_converter, drm_decrypter)")
    except ImportError as e:
        logger = logging.getLogger(__name__)
        logger.warning(f"⚠️ MPD_MODE=legacy but modules not found: {e}")

# --- External Modules ---
VavooExtractor, DLHDExtractor, VixSrcExtractor, PlaylistBuilder, SportsonlineExtractor = None, None, None, None, None
MixdropExtractor, VoeExtractor, StreamtapeExtractor, OrionExtractor, FreeshotExtractor = None, None, None, None, None


# Default User-Agent for all outgoing requests
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

logger = logging.getLogger(__name__)


# Conditional import of extractors
try:
    from extractors.freeshot import FreeshotExtractor
    logger.info("✅ FreeshotExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ FreeshotExtractor module not found.")

try:
    from extractors.vavoo import VavooExtractor
    logger.info("✅ VavooExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VavooExtractor module not found. Vavoo functionality disabled.")

try:
    from extractors.dlhd import DLHDExtractor
    logger.info("✅ DLHDExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ DLHDExtractor module not found. DLHD functionality disabled.")

try:
    from routes.playlist_builder import PlaylistBuilder
    logger.info("✅ PlaylistBuilder module loaded.")
except ImportError:
    logger.warning("⚠️ PlaylistBuilder module not found. PlaylistBuilder functionality disabled.")
    
try:
    from extractors.vixsrc import VixSrcExtractor
    logger.info("✅ VixSrcExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VixSrcExtractor module not found. VixSrc functionality disabled.")

try:
    from extractors.sportsonline import SportsonlineExtractor
    logger.info("✅ SportsonlineExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ SportsonlineExtractor module not found. Sportsonline functionality disabled.")

try:
    from extractors.mixdrop import MixdropExtractor
    logger.info("✅ MixdropExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ MixdropExtractor module not found.")

try:
    from extractors.voe import VoeExtractor
    logger.info("✅ VoeExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ VoeExtractor module not found.")

try:
    from extractors.streamtape import StreamtapeExtractor
    logger.info("✅ StreamtapeExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ StreamtapeExtractor module not found.")

try:
    from extractors.orion import OrionExtractor
    logger.info("✅ OrionExtractor module loaded.")
except ImportError:
    logger.warning("⚠️ OrionExtractor module not found.")

class HLSProxy:
    """HLS Proxy to handle Vavoo, DLHD, generic HLS streams and playlist builder with AES-128 support"""
    
    def __init__(self, ffmpeg_manager=None):
        self.extractors = {}
        self.ffmpeg_manager = ffmpeg_manager
        
        # Initialize playlist_builder if the module is available
        if PlaylistBuilder:
            self.playlist_builder = PlaylistBuilder()
            logger.info("✅ PlaylistBuilder initialized")
        else:
            self.playlist_builder = None
        
        # Cache for initialization segments (URL -> content)
        self.init_cache = {}
        
        # Cache for decrypted segments (URL -> (content, timestamp))
        self.segment_cache = {}
        self.segment_cache_ttl = 30  # Seconds
        
        # Prefetch queue for background downloading
        self.prefetch_tasks = set()
        
        # Shared session for proxy (no proxy)
        self.session = None
        
        # Cache for proxy sessions (proxy_url -> session)
        # This reuses connections for the same proxy to improve performance
        self.proxy_sessions = {}

    async def _get_session(self):
        if self.session is None or self.session.closed:
            # Unlimited connections for maximum speed
            connector = TCPConnector(
                limit=0,  # Unlimited connections
                limit_per_host=0,  # Unlimited per host
                keepalive_timeout=60,  # Keep connections alive longer
                enable_cleanup_closed=True
            )
            self.session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=30),
                connector=connector
            )
        return self.session

    async def _get_proxy_session(self, url: str):
        """Get a session with proxy support for the given URL.
        
        Sessions are cached and reused for the same proxy to improve performance.
        
        Returns: (session, should_close) tuple
        - session: The aiohttp ClientSession to use
        - should_close: Always False now since sessions are cached and reused
        """
        proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, GLOBAL_PROXIES)
        
        if proxy:
            # Check if we have a cached session for this proxy
            if proxy in self.proxy_sessions:
                cached_session = self.proxy_sessions[proxy]
                if not cached_session.closed:
                    logger.debug(f"♻️ Reusing cached proxy session: {proxy}")
                    return cached_session, False  # Reuse cached session
                else:
                    # Remove closed session from cache
                    del self.proxy_sessions[proxy]
            
            # Create new session and cache it
            logger.info(f"🌍 Creating proxy session: {proxy}")
            try:
                # Unlimited connections for maximum speed
                connector = ProxyConnector.from_url(
                    proxy,
                    limit=0,  # Unlimited connections
                    limit_per_host=0,  # Unlimited per host
                    keepalive_timeout=60  # Keep connections alive longer
                )
                timeout = ClientTimeout(total=30)
                session = ClientSession(timeout=timeout, connector=connector)
                self.proxy_sessions[proxy] = session  # Cache the session
                return session, False  # Don't close - it's cached for reuse
            except Exception as e:
                logger.warning(f"⚠️ Failed to create proxy connector: {e}, falling back to direct")
        
        # Fallback to shared non-proxy session
        return await self._get_session(), False


    async def get_extractor(self, url: str, request_headers: dict, host: str = None):
        """Gets the appropriate extractor for the URL"""
        try:
            # 1. Manual Selection via 'host' parameter
            if host:
                host = host.lower()
                key = host

                if host == "vavoo":
                    if key not in self.extractors:
                        self.extractors[key] = VavooExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host in ["dlhd", "daddylive", "daddyhd"]:
                    key = "dlhd"
                    if key not in self.extractors:
                        self.extractors[key] = DLHDExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host == "vixsrc":
                    if key not in self.extractors:
                        self.extractors[key] = VixSrcExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host in ["sportsonline", "sportzonline"]:
                    key = "sportsonline"
                    if key not in self.extractors:
                        self.extractors[key] = SportsonlineExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host == "mixdrop":
                    if key not in self.extractors:
                        self.extractors[key] = MixdropExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host == "voe":
                    if key not in self.extractors:
                        self.extractors[key] = VoeExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host == "streamtape":
                    if key not in self.extractors:
                        self.extractors[key] = StreamtapeExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host == "orion":
                    if key not in self.extractors:
                        self.extractors[key] = OrionExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]
                elif host == "freeshot":
                    if key not in self.extractors:
                        self.extractors[key] = FreeshotExtractor(request_headers, proxies=GLOBAL_PROXIES)
                    return self.extractors[key]

            # 2. Auto-detection based on URL
            if "vavoo.to" in url:
                key = "vavoo"
                proxy = get_proxy_for_url('vavoo.to', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VavooExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif any(domain in url for domain in ["daddylive", "dlhd", "daddyhd"]) or re.search(r'watch\.php\?id=\d+', url):
                key = "dlhd"
                proxy = get_proxy_for_url('dlhd.dad', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = DLHDExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif 'vixsrc.to/' in url.lower() and any(x in url for x in ['/movie/', '/tv/', '/iframe/']):
                key = "vixsrc"
                proxy = get_proxy_for_url('vixsrc.to', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VixSrcExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif any(domain in url for domain in ["sportzonline", "sportsonline"]):
                key = "sportsonline"
                proxy = get_proxy_for_url('sportsonline', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = SportsonlineExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif "mixdrop" in url:
                key = "mixdrop"
                proxy = get_proxy_for_url('mixdrop', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = MixdropExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif any(d in url for d in ["voe.sx", "voe.to", "voe.st", "voe.eu", "voe.la", "voe-network.net"]):
                key = "voe"
                proxy = get_proxy_for_url('voe.sx', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = VoeExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif "popcdn.day" in url:
                key = "freeshot"
                proxy = get_proxy_for_url('popcdn.day', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = FreeshotExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif "streamtape.com" in url or "streamtape.to" in url or "streamtape.net" in url:
                key = "streamtape"
                proxy = get_proxy_for_url('streamtape', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = StreamtapeExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            elif "orionoid.com" in url:
                key = "orion"
                proxy = get_proxy_for_url('orionoid.com', TRANSPORT_ROUTES, GLOBAL_PROXIES)
                proxy_list = [proxy] if proxy else []
                if key not in self.extractors:
                    self.extractors[key] = OrionExtractor(request_headers, proxies=proxy_list)
                return self.extractors[key]
            else:
                # ✅ MODIFIED: Fallback to GenericHLSExtractor for any other URL.
                # This allows handling unknown extensions or URLs without extensions.
                key = "hls_generic"
                if key not in self.extractors:
                    self.extractors[key] = GenericHLSExtractor(request_headers, proxies=GLOBAL_PROXIES)
                return self.extractors[key]
        except (NameError, TypeError) as e:
            raise ExtractorError(f"Extractor not available - missing module: {e}")

    async def handle_proxy_request(self, request):
        """Handles main proxy requests"""
        if not check_password(request):
            logger.warning(f"⛔ Access denied: Invalid or missing API Password. IP: {request.remote}")
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        
        extractor = None
        try:
            target_url = request.query.get('url') or request.query.get('d')
            force_refresh = request.query.get('force', 'false').lower() == 'true'
            redirect_stream = request.query.get('redirect_stream', 'true').lower() == 'true'
            
            if not target_url:
                return web.Response(text="Missing 'url' or 'd' parameter", status=400)
            
            try:
                target_url = urllib.parse.unquote(target_url)
            except:
                pass
            
            # ✅ FIX: Extract h_ headers and only specific safe headers from origin request.
            # This prevents header leakage (like Referer/Origin from previous sessions)
            # from being passed to sensitive extractors like Torrentio.
            combined_headers = {}
            # Take only ESSENTIAL headers from original client request
            for h in ['User-Agent', 'Referer', 'Origin', 'Cookie', 'Authorization']:
                if h in request.headers:
                    val = request.headers[h]
                    # ✅ FIX: Prevent Referer Leakage. Only pass Referer/Origin if it seems relevant
                    # to the current domain, OR if it's a standard internal use.
                    if h.lower() in ['referer', 'origin']:
                        target_domain = urllib.parse.urlparse(target_url).netloc
                        ref_domain = urllib.parse.urlparse(val).netloc
                        # If the referer domain is fundamentally different (e.g. leaking from a previous Sky stream), strip it.
                        if ref_domain and target_domain and ref_domain != target_domain:
                            if "torrentio" in target_url.lower() or "resolve" in target_url.lower():
                                logger.debug(f"🛡️ Stripping unrelated Referer leakage: {val}")
                                continue
                    combined_headers[h] = val
            
            # h_ params ALWAYS have priority and override everything else
            for param_name, param_value in request.query.items():
                if param_name.startswith('h_'):
                    header_name = param_name[2:]
                    combined_headers[header_name] = param_value
            
            # DEBUG LOGGING    
            print(f"🔍 [DEBUG] Processing URL: {target_url}")
            print(f"   Headers: {dict(request.headers)}")
            
            extractor = await self.get_extractor(target_url, combined_headers)
            
            print(f"   Extractor: {type(extractor).__name__}")
            
            try:
                # Pass force_refresh flag to the extractor
                result = await extractor.extract(target_url, force_refresh=force_refresh)
                stream_url = result["destination_url"]
                stream_headers = result.get("request_headers", {})

                print(f"   Resolved Stream URL: {stream_url}")
                print(f"   Stream Headers: {stream_headers}")
                
                # If redirect_stream is False, return JSON with details (MediaFlow style)
                if not redirect_stream:
                    # Build the proxy base URL
                    scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
                    host = request.headers.get('X-Forwarded-Host', request.host)
                    proxy_base = f"{scheme}://{host}"
                    
                    mediaflow_endpoint = result.get("mediaflow_endpoint", "hls_proxy")
                    
                    # Determine correct endpoint (updated logic as in extractor)
                    endpoint = "/proxy/hls/manifest.m3u8"
                    if mediaflow_endpoint == "proxy_stream_endpoint" or ".mp4" in stream_url or ".mkv" in stream_url or ".avi" in stream_url:
                         endpoint = "/proxy/stream"
                    elif ".mpd" in stream_url:
                        endpoint = "/proxy/mpd/manifest.m3u8"
                        
                    # Prepare JSON parameters
                    q_params = {}
                    api_password = request.query.get('api_password')
                    if api_password:
                        q_params['api_password'] = api_password
                    
                    response_data = {
                        "destination_url": stream_url,
                        "request_headers": stream_headers,
                        "mediaflow_endpoint": mediaflow_endpoint,
                        "mediaflow_proxy_url": f"{proxy_base}{endpoint}", # Clean URL
                        "query_params": q_params
                    }
                    return web.json_response(response_data)

                # Add custom headers from query params
                h_params_found = []
                for param_name, param_value in request.query.items():
                    if param_name.startswith('h_'):
                        header_name = param_name[2:]
                        h_params_found.append(header_name)
                        
                        # ✅ FIX: Remove any duplicate headers (case-insensitive) present in stream_headers
                        # This ensures the header passed via query param (e.g. h_Referer) has priority
                        # and doesn't conflict with those generated by extractors (e.g. lowercase referer).
                        keys_to_remove = [k for k in stream_headers.keys() if k.lower() == header_name.lower()]
                        for k in keys_to_remove:
                            del stream_headers[k]
                        
                        stream_headers[header_name] = param_value
                
                if h_params_found:
                    logger.debug(f"   Headers overridden by query params: {h_params_found}")
                else:
                    logger.debug("   No h_ params found in query string.")
                    
                # Stream URL resolved
                # ✅ MPD/DASH handling based on MPD_MODE
                if ".mpd" in stream_url or "dash" in stream_url.lower():
                    if MPD_MODE == "ffmpeg" and self.ffmpeg_manager:
                        # FFmpeg transcoding mode
                        logger.info(f"🔄 [FFmpeg Mode] Routing MPD stream: {stream_url}")
                        
                        # Extract ClearKey if present
                        clearkey_param = request.query.get('clearkey')
                        
                        # Support separate key_id and key params (handling multiple keys)
                        if not clearkey_param:
                            key_id_param = request.query.get('key_id')
                            key_val_param = request.query.get('key')
                            
                            if key_id_param and key_val_param:
                                # Check for multiple keys
                                key_ids = key_id_param.split(',')
                                key_vals = key_val_param.split(',')
                                
                                if len(key_ids) == len(key_vals):
                                    clearkey_parts = []
                                    for kid, kval in zip(key_ids, key_vals):
                                        clearkey_parts.append(f"{kid.strip()}:{kval.strip()}")
                                    clearkey_param = ",".join(clearkey_parts)
                                else:
                                    # Fallback or error? defaulting to first or simple concat if mismatch
                                    # Let's try to handle single mismatch case gracefully or just use as is
                                    if len(key_ids) == 1 and len(key_vals) == 1:
                                         clearkey_param = f"{key_id_param}:{key_val_param}"
                                    else:
                                         logger.warning(f"Mismatch in key_id/key count: {len(key_ids)} vs {len(key_vals)}")
                                         # Try to pair as many as possible
                                         min_len = min(len(key_ids), len(key_vals))
                                         clearkey_parts = []
                                         for i in range(min_len):
                                             clearkey_parts.append(f"{key_ids[i].strip()}:{key_vals[i].strip()}")
                                         clearkey_param = ",".join(clearkey_parts)

                            elif key_val_param:
                                clearkey_param = key_val_param
                        
                        playlist_rel_path = await self.ffmpeg_manager.get_stream(stream_url, stream_headers, clearkey=clearkey_param)
                        
                        if playlist_rel_path:
                            # Construct local URL for the FFmpeg stream
                            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
                            host = request.headers.get('X-Forwarded-Host', request.host)
                            local_url = f"{scheme}://{host}/ffmpeg_stream/{playlist_rel_path}"
                            
                            # Generate Master Playlist for compatibility
                            master_playlist = (
                                "#EXTM3U\n"
                                "#EXT-X-VERSION:3\n"
                                "#EXT-X-STREAM-INF:BANDWIDTH=6000000,NAME=\"Live\"\n"
                                f"{local_url}\n"
                            )
                            
                            return web.Response(
                                text=master_playlist,
                                content_type="application/vnd.apple.mpegurl",
                                headers={
                                    "Access-Control-Allow-Origin": "*",
                                    "Cache-Control": "no-cache"
                                }
                            )
                        else:
                            logger.error("❌ FFmpeg failed to start")
                            return web.Response(text="FFmpeg failed to process stream", status=502)
                    else:
                        # Legacy mode: use mpd_converter for HLS conversion with server-side decryption
                        logger.info(f"🔄 [Legacy Mode] Converting MPD to HLS: {stream_url}")
                        
                        if MPDToHLSConverter is None:
                            logger.error("❌ MPDToHLSConverter not available in legacy mode")
                            return web.Response(text="Legacy MPD converter not available", status=503)
                        
                        # Fetch the MPD manifest with proxy support
                        ssl_context = None
                        disable_ssl = get_ssl_setting_for_url(stream_url, TRANSPORT_ROUTES)
                        if disable_ssl:
                            ssl_context = False
                        
                        # Use helper to get proxy-enabled session
                        mpd_session, should_close = await self._get_proxy_session(stream_url)
                        final_mpd_url = stream_url  # Will be updated if redirected
                        
                        try:
                            async with mpd_session.get(stream_url, headers=stream_headers, ssl=ssl_context, allow_redirects=True) as resp:
                                # Capture final URL after redirects (use for segment URL construction)
                                final_mpd_url = str(resp.url)
                                if final_mpd_url != stream_url:
                                    logger.info(f"↪️ MPD redirected to: {final_mpd_url}")
                                
                                if resp.status != 200:
                                    error_text = await resp.text()
                                    logger.error(f"❌ Failed to fetch MPD. Status: {resp.status}, URL: {stream_url}")
                                    logger.error(f"   Headers: {stream_headers}")
                                    logger.error(f"   Response: {error_text[:500]}") # Truncate for safety
                                    return web.Response(text=f"Failed to fetch MPD: {resp.status}\nResponse: {error_text[:1000]}", status=502)
                                manifest_content = await resp.text()
                        finally:
                            # Close the session if we created one for proxy
                            if should_close and mpd_session and not mpd_session.closed:
                                await mpd_session.close()
                        
                        # Build proxy base URL
                        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
                        host = request.headers.get('X-Forwarded-Host', request.host)
                        proxy_base = f"{scheme}://{host}"
                        
                        # Build params string with headers
                        params = "".join([f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}" for key, value in stream_headers.items()])
                        
                        # Add api_password if present
                        api_password = request.query.get('api_password')
                        if api_password:
                            params += f"&api_password={api_password}"
                        
                        # Get ClearKey param
                        clearkey_param = request.query.get('clearkey')
                        if not clearkey_param:
                            key_id_param = request.query.get('key_id')
                            key_val_param = request.query.get('key')
                            
                            if key_id_param and key_val_param:
                                # Check for multiple keys
                                key_ids = key_id_param.split(',')
                                key_vals = key_val_param.split(',')
                                
                                if len(key_ids) == len(key_vals):
                                    clearkey_parts = []
                                    for kid, kval in zip(key_ids, key_vals):
                                        clearkey_parts.append(f"{kid.strip()}:{kval.strip()}")
                                    clearkey_param = ",".join(clearkey_parts)
                                else:
                                    if len(key_ids) == 1 and len(key_vals) == 1:
                                         clearkey_param = f"{key_id_param}:{key_val_param}"
                                    else:
                                         logger.warning(f"Mismatch in key_id/key count: {len(key_ids)} vs {len(key_vals)}")
                                         # Try to pair as many as possible
                                         min_len = min(len(key_ids), len(key_vals))
                                         clearkey_parts = []
                                         for i in range(min_len):
                                             clearkey_parts.append(f"{key_ids[i].strip()}:{key_vals[i].strip()}")
                                         clearkey_param = ",".join(clearkey_parts)
                            elif key_val_param:
                                clearkey_param = key_val_param
                        
                        if clearkey_param:
                            params += f"&clearkey={clearkey_param}"
                        
                        # Pass 'ext' param if present (e.g. ext=ts)
                        ext_param = request.query.get('ext')
                        if ext_param:
                            params += f"&ext={ext_param}"
                        
                        # Check if requesting specific representation
                        rep_id = request.query.get('rep_id')
                        
                        converter = MPDToHLSConverter()
                        if rep_id:
                            # Generate media playlist for specific representation
                            # Use final_mpd_url (after redirects) for segment URL construction
                            hls_content = converter.convert_media_playlist(
                                manifest_content, rep_id, proxy_base, final_mpd_url, params, clearkey_param
                            )
                        else:
                            # Generate master playlist
                            # Use final_mpd_url (after redirects) for segment URL construction
                            hls_content = converter.convert_master_playlist(
                                manifest_content, proxy_base, final_mpd_url, params
                            )
                        
                        return web.Response(
                            text=hls_content,
                            content_type="application/vnd.apple.mpegurl",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Cache-Control": "no-cache"
                            }
                        )
                
                return await self._proxy_stream(request, stream_url, stream_headers)
            except ExtractorError as e:
                logger.warning(f"Extraction failed, retrying by forcing update: {e}")
                result = await extractor.extract(target_url, force_refresh=True) # Always force refresh on second attempt
                stream_url = result["destination_url"]
                stream_headers = result.get("request_headers", {})
                # Stream URL resolved after refresh
                return await self._proxy_stream(request, stream_url, stream_headers)
            
        except Exception as e:
            # ✅ IMPROVED: Distinguish between temporary errors (site offline) and critical errors
            error_msg = str(e).lower()
            is_temporary_error = any(x in error_msg for x in ['403', 'forbidden', '502', 'bad gateway', 'timeout', 'connection', 'temporarily unavailable'])
            
            extractor_name = "unknown"
            if DLHDExtractor and isinstance(extractor, DLHDExtractor):
                extractor_name = "DLHDExtractor"
            elif VavooExtractor and isinstance(extractor, VavooExtractor):
                extractor_name = "VavooExtractor"

            # If it's a temporary error (site offline), log only a WARNING without traceback
            if is_temporary_error:
                logger.warning(f"⚠️ {extractor_name}: Service temporarily unavailable - {str(e)}")
                return web.Response(text=f"Service temporarily unavailable: {str(e)}", status=503)
            
            # For real errors (not temporary), log as CRITICAL with full traceback
            logger.critical(f"❌ Critical error with {extractor_name}: {e}")
            logger.exception(f"Error in proxy request: {str(e)}")
            return web.Response(text=f"Proxy error: {str(e)}", status=500)

    async def handle_extractor_request(self, request):
        """
        MediaFlow-Proxy compatible endpoint to get stream information.
        Supports redirect_stream to redirect directly to the proxy.
        """
        # Log request details for debugging
        logger.info(f"📥 Extractor Request: {request.url}")
        
        if not check_password(request):
            logger.warning("⛔ Unauthorized extractor request")
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        try:
            # Supports both 'url' and 'd' as parameters
            url = request.query.get('url') or request.query.get('d')
            if not url:
                # If no URL, return a JSON help page with available hosts
                help_response = {
                    "message": "EasyProxy Extractor API",
                    "usage": {
                        "endpoint": "/extractor/video",
                        "parameters": {
                            "url": "(Required) URL to extract. Supports plain text, URL encoded, or Base64.",
                            "host": "(Optional) Force specific extractor (bypass auto-detect).",
                            "redirect_stream": "(Optional) 'true' to redirect to stream, 'false' for JSON.",
                            "api_password": "(Optional) API Password if configured."
                        }
                    },
                    "available_hosts": [
                        "vavoo", "dlhd", "daddylive", "vixsrc", "sportsonline", 
                        "mixdrop", "voe", "streamtape", "orion"
                    ],
                    "examples": [
                        f"{request.scheme}://{request.host}/extractor/video?url=https://vavoo.to/channel/123",
                        f"{request.scheme}://{request.host}/extractor/video?host=vavoo&url=https://custom-link.com",
                        f"{request.scheme}://{request.host}/extractor/video?url=BASE64_STRING"
                    ]
                }
                return web.json_response(help_response)

            # Decode URL if necessary
            try:
                url = urllib.parse.unquote(url)
            except:
                pass

            # 2. Base64 Decoding (Try)
            try:
                # Attempt Base64 decoding if it doesn't look like a valid URL or if requested
                # Add padding if necessary
                padded_url = url + '=' * (-len(url) % 4)
                decoded_bytes = base64.b64decode(padded_url, validate=True)
                decoded_str = decoded_bytes.decode('utf-8').strip()
                
                # Check if the result looks like a valid URL
                if decoded_str.startswith('http://') or decoded_str.startswith('https://'):
                    url = decoded_str
                    logger.info(f"🔓 Base64 URL decoded: {url}")
            except Exception:
                # Not Base64 or not a valid URL, continue with the original
                pass
                
            host_param = request.query.get('host')
            redirect_stream = request.query.get('redirect_stream', 'false').lower() == 'true'
            logger.info(f"🔍 Extracting: {url} (Host: {host_param}, Redirect: {redirect_stream})")

            extractor = await self.get_extractor(url, dict(request.headers), host=host_param)
            result = await extractor.extract(url)
            
            stream_url = result["destination_url"]
            stream_headers = result.get("request_headers", {})
            mediaflow_endpoint = result.get("mediaflow_endpoint", "hls_proxy")
            
            logger.info(f"✅ Extraction success: {stream_url[:50]}... Endpoint: {mediaflow_endpoint}")

            # Build the proxy URL for this stream
            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
            host = request.headers.get('X-Forwarded-Host', request.host)
            proxy_base = f"{scheme}://{host}"
            
            # Determine the correct endpoint
            endpoint = "/proxy/hls/manifest.m3u8"
            if mediaflow_endpoint == "proxy_stream_endpoint" or ".mp4" in stream_url or ".mkv" in stream_url or ".avi" in stream_url:
                 endpoint = "/proxy/stream"
            elif ".mpd" in stream_url:
                endpoint = "/proxy/mpd/manifest.m3u8"

            encoded_url = urllib.parse.quote(stream_url, safe='')
            header_params = "".join([f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}" for key, value in stream_headers.items()])
            
            # Add api_password if present
            api_password = request.query.get('api_password')
            if api_password:
                header_params += f"&api_password={api_password}"

            # 1. FULL URL (Only for redirect)
            full_proxy_url = f"{proxy_base}{endpoint}?d={encoded_url}{header_params}"

            if redirect_stream:
                logger.info(f"↪️ Redirecting to: {full_proxy_url}")
                return web.HTTPFound(full_proxy_url)

            # 2. CLEAN URL (For MediaFlow style JSON)
            q_params = {}
            if api_password:
                q_params['api_password'] = api_password

            response_data = {
                "destination_url": stream_url,
                "request_headers": stream_headers,
                "mediaflow_endpoint": mediaflow_endpoint,
                "mediaflow_proxy_url": f"{proxy_base}{endpoint}",
                "query_params": q_params
            }
            
            logger.info(f"✅ Extractor OK: {url} -> {stream_url[:50]}...")
            return web.json_response(response_data)

        except Exception as e:
            error_message = str(e).lower()
            # For expected errors (video not found, service unavailable), do not print traceback
            is_expected_error = any(x in error_message for x in [
                'not found', 'unavailable', '403', 'forbidden', 
                '502', 'bad gateway', 'timeout', 'temporarily unavailable'
            ])
            
            if is_expected_error:
                logger.warning(f"⚠️ Extractor request failed (expected error): {e}")
            else:
                logger.error(f"❌ Error in extractor request: {e}")
                import traceback
                traceback.print_exc()
            
            return web.Response(text=str(e), status=500)

    async def handle_license_request(self, request):
        """✅ NEW: Handles DRM license requests (ClearKey and Proxy)"""
        try:
            # 1. Static ClearKey Mode
            clearkey_param = request.query.get('clearkey')
            if clearkey_param:
                logger.info(f"🔑 Static ClearKey license request: {clearkey_param}")
                try:
                    # Support multiple keys separated by comma
                    # Format: KID1:KEY1,KID2:KEY2
                    key_pairs = clearkey_param.split(',')
                    keys_jwk = []
                    
                    # Helper to convert hex to base64url
                    def hex_to_b64url(hex_str):
                        return base64.urlsafe_b64encode(binascii.unhexlify(hex_str)).decode('utf-8').rstrip('=')

                    for pair in key_pairs:
                        if ':' in pair:
                            kid_hex, key_hex = pair.split(':')
                            keys_jwk.append({
                                "kty": "oct",
                                "k": hex_to_b64url(key_hex),
                                "kid": hex_to_b64url(kid_hex),
                                "type": "temporary"
                            })
                    
                    if not keys_jwk:
                        raise ValueError("No valid keys found")

                    jwk_response = {
                        "keys": keys_jwk,
                        "type": "temporary"
                    }
                    
                    logger.info(f"🔑 Serving static ClearKey license with {len(keys_jwk)} keys")
                    return web.json_response(jwk_response)
                except Exception as e:
                    logger.error(f"❌ Error generating static ClearKey license: {e}")
                    return web.Response(text="Invalid ClearKey format", status=400)

            # 2. License Proxy Mode
            license_url = request.query.get('url')
            if not license_url:
                return web.Response(text="Missing url parameter", status=400)

            license_url = urllib.parse.unquote(license_url)
            
            headers = {}
            for param_name, param_value in request.query.items():
                if param_name.startswith('h_'):
                    header_name = param_name[2:].replace('_', '-')
                    headers[header_name] = param_value

            # Add specific headers from the original request (e.g. content-type for the body)
            if request.headers.get('Content-Type'):
                headers['Content-Type'] = request.headers.get('Content-Type')

            # Ensure Default User-Agent exists
            if not any(k.lower() == 'user-agent' for k in headers):
                headers['User-Agent'] = DEFAULT_USER_AGENT

            # Read request body (DRM challenge)
            body = await request.read()
            
            logger.info(f"🔐 Proxying License Request to: {license_url}")
            
            proxy = random.choice(GLOBAL_PROXIES) if GLOBAL_PROXIES else None
            connector_kwargs = {}
            if proxy:
                connector_kwargs['proxy'] = proxy
            
            async with ClientSession() as session:
                async with session.request(
                    request.method, 
                    license_url, 
                    headers=headers, 
                    data=body, 
                    **connector_kwargs
                ) as resp:
                    response_body = await resp.read()
                    logger.info(f"✅ License response: {resp.status} ({len(response_body)} bytes)")
                    
                    response_headers = {
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS"
                    }
                    # Copy some useful headers from the original response
                    if 'Content-Type' in resp.headers:
                        response_headers['Content-Type'] = resp.headers['Content-Type']

                    return web.Response(
                        body=response_body,
                        status=resp.status,
                        headers=response_headers
                    )

        except Exception as e:
            logger.error(f"❌ License proxy error: {str(e)}")
            return web.Response(text=f"License error: {str(e)}", status=500)

    async def handle_key_request(self, request):
        """✅ NEW: Handles requests for AES-128 keys"""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        # 1. Static key handling (from MPD converter)
        static_key = request.query.get('static_key')
        if static_key:
            try:
                key_bytes = binascii.unhexlify(static_key)
                return web.Response(
                    body=key_bytes,
                    content_type='application/octet-stream',
                    headers={'Access-Control-Allow-Origin': '*'}
                )
            except Exception as e:
                logger.error(f"❌ Error decoding static key: {e}")
                return web.Response(text="Invalid static key", status=400)

        # 2. Remote key proxy handling
        key_url = request.query.get('key_url')
        
        if not key_url:
            return web.Response(text="Missing key_url or static_key parameter", status=400)
        
        try:
            # Decode URL if necessary
            try:
                key_url = urllib.parse.unquote(key_url)
            except:
                pass
                
            # Initialize headers exclusively from those passed dynamically
            headers = {}
            for param_name, param_value in request.query.items():
                if param_name.startswith('h_'):
                    header_name = param_name[2:].replace('_', '-')
                    # ✅ FIX: Remove Range header for key requests.
                    if header_name.lower() == 'range':
                        continue
                    headers[header_name] = param_value

            # Ensure Default User-Agent exists
            if not any(k.lower() == 'user-agent' for k in headers):
                headers['User-Agent'] = DEFAULT_USER_AGENT

            logger.info(f"🔑 Fetching AES key from: {key_url}")
            logger.info(f"   -> with headers: {headers}")
            
            # ✅ NEW: Use routing system based on TRANSPORT_ROUTES
            proxy = get_proxy_for_url(key_url, TRANSPORT_ROUTES, GLOBAL_PROXIES)
            connector_kwargs = {}
            if proxy:
                connector_kwargs['proxy'] = proxy
                logger.info(f"Using proxy {proxy} for key request.")
            
            timeout = ClientTimeout(total=30)
            async with ClientSession(timeout=timeout) as session:
                # ✅ DLHD Heartbeat: Necessary to establish the session before receiving keys
                # Use Heartbeat-Url header to detect DLHD stream (fully dynamic)
                heartbeat_url = headers.pop('Heartbeat-Url', None)  # Remove it from headers
                client_token = headers.pop('X-Client-Token', None)  # ✅ Token for heartbeat
                if heartbeat_url:
                    try:
                        
                        hb_headers = {
                            'Authorization': headers.get('Authorization', ''),
                            'X-Channel-Key': headers.get('X-Channel-Key', ''),
                            'User-Agent': headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'),
                            'Referer': headers.get('Referer', ''),
                            'Origin': headers.get('Origin', ''),
                            'X-Client-Token': client_token or '',  # ✅ Token required by the provider
                        }
                        
                        logger.info(f"💓 Pre-key heartbeat to: {heartbeat_url}")
                        async with session.get(heartbeat_url, headers=hb_headers, ssl=False, **connector_kwargs) as hb_resp:
                            hb_text = await hb_resp.text()
                            logger.info(f"💓 Heartbeat response: {hb_resp.status} - {hb_text[:100]}")
                    except Exception as hb_e:
                        logger.warning(f"⚠️ Pre-key heartbeat failed: {hb_e}")
                
                async with session.get(key_url, headers=headers, **connector_kwargs) as resp:
                    if resp.status == 200 or resp.status == 206:
                        key_data = await resp.read()
                        logger.info(f"✅ AES key fetched successfully: {len(key_data)} bytes")
                        
                        return web.Response(
                            body=key_data,
                            content_type="application/octet-stream",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Access-Control-Allow-Headers": "*",
                                "Cache-Control": "no-cache, no-store, must-revalidate"
                            }
                        )
                    else:
                        logger.error(f"❌ Key fetch failed with status: {resp.status}")
                        # --- AUTOMATIC CACHE INVALIDATION LOGIC ---
                        try:
                            url_param = request.query.get('original_channel_url')
                            if url_param:
                                extractor = await self.get_extractor(url_param, {})
                                if hasattr(extractor, 'invalidate_cache_for_url'):
                                    await extractor.invalidate_cache_for_url(url_param)
                        except Exception as cache_e:
                            logger.error(f"⚠️ Error during automatic cache invalidation: {cache_e}")
                        # --- END LOGIC ---
                        return web.Response(text=f"Key fetch failed: {resp.status}", status=resp.status)
                        
        except Exception as e:
            logger.error(f"❌ Error fetching AES key: {str(e)}")
            return web.Response(text=f"Key error: {str(e)}", status=500)

    async def handle_ts_segment(self, request):
        """Handles requests for .ts segments"""
        try:
            segment_name = request.match_info.get('segment')
            base_url = request.query.get('base_url')
            
            if not base_url:
                return web.Response(text="Base URL missing for segment", status=400)
            
            base_url = urllib.parse.unquote(base_url)
            
            if base_url.endswith('/'):
                segment_url = f"{base_url}{segment_name}"
            else:
                # ✅ FIX: If base_url is a full URL (e.g. generated by the converter), use it directly.
                if any(ext in base_url for ext in ['.mp4', '.m4s', '.ts', '.m4i', '.m4a', '.m4v']):
                    segment_url = base_url
                else:
                    segment_url = f"{base_url.rsplit('/', 1)[0]}/{segment_name}"
            
            logger.info(f"📦 Proxy Segment: {segment_name}")
            
            # Use default headers if none provided
            segment_headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": base_url
            }

            # Handles proxy response for the segment
            return await self._proxy_segment(request, segment_url, segment_headers, segment_name)
            
        except Exception as e:
            logger.error(f"Error in .ts segment proxy: {str(e)}")
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_segment(self, request, segment_url, stream_headers, segment_name):
        """✅ NEW: Dedicated proxy for .ts segments with Content-Disposition"""
        try:
            headers = dict(stream_headers)
            
            # Pass through some client headers
            for header in ['range', 'if-none-match', 'if-modified-since']:
                if header in request.headers:
                    headers[header] = request.headers[header]
            
            # Ensure Default User-Agent exists
            if not any(k.lower() == 'user-agent' for k in headers):
                headers['User-Agent'] = DEFAULT_USER_AGENT
            
            proxy = random.choice(GLOBAL_PROXIES) if GLOBAL_PROXIES else None
            connector_kwargs = {}
            if proxy:
                connector_kwargs['proxy'] = proxy
                logger.debug(f"📡 [Proxy Segment] Using proxy {proxy} for the .ts segment")

            timeout = ClientTimeout(total=60, connect=30)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(segment_url, headers=headers, **connector_kwargs) as resp:
                    response_headers = {}
                    
                    for header in ['content-type', 'content-length', 'content-range', 
                                 'accept-ranges', 'last-modified', 'etag']:
                        if header in resp.headers:
                            response_headers[header] = resp.headers[header]
                    
                    # Force content-type and add Content-Disposition for .ts
                    response_headers['Content-Type'] = 'video/MP2T'
                    response_headers['Content-Disposition'] = f'attachment; filename="{segment_name}"'
                    response_headers['Access-Control-Allow-Origin'] = '*'
                    response_headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
                    response_headers['Access-Control-Allow-Headers'] = 'Range, Content-Type'
                    
                    response = web.StreamResponse(
                        status=resp.status,
                        headers=response_headers
                    )
                    
                    await response.prepare(request)
                    
                    async for chunk in resp.content.iter_chunked(8192):
                        await response.write(chunk)
                    
                    await response.write_eof()
                    return response
                    
        except Exception as e:
            logger.error(f"Error in segment proxy: {str(e)}")
            return web.Response(text=f"Segment error: {str(e)}", status=500)

    async def _proxy_stream(self, request, stream_url, stream_headers):
        """Proxies the stream with manifest and AES-128 handling"""
        try:
            headers = dict(stream_headers)
            
            # Pass through some client headers, but FILTER those that might leak the IP
            # ✅ FIX: Strip Range and cache headers for redirectors/resolvers (like Torrentio)
            # These endpoints are not meant to handle ranges and can return 500/520 if they receive them.
            is_redirector = "/resolve/" in stream_url.lower() or "torrentio" in stream_url.lower()
            
            if not is_redirector:
                for header in ['range', 'if-none-match', 'if-modified-since']:
                    if header in request.headers:
                        headers[header] = request.headers[header]
            else:
                logger.info(f"🛡️ Stripping Range/Cache headers for suspected redirector: {stream_url}")
            
            # Explicitly remove headers that might reveal the original IP
            for h in ["x-forwarded-for", "x-real-ip", "forwarded", "via"]:
                if h in headers:
                    del headers[h]
            
            # Ensure Default User-Agent exists
            if not any(k.lower() == 'user-agent' for k in headers):
                headers['User-Agent'] = DEFAULT_USER_AGENT
            
            proxy = random.choice(GLOBAL_PROXIES) if GLOBAL_PROXIES else None
            connector_kwargs = {}
            if proxy:
                connector_kwargs['proxy'] = proxy
                logger.info(f"📡 [Proxy Stream] Using proxy {proxy} for the request to: {stream_url}")

            # ✅ FIX: Normalize critical headers (User-Agent, Referer) to Title-Case
            for key in list(headers.keys()):
                if key.lower() == 'user-agent':
                    headers['User-Agent'] = headers.pop(key)
                elif key.lower() == 'referer':
                    headers['Referer'] = headers.pop(key)
                elif key.lower() == 'origin':
                    headers['Origin'] = headers.pop(key)
                elif key.lower() == 'authorization':
                    headers['Authorization'] = headers.pop(key)
                elif key.lower() == 'cookie':
                    headers['Cookie'] = headers.pop(key)

            # ✅ FIX: Remove explicit duplicates if present (e.g. user-agent and User-Agent)
            # This can happen if GenericHLSExtractor adds 'user-agent' and we have 'User-Agent' from h_ params
            # The normalization above should have unified them, but for safety, we clean up.
            
            # Log final headers for debug
            # logger.info(f"   Final Stream Headers: {headers}")

            # ✅ NEW: Determine whether to disable SSL for this domain
            disable_ssl = get_ssl_setting_for_url(stream_url, TRANSPORT_ROUTES)

            timeout = ClientTimeout(total=60, connect=30)
            async with ClientSession(timeout=timeout) as session:
                async with session.get(stream_url, headers=headers, **connector_kwargs, ssl=not disable_ssl) as resp:
                    content_type = resp.headers.get('content-type', '')
                    
                    print(f"   Upstream Response: {resp.status} [{content_type}]")

                    # ✅ FIX: If the response is not OK, return the error directly without processing
                    if resp.status not in [200, 206]:
                        error_body = await resp.read()
                        logger.warning(f"⚠️ Upstream returned error {resp.status} for {stream_url}")
                        # ✅ DEBUG: Log error body to understand what CDN is complaining about
                        try:
                            print(f"   ❌ Error Body: {error_body.decode('utf-8')[:500]}")
                        except:
                            print(f"   ❌ Error Body (bytes): {error_body[:200]}")
                        return web.Response(
                            body=error_body,
                            status=resp.status,
                            headers={
                                'Content-Type': content_type,
                                'Access-Control-Allow-Origin': '*'
                            }
                        )
                    
                    # Special handling for HLS manifests
                    # ✅ Handles standard HLS manifests and those masked as .css (used by DLHD)
                    # For .css, check if it contains #EXTM3U (HLS signature) to detect masked manifests
                    is_hls_manifest = 'mpegurl' in content_type or stream_url.endswith('.m3u8')
                    is_css_file = stream_url.endswith('.css')
                    
                    if is_hls_manifest or is_css_file:
                        try:
                            # Read as bytes first to avoid decode crashes
                            content_bytes = await resp.read()
                            
                            try:
                                # Attempt text decoding
                                manifest_content = content_bytes.decode('utf-8')
                            except UnicodeDecodeError:
                                # IF IT FAILS: It's masked binary (e.g. .ts segment in a .css)
                                logger.warning(f"⚠️ Binary detected in {stream_url} (masked as {content_type}). Serving as binary.")
                                return web.Response(
                                    body=content_bytes,
                                    status=resp.status,
                                    headers={
                                        'Content-Type': 'video/MP2T', # Force TS if it's disguised binary
                                        'Access-Control-Allow-Origin': '*'
                                    }
                                )

                            # For .css, verify that it is indeed an HLS manifest
                            if is_css_file and not manifest_content.strip().startswith('#EXTM3U'):
                                # It's not an HLS manifest, return as normal CSS
                                return web.Response(
                                    text=manifest_content,
                                    content_type=content_type or 'text/css',
                                    headers={'Access-Control-Allow-Origin': '*'}
                                )
                        except Exception as e:
                             logger.error(f"Error processing manifest/css: {e}")
                             # Fallback to binary proxy
                             return web.Response(body=await resp.read(), status=resp.status, headers={'Access-Control-Allow-Origin': '*'})
                        
                        # ✅ FIX: Detect the correct scheme and host when behind a reverse proxy
                        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
                        host = request.headers.get('X-Forwarded-Host', request.host)
                        proxy_base = f"{scheme}://{host}"
                        original_channel_url = request.query.get('url', '')
                        
                        api_password = request.query.get('api_password')
                        no_bypass = request.query.get('no_bypass') == '1'
                        rewritten_manifest = await ManifestRewriter.rewrite_manifest_urls(
                            manifest_content, stream_url, proxy_base, headers, original_channel_url, api_password, self.get_extractor, no_bypass
                        )
                        
                        return web.Response(
                            text=rewritten_manifest,
                            headers={
                                'Content-Type': 'application/vnd.apple.mpegurl',
                                'Content-Disposition': 'attachment; filename="stream.m3u8"',
                                'Access-Control-Allow-Origin': '*',
                                'Cache-Control': 'no-cache'
                            }
                        )
                    
                    # ✅ UPDATED: Handling for MPD (DASH) manifests
                    elif 'dash+xml' in content_type or stream_url.endswith('.mpd'):
                        manifest_content = await resp.text()
                        
                        # ✅ FIX: Detect the correct scheme and host when behind a reverse proxy
                        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
                        host = request.headers.get('X-Forwarded-Host', request.host)
                        proxy_base = f"{scheme}://{host}"
                        
                        # Retrieve parameters
                        clearkey_param = request.query.get('clearkey')
                        
                        # ✅ FIX: Support for separate key_id and key (MediaFlowProxy style)
                        if not clearkey_param:
                            key_id_param = request.query.get('key_id')
                            key_val_param = request.query.get('key')
                            
                            if key_id_param and key_val_param:
                                # Check for multiple keys
                                key_ids = key_id_param.split(',')
                                key_vals = key_val_param.split(',')
                                
                                if len(key_ids) == len(key_vals):
                                    clearkey_parts = []
                                    for kid, kval in zip(key_ids, key_vals):
                                        clearkey_parts.append(f"{kid.strip()}:{kval.strip()}")
                                    clearkey_param = ",".join(clearkey_parts)
                                else:
                                    if len(key_ids) == 1 and len(key_vals) == 1:
                                         clearkey_param = f"{key_id_param}:{key_val_param}"
                                    else:
                                         # Try to pair as many as possible
                                         min_len = min(len(key_ids), len(key_vals))
                                         clearkey_parts = []
                                         for i in range(min_len):
                                             clearkey_parts.append(f"{key_ids[i].strip()}:{key_vals[i].strip()}")
                                         clearkey_param = ",".join(clearkey_parts)

                        # --- LEGACY MODE: MPD -> HLS Conversion ---
                        if MPD_MODE == "legacy" and MPDToHLSConverter:
                            logger.info(f"🔄 [Legacy Mode] Converting MPD to HLS for {stream_url}")
                            try:
                                converter = MPDToHLSConverter()
                                
                                # Check if requesting a Media Playlist (Variant)
                                rep_id = request.query.get('rep_id')
                                
                                if rep_id:
                                    # Generate Media Playlist (Segments)
                                    hls_playlist = converter.convert_media_playlist(
                                        manifest_content, rep_id, proxy_base, stream_url, request.query_string, clearkey_param
                                    )
                                    # Log first few lines for debugging
                                    logger.info(f"📜 Generated Media Playlist for {rep_id} (first 10 lines):\n{chr(10).join(hls_playlist.splitlines()[:10])}")
                                else:
                                    # Generate Master Playlist
                                    hls_playlist = converter.convert_master_playlist(
                                        manifest_content, proxy_base, stream_url, request.query_string
                                    )
                                    logger.info(f"📜 Generated Master Playlist (first 5 lines):\n{chr(10).join(hls_playlist.splitlines()[:5])}")
                                
                                return web.Response(
                                    text=hls_playlist,
                                    headers={
                                        'Content-Type': 'application/vnd.apple.mpegurl',
                                        'Content-Disposition': 'attachment; filename="stream.m3u8"',
                                        'Access-Control-Allow-Origin': '*',
                                        'Cache-Control': 'no-cache'
                                    }
                                )
                            except Exception as e:
                                logger.error(f"❌ Legacy conversion failed: {e}")
                                # Fallback to DASH proxy if conversion fails
                                pass

                        # --- DEFAULT: DASH Proxy (Rewriting) ---
                        req_format = request.query.get('format')
                        rep_id = request.query.get('rep_id')

                        api_password = request.query.get('api_password')
                        rewritten_manifest = ManifestRewriter.rewrite_mpd_manifest(manifest_content, stream_url, proxy_base, headers, clearkey_param, api_password)
                        
                        return web.Response(
                            text=rewritten_manifest,
                            headers={
                                'Content-Type': 'application/dash+xml',
                                'Content-Disposition': 'attachment; filename="stream.mpd"',
                                'Access-Control-Allow-Origin': '*',
                                'Cache-Control': 'no-cache'
                            })
                    
                    # Normal streaming for other content types
                    response_headers = {}
                    
                    for header in ['content-type', 'content-length', 'content-range', 
                                 'accept-ranges', 'last-modified', 'etag']:
                        if header in resp.headers:
                            response_headers[header] = resp.headers[header]
                    
                    # ✅ FIX: Force Content-Type for .ts segments if the server does not send it correctly
                    if (stream_url.endswith('.ts') or request.path.endswith('.ts')) and 'video/mp2t' not in response_headers.get('content-type', '').lower():
                        response_headers['Content-Type'] = 'video/MP2T'

                    response_headers['Access-Control-Allow-Origin'] = '*'
                    response_headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
                    response_headers['Access-Control-Allow-Headers'] = 'Range, Content-Type'
                    
                    response = web.StreamResponse(
                        status=resp.status,
                        headers=response_headers
                    )
                    
                    await response.prepare(request)
                    
                    async for chunk in resp.content.iter_chunked(8192):
                        await response.write(chunk)
                    
                    await response.write_eof()
                    return response
                    
        except (ClientPayloadError, ConnectionResetError, OSError) as e:
            # Typical client disconnection errors
            logger.info(f"ℹ️ Client disconnected from stream: {stream_url} ({str(e)})")
            return web.Response(text="Client disconnected", status=499)
            
        except (ServerDisconnectedError, ClientConnectionError, asyncio.TimeoutError) as e:
            # Upstream connection errors
            logger.warning(f"⚠️ Connection lost with source: {stream_url} ({str(e)})")
            return web.Response(text=f"Upstream connection lost: {str(e)}", status=502)

        except Exception as e:
            logger.error(f"❌ Generic error in stream proxy: {str(e)}")
            return web.Response(text=f"Stream error: {str(e)}", status=500)

    async def handle_playlist_request(self, request):
        """Handles requests for the playlist builder"""
        if not self.playlist_builder:
            return web.Response(text="❌ Playlist Builder not available - missing module", status=503)
            
        try:
            url_param = request.query.get('url')
            
            if not url_param:
                return web.Response(text="Missing 'url' parameter", status=400)
            
            if not url_param.strip():
                return web.Response(text="Parameter 'url' cannot be empty", status=400)
            
            playlist_definitions = [def_.strip() for def_ in url_param.split(';') if def_.strip()]
            if not playlist_definitions:
                return web.Response(text="No playlist definitions provided", status=400) # Added missing return
            # ✅ FIX: Detect correct scheme and host when behind a reverse proxy
            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
            host = request.headers.get('X-Forwarded-Host', request.host)
            base_url = f"{scheme}://{host}"
            
            # ✅ FIX: Pass api_password to the builder if present
            api_password = request.query.get('api_password')
            
            async def generate_response():
                async for line in self.playlist_builder.async_generate_combined_playlist(
                    playlist_definitions, base_url, api_password=api_password
                ):
                    yield line.encode('utf-8')
            
            response = web.StreamResponse(
                status=200,
                headers={
                    'Content-Type': 'application/vnd.apple.mpegurl',
                    'Content-Disposition': 'attachment; filename="playlist.m3u"',
                    'Access-Control-Allow-Origin': '*'
                }
            )
            
            await response.prepare(request)
            
            async for chunk in generate_response():
                await response.write(chunk)
            
            await response.write_eof()
            return response
            
        except Exception as e:
            logger.error(f"❌ General error in playlist handler: {str(e)}")
            return web.Response(text=f"Error: {str(e)}", status=500)

    def _read_template(self, filename: str) -> str:
        """Helper function to read a template file."""
        # Note: assumes templates are in the 'templates' directory in the project root
        # Since we are in services/, we need to go up one level
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template_path = os.path.join(base_dir, 'templates', filename)
        with open(template_path, 'r', encoding='utf-8') as f:
            return f.read()

    async def handle_root(self, request):
        """Serves the main index.html page."""
        try:
            html_content = self._read_template('index.html')
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            logger.error(f"❌ Critical error: unable to load 'index.html': {e}")
            return web.Response(text="<h1>Error 500</h1><p>Page not found.</p>", status=500, content_type='text/html')

    async def handle_builder(self, request):
        """Handles the web interface of the playlist builder."""
        try:
            html_content = self._read_template('builder.html')
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            logger.error(f"❌ Critical error: unable to load 'builder.html': {e}")
            return web.Response(text="<h1>Error 500</h1><p>Unable to load builder interface.</p>", status=500, content_type='text/html')

    async def handle_info_page(self, request):
        """Serves the HTML info page."""
        try:
            html_content = self._read_template('info.html')
            return web.Response(text=html_content, content_type='text/html')
        except Exception as e:
            logger.error(f"❌ Critical error: unable to load 'info.html': {e}")
            return web.Response(text="<h1>Error 500</h1><p>Unable to load info page.</p>", status=500, content_type='text/html')

    async def handle_favicon(self, request):
        """Serves the favicon.ico file."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        favicon_path = os.path.join(base_dir, 'static', 'favicon.ico')
        if os.path.exists(favicon_path):
            return web.FileResponse(favicon_path)
        return web.Response(status=404)

    async def handle_options(self, request):
        """Handles OPTIONS requests for CORS"""
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
            'Access-Control-Allow-Headers': 'Range, Content-Type',
            'Access-Control-Max-Age': '86400'
        }
        return web.Response(headers=headers)

    async def handle_api_info(self, request):
        """API endpoint that returns server information in JSON format."""
        info = {
            "proxy": "HLS Proxy Server",
            "version": "2.5.0",  # Updated for AES-128 support
            "status": "✅ Working",
            "features": [
                "✅ Proxy HLS streams",
                "✅ AES-128 key proxying",  # ✅ NEW
                "✅ Playlist building",
                "✅ Proxy Support (SOCKS5, HTTP/S)",
                "✅ Multi-extractor support",
                "✅ CORS enabled"
            ],
            "extractors_loaded": list(self.extractors.keys()),
            "modules": {
                "playlist_builder": PlaylistBuilder is not None,
                "vavoo_extractor": VavooExtractor is not None,
                "dlhd_extractor": DLHDExtractor is not None,
                "vixsrc_extractor": VixSrcExtractor is not None,
                "sportsonline_extractor": SportsonlineExtractor is not None,
                "mixdrop_extractor": MixdropExtractor is not None,
                "voe_extractor": VoeExtractor is not None,
                "streamtape_extractor": StreamtapeExtractor is not None,
            },
            "proxy_config": {
                "global_proxies": f"{len(GLOBAL_PROXIES)} proxies loaded",
                "transport_routes": f"{len(TRANSPORT_ROUTES)} routing rules configured",
                "routes": [{"url": route['url'], "has_proxy": route['proxy'] is not None} for route in TRANSPORT_ROUTES]
            },
            "endpoints": {
                "/proxy/hls/manifest.m3u8": "Proxy HLS (MFP compatibility) - ?d=<URL>",
                "/proxy/mpd/manifest.m3u8": "Proxy MPD (MFP compatibility) - ?d=<URL>",
                "/proxy/manifest.m3u8": "Legacy Proxy - ?url=<URL>",
                "/key": "AES-128 key proxy - ?key_url=<URL>",  # ✅ NEW
                "/playlist": "Playlist builder - ?url=<definitions>",
                "/builder": "Web interface for playlist builder",
                "/segment/{segment}": "Proxy for .ts segments - ?base_url=<URL>",
                "/license": "DRM license proxy (ClearKey/Widevine) - ?url=<URL> or ?clearkey=<id:key>",
                "/info": "HTML page with server information",
                "/api/info": "JSON endpoint with server information"
            },
            "usage_examples": {
                "proxy_hls": "/proxy/hls/manifest.m3u8?d=https://example.com/stream.m3u8",
                "proxy_mpd": "/proxy/mpd/manifest.m3u8?d=https://example.com/stream.mpd",
                "aes_key": "/key?key_url=https://server.com/key.bin",  # ✅ NEW
                "playlist": "/playlist?url=http://example.com/playlist1.m3u8;http://example.com/playlist2.m3u8",
                "custom_headers": "/proxy/hls/manifest.m3u8?d=<URL>&h_Authorization=Bearer%20token"
            }
        }
        return web.json_response(info)

    def _prefetch_next_segments(self, current_url, init_url, key, key_id, headers):
        """Identifies the next segments and starts background download."""
        try:
            parsed = urllib.parse.urlparse(current_url)
            path = parsed.path
            
            # Look for numeric pattern at the end of the path (e.g. segment-1.m4s)
            match = re.search(r'([-_])(\d+)(\.[^.]+)$', path)
            if not match:
                return

            separator, current_number, extension = match.groups()
            current_num = int(current_number)

            # Prefetch next 3 segments
            for i in range(1, 4):
                next_num = current_num + i
                
                # Replace number in path
                pattern = f"{separator}{current_number}{re.escape(extension)}$"
                replacement = f"{separator}{next_num}{extension}"
                new_path = re.sub(pattern, replacement, path)
                
                # Reconstruct URL
                next_url = urllib.parse.urlunparse(parsed._replace(path=new_path))
                
                cache_key = f"{next_url}:{key_id}"
                
                if (cache_key not in self.segment_cache and 
                    cache_key not in self.prefetch_tasks):
                    
                    self.prefetch_tasks.add(cache_key)
                    asyncio.create_task(
                        self._fetch_and_cache_segment(next_url, init_url, key, key_id, headers, cache_key)
                    )

        except Exception as e:
            logger.warning(f"⚠️ Prefetch error: {e}")

    async def _fetch_and_cache_segment(self, url, init_url, key, key_id, headers, cache_key):
        """Downloads, decrypts, and caches a segment in the background."""
        try:
            if decrypt_segment is None:
                return

            session = await self._get_session()
            
            # Download Init (use cache if possible)
            init_content = b""
            if init_url:
                if init_url in self.init_cache:
                    init_content = self.init_cache[init_url]
                else:
                    disable_ssl = get_ssl_setting_for_url(init_url, TRANSPORT_ROUTES)
                    try:
                        async with session.get(init_url, headers=headers, ssl=not disable_ssl, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                init_content = await resp.read()
                                self.init_cache[init_url] = init_content
                    except Exception:
                        pass 

            # Download Segment
            segment_content = None
            disable_ssl = get_ssl_setting_for_url(url, TRANSPORT_ROUTES)
            try:
                async with session.get(url, headers=headers, ssl=not disable_ssl, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.read()
            except Exception:
                pass

            if segment_content:
                # Decrypt
                # Decrypt in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
                decrypted_content = await loop.run_in_executor(None, decrypt_segment, init_content, segment_content, key_id, key)
                import time
                self.segment_cache[cache_key] = (decrypted_content, time.time())
                logger.info(f"📦 Prefetched segment: {url.split('/')[-1]}")

        except Exception as e:
            pass
        finally:
            if cache_key in self.prefetch_tasks:
                self.prefetch_tasks.remove(cache_key)

    async def _remux_to_ts(self, content):
        """Converts segments (fMP4) to MPEG-TS using FFmpeg pipe."""
        try:
            cmd = [
                'ffmpeg',
                '-y',
                '-i', 'pipe:0',
                '-c', 'copy',
                '-copyts',                      # Preserve timestamps to prevent freezing/gap issues
                '-bsf:v', 'h264_mp4toannexb',   # Ensure video is Annex B (MPEG-TS requirement)
                '-bsf:a', 'aac_adtstoasc',      # Ensure audio is ADTS (MPEG-TS requirement)
                '-f', 'mpegts',
                'pipe:1'
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await proc.communicate(input=content)
            
            # Check for data presence regardless of return code (workaround for asyncio race condition on some platforms)
            if len(stdout) > 0:
                if proc.returncode != 0:
                    logger.debug(f"FFmpeg remux finished with code {proc.returncode} but produced output (ignoring). Stderr: {stderr.decode()[:200]}")
                return stdout
            
            if proc.returncode != 0:
                logger.error(f"❌ FFmpeg remux failed: {stderr.decode()}")
                return None
                
            return stdout
        except Exception as e:
            logger.error(f"❌ Remux error: {e}")
            return None

    async def handle_decrypt_segment(self, request):
        """Decrypts fMP4 segments server-side for ClearKey (legacy mode)."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        url = request.query.get('url')
        logger.info(f"🔓 Decrypt Request: {url.split('/')[-1] if url else 'unknown'}")

        init_url = request.query.get('init_url')
        key = request.query.get('key')
        key_id = request.query.get('key_id')
        
        if not url or not key or not key_id:
            return web.Response(text="Missing url, key, or key_id", status=400)

        if decrypt_segment is None:
            return web.Response(text="Decrypt not available (MPD_MODE is not legacy)", status=503)

        # Check cache first
        import time
        cache_key = f"{url}:{key_id}:ts" # Use distinct cache key for TS
        if cache_key in self.segment_cache:
            cached_content, cached_time = self.segment_cache[cache_key]
            if time.time() - cached_time < self.segment_cache_ttl:
                logger.info(f"📦 Cache HIT for segment: {url.split('/')[-1]}")
                return web.Response(
                    body=cached_content,
                    status=200,
                    headers={
                        'Content-Type': 'video/MP2T',
                        'Access-Control-Allow-Origin': '*',
                        'Cache-Control': 'no-cache',
                        'Connection': 'keep-alive'
                    }
                )
            else:
                del self.segment_cache[cache_key]

        try:
            # Reconstruct headers for upstream requests
            headers = {
                'Connection': 'keep-alive',
                'Accept-Encoding': 'identity'
            }
            for param_name, param_value in request.query.items():
                if param_name.startswith('h_'):
                    header_name = param_name[2:].replace('_', '-')
                    headers[header_name] = param_value

            # Get proxy-enabled session for segment fetches
            segment_session, should_close = await self._get_proxy_session(url)

            try:
                # Parallel download of init and media segment
                async def fetch_init():
                    if not init_url:
                        return b""
                    if init_url in self.init_cache:
                        return self.init_cache[init_url]
                    disable_ssl = get_ssl_setting_for_url(init_url, TRANSPORT_ROUTES)
                    try:
                        async with segment_session.get(init_url, headers=headers, ssl=not disable_ssl, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                self.init_cache[init_url] = content
                                return content
                            logger.error(f"❌ Init segment returned status {resp.status}: {init_url}")
                            return None
                    except Exception as e:
                        logger.error(f"❌ Failed to fetch init segment: {e}")
                        return None

                async def fetch_segment():
                    disable_ssl = get_ssl_setting_for_url(url, TRANSPORT_ROUTES)
                    try:
                        async with segment_session.get(url, headers=headers, ssl=not disable_ssl, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                return await resp.read()
                            logger.error(f"❌ Segment returned status {resp.status}: {url}")
                            return None
                    except Exception as e:
                        logger.error(f"❌ Failed to fetch segment: {e}")
                        return None

                # Parallel fetch
                init_content, segment_content = await asyncio.gather(fetch_init(), fetch_segment())
            finally:
                # Close the session if we created one for proxy
                if should_close and segment_session and not segment_session.closed:
                    await segment_session.close()
            
            if init_content is None and init_url:
                logger.error(f"❌ Failed to fetch init segment")
                return web.Response(status=502)
            if segment_content is None:
                logger.error(f"❌ Failed to fetch segment")
                return web.Response(status=502)

            init_content = init_content or b""

            # Check if we should skip decryption (null key case)
            skip_decrypt = request.query.get('skip_decrypt') == '1'
            
            if skip_decrypt:
                # Null key: just concatenate init + segment without decryption
                logger.info(f"🔓 Skip decrypt mode - remuxing without decryption")
                combined_content = init_content + segment_content
            else:
                # Decrypt with PyCryptodome
                # Decrypt in thread pool to avoid blocking event loop
                loop = asyncio.get_event_loop()
                combined_content = await loop.run_in_executor(None, decrypt_segment, init_content, segment_content, key_id, key)

            # Lightweight REMUX to TS
            ts_content = await self._remux_to_ts(combined_content)
            if not ts_content:
                 logger.warning("⚠️ Remux failed, serving raw fMP4")
                 # Fallback: serve fMP4 if remux fails
                 ts_content = combined_content
                 content_type = 'video/mp4'
            else:
                 content_type = 'video/MP2T'
                 logger.info("⚡ Remuxed fMP4 -> TS")

            # Store in cache
            self.segment_cache[cache_key] = (ts_content, time.time())
            
            # Clean old cache entries (keep max 50)
            if len(self.segment_cache) > 50:
                oldest_keys = sorted(self.segment_cache.keys(), key=lambda k: self.segment_cache[k][1])[:20]
                for k in oldest_keys:
                    del self.segment_cache[k]

            # Prefetch next segments in background
            self._prefetch_next_segments(url, init_url, key, key_id, headers)

            # Send Response
            return web.Response(
                body=ts_content,
                status=200,
                headers={
                    'Content-Type': content_type,
                    'Access-Control-Allow-Origin': '*',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive'
                }
            )

        except Exception as e:
            logger.error(f"❌ Decryption error: {e}")
            return web.Response(status=500, text=f"Decryption failed: {str(e)}")

    async def handle_generate_urls(self, request):
        """
        Endpoint compatible with MediaFlow-Proxy to generate proxy URLs.
        Supports POST requests from ilCorsaroViola.
        """
        try:
            data = await request.json()
            
            # Verify password if present in body (ilCorsaroViola sends it here)
            req_password = data.get('api_password')
            if API_PASSWORD and req_password != API_PASSWORD:
                 # Fallback: check standard auth methods if body auth fails or is missing
                 if not check_password(request):
                    logger.warning("⛔ Unauthorized generate_urls request")
                    return web.Response(status=401, text="Unauthorized: Invalid API Password")

            urls_to_process = data.get('urls', [])
            
            # --- REQUESTED LOGGING ---
            client_ip = request.remote
            exit_strategy = "Server IP (Direct)"
            if GLOBAL_PROXIES:
                exit_strategy = f"Random Global Proxy ({len(GLOBAL_PROXIES)} proxy pool)"
            
            logger.info(f"🔄 [Generate URLs] Request from Client IP: {client_ip}")
            logger.info(f"    -> Predicted exit strategy for stream: {exit_strategy}")
            if urls_to_process:
                logger.info(f"    -> Generating {len(urls_to_process)} proxy URLs for destination: {urls_to_process[0].get('destination_url', 'N/A')}")
            # -------------------------

            generated_urls = []
            
            # Determine proxy base URL
            scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
            host = request.headers.get('X-Forwarded-Host', request.host)
            proxy_base = f"{scheme}://{host}"

            for item in urls_to_process:
                dest_url = item.get('destination_url')
                if not dest_url:
                    continue
                    
                endpoint = item.get('endpoint', '/proxy/stream')
                req_headers = item.get('request_headers', {})
                
                # Build query params
                encoded_url = urllib.parse.quote(dest_url, safe='')
                params = [f"d={encoded_url}"]
                
                # Add headers as h_ params
                for key, value in req_headers.items():
                    params.append(f"h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}")
                
                # Add password if necessary
                if API_PASSWORD:
                    params.append(f"api_password={API_PASSWORD}")
                
                # Build final URL
                query_string = "&".join(params)
                
                # Ensure the endpoint starts with /
                if not endpoint.startswith('/'):
                    endpoint = '/' + endpoint
                
                full_url = f"{proxy_base}{endpoint}?{query_string}"
                generated_urls.append(full_url)

            return web.json_response({"urls": generated_urls})

        except Exception as e:
            logger.error(f"❌ Error generating URLs: {e}")
            return web.Response(text=str(e), status=500)

    async def handle_proxy_ip(self, request):
        """Returns the public IP address of the server (or the proxy if configured)."""
        if not check_password(request):
            return web.Response(status=401, text="Unauthorized: Invalid API Password")

        try:
            # Use a global proxy if configured, otherwise direct connection
            proxy = random.choice(GLOBAL_PROXIES) if GLOBAL_PROXIES else None
            
            # Create a dedicated session with the configured proxy
            if proxy:
                logger.info(f"🌍 Checking IP via proxy: {proxy}")
                connector = ProxyConnector.from_url(proxy)
            else:
                connector = TCPConnector()
            
            timeout = ClientTimeout(total=10)
            async with ClientSession(timeout=timeout, connector=connector) as session:
                # Use an external service to determine the public IP
                async with session.get('https://api.ipify.org?format=json', headers={"User-Agent": DEFAULT_USER_AGENT}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return web.json_response(data)
                    else:
                        logger.error(f"❌ Failed to fetch IP: {resp.status}")
                        return web.Response(text="Failed to fetch IP", status=502)
                    
        except Exception as e:
            logger.error(f"❌ Error fetching IP: {e}")
            return web.Response(text=str(e), status=500)

    async def cleanup(self):
        """Resource cleanup"""
        try:
            if self.session and not self.session.closed:
                await self.session.close()
            
            # Close all cached proxy sessions
            for proxy_url, session in list(self.proxy_sessions.items()):
                if session and not session.closed:
                    await session.close()
            self.proxy_sessions.clear()
                
            for extractor in self.extractors.values():
                if hasattr(extractor, 'close'):
                    await extractor.close()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
