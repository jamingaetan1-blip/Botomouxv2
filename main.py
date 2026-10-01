import discord
from discord.ext import commands, tasks
import pytesseract
from PIL import Image, ImageOps, ImageFilter
import requests
from io import BytesIO
import asyncio
import datetime
import re
import json
import os
import unicodedata
from collections import defaultdict

# ---------------- CONFIG ---------------- #
intents = discord.Intents.default()
intents.message_content = True
# Facultatif : archiver les salons FM d'un joueur qui quitte le serveur. Demande d'activer
# "Server Members Intent" dans le portail developpeur Discord, puis FM_MEMBERS_INTENT=1.
intents.members = os.environ.get("FM_MEMBERS_INTENT") == "1"
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

# ---------------- CONFIG DOFUS CRAFT (facultative) ---------------- #
# Envoi des sessions au site Dofus Craft. Sans ces deux variables, le bot
# fonctionne exactement comme avant : rien n'est envoye nulle part.
SITE_URL = os.environ.get("DOFUS_CRAFT_API_URL", "").rstrip("/")
SITE_KEY = os.environ.get("FM_BOT_API_KEY", "")
SITE_ENABLED = bool(SITE_URL and SITE_KEY)

# Salons FM personnels : actives par FM_PERSONAL_CHANNELS=1. Sur chaque serveur ou il se
# trouve, le bot cree alors lui-meme les categories "Forgemagie" et "Archives FM" et le salon
# d'accueil #forgemagie (ou reprend ceux qui existent deja sous ces noms). Sinon, les
# commandes /fm... marchent comme avant.
def _int_env(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default

FM_PERSONAL_CHANNELS = os.environ.get("FM_PERSONAL_CHANNELS") == "1"
FM_MAX_ACTIVE_CHANNELS = _int_env("FM_MAX_ACTIVE_CHANNELS", 5)
FM_INACTIVITY_DAYS = _int_env("FM_INACTIVITY_DAYS", 14)

FM_CATEGORY_NAME = "Forgemagie"
FM_ARCHIVE_CATEGORY_NAME = "Archives FM"
FM_WELCOME_CHANNEL_NAME = "forgemagie"

MAX_CHANNEL_NAME = 30
NEW_SESSION_BUTTON_ID = "fm:new-session"

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

# Les salons FM personnels sont ranges dans data.json sous la cle "_fm" (jamais un
# identifiant de salon), pour survivre aux redemarrages avec le volume docker actuel.
def fm_state():
    state = sessions.setdefault("_fm", {})
    state.setdefault("channels", {})
    return state

def fm_channels():
    return fm_state()["channels"]

def now_iso():
    return discord.utils.utcnow().isoformat()

def touch_channel(channel_id):
    entry = fm_channels().get(str(channel_id))
    if entry and entry.get("status") == "active":
        entry["last_activity"] = now_iso()

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
    cleaned = raw.replace(" ", "").replace(" ", "").replace("\xa0", "")
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

# Texte du resume d'une session, commun a /fmstop, /fmtotal, a la fermeture
# demandee par le site et a /fmarchive.
def session_summary(session, title):
    users = session["users"]
    if users:
        resume = "\n".join(
            [f"<@{uid}> : {format_number(val)}" for uid, val in users.items()]
        )
    else:
        resume = "Aucune donnee."
    return f"{title}\n\n{resume}\n\nTOTAL : {format_number(session['total'])} kamas"

# Ferme la session d'un salon en publiant le meme resume que /fmstop.
async def close_session_in_channel(channel):
    channel_id = str(channel.id)
    session = sessions.pop(channel_id, None)
    save_data()
    if session is None:
        return False
    await channel.send(session_summary(session, "Resume final"))
    if session.get("runes"):
        await send_rune_embeds(channel.send, session["runes"])
    return True

# ---------------- SITE DOFUS CRAFT ---------------- #
# Chaque envoi part en arriere-plan : le bot ne bloque jamais, meme si le site
# est lent ou en panne. Un envoi rate est retente plus tard ; le site ignore les
# doublons grace a eventId (l'identifiant du message ou de l'interaction Discord).
pending_events = []
handled_commands = set()

def _site_request(method, path, payload=None):
    return requests.request(
        method,
        f"{SITE_URL}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {SITE_KEY}"},
        timeout=10,
    )

