# trialconnect/oauth_setup.py

from authlib.integrations.flask_client import OAuth
import json

oauth = OAuth()

def init_oauth(app):
    """Initializes all OAuth providers."""
    oauth.init_app(app)

    # --- Load Google Credentials ---
    with open('google_secret.json') as f:
        google_secrets = json.load(f)['web']

    # --- Register Google ---
    oauth.register(
        name='google',
        client_id=google_secrets['client_id'],
        client_secret=google_secrets['client_secret'],
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