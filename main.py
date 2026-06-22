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

@bot.tree.command(name="fmstart", description="Demarrer une session FM")
async def fmstart(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)

    sessions[channel_id] = {
        "total": 0,
        "users": {},
        "active": True
    }

    save_data()
    await interaction.response.send_message("Session FM demarree !")

@bot.tree.command(name="fmstop", description="Arreter la session et afficher le resume")
async def fmstop(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)

    if channel_id not in sessions:
        return await interaction.response.send_message("Aucune session active.")

    session = sessions[channel_id]

    total = session["total"]
    users = session["users"]

    if users:
        resume = "\n".join(
            [f"<@{uid}> : {format_number(val)}" for uid, val in users.items()]
        )
    else:
        resume = "Aucune donnee."

    del sessions[channel_id]
    save_data()

    await interaction.response.send_message(
        f"Resume final\n\n{resume}\n\n"
        f"TOTAL : {format_number(total)} kamas"
    )

@bot.tree.command(name="fmreset", description="Reinitialiser la session")
async def fmreset(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)

    sessions[channel_id] = {
        "total": 0,
        "users": {},
        "active": True
    }

    save_data()
    await interaction.response.send_message("Session reinitialisee !")

@bot.tree.command(name="fmtotal", description="Voir le total actuel")
async def fmtotal(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)

    if channel_id not in sessions:
        return await interaction.response.send_message("Aucune session active.")

    session = sessions[channel_id]

    total = session["total"]
    users = session["users"]

    if users:
        resume = "\n".join(
            [f"<@{uid}> : {format_number(val)}" for uid, val in users.items()]
        )
    else:
        resume = "Aucune donnee."

    await interaction.response.send_message(
        f"Etat actuel\n\n{resume}\n\n"
        f"TOTAL : {format_number(total)} kamas"
    )

# ---------------- EVENTS ---------------- #

@bot.event
async def on_ready():
    load_data()
    await bot.tree.sync()
    print(f"Bot connecte en tant que {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = str(message.channel.id)

    if channel_id not in sessions:
        return

    session = sessions[channel_id]

    if not session["active"]:
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
            session["total"] += added_total

            user_id = str(message.author.id)

            if user_id not in session["users"]:
                session["users"][user_id] = 0

            session["users"][user_id] += added_total

            save_data()

            detail_text = "\n".join(
                [f"+ {format_number(v)}" for v in details]
            )

            await message.reply(
                f"Capture traitee :\n{detail_text}\n\n"
                f"{message.author.mention} -> +{format_number(added_total)}\n"
                f"Total global : {format_number(session['total'])}"
            )

    await bot.process_commands(message)

# ---------------- RUN ---------------- #

TOKEN = os.environ.get("TOKEN")
bot.run(TOKEN)