async def site_request(method, path, payload=None):
    if not SITE_ENABLED:
        return None
    try:
        return await asyncio.to_thread(_site_request, method, path, payload)
    except Exception as error:
        print(f"[Dofus Craft] {method} {path} a echoue : {error}")
        return None

def site_event(event_type, event_id, channel, occurred_at=None, **fields):
    return {
        "type": event_type,
        "eventId": str(event_id),
        "channelId": str(channel.id),
        "channelName": channel.name,
        "occurredAt": (occurred_at or discord.utils.utcnow()).isoformat(),
        **fields,
    }

def site_author(user):
    return {
        "id": str(user.id),
        "displayName": user.display_name,
        "avatarUrl": str(user.display_avatar.url),
    }

async def send_event(payload):
    if not SITE_ENABLED:
        return
    response = await site_request("POST", "/fm/bot/events", payload)
    if response is None or response.status_code >= 500:
        pending_events.append(payload)
    elif response.status_code >= 400:
        print(f"[Dofus Craft] evenement refuse ({response.status_code}) : {response.text[:200]}")

async def flush_pending_events():
    while pending_events:
        payload = pending_events[0]
        response = await site_request("POST", "/fm/bot/events", payload)
        if response is None or response.status_code >= 500:
            return
        pending_events.pop(0)

async def ack_command(command_id, status, error=None, channel_id=None):
    body = {"status": status}
    if error:
        body["error"] = error[:200]
    if channel_id:
        body["channelId"] = str(channel_id)
    response = await site_request("POST", f"/fm/bot/commands/{command_id}/ack", body)
    return response is not None and response.status_code in (200, 404)

# ---------------- SALONS FM PERSONNELS ---------------- #
def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

def active_channels_of(user_id):
    return [
        channel_id for channel_id, entry in fm_channels().items()
        if entry.get("owner_id") == str(user_id) and entry.get("status") == "active"
    ]

class ChannelError(Exception):
    """Une creation de salon impossible, avec le message a montrer au joueur."""

# Les emplacements des salons FM d'un serveur (categories, salon et message d'accueil),
# gardes dans data.json par serveur.
def guild_state(guild):
    state = fm_state()
    guilds = state.setdefault("guilds", {})
    # Ancien format (un seul serveur) : les emplacements sont ranges sous leur serveur.
    if "category_id" in state or "welcome_channel_id" in state:
        old = {key: state.pop(key) for key in list(state) if key in PLACE_KEYS}
        channel = bot.get_channel(int(old.get("welcome_channel_id") or old.get("category_id") or 0))
        if channel is not None:
            guilds.setdefault(str(channel.guild.id), old)
    return guilds.setdefault(str(guild.id), {})

PLACE_KEYS = ("category_id", "archive_category_id", "welcome_channel_id", "welcome_message_id", "welcome_version", "welcome_emoji")

# Un des emplacements des salons FM d'un serveur, retrouve par l'identifiant garde dans data.json.
def fm_place(guild, key, kind):
    channel = bot.get_channel(int(guild_state(guild).get(key) or 0))
    if not isinstance(channel, kind) or channel.guild != guild:
        return None
    return channel

