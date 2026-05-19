"""
TraceX Lookup Bot - Premium Telecom Lookup Bot
Enhanced Credit System with Supabase + Website Payment Sessions
Version: 5.3 - Stateless Website Payment Link Flow
"""

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import time
import re
from datetime import datetime, timedelta, timezone
import os
import threading
import signal
import sys
import uuid
import hmac
import hashlib
import json
from urllib.parse import urlencode, quote_plus
from flask import Flask, request, jsonify
from supabase import create_client, Client

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7850023357"))
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "-1003743686626"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@gaurav_beniwal_0001")
BOT_VERSION = "5.3"

# Lookup API Configuration
LOOKUP_API_URL = os.getenv("LOOKUP_API_URL", "https://techvishalboss.com/apibuy/public/lookup.php")
LOOKUP_API_KEY = os.getenv("LOOKUP_API_KEY", "TVB_Y9T032")
LOOKUP_API_SERVICE = os.getenv("LOOKUP_API_SERVICE", "number")

COOLDOWN_SECONDS = 3
AUTO_DELETE_SECONDS = 120
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/Gaurav_beni_0001")

# Website Payment Session Configuration
# Your existing website/backend will open and verify this session link.
# IMPORTANT: PAYMENT_BASE_URL is the Render bot backend URL used for /pay and webhook.
# Do NOT use old TELEGRAM_PAYMENT_BASE_URL here because it can force Cashfree to see Render as return domain.
TELEGRAM_PAYMENT_BASE_URL = os.getenv("PAYMENT_BASE_URL", "https://tracexnumber-bot.onrender.com")
CASHFREE_RETURN_BASE_URL = os.getenv("CASHFREE_RETURN_BASE_URL", "https://tracexnumber.web.app")
# Approved website domain. Telegram payment checkout opens here; Render only creates sessions and stores data.
WEBSITE_PAYMENT_BASE_URL = os.getenv("WEBSITE_PAYMENT_BASE_URL", CASHFREE_RETURN_BASE_URL).rstrip("/")
# Stateless Telegram payment links: bot does NOT create rows in telegram_payment_sessions anymore.
# Website must verify this signature server-side using the same TELEGRAM_PAYMENT_LINK_SECRET.
TELEGRAM_PAYMENT_LINK_SECRET = os.getenv("TELEGRAM_PAYMENT_LINK_SECRET") or BOT_TOKEN or "tracex-default-secret"
TELEGRAM_PAY_ROUTE = os.getenv("TELEGRAM_PAY_ROUTE", "telegram-pay")
PAYMENT_SESSION_TTL_MINUTES = int(os.getenv("PAYMENT_SESSION_TTL_MINUTES", "10"))
PAYMENT_SESSION_CLEANUP_DAYS = int(os.getenv("PAYMENT_SESSION_CLEANUP_DAYS", "1"))
# Old redirect model disabled. Direct Cashfree is used.
TELEGRAM_PAYMENT_CHECKOUT_BASE_URL = ""

PLAN_CONFIG = {
    "c10": {"amount": 20, "credits": 10, "unlimited_minutes": 0, "payment_for": "credits", "label": "10 Credits"},
    "c50": {"amount": 70, "credits": 50, "unlimited_minutes": 0, "payment_for": "credits", "label": "50 Credits"},
    "c100": {"amount": 100, "credits": 100, "unlimited_minutes": 0, "payment_for": "credits", "label": "100 Credits"},
    "u1h": {"amount": 9, "credits": 0, "unlimited_minutes": 60, "payment_for": "unlimited", "label": "1 Hour Unlimited"},
    "u1d": {"amount": 29, "credits": 0, "unlimited_minutes": 1440, "payment_for": "unlimited", "label": "1 Day Unlimited"},
    "u1w": {"amount": 149, "credits": 0, "unlimited_minutes": 10080, "payment_for": "unlimited", "label": "7 Days Unlimited"},
    "u1m": {"amount": 399, "credits": 0, "unlimited_minutes": 43200, "payment_for": "unlimited", "label": "30 Days Unlimited"},
    "protect49": {"amount": 49, "credits": 0, "unlimited_minutes": 0, "payment_for": "protect_number", "label": "Protect Number"},
}

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Cashfree Configuration
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
CASHFREE_WEBHOOK_SECRET = os.getenv("CASHFREE_WEBHOOK_SECRET")
CASHFREE_ENV = os.getenv("CASHFREE_ENV", "TEST")
RENDER_BASE_URL = os.getenv("RENDER_BASE_URL", "https://your-app.onrender.com")

# Cashfree API URLs - will be set dynamically
CASHFREE_API_BASE = None
CASHFREE_API_VERSION = os.getenv("CASHFREE_API_VERSION", "2023-08-01")

# Initialize Bot
bot = telebot.TeleBot(BOT_TOKEN)

# Initialize Supabase Client
# Backend writes need service role key because RLS can block inserts/updates from anon key.
SUPABASE_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
if not SUPABASE_SERVICE_ROLE_KEY:
    print("⚠️ SUPABASE_SERVICE_ROLE_KEY missing, RLS may block backend writes")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# State Management
user_states = {}
user_cooldown = {}
temp_data = {}
MAINTENANCE_MODE = False

# ==================== MAINTENANCE MODE ====================
MAINTENANCE_MESSAGE = """
⚠️ BOT UNDER MAINTENANCE
🛠️ TraceX is currently under maintenance and upgrades.
⏰ Please try again later.
📢 Till then, join our official channel for updates and announcements: https://t.me/Gaurav_beni_0001
🙏 Thanks for your patience.
"""

@bot.message_handler(func=lambda message: MAINTENANCE_MODE and message.from_user.id != ADMIN_ID)
def maintenance_handler(message):
    bot.reply_to(message, MAINTENANCE_MESSAGE, parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: MAINTENANCE_MODE and call.from_user.id != ADMIN_ID)
def maintenance_callback(call):
    bot.answer_callback_query(call.id, "Bot under maintenance 🛠️", show_alert=True)

# ==================== UI COMPONENTS ====================
def universal_markup(back=False, buy=False, join=False, admin=False):
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    if buy:
        buttons.append(InlineKeyboardButton("💎 BUY CREDITS", callback_data="buy"))
    if join:
        buttons.append(InlineKeyboardButton("📢 JOIN GROUP", url=GROUP_LINK))
    if admin:
        buttons.append(InlineKeyboardButton("👨‍💻 CONTACT ADMIN", url="https://t.me/gaurav_beniwal_0001"))
    if back:
        buttons.append(InlineKeyboardButton("🏠 MAIN MENU", callback_data="main_menu"))
    for btn in buttons:
        markup.add(btn)
    return markup

def footer():
    return f"\n\n━━━━━━━━━━━━━━━━\n👨‍💻 Admin: {ADMIN_USERNAME}\n👥 Group: [Join Community]({GROUP_LINK})"

def header(title, emoji="🚀"):
    return f"{emoji} *{title}*\n━━━━━━━━━━━━━━━━\n"

def main_menu_markup(current_user_id=None):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📱 NUMBER LOOKUP", callback_data="lookup"),
        InlineKeyboardButton("💎 MY CREDITS", callback_data="credits")
    )
    markup.add(
        InlineKeyboardButton("🛒 BUY CREDITS", callback_data="buy"),
        InlineKeyboardButton("🛡️ PROTECT NUMBER", callback_data="protect")
    )
    markup.add(
        InlineKeyboardButton("👤 PROFILE", callback_data="profile"),
        InlineKeyboardButton("ℹ️ HELP", callback_data="help")
    )
    if current_user_id == ADMIN_ID:
        markup.add(InlineKeyboardButton("🛠 ADMIN", callback_data="admin"))
    return markup

def cancel_button():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ CANCEL", callback_data="cancel"))
    return markup

def credit_packs_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("💰 10 Credits - ₹20", callback_data="plan_c10"),
        InlineKeyboardButton("💰 50 Credits - ₹70", callback_data="plan_c50"),
        InlineKeyboardButton("💰 100 Credits - ₹100", callback_data="plan_c100")
    )
    markup.add(
        InlineKeyboardButton("🚀 1 Hour Unlimited - ₹9", callback_data="plan_u1h"),
        InlineKeyboardButton("🚀 1 Day Unlimited - ₹29", callback_data="plan_u1d")
    )
    markup.add(
        InlineKeyboardButton("🚀 7 Days Unlimited - ₹149", callback_data="plan_u1w"),
        InlineKeyboardButton("🚀 30 Days Unlimited - ₹399", callback_data="plan_u1m")
    )
    markup.add(
        InlineKeyboardButton("🛡️ Protect Number - ₹49", callback_data="plan_protect49")
    )
    markup.add(InlineKeyboardButton("🔙 BACK", callback_data="main_menu"))
    return markup

