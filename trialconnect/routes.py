import secrets
from datetime import datetime, timedelta, timezone
from flask import get_flashed_messages, render_template, request, redirect, url_for, session, current_app, abort, flash, make_response, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import os, uuid, requests
from bson import ObjectId
from .oauth_setup import oauth
from helpers import (
    add_contact_message, get_or_create_user, get_mongo_db, get_user_by_email,
    validate_password_strength, allowed_file,
    get_user_by_id, search_clinical_trials, get_location_from_ip, haversine,
    remove_promoted_study, get_all_promoted_studies, get_all_promoted_studies_set,
    add_promoted_study, log_promotion_analytic, check_user_study_match,
    search_trials_mongo, score_trial,
    fetch_trial_eligibility_text, gemini_eligibility_check,
    extract_patient_profile_from_document,
    save_patient_dossier, load_patient_dossier
)

app = current_app

# ---------------------------------------------------------------------------
# Helper: resolve admin email from env (never hardcoded)
# ---------------------------------------------------------------------------
def _is_admin():
    admin_email = os.environ.get('ADMIN_EMAIL', '')
    return bool(admin_email) and session.get('user', {}).get('email') == admin_email


# --- Page Routes ---
@app.route('/')
def index():
    query = request.args.get('query')
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    fallback_lat, fallback_lon = get_location_from_ip(user_ip)
    if not fallback_lat or not fallback_lon:
        fallback_lat = 40.7128
        fallback_lon = -74.0060
    return render_template(
        "index.html",
        query=query,
        results=[],
        fallback_lat=fallback_lat,
        fallback_lon=fallback_lon,
        google_maps_api_key=current_app.config['GOOGLE_MAPS_API_KEY']
    )


# ---------------------------------------------------------------------------
# GUIDED ONBOARDING WIZARD
# ---------------------------------------------------------------------------
@app.route('/onboarding')
def onboarding():
    """4-step guided wizard: condition → location → profile → review & launch."""
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    fallback_lat, fallback_lon = get_location_from_ip(user_ip)
    if not fallback_lat or not fallback_lon:
        fallback_lat = 40.7128
        fallback_lon = -74.0060
    return render_template(
        'onboarding.html',
        fallback_lat=fallback_lat,
        fallback_lon=fallback_lon
    )