async def ensure_fm_places(guild):
    """Trouve ou cree, sur ce serveur, les categories Forgemagie et Archives FM et le salon d'accueil."""
    state = guild_state(guild)
    try:
        category = fm_place(guild, "category_id", discord.CategoryChannel)
        if category is None:
            category = (discord.utils.get(guild.categories, name=FM_CATEGORY_NAME)
                        or await guild.create_category(FM_CATEGORY_NAME))
            state["category_id"] = str(category.id)

        if fm_place(guild, "archive_category_id", discord.CategoryChannel) is None:
            archive = (discord.utils.get(guild.categories, name=FM_ARCHIVE_CATEGORY_NAME)
                       or await guild.create_category(FM_ARCHIVE_CATEGORY_NAME))
            state["archive_category_id"] = str(archive.id)

        if fm_place(guild, "welcome_channel_id", discord.TextChannel) is None:
            welcome = discord.utils.get(guild.text_channels, name=FM_WELCOME_CHANNEL_NAME)
            if welcome is None:
                # Lecture seule pour tout le monde : on n'y fait que cliquer sur le bouton.
                welcome = await guild.create_text_channel(
                    FM_WELCOME_CHANNEL_NAME, category=category, position=0,
                    topic="Clique sur le bouton pour creer ton salon FM prive.",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(
                            view_channel=True, read_message_history=True, send_messages=False,
                            add_reactions=False, create_public_threads=False, create_private_threads=False,
                        ),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
                        ),
                    },
                )
            state["welcome_channel_id"] = str(welcome.id)
    except discord.Forbidden:
        print(f"[Salons FM] Sur le serveur \"{guild.name}\", le bot n'a pas la permission \"Gerer les salons\" : salons FM desactives")
        return False
    finally:
        save_data()
    return True

# Met en place les salons FM d'un serveur : au demarrage, et quand le bot rejoint un serveur.
async def setup_guild(guild):
    if not await ensure_fm_places(guild):
        return
    try:
        await ensure_welcome_message(guild)
    except discord.HTTPException as error:
        print(f"[Salons FM] Message d'accueil impossible sur \"{guild.name}\" : {error}")

async def create_fm_channel(guild, member, raw_name, command_id=None):
    if not FM_PERSONAL_CHANNELS:
        raise ChannelError("Les salons FM personnels ne sont pas actives sur ce serveur.")
    name = slugify(raw_name.strip()[:MAX_CHANNEL_NAME])
    if not name:
        raise ChannelError("Ce nom de salon n'est pas valable : utilise des lettres ou des chiffres.")
    if len(active_channels_of(member.id)) >= FM_MAX_ACTIVE_CHANNELS:
        raise ChannelError(
            f"Tu as deja {FM_MAX_ACTIVE_CHANNELS} salons FM actifs : archives-en un avec /fmarchive."
        )
    # Categorie supprimee entre-temps : le bot la recree.
    category = fm_place(guild, "category_id", discord.CategoryChannel)
    if category is None and await ensure_fm_places(guild):
        category = fm_place(guild, "category_id", discord.CategoryChannel)
    if category is None:
        raise ChannelError("La categorie Forgemagie n'a pas pu etre creee.")

    base = f"fm-{slugify(member.display_name) or member.id}-{name}"[:90]
    existing = {channel.name for channel in category.channels}
    channel_name, suffix = base, 2
    while channel_name in existing:
        channel_name, suffix = f"{base}-{suffix}", suffix + 1

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            read_message_history=True, use_application_commands=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True,
            read_message_history=True, manage_channels=True,
        ),
    }
    channel = await guild.create_text_channel(
        channel_name, category=category, overwrites=overwrites,
        topic=f"Salon FM de {member.display_name}",
    )

    fm_channels()[str(channel.id)] = {
        "owner_id": str(member.id),
        "status": "active",
        "created_at": now_iso(),
        "last_activity": now_iso(),
    }
    sessions[str(channel.id)] = empty_session()
    save_data()

    await channel.send(embed=channel_welcome_embed())
    created = {"author": site_author(member)}
    if command_id:
        created["commandId"] = command_id
    await send_event(site_event("channel-created", f"{channel.id}-created", channel, **created))
    await send_event(site_event("session-start", f"{channel.id}-start", channel, author=site_author(member)))
    return channel

async def archive_fm_channel(channel, message):
    entry = fm_channels().get(str(channel.id))
    if not entry or entry.get("status") != "active":
        return
    # Un salon peut etre rouvert puis archive de nouveau : chaque archivage a ses propres identifiants
    # d'evenement, sinon le site prendrait le second pour un doublon du premier.
    archived_at = now_iso()
    if await close_session_in_channel(channel):
        await send_event(site_event("session-stop", f"{channel.id}-archive-stop-{archived_at}", channel))

    guild = channel.guild
    overwrites = dict(channel.overwrites)
    owner = guild.get_member(int(entry["owner_id"]))
    if owner is not None:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    archive = fm_place(guild, "archive_category_id", discord.CategoryChannel)
    if archive is None and await ensure_fm_places(guild):
        archive = fm_place(guild, "archive_category_id", discord.CategoryChannel)
    await channel.edit(
        category=archive or channel.category,
        overwrites=overwrites,
    )
    entry["status"] = "archived"
    entry["archived_at"] = archived_at
    save_data()
    await channel.send(message)
    await send_event(site_event("channel-archived", f"{channel.id}-archived-{archived_at}", channel))

