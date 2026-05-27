import datetime
import os, re, math
from werkzeug.security import generate_password_hash
from flask import g, current_app
import secrets, requests, warnings, json
from pymongo import MongoClient, ASCENDING, TEXT
from pymongo.errors import DuplicateKeyError, PyMongoError
import vertexai
from vertexai.language_models import TextEmbeddingModel
from google.api_core.exceptions import GoogleAPIError
from google.genai.types import GenerateContentResponse
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

# Suppress residual SSL warnings globally
# WARNING: Suppressing SSL warnings is NOT recommended for production environments.
# Ensure proper SSL certificate validation in a deployed application.
# warnings.filterwarnings('ignore', message='Unverified HTTPS request')


ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Constants for search and scoring
DEFAULT_SEARCH_RADIUS_KM = 300
DEFAULT_SEARCH_PAGE_SIZE = 100 # For ClinicalTrials.gov API and MongoDB text search fallback
VECTOR_SEARCH_NUM_CANDIDATES = 300
VECTOR_SEARCH_LIMIT = 150
DISTANCE_DECAY_FACTOR = 300 # Used in score_trial haversine calculation

# =============================================================================
# MongoDB — single shared client per app context
# =============================================================================

def get_mongo_db():
    if 'mongo_db' not in g:
        from flask import current_app
        uri = current_app.config.get('MONGODB_URI') or os.environ.get('MONGODB_URI')
        if not uri:
            raise RuntimeError("MONGODB_URI is not set in environment variables.")
        client = MongoClient(uri)
        g.mongo_client = client
        g.mongo_db = client['trialconnect']
    return g.mongo_db


def close_db(e=None):
    client = g.pop('mongo_client', None)
    if client is not None:
        client.close()


def init_db():
    """Create MongoDB indexes on startup (idempotent)."""
    try:
        db = _get_db_direct()
        db['users'].create_index('email', unique=True)
        db['users'].create_index('remember_token')
        db['contacts'].create_index([('created_at', ASCENDING)])
        db['promoted_studies'].create_index('nct_id', unique=True)
        db['promotion_analytics'].create_index('nct_id')
        db['patient_dossiers'].create_index('user_id', unique=True)
        existing_indexes = db['trials'].index_information()
        for idx_name in list(existing_indexes.keys()):
            if idx_name not in ('_id_',) and 'text' in str(existing_indexes[idx_name].get('key', {})):
                try:
                    db['trials'].drop_index(idx_name)
                    print(f"Dropped conflicting text index: {idx_name}")
                except Exception:
                    pass
        db['trials'].create_index(
            [('brief_title', TEXT), ('conditions_str', TEXT), ('eligibility_criteria', TEXT)],
            name='trial_text_index',
            weights={'brief_title': 10, 'conditions_str': 8, 'eligibility_criteria': 2},
            default_language='english'
        )
        print("MongoDB indexes initialized successfully.")
    except Exception as e:
        print(f"MongoDB index init warning: {e}")


def _get_db_direct():
    """Direct connection outside Flask app context (used by init_db at startup)."""
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        raise RuntimeError("MONGODB_URI is not set.")
    client = MongoClient(uri)
    return client['trialconnect']


# =============================================================================
# Contacts
# =============================================================================

def add_contact_message(firstName, lastName, email, message):
    db = get_mongo_db()
    db['contacts'].insert_one({
        'firstName': firstName,
        'lastName': lastName,
        'email': email,
        'message': message,
        'created_at': datetime.datetime.utcnow()
    })


# =============================================================================
# Users
# =============================================================================

def _user_doc_to_dict(doc):
    if doc is None:
        return None
    d = dict(doc)
    d['id'] = str(d.pop('_id'))
    return d


