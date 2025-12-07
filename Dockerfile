# Fase 1: Build
# Usa un'immagine Python ufficiale e leggera come base.
FROM python:3.11-slim

# Imposta la directory di lavoro all'interno del container.
WORKDIR /app

# Copia il file delle dipendenze.
# Farlo prima del resto del codice sfrutta la cache di Docker se le dipendenze non cambiano.
COPY requirements.txt .

# Installa FFmpeg (necessario per transcoding MPD streams)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Installa le dipendenze Python.
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del codice dell'applicazione nella directory di lavoro.
COPY . .

# Metadata dell'immagine OCI (Open Container Initiative) corretti.
LABEL org.opencontainers.image.title="HLS Proxy Server"
LABEL org.opencontainers.image.description="Server proxy universale per stream HLS con supporto Vavoo, DLHD e playlist builder"
LABEL org.opencontainers.image.version="2.5.0"
LABEL org.opencontainers.image.source="https://github.com/nzo66/EasyProxy"

# Esponi la porta su cui l'applicazione è in ascolto.
EXPOSE 7860

# Comando per avviare l'app in produzione con Gunicorn
# Usa sh -c per permettere l'espansione della variabile d'ambiente $PORT
CMD sh -c "gunicorn --bind 0.0.0.0:${PORT:-7860} --workers 4 --worker-class aiohttp.worker.GunicornWebWorker --timeout 120 --graceful-timeout 120 app:app"