# ---------------------------------------------------------------------------
# TRIAL DETAIL PAGE
# ---------------------------------------------------------------------------
@app.route('/trial/<nct_id>')
def trial_detail(nct_id):
    """Full trial detail page: eligibility, map of locations, AI match button."""
    db = get_mongo_db()
    trial = db['trials'].find_one({'nctId': nct_id})
    if trial:
        trial['id'] = str(trial.pop('_id', ''))
        # Normalise field names consistently
        trial['nctId']  = trial.get('nctId') or nct_id
        trial['title']  = trial.get('title') or trial.get('brief_title') or trial.get('briefTitle') or 'Unknown Title'
        trial['status'] = trial.get('status') or trial.get('overall_status') or trial.get('overallStatus') or 'UNKNOWN'
        trial['conditions'] = trial.get('conditions') or trial.get('conditions_str') or ''
        # Normalise locations
        normalised = []
        for loc in trial.get('locations', []):
            gp = loc.get('geoPoint') or {}
            lat = gp.get('lat') or loc.get('lat')
            lon = gp.get('lon') or loc.get('lon')
            if lat and lon:
                normalised.append({**loc, 'geoPoint': {'lat': float(lat), 'lon': float(lon)}, 'lat': float(lat), 'lon': float(lon)})
        trial['locations'] = normalised
        # Try to get eligibility text for display
        try:
            trial['eligibility_text'] = fetch_trial_eligibility_text(nct_id)
        except Exception:
            trial['eligibility_text'] = None
    else:
        # Fallback: fetch from ClinicalTrials.gov API
        try:
            r = requests.get(
                f'https://clinicaltrials.gov/api/v2/studies/{nct_id}',
                params={'format': 'json'},
                timeout=8
            )
            raw = r.json()
            proto = raw.get('protocolSection', {})
            id_mod   = proto.get('identificationModule', {})
            status_mod = proto.get('statusModule', {})
            cond_mod = proto.get('conditionsModule', {})
            elig_mod = proto.get('eligibilityModule', {})
            contacts_mod = proto.get('contactsLocationsModule', {})
            interv_mod   = proto.get('armsInterventionsModule', {})
            desc_mod     = proto.get('descriptionModule', {})
            trial = {
                'nctId':    nct_id,
                'title':    id_mod.get('briefTitle', 'Unknown Title'),
                'status':   status_mod.get('overallStatus', 'UNKNOWN'),
                'conditions': ', '.join(cond_mod.get('conditions', [])),
                'phase':    ', '.join(proto.get('designModule', {}).get('phases', [])),
                'sponsor':  proto.get('sponsorCollaboratorsModule', {}).get('leadSponsor', {}).get('name', ''),
                'eligibility_text': elig_mod.get('eligibilityCriteria', ''),
                'interventions': [
                    iv.get('name', '') for iv in interv_mod.get('interventions', [])
                ],
                'contacts': [
                    {'name': c.get('name'), 'email': c.get('email'), 'phone': c.get('phone')}
                    for c in contacts_mod.get('centralContacts', [])
                ],
                'locations': [
                    {
                        'facility': l.get('facility', {}).get('name', ''),
                        'city':    l.get('geoPoint', {}).get('lat') and l.get('facility', {}).get('address', {}).get('city', ''),
                        'country': l.get('facility', {}).get('address', {}).get('country', ''),
                        'status':  l.get('status', ''),
                        'lat':     l.get('geoPoint', {}).get('lat'),
                        'lon':     l.get('geoPoint', {}).get('lng'),
                        'geoPoint': {
                            'lat': l.get('geoPoint', {}).get('lat'),
                            'lon': l.get('geoPoint', {}).get('lng')
                        }
                    }
                    for l in contacts_mod.get('locations', [])
                    if l.get('geoPoint')
                ]
            }
        except Exception as e:
            print(f'trial_detail fetch error: {e}')
            abort(404)
    return render_template('trial_detail.html', trial=trial)


# ---------------------------------------------------------------------------
# STATS API  (MongoDB aggregation showcase)
# ---------------------------------------------------------------------------
@app.route('/api/stats')
def api_stats():
    """Live platform stats powered by MongoDB aggregation pipeline."""
    try:
        db = get_mongo_db()
        total_trials = db['trials'].count_documents({})
        recruiting   = db['trials'].count_documents({'status': {'$regex': 'RECRUITING', '$options': 'i'}})
        conditions_count = len(db['trials'].distinct('conditions_str'))

        # Top 5 conditions by trial count
        pipeline = [
            {'$group': {'_id': '$conditions_str', 'count': {'$sum': 1}}},
            {'$sort': {'count': -1}},
            {'$limit': 5}
        ]
        top_conditions = list(db['trials'].aggregate(pipeline))

        return jsonify({
            'total_trials':     total_trials,
            'recruiting':       recruiting,
            'conditions_covered': conditions_count,
            'top_conditions':   [{'condition': c['_id'], 'count': c['count']} for c in top_conditions]
        })
    except Exception as e:
        print(f'api_stats error: {e}')
        return jsonify({'error': 'Stats unavailable'}), 500


@app.route('/api/openapi.json')
def openapi_spec():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "TrialConnect API", "version": "1.0.0", "description": "AI-powered clinical trial search and eligibility matching API."},
        "servers": [{"url": "https://trialconnect-404183020569.us-central1.run.app"}],
        "paths": {
            "/api/search": {
                "get": {
                    "operationId": "searchTrials",
                    "summary": "Search for clinical trials by condition and location",
                    "parameters": [
                        {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "lat", "in": "query", "required": True, "schema": {"type": "number"}},
                        {"name": "lon", "in": "query", "required": True, "schema": {"type": "number"}}
                    ],
                    "responses": {"200": {"description": "List of matching trials"}}
                }
            },
            "/api/agent_chat": {
                "post": {
                    "operationId": "agentChat",
                    "summary": "Chat with TrialConnect AI agent (requires login)",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"message": {"type": "string"}, "context": {"type": "object"}}}}}},
                    "responses": {"200": {"description": "Agent reply"}, "401": {"description": "Login required"}}
                }
            },
            "/api/check_match/{nct_id}": {
                "get": {
                    "operationId": "checkMatch",
                    "summary": "Check patient eligibility for a trial (requires login)",
                    "parameters": [{"name": "nct_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Eligibility result"}, "401": {"description": "Login required"}}
                }
            }
        }
    }
    return jsonify(spec)


