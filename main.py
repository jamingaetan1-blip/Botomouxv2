import discord
from discord.ext import commands
import pytesseract
from PIL import Image, ImageOps, ImageFilter
import requests
from io import BytesIO
import re
import json
import os
from collections import defaultdict

# ---------------- CONFIG ---------------- #
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

sessions = {}

# Langue pour Tesseract. "fra" aide sur les mots comme "Rune"/"Kamas",
# mais nécessite le paquet tesseract-ocr-fra installé sur l'hôte.
# Si le paquet n'est pas dispo, remplace par "eng" (ça n'empêchera pas
# la lecture des chiffres, juste un peu moins bon sur le texte).
TESS_LANG = "fra"

# Repère les lignes du type : "100 x [Rune Cri] (235 999 kamas)"
LINE_PATTERN = re.compile(
    r'(\d[\d\s]{0,6})\s*x\s*\[([^\]]{2,40})\]\s*\(?\s*(\d[\d\s]{2,12})\s*kamas\)?',
    re.IGNORECASE
)

# ---------------- DATA ---------------- #
def save_data():
    with open("data.json", "w") as f:
        json.dump(sessions, f)

def load_data():
    global sessions
    try:
        with open("data.json", "r") as f:
            sessions = json.load(f)
    except Exception:
        sessions = {}

# ---------------- PRETRAITEMENT IMAGE ---------------- #
def preprocess_image(img: Image.Image, scale: int = 3) -> Image.Image:
    """
    Ameliore la lisibilite d'une capture avant l'OCR :
    - passage en niveaux de gris
    - agrandissement (les petits caracteres sont la 1ere cause de
      confusion 6/8, 0/8, 5/6...)
    - renforcement du contraste + nettete
    - seuillage noir/blanc pour detacher nettement le texte du fond
    """
    img = img.convert("L")
    w, h = img.size
    img = img.resize((w * scale, h * scale), Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p > 150 else 0)
    return img

def clean_number(raw: str) -> int:
    cleaned = raw.replace(" ", "").replace("\u202f", "").replace("\xa0", "")
    return int(cleaned)

# ---------------- EXTRACTION ---------------- #
def extract_kamas(text):
    matches = re.findall(r'(\d[\d\s]*)\s*kamas', text, re.IGNORECASE)
    values = []
    for match in matches:
        try:
            value = clean_number(match)
        except ValueError:
            continue
        if value > 1000:  # filtre anti erreur OCR
            values.append(value)
    return values

def extract_runes(text):
    """
    Retourne une liste de dicts {name, qty, price} pour chaque ligne
    du type '100 x [Rune Cri] (235 999 kamas)' trouvee dans le texte OCR.
    """
    results = []
    for m in LINE_PATTERN.finditer(text):
        raw_qty, name, raw_price = m.groups()
        try:
            qty = clean_number(raw_qty)
            price = clean_number(raw_price)
        except ValueError:
            continue
        results.append({
            "name": re.sub(r'\s+', ' ', name.strip()),
            "qty": qty,
            "price": price,
        })
    return results

def format_number(n):
    return f"{n:,}".replace(",", " ")

async def process_image(url):
    response = requests.get(url)
    raw_img = Image.open(BytesIO(response.content))
    img = preprocess_image(raw_img)

    # --psm 6 = on suppose un bloc de texte uniforme (liste de lignes).
    # Si les captures ont une mise en page differente, essaie --psm 4.
    config = "--psm 6"
    text = pytesseract.image_to_string(img, lang=TESS_LANG, config=config)

    values = extract_kamas(text)
    runes = extract_runes(text)
    return values, runes

def empty_session():
    return {
        "total": 0,
        "users": {},
        "runes": {},
        "active": True
    }

