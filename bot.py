#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Bot for Number Lookup Service
Optimized for Render Deployment
Version: 2.0.1 - Fixed for Python 3.14+
"""

import os
import asyncio
import logging
import json
import html
from typing import Dict, Any, List

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ==================== CONFIGURATION ====================
# Get from environment variables for Render
BOT_TOKEN = os.getenv("BOT_TOKEN", "8437341863:AAG8EWGdgCcgKLhrvj96m5JSK32qPyqTfxY")
BOT_USERNAME = os.getenv("BOT_USERNAME", "tracexdatafreebot")
API_URL = os.getenv("API_URL", "https://tracexdata-api.onrender.com/api/lookup?key=Pvttbott&number={number}")

MAX_MESSAGE_LENGTH = 4096

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ==================== HELPER FUNCTIONS ====================

def format_json_to_markdown(data: Any) -> str:
    """
    Convert JSON data to nicely formatted JSON string with markdown code block.
    """
    try:
        if isinstance(data, (dict, list)):
            json_str = json.dumps(data, indent=2, ensure_ascii=False, default=str)
            return f"```json\n{json_str}\n```"
        elif isinstance(data, str):
            try:
                parsed = json.loads(data)
                json_str = json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
                return f"```json\n{json_str}\n```"
            except json.JSONDecodeError:
                return html.escape(data)
        else:
            json_str = json.dumps(data, indent=2, ensure_ascii=False, default=str)
            return f"```json\n{json_str}\n```"
    except Exception as e:
        logger.error(f"JSON formatting error: {e}")
        return html.escape(str(data))


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split a long message into chunks that fit within Telegram's limit."""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    lines = text.split("\n")
    
    for line in lines:
        if len(line) > max_length:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
            for i in range(0, len(line), max_length):
                chunks.append(line[i:i + max_length])
            continue
        
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                for i in range(0, len(line), max_length):
                    chunks.append(line[i:i + max_length])
        else:
            if current_chunk:
                current_chunk += "\n" + line
            else:
                current_chunk = line
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks


def create_inline_keyboard() -> InlineKeyboardMarkup:
    """Create the inline keyboard with the required buttons."""
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Add Me To Your Group",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
            ),
        ],
        [
            InlineKeyboardButton(
                "🚀 Try Advance Lookup Tool",
                url="https://t.me/tracexnumberbot"
            ),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def validate_phone_number(number: str) -> bool:
    """Validate if the input is a valid phone number."""
    if not number:
        return False
    
    if number.startswith('+'):
        number = number[1:]
    
    if not number.isdigit():
        return False
    
    if len(number) < 5 or len(number) > 15:
        return False
    
    return True


def fetch_number_lookup(number: str) -> Dict[str, Any]:
    """Fetch number lookup data from the API."""
    url = API_URL.format(number=number)
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def clean_response_data(data: Any) -> Any:
    """Clean the response data by removing developer/branding fields."""
    if isinstance(data, dict):
        fields_to_remove = ['developer', 'api_buy_link', 'website_link', 'support', 'buy_api', 'BUY API', 'SUPPORT']
        cleaned = {}
        for key, value in data.items():
            if key not in fields_to_remove:
                cleaned[key] = clean_response_data(value)
        return cleaned
    elif isinstance(data, list):
        return [clean_response_data(item) for item in data]
    else:
        return data


# ==================== COMMAND HANDLERS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    welcome_message = (
        "👋 <b>Welcome to Number Lookup Bot!</b>\n\n"
        "This bot allows you to lookup information about phone numbers.\n\n"
        "<b>How to use:</b>\n"
        "1. Type <code>/number</code> followed by the phone number\n"
        "2. Example: <code>/number 9876543210</code>\n\n"
        "<b>Features:</b>\n"
        "• Fast number lookup\n"
        "• Clean JSON output with markdown formatting\n"
        "• Works in groups and private chats\n\n"
        "Click the button below to add me to your group!"
    )
    
    await update.message.reply_html(
        welcome_message,
        reply_markup=create_inline_keyboard()
    )


