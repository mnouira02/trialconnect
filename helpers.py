import datetime
import sqlite3, os, re, math
from werkzeug.security import generate_password_hash
from flask import g
import secrets, requests, warnings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trialconnect', 'data', 'trial_connect.db')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    cursor = db.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firstName TEXT NOT NULL, lastName TEXT NOT NULL,
            email TEXT NOT NULL, message TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE, firstName TEXT NOT NULL, lastName TEXT NOT NULL,
            birthYear INTEGER, sex TEXT, password_hash TEXT, auth_provider TEXT NOT NULL,
            provider_id TEXT, profile_picture_url TEXT, remember_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reset_token TEXT, reset_token_expiration TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promoted_studies (
            nct_id TEXT PRIMARY KEY NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promotion_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nct_id TEXT NOT NULL, search_term TEXT NOT NULL,
            view_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()
    db.close()
    print("Database initialized successfully.")

def add_contact_message(firstName, lastName, email, message):
    db = get_db()
    db.execute("INSERT INTO contacts (firstName, lastName, email, message) VALUES (?, ?, ?, ?)",
               (firstName, lastName, email, message))
    db.commit()

def get_or_create_user(email, firstName, lastName, birthYear=None, sex=None,
                       auth_provider='local', profile_picture_url=None, provider_id=None, password=None):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        return dict(user)
    password_hash = generate_password_hash(password) if password else None
    remember_token = secrets.token_hex(16)
    cursor = db.execute(
        "INSERT INTO users (email, firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (email, firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token)
    )
    db.commit()
    return dict(db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone())

def get_user_by_email(email):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(user) if user else None

def get_user_by_remember_token(token):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE remember_token = ?", (token,)).fetchone()
    return dict(user) if user else None

def validate_password_strength(password):
    errors = []
    if len(password) < 8: errors.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", password): errors.append("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password): errors.append("Password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password): errors.append("Password must contain at least one number.")
    return errors

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_by_id(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(user) if user else None

def search_clinical_trials(query, user_lat, user_lon, radius=100, unit="km"):
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
            api_locations = protocol.get("contactsLocationsModule", {}).get("locations", [])
            for loc in api_locations:
                location_data = {k: loc.get(k) for k in ["facility","city","state","zip","country","geoPoint"]}
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

def get_all_promoted_studies():
    db = get_db()
    return [dict(s) for s in db.execute("SELECT * FROM promoted_studies ORDER BY added_at DESC").fetchall()]

def get_all_promoted_studies_set():
    db = get_db()
    return {item[0] for item in db.execute("SELECT nct_id FROM promoted_studies").fetchall()}

def add_promoted_study(nct_id):
    db = get_db()
    try:
        db.execute("INSERT INTO promoted_studies (nct_id) VALUES (?)", (nct_id.strip().upper(),))
        db.commit()
    except sqlite3.IntegrityError:
        pass

def remove_promoted_study(nct_id):
    db = get_db()
    db.execute("DELETE FROM promoted_studies WHERE nct_id = ?", (nct_id,))
    db.commit()

def log_promotion_analytic(nct_id, search_term):
    db = get_db()
    db.execute("INSERT INTO promotion_analytics (nct_id, search_term) VALUES (?, ?)", (nct_id, search_term))
    db.commit()

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
        warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        response = requests.get(url, timeout=5, verify=False)
        response.raise_for_status()
        eligibility = response.json().get("protocolSection", {}).get("eligibilityModule", {})
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch eligibility for {nct_id}: {e}")
        return {'status': 'NO_DATA', 'reason': 'API fetch failed.'}
    if not eligibility:
        return {'status': 'NO_DATA', 'reason': 'No eligibility data available.'}
    study_sex = eligibility.get('sex', 'ALL').upper()
    is_sex_match = study_sex == 'ALL' or study_sex == user_sex
    min_age = int((re.search(r'\d+', eligibility.get('minimumAge','0 Years')) or type('',(),{'group':lambda s,x:'0'})()).group(0))
    max_age = 150
    max_age_str = eligibility.get('maximumAge')
    if max_age_str:
        m = re.search(r'\d+', max_age_str)
        if m: max_age = int(m.group(0))
    is_age_match = min_age <= user_age <= max_age
    if is_age_match and is_sex_match:
        return {'status': 'MATCH', 'verdict': 'MATCH',
                'reason': f'Age ({user_age}) and sex ({user_sex}) match study criteria.'}
    reason = ""
    if not is_age_match: reason += f"Age ({user_age}) outside range ({min_age}-{max_age}). "
    if not is_sex_match: reason += f"Sex ({user_sex}) does not match study requirement ({study_sex})."
    return {'status': 'NO_MATCH', 'verdict': 'NO_MATCH', 'reason': reason.strip()}


# =============================================================================
# HACKATHON ADDITIONS: MongoDB Atlas + Gemini AI
# =============================================================================

def get_mongo_db():
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


def get_text_embedding(text):
    """
    Generates a text embedding using the new google-genai SDK.
    Falls back gracefully if GOOGLE_CLOUD_PROJECT is not set.
    Uses 'text-embedding-005' which is GA on Vertex AI.
    """
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(
            vertexai=True,
            project=project,
            location='global'
        )
        result = client.models.embed_content(
            model='text-embedding-005',
            contents=text,
            config=types.EmbedContentConfig(task_type='RETRIEVAL_QUERY')
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"Embedding generation failed: {e}")
        return None


def search_trials_mongo(query, user_lat, user_lon, radius_km=200):
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
                    "numCandidates": 200, "limit": 100
                }},
                {"$addFields": {"vector_score": {"$meta": "vectorSearchScore"}}},
                {"$match": {"overall_status": {"$in": ["RECRUITING","NOT_YET_RECRUITING","AVAILABLE"]}}},
                {"$project": {
                    "_id": 0, "nctId": "$nct_id", "title": "$brief_title",
                    "status": "$overall_status", "conditions": "$conditions_str",
                    "locations": "$locations", "eligibility_criteria": 1, "vector_score": 1
                }}
            ]
            return list(collection.aggregate(pipeline))
        else:
            return list(collection.find(
                {"$text": {"$search": query}, "overall_status": {"$in": ["RECRUITING","NOT_YET_RECRUITING","AVAILABLE"]}},
                {"_id": 0, "nctId": "$nct_id", "title": "$brief_title", "status": "$overall_status",
                 "conditions": "$conditions_str", "locations": 1, "eligibility_criteria": 1}
            ).limit(100))
    except Exception as e:
        print(f"MongoDB search failed: {e} — falling back to ClinicalTrials.gov API")
        return search_clinical_trials(query, user_lat, user_lon, radius_km)


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
            distance_score = math.exp(-min(distances) / 300)
    except (KeyError, TypeError):
        pass
    return round((0.70 * vector_score) + (0.30 * distance_score), 4)


