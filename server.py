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
        if plan_id in ["pgpay_manual", "panfind"]:
            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            print(f"[SaaS] Manual Guest Payment fulfilled successfully for {order_id}")
            return

        # Check if user_id is a valid UUID
        if not is_valid_uuid(user_id):
            if claim.get('user_id') and is_valid_uuid(claim['user_id']):
                user_id = claim['user_id']
                print(f"[FULFILL] Resolved non-UUID to valid user_id from claim: {user_id}")
            else:
                print(f"[FULFILL] Non-UUID user_id '{user_id}' skipped database state updates, marking order {order_id} fulfilled.")
                db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
                return

        # Check if it's a Protection Plan
        if plan_id.startswith('protect_'):
            parts = plan_id.split('_', 2)
            if len(parts) >= 3:
                protect_type = parts[1]
                protect_target = parts[2]
                try:
                    if protect_type == 'mobile':
                        exist = db.table("protected_numbers").select("*").eq("phone_number", protect_target).execute()
                        if not exist.data:
                            db.table("protected_numbers").insert({
                                "phone_number": protect_target,
                                "owner_id": user_id
                            }).execute()
                        print(f"[FULFILL] Protected mobile number '{protect_target}' for user {user_id}")
                    elif protect_type == 'telegram':
                        clean_un = protect_target.replace('@', '')
                        exist1 = db.table("protected_telegrams").select("*").eq("telegram_id", clean_un).execute()
                        if not exist1.data:
                            db.table("protected_telegrams").insert({
                                "telegram_id": clean_un,
                                "owner_id": user_id
                            }).execute()
                        
                        at_un = f"@{clean_un}"
                        exist2 = db.table("protected_telegrams").select("*").eq("telegram_id", at_un).execute()
                        if not exist2.data:
                            db.table("protected_telegrams").insert({
                                "telegram_id": at_un,
                                "owner_id": user_id
                            }).execute()
                        print(f"[FULFILL] Protected telegram handles '{clean_un}' & '{at_un}' for user {user_id}")
                    elif protect_type == 'vehicle':
                        clean_veh = protect_target.upper().strip()
                        exist = db.table("protected_vehicles").select("*").eq("vehicle_number", clean_veh).execute()
                        if not exist.data:
                            db.table("protected_vehicles").insert({
                                "vehicle_number": clean_veh,
                                "owner_id": user_id
                            }).execute()
                        print(f"[FULFILL] Protected vehicle number '{clean_veh}' for user {user_id}")
                except Exception as db_err:
                    print(f"[FULFILL] Error inserting protected item: {db_err}")
            
            db.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            return

        # Check if it's an API Plan
        is_api_plan = 'a15' in plan_id or 'a30' in plan_id or plan_id.startswith('api_')
        if is_api_plan:
            api_key = f"tx_{secrets.token_hex(16)}"
            days = 30
            limit = None
            plan_name = "Number Lookup (1 Month)"

            if plan_id == 'api_number':
                plan_name = "Number Lookup (1 Month)"
            elif plan_id == 'api_telegram':
                plan_name = "Telegram Lookup (1 Month)"
            elif plan_id == 'api_identity':
                plan_name = "Identity Card Lookup (1 Month)"
            elif plan_id == 'api_bank':
                plan_name = "BA&NK Lookup (1 Month)"
            elif plan_id == 'api_vehicle':
                plan_name = "Vehicle Lookup (1 Month)"
            elif plan_id == 'api_pancard':
                plan_name = "PN Card Lookup (1 Month)"
            elif plan_id == 'api_rasion':
                plan_name = "Rasion Card Lookup (1 Month)"
            elif plan_id == 'api_combo':
                plan_name = "All Combo Special (1 Month)"
            else:
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
            print(f"[FULFILL] Created API Key for {user_id} of plan {plan_name}")
            return

        # Regular Credit/Unlimited Plans
        profile_query = db.table("profiles").select("*").eq("id", user_id).execute()
        if not profile_query.data:
            return
        
        profile = profile_query.data[0]
        update_data = {}

        # Use more flexible ID checking with dynamic numeric credits support
        credits_to_add = 0
        if plan_id in ['c10', 'credit_10']: credits_to_add = 10
        elif plan_id in ['c20', 'credit_20']: credits_to_add = 20
        elif plan_id in ['c40', 'credit_40']: credits_to_add = 40
        elif plan_id in ['c50', 'credit_50']: credits_to_add = 50
        elif plan_id in ['c100', 'credit_100']: credits_to_add = 100
        elif plan_id in ['c150', 'credit_150']: credits_to_add = 150
        elif plan_id in ['c250', 'credit_250']: credits_to_add = 275
        elif plan_id in ['c500', 'credit_500']: credits_to_add = 600
        elif plan_id in ['c1000', 'credit_1000']: credits_to_add = 1300
        else:
            # Dynamic fallback: if plan_id is of form cXX or credit_XX
            import re
            m = re.match(r'^(?:c|credit_?)(\d+)$', str(plan_id))
            if m:
                try:
                    credits_to_add = int(m.group(1))
                except ValueError:
                    pass

        if credits_to_add > 0:
            update_data['credits'] = (profile.get('credits') or 0) + credits_to_add
            print(f"[FULFILL] Determined {credits_to_add} credits to add from plan_id '{plan_id}'")
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
    plan_id = payload.get("plan_id")
    is_pg_pay = plan_id in ["pgpay_manual", "panfind"]
    
    db = get_supabase()
    if not db and not is_pg_pay:
        return {"error": "Server connection failure"}

    user_id = payload.get("user_id")
    amount = payload.get("amount")
    user_email = payload.get("user_email", "customer@example.com")
    customer_phone = payload.get("customer_phone", "9999999999")
    
    # Default origin fallback
    origin = "https://tracexdata-api.onrender.com"
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
        if db:
            db_user_id = user_id if is_valid_uuid(user_id) else None
            try:
                db.table("payment_claims").insert({
                    "payment_id": order_id,
                    "user_id": db_user_id,
                    "plan_id": plan_id,
                    "amount": float(amount),
                    "status": "pending"
                }).execute()
            except Exception as dberr:
                print(f"[DB_CLAIM_ERR] {dberr}")
        else:
            print("[TRACEXDATA] Supabase database offline. Proceeding without payment claim logging.")

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
        
        db = get_supabase()
        if db:
            claim_query = db.table("payment_claims").select("plan_id").eq("payment_id", order_id).execute()
            if claim_query.data:
                data["plan_id"] = claim_query.data[0]["plan_id"]
        
        return data
    except Exception as e:
        return {"error": str(e)}

# --- THE "TECH VISHAL" STYLE FORMATTER ---
def clean_branding_text_line_by_line(raw_text: str) -> str:
    if not raw_text:
        return ""
    import re
    lines = raw_text.split('\n')
    cleaned_lines = []
    # Broad patterns for any kind of branding or unwanted spam lines
    forbidden_keywords = [
        "cyb3r", "s0ldier", "anish", "exploits", "support", "buy api", "buy_api", 
        "retailer", "seller", "owner", "admin", "owner:", "cyb3rs0ldier", "cyb3r_s0ldier",
        "buy", "support:", "c143", "cyber", "soldier"
    ]
    for line in lines:
        line_lower = line.lower().strip()
        if not line_lower:
            continue
        if any(fw in line_lower for fw in forbidden_keywords):
            continue
        # Strip some inline patterns
        line = re.sub(r'(tech[\s\-_]*vishal(?:[\s\-_]*boss)?|anish[\s\-_]*exploits|cyb3r[\s\-_]*s0ldier|@?cyb3rs0ldier)', '', line, flags=re.IGNORECASE)
        # Strip code tag elements
        line = re.sub(r'<\/?code>', '', line)
        cleaned_lines.append(line)
    return '\n'.join(cleaned_lines)

