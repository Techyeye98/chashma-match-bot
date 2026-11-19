"""Frame Matcher - Telegram bot for Lenskart frames (Python standard library only).

Send a Lenskart product link (or specs like "50-20-145 136mm black square")
and the bot replies with the closest frames in the offer section (or the
whole catalogue). Tracked frames are watched for stock / price / offer
changes, and new offer arrivals that look like a tracked frame trigger an alert.

Run:   python bot.py            (needs BOT_TOKEN in .env next to this file)
Test:  python bot.py test <lenskart-url-or-specs> [budget] [offer|all]
"""
import datetime
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
ENV_FILE = os.path.join(HERE, ".env")


def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("BOT_TOKEN", "CHECK_TIMES", "DEFAULT_BUDGET", "ALERT_SCORE", "REQUIRED_CHANNELS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


ENV = load_env()


def env_num(key, default):
    try:
        return float(ENV.get(key, "") or default)
    except ValueError:
        return default


API = "https://api-gateway.juno.lenskart.com/v2/products"
OFFER_CAT = "12424"   # eyeglasses offer section
ALL_CAT = "7112"      # all eyeglasses
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

def parse_times(s):
    out = []
    for part in (s or "").split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", part)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(out))


# All schedule times are India time (IST, UTC+5:30), whatever the server's clock is set to.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")


def now_ist():
    return datetime.datetime.now(IST)


# Daily auto-check times, 24-hour India time. Default: 12:00 AM and 1:00 PM IST.
CHECK_TIMES = parse_times(ENV.get("CHECK_TIMES")) or [(0, 0), (13, 0)]


def times_text():
    return " aur ".join(
        datetime.time(h, m).strftime("%I:%M %p").lstrip("0") for h, m in CHECK_TIMES)
ALERT_SCORE = env_num("ALERT_SCORE", 14)           # max score for a new-arrival alert
DEFAULT_BUDGET = int(env_num("DEFAULT_BUDGET", 1200))
MAX_DETAIL = 150       # max product pages fetched per search
MAX_FAVS = 25

LOCK = threading.RLock()
STATE = {"users": {}, "seen_offer": None, "cache": {}, "recent_new": []}


# ---------------------------------------------------------------- state
def load_state():
    global STATE
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            STATE = json.load(f)
    STATE.setdefault("users", {})
    STATE.setdefault("seen_offer", None)
    STATE.setdefault("cache", {})
    STATE.setdefault("recent_new", [])


def save_state():
    with LOCK:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)


def user(chat_id):
    with LOCK:
        u = STATE["users"].setdefault(str(chat_id), {})
        u.setdefault("budget", DEFAULT_BUDGET)
        u.setdefault("scope", "offer")
        u.setdefault("alerts", True)
        u.setdefault("favs", [])
        u.setdefault("last", None)
        return u


# ---------------------------------------------------------------- http
def http_get(url, headers=None, timeout=30, tries=3):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # network hiccup: retry
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def num(s):
    m = re.search(r"\d+(\.\d+)?", str(s or ""))
    return float(m.group()) if m else None


# ---------------------------------------------------------------- lenskart
def eff_price(prices):
    vals = [p.get("price") for p in prices or []
            if p.get("name") != "Market Price" and p.get("price")]
    return int(min(vals)) if vals else None


def mrp_price(prices):
    for p in prices or []:
        if p.get("name") == "Market Price":
            return int(p.get("price") or 0)
    return None


def list_category(cat):
    """All products of a category (light listing data)."""
    out, page = [], 0
    while page < 40:
        j = json.loads(http_get(f"{API}/category/{cat}?page-size=200&page={page}"))
        items = (j.get("result") or {}).get("product_list") or []
        for p in items:
            tags = {t.get("property"): t.get("name") for t in p.get("hashtagList") or []}
            out.append({
                "id": str(p.get("id")),
                "url": p.get("product_url"),
                "width": num(p.get("width")),
                "price": eff_price(p.get("prices")),
                "colour": p.get("frameColor") or p.get("color") or "",
                "shape": tags.get("frame_shape_id"),
                "type": tags.get("frametype_id"),
            })
        if len(items) < 200:
            break
        page += 1
    return out