def fetch_trial_eligibility_text(nct_id):
    mongo_uri = os.environ.get('MONGODB_URI')
    if mongo_uri:
        try:
            db = get_mongo_db()
            trial = db['trials'].find_one({"nct_id": nct_id}, {"_id": 0, "eligibility_criteria": 1})
            if trial and trial.get("eligibility_criteria"):
                return trial["eligibility_criteria"]
        except Exception as e:
            print(f"MongoDB eligibility fetch failed for {nct_id}: {e}")
    try:
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}?fields=EligibilityModule"
        warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        response = requests.get(url, timeout=10, verify=False)
        response.raise_for_status()
        eligibility = response.json().get("protocolSection", {}).get("eligibilityModule", {})
        return eligibility.get("eligibilityCriteria")
    except Exception as e:
        print(f"CT.gov eligibility fetch failed for {nct_id}: {e}")
        return None


def gemini_eligibility_check(patient_profile, eligibility_criteria_text, nct_id):
    import json
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return {
            "verdict": "UNKNOWN", "confidence": 0, "match_reasons": [],
            "exclusion_flags": [], "missing_info": ["Gemini not configured"],
            "plain_english_summary": "AI matching is not configured yet.",
            "status": "UNKNOWN", "reason": "AI matching is not configured yet."
        }
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(vertexai=True, project=project, location='global')
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
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type='application/json')
        )
        result = json.loads(response.text)
        result['status'] = result.get('verdict', 'UNKNOWN')
        result['reason'] = result.get('plain_english_summary', '')
        return result
    except Exception as e:
        print(f"Gemini eligibility check failed for {nct_id}: {e}")
        return {
            "status": "NO_DATA", "verdict": "UNKNOWN", "confidence": 0,
            "match_reasons": [], "exclusion_flags": [], "missing_info": [],
            "plain_english_summary": "AI analysis temporarily unavailable.",
            "reason": "AI analysis temporarily unavailable."
        }


def extract_patient_profile_from_document(file_bytes, mime_type):
    """
    Uses Gemini 2.5 Flash multimodal via the new google-genai SDK to extract
    a structured patient profile from a PDF/image medical document.
    Forces response_mime_type=application/json to avoid markdown-wrapped output.
    """
    import json
    project = os.environ.get('GOOGLE_CLOUD_PROJECT')
    if not project:
        return {"error": "GOOGLE_CLOUD_PROJECT not configured.", "extraction_confidence": 0}
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(vertexai=True, project=project, location='global')
        document_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
        prompt = """Extract structured medical information from this document.
Return ONLY valid JSON:
{
  "diagnosis": ["<condition>"],
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
  "notes": "<anything ambiguous>"
}"""
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[document_part, prompt],
            config=types.GenerateContentConfig(response_mime_type='application/json')
        )
        raw = response.text.strip()
        # Belt-and-suspenders fence stripper
        if raw.startswith('```'):
            raw = re.sub(r'^```[a-z]*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw.strip())
        return json.loads(raw)
    except Exception as e:
        print(f"Document extraction failed: {e}")
        return {
            "error": str(e), "extraction_confidence": 0,
            "diagnosis": [], "prior_treatments": [], "labs": {}
        }