# Rouvre un salon FM archive, a la demande du site (« Rouvrir » une seance de FM) : le meme salon
# revient dans la categorie Forgemagie et son proprietaire peut de nouveau y ecrire. La session est
# ouverte ensuite par la demande « open » du site.
async def reopen_fm_channel(channel):
    entry = fm_channels().get(str(channel.id))
    if not entry:
        raise ChannelError("Ce salon n'est pas un salon FM.")
    if entry.get("status") == "active":
        return
    if len(active_channels_of(entry["owner_id"])) >= FM_MAX_ACTIVE_CHANNELS:
        raise ChannelError(
            f"Tu as deja {FM_MAX_ACTIVE_CHANNELS} salons FM actifs : archives-en un avec /fmarchive."
        )
    guild = channel.guild
    category = fm_place(guild, "category_id", discord.CategoryChannel)
    if category is None and await ensure_fm_places(guild):
        category = fm_place(guild, "category_id", discord.CategoryChannel)

    overwrites = dict(channel.overwrites)
    owner = guild.get_member(int(entry["owner_id"]))
    if owner is not None:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            read_message_history=True, use_application_commands=True,
        )
    await channel.edit(category=category or channel.category, overwrites=overwrites)
    reopened_at = now_iso()
    entry["status"] = "active"
    entry.pop("archived_at", None)
    # Sans cela, l'archivage automatique pour inactivite le refermerait aussitot.
    entry["last_activity"] = reopened_at
    save_data()
    await channel.send("Salon rouvert : la seance de FM reprend.")
    await send_event(site_event("channel-reopened", f"{channel.id}-reopened-{reopened_at}", channel))

