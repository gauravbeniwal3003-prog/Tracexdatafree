import os
import requests
import time
import secrets
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Query, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from typing import Optional, Dict, Any
from collections import defaultdict

# --- RATE LIMITING ---
ip_records = defaultdict(list)
RATE_LIMIT = 5 # requests
RATE_WINDOW = 10 # seconds

def check_rate_limit(request: Request):
    client_ip = request.headers.get('x-forwarded-for', request.client.host) or "unknown"
    now = time.time()
    
    # Clean up old timestamps
    ip_records[client_ip] = [ts for ts in ip_records[client_ip] if now - ts < RATE_WINDOW]
    
    if len(ip_records[client_ip]) >= RATE_LIMIT:
        return False
        
    ip_records[client_ip].append(now)
    return True

def is_valid_uuid(val):
    if not val:
        return False
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False

# --- PRODUCTION CONFIGURATION ---
app = FastAPI(title="TraceXData Intelligence PRO")

# Global CORS for Public SaaS API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cashfree Configuration
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
CASHFREE_BASE_URL = os.getenv("CASHFREE_BASE_URL", "https://api.cashfree.com/pg")

# --- ENGINE STATE (Lazy-loading for Render Stability) ---
_db: Optional[Client] = None

def get_supabase() -> Optional[Client]:
    """Ensures server doesn't crash if env vars are missing during cold start."""
    global _db
    if _db is None:
        url = os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("VITE_SUPABASE_ANON_KEY")
        if url and key:
            try:
                _db = create_client(url, key)
            except Exception as e:
                print(f"[Supabase] Creation failed: {e}")
                return None
    return _db

async def fulfill_order(order_id: str, user_id: str):
    db = get_supabase()
    if not db:
        return

    try:
        # Check if already fulfilled
        claim_query = db.table("payment_claims").select("*").eq("payment_id", order_id).execute()
        if not claim_query.data or claim_query.data[0]['status'] == 'success':
            return

        claim = claim_query.data[0]
        plan_id = claim['plan_id']
        user_email = claim.get('user_email', 'N/A')

        # Handle manual pgpay guest payments
        if plan_id == "pgpay_manual":
            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            print(f"[SaaS] Manual Guest Payment fulfilled successfully for {order_id}")
            return

        # Check if user_id is a valid UUID
        if not is_valid_uuid(user_id):
            print(f"[FULFILL] Non-UUID user_id '{user_id}' skipped database state updates, marking order {order_id} fulfilled.")
            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            return

        # Check if it's an API Plan
        is_api_plan = 'a15' in plan_id or 'a30' in plan_id or plan_id.startswith('api_')
        if is_api_plan:
            api_key = f"tx_{secrets.token_hex(16)}"
            days = 15
            limit = 500
            plan_name = "15 Days API (500 Req)"

            if 'unl' in plan_id:
                limit = None
                plan_name = "15 Days Unlimited API" if '15' in plan_id else "1 Month Unlimited API"
            
            if '30' in plan_id:
                days = 30
            
            if '1000' in plan_id:
                limit = 1000
                plan_name = "1 Month API (1000 Req)"
            elif '500' in plan_id:
                limit = 500
                plan_name = "15 Days API (500 Req)"

            expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
            
            db.table("api_keys").insert({
                "api_key": api_key,
                "user_id": user_id,
                "user_email": user_email,
                "plan_name": plan_name,
                "duration_days": days,
                "request_limit": limit,
                "expires_at": expires_at,
                "order_id": order_id
            }).execute()

            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            print(f"[FULFILL] Created API Key for {user_id}")
            return

        # Regular Credit/Unlimited Plans
        profile_query = db.table("profiles").select("*").eq("id", user_id).execute()
        if not profile_query.data:
            return
        
        profile = profile_query.data[0]
        update_data = {}

        # Use more flexible ID checking
        if plan_id in ['c10', 'credit_10']: update_data['credits'] = (profile.get('credits') or 0) + 10
        elif plan_id in ['c50', 'credit_50']: update_data['credits'] = (profile.get('credits') or 0) + 50
        elif plan_id in ['c100', 'credit_100']: update_data['credits'] = (profile.get('credits') or 0) + 100
        elif plan_id.startswith('u') or plan_id.startswith('unlimited'):
            # Hours mapping
            hours_map = {
                'u1h': 1, 'unlimited_1h': 1,
                'u1d': 24, 'u24h': 24, 'unlimited_24h': 24, 'unlimited_1d': 24,
                'u1w': 168, 'unlimited_1w': 168,
                'u1m': 720, 'unlimited_1m': 720
            }
            hours = hours_map.get(plan_id, 0)
            if not hours and 'h' in plan_id:
                try: hours = int(plan_id.split('h')[0].replace('u', '').replace('unlimited_', ''))
                except: hours = 0
            
            if hours > 0:
                now = datetime.utcnow()
                start = now
                expiry_str = profile.get('unlimited_expiry')
                if expiry_str:
                    try:
                        clean_expiry = expiry_str.replace('Z', '+00:00')
                        start = datetime.fromisoformat(clean_expiry).replace(tzinfo=None)
                    except Exception as date_err:
                        print(f"[FULFILL] Error parsing unlimited_expiry '{expiry_str}': {date_err}")
                        start = now
                if start < now:
                    start = now
                update_data['unlimited_expiry'] = (start + timedelta(hours=hours)).isoformat()

        if update_data:
            db.table("profiles").update(update_data).eq("id", user_id).execute()
            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            print(f"[FULFILL] Updated profile for {user_id}")
            
    except Exception as e:
        print(f"Fulfillment error: {e}")

