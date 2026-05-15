import os
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from typing import Optional

app = FastAPI(title="TraceXData Intelligence Engine")

# CORS Setup for Frontend Integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# --- HELPER: RESPONSE FILTERING & STANDARDIZATION ---
def clean_response(raw_data: dict, query: str, plan: str, expiry: str, used: int):
    # Determine the results array from the raw API response (Handles various provider formats)
    results = raw_data.get('results') or raw_data.get('data') or ([raw_data] if raw_data.get('status') == True else [])
    
    data_list = []
    if isinstance(results, list):
        for idx, item in enumerate(results, 1):
            if not isinstance(item, dict): continue
            
            # Map fields to TraceX Standards
            obj = {
                "result_no": idx,
                "name": str(item.get('name', item.get('full_name', 'N/A'))).upper(),
                "mobile": str(item.get('mobile', item.get('number', query))),
                "alt_mobile": str(item.get('alt_mobile', 'N/A')),
                "operator": str(item.get('operator', 'N/A')).upper(),
                "circle": str(item.get('circle', item.get('state', 'N/A'))).upper(),
                "address": str(item.get('address', 'N/A'))
            }
            # Clean duplicate/null values
            for k, v in obj.items():
                if not v or str(v).lower() in ['null', 'na', 'none', 'n-a', '']: obj[k] = "N/A"
            
            data_list.append(obj)

    # Calculate Subscription Time Left
    try:
        expires_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00')).replace(tzinfo=None)
        delta = expires_dt - datetime.utcnow()
        hours = max(0, int(delta.total_seconds() // 3600))
        mins = max(0, int((delta.total_seconds() % 3600) // 60))
        time_left_str = f"{hours}h {mins}m"
    except:
        time_left_str = "Unknown"

    return {
        "status": "success" if data_list else "not_found",
        "powered_by": "TraceXData Intelligence",
        "owner": "@gaurav_beniwal_0001",
        "buy_api": "https://tracexnumber.web.app/buy-api",
        "query": query,
        "api_status": {
            "plan": plan,
            "expires_at": expiry,
            "time_left": time_left_str,
            "requests_used": used
        },
        "results_found": len(data_list),
        "data": data_list
    }

# --- ROOT STATUS ---
@app.get("/")
async def root():
    return {
        "status": "online",
        "engine": "TraceXData Intelligence Node",
        "version": "2.5.0-PROD",
        "author": "@gaurav_beniwal_0001"
    }

# --- SAAS PUBLIC ENDPOINT ---
@app.get("/api/lookup")
async def api_gateway(
    key: str, 
    number: Optional[str] = None, 
    query: Optional[str] = None, 
    request: Request = None
):
    start_time = datetime.utcnow()
    client_ip = "0.0.0.0"
    if request:
        client_ip = request.headers.get('x-forwarded-for', request.client.host)

    # 1. Parameter Normalization
    target_number = (number or query or "").strip()

    # 2. Key Presence Check
    if not key:
        return {"status": "error", "message": "Access Denied: API key parameter 'key' is required"}
    
    # 3. Number Presence Check
    if not target_number:
        return {"status": "error", "message": "Missing Query: Please provide a phone number using 'number' or 'query' parameter"}

    # 4. Strict 10-Digit Validation
    if not target_number.isdigit() or len(target_number) != 10:
        return {
            "status": "error", 
            "message": f"Invalid Format: '{target_number}' is not a valid 10-digit number. Lookups are restricted to exactly 10 numeric digits."
        }

    try:
        # 5. Database: Validate API Key
        res = supabase.table("api_keys").select("*").eq("api_key", key).single().execute()
        if not res.data:
            return {
                "status": "error", 
                "message": "Authentication Failed: The provided API key is invalid or unauthorized"
            }
        
        api_record = res.data
        
        # 6. Database: Expiry & Access Status
        try:
            expiry_str = api_record['expires_at'].replace('Z', '+00:00')
            expiry_date = datetime.fromisoformat(expiry_str).replace(tzinfo=None)
        except:
            expiry_date = datetime.utcnow() - timedelta(days=1)

        if expiry_date < datetime.utcnow() or api_record['status'] != 'active':
            return {
                "status": "error", 
                "message": "Subscription Blocked: Your API access key has expired or is currently suspended", 
                "buy_api": "https://tracexnumber.web.app/buy-api"
            }

        # 7. Database: Usage Quota Enforcer
        if api_record['request_limit'] is not None and api_record['requests_used'] >= api_record['request_limit']:
            return {
                "status": "error", 
                "message": "Quota Exhausted: You have reached the maximum lookup limit for your plan"
            }

        # 8. Engine: Resolve Hidden Target Node
        settings_res = supabase.table("api_settings").select("*").limit(1).single().execute()
        real_url_template = settings_res.data['real_api_url'] if settings_res.data else os.getenv("REAL_LOOKUP_URL")
        
        if not real_url_template:
            return {
                "status": "error", 
                "message": "Gateway Offline: The remote intelligence engine is not configured for this node"
            }

        target_url = real_url_template.replace("ENTER_TARGET_HERE", target_number)

        # 9. Intelligence Fetch with 12s Gateway Timeout
        try:
            resp = requests.get(
                target_url, 
                timeout=12, 
                headers={
                    "User-Agent": "TraceXData-Intelligence/2.5 (PROD-Gateway)",
                    "Accept": "application/json"
                }
            )
            
            if resp.status_code != 200:
                return {
                    "status": "error", 
                    "message": f"Source Timeout: Internal lookup engine returned status {resp.status_code}"
                }

            raw_json = resp.json()
        except requests.exceptions.Timeout:
            return {"status": "error", "message": "Source Timeout: The remote data node failed to respond within 12 seconds"}
        except Exception:
            return {"status": "error", "message": "Connectivity Error: Failed to establish secure relay with the intelligence source"}
        
        # 10. Database: Commit Usage Update
        new_count = (api_record.get('requests_used') or 0) + 1
        supabase.table("api_keys").update({
            "requests_used": new_count,
            "last_used_at": datetime.utcnow().isoformat()
        }).eq("id", api_record['id']).execute()

        # 11. Core Logic: Refine and Return
        final_output = clean_response(raw_json, target_number, api_record['plan_name'], api_record['expires_at'], new_count)

        # 12. Logging: Background Pulse (Best Effort)
        try:
            supabase.table("api_logs").insert({
                "api_key_id": api_record['id'],
                "masked_number": f"{target_number[:5]}****",
                "status": final_output['status'],
                "response_time_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000),
                "ip_address": str(client_ip),
                "endpoint": "/api/lookup"
            }).execute()
        except: pass

        return final_output

    except Exception as e:
        print(f"[PROD_FATAL_ENGINE_FAULT] {e}")
        return {
            "status": "error", 
            "message": "Internal Gateway Fault: An unexpected logic error occurred"
        }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