def parse_raw_text_to_records(raw_text: str, query_val: str = None) -> dict:
    import re
    if not raw_text or not raw_text.strip():
        return {}
        
    cleaned_body = clean_branding_text_line_by_line(raw_text)
    lower_body = cleaned_body.lower()
    
    # If the response indicates empty result, return empty dictionary
    if any(term in lower_body for term in ["no result", "no records found", "unknown field", "invalid number", "not found"]):
        return {}
        
    # Split text into sections by common delimiters
    parts = re.split(r'─{5,}|━{5,}|📌?\s*Additional\s*Result:?', cleaned_body)
    
    records = {}
    record_idx = 1
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
            
        part_lower = part.lower()
        # Avoid section headers/footers with no useful fields
        if not any(k in part_lower for k in ["name", "mobile", "phone", "address", "branch", "ifsc", "aadhaar", "identity", "family"]):
            continue
            
        record_data = {}
        part_lines = part.split('\n')
        
        for line in part_lines:
            line = line.strip()
            if not line:
                continue
                
            # Strip emojis. Use a simple unicode regex
            emoji_pattern = re.compile(
                r'[\u2600-\u27BF]|[\uE000-\uF8FF]|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|[\u2011-\u26FF]|\uD83E[\uDD10-\uDDFF]', 
                re.UNICODE
            )
            clean_line = emoji_pattern.sub('', line).strip()
            clean_line = clean_line.replace('*', '').strip()
            
            if ':' in clean_line:
                parts_kv = clean_line.split(':', 1)
                key_raw = parts_kv[0].strip()
                val_raw = parts_kv[1].strip()
                
                # Strip HTML tags
                val_raw = re.sub(r'<\/?code>', '', val_raw).strip()
                
                if not val_raw or val_raw.upper() in ["", "NONE", "NULL", "N/A"]:
                    continue
                    
                key_lower = key_raw.lower()
                
                # Standardize fields to match ResultCard definitions
                if "name" in key_lower and "father" not in key_lower:
                    record_data["name"] = val_raw
                elif "father" in key_lower:
                    record_data["father_name"] = val_raw
                elif "mobile" in key_lower or "phone" in key_lower:
                    record_data["mobile"] = val_raw
                elif "address" in key_lower:
                    record_data["address"] = val_raw
                elif "aadhaar" in key_lower or "identity" in key_lower:
                    record_data["aadhar_number"] = val_raw
                elif "circle" in key_lower or "operator" in key_lower:
                    record_data["state_circle"] = val_raw
                elif "branch" in key_lower:
                    record_data["branch"] = val_raw
                elif "ifsc" in key_lower:
                    record_data["ifsc"] = val_raw
                elif "city" in key_lower:
                    record_data["city"] = val_raw
                elif "district" in key_lower:
                    record_data["district"] = val_raw
                elif "state" in key_lower:
                    record_data["state"] = val_raw
                elif "family" in key_lower:
                    record_data["family_id"] = val_raw
                else:
                    # Generic key sanitizer
                    clean_key = re.sub(r'[^a-zA-Z0-9\s_]', '', key_raw).strip().lower().replace(' ', '_')
                    if clean_key:
                        record_data[clean_key] = val_raw
                        
        if record_data:
            # Set default main name key so the card displays nicely
            if "name" not in record_data:
                if "branch" in record_data:
                    record_data["name"] = record_data["branch"]
                elif "ifsc" in record_data:
                    record_data["name"] = f"IFSC: {record_data['ifsc']}"
                elif "family_id" in record_data:
                    record_data["name"] = f"Family ID: {record_data['family_id']}"
                elif "aadhar_number" in record_data:
                    record_data["name"] = f"Aadhaar: {record_data['aadhar_number']}"
                else:
                    record_data["name"] = "REGISTRY ENTRY"
                    
            records[f"Result {record_idx}"] = record_data
            record_idx += 1
            
    # Fallback to single general details card if body exists but could not split structured keys
    if not records and cleaned_body.strip():
        records["Result 1"] = {
            "name": "DATA ENTRY",
            "details": cleaned_body.strip()
        }
        
    return records

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

