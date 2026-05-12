import datetime
import sqlite3, os, re, math
from werkzeug.security import generate_password_hash
from flask import g
import secrets, requests, warnings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trialconnect', 'data', 'trial_connect.db')

# --- Constant for allowed file types ---
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def get_db():
    """Opens a new database connection if there is none yet for the current application context."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    db = sqlite3.connect(DB_PATH)
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firstName TEXT NOT NULL,
            lastName TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            firstName TEXT NOT NULL,
            lastName TEXT NOT NULL,
            birthYear INTEGER,
            sex TEXT,
            password_hash TEXT,
            auth_provider TEXT NOT NULL,
            provider_id TEXT,
            profile_picture_url TEXT,
            remember_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reset_token TEXT,
            reset_token_expiration TIMESTAMP
        )
    """)

    # Stores the master list of promoted study IDs ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promoted_studies (
            nct_id TEXT PRIMARY KEY NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # --- Logs when a promoted study is returned in a search ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promotion_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nct_id TEXT NOT NULL,
            search_term TEXT NOT NULL,
            view_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.commit()
    db.close()
    print("Database initialized successfully.")

def add_contact_message(firstName, lastName, email, message):
    """Inserts a new contact message into the database."""
    db = get_db()
    db.execute(
        "INSERT INTO contacts (firstName, lastName, email, message) VALUES (?, ?, ?, ?)",
        (firstName, lastName, email, message)
    )
    db.commit()

