import hmac
import hashlib
import os
import requests
import time
import secrets
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Query, Body
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

# Cashfree Configuration
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
CASHFREE_BASE_URL = os.getenv("CASHFREE_BASE_URL", "https://api.cashfree.com/pg")

# Telegram Stateless Secret
TELEGRAM_PAYMENT_LINK_SECRET = os.getenv("TELEGRAM_PAYMENT_LINK_SECRET")

# Plan Configuration
PLAN_CONFIG = {
    "c10": {"amount": 20.0, "credits": 10, "name": "10 Credits"},
    "c50": {"amount": 70.0, "credits": 50, "name": "50 Credits"},
    "c100": {"amount": 100.0, "credits": 100, "name": "100 Credits"},
    "u1h": {"amount": 9.0, "credits": 0, "minutes": 60, "name": "1 Hour Unlimited"},
    "u1d": {"amount": 29.0, "credits": 0, "minutes": 1440, "name": "24 Hours Unlimited"},
    "u1w": {"amount": 149.0, "credits": 0, "minutes": 10080, "name": "7 Days Unlimited"},
    "u1m": {"amount": 399.0, "credits": 0, "minutes": 43200, "name": "30 Days Unlimited"},
    "protect49": {"amount": 49.0, "credits": 0, "name": "Protect Number"}
}

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