def get_or_create_user(email, firstName, lastName, birthYear=None, sex=None,
                       auth_provider='local', profile_picture_url=None,
                       provider_id=None, password=None):
    db = get_mongo_db()
    existing = db['users'].find_one({'email': email})
    if existing:
        return _user_doc_to_dict(existing)
    password_hash = generate_password_hash(password) if password else None
    remember_token = secrets.token_hex(16)
    doc = {
        'email': email,
        'firstName': firstName,
        'lastName': lastName,
        'birthYear': birthYear,
        'sex': sex,
        'password_hash': password_hash,
        'auth_provider': auth_provider,
        'provider_id': provider_id,
        'profile_picture_url': profile_picture_url,
        'remember_token': remember_token,
        'created_at': datetime.datetime.utcnow(),
        'reset_token': None,
        'reset_token_expiration': None
    }
    result = db['users'].insert_one(doc)
    created = db['users'].find_one({'_id': result.inserted_id})
    return _user_doc_to_dict(created)


def get_user_by_email(email):
    db = get_mongo_db()
    return _user_doc_to_dict(db['users'].find_one({'email': email}))


def get_user_by_remember_token(token):
    db = get_mongo_db()
    return _user_doc_to_dict(db['users'].find_one({'remember_token': token}))


def get_user_by_id(user_id):
    db = get_mongo_db()
    from bson import ObjectId
    try:
        oid = ObjectId(user_id)
    except Exception:
        return None
    return _user_doc_to_dict(db['users'].find_one({'_id': oid}))


def validate_password_strength(password):
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password):
        errors.append("Password must contain at least one number.")
    return errors


def generate_reset_token(email):
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='password-reset-salt')


def verify_reset_token(token, expiration=3600): # 1 hour default expiration
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=expiration)
    except SignatureExpired:
        return None # Token is expired
    except BadTimeSignature:
        return None # Token is invalid
    return email


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_image_by_magic_bytes(file_stream):
    file_stream.seek(0) # Go to the beginning of the file
    header = file_stream.read(8)
    file_stream.seek(0) # Reset stream position for subsequent reads

    # GIF (GIF87a, GIF89a)
    if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
        return True
    # PNG
    if header.startswith(b'\x89PNG\x0D\x0A\x1A\x0A'):
        return True
    # JPEG (various magic bytes)
    if header.startswith(b'\xFF\xD8\xFF'):
        return True

    return False


# =============================================================================
# Patient Dossier — persistent MongoDB storage
# =============================================================================

def save_patient_dossier(user_id, profile_data):
    """Upsert the patient dossier for a given user into MongoDB."""
    db = get_mongo_db()
    db['patient_dossiers'].update_one(
        {'user_id': user_id},
        {'$set': {
            'user_id': user_id,
            'profile': profile_data,
            'updated_at': datetime.datetime.utcnow()
        }},
        upsert=True
    )


def load_patient_dossier(user_id):
    """Load the patient dossier for a given user from MongoDB.
    Returns the profile dict, or an empty dict if none exists.
    """
    db = get_mongo_db()
    doc = db['patient_dossiers'].find_one({'user_id': user_id}, {'_id': 0, 'profile': 1})
    return doc.get('profile', {}) if doc else {}


# =============================================================================
# Promoted Studies
# =============================================================================

def get_all_promoted_studies():
    db = get_mongo_db()
    docs = list(db['promoted_studies'].find({}, {'_id': 0}).sort('added_at', -1))
    return docs


def get_all_promoted_studies_set():
    db = get_mongo_db()
    return {doc['nct_id'] for doc in db['promoted_studies'].find({}, {'nct_id': 1, '_id': 0})}


def add_promoted_study(nct_id):
    db = get_mongo_db()
    try:
        db['promoted_studies'].insert_one({
            'nct_id': nct_id.strip().upper(),
            'added_at': datetime.datetime.utcnow()
        })
    except DuplicateKeyError:
        pass


def remove_promoted_study(nct_id):
    db = get_mongo_db()
    db['promoted_studies'].delete_one({'nct_id': nct_id})


