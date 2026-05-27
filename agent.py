import os
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

# Determine if Vertex AI / ADC is available, otherwise fall back to AI Studio API key
adc_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
default_adc_path = os.path.expanduser('~/.config/gcloud/application_default_credentials.json')
default_adc_path_win = os.path.expandvars('%APPDATA%/gcloud/application_default_credentials.json')
if adc_path or os.path.exists(default_adc_path) or os.path.exists(default_adc_path_win):
    os.environ['GOOGLE_GENAI_USE_VERTEXAI'] = 'true'
else:
    os.environ['GOOGLE_GENAI_USE_VERTEXAI'] = 'false'
    if not os.environ.get('GEMINI_API_KEY'):
        os.environ['GEMINI_API_KEY'] = os.environ.get('GOOGLE_MAPS_API_KEY', '')

os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'trialconnect-app')
os.environ.setdefault('GOOGLE_CLOUD_LOCATION', 'us-central1')


def search_trials(condition: str, location: str) -> dict:
    """Search for clinical trials by medical condition and location.

    Args:
        condition: The medical condition or disease to search for (e.g. 'lung cancer', 'diabetes').
        location: City, state, or address to search near (e.g. 'Chicago, IL').

    Returns:
        A dict with a 'trials' list, each containing nctId, title, status, conditions,
        interventions, closest_site_km, and a URL.
    """
    import requests as req
    try:
        maps_key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
        geo_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={req.utils.quote(location)}&key={maps_key}"
        geo_resp = req.get(geo_url, timeout=5).json()
        if not geo_resp.get('results'):
            return {"error": f"Could not geocode location: {location}"}
        loc = geo_resp['results'][0]['geometry']['location']
        lat, lon = loc['lat'], loc['lng']

        from helpers import search_trials_mongo, haversine
        raw_results = search_trials_mongo(condition, lat, lon, radius_km=300)
        output = []
        for study in raw_results[:5]:
            # search_trials_mongo $project renames fields to: nctId, title, status, conditions
            # Fall back to raw MongoDB field names in case vector search is skipped
            nct_id   = study.get('nctId')   or study.get('nct_id')
            title    = study.get('title')   or study.get('brief_title')
            status   = study.get('status')  or study.get('overall_status')
            conditions = study.get('conditions') or study.get('conditions_str', '')

            min_distance = float('inf')
            for site in study.get('locations', []):
                geo = site.get('geoPoint') or site
                slat, slon = geo.get('lat'), geo.get('lon')
                if slat and slon:
                    dist = haversine(lat, lon, slat, slon)
                    if dist < min_distance:
                        min_distance = dist

            interventions = [
                i.get('name', '') for i in study.get('interventions', [])
            ][:3]

            output.append({
                "nctId": nct_id,
                "title": title,
                "status": status,
                "conditions": conditions,
                "interventions": interventions,
                "closest_site_km": round(min_distance) if min_distance != float('inf') else None,
                "url": f"https://clinicaltrials.gov/study/{nct_id}"
            })
        return {"trials": output, "count": len(output)}
    except Exception as e:
        return {"error": str(e)}


from typing import Optional

def check_eligibility(nct_id: str, age: Optional[int] = None, sex: Optional[str] = None,
                      diagnosis: Optional[str] = None, prior_treatments: Optional[str] = None,
                      comorbidities: Optional[str] = None) -> dict:
    """Check whether a patient is eligible for a specific clinical trial.

    Args:
        nct_id: The ClinicalTrials.gov NCT identifier (e.g. 'NCT04123456').
        age: Patient age in years.
        sex: Patient sex ('MALE' or 'FEMALE').
        diagnosis: Patient's primary diagnosis or condition details.
        prior_treatments: Previous treatments the patient has received.
        comorbidities: Other conditions or comorbidities the patient has.

    Returns:
        A dict with eligibility status ('ELIGIBLE', 'INELIGIBLE', or 'UNCERTAIN')
        and a reason explaining the determination.
    """
    try:
        from helpers import fetch_trial_eligibility_text, gemini_eligibility_check
        patient_profile = {k: v for k, v in {
            'age': age,
            'sex': (sex or '').upper() or None,
            'diagnosis': diagnosis,
            'prior_treatments': prior_treatments,
            'comorbidities': comorbidities
        }.items() if v}
        eligibility_text = fetch_trial_eligibility_text(nct_id)
        if not eligibility_text:
            return {"status": "NO_DATA", "reason": "No eligibility criteria found for this trial."}
        return gemini_eligibility_check(patient_profile, eligibility_text, nct_id)
    except Exception as e:
        return {"error": str(e)}


# Single source of truth for the TrialConnect agent.
# Used by both Flask (routes.py) and Google Agent Builder studio.
root_agent = LlmAgent(
    name='trialconnect_agent',
    model='gemini-2.5-flash',
    description=(
        'TrialConnect clinical trial search and eligibility matching assistant. '
        'Helps patients find relevant clinical trials based on their condition and location, '
        'and checks eligibility using Gemini AI.'
    ),
    instruction=(
        'You are a clinical trial assistant. When a user describes their condition and location:\n'
        '1. Call search_trials with their condition and location\n'
        '2. Present the top results clearly: trial title, NCT ID, status, and distance in km\n'
        '3. Ask if they want an eligibility check — if yes, call check_eligibility with their NCT ID and profile details\n'
        'Always recommend consulting a doctor before enrolling.'
    ),
    tools=[
        FunctionTool(search_trials),
        FunctionTool(check_eligibility),
    ],
)