def product(pid):
    """Full specs of one product, live price and stock included."""
    pid = str(pid)
    j = json.loads(http_get(f"{API}/product/{pid}", {"x-api-client": "desktop"}))
    r = j.get("result") or {}
    if not r.get("id"):
        raise ValueError("product not found")
    sp = {}
    for g in r.get("specifications") or []:
        for it in g.get("items") or []:
            sp[it.get("name")] = it.get("value")
    d = [int(x) for x in re.findall(r"\d+", sp.get("Frame Dimensions") or "")][:3]
    d += [None] * (3 - len(d))
    p = {
        "id": pid,
        "url": r.get("url"),
        "brand": (r.get("brandName") or "").replace(" Online", ""),
        "model": sp.get("Model No.") or r.get("modelName"),
        "lens": d[0], "bridge": d[1], "temple": d[2],
        "width": num(sp.get("Frame Width") or r.get("frameWidth")),
        "height": num(sp.get("Lens Height") or sp.get("Height")),
        "weight": num(sp.get("Weight")),
        "material": sp.get("Frame Material") or sp.get("Material"),
        "temple_mat": sp.get("Temple Material"),
        "colour": sp.get("Frame colour") or r.get("frameColour") or r.get("color"),
        "type": sp.get("Frame Type"),
        "shape": sp.get("Frame Shape"),
        "size": sp.get("Frame Size"),
        "price": eff_price(r.get("prices")),
        "mrp": mrp_price(r.get("prices")),
        "qty": int(r.get("qty") or 0),
        "rating": round(float(r.get("avgRating") or 0), 2) or None,
        "ratings": r.get("totalNoOfRatings"),
    }
    with LOCK:
        STATE["cache"][pid] = p
    return p


def _find_pdp(o, depth=0):
    if depth > 14:
        return None
    if isinstance(o, dict):
        if o.get("id") and isinstance(o.get("specifications"), list) and o["specifications"]:
            return str(o["id"])
        for v in o.values():
            r = _find_pdp(v, depth + 1)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_pdp(v, depth + 1)
            if r:
                return r
    return None


def id_from_url(url):
    url = url.split("?")[0].split("#")[0]
    try:
        page = http_get(url, {"Accept": "text/html"})
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
        if m:
            pid = _find_pdp(json.loads(m.group(1)))
            if pid:
                return pid
    except Exception:
        pass
    path = urllib.parse.urlparse(url).path
    for cat in (ALL_CAT, OFFER_CAT):  # fallback: look the link up in the listings
        for p in list_category(cat):
            if p["url"] and urllib.parse.urlparse(p["url"]).path == path:
                return p["id"]
    raise ValueError("Is link se product nahi mila")


# ---------------------------------------------------------------- matching
BOXY = {"Square", "Rectangle", "Wayfarer"}
SHAPES = ["Rectangle", "Square", "Round", "Cat Eye", "Aviator", "Geometric",
          "Wayfarer", "Oval", "Hexagonal", "Clubmaster"]
COLOURS = ["black", "blue", "brown", "grey", "gray", "gold", "silver", "green",
           "red", "pink", "transparent", "gunmetal", "white", "tortoise",
           "purple", "yellow", "orange", "rose"]
MATERIALS = {"acetate": "Acetate", "tr90": "TR90", "metal": "Metal",
             "steel": "Stainless Steel", "titanium": "Titanium"}


def colour_words(c):
    c = (c or "").lower()
    return {w for w in COLOURS if w in c}


def score(t, c):
    """Lower = closer. None when the candidate lacks dimensions."""
    if c.get("lens") is None or c.get("width") is None:
        return None
    s = 0.0
    for key, wt in (("lens", 2), ("bridge", 1.5), ("temple", 0.3),
                    ("width", 1), ("height", 1.5)):
        if t.get(key) is not None and c.get(key) is not None:
            s += abs(t[key] - c[key]) * wt
    if t.get("type") and c.get("type") != t["type"]:
        s += 15
    if t.get("shape") and c.get("shape") != t["shape"]:
        s += 4 if {t["shape"], c.get("shape")} <= BOXY else 10
    tc, cc = colour_words(t.get("colour")), colour_words(c.get("colour"))
    if tc and not (tc & cc):
        s += 6
    if t.get("material") and c.get("material") != t["material"]:
        s += 3
    return round(s, 1)


def parse_specs(text):
    """Manual specs, e.g. '50-20-145 136mm black square acetate'."""
    m = re.search(r"(\d{2})\s*[-x ]\s*(\d{2})\s*[-x ]\s*(\d{3})", text)
    if not m:
        return None
    t = {"lens": int(m.group(1)), "bridge": int(m.group(2)), "temple": int(m.group(3)),
         "type": "Full Rim", "model": "Manual specs", "brand": ""}
    rest = text[:m.start()] + " " + text[m.end():]
    w = re.search(r"\b(1[2-5]\d)\s*(mm)?\b", rest)
    t["width"] = float(w.group(1)) if w else t["lens"] * 2 + t["bridge"] + 15.0
    t["width_guess"] = not w
    low = rest.lower()
    for sh in SHAPES:
        if sh.lower() in low or (sh == "Rectangle" and "rect" in low):
            t["shape"] = sh
            break
    cols = [c for c in COLOURS if c in low]
    if cols:
        t["colour"] = " ".join(cols).title()
    for k, v in MATERIALS.items():
        if k in low:
            t["material"] = v
            break
    if "half rim" in low:
        t["type"] = "Half Rim"
    elif "rimless" in low:
        t["type"] = "Rimless"
    return t


