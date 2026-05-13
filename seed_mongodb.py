#!/usr/bin/env python3
"""
seed_mongodb.py

Resumable seed script — safely restart any time.

What it does:
  1. Fetches trials from ClinicalTrials.gov using query.cond (condition-specific)
  2. Upserts trial metadata into MongoDB (always, so data stays fresh)
  3. Generates Vertex AI text-embedding-005 embeddings for trials missing one
     Embed text = "brief_title | conditions_str | eligibility_criteria"
     so the vector captures what the trial is ABOUT, not just eligibility boilerplate
  4. Writes embeddings back immediately (resumable)
  5. Atlas Vector Search index must use numDimensions: 768 (text-embedding-005)

Re-seeding note:
  Set FORCE_REEMBED=1 in env to wipe existing embeddings and regenerate with -005.
  Required if you previously seeded with text-embedding-004.

Run from repo root (with .venv active):
    python seed_mongodb.py
    FORCE_REEMBED=1 python seed_mongodb.py   # re-embed everything with -005

Requirements in .env:
    MONGODB_URI
    GOOGLE_CLOUD_PROJECT
    VERTEX_AI_LOCATION  (optional, defaults to us-central1)
"""

import os, time, requests, warnings
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
BATCH_SIZE         = 50
EMBED_BATCH_SIZE   = 5
MAX_TRIALS_PER_CONDITION = 200
FORCE_REEMBED      = os.environ.get('FORCE_REEMBED', '0') == '1'

# Conditions are fetched via query.cond (condition-specific field),
# which returns trials where these are listed as the primary condition.
CONDITIONS = [
    "asthma",
    "diabetes type 2",
    "breast cancer",
    "lung cancer",
    "non-small cell lung cancer",
    "small cell lung cancer",
    "COPD",
    "heart failure",
    "depression",
    "Alzheimer disease",
    "Parkinson disease",
    "multiple sclerosis",
    "rheumatoid arthritis",
    "Crohn disease",
    "hypertension",
    "obesity",
    "COVID-19",
    "renal cell carcinoma",
    "colorectal cancer",
    "prostate cancer",
    "melanoma",
    "glioblastoma",
    "leukemia",
    "lymphoma",
    "ovarian cancer",
    "pancreatic cancer",
]

# ------------------------------------------------------------------ helpers

def build_embed_text(trial):
    """Build a rich combined text for embedding.
    Captures WHAT the trial is about (title + conditions) and WHO qualifies
    (eligibility). This ensures semantic search on 'lung cancer' surfaces
    lung cancer trials, not just any oncology study.
    """
    title      = trial.get('brief_title', '') or ''
    conditions = trial.get('conditions_str', '') or ''
    eligibility = trial.get('eligibility_criteria', '') or ''
    combined = f"{title} | {conditions} | {eligibility}"
    return combined[:3072]  # text-embedding-005 supports up to ~3072 tokens


def fetch_trials(condition, max_results=MAX_TRIALS_PER_CONDITION):
    """Fetch trials from ClinicalTrials.gov using query.cond (condition field only).
    This is more precise than query.term which searches across all text fields.
    """
    url = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "query.cond": condition,          # condition-specific, not broad query.term
        "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,AVAILABLE",
        "pageSize": max_results,
        "format": "json",
        "fields": (
            "NCTId,BriefTitle,OverallStatus,Condition,"
            "EligibilityCriteria,MinimumAge,MaximumAge,Sex,"
            "LocationFacility,LocationCity,LocationCountry,"
            "LocationGeoPoint,InterventionName,InterventionType"
        )
    }
    try:
        r = requests.get(url, params=params, timeout=30)
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
        int_mod  = p.get('armsInterventionsModule', {})

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
                entry['geoPoint'] = {'lat': entry['lat'], 'lon': entry['lon']}
            locations.append({k: v for k, v in entry.items() if v is not None})

        conditions_list = cond_mod.get('conditions', [])
        eligibility_text = elig_mod.get('eligibilityCriteria', '')
        interventions_list = [
            {'name': i.get('interventionName', ''), 'type': i.get('interventionType', '')}
            for i in int_mod.get('interventions', [])
        ]

        trials.append({
            'nct_id':               id_mod.get('nctId'),
            'brief_title':          id_mod.get('briefTitle'),
            'overall_status':       stat_mod.get('overallStatus'),
            'conditions':           conditions_list,
            'conditions_str':       ', '.join(conditions_list),
            'eligibility_criteria': eligibility_text,
            'minimum_age':          elig_mod.get('minimumAge'),
            'maximum_age':          elig_mod.get('maximumAge'),
            'sex':                  elig_mod.get('sex', 'ALL'),
            'locations':            locations,
            'interventions':        interventions_list,
            'seed_condition':       condition,
        })
    return trials