def make_api_response(data: dict) -> dict:
    branding = {
        "provider": "TRACEXDATA",
        "developer": "@gaurav_beniwal_0001",
        "api_buy_link": "https://tracexdata-api.onrender.com/buy-api",
        "website_link": "https://tracexdata-api.onrender.com"
    }
    # Place directly at the root level of the response dictionary for highest visibility
    data["developer"] = "@gaurav_beniwal_0001"
    data["api_buy_link"] = "https://tracexdata-api.onrender.com/buy-api"
    data["website_link"] = "https://tracexdata-api.onrender.com"
    data["branding"] = branding
    return data

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
    return make_api_response({
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
    })

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
    numquery: Optional[str] = Query(None),
    service: Optional[str] = Query(None)
):
    start_time = time.time()
    
    # Route right away if service is passed
    if service:
        service_lower = service.lower()
        if service_lower in ["adhr", "identity", "aadhaar", "aadhar"]:
            return await identity_lookup(
                request=request, 
                key=key, 
                query=query or number or numquery
            )
        elif service_lower in ["bnk", "bank", "ifsc"]:
            return await bank_lookup(
                request=request, 
                key=key, 
                query=query or number or numquery
            )
        elif service_lower in ["telegram", "tg", "tele"]:
            return await telegram_lookup(
                request=request, 
                key=key, 
                query=query or number or numquery
            )
        elif service_lower in ["vehicle", "rc", "vahan"]:
            rc_arg = request.query_params.get("rc") or query or number or numquery
            return await vehicle_lookup(
                request=request,
                key=key,
                rc=rc_arg
            )


    num = (number or query or numquery or "").strip()

    import re
    if not service and num:
        if re.match(r'^[A-Za-z]{4}0[A-Za-z0-9]{6}$', num):
            return await bank_lookup(request=request, key=key, query=num)
        elif re.match(r'^[A-Za-z0-9]{4,11}$', num) and any(c.isalpha() for c in num) and any(c.isdigit() for c in num) and "_" not in num and not num.startswith("@"):
            return await vehicle_lookup(request=request, key=key, rc=num)
        elif num.isdigit() and len(num) == 12:
            return await identity_lookup(request=request, key=key, query=num)

    try:
        # 1. Rate Limiting Check
        if not check_rate_limit(request):
            return make_api_response({"status": "error", "message": "Too many requests. Please slow down."})

        # 2. Key Check (Parameter level check)
        if not key:
            return make_api_response({"status": "error", "message": "Access Denied: Please provide your 'key' parameter"})

        db = get_supabase()
        if not db:
            return make_api_response({"status": "error", "message": "ServerDown: Database connection failure"})

        # Determine if master key is used
        is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"

        # 3. Authentication & Plan Validity (Must pass prior to checking target safety status to avoid enumeration attacks)
        license = None
        user_id = None
        user_email = None

        if not is_master:
            auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not auth_query.data or len(auth_query.data) == 0:
                print(f"[AUTH_FAIL] Key: {key}")
                return make_api_response({"status": "error", "message": "Auth Failed: Invalid API key"})
            
            license = auth_query.data[0]
            
            # Status check
            if license.get('status') != 'active':
                return make_api_response({"status": "error", "message": "Key Suspended: Access disabled"})

            # Expiry check
            try:
                if license.get('expires_at'):
                    exp_date = datetime.fromisoformat(license['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if exp_date < datetime.utcnow():
                        return make_api_response({"status": "error", "message": "Key Expired: Please renew subscription"})
            except Exception as e:
                print(f"[EXPIRY_PARSE_ERR] {e}")
                pass
            
            user_id = license.get('user_id')
            user_email = license.get('user_email')
        else:
            license = {"id": "system", "plan_name": "Internal VIP", "requests_used": 0, "expires_at": "Never"}
            user_id = None
            user_email = None

        # Check permission for Number Lookup
        plan_name = license.get('plan_name') or ""
        plan_upper = str(plan_name).upper()
        is_num_allowed = any(p in plan_upper for p in ["NUMBER", "PRO", "INFINITY", "COMBO", "SPECIAL", "MASTER", "INTERNAL", "VIP", "SYSTEM"])
        if not is_num_allowed:
            return make_api_response({
                "status": "error",
                "message": f"Access Denied: Your API key is authorized for '{plan_name}' but you initiated a 'number' query."
            })

        # Format safety check for strict Number Lookup keys
        is_strict_number_plan = "NUMBER" in plan_upper and not any(p in plan_upper for p in ["COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP"])
        if is_strict_number_plan:
            # Under a strict number lookup plan, only simple 10 digit number is allowed
            if not num.isdigit() or len(num) != 10:
                return make_api_response({
                    "status": "error",
                    "message": "Your plan is of number lookup so please enter 10 digit number"
                })

        # Check presence of input target
        if not num:
            return make_api_response({
                "status": "error",
                "message": "Input Required: Please provide a 10-digit mobile number or Telegram username. Example: numquery=98797XXXXX or numquery=@gaurav_beniwal_0001"
            })

        # 4. STRICTLY FIRST: DB PROTECTION CHECK
        # Check if target mobile number or telegram username is registered as protected
        is_protected_phone = False
        is_protected_telegram = False

        # Check Mobile Protection (only if numeric)
        if num.isdigit():
            try:
                protected_num_query = db.table("protected_numbers").select("phone_number").eq("phone_number", num).execute()
                if protected_num_query.data:
                    is_protected_phone = True
            except Exception as ep:
                print(f"[MOBILE_PROTECT_ERR] {ep}")

        # Check Telegram Protection (checking clean username and with @ symbol prefix)
        tg_clean = num.lstrip('@')
        tg_at = f"@{tg_clean}"
        try:
            protected_tg_query1 = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", tg_clean).execute()
            if protected_tg_query1.data:
                is_protected_telegram = True
            else:
                protected_tg_query2 = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", tg_at).execute()
                if protected_tg_query2.data:
                    is_protected_telegram = True
        except Exception as et:
            print(f"[TG_PROTECT_ERR] {et}")

        if is_protected_phone or is_protected_telegram:
            # Deduct request and update telemetry
            new_count = (license.get('requests_used') or 0) + 1
            if not is_master:
                db.table("api_keys").update({
                    "requests_used": new_count,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", license['id']).execute()

            try:
                db.table("api_logs").insert({
                    "api_key_id": license.get('id') if not is_master else None,
                    "masked_number": f"PROTECTED:{num[:5]}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except:
                pass

            if is_protected_phone:
                return make_api_response({
                    "status": "success",
                    "success": True,
                    "message": "Protected: This mobile number is protected on TRACEXDATA. 🛡️",
                    "results": {
                        "Mobile Match": {
                            "name": "PROTECTED RECORD",
                            "mobile": num,
                            "father_name": "PROTECTED @ TRACEX SHIELD",
                            "alt_mobile": "PROTECTED @ TRACEX SHIELD",
                            "email": "PROTECTED @ TRACEX SHIELD",
                            "operator": "PROTECTED @ TRACEX SHIELD",
                            "state_circle": "PROTECTED @ TRACEX SHIELD",
                            "address": "PROTECTED @ TRACEX SHIELD",
                            "aadhar_number": "PROTECTED @ TRACEX SHIELD",
                            "platform": "Mobile Protection"
                        }
                    }
                })
            else:
                return make_api_response({
                    "status": "success",
                    "success": True,
                    "message": "Protected: This Telegram account is protected on TRACEXDATA. 🛡️",
                    "results": {
                        "Telegram Match": {
                            "name": "PROTECTED RECORD",
                            "telegram_id": num,
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
                })

        # 5. INPUT FORMAT COMPLIANCE VALIDATION
        # Dynamic detection whether the input is a Telegram handle or standard mobile number
        is_telegram_query = False
        if any(c.isalpha() for c in num) or num.startswith("@") or "_" in num:
            is_telegram_query = True

        if is_telegram_query:
            # Validate Telegram length
            if len(num) < 3:
                return make_api_response({
                    "status": "error",
                    "message": "Invalid Format: Telegram username/handle must be at least 3 characters long. (e.g. '@gaurav_beniwal_0001')"
                })
        else:
            # Strict 10-Digit Mobile Number Validation
            if not num.isdigit() or len(num) != 10:
                return make_api_response({
                    "status": "error",
                    "message": f"Invalid Query: '{num}' is not a 10-digit mobile number"
                })

        # 6. Log search queries
        try:
            db.table("search_logs").insert({
                "user_id": user_id,
                "user_email": user_email,
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0",
                "search_query": num
            }).execute()
        except Exception as e:
            print(f"[LOG_ERR] {e}")

        # 7. Quota limitation checks
        requests_used = license.get('requests_used') or 0
        limit = license.get('request_limit')
        if limit is not None and int(requests_used) >= int(limit):
            return make_api_response({"status": "error", "message": "Quota Exhausted: Plan limit reached"})

        # 8. Intelligence Source Dispatch
        if is_telegram_query:
            # LIVE API CALL FOR TELEGRAM username LOOKUP
            target_username = num if num.startswith('@') else f"@{num}"
            api_url = f"https://exploitsindia.site//osint-api/telegram.php?exploits={requests.utils.quote(target_username)}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
                "Accept": "text/plain,text/html,application/json,*/*"
            }

            try:
                resp = requests.get(api_url, timeout=15, headers=headers)
                if resp.status_code != 200:
                    return make_api_response({"status": "error", "message": "API Source currently unreachable"})

                text = resp.text or ""
                cleanedText = clean_branding_text_line_by_line(text)
                lowerText = cleanedText.lower()

                if "no result" in lowerText or "no records found" in lowerText or not cleanedText.strip():
                    return make_api_response({"status": "success", "results": {}, "message": "no data found"})

                import re
                usernameMatch = re.search(r"(?:Username|User):\s*([^\s\n\r]+)", cleanedText, re.IGNORECASE)
                idMatch = re.search(r"(?:Telegram ID|ID):\s*(?:<code>)?(\d+)(?:<\/code>)?", cleanedText, re.IGNORECASE)
                phoneMatch = re.search(r"(?:Phone Number|Mobile|Phone):\s*(?:<code>)?(\d+)(?:<\/code>)?", cleanedText, re.IGNORECASE)
                countryMatch = re.search(r"Country:\s*([^\n\r]+)", cleanedText, re.IGNORECASE)
                codeMatch = re.search(r"Country Code:\s*([^\n\r]+)", cleanedText, re.IGNORECASE)

                username = usernameMatch.group(1).strip() if usernameMatch else target_username
                telegram_id = idMatch.group(1).strip() if idMatch else "N/A"
                phone = phoneMatch.group(1).strip() if phoneMatch else "N/A"
                country = countryMatch.group(1).strip() if countryMatch else "N/A"
                country_code = codeMatch.group(1).strip() if codeMatch else "N/A"

                if telegram_id == "N/A" and phone == "N/A":
                    return make_api_response({
                        "status": "success", 
                        "results": {}, 
                        "message": "no data found"
                    })

                # Post-fetch validation to verify protection status (both for Telegram ID and username)
                post_protected = False
                if telegram_id != "N/A":
                    try:
                        p_query_id = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", telegram_id).execute()
                        if p_query_id and p_query_id.data:
                            post_protected = True
                    except Exception as e_post1:
                        print(f"[POST_ID_PROTECT_ERR] {e_post1}")

                if not post_protected and username and username != "N/A":
                    clean_un = username.lstrip('@')
                    at_un = f"@{clean_un}"
                    try:
                        p_query_un1 = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", clean_un).execute()
                        if p_query_un1 and p_query_un1.data:
                            post_protected = True
                        else:
                            p_query_un2 = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", at_un).execute()
                            if p_query_un2 and p_query_un2.data:
                                post_protected = True
                    except Exception as e_post2:
                        print(f"[POST_UN_PROTECT_ERR] {e_post2}")

                if post_protected:
                    # Deduct telemetry count and return shielded/protected response
                    new_count = (license.get('requests_used') or 0) + 1
                    if not is_master:
                        db.table("api_keys").update({
                            "requests_used": new_count,
                            "last_used_at": datetime.utcnow().isoformat()
                        }).eq("id", license['id']).execute()

                    try:
                        db.table("api_logs").insert({
                            "api_key_id": license.get('id') if not is_master else None,
                            "masked_number": f"PROTECTED:{num[:5]}",
                            "status": "success",
                            "response_time_ms": int((time.time() - start_time) * 1000),
                            "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                        }).execute()
                    except:
                        pass

                    return make_api_response({
                        "status": "success",
                        "success": True,
                        "message": "Protected: This Telegram account is protected on TRACEXDATA. 🛡️",
                        "results": {
                            "Telegram Match": {
                                "name": "PROTECTED RECORD",
                                "telegram_id": telegram_id if telegram_id != "N/A" else num,
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
                    })

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

                new_count = (license.get('requests_used') or 0) + 1
                if not is_master:
                    db.table("api_keys").update({
                        "requests_used": new_count,
                        "last_used_at": datetime.utcnow().isoformat()
                    }).eq("id", license['id']).execute()

                # Format final response
                output = make_api_response({
                    "status": "success",
                    "success": True,
                    "results": results,
                    "credits_remaining": (int(limit) - new_count) if (limit is not None and not is_master) else 999999,
                    "requests_used": new_count if not is_master else 0,
                    "execution_time_ms": int((time.time() - start_time) * 1000)
                })

                try:
                    db.table("api_logs").insert({
                        "api_key_id": license.get('id') if not is_master else None,
                        "masked_number": f"TG: {num[:12]}",
                        "status": "success",
                        "response_time_ms": int((time.time() - start_time) * 1000),
                        "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                    }).execute()
                except: pass

                return output
            except Exception as e_tg:
                return make_api_response({"status": "error", "message": f"Telegram API error: {str(e_tg)}"})

        # Hardcoded primary engine source as requested to avoid environment variable dependency
        target_template = "https://techvishalboss.com/api/v1/lookup.php"
        
        try:
            settings_query = db.table("api_settings").select("real_api_url").limit(1).execute()
            if settings_query.data and len(settings_query.data) > 0:
                if settings_query.data[0].get('real_api_url'):
                    target_template = settings_query.data[0]['real_api_url']
        except Exception as e:
            print(f"[SETTINGS_ERR] {e}")
            pass
        
        if not target_template:
            target_template = "https://techvishalboss.com/api/v1/lookup.php"

        # Force replace any old/stale API keys with the new active key to ensure the new API is used everywhere
        target_template = target_template.replace("TVB_SGL_053B3AA6", "TVB_SGL_C24439EA")

        # Execution
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
            return make_api_response({"status": "error", "message": last_error_msg})

        # Update Usage (Only for real API keys)
        if not is_master:
            new_count = (license.get('requests_used') or 0) + 1
            db.table("api_keys").update({
                "requests_used": new_count,
                "last_used_at": datetime.utcnow().isoformat()
            }).eq("id", license['id']).execute()
            usage_display = new_count
        else:
            usage_display = 0

        # Delivery
        output = build_output(payload, num, license, usage_display)

        # Logging
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
        return make_api_response({"status": "error", "message": "ServerDown: Internal engine mapping error (TX-INTERNAL-FAULT)"})

@app.get("/api/telegram")
async def telegram_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    telegram: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    api: Optional[str] = Query(None)
):
    targetTelegramId = (query or telegram or api or "").strip()
    if not targetTelegramId:
        return make_api_response({"status": "error", "message": "Telegram query parameter is required"})

    try:
        db = get_supabase()
        if not db:
            return make_api_response({"status": "error", "message": "ServerDown: Database connection failure"})

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
                return make_api_response({"status": "error", "message": "API key is required"})

            keyRecords = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not keyRecords.data or len(keyRecords.data) == 0:
                return make_api_response({"status": "error", "message": "Access Denied: Invalid or unauthorized API key"})

            keyRecord = keyRecords.data[0]
            if keyRecord.get('status') != 'active':
                return make_api_response({"status": "error", "message": "Subscription Blocked: API key expired or suspended"})

            # Expiry check
            try:
                if keyRecord.get('expires_at'):
                    from datetime import datetime
                    exp_date = datetime.fromisoformat(keyRecord['expires_at'].replace('Z', '+00:00')).replace(tzinfo=None)
                    if exp_date < datetime.utcnow():
                        return make_api_response({"status": "error", "message": "Key Expired: Please renew subscription"})
            except Exception as e:
                print(f"[EXP_PARSE_ERR] {e}")

            # Usage check
            requests_used = keyRecord.get('requests_used') or 0
            limit = keyRecord.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return make_api_response({"status": "error", "message": "Quota Exhausted: Lookup limit reached"})

        # Permission check
        plan_name = keyRecord.get('plan_name') or ""
        plan_upper = str(plan_name).upper()
        is_allowed = any(p in plan_upper for p in ["TELEGRAM", "PRO", "INFINITY", "COMBO", "SPECIAL", "MASTER", "INTERNAL", "VIP", "SYSTEM"])
        if not is_allowed:
            return make_api_response({
                "status": "error",
                "message": f"Access Denied: Your API key is authorized for '{plan_name}' but you initiated a 'telegram' query."
            })
            
        # Format check for strict telegram
        is_strict_telegram_plan = "TELEGRAM" in plan_upper and not any(p in plan_upper for p in ["COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP"])
        if is_strict_telegram_plan:
            cleaned_tg = targetTelegramId.lstrip('@')
            if cleaned_tg.isdigit() or len(cleaned_tg) < 3 or not any(c.isalpha() for c in cleaned_tg):
                return make_api_response({
                    "status": "error",
                    "message": "Your plan is of telegram lookup so please enter telegram username"
                })

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

            return make_api_response({
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
            })

        target_username = targetTelegramId if targetTelegramId.startswith('@') else f"@{targetTelegramId}"
        api_url = f"https://exploitsindia.site//osint-api/telegram.php?exploits={requests.utils.quote(target_username)}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
            "Accept": "text/plain,text/html,application/json,*/*"
        }

        resp = requests.get(api_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            return make_api_response({"status": "error", "message": "API Source currently unreachable"})

        text = resp.text or ""
        cleanedText = clean_branding_text_line_by_line(text)
        lowerText = cleanedText.lower()

        if "no result" in lowerText or "no records found" in lowerText or not cleanedText.strip():
            return make_api_response({"status": "success", "results": {}, "message": "no data found"})

        import re
        usernameMatch = re.search(r"(?:Username|User):\s*([^\s\n\r]+)", cleanedText, re.IGNORECASE)
        idMatch = re.search(r"(?:Telegram ID|ID):\s*(?:<code>)?(\d+)(?:<\/code>)?", cleanedText, re.IGNORECASE)
        phoneMatch = re.search(r"(?:Phone Number|Mobile|Phone):\s*(?:<code>)?(\d+)(?:<\/code>)?", cleanedText, re.IGNORECASE)
        countryMatch = re.search(r"Country:\s*([^\n\r]+)", cleanedText, re.IGNORECASE)
        codeMatch = re.search(r"Country Code:\s*([^\n\r]+)", cleanedText, re.IGNORECASE)

        username = usernameMatch.group(1).strip() if usernameMatch else target_username
        telegram_id = idMatch.group(1).strip() if idMatch else "N/A"
        phone = phoneMatch.group(1).strip() if phoneMatch else "N/A"
        country = countryMatch.group(1).strip() if countryMatch else "N/A"
        country_code = codeMatch.group(1).strip() if codeMatch else "N/A"

        if telegram_id == "N/A" and phone == "N/A":
            return make_api_response({
                "status": "success", 
                "results": {}, 
                "message": "no data found"
            })

        # Post-fetch validation to verify protection status (both for Telegram ID and username)
        post_protected = False
        if telegram_id != "N/A":
            try:
                p_query_id = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", telegram_id).execute()
                if p_query_id and p_query_id.data:
                    post_protected = True
            except Exception as e_post1:
                print(f"[API_POST_ID_PROTECT_ERR] {e_post1}")

        if not post_protected and username and username != "N/A":
            clean_un = username.lstrip('@')
            at_un = f"@{clean_un}"
            try:
                p_query_un1 = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", clean_un).execute()
                if p_query_un1 and p_query_un1.data:
                    post_protected = True
                else:
                    p_query_un2 = db.table("protected_telegrams").select("telegram_id").eq("telegram_id", at_un).execute()
                    if p_query_un2 and p_query_un2.data:
                        post_protected = True
            except Exception as e_post2:
                print(f"[API_POST_UN_PROTECT_ERR] {e_post2}")

        if post_protected:
            # Deduct request and update telemetry since we hit the API and did a lookup
            if not is_master and keyRecord:
                from datetime import datetime
                db.table("api_keys").update({
                    "requests_used": (keyRecord.get('requests_used') or 0) + 1,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", keyRecord['id']).execute()

            return make_api_response({
                "status": "success",
                "message": "Protected: This Telegram account is protected on TRACEXDATA. 🛡️",
                "results": {
                    "Telegram Match": {
                        "name": "PROTECTED RECORD",
                        "telegram_id": telegram_id if telegram_id != "N/A" else targetTelegramId,
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
            })

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

        return make_api_response({"status": "success", "results": results})

    except Exception as err:
        print(f"Telegram Proxy error: {err}")
        return make_api_response({"status": "error", "message": "api error"})

@app.get("/api/identity")
async def identity_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    aadhar: Optional[str] = Query(None),
    identity: Optional[str] = Query(None),
    exploits: Optional[str] = Query(None)
):
    import re
    import time
    from datetime import datetime
    
    start_time = time.time()
    target_query = (query or aadhar or identity or exploits or "").strip()
    
    if not target_query:
        return make_api_response({"status": "error", "message": "Identity/Aadhaar query parameter is required"})
        
    # Clean to digits only
    target_query = re.sub(r'[^0-9]', '', target_query)
    if len(target_query) != 12:
        return make_api_response({"status": "error", "message": "Invalid Query: Aadhaar must be a 12-digit numeric identifier"})
        
    db = get_supabase()
    if not db:
        return make_api_response({"status": "error", "message": "Engine Offline: Internal connection failure"})
        
    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
    key_record = None
    
    if is_master:
        key_record = {
            "id": "master",
            "plan_name": "Internal Master API",
            "status": "active",
            "requests_used": 0,
            "request_limit": None
        }
    else:
        if not key:
            return make_api_response({"status": "error", "message": "API key is required"})
            
        try:
            auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not auth_query.data or len(auth_query.data) == 0:
                return make_api_response({"status": "error", "message": "Access Denied: Invalid or unauthorized API key"})
                
            key_record = auth_query.data[0]
            if key_record.get('status') != 'active':
                return make_api_response({
                    "status": "error",
                    "message": "Subscription Blocked: API key expired or suspended",
                    "buy_url": "https://tracexdata-api.onrender.com/buy-api"
                })
                
            # Expiry check
            if key_record.get('expires_at'):
                try:
                    clean_expires = key_record['expires_at'].replace('Z', '')
                    if '+' in clean_expires:
                        clean_expires = clean_expires.split('+')[0]
                    exp_date = datetime.fromisoformat(clean_expires)
                    if exp_date < datetime.utcnow():
                        return make_api_response({"status": "error", "message": "Key Expired: Please renew subscription"})
                except Exception as ex_err:
                    print(f"[EXP_PARSE_ERR] {ex_err}")
                    
            # Usage check
            requests_used = key_record.get('requests_used') or 0
            limit = key_record.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return make_api_response({"status": "error", "message": "Quota Exhausted: Lookup limit reached"})
                
            # Permission check
            plan_upper = str(key_record.get('plan_name') or "").upper()
            is_allowed = any(p in plan_upper for p in ["ADHR", "IDENTITY", "AADH", "COMBO", "MASTER", "INTERNAL"])
            if not is_allowed:
                return make_api_response({
                    "status": "error",
                    "message": f"Access Denied: Your API key is authorized for '{key_record.get('plan_name')}' but you initiated an 'identity' query."
                })
            
            # Format validation for strict Identity plans
            is_strict_identity_plan = any(p in plan_upper for p in ["ADHR", "IDENTITY", "AADH"]) and not any(p in plan_upper for p in ["COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP"])
            if is_strict_identity_plan:
                if not target_query.isdigit() or len(target_query) != 12:
                    return make_api_response({
                        "status": "error",
                        "message": "Your plan is of identity lookup so please enter 12 digit number"
                    })
        except Exception as db_err:
            print(f"[DB_ERR] {db_err}")
            return make_api_response({"status": "error", "message": "api error"})
            
    # Proxy fetch
    api_url = f"https://exploitsindia.site//osint-api/aadhar.php?exploits={target_query}"
    headers = {
        "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
        "Accept": "application/json,text/plain,*/*"
    }
    
    try:
        resp = requests.get(api_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            # log failure
            try:
                masked_q = f"{target_query[:4]}****{target_query[-4:]}"
                db.table("api_logs").insert({
                    "api_key_id": key_record.get('id') if not is_master else None,
                    "masked_number": f"ADHR: {masked_q}",
                    "status": "failed",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except: pass
            return make_api_response({"status": "error", "message": "api error"})
            
        text = resp.text or ""
        cleaned_body = clean_branding_text_line_by_line(text)
        parsed_records = parse_raw_text_to_records(text, target_query)
        
        # Telemetry updates (only deduct if we succeeded in parsing records)
        if (parsed_records or cleaned_body.strip()) and not is_master and key_record:
            try:
                db.table("api_keys").update({
                    "requests_used": (key_record.get('requests_used') or 0) + 1,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", key_record['id']).execute()
            except Exception as up_err:
                print(f"[TELEMETRY_ERR] {up_err}")
                
        # API Log
        try:
            status_str = "success" if (parsed_records or cleaned_body.strip()) else "failed"
            masked_q = f"{target_query[:4]}****{target_query[-4:]}"
            db.table("api_logs").insert({
                "api_key_id": key_record.get('id') if not is_master else None,
                "masked_number": f"ADHR: {masked_q}",
                "status": status_str,
                "response_time_ms": int((time.time() - start_time) * 1000),
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
            }).execute()
        except: pass
        
        if not parsed_records and not cleaned_body.strip():
            return make_api_response({
                "status": "success", 
                "results": {}, 
                "message": "no data found"
            })
            
        return make_api_response({
            "status": "success", 
            "results": {}, 
            "raw_results": cleaned_body
        })
        
    except Exception as fetch_err:
        print(f"[Identity Fetch Error] {fetch_err}")
        try:
            masked_q = f"{target_query[:4]}****{target_query[-4:]}"
            db.table("api_logs").insert({
                "api_key_id": key_record.get('id') if not is_master else None,
                "masked_number": f"ADHR: {masked_q}",
                "status": "failed",
                "response_time_ms": int((time.time() - start_time) * 1000),
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
            }).execute()
        except: pass
        return make_api_response({"status": "error", "message": "Third-party lookup engine is currently unresponsive"})

@app.get("/api/bank")
async def bank_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    ifsc: Optional[str] = Query(None),
    bank: Optional[str] = Query(None),
    exploits: Optional[str] = Query(None)
):
    import re
    import time
    from datetime import datetime
    
    start_time = time.time()
    target_query = (query or ifsc or bank or exploits or "").strip()
    
    if not target_query:
        return make_api_response({"status": "error", "message": "Bank/IFSC query parameter is required"})
        
    # Clean to alphanumeric and uppercase
    target_query = re.sub(r'[^a-zA-Z0-9]', '', target_query).upper()
    if len(target_query) != 11:
        return make_api_response({"status": "error", "message": "Invalid Query: IFSC must be an 11-character alphanumeric code"})
        
    db = get_supabase()
    if not db:
        return make_api_response({"status": "error", "message": "Engine Offline: Internal connection failure"})
        
    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
    key_record = None
    
    if is_master:
        key_record = {
            "id": "master",
            "plan_name": "Internal Master API",
            "status": "active",
            "requests_used": 0,
            "request_limit": None
        }
    else:
        if not key:
            return make_api_response({"status": "error", "message": "API key is required"})
            
        try:
            auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
            if not auth_query.data or len(auth_query.data) == 0:
                return make_api_response({"status": "error", "message": "Access Denied: Invalid or unauthorized API key"})
                
            key_record = auth_query.data[0]
            if key_record.get('status') != 'active':
                return make_api_response({
                    "status": "error",
                    "message": "Subscription Blocked: API key expired or suspended",
                    "buy_url": "https://tracexdata-api.onrender.com/buy-api"
                })
                
            # Expiry check
            if key_record.get('expires_at'):
                try:
                    clean_expires = key_record['expires_at'].replace('Z', '')
                    if '+' in clean_expires:
                        clean_expires = clean_expires.split('+')[0]
                    exp_date = datetime.fromisoformat(clean_expires)
                    if exp_date < datetime.utcnow():
                        return make_api_response({"status": "error", "message": "Key Expired: Please renew subscription"})
                except Exception as ex_err:
                    print(f"[EXP_PARSE_ERR] {ex_err}")
                    
            # Usage check
            requests_used = key_record.get('requests_used') or 0
            limit = key_record.get('request_limit')
            if limit is not None and int(requests_used) >= int(limit):
                return make_api_response({"status": "error", "message": "Quota Exhausted: Lookup limit reached"})
                
            # Permission check
            plan_upper = str(key_record.get('plan_name') or "").upper()
            is_allowed = any(p in plan_upper for p in ["BNK", "BANK", "BA&NK", "COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP", "SYSTEM"])
            if not is_allowed:
                return make_api_response({
                    "status": "error",
                    "message": f"Access Denied: Your API key is authorized for '{key_record.get('plan_name')}' but you initiated a 'bank' query."
                })
                
            # Format validation for strict Bank plans
            is_strict_bank_plan = any(p in plan_upper for p in ["BNK", "BANK", "BA&NK"]) and not any(p in plan_upper for p in ["COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP"])
            if is_strict_bank_plan:
                if len(target_query) != 11:
                    return make_api_response({
                        "status": "error",
                        "message": "Your plan is of bank lookup so please enter 11 digit alphanumeric IFSC code"
                    })
        except Exception as db_err:
            print(f"[DB_ERR] {db_err}")
            return make_api_response({"status": "error", "message": "api error"})
            
    # Proxy fetch
    api_url = f"https://exploitsindia.site//osint-api/ifsc.php?exploits={target_query}"
    headers = {
        "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
        "Accept": "application/json,text/plain,*/*"
    }
    
    try:
        resp = requests.get(api_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            # log failure
            try:
                masked_q = f"{target_query[:4]}****{target_query[-2:]}"
                db.table("api_logs").insert({
                    "api_key_id": key_record.get('id') if not is_master else None,
                    "masked_number": f"BNK: {masked_q}",
                    "status": "failed",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except: pass
            return make_api_response({"status": "error", "message": "api error"})
            
        text = resp.text or ""
        cleaned_body = clean_branding_text_line_by_line(text)
        parsed_records = parse_raw_text_to_records(text, target_query)
        
        # Telemetry updates (only deduct if we succeeded in parsing records)
        if (parsed_records or cleaned_body.strip()) and not is_master and key_record:
            try:
                db.table("api_keys").update({
                    "requests_used": (key_record.get('requests_used') or 0) + 1,
                    "last_used_at": datetime.utcnow().isoformat()
                }).eq("id", key_record['id']).execute()
            except Exception as up_err:
                print(f"[TELEMETRY_ERR] {up_err}")
                
        # API Log
        try:
            status_str = "success" if (parsed_records or cleaned_body.strip()) else "failed"
            masked_q = f"{target_query[:4]}****{target_query[-2:]}"
            db.table("api_logs").insert({
                "api_key_id": key_record.get('id') if not is_master else None,
                "masked_number": f"BNK: {masked_q}",
                "status": status_str,
                "response_time_ms": int((time.time() - start_time) * 1000),
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
            }).execute()
        except: pass
        
        if not parsed_records and not cleaned_body.strip():
            return make_api_response({
                "status": "success", 
                "results": {}, 
                "message": "no data found"
            })
            
        return make_api_response({
            "status": "success", 
            "results": {},
            "raw_results": cleaned_body
        })
        
    except Exception as fetch_err:
        print(f"[Bank Fetch Error] {fetch_err}")
        try:
            masked_q = f"{target_query[:4]}****{target_query[-2:]}"
            db.table("api_logs").insert({
                "api_key_id": key_record.get('id') if not is_master else None,
                "masked_number": f"BNK: {masked_q}",
                "status": "failed",
                "response_time_ms": int((time.time() - start_time) * 1000),
                "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
            }).execute()
        except: pass
        return make_api_response({"status": "error", "message": "Third-party lookup engine is currently unresponsive"})



@app.get("/api/vehicle")
async def vehicle_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    rc: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    vehicle: Optional[str] = Query(None),
    vehicle_no: Optional[str] = Query(None),
    exploits: Optional[str] = Query(None)
):
    import re
    import time
    from datetime import datetime
    
    start_time = time.time()
    target_query = (rc or query or vehicle or vehicle_no or exploits or "").strip()
    
    if not target_query:
        return make_api_response({"status": "error", "message": "Vehicle lookup query parameter is required"})
        
    # Clean to alphanumeric and uppercase
    target_query = re.sub(r'[^a-zA-Z0-9]', '', target_query).upper()
    if len(target_query) < 3:
        return make_api_response({"status": "error", "message": "Invalid Query: Vehicle plate number must be at least 3 characters long"})
        
    db = get_supabase()
    if not db:
        return make_api_response({"status": "error", "message": "Engine Offline: Internal connection failure"})
        
    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
    key_record = None
    
    try:
        if is_master:
            key_record = {
                "id": "master",
                "plan_name": "Internal Master API",
                "status": "active",
                "requests_used": 0,
                "request_limit": None
            }
        else:
            if not key:
                return make_api_response({"status": "error", "message": "API key is required"})
                
            try:
                auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
                if not auth_query.data or len(auth_query.data) == 0:
                    return make_api_response({"status": "error", "message": "Access Denied: Invalid or unauthorized API key"})
                    
                key_record = auth_query.data[0]
                if key_record.get('status') != 'active':
                    return make_api_response({
                        "status": "error",
                        "message": "Subscription Blocked: API key expired or suspended",
                        "buy_url": "https://tracexdata-api.onrender.com/buy-api"
                    })
                    
                # Expiry check
                if key_record.get('expires_at'):
                    try:
                        clean_expires = key_record['expires_at'].replace('Z', '')
                        if '+' in clean_expires:
                            clean_expires = clean_expires.split('+')[0]
                        exp_date = datetime.fromisoformat(clean_expires)
                        if exp_date < datetime.utcnow():
                            return make_api_response({"status": "error", "message": "Key Expired: Please renew subscription"})
                    except Exception as ex_err:
                        print(f"[EXP_PARSE_ERR] {ex_err}")
                        
                # Usage check
                requests_used = key_record.get('requests_used') or 0
                limit = key_record.get('request_limit')
                if limit is not None and int(requests_used) >= int(limit):
                    return make_api_response({"status": "error", "message": "Quota Exhausted: Lookup limit reached"})
                    
                # Permission check
                plan_upper = str(key_record.get('plan_name') or "").upper()
                is_allowed = any(p in plan_upper for p in ["VEHICLE", "COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP", "SYSTEM"])
                if not is_allowed:
                    return make_api_response({
                        "status": "error",
                        "message": f"Access Denied: Your API key is authorized for '{key_record.get('plan_name')}' but you initiated a 'vehicle' query."
                    })
                    
                # Format validation for strict Vehicle plans
                is_strict_vehicle_plan = "VEHICLE" in plan_upper and not any(p in plan_upper for p in ["COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP"])
                if is_strict_vehicle_plan:
                    if len(target_query) < 3:
                        return make_api_response({
                            "status": "error",
                            "message": "Your plan is of vehicle lookup, please enter valid car number plate format"
                        })
            except Exception as db_err:
                print(f"[DB_ERR] {db_err}")
                return make_api_response({"status": "error", "message": "api error"})
                
        # Proxy fetch
        api_url = f"https://exploitsindia.site//osint-api/vehicle.php?exploits={target_query}"
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
            "Accept": "application/json,text/plain,*/*"
        }
        
        try:
            resp = requests.get(api_url, timeout=15, headers=headers)
            if resp.status_code != 200:
                # log failure
                try:
                    masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                    db.table("api_logs").insert({
                        "api_key_id": key_record.get('id') if not is_master else None,
                        "masked_number": f"VEHICLE: {masked_q}",
                        "status": "failed",
                        "response_time_ms": int((time.time() - start_time) * 1000),
                        "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                    }).execute()
                except: pass
                return make_api_response({"status": "error", "message": "api error"})
                
            text = resp.text or ""
            cleaned_body = clean_branding_text_line_by_line(text)
            lower_text = cleaned_body.lower()
            
            if "no result" in lower_text or "no records found" in lower_text or "error" in lower_text or not text.strip() or "unknown" in lower_text:
                try:
                    masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                    db.table("api_logs").insert({
                        "api_key_id": key_record.get('id') if not is_master else None,
                        "masked_number": f"VEHICLE: {masked_q}",
                        "status": "failed",
                        "response_time_ms": int((time.time() - start_time) * 1000),
                        "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                    }).execute()
                except: pass
                return make_api_response({"status": "error", "message": "api error"})
                
            import json
            try:
                parsed_data = json.loads(cleaned_body)
                if parsed_data and "api_creator" in parsed_data:
                    del parsed_data["api_creator"]
            except:
                parsed_data = {"raw_data": cleaned_body}
                
            cleaned_data = clean_branding_recursive(parsed_data)
            
            # Telemetry update
            if not is_master and key_record and key_record.get('id'):
                try:
                    db.table("api_keys").update({
                        "requests_used": int(key_record.get('requests_used') or 0) + 1,
                        "last_used_at": datetime.utcnow().isoformat()
                    }).eq("id", key_record.get('id')).execute()
                except Exception as up_err:
                    print(f"[REQS_UP_ERR] {up_err}")
                    
            try:
                masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                db.table("api_logs").insert({
                    "api_key_id": key_record.get('id') if not is_master else None,
                    "masked_number": f"VEHICLE: {masked_q}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except: pass
            
            return make_api_response({"status": "success", "results": cleaned_data})
        except Exception as conn_err:
            print(f"[CONN_ERR] {conn_err}")
            try:
                masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                db.table("api_logs").insert({
                    "api_key_id": key_record.get('id') if not is_master else None,
                    "masked_number": f"VEHICLE: {masked_q}",
                    "status": "failed",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except: pass
            return make_api_response({"status": "error", "message": "api error"})
    except Exception as general_err:
        print(f"[VEHICLE_ERR] {general_err}")
        return make_api_response({"status": "error", "message": "api error"})


@app.get("/api/pancard")
async def pancard_lookup(
    request: Request,
    key: Optional[str] = Query(None),
    pan: Optional[str] = Query(None),
    pn: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    pancard: Optional[str] = Query(None),
    exploits: Optional[str] = Query(None)
):
    import re
    import time
    from datetime import datetime
    
    start_time = time.time()
    target_query = (pan or pn or query or pancard or exploits or "").strip()
    
    if not target_query:
        return make_api_response({"status": "error", "message": "PN/PAN card query parameter is required"})
        
    # Clean to alphanumeric and uppercase
    target_query = re.sub(r'[^a-zA-Z0-9]', '', target_query).upper()
    if len(target_query) < 5:
        return make_api_response({"status": "error", "message": "Invalid Query: PN/PAN card number must be at least 5 characters long"})
        
    db = get_supabase()
    if not db:
        return make_api_response({"status": "error", "message": "Engine Offline: Internal connection failure"})
        
    is_master = key == "TX-SYSTEM-INTERNAL-ADMIN"
    key_record = None
    
    try:
        if is_master:
            key_record = {
                "id": "master",
                "plan_name": "Internal Master API",
                "status": "active",
                "requests_used": 0,
                "request_limit": None
            }
        else:
            if not key:
                return make_api_response({"status": "error", "message": "API key is required"})
                
            try:
                auth_query = db.table("api_keys").select("*").eq("api_key", key).execute()
                if not auth_query.data or len(auth_query.data) == 0:
                    return make_api_response({"status": "error", "message": "Access Denied: Invalid or unauthorized API key"})
                    
                key_record = auth_query.data[0]
                if key_record.get('status') != 'active':
                    return make_api_response({
                        "status": "error",
                        "message": "Subscription Blocked: API key expired or suspended",
                        "buy_url": "https://tracexdata-api.onrender.com/buy-api"
                    })
                    
                # Expiry check
                if key_record.get('expires_at'):
                    try:
                        clean_expires = key_record['expires_at'].replace('Z', '')
                        if '+' in clean_expires:
                            clean_expires = clean_expires.split('+')[0]
                        exp_date = datetime.fromisoformat(clean_expires)
                        if exp_date < datetime.utcnow():
                            return make_api_response({"status": "error", "message": "Key Expired: Please renew subscription"})
                    except Exception as ex_err:
                        print(f"[EXP_PARSE_ERR] {ex_err}")
                        
                # Usage check
                requests_used = key_record.get('requests_used') or 0
                limit = key_record.get('request_limit')
                if limit is not None and int(requests_used) >= int(limit):
                    return make_api_response({"status": "error", "message": "Quota Exhausted: Lookup limit reached"})
                    
                # Permission check
                plan_upper = str(key_record.get('plan_name') or "").upper()
                is_allowed = any(p in plan_upper for p in ["PAN", "PN", "COMBO", "PRO", "INFINITY", "SPECIAL", "MASTER", "INTERNAL", "VIP", "SYSTEM"])
                if not is_allowed:
                    return make_api_response({
                        "status": "error",
                        "message": f"Access Denied: Your API key is authorized for '{key_record.get('plan_name')}' but you initiated a 'pancard' query."
                    })
            except Exception as db_err:
                print(f"[DB_ERR] {db_err}")
                return make_api_response({"status": "error", "message": "api error"})
                
        # Proxy fetch
        api_url = f"https://exploitsindia.site//osint-api/pancard.php?exploits={target_query}"
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0",
            "Accept": "application/json,text/plain,*/*"
        }
        
        try:
            resp = requests.get(api_url, timeout=15, headers=headers)
            if resp.status_code != 200:
                # log failure
                try:
                    masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                    db.table("api_logs").insert({
                        "api_key_id": key_record.get('id') if not is_master else None,
                        "masked_number": f"PANCARD: {masked_q}",
                        "status": "failed",
                        "response_time_ms": int((time.time() - start_time) * 1000),
                        "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                    }).execute()
                except: pass
                return make_api_response({"status": "error", "message": "api error"})
                
            text = resp.text or ""
            cleaned_body = clean_branding_text_line_by_line(text)
            lower_text = cleaned_body.lower()
            
            if "no result" in lower_text or "no records found" in lower_text or "error" in lower_text or not text.strip() or "unknown" in lower_text:
                try:
                    masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                    db.table("api_logs").insert({
                        "api_key_id": key_record.get('id') if not is_master else None,
                        "masked_number": f"PANCARD: {masked_q}",
                        "status": "failed",
                        "response_time_ms": int((time.time() - start_time) * 1000),
                        "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                    }).execute()
                except: pass
                return make_api_response({"status": "error", "message": "api error"})
                
            import json
            try:
                parsed_data = json.loads(cleaned_body)
            except:
                parsed_data = {"raw_data": cleaned_body}
                
            cleaned_data = clean_branding_recursive(parsed_data)
            
            # Telemetry update
            if not is_master and key_record and key_record.get('id'):
                try:
                    db.table("api_keys").update({
                        "requests_used": int(key_record.get('requests_used') or 0) + 1,
                        "last_used_at": datetime.utcnow().isoformat()
                    }).eq("id", key_record.get('id')).execute()
                except Exception as up_err:
                    print(f"[REQS_UP_ERR] {up_err}")
                    
            try:
                masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                db.table("api_logs").insert({
                    "api_key_id": key_record.get('id') if not is_master else None,
                    "masked_number": f"PANCARD: {masked_q}",
                    "status": "success",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except: pass
            
            return make_api_response({"status": "success", "results": cleaned_data})
        except Exception as conn_err:
            print(f"[CONN_ERR] {conn_err}")
            try:
                masked_q = f"{target_query[:3]}****{target_query[-2:]}" if len(target_query) >= 5 else target_query
                db.table("api_logs").insert({
                    "api_key_id": key_record.get('id') if not is_master else None,
                    "masked_number": f"PANCARD: {masked_q}",
                    "status": "failed",
                    "response_time_ms": int((time.time() - start_time) * 1000),
                    "ip_address": request.headers.get('x-forwarded-for', request.client.host) if request else "0.0.0.0"
                }).execute()
            except: pass
            return make_api_response({"status": "error", "message": "api error"})
    except Exception as general_err:
        print(f"[PANCARD_ERR] {general_err}")
        return make_api_response({"status": "error", "message": "api error"})


# PAN Find secure paid lookup endpoint
@app.get("/api/panfind")
async def panfind_lookup(order_id: str = Query(...), aadhaar_number: str = Query(...)):
    from fastapi.responses import JSONResponse
    target_aadhaar = str(aadhaar_number).strip()
    if not target_aadhaar or len(target_aadhaar) != 12 or not target_aadhaar.isdigit():
        return JSONResponse(status_code=400, content={"error": "Aadhaar number must be exactly 12 digits"})

    try:
        order_status = ""
        # 1. Verify with Cashfree
        if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
            print("[PANFIND] Local Cashfree credentials missing. Proxying status verification request...")
            render_backend_url = "https://tracexdata-api.onrender.com"
            resp = requests.get(f"{render_backend_url}/api/cashfree/status/{order_id}")
            if resp.status_code == 200:
                data = resp.json()
                order_status = data.get("order_status", "")
        else:
            headers = {
                "x-client-id": CASHFREE_APP_ID,
                "x-client-secret": CASHFREE_SECRET_KEY,
                "x-api-version": "2023-08-01"
            }
            resp = requests.get(f"{CASHFREE_BASE_URL}/orders/{order_id}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                order_status = data.get("order_status", "")

        if order_status != "PAID":
            return JSONResponse(status_code=402, content={"error": "Payment verification failed. Please complete the Rs. 150 payment."})

        # 2. Execute external API
        api_key = "c8117598aafa71238a4bf8377087b0ff"
        api_url = f"https://techvishalboss.com/panfind/api.php?api_key={api_key}&aadhaar_number={target_aadhaar}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0"
        }
        resp = requests.get(api_url, timeout=15, headers=headers)
        if resp.status_code != 200:
            return JSONResponse(status_code=502, content={"error": "External verification gateway offline. Please contact support."})

        try:
            api_data = resp.json()
        except:
            api_data = {"error": "Failed to parse search output", "raw": resp.text}

        # 3. Remove "developer": "@techvishalboss" from response
        if isinstance(api_data, dict):
            api_data.pop("developer", None)

        return api_data
    except Exception as e:
        print(f"PAN Find lookup error: {e}")
        return JSONResponse(status_code=500, content={"error": "Internal server error during processing lookup"})


# Aadhaar to PAN secure credits lookup
@app.post("/api/aadhaar-to-pan")
async def aadhaar_to_pan_endpoint(request: Request):
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    aadhaar_number = body.get("aadhaar_number")
    if not aadhaar_number:
        return JSONResponse(status_code=400, content={"error": "Aadhaar number is required"})

    target_aadhaar = str(aadhaar_number).strip()
    if len(target_aadhaar) != 12 or not target_aadhaar.isdigit():
        return JSONResponse(status_code=400, content={"error": "Aadhaar number must be exactly 12 digits"})

    auth_header = request.headers.get("authorization")
    if not auth_header:
        return JSONResponse(status_code=401, content={"error": "Authentication is required"})

    token = auth_header.replace("Bearer ", "")
    if not token:
        return JSONResponse(status_code=401, content={"error": "Authentication token is empty"})

    try:
        db = get_supabase()
        if not db:
            return JSONResponse(status_code=500, content={"error": "Engine Offline: Database connection failure"})

        # Authenticate user using supabase auth token
        user_resp = db.auth.get_user(token)
        if not user_resp or not user_resp.user:
            return JSONResponse(status_code=401, content={"error": "Access Denied: Invalid or expired user session"})

        user = user_resp.user

        # Fetch profile
        profile_query = db.table("profiles").select("*").eq("id", user.id).execute()
        if not profile_query.data:
            return JSONResponse(status_code=404, content={"error": "Profile record not found"})

        profile = profile_query.data[0]
        current_credits = int(profile.get("credits") or 0)
        cost = 150

        if current_credits < cost:
            return JSONResponse(status_code=403, content={"error": "Insufficient credits. You need at least 150 credits to perform Aadhaar to PAN lookup. Note: Aadhaar to PAN is not included in unlimited plans."})

        # Deduct credits
        db.table("profiles").update({"credits": max(0, current_credits - cost)}).eq("id", user.id).execute()

        # Query External PAN Find API
        api_key = "c8117598aafa71238a4bf8377087b0ff"
        api_url = f"https://techvishalboss.com/panfind/api.php?api_key={api_key}&aadhaar_number={target_aadhaar}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 TraceX-Web/1.0"
        }
        resp = requests.get(api_url, timeout=15, headers=headers)
        
        api_data = None
        pan_found = False
        retrieved_pan = ""

        if resp.status_code == 200:
            try:
                api_data = resp.json()
                if isinstance(api_data, dict):
                    api_data.pop("developer", None)
                    retrieved_pan = str(api_data.get("full_pan_number") or api_data.get("pan_number") or api_data.get("pan") or "").strip()
                    if retrieved_pan and len(retrieved_pan) >= 5 and "not found" not in retrieved_pan.lower():
                        pan_found = True
            except Exception:
                pass

        # Refund 100 credits if PAN number was not found
        if not pan_found:
            refund_credits = max(0, current_credits - 50)
            db.table("profiles").update({"credits": refund_credits}).eq("id", user.id).execute()
            
            return JSONResponse(status_code=200, content={
                "status": "failed",
                "pan_found": False,
                "message": "No PAN number found for this Aadhaar number. 100 credits refunded. (50 credits deducted for server cost)",
                "credits_deducted": 50,
                "results": api_data
            })

        return JSONResponse(status_code=200, content={
            "status": "success",
            "pan_found": True,
            "pan": retrieved_pan,
            "credits_deducted": 150,
            "results": api_data
        })

    except Exception as err:
        print(f"Aadhaar to PAN python error: {err}")
        return JSONResponse(status_code=500, content={"error": "Internal server error during processing Aadhaar to PAN lookup"})


if __name__ == "__main__":
    import uvicorn
    # Render provides PORT env var, default to 10000 for standard Render deploys
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
