# trialconnect/__init__.py

from flask import Flask, session, g, request
import os
from dotenv import load_dotenv
from helpers import init_db, close_db, get_user_by_remember_token

def create_app():
    """Creates and configures the Flask application."""

    # Load .env file FIRST before reading any env vars
    load_dotenv()

    app = Flask(__name__)

    # --- Load all config from environment ---
    app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(24)
    app.config['GOOGLE_MAPS_API_KEY'] = os.environ.get('GOOGLE_MAPS_API_KEY')
    app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID')
    app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET')
    app.config['MONGODB_URI'] = os.environ.get('MONGODB_URI')
    app.config['GOOGLE_CLOUD_PROJECT'] = os.environ.get('GOOGLE_CLOUD_PROJECT')
    app.config['VERTEX_AI_LOCATION'] = os.environ.get('VERTEX_AI_LOCATION', 'us-central1')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'profile_pictures')
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Initialize MongoDB indexes
    init_db()

    # Initialize OAuth providers
    from .oauth_setup import init_oauth
    init_oauth(app)

    # Register the database close function
    app.teardown_appcontext(close_db)

    # Import and register routes
    with app.app_context():
        from . import routes

    # --- App-wide ADK agent runner (persists across requests) ---
    # This fixes the agent losing conversation memory between turns.
    try:
        from google.adk.sessions import InMemorySessionService
        from google.adk.runners import Runner
        from agent import root_agent
        app.agent_session_service = InMemorySessionService()
        app.agent_runner = Runner(
            agent=root_agent,
            app_name='trialconnect',
            session_service=app.agent_session_service
        )
    except Exception as e:
        print(f"Warning: ADK agent runner could not be initialized: {e}")
        app.agent_session_service = None
        app.agent_runner = None

    @app.before_request
    def load_logged_in_user():
        remember_token = request.cookies.get('remember_token')
        if not session.get('user') and remember_token:
            user = get_user_by_remember_token(remember_token)
            if user:
                session['user'] = user

    return app
