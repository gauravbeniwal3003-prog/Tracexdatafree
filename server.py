from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
from typing import Optional
import json

app = FastAPI()

# Enable CORS for your frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LOOKUP_API_URL = os.getenv("LOOKUP_API_URL", "https://techvishalboss.com/apibuy/public/lookup.php")
LOOKUP_API_KEY = os.getenv("LOOKUP_API_KEY", "TVB_Y9T032")

@app.get("/api/lookup")
async def lookup_number(query: str = Query(...)):
    """
    Proxies the lookup request, cleans the data, and removes branding.
    """
    if not query:
        raise HTTPException(status_code=400, detail="Query parameter is required")

    # Build the target URL
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
                # Handle non-200 responses from upstream
                return {"status": False, "error": f"Upstream error: {response.status_code}"}

            data = response.json()
            
            # 1. Check for "No Record" or API errors
            message = str(data.get("message", "")).lower()
            error_val = str(data.get("error", "")).lower()
            
            if "no record" in message or "no record" in error_val:
                return {"status": False, "results": {}, "error": "No Record Found for this number."}

            results = data.get("results") or data.get("data")
            
            # If the response itself is the result object (flattened)
            if not results and data.get("status") is True:
                results = data

            if not results:
                return {"status": False, "results": {}, "error": "No Record Found."}

            # 2. CLEANING: Remove branding/watermarks and normalize
            cleaned_results = {}
            
            # Standardize records
            items_to_process = results.items() if isinstance(results, dict) else []
            
            for key, val in items_to_process:
                if not isinstance(val, dict):
                    continue
                
                # Skip branding/system fields
                if key.lower() in ["branding", "powered_by", "contact", "timestamp", "status", "success"]:
                    continue
                
                # Normalize field names for the frontend
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

        except httpx.ReadTimeout:
            return {"status": False, "error": "The search engine is responding slowly. Please try again."}
        except Exception as e:
            print(f"Proxy Error: {str(e)}")
            return {"status": False, "error": "Search engine connection failed."}

if __name__ == "__main__":
    import uvicorn
    # Use port 3000 for AI Studio environment compatibility if testing locally here, 
    # but Render will use its own PORT env var.
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
