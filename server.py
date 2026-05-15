import os
import time
import uuid
import httpx
import logging
import datetime
import secrets
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
logger = logging.getLogger("uvicorn")
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Credentials
LOOKUP_API_URL = os.getenv("LOOKUP_API_URL", "https://techvishalboss.com/apibuy/public/lookup.php")
LOOKUP_API_KEY = os.getenv("LOOKUP_API_KEY", "TVB_Y9T032")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
CASHFREE_BASE_URL = os.getenv("CASHFREE_BASE_URL", "https://api.cashfree.com/pg")
SUPABASE_URL = os.getenv("VITE_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY else None

@app.get("/")
def health():
    return {"status": "active", "engine": "TraceXData Intelligence SaaS", "supabase": supabase is not None}

# --- SAAS API FILTERING LOGIC ---
def filter_response(raw: dict, query: str, plan: str, expires: str, used: int):
    results = raw.get('results') or raw.get('data') or (raw if raw.get('status') is True else None)
    cleaned = []
    if results:
        items = results if isinstance(results, list) else [results]
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict): continue
            cleaned.append({
                "result_no": idx,
                "name": item.get('name', 'N/A'),
                "mobile": item.get('mobile', query),
                "alt_mobile": item.get('alt_mobile', 'N/A'),
                "operator": item.get('operator', 'N/A'),
                "circle": item.get('state_circle', 'N/A'),
                "address": item.get('address', 'N/A')
            })
    return {
        "status": "success" if cleaned else "not_found",
        "powered_by": "TraceXData Intelligence",
        "owner": "@gaurav_beniwal_0001",
        "api_status": {"plan": plan, "expires_at": expires, "requests_used": used},
        "data": cleaned
    }

# --- SAAS PUBLIC ENDPOINT ---
@app.get("/api/lookup")
async def saas_lookup(key: str, number: str, request: Request):
    if not supabase: raise HTTPException(500, "DB Error")
    
    # Validate Key
    key_res = supabase.table("api_keys").select("*").eq("api_key", key).single().execute()
    if not key_res.data: return {"status": "error", "message": "Invalid API key"}
    
    k = key_res.data
    now = datetime.datetime.now(datetime.timezone.utc)
    expiry = datetime.datetime.fromisoformat(k['expires_at'].replace('Z', '+00:00'))
    
    if k['status'] != 'active' or expiry < now:
        return {"status": "error", "message": "API Key Expired", "buy_api": "https://tracexnumber.web.app/buy-api"}

    # Fetch Real API
    settings = supabase.table("api_settings").select("*").limit(1).single().execute()
    url_template = settings.data['real_api_url'] if settings.data else LOOKUP_API_URL + "?key=" + LOOKUP_API_KEY + "&query=ENTER_TARGET_HERE"
    
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url_template.replace("ENTER_TARGET_HERE", number))
        
        new_count = k['requests_used'] + 1
        supabase.table("api_keys").update({"requests_used": new_count, "last_used_at": now.isoformat()}).eq("id", k['id']).execute()
        
        # Log Trace
        supabase.table("api_logs").insert({
            "api_key_id": k['id'], "masked_number": number[:5]+"****", "status": "processed", "response_time_ms": 100
        }).execute()
        
        return filter_response(resp.json(), number, k['plan_name'], k['expires_at'], new_count)

# --- PAYMENT & FULFILLMENT ---
@app.post("/api/cashfree/create-order")
async def create_order(request: Request):
    body = await request.json()
    order_id = f"order_{int(time.time())}_{secrets.token_hex(3)}"
    
    payload = {
        "order_id": order_id,
        "order_amount": float(body.get("amount")),
        "order_currency": "INR",
        "customer_details": {"customer_id": body.get("user_id"), "customer_email": body.get("user_email", "cust@example.com"), "customer_phone": "9999999999"},
        "order_meta": {"return_url": body.get("return_url") or f"https://tracexnumber.web.app?order_id={order_id}"}
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(f"{CASHFREE_BASE_URL}/orders", json=payload, headers={
            "x-client-id": CASHFREE_APP_ID, "x-client-secret": CASHFREE_SECRET_KEY, "x-api-version": "2023-08-01", "Content-Type": "application/json"
        })
        if res.status_code == 200 and supabase:
            supabase.table("payment_claims").insert({
                "payment_id": order_id, "user_id": body.get("user_id"), "user_email": body.get("user_email"),
                "plan_id": body.get("plan_id"), "amount": float(body.get("amount")), "status": "pending"
            }).execute()
        return res.json()

@app.get("/api/cashfree/status/{order_id}")
async def check_status(order_id: str):
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{CASHFREE_BASE_URL}/orders/{order_id}", headers={
            "x-client-id": CASHFREE_APP_ID, "x-client-secret": CASHFREE_SECRET_KEY, "x-api-version": "2023-08-01"
        })
        data = res.json()
        if data.get("order_status") == "PAID":
            await fulfill_order(order_id, data["customer_details"]["customer_id"])
        return data

async def fulfill_order(order_id: str, user_id: str):
    if not supabase: return
    claim_res = supabase.table("payment_claims").select("*").eq("payment_id", order_id).execute()
    if not claim_res.data or claim_res.data[0]["status"] == "success": return
    
    claim = claim_res.data[0]
    pid = claim["plan_id"]
    
    if pid.startswith('a'): # API Plan
        days = 30 if '30' in pid else 15
        limit = 1000 if '1000' in pid else (500 if '500' in pid else None)
        expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        supabase.table("api_keys").insert({
            "api_key": f"tx_{secrets.token_hex(16)}", "user_id": user_id, "user_email": claim["user_email"],
            "plan_name": pid, "expires_at": expiry.isoformat(), "request_limit": limit, "order_id": order_id
        }).execute()
    else: # Credit/Unlimited Plan
        profile = supabase.table("profiles").select("*").eq("id", user_id).single().execute().data
        update = {}
        if pid == 'c10': update["credits"] = (profile.get("credits") or 0) + 10
        # Add other credit logic here...
        supabase.table("profiles").update(update).eq("id", user_id).execute()

    supabase.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 3000)))
