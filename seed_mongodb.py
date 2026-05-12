#!/usr/bin/env python3
"""
seed_mongodb.py

One-time script to populate the MongoDB Atlas 'trials' collection.

What it does:
  1. Fetches trials from ClinicalTrials.gov for a list of common conditions
  2. Generates a Vertex AI text embedding for each trial's eligibility criteria
  3. Inserts / upserts everything into MongoDB Atlas trialconnect.trials
  4. Creates a text index on brief_title + conditions_str (fallback search)

Run once from the repo root (with .venv active):
    python seed_mongodb.py

Requirements in .env:
    MONGODB_URI
    GOOGLE_CLOUD_PROJECT
    VERTEX_AI_LOCATION  (optional, defaults to us-central1)
"""

import os, math, time, requests, warnings
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

load_dotenv()

# ------------------------------------------------------------------ config
MONGODB_URI        = os.environ['MONGODB_URI']
GCP_PROJECT        = os.environ['GOOGLE_CLOUD_PROJECT']
VERTEX_LOCATION    = os.environ.get('VERTEX_AI_LOCATION', 'us-central1')
DB_NAME            = 'trialconnect'
COLLECTION_NAME    = 'trials'
BATCH_SIZE         = 50          # upsert batch size
EMBED_BATCH_SIZE   = 5           # Vertex AI calls per batch (rate-limit friendly)
MAX_TRIALS_PER_CONDITION = 200   # keep seed manageable

# Conditions to seed — covers the most common search terms
CONDITIONS = [
    "asthma",
    "diabetes type 2",
    "breast cancer",
    "lung cancer",
    "COPD",
    "heart failure",
    "depression",
    "Alzheimer",
    "Parkinson",
    "multiple sclerosis",
    "rheumatoid arthritis",
    "Crohn disease",
    "hypertension",
    "obesity",
    "COVID-19",
]

# ------------------------------------------------------------------ helpers

def fetch_trials(condition, max_results=MAX_TRIALS_PER_CONDITION):
    """Fetch trials from ClinicalTrials.gov v2 API for a given condition."""
    url = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "query.term": condition,
        "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,AVAILABLE",
        "pageSize": max_results,
        "format": "json",
        "fields": (
            "NCTId,BriefTitle,OverallStatus,Condition,"
            "EligibilityCriteria,MinimumAge,MaximumAge,Sex,"
            "LocationFacility,LocationCity,LocationCountry,"
            "LocationGeoPoint"
        )
    }
    warnings.filterwarnings('ignore', message='Unverified HTTPS request')
    try:
        r = requests.get(url, params=params, timeout=30, verify=False)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [WARN] CT.gov fetch failed for '{condition}': {e}")
        return []

    trials = []
    for study in data.get('studies', []):
        p = study.get('protocolSection', {})
        id_mod   = p.get('identificationModule', {})
        stat_mod = p.get('statusModule', {})
        cond_mod = p.get('conditionsModule', {})
        elig_mod = p.get('eligibilityModule', {})
        loc_mod  = p.get('contactsLocationsModule', {})

        locations = []
        for loc in loc_mod.get('locations', []):
            entry = {
                'facility': loc.get('facility'),
                'city':     loc.get('city'),
                'country':  loc.get('country'),
            }
            gp = loc.get('geoPoint')
            if gp:
                entry['lat'] = gp.get('lat')
                entry['lon'] = gp.get('lng') or gp.get('lon')
            locations.append({k: v for k, v in entry.items() if v is not None})

        conditions_list = cond_mod.get('conditions', [])
        eligibility_text = elig_mod.get('eligibilityCriteria', '')

        trials.append({
            'nct_id':          id_mod.get('nctId'),
            'brief_title':     id_mod.get('briefTitle'),
            'overall_status':  stat_mod.get('overallStatus'),
            'conditions':      conditions_list,
            'conditions_str':  ', '.join(conditions_list),
            'eligibility_criteria': eligibility_text,
            'minimum_age':     elig_mod.get('minimumAge'),
            'maximum_age':     elig_mod.get('maximumAge'),
            'sex':             elig_mod.get('sex', 'ALL'),
            'locations':       locations,
            'seed_condition':  condition,
        })
    return trials


def get_embeddings_batch(texts):
    """
    Generate embeddings for a list of texts using Vertex AI text-embedding-004.
    Returns list of float vectors (or None per item on failure).
    """
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
        vertexai.init(project=GCP_PROJECT, location=VERTEX_LOCATION)
        model = TextEmbeddingModel.from_pretrained('text-embedding-004')
        # Truncate to 2048 chars to stay within token limits
        truncated = [t[:2048] if t else '' for t in texts]
        results = model.get_embeddings(truncated)
        return [r.values for r in results]
    except Exception as e:
        print(f"  [WARN] Embedding batch failed: {e}")
        return [None] * len(texts)


