import os
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

# Tell ADK to use Vertex AI (uses gcloud ADC credentials) instead of Gemini API
os.environ.setdefault('GOOGLE_GENAI_USE_VERTEXAI', 'true')
os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'trialconnect-app')
os.environ.setdefault('GOOGLE_CLOUD_LOCATION', 'us-central1')

# Single source of truth for the TrialConnect agent.
# Used by both Flask (routes.py) and Google Agent Builder studio.
root_agent = LlmAgent(
    name='trialconnect_agent',
    model='gemini-2.5-flash',
    description=(
        'TrialConnect clinical trial search platform. Contains information about clinical trials, '
        'eligibility criteria, locations, and patient matching. Use this to answer questions about '
        'finding clinical trials, checking eligibility, and understanding trial requirements.'
    ),
    instruction=(
        'You are a clinical trial assistant. When a user describes their condition and location:\n'
        '1. Call search_trials with their condition and location\n'
        '2. Present the top results with trial name, status, and distance\n'
        '3. Ask if they want an eligibility check — if yes, call check_eligibility with their NCT ID and profile details\n'
        'Always recommend consulting a doctor before enrolling.'
    ),
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url='https://trialconnect-404183020569.us-central1.run.app/mcp',
            )
        )
    ],
)