async def fulfill_order(order_id: str, user_id_raw: str):
    db = get_supabase()
    if not db:
        print(f"[FATAL] No DB connection for fulfillment: {order_id}")
        return

    print(f"[FULFILL_START] order_id: {order_id}, user_id_raw: {user_id_raw}")

    try:
        # Check if already fulfilled via payment_claims
        # We check both payment_id (Cashfree ID) and session_id (which could be the 'tx' code)
        claim_query = db.table("payment_claims").select("*").or_(f"payment_id.eq.{order_id},cashfree_order_id.eq.{order_id}").execute()
        
        if claim_query.data and any(c['status'] in ['success', 'paid', 'completed'] for c in claim_query.data):
            print(f"[ALREADY_FULFILLED] Order: {order_id}")
            return

        # Determine if it's a Telegram Order
        payment_source = 'web'
        tg_session_id = None
        tg_user_id = None
        plan_id = None
        credits_to_add = 0
        minutes_to_add = 0
        protected_number = None

        if claim_query.data:
            claim = claim_query.data[0]
            plan_id = claim.get('plan_id')
            payment_source = claim.get('payment_source')
            tg_session_id = claim.get('session_id')
            tg_user_id = claim.get('telegram_user_id')
            credits_to_add = claim.get('credits') or 0
            protected_number = claim.get('protected_number')
        
        # Recovery/Fallback for Telegram: Try to find session by order_id in telegram_payment_sessions (old way)
        if (not tg_session_id or payment_source != 'telegram_bot') and not (tg_user_id):
            tg_session_query = db.table("telegram_payment_sessions").select("*").eq("cashfree_order_id", order_id).execute()
            if tg_session_query.data:
                session_data = tg_session_query.data[0]
                tg_session_id = session_data['session_id']
                payment_source = 'telegram_bot'
                tg_user_id = session_data.get('telegram_user_id')
                plan_id = session_data.get('plan_id')
                credits_to_add = session_data.get('credits') or 0
                protected_number = session_data.get('protected_number')
                print(f"[TG_RECOVERY] Found old session {tg_session_id} for order {order_id}")

        if (payment_source in ['telegram_bot', 'telegram_bot_stateless']) and tg_user_id:
            print(f"[TG_DEBUG] Fulfilling Telegram Order: {order_id} | User: {tg_user_id} | Source: {payment_source}")
            
            # 1. Update Old Session if exists
            if tg_session_id:
                try:
                    db.table("telegram_payment_sessions").update({
                        "status": "success",
                        "paid_at": datetime.utcnow().isoformat(),
                        "cashfree_order_id": order_id,
                        "updated_at": datetime.utcnow().isoformat()
                    }).eq("session_id", tg_session_id).execute()
                except: pass

            # 2. Update Telegram User Account
            user_query = db.table("telegram_users").select("*").eq("telegram_user_id", tg_user_id).execute()
            if user_query.data:
                tg_user = user_query.data[0]
                tg_update = {"updated_at": datetime.utcnow().isoformat()}
                
                # Credits mapping fallback from PLAN_CONFIG if not in claim
                if credits_to_add <= 0 and plan_id in PLAN_CONFIG:
                   credits_to_add = PLAN_CONFIG[plan_id].get('credits') or 0
                
                # Manual legacy mapping
                if credits_to_add <= 0:
                    if plan_id in ['c10', 'credit_10']: credits_to_add = 10
                    elif plan_id in ['c50', 'credit_50']: credits_to_add = 50
                    elif plan_id in ['c100', 'credit_100']: credits_to_add = 100
                
                if credits_to_add > 0:
                    tg_update['credits'] = (tg_user.get('credits') or 0) + credits_to_add
                    print(f"[TG_DEBUG] +{credits_to_add} Credits for {tg_user_id}")
                
                # Unlimited mapping from PLAN_CONFIG
                hours = 0
                if plan_id in PLAN_CONFIG:
                   minutes = PLAN_CONFIG[plan_id].get('minutes') or 0
                   if minutes > 0:
                      hours = minutes / 60.0

                # Manual legacy mapping for Unlimited
                if hours <= 0 and plan_id and (plan_id.startswith('u') or plan_id.startswith('unlimited')):
                    hours_map = {
                        'u1h': 1, 'unlimited_1h': 1,
                        'u1d': 24, 'u24h': 24, 'unlimited_24h': 24, 'unlimited_1d': 24,
                        'u1w': 168, 'unlimited_1w': 168,
                        'u1m': 720, 'unlimited_1m': 720
                    }
                    hours = hours_map.get(plan_id, 0)
                    if not hours:
                       match = re.search(r'(\d+)([hdwm])', plan_id)
                       if match:
                           val, unit = match.groups()
                           val = int(val)
                           if unit == 'h': hours = val
                           elif unit == 'd': hours = val * 24
                           elif unit == 'w': hours = val * 168
                           elif unit == 'm': hours = val * 720

                if hours > 0:
                    now = datetime.utcnow()
                    start_str = tg_user.get('unlimited_expiry')
                    start = now
                    if start_str:
                        try:
                            start = datetime.fromisoformat(start_str.replace('Z', '+00:00')).replace(tzinfo=None)
                        except: pass
                    
                    if start < now: start = now
                    tg_update['unlimited_expiry'] = (start + timedelta(hours=hours)).isoformat()
                    print(f"[TG_DEBUG] +{hours}h Unlimited for {tg_user_id}")
                
                # Protected Number mapping
                if plan_id == 'protect_number' or plan_id == 'protect49':
                   if protected_number:
                      db.table("protected_numbers").upsert({
                          "phone_number": protected_number,
                          "telegram_user_id": tg_user_id,
                          "description": "Protected via Telegram Bot",
                          "updated_at": datetime.utcnow().isoformat()
                      }, on_conflict="phone_number").execute()
                      print(f"[TG_DEBUG] Protected {protected_number} for {tg_user_id}")

                db.table("telegram_users").update(tg_update).eq("telegram_user_id", tg_user_id).execute()
                print(f"[TG_STATELESS_PAY] tx={tg_session_id}, tg_id={tg_user_id}, plan={plan_id}, order_id={order_id}, status=success, credits_added={credits_to_add}")
            else:
                print(f"[TG_WARN] User {tg_user_id} not found in telegram_users table")

            # Finalize claim record
            db.table("payment_claims").update({"status": "success", "updated_at": datetime.utcnow().isoformat()}).eq("payment_id", order_id).execute()
            print(f"[TG_SUCCESS] Fulfilled Order: {order_id}")
            return

        # --- REGULAR WEB FLOW ---
        if not claim_query.data:
            print(f"[WEB_WARN] No claim record found for {order_id}")
            return

        claim = claim_query.data[0]
        plan_id = claim['plan_id']
        user_id = claim.get('user_id')
        user_email = claim.get('user_email', 'N/A')

        if not user_id:
            # Try to extract from user_id_raw (from Cashfree customer_id)
            if user_id_raw and not user_id_raw.startswith("tg_"):
                user_id = user_id_raw
        
        if not user_id:
            print(f"[WEB_ERROR] Could not identify User UUID for {order_id}")
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

        # Regular Credit/Unlimited Plans for Web Profiles
        profile_query = db.table("profiles").select("*").eq("id", user_id).execute()
        if not profile_query.data:
            print(f"[WEB_ERROR] Profile not found for {user_id}")
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
            if hours > 0:
                now = datetime.utcnow()
                start_str = profile.get('unlimited_expiry')
                start = now
                if start_str:
                    try:
                        start = datetime.fromisoformat(start_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    except: pass
                
                if start < now: start = now
                update_data['unlimited_expiry'] = (start + timedelta(hours=hours)).isoformat()

        if update_data:
            db.table("profiles").update(update_data).eq("id", user_id).execute()
            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            print(f"[FULFILL] Updated profile for {user_id}")
            
    except Exception as e:
        print(f"CRITICAL Fulfillment error: {e}")

@app.post("/api/cashfree/create-order")
async def create_order(payload: dict = Body(...), request: Request = None):
    db = get_supabase()
    if not db:
        return {"error": "Server connection failure"}

    payment_source = payload.get("payment_source")
    
    # --- STATELESS TELEGRAM FLOW ---
    if payment_source == "telegram_bot_stateless":
        if not TELEGRAM_PAYMENT_LINK_SECRET:
            return {"error": "Stateless verification secret missing"}
        
        # 1. Verify Signature
        sig = payload.get("sig")
        # Extract all params except sig to verify
        verify_fields = ["source", "tx", "tg_id", "username", "plan", "amount", "credits", "unlimited_minutes", "payment_for", "exp", "protected_number"]
        params_to_verify = {}
        for f in verify_fields:
            if payload.get(f) is not None:
                params_to_verify[f] = str(payload.get(f))
        
        # Sort and sign
        sorted_keys = sorted(params_to_verify.keys())
        canonical_str = "&".join([f"{k}={params_to_verify[k]}" for k in sorted_keys])
        expected_sig = hmac.new(TELEGRAM_PAYMENT_LINK_SECRET.encode(), canonical_str.encode(), hashlib.sha256).hexdigest()[:32]
        
        if not hmac.compare_digest(expected_sig, str(sig)):
            print(f"[TG_VERIFY_FAIL] Expected: {expected_sig}, Got: {sig}")
            return {"error": "Invalid payment signature"}
            
        # 2. Check Expiry
        try:
            exp_unix = int(payload.get("exp", 0))
            if exp_unix < time.time():
                return {"error": "Payment link has expired"}
        except:
            return {"error": "Invalid expiry format"}
            
        # 3. Cross-check Plan and Amount (Don't trust amount from payload)
        plan_id = payload.get("plan")
        if plan_id not in PLAN_CONFIG:
            return {"error": f"Invalid plan identifier: {plan_id}"}
        
        verified_plan = PLAN_CONFIG[plan_id]
        verified_amount = verified_plan['amount']
        verified_credits = verified_plan.get('credits', 0)
        
        # Override payload values with verified ones
        payload['amount'] = verified_amount
        payload['credits'] = verified_credits
        payload['plan_id'] = plan_id
        payload['session_id'] = payload.get("tx")
        payload['telegram_user_id'] = payload.get("tg_id")
        
        print(f"[TG_STATELESS_INIT] tx={payload['session_id']}, tg_id={payload['telegram_user_id']}, plan={plan_id}, amount={verified_amount}")

    user_id = payload.get("user_id")
    telegram_user_id = payload.get("telegram_user_id")
    plan_id = payload.get("plan_id")
    amount = payload.get("amount")
    user_email = payload.get("user_email", "customer@example.com")
    customer_phone = payload.get("customer_phone", "9999999999")
    
    # Force email if telegram
    if not user_id and telegram_user_id:
       user_email = f"{telegram_user_id}@telegram.com"

    # Default origin fallback
    origin = "https://tracexnumber.web.app"
    if request and request.headers.get("origin"):
        origin = request.headers.get("origin")
        
    return_url = payload.get("return_url")
    if not return_url:
       if payment_source in ["telegram_bot", "telegram_bot_stateless"]:
          session_id = payload.get("session_id") or payload.get("tx")
          return_url = f"{origin}/payment-success?session_id={session_id}&order_id={{order_id}}"
       else:
          return_url = f"{origin}?order_id={{order_id}}"

    if not (user_id or telegram_user_id) or not plan_id or not amount:
        return {"error": "Missing required parameters (user identification)"}

    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        return {"error": "Payment gateway credentials not set"}

    # Generate external customer ID for Cashfree
    # Use telegram ID if available, otherwise web user ID
    cf_customer_id = str(user_id) if user_id else f"tg_{telegram_user_id}"

    order_id = f"order_{int(time.time())}_{secrets.token_hex(3)}"
    
    cf_payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": cf_customer_id,
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
        claim_data = {
            "payment_id": order_id,
            "plan_id": plan_id,
            "amount": float(amount),
            "status": "pending",
            "session_id": payload.get("session_id") or payload.get("tx"),
            "telegram_user_id": telegram_user_id,
            "telegram_username": payload.get("username"),
            "payment_source": payment_source,
            "credits": payload.get("credits"),
            "protected_number": payload.get("protected_number")
        }
        
        # Only assign user_id if it's not a Telegram ID (which follows 'tg_...' prefix)
        if user_id and not str(user_id).startswith("tg_"):
            claim_data["user_id"] = user_id
        
        try:
            db.table("payment_claims").insert(claim_data).execute()
        except Exception as e:
            print(f"[DB_ERR] Payment claim insert failed: {e}")

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
        
        status = data.get("order_status")
        print(f"[STATUS_CHECK] Order: {order_id} | Status: {status}")

        if resp.status_code == 200 and status in ["PAID", "SUCCESS", "COMPLETED"]:
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
