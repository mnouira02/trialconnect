# TrialConnect

#### Video Demo: https://youtu.be/m08SbEcXtgQ

#### Description

TrialConnect is a Flask-based web application that helps people discover recruiting and not-yet-recruiting clinical trials near a chosen location, presenting results on an interactive map and ranking them by distance, promotion priority, and profile-based eligibility matching when a user is logged in. The backend integrates the ClinicalTrials.gov API to search studies, extracts sites with geocoordinates for mapping, and queries per-study eligibility to support a lightweight age/sex match indicator that can improve result ordering and explain “match/no match” outcomes. The application includes local authentication with secure password hashing, Google OAuth sign-in, an admin panel to manage users and promoted studies, and basic analytics that log when promoted studies are returned for specific search terms.

### Features

- Location-aware search for recruiting/not-yet-recruiting studies with a “search this area” interaction as the map is moved or zoomed.  
- Structured ranking that boosts a curated list of promoted NCT IDs before sorting by distance; when logged in, adds an eligibility-based signal from age/sex checks. 
- Study details sourced from API, including locations for hospital sites. 
- Account system supporting local registration and Google sign-in, a profile page with birth year and sex, password change capabilities, a way for user to reset password with a One Time Passcode feature (accessible via admin page)
- Admin views to list/delete users, manage promoted studies, and observe basic promotion analytics by search term and timestamp. The admin allows to check for contact forms and delete them. There is also the option to delete all entries to database (very useful for development). 
- Contact form with server-side persistence of messages in SQLite.

### Architecture

The app uses Flask. SQLite is used for database management, and a central helpers.py module manages DB connections, schema initialization, API calls, and key domain utilities. HTTP requests to ClinicalTrials.gov and external services use the requests library, and templates are rendered using Jinja2 alongside Flask-Session for session management.

### Data Model

The database schema includes four tables: contacts, users, promoted_studies, and promotion_analytics. 
- contacts: id, firstName, lastName, email, message.  
- users: id, email (unique), firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token, created_at, reset_token, reset_token_expiration.  
- promoted_studies: nct_id (PK), added_at.  
- promotion_analytics: id, nct_id, search_term, view_date.

### ClinicalTrials.gov Integration

Search is implemented against the v2 Studies endpoint with parameters such as query.term, filter.overallStatus, and filter.geo distance(...) to retrieve nearby recruiting and not-yet-recruiting studies in JSON. The response is parsed from protocolSection to extract identificationModule (e.g., nctId, briefTitle), statusModule (overallStatus), conditionsModule, and contactsLocationsModule.locations with geoPoint for mapping. For per-study eligibility, the app requests fields=EligibilityModule and reads sex, minimumAge, and maximumAge to support simple age/sex matching logic.

### Eligibility Matching

When a user profile contains a birthYear and sex, the application computes age from the current year and compares sex and age to the study’s eligibilityModule constraints. Sex matches when the study accepts ALL or explicitly matches MALE/FEMALE, and age matches when the computed age falls within [minimumAge, maximumAge] after parsing numeric values from strings like “0 Years.” Outcomes are one of MATCH, NO_MATCH, or NO_DATA (with a reason), enabling transparent ranking signals and explanations in the UI.

### Authentication & Security

Local registration uses werkzeug’s password hashing with a basic password-strength validator requiring length and character variety. Google sign-in is supported via Authlib and a Google OAuth client configuration.

### Geolocation & Distance

A helper uses ip-api.com to resolve approximate latitude and longitude from the requester’s IP. A Haversine implementation computes distances between coordinates when needed, complementing API-driven location filtering via filter.geo.

### Setup

- Create and activate a Python virtual environment, then install dependencies: pip install -r requirements.txt.  
- Provide configuration via environment variables or a .env file for Flask secret keys and Google OAuth credentials.

### Running

Start the development server with python run.py, which creates the Flask app via the application factory and enables debug mode for local iteration.

### Design Decisions

- ClinicalTrials.gov v2 was chosen for its structured protocolSection modules and modernized JSON schema, simplifying parsing and long-term maintenance.  
- SQLite was selected for simplicity and zero external dependencies. 
- A promotions table plus lightweight analytics enable non-invasive boosting and visibility tracking without overcomplicating ranking. 
- The initial eligibility match is intentionally narrow (age/sex) to balance clarity, performance, and testability before tackling free-text inclusion/exclusion parsing.

### Limitations & Future Work

- Expand eligibility matching to parse additional inclusion/exclusion text, potentially leveraging structured fields and NLP for robust criteria interpretation.
- Improve dependency hygiene by removing items (e.g., sqlite3) from requirements.txt and managing frontend libraries like Bootstrap via static assets or a bundler.

### File Guide (Authored/Configured) [2]

- run.py: Entrypoint that imports create_app.
- helpers.py: DB connection lifecycle, schema init, contact/user helpers, promotions/analytics, ClinicalTrials.gov search/formatting, IP geolocation, Haversine, and eligibility matching.
- templates folder: contains all the html leveraging jinja templating