# ---------------- EMBED RUNES ---------------- #
def build_rune_embeds(runes, title="Detail des runes"):
    """
    Construit une (ou plusieurs, si +25 runes) liste d'embeds Discord,
    un champ par rune en mode inline -> Discord les affiche automatiquement
    en grille propre, sans les soucis d'alignement des blocs de code
    (qui cassent sur mobile faute de defilement horizontal).
    """
    if not runes:
        return []

    embeds = []
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    field_count = 0

    for name, vals in runes.items():
        qty = vals["qty"]
        total = vals["price"]
        avg = round(total / qty) if qty else 0
        value = (
            f"Qte : **{qty}**\n"
            f"Total : **{format_number(total)}** kamas\n"
            f"Moyenne : **{format_number(avg)}** kamas/u"
        )

        if field_count == 25:
            embeds.append(embed)
            embed = discord.Embed(title=title, color=discord.Color.blurple())
            field_count = 0

        embed.add_field(name=name, value=value, inline=True)
        field_count += 1

    embeds.append(embed)
    return embeds

async def send_rune_embeds(send_func, runes, title="Detail des runes"):
    for embed in build_rune_embeds(runes, title=title):
        await send_func(embed=embed)

# ---------------- COMMANDES ---------------- #
@bot.tree.command(name="fmstart", description="Demarrer une session FM")
async def fmstart(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    sessions[channel_id] = empty_session()
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
    runes = session.get("runes", {})

    if users:
        resume = "\n".join(
            [f"<@{uid}> : {format_number(val)}" for uid, val in users.items()]
        )
    else:
        resume = "Aucune donnee."

    message_out = f"Resume final\n\n{resume}\n\nTOTAL : {format_number(total)} kamas"

    del sessions[channel_id]
    save_data()

    await interaction.response.send_message(message_out)
    if runes:
        await send_rune_embeds(interaction.followup.send, runes)

@bot.tree.command(name="fmreset", description="Reinitialiser la session")
async def fmreset(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    sessions[channel_id] = empty_session()
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
    runes = session.get("runes", {})

    if users:
        resume = "\n".join(
            [f"<@{uid}> : {format_number(val)}" for uid, val in users.items()]
        )
    else:
        resume = "Aucune donnee."

    message_out = f"Etat actuel\n\n{resume}\n\nTOTAL : {format_number(total)} kamas"

    await interaction.response.send_message(message_out)
    if runes:
        await send_rune_embeds(interaction.followup.send, runes)

@bot.command()
async def sync(ctx):
    await bot.tree.sync()
    await ctx.send("Commandes synchronisees !")

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

    if "runes" not in session:
        session["runes"] = {}

    if message.attachments:
        added_total = 0
        details = []
        rune_totals = defaultdict(lambda: {"qty": 0, "price": 0})

        for attachment in message.attachments:
            if attachment.filename.lower().endswith(("png", "jpg", "jpeg")):
                values, runes = await process_image(attachment.url)

                for v in values:
                    added_total += v
                    details.append(v)

                for r in runes:
                    rune_totals[r["name"]]["qty"] += r["qty"]
                    rune_totals[r["name"]]["price"] += r["price"]

        if added_total > 0:
            session["total"] += added_total
            user_id = str(message.author.id)
            if user_id not in session["users"]:
                session["users"][user_id] = 0
            session["users"][user_id] += added_total

        for name, vals in rune_totals.items():
            if name not in session["runes"]:
                session["runes"][name] = {"qty": 0, "price": 0}
            session["runes"][name]["qty"] += vals["qty"]
            session["runes"][name]["price"] += vals["price"]

        if added_total > 0 or rune_totals:
            save_data()

        reply_parts = []

        if rune_totals:
            rune_lines = "\n".join(
                f"Rune {name} x{vals['qty']} {format_number(vals['price'])} Kamas"
                for name, vals in rune_totals.items()
            )
            reply_parts.append(rune_lines)

        if details:
            detail_text = "\n".join([f"+ {format_number(v)}" for v in details])
            reply_parts.append(
                f"Capture traitee :\n{detail_text}\n\n"
                f"{message.author.mention} -> +{format_number(added_total)}\n"
                f"Total global : {format_number(session['total'])}"
            )

        if reply_parts:
            await message.reply("\n\n".join(reply_parts))

    await bot.process_commands(message)

# ---------------- RUN ---------------- #
TOKEN = os.environ.get("TOKEN")
bot.run(TOKEN)