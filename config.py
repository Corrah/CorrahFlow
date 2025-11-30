import os
import logging
from dotenv import load_dotenv

load_dotenv() # Carica le variabili dal file .env

# Configurazione logging
# ✅ CORREZIONE: Imposta un formato standard e assicurati che il logger 'aiohttp.access'
# non venga silenziato, permettendo la visualizzazione dei log di accesso.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

# Silenzia i log di accesso di aiohttp a meno che non siano errori
# logging.getLogger('aiohttp.access').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Configurazione Proxy ---
def parse_proxies(proxy_env_var: str) -> list:
    """Analizza una stringa di proxy separati da virgola da una variabile d'ambiente."""
    proxies_str = os.environ.get(proxy_env_var, "").strip()
    if proxies_str:
        return [p.strip() for p in proxies_str.split(',') if p.strip()]
    return []

GLOBAL_PROXIES = parse_proxies('GLOBAL_PROXY')
VAVOO_PROXIES = parse_proxies('VAVOO_PROXY')
DLHD_PROXIES = parse_proxies('DLHD_PROXY')

if GLOBAL_PROXIES: logging.info(f"🌍 Caricati {len(GLOBAL_PROXIES)} proxy globali.")
if VAVOO_PROXIES: logging.info(f"🎬 Caricati {len(VAVOO_PROXIES)} proxy Vavoo.")
if DLHD_PROXIES: logging.info(f"📺 Caricati {len(DLHD_PROXIES)} proxy DLHD.")

API_PASSWORD = os.environ.get("API_PASSWORD")
PORT = int(os.environ.get("PORT", 7860))

def check_password(request):
    """Verifica la password API se impostata."""
    if not API_PASSWORD:
        return True
    
    # Check query param
    api_password_param = request.query.get("api_password")
    if api_password_param == API_PASSWORD:
        return True
        
    # Check header
    if request.headers.get("x-api-password") == API_PASSWORD:
        return True
        
    return False
