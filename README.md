# 🚀 EasyProxy - Server Proxy Universale per Streaming HLS

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://docker.com)
[![HLS](https://img.shields.io/badge/HLS-Streaming-red.svg)](https://developer.apple.com/streaming/)

> **Un server proxy universale per streaming HLS, M3U8 e IPTV** 🎬  
> Supporto nativo per Vavoo, DaddyLive HD, Sportsonline e molti altri.  
> Interfaccia web integrata, Playlist Builder avanzato e configurazione zero.

---

## 📚 Indice

- [✨ Caratteristiche Principali](#-caratteristiche-principali)
- [💾 Setup Rapido](#-setup-rapido)
- [☁️ Deploy Cloud](#️-deploy-cloud)
- [💻 Installazione Locale](#-installazione-locale)
- [⚙️ Configurazione Proxy](#️-configurazione-proxy)
- [🧰 Utilizzo del Proxy](#-utilizzo-del-proxy)
- [🔗 Playlist Builder](#-playlist-builder)
- [🔧 Configurazione Avanzata](#-configurazione-avanzata)
- [📖 Architettura](#-architettura)

---

## ✨ Caratteristiche Principali

| 🎯 **Proxy Universale** | 🔐 **Estrattori Specializzati** | ⚡ **Performance** |
|------------------------|------------------------|-------------------|
| HLS, M3U8, MPD, DLHD streams | Vavoo, DLHD, Sportsonline, VixSrc | Connessioni async e keep-alive |
| **🔓 DRM Decryption** | **🎬 MPD to HLS** | **🔑 ClearKey Support** |
| CENC decryption con PyCryptodome | Conversione automatica DASH → HLS | Server-side ClearKey per VLC |

| 🌐 **Multi-formato** | 🛡️ **Anti-Bot System** | 🚀 **Scalabilità** |
|--------------------|-------------------|------------------|
| Supporto #EXTVLCOPT e #EXTHTTP | Sessioni persistenti, Cookie Jar | Server asincrono |

| 🛠️ **Builder Integrato** | 📱 **Interfaccia Web** | 🔗 **Playlist Manager** |
|--------------------------|----------------------|---------------------|
| Combinazione playlist, Sort A-Z | Dashboard completa | Gestione automatica headers |

### 📋 Servizi Supportati
- **Vavoo**: Risoluzione automatica link `vavoo.to` con generazione firma.
- **DaddyLiveHD (DLHD)**: Bypass anti-bot avanzato, supporto `newkso.ru` e `lovecdn.ru`.
- **Sportsonline**: Estrazione automatica da iframe e decodifica stream.
- **VixSrc**: Supporto streaming VOD.
- **Mixdrop, Streamtape, VOE**: Estrazione link diretti dai file hoster.
- **MPD (DASH)**: Conversione on-the-fly in HLS per massima compatibilità.

---

## 💾 Setup Rapido

### 🐳 Docker (Raccomandato)

**Assicurati di avere un file `Dockerfile` e `requirements.txt` nella root del progetto.**

```bash
git clone https://github.com/nzo66/EasyProxy.git
cd EasyProxy
docker build -t EasyProxy .
docker run -d -p 7860:7860 --name EasyProxy EasyProxy
```

### 🐍 Python Diretto

```bash
git clone https://github.com/nzo66/EasyProxy.git
cd EasyProxy
pip install -r requirements.txt
gunicorn --bind 0.0.0.0:7860 --workers 4 --worker-class aiohttp.worker.GunicornWebWorker app:app
```

**Server disponibile su:** `http://localhost:7860`

---

## ☁️ Deploy Cloud

### ▶️ Render

1. **Projects** → **New → Web Service** → *Public Git Repository*
2. **Repository**: `https://github.com/nzo66/EasyProxy`
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `gunicorn --bind 0.0.0.0:7860 --workers 4 --worker-class aiohttp.worker.GunicornWebWorker app:app`
5. **Deploy**

### 🤖 HuggingFace Spaces

1. Crea nuovo **Space** (SDK: *Docker*)
2. Carica tutti i file
3. Deploy automatico
4. **Pronto!**

---

## 💻 Installazione Locale

### 📋 Requisiti

- **Python 3.8+**
- **aiohttp**
- **gunicorn**
- **pycryptodome** (per DRM)

### 🔧 Installazione Completa

```bash
# Clone repository
git clone https://github.com/nzo66/EasyProxy.git
cd EasyProxy

# Installa dipendenze
pip install -r requirements.txt

# Avvio 
gunicorn --bind 0.0.0.0:7860 --workers 4 --worker-class aiohttp.worker.GunicornWebWorker app:app
```

---

## ⚙️ Configurazione Proxy

Il modo più semplice per configurare i proxy è tramite un file `.env`.

1.  **Crea un file `.env`** nella cartella principale del progetto (puoi rinominare il file `.env.example`).
2.  **Aggiungi le tue variabili proxy** al file `.env`.

**Esempio di file `.env`:**

```env
# Proxy globale per tutto il traffico
GLOBAL_PROXY=http://user:pass@myproxy.com:8080

# Proxy multipli per DLHD (uno verrà scelto a caso)
DLHD_PROXY=socks5://proxy1.com:1080,socks5://proxy2.com:1080

# Proxy specifico per Vavoo
VAVOO_PROXY=socks5://vavoo-proxy.net:9050

# Password API per proteggere le playlist generate (Opzionale)
API_PASSWORD=segretissimo
```

---

## 🧰 Utilizzo del Proxy

Sostituisci `<server-ip>` con l'IP del tuo server.

### 🎯 Interfaccia Web Principale

```
http://<server-ip>:7860/
```

### 📺 Proxy HLS Universale

```
http://<server-ip>:7860/proxy/manifest.m3u8?url=<URL_STREAM>
```

**Supporta:**
- **HLS (.m3u8)** - Streaming live e VOD
- **M3U playlist** - Liste canali IPTV  
- **MPD (DASH)** - Streaming adattivo con conversione automatica HLS
- **MPD + ClearKey DRM** - Decrittazione server-side CENC (VLC compatible)
- **DLHD streams** - Flussi dinamici con bypass anti-bot
- **Vavoo** - Risoluzione automatica

**Esempi:**
```bash
# Stream HLS generico
http://server:7860/proxy/manifest.m3u8?url=https://example.com/stream.m3u8

# MPD con ClearKey DRM (decrittazione server-side)
http://server:7860/proxy/manifest.m3u8?url=https://cdn.com/stream.mpd&clearkey=KID:KEY

# Playlist IPTV
http://server:7860/playlist?url=https://iptv-provider.com/playlist.m3u

# Stream con headers personalizzati
http://server:7860/proxy/manifest.m3u8?url=https://stream.com/video.m3u8&h_user-agent=VLC&h_referer=https://site.com
```

---

## 🔗 Playlist Builder

```
http://<server-ip>:7860/builder
```

Il **Playlist Builder** permette di combinare multiple playlist M3U in un unico link, con opzioni avanzate per ogni sorgente.

### Funzionalità Avanzate

Quando aggiungi una playlist, puoi specificare opzioni aggiuntive direttamente nell'URL (separatore `|`):

- **Sort (A-Z)**: Ordina alfabeticamente i canali della playlist.
- **No Proxy**: Usa i link originali senza passare per il proxy (utile per link diretti o locali).

**Formato URL Builder:**
```
URL_PLAYLIST|sort=true|noproxy=true
```

**Esempio Link Generato:**
```
http://server:7860/playlist?url=https://iptv.com/list.m3u|sort=true&url=https://local.com/list.m3u|noproxy=true
```

### Protezione Playlist
Se imposti `API_PASSWORD` nel file `.env`, puoi proteggere le tue playlist. Inserisci la password nel campo "API Password" del Builder per includerla nel link generato.

---

## 📖 Architettura

### 🔄 Flusso di Elaborazione

1. **Richiesta Stream** → Endpoint proxy universale
2. **Rilevamento Servizio** → Auto-detect Vavoo/DLHD/Generic
3. **Estrazione URL** → Risoluzione link reali tramite `extractors/`
    - *DLHD*: Gestione sessione, cookie, e nuovi flussi auth.
    - *Vavoo*: Calcolo firma app e risoluzione JSON.
    - *MPD*: Decrittazione CENC e conversione HLS.
4. **Proxy Stream** → Forward con headers ottimizzati
5. **Risposta Client** → Stream diretto compatibile

### ⚡ Sistema Asincrono

- **aiohttp** - HTTP client non-bloccante
- **Connection pooling** - Riutilizzo connessioni
- **Retry automatico** - Gestione errori intelligente
- **Caching Intelligente** - Cache risultati estrazione per ridurre carico (es. DLHD)

---

## 🤝 Contributi

I contributi sono benvenuti! Per contribuire:

1. **Fork** del repository
2. **Crea** un branch per le modifiche (`git checkout -b feature/AmazingFeature`)
3. **Commit** le modifiche (`git commit -m 'Add some AmazingFeature'`)
4. **Push** al branch (`git push origin feature/AmazingFeature`)
5. **Apri** una Pull Request

---

## 📚 API Reference

EasyProxy espone diversi endpoint per la gestione dei flussi e delle playlist.

### Endpoints Principali

| Metodo | Endpoint | Descrizione | Parametri |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | Pagina principale con stato del server. | - |
| `GET` | `/builder` | Interfaccia Web per il Playlist Builder. | - |
| `GET` | `/info` | Pagina informativa dettagliata. | - |
| `GET` | `/api/info` | Informazioni server in formato JSON. | - |

### Proxy & Streaming

| Metodo | Endpoint | Descrizione | Parametri |
| :--- | :--- | :--- | :--- |
| `GET` | `/proxy/manifest.m3u8` | **Proxy Universale**. Riscrive manifest HLS/DASH. | `url` (richiesto), `api_password`, headers custom (es. `h_Referer`) |
| `GET` | `/proxy/hls/manifest.m3u8` | Alias per compatibilità MediaFlow. | `d` (url), `api_password`, headers custom |
| `GET` | `/proxy/mpd/manifest.m3u8` | Proxy specifico per manifest DASH (.mpd). | `d` (url), `api_password`, `clearkey` |
| `GET` | `/proxy/stream` | Proxy generico per stream diretti. | `d` (url), headers custom |
| `GET` | `/key` | Proxy per chiavi di decifrazione AES-128. | `key_url`, `original_channel_url` |
| `GET` | `/license` | Proxy per licenze DRM (ClearKey/Widevine). | `url`, `clearkey` |
| `GET` | `/decrypt/segment.mp4` | Decifrazione segmenti lato server (CENC). | `url`, `key`, `key_id`, `init_url` |

### Playlist Builder

| Metodo | Endpoint | Descrizione | Parametri |
| :--- | :--- | :--- | :--- |
| `GET` | `/playlist` | Genera una playlist combinata. | `url` (lista URL separati da `;`), `api_password` |
| `POST` | `/generate_urls` | Generazione batch di URL proxy (compatibile MFP). | JSON body con lista URL |

---

## 🧩 Dettagli Estrattori

EasyProxy include moduli specializzati per estrarre stream da siti complessi.

### 1. DaddyLiveHD (`dlhd.py`)
*   **Domini**: `daddylive.mp`, `dlhd.sx`, `daddylivehd.sx`
*   **Funzionalità**:
    *   Bypassa protezioni anti-bot avanzate.
    *   Gestisce il nuovo flusso di autenticazione `newkso.ru` / `lovecdn.ru`.
    *   Mantiene sessioni persistenti per ridurre il carico.
    *   Cache intelligente dei risultati di estrazione.

### 2. Vavoo (`vavoo.py`)
*   **Domini**: `vavoo.to`
*   **Funzionalità**:
    *   Risolve automaticamente i link `vavoo.to/live/...`.
    *   Gestisce la firma crittografica richiesta dall'API Vavoo.
    *   Aggiunge automaticamente i parametri `sig` corretti.

### 3. Sportsonline (`sportsonline.py`)
*   **Domini**: `sportzonline.st`
*   **Funzionalità**:
    *   Scansiona la pagina alla ricerca di iframe.
    *   Decodifica script JavaScript offuscati (P.A.C.K.E.R.).
    *   Estrae link `.m3u8` nascosti.

### 4. VixSrc (`vixsrc.py`)
*   **Domini**: `vixsrc.to`
*   **Funzionalità**:
    *   Supporta link embed, movie e tv.
    *   Gestisce header `x-inertia` per navigazione API.
    *   Estrae token e parametri di scadenza dagli script della pagina.

### 5. VOE (`voe.py`)
*   **Domini**: `voe.sx`
*   **Funzionalità**:
    *   Gestisce redirect multipli.
    *   Decodifica payload offuscati in Base64 e rotazione caratteri.
    *   Estrae il link diretto al file video.

### 6. Streamtape (`streamtape.py`)
*   **Domini**: `streamtape.com`
*   **Funzionalità**:
    *   Estrae ID e token IP dal codice HTML.
    *   Costruisce l'URL finale `get_video`.

### 7. Mixdrop (`mixdrop.py`)
*   **Domini**: `mixdrop.co`, `mixdrop.to`
*   **Funzionalità**:
    *   Risolve la protezione "MDCore".
    *   Esegue sandbox di codice JavaScript per estrarre `wurl`.

---

## 📄 Licenza

Questo progetto è distribuito sotto licenza MIT. Vedi il file `LICENSE` per maggiori dettagli.

---

<div align="center">

**⭐ Se questo progetto ti è utile, lascia una stella! ⭐**

> 🎉 **Enjoy Your Streaming!**  
> Accedi ai tuoi contenuti preferiti ovunque, senza restrizioni, con controllo completo e performance ottimizzate.

</div>
