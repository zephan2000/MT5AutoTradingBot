# lib/supa.py
import os
from supabase import create_client, Client
from dotenv import load_dotenv   # 👈 add this

# load .env before accessing environment variables
load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

def user_client() -> Client:
    return create_client(SUPABASE_URL, ANON_KEY)

def service_client() -> Client:
    # WARNING: server-side only
    return create_client(SUPABASE_URL, SERVICE_KEY)
