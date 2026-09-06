"""
Homepty Swarm Backend - Flask Application Factory
"""

import os
import warnings

# Suppress multiprocessing resource_tracker warnings (from third-party libraries like transformers)
# Must be set before all other imports
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
def create_app(config_class=None, **kwargs):
    """Production is the default; legacy is explicitly local research only."""
    if os.environ.get("SWARM_RESEARCH_ROUTES_ENABLED") == "true":
        if os.environ.get("SWARM_ENV") != "local":
            raise RuntimeError("RESEARCH_ROUTES_FORBIDDEN")
        return _create_research_app(config_class)
    from .api.runs_v1 import create_runtime_app
    return create_runtime_app(**kwargs)


def _create_research_app(config_class=None):
    """Flask application factory function"""
    from flask_cors import CORS
    from .config import Config
    from .utils.logger import setup_logger, get_logger
    config_class = config_class or Config
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # Set JSON encoding: ensure non-ASCII characters are displayed directly
    # Flask >= 2.3 uses app.json.ensure_ascii, older versions use JSON_AS_ASCII config
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # Setup logger
    logger = setup_logger('homepty_swarm')
    
    # Only print startup info in reloader sub-process (avoid printing twice in debug mode)
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("Homepty Swarm Backend starting...")
        logger.info("=" * 50)
    
    # Enable CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Register simulation process cleanup function (ensure all simulation processes are terminated on server shutdown)
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("Simulation process cleanup function registered")
    
    # Request logging middleware
    @app.before_request
    def log_request():
        logger = get_logger('homepty_swarm.request')
        logger.debug(f"Request: {request.method} {request.path}")
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"Response: {response.status_code}")
        return response
    
    # Register blueprints
    from .api import graph_bp, simulation_bp, report_bp, register_research_routes
    register_research_routes()
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')
    
    # Health check
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'Homepty Swarm Backend'}
    
    if should_log_startup:
        logger.info("Homepty Swarm Backend started successfully")
    
    return app
