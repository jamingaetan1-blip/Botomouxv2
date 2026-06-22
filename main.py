import discord
from discord.ext import commands
import pytesseract
from PIL import Image
import requests
from io import BytesIO
import re
import json
import os

# ---------------- CONFIG ---------------- #

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = {}

# ---------------- DATA ---------------- #

def save_data():
    with open("data.json", "w") as f:
        json.dump(sessions, f)

def load_data():
    global sessions
    try:
        with open("data.json", "r") as f:
            sessions = json.load(f)
    except:
        sessions = {}

# ---------------- UTILS ---------------- #

def extract_kamas(text):
    matches = re.findall(r'(\d[\d\s]*)\s*kamas', text, re.IGNORECASE)

    values = []
    for match in matches:
        clean = match.replace(" ", "")
        value = int(clean)

        if value > 1000:  # filtre anti erreur OCR
            values.append(value)

    return values

def format_number(n):
    return f"{n:,}".replace(",", " ")

async def process_image(url):
    response = requests.get(url)
    img = Image.open(BytesIO(response.content))

    text = pytesseract.image_to_string(img)

    return extract_kamas(text)

# ---------------- COMMANDES ---------------- #

@bot.tree.command(name="fmstart", description="Démarrer une session FM")