# ==================== SUPABASE FUNCTIONS ====================
def get_user(telegram_user_id):
    try:
        response = supabase.table("telegram_users").select("*").eq("telegram_user_id", telegram_user_id).execute()
        if response.data and len(response.data) > 0:
            user = response.data[0]
            supabase.table("telegram_users").update({
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telegram_user_id", telegram_user_id).execute()
            return user
        else:
            new_user = {
                "telegram_user_id": telegram_user_id,
                "credits": 3,
                "total_searches": 0,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "is_banned": False
            }
            result = supabase.table("telegram_users").insert(new_user).execute()
            if result.data and len(result.data) > 0:
                return result.data[0]
            return None
    except Exception as e:
        print(f"Supabase get_user error: {e}")
        return None

def add_credits(telegram_user_id, amount):
    """Add credits to user and return new total. Creates user if not exists."""
    try:
        # First check if user exists
        response = supabase.table("telegram_users").select("credits, telegram_user_id, telegram_name").eq("telegram_user_id", telegram_user_id).execute()
        
        if not response.data or len(response.data) == 0:
            print(f"User {telegram_user_id} not found, creating new user...")
            new_user = {
                "telegram_user_id": telegram_user_id,
                "credits": amount,
                "total_searches": 0,
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "is_banned": False
            }
            result = supabase.table("telegram_users").insert(new_user).execute()
            if result.data and len(result.data) > 0:
                final_credits = result.data[0].get('credits', amount)
                print(f"New user created with {final_credits} credits")
                return final_credits
            else:
                print(f"Failed to create user: {result}")
                return 0
        else:
            current_credits = response.data[0].get('credits', 0)
            new_credits = current_credits + amount
            
            print(f"Updating user {telegram_user_id}: {current_credits} -> {new_credits} (+{amount})")
            
            update_result = supabase.table("telegram_users").update({
                "credits": new_credits,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telegram_user_id", telegram_user_id).execute()
            
            if update_result.data and len(update_result.data) > 0:
                final_credits = update_result.data[0].get('credits', new_credits)
                print(f"Credits updated successfully: {final_credits}")
                return final_credits
            else:
                verify_response = supabase.table("telegram_users").select("credits").eq("telegram_user_id", telegram_user_id).execute()
                if verify_response.data and len(verify_response.data) > 0:
                    final_credits = verify_response.data[0].get('credits', 0)
                    print(f"Verified credits after update: {final_credits}")
                    return final_credits
                else:
                    print(f"Update may have failed - could not verify")
                    return new_credits
                    
    except Exception as e:
        print(f"Add credits error for user {telegram_user_id}: {e}")
        import traceback
        traceback.print_exc()
        return 0

def update_user_credits(telegram_user_id, new_credits):
    """Update user credits and return updated credits. Returns None on failure."""
    try:
        print(f"Updating user {telegram_user_id} credits to {new_credits}")
        
        update_result = supabase.table("telegram_users").update({
            "credits": new_credits,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telegram_user_id", telegram_user_id).execute()
        
        if update_result.data and len(update_result.data) > 0:
            updated_credits = update_result.data[0].get('credits', new_credits)
            print(f"Update successful. New credits: {updated_credits}")
            return updated_credits
        else:
            verify = supabase.table("telegram_users").select("credits").eq("telegram_user_id", telegram_user_id).execute()
            if verify.data and len(verify.data) > 0:
                verified_credits = verify.data[0].get('credits', 0)
                print(f"Verified credits after update: {verified_credits}")
                return verified_credits
            else:
                print(f"Update failed - user {telegram_user_id} not found")
                return None
                
    except Exception as e:
        print(f"Update user credits error: {e}")
        import traceback
        traceback.print_exc()
        return None

def deduct_credit(telegram_user_id):
    try:
        user = get_user(telegram_user_id)
        if not user:
            return False
        
        unlimited_expiry = user.get('unlimited_expiry')
        if unlimited_expiry:
            if isinstance(unlimited_expiry, str):
                expiry_date = datetime.fromisoformat(unlimited_expiry.replace('Z', '+00:00'))
            else:
                expiry_date = unlimited_expiry
            if expiry_date > datetime.now(timezone.utc):
                return True
        
        credits = user.get('credits', 0)
        if credits > 0:
            supabase.table("telegram_users").update({
                "credits": credits - 1,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telegram_user_id", telegram_user_id).execute()
            return True
        return False
    except Exception as e:
        print(f"Deduct credit error: {e}")
        return False

def increment_total_searches(telegram_user_id):
    try:
        user = get_user(telegram_user_id)
        if user:
            new_total = user.get('total_searches', 0) + 1
            supabase.table("telegram_users").update({
                "total_searches": new_total,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telegram_user_id", telegram_user_id).execute()
            return True
    except Exception as e:
        print(f"Increment searches error: {e}")
    return False

def get_total_credits(telegram_user_id):
    try:
        user = get_user(telegram_user_id)
        return user.get('credits', 0) if user else 0
    except Exception as e:
        print(f"Get total credits error: {e}")
        return 0

def is_number_protected(phone_number):
    try:
        response = supabase.table("protected_numbers").select("*").eq("phone_number", phone_number).execute()
        return len(response.data) > 0
    except Exception as e:
        print(f"Check protected number error: {e}")
        return False

def add_protected_number(phone_number, telegram_user_id):
    try:
        supabase.table("protected_numbers").insert({
            "phone_number": phone_number,
            "telegram_user_id": telegram_user_id,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        return True
    except Exception as e:
        print(f"Add protected number error: {e}")
        return False

def get_cached_result(phone_number):
    try:
        response = supabase.table("search_results").select("*").eq("mobile_number", phone_number).execute()
        if response.data and len(response.data) > 0:
            return response.data[0].get('raw_data')
        return None
    except Exception as e:
        print(f"Get cached result error: {e}")
        return None

def save_cached_result(phone_number, raw_data):
    """Save lookup result to cache without requiring optional columns."""
    try:
        existing = supabase.table("search_results").select("id,mobile_number").eq("mobile_number", phone_number).limit(1).execute()
        if existing.data and len(existing.data) > 0:
            row_id = existing.data[0].get("id")
            # Do not use updated_at here because the current table may not have that column.
            supabase.table("search_results").update({
                "raw_data": raw_data
            }).eq("id", row_id).execute()
        else:
            supabase.table("search_results").insert({
                "mobile_number": phone_number,
                "raw_data": raw_data,
                "created_at": datetime.now(timezone.utc).isoformat()
            }).execute()
        return True
    except Exception as e:
        print(f"Save cached result error: {e}")
        return False

def ban_user(telegram_user_id):
    try:
        supabase.table("telegram_users").update({
            "is_banned": True,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telegram_user_id", telegram_user_id).execute()
        return True
    except Exception as e:
        print(f"Ban user error: {e}")
        return False

def unban_user(telegram_user_id):
    try:
        supabase.table("telegram_users").update({
            "is_banned": False,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telegram_user_id", telegram_user_id).execute()
        return True
    except Exception as e:
        print(f"Unban user error: {e}")
        return False

def get_stats():
    try:
        users_resp = supabase.table("telegram_users").select("*", count="exact").execute()
        total_users = users_resp.count
        
        searches_resp = supabase.table("telegram_users").select("total_searches").execute()
        total_searches = sum(u.get('total_searches', 0) for u in searches_resp.data)
        
        credits_resp = supabase.table("telegram_users").select("credits").execute()
        total_credits = sum(u.get('credits', 0) for u in credits_resp.data)
        
        banned_resp = supabase.table("telegram_users").select("*", count="exact").eq("is_banned", True).execute()
        banned_users = banned_resp.count
        
        pending_resp = supabase.table("payment_claims").select("*", count="exact").eq("status", "pending").execute()
        pending_payments_count = pending_resp.count
        
        revenue_resp = supabase.table("payment_claims").select("amount").eq("status", "success").execute()
        total_revenue = sum(p.get('amount', 0) for p in revenue_resp.data)
        
        cache_resp = supabase.table("search_results").select("*", count="exact").execute()
        cache_size = cache_resp.count
        
        protected_resp = supabase.table("protected_numbers").select("*", count="exact").execute()
        protected_count = protected_resp.count
        
        return {
            'total_users': total_users,
            'total_searches': total_searches,
            'total_credits': total_credits,
            'banned_users': banned_users,
            'pending_payments': pending_payments_count,
            'total_revenue': total_revenue,
            'cache_size': cache_size,
            'protected_count': protected_count
        }
    except Exception as e:
        print(f"Get stats error: {e}")
        return {
            'total_users': 0,
            'total_searches': 0,
            'total_credits': 0,
            'banned_users': 0,
            'pending_payments': 0,
            'total_revenue': 0,
            'cache_size': 0,
            'protected_count': 0
        }

def get_all_users():
    try:
        response = supabase.table("telegram_users").select("telegram_user_id").eq("is_banned", False).execute()
        return [row['telegram_user_id'] for row in response.data]
    except Exception as e:
        print(f"Get all users error: {e}")
        return []

def add_giveaway_credits(credits):
    try:
        users = get_all_users()
        success_count = 0
        for user_id in users:
            add_credits(user_id, credits)
            success_count += 1
        return success_count, 0
    except Exception as e:
        print(f"Giveaway error: {e}")
        return 0, 0

def get_recent_transactions(limit=20):
    try:
        response = supabase.table("payment_claims").select("*").order("created_at", desc=True).limit(limit).execute()
        return response.data
    except Exception as e:
        print(f"Get transactions error: {e}")
        return []

# ==================== CASHFREE PAYMENT FUNCTIONS ====================
def verify_cashfree_signature(raw_body, signature):
    """Verify Cashfree webhook signature using webhook secret"""
    if not CASHFREE_WEBHOOK_SECRET:
        print("CASHFREE_WEBHOOK_SECRET not set")
        return False
    
    if not signature:
        print("No signature provided in webhook")
        return False
    
    try:
        expected_signature = hmac.new(
            CASHFREE_WEBHOOK_SECRET.encode('utf-8'),
            raw_body.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected_signature.lower(), signature.lower())
    except Exception as e:
        print(f"Signature verification error: {e}")
        return False

def get_cashfree_order_status(order_id):
    """Get order status from Cashfree API"""
    try:
        # Determine API URL based on environment
        env_upper = CASHFREE_ENV.upper()
        if env_upper in ["PROD", "PRODUCTION"]:
            api_base = "https://api.cashfree.com/pg"
        else:
            api_base = "https://sandbox.cashfree.com/pg"
        
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-client-id": CASHFREE_APP_ID.strip() if CASHFREE_APP_ID else "",
            "x-client-secret": CASHFREE_SECRET_KEY.strip() if CASHFREE_SECRET_KEY else "",
            "x-api-version": CASHFREE_API_VERSION
        }
        
        response = requests.get(f"{api_base}/orders/{order_id}", headers=headers, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Cashfree status API error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Get order status error: {e}")
        return None



# Success states used by website + bot + fallback verification.
SUCCESS_STATUSES = {"success", "paid", "completed", "complete", "payment_success", "captured"}


def normalize_status(value):
    return str(value or "pending").strip().lower()


def get_session_order_id(session):
    """Website/backend may store Cashfree order id in different columns."""
    if not isinstance(session, dict):
        return None
    for key in [
        "gateway_order_id",
        "cashfree_order_id",
        "order_id",
        "cf_order_id",
        "payment_order_id",
    ]:
        value = session.get(key)
        if value:
            return str(value)

    raw = session.get("raw_response")
    if isinstance(raw, dict):
        for key in ["order_id", "cf_order_id", "cashfree_order_id"]:
            value = raw.get(key)
            if value:
                return str(value)
        data = raw.get("data")
        if isinstance(data, dict):
            for key in ["order_id", "cf_order_id", "cashfree_order_id"]:
                value = data.get(key)
                if value:
                    return str(value)
    return None


def get_session_by_order_id(order_id):
    """Find Telegram payment session by any known order id column."""
    if not order_id:
        return None
    for column in ["gateway_order_id", "cashfree_order_id", "order_id", "cf_order_id", "payment_order_id"]:
        try:
            resp = supabase.table("telegram_payment_sessions").select("*").eq(column, order_id).limit(1).execute()
            if resp.data:
                return resp.data[0]
        except Exception as e:
            # Some columns may not exist in user's table; safely skip them.
            print(f"Session lookup skipped column {column}: {e}")
    return None

def cashfree_api_base():
    env_upper = CASHFREE_ENV.upper()
    if env_upper in ["PROD", "PRODUCTION", "LIVE"]:
        return "https://api.cashfree.com/pg", "production"
    return "https://sandbox.cashfree.com/pg", "sandbox"


def create_cashfree_order_for_session(session):
    """Create or reuse a Cashfree PG order for a Telegram payment session and return payment_session_id."""
    try:
        if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
            print("❌ Cashfree credentials missing: set CASHFREE_APP_ID and CASHFREE_SECRET_KEY")
            return None, None

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            print("❌ session_id missing in telegram_payment_sessions row")
            return None, None

        existing_order_id = session.get("gateway_order_id")
        if existing_order_id:
            print(f"♻️ Reusing existing Cashfree order: {existing_order_id}")
            order_data = get_cashfree_order_status(existing_order_id)
            if order_data:
                psid = order_data.get("payment_session_id")
                if psid:
                    return existing_order_id, psid
                print("Existing order has no payment_session_id, creating fresh order")

        api_base, _mode = cashfree_api_base()
        order_id = f"TG{session_id}"[:45]  # safe length for Cashfree
        amount = float(session.get("amount") or 0)
        telegram_user_id = str(session.get("telegram_user_id") or "0")
        username = str(session.get("telegram_username") or "Telegram User")[:80]

        if amount <= 0:
            print(f"❌ Invalid amount for session {session_id}: {amount}")
            return None, None

        base_url = TELEGRAM_PAYMENT_BASE_URL.rstrip("/")
        return_base_url = CASHFREE_RETURN_BASE_URL.rstrip("/")
        # Cashfree checkout should return to approved website domain, while webhook stays on Render.
        return_url = f"{return_base_url}/payment-success?session_id={session_id}&order_id={{order_id}}"
        notify_url = f"{base_url}/cashfree/webhook"

        # Cashfree PG Orders API payload. Keep it minimal and valid for PROD.
        payload = {
            "order_id": order_id,
            "order_amount": amount,
            "order_currency": "INR",
            "customer_details": {
                "customer_id": f"tg_{telegram_user_id}",
                "customer_name": username,
                "customer_email": f"tg{telegram_user_id}@tracex.local",
                "customer_phone": os.getenv("CASHFREE_DEFAULT_PHONE", "9999999999")
            },
            "order_meta": {
                "return_url": return_url,
                "notify_url": notify_url
            },
            "order_note": f"TraceX Telegram {session.get('plan_id')} {session_id}"
        }

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-client-id": CASHFREE_APP_ID.strip(),
            "x-client-secret": CASHFREE_SECRET_KEY.strip(),
            "x-api-version": CASHFREE_API_VERSION
        }

        print("🔥 Calling Cashfree Create Order")
        print("Cashfree Env:", CASHFREE_ENV)
        print("Cashfree API Base:", api_base)
        print("Cashfree API Version:", CASHFREE_API_VERSION)
        print("Cashfree return_url:", return_url)
        print("Cashfree notify_url:", notify_url)
        print("Cashfree Payload:", json.dumps(payload, ensure_ascii=False))

        response = requests.post(f"{api_base}/orders", headers=headers, json=payload, timeout=30)
        print("Cashfree Response Status:", response.status_code)
        print("Cashfree Response Body:", response.text[:5000])

        try:
            resp_json = response.json()
        except Exception:
            resp_json = {"raw_text": response.text[:5000]}

        if response.status_code not in [200, 201]:
            try:
                supabase.table("telegram_payment_sessions").update({
                    "raw_response": {"cashfree_error": resp_json, "status_code": response.status_code},
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("session_id", session_id).execute()
            except Exception as db_e:
                print("Cashfree error save failed:", db_e)
            return None, None

        payment_session_id = resp_json.get("payment_session_id")
        if not payment_session_id:
            print("❌ payment_session_id missing in Cashfree response")
            try:
                supabase.table("telegram_payment_sessions").update({
                    "raw_response": {"cashfree_missing_payment_session_id": resp_json},
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("session_id", session_id).execute()
            except Exception:
                pass
            return None, None

        supabase.table("telegram_payment_sessions").update({
            "gateway_order_id": order_id,
            "status": "processing",
            "raw_response": resp_json,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("session_id", session_id).execute()

        print(f"✅ Cashfree order created: {order_id}")
        return order_id, payment_session_id
    except Exception as e:
        print(f"Create Cashfree order for session error: {e}")
        import traceback
        traceback.print_exc()
        return None, None

def process_telegram_session_success(order_id, payment_id=None, raw_response=None):
    """Add credits/unlimited/protection for telegram_payment_sessions once payment is PAID.
    Works even if website stored order id as cashfree_order_id instead of gateway_order_id.
    """
    try:
        session = get_session_by_order_id(order_id)
        if not session:
            print(f"No telegram payment session found for order {order_id}")
            return False

        session_id = session.get("session_id")
        current_status = normalize_status(session.get("status"))
        if current_status in SUCCESS_STATUSES:
            print(f"Telegram session already processed for order {order_id} / session {session_id}")
            return True

        telegram_user_id = session.get("telegram_user_id")
        plan_id = session.get("plan_id")
        payment_for = session.get("payment_for")
        credits = int(session.get("credits") or 0)
        unlimited_minutes = int(session.get("unlimited_minutes") or 0)
        protected_number = session.get("protected_number")

        if not telegram_user_id:
            print(f"Session {session_id} has no telegram_user_id")
            return False

        if payment_for == "credits" and credits > 0:
            new_total = add_credits(telegram_user_id, credits)
            try:
                bot.send_message(
                    telegram_user_id,
                    f"✅ *Payment Success!*\n\n💎 `{credits}` credits added.\n📊 Total: `{new_total}`\n\nUse /start to continue.",
                    parse_mode="Markdown"
                )
            except Exception as msg_e:
                print(f"User success message failed: {msg_e}")

        elif payment_for == "unlimited" and unlimited_minutes > 0:
            user = get_user(telegram_user_id)
            now = datetime.now(timezone.utc)
            current_expiry = user.get("unlimited_expiry") if user else None
            start_from = now
            if current_expiry:
                try:
                    expiry_date = datetime.fromisoformat(str(current_expiry).replace('Z', '+00:00'))
                    if expiry_date > now:
                        start_from = expiry_date
                except Exception:
                    pass
            new_expiry = start_from + timedelta(minutes=unlimited_minutes)
            supabase.table("telegram_users").update({
                "unlimited_expiry": new_expiry.isoformat(),
                "updated_at": now.isoformat()
            }).eq("telegram_user_id", telegram_user_id).execute()
            try:
                bot.send_message(
                    telegram_user_id,
                    f"✅ *Payment Success!*\n\n🚀 Unlimited plan activated.\n⏰ Expires: `{new_expiry.strftime('%Y-%m-%d %H:%M:%S')} UTC`\n\nUse /start to continue.",
                    parse_mode="Markdown"
                )
            except Exception as msg_e:
                print(f"User success message failed: {msg_e}")

        elif payment_for == "protect_number" and protected_number:
            if not is_number_protected(protected_number):
                add_protected_number(protected_number, telegram_user_id)
            try:
                bot.send_message(
                    telegram_user_id,
                    f"✅ *Payment Success!*\n\n🛡️ Number protected: `{protected_number}`\n\nUse /start to continue.",
                    parse_mode="Markdown"
                )
            except Exception as msg_e:
                print(f"User success message failed: {msg_e}")

        update_payload = {
            "status": "success",
            "payment_id": payment_id,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        if raw_response is not None:
            update_payload["raw_response"] = raw_response
        # Update by session_id so it works regardless of order id column names.
        supabase.table("telegram_payment_sessions").update(update_payload).eq("session_id", session_id).execute()

        try:
            bot.send_message(
                ADMIN_CHANNEL_ID,
                f"✅ *TELEGRAM PAYMENT SUCCESS*\n━━━━━━━━━━━━━━━━\n👤 User: `{telegram_user_id}`\n📦 Plan: `{plan_id}`\n💰 Amount: ₹{session.get('amount')}\n🆔 Session: `{session_id}`\n🧾 Order: `{order_id}`",
                parse_mode="Markdown"
            )
        except Exception as log_e:
            print("Admin payment success log failed:", log_e)

        return True
    except Exception as e:
        print(f"Process telegram session success error: {e}")
        import traceback
        traceback.print_exc()
        return False

def sign_telegram_payment_params(params):
    """Create short HMAC signature for stateless Telegram payment URL."""
    try:
        clean = {str(k): str(v) for k, v in params.items() if k != "sig" and v is not None}
        canonical = "&".join(f"{k}={clean[k]}" for k in sorted(clean.keys()))
        return hmac.new(
            TELEGRAM_PAYMENT_LINK_SECRET.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()[:32]
    except Exception as e:
        print(f"Payment link signing error: {e}")
        return ""


def verify_telegram_payment_params(params):
    """Optional local verifier for /pay compatibility route."""
    try:
        received = str(params.get("sig") or "")
        expected = sign_telegram_payment_params(params)
        if not received or not hmac.compare_digest(received, expected):
            return False
        exp = int(params.get("exp") or 0)
        return exp > int(datetime.now(timezone.utc).timestamp())
    except Exception:
        return False


def generate_payment_session_code():
    """Generate unique short session code for Telegram payment links."""
    return "TG" + uuid.uuid4().hex[:18].upper()

def cleanup_old_payment_sessions():
    """Delete temporary Telegram payment sessions older than configured cleanup days."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=PAYMENT_SESSION_CLEANUP_DAYS)
        result = supabase.table("telegram_payment_sessions").delete().lt("created_at", cutoff.isoformat()).execute()
        print(f"🧹 Old telegram payment sessions cleanup completed before {cutoff.isoformat()}")
        return True
    except Exception as e:
        print(f"Payment session cleanup warning: {e}")
        return False

def create_telegram_payment_session(plan_id, telegram_user_id, telegram_username, protected_number=None):
    """
    Create a stateless signed website payment link.
    IMPORTANT:
    - No row is inserted into telegram_payment_sessions.
    - Link itself carries tx/user/plan/amount/expiry/protected_number + HMAC signature.
    - Website backend must verify signature and then create Cashfree order + update telegram_users after success.
    """
    try:
        plan = PLAN_CONFIG.get(plan_id)
        if not plan:
            print(f"Invalid plan_id: {plan_id}")
            return None, None, None

        if plan_id == "protect49" and not protected_number:
            print("protected_number is required for protect49")
            return None, None, None

        tx_code = generate_payment_session_code().replace("TG", "TX", 1)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=PAYMENT_SESSION_TTL_MINUTES)
        exp_unix = int(expires_at.timestamp())
        safe_username = (telegram_username or "no_username").replace("@", "")[:60]

        params = {
            "source": "telegram_bot",
            "tx": tx_code,
            "tg_id": str(telegram_user_id),
            "username": safe_username,
            "plan": plan_id,
            "amount": str(plan["amount"]),
            "credits": str(plan["credits"]),
            "unlimited_minutes": str(plan["unlimited_minutes"]),
            "payment_for": plan["payment_for"],
            "exp": str(exp_unix),
        }
        if protected_number:
            params["protected_number"] = str(protected_number)

        params["sig"] = sign_telegram_payment_params(params)
        payment_url = f"{WEBSITE_PAYMENT_BASE_URL}/{TELEGRAM_PAY_ROUTE}?{urlencode(params)}"

        print(
            f"✅ Stateless Telegram payment link created: tx={tx_code} "
            f"user={telegram_user_id} plan={plan_id} url={payment_url} expires={expires_at.isoformat()}"
        )
        return tx_code, payment_url, expires_at

    except Exception as e:
        print(f"Create stateless telegram payment link error: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None

# Backward-compatible alias: no direct Cashfree from bot anymore.
def create_cashfree_order(plan_id, amount, telegram_user_id, telegram_username, payment_for=None, protected_number=None):
    return create_telegram_payment_session(plan_id, telegram_user_id, telegram_username, protected_number)

def process_payment_success(order_id, cashfree_payment_id, raw_response):
    """Process successful payment with idempotency check"""
    try:
        # Get payment claim - check if already processed
        response = supabase.table("payment_claims").select("*").eq("cashfree_order_id", order_id).execute()
        
        if not response.data:
            print(f"Payment claim not found for order: {order_id}")
            return False
        
        claim = response.data[0]
        
        # Idempotency check - if already success, return
        if claim.get('status') == "success":
            print(f"Payment {order_id} already processed")
            return True
        
        telegram_user_id = claim['telegram_user_id']
        plan_id = claim['plan_id']
        payment_for = claim.get('payment_for')
        
        # Update payment claim
        supabase.table("payment_claims").update({
            "status": "success",
            "cashfree_payment_id": cashfree_payment_id,
            "raw_response": raw_response,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("cashfree_order_id", order_id).execute()
        
        # Process based on plan
        if plan_id in ["c10", "c50", "c100"]:
            credits_to_add = claim.get('credits', 0)
            if credits_to_add > 0:
                add_credits(telegram_user_id, credits_to_add)
                bot.send_message(telegram_user_id, f"""
✅ *PAYMENT SUCCESSFUL!*

💎 `{credits_to_add}` credits added to your account!

📊 *Total Credits:* `{get_total_credits(telegram_user_id)}`

Use /start to continue!
""", parse_mode='Markdown')
        
        elif plan_id in ["u1h", "u1d", "u1w", "u1m"]:
            durations = {"u1h": 1/24, "u1d": 1, "u1w": 7, "u1m": 30}
            days = durations.get(plan_id, 0)
            
            user = get_user(telegram_user_id)
            current_expiry = user.get('unlimited_expiry')
            
            if current_expiry:
                if isinstance(current_expiry, str):
                    expiry_date = datetime.fromisoformat(current_expiry.replace('Z', '+00:00'))
                else:
                    expiry_date = current_expiry
                if expiry_date > datetime.now(timezone.utc):
                    new_expiry = expiry_date + timedelta(days=days)
                else:
                    new_expiry = datetime.now(timezone.utc) + timedelta(days=days)
            else:
                new_expiry = datetime.now(timezone.utc) + timedelta(days=days)
            
            supabase.table("telegram_users").update({
                "unlimited_expiry": new_expiry.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("telegram_user_id", telegram_user_id).execute()
            
            duration_text = {"u1h": "1 Hour", "u1d": "1 Day", "u1w": "7 Days", "u1m": "30 Days"}.get(plan_id, "Limited")
            
            bot.send_message(telegram_user_id, f"""
✅ *PAYMENT SUCCESSFUL!*

🚀 *Unlimited Plan Activated!*
📅 Duration: `{duration_text}`
⏰ Expires: `{new_expiry.strftime('%Y-%m-%d %H:%M:%S')}`

Use /start to continue!
""", parse_mode='Markdown')
        
        elif plan_id == "protect49" and payment_for == "protect_number":
            protected_number = claim.get('protected_number')
            if protected_number:
                add_protected_number(protected_number, telegram_user_id)
                bot.send_message(telegram_user_id, f"""
✅ *PAYMENT SUCCESSFUL!*

🛡️ *Number Protected!*
📱 Number: `{protected_number}`

Your number is now protected. No one can lookup details for this number!

Use /start to continue!
""", parse_mode='Markdown')
        
        # Send admin log
        bot.send_message(ADMIN_ID, f"""
✅ *PAYMENT SUCCESS*

👤 User: `{telegram_user_id}`
💎 Plan: `{plan_id}`
💰 Amount: ₹{claim['amount']}
🆔 Order: `{order_id}`

Status: COMPLETED
""", parse_mode='Markdown')
        
        return True
    except Exception as e:
        print(f"Process payment success error: {e}")
        return False

# ==================== RESULT FORMATTING ====================
def md_escape_value(value):
    """Keep values readable in Telegram Markdown without hiding API info."""
    if value is None:
        return "N/A"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = str(value)
    if value.lower() in ["n/a", "na", "none", "null", ""]:
        return "N/A"
    return value.replace("`", "'")


def extract_result_rows(result):
    """Supports API format: results -> Result 1..16+ and direct Result keys."""
    if not isinstance(result, dict):
        return []
    parsed_results = []
    api_results = result.get('results')

    def natural_key(item):
        m = re.search(r'\d+', str(item[0]))
        return int(m.group()) if m else 9999

    if isinstance(api_results, dict):
        for _, value in sorted(api_results.items(), key=natural_key):
            if isinstance(value, dict):
                parsed_results.append(value)
            elif isinstance(value, list):
                parsed_results.extend([v for v in value if isinstance(v, dict)])
    elif isinstance(api_results, list):
        parsed_results.extend([v for v in api_results if isinstance(v, dict)])

    if not parsed_results:
        direct_items = [(k, v) for k, v in result.items() if str(k).lower().startswith('result') and isinstance(v, dict)]
        for _, value in sorted(direct_items, key=natural_key):
            parsed_results.append(value)

    if not parsed_results and ('name' in result or 'mobile' in result or 'phone' in result):
        parsed_results = [result]
    return parsed_results


def format_lookup_result(result, phone, user_id, unlimited_active=False, unlimited_expiry=None):
    """Short attractive output. Shows all API fields inside result rows."""
    if not isinstance(result, dict):
        result = {}
    parsed_results = extract_result_rows(result)
    first_name = "Unknown"

    output = f"""
🔍 *TraceX Result*
━━━━━━━━━━━━━━
📱 Number: `{phone}`
📊 Records: `{len(parsed_results)}`
"""
    meta_lines = []
    for meta_key in ["Powered_by", "Contact", "Timestamp", "message"]:
        if meta_key in result:
            meta_lines.append(f"• {meta_key}: `{md_escape_value(result.get(meta_key))}`")
    if meta_lines:
        output += "\n🧾 *API Info*\n" + "\n".join(meta_lines) + "\n"

    known_order = [
        ("mobile", "📱 Mobile"),
        ("alt_mobile", "📞 Alternate"),
        ("name", "👤 Name"),
        ("father_name", "👨 Father"),
        ("email", "📧 Email"),
        ("aadhar_number", "🪪 Aadhaar"),
        ("operator", "📡 Operator"),
        ("state_circle", "📍 Circle"),
        ("address", "🏠 Address"),
    ]
    aliases = {
        "mobile": ["mobile", "Mobile", "phone", "number"],
        "alt_mobile": ["alt_mobile", "alternate_mobile", "Alt_Mobile", "alternate"],
        "name": ["name", "Name", "full_name"],
        "father_name": ["father_name", "Father_Name", "father", "fatherName"],
        "email": ["email", "Email"],
        "aadhar_number": ["aadhar_number", "aadhar", "Aadhar", "aadhaar", "Aadhaar"],
        "operator": ["operator", "Operator", "carrier"],
        "state_circle": ["state_circle", "circle", "Circle", "state"],
        "address": ["address", "Address", "full_address"],
    }

    for idx, data in enumerate(parsed_results, 1):
        output += f"\n━━━━━━━━━━━━━━\n📄 *Result {idx}*\n"
        displayed_keys = set()
        for canonical, label in known_order:
            value = None
            used_key = None
            for key in aliases.get(canonical, [canonical]):
                if key in data:
                    value = data.get(key)
                    used_key = key
                    break
            if used_key:
                displayed_keys.add(used_key)
            if canonical == "name" and idx == 1 and value and md_escape_value(value) != "N/A":
                first_name = md_escape_value(value)
            output += f"{label}: `{md_escape_value(value)}`\n"

        extras = []
        for key, value in data.items():
            if key in displayed_keys:
                continue
            if any(key in keys for keys in aliases.values()):
                continue
            extras.append(f"• {key}: `{md_escape_value(value)}`")
        if extras:
            output += "\n🧩 *Extra Data*\n" + "\n".join(extras) + "\n"

    user = get_user(user_id)
    updated_total = get_total_credits(user_id)
    output += "\n━━━━━━━━━━━━━━\n"
    if unlimited_active:
        output += f"🚀 Unlimited Active\n⏰ Expires: `{md_escape_value(str(unlimited_expiry)[:16])}`\n"
    else:
        output += f"💎 Used: `1` | Left: `{updated_total}`\n🔎 Searches: `{user.get('total_searches', 0) if user else 0}`\n"
    output += f"\n⚠️ Auto-delete: `{AUTO_DELETE_SECONDS}s`{footer()}"
    return output, first_name


def split_telegram_message(text, limit=3600):
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for part in text.split("\n━━━━━━━━━━━━━━\n"):
        candidate = part if not current else current + "\n━━━━━━━━━━━━━━\n" + part
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = part
            while len(current) > limit:
                chunks.append(current[:limit])
                current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


def send_lookup_output(chat_id, message_id, output, markup=None):
    chunks = split_telegram_message(output)
    sent = bot.edit_message_text(
        chunks[0], chat_id, message_id,
        reply_markup=markup if len(chunks) == 1 else None,
        parse_mode='Markdown'
    )
    for i, chunk in enumerate(chunks[1:], 1):
        sent = bot.send_message(
            chat_id, chunk,
            reply_markup=markup if i == len(chunks) - 1 else None,
            parse_mode='Markdown'
        )
    return sent

# ==================== LOOKUP PROCESS ====================
def process_lookup(message):
    user_id = message.from_user.id
    phone = message.text.strip()
    
    if user_id in user_states:
        del user_states[user_id]
    
    if phone == "/cancel":
        bot.reply_to(message, "❌ Cancelled!", reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
        return
    
    if not re.match(r'^[6-9]\d{9}$', phone):
        bot.reply_to(message, "❌ *Invalid number!*\n\nEnter 10-digit Indian number.\nExample: `9876543210`", 
                    reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
        return
    
    if user_id in user_cooldown:
        if time.time() - user_cooldown[user_id] < COOLDOWN_SECONDS:
            wait_time = int(COOLDOWN_SECONDS - (time.time() - user_cooldown[user_id]))
            bot.reply_to(message, f"⏳ *Please wait {wait_time} seconds*", reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
            return
    
    user = get_user(user_id)
    total_credits = get_total_credits(user_id)
    
    unlimited_active = False
    unlimited_expiry = None
    unlimited_expiry_raw = user.get('unlimited_expiry') if user else None
    if unlimited_expiry_raw:
        try:
            if isinstance(unlimited_expiry_raw, str):
                expiry_date = datetime.fromisoformat(unlimited_expiry_raw.replace('Z', '+00:00'))
            else:
                expiry_date = unlimited_expiry_raw
            if expiry_date > datetime.now(timezone.utc):
                unlimited_active = True
                unlimited_expiry = unlimited_expiry_raw
        except:
            pass
    
    if total_credits <= 0 and not unlimited_active:
        markup = universal_markup(buy=True, join=True, admin=True)
        bot.reply_to(message, "❌ *No credits!* Buy more credits or get an unlimited plan.", reply_markup=markup, parse_mode='Markdown')
        return
    
    if is_number_protected(phone):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛡️ PROTECT MY NUMBER", callback_data="protect"))
        markup.add(InlineKeyboardButton("🔙 MAIN MENU", callback_data="main_menu"))
        bot.reply_to(message, f"""
🛡️ *PROTECTED NUMBER*

📱 `{phone}`

This number is protected by the Number Protection Plan.

The owner has purchased privacy protection. Details are hidden.

You can also protect your number for just ₹49!
""", reply_markup=markup, parse_mode='Markdown')
        return
    
    user_cooldown[user_id] = time.time()
    loading_msg = bot.reply_to(message, "🔍 *Searching...*", parse_mode='Markdown')
    
    time.sleep(1)
    bot.edit_message_text("🔍 *Searching..*", message.chat.id, loading_msg.message_id, parse_mode='Markdown')
    time.sleep(1)
    bot.edit_message_text("🔍 *Searching...*", message.chat.id, loading_msg.message_id, parse_mode='Markdown')
    
    cached_result = get_cached_result(phone)
    
    if cached_result:
        if not unlimited_active:
            if not deduct_credit(user_id):
                bot.edit_message_text("❌ *Failed to deduct credit. Please try again.*", 
                                    message.chat.id, loading_msg.message_id, parse_mode='Markdown')
                return
        
        increment_total_searches(user_id)
        
        output, first_name = format_lookup_result(cached_result, phone, user_id, unlimited_active, unlimited_expiry)
        
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🔍 NEW SEARCH", callback_data="lookup"),
            InlineKeyboardButton("🏠 MENU", callback_data="main_menu")
        )
        markup.add(InlineKeyboardButton("📢 JOIN GROUP", url=GROUP_LINK))
        
        sent_msg = send_lookup_output(message.chat.id, loading_msg.message_id, output, markup)
        
        log_message = f"""
🔍 *SEARCH LOG (Cached)*
━━━━━━━━━━━━━━━━
👤 User: {message.from_user.first_name}
🆔 ID: `{user_id}`
📱 Number: `{phone}`
👤 Name Found: `{first_name}`
🕐 Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
"""
        try:
            bot.send_message(ADMIN_CHANNEL_ID, log_message, parse_mode='Markdown')
        except:
            pass
        
        def auto_delete():
            time.sleep(AUTO_DELETE_SECONDS)
            try:
                bot.delete_message(message.chat.id, sent_msg.message_id)
            except:
                pass
        
        threading.Thread(target=auto_delete, daemon=True).start()
        return
    
    try:
        url = f"{LOOKUP_API_URL}?key={LOOKUP_API_KEY}&service={LOOKUP_API_SERVICE}&query={phone}"
        r = requests.get(url, timeout=10)
        result = r.json()
    except Exception as e:
        bot.edit_message_text(f"❌ *API Error*\n\n{str(e)}", message.chat.id, loading_msg.message_id, parse_mode='Markdown')
        return

    if result and result.get('results'):
        if not unlimited_active:
            if not deduct_credit(user_id):
                bot.edit_message_text("❌ *Failed to deduct credit. Please try again.*", 
                                    message.chat.id, loading_msg.message_id, parse_mode='Markdown')
                return
        
        increment_total_searches(user_id)
        save_cached_result(phone, result)
        output, first_name = format_lookup_result(result, phone, user_id, unlimited_active, unlimited_expiry)
        
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🔍 NEW SEARCH", callback_data="lookup"),
            InlineKeyboardButton("🏠 MENU", callback_data="main_menu")
        )
        markup.add(InlineKeyboardButton("📢 JOIN GROUP", url=GROUP_LINK))
        
        sent_msg = send_lookup_output(message.chat.id, loading_msg.message_id, output, markup)
        
        log_message = f"""
🔍 *SEARCH LOG*
━━━━━━━━━━━━━━━━
👤 User: {message.from_user.first_name}
🆔 ID: `{user_id}`
📱 Number: `{phone}`
👤 Name Found: `{first_name}`
🕐 Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
"""
        try:
            bot.send_message(ADMIN_CHANNEL_ID, log_message, parse_mode='Markdown')
        except:
            pass
        
        def auto_delete():
            time.sleep(AUTO_DELETE_SECONDS)
            try:
                bot.delete_message(message.chat.id, sent_msg.message_id)
            except:
                pass
        
        threading.Thread(target=auto_delete, daemon=True).start()
        
    else:
        output = f"""
❌ *NO DATA FOUND*
━━━━━━━━━━━━━━━━━━

📱 Number
`{phone}`

🚫 No records available in database

💡 Tips:
• Check number again
• Try another number
• Ensure Indian mobile number

━━━━━━━━━━━━━━━━━━
💎 Credits NOT deducted
{footer()}
"""
        
        bot.edit_message_text(output, message.chat.id, loading_msg.message_id, parse_mode='Markdown')
        
        log_message = f"""
🔍 *SEARCH LOG (No Data)*
━━━━━━━━━━━━━━━━
👤 User: {message.from_user.first_name}
🆔 ID: `{user_id}`
📱 Number: `{phone}`
🕐 Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
"""
        try:
            bot.send_message(ADMIN_CHANNEL_ID, log_message, parse_mode='Markdown')
        except:
            pass

# ==================== FLASK WEBHOOK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "TraceX Bot Running"

@app.route('/cashfree/webhook', methods=['POST'])
def cashfree_webhook():
    """Handle Cashfree payment webhook.
    Security flow: signature check if possible + final verification from Cashfree Order Status API.
    This avoids false 401 issues while still preventing fake credit addition.
    """
    try:
        signature = (
            request.headers.get('x-webhook-signature') or
            request.headers.get('X-Cashfree-Signature') or
            request.headers.get('x-cashfree-signature') or
            request.headers.get('X-Webhook-Signature') or
            ''
        )
        raw_body = request.get_data(as_text=True)
        data = request.get_json(silent=True) or {}

        print(f"Webhook received headers signature-present={bool(signature)}")
        print(f"Webhook body preview: {raw_body[:1000]}")

        # Extract order_id from common Cashfree webhook formats.
        order_id = None
        payment_id = None
        if isinstance(data, dict):
            order_id = data.get('order_id')
            payment_id = data.get('payment_id')

            if not order_id and isinstance(data.get('order'), dict):
                order_id = data['order'].get('order_id')
            if not payment_id and isinstance(data.get('payment'), dict):
                payment_id = data['payment'].get('cf_payment_id') or data['payment'].get('payment_id')

            nested = data.get('data')
            if isinstance(nested, dict):
                if not order_id:
                    order_id = nested.get('order_id')
                if not payment_id:
                    payment_id = nested.get('payment_id')
                if isinstance(nested.get('order'), dict) and not order_id:
                    order_id = nested['order'].get('order_id')
                if isinstance(nested.get('payment'), dict) and not payment_id:
                    payment_id = nested['payment'].get('cf_payment_id') or nested['payment'].get('payment_id')

        if not order_id:
            print("Could not extract order_id from webhook data")
            return jsonify({"error": "No order_id found"}), 400

        # Signature verification is attempted, but final trust is based on Cashfree Order Status API.
        if CASHFREE_WEBHOOK_SECRET:
            if verify_cashfree_signature(raw_body, signature):
                print("✅ Cashfree webhook signature verified")
            else:
                print("⚠️ Cashfree webhook signature mismatch. Continuing with Order Status API verification.")
        else:
            print("⚠️ CASHFREE_WEBHOOK_SECRET not set. Using Order Status API verification only.")

        # Idempotency early check.
        try:
            claim_check = supabase.table("payment_claims").select("status").eq("cashfree_order_id", order_id).limit(1).execute()
            if claim_check.data and claim_check.data[0].get("status") == "success":
                print(f"Payment {order_id} already success, ignoring duplicate webhook")
                return jsonify({"status": "already_processed"}), 200
        except Exception as e:
            print(f"Payment claim precheck error: {e}")

        # Final verification from Cashfree API.
        order_data = get_cashfree_order_status(order_id)
        if not order_data:
            print(f"Could not verify order {order_id} status via Cashfree API")
            return jsonify({"error": "Verification failed"}), 400

        order_status = str(order_data.get('order_status', '')).upper()
        print(f"Cashfree verified order status for {order_id}: {order_status}")

        if order_status in ['PAID', 'SUCCESS', 'COMPLETED']:
            pay_id = payment_id or order_data.get('payment_id') or order_data.get('cf_payment_id')
            if not process_telegram_session_success(order_id, pay_id, order_data):
                process_payment_success(order_id, pay_id, order_data)
        else:
            print(f"Order {order_id} status is {order_status}, not processing")

        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/pay/<session_id>', methods=['GET'])
def telegram_payment_session_page(session_id):
    """Backward-compatible route: redirect old Render payment links to approved website checkout."""
    try:
        website_url = f"{WEBSITE_PAYMENT_BASE_URL}/telegram-pay?session_id={session_id}"
        return f"""
        <!doctype html>
        <html>
        <head>
          <meta name="viewport" content="width=device-width, initial-scale=1.0" />
          <title>TraceX Redirecting...</title>
          <meta http-equiv="refresh" content="0;url={website_url}">
          <style>
            body {{font-family:Arial,sans-serif;background:#0b0f17;color:#fff;text-align:center;padding:40px;}}
            .card {{max-width:480px;margin:auto;background:#111827;border:1px solid #273244;border-radius:18px;padding:24px;}}
            a {{color:#22d3ee;font-weight:bold;}}
          </style>
        </head>
        <body>
          <div class="card">
            <h2>💳 Opening Secure Checkout...</h2>
            <p>Redirecting to approved TraceX payment page.</p>
            <p><a href="{website_url}">Click here if not redirected</a></p>
          </div>
          <script>window.location.replace("{website_url}");</script>
        </body>
        </html>
        """
    except Exception as e:
        print(f"/pay redirect error: {e}")
        return "Payment redirect error. Please generate a new link from Telegram.", 500


@app.route('/verify-payment/<session_id>', methods=['GET'])
def verify_telegram_payment(session_id):
    """Website success page calls this route to verify payment and add Telegram credits/plan."""
    try:
        resp = supabase.table("telegram_payment_sessions").select("*").eq("session_id", session_id).limit(1).execute()
        if not resp.data:
            return jsonify({"success": False, "status": "invalid", "message": "Invalid payment session"}), 404

        session = resp.data[0]
        status = str(session.get("status") or "pending").lower()
        plan_id = session.get("plan_id")
        credits = int(session.get("credits") or 0)
        unlimited_minutes = int(session.get("unlimited_minutes") or 0)

        if status in SUCCESS_STATUSES:
            return jsonify({
                "success": True,
                "status": status,
                "message": "Payment already verified",
                "credits_added": credits,
                "plan_id": plan_id,
                "unlimited_minutes": unlimited_minutes
            }), 200

        expires_raw = session.get("expires_at")
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(str(expires_raw).replace('Z', '+00:00'))
                if expires_at <= datetime.now(timezone.utc) and status in ["pending", "processing"]:
                    supabase.table("telegram_payment_sessions").update({
                        "status": "expired",
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).eq("session_id", session_id).execute()
                    return jsonify({"success": False, "status": "expired", "message": "Payment link expired"}), 410
            except Exception as exp_e:
                print(f"Verify expiry parse warning: {exp_e}")

        order_id = get_session_order_id(session)
        if not order_id:
            return jsonify({"success": False, "status": status, "message": "Payment order not created yet"}), 200

        order_data = get_cashfree_order_status(order_id)
        if not order_data:
            return jsonify({"success": False, "status": status, "message": "Unable to verify payment right now"}), 200

        order_status = str(order_data.get("order_status") or "").upper()
        print(f"Website verify route: session={session_id} order={order_id} status={order_status}")

        if order_status in ["PAID", "SUCCESS", "COMPLETED"]:
            payment_id = order_data.get("payment_id") or order_data.get("cf_payment_id")
            ok = process_telegram_session_success(order_id, payment_id, order_data)
            return jsonify({
                "success": bool(ok),
                "status": "success" if ok else "processing_error",
                "message": "Payment verified" if ok else "Payment verified but credit update failed",
                "credits_added": credits,
                "plan_id": plan_id,
                "unlimited_minutes": unlimited_minutes
            }), 200 if ok else 500

        return jsonify({
            "success": False,
            "status": order_status.lower() or status,
            "message": "Payment pending or failed",
            "credits_added": 0,
            "plan_id": plan_id
        }), 200
    except Exception as e:
        print(f"Verify telegram payment error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "status": "error", "message": str(e)}), 500

@app.route('/payment/success', methods=['GET'])
def payment_success():
    """Payment success redirect page. Fallback if Cashfree returns to Render instead of web.app."""
    order_id = request.args.get('order_id', '')
    session_id = request.args.get('session_id', '')

    if session_id and not order_id:
        try:
            resp = supabase.table("telegram_payment_sessions").select("*").eq("session_id", session_id).limit(1).execute()
            if resp.data:
                order_id = get_session_order_id(resp.data[0]) or ""
        except Exception as e:
            print(f"payment_success session lookup warning: {e}")
    
    order_data = get_cashfree_order_status(order_id) if order_id else None
    
    if order_data and str(order_data.get('order_status', '')).upper() in ['PAID', 'SUCCESS', 'COMPLETED']:
        process_telegram_session_success(order_id, order_data.get('payment_id') or order_data.get('cf_payment_id'), order_data)
        return """
        <html>
            <head><title>Payment Successful - TraceX</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: green;">✅ Payment Successful!</h1>
                <p>Your order has been processed successfully.</p>
                <p>You will receive a confirmation in Telegram shortly.</p>
                <p>You can now close this window and return to Telegram.</p>
                <script>
                    setTimeout(function() {
                        window.close();
                    }, 5000);
                </script>
            </body>
        </html>
        """
    else:
        return """
        <html>
            <head><title>Payment Processing - TraceX</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: orange;">⏳ Payment Processing</h1>
                <p>Your payment is being processed.</p>
                <p>You will receive a confirmation in Telegram shortly.</p>
                <p>You can close this window and wait for Telegram notification.</p>
            </body>
        </html>
        """

def keep_alive():
    """Run Flask app in a separate thread"""
    def run():
        app.run(host='0.0.0.0', port=8080)
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()

# ==================== PAYMENT HANDLERS ====================
def show_credit_packs(message, user_id):
    packs_msg = f"""
💎 *PREMIUM CREDIT STORE*
━━━━━━━━━━━━━━━━━━

💰 *CREDIT PACKS*
• 10 Credits → ₹20
• 50 Credits → ₹70  
• 100 Credits → ₹100

🚀 *UNLIMITED PLANS*
• 1 Hour Unlimited → ₹9
• 1 Day Unlimited → ₹29
• 7 Days Unlimited → ₹149
• 30 Days Unlimited → ₹399

🛡️ *PROTECTION*
• Protect Number → ₹49

━━━━━━━━━━━━━━━━━━
✅ Permanent Credits NEVER EXPIRE
✅ Unlimited Plans for heavy users
✅ Secure Payment via Website

👇 Select your plan below
{footer()}
"""
    bot.send_message(message.chat.id, packs_msg, reply_markup=credit_packs_markup(), parse_mode='Markdown')

def check_telegram_session_status(call, tx_code):
    """Button helper for stateless website payment flow.
    Website should write a successful row into payment_claims with session_id/tx = tx_code, then update telegram_users.
    Fallback also checks old telegram_payment_sessions for compatibility.
    """
    try:
        user_id = call.from_user.id
        tx_code = str(tx_code).strip()

        # 1) New stateless flow: website stores transaction in payment_claims.session_id or idempotency_key.
        claim = None
        for field in ["session_id", "idempotency_key", "cashfree_order_id"]:
            try:
                q = supabase.table("payment_claims").select("*").eq(field, tx_code).limit(1).execute()
                if q.data:
                    claim = q.data[0]
                    break
            except Exception as qerr:
                print(f"payment_claims check warning field={field}: {qerr}")

        if claim:
            claim_user = claim.get("telegram_user_id")
            if claim_user and int(claim_user) != int(user_id) and user_id != ADMIN_ID:
                bot.answer_callback_query(call.id, "This payment is not yours.", show_alert=True)
                return
            status = normalize_status(claim.get("status"))
            if status in SUCCESS_STATUSES:
                user = get_user(user_id)
                credits = user.get("credits", 0) if user else 0
                unlimited_expiry = user.get("unlimited_expiry") if user else None
                extra = f"\n🚀 Unlimited: `{str(unlimited_expiry)[:19]}`" if unlimited_expiry else ""
                bot.answer_callback_query(call.id, "Payment verified ✅", show_alert=True)
                bot.send_message(
                    call.message.chat.id,
                    f"✅ *Payment Verified!*\n\n💎 Account updated successfully.\n💰 Current Credits: `{credits}`{extra}\n\nUse /start to refresh.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_markup(user_id)
                )
                return
            bot.answer_callback_query(call.id, "Payment found but not success yet. Wait few seconds.", show_alert=True)
            return

        # 2) Compatibility fallback: old telegram_payment_sessions table.
        try:
            resp = supabase.table("telegram_payment_sessions").select("*").eq("session_id", tx_code).limit(1).execute()
        except Exception as old_e:
            print(f"old telegram_payment_sessions check warning: {old_e}")
            resp = None

        if resp and resp.data:
            session = resp.data[0]
            if int(session.get("telegram_user_id") or 0) != int(user_id) and user_id != ADMIN_ID:
                bot.answer_callback_query(call.id, "This payment session is not yours.", show_alert=True)
                return
            status = normalize_status(session.get("status"))
            if status in SUCCESS_STATUSES:
                user = get_user(user_id)
                credits = user.get("credits", 0) if user else 0
                bot.answer_callback_query(call.id, "Payment verified ✅", show_alert=True)
                bot.send_message(
                    call.message.chat.id,
                    f"✅ *Payment Verified!*\n\n💎 Account updated successfully.\n💰 Current Credits: `{credits}`\n\nUse /start to refresh.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_markup(user_id)
                )
                return
            if status == "expired":
                bot.answer_callback_query(call.id, "Session expired. Generate a new link.", show_alert=True)
                return

        bot.answer_callback_query(call.id, "Payment not confirmed yet. Try again after a few seconds.", show_alert=True)
    except Exception as e:
        print(f"Check payment status error: {e}")
        import traceback
        traceback.print_exc()
        bot.answer_callback_query(call.id, "Unable to check payment right now.", show_alert=True)

def handle_plan_selection(call):
    plan_id = call.data.replace("plan_", "")
    user_id = call.from_user.id
    username = call.from_user.username or "no_username"

    plan = PLAN_CONFIG.get(plan_id)
    if not plan:
        bot.answer_callback_query(call.id, "Invalid plan selected.", show_alert=True)
        return

    amount = plan["amount"]
    plan_label = plan["label"]
    print(f"Plan selected: {plan_id}, Amount: ₹{amount}, User: {user_id}")

    if plan_id == "protect49":
        user_states[user_id] = "awaiting_protect_number"
        msg = bot.send_message(
            call.message.chat.id,
            "🛡️ *PROTECT NUMBER*\n\nEnter the 10-digit mobile number you want to protect:\n\n`Example: 9876543210`\n\n⚠️ Payment link will be valid for 10 minutes only.\n\nType /cancel to abort",
            reply_markup=cancel_button(),
            parse_mode='Markdown'
        )
        bot.register_next_step_handler(msg, process_protect_number, plan_id, amount)
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Generating secure payment link... ⏳")

    session_code, payment_link, expires_at = create_telegram_payment_session(
        plan_id=plan_id,
        telegram_user_id=user_id,
        telegram_username=username
    )

    if session_code and payment_link:
        expires_text = expires_at.strftime('%Y-%m-%d %H:%M:%S UTC') if expires_at else "10 minutes"
        payment_msg = f"""
💳 *Pay Now*
━━━━━━━━━━━━━━━━━━

💰 *Amount:* ₹{amount}
📦 *Plan:* `{plan_label}`
🆔 *Session:* `{session_code}`

⏳ *Valid for:* `{PAYMENT_SESSION_TTL_MINUTES} minutes`
🕐 *Expires:* `{expires_text}`

✅ Pay within `{PAYMENT_SESSION_TTL_MINUTES} min`. Credits/plan auto-add after payment.

━━━━━━━━━━━━━━━━━━
📞 For issues: {ADMIN_USERNAME}
"""
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 PAY NOW", url=payment_link))
        markup.add(InlineKeyboardButton("✅ CHECK PAYMENT", callback_data=f"checkpay_{session_code}"))
        markup.add(InlineKeyboardButton("🔙 MAIN MENU", callback_data="main_menu"))
        bot.send_message(call.message.chat.id, payment_msg, reply_markup=markup, parse_mode='Markdown')

        try:
            bot.send_message(
                ADMIN_CHANNEL_ID,
                f"💳 *TELEGRAM PAYMENT SESSION CREATED*\n━━━━━━━━━━━━━━━━\n👤 User: `{user_id}`\n@ Username: @{username}\n📦 Plan: `{plan_id}`\n💰 Amount: ₹{amount}\n🆔 Session: `{session_code}`\n⏳ Expires in: `{PAYMENT_SESSION_TTL_MINUTES} min`",
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"Admin payment session log failed: {e}")
    else:
        bot.answer_callback_query(call.id, "Payment link creation failed. Try again later.", show_alert=True)
        bot.send_message(
            call.message.chat.id,
            f"❌ *Payment Link Error*\n\nCould not generate payment link right now.\nPlease try again or contact {ADMIN_USERNAME}.",
            parse_mode='Markdown'
        )

def process_protect_number(message, plan_id, amount):
    user_id = message.from_user.id

    if user_id in user_states:
        del user_states[user_id]

    if message.text == "/cancel":
        bot.reply_to(message, "❌ Cancelled!", reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
        return

    phone = message.text.strip()

    if not re.match(r'^[6-9]\d{9}$', phone):
        bot.reply_to(message, "❌ *Invalid number!*\n\nEnter 10-digit Indian number.\nExample: `9876543210`", reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
        return

    if is_number_protected(phone):
        bot.reply_to(message, f"❌ *Number already protected!*\n\n📱 `{phone}`\n\nThis number is already in the protection list.", parse_mode='Markdown')
        return

    username = message.from_user.username or "no_username"
    session_code, payment_link, expires_at = create_telegram_payment_session(
        plan_id=plan_id,
        telegram_user_id=user_id,
        telegram_username=username,
        protected_number=phone
    )

    if session_code and payment_link:
        expires_text = expires_at.strftime('%Y-%m-%d %H:%M:%S UTC') if expires_at else "10 minutes"
        payment_msg = f"""
🛡️ *PROTECT NUMBER PAYMENT*
━━━━━━━━━━━━━━━━━━

📱 *Number:* `{phone}`
💰 *Amount:* ₹{amount}
📦 *Plan:* Number Protection
🆔 *Session:* `{session_code}`

⏳ *Valid for:* `{PAYMENT_SESSION_TTL_MINUTES} minutes`
🕐 *Expires:* `{expires_text}`

✅ Pay within `{PAYMENT_SESSION_TTL_MINUTES} min`. Protection auto-activates after payment.

━━━━━━━━━━━━━━━━━━
📞 For issues: {ADMIN_USERNAME}
"""
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 PAY NOW", url=payment_link))
        markup.add(InlineKeyboardButton("✅ CHECK PAYMENT", callback_data=f"checkpay_{session_code}"))
        markup.add(InlineKeyboardButton("🔙 MAIN MENU", callback_data="main_menu"))
        bot.send_message(message.chat.id, payment_msg, reply_markup=markup, parse_mode='Markdown')

        try:
            bot.send_message(
                ADMIN_CHANNEL_ID,
                f"🛡️ *PROTECTION PAYMENT SESSION CREATED*\n━━━━━━━━━━━━━━━━\n👤 User: `{user_id}`\n📱 Number: `{phone}`\n💰 Amount: ₹{amount}\n🆔 Session: `{session_code}`\n⏳ Expires in: `{PAYMENT_SESSION_TTL_MINUTES} min`",
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"Admin protection session log failed: {e}")
    else:
        bot.reply_to(message, "❌ Payment link generation failed. Please try again later.", reply_markup=main_menu_markup(user_id), parse_mode='Markdown')

# ==================== BOT HANDLERS ====================
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    user = get_user(user_id)
    
    if user and user.get('is_banned'):
        bot.reply_to(message, f"🚫 YOU ARE BANNED\n\nContact: {ADMIN_USERNAME}")
        return
    
    try:
        supabase.table("telegram_users").update({
            "telegram_username": username,
            "telegram_name": first_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("telegram_user_id", user_id).execute()
    except:
        pass
    
    total_credits = get_total_credits(user_id)
    unlimited_expiry = user.get('unlimited_expiry') if user else None
    
    unlimited_text = ""
    if unlimited_expiry:
        try:
            if isinstance(unlimited_expiry, str):
                expiry_date = datetime.fromisoformat(unlimited_expiry.replace('Z', '+00:00'))
            else:
                expiry_date = unlimited_expiry
            if expiry_date > datetime.now(timezone.utc):
                unlimited_text = f"\n🚀 Unlimited Active until: `{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}`"
        except:
            pass
    
    welcome_msg = f"""
{header("TRACEX LOOKUP", "🚀")}

👋 Welcome, *{first_name}*

💎 *Credit Details:*
━━━━━━━━━━━━━━━━
💰 Credits: `{total_credits}`{unlimited_text}

🔎 Total Searches: `{user.get('total_searches', 0) if user else 0}`

━━━━━━━━━━━━━━━━
🎯 *FEATURES*
• Instant Number Lookup
• Fast Response
• Secure Credits System
• Unlimited Plans Available
• Number Protection Service

━━━━━━━━━━━━━━━━
🎁 New users get 3 free credits!

👇 Choose an option below
{footer()}
"""
    
    bot.send_message(message.chat.id, welcome_msg, reply_markup=main_menu_markup(user_id), parse_mode='Markdown')

@bot.message_handler(commands=['cancel'])
def cancel_command(message):
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]
    if user_id in temp_data:
        del temp_data[user_id]
    bot.reply_to(message, "❌ Cancelled. Use /start for main menu.", reply_markup=main_menu_markup(user_id), parse_mode='Markdown')

@bot.message_handler(commands=['maintenance'])
def toggle_maintenance(message):
    global MAINTENANCE_MODE
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage:\n/maintenance on\n/maintenance off")
        return
    mode = parts[1].lower()
    if mode == "on":
        MAINTENANCE_MODE = True
        bot.reply_to(message, "🛠 Maintenance mode ENABLED")
    elif mode == "off":
        MAINTENANCE_MODE = False
        bot.reply_to(message, "✅ Maintenance mode DISABLED")
    else:
        bot.reply_to(message, "Invalid option!\nUse:\n/maintenance on\n/maintenance off")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    user = get_user(user_id)
    
    if user and user.get('is_banned'):
        bot.answer_callback_query(call.id, "You are banned!", show_alert=True)
        return
    
    if call.data == "main_menu":
        bot.edit_message_text("🏠 *MAIN MENU*", call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
    elif call.data == "cancel":
        if user_id in user_states:
            del user_states[user_id]
        if user_id in temp_data:
            del temp_data[user_id]
        bot.edit_message_text("❌ Cancelled. Use /start for main menu.", call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
        bot.answer_callback_query(call.id)
    elif call.data == "lookup":
        user_states[user_id] = "awaiting_number"
        msg = bot.send_message(call.message.chat.id, "📱 *Enter 10-digit number:*\n\n`Example: 9876543210`\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
        bot.register_next_step_handler(msg, process_lookup)
        bot.answer_callback_query(call.id)
    elif call.data == "protect":
        user_states[user_id] = "awaiting_protect_number"
        msg = bot.send_message(call.message.chat.id, "🛡️ *PROTECT NUMBER*\n\nEnter the 10-digit mobile number you want to protect:\n\n`Example: 9876543210`\n\n⚠️ Once protected, no one can lookup details for this number!\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
        bot.register_next_step_handler(msg, process_protect_number, "protect49", 49)
        bot.answer_callback_query(call.id)
    elif call.data == "credits":
        total_credits = get_total_credits(user_id)
        unlimited_expiry = user.get('unlimited_expiry') if user else None
        unlimited_text = ""
        if unlimited_expiry:
            try:
                if isinstance(unlimited_expiry, str):
                    expiry_date = datetime.fromisoformat(unlimited_expiry.replace('Z', '+00:00'))
                else:
                    expiry_date = unlimited_expiry
                if expiry_date > datetime.now(timezone.utc):
                    unlimited_text = f"\n🚀 *Unlimited Plan Active*\n   Expires: `{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}`"
            except:
                pass
        credits_msg = f"""
*💎 MY CREDITS*
━━━━━━━━━━━━━━━━━━
💰 *Credits:* `{total_credits}`{unlimited_text}
🔎 *Used:* `{user.get('total_searches', 0) if user else 0}`
━━━━━━━━━━━━━━━━━━
*📦 CREDIT PACKS*
• 10 Credits → ₹20
• 50 Credits → ₹70  
• 100 Credits → ₹100
*🚀 UNLIMITED PLANS*
• 1 Hour → ₹9
• 1 Day → ₹29
• 7 Days → ₹149
• 30 Days → ₹399
*🛡️ PROTECTION*
• Protect Number → ₹49
"""
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛒 BUY CREDITS", callback_data="buy"))
        markup.add(InlineKeyboardButton("🔙 BACK", callback_data="main_menu"))
        bot.edit_message_text(credits_msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
        bot.answer_callback_query(call.id)
    elif call.data == "buy":
        show_credit_packs(call.message, user_id)
        bot.answer_callback_query(call.id)
    elif call.data.startswith("checkpay_"):
        session_id = call.data.replace("checkpay_", "", 1)
        check_telegram_session_status(call, session_id)
    elif call.data.startswith("plan_"):
        handle_plan_selection(call)
    elif call.data == "profile":
        total_credits = get_total_credits(user_id)
        unlimited_expiry = user.get('unlimited_expiry') if user else None
        unlimited_text = ""
        if unlimited_expiry:
            try:
                if isinstance(unlimited_expiry, str):
                    expiry_date = datetime.fromisoformat(unlimited_expiry.replace('Z', '+00:00'))
                else:
                    expiry_date = unlimited_expiry
                if expiry_date > datetime.now(timezone.utc):
                    unlimited_text = f"\n🚀 Unlimited until: `{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}`"
            except:
                pass
        profile_msg = f"""
👤 *USER PROFILE*
━━━━━━━━━━━━━━━━━━
🆔 User ID: `{user_id}`
👤 Name: `{call.from_user.first_name}`
💎 *Credits:* `{total_credits}`{unlimited_text}
🔎 Total Searches: `{user.get('total_searches', 0) if user else 0}`
🛡️ Account Status: `{'ACTIVE ✅' if not (user and user.get('is_banned')) else 'BANNED ❌'}`
━━━━━━━━━━━━━━━━━━
🚀 Thanks for using TraceX
{footer()}
"""
        markup = universal_markup(back=True, join=True, admin=(user_id == ADMIN_ID))
        bot.edit_message_text(profile_msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
        bot.answer_callback_query(call.id)
    elif call.data == "help":
        help_msg = f"""
📖 *HOW TO USE TRACEX*
━━━━━━━━━━━━━━━━━━
1️⃣ Click NUMBER LOOKUP
2️⃣ Enter mobile number
3️⃣ Get instant results
━━━━━━━━━━━━━━━━━━
💎 *CREDIT SYSTEM*
• New User: `3` free credits
• Credits never expire
• Each lookup costs 1 credit
• Unlimited plans available
• Protected numbers cost ₹49
━━━━━━━━━━━━━━━━━━
🛒 BUYING
• Select plan
• Pay through secure website link
• Automatic activation
━━━━━━━━━━━━━━━━━━
⚡ FEATURES
✅ Fast Lookup
✅ Unlimited Plans
✅ Number Protection
✅ Secure Payments
{footer()}
"""
        markup = universal_markup(back=True, join=True, admin=(user_id == ADMIN_ID))
        bot.edit_message_text(help_msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='Markdown')
        bot.answer_callback_query(call.id)
    elif call.data == "admin":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "Unauthorized!", show_alert=True)
            return
        show_admin_panel(call.message)
        bot.answer_callback_query(call.id)
    elif call.data == "broadcast_confirm":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "Unauthorized!", show_alert=True)
            return
        confirm_broadcast(call)
    elif call.data == "giveaway_confirm":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "Unauthorized!", show_alert=True)
            return
        confirm_giveaway(call)
    elif call.data in ["admin_add", "admin_remove", "admin_ban", "admin_unban", "admin_broadcast", "admin_stats", "admin_transactions", "admin_back", "admin_giveaway"]:
        if user_id != ADMIN_ID:
            return
        if call.data == "admin_add":
            user_states[user_id] = "admin_add"
            msg = bot.send_message(call.message.chat.id, "➕ *ADD CREDITS*\n\nFormat: `user_id credits`\nExample: `123456789 50`\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
            bot.register_next_step_handler(msg, process_admin_add)
        elif call.data == "admin_remove":
            user_states[user_id] = "admin_remove"
            msg = bot.send_message(call.message.chat.id, "➖ *REMOVE CREDITS*\n\nFormat: `user_id credits`\nExample: `123456789 10`\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
            bot.register_next_step_handler(msg, process_admin_remove)
        elif call.data == "admin_ban":
            user_states[user_id] = "admin_ban"
            msg = bot.send_message(call.message.chat.id, "🚫 *BAN USER*\n\nEnter user ID to ban\nExample: `123456789`\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
            bot.register_next_step_handler(msg, process_admin_ban)
        elif call.data == "admin_unban":
            user_states[user_id] = "admin_unban"
            msg = bot.send_message(call.message.chat.id, "✅ *UNBAN USER*\n\nEnter user ID to unban\nExample: `123456789`\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
            bot.register_next_step_handler(msg, process_admin_unban)
        elif call.data == "admin_broadcast":
            user_states[user_id] = "admin_broadcast"
            msg = bot.send_message(call.message.chat.id, "📢 *BROADCAST*\n\nSend your broadcast message below:\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
            bot.register_next_step_handler(msg, process_admin_broadcast)
        elif call.data == "admin_giveaway":
            user_states[user_id] = "admin_giveaway"
            msg = bot.send_message(call.message.chat.id, "🎁 *GIVEAWAY CREDITS*\n\nEnter number of credits to give to ALL users:\n\nExample: `50`\n\nType /cancel to abort", reply_markup=cancel_button(), parse_mode='Markdown')
            bot.register_next_step_handler(msg, process_admin_giveaway)
        elif call.data == "admin_stats":
            show_admin_stats(call.message)
        elif call.data == "admin_transactions":
            show_admin_transactions(call.message)
        elif call.data == "admin_back":
            show_admin_panel(call.message)
        bot.answer_callback_query(call.id)

# ==================== ADMIN FUNCTIONS ====================
def show_admin_panel(message):
    stats = get_stats()
    admin_msg = f"""
*🛠 ADMIN PANEL*
*📊 STATS*
👥 Users: `{stats['total_users']}`
🔍 Searches: `{stats['total_searches']}`
💾 Cache: `{stats['cache_size']}`
🛡️ Protected: `{stats['protected_count']}`
*💎 CREDITS SYSTEM*
💰 Total Credits: `{stats['total_credits']}`
*💰 FINANCIAL*
💵 Revenue: ₹{stats['total_revenue']}
⏳ Pending: `{stats['pending_payments']}`
🚫 Banned: `{stats['banned_users']}`
📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
    """
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ ADD CREDITS", callback_data="admin_add"),
        InlineKeyboardButton("➖ REMOVE CREDITS", callback_data="admin_remove")
    )
    markup.add(
        InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"),
        InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")
    )
    markup.add(
        InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast"),
        InlineKeyboardButton("🎁 GIVEAWAY", callback_data="admin_giveaway")
    )
    markup.add(
        InlineKeyboardButton("📊 STATS", callback_data="admin_stats"),
        InlineKeyboardButton("📋 TRANSACTIONS", callback_data="admin_transactions")
    )
    markup.add(InlineKeyboardButton("🔙 BACK", callback_data="main_menu"))
    bot.send_message(message.chat.id, admin_msg, reply_markup=markup, parse_mode='Markdown')

def show_admin_stats(message):
    stats = get_stats()
    stats_msg = f"""
*📊 DETAILED STATS*
━━━━━━━━━━━━━━━━━━
👥 *USERS*
━━━━━━━━━━━━━━━━━━
Total Users: `{stats['total_users']}`
Banned Users: `{stats['banned_users']}`
Active Users: `{stats['total_users'] - stats['banned_users']}`
━━━━━━━━━━━━━━━━━━
💎 *CREDITS*
━━━━━━━━━━━━━━━━━━
Total Credits: `{stats['total_credits']}`
━━━━━━━━━━━━━━━━━━
📊 *USAGE*
━━━━━━━━━━━━━━━━━━
Total Searches: `{stats['total_searches']}`
Cache Size: `{stats['cache_size']}`
Protected Numbers: `{stats['protected_count']}`
━━━━━━━━━━━━━━━━━━
💰 *FINANCIAL*
━━━━━━━━━━━━━━━━━━
Total Revenue: ₹{stats['total_revenue']}
Pending Payments: `{stats['pending_payments']}`
📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
    """
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 BACK", callback_data="admin_back"))
    bot.send_message(message.chat.id, stats_msg, reply_markup=markup, parse_mode='Markdown')

def show_admin_transactions(message):
    transactions = get_recent_transactions()
    if not transactions:
        trans_msg = "📋 *No transactions found!*"
    else:
        trans_msg = "*📋 RECENT TRANSACTIONS*\n\n"
        for trans in transactions:
            status_emoji = "✅" if trans.get('status') == "success" else "⏳" if trans.get('status') == "pending" else "❌"
            trans_msg += f"{status_emoji} `{trans.get('payment_id', '')[:20]}` | User: `{trans.get('telegram_user_id', '')}` | ₹{trans.get('amount', 0)} | {trans.get('plan_id', '')}\n"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 BACK", callback_data="admin_back"))
    bot.send_message(message.chat.id, trans_msg, reply_markup=markup, parse_mode='Markdown')

def process_admin_add(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    if user_id in user_states:
        del user_states[user_id]
    if message.text == "/cancel":
        bot.reply_to(message, "Cancelled", reply_markup=main_menu_markup(user_id))
        return
    try:
        parts = message.text.split()
        target_user = int(parts[0])
        credits = int(parts[1])
        new_total = add_credits(target_user, credits)
        bot.reply_to(message, f"✅ Added {credits} credits to `{target_user}`\nNew total: `{new_total}`", parse_mode='Markdown')
        try:
            bot.send_message(target_user, f"✅ *{credits} credits added!*\nNew total: `{new_total}`", parse_mode='Markdown')
        except:
            pass
    except:
        bot.reply_to(message, "❌ Invalid format! Use: `user_id credits`", parse_mode='Markdown')

def process_admin_remove(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    if user_id in user_states:
        del user_states[user_id]
    if message.text == "/cancel":
        bot.reply_to(message, "Cancelled", reply_markup=main_menu_markup(user_id))
        return
    try:
        parts = message.text.split()
        target_user = int(parts[0])
        credits = int(parts[1])
        user = get_user(target_user)
        if user:
            current_credits = user.get('credits', 0)
            new_credits = max(0, current_credits - credits)
            supabase.table("telegram_users").update({"credits": new_credits, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("telegram_user_id", target_user).execute()
            bot.reply_to(message, f"✅ Removed {credits} credits from `{target_user}`\nNew total: `{new_credits}`", parse_mode='Markdown')
        else:
            bot.reply_to(message, f"❌ User `{target_user}` not found!", parse_mode='Markdown')
    except:
        bot.reply_to(message, "❌ Invalid format! Use: `user_id credits`", parse_mode='Markdown')

def process_admin_ban(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    if user_id in user_states:
        del user_states[user_id]
    if message.text == "/cancel":
        bot.reply_to(message, "Cancelled", reply_markup=main_menu_markup(user_id))
        return
    try:
        target_user = int(message.text.strip())
        ban_user(target_user)
        bot.reply_to(message, f"✅ Banned user `{target_user}`", parse_mode='Markdown')
        try:
            bot.send_message(target_user, "🚫 *You have been banned.* Contact support.", parse_mode='Markdown')
        except:
            pass
    except:
        bot.reply_to(message, "❌ Invalid user ID!", parse_mode='Markdown')

def process_admin_unban(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    if user_id in user_states:
        del user_states[user_id]
    if message.text == "/cancel":
        bot.reply_to(message, "Cancelled", reply_markup=main_menu_markup(user_id))
        return
    try:
        target_user = int(message.text.strip())
        unban_user(target_user)
        bot.reply_to(message, f"✅ Unbanned user `{target_user}`", parse_mode='Markdown')
        try:
            bot.send_message(target_user, "✅ *You have been unbanned!* Use /start", parse_mode='Markdown')
        except:
            pass
    except:
        bot.reply_to(message, "❌ Invalid user ID!", parse_mode='Markdown')

def process_admin_broadcast(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    if user_id in user_states:
        del user_states[user_id]
    if message.text == "/cancel":
        bot.reply_to(message, "Cancelled", reply_markup=main_menu_markup(user_id))
        return
    broadcast_text = message.text.strip()
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ YES, SEND", callback_data="broadcast_confirm"), InlineKeyboardButton("❌ NO, CANCEL", callback_data="cancel"))
    temp_data[user_id] = {'broadcast_text': broadcast_text}
    bot.reply_to(message, f"📢 *Confirm Broadcast*\n\n📝 Message:\n`{broadcast_text}`\n\nSend to all active users?", reply_markup=markup, parse_mode='Markdown')

def process_admin_giveaway(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    if user_id in user_states:
        del user_states[user_id]
    if message.text == "/cancel":
        bot.reply_to(message, "Cancelled", reply_markup=main_menu_markup(user_id))
        return
    try:
        credits = int(message.text.strip())
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ YES, GIVE AWAY", callback_data="giveaway_confirm"), InlineKeyboardButton("❌ NO, CANCEL", callback_data="cancel"))
        temp_data[user_id] = {'giveaway_credits': credits}
        bot.reply_to(message, f"🎁 *Confirm Giveaway*\n\nGive `{credits}` credits to ALL active users?\n\nThis will be sent to all users immediately!", reply_markup=markup, parse_mode='Markdown')
    except:
        bot.reply_to(message, "❌ Invalid number! Enter a valid credit amount.", parse_mode='Markdown')

def confirm_broadcast(call):
    user_id = call.from_user.id
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Unauthorized!", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if user_id not in temp_data:
        bot.edit_message_text("❌ Broadcast cancelled. No data found.", call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id))
        return
    broadcast_text = temp_data[user_id]['broadcast_text']
    users = get_all_users()
    success = 0
    failed = 0
    bot.edit_message_text(f"📡 *Broadcasting to {len(users)} users...*\n\nPlease wait...", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    for target_user_id in users:
        try:
            broadcast_msg = f"""
*📢 TRACEX BROADCAST*
{broadcast_text}
━━━━━━━━━━━━━━━━
📞 *Support:* {ADMIN_USERNAME}
👥 *Group:* [Join Community]({GROUP_LINK})
"""
            bot.send_message(target_user_id, broadcast_msg, parse_mode='Markdown', disable_web_page_preview=True)
            success += 1
        except Exception as e:
            failed += 1
        time.sleep(0.05)
    result_msg = f"""
✅ *Broadcast Complete!*
📊 *Statistics:*
• ✅ Sent: `{success}` users
• ❌ Failed: `{failed}` users
• 📝 Total: `{len(users)}` users
⏱️ Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
"""
    bot.edit_message_text(result_msg, call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
    del temp_data[user_id]

def confirm_giveaway(call):
    user_id = call.from_user.id
    if user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Unauthorized!", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    if user_id not in temp_data:
        bot.edit_message_text("❌ Giveaway cancelled. No data found.", call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id))
        return
    credits = temp_data[user_id]['giveaway_credits']
    bot.edit_message_text(f"🎁 *Processing Giveaway...*\n\nGiving `{credits}` credits to all users...", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
    success, failed = add_giveaway_credits(credits)
    result_msg = f"""
🎉 *Giveaway Complete!* 🎉
✨ `{credits}` credits given to each user!
📊 *Statistics:*
• ✅ Successful: `{success}` users
• ❌ Failed: `{failed}` users
💎 Total Credits Distributed: `{success * credits}`
⏱️ Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
"""
    bot.edit_message_text(result_msg, call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id), parse_mode='Markdown')
    del temp_data[user_id]

# ==================== START BOT ====================
if __name__ == "__main__":
    print("=" * 50)
    print(f"TraceX Lookup v{BOT_VERSION} is starting...")
    print(f"Admin ID: {ADMIN_ID}")
    print(f"Admin: {ADMIN_USERNAME}")
    print(f"Render Base URL: {TELEGRAM_PAYMENT_BASE_URL}")
    print(f"Cashfree Return Base URL: {CASHFREE_RETURN_BASE_URL}")
    print("Checkout Forward URL: DISABLED - Direct Cashfree")
    print(f"Payment Session TTL: {PAYMENT_SESSION_TTL_MINUTES} minutes")
    print(f"Admin/Log Channel ID: {ADMIN_CHANNEL_ID}")
    print("=" * 50)
    
    # Start Flask keep_alive server
    keep_alive()
    print("✅ Flask server started on port 8080")
    cleanup_old_payment_sessions()
    
    print("✅ Bot is running! Press Ctrl+C to stop.")
    print("=" * 50)
    
    def signal_handler(sig, frame):
        print("\n🛑 Bot stopped by user")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # Clear any old webhook before long polling. Only ONE bot instance must be running.
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception as e:
        print(f"remove_webhook warning: {e}")

    while True:
        try:
            bot.infinity_polling(timeout=60, skip_pending=True)
        except Exception as e:
            print(f"Polling error: {e}")
            # Telegram 409 means another instance is still running with the same bot token.
            if "409" in str(e) or "getUpdates" in str(e):
                print("⚠️ Telegram 409 conflict: stop old Render/Termux/other bot instance using same BOT_TOKEN.")
            time.sleep(10)