def find_matches(t, budget, scope, top=5):
    cat = ALL_CAT if scope == "all" else OFFER_CAT
    listing = [p for p in list_category(cat)
               if p["price"] and p["price"] <= budget and p["id"] != str(t.get("id"))]
    total = len(listing)
    if t.get("width") is not None:
        tol = 8 if scope == "offer" else 5
        listing = [p for p in listing
                   if p["width"] is None or abs(p["width"] - t["width"]) <= tol]
    if scope == "all" and t.get("type"):
        listing = [p for p in listing if p["type"] in (None, t["type"])]

    def rough(p):  # cheap pre-rank from listing data only
        r = abs((p["width"] or 0) - (t.get("width") or 0))
        if t.get("shape") and p["shape"] != t["shape"]:
            r += 3 if {t["shape"], p["shape"]} <= BOXY else 8
        if colour_words(t.get("colour")) and not (
                colour_words(t.get("colour")) & colour_words(p["colour"])):
            r += 5
        return r

    listing.sort(key=rough)
    listing = listing[:MAX_DETAIL]

    def detail(p):
        try:
            c = dict(STATE["cache"].get(p["id"]) or product(p["id"]))
            c["price"] = p["price"]  # listing price is the live one
            return c
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        cands = [c for c in ex.map(detail, listing) if c]
    save_state()
    ranked = []
    for c in cands:
        s = score(t, c)
        if s is not None:
            ranked.append((s, c))
    ranked.sort(key=lambda x: x[0])
    ranked = ranked[:top]
    refresh_live([c for _, c in ranked])  # cached specs may carry old stock
    save_state()
    return ranked, total


# ---------------------------------------------------------------- text
def e(s):
    return html.escape(str(s if s is not None else "-"))


def fmt_num(v, unit=""):
    if v is None:
        return "-"
    return f"{v:g}{unit}"


def dims(p):
    if p.get("lens") is None:
        return "-"
    return f"{p['lens']}-{p['bridge']}-{p['temple']}"


def name_of(p):
    return f"{p.get('brand') or ''} {p.get('model') or ''}".strip()


def describe(p):
    return (f"<b>{e(name_of(p))}</b>\n"
            f"Size: {dims(p)} | width {fmt_num(p.get('width'), 'mm')} | "
            f"height {fmt_num(p.get('height'), 'mm')} | {fmt_num(p.get('weight'), 'g')}\n"
            f"{e(p.get('colour'))} | {e(p.get('shape'))} | {e(p.get('material'))}"
            + (f"\n{stock_label(p.get('qty'))}" if p.get("id") else ""))


def diff_line(t, c):
    out = []
    for key in ("lens", "bridge", "temple", "width", "height"):
        if t.get(key) is None or c.get(key) is None:
            continue
        d = c[key] - t[key]
        if d:
            out.append(f"{key} {'+' if d > 0 else ''}{d:g}mm")
    if t.get("weight") is not None and c.get("weight") is not None:
        d = c["weight"] - t["weight"]
        if abs(d) >= 3:
            out.append(f"{abs(d):g}g {'bhaari' if d > 0 else 'halka'}")
    if t.get("shape") and c.get("shape") != t["shape"]:
        out.append(f"shape {c.get('shape')}")
    if t.get("material") and c.get("material") != t["material"]:
        out.append(f"material {c.get('material')}")
    if colour_words(t.get("colour")) and not (
            colour_words(t.get("colour")) & colour_words(c.get("colour"))):
        out.append(f"colour {c.get('colour')}")
    return ", ".join(out) if out else "almost same"


def label(s):
    return "Bahut close" if s <= 8 else ("Close" if s <= 14 else "Thoda alag")


def stock_label(q):
    """Online stock count from Lenskart, with a buy-soon hint."""
    if q is None:
        return "Stock: pata nahi"
    q = int(q)
    if q <= 0:
        return "❌ Out of stock"
    if q <= 10:
        return f"🔥 Sirf {q} piece bache, jaldi lo"
    if q <= 50:
        return f"⚠️ Kam stock: {q} piece"
    return f"✅ Achha stock: {q} piece"


def refresh_live(items):
    """Re-fetch stock (and price when missing) for the frames we are about to show."""
    def one(c):
        try:
            p = product(c["id"])
            c["qty"] = p["qty"]
            c["price"] = c.get("price") or p["price"]
        except Exception:
            c.setdefault("qty", None)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, items))


def scope_name(scope):
    return "poora Lenskart" if scope == "all" else "offer section"


def match_text(t, budget, scope):
    """Returns (message text, ranked matches)."""
    ranked, total = find_matches(t, budget, scope)
    head = (f"🎯 <b>Tumhara frame</b>\n{describe(t)}\n\n"
            f"🔎 {scope_name(scope)} mein ₹{budget} tak ke {total} frames check kiye.\n")
    if t.get("width_guess"):
        head += "(Frame width nahi diya tha, to andaza lagaya hai.)\n"
    if not ranked:
        return head + "\nKoi match nahi mila. Budget badha ke ya poora Lenskart chun ke try karo.", []
    lines = [head]
    for i, (s, c) in enumerate(ranked, 1):
        rating = f" | ⭐ {c['rating']}" if c.get("rating") else ""
        lines.append(
            f"<b>{i}. {e(name_of(c))}</b> - ₹{c['price']}{rating}\n"
            f"{label(s)} | {dims(c)}, width {fmt_num(c['width'], 'mm')}, "
            f"height {fmt_num(c['height'], 'mm')}, {fmt_num(c['weight'], 'g')}\n"
            f"{e(c['colour'])} | {e(c['shape'])} | {e(c['material'])}\n"
            f"{stock_label(c.get('qty'))}\n"
            f"Farak: {e(diff_line(t, c))}\n{e(c['url'])}\n")
    return "\n".join(lines), ranked


