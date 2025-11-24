from flask import Blueprint, render_template, request

player_bp = Blueprint('player', __name__)

@player_bp.route('/player')
def player():
    # Get query parameters with fallback to empty strings or default value
    playlist_urls = request.args.get('playlist', '')
    epg_urls = request.args.get('epg', '')
    proxy = request.args.get('proxy', 'true').lower() == 'true'
    
    # Pass parameters to template for initial form population
    return render_template('player.html', playlist_urls=playlist_urls, epg_urls=epg_urls, proxy=proxy)
