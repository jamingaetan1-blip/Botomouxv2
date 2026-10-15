import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytesseract
from PIL import Image, ImageOps, ImageStat, features
import requests
from io import BytesIO
import asyncio
import base64
import datetime
import difflib
import html as html_lib
import math
import shutil
import time
import traceback
import re
import json
import os
import unicodedata
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import defaultdict, Counter

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

# Double lecture de chaque capture (la meme image lue a deux echelles differentes). Une capture
# n'est ajoutee automatiquement que si les DEUX lectures donnent exactement les memes lignes.
# Mettre FM_DOUBLE_READ=0 pour la desactiver (non recommande : c'est un filet de securite).
BOT_VERSION = "2026-10-15"   # affichee au demarrage et par !sync : permet de savoir quel code tourne vraiment
DOUBLE_READ = os.environ.get("FM_DOUBLE_READ", "1") != "0"
OCR_CONFIG = "--psm 6"
# Echelles d'agrandissement des deux lectures (mesurees : la paire 3 + 4 est la plus fiable).
READ_SCALES = (3, 4)

# Une ligne d'achat VALIDE ressemble EXACTEMENT a :   1 000 x [Pepite] (433 166 kamas)
# - la quantite est un nombre bien forme ("1 000" oui, "5 1" non : ca evite de fabriquer
#   une quantite a partir de chiffres parasites) ;
# - l'objet est entre crochets ;
# - le prix est suivi de "kamas" ;
# - AUCUN chiffre ni lettre en dehors de ca (seule la ponctuation parasite est toleree).
# Tout ce qui s'en ecarte n'est jamais devine : la ligne est classee "douteuse".
_QTY = r'(\d{1,3}(?: \d{3})+|\d{1,7})'
_PRICE = r'(\d{1,3}(?: \d{3})+|\d{1,12})'
PURCHASE_LINE = re.compile(
    r'^[^\w\[\]()]*' + _QTY + r'\s*[xX\u00d7]\s*\[([^\[\]]{2,60})\]\s*\(?\s*' + _PRICE
    + r'\s*kamas\s*\)?[^\w\[\]()]*$',
    re.IGNORECASE,
)
ITEM_BRACKET = re.compile(r'\[[^\[\]]*\]')
KAMAS_WORD = re.compile(r'kamas', re.IGNORECASE)
LOOKS_LIKE_PURCHASE = re.compile(r'\d\s*[xX\u00d7]\s')

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

# Issues possibles d'une tentative de FM, dans l'ordre des boutons du panneau :
# (cle interne, libelle, emoji, couleur du bouton). Pour ajouter, retirer ou renommer une issue,
# il suffit de modifier cette liste (4 issues au maximum : avec « Annuler », une rangee de
# boutons Discord n'en contient que 5). Apres un changement, reposte les panneaux avec /fmpanel.
FM_OUTCOMES = [
    ("sc", "Succès critique", "✅", discord.ButtonStyle.success),
    ("sn", "Succès neutre", "🟡", discord.ButtonStyle.secondary),
    ("ec", "Échec", "❌", discord.ButtonStyle.danger),
]
ATTEMPT_BUTTON_PREFIX = "fm:attempt:"
ATTEMPT_HISTORY_CAP = 200  # profondeur maximale du bouton « Annuler » (les compteurs, eux, sont illimites)
# Le panneau redescend sous chaque nouvelle capture : il est toujours a portee de clic, sans remonter
# le fil. Mettre FM_PANEL_FOLLOWS=0 pour le garder en un seul exemplaire, epingle (demande alors a
# donner au bot les droits « Gerer les messages » / « Epingler des messages »).
FM_PANEL_FOLLOWS = os.environ.get("FM_PANEL_FOLLOWS", "1") != "0"
# Journal durable des tentatives (base de /fmstats), range dans data.json : au-dela, les plus
# anciennes entrees sont oubliees. Les ecarts de plus de ATTEMPT_PAUSE_SECONDS entre deux
# tentatives sont des pauses : ils ne comptent pas dans le rythme.
ATTEMPT_LOG_CAP = 20000
ATTEMPT_PAUSE_SECONDS = 600
STATS_MIN_SAMPLE = 30
# Historique des sessions terminees (base de /fmhistorique), range dans data.json.
SESSION_HISTORY_CAP = 500
SESSION_HISTORY_ITEMS = 40   # objets gardes par session (les plus depenses)

# ---------------- DATA ---------------- #
# ---------------- SAUVEGARDE DES DONNEES ---------------- #
# data.json contient les totaux, paiements, historiques et statistiques : il ne doit JAMAIS etre
# perdu ni corrompu. Ecriture : on serialise d'abord (une donnee invalide ne touche pas au fichier),
# on ecrit dans un fichier temporaire, puis on le renomme (jamais de fichier a moitie ecrit).
# Chargement : un fichier illisible n'est plus ignore en silence, il est conserve a cote et le bot
# repart de la derniere copie de secours valide.
DATA_PATH = os.environ.get("DATA_PATH", "data.json")
BACKUP_KEEP = 5              # copies de secours conservees : data.json.bak1 (la plus recente) a .bak5
BACKUP_MIN_INTERVAL = 300    # au plus une nouvelle copie de secours toutes les 5 minutes
_last_backup_at = None

