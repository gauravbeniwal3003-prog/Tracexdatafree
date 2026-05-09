import logging
import httpx
import os
import json
import uuid
import time
import datetime
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from supabase import create_client, Client

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Enable CORS for your website
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, replace with ["https://tracexnumber.web.app"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Credentials from Environment
# ON RENDER: Add these in Dashboard -> Settings -> Environment Variables
LOOKUP_API_URL = os.getenv("LOOKUP_API_URL", "https://techvishalboss.com/apibuy/public/lookup.php")
LOOKUP_API_KEY = os.getenv("LOOKUP_API_KEY", "TVB_Y9T032")

CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
CASHFREE_BASE_URL = "https://api.cashfree.com/pg"

SUPABASE_URL = os.getenv("VITE_SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Initialize Supabase Admin
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        logger.info("Supabase Admin initialized")
    except Exception as e:
        logger.error(f"Supabase init failed: {e}")

@app.get("/")
def health():
    return {
        "status": "healthy", 
        "engine": "TRACEXDATA Python",
        "supabase": supabase is not None,
        "cashfree": CASHFREE_APP_ID is not None
    }

@app.get("/api/lookup")
async def lookup_number(query: str = Query(...)):
    api_url = f"{LOOKUP_API_URL}?key={LOOKUP_API_KEY}&service=number&query={query}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(api_url, headers=headers)
            return resp.json()
        except Exception as e:
            logger.error(f"Lookup failed: {e}")
            return {"status": False, "error": "Search engine busy"}

# --- PAYMENT ROUTES ---

@app.post("/api/cashfree/create-order")
async def create_order(request: Request):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Cashfree not configured")
    
    try:
        body = await request.json()
        user_id = body.get("user_id")
        plan_id = body.get("plan_id")
        amount = body.get("amount")

        if not user_id or not plan_id or not amount:
            raise HTTPException(status_code=400, detail="Missing user_id, plan_id or amount")

        order_id = f"order_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        
        payload = {
            "order_id": order_id,
            "order_amount": float(amount),
            "order_currency": "INR",
            "customer_details": {
                "customer_id": user_id,
                "customer_email": body.get("user_email") or "cust@example.com",
                "customer_phone": body.get("customer_phone") or "9999999999"
            },
            "order_meta": {
                "return_url": body.get("return_url") or f"https://tracexnumber.web.app?order_id={order_id}"
            }
        }

        headers = {
            "x-client-id": CASHFREE_APP_ID,
            "x-client-secret": CASHFREE_SECRET_KEY,
            "x-api-version": "2023-08-01",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(f"{CASHFREE_BASE_URL}/orders", json=payload, headers=headers)
            data = response.json()
            
            if response.status_code == 200:
                # Log pending order in database
                if supabase:
                    try:
                        supabase.table("payment_claims").insert({
                            "payment_id": order_id,
                            "user_id": user_id,
                            "plan_id": plan_id,
                            "amount": float(amount),
                            "status": "pending"
                        }).execute()
                    except Exception as e:
                        logger.error(f"Failed to log order: {e}")
                
                return data
            else:
                logger.error(f"Cashfree API Error: {data}")
                raise HTTPException(status_code=400, detail=data.get("message", "Gateway Error"))
    except Exception as e:
        logger.exception("Order creation failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cashfree/status/{order_id}")
async def check_status(order_id: str):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Gateway credentials missing")

    headers = {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": "2023-08-01"
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{CASHFREE_BASE_URL}/orders/{order_id}", headers=headers)
            data = response.json()
            
            if data.get("order_status") == "PAID":
                user_id = data.get("customer_details", {}).get("customer_id")
                await fulfill_order(order_id, user_id)
                
            return data
        except Exception as e:
            logger.error(f"Status check failed: {e}")
            raise HTTPException(status_code=500, detail="Verification failed")

async def fulfill_order(order_id: str, user_id: str):
    if not supabase: return
    
    try:
        # Check if already fulfilled
        claim_res = supabase.table("payment_claims").select("*").eq("payment_id", order_id).execute()
        if not claim_res.data or claim_res.data[0]["status"] == "success":
            return

        claim = claim_res.data[0]
        plan_id = claim["plan_id"]
        
        profile_res = supabase.table("profiles").select("*").eq("id", user_id).execute()
        if not profile_res.data: return
        profile = profile_res.data[0]
        
        update = {}
        # Credits logic
        if plan_id == 'c10': update["credits"] = (profile.get("credits") or 0) + 10
        elif plan_id == 'c50': update["credits"] = (profile.get("credits") or 0) + 50
        elif plan_id == 'c100': update["credits"] = (profile.get("credits") or 0) + 100
        
        # Unlimited Plans logic
        elif plan_id.startswith('u'):
            hours = {'u1h': 1, 'u1d': 24, 'u1w': 168, 'u1m': 720}.get(plan_id, 0)
            now = datetime.datetime.now(datetime.timezone.utc)
            expiry_str = profile.get("unlimited_expiry")
            
            current_exp = now
            if expiry_str:
                try:
                    current_exp = datetime.datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
                except:
                    pass
            
            start = max(now, current_exp)
            update["unlimited_expiry"] = (start + datetime.timedelta(hours=hours)).isoformat()

        if update:
            supabase.table("profiles").update(update).eq("id", user_id).execute()
            supabase.table("payment_claims").update({"status": "success"}).eq("payment_id", order_id).execute()
            logger.info(f"Fulfilled order {order_id} for user {user_id}")
            
    except Exception as e:
        logger.error(f"Fulfillment failed for {order_id}: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