@app.route('/api/search')
def api_search():
    query = request.args.get('query')
    user_lat = request.args.get('lat', type=float)
    user_lon = request.args.get('lon', type=float)
    if not query:
        return jsonify({"error": "A query is required."}), 400
    if not user_lat or not user_lon:
        return jsonify({"error": "A location is required."}), 400
    try:
        search_results_raw = search_trials_mongo(query, user_lat, user_lon, radius_km=200)
        processed_studies = []
        patient_location = {"location": {"lat": user_lat, "lon": user_lon}}
        for study in search_results_raw:
            study['nctId'] = study.get('nctId') or study.get('nct_id')
            study['title'] = study.get('title') or study.get('brief_title') or study.get('briefTitle') or 'Unknown Title'
            study['status'] = study.get('status') or study.get('overall_status') or study.get('overallStatus') or 'UNKNOWN'
            study['conditions'] = study.get('conditions') or study.get('conditions_str') or ''
            study['interventions'] = study.get('interventions') or []

            normalised_locations = []
            for location in study.get('locations', []):
                gp = location.get('geoPoint') or {}
                lat = gp.get('lat') or location.get('lat')
                lon = gp.get('lon') or location.get('lon')
                if lat and lon:
                    normalised_locations.append({
                        **location,
                        'geoPoint': {'lat': float(lat), 'lon': float(lon)},
                        'lat': float(lat),
                        'lon': float(lon)
                    })
            study['locations'] = normalised_locations

            min_distance = float('inf')
            for loc in normalised_locations:
                dist = haversine(user_lat, user_lon, loc['lat'], loc['lon'])
                if dist < min_distance:
                    min_distance = dist

            if min_distance != float('inf'):
                study['closest_distance_km'] = round(min_distance)
                study['score'] = score_trial(study, patient_location)
                processed_studies.append(study)

        promoted_set = get_all_promoted_studies_set()
        promoted_list = []
        regular_list = []
        for study in processed_studies:
            if study.get('nctId') in promoted_set:
                study['is_promoted'] = True
                promoted_list.append(study)
                log_promotion_analytic(study['nctId'], query)
            else:
                study['is_promoted'] = False
                regular_list.append(study)
        promoted_list.sort(key=lambda x: x.get('score', 0), reverse=True)
        regular_list.sort(key=lambda x: x.get('score', 0), reverse=True)
        return jsonify(promoted_list + regular_list)
    except Exception as e:
        print(f"Error in api_search: {e}")
        return jsonify({"error": "An error occurred while searching."}), 500