def log_promotion_analytic(nct_id, search_term):
    db = get_mongo_db()
    db['promotion_analytics'].insert_one({
        'nct_id': nct_id,
        'search_term': search_term,
        'view_date': datetime.datetime.utcnow()
    })


# =============================================================================
# Geolocation & Scoring
# =============================================================================

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def get_location_from_ip(ip_address):
    if ip_address == '127.0.0.1':
        ip_address = '8.8.8.8'
    try:
        response = requests.get(f"http://ip-api.com/json/{ip_address}?fields=status,lat,lon")
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'success':
            return data.get('lat'), data.get('lon')
    except requests.exceptions.RequestException:
        return None, None
    return None, None


def score_trial(trial, patient_profile):
    vector_score = float(trial.get("vector_score", 0.5))
    distance_score = 0.5
    try:
        user_lat = patient_profile["location"]["lat"]
        user_lon = patient_profile["location"]["lon"]
        distances = [
            haversine(user_lat, user_lon, loc["lat"], loc["lon"])
            for loc in trial.get("locations", [])
            if loc.get("lat") and loc.get("lon")
        ]
        if distances:
            distance_score = math.exp(-min(distances) / DISTANCE_DECAY_FACTOR)
    except (KeyError, TypeError):
        pass
    return round((0.70 * vector_score) + (0.30 * distance_score), 4)


# =============================================================================
# Search ranking boost — applied after vector search and text fallback
# =============================================================================

def _condition_boost(query, doc):
    """Return a ranking boost score based on how well the query matches
    the trial's title and condition fields (lexical, not semantic).

    Boosts:
      +1.0  — exact query phrase in title or conditions_str
      +0.50 — all significant query tokens found in title or conditions_str
      +0.25 — majority of significant query tokens found
      +0.0  — no meaningful overlap

    This ensures 'lung cancer' surfaces NSCLC/SCLC/lung adenocarcinoma
    studies above generic oncology trials that happen to mention cancer.
    """
    if not query:
        return 0.0
    q = query.lower().strip()
    # Haystack: only title + conditions (not full eligibility text — too noisy)
    haystack = ' '.join([
        str(doc.get('title', '')),
        str(doc.get('conditions', ''))
    ]).lower()

    if q in haystack:
        return 1.0

    # Tokenise: only tokens >3 chars to skip stop words
    tokens = [t for t in re.split(r'[\s,/\-]+', q) if len(t) > 3]
    if not tokens:
        return 0.0
    matches = sum(1 for tok in tokens if tok in haystack)
    ratio = matches / len(tokens)
    if ratio >= 1.0:
        return 0.50
    if ratio >= 0.5:
        return 0.25
    return 0.0


# =============================================================================
# ClinicalTrials.gov API (fallback when MongoDB unavailable)
# =============================================================================