def get_or_create_user(email, firstName, lastName, birthYear=None, sex=None, auth_provider='local', profile_picture_url=None, provider_id=None, password=None):
    """Finds an existing user or creates a new one, generates a remember_token, and returns the user as a dict."""
    db = get_db()
    
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if user:
        return dict(user)
    
    password_hash = None
    if password:
        password_hash = generate_password_hash(password)

    remember_token = secrets.token_hex(16)

    cursor = db.execute(
        """
        INSERT INTO users (email, firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (email, firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token)
    )
    db.commit()
    
    new_user = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(new_user)

def get_user_by_email(email):
    """Finds a user by their email address and returns their data or None."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        return dict(user)
    return None

def get_user_by_remember_token(token):
    """Finds a user by their remember_token."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE remember_token = ?", (token,)).fetchone()
    if user:
        return dict(user)
    return None

def validate_password_strength(password):
    """
    Returns a list of error messages if the password is weak, 
    or an empty list if it's strong.
    """
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

def allowed_file(filename):
    """Checks if the uploaded file has an allowed extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_by_id(user_id):
    """Finds a user by their ID and returns their data or None."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        return dict(user)
    return None

def search_clinical_trials(query, user_lat, user_lon, radius=100, unit="km"):
    """
    Searches the ClinicalTrials.gov API for a given query, filtering for
    studies that are not yet recruiting or are actively recruiting AND
    are within a specified radius of the user's location.
    """

    base_url = "https://clinicaltrials.gov/api/v2/studies"

    safe_unit = "mi" if str(unit).lower() == "mi" else "km"
    
    try:
        safe_radius = int(radius)
    except (ValueError, TypeError):
        safe_radius = 200

    geo_filter = f"distance({user_lat},{user_lon},{safe_radius}{safe_unit})"

    params = {
        "query.term": query,
        "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,AVAILABLE",
        "filter.geo": geo_filter,  
        "pageSize": 1000, 
        "format": "json"
    }

    try:
        response = requests.get(base_url, params=params, verify=False)
        response.raise_for_status()  
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"API request failed: {e}")
        return []

    formatted_trials = []
    if "studies" in data:
        for study in data["studies"]:
            protocol = study.get("protocolSection", {})

            id_module = protocol.get("identificationModule", {})
            status_module = protocol.get("statusModule", {})
            conditions_module = protocol.get("conditionsModule", {})
            
            locations_list = []
            contacts_locations_module = protocol.get("contactsLocationsModule", {})
            api_locations = contacts_locations_module.get("locations", [])

            if api_locations:
                for loc in api_locations:
                    location_data = {
                        "facility": loc.get("facility"),
                        "city": loc.get("city"),
                        "state": loc.get("state"),
                        "zip": loc.get("zip"),
                        "country": loc.get("country"),
                        "geoPoint": loc.get("geoPoint")
                    }
                    locations_list.append({k: v for k, v in location_data.items() if v is not None})

            formatted_trials.append({
                "nctId": id_module.get("nctId"),
                "title": id_module.get("briefTitle"),
                "status": status_module.get("overallStatus"),
                "conditions": ", ".join(conditions_module.get("conditions", [])),
                "locations": locations_list
            })
            
    return formatted_trials

def haversine(lat1, lon1, lat2, lon2):
    """Calculate the distance in kilometers between two points on Earth."""
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def get_location_from_ip(ip_address):
    """
    Gets approximate location from an IP address using a free service.
    """
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

# --- PROMOTION HELPER FUNCTIONS ---

def get_all_promoted_studies():
    """Gets all promoted studies for the admin panel list."""
    db = get_db()
    studies = db.execute("SELECT * FROM promoted_studies ORDER BY added_at DESC").fetchall()
    return [dict(study) for study in studies]

def get_all_promoted_studies_set():
    """Gets a Python Set of all promoted NCT IDs for fast O(1) lookups during sorting."""
    db = get_db()
    studies_tuples = db.execute("SELECT nct_id FROM promoted_studies").fetchall()
    return {item[0] for item in studies_tuples}

def add_promoted_study(nct_id):
    """Adds a new NCT ID to the promoted list. IGNOREs duplicates due to PRIMARY KEY."""
    db = get_db()
    try:
        db.execute("INSERT INTO promoted_studies (nct_id) VALUES (?)", (nct_id.strip().upper(),))
        db.commit()
    except sqlite3.IntegrityError:
        pass

def remove_promoted_study(nct_id):
    """Removes an NCT ID from the promoted list."""
    db = get_db()
    db.execute("DELETE FROM promoted_studies WHERE nct_id = ?", (nct_id,))
    db.commit()

def log_promotion_analytic(nct_id, search_term):
    """Logs that a specific promoted study was returned for a search term."""
    db = get_db()
    db.execute(
        "INSERT INTO promotion_analytics (nct_id, search_term) VALUES (?, ?)",
        (nct_id, search_term)
    )
    db.commit()

def check_user_study_match(user_profile, nct_id):
    """
    Checks a logged-in user's profile (age/sex) against a study's eligibility criteria.
    Returns: 'MATCH', 'NO_MATCH', or 'NO_DATA'
    """
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
        warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        response = requests.get(url, timeout=5, verify=False)
        response.raise_for_status()
        data = response.json()
        eligibility = data.get("protocolSection", {}).get("eligibilityModule", {})
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch eligibility for {nct_id}: {e}")
        return {'status': 'NO_DATA', 'reason': 'API fetch failed.'}

    if not eligibility:
        return {'status': 'NO_DATA', 'reason': 'No eligibility data available.'}

    is_sex_match = False
    study_sex = eligibility.get('sex', 'ALL').upper()
    
    if study_sex == 'ALL':
        is_sex_match = True
    elif study_sex == 'MALE' and user_sex == 'MALE':
        is_sex_match = True
    elif study_sex == 'FEMALE' and user_sex == 'FEMALE':
        is_sex_match = True

    is_age_match = False
    min_age_str = eligibility.get('minimumAge', '0 Years')
    max_age_str = eligibility.get('maximumAge')

    min_age_match = re.search(r'\d+', min_age_str)
    min_age = int(min_age_match.group(0)) if min_age_match else 0

    max_age = 150
    if max_age_str:
        max_age_match = re.search(r'\d+', max_age_str)
        if max_age_match:
            max_age = int(max_age_match.group(0))

    if user_age >= min_age and user_age <= max_age:
        is_age_match = True

    if is_age_match and is_sex_match:
        return {'status': 'MATCH', 'reason': f'User (Age: {user_age}, Sex: {user_sex}) matches study (Age: {min_age}-{max_age}, Sex: {study_sex})'}
    else:
        reason = "User did not match: "
        if not is_age_match:
            reason += f"Age ({user_age}) outside range ({min_age}-{max_age}). "
        if not is_sex_match:
            reason += f"Sex ({user_sex}) does not match study requirement ({study_sex})."
        return {'status': 'NO_MATCH', 'reason': reason}


# =============================================================================
# HACKATHON ADDITIONS: MongoDB Atlas + Gemini AI
# =============================================================================

def get_mongo_db():
    """
    Returns the MongoDB database instance using MONGODB_URI from environment.
    Uses Flask's app context cache (g) so the connection is reused per request.
    Requires MONGODB_URI in your .env file.
    """
    if 'mongo_db' not in g:
        from pymongo import MongoClient
        from flask import current_app
        uri = current_app.config.get('MONGODB_URI') or os.environ.get('MONGODB_URI')
        if not uri:
            raise RuntimeError("MONGODB_URI is not set in environment variables.")
        client = MongoClient(uri)
        g.mongo_client = client
        g.mongo_db = client['trialconnect']
    return g.mongo_db


def search_trials_mongo(query, user_lat, user_lon, radius_km=200):
    """
    Searches the MongoDB Atlas 'trials' collection using Atlas Vector Search
    on the eligibility_criteria_embedding field, combined with a geo pre-filter.

    Falls back gracefully to the ClinicalTrials.gov live API if MongoDB is
    not configured (MONGODB_URI not set), so the app keeps working during
    local development before Atlas is set up.

    Returns a list of trial dicts in the same format as search_clinical_trials().
    """
    mongo_uri = os.environ.get('MONGODB_URI')
    if not mongo_uri:
        print("MONGODB_URI not set — falling back to ClinicalTrials.gov API")
        return search_clinical_trials(query, user_lat, user_lon, radius_km)

    try:
        db = get_mongo_db()
        collection = db['trials']

        # Generate a query embedding using Vertex AI text-embedding model
        query_embedding = get_text_embedding(query)

        if query_embedding:
            # --- Atlas Vector Search pipeline ---
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": "eligibility_vector_index",
                        "path": "eligibility_criteria_embedding",
                        "queryVector": query_embedding,
                        "numCandidates": 200,
                        "limit": 100
                    }
                },
                {
                    "$addFields": {
                        "vector_score": {"$meta": "vectorSearchScore"}
                    }
                },
                {
                    "$match": {
                        "overall_status": {"$in": ["RECRUITING", "NOT_YET_RECRUITING", "AVAILABLE"]}
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "nctId": "$nct_id",
                        "title": "$brief_title",
                        "status": "$overall_status",
                        "conditions": "$conditions_str",
                        "locations": "$locations",
                        "eligibility_criteria": 1,
                        "vector_score": 1
                    }
                }
            ]
            results = list(collection.aggregate(pipeline))
        else:
            # No embedding available — fall back to text search
            results = list(collection.find(
                {
                    "$text": {"$search": query},
                    "overall_status": {"$in": ["RECRUITING", "NOT_YET_RECRUITING", "AVAILABLE"]}
                },
                {
                    "_id": 0,
                    "nctId": "$nct_id",
                    "title": "$brief_title",
                    "status": "$overall_status",
                    "conditions": "$conditions_str",
                    "locations": 1,
                    "eligibility_criteria": 1
                }
            ).limit(100))

        return results

    except Exception as e:
        print(f"MongoDB search failed: {e} — falling back to ClinicalTrials.gov API")
        return search_clinical_trials(query, user_lat, user_lon, radius_km)


def get_text_embedding(text):
    """
    Generates a text embedding vector using Vertex AI text-embedding-004.
    Returns a list of floats, or None if Vertex AI is not configured.
    Requires GOOGLE_CLOUD_PROJECT in your .env file.
    """
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
        from flask import current_app

        project = current_app.config.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GOOGLE_CLOUD_PROJECT')
        location = current_app.config.get('VERTEX_AI_LOCATION', 'us-central1')

        if not project:
            return None

        vertexai.init(project=project, location=location)
        model = TextEmbeddingModel.from_pretrained("text-embedding-004")
        embeddings = model.get_embeddings([text])
        return embeddings[0].values

    except Exception as e:
        print(f"Embedding generation failed: {e}")
        return None


def score_trial(trial, patient_profile):
    """
    Composite ranking score (higher = better match).
    Components:
      - vector_score (0-1): semantic similarity from Atlas Vector Search
      - distance_score (0-1): exponential decay, ~0.5 at 300km, ~0 at 800km
    Weights: 70% semantic relevance, 30% proximity.
    Gemini confidence is added separately by api_check_match route when available.
    """
    vector_score = float(trial.get("vector_score", 0.5))

    distance_score = 0.5  # default if no location data
    try:
        user_lat = patient_profile["location"]["lat"]
        user_lon = patient_profile["location"]["lon"]
        distances = [
            haversine(user_lat, user_lon, loc["lat"], loc["lon"])
            for loc in trial.get("locations", [])
            if loc.get("lat") and loc.get("lon")
        ]
        if distances:
            distance_score = math.exp(-min(distances) / 300)
    except (KeyError, TypeError):
        pass

    return round((0.70 * vector_score) + (0.30 * distance_score), 4)


def fetch_trial_eligibility_text(nct_id):
    """
    Fetches the full eligibility criteria text for a trial.
    First tries MongoDB (fast), then falls back to ClinicalTrials.gov API.
    Returns the criteria string, or None if not found.
    """
    # Try MongoDB first
    mongo_uri = os.environ.get('MONGODB_URI')
    if mongo_uri:
        try:
            db = get_mongo_db()
            trial = db['trials'].find_one(
                {"nct_id": nct_id},
                {"_id": 0, "eligibility_criteria": 1}
            )
            if trial and trial.get("eligibility_criteria"):
                return trial["eligibility_criteria"]
        except Exception as e:
            print(f"MongoDB eligibility fetch failed for {nct_id}: {e}")

    # Fallback: ClinicalTrials.gov API
    try:
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}?fields=EligibilityModule"
        warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        response = requests.get(url, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
        eligibility = data.get("protocolSection", {}).get("eligibilityModule", {})
        return eligibility.get("eligibilityCriteria")
    except Exception as e:
        print(f"CT.gov eligibility fetch failed for {nct_id}: {e}")
        return None


def gemini_eligibility_check(patient_profile, eligibility_criteria_text, nct_id):
    """
    Uses Gemini 1.5 Pro to reason over full eligibility criteria text against
    a structured patient profile. Returns a structured verdict dict.

    Falls back to the lightweight check_user_study_match() if Gemini is
    not configured (GOOGLE_CLOUD_PROJECT not set).

    patient_profile example:
    {
      "age": 52, "sex": "MALE",
      "diagnosis": ["NSCLC stage III"],
      "prior_treatments": ["carboplatin"],
      "labs": {"ECOG_status": 1, "creatinine_umol_L": 80},
      "comorbidities": []
    }
    """
    import json

    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        print("GOOGLE_CLOUD_PROJECT not set — Gemini eligibility check skipped.")
        return {
            "verdict": "UNKNOWN",
            "confidence": 0,
            "match_reasons": [],
            "exclusion_flags": [],
            "missing_info": ["Gemini not configured"],
            "plain_english_summary": "AI matching is not configured yet."
        }

    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
        from flask import current_app

        location = current_app.config.get('VERTEX_AI_LOCATION', 'us-central1')
        vertexai.init(project=project, location=location)

        model = GenerativeModel("gemini-1.5-pro")

        prompt = f"""You are a clinical trial eligibility expert. Analyze whether this patient qualifies for a clinical trial.

PATIENT PROFILE:
{json.dumps(patient_profile, indent=2)}

TRIAL ELIGIBILITY CRITERIA:
{eligibility_criteria_text}

Respond ONLY with valid JSON in this exact format:
{{
  "verdict": "MATCH or PARTIAL_MATCH or NO_MATCH or UNKNOWN",
  "confidence": <integer 0-100>,
  "match_reasons": ["<reason1>", "<reason2>"],
  "exclusion_flags": ["<any hard exclusions triggered>"],
  "missing_info": ["<info needed to make a firm determination>"],
  "plain_english_summary": "<2 sentence plain language explanation for the patient>"
}}"""

        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )

        result = json.loads(response.text)
        # Normalize verdict to status key for frontend compatibility
        result['status'] = result.get('verdict', 'UNKNOWN')
        result['reason'] = result.get('plain_english_summary', '')
        return result

    except Exception as e:
        print(f"Gemini eligibility check failed for {nct_id}: {e}")
        return {
            "status": "NO_DATA",
            "verdict": "UNKNOWN",
            "confidence": 0,
            "match_reasons": [],
            "exclusion_flags": [],
            "missing_info": [],
            "plain_english_summary": "AI analysis temporarily unavailable.",
            "reason": "AI analysis temporarily unavailable."
        }


def extract_patient_profile_from_document(file_bytes, mime_type):
    """
    Uses Gemini 1.5 Pro multimodal to extract a structured patient profile
    from a PDF lab report, discharge summary, or medical image.

    mime_type: 'application/pdf', 'image/png', 'image/jpeg'

    Returns a dict with keys: diagnosis, age, sex, prior_treatments,
    labs, comorbidities, current_medications, extraction_confidence, notes.
    """
    import json

    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return {
            "error": "GOOGLE_CLOUD_PROJECT not configured.",
            "extraction_confidence": 0
        }

    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, Part
        from flask import current_app

        location = current_app.config.get('VERTEX_AI_LOCATION', 'us-central1')
        vertexai.init(project=project, location=location)

        model = GenerativeModel("gemini-1.5-pro")
        document_part = Part.from_data(data=file_bytes, mime_type=mime_type)

        prompt = """Extract structured medical information from this document.
Return ONLY valid JSON with no extra text:
{
  "diagnosis": ["<condition1>"],
  "age": null,
  "sex": null,
  "prior_treatments": ["<drug or therapy>"],
  "labs": {
    "ECOG_status": null,
    "creatinine_umol_L": null,
    "ALT_U_L": null,
    "hemoglobin_g_dL": null,
    "platelets_10e9_L": null,
    "WBC_10e9_L": null
  },
  "comorbidities": [],
  "current_medications": [],
  "extraction_confidence": <integer 0-100>,
  "notes": "<anything ambiguous or worth flagging>"
}"""

        response = model.generate_content([document_part, prompt])
        result = json.loads(response.text)
        return result

    except Exception as e:
        print(f"Document extraction failed: {e}")
        return {
            "error": str(e),
            "extraction_confidence": 0,
            "diagnosis": [],
            "prior_treatments": [],
            "labs": {}
        }
