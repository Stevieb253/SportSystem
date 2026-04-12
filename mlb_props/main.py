# main.py — CLI entry point.
# Usage:
#   python main.py --output web            (start Flask server)
#   python main.py --output json           (print model JSON to stdout)
#   python main.py --output html           (write static HTML file)
#   python main.py --output web --port 8080
#   python main.py --output web --date 2026-04-12

import argparse
import json
import logging
import os
import socket
import sys
from datetime import date

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

import config
from data.cache import Cache
from data.pipeline import DataPipeline
from services import hit_probability, hr_probability
from services.model_builder import ModelBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def _build_pipeline() -> tuple[DataPipeline, ModelBuilder]:
    """Initialise pipeline and model builder."""
    cache    = Cache(config.CACHE_DIR, config.CACHE_TTL_HOURS)
    pipeline = DataPipeline(cache)
    builder  = ModelBuilder(pipeline, hit_probability, hr_probability)
    return pipeline, builder


def _local_ip() -> str:
    """Return the machine's local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def run_web(date_str: str, port: int) -> None:
    """Build model into cache then start Flask server."""
    import dataclasses
    _, builder = _build_pipeline()
    logger.info("Pre-building model for %s…", date_str)
    try:
        model = builder.get_model_for_date(date_str)
        games = model.get("games", [])
        hits  = model.get("hit_probabilities", [])
        logger.info("Model ready: %d games, %d player projections", len(games), len(hits))
    except Exception as exc:
        logger.warning("Model pre-build failed (will retry on request): %s", exc)

    from web.app import create_app
    app = create_app()
    local_ip = _local_ip()
    print(f"\n  Server running at   http://localhost:{port}")
    print(f"  Friends on network  http://{local_ip}:{port}\n")
    app.run(host=config.HOST, port=port, debug=config.DEBUG, use_reloader=False)


def run_json(date_str: str) -> None:
    """Print full model as JSON to stdout."""
    import dataclasses
    _, builder = _build_pipeline()
    model = builder.get_model_for_date(date_str)

    def _convert(obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if isinstance(obj, list):
            return [_convert(i) for i in obj]
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        return obj

    print(json.dumps(_convert(model), indent=2, default=str))


def run_html(date_str: str) -> None:
    """Render today's model to a standalone HTML file."""
    from web.app import create_app
    app = create_app()
    _, builder = _build_pipeline()
    model = builder.get_model_for_date(date_str)

    with app.app_context():
        from flask import render_template
        html = render_template("index.html", model=model, selected_date=date_str)

    filename = f"mlb_model_{date_str}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Saved: {os.path.abspath(filename)}")


def main() -> None:
    """Parse CLI arguments and dispatch to the correct mode."""
    parser = argparse.ArgumentParser(description="MLB Props Model")
    parser.add_argument("--date",   default=date.today().isoformat(), help="YYYY-MM-DD (default today)")
    parser.add_argument("--output", default="web", choices=["web", "json", "html"], help="Output mode")
    parser.add_argument("--port",   type=int, default=config.PORT, help="Flask port (web mode only)")
    args = parser.parse_args()

    if args.output == "web":
        run_web(args.date, args.port)
    elif args.output == "json":
        run_json(args.date)
    elif args.output == "html":
        run_html(args.date)


if __name__ == "__main__":
    main()
