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

# --- CLEAN PROFESSIONAL FORMATTER ---
def build_output(raw_json: dict, query_num: str, plan_info: dict, usage: int):

    # Detect items from upstream API
    items = (
        raw_json.get('results')
        or raw_json.get('data')
        or raw_json.get('records')
    )

    clean_results = []

    # CASE 1: results is dict
    if isinstance(items, dict):

        for _, val in items.items():

            if isinstance(val, dict):

                clean_results.append({
                    "name": str(
                        val.get('name', val.get('full_name', 'N/A'))
                    ).upper(),

                    "father_name": str(
                        val.get('father_name', val.get('fathername', 'N/A'))
                    ).upper(),

                    "mobile": str(
                        val.get('mobile', val.get('number', query_num))
                    ),

                    "alt_mobile": str(
                        val.get('alt_mobile', 'N/A')
                    ),

                    "email": str(
                        val.get('email', 'N/A')
                    ),

                    "aadhar_number": str(
                        val.get('aadhar_number', 'N/A')
                    ),

                    "operator": str(
                        val.get('operator', val.get('carrier', 'N/A'))
                    ).upper(),

                    "state_circle": str(
                        val.get(
                            'circle',
                            val.get(
                                'state_circle',
                                val.get('state', 'N/A')
                            )
                        )
                    ).upper(),

                    "address": str(
                        val.get('address', val.get('location', 'N/A'))
                    )
                })

    # CASE 2: results is list
    elif isinstance(items, list):

        for val in items:

            if isinstance(val, dict):

                clean_results.append({
                    "name": str(
                        val.get('name', val.get('full_name', 'N/A'))
                    ).upper(),

                    "father_name": str(
                        val.get('father_name', val.get('fathername', 'N/A'))
                    ).upper(),

                    "mobile": str(
                        val.get('mobile', val.get('number', query_num))
                    ),

                    "alt_mobile": str(
                        val.get('alt_mobile', 'N/A')
                    ),

                    "email": str(
                        val.get('email', 'N/A')
                    ),

                    "aadhar_number": str(
                        val.get('aadhar_number', 'N/A')
                    ),

                    "operator": str(
                        val.get('operator', val.get('carrier', 'N/A'))
                    ).upper(),

                    "state_circle": str(
                        val.get(
                            'circle',
                            val.get(
                                'state_circle',
                                val.get('state', 'N/A')
                            )
                        )
                    ).upper(),

                    "address": str(
                        val.get('address', val.get('location', 'N/A'))
                    )
                })

    # CASE 3: raw object itself
    elif raw_json.get('status') is True or raw_json.get('name'):

        clean_results.append({
            "name": str(
                raw_json.get('name', 'N/A')
            ).upper(),

            "father_name": str(
                raw_json.get('father_name', 'N/A')
            ).upper(),

            "mobile": str(
                raw_json.get('mobile', query_num)
            ),

            "alt_mobile": str(
                raw_json.get('alt_mobile', 'N/A')
            ),

            "email": str(
                raw_json.get('email', 'N/A')
            ),

            "aadhar_number": str(
                raw_json.get('aadhar_number', 'N/A')
            ),

            "operator": str(
                raw_json.get('operator', 'N/A')
            ).upper(),

            "state_circle": str(
                raw_json.get('circle', 'N/A')
            ).upper(),

            "address": str(
                raw_json.get('address', 'N/A')
            )
        })

    # FINAL CLEAN OUTPUT
    return {
        "status": "success" if clean_results else "no_data",

        "provider": "TraceXData Intelligence PRO",

        "developer": "@gaurav_beniwal_0001",

        "timestamp": datetime.utcnow().strftime(
            "%d-%m-%Y %I:%M:%S %p"
        ),

        "api_info": {
            "query": query_num,
            "plan": plan_info.get('plan_name', 'Basic'),
            "expires": plan_info.get('expires_at', 'N/A'),
            "used_requests": usage
        },

        "results_found": len(clean_results),

        "data": clean_results if clean_results else [],

        "support": "https://tracexnumber.web.app/support"
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
    start_time = time.time()
    num = (number or query or "").strip()

    # 1. Parameter Validation
    # Allow a special internal key for the website or check if there's no key but it's a valid number
    if not key:
        # Check if this is an internal website request (optional: verify Referer header)
        # For now, we will require a key but I'll provide a 'Master Key' logic or allow bypass for testing
        return {"status": "error", "message": "Access Denied: Please provide your 'key' parameter"}
    
    if not num:
        return {"status": "error", "message": "Input Required: Please provide a 10-digit number"}

    # 2. Strict 10-Digit Validation
    if not num.isdigit() or len(num) != 10:
        return {"status": "error", "message": f"Invalid Data: '{num}' is not a 10-digit mobile number"}

    # 3. Master Key / System Key Check (Optional: For your own website)
    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN" # You can use this for your website

    db = get_supabase()
    if not db:
        return {"status": "error", "message": "ServerDown: Database connection failure"}

    try:
        # 4. Key Authentication (Skip if master key used)
        if not is_master:
            auth = db.table("api_keys").select("*").eq("api_key", key).single().execute()
            if not auth.data:
                return {"status": "error", "message": "Auth Failed: Invalid API key"}
            
            license = auth.data
            
            # 5. Status & Expiry Check
            if license['status'] != 'active':
                return {"status": "error", "message": "Key Suspended: Access disabled"}

            try:
                exp_date = datetime.fromisoformat(license['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                if exp_date < datetime.utcnow():
                    return {"status": "error", "message": "Key Expired: Please renew subscription"}
            except:
                pass

            # 6. Usage Quota
            if license['request_limit'] and int(license['requests_used']) >= int(license['request_limit']):
                return {"status": "error", "message": "Quota Exhausted: Plan limit reached"}
        else:
            # Fake license data for master key
            license = {"id": "system", "plan_name": "Internal VIP", "requests_used": 0, "expires_at": "Never"}

        # 7. Intelligence Source Fetch
        settings = db.table("api_settings").select("real_api_url").limit(1).single().execute()
        target_template = settings.data['real_api_url'] if settings.data else os.getenv("REAL_LOOKUP_URL")
        
        if not target_template:
             return {"status": "error", "message": "ServerDown: Backend URL not configured"}

        # 8. Execution
        final_url = target_template.replace("ENTER_TARGET_HERE", num)
        
        try:
            resp = requests.get(final_url, timeout=12, headers={"User-Agent": "TraceX-SaaS-Node"})
            if resp.status_code != 200:
                return {"status": "error", "message": "ServerDown: Data source unresponsive"}
            
            payload = resp.json()
        except:
            return {"status": "error", "message": "ServerDown: Gateway connection timeout"}

        # 9. Update Usage (Only for real API keys)
        if not is_master:
            new_count = (license.get('requests_used') or 0) + 1
            db.table("api_keys").update({
                "requests_used": new_count,
                "last_used_at": datetime.utcnow().isoformat()
            }).eq("id", license['id']).execute()
            usage_display = new_count
        else:
            usage_display = 0

        # 10. Delivery
        output = build_output(payload, num, license, usage_display)

        # 11. Logging
        try:
            db.table("api_logs").insert({
                "api_key_id": license.get('id') if not is_master else None,
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
    
