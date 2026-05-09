from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import json
import uuid
import time
import datetime
from typing import Optional, Dict, Any
from supabase import create_client, Client

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
LOOKUP_API_URL = os.getenv("LOOKUP_API_URL", "https://techvishalboss.com/apibuy/public/lookup.php")
LOOKUP_API_KEY = os.getenv("LOOKUP_API_KEY", "TVB_Y9T032")

CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID", "12765199c4c89286efc175eec099156721")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "cfsk_ma_prod_1f9abc0880569bd7a4b0ea1c712adb53_ad67e85f")
CASHFREE_BASE_URL = "https://api.cashfree.com/pg"

SUPABASE_URL = os.getenv("VITE_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Initialize Supabase Admin client
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

@app.get("/api/lookup")
async def lookup_number(query: str = Query(...)):
    # ... (existing lookup code remains the same)
    if not query:
        raise HTTPException(status_code=400, detail="Query parameter is required")
    api_url = f"{LOOKUP_API_URL}?key={LOOKUP_API_KEY}&service=number&query={query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://techvishalboss.com/"
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(api_url, headers=headers)
            if response.status_code != 200:
                return {"status": False, "error": f"Upstream error: {response.status_code}"}
            data = response.json()
            message = str(data.get("message", "")).lower()
            error_val = str(data.get("error", "")).lower()
            if "no record" in message or "no record" in error_val:
                return {"status": False, "results": {}, "error": "No Record Found for this number."}
            results = data.get("results") or data.get("data")
            if not results and data.get("status") is True:
                results = data
            if not results:
                return {"status": False, "results": {}, "error": "No Record Found."}
            cleaned_results = {}
            items_to_process = results.items() if isinstance(results, dict) else []
            for key, val in items_to_process:
                if not isinstance(val, dict) or key.lower() in ["branding", "powered_by", "contact", "timestamp", "status", "success"]:
                    continue
                cleaned_results[key] = {
                    "name": val.get("name") or val.get("full_name") or "N/A",
                    "father_name": val.get("father_name") or val.get("fathername") or "N/A",
                    "mobile": val.get("mobile") or val.get("number") or query or "N/A",
                    "alt_mobile": val.get("alt_mobile") or val.get("alt_number") or "N/A",
                    "email": val.get("email") or "N/A",
                    "aadhar_number": val.get("aadhar_number") or val.get("aadhar") or "N/A",
                    "operator": val.get("operator") or val.get("carrier") or "N/A",
                    "state_circle": val.get("state_circle") or val.get("circle") or "N/A",
                    "address": val.get("address") or val.get("location") or "N/A"
                }
            if not cleaned_results:
                 return {"status": False, "results": {}, "error": "No Record Found for this number."}
            return {"status": True, "results": cleaned_results}
        except Exception as e:
            return {"status": False, "error": "Search engine connection failed."}

# --- CASHFREE PAYMENT INTEGRATION ---

@app.post("/api/cashfree/create-order")
async def create_cashfree_order(request: Request):
    if not supabase:
        raise HTTPException(status_code=500, detail="Database connection not configured")
    
    body = await request.json()
    user_id = body.get("user_id")
    user_email = body.get("user_email")
    plan_id = body.get("plan_id")
    amount = body.get("amount")
    
    if not user_id or not plan_id or not amount:
        raise HTTPException(status_code=400, detail="Missing required parameters")

    order_id = f"order_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    async with httpx.AsyncClient() as client:
        cf_payload = {
            "order_id": order_id,
            "order_amount": float(amount),
            "order_currency": "INR",
            "customer_details": {
                "customer_id": user_id,
                "customer_email": user_email or "customer@example.com",
                "customer_phone": "9999999999" # Default or passed from frontend
            },
            "order_meta": {
                "return_url": body.get("return_url", "https://tracexnumber.web.app?order_id={order_id}")
            }
        }
        
        headers = {
            "x-client-id": CASHFREE_APP_ID,
            "x-client-secret": CASHFREE_SECRET_KEY,
            "x-api-version": "2023-08-01",
            "Content-Type": "application/json"
        }

        response = await client.post(f"{CASHFREE_BASE_URL}/orders", json=cf_payload, headers=headers)
        
        if response.status_code != 200:
            error_data = response.json()
            print(f"Cashfree Error: {error_data}")
            raise HTTPException(status_code=400, detail="Failed to create Cashfree order")

        order_data = response.json()
        
        # Log entry in Supabase (optional, but good for tracking)
        supabase.table("payment_claims").insert({
            "payment_id": order_id,
            "user_id": user_id,
            "plan_id": plan_id,
            "amount": float(amount),
            "status": "pending"
        }).execute()

        return order_data

@app.get("/api/cashfree/status/{order_id}")
async def get_payment_status(order_id: str):
    if not supabase:
        raise HTTPException(status_code=500, detail="Database connection not configured")

    async with httpx.AsyncClient() as client:
        headers = {
            "x-client-id": CASHFREE_APP_ID,
            "x-client-secret": CASHFREE_SECRET_KEY,
            "x-api-version": "2023-08-01",
        }
        
        response = await client.get(f"{CASHFREE_BASE_URL}/orders/{order_id}", headers=headers)
        
        if response.status_code != 200:
            raise HTTPException(status_code=404, detail="Order not found")

        data = response.json()
        
        if data.get("order_status") == "PAID":
            await fulfill_order(order_id, data.get("customer_details", {}).get("customer_id"))
            
        return data

async def fulfill_order(order_id: str, user_id: str):
    if not supabase: return

    # Check if already processed
    claim_check = supabase.table("payment_claims").select("*").eq("payment_id", order_id).execute()
    if not claim_check.data or claim_check.data[0]["status"] == "success":
        return

    claim = claim_check.data[0]
    plan_id = claim["plan_id"]
    
    profile_response = supabase.table("profiles").select("*").eq("id", user_id).execute()
    if not profile_response.data: return
    profile = profile_response.data[0]
    
    update_data = {}
    
    # Credit logic
    if plan_id == 'c10': update_data["credits"] = (profile.get("credits") or 0) + 10
    elif plan_id == 'c50': update_data["credits"] = (profile.get("credits") or 0) + 50
    elif plan_id == 'c100': update_data["credits"] = (profile.get("credits") or 0) + 100
    
    # Unlimited logic
    elif plan_id.startswith('u'):
        hours_map = {'u1h': 1, 'u1d': 24, 'u1w': 168, 'u1m': 720}
        hours = hours_map.get(plan_id, 0)
        
        start_time = time.time()
        expiry_str = profile.get("unlimited_expiry")
        if expiry_str:
            current_expiry = datetime.datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
            if current_expiry.timestamp() > start_time:
                start_time = current_expiry.timestamp()
        
        new_expiry = datetime.datetime.fromtimestamp(start_time + (hours * 3600), tz=datetime.timezone.utc)
        update_data["unlimited_expiry"] = new_expiry.isoformat()

    if update_data:
        supabase.table("profiles").update(update_data).eq("id", user_id).execute()
        supabase.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