@app.post("/api/cashfree/create-order")
async def create_order(payload: dict = Body(...), request: Request = None):
    db = get_supabase()
    if not db:
        return {"error": "Server connection failure"}

    user_id = payload.get("user_id")
    plan_id = payload.get("plan_id")
    amount = payload.get("amount")
    user_email = payload.get("user_email", "customer@example.com")
    customer_phone = payload.get("customer_phone", "9999999999")
    
    # Default origin fallback
    origin = "https://tracexnumber.web.app"
    if request and request.headers.get("origin"):
        origin = request.headers.get("origin")
        
    return_url = payload.get("return_url", f"{origin}?order_id={{order_id}}")

    if not user_id or not plan_id or not amount:
        return {"error": "Missing required parameters"}

    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        return {"error": "Payment gateway credentials not set"}

    order_id = f"order_{int(time.time())}_{secrets.token_hex(3)}"
    
    cf_payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": user_id,
            "customer_email": user_email,
            "customer_phone": customer_phone
        },
        "order_meta": {
            "return_url": return_url
        }
    }

    try:
        headers = {
            "x-client-id": CASHFREE_APP_ID,
            "x-client-secret": CASHFREE_SECRET_KEY,
            "x-api-version": "2023-08-01",
            "Content-Type": "application/json"
        }
        resp = requests.post(f"{CASHFREE_BASE_URL}/orders", json=cf_payload, headers=headers)
        data = resp.json()

        if resp.status_code != 200:
            return {"error": data.get("message", "Cashfree error")}

        # Log pending claim
        db_user_id = user_id if is_valid_uuid(user_id) else None
        db.table("payment_claims").insert({
            "payment_id": order_id,
            "user_id": db_user_id,
            "plan_id": plan_id,
            "amount": float(amount),
            "status": "pending"
        }).execute()

        return data
    except Exception as e:
        return {"error": f"Gateway Exception: {str(e)}"}

@app.get("/api/cashfree/status/{order_id}")
async def get_status(order_id: str):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        return {"error": "Credentials missing"}

    try:
        headers = {
            "x-client-id": CASHFREE_APP_ID,
            "x-client-secret": CASHFREE_SECRET_KEY,
            "x-api-version": "2023-08-01"
        }
        resp = requests.get(f"{CASHFREE_BASE_URL}/orders/{order_id}", headers=headers)
        data = resp.json()

        if resp.status_code == 200 and data.get("order_status") == "PAID":
            await fulfill_order(order_id, data['customer_details']['customer_id'])
        
        return data
    except Exception as e:
        return {"error": str(e)}