SUPPORT_CONTACT = "@Techyangel"
SUPPORT_USERNAME = SUPPORT_CONTACT.lstrip("@")
SUPPORT_PREFILL = "Hey, I need offer details for Lenskart"
SUPPORT_URL = (f"https://t.me/{SUPPORT_USERNAME}?text="
               + urllib.parse.quote(SUPPORT_PREFILL))


# ---------------------------------------------------------------- channel join gate
def parse_channels(s):
    """REQUIRED_CHANNELS=@public1,@public2,-1001234567890|https://t.me/+invite
    Public channels: @username. Private channels: numeric id|invite link."""
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        cid, _, link = part.partition("|")
        cid, link = cid.strip(), link.strip()
        if cid.startswith("@") and not link:
            link = f"https://t.me/{cid[1:]}"
        if cid and link:
            out.append({"id": cid, "link": link})
    return out


REQUIRED_CHANNELS = parse_channels(ENV.get("REQUIRED_CHANNELS"))
JOIN_OK_SECONDS = 600            # re-check a member at most every 10 min
JOINED_CACHE = {}                # user_id -> time last confirmed as member
CHANNEL_TITLES = {}


def channel_title(ch):
    if ch["id"] not in CHANNEL_TITLES:
        try:
            CHANNEL_TITLES[ch["id"]] = tg("getChat", chat_id=ch["id"])["result"].get("title")
        except Exception:
            CHANNEL_TITLES[ch["id"]] = None
    return CHANNEL_TITLES[ch["id"]] or ch["id"]


def missing_channels(user_id, fresh=False):
    """Channels this user has not joined. If the bot can't check a channel
    (not admin there, wrong id) that channel is skipped, so users never get stuck."""
    if not REQUIRED_CHANNELS or not user_id:
        return []
    if not fresh and time.time() - JOINED_CACHE.get(user_id, 0) < JOIN_OK_SECONDS:
        return []
    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            st = tg("getChatMember", chat_id=ch["id"], user_id=user_id)["result"]
            ok = st.get("status") in ("creator", "administrator", "member") or (
                st.get("status") == "restricted" and st.get("is_member"))
        except Exception as ex:
            print(f"join check failed for {ch['id']} (bot ko channel mein admin banao):", ex, flush=True)
            ok = True
        if not ok:
            missing.append(ch)
    if not missing:
        JOINED_CACHE[user_id] = time.time()
    return missing


def join_prompt(chat_id, missing):
    rows = [[{"text": f"📢 Join karo: {channel_title(ch)}", "url": ch["link"]}] for ch in missing]
    rows.append([{"text": "✅ Join kar liya", "callback_data": "joined"}])
    send(chat_id, "🔒 <b>Bot use karne ke liye pehle hamare channel join karo</b>\n\n"
                  "Neeche ke channel join karo, phir <b>✅ Join kar liya</b> dabao.",
         {"inline_keyboard": rows})