class ChannelNameModal(discord.ui.Modal, title="Nouvelle séance de FM"):
    channel_name = discord.ui.TextInput(
        label="Nom du salon",
        placeholder="Par exemple : mon rtograf",
        min_length=1,
        max_length=MAX_CHANNEL_NAME,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await create_fm_channel(interaction.guild, interaction.user, str(self.channel_name))
        except ChannelError as error:
            return await interaction.followup.send(str(error), ephemeral=True)
        await interaction.followup.send(f"Ton salon est pret : {channel.mention}", ephemeral=True)

# Emoji du bouton : l'icone de la Rune Ga Pa, enregistree comme emoji de l'application
# (servie par le site Dofus Craft ; FM_BUTTON_EMOJI_URL permet d'en changer).
BUTTON_EMOJI_NAME = "rune_ga_pa"
BUTTON_EMOJI_URL = os.environ.get("FM_BUTTON_EMOJI_URL", "https://dofus-craft.dofus-craft.workers.dev/rune-ga-pa.png")
button_emoji = "🔨"

async def ensure_button_emoji():
    """Retrouve ou cree l'emoji du bouton ; garde le marteau si c'est impossible."""
    global button_emoji
    try:
        for emoji in await bot.fetch_application_emojis():
            if emoji.name == BUTTON_EMOJI_NAME:
                button_emoji = emoji
                return
        response = await asyncio.to_thread(requests.get, BUTTON_EMOJI_URL, timeout=10)
        response.raise_for_status()
        button_emoji = await bot.create_application_emoji(name=BUTTON_EMOJI_NAME, image=response.content)
    except (discord.HTTPException, requests.RequestException) as error:
        print(f"[Salons FM] Emoji du bouton indisponible, marteau utilise : {error}")

class NewSessionView(discord.ui.View):
    # Bouton "persistant" : il reste actif apres un redemarrage du bot.
    def __init__(self):
        super().__init__(timeout=None)
        self.new_session.emoji = button_emoji

    @discord.ui.button(label="Nouvelle séance de FM", style=discord.ButtonStyle.success, custom_id=NEW_SESSION_BUTTON_ID)
    async def new_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(active_channels_of(interaction.user.id)) >= FM_MAX_ACTIVE_CHANNELS:
            return await interaction.response.send_message(
                f"Tu as deja {FM_MAX_ACTIVE_CHANNELS} salons FM actifs : archives-en un avec /fmarchive.",
                ephemeral=True,
            )
        await interaction.response.send_modal(ChannelNameModal())

# Version du message d'accueil : a augmenter quand son texte change, pour que le bot
# mette a jour le message deja publie au lieu d'en poster un nouveau.
WELCOME_VERSION = 5

# Apparence du message d'accueil : bleu du Zaap et une illustration du Zaap (servie
# par le site Dofus Craft ; FM_WELCOME_IMAGE_URL permet d'en changer sans toucher au code).
WELCOME_COLOR = discord.Color.from_rgb(54, 169, 225)
WELCOME_IMAGE = os.environ.get("FM_WELCOME_IMAGE_URL", "https://dofus-craft.dofus-craft.workers.dev/fm-banner.jpg")
SITE_PAGE = "https://dofus-craft.dofus-craft.workers.dev/forgemagie"

# Message poste dans un salon FM qui vient d'etre cree : c'est la que les commandes servent.
def channel_welcome_embed():
    # Les commandes d'abord (c'est ce qu'on revient chercher), puis quoi faire.
    embed = discord.Embed(
        title="Ton salon FM est prêt",
        description=(
            "**Commandes**\n"
            "`/fmtotal` · total en cours\n"
            "`/fmstop` · fermer la session et voir le résumé\n"
            "`/fmstart` · nouvelle session\n"
            "`/fmreset` · remettre à zéro\n"
            "`/fmarchive` · archiver le salon, FM terminée\n\n"
            "Ta session est démarrée : poste ici tes **captures du chat** après tes achats à l'HDV."
        ),
        color=WELCOME_COLOR,
    )
    return embed

def welcome_embed():
    # Volontairement court : les commandes sont expliquees dans le salon cree.
    embed = discord.Embed(
        title="🔨  Forgemagie",
        description=(
            "Clique sur **Nouvelle séance de FM** pour créer ton salon privé : "
            "seuls toi et le bot le voient, et ta session démarre aussitôt.\n\n"
            f"[Tes séances sur le site Dofus Craft]({SITE_PAGE})"
        ),
        color=WELCOME_COLOR,
    )
    if WELCOME_IMAGE:
        embed.set_image(url=WELCOME_IMAGE)
    embed.set_footer(text="Illustration © Ankama")
    return embed

async def ensure_welcome_message(guild):
    channel = fm_place(guild, "welcome_channel_id", discord.TextChannel)
    if channel is None:
        return
    state = guild_state(guild)
    message_id = state.get("welcome_message_id")
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            if state.get("welcome_version") != WELCOME_VERSION or state.get("welcome_emoji") != str(button_emoji):
                await message.edit(embed=welcome_embed(), view=NewSessionView())
                state["welcome_version"] = WELCOME_VERSION
                state["welcome_emoji"] = str(button_emoji)
                save_data()
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    message = await channel.send(embed=welcome_embed(), view=NewSessionView())
    state["welcome_message_id"] = str(message.id)
    state["welcome_version"] = WELCOME_VERSION
    state["welcome_emoji"] = str(button_emoji)
    save_data()

# ---------------- COMMANDES ---------------- #
@bot.tree.command(name="fmstart", description="Demarrer une session FM")
async def fmstart(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    sessions[channel_id] = empty_session()
    touch_channel(channel_id)
    save_data()
    await interaction.response.send_message("Session FM demarree !")
    await send_event(site_event(
        "session-start", interaction.id, interaction.channel, interaction.created_at,
        author=site_author(interaction.user),
    ))

@bot.tree.command(name="fmstop", description="Arreter la session et afficher le resume")
async def fmstop(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    if channel_id not in sessions:
        return await interaction.response.send_message("Aucune session active.")

    session = sessions[channel_id]
    runes = session.get("runes", {})
    message_out = session_summary(session, "Resume final")

    del sessions[channel_id]
    touch_channel(channel_id)
    save_data()

    await interaction.response.send_message(message_out)
    if runes:
        await send_rune_embeds(interaction.followup.send, runes)
    await send_event(site_event("session-stop", interaction.id, interaction.channel, interaction.created_at))

@bot.tree.command(name="fmreset", description="Reinitialiser la session")
async def fmreset(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    sessions[channel_id] = empty_session()
    touch_channel(channel_id)
    save_data()
    await interaction.response.send_message("Session reinitialisee !")
    await send_event(site_event("session-reset", interaction.id, interaction.channel, interaction.created_at))

@bot.tree.command(name="fmtotal", description="Voir le total actuel")
async def fmtotal(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    if channel_id not in sessions:
        return await interaction.response.send_message("Aucune session active.")

    session = sessions[channel_id]
    runes = session.get("runes", {})
    message_out = session_summary(session, "Etat actuel")

    await interaction.response.send_message(message_out)
    if runes:
        await send_rune_embeds(interaction.followup.send, runes)

@bot.tree.command(name="fmarchive", description="Archiver ce salon FM (fin de la FM de l'objet)")
async def fmarchive(interaction: discord.Interaction):
    entry = fm_channels().get(str(interaction.channel.id))
    if not entry or entry.get("status") != "active":
        return await interaction.response.send_message("Ce salon n'est pas un salon FM actif.", ephemeral=True)
    if entry.get("owner_id") != str(interaction.user.id):
        return await interaction.response.send_message("Seul le proprietaire du salon peut l'archiver.", ephemeral=True)
    await interaction.response.send_message("Archivage du salon...")
    await archive_fm_channel(interaction.channel, "Salon archive : il reste consultable en lecture seule.")

@bot.command()
async def sync(ctx):
    await bot.tree.sync()
    await ctx.send("Commandes synchronisees !")

# ---------------- TACHES DE FOND ---------------- #
# Toutes les 10 secondes : les demandes du site (ouvrir / fermer une session,
# creer, archiver ou rouvrir un salon), puis les envois qui avaient echoue.
@tasks.loop(seconds=10)
async def poll_site():
    await flush_pending_events()
    response = await site_request("GET", "/fm/bot/commands")
    if response is None or response.status_code != 200:
        return
    data = response.json()
    interval = data.get("pollAfterSeconds")
    if isinstance(interval, int) and interval >= 5 and interval != poll_site.seconds:
        poll_site.change_interval(seconds=interval)

    for command in data.get("commands", []):
        command_id = command["id"]
        if command_id in handled_commands:
            continue
        # Une demande d'un type que ce bot ne connait pas reste en attente : le site l'affiche comme
        # « pas encore fait dans Discord » au lieu de la croire faite.
        if command["type"] not in ("create-channel", "open", "close", "archive-channel", "reopen-channel"):
            continue
        status, error, channel_id = "done", None, None
        try:
            if command["type"] == "create-channel":
                # Le site indique son serveur Discord (celui dont ses joueurs sont membres).
                guild = bot.get_guild(int(command.get("guildId") or 0))
                if guild is None:
                    raise ChannelError("Le bot n'est pas sur le serveur Discord du site.")
                member = await guild.fetch_member(int(command["player"]["id"]))
                channel = await create_fm_channel(guild, member, command["name"], command_id)
                channel_id = channel.id
            else:
                channel = bot.get_channel(int(command["channelId"]))
                if command["type"] == "open":
                    if channel is None:
                        raise ChannelError("Salon introuvable")
                    sessions[str(channel.id)] = empty_session()
                    touch_channel(channel.id)
                    save_data()
                    item = command.get("item")
                    player = command.get("player") or {}
                    await channel.send(
                        f"Session FM demarree depuis le site par {player.get('displayName', 'un joueur')}"
                        + (f" ({item['name']})" if item else "")
                    )
                elif command["type"] == "close" and channel is not None:
                    await close_session_in_channel(channel)
                # « Archiver la seance » sur le site : comme /fmarchive. Salon deja archive ou
                # introuvable : rien a faire.
                elif command["type"] == "archive-channel" and channel is not None:
                    await archive_fm_channel(channel, "Salon archive depuis le site : il reste consultable en lecture seule.")
                # « Rouvrir » sur le site : le meme salon redevient actif.
                elif command["type"] == "reopen-channel":
                    if channel is None:
                        raise ChannelError("Salon introuvable")
                    await reopen_fm_channel(channel)
        except ChannelError as failure:
            status, error = "failed", str(failure)
        except discord.HTTPException as failure:
            status, error = "failed", f"Discord a refuse : {failure.text or failure.status}"
        if await ack_command(command_id, status, error, channel_id):
            handled_commands.add(command_id)
    if len(handled_commands) > 1000:
        handled_commands.clear()

# Toutes les heures : archive les salons FM sans activite depuis FM_INACTIVITY_DAYS jours.
@tasks.loop(hours=1)
async def archive_inactive_channels():
    limit = discord.utils.utcnow() - datetime.timedelta(days=FM_INACTIVITY_DAYS)
    for channel_id, entry in list(fm_channels().items()):
        if entry.get("status") != "active":
            continue
        try:
            last = datetime.datetime.fromisoformat(entry.get("last_activity", entry.get("created_at")))
        except (TypeError, ValueError):
            continue
        channel = bot.get_channel(int(channel_id))
        if last < limit and channel is not None:
            await archive_fm_channel(channel, f"Salon archive automatiquement apres {FM_INACTIVITY_DAYS} jours sans activite.")

# ---------------- EVENTS ---------------- #
@bot.event
async def setup_hook():
    bot.add_view(NewSessionView())

@bot.event
async def on_ready():
    load_data()
    await bot.tree.sync()
    print(f"Bot connecte en tant que {bot.user}")
    if FM_PERSONAL_CHANNELS:
        await ensure_button_emoji()
        for guild in bot.guilds:
            await setup_guild(guild)
        if not archive_inactive_channels.is_running():
            archive_inactive_channels.start()
    if SITE_ENABLED and not poll_site.is_running():
        poll_site.start()

# Le bot est ajoute a un serveur : il y cree ses salons FM.
@bot.event
async def on_guild_join(guild):
    if FM_PERSONAL_CHANNELS:
        await setup_guild(guild)

@bot.event
async def on_member_remove(member):
    for channel_id in active_channels_of(member.id):
        channel = bot.get_channel(int(channel_id))
        if channel is not None and channel.guild == member.guild:
            await archive_fm_channel(channel, f"Salon archive : {member.display_name} a quitte le serveur.")

@bot.event
async def on_guild_channel_delete(channel):
    # Salon d'accueil ou categorie supprime : le bot les recree aussitot.
    places = guild_state(channel.guild)
    if FM_PERSONAL_CHANNELS and str(channel.id) in (
        places.get("welcome_channel_id"), places.get("category_id"), places.get("archive_category_id"),
    ):
        await setup_guild(channel.guild)
        return
    entry = fm_channels().get(str(channel.id))
    if entry:
        entry["status"] = "deleted"
        sessions.pop(str(channel.id), None)
        save_data()
        await send_event(site_event("channel-deleted", f"{channel.id}-deleted", channel))

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
        # Chaque ligne lue, avant regroupement : c'est ce que le site recoit.
        all_lines = []

        for attachment in message.attachments:
            if attachment.filename.lower().endswith(("png", "jpg", "jpeg")):
                values, runes = await process_image(attachment.url)

                for v in values:
                    added_total += v
                    details.append(v)

                for r in runes:
                    rune_totals[r["name"]]["qty"] += r["qty"]
                    rune_totals[r["name"]]["price"] += r["price"]
                    all_lines.append(r)

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
            touch_channel(channel_id)
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
            await send_event(site_event(
                "capture", message.id, message.channel, message.created_at,
                author=site_author(message.author),
                spentKamas=added_total,
                lines=all_lines,
            ))

    await bot.process_commands(message)

# ---------------- RUN ---------------- #
TOKEN = os.environ.get("TOKEN")
bot.run(TOKEN)