async def number_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /number command."""
    try:
        message_text = update.message.text.strip()
        parts = message_text.split(maxsplit=1)
        
        if len(parts) < 2 or not parts[1].strip():
            usage_message = (
                "🔍 <b>Number Lookup</b>\n\n"
                "Please provide a phone number to lookup.\n\n"
                "<b>Usage:</b>\n"
                "<code>/number 9876543210</code>\n\n"
                "The number should be 5-15 digits long."
            )
            await update.message.reply_html(usage_message, reply_markup=create_inline_keyboard())
            return
        
        number = parts[1].strip()
        
        if not validate_phone_number(number):
            error_message = (
                "❌ <b>Invalid Number</b>\n\n"
                "Please provide a valid phone number.\n\n"
                "<b>Requirements:</b>\n"
                "• 5-15 digits long\n"
                "• Only numbers (no special characters)\n\n"
                "<b>Example:</b>\n"
                "<code>/number 9876543210</code>"
            )
            await update.message.reply_html(error_message, reply_markup=create_inline_keyboard())
            return
        
        processing_msg = await update.message.reply_text(
            "⏳ <i>Looking up number...</i>",
            parse_mode="HTML"
        )
        
        try:
            api_response = fetch_number_lookup(number)
        except requests.Timeout:
            await processing_msg.edit_text(
                "⏱️ <b>Timeout</b>\n\nThe API request timed out. Please try again later.",
                parse_mode="HTML",
                reply_markup=create_inline_keyboard()
            )
            return
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response else "Unknown"
            await processing_msg.edit_text(
                f"❌ <b>API Error</b>\n\n<b>Status Code:</b> {status_code}\n\nPlease try again later.",
                parse_mode="HTML",
                reply_markup=create_inline_keyboard()
            )
            logger.error(f"HTTP Error for number {number}: {e}")
            return
        except requests.RequestException as e:
            await processing_msg.edit_text(
                "❌ <b>Connection Error</b>\n\nFailed to connect to the lookup service. Please try again later.",
                parse_mode="HTML",
                reply_markup=create_inline_keyboard()
            )
            logger.error(f"Request error for number {number}: {e}")
            return
        except Exception as e:
            await processing_msg.edit_text(
                "❌ <b>Unexpected Error</b>\n\nAn unexpected error occurred. Please try again later.",
                parse_mode="HTML",
                reply_markup=create_inline_keyboard()
            )
            logger.error(f"Unexpected error for number {number}: {e}", exc_info=True)
            return
        
        # Clean and format the response
        cleaned_response = clean_response_data(api_response)
        
        if isinstance(cleaned_response, dict) and cleaned_response.get("error"):
            error_msg = cleaned_response.get("message") or cleaned_response.get("error") or "Unknown API error"
            await processing_msg.edit_text(
                f"❌ <b>API Error</b>\n\n{html.escape(str(error_msg))}",
                parse_mode="HTML",
                reply_markup=create_inline_keyboard()
            )
            return
        
        formatted_response = format_json_to_markdown(cleaned_response)
        
        header = (
            f"🔍 <b>Lookup Result</b>\n"
            f"📱 <b>Number:</b> <code>{html.escape(number)}</code>\n"
            f"{'─' * 30}\n\n"
        )
        
        final_message = header + formatted_response
        message_parts = split_message(final_message)
        
        await processing_msg.delete()
        
        for i, part in enumerate(message_parts):
            if i == 0:
                await update.message.reply_text(
                    part,
                    parse_mode="Markdown",
                    reply_markup=create_inline_keyboard(),
                    disable_web_page_preview=True
                )
            else:
                await update.message.reply_text(
                    part,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
        
        logger.info(f"Successfully processed lookup for number: {number}")
        
    except Exception as e:
        logger.error(f"Unhandled error in number_command: {e}", exc_info=True)
        try:
            await update.message.reply_html(
                "❌ <b>Error</b>\n\nAn unexpected error occurred. Please try again later.",
                reply_markup=create_inline_keyboard()
            )
        except:
            pass


async def handle_other_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all other messages silently."""
    pass


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for the bot."""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_html(
                "❌ <b>Error</b>\n\nAn unexpected error occurred. Please try again later.",
                reply_markup=create_inline_keyboard()
            )
    except:
        pass


# ==================== MAIN APPLICATION ====================

async def run_bot() -> None:
    """Run the bot asynchronously."""
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("number", number_command))
    
    # Add message handler for all other messages (silent ignore)
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_other_messages)
    )
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Log startup information
    logger.info("=" * 60)
    logger.info("TraceX Free Bot v2.0.1 is starting...")
    logger.info(f"Bot username: @{BOT_USERNAME}")
    logger.info(f"API URL: {API_URL}")
    logger.info("=" * 60)
    logger.info("✅ Features:")
    logger.info("   • JSON Markdown Formatting with ```json code blocks")
    logger.info("   • Clean JSON output for better Telegram display")
    logger.info("   • Removed developer branding from responses")
    logger.info("   • /start command with welcome message")
    logger.info("   • Fixed for Python 3.14+ compatibility")
    logger.info("   • Optimized for Render deployment")
    logger.info("=" * 60)
    
    # Start the bot
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    """Main function to start the bot."""
    try:
        # For Python 3.14+, we need to handle event loops differently
        try:
            # Try to get or create event loop
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, create one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # Run the bot
        loop.run_until_complete(run_bot())
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