# --- THE "TECH VISHAL" STYLE FORMATTER ---
def clean_branding_recursive(obj):
    if isinstance(obj, dict):
        return {clean_branding_recursive(k): clean_branding_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_branding_recursive(x) for x in obj]
    elif isinstance(obj, str):
        import re
        forbidden_phrases = [
            "tech_vishal", "techvishal", "tech vishal", "vishal boss", "vishal_boss", 
            "techvishalboss", "tech vishal boss", "vishal"
        ]
        val = obj
        for phrase in forbidden_phrases:
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            val = pattern.sub("", val)
        val = re.sub(r'\s+', ' ', val).strip()
        if not val or val.upper() in ["", "BOSS"]:
            return "N/A"
        return val
    return obj

def build_output(raw_json: dict, query_num: str, plan_info: dict, usage: int):
    # Detect items: could be a list or a dict (Result 1, Result 2, etc.)
    items = raw_json.get('results') or raw_json.get('data') or raw_json.get('records')
    
    clean_results = {}
    
    # CASE 1: Items is a Dictionary (e.g., {"Result 1": {...}})
    if isinstance(items, dict):
        for key, val in items.items():
            if isinstance(val, dict):
                clean_results[key] = {
                    "name": str(val.get('name', val.get('full_name', 'N/A'))).upper(),
                    "father_name": str(val.get('father_name', val.get('fathername', 'N/A'))).upper(),
                    "mobile": str(val.get('mobile', val.get('number', query_num))),
                    "alt_mobile": str(val.get('alt_mobile', 'N/A')),
                    "email": str(val.get('email', 'N/A')),
                    "aadhar_number": str(val.get('aadhar_number', 'N/A')),
                    "operator": str(val.get('operator', val.get('carrier', 'N/A'))).upper(),
                    "state_circle": str(val.get('circle', val.get('state_circle', val.get('state', 'N/A')))).upper(),
                    "address": str(val.get('address', val.get('location', 'N/A')))
                }
    
    # CASE 2: Items is a List
    elif isinstance(items, list):
        for i, val in enumerate(items, 1):
            if isinstance(val, dict):
                clean_results[f"Result {i}"] = {
                    "name": str(val.get('name', val.get('full_name', 'N/A'))).upper(),
                    "father_name": str(val.get('father_name', val.get('fathername', 'N/A'))).upper(),
                    "mobile": str(val.get('mobile', val.get('number', query_num))),
                    "alt_mobile": str(val.get('alt_mobile', 'N/A')),
                    "email": str(val.get('email', 'N/A')),
                    "aadhar_number": str(val.get('aadhar_number', 'N/A')),
                    "operator": str(val.get('operator', val.get('carrier', 'N/A'))).upper(),
                    "state_circle": str(val.get('circle', val.get('state_circle', val.get('state', 'N/A')))).upper(),
                    "address": str(val.get('address', val.get('location', 'N/A')))
                }
    
    # CASE 3: Raw response is the data itself or we can locate data inside raw_json itself
    elif raw_json.get('status') is True or raw_json.get('name') or raw_json.get('owner_name') or raw_json.get('data') or isinstance(raw_json.get('data'), list):
        clean_results["Result 1"] = {
            "name": str(raw_json.get('name', raw_json.get('owner_name', 'N/A'))).upper(),
            "father_name": str(raw_json.get('father_name', 'N/A')).upper(),
            "mobile": str(raw_json.get('mobile', query_num)),
            "alt_mobile": str(raw_json.get('alt_mobile', 'N/A')),
            "email": str(raw_json.get('email', 'N/A')),
            "aadhar_number": str(raw_json.get('aadhar_number', 'N/A')),
            "operator": str(raw_json.get('operator', 'N/A')).upper(),
            "state_circle": str(raw_json.get('circle', 'N/A')).upper(),
            "address": str(raw_json.get('address', 'N/A'))
        }

    # Clean the brand marks and references (such as Tech Vishal) recursively
    clean_results = clean_branding_recursive(clean_results)

    # All search results are retained and forwarded without truncation
    return {
        "status": "success" if clean_results else "failed",
        "success": True if clean_results else False,
        "results_found": len(clean_results),
        "query": query_num,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %I:%M:%S %p UTC"),
        "license_info": {
            "plan_name": plan_info.get('plan_name', 'Basic'),
            "expires_at": plan_info.get('expires_at', 'N/A'),
            "requests_used": usage
        },
        "results": clean_results
    }

