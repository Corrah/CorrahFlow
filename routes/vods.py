from aiohttp import web
import aiohttp
import json
import random
import os
import time
import datetime

routes = web.RouteTableDef()

# --- Cache su disco per tutti i dati (consumo RAM ridotto) ---
import os
import json
CACHE_DIR = 'cache'
os.makedirs(CACHE_DIR, exist_ok=True)

def get_cache_file(content_type, suffix):
    return os.path.join(CACHE_DIR, f'{content_type}_{suffix}.json')

def load_cached_raw_data(content_type):
    """Carica dati raw dalla cache su disco se validi"""
    cache_file = get_cache_file(content_type, 'raw')
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            # Verifica se la cache è ancora valida (stesso giorno)
            now = datetime.datetime.now()
            cache_date = datetime.datetime.fromisoformat(cached['date'])
            if cache_date.date() == now.date():
                return cached['data'], cached['timestamp']
        except Exception as e:
            print(f"Errore caricamento cache raw: {e}")
    return None, 0

def save_cached_raw_data(content_type, data, timestamp):
    """Salva dati raw su disco"""
    cache_file = get_cache_file(content_type, 'raw')
    try:
        cache_data = {
            'date': datetime.datetime.now().isoformat(),
            'data': data,
            'timestamp': timestamp
        }
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Errore salvataggio cache raw: {e}")

def load_cached_processed_data(content_type):
    """Carica dati processati dalla cache su disco se validi"""
    cache_file = get_cache_file(content_type, 'processed')
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            # Verifica se la cache è ancora valida (stesso giorno)
            now = datetime.datetime.now()
            cache_date = datetime.datetime.fromisoformat(cached['date'])
            if cache_date.date() == now.date():
                return cached['grouped_content'], cached['filtered_data']
        except Exception as e:
            print(f"Errore caricamento cache processata: {e}")
    return None, None

def save_cached_processed_data(content_type, grouped_content, filtered_data):
    """Salva dati processati su disco"""
    cache_file = get_cache_file(content_type, 'processed')
    try:
        data = {
            'date': datetime.datetime.now().isoformat(),
            'grouped_content': grouped_content,
            'filtered_data': filtered_data
        }
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Errore salvataggio cache processata: {e}")
async def get_cached_data(content_type, session):
    """Recupera i dati dalla cache su disco o li aggiorna se scaduti (reset giornaliero alle 03:00)."""
    # Try to load from disk cache first
    data, timestamp = load_cached_raw_data(content_type)

    should_refresh = False
    if data is None:
        should_refresh = True
    else:
        now = time.time()

        # Calcola l'orario delle ultime 03:00
        dt_now = datetime.datetime.fromtimestamp(now)
        dt_3am = dt_now.replace(hour=3, minute=0, second=0, microsecond=0)

        if dt_now < dt_3am:
            # Se siamo prima delle 03:00 di oggi, l'ultimo reset è stato ieri alle 03:00
            dt_3am = dt_3am - datetime.timedelta(days=1)

        last_3am_timestamp = dt_3am.timestamp()

        # Se la cache è più vecchia dell'ultimo orario di reset (03:00), aggiorna
        if timestamp < last_3am_timestamp:
            should_refresh = True

    if should_refresh:
        url = 'https://raw.githubusercontent.com/nzo66/public-files/refs/heads/main/playlist.json' if content_type == 'movie' else 'https://raw.githubusercontent.com/nzo66/public-files/refs/heads/main/tv_playlist.json'
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
            timestamp = time.time()
            # Save to disk cache
            save_cached_raw_data(content_type, data, timestamp)

    return data

@routes.get('/movies')
async def movies(request):
    # Call vods logic directly with movie type
    return await get_vods_content(request, 'movie')

@routes.get('/tv')
async def tv(request):
    # Call vods logic directly with series type
    return await get_vods_content(request, 'series')