def search_clinical_trials(query, user_lat, user_lon, radius=100, unit="km"):
    """Fallback: search CT.gov directly.
    Uses query.cond for condition-first precision, then retries with
    query.term if no condition results are found.
    """
    base_url = "https://clinicaltrials.gov/api/v2/studies"
    safe_unit = "mi" if str(unit).lower() == "mi" else "km"
    try:
        safe_radius = int(radius)
    except (ValueError, TypeError):
        safe_radius = 200
    geo_filter = f"distance({user_lat},{user_lon},{safe_radius}{safe_unit})"

    def _fetch(search_params):
        try:
            response = requests.get(base_url, params=search_params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            return {}

    # Try condition-specific search first
    params = {
        "query.cond": query,
        "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,AVAILABLE",
        "filter.geo": geo_filter,
        "pageSize": DEFAULT_SEARCH_PAGE_SIZE,
        "format": "json"
    }
    data = _fetch(params)

    # If no results, fall back to broad term search
    if not data.get('studies'):
        params_broad = dict(params)
        params_broad.pop('query.cond')
        params_broad['query.term'] = query
        params_broad['pageSize'] = 100
        data = _fetch(params_broad)

    formatted_trials = []
    for study in data.get('studies', []):
        protocol = study.get("protocolSection", {})
        id_module = protocol.get("identificationModule", {})
        status_module = protocol.get("statusModule", {})
        conditions_module = protocol.get("conditionsModule", {})
        interventions_module = protocol.get("armsInterventionsModule", {})
        locations_list = []
        for loc in protocol.get("contactsLocationsModule", {}).get("locations", []):
            location_data = {k: loc.get(k) for k in ["facility", "city", "state", "zip", "country", "geoPoint"]}
            locations_list.append({k: v for k, v in location_data.items() if v is not None})
        interventions_list = [
            {'name': i.get('interventionName', ''), 'type': i.get('interventionType', '')}
            for i in interventions_module.get('interventions', [])
        ]
        t = {
            "nctId": id_module.get("nctId"),
            "title": id_module.get("briefTitle"),
            "status": status_module.get("overallStatus"),
            "conditions": ", ".join(conditions_module.get("conditions", [])),
            "interventions": interventions_list,
            "locations": locations_list
        }
        t['score'] = _condition_boost(query, t)
        formatted_trials.append(t)

    formatted_trials.sort(key=lambda x: x.get('score', 0), reverse=True)
    return formatted_trials


# =============================================================================
# MongoDB Atlas Trial Search
# =============================================================================

def _get_genai_client():
    from google import genai
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GOOGLE_CLOUD_LOCATION') or 'us-central1'
    if location == 'global':
        location = 'us-central1'
    
    # Try Vertex AI first (if ADC is configured)
    try:
        adc_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        default_adc_path = os.path.expanduser('~/.config/gcloud/application_default_credentials.json')
        default_adc_path_win = os.path.expandvars('%APPDATA%/gcloud/application_default_credentials.json')
        if adc_path or os.path.exists(default_adc_path) or os.path.exists(default_adc_path_win):
            print(f"[GEMINI SETUP] Initializing Vertex AI client (project={project}, location={location}) using Application Default Credentials.")
            return genai.Client(vertexai=True, project=project, location=location)
    except Exception as e:
        print(f"[GEMINI SETUP] Vertex AI initialization check encountered error: {e}")
    
    # Fallback to standard Gemini API using the maps/general API key
    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_MAPS_API_KEY')
    if api_key:
        masked_key = api_key[:6] + "..." if len(api_key) > 6 else "None"
        print(f"[GEMINI SETUP] Falling back to Google AI Studio Gemini developer client using API Key: {masked_key}")
        return genai.Client(vertexai=False, api_key=api_key)
    
    print(f"[GEMINI SETUP] No local API key found. Attempting ultimate fallback to Vertex AI (project={project}, location={location}).")
    # Ultimate fallback
    return genai.Client(vertexai=True, project=project, location=location)


def get_text_embedding(text):
    """Generate a query embedding using text-embedding-005 (or text-embedding-004 for fallback)."""
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return None
    try:
        from google import genai
        from google.genai import types
        client = _get_genai_client()
        model_name = 'text-embedding-005'
        if not getattr(client, 'vertexai', True):
            model_name = 'text-embedding-004'
        result = client.models.embed_content(
            model=model_name,
            contents=text[:3072],
            config=types.EmbedContentConfig(task_type='RETRIEVAL_QUERY')
        )
        return result.embeddings[0].values
    except GoogleAPIError as e:
        print(f"Embedding generation failed due to Google API error: {e}")
        return None
    except Exception as e:
        print(f"Embedding generation failed due to unexpected error: {e}")
        return None


def search_trials_mongo(query, user_lat, user_lon, radius_km=DEFAULT_SEARCH_RADIUS_KM):
    """Search trials in MongoDB Atlas.

    Strategy (in order):
    1. Vector search using text-embedding-005 (same model as seed).
       Query vector = plain user query string (e.g. 'lung cancer').
       The indexed documents embed title+conditions+eligibility, so
       a query of 'lung cancer' will match documents where those fields
       are about lung cancer.
    2. After vector retrieval, apply a lexical condition boost:
       trials whose title/conditions contain the query phrase get ranked up.
       This fixes the problem where generic cancer studies rank above specific
       lung cancer ones.
    3. Fall back to MongoDB text search if no embedding available.
    4. Fall back to CT.gov API if MongoDB unavailable.
    """
    mongo_uri = os.environ.get('MONGODB_URI')
    if not mongo_uri:
        print("MONGODB_URI not set — falling back to ClinicalTrials.gov API")
        return search_clinical_trials(query, user_lat, user_lon, radius_km)
    try:
        db = get_mongo_db()
        collection = db['trials']
        query_embedding = get_text_embedding(query)
        if query_embedding:
            pipeline = [
                {"$vectorSearch": {
                    "index": "eligibility_vector_index",
                    "path": "eligibility_criteria_embedding",
                    "queryVector": query_embedding,
                    "numCandidates": VECTOR_SEARCH_NUM_CANDIDATES,
                    "limit": VECTOR_SEARCH_LIMIT
                }},
                {"$addFields": {"vector_score": {"$meta": "vectorSearchScore"}}},
                {"$match": {"overall_status": {"$in": ["RECRUITING", "NOT_YET_RECRUITING", "AVAILABLE"]}}},
                {"$project": {
                    "_id": 0, "nctId": "$nct_id", "title": "$brief_title",
                    "status": "$overall_status", "conditions": "$conditions_str",
                    "locations": "$locations", "eligibility_criteria": 1,
                    "interventions": 1, "vector_score": 1, "seed_conditions": 1
                }}
            ]
            results = list(collection.aggregate(pipeline))

            # Apply lexical condition boost on top of vector score
            for doc in results:
                base = float(doc.get('vector_score', 0))
                boost = _condition_boost(query, doc)
                doc['vector_score'] = base + boost

            results.sort(key=lambda x: x.get('vector_score', 0), reverse=True)
            return results

        else:
            # Text search fallback (weighted: title > conditions > eligibility)
            results = list(collection.find(
                {
                    "$text": {"$search": query},
                    "overall_status": {"$in": ["RECRUITING", "NOT_YET_RECRUITING", "AVAILABLE"]}
                },
                {
                    "_id": 0, "nctId": "$nct_id", "title": "$brief_title",
                    "status": "$overall_status", "conditions": "$conditions_str",
                    "locations": 1, "eligibility_criteria": 1, "interventions": 1,
                    "text_score": {"$meta": "textScore"}
                }
            ).sort([("text_score", {"$meta": "textScore"})]).limit(DEFAULT_SEARCH_PAGE_SIZE))

            for doc in results:
                doc['vector_score'] = float(doc.get('text_score', 0)) + _condition_boost(query, doc)

            results.sort(key=lambda x: x.get('vector_score', 0), reverse=True)
            return results

    except pymongo.errors.PyMongoError as e:
        print(f"MongoDB search failed: {e} — falling back to ClinicalTrials.gov API")
        return search_clinical_trials(query, user_lat, user_lon, radius_km)
    except Exception as e: # Catch any other unexpected errors
        print(f"Unexpected error during MongoDB search: {e} — falling back to ClinicalTrials.gov API")
        return search_clinical_trials(query, user_lat, user_lon, radius_km)


def fetch_trial_eligibility_text(nct_id):
    mongo_uri = os.environ.get('MONGODB_URI')
    if mongo_uri:
        try:
            db = get_mongo_db()
            trial = db['trials'].find_one({"nct_id": nct_id}, {"_id": 0, "eligibility_criteria": 1})
            if trial and trial.get("eligibility_criteria"):
                return trial["eligibility_criteria"]
        except pymongo.errors.PyMongoError as e:
            print(f"MongoDB eligibility fetch failed for {nct_id} due to database error: {e}")
        except Exception as e: # Catch other MongoDB related issues
            print(f"MongoDB eligibility fetch failed for {nct_id} due to unexpected error: {e}")
    try:
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}?fields=EligibilityModule"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        eligibility = response.json().get("protocolSection", {}).get("eligibilityModule", {})
        return eligibility.get("eligibilityCriteria")
    except requests.exceptions.RequestException as e:
        print(f"CT.gov eligibility fetch failed for {nct_id} due to network/API error: {e}")
        return None
    except Exception as e:
        print(f"CT.gov eligibility fetch failed for {nct_id} due to unexpected error: {e}")
        return None


