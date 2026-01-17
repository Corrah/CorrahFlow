import asyncio
import random
import logging
import ssl
import urllib.parse
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from aiohttp_socks import ProxyConnector

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    """Eccezione personalizzata per errori di estrazione"""
    pass

class GenericHLSExtractor:
    def __init__(self, request_headers, proxies=None):
        self.request_headers = request_headers
        self.base_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "accept": "*/*",
            "accept-language": "it,en-US;q=0.9,en;q=0.8"
        }
        self.session = None
        self.proxies = proxies or []

    def _get_random_proxy(self):
        """Restituisce un proxy casuale dalla lista."""
        return random.choice(self.proxies) if self.proxies else None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            proxy = self._get_random_proxy()
            if proxy:
                logging.info(f"Utilizzo del proxy {proxy} per la sessione generica.")
                connector = ProxyConnector.from_url(proxy)
            else:
                # Create SSL context that doesn't verify certificates
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                
                connector = TCPConnector(
                    limit=0, limit_per_host=0, 
                    keepalive_timeout=60, enable_cleanup_closed=True, 
                    force_close=False, use_dns_cache=True,
                    ssl=ssl_context
                )

            timeout = ClientTimeout(total=60, connect=30, sock_read=30)
            self.session = ClientSession(
                timeout=timeout, connector=connector, 
                headers={'user-agent': self.base_headers['user-agent']}
            )
        return self.session

    async def extract(self, url, **kwargs):
        # ✅ AGGIORNATO: Rimossa validazione estensioni su richiesta utente.
        # Accetta qualsiasi URL per evitare errori con segmenti mascherati.
        # if not any(pattern in url.lower() for pattern in ['.m3u8', '.mpd', '.ts', '.js', '.css', '.html', '.txt', 'vixsrc.to/playlist', 'newkso.ru']):
        #     raise ExtractorError("URL non supportato (richiesto .m3u8, .mpd, .ts, .js, .css, .html, .txt, URL VixSrc o URL newkso.ru valido)")

        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        headers = self.base_headers.copy()
        
        # ✅ FIX: Avoid circular referer/origin (referer == domain) which can be blocked.
        # Only add referer/origin if they are not already present in request_headers.
        if "/resolve/" not in url.lower() and "torrentio" not in url.lower():
            if not any(k.lower() == 'referer' for k in self.request_headers):
                headers["referer"] = origin
            if not any(k.lower() == 'origin' for k in self.request_headers):
                headers["origin"] = origin
        else:
            # For Torrentio/Redirectors, use a standard Stremio referer if none provided
            if not any(k.lower() == 'referer' for k in self.request_headers):
                headers["referer"] = "https://strem.io/"
            logger.debug(f"ℹ️ Redirector detected ({urlparse(url).netloc}), using optimized headers.")

        # ✅ FIX: Ripristinata logica conservativa. Non inoltrare tutti gli header del client
        # per evitare conflitti (es. Host, Cookie, Accept-Encoding) con il server di destinazione.
        # Gli header necessari (Referer, User-Agent) vengono gestiti tramite i parametri h_.
        # ✅ FIX: Prevent IP Leakage. Explicitly filter out X-Forwarded-For and similar headers.
        # Only allow specific headers that are safe or necessary for authentication.
        for h, v in self.request_headers.items():
            h_lower = h.lower()
            # ✅ FIX: Only pass Referer/Origin if they match the destination or were forced via h_ params
            # BUT for Torrentio, we already set a default above.
            if h_lower == "user-agent":
                if "chrome" in v.lower() or "applewebkit" in v.lower():
                    headers["user-agent"] = v
                continue
            
            # Filter Referer/Origin: Only keep if they don't look like leakage from unrelated streams
            if h_lower in ["referer", "origin"]:
                # If it's a Sky Sport link in a Torrentio request, IGNORE IT
                if "torrentio" in url.lower() and ("pcdn" in v.lower() or "cssott" in v.lower()):
                    continue
                headers[h] = v
                
            if h_lower in ["authorization", "x-api-key", "x-auth-token", "cookie", "x-channel-key"]:
                headers[h] = v
                
            if h_lower in ["x-forwarded-for", "x-real-ip", "forwarded", "via"]:
                continue

        # --- MANUAL REDIRECT RESOLUTION FOR REDIRECTORS (e.g. Torrentio) ---
        # Resolve the final URL now to avoid passing problematic headers (like Range)
        # to the resolution script, which often causes 500/520 errors.
        if "/resolve/" in url.lower() or "torrentio" in url.lower():
            try:
                session = await self._get_session()
                # Use clean browser headers for resolution, WITHOUT client-specific headers (Range, etc.)
                resolution_headers = {
                    "User-Agent": headers.get("user-agent", self.base_headers["user-agent"]),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Referer": "https://strem.io/"
                }
                
                # ✅ FIX: Properly encode URL if it contains spaces or special characters
                safe_url = url
                if ' ' in url:
                    # Parse and re-quote path parts to handle spaces correctly
                    parsed = urllib.parse.urlparse(url)
                    clean_path = urllib.parse.quote(parsed.path)
                    safe_url = urllib.parse.urlunparse(parsed._replace(path=clean_path))
                
                # ✅ FIX: Try to use a session WITHOUT proxy for resolution if possible, 
                # as Torrentio/Cloudflare often blocks public datacenter proxies.
                resolution_session = session
                if self.proxies:
                    # Create a temporary direct session for resolution
                    resolution_session = ClientSession(timeout=ClientTimeout(total=20), connector=TCPConnector(ssl=False))
                
                try:
                    logger.info(f"🔗 Resolving redirect for suspected redirector: {safe_url}")
                    async with resolution_session.get(safe_url, headers=resolution_headers, allow_redirects=False, timeout=ClientTimeout(total=20), ssl=False) as resp:
                        if resp.status in [301, 302, 303, 307, 308] and 'Location' in resp.headers:
                            redirected_url = resp.headers['Location']
                            if not redirected_url.startswith('http'):
                                redirected_url = urllib.parse.urljoin(safe_url, redirected_url)
                                
                            logger.info(f"✅ Resolved to final URL: {redirected_url[:100]}...")
                            # Use ORIGINAL headers for the final stream, but safe UA/Referer
                            return {
                                "destination_url": redirected_url,
                                "request_headers": headers,
                                "mediaflow_endpoint": "hls_proxy"
                            }
                        else:
                            logger.warning(f"⚠️ Resolution returned status {resp.status} (Expected 3xx). Headers: {dict(resp.headers)}")
                finally:
                    # Clean up temporary session if created
                    if resolution_session is not session:
                        await resolution_session.close()
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ Timeout (20s) resolving redirect for: {url}")
            except Exception as e:
                logger.warning(f"⚠️ Error resolving redirect ({type(e).__name__}): {e}")

        return {
            "destination_url": url, 
            "request_headers": headers, 
            "mediaflow_endpoint": "hls_proxy"
        }

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