async def render_template(template_name, context):
    with open(f'templates/{template_name}', 'r', encoding='utf-8') as f:
        template_string = f.read()

    # Sostituzione manuale e semplice dei segnaposto nel template
    for key, value in context.items():
        template_string = template_string.replace(f'{{{{ {key} }}}}', str(value))

    # Hero Banner
    hero_item = context.get('hero_item')
    hero_id = None
    if hero_item:
        hero_id = hero_item.get('series_id') if context.get('content_type') == 'series' else hero_item.get('stream_id')

    if not (hero_item and hero_id):
        # Se non c'è un hero item valido, nascondi la sezione
        template_string = template_string.replace('{{ hero_section_visibility }}', 'style="display: none;"')
    else:
        template_string = template_string.replace('{{ hero_section_visibility }}', '')

    # Content Rows
    if 'grouped_content' in context:
        rows_html = ''
        # Ordina i gruppi alfabeticamente per una visualizzazione coerente
        sorted_groups = sorted(context['grouped_content'].items())

        # Genera le opzioni per il select delle categorie
        category_options_html = '<option value="">Tutte le categorie</option>'
        for group, _ in sorted_groups:
            category_options_html += f'<option value="{group}">{group}</option>'
        template_string = template_string.replace('{{ category_options }}', category_options_html)

        for group, items in sorted_groups:
            rows_html += f'<div class="movie-row-container" data-category="{group}">'
            rows_html += f'<h2>{group}</h2>'
            rows_html += '<div class="movie-row">'
            for item in items:
                # Handle different field names for movies vs series
                vod_id = item.get('stream_id') or item.get('series_id') or item.get('num', '')
                image_url = item.get('stream_icon') or item.get('cover') or ''
                item_name = item.get('name', '')

                rows_html += f'''
                <div class="movie-card" data-vod-id="{vod_id}" data-category="{group}">
                    <img class="movie-card-image lazy-load" data-src="{image_url}" alt="{item_name}" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 300'%3E%3Crect fill='%23222' width='200' height='300'/%3E%3C/svg%3E">
                    <div class="movie-card-title">{item_name}</div>
                    <div class="movie-card-expanded-content" style="display: none;">
                        <!-- Content will be loaded dynamically -->
                    </div>
                </div>
                '''
            rows_html += '</div>'
            rows_html += '</div>'
        template_string = template_string.replace('{{ content_rows }}', rows_html)

    return web.Response(text=template_string, content_type='text/html')

async def get_vods_content(request, content_type):
    """Common function to get VOD content for movies or series"""
    # Try to load from disk cache first
    grouped_content, filtered_data = load_cached_processed_data(content_type)

    if grouped_content is None or filtered_data is None:
        # Cache miss, process the data
        try:
            async with aiohttp.ClientSession() as session:
                # Usa la funzione di caching invece di scaricare sempre i dati
                all_data = await get_cached_data(content_type, session)
        except aiohttp.ClientError as e:
            return web.Response(text=f"Error fetching data: {e}", status=500)

        # Filter data by type (adapt to different JSON structures)
        if content_type == 'series':
            # For series, all items are series, filter by category if needed
            filtered_data = all_data
        else:
            # For movies, filter by stream_type
            filtered_data = [item for item in all_data if item.get('stream_type') == content_type]

        # Group by category (adapt to different field names)
        grouped_content = {}
        for item in filtered_data:
            if content_type == 'series':
                # For series, use genre or create a default group
                group = item.get('genre', 'Serie TV')
            else:
                # For movies, use the existing group field
                group = item.get('group', 'Uncategorized')

            if group not in grouped_content:
                grouped_content[group] = []
            grouped_content[group].append(item)

        # Save to disk cache
        save_cached_processed_data(content_type, grouped_content, filtered_data)

    # Select a random item for the hero banner
    # Assicurati di scegliere un hero item che abbia un ID valido per evitare errori
    if content_type == 'movie':
        valid_hero_items = [item for item in filtered_data if item.get('stream_id')]
    else: # series
        valid_hero_items = [item for item in filtered_data if item.get('series_id')]
    hero_item = random.choice(valid_hero_items) if valid_hero_items else None
    hero_id = None
    backdrop = ''
    if hero_item:
        hero_id = hero_item.get('series_id') if content_type == 'series' else hero_item.get('stream_id')
        backdrop_path = hero_item.get('backdrop_path', '')
        if isinstance(backdrop_path, list) and backdrop_path:
            backdrop = backdrop_path[0]
        elif isinstance(backdrop_path, str):
            backdrop = backdrop_path

    # Determine active navigation based on request path
    path = str(request.path)
    if path == '/movies':
        active_nav = 'Film'
    elif path == '/tv':
        active_nav = 'Serie TV'
    else:
        active_nav = 'Home'

    return await render_template('vods.html', {
        'grouped_content': grouped_content,
        'hero_item': hero_item,
        'content_type': content_type,
        'hero_backdrop': backdrop,
        'hero_title': hero_item.get('name', '') if hero_item else '',
        'hero_plot': hero_item.get('plot', '') if hero_item else '',
        'hero_vod_id': str(hero_id or ''),
        'active_nav': active_nav,
    })

