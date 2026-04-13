# web/app.py
# Flask app factory. No routes defined here.

from datetime import date

from flask import Flask

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

APP_VERSION = "1.0.0"


def create_app() -> Flask:
    """Create and configure the Flask application.

    Returns:
        Configured Flask app instance.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["SECRET_KEY"] = "mlb-props-dev-key"

    # Register blueprint
    from web.routes import bp
    app.register_blueprint(bp)

    # Template globals available in every template
    @app.context_processor
    def _inject_globals():
        return {
            "current_date": date.today().isoformat(),
            "app_version":  APP_VERSION,
        }

    # ── Team helpers ─────────────────────────────────────────────────────────
    _TEAM_SHORT = {
        "ARI": "D-backs",  "ATL": "Braves",    "BAL": "Orioles",
        "BOS": "Red Sox",  "CHC": "Cubs",       "CWS": "White Sox",
        "CIN": "Reds",     "CLE": "Guardians",  "COL": "Rockies",
        "DET": "Tigers",   "HOU": "Astros",     "KC":  "Royals",
        "LAA": "Angels",   "LAD": "Dodgers",    "MIA": "Marlins",
        "MIL": "Brewers",  "MIN": "Twins",      "NYM": "Mets",
        "NYY": "Yankees",  "OAK": "Athletics",  "PHI": "Phillies",
        "PIT": "Pirates",  "SD":  "Padres",     "SEA": "Mariners",
        "SF":  "Giants",   "STL": "Cardinals",  "TB":  "Rays",
        "TEX": "Rangers",  "TOR": "Blue Jays",  "WSH": "Nationals",
        "AZ":  "D-backs",  "ATH": "Athletics",
    }
    # MLB abbr → ESPN CDN abbr (only where they differ)
    _ESPN_ABBR = {
        "CWS": "chw", "AZ": "ari", "ATH": "oak",
    }

    @app.template_filter("team_name")
    def team_name_filter(abbr):
        return _TEAM_SHORT.get(abbr, abbr)

    @app.template_filter("team_logo")
    def team_logo_filter(abbr):
        espn = _ESPN_ABBR.get(abbr, abbr.lower())
        return f"https://a.espncdn.com/i/teamlogos/mlb/500/{espn}.png"

    # Jinja2 filter: {{ player_id | mlb_photo }}
    @app.template_filter("mlb_photo")
    def mlb_photo_filter(player_id):
        if not player_id or int(player_id) == 0:
            return ""  # empty → probe skipped, SVG placeholder stays
        return f"https://securea.mlb.com/mlb/images/players/head_shot/{player_id}.jpg"

    # Jinja2 filter: {{ '2026-04-12' | format_date }} → 'Apr 12, 2026'
    @app.template_filter("format_date")
    def format_date_filter(date_str):
        try:
            from datetime import datetime
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%b %-d, %Y")
        except Exception:
            try:
                from datetime import datetime
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                # Windows-compatible: no %-d
                return dt.strftime("%b %d, %Y").replace(" 0", " ")
            except Exception:
                return date_str

    return app