# =============================================================================
# Eligibility Matching
# =============================================================================

def check_user_study_match(user_profile, nct_id):
    if not user_profile or not user_profile.get('birthYear') or not user_profile.get('sex'):
        return {'status': 'NO_DATA', 'reason': 'User profile incomplete.'}
    try:
        current_year = datetime.datetime.now().year
        user_age = current_year - int(user_profile['birthYear'])
        user_sex = user_profile['sex'].upper()
    except (TypeError, ValueError):
        return {'status': 'NO_DATA', 'reason': 'Invalid user profile data.'}
    try:
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}?fields=EligibilityModule"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        eligibility = response.json().get("protocolSection", {}).get("eligibilityModule", {})
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch eligibility for {nct_id}: {e}")
        return {'status': 'NO_DATA', 'reason': 'API fetch failed.'}
    if not eligibility:
        return {'status': 'NO_DATA', 'reason': 'No eligibility data available.'}
    study_sex = eligibility.get('sex', 'ALL').upper()
    is_sex_match = study_sex == 'ALL' or study_sex == user_sex
    min_age = int((re.search(r'\d+', eligibility.get('minimumAge', '0 Years')) or type('', (), {'group': lambda s, x: '0'})()).group(0))
    max_age = 150
    max_age_str = eligibility.get('maximumAge')
    if max_age_str:
        m = re.search(r'\d+', max_age_str)
        if m:
            max_age = int(m.group(0))
    is_age_match = min_age <= user_age <= max_age
    if is_age_match and is_sex_match:
        return {'status': 'MATCH', 'verdict': 'MATCH',
                'reason': f'Age ({user_age}) and sex ({user_sex}) match study criteria.'}
    reason = ""
    if not is_age_match:
        reason += f"Age ({user_age}) outside range ({min_age}-{max_age}). "
    if not is_sex_match:
        reason += f"Sex ({user_sex}) does not match study requirement ({study_sex})."
    return {'status': 'NO_MATCH', 'verdict': 'NO_MATCH', 'reason': reason.strip()}