def gate(chat, uid, text, cq):
    """True when the update may go to the normal handlers."""
    if not REQUIRED_CHANNELS:
        return True
    if cq:
        if (cq.get("data") or "") == "joined":
            missing = missing_channels(uid, fresh=True)
            if missing:
                try:
                    tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert="true",
                       text="Abhi saare channel join nahi hue. Join karke dobara dabao.")
                except Exception:
                    pass
                return False
            try:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Shukriya! 🎉")
            except Exception:
                pass
            send(chat, "✅ Shukriya! Ab bot use kar sakte ho.\n\n" + help_text(), MAIN_KB)
            return False
        missing = missing_channels(uid)
        if missing:
            try:
                tg("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass
            join_prompt(chat, missing)
            return False
        return True
    first = (text or "").strip().split(" ")[0].lower().split("@")[0]
    if first == "/help" or (text or "").strip() == B_HELP:
        return True              # contact button always works
    missing = missing_channels(uid)
    if missing:
        join_prompt(chat, missing)
        return False
    return True


def support_text():
    return ("📩 <b>Lenskart offers aur help</b>\n\n"
            "Lenskart ke latest offers, best deal ya bot use karne mein koi dikkat ho, "
            "to seedha humse baat karo.\n"
            "Neeche button dabao.")


def support_markup():
    return {"inline_keyboard": [[{"text": "💬 Offers aur help ke liye message karo",
                                  "url": SUPPORT_URL}]]}


def help_text():
    return (
        "👓 <b>Frame Matcher</b>\n\n"
        "Apne pasand ke frame ka Lenskart link bhejo. Main offer section mein "
        "uske sabse close frames dhoond dunga, farak ke saath.\n\n"
        "<b>Kaise use karein</b>\n"
        "• Lenskart product link bhejo\n"
        "• Ya specs likho: <code>50-20-145 136mm black square acetate</code>\n\n"
        "<b>Neeche ke buttons</b>\n"
        "🔎 Frame dhoondo - link ya specs bhejne ka tareeka\n"
        "⭐ Tracked frames - jo frames track ho rahe hain, stock aur price ke saath\n"
        "💰 Budget - max price chuno\n"
        "🌐 Kahan dhoondu - offer section ya poora Lenskart\n"
        "🆕 Naye offer frames - pichhle 7 din mein offer mein aaye frames\n"
        "🔄 Abhi check karo - stock aur price turant check\n"
        "🔔 Alerts - auto alerts on ya off\n"
        "⚙️ Settings - current settings\n\n"
        "<b>Track karna</b>\n"
        "Result ke neeche ⭐ button dabao. Apna frame ya koi bhi match track ho jayega.\n\n"
        "<b>Stock</b> (har frame ke saath live dikhta hai)\n"
        "❌ Out of stock - abhi nahi mil raha\n"
        "🔥 Sirf kuch piece bache - 10 ya kam, jaldi lo\n"
        "⚠️ Kam stock - 11 se 50 piece\n"
        "✅ Achha stock - 50 se zyada, araam se le sakte ho\n\n"
        f"<b>Auto alerts</b> (roz {times_text()})\n"
        "• Tracked frame stock mein wapas aaye\n"
        "• Uska price gire ya wo offer mein aa jaye\n"
        "• Offer mein naya frame aaye jo tumhare tracked frame jaisa ho\n\n"
        f"📩 <b>Lenskart offers aur help ke liye contact:</b> {SUPPORT_CONTACT}"
    )


# ---------------------------------------------------------------- telegram
TOKEN = None

B_FIND = "🔎 Frame dhoondo"
B_FAVS = "⭐ Tracked frames"
B_BUDGET = "💰 Budget"
B_SCOPE = "🌐 Kahan dhoondu"
B_NEW = "🆕 Naye offer frames"
B_CHECK = "🔄 Abhi check karo"
B_ALERTS = "🔔 Alerts"
B_SETTINGS = "⚙️ Settings"
B_HELP = "❓ Help"

BUTTON_CMD = {B_FIND: "/find", B_FAVS: "/favs", B_BUDGET: "/budget", B_SCOPE: "/scope",
              B_NEW: "/new", B_CHECK: "/check", B_ALERTS: "/alerts",
              B_SETTINGS: "/settings", B_HELP: "/help"}

MAIN_KB = {
    "keyboard": [[B_FIND, B_FAVS], [B_BUDGET, B_SCOPE], [B_NEW, B_CHECK],
                 [B_ALERTS, B_SETTINGS], [B_HELP]],
    "resize_keyboard": True,
    "is_persistent": True,
}

COMMANDS = [
    ("start", "Bot shuru karo, buttons dikhao"),
    ("favs", "Tracked frames"),
    ("budget", "Max price set karo"),
    ("scope", "Offer section ya poora Lenskart"),
    ("new", "Naye offer frames"),
    ("check", "Abhi stock aur price check karo"),
    ("alerts", "Auto alerts on/off"),
    ("settings", "Current settings"),
    ("help", "Help"),
]


def inline(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                for row in rows]}


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read().decode())


def send(chat_id, text, markup=None):
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        params = dict(chat_id=chat_id, text=c.strip() or "-", parse_mode="HTML",
                      disable_web_page_preview="true")
        if markup and i == len(chunks) - 1:
            params["reply_markup"] = json.dumps(markup)
        try:
            tg("sendMessage", **params)
        except Exception as ex:
            print("send failed:", ex)


LINK_RE = re.compile(r"https?://(?:www\.)?lenskart\.com/\S+")


def target_from_text(text):
    m = LINK_RE.search(text)
    if m:
        return product(id_from_url(m.group()))
    return parse_specs(text)


def add_fav(u, t):
    if not t.get("id"):
        return "Sirf Lenskart link waale frames track ho sakte hain."
    if any(f["id"] == t["id"] for f in u["favs"]):
        return f"<b>{e(name_of(t))}</b> pehle se track ho raha hai."
    if len(u["favs"]) >= MAX_FAVS:
        return f"Max {MAX_FAVS} frames track ho sakte hain. Pehle koi hatao."
    f = dict(t)
    f["in_stock"] = t.get("qty", 0) > 0
    f["in_offer"] = False
    u["favs"].append(f)
    save_state()
    stock = stock_label(t.get("qty"))
    if not f["in_stock"]:
        stock += ", wapas aate hi bataunga"
    return (f"⭐ Track ho raha hai: <b>{e(name_of(t))}</b> (₹{t.get('price')})\n{stock}\n"
            "Stock, price aur iske jaise naye offer frames ka alert milega.")