def sanitize_error_message(msg: str) -> str:
    lowercase_msg = str(msg or "").lower()
    if any(forbidden in lowercase_msg for forbidden in ["vishal", "tech_vishal", "techvishal", "boss", "telegram", "channel", "access denied", "restricted", "authorized", "engine error"]):
        return "API error, please try again later."
    return msg

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
    query: Optional[str] = Query(None),
    numquery: Optional[str] = Query(None)
):
    start_time = time.time()
    num = (number or query or numquery or "").strip()

    # 1. Parameter Validation
    # Allow a special internal key for the website or check if there's no key but it's a valid number
    if not check_rate_limit(request):
        return {"status": "error", "message": "Too many requests. Please slow down."}
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
            auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not auth_query.data or len(auth_query.data) == 0:
                print(f"[AUTH_FAIL] Key: {key}")
                return {"status": "error", "message": "Auth Failed: Invalid API key"}
            
            license = auth_query.data[0]
            
            # 5. Status & Expiry Check
            if license.get('status') != 'active':
                return {"status": "error", "message": "Key Suspended: Access disabled"}

            try:
                if license.get('expires_at'):
                    exp_date = datetime.fromisoformat(license['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if exp_date < datetime.utcnow():
                        return {"status": "error", "message": "Key Expired: Please renew subscription"}
            except Exception as e:
                print(f"[EXPIRY_PARSE_ERR] {e}")
                pass
            
            user_id = license.get('user_id')
            user_email = license.get('user_email')
        else:
            license = {"id": "system", "plan_name": "Internal VIP", "requests_used": 0, "expires_at": "Never"}
            user_id = None
            user_email = None

        # Log the search
        try:
            db.table("search_logs").insert({
                "user_id": user_id,
                "user_email": user_email,
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0",
                "search_query": num
            }).execute()
        except Exception as e:
            print(f"[LOG_ERR] {e}")

        # 6. Usage Quota
        requests_used = license.get('requests_used') or 0
        limit = license.get('request_limit')
        if limit is not None and int(requests_used) >= int(limit):
            return {"status": "error", "message": "Quota Exhausted: Plan limit reached"}

        # 7. Intelligence Source Fetch
        target_template = os.getenv("REAL_LOOKUP_URL") or os.getenv("LOOKUP_API_URL")
        try:
            settings_query = db.table("api_settings").select("real_api_url").limit(1).execute()
            if settings_query.data and len(settings_query.data) > 0:
                if settings_query.data[0].get('real_api_url'):
                    target_template = settings_query.data[0]['real_api_url']
        except Exception as e:
            print(f"[SETTINGS_ERR] {e}")
            pass
        
        if not target_template:
             return {"status": "error", "message": "ServerDown: Backend URL not configured"}

        # Force replace any old/stale API keys with the new active key to ensure the new API is used everywhere
        target_template = target_template.replace("TVB_SGL_053B3AA6", "TVB_SGL_C24439EA")

        # 8. Execution
        if "ENTER_TARGET_HERE" not in target_template:
            key_param = os.getenv("LOOKUP_API_KEY") or "TVB_SGL_C24439EA"
            service_param = os.getenv("LOOKUP_API_SERVICE") or "number"
            final_url = f"{target_template.rstrip('/')}?key={key_param}&service={service_param}&number={num}"
        else:
            final_url = target_template.replace("ENTER_TARGET_HERE", num)
        
        max_attempts = 5
        delays = [1, 2, 3, 4, 5]
        payload = None
        last_error_msg = "ServerDown: Data source unresponsive"
        
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
            "Accept": "application/json,text/plain,*/*"
        }
        
        for attempt in range(1, max_attempts + 1):
            try:
                print(f"[LOOKUP_DIAGNOSTIC] Attempt {attempt} - Fetching compiled URL: {final_url}")
                resp = requests.get(final_url, timeout=12, headers=headers)
                print(f"[LOOKUP_DIAGNOSTIC] Attempt {attempt} - Status Code: {resp.status_code}")
                
                if resp.status_code != 200:
                    print(f"[LOOKUP_DIAGNOSTIC] Attempt {attempt} - Bad status content: {resp.text[:400]}")
                    raise Exception(f"HTTP code {resp.status_code}")
                
                body_text = resp.text.strip()
                if "html" in resp.headers.get("content-type", "").lower() or body_text.startswith("<!DOCTYPE") or body_text.startswith("<html"):
                    print(f"[LOOKUP_DIAGNOSTIC] Attempt {attempt} - Received HTML instead of JSON")
                    raise Exception("HTML page blocked / Cloudflare gate challenge")
                
                payload = resp.json()
                break
            except Exception as lookup_err:
                print(f"[LOOKUP_DIAGNOSTIC] Attempt {attempt} failed: {lookup_err}")
                last_error_msg = f"ServerDown: Data source unresponsive ({lookup_err})"
                if attempt < max_attempts:
                    sleep_time = delays[attempt - 1]
                    print(f"[LOOKUP_DIAGNOSTIC] Sleeping {sleep_time}s before next attempt...")
                    time.sleep(sleep_time)

        if payload is None:
            return {"status": "error", "message": last_error_msg}

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

@app.get("/api/telegram")
async def telegram_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    telegram: Optional[str] = Query(None),
    query: Optional[str] = Query(None)
):
    targetTelegramId = (query or telegram or "").strip()
    if not targetTelegramId:
        return {"status": "error", "message": "Telegram query parameter is required"}

    try:
        db = get_supabase()
        if not db:
            return {"status": "error", "message": "ServerDown: Database connection failure"}

        is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
        keyRecord = None

        if is_master:
            keyRecord = {
                "id": "master",
                "plan_name": "Internal Master API",
                "status": "active"
            }
        else:
            if not key:
                return {"status": "error", "message": "API key is required"}

            keyRecords = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not keyRecords.data or len(keyRecords.data) == 0:
                return {"status": "error", "message": "Access Denied: Invalid or unauthorized API key"}

            keyRecord = keyRecords.data[0]
            if keyRecord.get('status') != 'active':
                return {"status": "error", "message": "Subscription Blocked: API key expired or suspended"}

            # Expiry check
            try:
                if keyRecord.get('expires_at'):
                    from datetime import datetime
                    exp_date = datetime.fromisoformat(keyRecord['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if exp_date < datetime.utcnow():
                        return {"status": "error", "message": "Key Expired: Please renew subscription"}
            except Exception as e:
                print(f"[EXP_PARSE_ERR] {e}")

            # Usage check
            requests_used = keyRecord.get('requests_used') or 0
            limit = keyRecord.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return {"status": "error", "message": "Quota Exhausted: Lookup limit reached"}

        # Checking safety protection bypass
        is_protected = False
        try:
            protected_query = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", targetTelegramId).execute()
            if protected_query.data:
                is_protected = True
        except Exception as e:
            print(f"[PROTECT_ERR] {e}")

        if is_protected:
            # Record telemetry for protected search
            if not is_master and keyRecord:
                from datetime import datetime
                db.table("api_keys").update({
                    "requests_used": (keyRecord.get('requests_used') or 0) + 1,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", keyRecord['id']).execute()

            return {
                "status": "success",
                "message": "Protected: This Telegram account is protected on TRACEXDATA. 🛡️",
                "results": {
                    "Telegram Match": {
                        "name": "PROTECTED RECORD",
                        "telegram_id": targetTelegramId,
                        "mobile": "PROTECTED @ TRACEX SHIELD",
                        "father_name": "PROTECTED @ TRACEX SHIELD",
                        "alt_mobile": "PROTECTED @ TRACEX SHIELD",
                        "email": "PROTECTED @ TRACEX SHIELD",
                        "operator": "PROTECTED @ TRACEX SHIELD",
                        "state_circle": "PROTECTED @ TRACEX SHIELD",
                        "address": "PROTECTED @ TRACEX SHIELD",
                        "platform": "Telegram Lookup"
                    }
                }
            }

        target_username = targetTelegramId if targetTelegramId.startswith('@') else f"@{targetTelegramId}"
        api_url = f"https://exploitsindia.site/lookup/telegram.php?username={requests.utils.quote(target_username)}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
            "Accept": "text/plain,text/html,application/json,*/*"
        }

        resp = requests.get(api_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            return {"status": "error", "message": "api error"}

        text = resp.text or ""
        cleanedText = text.replace("@Cyb3rS0ldier", "")
        lowerText = cleanedText.lower()

        if "no result" in lowerText or "no records found" in lowerText or "error" in lowerText or not text.strip():
            return {"status": "error", "message": "no result"}

        import re
        usernameMatch = re.search(r"Username:\s*([^\s\n\r]+)", cleanedText, re.IGNORECASE)
        idMatch = re.search(r"Telegram ID:\s*(?:<code>)?(\d+)(?:<\/code>)?", cleanedText, re.IGNORECASE)
        phoneMatch = re.search(r"Phone Number:\s*(?:<code>)?(\d+)(?:<\/code>)?", cleanedText, re.IGNORECASE)
        countryMatch = re.search(r"Country:\s*([^\n\r]+)", cleanedText, re.IGNORECASE)
        codeMatch = re.search(r"Country Code:\s*([^\n\r]+)", cleanedText, re.IGNORECASE)

        username = usernameMatch.group(1).strip() if usernameMatch else target_username
        telegram_id = idMatch.group(1).strip() if idMatch else "N/A"
        phone = phoneMatch.group(1).strip() if phoneMatch else "N/A"
        country = countryMatch.group(1).strip() if countryMatch else "N/A"
        country_code = codeMatch.group(1).strip() if codeMatch else "N/A"

        if telegram_id == "N/A" and phone == "N/A":
            return {"status": "error", "message": "no result"}

        results = {
            "Telegram Match": {
                "name": username,
                "telegram_id": telegram_id,
                "mobile": phone,
                "father_name": "N/A",
                "alt_mobile": country_code,
                "email": "N/A",
                "operator": country,
                "state_circle": "N/A",
                "address": "N/A",
                "platform": "Telegram Lookup"
            }
        }

        # Record telemetry for successful search
        if not is_master and keyRecord:
            from datetime import datetime
            db.table("api_keys").update({
                "requests_used": (keyRecord.get('requests_used') or 0) + 1,
                "last_used_at": datetime.utcnow().isoformat()
            }).eq("id", keyRecord['id']).execute()

        return {"status": "success", "results": results}

    except Exception as err:
        print(f"Telegram Proxy error: {err}")
        return {"status": "error", "message": "api error"}

# Disabled block placeholder to maintain structure
def disabled_telegram_placeholder():
    pass

@app.get("/api/vehicle")
async def vehicle_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    rc: Optional[str] = Query(None),
    query: Optional[str] = Query(None)
):
    return {
        "status": "error",
        "success": False,
        "message": "This endpoint is disabled. Only the Number Details Lookup API is active and supported.",
        "buy_url": "https://tracexnumber.web.app/buy-api"
    }

if __name__ == "__main__":
    import uvicorn
    # Render provides PORT env var, default to 10000 for standard Render deploys
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
