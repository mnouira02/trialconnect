# run.py

from trialconnect import create_app
import os

app = create_app()

if __name__ == '__main__':
    # This setting is for local development only. Do not use in production.
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(debug=True)