def favs_message(u):
    if not u["favs"]:
        return ("Abhi koi frame track nahi ho raha.\n"
                "Link bhejo, phir result ke neeche ⭐ button dabao."), None
    lines, rows = ["⭐ <b>Tracked frames</b>\n"], []
    for i, f in enumerate(u["favs"], 1):
        try:
            p = product(f["id"])
            f.update(price=p["price"], qty=p["qty"], in_stock=p["qty"] > 0)
        except Exception:
            pass
        lines.append(f"<b>{i}. {e(name_of(f))}</b> - ₹{f.get('price')}\n"
                     f"{stock_label(f.get('qty'))}\n"
                     f"{dims(f)}, {e(f.get('colour'))} {e(f.get('shape'))}\n{e(f['url'])}\n")
        rows.append([(f"🔎 {i} jaise dhoondo", f"sim:{f['id']}"), (f"❌ {i} hatao", f"unfav:{f['id']}")])
    save_state()
    return "\n".join(lines), inline(rows)


def new_message(u):
    cutoff = time.time() - 7 * 86400
    with LOCK:
        items = [n for n in STATE["recent_new"] if n["ts"] >= cutoff]
    items = [n for n in items if (n.get("price") or 0) <= u["budget"]][-12:][::-1]
    if not items:
        return ("Pichhle 7 din mein tumhare budget mein koi naya offer frame nahi aaya.\n"
                "(Bot jab se chal raha hai tab se hi naye frames ginta hai.)"), None
    items = [dict(n) for n in items]
    refresh_live(items)
    lines, rows, row = [f"🆕 <b>Naye offer frames</b> (₹{u['budget']} tak)\n"], [], []
    for i, n in enumerate(items, 1):
        lines.append(f"<b>{i}. {e(name_of(n))}</b> - ₹{n.get('price')}\n"
                     f"{stock_label(n.get('qty'))}\n"
                     f"{dims(n)}, width {fmt_num(n.get('width'), 'mm')}, "
                     f"{e(n.get('colour'))} {e(n.get('shape'))}\n{e(n['url'])}\n")
        row.append((f"⭐ {i}", f"fav:{n['id']}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    lines.append("Track karne ke liye neeche ⭐ number dabao.")
    return "\n".join(lines), inline(rows)


def settings_text(u):
    return (f"⚙️ <b>Settings</b>\nBudget: ₹{u['budget']}\nKahan: {scope_name(u['scope'])}\n"
            f"Alerts: {'ON' if u['alerts'] else 'OFF'}\nTracked frames: {len(u['favs'])}")


def do_match(chat_id, u, t):
    send(chat_id, "Dhoond raha hoon, thoda time lagega...")
    u["last"] = t
    save_state()
    text, ranked = match_text(t, u["budget"], u["scope"])
    rows = []
    if t.get("id"):
        rows.append([("⭐ Mera frame track karo", f"fav:{t['id']}")])
    if ranked:
        rows.append([(f"⭐ {i}", f"fav:{c['id']}") for i, (_, c) in enumerate(ranked, 1)])
        text += "\nTrack karne ke liye neeche ⭐ number dabao."
    send(chat_id, text, inline(rows) if rows else None)


def handle(chat_id, text):
    u = user(chat_id)
    text = text.strip()
    if text in BUTTON_CMD:
        text = BUTTON_CMD[text]
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    arg = arg.strip()

    if cmd == "/start":
        save_state()
        return send(chat_id, help_text(), MAIN_KB)
    if cmd == "/help":
        return send(chat_id, support_text(), support_markup())
    if cmd == "/find":
        return send(chat_id, "Frame ka Lenskart link yahan paste karo.\n\n"
                             "Ya specs likho, jaise:\n<code>50-20-145 136mm black square acetate</code>\n\n"
                             f"Abhi: ₹{u['budget']} tak, {scope_name(u['scope'])} mein.")
    if cmd == "/budget":
        n = num(arg)
        if n and n >= 100:
            u["budget"] = int(n)
            save_state()
            return send(chat_id, f"Budget ab ₹{u['budget']} hai.")
        opts = [500, 800, 1200, 1500, 2000, 3000]
        return send(chat_id, f"Abhi budget ₹{u['budget']} hai. Naya chuno, ya likho: /budget 900",
                    inline([[(f"₹{o}", f"budget:{o}") for o in opts[:3]],
                            [(f"₹{o}", f"budget:{o}") for o in opts[3:]]]))
    if cmd == "/scope":
        if arg.lower() in ("offer", "all"):
            u["scope"] = arg.lower()
            save_state()
            return send(chat_id, f"Ab {scope_name(u['scope'])} mein dhoondunga.")
        return send(chat_id, f"Abhi: {scope_name(u['scope'])}. Kahan dhoondu?",
                    inline([[("🏷 Sirf offer section", "scope:offer"),
                             ("🌐 Poora Lenskart", "scope:all")]]))
    if cmd == "/alerts":
        if arg.lower() in ("on", "off"):
            u["alerts"] = arg.lower() == "on"
            save_state()
            return send(chat_id, "Auto alerts: " + ("ON" if u["alerts"] else "OFF"))
        return send(chat_id, "Auto alerts abhi " + ("ON" if u["alerts"] else "OFF") + " hain.",
                    inline([[("🔔 ON", "alerts:on"), ("🔕 OFF", "alerts:off")]]))
    if cmd == "/settings":
        return send(chat_id, settings_text(u))
    if cmd == "/favs":
        return send(chat_id, *favs_message(u))
    if cmd == "/new":
        return send(chat_id, *new_message(u))
    if cmd == "/unfav":
        n = num(arg)
        if not n or not (1 <= int(n) <= len(u["favs"])):
            return send(chat_id, "Number batao, jaise /unfav 1")
        f = u["favs"].pop(int(n) - 1)
        save_state()
        return send(chat_id, f"Hata diya: {e(name_of(f))}")
    if cmd == "/fav":
        t = target_from_text(arg) if arg else u.get("last")
        if not t:
            return send(chat_id, "Pehle frame ka link bhejo, phir ⭐ button dabao.")
        return send(chat_id, add_fav(u, t))
    if cmd == "/check":
        send(chat_id, "Check kar raha hoon...")
        if not run_watch(only_chat=str(chat_id)):
            send(chat_id, "Abhi koi nayi update nahi hai.")
        return
    if cmd.startswith("/"):
        return send(chat_id, "Ye command samajh nahi aaya. Neeche ke buttons use karo.", MAIN_KB)

    t = target_from_text(text)
    if not t:
        return send(chat_id, "Lenskart ka product link bhejo, ya specs aise likho:\n"
                             "<code>50-20-145 136mm black square</code>", MAIN_KB)
    do_match(chat_id, u, t)


def on_callback(cq):
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    data = cq.get("data") or ""
    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
    except Exception:
        pass
    if not chat_id:
        return
    u = user(chat_id)
    kind, _, val = data.partition(":")
    if kind == "fav":
        return send(chat_id, add_fav(u, product(val)))
    if kind == "unfav":
        for f in list(u["favs"]):
            if f["id"] == val:
                u["favs"].remove(f)
                save_state()
                return send(chat_id, f"Hata diya: {e(name_of(f))}")
        return send(chat_id, "Ye pehle hi hat chuka hai.")
    if kind == "sim":
        return do_match(chat_id, u, product(val))
    if kind == "budget" and num(val):
        u["budget"] = int(num(val))
        save_state()
        return send(chat_id, f"Budget ab ₹{u['budget']} hai.")
    if kind == "scope" and val in ("offer", "all"):
        u["scope"] = val
        save_state()
        return send(chat_id, f"Ab {scope_name(val)} mein dhoondunga.")
    if kind == "alerts" and val in ("on", "off"):
        u["alerts"] = val == "on"
        save_state()
        return send(chat_id, "Auto alerts: " + ("ON" if u["alerts"] else "OFF"))


# ---------------------------------------------------------------- watcher
def run_watch(only_chat=None):
    """Stock / price / offer / new-arrival alerts. Returns number of alerts sent."""
    sent = 0
    offer = list_category(OFFER_CAT)
    offer_ids = {p["id"] for p in offer}
    offer_by_id = {p["id"]: p for p in offer}
    with LOCK:
        seen = STATE["seen_offer"]
        new_ids = [] if seen is None else [i for i in offer_ids if i not in set(seen)]
        users = {k: v for k, v in STATE["users"].items()
                 if (only_chat is None or k == only_chat)}

    new_items = []
    for pid in new_ids[:80]:
        try:
            c = dict(product(pid))
            c["price"] = offer_by_id[pid]["price"] or c["price"]
            new_items.append(c)
        except Exception:
            pass

    for chat_id, u in users.items():
        if not u.get("alerts", True) and only_chat is None:
            continue
        msgs = []
        for f in u.get("favs", []):
            try:
                p = product(f["id"])
            except Exception:
                continue
            name = f"<b>{e(name_of(f))}</b>"
            now_stock = p["qty"] > 0
            if now_stock and not f.get("in_stock", True):
                msgs.append((f"🟢 Stock mein wapas aa gaya: {name} - ₹{p['price']}\n"
                             f"{stock_label(p['qty'])}\n{e(f['url'])}", None))
            elif not now_stock and f.get("in_stock", True):
                msgs.append((f"🔴 Out of stock ho gaya: {name}. Wapas aate hi bataunga.", None))
            if p["price"] and f.get("price") and p["price"] < f["price"]:
                msgs.append((f"💰 Price gir gaya: {name} ₹{f['price']} → ₹{p['price']}\n{e(f['url'])}", None))
            now_offer = f["id"] in offer_ids
            if now_offer and not f.get("in_offer"):
                msgs.append((f"🏷 Tumhara tracked frame ab offer section mein hai: {name} - "
                             f"₹{offer_by_id[f['id']]['price']}\n{e(f['url'])}", None))
            f.update(in_stock=now_stock, price=p["price"] or f.get("price"), in_offer=now_offer)

            for c in new_items:
                if c["id"] == f["id"] or (c["price"] or 0) > u.get("budget", DEFAULT_BUDGET):
                    continue
                s = score(f, c)
                if s is not None and s <= ALERT_SCORE:
                    msgs.append((
                        f"🆕 Offer mein naya frame aaya jo {name} jaisa hai:\n"
                        f"<b>{e(name_of(c))}</b> - ₹{c['price']} ({label(s)})\n"
                        f"{stock_label(c.get('qty'))}\n"
                        f"{dims(c)}, width {fmt_num(c['width'], 'mm')}, {e(c['colour'])} {e(c['shape'])}\n"
                        f"Farak: {e(diff_line(f, c))}\n{e(c['url'])}",
                        inline([[("⭐ Ise track karo", f"fav:{c['id']}")]])))
        for m, markup in msgs:
            send(chat_id, m, markup)
            sent += 1

    with LOCK:
        if only_chat is None or STATE["seen_offer"] is None:
            STATE["seen_offer"] = sorted(offer_ids)
            now = time.time()
            for c in new_items:
                STATE["recent_new"].append({k: c.get(k) for k in (
                    "id", "url", "brand", "model", "lens", "bridge", "temple",
                    "width", "colour", "shape", "price")} | {"ts": now})
            STATE["recent_new"] = [n for n in STATE["recent_new"]
                                   if n["ts"] >= now - 7 * 86400][-200:]
    save_state()
    return sent


def next_check(now=None):
    """Next scheduled check time (India time) after `now`."""
    now = now or now_ist()
    slots = []
    for day in (0, 1):
        base = (now + datetime.timedelta(days=day)).replace(second=0, microsecond=0)
        for h, m in CHECK_TIMES:
            t = base.replace(hour=h, minute=m)
            if t > now:
                slots.append(t)
    return min(slots)


def watcher_loop():
    # First ever start: only remember today's offer list, so later checks can spot new frames.
    if STATE.get("seen_offer") is None:
        try:
            run_watch()
        except Exception as ex:
            print("first watch error:", ex)
    while True:
        target = next_check()
        print("Agla auto check:", target.strftime("%d-%m %I:%M %p IST"), flush=True)
        while now_ist() < target:
            left = (target - now_ist()).total_seconds()
            time.sleep(max(1, min(60, left)))
        try:
            n = run_watch()
            print(now_ist().strftime("%d-%m %I:%M %p IST"), "auto check done, alerts:", n, flush=True)
        except Exception as ex:
            print("watch error:", ex)


# ---------------------------------------------------------------- main
def main():
    global TOKEN
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    load_state()
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        t = target_from_text(sys.argv[2])
        budget = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BUDGET
        scope = sys.argv[4] if len(sys.argv) > 4 else "offer"
        txt = re.sub(r"<[^>]+>", "", match_text(t, budget, scope)[0])
        sys.stdout.buffer.write(html.unescape(txt).encode("utf-8") + b"\n")
        return
    TOKEN = (ENV.get("BOT_TOKEN") or "").strip()
    if not TOKEN or ":" not in TOKEN:
        print("Bot token nahi mila. .env file mein BOT_TOKEN= ke aage BotFather ka token paste karo.")
        sys.exit(1)
    me = tg("getMe")["result"]
    try:
        tg("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in COMMANDS]))
    except Exception as ex:
        print("setMyCommands failed:", ex)
    print(f"Bot chal raha hai: @{me['username']}  (band karne ke liye Ctrl+C)")
    threading.Thread(target=watcher_loop, daemon=True).start()
    offset = 0
    while True:
        try:
            ups = tg("getUpdates", offset=offset, timeout=50).get("result", [])
        except Exception as ex:
            if "409" in str(ex):
                print("poll error: ye bot kahin aur bhi chal raha hai (PC ya dusra server). "
                      "Ek hi jagah chalao, warna messages miss honge.", flush=True)
            else:
                print("poll error:", ex, flush=True)
            time.sleep(5)
            continue
        for up in ups:
            offset = up["update_id"] + 1
            cq = up.get("callback_query")
            msg = up.get("message") or {}
            text, chat = msg.get("text"), (msg.get("chat") or {}).get("id")
            if cq:
                chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            elif not text or not chat:
                continue

            uid = ((cq or msg).get("from") or {}).get("id")

            def work(chat=chat, text=text, cq=cq, uid=uid):
                try:
                    if not gate(chat, uid, text, cq):
                        return
                    if cq:
                        on_callback(cq)
                    else:
                        handle(chat, text)
                except Exception as ex:
                    print("handle error:", repr(ex))
                    if chat:
                        send(chat, "Kuch gadbad ho gayi, thodi der baad try karo.")
            threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    main()
