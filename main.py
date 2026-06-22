import discord
from discord.ext import commands
import pytesseract
from PIL import Image
import requests
from io import BytesIO
import re
import json
import os

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
        if value > 1000:  # filtre anti erreurs OCR
            values.append(value)

    return values

def format_number(n):
    return f"{n:,}".replace(",", " ")

async def process_image(url):
    response = requests.get(url)
    img = Image.open(BytesIO(response.content))

    text = pytesseract.image_to_string(img)

    values = extract_kamas(text)

    return values

# ---------------- COMMANDES ---------------- #

@bot.tree.command(name="start")
async def start(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)

    sessions[channel_id] = {
        "total": 0,
        "active": True
    }

    save_data()

    await interaction.response.send_message("✅ Session démarrée !")

@bot.tree.command(name="stop")
async def stop(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)

    if channel_id not in sessions:
        return await interaction.response.send_message("❌ Aucune session.")

    total = sessions[channel_id]["total"]

    del sessions[channel_id]

    save_data()

    await interaction.response.send_message(
        f"🧾 Total final : **{format_number(total)}** kamas"
    )

# ---------------- EVENTS ---------------- #

@bot.event
async def on_ready():
    load_data()
    await bot.tree.sync()
    print(f"✅ Connecté en tant que {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = str(message.channel.id)

    if channel_id not in sessions:
        return

    if not sessions[channel_id]["active"]:
        return

    if message.attachments:
        added_total = 0
        details = []

        for attachment in message.attachments:
            if attachment.filename.lower().endswith(("png", "jpg", "jpeg")):
                values = await process_image(attachment.url)

                for v in values:
                    added_total += v
                    details.append(v)

        if added_total > 0:
            sessions[channel_id]["total"] += added_total
            save_data()

            detail_text = "\n".join(
                [f"+ {format_number(v)}" for v in details]
            )

            await message.reply(
                f"📸 Capture traitée :\n{detail_text}\n\n"
                f"💰 Ajout : **{format_number(added_total)}**\n"
                f"📊 Total : **{format_number(sessions[channel_id]['total'])}**"
            )

    await bot.process_commands(message)

# ---------------- RUN ---------------- #

TOKEN = os.environ.get("TOKEN")
bot.run(TOKEN)