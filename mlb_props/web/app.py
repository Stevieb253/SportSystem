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

    # Jinja2 filter: {{ player_id | mlb_photo }}
    @app.template_filter("mlb_photo")
    def mlb_photo_filter(player_id):
        if not player_id or int(player_id) == 0:
            return "https://img.mlb.com/headshots/current/60x60/generic@2x.jpg"
        return f"https://img.mlb.com/headshots/current/60x60/{player_id}@2x.jpg"

    return app