# =============================================================================
# Gemini AI
# =============================================================================

def gemini_eligibility_check(patient_profile, eligibility_criteria_text, nct_id):
    import json
    import traceback
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    print(f"[GEMINI MATCH] Starting eligibility check for trial: {nct_id}")
    if not project:
        print("[GEMINI MATCH] Error: GOOGLE_CLOUD_PROJECT is not set.")
        return {
            "verdict": "UNKNOWN", "confidence": 0, "match_reasons": [],
            "exclusion_flags": [], "missing_info": ["Gemini not configured"],
            "plain_english_summary": "AI matching is not configured yet.",
            "status": "UNKNOWN", "reason": "AI matching is not configured yet."
        }
    try:
        from google import genai
        from google.genai import types
        client = _get_genai_client()
        prompt = f"""You are a clinical trial eligibility expert. Analyze whether this patient qualifies for a clinical trial.

PATIENT PROFILE:
{json.dumps(patient_profile, indent=2)}

TRIAL ELIGIBILITY CRITERIA:
{eligibility_criteria_text}

Respond ONLY with valid JSON:
{{
  "verdict": "MATCH or PARTIAL_MATCH or NO_MATCH or UNKNOWN",
  "confidence": <integer 0-100>,
  "match_reasons": ["<reason1>"],
  "exclusion_flags": ["<hard exclusions triggered>"],
  "missing_info": ["<info needed>"],
  "plain_english_summary": "<2 sentence plain language explanation>"
}}"""
        print(f"[GEMINI MATCH] Sending request to gemini-2.5-flash for {nct_id}...")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type='application/json')
        )
        print(f"[GEMINI MATCH] Received response from model for {nct_id}. Parsing...")
        result = json.loads(response.text)
        result['status'] = result.get('verdict', 'UNKNOWN')
        result['reason'] = result.get('plain_english_summary', '')
        print(f"[GEMINI MATCH] Eligibility verdict for {nct_id} successfully parsed: {result['status']}")
        return result
    except GoogleAPIError as e:
        print(f"[GEMINI MATCH] Google API Error for {nct_id}: {e}")
        traceback.print_exc()
        return {
            "status": "NO_DATA", "verdict": "UNKNOWN", "confidence": 0,
            "match_reasons": [], "exclusion_flags": [], "missing_info": [],
            "plain_english_summary": "AI analysis temporarily unavailable due to API error.",
            "reason": "AI analysis temporarily unavailable due to API error."
        }
    except json.JSONDecodeError as e:
        print(f"[GEMINI MATCH] JSON Decode Error for {nct_id}: {e}")
        traceback.print_exc()
        return {
            "status": "NO_DATA", "verdict": "UNKNOWN", "confidence": 0,
            "match_reasons": [], "exclusion_flags": [], "missing_info": [],
            "plain_english_summary": "AI analysis returned malformed data.",
            "reason": "AI analysis returned malformed data."
        }
    except Exception as e:
        print(f"[GEMINI MATCH] Unexpected Exception for {nct_id}: {e}")
        traceback.print_exc()
        return {
            "status": "NO_DATA", "verdict": "UNKNOWN", "confidence": 0,
            "match_reasons": [], "exclusion_flags": [], "missing_info": [],
            "plain_english_summary": "AI analysis temporarily unavailable due to unexpected error.",
            "reason": "AI analysis temporarily unavailable due to unexpected error."
        }