@app.route('/api/upload_profile', methods=['POST'])
def api_upload_profile():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided."}), 400
    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"error": "No file selected."}), 400
    allowed_upload_types = {'pdf', 'png', 'jpg', 'jpeg'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed_upload_types:
        return jsonify({"error": f"File type .{ext} not supported. Use PDF, PNG, or JPG."}), 400
    mime_map = {'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}
    try:
        file_bytes = file.read()
        mime_type = mime_map[ext]
        profile = extract_patient_profile_from_document(file_bytes, mime_type)
        return jsonify({"status": "ok", "patient_profile": profile})
    except Exception as e:
        print(f"Document extraction error: {e}")
        return jsonify({"error": "Failed to extract profile from document."}), 500


@app.route('/api/apply_medical_profile', methods=['POST'])
def api_apply_medical_profile():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided."}), 400

    # Save to session always (works for anonymous onboarding too)
    session['medical_profile'] = data
    session.modified = True

    # Persist to MongoDB only if logged in
    if session.get('user'):
        user_id = session['user']['id']
        try:
            save_patient_dossier(user_id, data)
        except Exception as e:
            print(f"Warning: could not persist dossier to MongoDB: {e}")

    return jsonify({"status": "ok", "message": "Medical profile saved."})


@app.route('/api/check_match/<nct_id>', methods=['GET', 'POST'])
def api_check_match(nct_id):
    if not session.get('user'):
        return jsonify({"status": "NOT_LOGGED_IN"}), 401
    if not nct_id:
        return jsonify({"error": "NCT ID is required."}), 400
    user_data = session.get('user')
    patient_profile = {}
    if user_data.get('birthYear'):
        patient_profile['age'] = datetime.now().year - int(user_data['birthYear'])
    if user_data.get('sex'):
        patient_profile['sex'] = user_data['sex'].upper()

    session_medical = session.get('medical_profile')
    if not session_medical:
        try:
            session_medical = load_patient_dossier(user_data['id'])
            if session_medical:
                session['medical_profile'] = session_medical
                session.modified = True
        except Exception as e:
            print(f"Warning: could not load dossier from MongoDB: {e}")
    if session_medical:
        patient_profile.update(session_medical)

    enriched = {}
    if request.method == 'POST':
        enriched = request.get_json(silent=True) or {}
        patient_profile.update(enriched)
    has_enriched_data = any(
        k in patient_profile for k in ('diagnosis', 'labs', 'prior_treatments', 'comorbidities', 'condition')
    )
    if not has_enriched_data:
        if not patient_profile.get('age') or not patient_profile.get('sex'):
            return jsonify({"status": "NO_DATA", "reason": "User profile is incomplete."})
        return jsonify(check_user_study_match(user_data, nct_id))
    try:
        eligibility_text = fetch_trial_eligibility_text(nct_id)
        if not eligibility_text:
            return jsonify(check_user_study_match(user_data, nct_id))
        result = gemini_eligibility_check(patient_profile, eligibility_text, nct_id)
        return jsonify(result)
    except Exception as e:
        print(f"Match check error for {nct_id}: {e}")
        return jsonify({"error": "Match check failed."}), 500


# --- AI Chat Agent Endpoint ---
@app.route('/api/agent_chat', methods=['POST'])
def api_agent_chat():
    if not session.get('user'):
        return jsonify({'reply': 'Please log in to use the AI assistant.'}), 401

    body = request.get_json(silent=True) or {}
    user_message = body.get('message', '').strip()
    context = body.get('context')

    if not user_message:
        return jsonify({'reply': 'Please ask me a question.'}), 400

    full_message = user_message
    if context and context.get('trials'):
        trials_lines = [
            f"#{t.get('rank')} [{t.get('nctId')}] {t.get('briefTitle')} "
            f"| {t.get('overallStatus')} | {t.get('closest_distance_km', '?')}km "
            f"| Eligibility: {t.get('eligibility_status', 'not checked')}"
            + (f" — {t['eligibility_reason']}" if t.get('eligibility_reason') else "")
            for t in context['trials']
        ]
        context_block = (
            f"[Search context: query=\"{context.get('query')}\", "
            f"location={context.get('location')}\n"
            + "\n".join(trials_lines) + "]"
        )
        full_message = f"{context_block}\n\n{user_message}"

    agent_session_id = session.get('agent_session_id')
    if not agent_session_id:
        agent_session_id = str(uuid.uuid4())
        session['agent_session_id'] = agent_session_id
        session.modified = True

    try:
        from google.genai import types as gentypes

        runner = getattr(current_app, 'agent_runner', None)
        session_service = getattr(current_app, 'agent_session_service', None)

        if runner is None:
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from agent import root_agent
            session_service = InMemorySessionService()
            runner = Runner(
                agent=root_agent,
                app_name='trialconnect',
                session_service=session_service
            )

        import asyncio
        import nest_asyncio
        nest_asyncio.apply()

        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(
                session_service.create_session(
                    app_name='trialconnect',
                    user_id='user',
                    session_id=agent_session_id
                )
            )
        except Exception:
            pass

        reply = ''
        for event in runner.run(
            user_id='user',
            session_id=agent_session_id,
            new_message=gentypes.Content(
                role='user',
                parts=[gentypes.Part(text=full_message)]
            )
        ):
            if event.is_final_response() and event.content:
                reply = event.content.parts[0].text
                break

        return jsonify({'reply': reply or 'No response from agent.'})
    except Exception as e:
        print(f"Agent chat error: {e}")
        return jsonify({'reply': 'Sorry, I had trouble connecting to the AI agent. Please try again in a moment.'}), 500


# --- Static Page Routes ---
@app.route('/about')
def about():
    return render_template("about.html")

@app.route('/faq')
def faq():
    return render_template("faq.html")

@app.route('/terms')
def terms():
    return render_template("terms.html")

@app.route('/privacy')
def privacy():
    return render_template("privacy.html")

@app.route('/thank_you')
def thank_you():
    return render_template("thank_you.html")

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    error = None
    if request.method == 'POST':
        firstName = request.form.get('firstName')
        lastName = request.form.get('lastName')
        email = request.form.get('email')
        message = request.form.get('message')
        if not all([firstName, lastName, email, message]):
            error = "All fields are required. Please fill out the entire form."
        else:
            add_contact_message(firstName, lastName, email, message)
            return redirect(url_for('thank_you'))
    return render_template("contact.html", error=error)


# --- Authentication Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('rememberMe') == 'on'
        user = get_user_by_email(email)
        if user and check_password_hash(user['password_hash'], password):
            session['user'] = user
            try:
                dossier = load_patient_dossier(user['id'])
                if dossier:
                    session['medical_profile'] = dossier
            except Exception as e:
                print(f"Warning: could not restore dossier on login: {e}")
            flash('You have been logged in successfully.', 'success')
            response = make_response(redirect(url_for('index')))
            if remember:
                response.set_cookie('remember_token', user['remember_token'], max_age=30*24*60*60)
            return response
        else:
            flash('Invalid email or password. Please try again.', 'danger')
    return render_template("login.html")


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        first_name = request.form.get('firstName')
        last_name = request.form.get('lastName')
        birthYear = request.form.get('birthYear')
        sex = request.form.get('sex')
        agreement_agreed = request.form.get('agreement')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        existing_user = get_user_by_email(email)
        if existing_user:
            if check_password_hash(existing_user['password_hash'], password):
                session['user'] = existing_user
                flash('Welcome back! You have been logged in successfully.', 'success')
                return redirect(url_for('index'))
            else:
                message = 'An account with this email already exists. Please <a href="/login" class="alert-link">log in</a> or reset your password.'
                flash(message, 'warning')
                return redirect(url_for('login'))
        strength_errors = validate_password_strength(password)
        if strength_errors:
            for error in strength_errors:
                flash(error, 'danger')
            return render_template('register.html')
        if password != confirm_password:
            flash("Passwords do not match. Please try again.", 'danger')
            return render_template('register.html')
        if not agreement_agreed:
            flash("You must agree to the Terms and Privacy Policy to register.", "danger")
            return render_template('register.html', now=datetime.now())
        user = get_or_create_user(
            email=email, firstName=first_name, lastName=last_name,
            birthYear=birthYear, sex=sex, password=password, auth_provider='local'
        )
        session['user'] = user
        return redirect(url_for('index'))
    return render_template("register.html", now=datetime.now())


@app.route('/login/google')
def login_google():
    redirect_uri = url_for('authorize', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route('/authorize')
def authorize():
    token = oauth.google.authorize_access_token()
    user_info = oauth.google.userinfo()
    first_name = user_info.get('given_name')
    last_name = user_info.get('family_name')
    if not first_name or not last_name:
        full_name = user_info.get('name', '').split(' ', 1)
        first_name = full_name[0]
        last_name = full_name[1] if len(full_name) > 1 else ''
    user = get_or_create_user(
        email=user_info['email'], firstName=first_name, lastName=last_name,
        auth_provider='google', provider_id=user_info['sub'],
        profile_picture_url=user_info.get('picture')
    )
    session['user'] = user
    try:
        dossier = load_patient_dossier(user['id'])
        if dossier:
            session['medical_profile'] = dossier
    except Exception as e:
        print(f"Warning: could not restore dossier on Google login: {e}")
    return redirect(url_for('index'))


@app.route('/logout')
def logout():
    session.pop('user', None)
    response = make_response(redirect(url_for('index')))
    response.set_cookie('remember_token', '', expires=0)
    return response


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = get_user_by_email(email)
        if user:
            otp = ''.join(secrets.choice('0123456789') for i in range(6))
            session[f'otp_for_{email}'] = otp
            otp_hash = generate_password_hash(otp)
            expiration = datetime.utcnow() + timedelta(minutes=10)
            db = get_mongo_db()
            db['users'].update_one(
                {'email': email},
                {'$set': {'reset_token': otp_hash, 'reset_token_expiration': expiration}}
            )
        flash("If an account with that email exists, a password reset code has been sent.", "success")
        return redirect(url_for('index'))
    return render_template("forgot_password.html")


@app.route('/reset_with_token', methods=['GET', 'POST'])
def reset_with_token():
    if request.method == 'POST':
        email = request.form.get('email')
        otp = request.form.get('otp')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        user = get_user_by_email(email)
        if not user or not user.get('reset_token') or not check_password_hash(user['reset_token'], otp):
            flash("Invalid email or reset code.", "danger")
            return redirect(url_for('reset_with_token'))
        expiration_time = user['reset_token_expiration']
        if isinstance(expiration_time, str):
            expiration_time = datetime.fromisoformat(expiration_time).replace(tzinfo=timezone.utc)
        elif expiration_time.tzinfo is None:
            expiration_time = expiration_time.replace(tzinfo=timezone.utc)
        if expiration_time < datetime.now(timezone.utc):
            flash("The reset code has expired. Please request a new one.", "danger")
            session.pop(f'otp_for_{email}', None)
            return redirect(url_for('forgot_password'))
        strength_errors = validate_password_strength(new_password)
        if strength_errors:
            for error in strength_errors:
                flash(error, 'danger')
            return redirect(url_for('reset_with_token'))
        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('reset_with_token'))
        new_password_hash = generate_password_hash(new_password)
        db = get_mongo_db()
        db['users'].update_one(
            {'email': email},
            {'$set': {'password_hash': new_password_hash, 'reset_token': None, 'reset_token_expiration': None}}
        )
        session.pop(f'otp_for_{email}', None)
        flash("Your password has been successfully reset. Please log in.", "success")
        return redirect(url_for('login'))
    return render_template("reset_with_token.html")


@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user' not in session:
        flash("You must be logged in to view this page.", "warning")
        return redirect(url_for('login'))
    user_id = session['user']['id']
    if request.method == 'POST':
        db = get_mongo_db()
        firstName = request.form.get('firstName')
        lastName = request.form.get('lastName')
        birthYear = request.form.get('birthYear')
        sex = request.form.get('sex')
        update_fields = {'firstName': firstName, 'lastName': lastName, 'birthYear': birthYear, 'sex': sex}
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = str(uuid.uuid4()) + "_" + filename
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(filepath)
                update_fields['profile_picture_url'] = url_for('static', filename=f'profile_pictures/{unique_filename}')
        new_password = request.form.get('new_password')
        if new_password:
            current_password = request.form.get('current_password')
            confirm_password = request.form.get('confirm_password')
            user = get_user_by_id(user_id)
            if not user or not user.get('password_hash') or not check_password_hash(user['password_hash'], current_password):
                flash("Your current password was incorrect.", "danger")
            elif validate_password_strength(new_password):
                flash("Your new password does not meet the strength requirements.", "danger")
            elif new_password != confirm_password:
                flash("The new passwords do not match.", "danger")
            else:
                update_fields['password_hash'] = generate_password_hash(new_password)
                flash("Your password was updated successfully!", "success")
        db['users'].update_one({'_id': ObjectId(user_id)}, {'$set': update_fields})
        session['user'] = get_user_by_id(user_id)
        password_flashed = any(
            cat in ['success', 'danger']
            for cat, msg in (get_flashed_messages(with_categories=True) or [])
            if "password" in msg.lower()
        )
        if not password_flashed:
            flash("Your profile has been saved.", "success")
        return redirect(url_for('profile'))
    user_data = get_user_by_id(user_id)
    return render_template("profile.html", user=user_data, now=datetime.now(timezone.utc))


@app.route('/delete_account', methods=['POST'])
def delete_account():
    if 'user' not in session:
        abort(403)
    user_id = session['user']['id']
    admin_email = os.environ.get('ADMIN_EMAIL', '')
    if admin_email and session['user'].get('email') == admin_email:
        flash("The primary admin account cannot be deleted.", "danger")
        return redirect(url_for('profile'))
    db = get_mongo_db()
    db['users'].delete_one({'_id': ObjectId(user_id)})
    db['patient_dossiers'].delete_one({'user_id': user_id})
    session.clear()
    flash("Your account has been permanently deleted.", "success")
    return redirect(url_for('index'))


# --- Admin Routes ---
@app.route('/admin')
def admin():
    if not _is_admin():
        abort(403)
    db = get_mongo_db()
    users_raw = list(db['users'].find({}).sort('created_at', -1))
    contacts = list(db['contacts'].find({}).sort('_id', -1))
    promoted_list_raw = get_all_promoted_studies()
    promoted_list = []
    for study in promoted_list_raw:
        s = dict(study)
        if isinstance(s.get('added_at'), str):
            try:
                s['added_at'] = datetime.strptime(s['added_at'], '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                s['added_at'] = datetime.now(timezone.utc)
        promoted_list.append(s)
    processed_users = []
    for user_doc in users_raw:
        user = dict(user_doc)
        user['id'] = str(user.pop('_id'))
        user['token_status'] = 'none'
        exp = user.get('reset_token_expiration')
        if exp:
            try:
                if isinstance(exp, str):
                    exp = datetime.fromisoformat(exp)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > datetime.now(timezone.utc):
                    user['token_status'] = 'valid'
                    user['formatted_expiration'] = exp.strftime('%H:%M:%S UTC')
                else:
                    user['token_status'] = 'expired'
            except (ValueError, TypeError):
                user['token_status'] = 'error'
        processed_users.append(user)
    for c in contacts:
        c['id'] = str(c.pop('_id'))
    return render_template("admin.html", users=processed_users, contacts=contacts, promoted_list=promoted_list)


@app.route('/admin/delete_user/<user_id>', methods=['POST'])
def delete_user(user_id):
    if not _is_admin():
        abort(403)
    db = get_mongo_db()
    admin_email = os.environ.get('ADMIN_EMAIL', '')
    try:
        user_to_delete = db['users'].find_one({'_id': ObjectId(user_id)})
    except Exception:
        abort(400)
    if user_to_delete and admin_email and user_to_delete.get('email') == admin_email:
        flash("You cannot delete the primary admin account.", "danger")
        return redirect(url_for('admin'))
    db['users'].delete_one({'_id': ObjectId(user_id)})
    flash("User has been successfully deleted.", "success")
    return redirect(url_for('admin'))


@app.route('/admin/delete_contact/<contact_id>', methods=['POST'])
def delete_contact(contact_id):
    if not _is_admin():
        abort(403)
    db = get_mongo_db()
    try:
        db['contacts'].delete_one({'_id': ObjectId(contact_id)})
    except Exception:
        abort(400)
    flash("Contact message has been successfully deleted.", "success")
    return redirect(url_for('admin'))


@app.route('/admin/clear_data', methods=['POST'])
def clear_data():
    if not _is_admin():
        abort(403)
    db = get_mongo_db()
    admin_email = os.environ.get('ADMIN_EMAIL', '')
    db['users'].delete_many({'email': {'$ne': admin_email}})
    db['contacts'].delete_many({})
    flash("All user and contact entries (except for the admin account) have been successfully deleted.", "success")
    return redirect(url_for('admin'))


@app.route("/admin/promote/add", methods=["POST"])
def add_promotion():
    if not _is_admin():
        abort(403)
    nct_id = request.form.get("nct_id")
    if nct_id:
        add_promoted_study(nct_id)
        flash(f"{nct_id} added to promotions.", "success")
    else:
        flash("NCT ID cannot be empty.", "danger")
    return redirect(url_for('admin'))


@app.route("/admin/promote/remove/<nct_id>", methods=["POST"])
def remove_promotion(nct_id):
    if not _is_admin():
        abort(403)
    remove_promoted_study(nct_id)
    flash(f"{nct_id} removed from promotions.", "success")
    return redirect(url_for('admin'))
