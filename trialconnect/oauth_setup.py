# trialconnect/oauth_setup.py

from authlib.integrations.flask_client import OAuth
import os

oauth = OAuth()

def init_oauth(app):
    """Initializes all OAuth providers using environment variables."""
    oauth.init_app(app)

    client_id = app.config.get('GOOGLE_CLIENT_ID') or os.environ.get('GOOGLE_CLIENT_ID')
    client_secret = app.config.get('GOOGLE_CLIENT_SECRET') or os.environ.get('GOOGLE_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in your .env file."
        )

    # --- Register Google ---
    oauth.register(
        name='google',
        client_id=client_id,
        client_secret=client_secret,
        client_kwargs={'scope': 'openid email profile'},
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        claims_options={
            'iss': {
                'essential': True,
                'values': ['https://accounts.google.com']
            }
        }
    )

    # TODO: --- Register Microsoft (Future) ---