@routes.get('/api/series/{series_id}/seasons')
async def get_series_seasons(request):
    series_id = request.match_info.get('series_id')
    tmdb_api_key = os.getenv('TMDB_API_KEY', '')

    if not tmdb_api_key:
        return web.json_response({'error': 'TMDb API key not configured'}, status=500)

    try:
        # Get series details from TMDB to get seasons
        async with aiohttp.ClientSession() as session:
            url = f"https://api.themoviedb.org/3/tv/{series_id}?api_key={tmdb_api_key}&language=it-IT"
            async with session.get(url) as response:
                if response.status != 200:
                    return web.json_response({'error': 'Series not found in TMDB'}, status=404)

                series_data = await response.json()

        # Extract seasons information
        seasons = series_data.get('seasons', [])
        # Filter out special seasons (season_number 0) and sort by season number
        filtered_seasons = [season for season in seasons if season.get('season_number', 0) > 0]
        filtered_seasons.sort(key=lambda x: x.get('season_number', 0))

        return web.json_response({'seasons': filtered_seasons})

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

@routes.get('/api/series/{series_id}/season/{season_number}')
async def get_season_episodes(request):
    series_id = request.match_info.get('series_id')
    season_number = request.match_info.get('season_number')
    tmdb_api_key = os.getenv('TMDB_API_KEY', '')

    if not tmdb_api_key:
        return web.json_response({'error': 'TMDb API key not configured'}, status=500)

    try:
        # Get season details from TMDB
        async with aiohttp.ClientSession() as session:
            url = f"https://api.themoviedb.org/3/tv/{series_id}/season/{season_number}?api_key={tmdb_api_key}&language=it-IT"
            async with session.get(url) as response:
                if response.status != 200:
                    return web.json_response({'error': 'Season not found in TMDB'}, status=404)

                season_data = await response.json()

        # Extract episodes information
        episodes = season_data.get('episodes', [])
        # Sort episodes by episode number
        episodes.sort(key=lambda x: x.get('episode_number', 0))

        return web.json_response({'episodes': episodes})

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

