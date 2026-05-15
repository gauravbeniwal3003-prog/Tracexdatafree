import os
import secrets
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional, List, Union

app = FastAPI(title="TraceXData Intelligence Engine")

# CORS Setup - Essential for web access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
# Ensure these environment variables are set in Render!
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") 
supabase: Client = create_client(supabase_url, supabase_key)

# --- HELPER: RESPONSE FILTERING & NORMALIZATION ---
def clean_response(raw_data: Union[dict, list], query: str, plan: str, expiry: str, used: int):
    # 1. Normalize input to a list of results
    results = []
    if isinstance(raw_data, list):
        results = raw_data
    elif isinstance(raw_data, dict):
        # Check common keys: 'results', 'data', 'records'
        results = raw_data.get('results') or raw_data.get('data') or raw_data.get('records')
        if results is None:
            # If no obvious list, check if the dict itself looks like a result
            if any(k in raw_data for k in ['name', 'mobile', 'full_name', 'number']):
                results = [raw_data]
            else:
                # Last resort: find any list in the values
                for v in raw_data.values():
                    if isinstance(v, list):
                        results = v
                        break
                if results is None: results = []
    
    # 2. Map and Sanitize
    data_list = []
    items_to_process = results if isinstance(results, list) else [results]
    
    for idx, item in enumerate(items_to_process, 1):
        if not isinstance(item, dict): continue
        
        # Standard TraceX Mapping
        obj = {
            "result_no": idx,
            "name": str(item.get('name', item.get('full_name', item.get('owner', 'N/A')))).upper(),
            "mobile": str(item.get('mobile', item.get('number', item.get('phone', query)))),
            "alt_mobile": str(item.get('alt_mobile', item.get('alt_number', 'N/A'))),
            "operator": str(item.get('operator', item.get('carrier', 'N/A'))).upper(),
            "circle": str(item.get('circle', item.get('state', item.get('region', 'N/A')))).upper(),
            "address": str(item.get('address', item.get('location', 'N/A')))
        }
        
        # Strip ugly null strings
        for k, v in obj.items():
            if not v or str(v).lower() in ['null', 'na', 'none', 'n-a', 'undefined', '']: 
                obj[k] = "N/A"
        
        data_list.append(obj)

    # 3. Calculate Meta-Data
    try:
        expires_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
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

# --- PUBLIC ROUTES ---

@app.get("/")
async def root():
    return {
        "status": "online",
        "engine": "TraceXData Intelligence SaaS",
        "api_docs": "https://tracexnumber.web.app/api-docs"
    }

@app.get("/api/lookup")
async def api_gateway(key: str = None, number: str = None, request: Request = None):
    start_time = datetime.utcnow()
    client_ip = request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"

    # --- INPUT VALIDATION ---
    if not key or not number:
        return {
            "status": "error", 
            "message": "Missing 'key' or 'number' parameter. Example: /api/lookup?key=TX-XXXXX&number=9876543210"
        }

    try:
        # 1. AUTHENTICATION & KEY CHECK
        res = supabase.table("api_keys").select("*").eq("api_key", key).single().execute()
        
        if not res.data:
            return {"status": "error", "message": "Access Denied: Invalid API key"}
        
        api_record = res.data
        
        # 2. STATUS & EXPIRY CHECK
        try:
            expiry_str = api_record['expires_at'].replace('Z', '+00:00')
            expiry_dt = datetime.fromisoformat(expiry_str)
        except:
            expiry_dt = datetime.utcnow() - timedelta(days=1)

        if expiry_dt < datetime.utcnow() or api_record['status'] != 'active':
            return {
                "status": "error", 
                "message": "Access Denied: Key expired or account suspended", 
                "buy_api": "https://tracexnumber.web.app/buy-api"
            }

        # 3. QUOTA CHECK
        if api_record['request_limit'] is not None and api_record['requests_used'] >= api_record['request_limit']:
            return {
                "status": "error", 
                "message": f"Quota Exceeded: Your plan limit ({api_record['request_limit']}) has been reached."
            }

        # 4. TARGET ENGINE FETCH
        settings_res = supabase.table("api_settings").select("*").limit(1).single().execute()
        real_url_template = settings_res.data['real_api_url'] if settings_res.data else os.getenv("REAL_LOOKUP_URL")
        
        if not real_url_template:
            return {"status": "error", "message": "Intelligence engine offline. System configuration missing."}

        target_url = real_url_template.replace("ENTER_TARGET_HERE", number)

        # 5. EXECUTE TARGET REQUEST
        try:
            resp = requests.get(target_url, timeout=12, headers={"User-Agent": "TraceX-Engine/2.1"})
            
            if resp.status_code != 200:
                return {"status": "error", "message": "Source Engine Error: Backend timeout or invalid target source."}

            raw_json = resp.json()
            
            # 6. USAGE UPDATE (Increment database)
            new_count = api_record['requests_used'] + 1
            supabase.table("api_keys").update({
                "requests_used": new_count,
                "last_used_at": datetime.utcnow().isoformat()
            }).eq("id", api_record['id']).execute()

            # 7. RESPONSE REFINEMENT
            result = clean_response(raw_json, number, api_record['plan_name'], api_record['expires_at'], new_count)

            # 8. AUDIT LOGGING
            try:
                supabase.table("api_logs").insert({
                    "api_key_id": api_record['id'],
                    "masked_number": f"{number[:5]}****",
                    "status": result['status'],
                    "response_time_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000),
                    "ip_address": str(client_ip),
                    "endpoint": "/api/lookup"
                }).execute()
            except: pass # Don't break if logging fails

            return result

        except requests.exceptions.Timeout:
            return {"status": "error", "message": "Gateway Timeout: Intelligence source took too long to respond."}
        except Exception as e:
            return {"status": "error", "message": f"Engine Fault: {str(e)}"}

    except Exception as e:
        return {"status": "error", "message": f"System Error: {str(e)}"}

if __name__ == "__main__":
    import uvicorn
    # Make sure to set PORT environment variable on Render
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
