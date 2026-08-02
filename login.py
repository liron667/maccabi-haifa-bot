"""Run once to generate TELETHON_SESSION for your .env.

    python login.py

Enter your phone (with country code, e.g. +9725...) and the code Telegram sends.
Copy the printed TELETHON_SESSION line into .env.
"""
import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id = os.getenv("TELEGRAM_API_ID") or input("API ID: ").strip()
api_hash = os.getenv("TELEGRAM_API_HASH") or input("API HASH: ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    print("\nPaste this into .env:\n")
    print("TELETHON_SESSION=" + client.session.save())