def _atomic_write(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        # Fichier monte seul dans docker (./data.json:/app/data.json) : on ne peut pas le remplacer par
        # renommage. On ecrit alors directement, le texte etant deja entierement pret en memoire.
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

def _rotate_backups(text):
    global _last_backup_at
    now = time.monotonic()
    if _last_backup_at is not None and now - _last_backup_at < BACKUP_MIN_INTERVAL:
        return
    _last_backup_at = now
    for i in range(BACKUP_KEEP, 1, -1):
        older = f"{DATA_PATH}.bak{i - 1}"
        if os.path.exists(older):
            os.replace(older, f"{DATA_PATH}.bak{i}")
    _atomic_write(f"{DATA_PATH}.bak1", text)   # ecrite depuis la memoire : toujours un JSON valide

def save_data():
    text = json.dumps(sessions)
    _atomic_write(DATA_PATH, text)
    try:
        _rotate_backups(text)
    except OSError as exc:
        print(f"[DATA] copie de secours impossible : {exc}")

def _read_data_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("le fichier ne contient pas un objet JSON")
    return data

def load_data():
    global sessions
    if not os.path.exists(DATA_PATH):
        sessions = {}   # premiere utilisation
        return
    try:
        sessions = _read_data_file(DATA_PATH)
        return
    except Exception as exc:
        print(f"[DATA] ERREUR : {DATA_PATH} est illisible ({exc}).")
    corrupt = f"{DATA_PATH}.corrupt-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.copy2(DATA_PATH, corrupt)
        print(f"[DATA] Fichier abime conserve dans {corrupt}.")
    except OSError as exc:
        print(f"[DATA] Impossible de conserver le fichier abime : {exc}")
    for i in range(1, BACKUP_KEEP + 1):
        backup = f"{DATA_PATH}.bak{i}"
        if os.path.exists(backup):
            try:
                sessions = _read_data_file(backup)
                print(f"[DATA] Donnees restaurees depuis {backup} : l'activite des dernieres minutes peut manquer.")
                return
            except Exception as exc:
                print(f"[DATA] {backup} inutilisable ({exc}).")
    print(f"[DATA] AUCUNE copie de secours utilisable : demarrage a vide. L'ancien fichier reste dans {corrupt}.")
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
    Prepare une capture pour l'OCR : niveaux de gris, agrandissement, contraste, et
    inversion sur fond sombre (texte fonce sur fond clair = ce que Tesseract lit le mieux).
    Pas de binarisation : mesuree sur des captures de test, elle detruit la forme des
    chiffres (ombre portee, JPEG) et provoque justement les confusions 6/8, 1/2...
    """
    g = img.convert("L")
    w, h = g.size
    g = g.resize((w * scale, h * scale), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    if ImageStat.Stat(g).mean[0] < 128:
        g = ImageOps.invert(g)
    return g

def clean_number(raw: str) -> int:
    cleaned = raw.replace(" ", "").replace(" ", "").replace(" ", "")
    return int(cleaned)

# ---------------- LECTURE D'UNE CAPTURE ---------------- #
def format_number(n):
    return f"{n:,}".replace(",", " ")

def normalize_ocr_line(raw):
    line = raw.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
    # Horodatage du chat ("[12:38]", crochets lus ou non) : on l'enleve, sinon ses crochets
    # seraient pris pour un objet et ses chiffres pour une quantite.
    # L'OCR lit parfois le crochet ouvrant comme « f », « l », « I » ou « | » et le fermant comme « ) » (vu sur de
    # vraies captures : « f12:16) ») : on tolere ces confusions, mais seulement collees a une heure HH:MM.
    line = re.sub(r"[\[\(\{fFIl|!]?\s*\d{1,2}\s*:\s*\d{2}\s*[\]\)\}|]?", " ", line)
    # Separateur de milliers lu "." ou "," (20.250 -> 20 250). Les kamas sont entiers :
    # un point suivi de 3 chiffres ne peut etre qu'un separateur de milliers.
    line = re.sub(r"(?<=\d)[.,](?=\d{3}(?!\d))", " ", line)
    return re.sub(r"\s+", " ", line).strip()

def entry_label(e):
    return f"{format_number(e['qty'])} x [{e['name']}] ({format_number(e['price'])} kamas)"

def entry_key(e):
    return (re.sub(r"\s+", " ", e["name"]).casefold(), e["qty"], e["price"])

def parse_capture_text(text):
    """
    Retourne (entries, rejects).
    entries : lignes d'achat lues SANS ambiguite -> {name, qty, price}.
    rejects : lignes qui ressemblent a un achat (crochet, "N x", "kamas") mais qui ne
              respectent pas EXACTEMENT le format. Elles ne sont JAMAIS comptees ni
              corrigees automatiquement : elles declenchent une verification humaine.
    Le texte est traite ligne par ligne : un chiffre ne passe jamais d'une ligne a l'autre.
    """
    entries, rejects = [], []
    for raw in text.splitlines():
        line = normalize_ocr_line(raw)
        if not line:
            continue
        brackets = len(ITEM_BRACKET.findall(line))
        kamas = len(KAMAS_WORD.findall(line))
        if brackets == 0 and kamas == 0:
            continue  # autre ligne du chat, sans rapport avec un achat
        if (brackets == 0 and "[" not in line and "]" not in line
                and not LOOKS_LIKE_PURCHASE.search(line)):
            continue  # ex. "Vous avez recu 5 000 kamas" : pas un achat d'objet
        m = PURCHASE_LINE.match(line)
        if m and brackets == 1 and kamas == 1:
            qty = int(m.group(1).replace(" ", ""))
            price = int(m.group(3).replace(" ", ""))
            name = re.sub(r"\s+", " ", m.group(2)).strip()
            if qty >= 1 and price >= 1 and name:
                entries.append({"name": name, "qty": qty, "price": price})
                continue
        rejects.append(raw.strip())
    return entries, rejects

def analyze_capture_bytes(content):
    """
    Lit une image et dit si on peut s'y fier.
    Retourne {"entries": [...], "doubts": [...], "hash": "..."}.
    L'image est lue a deux echelles ; elle n'est acceptee automatiquement que si les deux
    lectures sont propres ET donnent exactement les memes lignes (doubts vide).
    """
    img = Image.open(BytesIO(content))
    img.load()
    digest = hashlib.sha256(content).hexdigest()

    scales = READ_SCALES if DOUBLE_READ else READ_SCALES[:1]
    reads = []
    for scale in scales:
        text = pytesseract.image_to_string(preprocess_image(img, scale), lang=TESS_LANG, config=OCR_CONFIG)
        entries_i, rejects_i = parse_capture_text(text)
        reads.append({"text": text, "entries": entries_i, "rejects": rejects_i})

    best = min(reads, key=lambda r: len(r["rejects"]))  # lecture la plus propre (1ere en cas d'egalite)
    doubts = []
    seen_rejects = []
    for r in reads:
        for line in r["rejects"]:
            if line not in seen_rejects:
                seen_rejects.append(line)
    for line in seen_rejects:
        doubts.append(f"ligne non reconnue : {line[:100]}")

    if len(reads) > 1:
        counters = [Counter(entry_key(e) for e in r["entries"]) for r in reads]
        labels = {entry_key(e): entry_label(e) for r in reads for e in r["entries"]}
        unstable = []
        for other in counters[1:]:
            for key in list((counters[0] - other).elements()) + list((other - counters[0]).elements()):
                if key not in unstable:
                    unstable.append(key)
        for key in unstable:
            doubts.append(f"ligne lue de façon instable : {labels[key]}")

    if doubts:
        # Aide au diagnostic dans les logs de l'hote (docker compose logs).
        print("[OCR] capture douteuse, textes bruts lus :")
        for i, r in enumerate(reads, 1):
            print(f"--- lecture {i} ---\n{r['text']}")
    return {"entries": best["entries"], "doubts": doubts, "hash": digest}

def analyze_capture_url(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return analyze_capture_bytes(response.content)

def empty_session():
    return {
        "total": 0,
        "users": {},
        "runes": {},
        "payments": [],
        "seen": [],
        "attempts": new_attempts(),
        "started_at": now_iso(),
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

# ---------------- PAIEMENTS ---------------- #
# "total" reste toujours le montant BRUT depense. Les paiements du client s'en deduisent :
# reste a payer = total - paiements. Les sessions anciennes (sans "payments") restent valides.
def payments_of(session):
    return session.get("payments") or []

def paid_total(session):
    return sum(p["amount"] for p in payments_of(session))

def amount_due(session):
    return session["total"] - paid_total(session)

_AMOUNT_RE = re.compile(r"^([\d\s.,]+?)\s*(kk|k|m)?\s*(?:kamas?)?$", re.IGNORECASE)
_PLAIN_INT = re.compile(r"^\d+$")
_GROUPED_INT = re.compile(r"^\d{1,3}(?:[ .,]\d{3})+$")
_DECIMAL = re.compile(r"^\d+(?:[.,]\d+)?$")
_AMOUNT_SUFFIX = {"k": 1_000, "m": 1_000_000, "kk": 1_000_000}

_SUFFIX_DECIMAL_RE = re.compile(r"^(\d+)\s*(kk|k|m)\s*(\d+)\s*(?:kamas?)?$", re.IGNORECASE)
MAX_KAMAS_AMOUNT = 1_000_000_000_000   # 1 000 milliards : au-dela, c'est une faute de frappe, jamais une vraie somme

def parse_kamas_amount(raw):
    """
    Lit un montant ecrit par un humain. Retourne un entier > 0, ou None si ambigu.
    Accepte : 20000000 | 20 000 000 | 20.000.000 | 20M | 20m | 20kk | 1.5M | 1,5M | 500k | 1M6 | 2M35
    Sans suffixe, "." et "," ne sont que des separateurs de milliers (20.5 est refuse
    plutot que lu "205"). Avec suffixe, ils sont la virgule decimale. « 1M6 » se lit 1,6 million
    (les chiffres apres la lettre sont les decimales : 1M6 = 1 600 000, 2M05 = 2 050 000).
    """
    text = (raw or "").replace("\u00a0", " ").replace("\u202f", " ").strip().lower()
    try:
        m_dec = _SUFFIX_DECIMAL_RE.match(text)
        if m_dec:
            value = Decimal(f"{m_dec.group(1)}.{m_dec.group(3)}") * _AMOUNT_SUFFIX[m_dec.group(2)]
        else:
            m = _AMOUNT_RE.match(text)
            if not m:
                return None
            number, suffix = m.group(1).strip(), (m.group(2) or "")
            if not number:
                return None
            if suffix:
                compact = number.replace(" ", "")
                if not _DECIMAL.match(compact):
                    return None
                value = Decimal(compact.replace(",", ".")) * _AMOUNT_SUFFIX[suffix]
            elif _PLAIN_INT.match(number):
                value = Decimal(number)
            elif _GROUPED_INT.match(number):
                value = Decimal(re.sub(r"[ .,]", "", number))
            else:
                return None
    except InvalidOperation:
        return None
    if value <= 0 or value != value.to_integral_value() or value > MAX_KAMAS_AMOUNT:
        return None
    return int(value)

def parse_signed_kamas_amount(raw):
    """
    Comme parse_kamas_amount, avec un signe optionnel pour /fmpay :
    20M ou +20M = paiement du client ; -1M6 = montant AJOUTE au total (stock apporte...).
    Retourne un entier non nul (signe conserve) ou None.
    """
    text = (raw or "").strip()
    sign = 1
    if text and text[0] in "-\u2212\u2013":  # tiret, vrai signe moins, tiret demi-cadratin (clavier mobile)
        sign, text = -1, text[1:]
    elif text[:1] == "+":
        text = text[1:]
    value = parse_kamas_amount(text)
    return None if value is None else sign * value

def payment_totals(session):
    """(paiements recus, ajustements ajoutes au total) : deux montants positifs."""
    amounts = [p["amount"] for p in payments_of(session)]
    return sum(a for a in amounts if a > 0), -sum(a for a in amounts if a < 0)

def _payment_when(payment):
    try:
        ts = int(datetime.datetime.fromisoformat(payment["at"]).timestamp())
        return f"<t:{ts}:f>"
    except (KeyError, ValueError, TypeError):
        return "date inconnue"

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
    text = f"{title}\n\n{resume}\n\n"
    attempts = attempts_line(session)
    if attempts:
        text += attempts + "\n\n"
    payments = payments_of(session)
    stock_text = "\n\n" + "\n".join(stock_summary_lines(stock_of(session), session["total"])) if stock_of(session) else ""
    if not payments:
        return text + f"TOTAL : {format_number(session['total'])} kamas" + stock_text

    received, charged = payment_totals(session)
    lines = [f"TOTAL DÉPENSÉ : {format_number(session['total'])} kamas"]
    if charged:
        n_adjust = sum(1 for p in payments if p["amount"] < 0)
        lines.append(f"Ajustements ({n_adjust}) : +{format_number(charged)} kamas")
    if received:
        n_paid = sum(1 for p in payments if p["amount"] > 0)
        lines.append(f"Paiements reçus ({n_paid}) : -{format_number(received)} kamas")
    for p in payments[-10:]:
        sign = "-" if p["amount"] > 0 else "+"   # effet sur le reste a payer
        note = f" · {p['note']}" if p.get("note") else ""
        lines.append(f"  • {sign}{format_number(abs(p['amount']))} kamas{note} ({_payment_when(p)})")
    if len(payments) > 10:
        lines.append(f"  (+{len(payments) - 10} ligne(s) plus ancienne(s))")
    due = amount_due(session)
    if due >= 0:
        lines.append(f"RESTE À PAYER : {format_number(due)} kamas")
    else:
        lines.append(f"TROP-PERÇU (crédit client) : {format_number(-due)} kamas")
    return text + "\n".join(lines) + stock_text

# ---------------- ENREGISTREMENT D'UNE CAPTURE ---------------- #
def aggregate_entries(entries):
    agg = {}
    for e in entries:
        v = agg.setdefault(e["name"], {"qty": 0, "price": 0})
        v["qty"] += e["qty"]
        v["price"] += e["price"]
    return agg

def commit_entries(session, channel_id, author, entries, hashes, ref=None, source="capture"):
    """SEUL endroit ou une capture modifie les totaux. Retourne le montant ajoute. Garde de quoi l'annuler (/fmundo)."""
    added = sum(e["price"] for e in entries)
    session["total"] += added
    uid = str(author.id)
    session["users"][uid] = session["users"].get(uid, 0) + added
    runes = session.setdefault("runes", {})
    for name, vals in aggregate_entries(entries).items():
        r = runes.setdefault(name, {"qty": 0, "price": 0})
        r["qty"] += vals["qty"]
        r["price"] += vals["price"]
    seen = session.setdefault("seen", [])
    for h in hashes:
        if h and h not in seen:
            seen.append(h)
    del seen[:-200]
    captures = session.setdefault("captures", [])
    captures.append({"ref": str(ref) if ref is not None else None, "by": uid, "at": now_iso(), "src": source,
                     "entries": [dict(e) for e in entries], "hashes": [h for h in hashes if h]})
    del captures[:-CAPTURE_HISTORY_CAP]
    touch_channel(channel_id)
    save_data()
    return added

def capture_reply_text(author, entries, session):
    agg = aggregate_entries(entries)
    added = sum(e["price"] for e in entries)
    # Le nom lu entre crochets est affiche tel quel (il contient deja "Rune ..." pour les
    # runes, et les autres objets ne sont plus faussement presentes comme des runes).
    objets = "\n".join(
        f"{name} x{v['qty']} {format_number(v['price'])} Kamas" for name, v in agg.items()
    )
    detail = "\n".join(f"+ {format_number(e['price'])}" for e in entries)
    tail = f"{author.mention} -> +{format_number(added)}\nTotal global : {format_number(session['total'])}"
    if payments_of(session):
        tail += f"\nReste à payer : {format_number(amount_due(session))}"
    text = f"{objets}\n\nCapture traitee :\n{detail}\n\n{tail}"
    if len(text) > 1900:
        text = f"{objets}\n\nCapture traitee : {len(entries)} lignes\n\n{tail}"
    if len(text) > 1900:
        text = f"Capture traitee : {len(entries)} lignes ({len(agg)} objets)\n\n{tail}"
    return text

def hold_message_text(entries, doubts, extra=""):
    parts = ["⚠️ **Capture à vérifier : rien n'a été ajouté pour l'instant.**"]
    if entries:
        shown = [f"• {entry_label(e)}" for e in entries[:12]]
        if len(entries) > 12:
            shown.append(f"(+{len(entries) - 12} autre(s))")
        parts.append(f"**Lignes lues ({len(entries)})** :\n" + "\n".join(shown))
    shown_d = [f"• {d}" for d in doubts[:8]]
    if len(doubts) > 8:
        shown_d.append(f"(+{len(doubts) - 8} autre(s))")
    parts.append("**À vérifier** :\n" + "\n".join(shown_d))
    if extra:
        parts.append(extra)
    if entries:
        parts.append("Les lignes « à vérifier » ne seront **pas** comptées si tu valides. "
                     "Si l'une d'elles est réelle, ignore et renvoie une capture plus nette.")
    else:
        parts.append("Rien à valider. Si une ligne est réelle, renvoie une capture plus nette.")
    return "\n\n".join(parts)[:1990]

async def send_capture_event(message, author, added, entries):
    await send_event(site_event(
        "capture", message.id, message.channel, message.created_at,
        author=site_author(author),
        spentKamas=added,
        lines=entries,
    ))

class CaptureConfirmView(discord.ui.View):
    """Boutons affichés quand une capture est douteuse : rien n'est compté sans ton accord."""
    def __init__(self, message, entries, hashes):
        super().__init__(timeout=900)
        self.message = message
        self.entries = entries
        self.hashes = hashes
        self.done = False
        self.reply_message = None

    async def _allowed(self, interaction):
        if interaction.user.id != self.message.author.id:
            await interaction.response.send_message(
                "Seul l'auteur de la capture peut la valider ou l'ignorer.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Cette capture a déjà été traitée.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Valider les lignes lues", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        self.done = True
        self.stop()
        channel_id = str(self.message.channel.id)
        session = sessions.get(channel_id)
        if session is None or not session.get("active", True):
            await interaction.response.edit_message(
                content="La session n'est plus active : rien n'a été ajouté.", view=None)
            return
        added = commit_entries(session, channel_id, self.message.author, self.entries, self.hashes, ref=self.message.id)
        await interaction.response.edit_message(
            content=capture_reply_text(self.message.author, self.entries, session), view=None)
        await send_capture_event(self.message, self.message.author, added, self.entries)

    @discord.ui.button(label="Ignorer cette capture", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(
            content="Capture ignorée : rien n'a été ajouté.", view=None)

    async def on_timeout(self):
        if not self.done and self.reply_message is not None:
            try:
                await self.reply_message.edit(
                    content="⌛ Capture expirée sans validation : rien n'a été ajouté.", view=None)
            except discord.HTTPException:
                pass

# ---------------- STATISTIQUES DES TENTATIVES ---------------- #
# Chaque clic du panneau est aussi range dans un journal durable, independant des sessions (il
# survit a /fmstop et /fmreset). Une entree = {"t": date ISO, "k": issue, "c": salon, "u": utilisateur}.
def attempt_log():
    return fm_state().setdefault("attempt_log", [])

def log_attempt(channel_id, key, user_id, at, rune=None, bulk=False):
    """rune : rune visee au moment du clic ; bulk : ajoutee en bloc (/fmtenta), sans heure reelle."""
    log = attempt_log()
    entry = {"t": at, "k": key, "c": str(channel_id), "u": str(user_id)}
    if rune:
        entry["r"] = rune
    if bulk:
        entry["b"] = 1
    log.append(entry)
    del log[:-ATTEMPT_LOG_CAP]

def unlog_latest(channel_id, key):
    """Retire du journal l'entree la plus recente d'une issue pour un salon (ajustement negatif)."""
    log = attempt_log()
    for i in range(len(log) - 1, -1, -1):
        if log[i].get("c") == str(channel_id) and log[i].get("k") == key:
            del log[i]
            return True
    return False

def unlog_attempt(channel_id, key, at):
    """Retire du journal l'entree d'un clic annule (la plus recente qui correspond)."""
    log = attempt_log()
    for i in range(len(log) - 1, -1, -1):
        e = log[i]
        if e.get("t") == at and e.get("c") == str(channel_id) and e.get("k") == key:
            del log[i]
            return True
    return False

def wilson_interval(successes, n, z=1.96):
    """Fourchette a 95 % du vrai taux, d'apres ce qui a ete observe (methode de Wilson)."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))

def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"

def compute_attempt_stats(entries, pause_seconds=ATTEMPT_PAUSE_SECONDS):
    """
    Statistiques d'une liste d'entrees du journal. L'issue « visee » est la premiere de FM_OUTCOMES
    (le succes critique). Le rythme est calcule sur les ecarts <= pause_seconds : les pauses (AFK,
    nuit...) ne faussent donc pas le temps moyen.
    """
    known = [key for key, *_ in FM_OUTCOMES]
    rows = []
    for e in entries:
        try:
            when = datetime.datetime.fromisoformat(e["t"])
        except (KeyError, ValueError, TypeError):
            continue
        if e.get("k") in known:
            rows.append((when, e["k"], bool(e.get("b"))))
    rows.sort(key=lambda r: r[0])

    n = len(rows)
    counts = {key: 0 for key in known}
    for _, key, _bulk in rows:
        counts[key] += 1
    # Les tentatives ajoutees en bloc n'ont pas d'heure reelle : comptees partout, mais exclues du rythme.
    timed = [r for r in rows if not r[2]]
    gaps = [(timed[i][0] - timed[i - 1][0]).total_seconds() for i in range(1, len(timed))]
    active = [g for g in gaps if g <= pause_seconds]
    target = known[0]

    longest = current = 0
    for _, key, _bulk in rows:
        if key == target:
            current = 0
        else:
            current += 1
            longest = max(longest, current)

    avg_gap = sum(active) / len(active) if active else None
    per_target = n / counts[target] if counts[target] else None
    return {
        "n": n, "counts": counts, "target": target, "bulk": n - len(timed),
        "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None,
        "avg_gap": avg_gap, "pauses": len(gaps) - len(active), "active_seconds": sum(active),
        "per_target": per_target,
        "time_per_target": avg_gap * per_target if (avg_gap is not None and per_target) else None,
        "longest_without_target": longest, "current_without_target": current,
    }

def stats_by_rune(entries):
    """Tentatives regroupees par rune visee : [(nom, total, {issue: n})], de la plus essayee a la moins essayee.
    Liste vide si aucune rune n'a jamais ete precisee (rien a afficher)."""
    known = [key for key, *_ in FM_OUTCOMES]
    groups, any_rune = {}, False
    for e in entries:
        if e.get("k") not in known:
            continue
        rune = e.get("r")
        any_rune = any_rune or bool(rune)
        group = groups.setdefault(rune or "", {k: 0 for k in known})
        group[e["k"]] += 1
    if not any_rune:
        return []
    rows = [(name or "(rune non précisée)", sum(c.values()), c) for name, c in groups.items()]
    return sorted(rows, key=lambda row: -row[1])

def _decimal_fr(x):
    return f"{x:.1f}".replace(".", ",")

def stats_embed(stats, scope_label, by_rune=None):
    n = stats["n"]
    description = f"**{n}** tentative(s) enregistrée(s)"
    if stats["first"]:
        description += (f" du <t:{int(stats['first'].timestamp())}:d>"
                        f" au <t:{int(stats['last'].timestamp())}:d>")
    if n < STATS_MIN_SAMPLE:
        description += (f"\n⚠️ Moins de {STATS_MIN_SAMPLE} tentatives : les pourcentages sont très "
                        "approximatifs, fie-toi aux fourchettes.")
    embed = discord.Embed(title=f"📊 Statistiques de FM : {scope_label}", description=description,
                          color=discord.Color.blurple())
    for key, label, emoji, _style in FM_OUTCOMES:
        c = stats["counts"][key]
        low, high = wilson_interval(c, n)
        embed.add_field(
            name=f"{emoji} {label}",
            value=f"**{c * 100 / n:.0f} %** ({c}/{n})\nfourchette : {low * 100:.0f} à {high * 100:.0f} %",
            inline=True)

    if stats["avg_gap"] is not None:
        rhythm = f"1 tentative toutes les **{format_duration(stats['avg_gap'])}**"
        if stats["pauses"]:
            rhythm += f"\n{stats['pauses']} pause(s) de plus de {ATTEMPT_PAUSE_SECONDS // 60} min ignorée(s)"
    else:
        rhythm = "Pas assez de tentatives pour estimer le rythme."
    if stats.get("bulk"):
        rhythm += f"\n{stats['bulk']} tentative(s) ajoutée(s) en bloc (/fmtenta) : comptées, mais exclues du rythme"
    embed.add_field(name="⏱️ Rythme", value=rhythm, inline=False)

    _key, label, emoji, _style = FM_OUTCOMES[0]
    if stats["per_target"]:
        freq = f"1 toutes les **{_decimal_fr(stats['per_target'])}** tentatives"
        if stats["time_per_target"] is not None:
            freq += f"\n≈ 1 toutes les {format_duration(stats['time_per_target'])} d'activité"
    else:
        freq = "Aucun pour l'instant."
    embed.add_field(name=f"{emoji} {label} : fréquence", value=freq, inline=False)
    embed.add_field(
        name="🔗 Séries",
        value=(f"Plus longue série sans {label.lower()} : **{stats['longest_without_target']}**\n"
               f"En cours : {stats['current_without_target']}"),
        inline=False)
    if by_rune:
        _k, _l, t_emoji, _s = FM_OUTCOMES[0]
        lines = [f"**{name}** : {n} tent. · {t_emoji} {c[FM_OUTCOMES[0][0]]} ({c[FM_OUTCOMES[0][0]] * 100 / n:.0f} %)"
                 for name, n, c in by_rune[:6]]
        embed.add_field(name="🎯 Par rune visée", value=_cut("\n".join(lines)), inline=False)
    embed.set_footer(text="Fourchette = intervalle de confiance à 95 %. Chiffres observés sur tes "
                          "tentatives, pas les probabilités réelles du jeu.")
    return embed

# ---------------- HISTORIQUE D'UN JOUEUR ---------------- #
# Une session qui se ferme ne laisse rien derriere elle (le salon ne garde que son proprietaire) :
# on en garde ici un resume durable. Une entree = une session terminee, attribuee au proprietaire du
# salon. Les sessions fermees AVANT cette fonction n'ont pas de detail : elles ne peuvent pas etre retrouvees.
def session_has_activity(session):
    return bool(session["total"] or attempts_total(session) or payments_of(session))

def build_session_record(channel_id, name, owner, session, end, why):
    entry = fm_channels().get(str(channel_id)) or {}
    received, charged = payment_totals(session)
    items = sorted((session.get("runes") or {}).items(), key=lambda kv: -kv[1].get("price", 0))
    return {
        "ch": str(channel_id), "name": name or "", "owner": owner,
        "start": session.get("started_at") or entry.get("created_at"), "end": end,
        "total": session["total"], "adjust": charged, "paid": received,
        "items": {n: [v.get("qty", 0), v.get("price", 0)] for n, v in items[:SESSION_HISTORY_ITEMS]},
        "attempts": dict(attempts_of(session)["counts"]), "why": why,
        "payments": [{k: v for k, v in p.items() if k in ("amount", "note", "at")} for p in payments_of(session)][-30:],
        **({"stock": {k: v for k, v in stock_of(session).items() if k in ("start", "end") and isinstance(v, int)}} if stock_of(session) else {}),
    }

def record_session_history(channel, session, why):
    """Appele quand une session se ferme. Les sessions vides (rien achete, aucun clic) ne sont pas gardees."""
    if not session_has_activity(session):
        return None
    entry = fm_channels().get(str(channel.id)) or {}
    history = fm_state().setdefault("history", [])
    record = build_session_record(channel.id, getattr(channel, "name", ""), entry.get("owner_id"), session, now_iso(), why)
    history.append(record)
    del history[:-SESSION_HISTORY_CAP]
    return record

def live_records(name_of, player_id=None):
    """Sessions EN COURS (d'un joueur, ou de tous si player_id est None), au format des sessions terminees."""
    records = []
    for channel_id, entry in fm_channels().items():
        owner = entry.get("owner_id")
        if player_id is not None and owner != str(player_id):
            continue
        session = sessions.get(channel_id)
        if session is None or not session_has_activity(session):
            continue
        record = build_session_record(channel_id, name_of(channel_id), owner, session, None, "live")
        record["live"] = True
        records.append(record)
    return records

def live_records_of(player_id, name_of):
    return live_records(name_of, player_id)

def longest_streaks(entries):
    """
    Records de series, calcules SALON PAR SALON (une serie ne traverse pas deux objets differents).
    Retourne {"fail": (longueur, salon, date de fin), "target": ..., "no_target": ...}.
    fail = enchainement d'echecs (derniere issue de FM_OUTCOMES), target = enchainement de succes
    critiques (premiere issue), no_target = tentatives d'affilee sans succes critique.
    """
    known = [key for key, *_ in FM_OUTCOMES]
    target_key, fail_key = known[0], known[-1]
    by_channel = {}
    for e in entries:
        try:
            when = datetime.datetime.fromisoformat(e["t"])
        except (KeyError, ValueError, TypeError):
            continue
        if e.get("k") in known:
            by_channel.setdefault(e.get("c"), []).append((when, e["k"]))
    best = {"fail": (0, None, None), "target": (0, None, None), "no_target": (0, None, None)}
    for channel, rows in by_channel.items():
        rows.sort(key=lambda r: r[0])
        runs = {"fail": 0, "target": 0, "no_target": 0}
        for when, key in rows:
            runs["fail"] = runs["fail"] + 1 if key == fail_key else 0
            runs["target"] = runs["target"] + 1 if key == target_key else 0
            runs["no_target"] = runs["no_target"] + 1 if key != target_key else 0
            for name, length in runs.items():
                if length > best[name][0]:
                    best[name] = (length, channel, when)
    return best

def record_amount(record):
    """Montant d'une commande = achats + ajustements (stock apporte), avant deduction des paiements."""
    return record["total"] + record.get("adjust", 0)

def compute_player_profile(records, log_entries):
    amounts = [record_amount(r) for r in records]
    items = {}
    for r in records:
        for name, (qty, price) in r.get("items", {}).items():
            cur = items.setdefault(name, [0, 0])
            cur[0] += qty
            cur[1] += price
    top_items = sorted(items.items(), key=lambda kv: -kv[1][1])[:3]
    stamps = []
    for r in records:
        for key in ("start", "end"):
            try:
                stamps.append(datetime.datetime.fromisoformat(r[key]))
            except (KeyError, ValueError, TypeError):
                pass
    return {
        "n": len(records),
        "biggest": max(records, key=record_amount) if records else None,
        "biggest_amount": max(amounts) if amounts else 0,
        "average": sum(amounts) / len(amounts) if amounts else 0,
        "sum_total": sum(r["total"] for r in records),
        "top_items": [(name, qty, price) for name, (qty, price) in top_items],
        "attempts": compute_attempt_stats(log_entries),
        "streaks": longest_streaks(log_entries),
        "first": min(stamps) if stamps else None, "last": max(stamps) if stamps else None,
    }

def _cut(text, limit=1000):
    return text if len(text) <= limit else text[:limit - 1] + "…"

def _record_date(record):
    for key in ("end", "start"):
        try:
            return f"<t:{int(datetime.datetime.fromisoformat(record[key]).timestamp())}:d>"
        except (KeyError, ValueError, TypeError):
            continue
    return "date inconnue"

def history_embed(display_name, profile, records, owned_total, without_details):
    n = profile["n"]
    live_n = sum(1 for r in records if r.get("live"))
    description = f"**{owned_total}** salon(s) FM · **{n}** session(s) avec données"
    if live_n:
        description += f" (dont {live_n} en cours)"
    if without_details:
        description += (f"\n{without_details} salon(s) plus ancien(s) sans détails : "
                        "fermés avant la mise en place de l'historique.")
    embed = discord.Embed(title=f"🗂️ Historique de {display_name}", description=description,
                          color=discord.Color.blurple())
    names = {r["ch"]: r.get("name") or "salon" for r in records}

    if n:
        big = profile["biggest"]
        stock = f", dont {format_number(big['adjust'])} de stock" if big.get("adjust") else ""
        embed.add_field(name="💰 Commandes", value=_cut(
            f"Plus grosse : **{format_number(profile['biggest_amount'])}** kamas{stock}\n"
            f"↳ #{big.get('name') or 'salon'} · {_record_date(big)}\n"
            f"Moyenne : {format_number(round(profile['average']))} kamas\n"
            f"Total dépensé : {format_number(profile['sum_total'])} kamas"), inline=False)

    a = profile["attempts"]
    if a["n"]:
        _k, t_label, t_emoji, _s = FM_OUTCOMES[0]
        c = a["counts"][FM_OUTCOMES[0][0]]
        low, high = wilson_interval(c, a["n"])
        lines = [f"**{a['n']}** tentatives · {t_emoji} {c} ({c * 100 / a['n']:.0f} %, "
                 f"fourchette {low * 100:.0f} à {high * 100:.0f} %)"]
        if a["per_target"]:
            lines.append(f"1 {t_label.lower()} toutes les {_decimal_fr(a['per_target'])} tentatives")
        if a["active_seconds"]:
            lines.append(f"Temps de forge actif : {format_duration(a['active_seconds'])}")
        embed.add_field(name="🔨 Tentatives", value=_cut("\n".join(lines)), inline=False)

        st = profile["streaks"]
        fail_label, target_label = FM_OUTCOMES[-1][1].lower(), FM_OUTCOMES[0][1].lower()
        rows = []
        for key, text in (("fail", f"Plus longue série d'{fail_label}s"),
                          ("target", f"Meilleure série de {target_label}s d'affilée"),
                          ("no_target", f"Plus longue série sans {target_label}")):
            length, channel, when = st[key]
            if length:
                rows.append(f"{text} : **{length}** · #{names.get(channel, 'salon')} "
                            f"· <t:{int(when.timestamp())}:d>")
        if rows:
            embed.add_field(name="🏅 Records", value=_cut("\n".join(rows)), inline=False)

    if profile["top_items"]:
        embed.add_field(name="🏆 Objets les plus dépensés", value=_cut("\n".join(
            f"{name} : {format_number(qty)} u. · {format_number(price)} kamas"
            for name, qty, price in profile["top_items"])), inline=False)

    if records:
        ordered = sorted(records, key=lambda r: (not r.get("live"), -(
            datetime.datetime.fromisoformat(r["end"]).timestamp() if r.get("end") else 0)))
        lines = []
        for r in ordered[:8]:
            tries = sum((r.get("attempts") or {}).values())
            state = "en cours" if r.get("live") else "terminée"
            lines.append(f"• {_record_date(r)} · #{r.get('name') or 'salon'} · "
                         f"**{format_number(record_amount(r))}** kamas · {tries} tent. · {state}")
        more = f"\n(+{len(ordered) - 8} plus ancienne(s))" if len(ordered) > 8 else ""
        embed.add_field(name="🕘 Dernières sessions", value=_cut("\n".join(lines) + more), inline=False)

    embed.set_footer(text="Historique détaillé depuis la mise en place de cette fonction. Montant d'une "
                          "commande = achats + ajustements, avant paiements.")
    return embed

# ---------------- BILAN : PERIODES ET TOTAUX ---------------- #
# /fmbilan produit une page HTML autonome : chronologie des sessions d'une periode + totaux par mois,
# trimestre et annee. Les dates sont enregistrees en UTC ; les mois, trimestres et annees se lisent dans
# le fuseau REPORT_TIMEZONE (sinon une session fermee a 00h30 le 1er serait comptee dans le mois precedent).
REPORT_TIMEZONE = os.environ.get("REPORT_TIMEZONE", "Europe/Paris")
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
             "octobre", "novembre", "décembre"]
MONTHS_SHORT_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
PERIOD_HELP = ("Formats acceptés : mois → `2026-09`, `09/2026`, `septembre` ou `septembre 2026` ; "
               "trimestre → `2026-T3`, `T3-2026` ou `T3` ; année → `2026`. "
               "Sans année, c'est la plus récente qui correspond.")

def report_tz():
    try:
        return ZoneInfo(REPORT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        print(f"[BILAN] fuseau {REPORT_TIMEZONE!r} introuvable (paquet tzdata manquant ?) : UTC utilisé.")
        return datetime.timezone.utc

def _norm(text):
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if unicodedata.category(c) != "Mn").strip()

_MONTH_BY_NAME = {_norm(name): i + 1 for i, name in enumerate(MONTHS_FR)}

def _first_of(year, month, tz):
    return datetime.datetime(year, month, 1, tzinfo=tz)

def _add_months(year, month, count):
    index = year * 12 + (month - 1) + count
    return index // 12, index % 12 + 1

def parse_report_period(kind, spec, now):
    """
    Retourne (debut, fin_exclue, libelle) en heure locale ; (None, None, libelle) pour « tout ».
    kind : mois | trimestre | annee | tout. Leve ValueError (message pret a afficher) si spec est illisible.
    """
    tz = now.tzinfo
    text = _norm(spec)
    if kind == "tout":
        return None, None, "Toutes les données"

    if kind == "annee":
        if not text:
            year = now.year
        elif re.fullmatch(r"\d{4}", text):
            year = int(text)
        else:
            raise ValueError(f"Année non comprise : « {spec} ». {PERIOD_HELP}")
        if not 2000 <= year <= 2100:
            raise ValueError(f"Année improbable : {year}. {PERIOD_HELP}")
        return _first_of(year, 1, tz), _first_of(year + 1, 1, tz), f"Année {year}"

    if kind == "trimestre":
        if not text:
            year, quarter = now.year, (now.month - 1) // 3 + 1
        else:
            m = re.fullmatch(r"(\d{4})\s*[-/ ]*\s*[tq]\s*([1-4])", text)
            if m:
                year, quarter = int(m.group(1)), int(m.group(2))
            else:
                m = re.fullmatch(r"[tq]\s*([1-4])(?:\s*[-/ ]*\s*(\d{4}))?", text)
                if not m:
                    raise ValueError(f"Trimestre non compris : « {spec} ». {PERIOD_HELP}")
                quarter = int(m.group(1))
                if m.group(2):
                    year = int(m.group(2))
                else:   # sans annee : le plus recent, jamais dans le futur
                    year = now.year if quarter <= (now.month - 1) // 3 + 1 else now.year - 1
        if not 2000 <= year <= 2100:
            raise ValueError(f"Année improbable : {year}. {PERIOD_HELP}")
        first = 3 * (quarter - 1) + 1
        end_year, end_month = _add_months(year, first, 3)
        label = f"T{quarter} {year} ({MONTHS_FR[first - 1]} à {MONTHS_FR[first + 1]})"
        return _first_of(year, first, tz), _first_of(end_year, end_month, tz), label

    # mois (defaut)
    if not text:
        year, month = now.year, now.month
    else:
        m = re.fullmatch(r"(\d{4})\s*[-/.]\s*(\d{1,2})", text)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
        else:
            m = re.fullmatch(r"(\d{1,2})\s*[-/.]\s*(\d{4})", text)
            if m:
                month, year = int(m.group(1)), int(m.group(2))
            else:
                m = re.fullmatch(r"([a-z]+)(?:\s+(\d{4}))?", text)
                if not m or m.group(1) not in _MONTH_BY_NAME:
                    raise ValueError(f"Mois non compris : « {spec} ». {PERIOD_HELP}")
                month = _MONTH_BY_NAME[m.group(1)]
                if m.group(2):
                    year = int(m.group(2))
                else:   # sans annee : le plus recent, jamais dans le futur
                    year = now.year if month <= now.month else now.year - 1
    if not 1 <= month <= 12:
        raise ValueError(f"Mois invalide : {month}. {PERIOD_HELP}")
    if not 2000 <= year <= 2100:
        raise ValueError(f"Année improbable : {year}. {PERIOD_HELP}")
    end_year, end_month = _add_months(year, month, 1)
    return _first_of(year, month, tz), _first_of(end_year, end_month, tz), f"{MONTHS_FR[month - 1].capitalize()} {year}"

def record_when(record, tz):
    """Date a laquelle une session compte : sa fermeture (son debut si elle est en cours), heure locale."""
    keys = ("start", "end") if record.get("live") else ("end", "start")
    for key in keys:
        raw = record.get(key)
        if not raw:
            continue
        try:
            moment = datetime.datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment.astimezone(tz)
    return None

def _new_agg():
    return {"n": 0, "total": 0, "adjust": 0, "paid": 0, "tries": 0, "target": 0, "stock_used": 0, "stock_n": 0}

def _add_agg(agg, record):
    agg["n"] += 1
    agg["total"] += record["total"]
    agg["adjust"] += record.get("adjust", 0)
    agg["paid"] += record.get("paid", 0)
    used = stock_used(stock_of(record))   # sessions dont le stock de depart ET le stock restant sont connus
    if used is not None:
        agg["stock_used"] += used
        agg["stock_n"] += 1
    counts = record.get("attempts") or {}
    agg["tries"] += sum(counts.values())
    agg["target"] += counts.get(FM_OUTCOMES[0][0], 0)

def _period_bounds(kind, key, tz):
    if kind == "month":
        year, month = key
        end = _add_months(year, month, 1)
        return _first_of(year, month, tz), _first_of(end[0], end[1], tz)
    if kind == "quarter":
        year, quarter = key
        first = 3 * (quarter - 1) + 1
        end = _add_months(year, first, 3)
        return _first_of(year, first, tz), _first_of(end[0], end[1], tz)
    return _first_of(key, 1, tz), _first_of(key + 1, 1, tz)

def _overlaps(start, end, a, b):
    return start is None or (a < end and b > start)

def build_report(records, tz, start, end, label, now, player_label=None, sample=False):
    """Toutes les donnees de la page : sessions de la periode, totaux par mois/trimestre/annee, joueurs, objets."""
    dated, undated = [], 0
    for record in records:
        when = record_when(record, tz)
        if when is None:
            undated += 1
        else:
            dated.append((when, record))
    dated.sort(key=lambda pair: pair[0])
    timeline = [(w, r) for w, r in dated if start is None or start <= w < end]

    period, players, items = _new_agg(), {}, {}
    by_item, unlinked = {}, []
    for _w, r in timeline:
        _add_agg(period, r)
        _add_agg(players.setdefault(r.get("owner"), _new_agg()), r)
        item = r.get("item")   # equipement associe au salon par /fmitem (ajoute par la commande, jamais stocke)
        if item:
            slot = by_item.setdefault(item["id"], {"item": item, "agg": _new_agg(), "records": []})
            _add_agg(slot["agg"], r)
            slot["records"].append((_w, r))
        else:
            room = r.get("name") or "salon"
            if room not in unlinked:
                unlinked.append(room)
        for name, (qty, price) in (r.get("items") or {}).items():
            cur = items.setdefault(name, [0, 0])
            cur[0] += qty
            cur[1] += price
    by_month, by_quarter, by_year = {}, {}, {}
    for w, r in dated:
        _add_agg(by_month.setdefault((w.year, w.month), _new_agg()), r)
        _add_agg(by_quarter.setdefault((w.year, (w.month - 1) // 3 + 1), _new_agg()), r)
        _add_agg(by_year.setdefault(w.year, _new_agg()), r)

    if end is not None:
        anchor = end - datetime.timedelta(microseconds=1)
    elif dated:
        anchor = dated[-1][0]
    else:
        anchor = now
    chart, (year, month) = [], (anchor.year, anchor.month)
    for _ in range(12):
        chart.append(((year, month), by_month.get((year, month), _new_agg())))
        year, month = _add_months(year, month, -1)
    chart.reverse()

    return {
        "label": label, "start": start, "end": end, "tz": tz, "now": now, "player": player_label,
        "sample": sample, "timeline": timeline, "period": period, "players": players,
        "top_items": sorted(items.items(), key=lambda kv: -kv[1][1])[:5], "items_all": items,
        "by_item": by_item, "unlinked": unlinked, "items_in_use": False,
        "by_month": by_month, "by_quarter": by_quarter, "by_year": by_year, "chart": chart,
        "first": dated[0][0] if dated else None, "undated": undated,
    }

# ---------------- BILAN : IMAGES DES RUNES (DOFUSDB) ----------------
# Les images viennent de l'API publique de DofusDB (non officielle). Pour la menager : la liste des runes
# est lue une fois puis gardee une semaine, chaque image n'est telechargee qu'une fois (cache disque), les
# requetes sont espacees et un budget de temps borne l'ensemble. Les images sont ensuite INTEGREES dans la
# page (base64) : le fichier reste autonome. Si DofusDB est injoignable, la page est produite sans images.
RUNE_IMAGES_ENABLED = os.environ.get("FM_RUNE_IMAGES", "1") != "0"
DOFUSDB_API = "https://api.dofusdb.fr"
DOFUSDB_IMG_PREFIX = DOFUSDB_API + "/img/items/"
RUNE_TYPE_ID = 78                      # « Rune de forgemagie » (types=78 sur dofusdb.fr)
RUNE_CATALOG_MAX_AGE = 7 * 86400       # on relit la liste des runes au plus une fois par semaine
RUNE_RETRY_AFTER = 1800                # apres un echec (ou sans liste), on attend 30 min avant de reessayer
RUNE_MISSING_RETRY_AFTER = 6 * 3600    # une rune absente de la liste : on relit la liste au plus toutes les 6 h
RUNE_FETCH_BUDGET = 40                 # secondes au maximum pour toutes les requetes d'un bilan
RUNE_CACHE_DIR = os.environ.get("RUNE_CACHE_DIR") or os.path.join(os.path.dirname(DATA_PATH) or ".", "rune_cache")
DOFUSDB_HEADERS = {"User-Agent": "Botomoux-FM/1.0 (bot Discord prive, usage non commercial)", "Accept": "application/json"}
ICON_MAX_BYTES = 300_000
ICON_MAX_PIXELS = 4_000_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

def rune_key(name):
    """Nom compare sans casse, sans accents, espaces normalises : « Rune Ré Per Feu » = « rune re per feu »."""
    return re.sub(r"\s+", " ", _norm(name))

def _http_get(http, url, params, deadline):
    """GET avec delai maximal, budget de temps global, et une nouvelle tentative apres un 429 (trop de requetes)."""
    response = None
    for _attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("budget de temps epuise")
        response = http.get(url, params=params, timeout=max(1, min(8, remaining)))
        if response.status_code != 429:
            return response
        try:
            wait = float(response.headers.get("Retry-After", 2))
        except (TypeError, ValueError):
            wait = 2
        time.sleep(min(max(wait, 1), 5))
    return response

def fetch_rune_catalog(http, deadline):
    """Toutes les runes de forgemagie : {cle normalisee: {"name": nom FR, "img": adresse de l'image}}."""
    catalog, skip, total, use_limit, pages = {}, 0, None, True, 0
    while (total is None or skip < total) and pages < 100:
        params = {"typeId[$in][]": RUNE_TYPE_ID, "$sort": "-id", "$skip": skip}
        if use_limit:
            params["$limit"] = 50
        response = _http_get(http, f"{DOFUSDB_API}/items", params, deadline)
        if response.status_code == 400 and use_limit and pages == 0:
            use_limit = False   # l'API n'accepte pas $limit : on se contente de sa taille de page par defaut
            continue
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            break
        if isinstance(payload.get("total"), int):
            total = payload["total"]
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("name"), dict):
                continue
            name, img = item["name"].get("fr"), item.get("img")
            # L'adresse de l'image n'est suivie que si elle pointe bien vers DofusDB.
            if isinstance(name, str) and name.strip() and isinstance(img, str) and img.startswith(DOFUSDB_IMG_PREFIX):
                catalog[rune_key(name)] = {"name": name.strip(), "img": img}
        skip += len(data)
        pages += 1
        time.sleep(0.25)
    return catalog

def fetch_rune_icon(http, img_url, deadline):
    """Icone en PNG (64 px max, octets) : cache disque d'abord, DofusDB sinon. None si indisponible ou suspecte."""
    if not isinstance(img_url, str) or not img_url.startswith(DOFUSDB_IMG_PREFIX):
        return None
    match = re.fullmatch(r"(\d{1,9})\.png", img_url[len(DOFUSDB_IMG_PREFIX):])
    if not match:
        return None
    path = os.path.join(RUNE_CACHE_DIR, match.group(1) + ".png")
    try:
        with open(path, "rb") as f:
            cached = f.read()
        if cached[:8] == PNG_MAGIC:
            return cached
    except OSError:
        pass
    response = _http_get(http, img_url, None, deadline)
    if response.status_code != 200 or len(response.content) > ICON_MAX_BYTES:
        return None
    try:
        image = Image.open(BytesIO(response.content))
        if image.width * image.height > ICON_MAX_PIXELS:
            return None
        image.load()
        image = image.convert("RGBA")
        image.thumbnail((64, 64), Image.LANCZOS)
        out = BytesIO()
        image.save(out, "PNG", optimize=True)
        data = out.getvalue()
    except Exception:
        return None   # pas une image valide : jamais integree telle quelle
    try:
        os.makedirs(RUNE_CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        pass
    return data

def load_rune_icons(wanted_keys, catalog, fetched_at, tried_at, now_ts, budget=RUNE_FETCH_BUDGET):
    """
    Travail reseau, a lancer dans un thread (il ne touche pas a l'etat du bot, tout lui est passe en
    parametre). Retourne {"catalog", "fetched_at", "tried_at", "icons": {cle: octets PNG}, "notes": [...]}.
    """
    result = {"catalog": dict(catalog or {}), "fetched_at": fetched_at, "tried_at": tried_at, "icons": {}, "notes": []}
    deadline = time.monotonic() + budget
    http = requests.Session()
    http.headers.update(DOFUSDB_HEADERS)

    missing = any(k.startswith("rune") and k not in result["catalog"] for k in wanted_keys)
    if not result["catalog"] or fetched_at is None or now_ts - fetched_at > RUNE_CATALOG_MAX_AGE:
        need, delay = True, RUNE_RETRY_AFTER
    elif missing:
        need, delay = True, RUNE_MISSING_RETRY_AFTER
    else:
        need, delay = False, 0
    if need and (tried_at is None or now_ts - tried_at >= delay):
        result["tried_at"] = now_ts
        try:
            fresh = fetch_rune_catalog(http, deadline)
            if fresh and any(v["name"].lower().startswith("rune") for v in fresh.values()):
                result["catalog"], result["fetched_at"] = fresh, now_ts
            else:
                result["notes"].append("la liste de runes reçue de DofusDB est vide ou inattendue")
        except Exception as exc:
            result["notes"].append(f"DofusDB injoignable ({type(exc).__name__})")
    elif need and not result["catalog"]:
        result["notes"].append("DofusDB était injoignable récemment : nouvelle tentative dans quelques minutes")

    for key in wanted_keys:
        entry = result["catalog"].get(key)
        if not entry:
            continue
        try:
            icon = fetch_rune_icon(http, entry["img"], deadline)
        except Exception as exc:   # reseau coupe, budget epuise... : inutile d'insister sur les suivantes
            result["notes"].append(f"images interrompues ({type(exc).__name__})")
            break
        if icon:
            result["icons"][key] = icon
    return result

async def prepare_rune_gallery(items):
    """Prepare les images pour les objets d'un bilan. Ne leve JAMAIS : au pire, pas d'images."""
    state = fm_state()
    known = dict(state.get("rune_catalog") or {})
    empty = {"catalog": known, "icons": {}, "notes": []}
    if not RUNE_IMAGES_ENABLED or not items:
        return empty
    try:
        keys = {rune_key(name) for name in items}
        res = await asyncio.to_thread(load_rune_icons, keys, known, state.get("rune_catalog_at"),
                                      state.get("rune_catalog_tried"), time.time())
    except Exception as exc:
        print(f"[RUNES] images indisponibles : {exc!r}")
        return {**empty, "notes": ["images indisponibles"]}
    state["rune_catalog"], state["rune_catalog_at"], state["rune_catalog_tried"] = res["catalog"], res["fetched_at"], res["tried_at"]
    save_data()
    icons = {key: "data:image/png;base64," + base64.b64encode(raw).decode() for key, raw in res["icons"].items()}
    return {"catalog": res["catalog"], "icons": icons, "notes": res["notes"]}

def build_rune_rows(items, gallery):
    """
    Lignes de la galerie : les objets de la periode qui sont des runes connues de DofusDB, plus les noms
    « Rune … » absents de la liste (souvent une faute de lecture de la capture, a verifier).
    """
    catalog, icons = gallery["catalog"], gallery["icons"]
    merged = {}
    for name, (qty, price) in items.items():
        key = rune_key(name)
        entry = catalog.get(key)
        if entry:
            status = "ok"
        elif key.startswith("rune"):
            status = "unknown" if catalog else "nocatalog"
        else:
            continue   # ressource (Pepite...) : pas une rune
        row = merged.setdefault(key, {"name": entry["name"] if entry else name, "qty": 0, "price": 0,
                                      "icon": icons.get(key), "status": status})
        row["qty"] += qty
        row["price"] += price
    return sorted(merged.values(), key=lambda r: -r["price"])

# ---------------- EQUIPEMENTS (DOFUSDB) : /fmitem ----------------
# /fmitem associe un equipement a un salon (meme archive). Son icone et son nom apparaissent ensuite dans
# le bilan. L'objet est cherche sur DofusDB a la demande (quelques requetes, jamais tout le catalogue : des
# milliers d'objets de ~15 Ko), puis garde en cache. Trois formes acceptees : le nom (« Anneau de Padgref »),
# l'identifiant DofusDB (34329) ou l'adresse de la fiche. Un resultat n'est accepte que si son nom correspond
# VRAIMENT a la demande : si l'API ignorait un filtre et renvoyait n'importe quoi, on refuse au lieu de
# lier le mauvais objet.
EQUIPMENT_TYPE_IDS = [1, 9, 2, 3, 4, 5, 6, 7, 8, 19, 21, 22, 271, 11, 82, 17, 10, 16]   # types de l'adresse dofusdb.fr
ITEM_LOOKUP_BUDGET = 25
ITEM_REMOVE_WORDS = {"aucun", "aucune", "retirer", "supprimer", "effacer", "none", "-"}

class ItemLookupError(Exception):
    """kind : badref (reference illisible) | notfound | network (DofusDB injoignable ou refuse)."""
    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail

def item_key(name):
    """Nom compare sans casse, sans accents, apostrophes typographiques unifiees (« L’Épée » = « l'epee »)."""
    text = (name or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    return re.sub(r"\s+", " ", _norm(text))

def parse_item_reference(text):
    """("id", 34329) | ("name", "Anneau de Padgref") | (None, None) si la reference est inutilisable."""
    raw = (text or "").strip()
    if not raw:
        return None, None
    if re.fullmatch(r"\d{1,9}", raw):
        return "id", int(raw)
    match = re.search(r"dofusdb\.fr/.+?/(\d{1,9})(?:[/?#].*)?$", raw)   # .../database/object/34329
    if match:
        return "id", int(match.group(1))
    if re.match(r"https?://", raw, re.IGNORECASE):
        return None, None   # une adresse qui n'est pas une fiche d'objet
    return "name", raw

def item_from_doc(doc):
    """{id, name, img, type, type_name, level} d'une fiche DofusDB, ou None si elle est inexploitable."""
    if not isinstance(doc, dict) or not isinstance(doc.get("id"), int) or isinstance(doc.get("id"), bool):
        return None
    name = doc["name"].get("fr") if isinstance(doc.get("name"), dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    img = doc.get("img")
    if not (isinstance(img, str) and img.startswith(DOFUSDB_IMG_PREFIX)):
        icon = doc.get("iconId")   # l'image est toujours img/items/{iconId}.png
        img = f"{DOFUSDB_IMG_PREFIX}{icon}.png" if isinstance(icon, int) and not isinstance(icon, bool) and icon >= 0 else None
    if not img:
        return None
    type_name = None
    if isinstance(doc.get("type"), dict) and isinstance(doc["type"].get("name"), dict):
        type_name = doc["type"]["name"].get("fr") if isinstance(doc["type"]["name"].get("fr"), str) else None
    level = doc.get("level")
    type_id = doc.get("typeId")
    return {"id": doc["id"], "name": name.strip(), "img": img, "type_name": type_name,
            "type": type_id if isinstance(type_id, int) and not isinstance(type_id, bool) else None,
            "level": level if isinstance(level, int) and not isinstance(level, bool) else None}

def _find_item_by_name(http, key, raw, deadline):
    """Cherche un equipement par son nom. Retourne (item, nombre de doublons) ou (None, 0)."""
    for extra in ({"slug.fr": key}, {"name.fr": raw.strip()}):   # le slug ignore casse et accents ; sinon nom exact
        params = {"typeId[$in][]": EQUIPMENT_TYPE_IDS, "$sort": "-id", **extra}
        response = _http_get(http, f"{DOFUSDB_API}/items", params, deadline)
        if response.status_code == 400:
            continue   # filtre refuse par l'API : on essaie l'autre forme
        response.raise_for_status()
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError):
            continue
        if not isinstance(data, list):
            continue
        matches = [it for it in (item_from_doc(d) for d in data) if it and item_key(it["name"]) == key]
        if matches:
            return max(matches, key=lambda it: it["id"]), len(matches)
        # Aucune correspondance : soit l'objet n'existe pas, soit l'API a ignore le filtre. Dans les deux
        # cas on n'en retient rien ; on tente l'autre forme avant de conclure.
    return None, 0

def lookup_dofusdb_item(reference, cache, budget=ITEM_LOOKUP_BUDGET):
    """
    Travail reseau, a lancer dans un thread. Retourne {"item", "icon" (octets PNG ou None), "notes"}.
    Leve ItemLookupError (badref | notfound | network).
    """
    kind, value = parse_item_reference(reference)
    if kind is None:
        raise ItemLookupError("badref")
    deadline = time.monotonic() + budget
    http = requests.Session()
    http.headers.update(DOFUSDB_HEADERS)
    notes, item, duplicates = [], None, 1
    cache = cache or {}
    try:
        if kind == "name":
            key = item_key(value)
            item = cache.get(key)
            if item is None:
                item, duplicates = _find_item_by_name(http, key, value, deadline)
        else:
            item = next((c for c in cache.values() if c.get("id") == value), None)
            if item is None:
                response = _http_get(http, f"{DOFUSDB_API}/items/{value}", None, deadline)
                if response.status_code in (400, 404):
                    raise ItemLookupError("notfound")
                response.raise_for_status()
                item = item_from_doc(response.json())
        if item is None:
            raise ItemLookupError("notfound")
        if duplicates > 1:
            notes.append(f"{duplicates} équipements portent ce nom : le plus récent (id {item['id']}) a été retenu. "
                         "Donne l'identifiant pour en choisir un autre.")
        icon = None
        try:
            icon = fetch_rune_icon(http, item["img"], deadline)
        except Exception:
            pass
        if icon is None:
            notes.append("icône indisponible pour l'instant (elle sera retéléchargée au prochain bilan)")
        return {"item": item, "icon": icon, "notes": notes}
    except ItemLookupError:
        raise
    except Exception as exc:
        raise ItemLookupError("network", type(exc).__name__) from exc

def load_item_icons(items, budget=RUNE_FETCH_BUDGET):
    """Icones (octets PNG) des equipements d'un bilan : presque toujours depuis le cache disque."""
    deadline = time.monotonic() + budget
    http = requests.Session()
    http.headers.update(DOFUSDB_HEADERS)
    icons = {}
    for item in items:
        try:
            raw = fetch_rune_icon(http, item.get("img"), deadline)
        except Exception:
            break   # reseau coupe ou budget epuise : inutile d'insister
        if raw:
            icons[item["id"]] = raw
    return icons

async def prepare_item_icons(report):
    """Icones (data URI) des equipements de la periode du bilan. Ne leve JAMAIS : au pire, pas d'icones."""
    items = {}
    for _when, record in report["timeline"]:
        item = record.get("item")
        if item:
            items[item["id"]] = item
    if not RUNE_IMAGES_ENABLED or not items:
        return {}
    try:
        raw = await asyncio.to_thread(load_item_icons, list(items.values()))
    except Exception as exc:
        print(f"[EQUIPEMENTS] icones indisponibles : {exc!r}")
        return {}
    return {item_id: "data:image/png;base64," + base64.b64encode(data).decode() for item_id, data in raw.items()}

def channel_items():
    return fm_state().setdefault("channel_items", {})

def item_cache():
    return fm_state().setdefault("item_cache", {})

def can_tag_channel(user, channel_id):
    """Proprietaire du salon FM, ou administrateur ; un salon sans proprietaire (classique) est libre."""
    entry = fm_channels().get(str(channel_id))
    if entry is None or str(user.id) == str(entry.get("owner_id")):
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_channels))

def item_autocomplete_choices(current, limit=25):
    """Objets deja resolus qui contiennent le texte saisi (aucun appel reseau) : [(libelle, valeur)]."""
    needle = item_key(current)
    found = [item for key, item in item_cache().items() if needle in key]
    found.sort(key=lambda item: item["name"])
    return [((f"{item['name']} (niv. {item['level']})" if item.get("level") else item["name"]), item["name"])
            for item in found[:limit]]

# ---------------- BILAN : PAGE HTML ---------------- #
# Page autonome (aucun script, aucune ressource externe, aucune image) : elle s'ouvre hors ligne dans
# n'importe quel navigateur, suit le theme clair/sombre et s'imprime proprement (PDF pour un client).
REPORT_CSS = """
:root{--bg:#f6f7fb;--card:#fff;--text:#1d2333;--muted:#667085;--line:#e4e7ee;--accent:#4f46e5;--accent-soft:#e8e7fd;--ok:#12805c;--warn:#b54708;--bar:#a5b4fc;--tile:80px;--tile-img:64px}
@media (prefers-color-scheme:dark){:root{--bg:#10131c;--card:#181c28;--text:#e6e9f2;--muted:#9aa3b8;--line:#272d3d;--accent:#8b93ff;--accent-soft:#232852;--ok:#4cc9a0;--warn:#f5a45d;--bar:#4a52a8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:1.7rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:36px 0 12px}
h3{font-size:.95rem;margin:0}
.sub{color:var(--muted);margin:0 0 22px}
.banner{background:var(--accent-soft);border:1px solid var(--accent);border-radius:10px;padding:10px 14px;margin:0 0 18px}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media (max-width:560px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi b{font-size:1rem;white-space:normal;overflow-wrap:anywhere}.kpi .n{white-space:normal}}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi .lab{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.kpi b{display:block;font-size:1.2rem;margin-top:2px;white-space:nowrap}
.kpi small{color:var(--muted);display:block;font-size:.8rem}
.n{white-space:nowrap;font-variant-numeric:tabular-nums}
.day{margin:20px 0 6px}
.day h3{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line);color:var(--muted);font-weight:600}
.day h3 .daytotal{color:var(--text)}
.tl{list-style:none;margin:12px 0 0 6px;padding:0 0 0 24px;border-left:2px solid var(--line)}
.tl li{position:relative;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 10px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 16px;align-items:start}
.tl li.has-tile{grid-template-columns:minmax(0,1fr) auto auto}
.main{min-width:0}
.tile{align-self:center;display:flex;align-items:center;justify-content:center;width:var(--tile,80px);height:var(--tile,80px);background:var(--bg);border:1px solid var(--line);border-radius:14px}
.tile img{width:var(--tile-img,64px);height:var(--tile-img,64px);object-fit:contain}
@media (max-width:640px){.tl li,.tl li.has-tile{grid-template-columns:auto minmax(0,1fr);grid-template-areas:"main main" "tile amt"}.tl li .main{grid-area:main}.tl li .tile{grid-area:tile}.tl li .amt{grid-area:amt;align-self:center}}
.tl li::before{content:"";position:absolute;left:-32px;top:18px;width:10px;height:10px;border-radius:50%;background:var(--accent);border:2px solid var(--bg)}
.row1{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline}
.when{color:var(--muted);font-variant-numeric:tabular-nums}
.name{font-weight:650;overflow-wrap:anywhere}
.amt{font-weight:700;white-space:nowrap;text-align:right}
.meta{color:var(--muted);font-size:.84rem;margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:.8rem;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:1px 9px;max-width:100%;overflow-wrap:anywhere}
.chip.who{color:var(--accent);border-color:var(--accent)}
.badge{display:inline-block;font-size:.72rem;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.badge.live{color:var(--ok);border-color:var(--ok)}
.badge.del{color:var(--warn);border-color:var(--warn)}
.scroll{overflow-x:auto;border-radius:12px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-size:.9rem}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:.76rem;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}
tr.sel td{background:var(--accent-soft)}
.chart{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px}
.chart svg{width:100%;height:auto;display:block}
.bar{fill:var(--bar)}
.bar.sel{fill:var(--accent)}
.lbl{fill:var(--muted);font-size:11px;text-anchor:middle}
.val{fill:var(--text);font-size:11px;text-anchor:middle}
.notes{color:var(--muted);font-size:.85rem;margin-top:36px}
.notes li{margin:3px 0}
.empty{color:var(--muted);padding:18px;border:1px dashed var(--line);border-radius:12px}
th[data-sort]:not([data-sort="none"]){cursor:pointer;user-select:none}
th[data-sort]:not([data-sort="none"]):hover{color:var(--text)}
th:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
th[aria-sort=ascending]::after{content:" ▲";font-size:.7em}
th[aria-sort=descending]::after{content:" ▼";font-size:.7em}
.hint{color:var(--muted);font-size:.85rem;margin:-4px 0 10px}
.ico{width:36px;height:36px;object-fit:contain;vertical-align:middle}
.ph{display:inline-flex;align-items:center;justify-content:center;border:1px dashed var(--line);border-radius:8px;color:var(--muted);font-size:.85rem}
.rname{display:flex;align-items:center;gap:10px;text-align:left}
.warn{color:var(--warn)}
.chip.item{display:inline-flex;align-items:center;gap:6px;font-weight:600;border-color:var(--accent)}
.lv{color:var(--muted);font-weight:400}
.shot{position:relative;display:inline-block;color:inherit;text-decoration:none;cursor:zoom-in}
.shot:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:12px}
.tile{position:relative}
a.tile.shot{display:flex}
.camph{font-size:1.8rem;opacity:.75}
.cam{position:absolute;right:-5px;bottom:-5px;font-size:.68rem;line-height:1;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:2px 4px;box-shadow:0 1px 3px rgba(0,0,0,.25)}
.lb{display:none;position:fixed;inset:0;z-index:60;align-items:center;justify-content:center;background:rgba(10,12,20,.72);padding:16px}
.lb:target{display:flex}
.lbx{position:absolute;inset:0;cursor:zoom-out}
.lb figure{position:relative;margin:0;background:var(--card);border:1px solid var(--line);padding:10px;border-radius:14px;max-width:min(94vw,860px);box-shadow:0 20px 60px rgba(0,0,0,.45)}
.lb img{display:block;max-width:100%;max-height:78vh;margin:0 auto;border-radius:8px}
.lb figcaption{display:flex;justify-content:space-between;align-items:center;gap:12px;color:var(--muted);font-size:.85rem;padding:8px 4px 0}
.lb figcaption a{color:var(--accent);text-decoration:none;font-weight:600;white-space:nowrap}
@media print{.lb,.cam{display:none!important}.hint{display:none}body{background:#fff;color:#000}.kpi,.tl li,table,.chart{border-color:#bbb}.tl li,tr,.kpi{break-inside:avoid}}
"""

REPORT_JS = """(function(){
function num(v){var n=parseFloat(v);return isNaN(n)?0:n}
var tables=document.querySelectorAll('table.sortable');
Array.prototype.forEach.call(tables,function(table){
 var body=table.tBodies[0],heads=table.tHead.rows[0].cells;
 Array.prototype.forEach.call(heads,function(th,col){
  var type=th.getAttribute('data-sort');
  if(!type||type==='none')return;
  th.tabIndex=0;
  function go(){
   var cur=th.getAttribute('aria-sort');
   var asc=cur?cur==='descending':type==='text';
   var rows=Array.prototype.slice.call(body.rows);
   rows.forEach(function(r,i){r._i=i});
   rows.sort(function(a,b){
    var x=a.cells[col].getAttribute('data-v')||'',y=b.cells[col].getAttribute('data-v')||'';
    var c=type==='text'?x.localeCompare(y,'fr',{numeric:true,sensitivity:'base'}):num(x)-num(y);
    return c!==0?(asc?c:-c):a._i-b._i;
   });
   rows.forEach(function(r){body.appendChild(r)});
   Array.prototype.forEach.call(heads,function(h){h.removeAttribute('aria-sort')});
   th.setAttribute('aria-sort',asc?'ascending':'descending');
  }
  th.addEventListener('click',go);
  th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go()}});
 });
});
})();"""
# Seul ce script exact pourra s'executer : sa valeur de hachage figure dans la politique de securite.
REPORT_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(REPORT_JS.encode("utf-8")).digest()).decode()

def _kamas_short(n):
    """Montant abrege pour le graphique : 193 450 000 -> « 193,5 M ». Arrondi decimal (jamais flottant)."""
    sign, n = ("-" if n < 0 else ""), abs(n)
    units = [(1_000_000_000, " Md"), (1_000_000, " M"), (1_000, " k")]
    for i, (divisor, unit) in enumerate(units):
        if n < divisor:
            continue
        value = (Decimal(n) / Decimal(divisor)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if value >= 1000 and i > 0:   # 999 999 -> « 1000 k » : on passe a l'unite superieure -> « 1 M »
            divisor, unit = units[i - 1]
            value = (Decimal(n) / Decimal(divisor)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return sign + f"{value:f}".rstrip("0").rstrip(".").replace(".", ",") + unit
    return f"{sign}{n}"

def render_report_html(report, owner_name_of, runes=None, item_icons=None, shots=None):
    shots = shots or {}          # {id du salon: data URI de la capture de l'equipement termine}
    shot_ids, shot_notes = {}, {}

    def shot_link(ch, inner, record, when, cls="", title=""):
        """Entoure une image d'un lien vers la capture du salon (survol = apercu, clic ou toucher = grand)."""
        ch = str(ch)
        if ch not in shots:
            return inner
        if ch not in shot_ids:
            shot_ids[ch] = f"s{len(shot_ids) + 1}"
            linked = record.get("item")
            what = linked["name"] if linked else f"#{record.get('name') or 'salon'}"
            who = owner_name_of(record.get("owner"))
            date = f"{when.day} {MONTHS_FR[when.month - 1]} {when.year}"
            shot_notes[ch] = " · ".join(x for x in (what, who, date) if x)
        tip = html_lib.escape(title or "Voir la capture de l'équipement terminé", quote=True)
        return (f'<a class="shot {cls}" href="#{shot_ids[ch]}" title="{tip}">{inner}'
                '<span class="cam" aria-hidden="true">📷</span></a>')

    esc = html_lib.escape
    tz, start, end = report["tz"], report["start"], report["end"]
    period = report["period"]
    _k, target_label, target_emoji, _s = FM_OUTCOMES[0]

    def amount(n):
        return f'<span class="n">{format_number(n)}</span>'

    def rate(agg):
        return f"{agg['target'] * 100 / agg['tries']:.0f} %" if agg["tries"] else "—"

    def agg_cells(agg):
        """Cellules (affichage, valeur brute de tri) d'un agregat."""
        billed = agg["total"] + agg["adjust"]
        rate_value = agg["target"] * 100 / agg["tries"] if agg["tries"] else -1
        return [(str(agg["n"]), agg["n"]), (amount(agg["total"]), agg["total"]),
                (amount(agg["adjust"]), agg["adjust"]), (amount(billed), billed),
                (amount(agg["paid"]), agg["paid"]), (str(agg["tries"]), agg["tries"]),
                (f"{agg['target']} ({rate(agg)})" if agg["tries"] else "—", rate_value)]

    agg_heads = [("Sessions", "num", ""), ("Dépensé", "num", ""), ("Ajustements", "num", "Montants ajoutés au total avec /fmpay -X"), ("Facturé", "num", ""),
                 ("Acomptes", "num", "Paiements enregistrés avec /fmpay pendant la session"), ("Tentatives", "num", ""), (target_label, "num", "Tri sur le taux de réussite")]

    def sortable_table(head_specs, rows):
        """head_specs : [(libelle, num|text|none, infobulle)] ; rows : [(cellules[(html, valeur)], en_surbrillance)]."""
        heads = ""
        for label, kind, tip in head_specs:
            title = f' title="{esc(tip)}"' if tip else ""
            heads += f'<th data-sort="{kind}"{title}>{esc(label)}</th>'
        body = ""
        for cells, selected in rows:
            tds = "".join(f'<td data-v="{esc(str(value))}">{shown}</td>' for shown, value in cells)
            body += ('<tr class="sel">' if selected else "<tr>") + tds + "</tr>"
        return (f'<div class="scroll"><table class="sortable"><thead><tr>{heads}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>")

    # ---- en-tete ----
    parts = [f'<h1>Bilan FM · {esc(report["label"])}</h1>']
    scope = f"Joueur : {esc(report['player'])}" if report["player"] else "Tous les joueurs"
    parts.append(f'<p class="sub">{scope} · généré le {report["now"].strftime("%d/%m/%Y à %H:%M")} '
                 f'({esc(str(tz))})</p>')
    if report["sample"]:
        parts.insert(0, '<div class="banner"><b>Données d\'exemple</b> : cette page illustre le rendu, '
                        'elle ne contient aucune vraie session.</div>')

    # ---- cartes ----
    cards = [("Sessions", str(period["n"]), ""),
             ("Dépensé", amount(period["total"]), "kamas, achats"),
             ("Ajustements", amount(period["adjust"]), "kamas, ajoutés au total"),
             ("Facturé", amount(period["total"] + period["adjust"]), "kamas, dépensé + ajustements"),
             ("Acomptes", amount(period["paid"]), "kamas, versés en cours de session"),
             ("Tentatives", str(period["tries"]), f"{target_emoji} {period['target']} · {rate(period)}")]
    if period["stock_n"]:   # suivi separe : jamais dans le facture
        cards.append(("Stock consommé", amount(period["stock_used"]), f"kamas, {period['stock_n']} session(s) renseignée(s)"))
    parts.append('<div class="kpis">' + "".join(
        f'<div class="kpi"><span class="lab">{esc(t)}</span><b>{v}</b><small>{esc(sub)}</small></div>' for t, v, sub in cards) + "</div>")

    # ---- chronologie ----
    parts.append("<h2>Chronologie</h2>")
    if not report["timeline"]:
        parts.append('<div class="empty">Aucune session sur cette période.</div>')
    else:
        days = {}
        for when, record in report["timeline"]:
            days.setdefault(when.date(), []).append((when, record))
        for day, rows in days.items():
            title = f"{DAYS_FR[day.weekday()].capitalize()} {day.day} {MONTHS_FR[day.month - 1]} {day.year}"
            day_total = sum(record_amount(r) for _w, r in rows)
            parts.append(f'<section class="day"><h3><span class="d">{esc(title)}</span>'
                         f'<span class="daytotal">{amount(day_total)} kamas</span></h3><ol class="tl">')
            for when, r in rows:
                if r.get("live"):
                    badge = '<span class="badge live">en cours</span>'
                elif r.get("why") == "deleted":
                    badge = '<span class="badge del">salon supprimé</span>'
                else:
                    badge = '<span class="badge">terminée</span>'
                owner = owner_name_of(r.get("owner")) if r.get("owner") else None
                counts = r.get("attempts") or {}
                tries = sum(counts.values())
                # Carte epuree : le total a droite suffit. Ni depense, ni acompte, ni ajustement, ni stock (suivi du stock : carte de synthese).
                chips = []
                if tries:
                    chips.append('<span class="chip">' + esc("  ".join(
                        f"{emoji} {counts.get(key, 0)}" for key, _l, emoji, _st in FM_OUTCOMES)) + "</span>")
                if owner:
                    chips.append(f'<span class="chip who">{esc(owner)}</span>')
                linked = r.get("item")
                tile, uri, label = "", None, ""
                if linked:
                    uri = (item_icons or {}).get(linked["id"])
                    lvl = f" · niv. {linked['level']}" if linked.get("level") else ""
                    label = linked["name"] + lvl
                    chips.insert(0, f'<span class="chip item">{esc(label)}</span>')
                inner = f'<img src="{uri}" alt="" width="64" height="64">' if uri else ""
                if str(r.get("ch")) in shots:   # capture de l'equipement termine : vignette cliquable (appareil photo si pas d'icone)
                    tile = shot_link(r.get("ch"), inner or '<span class="camph" aria-hidden="true">📷</span>', r, when,
                                     cls="tile", title=label)
                elif inner:   # grande vignette carree entre le contenu et le montant
                    tile = f'<div class="tile" title="{esc(label)}">{inner}</div>'
                top = list((r.get("items") or {}).items())[:3]
                items_line = ""
                if top:
                    items_line = ('<div class="meta">' + esc(" · ".join(
                        f"{name} ({_kamas_short(price)})" for name, (_q, price) in top)) + "</div>")
                parts.append(
                    ('<li class="has-tile">' if tile else "<li>") + '<div class="main"><div class="row1">'
                    f'<span class="when">{when.strftime("%H:%M")}</span>'
                    f'<span class="name">#{esc(r.get("name") or "salon")}</span>{badge}</div>'
                    f'<div class="chips">{"".join(chips)}</div>{items_line}</div>'
                    + tile + f'<div class="amt">{amount(record_amount(r))} kamas</div></li>')
            parts.append("</ol></section>")

    # ---- graphique : depense par mois ----
    chart = report["chart"]
    width, height, top, bottom, left = 720, 210, 26, 34, 8
    biggest = max((a["total"] for _k2, a in chart), default=0) or 1
    slot = (width - 2 * left) / len(chart)
    svg = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Montant dépensé par mois sur 12 mois">']
    for i, ((year, month), agg) in enumerate(chart):
        bar_h = (height - top - bottom) * agg["total"] / biggest
        x, w = left + i * slot + slot * 0.14, slot * 0.72
        y = height - bottom - bar_h
        a, b = _period_bounds("month", (year, month), tz)
        selected = _overlaps(start, end, a, b) and start is not None
        name = f"{MONTHS_FR[month - 1].capitalize()} {year}"
        svg.append(f'<rect class="bar{" sel" if selected else ""}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                   f'height="{bar_h:.1f}" rx="4"><title>{esc(name)} : {format_number(agg["total"])} kamas</title></rect>')
        if agg["total"]:
            svg.append(f'<text class="val" x="{x + w / 2:.1f}" y="{y - 5:.1f}">{esc(_kamas_short(agg["total"]))}</text>')
        label = MONTHS_SHORT_FR[month - 1] + (f" {str(year)[2:]}" if month == 1 or i == 0 else "")
        svg.append(f'<text class="lbl" x="{x + w / 2:.1f}" y="{height - 12}">{esc(label)}</text>')
    svg.append("</svg>")
    parts.append("<h2>Dépensé par mois</h2>")
    parts.append('<div class="chart">' + "".join(svg) + "</div>")

    # ---- totaux par mois / trimestre / annee ----
    def sel(kind, key):
        a, b = _period_bounds(kind, key, tz)
        return start is not None and _overlaps(start, end, a, b)

    parts.append('<p class="hint">Clique sur un en-tête de colonne pour trier un tableau (un second clic inverse le tri).</p>')
    for title, first_header, kind, data, label_fn, order_fn in (
            ("Par mois", "Mois", "month", report["by_month"],
             lambda k: f"{MONTHS_FR[k[1] - 1].capitalize()} {k[0]}", lambda k: k[0] * 100 + k[1]),
            ("Par trimestre", "Trimestre", "quarter", report["by_quarter"],
             lambda k: f"T{k[1]} {k[0]}", lambda k: k[0] * 10 + k[1]),
            ("Par année", "Année", "year", report["by_year"], lambda k: str(k), lambda k: k)):
        parts.append(f"<h2>{title}</h2>")
        if not data:
            parts.append('<div class="empty">Aucune donnée.</div>')
            continue
        rows = [([(esc(label_fn(k)), order_fn(k))] + agg_cells(data[k]), sel(kind, k)) for k in sorted(data, reverse=True)]
        parts.append(sortable_table([(first_header, "num", "Tri chronologique")] + agg_heads, rows))

    # ---- par joueur / objets ----
    if len(report["players"]) > 1:
        parts.append("<h2>Par joueur (période affichée)</h2>")
        ordered = sorted(report["players"].items(), key=lambda kv: -(kv[1]["total"] + kv[1]["adjust"]))
        rows = []
        for owner, agg in ordered:
            name = owner_name_of(owner) if owner else "(salon sans propriétaire)"
            rows.append(([(esc(name), name)] + agg_cells(agg), False))
        parts.append(sortable_table([("Joueur", "text", "")] + agg_heads, rows))
    if report["top_items"]:
        parts.append("<h2>Objets les plus dépensés (période affichée)</h2>")
        rows = [([(esc(name), name), (format_number(qty), qty), (amount(price), price)], False)
                for name, (qty, price) in report["top_items"]]
        parts.append(sortable_table([("Objet", "text", ""), ("Quantité", "num", ""), ("Dépensé", "num", "")], rows))

    # ---- equipements associes aux salons (/fmitem) ----
    if report.get("by_item"):
        parts.append("<h2>Équipements forgés (période affichée)</h2>")
        rows = []
        for slot in sorted(report["by_item"].values(), key=lambda sl: -(sl["agg"]["total"] + sl["agg"]["adjust"])):
            it, agg = slot["item"], slot["agg"]
            uri = (item_icons or {}).get(it["id"])
            pic = (f'<img class="ico" alt="" src="{uri}" width="36" height="36">' if uri
                   else '<span class="ico ph" title="Image indisponible">?</span>')
            lvl = f' <span class="lv">niv. {it["level"]}</span>' if it.get("level") else ""
            with_shot = [(w, rr) for w, rr in slot["records"] if str(rr.get("ch")) in shots]
            if with_shot:   # la capture la plus recente de cet equipement
                w_best, r_best = max(with_shot, key=lambda x: x[0])
                pic = shot_link(r_best.get("ch"), pic, r_best, w_best)
            rows.append(([(f'<span class="rname">{pic}<span>{esc(it["name"])}{lvl}</span></span>', it["name"])]
                         + agg_cells(agg), False))
        parts.append(sortable_table([("Équipement", "text", "")] + agg_heads, rows))

    # ---- galerie des runes (a la fin) ----
    gallery_notes = []
    if runes and runes.get("rows"):
        parts.append("<h2>Runes achetées (période affichée)</h2>")
        rows = []
        for r in runes["rows"]:
            if r["icon"]:
                icon = f'<img class="ico" alt="" src="{r["icon"]}" width="36" height="36">'
            else:
                icon = '<span class="ico ph" title="Image indisponible">?</span>'
            flag = ' <span class="badge del">nom inconnu de DofusDB</span>' if r["status"] == "unknown" else ""
            average = int((Decimal(r["price"]) / Decimal(r["qty"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if r["qty"] else 0
            rows.append(([(f'<span class="rname">{icon}<span>{esc(r["name"])}{flag}</span></span>', r["name"]),
                          (format_number(r["qty"]), r["qty"]), (amount(r["price"]), r["price"]),
                          (amount(average), average)], False))
        parts.append(sortable_table([("Rune", "text", ""), ("Quantité", "num", ""), ("Dépensé", "num", ""),
                                     ("Prix moyen / unité", "num", "Dépensé ÷ quantité")], rows))
        if any(r["status"] == "unknown" for r in runes["rows"]):
            gallery_notes.append("Une rune marquée « nom inconnu de DofusDB » ne correspond à aucune rune connue : "
                                 "c'est souvent une faute de lecture de la capture, à vérifier.")
        gallery_notes.extend(esc(f"Images : {n}.") for n in (runes.get("notes") or []))

    # ---- notes ----
    first = report["first"].strftime("%d/%m/%Y") if report["first"] else "—"
    notes = [f"Données enregistrées depuis le {first} : les sessions fermées avant la mise en place de l'historique n'y figurent pas.",
             "Une session compte à la date de sa fermeture (à la date de son début si elle est en cours).",
             "Dépensé = achats lus sur les captures. Facturé = dépensé + ajustements (/fmpay -X). Acomptes = paiements enregistrés "
             "avec /fmpay pendant la session ; le règlement final du client n'est pas enregistré dans le bot. Stock consommé = "
             "stock de départ - stock restant : un suivi séparé, jamais compté dans le facturé.",
             f"Mois, trimestres et années sont lus en heure de {esc(str(tz))}."]
    if (item_icons or any(r["icon"] for r in (runes or {}).get("rows", []))):
        if report["sample"]:
            gallery_notes.insert(0, "Images d'exemple (pictogrammes fictifs) : dans un vrai bilan, ce sont les illustrations "
                                    "fournies par DofusDB.")
        else:
            gallery_notes.insert(0, "Images : DofusDB (illustrations © Ankama), chargées à la demande puis conservées en cache.")
    if report.get("unlinked") and (report.get("by_item") or report.get("items_in_use")):
        shown = report["unlinked"][:8]
        more = f" (+{len(report['unlinked']) - 8})" if len(report["unlinked"]) > 8 else ""
        gallery_notes.append(esc(f"{len(report['unlinked'])} salon(s) sans équipement associé : "
                                 + ", ".join("#" + n for n in shown) + more
                                 + ". Associe-les avec /fmitem (même un salon archivé)."))
    if report.get("shots_skipped"):
        gallery_notes.append(f"{report['shots_skipped']} capture(s) d'équipement non intégrée(s) pour tenir dans la taille "
                             "maximale d'un fichier Discord : relance le bilan sur une période plus courte pour les voir.")
    notes.extend(gallery_notes)
    if report["undated"]:
        notes.append(f"{report['undated']} session(s) sans date lisible n'apparaissent pas dans la chronologie ni dans les totaux.")
    parts.append('<ul class="notes">' + "".join(f"<li>{n}</li>" for n in notes) + "</ul>")

    # Captures : UNE seule copie de chaque image (dans la reserve en fin de page), referencee par les liens des
    # cartes et du tableau. Apercu au survol (:has) pour la souris, grand format au clic ou au toucher (:target).
    pool, hover_css = [], []
    for ch, sid in shot_ids.items():
        pool.append(f'<div class="lb" id="{sid}"><a class="lbx" href="#fermer" aria-label="Fermer"></a><figure>'
                    f'<img src="{shots[ch]}" alt="Capture de l\'équipement terminé">'
                    f'<figcaption><span>{esc(shot_notes[ch])}</span><a href="#fermer">Fermer ✕</a></figcaption></figure></div>')
        sel = (f'body:has(a.shot[href="#{sid}"]:hover) #{sid}:not(:target),'
               f'body:has(a.shot[href="#{sid}"]:focus-visible) #{sid}:not(:target)')
        sel_fig = ",".join(f"{part} figure" for part in sel.split(","))
        sel_cap = ",".join(f"{part} figcaption a" for part in sel.split(","))
        sel_img = ",".join(f"{part} img" for part in sel.split(","))
        # Largeur de l'apercu = espace libre a droite de la page (980 px centres), entre 300 et 460 px : sur grand ecran
        # il ne recouvre rien, sur petite fenetre il reste lisible.
        hover_css.append(f"{sel}{{display:flex;inset:auto 20px 20px auto;background:none;padding:0;pointer-events:none}}"
                         f"{sel_fig}{{max-width:clamp(300px,calc((100vw - 1000px)/2 - 30px),460px)}}"
                         f"{sel_img}{{max-height:70vh}}{sel_cap}{{display:none}}")
    shot_css = ("@media (hover:hover){" + "".join(hover_css) + "}") if hover_css else ""
    return ("<!DOCTYPE html>\n<html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
            f"img-src data:; script-src '{REPORT_JS_HASH}'\">"
            f"<title>Bilan FM · {esc(report['label'])}</title><style>{REPORT_CSS}{shot_css}</style></head><body><main>"
            + "".join(parts) + f"</main>{''.join(pool)}<script>{REPORT_JS}</script></body></html>")

# ---------------- CORRECTIONS DE SESSION : /fmreset confirme, /fmundo, /fmadd ---------------- #
CAPTURE_HISTORY_CAP = 40   # captures annulables par session (les plus recentes)

def reset_session_state(channel_id):
    sessions[channel_id] = empty_session()
    touch_channel(channel_id)
    save_data()

async def after_reset(interaction, channel_id):
    """Apres une remise a zero : previent le site, pose la question du stock, remet le panneau a jour."""
    await send_event(site_event("session-reset", interaction.id, interaction.channel, interaction.created_at))
    await post_stock_prompt(interaction.channel, sessions[channel_id])
    await sync_attempt_panel(interaction.channel, sessions[channel_id])

async def perform_reset(interaction, channel_id):
    """Remet la session a zero (le journal des statistiques est conserve) puis previent site, stock et panneau."""
    reset_session_state(channel_id)
    await after_reset(interaction, channel_id)

class ResetConfirmView(discord.ui.View):
    """Confirmation avant d'effacer une session : rien n'est supprime sans un clic explicite."""
    def __init__(self, user_id, channel_id):
        super().__init__(timeout=60)
        self.user_id, self.channel_id = user_id, channel_id
        self.done = False
        self.reply_message = None

    async def _allowed(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Seul l'auteur de la commande peut confirmer ou annuler.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Déjà traité.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Oui, tout effacer", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        self.done = True
        self.stop()
        reset_session_state(self.channel_id)
        await interaction.response.edit_message(content="Session réinitialisée !", view=None)
        await after_reset(interaction, self.channel_id)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._allowed(interaction):
            return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Remise à zéro annulée : rien n'a été effacé.", view=None)

    async def on_timeout(self):
        if not self.done and self.reply_message is not None:
            try:
                await self.reply_message.edit(content="⌛ Confirmation expirée : rien n'a été effacé.", view=None)
            except discord.HTTPException:
                pass

def undo_last_capture(session):
    """Retire de la session la derniere capture (ou ligne ajoutee a la main). Retourne la capture retiree, ou None."""
    captures = session.get("captures") or []
    if not captures:
        return None
    last = captures.pop()
    entries = last.get("entries") or []
    added = sum(e["price"] for e in entries)
    session["total"] = max(0, session["total"] - added)
    users = session.get("users") or {}
    uid = last.get("by")
    if uid in users:
        users[uid] -= added
        if users[uid] <= 0:
            users.pop(uid, None)
    runes = session.get("runes") or {}
    for name, vals in aggregate_entries(entries).items():
        slot = runes.get(name)
        if slot is None:
            continue
        slot["qty"] -= vals["qty"]
        slot["price"] -= vals["price"]
        if slot["qty"] <= 0 and slot["price"] <= 0:
            runes.pop(name, None)
    seen = session.get("seen") or []
    for h in last.get("hashes") or []:   # l'image peut etre renvoyee : elle ne sera plus vue comme un doublon
        if h in seen:
            seen.remove(h)
    return last

ADD_LINE_A = re.compile(r"^\s*\[?\s*(.+?)\s*\]?\s+[xX\u00d7]\s*(\d{1,7})\s+(\S.*?)\s*$")   # Rune Ga Pa x100 2M45
ADD_LINE_B = re.compile(r"^\s*(\d{1,7})\s*[xX\u00d7]\s+(\S.*?)\s*$")   # 100 x Rune Ga Pa 2M45
ADD_HELP = "Format : `/fmadd ligne:Rune Ga Pa x100 2M45` (objet, `x` quantité, montant TOTAL de la ligne : 2M45, 2 450 000, 2.45M…)."

def _split_name_and_price(text):
    """Separe « Rune Ga Pa 2 450 000 » en (nom, prix) : le prix est le PLUS LONG montant valide en fin de ligne."""
    tokens = text.split()
    for cut in range(1, len(tokens)):
        price = parse_kamas_amount(" ".join(tokens[cut:]))
        if price is not None:
            return " ".join(tokens[:cut]), price
    return None

def parse_add_line(text):
    """(nom, quantite, prix total) d'une ligne saisie a la main. Leve ValueError avec un message pret a afficher."""
    m = ADD_LINE_A.match(text or "")
    if m:
        name, qty, price_text = m.group(1), int(m.group(2)), m.group(3)
        price = parse_kamas_amount(price_text)
        if price is None:
            raise ValueError(f"Montant non compris : « {price_text} ». {ADD_HELP}")
    else:
        m = ADD_LINE_B.match(text or "")
        if not m:
            raise ValueError(f"Ligne non comprise : « {text} ». {ADD_HELP}")
        qty = int(m.group(1))
        split = _split_name_and_price(m.group(2))
        if split is None:
            raise ValueError(f"Montant non compris dans « {m.group(2)} ». {ADD_HELP}")
        name, price = split
    name = re.sub(r"\s+", " ", name).strip(" []")
    if not name or len(name) > 60:
        raise ValueError(f"Nom d'objet absent ou trop long. {ADD_HELP}")
    if qty < 1:
        raise ValueError(f"La quantité doit être au moins 1. {ADD_HELP}")
    return name, qty, price

def resolve_added_name(session, name):
    """
    Nom d'objet a retenir pour un ajout manuel : (nom, avertissement, refus). Un nom deja vu dans la session ou connu
    de DofusDB reprend son orthographe officielle (les lignes se regroupent) ; une rune inconnue mais proche d'une
    rune connue est REFUSEE avec des suggestions plutot que d'ajouter une ligne a part.
    """
    key = rune_key(name)
    for known in (session.get("runes") or {}):
        if rune_key(known) == key:
            return known, None, None
    catalog = fm_state().get("rune_catalog") or {}
    if key in catalog:
        return catalog[key]["name"], None, None
    if not key.startswith("rune"):
        return name, None, None   # ressource ou autre objet : la liste de DofusDB ne couvre que les runes
    if not catalog:
        return name, "liste des runes de DofusDB indisponible : nom non vérifié", None
    close = difflib.get_close_matches(key, list(catalog), n=3, cutoff=0.7)
    if close:
        suggestions = " ou ".join(f"« {catalog[c]['name']} »" for c in close)
        return None, None, f"Rune inconnue : « {name} ». Tu voulais dire {suggestions} ? Relance avec le nom exact."
    return name, "nom inconnu de DofusDB : ajouté tel quel, vérifie l'orthographe", None

# ---------------- RUNE VISEE ET AJUSTEMENT DES TENTATIVES ---------------- #
ATTEMPT_DELTA_MAX = 500
DELTA_RE = re.compile(r"^\s*([+\-\u2212\u2013])?\s*(\d{1,4})\s*$")

def parse_attempt_delta(text):
    """« +20 », « -3 », « 20 » -> entier non nul (|n| <= 500). Leve ValueError avec un message pret a afficher."""
    m = DELTA_RE.match(text or "")
    if not m:
        raise ValueError(f"Nombre non compris : « {text} ». Exemples : `+20`, `-3`, `20` (le + est facultatif).")
    value = int(m.group(2)) * (-1 if m.group(1) in ("-", "\u2212", "\u2013") else 1)
    if value == 0:
        raise ValueError("Le nombre doit être différent de zéro.")
    if abs(value) > ATTEMPT_DELTA_MAX:
        raise ValueError(f"Maximum {ATTEMPT_DELTA_MAX} tentatives à la fois (pour éviter une faute de frappe).")
    return value

def adjust_attempts(session, channel_id, user_id, key, delta, at):
    """
    Ajoute (delta > 0) ou retire (delta < 0) des tentatives d'une issue. Retourne le nombre REELLEMENT applique
    (signe conserve) : un retrait ne descend jamais sous zero. Les ajouts sont marques « en bloc » : comptes
    partout, mais sans heure reelle, donc exclus du calcul du rythme.
    """
    attempts = attempts_of(session)
    counts = attempts["counts"]
    if delta > 0:
        rune = attempts.get("target")
        counts[key] = counts.get(key, 0) + delta
        for _ in range(delta):
            attempts["history"].append({"k": key, "at": at, "by": str(user_id), "b": 1, **({"r": rune} if rune else {})})
            log_attempt(channel_id, key, user_id, at, rune=rune, bulk=True)
        del attempts["history"][:-ATTEMPT_HISTORY_CAP]
        return delta
    applied = 0
    for _ in range(-delta):
        if counts.get(key, 0) <= 0:
            break
        counts[key] -= 1
        for i in range(len(attempts["history"]) - 1, -1, -1):
            if attempts["history"][i].get("k") == key:
                del attempts["history"][i]
                break
        unlog_latest(channel_id, key)
        applied -= 1
    return applied

def attempts_summary_line(session):
    counts = attempts_of(session)["counts"]
    detail = " · ".join(f"{emoji} {counts.get(key, 0)}" for key, _l, emoji, _st in FM_OUTCOMES)
    return f"total {attempts_total(session)} ({detail})"

def rune_autocomplete_choices(session, current, limit=25):
    """Runes deja achetees dans la session, puis runes connues de DofusDB, contenant le texte saisi."""
    needle = rune_key(current)
    names, seen = [], set()
    for name in sorted((session or {}).get("runes") or {}):
        if needle in rune_key(name) and rune_key(name) not in seen:
            names.append(name); seen.add(rune_key(name))
    catalog = fm_state().get("rune_catalog") or {}
    for key in sorted(catalog):
        if needle in key and key not in seen:
            names.append(catalog[key]["name"]); seen.add(key)
    return names[:limit]

# ---------------- RECAPITULATIF CLIENT : /fmrecap ---------------- #
# Une page imprimable pour UNE commande, a transmettre au client : achats detailles, stock apporte, acomptes et
# MONTANT A REGLER. Garantie d'exactitude : les lignes affichees additionnent toujours au total (un ecart eventuel
# devient une ligne explicite), et chaque decompte additionne aux totaux enregistres.
RECAP_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f3f4f8;color:#1d2333;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.sheet{max-width:820px;margin:24px auto;background:#fff;padding:36px 42px;border:1px solid #e3e6ee;border-radius:12px}
h1{font-size:1.6rem;margin:0}
h2{font-size:1.05rem;margin:30px 0 8px;text-transform:uppercase;letter-spacing:.05em;color:#667085}
.ref{color:#667085;margin:2px 0 0}
.banner{background:#fff7e6;border:1px solid #f0c36d;border-radius:10px;padding:9px 14px;margin:16px 0 0}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px 28px;margin:20px 0 4px}
.meta div{border-bottom:1px solid #eceef4;padding-bottom:6px}
.meta span{display:block;color:#667085;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
.meta b{font-weight:650}
.itemcard{display:flex;gap:16px;align-items:center;border:1px solid #e3e6ee;border-radius:12px;padding:12px 16px;margin:18px 0 0}
.tile{flex:none;display:flex;align-items:center;justify-content:center;width:80px;height:80px;background:#f6f7fb;border:1px solid #e3e6ee;border-radius:14px}
.tile img{width:64px;height:64px;object-fit:contain}
.itemcard strong{font-size:1.15rem;display:block}
.itemcard small{color:#667085}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 6px;border-bottom:1px solid #eceef4;text-align:left;vertical-align:middle}
th{color:#667085;font-size:.76rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.r{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.rname{display:flex;align-items:center;gap:10px}
.ico{width:32px;height:32px;object-fit:contain;flex:none}
.ph{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border:1px dashed #cfd3de;border-radius:8px;color:#98a2b3;font-size:.8rem;flex:none}
tr.sub td{font-weight:650;border-bottom:2px solid #1d2333}
.plus{color:#12805c}
.moins{color:#b54708}
.grand td{font-size:1.3rem;font-weight:800;border-top:2px solid #1d2333;border-bottom:0;padding-top:12px}
.attempts{display:flex;gap:10px;flex-wrap:wrap}
.attempts span{border:1px solid #e3e6ee;border-radius:10px;padding:6px 12px;background:#f6f7fb}
.foot{margin-top:30px;color:#667085;font-size:.82rem;border-top:1px solid #eceef4;padding-top:10px}
.note{color:#667085;font-size:.82rem;margin:6px 0 0}
@page{size:A4;margin:16mm}
.keep{break-inside:avoid}
h2{break-after:avoid}
@media print{body{background:#fff;font-size:12px;line-height:1.35}.sheet{margin:0;border:0;border-radius:0;padding:0;max-width:none}
tr,.itemcard,.keep{break-inside:avoid}h1{font-size:1.3rem}h2{margin:14px 0 4px}.banner{margin-top:8px;padding:5px 12px}
.meta{margin:10px 0 0;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.itemcard{margin-top:10px;padding:6px 12px}
.tile{width:56px;height:56px}.tile img{width:46px;height:46px}th,td{padding:2px 6px}.ico,.ph{width:22px;height:22px}
.grand td{font-size:1.1rem;padding-top:6px}.foot{margin-top:12px}}
@media (max-width:560px){.sheet{margin:0;border-radius:0;padding:22px 16px}.avg{display:none}th{white-space:normal}}
@media (max-width:400px){.sheet{padding:18px 12px}.grand td{font-size:1.05rem}h1{font-size:1.35rem}}
"""

def build_recap(record, tz, now, forgemage, client="", item=None, show_attempts=False, provisional=False, sample=False):
    """Toutes les donnees du recapitulatif, calculees a partir d'un enregistrement de session (terminee ou en cours)."""
    items = record.get("items") or {}
    lines = [{"name": n, "qty": q, "price": p} for n, (q, p) in sorted(items.items(), key=lambda kv: -kv[1][1])]
    total = record["total"]
    other = total - sum(line["price"] for line in lines)   # achats non detailles (au-dela des 40 objets gardes, anciennes sessions)
    adjust_total, paid_total = record.get("adjust", 0), record.get("paid", 0)
    payments = record.get("payments")
    if payments is None:   # session fermee avant l'enregistrement du detail : on ne connait que les totaux
        adjustments = [{"amount": adjust_total, "note": "Ajustements", "at": None}] if adjust_total else []
        deposits = [{"amount": paid_total, "note": "Acomptes versés", "at": None}] if paid_total else []
    else:
        adjustments = [{"amount": -p["amount"], "note": p.get("note") or "", "at": p.get("at")} for p in payments if p["amount"] < 0]
        deposits = [{"amount": p["amount"], "note": p.get("note") or "", "at": p.get("at")} for p in payments if p["amount"] > 0]
        for rows, expected, label in ((adjustments, adjust_total, "Autres ajustements"), (deposits, paid_total, "Autres acomptes")):
            gap = expected - sum(r["amount"] for r in rows)   # detail tronque : la difference reste visible
            if gap:
                rows.append({"amount": gap, "note": label, "at": None})
    stock = stock_of(record)   # suivi separe : n'entre JAMAIS dans « due »
    used = stock_used(stock)
    stock_figures = ({"start": stock.get("start"), "end": stock.get("end"), "used": used,
                      "runes_used": total + used if used is not None else None} if stock else None)
    return {
        "stock": stock_figures,
        "forgemage": forgemage, "client": client.strip(), "reference": record.get("name") or "", "when": record_when(record, tz) or now,
        "now": now, "tz": tz, "item": item, "lines": lines, "other": other, "total": total,
        "adjustments": adjustments, "deposits": deposits, "due": total + adjust_total - paid_total,
        "attempts": record.get("attempts") if show_attempts else None, "provisional": provisional, "sample": sample,
    }

def render_recap_html(recap, icons=None, item_icon=None):
    esc = html_lib.escape
    icons = icons or {}
    def amt(n):
        return f'<span class="n">{format_number(n)}</span> kamas'
    when = recap["when"]
    date_text = f"{when.day} {MONTHS_FR[when.month - 1]} {when.year}"

    head = [f"<h1>Récapitulatif de commande</h1><p class=\"ref\">Référence : {esc(recap['reference'] or '—')}</p>"]
    if recap["sample"]:
        head.append('<div class="banner"><b>Exemple</b> : données et icônes fictives, pour illustrer le rendu.</div>')
    if recap["provisional"]:
        head.append('<div class="banner"><b>Montants provisoires</b> : la session est encore en cours, '
                    'ce document sera mis à jour à sa clôture.</div>')
    meta = [("Forgemage", recap["forgemage"] or "—"), ("Date", date_text)]
    if recap["client"]:
        meta.insert(0, ("Client", recap["client"]))
    head.append('<div class="meta">' + "".join(f"<div><span>{esc(k)}</span><b>{esc(v)}</b></div>" for k, v in meta) + "</div>")

    body = []
    item = recap["item"]
    if item:
        pic = (f'<div class="tile"><img src="{item_icon}" alt="" width="64" height="64"></div>' if item_icon else "")
        detail = " · ".join(x for x in (f"niveau {item['level']}" if item.get("level") else None, item.get("type_name")) if x)
        body.append(f'<div class="itemcard">{pic}<div><small>Équipement forgé</small><strong>{esc(item["name"])}</strong>'
                    f'<small>{esc(detail)}</small></div></div>')

    rows = []
    for line in recap["lines"]:
        icon = icons.get(rune_key(line["name"]))
        pic = f'<img class="ico" alt="" src="{icon}" width="32" height="32">' if icon else ""
        avg = int((Decimal(line["price"]) / Decimal(line["qty"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if line["qty"] else 0
        rows.append(f'<tr><td><span class="rname">{pic}<span>{esc(line["name"])}</span></span></td>'
                    f'<td class="r">{format_number(line["qty"])}</td><td class="r avg">{format_number(avg)}</td>'
                    f'<td class="r">{format_number(line["price"])}</td></tr>')
    if recap["other"]:
        label = "Autres achats" if recap["other"] > 0 else "Correction"
        rows.append(f'<tr><td>{label}</td><td class="r"></td><td class="r avg"></td><td class="r">{format_number(recap["other"])}</td></tr>')
    rows.append(f'<tr class="sub"><td colspan="2">Total des achats</td><td class="r avg"></td><td class="r">{format_number(recap["total"])}</td></tr>')
    body.append("<h2>Détail des achats</h2><table><thead><tr><th>Objet</th><th class=\"r\">Quantité</th>"
                "<th class=\"r avg\">Prix moyen / unité</th><th class=\"r\">Montant</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    settle = [f'<tr><td>Total des achats</td><td class="r">{amt(recap["total"])}</td></tr>']
    for a in recap["adjustments"]:
        note = f" · {esc(a['note'])}" if a["note"] else ""
        settle.append(f'<tr><td class="plus">Ajustement{note}</td><td class="r plus">+ {amt(a["amount"])}</td></tr>')
    for d in recap["deposits"]:
        note = f" · {esc(d['note'])}" if d["note"] else ""
        try:
            day = datetime.datetime.fromisoformat(d["at"]).astimezone(recap["tz"]).strftime("%d/%m/%Y") if d["at"] else None
        except (ValueError, TypeError):
            day = None
        when_txt = f" du {day}" if day else ""
        settle.append(f'<tr><td class="moins">Acompte{when_txt}{note}</td><td class="r moins">− {amt(d["amount"])}</td></tr>')
    due = recap["due"]
    if due >= 0:
        settle.append(f'<tr class="grand"><td>Montant à régler</td><td class="r">{amt(due)}</td></tr>')
    else:
        settle.append(f'<tr class="grand"><td>Trop-perçu (en votre faveur)</td><td class="r">{amt(-due)}</td></tr>')
    body.append("<section class=\"keep\"><h2>Décompte</h2><table><tbody>" + "".join(settle) + "</tbody></table></section>")

    figures = recap.get("stock")
    if figures:   # information separee : le stock n'entre pas dans le montant a regler
        start, end, used_value = figures["start"], figures["end"], figures["used"]
        rows_stock = [f'<tr><td>Stock de départ</td><td class="r">{amt(start) if start is not None else "non renseigné"}</td></tr>',
                      f'<tr><td>Stock restant</td><td class="r">{amt(end) if end is not None else "non renseigné"}</td></tr>']
        if used_value is not None:
            rows_stock.append(f'<tr class="sub"><td>{"Stock consommé" if used_value >= 0 else "Stock en hausse (achats non utilisés)"}</td>'
                              f'<td class="r">{amt(abs(used_value))}</td></tr>')
            rows_stock.append(f'<tr><td>Runes utilisées au total (achats + stock consommé)</td><td class="r">{amt(figures["runes_used"])}</td></tr>')
        body.append('<section class="keep"><h2>Stock de runes</h2><table><tbody>' + "".join(rows_stock)
                    + '</tbody></table><p class="note">Information à part : le stock n\'entre pas dans le montant à régler.</p></section>')

    if recap["attempts"]:
        counts = recap["attempts"]
        chips = "".join(f"<span>{emoji} {esc(label)} : <b>{counts.get(key, 0)}</b></span>" for key, label, emoji, _st in FM_OUTCOMES)
        body.append(f"<section class=\"keep\"><h2>Déroulé de la forgemagie</h2><div class=\"attempts\"><span>Tentatives : <b>{sum(counts.values())}</b></span>{chips}</div></section>")

    foot = (f'<div class="foot">Document généré le {recap["now"].strftime("%d/%m/%Y à %H:%M")} · montants exprimés en kamas.</div>')
    return ("<!DOCTYPE html>\n<html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
            f"<title>Récapitulatif · {esc(recap['reference'])}</title><style>{RECAP_CSS}</style></head><body><main class=\"sheet\">"
            + "".join(head) + "".join(body) + foot + "</main></body></html>")

# ---------------- STOCK DE DEPART ET CAPTURE DE L'EQUIPEMENT TERMINE ---------------- #
# Le stock de runes en inventaire est un SUIVI : valeur au depart, valeur restante a l'arret, donc consommation.
# Il n'entre JAMAIS dans le montant a regler (ni comme ajustement, ni comme paiement) : un stock a facturer au client
# se declare explicitement avec /fmpay -X. Stocke dans la session (puis dans l'historique) sous la cle "stock" :
# {"start": valeur au depart, "end": valeur restante} ; une cle absente = non renseigne.
STOCK_NOTE = "stock de départ"   # ancienne note d'ajustement : ne sert plus qu'a migrer les sessions en cours
STOCK_NONE_WORDS = {"0", "aucun", "aucune", "non", "rien", "-"}
STOCK_PROMPT_TEXT = (
    "💰 **Stock de runes déjà en inventaire ?**\n"
    "Si tu as déjà des runes en stock pour cette FM, indique leur **valeur totale**. Elle sert à suivre ta consommation "
    "de stock (`/fmstop` te demandera ce qu'il en reste) : elle n'est **pas** ajoutée au montant à régler.\n"
    "Sinon, clique sur « Aucun stock ». Tu peux aussi le renseigner plus tard avec `/fmstock`.")
STOCK_END_TEXT = (
    "📦 **Stock restant ?**\n"
    "Tu avais **{start} kamas** de runes en stock au départ. Indique la valeur totale de ce qu'il te reste en inventaire "
    "pour calculer la consommation de stock. Cela ne change pas le montant à régler.")

def stock_of(holder):
    """Stock declare d'une session ou d'un enregistrement d'historique : {} si rien n'a ete renseigne."""
    stock = (holder or {}).get("stock")
    return stock if isinstance(stock, dict) else {}

def set_stock(holder, field, value):
    """Renseigne (ou efface, si value est None) le stock de depart ("start") ou restant ("end")."""
    stock = holder.setdefault("stock", {})
    if value is None:
        stock.pop(field, None)
    else:
        stock[field] = value
    if not stock:
        holder.pop("stock", None)

def stock_used(stock):
    """Stock consomme = depart - restant (negatif : le stock a augmente). None si l'une des deux valeurs manque."""
    start, end = stock.get("start"), stock.get("end")
    if isinstance(start, int) and isinstance(end, int):
        return start - end
    return None

def stock_summary_lines(stock, purchases=None):
    """Lignes de texte du suivi de stock ([] si rien n'est declare). purchases : total des achats de la session."""
    start, end = stock.get("start"), stock.get("end")
    if start is None and end is None:
        return []
    head = ["départ " + (format_number(start) if start is not None else "non renseigné"),
            "restant " + (format_number(end) if end is not None else "à renseigner")]
    lines = [f"STOCK : {' · '.join(head)} kamas (suivi séparé : hors montant à payer)"]
    used = stock_used(stock)
    if used is not None:
        lines.append(f"Stock consommé : {format_number(used)} kamas" if used >= 0
                     else f"Stock en hausse de {format_number(-used)} kamas (achats non utilisés)")
        if purchases is not None:
            lines.append(f"Runes utilisées au total (achats + stock consommé) : {format_number(purchases + used)} kamas")
    return lines

def migrate_stock_adjustments():
    """
    Version precedente : le stock de depart etait enregistre comme un AJUSTEMENT (donc ajoute au montant du).
    Les sessions EN COURS qui l'avaient declare sont converties une fois : l'ajustement disparait des paiements et sa
    valeur devient le stock de depart. Idempotent. Les sessions deja fermees ne sont pas touchees : on ne peut pas
    distinguer un stock declare par la question d'un ajustement saisi a la main avec /fmpay.
    """
    changed = 0
    for key, session in list(sessions.items()):
        if key == "_fm" or not isinstance(session, dict) or "stock" in session:
            continue
        prompt = session.get("stock_prompt")
        amount = prompt.get("amount") if isinstance(prompt, dict) else None
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            continue
        payments = session.get("payments") or []
        for i, payment in enumerate(payments):
            if payment.get("amount") == -amount and payment.get("note") == STOCK_NOTE:
                del payments[i]
                set_stock(session, "start", amount)
                changed += 1
                break
    if changed:
        save_data()
        print(f"[STOCK] {changed} session(s) en cours migree(s) : le stock de depart n'est plus compte dans le montant a regler.")
    return changed

async def post_stock_prompt(channel, session):
    """Pose la question du stock de depart. Ne leve JAMAIS : c'est un confort, jamais un prerequis."""
    try:
        message = await channel.send(STOCK_PROMPT_TEXT, view=StockPromptView())
        session["stock_prompt"] = {"message_id": str(message.id), "done": False}
        save_data()
    except Exception as exc:
        print(f"[STOCK] question impossible dans #{getattr(channel, 'name', '?')} : {exc}")

async def _close_stock_prompt(channel, message_id, text):
    try:
        await channel.get_partial_message(int(message_id)).edit(content=text, view=None)
    except Exception as exc:   # message supprime, droits retires : sans consequence
        print(f"[STOCK] message de question non mis a jour : {exc}")

STOCK_AMOUNT_HELP = ("Montant non compris (ou trop grand) : « {raw} ». Exemples : `1M6`, `2 450 000`, `500k`. "
                     "Rien n'a été enregistré : clique de nouveau sur le bouton.")

class StockModal(discord.ui.Modal):
    """Fenetre de saisie du stock de DEPART."""
    def __init__(self, channel_id, message_id):
        super().__init__(title="Stock de runes en inventaire", timeout=300)
        self.channel_id, self.message_id = channel_id, message_id
        self.amount = discord.ui.TextInput(label="Valeur totale du stock (kamas)", placeholder="ex : 1M6, 2 450 000, 500k",
                                           min_length=1, max_length=20, required=True)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        session = sessions.get(self.channel_id)
        prompt = (session or {}).get("stock_prompt")
        if session is None or not prompt or prompt.get("done") or prompt.get("message_id") != self.message_id:
            return await interaction.response.send_message("Cette question n'est plus d'actualité : rien n'a été enregistré.", ephemeral=True)
        raw = str(self.amount.value).strip()
        if raw.lower() in STOCK_NONE_WORDS:
            prompt["done"], prompt["skipped"] = True, True
            save_data()
            await interaction.response.send_message("Aucun stock de départ déclaré.")
            return await _close_stock_prompt(interaction.channel, self.message_id, "Aucun stock de départ déclaré.")
        value = parse_kamas_amount(raw)
        if value is None:
            return await interaction.response.send_message(STOCK_AMOUNT_HELP.format(raw=raw), ephemeral=True)
        set_stock(session, "start", value)
        prompt["done"], prompt["amount"] = True, value
        touch_channel(self.channel_id)
        save_data()
        await interaction.response.send_message(
            f"📦 Stock de départ enregistré : **{format_number(value)} kamas**. Suivi séparé : il n'entre pas dans le montant "
            "à régler.\n`/fmstock depart:aucun` pour corriger.")
        await _close_stock_prompt(interaction.channel, self.message_id,
                                  f"✅ Stock de départ enregistré : {format_number(value)} kamas, par <@{interaction.user.id}>.")

class StockPromptView(discord.ui.View):
    """Boutons persistants (survivent aux redemarrages) : chaque question n'est valable qu'UNE fois."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _open_prompt(self, interaction):
        channel_id = str(interaction.channel.id)
        session = sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("Aucune session active.", ephemeral=True)
            return None
        if not can_tag_channel(interaction.user, channel_id):
            await interaction.response.send_message("Seul le propriétaire du salon (ou un administrateur) peut répondre.", ephemeral=True)
            return None
        prompt = session.get("stock_prompt")
        if not prompt or prompt.get("done") or prompt.get("message_id") != str(interaction.message.id):
            await interaction.response.send_message("Cette question n'est plus d'actualité (déjà traitée, ou nouvelle session).", ephemeral=True)
            return None
        return session

    @discord.ui.button(label="Renseigner mon stock", style=discord.ButtonStyle.primary, emoji="💰", custom_id="fm:stock:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._open_prompt(interaction) is None:
            return
        await interaction.response.send_modal(StockModal(str(interaction.channel.id), str(interaction.message.id)))

    @discord.ui.button(label="Aucun stock", style=discord.ButtonStyle.secondary, custom_id="fm:stock:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await self._open_prompt(interaction)
        if session is None:
            return
        session["stock_prompt"]["done"] = True
        session["stock_prompt"]["skipped"] = True
        save_data()
        await interaction.response.edit_message(
            content="Aucun stock de départ déclaré. (Tu pourras en ajouter un plus tard avec `/fmstock`.)", view=None)

# ---- stock RESTANT : meme fenetre, posee apres /fmstop ----
# La session est deja fermee : la reponse met a jour son enregistrement d'historique (retrouve par le message de la question).
def find_stop_record(message_id):
    for record in reversed(fm_state().get("history", [])):
        prompt = record.get("stock_prompt")
        if isinstance(prompt, dict) and prompt.get("message_id") == str(message_id):
            return record
    return None

async def post_stock_end_prompt(channel, record):
    """Apres /fmstop : demande le stock restant, SEULEMENT si un stock de depart avait ete declare. Ne leve JAMAIS."""
    start = stock_of(record).get("start")
    if not start:
        return
    try:
        message = await channel.send(STOCK_END_TEXT.format(start=format_number(start)), view=StockEndPromptView())
        record["stock_prompt"] = {"message_id": str(message.id), "done": False}
        save_data()
    except Exception as exc:
        print(f"[STOCK] question du stock restant impossible dans #{getattr(channel, 'name', '?')} : {exc}")

def apply_stock_end(record, value):
    """Enregistre le stock restant, referme la question et retourne le texte de la quantification."""
    set_stock(record, "end", value)
    record["stock_prompt"]["done"] = True
    return "📦 " + "\n".join(stock_summary_lines(stock_of(record), record.get("total", 0)))

class StockEndModal(discord.ui.Modal):
    """Fenetre de saisie du stock RESTANT."""
    def __init__(self, channel_id, message_id):
        super().__init__(title="Stock restant en inventaire", timeout=300)
        self.channel_id, self.message_id = channel_id, message_id
        self.amount = discord.ui.TextInput(label="Valeur totale du stock restant (kamas)", placeholder="ex : 500k, 1M2 ; 0 s'il ne reste rien",
                                           min_length=1, max_length=20, required=True)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        record = find_stop_record(self.message_id)
        if record is None or record["stock_prompt"].get("done"):
            return await interaction.response.send_message("Cette question n'est plus d'actualité : rien n'a été enregistré.", ephemeral=True)
        raw = str(self.amount.value).strip()
        value = 0 if raw.lower() in STOCK_NONE_WORDS else parse_kamas_amount(raw)
        if value is None:
            return await interaction.response.send_message(STOCK_AMOUNT_HELP.format(raw=raw).replace("Renseigner mon stock", "Renseigner le stock restant"), ephemeral=True)
        text = apply_stock_end(record, value)
        save_data()
        await interaction.response.send_message(text[:1990])
        await _close_stock_prompt(interaction.channel, self.message_id,
                                  f"✅ Stock restant enregistré : {format_number(value)} kamas, par <@{interaction.user.id}>.")

class StockEndPromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _open_prompt(self, interaction):
        record = find_stop_record(interaction.message.id)
        if record is None or record["stock_prompt"].get("done"):
            await interaction.response.send_message("Cette question n'est plus d'actualité (déjà traitée).", ephemeral=True)
            return None
        if not can_tag_channel(interaction.user, str(interaction.channel.id)):
            await interaction.response.send_message("Seul le propriétaire du salon (ou un administrateur) peut répondre.", ephemeral=True)
            return None
        return record

    @discord.ui.button(label="Renseigner le stock restant", style=discord.ButtonStyle.primary, emoji="📦", custom_id="fm:stockend:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._open_prompt(interaction) is None:
            return
        await interaction.response.send_modal(StockEndModal(str(interaction.channel.id), str(interaction.message.id)))

    @discord.ui.button(label="Plus rien en stock", style=discord.ButtonStyle.secondary, custom_id="fm:stockend:none")
    async def none_left(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = await self._open_prompt(interaction)
        if record is None:
            return
        text = apply_stock_end(record, 0)
        save_data()
        await interaction.response.edit_message(content=text[:1990], view=None)

# ---- capture de l'equipement termine : traitement, stockage sur disque, budget de taille ----
# Les captures sont des FICHIERS (jamais dans data.json, qui est reecrit a chaque clic) et sont toujours
# re-encodees : metadonnees retirees, dimensions et poids plafonnes, aucune image brute n'est conservee.
SHOT_DIR = os.environ.get("ITEM_SHOT_DIR") or os.path.join(os.path.dirname(DATA_PATH) or ".", "item_shots")
SHOT_MAX_INPUT = 8_000_000        # octets acceptes en entree
SHOT_MAX_PIXELS = 40_000_000      # protection contre les images pieges (bombes de decompression)
SHOT_MAX_SIZE = (800, 1200)       # largeur, hauteur maximales apres reduction
SHOT_MAX_BYTES = 250_000          # poids maximal apres encodage
SHOT_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}

class ShotError(Exception):
    """Capture refusee : le message est pret a etre affiche a l'utilisateur."""

def encode_shot(raw):
    """Image brute -> {"data", "w", "h", "ext"} (WebP, ou JPEG si WebP indisponible). Leve ShotError."""
    if len(raw) > SHOT_MAX_INPUT:
        raise ShotError("Image trop lourde (8 Mo au maximum).")
    try:
        image = Image.open(BytesIO(raw))
        if image.format not in SHOT_FORMATS:
            raise ShotError("Format non pris en charge : envoie une capture PNG, JPEG ou WebP.")
        if image.width * image.height > SHOT_MAX_PIXELS:
            raise ShotError("Image trop grande (dimensions) : réduis la capture.")
        image.load()
    except ShotError:
        raise
    except Image.DecompressionBombError as exc:
        raise ShotError("Image trop grande (dimensions) : réduis la capture.") from exc
    except Exception as exc:
        raise ShotError("Fichier illisible : ce n'est pas une image valide.") from exc
    image = ImageOps.exif_transpose(image)
    has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
    image = image.convert("RGBA" if has_alpha else "RGB")
    image.thumbnail(SHOT_MAX_SIZE, Image.LANCZOS)   # ne grossit jamais une petite image
    use_webp = features.check("webp")
    for quality, scale in ((82, 1.0), (74, 1.0), (66, 0.85), (58, 0.7), (50, 0.55)):
        work = image if scale == 1.0 else image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)
        buffer = BytesIO()
        if use_webp:
            work.save(buffer, "WEBP", quality=quality, method=4)
        else:
            work.convert("RGB").save(buffer, "JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= SHOT_MAX_BYTES:
            return {"data": data, "w": work.width, "h": work.height, "ext": "webp" if use_webp else "jpg"}
    raise ShotError("Image trop complexe : plus de 250 Ko même après réduction. Envoie une capture plus simple ou plus petite.")

def channel_shots():
    return fm_state().setdefault("channel_shots", {})

def shot_file(channel_id, ext):
    return os.path.join(SHOT_DIR, f"{int(channel_id)}.{ext}")   # int() : l'identifiant ne peut jamais sortir du dossier

def save_shot(channel_id, info):
    """Ecrit la capture (atomique) et retire une eventuelle ancienne version d'un autre format."""
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = shot_file(channel_id, info["ext"])
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(info["data"])
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    for ext in ("webp", "jpg"):
        if ext != info["ext"]:
            try:
                os.remove(shot_file(channel_id, ext))
            except OSError:
                pass

def remove_shot(channel_id):
    removed = False
    for ext in ("webp", "jpg"):
        try:
            os.remove(shot_file(channel_id, ext))
            removed = True
        except OSError:
            pass
    return removed

def shots_dir_ephemeral():
    """
    True si les captures seront PERDUES a la prochaine mise a jour : bot dans Docker et dossier sur le meme
    systeme de fichiers que la racine du conteneur (donc pas un volume). Heuristique, utilisee pour avertir seulement.
    """
    try:
        if not os.path.exists("/.dockerenv"):
            return False
        os.makedirs(SHOT_DIR, exist_ok=True)
        return os.stat(SHOT_DIR).st_dev == os.stat("/").st_dev
    except OSError:
        return False

EPHEMERAL_SHOT_WARNING = ("⚠️ Le dossier des captures n'est pas conservé entre deux mises à jour du bot : elles seraient "
                          "perdues au prochain `docker compose up -d --build`. Ajoute `./item_shots:/app/item_shots` "
                          "aux `volumes` du docker-compose.yml.")

def shot_budget(guild):
    """Octets (base64) que les captures peuvent occuper dans le bilan : l'ensemble doit rester sous la limite d'envoi de Discord."""
    limit = getattr(guild, "filesize_limit", None)
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 10_000_000
    return max(0, min(int(limit * 0.6), 8_000_000) - 400_000)

def load_shot_uris(channel_ids, budget):
    """Lit les captures (les plus recentes d'abord) jusqu'a epuisement du budget. Retourne ({salon: data URI}, nombre ignore)."""
    uris, used, skipped = {}, 0, 0
    for channel_id in channel_ids:
        entry = (channel_shots().get(str(channel_id)) or {})
        ext = entry.get("ext") or "webp"
        if ext not in ("webp", "jpg"):
            skipped += 1
            continue
        try:
            with open(shot_file(channel_id, ext), "rb") as f:
                data = f.read(SHOT_MAX_BYTES * 2 + 1)
        except (OSError, ValueError):
            skipped += 1   # fichier absent (volume non monte) : le bilan reste produit
            continue
        magic_ok = (data[:4] == b"RIFF" and data[8:12] == b"WEBP") if ext == "webp" else data[:3] == b"\xff\xd8\xff"
        if not magic_ok or len(data) > SHOT_MAX_BYTES * 2:
            skipped += 1
            continue
        encoded = base64.b64encode(data).decode()
        if used + len(encoded) > budget:
            skipped += 1
            continue
        used += len(encoded)
        uris[str(channel_id)] = f"data:image/{'webp' if ext == 'webp' else 'jpeg'};base64,{encoded}"
    return uris, skipped

async def prepare_shots(channel_ids, budget):
    """Captures du bilan. Ne leve JAMAIS : au pire, un bilan sans captures."""
    if not channel_ids or budget <= 0:
        return {}, len(channel_ids)
    try:
        return await asyncio.to_thread(load_shot_uris, list(channel_ids), budget)
    except Exception as exc:
        print(f"[CAPTURES] ignorees : {exc!r}")
        return {}, len(channel_ids)

# ---------------- SUIVI DES TENTATIVES DE FM ---------------- #
# Un panneau a boutons, epingle dans le salon : un clic = une tentative. Les compteurs sont ranges
# dans la session (data.json) ; l'identifiant du message du panneau est range a part, dans l'etat
# FM, pour survivre a /fmreset et /fmstart.
def new_attempts():
    return {"counts": {key: 0 for key, *_ in FM_OUTCOMES}, "history": [], "target": None}

def attempts_of(session):
    # Les sessions creees avant cette fonctionnalite n'ont pas la cle : on la cree a la volee.
    attempts = session.setdefault("attempts", new_attempts())
    counts = attempts.setdefault("counts", {})
    for key, *_ in FM_OUTCOMES:
        counts.setdefault(key, 0)
    attempts.setdefault("history", [])
    attempts.setdefault("target", None)
    return attempts

def attempts_total(session):
    counts = attempts_of(session)["counts"]
    return sum(counts.get(key, 0) for key, *_ in FM_OUTCOMES)

def attempts_line(session):
    total = attempts_total(session)
    if total == 0:
        return ""
    counts = attempts_of(session)["counts"]
    detail = " · ".join(f"{emoji} {counts.get(key, 0)}" for key, _label, emoji, _style in FM_OUTCOMES)
    return f"Tentatives : {total} ({detail})"

def attempts_embed(session, closed=False):
    counts = attempts_of(session)["counts"]
    total = attempts_total(session)
    embed = discord.Embed(
        title="🔨 Suivi des tentatives de FM",
        description="Session terminée." if closed else "Clique après chaque rune passée.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Tentatives", value=f"**{total}**", inline=False)
    for key, label, emoji, _style in FM_OUTCOMES:
        n = counts.get(key, 0)
        share = f" ({n * 100 / total:.0f} %)" if total else ""
        embed.add_field(name=f"{emoji} {label}", value=f"**{n}**{share}", inline=True)
    target = attempts_of(session).get("target")
    if target:
        embed.add_field(name="🎯 Rune visée", value=f"**{target}**", inline=False)
    return embed

def panels():
    return fm_state().setdefault("panels", {})

async def _fetch_panel(channel):
    panel_id = panels().get(str(channel.id))
    if not panel_id:
        return None
    try:
        return await channel.fetch_message(int(panel_id))
    except (discord.HTTPException, ValueError):  # message supprime, droits retires...
        return None

async def post_attempt_panel(channel, session):
    """
    Poste un nouveau panneau, l'epingle (sauf en mode « suit le fil »), puis supprime l'ancien.
    Retourne (message, epingle). Un epinglage refuse n'empeche pas le panneau de fonctionner.
    """
    old = await _fetch_panel(channel)
    message = await channel.send(embed=attempts_embed(session), view=AttemptPanelView())
    panels()[str(channel.id)] = str(message.id)
    save_data()
    pinned = False
    if not FM_PANEL_FOLLOWS:
        try:
            await message.pin()
            pinned = True
        except discord.HTTPException as exc:  # Forbidden en fait partie
            print(f"[PANNEAU] epinglage impossible dans #{channel.name} : {exc}")
    if old is not None:
        try:
            await old.delete()
        except discord.HTTPException:
            pass
    return message, pinned

async def refresh_attempt_panel(channel, session):
    message = await _fetch_panel(channel)
    if message is None:
        return False
    try:
        await message.edit(embed=attempts_embed(session))
        return True
    except discord.HTTPException:
        return False

async def finalize_attempt_panel(channel, session):
    """Session terminee : le panneau reste (epingle, avec ses chiffres finaux) mais sans boutons."""
    panel_id = panels().pop(str(channel.id), None)
    if not panel_id:
        return
    save_data()
    try:
        message = await channel.fetch_message(int(panel_id))
        await message.edit(embed=attempts_embed(session, closed=True), view=None)
    except (discord.HTTPException, ValueError):
        pass

async def sync_attempt_panel(channel, session):
    """
    Session (re)demarree : remet le panneau existant a jour, ou en cree un dans un salon FM
    personnel. Ne leve jamais : le panneau est un confort, il ne doit pas gener les commandes.
    """
    try:
        if await refresh_attempt_panel(channel, session):
            return
        if str(channel.id) in fm_channels():
            await post_attempt_panel(channel, session)
    except Exception as exc:
        print(f"[PANNEAU] mise a jour impossible dans #{getattr(channel, 'name', '?')} : {exc}")

async def bump_attempt_panel(channel, session):
    """
    Fait redescendre le panneau sous le dernier message (appele apres chaque capture traitee).
    Un seul panneau existe a la fois : le nouveau est poste, puis l'ancien supprime. Ne leve jamais.
    """
    if not FM_PANEL_FOLLOWS or str(channel.id) not in panels():
        return
    try:
        old = await _fetch_panel(channel)
        snapshot = attempts_total(session)
        message = await channel.send(embed=attempts_embed(session), view=AttemptPanelView())
        panels()[str(channel.id)] = str(message.id)
        save_data()
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        if attempts_total(session) != snapshot:
            # un clic est arrive pendant le deplacement : on rattrape l'affichage
            await message.edit(embed=attempts_embed(session))
    except Exception as exc:
        print(f"[PANNEAU] deplacement impossible dans #{getattr(channel, 'name', '?')} : {exc}")

class AttemptPanelView(discord.ui.View):
    """Boutons du panneau. Persistant (timeout=None) : il survit aux redemarrages du bot."""
    def __init__(self):
        super().__init__(timeout=None)
        for key, label, emoji, style in FM_OUTCOMES:
            button = discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=ATTEMPT_BUTTON_PREFIX + key)
            button.callback = self._outcome_callback(key)
            self.add_item(button)
        undo = discord.ui.Button(
            label="Annuler", emoji="↩️", style=discord.ButtonStyle.secondary,
            custom_id=ATTEMPT_BUTTON_PREFIX + "undo")
        undo.callback = self._undo
        self.add_item(undo)

    def _outcome_callback(self, key):
        async def callback(interaction):
            await self._record(interaction, key)
        return callback

    async def _session_for(self, interaction):
        """Session du salon, apres controle du proprietaire. Sinon repond a l'utilisateur et renvoie None."""
        channel_id = str(interaction.channel.id)
        session = sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message(
                "Aucune session active dans ce salon : ce panneau n'est plus utilisable.", ephemeral=True)
            return None
        entry = fm_channels().get(channel_id)
        if entry and str(interaction.user.id) != str(entry.get("owner_id")):
            await interaction.response.send_message(
                "Seul le propriétaire du salon peut utiliser ce panneau.", ephemeral=True)
            return None
        return session

    async def _record(self, interaction, key):
        session = await self._session_for(interaction)
        if session is None:
            return
        attempts = attempts_of(session)
        attempts["counts"][key] = attempts["counts"].get(key, 0) + 1
        at = now_iso()
        target = attempts.get("target")
        attempts["history"].append({"k": key, "at": at, "by": str(interaction.user.id), **({"r": target} if target else {})})
        del attempts["history"][:-ATTEMPT_HISTORY_CAP]
        log_attempt(interaction.channel.id, key, interaction.user.id, at, rune=target)
        touch_channel(interaction.channel.id)
        save_data()
        await interaction.response.edit_message(embed=attempts_embed(session))

    async def _undo(self, interaction):
        session = await self._session_for(interaction)
        if session is None:
            return
        attempts = attempts_of(session)
        if not attempts["history"]:
            await interaction.response.send_message("Rien à annuler.", ephemeral=True)
            return
        last = attempts["history"].pop()
        key = last.get("k")
        if attempts["counts"].get(key, 0) > 0:
            attempts["counts"][key] -= 1
        unlog_attempt(interaction.channel.id, key, last.get("at"))
        touch_channel(interaction.channel.id)
        save_data()
        await interaction.response.edit_message(embed=attempts_embed(session))

def split_message(text, limit=1900):
    """Decoupe un texte en morceaux que Discord accepte (2000 caracteres max), sur des fins de ligne."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                 # ligne demesuree : coupee
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]

# Ferme la session d'un salon en publiant le meme resume que /fmstop.
async def close_session_in_channel(channel):
    channel_id = str(channel.id)
    session = sessions.pop(channel_id, None)
    if session is not None:
        record_session_history(channel, session, "close")
    save_data()
    if session is None:
        return False
    for chunk in split_message(session_summary(session, "Resume final")):
        await channel.send(chunk)
    if session.get("runes"):
        await send_rune_embeds(channel.send, session["runes"])
    await finalize_attempt_panel(channel, session)
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

def bot_channel_overwrite():
    """Droits du bot dans un salon FM. Les droits d'epinglage (« Gerer les messages » et, sur les
    versions recentes de Discord et de discord.py, « Epingler des messages ») ne sont demandes
    que si le panneau est epingle (FM_PANEL_FOLLOWS=0)."""
    perms = dict(
        view_channel=True, send_messages=True, embed_links=True,
        read_message_history=True, manage_channels=True,
    )
    if not FM_PANEL_FOLLOWS:
        perms["manage_messages"] = True
        if hasattr(discord.Permissions, "pin_messages"):
            perms["pin_messages"] = True
    return discord.PermissionOverwrite(**perms)

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
        guild.me: bot_channel_overwrite(),
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
    await post_stock_prompt(channel, sessions[str(channel.id)])
    try:
        await post_attempt_panel(channel, sessions[str(channel.id)])
    except Exception as exc:  # confort : ne doit jamais empecher la creation du salon
        print(f"[PANNEAU] creation impossible dans #{channel.name} : {exc}")
    created = {"author": site_author(member)}
    if command_id:
        created["commandId"] = command_id
    await send_event(site_event("channel-created", f"{channel.id}-created", channel, **created))
    await send_event(site_event("session-start", f"{channel.id}-start", channel, author=site_author(member)))
    return channel

def explain_discord_error(exc):
    """Traduit un refus de Discord en explication utilisable (le texte brut de l'API est peu parlant)."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    text = getattr(exc, "text", "") or ""
    if code == 30013:
        return ("la catégorie de destination est pleine (Discord limite une catégorie à 50 salons). "
                "Libère de la place dans la catégorie d'archives, ou change-la.")
    if code == 50013 or status == 403:
        return ("il me manque un droit (Gérer les salons, et Gérer les rôles pour modifier les permissions "
                "d'un salon). Vérifie les droits du rôle du bot.")
    if status == 429:
        return "Discord limite temporairement cette action : réessaie dans quelques minutes."
    return f"Discord a refusé ({status or '?'}{', code ' + str(code) if code else ''}) : {text or 'sans précision'}"

async def archive_fm_channel(channel, message):
    entry = fm_channels().get(str(channel.id))
    if not entry or entry.get("status") != "active":
        return
    # Un salon peut etre rouvert puis archive de nouveau : chaque archivage a ses propres identifiants
    # d'evenement, sinon le site prendrait le second pour un doublon du premier.
    archived_at = now_iso()

    guild = channel.guild
    overwrites = dict(channel.overwrites)
    owner = guild.get_member(int(entry["owner_id"]))
    if owner is not None:
        overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    archive = fm_place(guild, "archive_category_id", discord.CategoryChannel)
    if archive is None and await ensure_fm_places(guild):
        archive = fm_place(guild, "archive_category_id", discord.CategoryChannel)

    # 1. Le deplacement D'ABORD : c'est l'etape que Discord peut refuser (categorie pleine, droit
    #    manquant, limite de debit). Si elle echoue, rien n'a ete ferme : la session, ses totaux et son
    #    panneau sont intacts et l'appelant peut afficher la raison.
    await channel.edit(
        category=archive or channel.category,
        overwrites=overwrites,
    )
    entry["status"] = "archived"
    entry["archived_at"] = archived_at
    save_data()

    # 2. Le salon est archive : on clot la session et on publie le resume. Un echec d'envoi ici ne doit
    #    pas annuler l'archivage (l'historique de la session est deja enregistre avant l'envoi).
    try:
        if await close_session_in_channel(channel):
            await send_event(site_event("session-stop", f"{channel.id}-archive-stop-{archived_at}", channel))
        await channel.send(message)
    except discord.HTTPException as exc:
        print(f"[ARCHIVAGE] #{getattr(channel, 'name', '?')} archive, mais resume ou message non envoye : {exc}")
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
            "`/fmpay` · paiement du client (ex : `20M`), ou `-1M6` pour ajouter un montant au total (stock à facturer)\n"
            "`/fmunpay` · annuler le dernier paiement\n"
            "`/fmpanel` · reposter le panneau de suivi des tentatives\n"
            "`/fmstats` · statistiques de tes tentatives (taux de réussite, rythme)\n"
            "`/fmhistorique` · ton historique et tes records (ou ceux d'un autre joueur)\n"
            "`/fmbilan` · page de bilan : chronologie et totaux par mois, trimestre, année\n"
            "`/fmitem` · associer l'équipement forgé à ce salon (son icône apparaît dans le bilan)\n"
            "`/fmundo` · annuler la dernière capture · `/fmadd` · ajouter une ligne à la main\n"
            "`/fmrune` · rune visée · `/fmtenta` · ajuster les tentatives (+20, -1)\n"
            "`/fmrecap` · récapitulatif de la commande à envoyer au client\n"
            "`/fmstock` · stock de départ et stock restant (suivi séparé, jamais ajouté au montant à régler)\n"
            "`/fmshot` · joindre la capture de l'équipement terminé (visible dans le bilan)\n"
            "`/fmstop` · fermer la session et voir le résumé\n"
            "`/fmstart` · nouvelle session\n"
            "`/fmreset` · remettre à zéro\n"
            "`/fmarchive` · archiver le salon, FM terminée\n\n"
            "Ta session est démarrée : poste ici tes **captures du chat** après tes achats à l'HDV.\n"
            + ("Le panneau de suivi redescend sous chaque capture et compte tes tentatives de FM : "
               if FM_PANEL_FOLLOWS else
               "Le panneau épinglé compte tes tentatives de FM : ")
            + "un clic à chaque rune passée."
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
    existing = sessions.get(channel_id)
    if existing is not None:   # jamais d'ecrasement silencieux d'une session en cours
        return await interaction.response.send_message(
            f"Une session est déjà en cours dans ce salon ({format_number(existing['total'])} kamas). "
            "`/fmstop` pour la clôturer, ou `/fmreset` pour la remettre à zéro.", ephemeral=True)
    sessions[channel_id] = empty_session()
    touch_channel(channel_id)
    save_data()
    await interaction.response.send_message("Session FM demarree !")
    await send_event(site_event(
        "session-start", interaction.id, interaction.channel, interaction.created_at,
        author=site_author(interaction.user),
    ))
    await post_stock_prompt(interaction.channel, sessions[channel_id])
    await sync_attempt_panel(interaction.channel, sessions[channel_id])

@bot.tree.command(name="fmstop", description="Arreter la session et afficher le resume")
async def fmstop(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    if channel_id not in sessions:
        return await interaction.response.send_message("Aucune session active.")

    session = sessions[channel_id]
    runes = session.get("runes", {})
    message_out = session_summary(session, "Resume final")

    # Le resume est envoye AVANT de fermer la session : si Discord refuse l'envoi, rien n'est perdu
    # et la commande peut etre relancee.
    chunks = split_message(message_out)
    await interaction.response.send_message(chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)

    record = record_session_history(interaction.channel, session, "stop")
    del sessions[channel_id]
    touch_channel(channel_id)
    save_data()
    if runes:
        await send_rune_embeds(interaction.followup.send, runes)
    await send_event(site_event("session-stop", interaction.id, interaction.channel, interaction.created_at))
    await finalize_attempt_panel(interaction.channel, session)
    if record is not None:   # stock restant : seulement si un stock de depart a ete declare
        await post_stock_end_prompt(interaction.channel, record)

@bot.tree.command(name="fmreset", description="Reinitialiser la session")
async def fmreset(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None or not session_has_activity(session):   # rien a perdre : pas de confirmation
        reset_session_state(channel_id)
        await interaction.response.send_message("Session reinitialisee !")
        return await after_reset(interaction, channel_id)

    lines = sum(1 for _ in (session.get("runes") or {}))
    paiements = len(payments_of(session))
    text = (f"⚠️ **Remettre la session à zéro ?**\n"
            f"Seront effacés : **{format_number(session['total'])} kamas** d'achats, {lines} objet(s), "
            f"{paiements} paiement(s) ou ajustement(s), {attempts_total(session)} tentative(s) du panneau.\n"
            "**Action irréversible.** Les statistiques déjà enregistrées (`/fmstats`) ne sont pas touchées.")
    view = ResetConfirmView(interaction.user.id, channel_id)
    await interaction.response.send_message(text, view=view)
    view.reply_message = await interaction.original_response()

@bot.tree.command(name="fmtotal", description="Voir le total actuel")
async def fmtotal(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    if channel_id not in sessions:
        return await interaction.response.send_message("Aucune session active.")

    session = sessions[channel_id]
    runes = session.get("runes", {})
    message_out = session_summary(session, "Etat actuel")

    chunks = split_message(message_out)
    await interaction.response.send_message(chunks[0])
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)
    if runes:
        await send_rune_embeds(interaction.followup.send, runes)

@bot.tree.command(name="fmpay", description="Paiement du client (20M) ou ajustement au total avec un montant négatif (-1M6)")
@app_commands.describe(
    montant="20000000, 20M, 20kk, 1.5M, 500k, 1M6 (= 1,6 million). Négatif (-1M6) : ajouté au montant dû",
    note="Facultatif : à quoi correspond cette ligne (ex : stock de départ)",
)
async def fmpay(interaction: discord.Interaction, montant: str, note: str = ""):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")

    amount = parse_signed_kamas_amount(montant)
    if amount is None:
        return await interaction.response.send_message(
            f"Montant non compris : « {montant} ». Exemples : `20000000`, `20 000 000`, `20M`, "
            "`20kk`, `1.5M`, `500k`, `1M6` (= 1,6 million), ou `-1M6` pour ajouter un montant "
            "au total. Rien n'a été enregistré.", ephemeral=True)

    entry = {"amount": amount, "by": str(interaction.user.id), "at": now_iso()}
    note = (note or "").strip()[:60]
    if note:
        entry["note"] = note
    session.setdefault("payments", []).append(entry)
    touch_channel(channel_id)
    save_data()

    received, charged = payment_totals(session)
    due = amount_due(session)
    if amount > 0:
        head = f"Paiement enregistré : -{format_number(amount)} kamas"
    else:
        head = f"Ajustement enregistré : +{format_number(-amount)} kamas (ajouté au montant dû)"
    lines = [head + (f" · {note}" if note else ""),
             f"Total dépensé : {format_number(session['total'])} kamas"]
    if charged:
        lines.append(f"Ajustements : +{format_number(charged)} kamas")
    if received:
        lines.append(f"Paiements reçus : -{format_number(received)} kamas")
    if due >= 0:
        lines.append(f"**Reste à payer : {format_number(due)} kamas**")
    else:
        lines.append(f"**Trop-perçu : {format_number(-due)} kamas**")
        if amount > 0:
            lines.append("⚠️ Ce paiement dépasse le reste dû. Si c'est une erreur : `/fmunpay`.")
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="fmunpay", description="Annuler le dernier paiement ou ajustement enregistré avec /fmpay")
async def fmunpay(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")
    payments = session.get("payments") or []
    if not payments:
        return await interaction.response.send_message("Aucun paiement à annuler.", ephemeral=True)

    cancelled = payments.pop()
    touch_channel(channel_id)
    save_data()
    kind = "Paiement" if cancelled["amount"] > 0 else "Ajustement"
    await interaction.response.send_message(
        f"{kind} annulé : {format_number(abs(cancelled['amount']))} kamas\n"
        f"**Reste à payer : {format_number(amount_due(session))} kamas**"
    )

@bot.tree.command(name="fmundo", description="Annuler la dernière capture validée (ou la dernière ligne ajoutée à la main)")
async def fmundo(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")
    if not can_tag_channel(interaction.user, channel_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut annuler une capture.", ephemeral=True)
    last = undo_last_capture(session)
    if last is None:
        return await interaction.response.send_message(
            "Rien à annuler : seules les captures validées avec cette version sont annulables.", ephemeral=True)
    touch_channel(channel_id)
    save_data()

    entries = last.get("entries") or []
    removed = sum(e["price"] for e in entries)
    origin = "ajout manuel" if last.get("src") == "manual" else "capture"
    lines = "\n".join(f"{name} x{v['qty']} {format_number(v['price'])} Kamas" for name, v in aggregate_entries(entries).items())
    text = (f"↩️ Annulé ({origin} de <@{last.get('by')}>) : **-{format_number(removed)} kamas**, {len(entries)} ligne(s)\n{lines}\n\n"
            f"Total global : {format_number(session['total'])}")
    if payments_of(session):
        text += f"\nReste à payer : {format_number(amount_due(session))}"
    remaining = len(session.get("captures") or [])
    text += f"\n({remaining} annulation(s) encore possible(s). Le site n'est pas prévenu de l'annulation.)"
    await interaction.response.send_message(text[:1990])

@bot.tree.command(name="fmadd", description="Ajouter à la main un achat que la capture n'a pas pu lire")
@app_commands.describe(ligne="Objet, quantité, montant TOTAL de la ligne. Ex : Rune Ga Pa x100 2M45")
async def fmadd(interaction: discord.Interaction, ligne: str):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")
    if not can_tag_channel(interaction.user, channel_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut ajouter une ligne.", ephemeral=True)
    try:
        name, qty, price = parse_add_line(ligne)
    except ValueError as exc:
        return await interaction.response.send_message(str(exc), ephemeral=True)
    name, warning, refusal = resolve_added_name(session, name)
    if refusal:
        return await interaction.response.send_message(refusal, ephemeral=True)

    entry = {"name": name, "qty": qty, "price": price}
    added = commit_entries(session, channel_id, interaction.user, [entry], [], ref=interaction.id, source="manual")
    average = int((Decimal(price) / Decimal(qty)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    text = (f"✍️ Ajout manuel : **{name} x{qty}** · {format_number(price)} kamas ({format_number(average)} / unité)\n"
            f"Total global : {format_number(session['total'])}")
    if payments_of(session):
        text += f"\nReste à payer : {format_number(amount_due(session))}"
    if warning:
        text += f"\n⚠️ {warning}"
    text += "\n`/fmundo` pour annuler."
    await interaction.response.send_message(text)
    await send_capture_event(interaction, interaction.user, added, [entry])

@bot.tree.command(name="fmrune", description="Choisir la rune visée : les tentatives suivantes lui seront rattachées")
@app_commands.describe(rune="Nom de la rune (ou « aucune » pour effacer le choix)")
async def fmrune(interaction: discord.Interaction, rune: str):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")
    if not can_tag_channel(interaction.user, channel_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut choisir la rune visée.", ephemeral=True)
    attempts = attempts_of(session)
    if item_key(rune) in ITEM_REMOVE_WORDS:
        attempts["target"] = None
        save_data()
        await refresh_attempt_panel(interaction.channel, session)
        return await interaction.response.send_message("Plus de rune visée : les prochaines tentatives ne seront rattachées à aucune rune.")
    name, warning, refusal = resolve_added_name(session, rune.strip())
    if refusal:
        return await interaction.response.send_message(refusal, ephemeral=True)
    attempts["target"] = name
    touch_channel(channel_id)
    save_data()
    await refresh_attempt_panel(interaction.channel, session)
    text = f"🎯 Rune visée : **{name}**. Les prochaines tentatives (panneau ou `/fmtenta`) lui seront rattachées."
    if warning:
        text += f"\n⚠️ {warning}"
    await interaction.response.send_message(text)

@fmrune.autocomplete("rune")
async def fmrune_autocomplete(interaction: discord.Interaction, current: str):
    session = sessions.get(str(interaction.channel.id))
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in rune_autocomplete_choices(session, current)]

@bot.tree.command(name="fmtenta", description="Ajuster les tentatives : +20 en ajoute 20, -1 en retire une")
@app_commands.describe(nombre="+20, -3… (le + est facultatif, 500 au maximum)", issue="Issue concernée (par défaut : échec)")
@app_commands.choices(issue=[app_commands.Choice(name=label, value=key) for key, label, *_ in FM_OUTCOMES])
async def fmtenta(interaction: discord.Interaction, nombre: str, issue: str = None):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")
    if not can_tag_channel(interaction.user, channel_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut ajuster les tentatives.", ephemeral=True)
    try:
        delta = parse_attempt_delta(nombre)
    except ValueError as exc:
        return await interaction.response.send_message(str(exc), ephemeral=True)
    known = {key: (label, emoji) for key, label, emoji, _style in FM_OUTCOMES}
    key = issue if issue in known else FM_OUTCOMES[-1][0]
    label, emoji = known[key]

    applied = adjust_attempts(session, channel_id, interaction.user.id, key, delta, now_iso())
    touch_channel(channel_id)
    save_data()
    await refresh_attempt_panel(interaction.channel, session)
    if applied == 0:
        return await interaction.response.send_message(f"Rien à retirer : aucune tentative « {label} » enregistrée.", ephemeral=True)
    sign = "+" if applied > 0 else "-"
    text = f"{emoji} **{sign}{abs(applied)}** tentative(s) « {label} » · {attempts_summary_line(session)}"
    if applied != delta:
        text += f"\n⚠️ Seulement {abs(applied)} retirée(s) sur {abs(delta)} demandée(s) : il n'y en avait pas plus."
    target = attempts_of(session).get("target")
    if target and applied > 0:
        text += f"\n🎯 Rattachées à : {target}"
    if applied > 0:
        text += "\nAjoutées en bloc : comptées, mais sans effet sur le calcul du rythme."
    await interaction.response.send_message(text)

@bot.tree.command(name="fmstock", description="Renseigner ou corriger le stock de départ et le stock restant (suivi séparé des paiements)")
@app_commands.describe(
    depart="Valeur du stock au départ (ex : 1M6) ; « aucun » pour l'effacer",
    restant="Valeur du stock restant (ex : 500k) ; « aucun » = plus rien en stock",
    salon="Salon concerné (par défaut : celui-ci, même archivé)",
)
async def fmstock(interaction: discord.Interaction, depart: str = "", restant: str = "", salon: discord.TextChannel = None):
    target = salon or interaction.channel
    target_id = str(target.id)
    if not can_tag_channel(interaction.user, target_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut modifier le stock.", ephemeral=True)
    if not (depart or "").strip() and not (restant or "").strip():
        return await interaction.response.send_message(
            "Indique `depart` et/ou `restant` : `/fmstock depart:20M`, `/fmstock restant:5M`. Le stock est un suivi à part : "
            "il n'entre jamais dans le montant à régler.", ephemeral=True)
    elsewhere = target_id != str(interaction.channel.id)

    live = sessions.get(target_id)
    if live is not None:
        holder, purchases, label = live, live["total"], "session en cours"
    else:
        past = [r for r in fm_state().get("history", []) if r.get("ch") == target_id]
        if not past:
            return await interaction.response.send_message("Aucune session (en cours ou terminée) dans ce salon.", ephemeral=True)
        holder = max(past, key=lambda r: r.get("end") or "")
        purchases, label = holder.get("total", 0), "dernière session terminée"

    # Les deux valeurs sont lues AVANT toute ecriture : une saisie illisible ne modifie rien.
    updates = {}
    for field, name, raw in (("start", "depart", depart), ("end", "restant", restant)):
        raw = (raw or "").strip()
        if not raw:
            continue
        if raw.lower() in STOCK_NONE_WORDS:
            updates[field] = None if field == "start" else 0   # départ « aucun » = effacé ; restant « aucun » = plus rien
            continue
        value = parse_kamas_amount(raw)
        if value is None:
            return await interaction.response.send_message(
                f"`{name}` non compris (ou trop grand) : « {raw} ». Exemples : `1M6`, `2 450 000`, `500k`. Rien n'a été modifié.", ephemeral=True)
        updates[field] = value
    for field, value in updates.items():
        set_stock(holder, field, value)
    touch_channel(target_id)
    save_data()
    lines = stock_summary_lines(stock_of(holder), purchases) or ["Aucun stock déclaré pour cette session."]
    await interaction.response.send_message(f"📦 Stock mis à jour ({label}) :\n" + "\n".join(lines), ephemeral=elsewhere)

@bot.tree.command(name="fmpanel", description="Poster (ou recréer) le panneau de suivi des tentatives")
async def fmpanel(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    session = sessions.get(channel_id)
    if session is None:
        return await interaction.response.send_message("Aucune session active.")
    entry = fm_channels().get(channel_id)
    if entry and str(interaction.user.id) != str(entry.get("owner_id")):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon peut recréer ce panneau.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        _message, pinned = await post_attempt_panel(interaction.channel, session)
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"Impossible de poster le panneau : {exc}", ephemeral=True)
    if FM_PANEL_FOLLOWS:
        await interaction.followup.send(
            "Panneau posté : il redescendra sous chaque nouvelle capture.", ephemeral=True)
    elif pinned:
        await interaction.followup.send("Panneau posté et épinglé.", ephemeral=True)
    else:
        await interaction.followup.send(
            "Panneau posté, mais je n'ai pas pu l'épingler : il me manque le droit « Gérer les messages » "
            "(ou « Épingler des messages ») dans ce salon.", ephemeral=True)

@bot.tree.command(name="fmstats", description="Statistiques de tes tentatives de FM : taux de réussite, rythme, séries")
@app_commands.describe(portee="Ce salon (par défaut) ou tous tes salons")
@app_commands.choices(portee=[
    app_commands.Choice(name="Ce salon", value="salon"),
    app_commands.Choice(name="Tous mes salons", value="tous"),
])
async def fmstats(interaction: discord.Interaction, portee: str = "salon"):
    log = attempt_log()
    if portee == "tous":
        user_id = str(interaction.user.id)
        entries = [e for e in log if e.get("u") == user_id]
        label = "tous tes salons"
    else:
        channel_id = str(interaction.channel.id)
        entries = [e for e in log if e.get("c") == channel_id]
        label = f"#{getattr(interaction.channel, 'name', 'ce salon')}"
    if not entries:
        return await interaction.response.send_message(
            "Aucune tentative enregistrée pour le moment. Elles sont comptées à chaque clic sur le "
            "panneau de suivi.", ephemeral=True)
    await interaction.response.send_message(
        embed=stats_embed(compute_attempt_stats(entries), label, stats_by_rune(entries)))

@bot.tree.command(name="fmhistorique", description="Historique et records d'un joueur : ses FM, ses commandes, ses séries")
@app_commands.describe(joueur="Le joueur (toi par défaut)")
async def fmhistorique(interaction: discord.Interaction, joueur: discord.Member = None):
    target = joueur or interaction.user
    player_id = str(target.id)
    guild = interaction.guild

    def name_of(channel_id):
        try:
            found = guild.get_channel(int(channel_id)) if guild else None
        except (TypeError, ValueError):
            found = None
        return getattr(found, "name", "") or ""

    closed = [r for r in fm_state().get("history", []) if r.get("owner") == player_id]
    records = live_records_of(player_id, name_of) + closed
    log_entries = [e for e in attempt_log() if e.get("u") == player_id]
    owned = [cid for cid, e in fm_channels().items() if e.get("owner_id") == player_id]
    with_data = {r["ch"] for r in records}
    without_details = len([cid for cid in owned if cid not in with_data])

    if not records and not log_entries:
        extra = (f" Il a {len(owned)} salon(s) FM plus ancien(s), sans détails enregistrés." if owned else "")
        return await interaction.response.send_message(
            f"Aucune FM enregistrée pour {target.display_name}.{extra} L'historique détaillé démarre avec "
            "cette fonction : seules les sessions fermées ou en cours depuis y figurent.", ephemeral=True)

    profile = compute_player_profile(records, log_entries)
    await interaction.response.send_message(
        embed=history_embed(target.display_name, profile, records, len(owned), without_details),
        ephemeral=True)

@bot.tree.command(name="fmitem", description="Associer un équipement (DofusDB) à un salon : son icône apparaît dans le bilan")
@app_commands.describe(
    objet="Nom exact (Anneau de Padgref), identifiant ou adresse de la fiche DofusDB ; « aucun » pour retirer",
    salon="Salon à associer (par défaut : celui-ci). Utile pour un salon archivé, où l'on ne peut plus écrire",
)
async def fmitem(interaction: discord.Interaction, objet: str, salon: discord.TextChannel = None):
    target = salon or interaction.channel
    target_id = str(target.id)
    if not can_tag_channel(interaction.user, target_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut lui associer un équipement.", ephemeral=True)
    elsewhere = target_id != str(interaction.channel.id)

    if item_key(objet) in ITEM_REMOVE_WORDS:
        removed = channel_items().pop(target_id, None)
        if removed:
            save_data()
        text = (f"Équipement retiré du salon : {removed['name']}." if removed else "Aucun équipement n'était associé à ce salon.")
        return await interaction.response.send_message(text, ephemeral=elsewhere)

    await interaction.response.defer(ephemeral=elsewhere)
    try:
        found = await asyncio.to_thread(lookup_dofusdb_item, objet, dict(item_cache()))
    except ItemLookupError as exc:
        messages = {
            "badref": "Référence illisible. Donne le nom exact de l'équipement, son identifiant DofusDB, ou l'adresse de sa fiche.",
            "notfound": (f"Équipement introuvable : « {objet} ». Vérifie l'orthographe exacte (accents compris), ou donne "
                         "son identifiant DofusDB (`/fmitem objet:34329`) ou l'adresse de sa fiche."),
            "network": f"DofusDB est injoignable pour le moment ({exc.detail or 'erreur réseau'}). Réessaie dans quelques minutes.",
        }
        return await interaction.followup.send(messages.get(exc.kind, "Recherche impossible."), ephemeral=True)
    except Exception as exc:
        print(f"[EQUIPEMENTS] /fmitem a echoue : {exc!r}")
        return await interaction.followup.send("Erreur inattendue pendant la recherche : le détail est dans les logs du bot.", ephemeral=True)

    item = found["item"]
    item_cache()[item_key(item["name"])] = item
    channel_items()[target_id] = item
    save_data()

    def build_embed():
        detail = " · ".join(x for x in (f"Niveau {item['level']}" if item.get("level") else None, item.get("type_name"),
                                       f"id {item['id']}") if x)
        embed = discord.Embed(title=f"Équipement associé : {item['name']}", description=detail, color=discord.Color.blurple())
        embed.add_field(name="Salon", value=f"<#{target_id}>", inline=True)
        if found["notes"]:
            embed.add_field(name="À savoir", value="\n".join(found["notes"])[:1000], inline=False)
        if found["icon"]:
            embed.set_thumbnail(url="attachment://objet.png")
        return embed

    def build_files():
        return {"file": discord.File(BytesIO(found["icon"]), filename="objet.png")} if found["icon"] else {}

    await interaction.followup.send(embed=build_embed(), **build_files())
    if elsewhere:   # le salon cible (peut-etre archive) garde une trace visible de l'association
        try:
            await target.send(embed=build_embed(), **build_files())
        except discord.HTTPException as exc:
            print(f"[EQUIPEMENTS] message non poste dans #{getattr(target, 'name', '?')} : {exc}")

@fmitem.autocomplete("objet")
async def fmitem_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=label[:100], value=value[:100]) for label, value in item_autocomplete_choices(current)]

@bot.tree.command(name="fmrecap", description="Récapitulatif d'une commande à envoyer au client (page imprimable)")
@app_commands.describe(
    client="Nom du client, affiché sur le document (facultatif)",
    salon="Salon concerné (par défaut : celui-ci, même archivé)",
    tentatives="Afficher aussi le nombre de tentatives et leurs issues",
)
async def fmrecap(interaction: discord.Interaction, client: str = "", salon: discord.TextChannel = None, tentatives: bool = False):
    target = salon or interaction.channel
    target_id = str(target.id)
    if not can_tag_channel(interaction.user, target_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut produire le récapitulatif.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    tz = report_tz()
    now = datetime.datetime.now(tz)
    guild = interaction.guild

    live = sessions.get(target_id)
    provisional = live is not None and session_has_activity(live)
    if provisional:
        entry = fm_channels().get(target_id) or {}
        record = build_session_record(target_id, getattr(target, "name", ""), entry.get("owner_id"), live, None, "live")
        record["live"] = True
    else:
        past = [r for r in fm_state().get("history", []) if r.get("ch") == target_id]
        if not past:
            return await interaction.followup.send(
                "Aucune session à récapituler dans ce salon. Le récapitulatif existe pour une session en cours "
                "ou une session fermée depuis la mise en place de l'historique.", ephemeral=True)
        record = max(past, key=lambda r: r.get("end") or "")

    owner_id = record.get("owner")
    try:
        member = guild.get_member(int(owner_id)) if (guild and owner_id) else None
    except (TypeError, ValueError):
        member = None
    forgemage = getattr(member, "display_name", None) or (f"joueur …{str(owner_id)[-4:]}" if owner_id else "")
    item = channel_items().get(target_id)
    recap = build_recap(record, tz, now, forgemage, client=(client or "")[:60], item=item,
                        show_attempts=tentatives, provisional=provisional)

    icons, item_icon = {}, None
    if RUNE_IMAGES_ENABLED:
        try:
            gallery = await prepare_rune_gallery(record.get("items") or {})
            icons = gallery["icons"]
            if item:
                raw = await asyncio.to_thread(load_item_icons, [item])
                if item["id"] in raw:
                    item_icon = "data:image/png;base64," + base64.b64encode(raw[item["id"]]).decode()
        except Exception as exc:   # le document prime : sans icones plutot que pas de document
            print(f"[RECAP] icones ignorees : {exc!r}")
    page = render_recap_html(recap, icons, item_icon)
    slug = re.sub(r"[^a-z0-9]+", "-", _norm(recap["reference"])).strip("-") or "commande"
    due = recap["due"]
    summary = (f"**Récapitulatif de #{recap['reference']}** : "
               + (f"montant à régler **{format_number(due)} kamas**" if due >= 0 else f"trop-perçu de **{format_number(-due)} kamas**")
               + (" (provisoire : session en cours)" if provisional else ""))
    await interaction.followup.send(
        content=summary + "\nOuvre le fichier dans ton navigateur, puis imprime-le (PDF) ou envoie-le tel quel.",
        file=discord.File(BytesIO(page.encode("utf-8")), filename=f"recap-{slug}-{recap['when'].strftime('%Y-%m-%d')}.html"),
        ephemeral=True)

@bot.tree.command(name="fmshot", description="Joindre la capture de l'équipement terminé à ce salon (visible dans le bilan)")
@app_commands.describe(
    image="Capture d'écran de l'équipement terminé (PNG, JPEG ou WebP)",
    salon="Salon concerné (par défaut : celui-ci, même archivé)",
    retirer="Supprimer la capture déjà enregistrée pour ce salon",
)
async def fmshot(interaction: discord.Interaction, image: discord.Attachment = None, salon: discord.TextChannel = None, retirer: bool = False):
    target = salon or interaction.channel
    target_id = str(target.id)
    if not can_tag_channel(interaction.user, target_id):
        return await interaction.response.send_message(
            "Seul le propriétaire du salon (ou un administrateur) peut joindre une capture.", ephemeral=True)
    elsewhere = target_id != str(interaction.channel.id)

    if retirer:
        existed = channel_shots().pop(target_id, None) is not None
        removed = remove_shot(target_id)
        if existed:
            save_data()
        return await interaction.response.send_message(
            "Capture retirée du salon." if (existed or removed) else "Aucune capture n'était enregistrée pour ce salon.", ephemeral=elsewhere)
    if image is None:
        return await interaction.response.send_message(
            "Joins la capture de l'équipement terminé dans l'option `image` de la commande.", ephemeral=True)
    content_type = (getattr(image, "content_type", None) or "").lower()
    if content_type and not content_type.startswith("image/"):
        return await interaction.response.send_message("Ce fichier n'est pas une image : envoie une capture PNG, JPEG ou WebP.", ephemeral=True)
    if isinstance(getattr(image, "size", None), int) and image.size > SHOT_MAX_INPUT:
        return await interaction.response.send_message("Image trop lourde (8 Mo au maximum).", ephemeral=True)

    await interaction.response.defer(ephemeral=elsewhere)
    try:
        raw = await image.read()
        info = await asyncio.to_thread(encode_shot, raw)
        await asyncio.to_thread(save_shot, target_id, info)
    except ShotError as exc:
        return await interaction.followup.send(f"Capture refusée : {exc}", ephemeral=True)
    except Exception as exc:
        print(f"[CAPTURES] /fmshot a echoue : {exc!r}")
        return await interaction.followup.send("Erreur pendant l'enregistrement de la capture : le détail est dans les logs du bot.", ephemeral=True)
    channel_shots()[target_id] = {"ext": info["ext"], "w": info["w"], "h": info["h"], "bytes": len(info["data"]),
                                  "by": str(interaction.user.id), "at": now_iso()}
    touch_channel(target_id)
    save_data()

    text = (f"📸 Capture de l'équipement terminé enregistrée pour <#{target_id}> "
            f"({info['w']}×{info['h']}, {len(info['data']) // 1024 or 1} Ko). Elle s'affichera au survol de l'équipement dans le bilan.")
    if target_id not in channel_items():
        text += "\nAstuce : `/fmitem` associe aussi l'équipement (icône et nom), que la capture illustrera."
    if await asyncio.to_thread(shots_dir_ephemeral):
        text += "\n" + EPHEMERAL_SHOT_WARNING

    def build_embed():
        embed = discord.Embed(title="Équipement terminé", description=f"Capture ajoutée par <@{interaction.user.id}>.", color=discord.Color.blurple())
        embed.set_image(url=f"attachment://equipement.{info['ext']}")
        return embed
    def build_file():
        return discord.File(BytesIO(info["data"]), filename=f"equipement.{info['ext']}")

    await interaction.followup.send(content=text, embed=build_embed(), file=build_file())
    if elsewhere:   # le salon cible (peut-etre archive) garde une trace visible
        try:
            await target.send(embed=build_embed(), file=build_file())
        except discord.HTTPException as exc:
            print(f"[CAPTURES] message non poste dans #{getattr(target, 'name', '?')} : {exc}")

@bot.tree.command(name="fmbilan", description="Page de bilan : chronologie des sessions et totaux par mois, trimestre, année")
@app_commands.describe(
    periode="Période détaillée dans la chronologie (mois par défaut)",
    repere="Facultatif : 2026-09, septembre, T3-2026, 2026... (sinon la période en cours)",
    joueur="Facultatif : se limiter aux sessions d'un seul joueur",
)
@app_commands.choices(periode=[
    app_commands.Choice(name="Mois", value="mois"),
    app_commands.Choice(name="Trimestre", value="trimestre"),
    app_commands.Choice(name="Année", value="annee"),
    app_commands.Choice(name="Tout", value="tout"),
])
async def fmbilan(interaction: discord.Interaction, periode: str = "mois", repere: str = "",
                  joueur: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    tz = report_tz()
    now = datetime.datetime.now(tz)
    try:
        start, end, label = parse_report_period(periode, repere, now)
    except ValueError as exc:
        return await interaction.followup.send(str(exc), ephemeral=True)

    guild = interaction.guild

    def name_of(channel_id):
        try:
            found = guild.get_channel(int(channel_id)) if guild else None
        except (TypeError, ValueError):
            found = None
        return getattr(found, "name", "") or ""

    def owner_name_of(owner_id):
        try:
            member = guild.get_member(int(owner_id)) if guild else None
        except (TypeError, ValueError):
            member = None
        return getattr(member, "display_name", None) or f"joueur …{str(owner_id)[-4:]}"

    player_id = str(joueur.id) if joueur else None
    closed = [r for r in fm_state().get("history", []) if player_id is None or r.get("owner") == player_id]
    linked_items = channel_items()
    # Copies : l'equipement associe est ajoute a la volee, il ne doit jamais s'ecrire dans l'historique.
    records = [dict(r, item=linked_items.get(str(r.get("ch")))) for r in closed + live_records(name_of, player_id)]
    if not records:
        return await interaction.followup.send(
            "Aucune session enregistrée pour le moment. L'historique démarre avec la fermeture des sessions "
            "(`/fmstop`, archivage) : il n'y a rien avant.", ephemeral=True)

    report = build_report(records, tz, start, end, label, now,
                          player_label=joueur.display_name if joueur else None)
    runes = None   # option desactivee (FM_RUNE_IMAGES=0) : aucune galerie
    if RUNE_IMAGES_ENABLED:
        try:
            gallery = await prepare_rune_gallery(report["items_all"])
            runes = {"rows": build_rune_rows(report["items_all"], gallery), "notes": gallery["notes"]}
        except Exception as exc:   # le bilan prime : sans images plutot que pas de bilan
            print(f"[RUNES] galerie ignoree : {exc!r}")
    report["items_in_use"] = bool(linked_items)
    item_icons = await prepare_item_icons(report)
    shot_meta = channel_shots()
    wanted = []
    for _when, rr in sorted(report["timeline"], key=lambda x: x[0], reverse=True):   # les plus recentes d'abord
        ch = str(rr.get("ch"))
        if ch in shot_meta and ch not in wanted:
            wanted.append(ch)
    shots, skipped = await prepare_shots(wanted, shot_budget(interaction.guild))
    report["shots_skipped"] = skipped
    page = render_report_html(report, owner_name_of, runes, item_icons, shots)
    limit = getattr(interaction.guild, "filesize_limit", None)
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 10_000_000
    if shots and len(page.encode("utf-8")) > int(limit * 0.95):   # garde-fou : un fichier refuse par Discord ne sert a rien
        report["shots_skipped"] = len(wanted)
        page = render_report_html(report, owner_name_of, runes, item_icons, None)
    p = report["period"]
    summary = (f"**{label}** : {p['n']} session(s) · dépensé {format_number(p['total'])} · "
               f"facturé {format_number(p['total'] + p['adjust'])} · acomptes {format_number(p['paid'])} kamas")
    if p["n"] == 0:
        summary = f"**{label}** : aucune session sur cette période (la page montre les autres périodes)."
    if start is None:
        slug = "toutes-les-donnees"
    elif periode == "annee":
        slug = f"{start.year}"
    elif periode == "trimestre":
        slug = f"{start.year}-t{(start.month - 1) // 3 + 1}"
    else:
        slug = f"{start.year}-{start.month:02d}"
    await interaction.followup.send(
        content=summary + "\nOuvre le fichier dans ton navigateur.",
        file=discord.File(BytesIO(page.encode("utf-8")), filename=f"bilan-fm-{slug}.html"),
        ephemeral=True)

@bot.tree.command(name="fmarchive", description="Archiver ce salon FM (fin de la FM de l'objet)")
async def fmarchive(interaction: discord.Interaction):
    entry = fm_channels().get(str(interaction.channel.id))
    if not entry or entry.get("status") != "active":
        return await interaction.response.send_message("Ce salon n'est pas un salon FM actif.", ephemeral=True)
    if entry.get("owner_id") != str(interaction.user.id):
        return await interaction.response.send_message("Seul le proprietaire du salon peut l'archiver.", ephemeral=True)
    await interaction.response.send_message("Archivage du salon...")
    try:
        await archive_fm_channel(interaction.channel, "Salon archive : il reste consultable en lecture seule.")
    except discord.HTTPException as exc:
        print(f"[ARCHIVAGE] echec dans #{getattr(interaction.channel, 'name', '?')} : {exc!r}")
        await interaction.edit_original_response(content=(
            f"❌ Archivage impossible : {explain_discord_error(exc)}\n"
            "Rien n'a été modifié : la session et son panneau sont intacts."))
        return
    try:
        await interaction.edit_original_response(content="✅ Salon archivé.")
    except discord.HTTPException:
        pass

async def sync_commands():
    """
    Synchronise les commandes slash avec Discord. Ne leve JAMAIS : une erreur ici ne doit pas empecher le reste
    du demarrage. Journalise exactement ce que Discord a accepte ou refuse. Retourne (noms synchronises, erreur).
    """
    local = sorted(command.name for command in bot.tree.get_commands())
    try:
        synced = await bot.tree.sync()
    except Exception as exc:
        # Le message de Discord nomme la commande et le champ refuses (ex : description trop longue).
        print(f"[SYNC] ECHEC de la synchronisation : {exc}")
        print(f"[SYNC] commandes definies dans le code ({len(local)}) : {', '.join(local)}")
        return [], exc
    names = sorted(command.name for command in synced)
    print(f"[SYNC] {len(names)} commandes synchronisees : {', '.join(names)}")
    missing = [name for name in local if name not in names]
    if missing:
        print(f"[SYNC] ATTENTION, definies dans le code mais absentes de la reponse de Discord : {', '.join(missing)}")
    return names, None

# Commande a prefixe : utilisable dans N'IMPORTE QUEL salon (session ou non), reservee aux administrateurs.
@bot.command()
async def sync(ctx):
    perms = getattr(ctx.author, "guild_permissions", None)
    if not (perms and perms.administrator):
        return await ctx.send("Commande réservée aux administrateurs du serveur.")
    names, error = await sync_commands()
    if error is not None:
        return await ctx.send(f"❌ Synchronisation refusée par Discord : {str(error)[:1500]}")
    await ctx.send(f"✅ {len(names)} commandes synchronisées (version {BOT_VERSION}) : " + ", ".join(f"/{n}" for n in names))

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
                    await post_stock_prompt(channel, sessions[str(channel.id)])
                    await sync_attempt_panel(channel, sessions[str(channel.id)])
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
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    """Filet de securite : une commande qui plante ne laisse plus l'utilisateur devant un message figé."""
    original = getattr(error, "original", error)
    name = getattr(getattr(interaction, "command", None), "name", "?")
    print(f"[COMMANDE] /{name} a echoue : {original!r}")
    traceback.print_exception(type(original), original, original.__traceback__)
    message = (f"⚠️ La commande /{name} a rencontré une erreur ({type(original).__name__}). "
               "Le détail est dans les logs du bot.")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.event
async def setup_hook():
    bot.add_view(NewSessionView())
    bot.add_view(AttemptPanelView())
    bot.add_view(StockPromptView())
    bot.add_view(StockEndPromptView())

@bot.event
async def on_ready():
    load_data()
    migrate_stock_adjustments()
    await sync_commands()
    print(f"Bot connecte en tant que {bot.user} (version {BOT_VERSION})")
    if shots_dir_ephemeral():
        print(f"[CAPTURES] ATTENTION : {SHOT_DIR} n'est pas un volume Docker, les captures d'equipement seront perdues a la prochaine mise a jour.")
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
        deleted_session = sessions.pop(str(channel.id), None)
        if deleted_session is not None:
            record_session_history(channel, deleted_session, "deleted")
        panels().pop(str(channel.id), None)
        save_data()
        await send_event(site_event("channel-deleted", f"{channel.id}-deleted", channel))

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    channel_id = str(message.channel.id)
    if channel_id not in sessions or not sessions[channel_id].get("active", True):
        await bot.process_commands(message)   # commandes a prefixe (!sync) : aussi hors session
        return

    session = sessions[channel_id]
    session.setdefault("runes", {})

    images = [a for a in message.attachments if a.filename.lower().endswith(("png", "jpg", "jpeg"))]
    if images:
        entries, doubts, hashes = [], [], []
        for attachment in images:
            try:
                # Lecture dans un thread : l'OCR bloque, il ne doit pas figer le bot.
                analysis = await asyncio.to_thread(analyze_capture_url, attachment.url)
            except Exception as exc:
                print(f"[OCR] lecture impossible de {attachment.filename} : {exc}")
                doubts.append(f"{attachment.filename} : image illisible")
                continue
            h = analysis["hash"]
            if h in hashes:
                doubts.append("la même image était jointe deux fois : la copie est ignorée")
                continue
            if h in session.get("seen", []):
                doubts.append("image déjà comptée dans cette session : ignorée")
                continue
            hashes.append(h)
            entries.extend(analysis["entries"])
            doubts.extend(analysis["doubts"])

        if not entries and not doubts:
            await message.reply("Aucune ligne d'achat reconnue sur cette image : rien n'a été ajouté.")
        elif doubts:
            # Au moindre doute, RIEN n'est compté sans validation explicite.
            text = hold_message_text(entries, doubts)
            if entries:
                view = CaptureConfirmView(message, entries, hashes)
                view.reply_message = await message.reply(text, view=view)
            else:
                await message.reply(text)
        else:
            added = commit_entries(session, channel_id, message.author, entries, hashes, ref=message.id)
            try:
                await message.reply(capture_reply_text(message.author, entries, session))
            except discord.HTTPException as exc:
                print(f"[BOT] reponse impossible apres enregistrement d'une capture : {exc}")
            await send_capture_event(message, message.author, added, entries)
        await bump_attempt_panel(message.channel, session)

    await bot.process_commands(message)

# ---------------- RUN ---------------- #
TOKEN = os.environ.get("TOKEN")
bot.run(TOKEN)