def get_embeddings_batch(texts):
    """Generate embeddings using text-embedding-005 (768 dims).
    MUST match the model used at query time in helpers.py.
    text-embedding-004 and text-embedding-005 are NOT interchangeable.
    """
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
        vertexai.init(project=GCP_PROJECT, location=VERTEX_LOCATION)
        model = TextEmbeddingModel.from_pretrained('text-embedding-005')
        truncated = [t[:3072] if t else '' for t in texts]
        results = model.get_embeddings(truncated)
        return [r.values for r in results]
    except Exception as e:
        print(f"  [WARN] Embedding batch failed: {e}")
        return [None] * len(texts)


def upsert_batch(collection, docs):
    """Upsert trial metadata (without overwriting existing embeddings)."""
    ops = []
    for d in docs:
        if not d.get('nct_id'):
            continue
        meta = {k: v for k, v in d.items() if k != 'eligibility_criteria_embedding'}
        ops.append(UpdateOne(
            {'nct_id': d['nct_id']},
            {'$set': meta},
            upsert=True
        ))
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
    print("\n=== TrialConnect MongoDB Seed Script (Resumable) ===")
    print(f"  Project      : {GCP_PROJECT}")
    print(f"  DB           : {DB_NAME}.{COLLECTION_NAME}")
    print(f"  Embed model  : text-embedding-005 (768 dims)")
    print(f"  Force re-embed: {FORCE_REEMBED}")
    print(f"  Seeding {len(CONDITIONS)} conditions, up to {MAX_TRIALS_PER_CONDITION} trials each\n")

    client = MongoClient(MONGODB_URI)
    db = client[DB_NAME]
    col = db[COLLECTION_NAME]

    # Ensure text index (covers title, conditions, eligibility for fallback search)
    print("[1/5] Ensuring text index...")
    try:
        existing = col.index_information()
        for idx_name in list(existing.keys()):
            if idx_name not in ('_id_',) and 'text' in str(existing[idx_name].get('key', {})):
                col.drop_index(idx_name)
                print(f"      Dropped old text index: {idx_name}")
        col.create_index(
            [('brief_title', 'text'), ('conditions_str', 'text'), ('eligibility_criteria', 'text')],
            name='trial_text_index',
            default_language='english',
            weights={'brief_title': 10, 'conditions_str': 8, 'eligibility_criteria': 2}
        )
        print("      Text index ready (weights: title=10, conditions=8, eligibility=2).")
    except Exception as e:
        print(f"      [WARN] Text index: {e}")

    # Optionally wipe embeddings so old text-embedding-004 vectors are replaced
    if FORCE_REEMBED:
        print("\n[!] FORCE_REEMBED=1 — wiping all existing embeddings for re-generation...")
        result = col.update_many({}, {'$unset': {'eligibility_criteria_embedding': ''}})
        print(f"    Cleared embeddings from {result.modified_count} documents.")

    # Fetch all trials from CT.gov using condition-specific query
    print("\n[2/5] Fetching trials from ClinicalTrials.gov (query.cond)...")
    all_trials = {}
    for cond in CONDITIONS:
        trials = fetch_trials(cond)
        for t in trials:
            if t['nct_id']:
                # If trial already fetched under another condition, keep both seed_conditions
                if t['nct_id'] in all_trials:
                    existing_conds = all_trials[t['nct_id']].get('seed_conditions', [])
                    if cond not in existing_conds:
                        existing_conds.append(cond)
                    all_trials[t['nct_id']]['seed_conditions'] = existing_conds
                else:
                    t['seed_conditions'] = [cond]
                    all_trials[t['nct_id']] = t
        print(f"      {cond:35s} -> {len(trials):4d} trials  (total unique: {len(all_trials)})")
        time.sleep(0.4)

    trials_list = list(all_trials.values())
    print(f"\n      Total unique trials fetched: {len(trials_list)}")

    # Upsert metadata
    print("\n[3/5] Upserting trial metadata into MongoDB...")
    total_written = 0
    for i in range(0, len(trials_list), BATCH_SIZE):
        batch = trials_list[i:i + BATCH_SIZE]
        total_written += upsert_batch(col, batch)
    print(f"      Metadata upserted: {total_written} documents")

    # Find which trials need embeddings
    print("\n[4/5] Generating embeddings (text-embedding-005)...")
    already_embedded = set(
        doc['nct_id'] for doc in
        col.find(
            {'eligibility_criteria_embedding': {'$exists': True}},
            {'nct_id': 1, '_id': 0}
        )
    )
    needs_embedding = [t for t in trials_list if t['nct_id'] not in already_embedded]

    print(f"      Already embedded : {len(already_embedded)}")
    print(f"      Need embedding   : {len(needs_embedding)}")

    if not needs_embedding:
        print("      Nothing to do — all trials already have embeddings! ✅")
    else:
        embed_ok = 0
        embed_fail = 0
        for i in range(0, len(needs_embedding), EMBED_BATCH_SIZE):
            batch = needs_embedding[i:i + EMBED_BATCH_SIZE]
            # Rich combined text: title + conditions + eligibility
            texts = [build_embed_text(t) for t in batch]
            vectors = get_embeddings_batch(texts)

            for trial, vec in zip(batch, vectors):
                if vec:
                    col.update_one(
                        {'nct_id': trial['nct_id']},
                        {'$set': {'eligibility_criteria_embedding': vec}}
                    )
                    embed_ok += 1
                else:
                    embed_fail += 1

            if (i // EMBED_BATCH_SIZE) % 10 == 0:
                pct = min(100, round(i / len(needs_embedding) * 100))
                print(f"      {pct:3d}%  ({i}/{len(needs_embedding)})  embedded={embed_ok} failed={embed_fail}")

            time.sleep(0.2)

        print(f"      Done. Embedded: {embed_ok}  Failed: {embed_fail}")

    # Summary
    final_count = col.count_documents({})
    embedded_count = col.count_documents({'eligibility_criteria_embedding': {'$exists': True}})

    print(f"\n[5/5] Summary")
    print(f"   Total documents  : {final_count}")
    print(f"   With embeddings  : {embedded_count}")
    print(f"   Without          : {final_count - embedded_count}")
    print()
    print("Atlas Vector Search index (create/verify in Atlas UI > Search Indexes):")
    print("  Name  : eligibility_vector_index")
    print("  Field : eligibility_criteria_embedding")
    print('''  JSON  :
{\n  "fields": [{\n    "type": "vector",\n    "path": "eligibility_criteria_embedding",\n    "numDimensions": 768,\n    "similarity": "cosine"\n  }]\n}''')
    print()
    print("⚠️  If you previously used text-embedding-004, run with FORCE_REEMBED=1")
    print("   to regenerate all embeddings with text-embedding-005.")
    client.close()


if __name__ == '__main__':
    main()
