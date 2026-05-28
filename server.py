import os
import requests
import time
import secrets
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from typing import Optional, Dict, Any

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
        url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        if url and key:
            try:
                _db = create_client(url, key)
            except:
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
                start = datetime.fromisoformat(profile['unlimited_expiry'].replace('Z', '+00:00')).replace(tzinfo=None) if profile.get('unlimited_expiry') else now
                if start < now: start = now
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
    
    # CASE 3: Raw response is the data itself
    elif raw_json.get('status') is True or raw_json.get('name'):
        clean_results["Result 1"] = {
            "name": str(raw_json.get('name', 'N/A')).upper(),
            "father_name": str(raw_json.get('father_name', 'N/A')).upper(),
            "mobile": str(raw_json.get('mobile', query_num)),
            "alt_mobile": str(raw_json.get('alt_mobile', 'N/A')),
            "email": str(raw_json.get('email', 'N/A')),
            "aadhar_number": str(raw_json.get('aadhar_number', 'N/A')),
            "operator": str(raw_json.get('operator', 'N/A')).upper(),
            "state_circle": str(raw_json.get('circle', 'N/A')).upper(),
            "address": str(raw_json.get('address', 'N/A'))
        }

    # Final Output Structure matching screenshot style
    return {
        "status": "success" if clean_results else "no_data",
        "Powered_by": "@gaurav_beniwal_0001",
        "Owner": "@gaurav_beniwal_0001",
        "Buy_API": "https://tracexnumber.web.app/buy-api",
        "Timestamp": datetime.utcnow().strftime("%d-%m-%Y %I:%M:%S %p"),
        "API_Info": {
            "query": query_num,
            "plan": plan_info.get('plan_name', 'Basic'),
            "expires": plan_info.get('expires_at', 'N/A'),
            "used": usage,
            "full_endpoint": f"https://tracexdata-api.onrender.com/api/lookup?key={plan_info.get('api_key')}&query={query_num}"
        },
        "results": clean_results if clean_results else "No Record Found for this number.",
        "branding": {
            "provider": "TraceXData Intelligence PRO",
            "developer": "@gaurav_beniwal_0001",
            "website": "https://tracexnumber.web.app",
            "support": "@gaurav_beniwal_0001"
        }
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

            # 6. Usage Quota
            requests_used = license.get('requests_used') or 0
            limit = license.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return {"status": "error", "message": "Quota Exhausted: Plan limit reached"}
        else:
            # Fake license data for master key
            license = {"id": "system", "plan_name": "Internal VIP", "requests_used": 0, "expires_at": "Never"}

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

        # 8. Execution
        if "ENTER_TARGET_HERE" not in target_template:
            key_param = os.getenv("LOOKUP_API_KEY") or "TVB_SGL_053B3AA6"
            service_param = os.getenv("LOOKUP_API_SERVICE") or "number"
            final_url = f"{target_template.rstrip('/')}?key={key_param}&service={service_param}&number={num}"
        else:
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

@app.get("/api/telegram")
async def telegram_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    telegram: Optional[str] = Query(None),
    query: Optional[str] = Query(None)
):
    start_time = time.time()
    target_telegram_id = (query or telegram or "").strip()
    api_key_header = request.headers.get("x-api-key", "")
    key = (key or api_key_header or "").strip()

    if not target_telegram_id:
        return {"status": "error", "message": "Telegram query parameter is required"}

    db = get_supabase()
    if not db:
        return {"status": "error", "message": "Engine Offline: Internal connection failure"}

    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
    license_data = None

    try:
        if is_master:
            license_data = {
                "id": "master",
                "plan_name": "Internal Master API",
                "expires_at": (datetime.utcnow() + timedelta(days=365)).isoformat(),
                "status": "active",
                "requests_used": 0,
                "request_limit": None
            }
        else:
            if not key:
                return {"status": "error", "message": "API key is required"}

            auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not auth_query.data or len(auth_query.data) == 0:
                return {"status": "error", "message": "Access Denied: Invalid or unauthorized API key"}

            license_data = auth_query.data[0]
            if license_data.get('status') != 'active':
                return {"status": "error", "message": "Subscription Blocked: API key expired or suspended", "buy_url": "https://tracexnumber.web.app/buy-api"}

            try:
                if license_data.get('expires_at'):
                    exp_date = datetime.fromisoformat(license_data['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if exp_date < datetime.utcnow():
                        return {"status": "error", "message": "Key Expired: Please renew subscription", "buy_url": "https://tracexnumber.web.app/buy-api"}
            except Exception as e:
                print(f"[EXPIRY_PARSE_ERR] {e}")

            requests_used = license_data.get('requests_used') or 0
            limit = license_data.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return {"status": "error", "message": "Quota Exhausted: Lookup limit reached"}

        # 1. Checking safety protection registry
        is_protected = False
        try:
            protected_query = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", target_telegram_id).execute()
            if protected_query and protected_query.data:
                is_protected = True
        except Exception as e:
            print(f"[PROTECTION_CHECK_ERR] {e}")

        if is_protected:
            # Record telemetry for protected search
            if not is_master and license_data and license_data.get('id'):
                new_count = (license_data.get('requests_used') or 0) + 1
                db.table("api_keys").update({
                    "requests_used": new_count,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", license_data['id']).execute()

            # Log
            try:
                db.table("api_logs").insert({
                    "api_key_id": license_data.get('id') if not is_master else None,
                    "masked_number": f"TG: {target_telegram_id}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000)
                }).execute()
            except: pass

            return {
                "status": "success",
                "message": "Protected: This Telegram account is protected on TRACEXDATA. 🛡️",
                "results": {
                    "Telegram Match": {
                        "name": "PROTECTED RECORD",
                        "telegram_id": target_telegram_id,
                        "mobile": "PROTECTED @ TRACEX SHIELD",
                        "father_name": "PROTECTED @ TRACEX SHIELD",
                        "alt_mobile": "PROTECTED @ TRACEX SHIELD",
                        "email": "PROTECTED @ TRACEX SHIELD",
                        "aadhar_number": "PROTECTED @ TRACEX SHIELD",
                        "operator": "PROTECTED @ TRACEX SHIELD",
                        "state_circle": "PROTECTED @ TRACEX SHIELD",
                        "address": "PROTECTED @ TRACEX SHIELD"
                    }
                }
            }

        # 2. Call external API
        api_url = f"https://techvishalboss.com/api/v1/lookup.php?key=TVB_SGL_D500F1C5&service=tg_to_number&telegram={target_telegram_id}"
        resp = requests.get(api_url, timeout=12, headers={"User-Agent": "TraceX-SaaS-Node"})
        if resp.status_code != 200:
            return {"status": "error", "message": "ServerDown: Data source unresponsive"}

        data = resp.json()
        status_val = data.get("Status") or data.get("status") or False
        if status_val is True or status_val == "success" or str(status_val).lower() == "true":
            mobile_no = "N/A"
            data_field = data.get("Data") or {}
            contact_field = data_field.get("Contact") if isinstance(data_field, dict) else None
            if isinstance(contact_field, list):
                mobile_no = contact_field[0] if len(contact_field) > 0 else "N/A"
            elif isinstance(contact_field, str):
                mobile_no = contact_field
            elif data.get("Search_Number"):
                mobile_no = data.get("Search_Number")

            results = {
                "Telegram Match": {
                    "name": "Telegram Registered Profile",
                    "telegram_id": target_telegram_id,
                    "mobile": mobile_no or "N/A",
                    "father_name": "N/A",
                    "alt_mobile": "N/A",
                    "email": "N/A",
                    "aadhar_number": "N/A",
                    "operator": "N/A",
                    "state_circle": "N/A",
                    "address": "N/A"
                }
            }

            # Record telemetry for successful search
            if not is_master and license_data and license_data.get('id'):
                new_count = (license_data.get('requests_used') or 0) + 1
                db.table("api_keys").update({
                    "requests_used": new_count,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", license_data['id']).execute()

            # Log API request
            try:
                db.table("api_logs").insert({
                    "api_key_id": license_data.get('id') if not is_master else None,
                    "masked_number": f"TG: {target_telegram_id}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000)
                }).execute()
            except Exception as e:
                print("Failed to log:", e)

            return {"status": "success", "results": results}
        else:
            try:
                db.table("api_logs").insert({
                    "api_key_id": license_data.get('id') if not is_master and license_data else None,
                    "masked_number": f"TG: {target_telegram_id}",
                    "status": "failed",
                    "response_time_ms": int((time.time() - start_time) * 1000)
                }).execute()
            except: pass
            return {"status": "error", "message": data.get("Message") or "No Telegram details available."}

    except Exception as e:
        print(f"Telegram processing error: {e}")
        try:
            db.table("api_logs").insert({
                "api_key_id": license_data.get('id') if not is_master and license_data else None,
                "masked_number": f"TG: {target_telegram_id}",
                "status": "failed",
                "response_time_ms": int((time.time() - start_time) * 1000)
            }).execute()
        except: pass
        return {"status": "error", "message": "Failed to connect to Telegram intelligence portal."}

@app.get("/api/vehicle")
async def vehicle_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    rc: Optional[str] = Query(None),
    query: Optional[str] = Query(None)
):
    start_time = time.time()
    target_vehicle_no = (query or rc or "").strip().upper()
    api_key_header = request.headers.get("x-api-key", "")
    key = (key or api_key_header or "").strip()

    if not target_vehicle_no:
        return {"status": "error", "message": "Vehicle RC query parameter is required"}

    db = get_supabase()
    if not db:
        return {"status": "error", "message": "Engine Offline: Internal connection failure"}

    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
    license_data = None

    try:
        if is_master:
            license_data = {
                "id": "master",
                "plan_name": "Internal Master API",
                "expires_at": (datetime.utcnow() + timedelta(days=365)).isoformat(),
                "status": "active",
                "requests_used": 0,
                "request_limit": None
            }
        else:
            if not key:
                return {"status": "error", "message": "API key is required"}

            auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not auth_query.data or len(auth_query.data) == 0:
                return {"status": "error", "message": "Access Denied: Invalid or unauthorized API key"}

            license_data = auth_query.data[0]
            if license_data.get('status') != 'active':
                return {"status": "error", "message": "Subscription Blocked: API key expired or suspended", "buy_url": "https://tracexnumber.web.app/buy-api"}

            try:
                if license_data.get('expires_at'):
                    exp_date = datetime.fromisoformat(license_data['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if exp_date < datetime.utcnow():
                        return {"status": "error", "message": "Key Expired: Please renew subscription", "buy_url": "https://tracexnumber.web.app/buy-api"}
            except Exception as e:
                print(f"[EXPIRY_PARSE_ERR] {e}")

            requests_used = license_data.get('requests_used') or 0
            limit = license_data.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return {"status": "error", "message": "Quota Exhausted: Lookup limit reached"}

        # 1. Checking safety protection registry
        is_protected = False
        try:
            protected_query = db.table("protected_vehicles").select("vehicle_number").eq("vehicle_number", target_vehicle_no).execute()
            if protected_query and protected_query.data:
                is_protected = True
        except Exception as e:
            print(f"[PROTECTION_CHECK_ERR] {e}")

        if is_protected:
            # Record telemetry for protected search
            if not is_master and license_data and license_data.get('id'):
                new_count = (license_data.get('requests_used') or 0) + 1
                db.table("api_keys").update({
                    "requests_used": new_count,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", license_data['id']).execute()

            # Log
            try:
                db.table("api_logs").insert({
                    "api_key_id": license_data.get('id') if not is_master else None,
                    "masked_number": f"RC: {target_vehicle_no}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000)
                }).execute()
            except: pass

            return {
                "status": "success",
                "message": "Protected: This Vehicle registration holds security clearance. 🛡️",
                "results": {
                    "Vehicle Match": {
                        "name": "PROTECTED RECORD",
                        "vehicle_no": target_vehicle_no,
                        "mobile": "PROTECTED @ TRACEX SHIELD",
                        "father_name": "PROTECTED @ TRACEX SHIELD",
                        "alt_mobile": "PROTECTED @ TRACEX SHIELD",
                        "email": "PROTECTED @ TRACEX SHIELD",
                        "aadhar_number": "PROTECTED @ TRACEX SHIELD",
                        "operator": "PROTECTED @ TRACEX SHIELD",
                        "state_circle": "PROTECTED @ TRACEX SHIELD",
                        "address": "PROTECTED @ TRACEX SHIELD"
                    }
                }
            }

        # 2. Call external API
        api_url = f"https://techvishalboss.com/api/v1/lookup.php?key=TVB_SGL_0435DADE&service=vehicle_owner_number&rc={target_vehicle_no}"
        resp = requests.get(api_url, timeout=12, headers={"User-Agent": "TraceX-SaaS-Node"})
        if resp.status_code != 200:
            return {"status": "error", "message": "ServerDown: Data source unresponsive"}

        data = resp.json()
        status_val = data.get("status") or data.get("Status") or False
        if status_val == "success" or status_val == "true" or status_val is True:
            details = data.get("data") or {}
            
            results = {
                "Vehicle Match": {
                    "name": details.get("owner_name") or "N/A",
                    "vehicle_no": details.get("rc_number") or target_vehicle_no,
                    "mobile": details.get("mobile") or "N/A",
                    "father_name": details.get("father_name") or "N/A",
                    "alt_mobile": "N/A",
                    "email": "N/A",
                    "aadhar_number": "N/A",
                    "operator": "N/A",
                    "state_circle": "N/A",
                    "address": details.get("present_address") or details.get("permanent_address") or "N/A"
                }
            }

            # Record telemetry for successful search
            if not is_master and license_data and license_data.get('id'):
                new_count = (license_data.get('requests_used') or 0) + 1
                db.table("api_keys").update({
                    "requests_used": new_count,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", license_data['id']).execute()

            # Log API request
            try:
                db.table("api_logs").insert({
                    "api_key_id": license_data.get('id') if not is_master else None,
                    "masked_number": f"RC: {target_vehicle_no}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000)
                }).execute()
            except Exception as e:
                print("Failed to log:", e)

            return {"status": "success", "results": results}
        else:
            try:
                db.table("api_logs").insert({
                    "api_key_id": license_data.get('id') if not is_master and license_data else None,
                    "masked_number": f"RC: {target_vehicle_no}",
                    "status": "failed",
                    "response_time_ms": int((time.time() - start_time) * 1000)
                }).execute()
            except: pass
            return {"status": "error", "message": data.get("message") or "No Vehicle details available."}

    except Exception as e:
        print(f"Vehicle processing error: {e}")
        try:
            db.table("api_logs").insert({
                "api_key_id": license_data.get('id') if not is_master and license_data else None,
                "masked_number": f"RC: {target_vehicle_no}",
                "status": "failed",
                "response_time_ms": int((time.time() - start_time) * 1000)
            }).execute()
        except: pass
        return {"status": "error", "message": "Failed to connect to Vehicle intelligence portal."}

if __name__ == "__main__":
    import uvicorn
    # Render provides PORT env var, default to 10000 for standard Render deploys
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
