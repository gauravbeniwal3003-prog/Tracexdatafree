import os
import requests
import time
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from typing import Optional, Dict, Any

# --- PRODUCTION CONFIGURATION ---
app = FastAPI(title="TraceXData Intelligence PRO")

# Global CORS for Public SaaS API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ENGINE STATE (Lazy-loading for Render Stability) ---
_db: Optional[Client] = None

def get_supabase() -> Optional[Client]:
    """Ensures server doesn't crash if env vars are missing during cold start."""
    global _db
    if _db is None:
        url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if url and key:
            try:
                _db = create_client(url, key)
            except:
                return None
    return _db

# --- THE "TECH VISHAL" STYLE FORMATTER ---
def build_output(raw_json: dict, query_num: str, plan_info: dict, usage: int):
    # Normalize results into a list first
    items = raw_json.get('results') or raw_json.get('data') or raw_json.get('records')
    if items is None and raw_json.get('status') is True:
        items = [raw_json]
    
    # Map into "Result 1", "Result 2" layout
    result_map = {}
    if isinstance(items, list):
        for i, item in enumerate(items, 1):
            if not isinstance(item, dict): continue
            result_map[f"Result {i}"] = {
                "Full Name": str(item.get('name', item.get('full_name', 'N/A'))).upper(),
                "Mobile No": str(item.get('mobile', item.get('number', query_num))),
                "Address": str(item.get('address', 'N/A')),
                "Operator": str(item.get('operator', item.get('carrier', 'N/A'))).upper(),
                "Circle": str(item.get('circle', item.get('state', 'N/A'))).upper()
            }

    # Format the final unified response
    return {
        "status": "success" if result_map else "no_data",
        "Powered_by": "@gaurav_beniwal_0001",
        "Owner": "@gaurav_beniwal_0001",
        "Buy_API": "https://tracexnumber.web.app/buy-api",
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "API_Info": {
            "query": query_num,
            "plan": plan_info.get('plan_name', 'Basic'),
            "expires": plan_info.get('expires_at', 'N/A'),
            "used": usage
        },
        "results": result_map if result_map else "No records found in database"
    }

# --- PRIMARY GATEWAY ---

@app.get("/")
async def index():
    return {
        "status": "Online",
        "engine": "TraceX Intelligence Node",
        "version": "2.8.0-STABLE"
    }

@app.get("/api/lookup")
async def saas_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    number: Optional[str] = Query(None),
    query: Optional[str] = Query(None)
):
    """
    Fixed 422 Error: Used Query(None) to make parameters truly optional 
    so my custom logic can return a helpful JSON error.
    """
    start_time = time.time()
    num = (number or query or "").strip()

    # 1. Parameter Validation
    if not key:
        return {"status": "error", "message": "Access Denied: Please provide your 'key' parameter"}
    
    if not num:
        return {"status": "error", "message": "Input Required: Please provide a 10-digit number"}

    if not num.isdigit() or len(num) != 10:
        return {"status": "error", "message": f"Invalid Data: '{num}' is not a valid 10-digit numeric mobile number"}

    # 2. Database Health
    db = get_supabase()
    if not db:
        return {"status": "error", "message": "ServerDown: Internal database link broken (Check Env Vars)"}

    try:
        # 3. Key Authentication
        auth = db.table("api_keys").select("*").eq("api_key", key).single().execute()
        if not auth.data:
            return {"status": "error", "message": "Auth Failed: Invalid API key provided"}
        
        license = auth.data
        
        # 4. Status Check
        if license['status'] != 'active':
            return {"status": "error", "message": "Key Suspended: Your API access is currently disabled"}

        # 5. Expiry Check
        try:
            exp_date = datetime.fromisoformat(license['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
            if exp_date < datetime.utcnow():
                return {"status": "error", "message": "Key Expired: Please renew your subscription to continue"}
        except:
            pass

        # 6. Usage Quota
        if license['request_limit'] and int(license['requests_used']) >= int(license['request_limit']):
            return {"status": "error", "message": "Quota Exhausted: Your plan limit has been reached for today"}

        # 7. Intelligence Source Fetch
        settings = db.table("api_settings").select("real_api_url").limit(1).single().execute()
        target_template = settings.data['real_api_url'] if settings.data else os.getenv("REAL_LOOKUP_URL")
        
        if not target_template:
            return {"status": "error", "message": "ServerDown: Backend intelligence URL not set in API Settings"}

        # 8. Execution with Fail-Safe Timeout
        final_url = target_template.replace("ENTER_TARGET_HERE", num)
        
        try:
            resp = requests.get(final_url, timeout=12, headers={"User-Agent": "TraceX-SaaS-Node"})
            if resp.status_code != 200:
                return {"status": "error", "message": f"ServerDown: Remote source returned status {resp.status_code}"}
            
            payload = resp.json()
        except requests.exceptions.Timeout:
            return {"status": "error", "message": "ServerDown: Remote source timed out (Gateway Timeout)"}
        except:
            return {"status": "error", "message": "ServerDown: Connectivity issue between node and source"}

        # 9. Update Transaction Log
        new_count = (license.get('requests_used') or 0) + 1
        db.table("api_keys").update({
            "requests_used": new_count,
            "last_used_at": datetime.utcnow().isoformat()
        }).eq("id", license['id']).execute()

        # 10. Delivery
        output = build_output(payload, num, license, new_count)

        # 11. Async Trace (Best-effort logging)
        try:
            db.table("api_logs").insert({
                "api_key_id": license['id'],
                "masked_number": f"{num[:5]}****",
                "status": output['status'],
                "response_time_ms": int((time.time() - start_time) * 1000),
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
            }).execute()
        except: pass

        return output

    except Exception as e:
        print(f"CRITICAL FAULT: {e}")
        return {"status": "error", "message": "ServerDown: Internal engine mapping error (TX-INTERNAL-FAULT)"}

if __name__ == "__main__":
    import uvicorn
    # Render provides PORT env var, default to 10000 for standard Render deploys
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
    
