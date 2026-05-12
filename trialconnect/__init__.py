# trialconnect/__init__.py

from flask import Flask, session, g, request
import os
from helpers import init_db, close_db, get_user_by_remember_token
from dotenv import load_dotenv

def create_app():
    """Creates and configures the Flask application."""
    app = Flask(__name__)
    app.config['GOOGLE_MAPS_API_KEY'] = os.environ.get('GOOGLE_MAPS_API_KEY')
    app.secret_key = os.urandom(12)

    # Initialize the database
    init_db()

    # Initialize OAuth providers
    from .oauth_setup import init_oauth
    init_oauth(app)

    # Register the database close function
    app.teardown_appcontext(close_db)

    # Import and register routes
    with app.app_context():
        from . import routes

    @app.before_request
    def load_logged_in_user():
        user_id = session.get('user_id')
        remember_token = request.cookies.get('remember_token')

        if user_id is None and remember_token:
            user = get_user_by_remember_token(remember_token)
            if user:
                session['user'] = user
        elif user_id is not None:
             pass

    return app