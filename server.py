import os
import secrets
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI(title="TraceXData Intelligence Engine")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") # Use service role for backend logic
supabase: Client = create_client(supabase_url, supabase_key)

# --- HELPER: RESPONSE FILTERING ---
def clean_response(raw_data: dict, query: str, plan: str, expiry: str, used: int):
    # Determine the results array from the raw API response
    results = raw_data.get('results') or raw_data.get('data') or ([raw_data] if raw_data.get('status') == True else [])
    
    data_list = []
    if isinstance(results, list):
        for idx, item in enumerate(results, 1):
            if not isinstance(item, dict): continue
            
            # Map fields to TraceX Standards and remove forbidden fields
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
                if not v or v in ['null', 'NA', 'None', 'n-a']: obj[k] = "N/A"
            
            data_list.append(obj)

    # Calculate Time Left
    try:
        expires_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
        delta = expires_dt - datetime.utcnow()
        hours = max(0, int(delta.total_seconds() // 3600))
        mins = max(0, int((delta.total_seconds() % 3600) // 60))
        time_left_str = f"{hours}h {mins}m"
    except:
        time_left_str = "Expired"

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

# --- SAAS PUBLIC ENDPOINT ---
@app.get("/api/lookup")
async def api_gateway(key: str, number: str, request: Request):
    start_time = datetime.utcnow()
    client_ip = request.headers.get('x-forwarded-for', request.client.host)

    if not key or not number:
        raise HTTPException(status_code=400, detail="Missing API Key or Phone Number")

    # 1. Validate Key
    res = supabase.table("api_keys").select("*").eq("api_key", key).single().execute()
    if not res.data:
        return {"status": "error", "message": "Invalid API key"}
    
    api_record = res.data
    
    # 2. Expiry Check
    expiry_dt = datetime.fromisoformat(api_record['expires_at'].replace('Z', '+00:00'))
    if expiry_dt < datetime.utcnow() or api_record['status'] != 'active':
        return {
            "status": "error", 
            "message": "API key expired", 
            "buy_api": "https://tracexnumber.web.app/buy-api"
        }

    # 3. Limit Check
    if api_record['request_limit'] and api_record['requests_used'] >= api_record['request_limit']:
        return {"status": "error", "message": "Request limit reached"}

    # 4. Get Hidden Real API URL
    settings_res = supabase.table("api_settings").select("real_api_url").limit(1).single().execute()
    real_url_template = settings_res.data['real_api_url'] if settings_res.data else os.getenv("REAL_LOOKUP_URL")
    
    if not real_url_template:
        raise HTTPException(status_code=500, detail="Backend configuration error (Target URL missing)")

    target_url = real_url_template.replace("ENTER_TARGET_HERE", number)

    # 5. Execute Secret Fetch
    try:
        resp = requests.get(target_url, timeout=12)
        raw_json = resp.json()
        
        # 6. Update Usage
        new_count = api_record['requests_used'] + 1
        supabase.table("api_keys").update({
            "requests_used": new_count,
            "last_used_at": datetime.utcnow().isoformat()
        }).eq("id", api_record['id']).execute()

        # 7. Filtered Response
        final_output = clean_response(raw_json, number, api_record['plan_name'], api_record['expires_at'], new_count)

        # 8. Log the request (Async)
        supabase.table("api_logs").insert({
            "api_key_id": api_record['id'],
            "masked_number": f"{number[:5]}****",
            "status": final_output['status'],
            "response_time_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000),
            "ip_address": client_ip
        }).execute()

        return final_output

    except Exception as e:
        print(f"Gateway Error: {e}")
        return {"status": "error", "message": "Internal service timeout"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    