@routes.get('/api/vod/{vod_id}')
async def get_vod_details(request):
    vod_id = request.match_info.get('vod_id')
    content_type = request.query.get('type', 'movie')  # Get type from query params
    tmdb_api_key = os.getenv('TMDB_API_KEY', '')

    if not tmdb_api_key:
        return web.json_response({'error': 'TMDb API key not configured'}, status=500)

    try:
        # Choose the correct data source based on type
        if content_type == 'series':
            data_url = 'https://raw.githubusercontent.com/nzo66/public-files/refs/heads/main/tv_playlist.json'
            tmdb_endpoint = 'tv'
        else:
            data_url = 'https://raw.githubusercontent.com/nzo66/public-files/refs/heads/main/playlist.json'
            tmdb_endpoint = 'movie'

        # Recupera le informazioni di base dalla cache
        async with aiohttp.ClientSession() as session:
            items_data = await get_cached_data(content_type, session)

        # Find the item (adapt to different ID fields)
        item_info = None
        for item in items_data:
            if content_type == 'series':
                # For series, check series_id or num
                if str(item.get('series_id', '')) == vod_id or str(item.get('num', '')) == vod_id:
                    item_info = item
                    break
            else:
                # For movies, check stream_id
                if str(item.get('stream_id', '')) == vod_id:
                    item_info = item
                    break

        if not item_info:
            return web.json_response({'error': 'VOD not found'}, status=404)

        # Try to fetch details from TMDb using the appropriate endpoint
        tmdb_data = None
        trailer_key = ''

        try:
            async with aiohttp.ClientSession() as session:
                # For series, we might not have TMDB data, so we'll use the JSON data directly
                if content_type == 'series':
                    # For series, try to find TMDB ID if available, otherwise use JSON data
                    tmdb_id = item_info.get('series_id')
                    if tmdb_id:
                        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=it-IT&append_to_response=videos"
                        async with session.get(url) as response:
                            if response.status == 200:
                                tmdb_data = await response.json()
                else:
                    # For movies, use the existing TMDB lookup
                    url = f"https://api.themoviedb.org/3/movie/{vod_id}?api_key={tmdb_api_key}&language=it-IT&append_to_response=videos"
                    async with session.get(url) as response:
                        if response.status == 200:
                            tmdb_data = await response.json()

            # Extract trailer if TMDB data is available
            if tmdb_data:
                videos = tmdb_data.get('videos', {}).get('results', [])
                trailer = next((v for v in videos if v.get('type') == 'Trailer' and v.get('site') == 'YouTube'), None)
                if trailer:
                    trailer_key = trailer.get('key')

                # Fallback: se non c'è trailer, cerca per nome e fai una seconda chiamata per i video
                if not trailer_key and item_info.get('name'):
                    search_url = f"https://api.themoviedb.org/3/search/{tmdb_endpoint}?api_key={tmdb_api_key}&query={item_info['name']}&language=it-IT"
                    async with session.get(search_url) as search_response:
                        if search_response.status == 200:
                            search_data = await search_response.json()
                            if search_data.get('results') and len(search_data['results']) > 0:
                                found_id = search_data['results'][0].get('id')
                                if found_id:
                                    videos_url = f"https://api.themoviedb.org/3/{tmdb_endpoint}/{found_id}/videos?api_key={tmdb_api_key}"
                                    async with session.get(videos_url) as videos_response:
                                        if videos_response.status == 200:
                                            videos_data = await videos_response.json()
                                            videos = videos_data.get('results', [])
                                            trailer = next((v for v in videos if v.get('type') == 'Trailer' and v.get('site') == 'YouTube'), None)
                                            if trailer:
                                                trailer_key = trailer.get('key')
        except Exception as e:
            # If TMDB fails, continue with JSON data only
            print(f"TMDB lookup failed: {e}")

        # Handle backdrop_path being either string or list
        backdrop = item_info.get('backdrop_path', '')
        if isinstance(backdrop, list) and backdrop:
            backdrop = backdrop[0]  # Take first backdrop if it's a list
        elif isinstance(backdrop, str) and backdrop.startswith('https://image.tmdb.org'):
            backdrop = backdrop  # Already a proper URL
        else:
            backdrop = ''  # Fallback

        # Extract additional information from TMDB if available
        cast = []
        director = ''
        duration = ''
        country = ''

        if tmdb_data:
            # Create a new session for additional API calls
            async with aiohttp.ClientSession() as credits_session:
                # Get cast information
                if content_type == 'movie':
                    # For movies, get credits
                    try:
                        credits_url = f"https://api.themoviedb.org/3/movie/{vod_id}/credits?api_key={tmdb_api_key}&language=it-IT"
                        async with credits_session.get(credits_url) as credits_response:
                            if credits_response.status == 200:
                                credits_data = await credits_response.json()
                                cast = [actor['name'] for actor in credits_data.get('cast', [])[:10]]  # Get first 10 actors
                                # Get director from crew
                                crew = credits_data.get('crew', [])
                                director_info = next((person for person in crew if person.get('job') == 'Director'), None)
                                if director_info:
                                    director = director_info['name']
                    except Exception as e:
                        print(f"Error fetching credits: {e}")

                    # Get runtime and production countries
                    duration = tmdb_data.get('runtime', '')
                    production_countries = tmdb_data.get('production_countries', [])
                    if production_countries:
                        country = production_countries[0].get('name', '')

                elif content_type == 'series':
                    # For series, get credits
                    try:
                        credits_url = f"https://api.themoviedb.org/3/tv/{vod_id}/credits?api_key={tmdb_api_key}&language=it-IT"
                        async with credits_session.get(credits_url) as credits_response:
                            if credits_response.status == 200:
                                credits_data = await credits_response.json()
                                cast = [actor['name'] for actor in credits_data.get('cast', [])[:10]]  # Get first 10 actors
                                # Get creator(s) as director equivalent
                                created_by = tmdb_data.get('created_by', [])
                                if created_by:
                                    director = created_by[0].get('name', '')
                    except Exception as e:
                        print(f"Error fetching series credits: {e}")

                    # Get episode runtime and origin country
                    episode_run_time = tmdb_data.get('episode_run_time', [])
                    if episode_run_time:
                        duration = f"{episode_run_time[0]} min per episodio"
                    origin_country = tmdb_data.get('origin_country', [])
                    if origin_country:
                        country = origin_country[0]

        # Combine data from JSON and TMDB (if available)
        combined_data = {
            'name': item_info.get('name'),
            'plot': tmdb_data.get('overview', item_info.get('plot')) if tmdb_data else item_info.get('plot'),
            'backdrop_path': ('https://image.tmdb.org/t/p/original' + tmdb_data.get('backdrop_path', '')) if tmdb_data and tmdb_data.get('backdrop_path') else backdrop,
            'rating': tmdb_data.get('vote_average', item_info.get('rating', 0)) if tmdb_data else item_info.get('rating', 0),
            'trailer_key': trailer_key,
            'stream_url': item_info.get('stream_url'), # Aggiunto l'URL dello stream per i film
            'stream_id': item_info.get('stream_id'), # Aggiunto per la riproduzione dal modale
            'series_id': item_info.get('series_id'), # Aggiunto per coerenza
            'release_date': tmdb_data.get('release_date') if tmdb_data else '', # Aggiunta la data di uscita
            'genres': [g['name'] for g in tmdb_data.get('genres', [])] if tmdb_data else [], # Aggiunti i generi
            'cast': cast, # Informazioni aggiuntive
            'director': director,
            'duration': duration,
            'country': country
        }

        return web.json_response(combined_data)

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

vods_bp = routes
