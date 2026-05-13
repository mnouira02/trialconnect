# TrialConnect 🧬

**AI-powered clinical trial matching for patients who need it most.**

[![Live Demo](https://img.shields.io/badge/Live%20Demo-Cloud%20Run-blue)](https://trialconnect-404183020569.us-central1.run.app)
[![Built With](https://img.shields.io/badge/Built%20With-MongoDB%20%2B%20GCP-green)](https://github.com/mnouira02/trialconnect)

> Built for the MongoDB + Google Cloud Hackathon 2025

---

## 🌍 The Problem

Over 80% of clinical trials fail to meet enrollment targets, while millions of patients who could benefit never find out they qualify. The gap between patients and trials is a navigation problem — not a supply problem.

## 💡 The Solution

TrialConnect is a full-stack web platform that uses AI to match patients to relevant clinical trials based on their condition, location, medical history, and eligibility criteria — in seconds.

**Live at:** https://trialconnect-404183020569.us-central1.run.app

---

## ✨ Key Features

- **Semantic Trial Search** — MongoDB Atlas vector search finds trials by meaning, not just keywords
- **AI Eligibility Matching** — Gemini 1.5 Pro analyzes trial inclusion/exclusion criteria against your profile
- **Medical Document Upload** — Upload a PDF/image of your medical records; Gemini extracts your profile automatically
- **Proximity Scoring** — Trials ranked by distance to nearest site using geospatial queries
- **AI Agent** — Vertex AI Agent Builder chatbot guides patients to the right trials
- **Promoted Trials** — Sponsor dashboard to boost trial visibility
- **Google OAuth + Local Auth** — Secure login with remember-me support
- **Admin Dashboard** — Full user and content management

---

## 🏗️ Architecture

```
User Browser
    │
    ▼
Flask App (Google Cloud Run)
    │
    ├── MongoDB Atlas
    │     ├── users          (accounts, profiles)
    │     ├── trials         (indexed trial data + vectors)
    │     ├── promoted       (sponsor-boosted trials)
    │     └── contacts       (contact form submissions)
    │
    ├── Vertex AI / Gemini 1.5 Pro
    │     ├── Eligibility matching
    │     ├── Medical document extraction
    │     └── Agent Builder chatbot
    │
    └── ClinicalTrials.gov API
          └── Live trial data source
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Frontend | HTML, Bootstrap 5, Vanilla JS |
| Backend | Python 3.11, Flask |
| Database | MongoDB Atlas (vector search + geospatial) |
| AI/ML | Google Gemini 1.5 Pro (Vertex AI) |
| Agent | Vertex AI Agent Builder |
| Hosting | Google Cloud Run |
| Auth | Google OAuth 2.0 + Werkzeug password hashing |
| Data | ClinicalTrials.gov REST API |
| DevOps | Docker, Cloud Build, Artifact Registry |

---

## 🚀 Running Locally

### Prerequisites
- Python 3.11+
- MongoDB Atlas account
- Google Cloud project with Vertex AI enabled

### Setup

```bash
git clone https://github.com/mnouira02/trialconnect.git
cd trialconnect
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Mac/Linux
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the root:

```env
FLASK_SECRET_KEY=your-secret-key-here
MONGODB_URI=mongodb+srv://...
GOOGLE_CLIENT_ID=your-google-oauth-client-id
GOOGLE_CLIENT_SECRET=your-google-oauth-client-secret
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
VERTEX_AI_LOCATION=us-central1
GOOGLE_MAPS_API_KEY=your-maps-api-key
```

### Run

```bash
python run.py
```

App runs at `http://localhost:5000`

---

## ☁️ Deploy to Cloud Run

```bash
gcloud run deploy trialconnect \
  --source . \
  --region us-central1 \
  --project YOUR_PROJECT_ID
```

---

## 🧠 How the AI Matching Works

1. **Basic match** — Age and sex checked against trial inclusion criteria using rule-based logic
2. **Semantic search** — MongoDB Atlas vector search finds trials semantically related to the patient's condition
3. **Gemini eligibility check** — Full eligibility text fetched from ClinicalTrials.gov API and analyzed by Gemini 1.5 Pro against the patient's complete medical profile
4. **Score** — Trials ranked by a composite score: semantic similarity + proximity + recruitment status + eligibility match

---

## 📁 Project Structure

```
trialconnect/
├── trialconnect/
│   ├── __init__.py        # App factory
│   ├── routes.py          # All Flask routes + OpenAPI spec
│   ├── oauth_setup.py     # Google OAuth configuration
│   ├── static/            # CSS, JS, images
│   └── templates/         # Jinja2 HTML templates
├── helpers.py             # MongoDB, Gemini, search logic
├── Dockerfile             # Container definition
├── requirements.txt       # Python dependencies
└── run.py                 # Local dev entry point
```

---

## 🔗 API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/search` | GET | Search trials by query + location |
| `/api/check_match/<nct_id>` | GET/POST | AI eligibility check for a trial |
| `/api/upload_profile` | POST | Extract medical profile from document |
| `/api/openapi.json` | GET | OpenAPI 3.0 spec for agent integration |

---

## 👥 Team

Built with ❤️ for patients navigating the clinical trial landscape.

---

## 📄 License

MIT License
