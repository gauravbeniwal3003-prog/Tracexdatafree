import os
import time
import requests
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from datetime import datetime, timedelta
from typing import Optional

app = FastAPI(title="TraceXData Intelligence API")

# Security: CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database Initialization
SUPABASE_URL = os.getenv("VITE_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("VITE_SUPABASE_ANON_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- RESPONSE REFINEMENT ENGINE ---
def clean_response(raw_data, query_num, plan, expiry, usage):
    """
    Transforms messy remote API data into the TraceXData Premium Standard.
    """
    # Extract results list/dict
    results_raw = raw_data.get("results") or raw_data.get("data") or raw_data.get("records")
    
    data_list = []
    
    if results_raw:
        # Handle dictionary-style results (e.g., {"Result 1": {...}})
        items = results_raw.values() if isinstance(results_raw, dict) else results_raw
        
        if isinstance(items, list):
            for idx, item in enumerate(items):
                if not isinstance(item, dict): continue
                
                # Standardized Mapping
                entry = {
                    "result_no": idx + 1,
                    "name": str(item.get("name") or item.get("full_name") or "N/A").upper(),
                    "mobile": str(item.get("mobile") or item.get("number") or query_num),
                    "alt_mobile": str(item.get("alt_mobile") or item.get("alt_number") or "N/A"),
                    "operator": str(item.get("operator") or item.get("carrier") or "N/A").upper(),
                    "circle": str(item.get("state_circle") or item.get("circle") or item.get("state") or "N/A").upper(),
                    "address": str(item.get("address") or item.get("location") or "N/A")
                }
                
                # Clean up dirty strings
                for k in entry:
                    v = str(entry[k]).strip()
                    if not v or v.lower() in ["null", "none", "n/a", "na", "-", "undefined"]:
                        entry[k] = "N/A"
                
                data_list.append(entry)

    status = "success" if data_list else "no_results"
    message = f"Found {len(data_list)} matching records" if data_list else "No intelligence records found for this number"

    return {
        "status": status,
        "message": message,
        "query": query_num,
        "engine": "TraceXData Evolution v2.5",
        "plan": plan,
        "expires": expiry,
        "requests_used": usage,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "data": data_list,
        "powered_by": "@gaurav_beniwal_0001"
    }

# --- GATEWAY STATUS ---
@app.get("/")
async def status_check():
    return {
        "status": "online",
        "engine": "TraceXData Intelligence Platform",
        "version": "2.1.0-PROD",
        "author": "@gaurav_beniwal_0001",
        "api_docs": "https://tracexnumber.web.app/docs"
    }

# --- PRIMARY SAAS GATEWAY ---
@app.get("/api/lookup")
async def intelligence_gateway(
    request: Request,
    key: str = Query(None),
    number: Optional[str] = Query(None),
    query: Optional[str] = Query(None) # Support both param styles
):
    start_time = datetime.utcnow()
    client_ip = request.headers.get('x-forwarded-for', request.client.host)
    
    # Resolve target number (number or query)
    target_number = (number or query or "").strip()

    # 1. Parameter Validation
    if not key:
        return {"status": "error", "message": "API authentication key is missing"}
    
    if not target_number:
        return {"status": "error", "message": "Query parameter 'number' is required"}

    # 2. Strict 10-Digit Validation
    if not target_number.isdigit() or len(target_number) != 10:
        return {
            "status": "error", 
            "message": "Validation Failed: Input must be a valid 10-digit numeric phone number"
        }

    try:
        # 3. Dynamic Key Authentication
        res = supabase.table("api_keys").select("*").eq("api_key", key).single().execute()
        
        if not res.data:
            return {
                "status": "error", 
                "message": "Invalid Key: The provided access token is not recognized by our system"
            }
        
        api_record = res.data
        
        # 4. Expiry & Security Checks
        try:
            expiry_str = api_record['expires_at'].replace('Z', '+00:00')
            expiry_dt = datetime.fromisoformat(expiry_str)
        except:
            expiry_dt = datetime.utcnow() - timedelta(days=1)

        if expiry_dt < datetime.utcnow() or api_record['status'] != 'active':
            return {
                "status": "error", 
                "message": "Subscription Status: Your API key has expired or been temporarily suspended", 
                "buy_api": "https://tracexnumber.web.app/buy-api"
            }

        # 5. Usage Quota Check
        if api_record['request_limit'] is not None and api_record['requests_used'] >= api_record['request_limit']:
            return {
                "status": "error", 
                "message": "Quota Reached: Your plan lookup limit has been exhausted for this period"
            }

        # 6. Resolve Intelligence Source
        settings_res = supabase.table("api_settings").select("*").limit(1).single().execute()
        real_url_template = settings_res.data['real_api_url'] if settings_res.data else os.getenv("REAL_LOOKUP_URL")
        
        if not real_url_template:
            return {"status": "error", "message": "System Error: Intelligence source node not configured"}

        target_url = real_url_template.replace("ENTER_TARGET_HERE", target_number)

        # 7. Execute Secure Proxy Fetch
        try:
            resp = requests.get(
                target_url, 
                timeout=12, 
                headers={"User-Agent": "TraceXData-Intelligence-Engine/2.5"}
            )
            
            if resp.status_code != 200:
                return {
                    "status": "error", 
                    "message": f"External Engine Error: Source node returned status {resp.status_code}"
                }

            raw_json = resp.json()
        except requests.exceptions.Timeout:
            return {"status": "error", "message": "Connection Timeout: The intelligence source is taking too long to respond"}
        except Exception as e:
            return {"status": "error", "message": f"Source Connectivity fault: {str(e)}"}
        
        # 8. Transaction Finalization
        new_count = (api_record.get('requests_used') or 0) + 1
        supabase.table("api_keys").update({
            "requests_used": new_count,
            "last_used_at": datetime.utcnow().isoformat()
        }).eq("id", api_record['id']).execute()

        # 9. Format Refined Response
        final_output = clean_response(raw_json, target_number, api_record['plan_name'], api_record['expires_at'], new_count)

        # 10. Audit Logging (Async-like)
        try:
            supabase.table("api_logs").insert({
                "api_key_id": api_record['id'],
                "masked_number": f"{target_number[:5]}****",
                "status": final_output['status'],
                "response_time_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000),
                "ip_address": str(client_ip),
                "endpoint": "/api/lookup"
            }).execute()
        except: 
            pass

        return final_output

    except Exception as e:
        print(f"[FATAL_GATEWAY_ERROR] {e}")
        return {
            "status": "error", 
            "message": "Internal Intelligence Fault: An unexpected error occurred on the gateway"
        }

if __name__ == "__main__":
    import uvicorn
    # Bind to 0.0.0.0 and PORT 3000 for standard deployment
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