def upsert_batch(collection, docs):
    """Upsert a batch of trial docs by nct_id."""
    ops = [
        UpdateOne({'nct_id': d['nct_id']}, {'$set': d}, upsert=True)
        for d in docs if d.get('nct_id')
    ]
    if not ops:
        return 0
    try:
        result = collection.bulk_write(ops, ordered=False)
        return result.upserted_count + result.modified_count
    except BulkWriteError as e:
        print(f"  [WARN] Bulk write partial error: {e.details.get('writeErrors', '')[:1]}")
        return 0


# ------------------------------------------------------------------ main

def main():
    print("\n=== TrialConnect MongoDB Seed Script ===")
    print(f"  Project : {GCP_PROJECT}")
    print(f"  DB      : {DB_NAME}.{COLLECTION_NAME}")
    print(f"  Seeding {len(CONDITIONS)} conditions, up to {MAX_TRIALS_PER_CONDITION} trials each\n")

    client = MongoClient(MONGODB_URI)
    db = client[DB_NAME]
    col = db[COLLECTION_NAME]

    # Create text index for fallback keyword search
    print("[1/4] Ensuring text index...")
    try:
        col.create_index(
            [('brief_title', 'text'), ('conditions_str', 'text'), ('eligibility_criteria', 'text')],
            name='trial_text_index',
            default_language='english'
        )
        print("      Text index ready.")
    except Exception as e:
        print(f"      [WARN] Text index: {e}")

    # Fetch all trials
    print("\n[2/4] Fetching trials from ClinicalTrials.gov...")
    all_trials = {}
    for cond in CONDITIONS:
        trials = fetch_trials(cond)
        for t in trials:
            if t['nct_id']:
                all_trials[t['nct_id']] = t  # deduplicate by NCT ID
        print(f"      {cond:30s} → {len(trials):4d} trials  (total unique: {len(all_trials)})")
        time.sleep(0.3)  # be polite to the API

    trials_list = list(all_trials.values())
    print(f"\n      Total unique trials to seed: {len(trials_list)}")

    # Generate embeddings
    print("\n[3/4] Generating embeddings with Vertex AI text-embedding-004...")
    embed_ok = 0
    embed_fail = 0
    for i in range(0, len(trials_list), EMBED_BATCH_SIZE):
        batch = trials_list[i:i + EMBED_BATCH_SIZE]
        texts = [t.get('eligibility_criteria', '') or t.get('brief_title', '') for t in batch]
        vectors = get_embeddings_batch(texts)
        for trial, vec in zip(batch, vectors):
            if vec:
                trial['eligibility_criteria_embedding'] = vec
                embed_ok += 1
            else:
                embed_fail += 1
        # Progress every 10 batches
        if (i // EMBED_BATCH_SIZE) % 10 == 0:
            pct = min(100, round(i / len(trials_list) * 100))
            print(f"      {pct:3d}%  ({i}/{len(trials_list)})  ok={embed_ok} fail={embed_fail}")
        time.sleep(0.2)  # rate limit

    print(f"      Done. Embedded: {embed_ok}  Skipped: {embed_fail}")

    # Upsert into MongoDB
    print("\n[4/4] Upserting into MongoDB Atlas...")
    total_written = 0
    for i in range(0, len(trials_list), BATCH_SIZE):
        batch = trials_list[i:i + BATCH_SIZE]
        written = upsert_batch(col, batch)
        total_written += written
        pct = min(100, round(i / len(trials_list) * 100))
        print(f"      {pct:3d}%  upserted so far: {total_written}")

    final_count = col.count_documents({})
    print(f"\n✅ Seed complete!")
    print(f"   Documents in collection : {final_count}")
    print(f"   Trials with embeddings  : {embed_ok}")
    print()
    print("Next step: create the Atlas Vector Search index named")
    print("'eligibility_vector_index' on field 'eligibility_criteria_embedding'")
    print("in the Atlas UI under Search Indexes → Create Index → JSON editor:")
    print()
    print('''{
  "fields": [{
    "type": "vector",
    "path": "eligibility_criteria_embedding",
    "numDimensions": 768,
    "similarity": "cosine"
  }]
}''')
    client.close()


if __name__ == '__main__':
    main()