def extract_patient_profile_from_document(file_bytes, mime_type):
    import json
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return {"error": "GOOGLE_CLOUD_PROJECT not configured.", "extraction_confidence": 0}
    try:
        from google import genai
        from google.genai import types
        client = _get_genai_client()
        document_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        prompt = """Extract structured medical information from this document.
Normalise all lab values to standard SI units during extraction.
Infer sex from patient name if not explicitly stated.
Return ONLY valid JSON:
{
  "diagnosis": ["<primary condition>"],
  "age": null,
  "sex": null,
  "prior_treatments": ["<drug or therapy>"],
  "labs": {
    "ECOG_status": null,
    "hemoglobin_g_dL": null,
    "WBC_10e9_L": null,
    "platelets_10e9_L": null,
    "creatinine_umol_L": null,
    "ALT_U_L": null,
    "eGFR_mL_min": null
  },
  "comorbidities": [],
  "current_medications": [],
  "extraction_confidence": <integer 0-100>,
  "notes": "<anything ambiguous or converted>"
}"""
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[document_part, prompt],
            config=types.GenerateContentConfig(response_mime_type='application/json')
        )
        raw = response.text.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.strip())
        return json.loads(raw)
    except GoogleAPIError as e:
        print(f"Document extraction failed due to Google API error: {e}")
        return {"error": "Document extraction failed due to API error.", "extraction_confidence": 0}
    except json.JSONDecodeError as e:
        print(f"Document extraction failed to decode JSON: {e}")
        return {"error": "Document extraction returned malformed data.", "extraction_confidence": 0}
    except Exception as e:
        print(f"Document extraction failed due to unexpected error: {e}")
        return {
            "error": str(e), "extraction_confidence": 0,
            "diagnosis": [], "prior_treatments": [], "labs": {}
        }
