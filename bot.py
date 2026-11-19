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
# FM_ENV_FILE / FM_DATA_DIR let a separate test bot run next to the real one (see test-bot.bat)
DATA_DIR = os.path.join(HERE, os.environ.get("FM_DATA_DIR") or "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
ENV_FILE = os.path.join(HERE, os.environ.get("FM_ENV_FILE") or ".env")


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
    for k in ("BOT_TOKEN", "CHECK_TIMES", "DEFAULT_BUDGET", "ALERT_SCORE", "REQUIRED_CHANNELS",
              "ADMIN_IDS"):
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


RANK = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def num_emoji(i):
    return NUMS[i - 1] if 1 <= i <= len(NUMS) else f"{i}."


def specs_line(p):
    bits = [dims(p)]
    if p.get("width") is not None:
        bits.append(f"{fmt_num(p['width'])}mm wide")
    if p.get("height") is not None:
        bits.append(f"{fmt_num(p['height'])}mm high")
    if p.get("weight") is not None:
        bits.append(f"{fmt_num(p['weight'])}g")
    return "📐 " + " · ".join(bits)


def look_line(p):
    bits = [x for x in (p.get("colour"), p.get("shape"), p.get("material")) if x]
    return "🎨 " + (" · ".join(e(x) for x in bits) if bits else "-")


def price_html(p):
    price, mrp = p.get("price"), p.get("mrp")
    if not price:
        return "💰 Price: -"
    out = f"💰 <b>₹{price}</b>"
    if mrp and mrp > price:
        out += f" <s>₹{mrp}</s> · {round((mrp - price) * 100 / mrp)}% off"
    return out


def link_html(p, text="Lenskart pe dekho ↗"):
    return f'🔗 <a href="{e(p.get("url"))}">{text}</a>' if p.get("url") else ""


def describe(p):
    title = name_of(p) if p.get("id") else "Tumhare specs"
    lines = [f"<b>{e(title)}</b>", specs_line(p), look_line(p)]
    if p.get("id"):
        lines.append(f"{price_html(p)} · {stock_label(p.get('qty'))}")
    return "\n".join(lines)


def diff_line(t, c):
    out = []
    for key in ("lens", "bridge", "temple", "width", "height"):
        if t.get(key) is None or c.get(key) is None:
            continue
        d = c[key] - t[key]
        if d:
            out.append(f"{key} {'+' if d > 0 else '−'}{abs(d):g}mm")
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
    return "🟢 Bahut close" if s <= 8 else ("🟡 Close" if s <= 14 else "🟠 Thoda alag")


def stock_label(q):
    """Online stock count from Lenskart, with a buy-soon hint."""
    if q is None:
        return "Stock: pata nahi"
    q = int(q)
    if q <= 0:
        return "❌ Out of stock"
    if q <= 10:
        return f"🔥 Sirf {q} bache, jaldi lo"
    if q <= 50:
        return f"⚠️ Kam stock ({q})"
    return f"✅ Stock mein ({q})"


def refresh_live(items):
    """Re-fetch stock (and price when missing) for the frames we are about to show."""
    def one(c):
        try:
            p = product(c["id"])
            c["qty"] = p["qty"]
            c["price"] = c.get("price") or p["price"]
            c["mrp"] = c.get("mrp") or p.get("mrp")
        except Exception:
            c.setdefault("qty", None)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, items))


def scope_name(scope):
    return "poora Lenskart" if scope == "all" else "offer section"


def match_card(i, s, t, c):
    rating = f" · ⭐ {c['rating']}" if c.get("rating") else ""
    return "\n".join(x for x in (
        f"{RANK[i - 1] if i <= len(RANK) else i} <b>{e(name_of(c))}</b>",
        f"{label(s)}{rating}",
        price_html(c),
        specs_line(c),
        look_line(c),
        f"📦 {stock_label(c.get('qty'))}",
        f"↔️ <i>Farak: {e(diff_line(t, c))}</i>",
        link_html(c),
    ) if x)


def match_text(t, budget, scope):
    """Returns (message text, ranked matches)."""
    ranked, total = find_matches(t, budget, scope)
    head = f"🎯 <b>Tumhara frame</b>\n{describe(t)}"
    if t.get("width_guess"):
        head += "\n<i>(Frame width nahi diya tha, andaza lagaya hai)</i>"
    if not ranked:
        return (head + f"\n\n😕 <b>Koi match nahi mila</b>\n{scope_name(scope)} mein ₹{budget} tak "
                f"{total} frames dekhe. Budget badhao ya poore Lenskart mein dhoondo."), []
    parts = [head,
             f"{DIV}\n🔎 <b>Top {len(ranked)} matches</b> · {scope_name(scope)} · ₹{budget} tak\n"
             f"<i>{total} frames check kiye</i>"]
    parts += [match_card(i, s, t, c) for i, (s, c) in enumerate(ranked, 1)]
    return "\n\n".join(parts), ranked


DIV = "━━━━━━━━━━━━━━━"

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
JOIN_OK_SECONDS = 3600           # re-check a member at most once an hour (leave events lock instantly)
JOINED_CACHE = {}                # user_id -> time last confirmed as member
CHANNEL_TITLES = {}


def resolve_channels():
    """Learn each channel's numeric id and title, so leave events can be matched."""
    for ch in REQUIRED_CHANNELS:
        try:
            r = tg("getChat", chat_id=ch["id"])["result"]
            ch["num"] = str(r.get("id"))
            CHANNEL_TITLES[ch["id"]] = r.get("title")
        except Exception as ex:
            print(f"channel {ch['id']} check failed (bot ko admin banao):", ex, flush=True)


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
    if missing:
        JOINED_CACHE.pop(user_id, None)
    else:
        JOINED_CACHE[user_id] = time.time()
    return missing


def join_view(name, missing, left_from=None):
    """Join / re-join screen with progress. Returns (text, markup)."""
    total = len(REQUIRED_CHANNELS)
    done = total - len(missing)
    miss_ids = {ch["id"] for ch in missing}
    bar = "🟩" * done + "⬜" * len(missing)
    hi = f"Hey {e(name)}! " if name else ""
    if left_from:
        top = (f"🔒 <b>Bot lock ho gaya</b>\n\n"
               f"{hi}Tumne <b>{e(channel_title(left_from))}</b> chhod diya. "
               "Bot aur saare alerts tab tak band rahenge jab tak tum wapas join nahi karte.")
    else:
        top = (f"🔒 <b>Bot unlock karo</b>\n\n"
               f"{hi}Chashma Match bilkul free hai. Bas hamare channels join karo, "
               "wahan roz Lenskart aur tech ke best deals aate hain. 🎁")
    status = "\n".join(
        f"{'⬜' if ch['id'] in miss_ids else '✅'} {e(channel_title(ch))}" for ch in REQUIRED_CHANNELS)
    text = (f"{top}\n\n<b>Progress</b>  {bar}  {done}/{total}\n{status}\n\n"
            "👇 Neeche se join karo, phir <b>Maine join kar liya</b> dabao.")
    rows = [[{"text": f"➕ Join: {channel_title(ch)}", "url": ch["link"]}] for ch in missing]
    rows.append([{"text": "✅ Maine join kar liya, unlock karo", "callback_data": "joined"}])
    return text, {"inline_keyboard": rows}


def gate(chat, uid, text, cq):
    """True when the update may go to the normal handlers."""
    if not REQUIRED_CHANNELS or is_admin(uid):
        return True
    u = user(chat)
    if cq:
        msg_id = (cq.get("message") or {}).get("message_id")
        if (cq.get("data") or "") == "joined":
            missing = missing_channels(uid, fresh=True)
            if missing:
                names = ", ".join(channel_title(ch) for ch in missing)
                answer(cq, f"Abhi ye join nahi hua: {names}. Join karke dobara dabao.", alert=True)
                edit(chat, msg_id, *join_view(u.get("name"), missing))
                return False
            answer(cq, "Unlock ho gaya! 🎉")
            edit(chat, msg_id, f"🔓 <b>Unlock ho gaya!</b>\n\nShukriya {e(u.get('name') or '')} 🙏 "
                               "Ab bot ke saare features tumhare hain.")
            send(chat, welcome_text(u), kb_for(chat))
            return False
        missing = missing_channels(uid)
        if missing:
            answer(cq, "Pehle channels join karo 🔒")
            send(chat, *join_view(u.get("name"), missing))
            return False
        return True
    first = (text or "").strip().split(" ")[0].lower().split("@")[0]
    if first in ("/help", "/myid", "/discount") or (text or "").strip() in (B_HELP, B_DISCOUNT):
        return True              # contact buttons and id lookup always work
    missing = missing_channels(uid)
    if missing:
        send(chat, *join_view(u.get("name"), missing))
        return False
    return True


def channel_of(chat):
    cid = str(chat.get("id"))
    uname = ("@" + chat["username"]).lower() if chat.get("username") else None
    for ch in REQUIRED_CHANNELS:
        if ch.get("num") == cid or ch["id"] == cid or (uname and ch["id"].lower() == uname):
            return ch
    return None


def on_chat_member(upd):
    """A user joined or left one of the required channels."""
    ch = channel_of(upd.get("chat") or {})
    new = upd.get("new_chat_member") or {}
    uid = (new.get("user") or {}).get("id")
    if not ch or not uid or (new.get("user") or {}).get("is_bot"):
        return
    st = new.get("status")
    left = st in ("left", "kicked") or (st == "restricted" and not new.get("is_member"))
    if not left:
        return
    JOINED_CACHE.pop(uid, None)
    with LOCK:
        known = str(uid) in STATE["users"]
    if not known:
        return                   # never used the bot, nothing to lock
    u = user(uid)
    missing = missing_channels(uid, fresh=True) or [ch]
    print(f"user {uid} left {ch['id']}, bot locked for them", flush=True)
    send(uid, *join_view(u.get("name"), missing, left_from=ch))


# ---------------------------------------------------------------- texts
def support_text():
    return ("📩 <b>Lenskart offers aur help</b>\n\n"
            "Lenskart ke latest offers, best deal ya bot use karne mein koi dikkat ho, "
            "to seedha humse baat karo.\n"
            "Neeche button dabao.")


def support_markup():
    return {"inline_keyboard": [[{"text": "💬 Offers aur help ke liye message karo",
                                  "url": SUPPORT_URL}]]}


def discount_url(frame=None):
    msg = "Hi, mujhe Lenskart frame discount pe kharidna hai."
    if frame and frame.get("url"):
        msg = f"Hi, mujhe ye Lenskart frame discount pe kharidna hai: {name_of(frame)} {frame['url']}"
    return f"https://t.me/{SUPPORT_USERNAME}?text=" + urllib.parse.quote(msg)


def discount_view(u):
    last = u.get("last") if (u.get("last") or {}).get("url") else None
    text = ("🛍 <b>Frame discount pe kharido</b>\n\n"
            "Pasand ka frame aur bhi kam daam mein chahiye? Hum tumhe Lenskart pe "
            "<b>extra discount</b> pe frame dilwa sakte hain.\n\n"
            "Neeche button dabao, seedha humse baat karo.")
    rows = []
    if last:
        text += f"\n\n<i>Tumhara last frame: {e(name_of(last))}</i>"
        rows.append([{"text": f"🛍 {name_of(last)} discount pe chahiye", "url": discount_url(last)}])
    rows.append([{"text": "💬 Discount ke liye message karo", "url": discount_url()}])
    return text, {"inline_keyboard": rows}


def welcome_text(u):
    hi = f"👋 <b>Namaste {e(u.get('name'))}!</b>" if u.get("name") else "👋 <b>Namaste!</b>"
    return (f"{hi}\n\n"
            "👓 <b>Chashma Match</b> mein swagat hai.\n"
            "Pasand ka frame mehenga hai? Main Lenskart offer mein uske jaise saste frames dhoondta hoon.\n\n"
            "<b>Kaise kaam karta hai</b>\n"
            "1️⃣ Lenskart frame ka link bhejo\n"
            "2️⃣ Top 5 closest frames milenge, farak ke saath\n"
            "3️⃣ ⭐ dabao, stock aur price ke alerts pao\n\n"
            f"⚙️ Abhi: <b>₹{u['budget']} tak</b> · <b>{scope_name(u['scope'])}</b>\n"
            f"⏰ Auto check: roz {times_text()}\n\n"
            "👇 Shuru karo: frame ka link yahan paste karo")


def guide_text():
    return (
        "📖 <b>Guide</b>\n\n"
        "<b>Frame kaise dhoondein</b>\n"
        "• Lenskart app ya website se frame ka link copy karke bhejo\n"
        "• Ya specs likho: <code>50-20-145 136mm black square acetate</code>\n\n"
        "<b>Buttons</b>\n"
        f"{B_FIND} · link bhejne ka tareeka\n"
        f"{B_FAVS} · track ho rahe frames, live stock ke saath\n"
        f"{B_BUDGET} · max price\n"
        f"{B_SCOPE} · offer section ya poora Lenskart\n"
        f"{B_NEW} · pichhle 7 din ke naye offer frames\n"
        f"{B_CHECK} · stock aur price abhi check karo\n"
        f"{B_SETTINGS} · settings aur alerts on/off\n"
        f"{B_DISCOUNT} · frame extra discount pe lene ke liye humse baat karo\n\n"
        "<b>Stock</b>\n"
        "✅ 50+ · araam se lo\n"
        "⚠️ 11-50 · kam bacha hai\n"
        "🔥 10 ya kam · jaldi lo\n"
        "❌ Out of stock\n\n"
        f"<b>Auto alerts</b> (roz {times_text()})\n"
        "• Tracked frame stock mein wapas aaye\n"
        "• Price gire ya frame offer mein aa jaye\n"
        "• Offer mein tumhare frame jaisa naya frame aaye")


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
B_GUIDE = "📖 Guide"
B_HELP = "❓ Help"
B_DISCOUNT = "🛍 Discount pe kharido"
B_ADMIN = "🛠 Admin panel"

BUTTON_CMD = {B_FIND: "/find", B_FAVS: "/favs", B_BUDGET: "/budget", B_SCOPE: "/scope",
              B_NEW: "/new", B_CHECK: "/check", B_ALERTS: "/alerts",
              B_SETTINGS: "/settings", B_GUIDE: "/guide", B_HELP: "/help", B_DISCOUNT: "/discount", B_ADMIN: "/admin"}

MAIN_KB = {
    "keyboard": [[B_FIND, B_FAVS], [B_BUDGET, B_SCOPE], [B_NEW, B_CHECK],
                 [B_SETTINGS, B_GUIDE], [B_DISCOUNT, B_HELP]],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Lenskart frame ka link paste karo...",
}

COMMANDS = [
    ("start", "Bot shuru karo"),
    ("favs", "Tracked frames"),
    ("budget", "Max price set karo"),
    ("scope", "Offer section ya poora Lenskart"),
    ("new", "Naye offer frames"),
    ("check", "Abhi stock aur price check karo"),
    ("settings", "Settings aur alerts"),
    ("guide", "Bot kaise use karein"),
    ("discount", "Frame discount pe kharido"),
    ("help", "Offers aur help ke liye contact"),
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
    """Send (splitting long text). Returns the sent message ids."""
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    chunks.append(cur)
    ids = []
    for i, c in enumerate(chunks):
        params = dict(chat_id=chat_id, text=c.strip() or "-", parse_mode="HTML",
                      disable_web_page_preview="true")
        if markup and i == len(chunks) - 1:
            params["reply_markup"] = json.dumps(markup)
        try:
            r = tg("sendMessage", **params)
            ids.append(((r or {}).get("result") or {}).get("message_id"))
        except urllib.error.HTTPError as ex:
            if ex.code == 403:           # user blocked the bot
                with LOCK:
                    if str(chat_id) in STATE["users"]:
                        STATE["users"][str(chat_id)]["blocked"] = True
            print("send failed:", ex)
        except Exception as ex:
            print("send failed:", ex)
    return ids


def edit(chat_id, msg_id, text, markup=None):
    """Edit a message in place; falls back to a new message."""
    if not msg_id:
        return send(chat_id, text, markup)
    params = dict(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML",
                  disable_web_page_preview="true")
    if markup:
        params["reply_markup"] = json.dumps(markup)
    try:
        tg("editMessageText", **params)
    except Exception as ex:
        if "not modified" not in str(getattr(ex, "read", lambda: b"")() or ex):
            send(chat_id, text, markup)


def delete(chat_id, msg_id):
    if msg_id:
        try:
            tg("deleteMessage", chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


def answer(cq, text=None, alert=False):
    try:
        params = {"callback_query_id": cq["id"]}
        if text:
            params["text"] = text[:190]
        if alert:
            params["show_alert"] = "true"
        tg("answerCallbackQuery", **params)
    except Exception:
        pass


LINK_RE = re.compile(r"https?://(?:www\.)?lenskart\.com/\S+")


def target_from_text(text):
    m = LINK_RE.search(text)
    if m:
        return product(id_from_url(m.group()))
    return parse_specs(text)


def add_fav(u, t):
    if not t.get("id"):
        return "⚠️ Sirf Lenskart link waale frames track ho sakte hain."
    if any(f["id"] == t["id"] for f in u["favs"]):
        return f"⭐ <b>{e(name_of(t))}</b> pehle se track ho raha hai."
    if len(u["favs"]) >= MAX_FAVS:
        return f"⚠️ Max {MAX_FAVS} frames track ho sakte hain. Pehle koi hatao ({B_FAVS})."
    f = dict(t)
    f["in_stock"] = t.get("qty", 0) > 0
    f["in_offer"] = False
    u["favs"].append(f)
    save_state()
    stock = stock_label(t.get("qty"))
    if not f["in_stock"]:
        stock += ", wapas aate hi bataunga"
    return (f"⭐ <b>Track ho gaya!</b>\n\n<b>{e(name_of(t))}</b>\n{price_html(t)}\n📦 {stock}\n\n"
            "🔔 Stock, price drop aur iske jaise naye offer frames ka alert milega.")


def favs_message(u):
    if not u["favs"]:
        return ("⭐ <b>Tracked frames</b>\n\nAbhi koi frame track nahi ho raha.\n"
                "Frame ka link bhejo, phir result ke neeche ⭐ dabao."), None
    refresh = [dict(id=f["id"]) for f in u["favs"]]
    refresh_live(refresh)
    live = {r["id"]: r for r in refresh}
    lines, rows = [f"⭐ <b>Tracked frames</b> ({len(u['favs'])}/{MAX_FAVS})"], []
    for i, f in enumerate(u["favs"], 1):
        r = live.get(f["id"]) or {}
        if r.get("qty") is not None:
            f.update(qty=r["qty"], in_stock=r["qty"] > 0)
        if r.get("price"):
            f["price"] = r["price"]
        lines.append(f"{num_emoji(i)} <b>{e(name_of(f))}</b>\n"
                     f"{price_html(f)} · {stock_label(f.get('qty'))}\n"
                     f"📐 {dims(f)} · 🎨 {e(f.get('colour'))} {e(f.get('shape'))}\n"
                     f"{link_html(f)}")
        rows.append([{"text": f"🔎 {i} jaise", "callback_data": f"sim:{f['id']}"},
                     {"text": f"🛒 {i} kholo", "url": f["url"]},
                     {"text": f"❌ {i} hatao", "callback_data": f"unfav:{f['id']}"}])
    save_state()
    return "\n\n".join(lines), {"inline_keyboard": rows}


def new_message(u):
    cutoff = time.time() - 7 * 86400
    with LOCK:
        items = [n for n in STATE["recent_new"] if n["ts"] >= cutoff]
    items = [n for n in items if (n.get("price") or 0) <= u["budget"]][-10:][::-1]
    if not items:
        return ("🆕 <b>Naye offer frames</b>\n\n"
                f"Pichhle 7 din mein ₹{u['budget']} tak koi naya offer frame nahi aaya.\n"
                f"<i>Roz {times_text()} check hota hai.</i>"), None
    items = [dict(n) for n in items]
    refresh_live(items)
    lines = [f"🆕 <b>Naye offer frames</b> · ₹{u['budget']} tak · pichhle 7 din"]
    for i, n in enumerate(items, 1):
        lines.append(f"{num_emoji(i)} <b>{e(name_of(n))}</b>\n"
                     f"{price_html(n)} · {stock_label(n.get('qty'))}\n"
                     f"📐 {dims(n)} · {fmt_num(n.get('width'), 'mm')} · "
                     f"🎨 {e(n.get('colour'))} {e(n.get('shape'))}\n{link_html(n)}")
    lines.append("👇 Track karne ke liye ⭐ number dabao")
    btns = [(f"⭐ {i}", f"fav:{n['id']}") for i, n in enumerate(items, 1)]
    return "\n\n".join(lines), inline([btns[k:k + 5] for k in range(0, len(btns), 5)])


def settings_view(u):
    text = ("⚙️ <b>Settings</b>\n\n"
            f"💰 Budget: <b>₹{u['budget']} tak</b>\n"
            f"🌐 Kahan dhoondu: <b>{scope_name(u['scope'])}</b>\n"
            f"🔔 Auto alerts: <b>{'ON' if u['alerts'] else 'OFF'}</b>\n"
            f"⭐ Tracked: <b>{len(u['favs'])}/{MAX_FAVS}</b>\n"
            f"⏰ Auto check: roz {times_text()} (IST)")
    return text, inline([[("💰 Budget badlo", "menu:budget"), ("🌐 Jagah badlo", "menu:scope")],
                         [("🔕 Alerts OFF karo" if u["alerts"] else "🔔 Alerts ON karo",
                           "alerts:" + ("off" if u["alerts"] else "on"))]])


def budget_view(u):
    opts = [500, 800, 1000, 1200, 1500, 2000, 3000, 5000]
    btns = [((f"✅ ₹{o}" if o == u["budget"] else f"₹{o}"), f"budget:{o}") for o in opts]
    return (f"💰 <b>Budget</b>\n\nAbhi: <b>₹{u['budget']} tak</b>\n"
            "Naya chuno, ya likho jaise <code>/budget 900</code>",
            inline([btns[:4], btns[4:]]))


def scope_view(u):
    def mark(s, t):
        return ("✅ " if u["scope"] == s else "") + t
    return (f"🌐 <b>Kahan dhoondu?</b>\n\nAbhi: <b>{scope_name(u['scope'])}</b>\n\n"
            "🏷 <b>Offer section</b> · sirf sale waale frames, sabse saste\n"
            "🌐 <b>Poora Lenskart</b> · zyada options, close match ke chances zyada",
            inline([[(mark("offer", "🏷 Offer section"), "scope:offer"),
                     (mark("all", "🌐 Poora Lenskart"), "scope:all")]]))


def settings_text(u):
    return settings_view(u)[0]


def do_match(chat_id, u, t, scope=None):
    scope = scope or u["scope"]
    try:
        tg("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass
    wait = send(chat_id, "🔍 <b>Dhoond raha hoon...</b>\n"
                         f"<i>{scope_name(scope)} ke frames check ho rahe hain, 10-20 second lagenge</i>")
    u["last"] = t
    save_state()
    try:
        text, ranked = match_text(t, u["budget"], scope)
    finally:
        for mid in wait:
            delete(chat_id, mid)
    rows = []
    if ranked:
        rows.append([{"text": f"⭐ {i}", "callback_data": f"fav:{c['id']}"}
                     for i, (_, c) in enumerate(ranked, 1)])
        rows.append([{"text": f"🛒 {i}", "url": c["url"]} for i, (_, c) in enumerate(ranked, 1)])
    if t.get("id"):
        rows.append([{"text": "⭐ Mera frame track karo", "callback_data": f"fav:{t['id']}"}])
        rows.append([{"text": "🛍 Ye frame discount pe chahiye", "url": discount_url(t)}])
        if scope == "offer":
            rows.append([{"text": "🌐 Poore Lenskart mein dhoondo", "callback_data": f"simall:{t['id']}"}])
    if ranked:
        text += f"\n\n{DIV}\n👇 <b>⭐ number</b> = track karo · <b>🛒 number</b> = Lenskart pe kholo"
    send(chat_id, text, {"inline_keyboard": rows} if rows else None)


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
        return send(chat_id, welcome_text(u), kb_for(chat_id))
    if cmd == "/myid":
        return send(chat_id, f"🆔 Tumhara Telegram ID: <code>{chat_id}</code>")
    if cmd == "/admin":
        if is_admin(chat_id):
            return send(chat_id, *admin_view())
        return send(chat_id, "🤔 Ye command samajh nahi aaya. Neeche ke buttons use karo.", kb_for(chat_id))
    if cmd == "/help":
        return send(chat_id, support_text(), support_markup())
    if cmd == "/discount":
        return send(chat_id, *discount_view(u))
    if cmd == "/guide":
        return send(chat_id, guide_text(), kb_for(chat_id))
    if cmd == "/find":
        return send(chat_id, "🔎 <b>Frame dhoondo</b>\n\n"
                             "Lenskart app ya website se frame ka <b>link copy karke</b> yahan paste karo.\n\n"
                             "Ya specs likho:\n<code>50-20-145 136mm black square acetate</code>\n\n"
                             f"⚙️ Abhi: ₹{u['budget']} tak · {scope_name(u['scope'])}")
    if cmd == "/budget":
        n = num(arg)
        if n and n >= 100:
            u["budget"] = int(n)
            save_state()
            return send(chat_id, f"✅ Budget ab <b>₹{u['budget']} tak</b> hai.")
        return send(chat_id, *budget_view(u))
    if cmd == "/scope":
        if arg.lower() in ("offer", "all"):
            u["scope"] = arg.lower()
            save_state()
            return send(chat_id, f"✅ Ab <b>{scope_name(u['scope'])}</b> mein dhoondunga.")
        return send(chat_id, *scope_view(u))
    if cmd == "/alerts":
        if arg.lower() in ("on", "off"):
            u["alerts"] = arg.lower() == "on"
            save_state()
        return send(chat_id, *settings_view(u))
    if cmd == "/settings":
        return send(chat_id, *settings_view(u))
    if cmd == "/favs":
        return send(chat_id, *favs_message(u))
    if cmd == "/new":
        return send(chat_id, *new_message(u))
    if cmd == "/unfav":
        n = num(arg)
        if not n or not (1 <= int(n) <= len(u["favs"])):
            return send(chat_id, "Number batao, jaise <code>/unfav 1</code>")
        f = u["favs"].pop(int(n) - 1)
        save_state()
        return send(chat_id, f"🗑 Hata diya: <b>{e(name_of(f))}</b>")
    if cmd == "/fav":
        t = target_from_text(arg) if arg else u.get("last")
        if not t:
            return send(chat_id, "Pehle frame ka link bhejo, phir ⭐ button dabao.")
        return send(chat_id, add_fav(u, t))
    if cmd == "/check":
        wait = send(chat_id, "🔄 <b>Check kar raha hoon...</b>")
        n = run_watch(only_chat=str(chat_id))
        for mid in wait:
            delete(chat_id, mid)
        if not n:
            send(chat_id, "✅ Sab same hai, abhi koi nayi update nahi.\n"
                          f"<i>Agla auto check: {next_check().strftime('%I:%M %p').lstrip('0')} IST</i>")
        return
    if cmd.startswith("/"):
        return send(chat_id, "🤔 Ye command samajh nahi aaya. Neeche ke buttons use karo.", kb_for(chat_id))

    try:
        t = target_from_text(text)
    except Exception:
        return send(chat_id, "😕 <b>Is link se frame nahi mila</b>\n\n"
                             "Lenskart ke <b>product page</b> ka link bhejo (jisme ek frame dikhta ho).")
    if not t:
        return send(chat_id, "🤔 <b>Ye samajh nahi aaya</b>\n\n"
                             "Lenskart frame ka <b>link</b> bhejo, ya specs aise likho:\n"
                             "<code>50-20-145 136mm black square</code>", kb_for(chat_id))
    do_match(chat_id, u, t)


def on_callback(cq):
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    msg_id = (cq.get("message") or {}).get("message_id")
    data = cq.get("data") or ""
    if not chat_id:
        return answer(cq)
    u = user(chat_id)
    kind, _, val = data.partition(":")
    if kind == "adm":
        return on_admin_callback(cq, chat_id, (cq.get("from") or {}).get("id"), val, msg_id)
    if kind == "fav":
        answer(cq, "⭐ Track ho raha hai")
        return send(chat_id, add_fav(u, product(val)))
    if kind == "unfav":
        for f in list(u["favs"]):
            if f["id"] == val:
                u["favs"].remove(f)
                save_state()
                answer(cq, "Hata diya")
                return edit(chat_id, msg_id, *favs_message(u))
        return answer(cq, "Ye pehle hi hat chuka hai")
    if kind == "sim":
        answer(cq)
        return do_match(chat_id, u, product(val))
    if kind == "simall":
        answer(cq)
        return do_match(chat_id, u, product(val), scope="all")
    if kind == "menu" and val == "budget":
        answer(cq)
        return edit(chat_id, msg_id, *budget_view(u))
    if kind == "menu" and val == "scope":
        answer(cq)
        return edit(chat_id, msg_id, *scope_view(u))
    if kind == "budget" and num(val):
        u["budget"] = int(num(val))
        save_state()
        answer(cq, f"Budget ₹{u['budget']}")
        return edit(chat_id, msg_id, *settings_view(u))
    if kind == "scope" and val in ("offer", "all"):
        u["scope"] = val
        save_state()
        answer(cq, scope_name(val))
        return edit(chat_id, msg_id, *settings_view(u))
    if kind == "alerts" and val in ("on", "off"):
        u["alerts"] = val == "on"
        save_state()
        answer(cq, "Alerts " + val.upper())
        return edit(chat_id, msg_id, *settings_view(u))
    answer(cq)


# ---------------------------------------------------------------- admin panel
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", ENV.get("ADMIN_IDS") or "")}
ADMIN_PENDING = {}               # admin user id -> {"stage": "await"|"confirm", "src": msg id}
BROADCAST_LOCK = threading.Lock()


def is_admin(uid):
    try:
        return int(uid) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


def kb_for(chat_id):
    if not is_admin(chat_id):
        return MAIN_KB
    kb = dict(MAIN_KB)
    kb["keyboard"] = MAIN_KB["keyboard"] + [[B_ADMIN]]
    return kb


def admin_view():
    with LOCK:
        users = list(STATE["users"].values())
        last = STATE.get("last_broadcast")
    blocked = sum(1 for u in users if u.get("blocked"))
    lines = ["🛠 <b>Admin panel</b>\n",
             f"👥 Users: <b>{len(users)}</b> (active {len(users) - blocked}, bot block kiya {blocked})",
             f"⭐ Tracked frames: <b>{sum(len(u.get('favs') or []) for u in users)}</b>",
             f"🔔 Alerts ON: <b>{sum(1 for u in users if u.get('alerts', True))}</b>"]
    if REQUIRED_CHANNELS:
        lines.append("📢 Join zaroori: " + ", ".join(e(channel_title(c)) for c in REQUIRED_CHANNELS))
    if last:
        when = datetime.datetime.fromtimestamp(last["ts"], IST).strftime("%d %b, %I:%M %p")
        lines.append(f"📨 Last broadcast: {when} · {last['sent']}/{last['total']} pahuncha")
    return "\n".join(lines), inline([[("📢 Sabko message bhejo", "adm:bc")],
                                     [("🔄 Refresh", "adm:refresh")]])


def admin_capture(chat, uid, msg):
    """While an admin is composing a broadcast, their next message is the content."""
    p = ADMIN_PENDING.get(uid)
    if not p or p.get("stage") != "await" or not is_admin(uid) or not msg.get("message_id"):
        return False
    text = (msg.get("text") or "").strip()
    if text in BUTTON_CMD or text.startswith("/"):
        ADMIN_PENDING.pop(uid, None)       # admin moved on: cancel the broadcast
        return False
    with LOCK:
        total = sum(1 for u in STATE["users"].values() if not u.get("blocked"))
    ADMIN_PENDING[uid] = {"stage": "confirm", "src": msg["message_id"]}
    send(chat, f"👀 <b>Preview upar hai.</b>\n\nYe message <b>{total} users</b> ko bot ki taraf se jayega. Bhejun?",
         inline([[("✅ Haan, sabko bhejo", "adm:send"), ("❌ Cancel", "adm:cancel")]]))
    return True


def broadcast(admin_chat, src_id):
    with LOCK:
        targets = [k for k, u in STATE["users"].items() if not u.get("blocked")]
    total, ok, failed, blocked = len(targets), 0, 0, 0
    prog = (send(admin_chat, f"📤 Bhej raha hoon... 0/{total}") or [None])[0]
    for i, chat in enumerate(targets, 1):
        for attempt in range(2):
            try:
                tg("copyMessage", chat_id=chat, from_chat_id=admin_chat, message_id=src_id)
                ok += 1
                break
            except urllib.error.HTTPError as ex:
                body = ex.read().decode(errors="ignore")
                if ex.code == 429 and attempt == 0:
                    wait = (json.loads(body or "{}").get("parameters") or {}).get("retry_after", 5)
                    time.sleep(int(wait) + 1)
                    continue
                failed += 1
                if ex.code == 403:
                    blocked += 1
                    with LOCK:
                        STATE["users"].get(chat, {})["blocked"] = True
                break
            except Exception:
                failed += 1
                break
        time.sleep(0.05)                    # stay under Telegram's ~30 msgs/sec limit
        if prog and i % 25 == 0:
            edit(admin_chat, prog, f"📤 Bhej raha hoon... {i}/{total}")
    with LOCK:
        STATE["last_broadcast"] = {"ts": time.time(), "sent": ok, "total": total}
    save_state()
    done = (f"✅ <b>Broadcast ho gaya</b>\n\n📨 Pahuncha: <b>{ok}</b>/{total}\n"
            f"❌ Fail: {failed}" + (f" ({blocked} ne bot block kiya hai)" if blocked else ""))
    edit(admin_chat, prog, done) if prog else send(admin_chat, done)


def on_admin_callback(cq, chat, uid, val, msg_id):
    if not is_admin(uid):
        return answer(cq, "Ye sirf admin ke liye hai")
    if val == "refresh":
        answer(cq, "Updated")
        return edit(chat, msg_id, *admin_view())
    if val == "bc":
        ADMIN_PENDING[uid] = {"stage": "await"}
        answer(cq)
        return edit(chat, msg_id,
                    "📢 <b>Sabko message bhejo</b>\n\n"
                    "Ab jo message bhejoge (text, photo, video, kuch bhi) wo <b>saare users</b> ko "
                    "bot ki taraf se jayega.\n\nBhejne se pehle preview aur confirm aayega.",
                    inline([[("❌ Cancel", "adm:cancel")]]))
    if val == "cancel":
        ADMIN_PENDING.pop(uid, None)
        answer(cq, "Cancel")
        return edit(chat, msg_id, "❌ Broadcast cancel kar diya.")
    if val == "send":
        p = ADMIN_PENDING.pop(uid, None)
        if not p or p.get("stage") != "confirm":
            return answer(cq, "Ye broadcast pehle hi ho chuka ya cancel hua", alert=True)
        if not BROADCAST_LOCK.acquire(blocking=False):
            return answer(cq, "Ek broadcast already chal raha hai", alert=True)
        answer(cq, "Bhejna shuru")
        edit(chat, msg_id, "📤 <b>Broadcast shuru ho gaya</b>")
        try:
            broadcast(chat, p["src"])
        finally:
            BROADCAST_LOCK.release()
        return
    answer(cq)


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
        if REQUIRED_CHANNELS and only_chat is None and u.get("favs"):
            try:
                missing = missing_channels(int(chat_id))
            except ValueError:
                missing = []
            if missing:          # left a channel: no alerts, one reminder per day
                today = now_ist().date().isoformat()
                if u.get("lock_notice") != today:
                    u["lock_notice"] = today
                    send(chat_id, *join_view(u.get("name"), missing))
                continue
        msgs = []
        for f in u.get("favs", []):
            try:
                p = product(f["id"])
            except Exception:
                continue
            name = f"<b>{e(name_of(f))}</b>"
            buy = {"inline_keyboard": [[{"text": "🛒 Lenskart pe kholo", "url": f["url"]}]]}
            now_stock = p["qty"] > 0
            if now_stock and not f.get("in_stock", True):
                msgs.append((f"🟢 <b>Stock mein wapas aa gaya!</b>\n\n{name}\n{price_html(p)}\n"
                             f"📦 {stock_label(p['qty'])}", buy))
            elif not now_stock and f.get("in_stock", True):
                msgs.append((f"🔴 <b>Out of stock ho gaya</b>\n\n{name}\nWapas aate hi bataunga.", None))
            if p["price"] and f.get("price") and p["price"] < f["price"]:
                msgs.append((f"💰 <b>Price gir gaya!</b>\n\n{name}\n"
                             f"<s>₹{f['price']}</s> → <b>₹{p['price']}</b>\n📦 {stock_label(p['qty'])}", buy))
            now_offer = f["id"] in offer_ids
            if now_offer and not f.get("in_offer"):
                msgs.append((f"🏷 <b>Tumhara frame ab offer mein hai!</b>\n\n{name}\n"
                             f"💰 <b>₹{offer_by_id[f['id']]['price']}</b>\n📦 {stock_label(p['qty'])}", buy))
            f.update(in_stock=now_stock, price=p["price"] or f.get("price"), in_offer=now_offer)

            for c in new_items:
                if c["id"] == f["id"] or (c["price"] or 0) > u.get("budget", DEFAULT_BUDGET):
                    continue
                s = score(f, c)
                if s is not None and s <= ALERT_SCORE:
                    msgs.append((
                        f"🆕 <b>Naya offer frame, tumhare {e(name_of(f))} jaisa!</b>\n\n"
                        f"<b>{e(name_of(c))}</b>\n{label(s)}\n{price_html(c)}\n"
                        f"{specs_line(c)}\n{look_line(c)}\n📦 {stock_label(c.get('qty'))}\n"
                        f"↔️ <i>Farak: {e(diff_line(f, c))}</i>",
                        {"inline_keyboard": [[{"text": "⭐ Track karo", "callback_data": f"fav:{c['id']}"},
                                              {"text": "🛒 Kholo", "url": c["url"]}]]}))
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
    if REQUIRED_CHANNELS:
        resolve_channels()
        print("Join zaroori:", ", ".join(f"{channel_title(c)} ({c['id']})" for c in REQUIRED_CHANNELS),
              flush=True)
    threading.Thread(target=watcher_loop, daemon=True).start()
    offset = 0
    allowed = json.dumps(["message", "callback_query", "chat_member"])
    while True:
        try:
            ups = tg("getUpdates", offset=offset, timeout=50,
                     allowed_updates=allowed).get("result", [])
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
            if up.get("chat_member"):
                cm = up["chat_member"]

                def member_work(cm=cm):
                    try:
                        on_chat_member(cm)
                    except Exception as ex:
                        print("chat_member error:", repr(ex))
                threading.Thread(target=member_work, daemon=True).start()
                continue
            cq = up.get("callback_query")
            msg = up.get("message") or {}
            text, chat = msg.get("text"), (msg.get("chat") or {}).get("id")
            frm = (cq or msg).get("from") or {}
            if cq:
                chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            elif not chat or not (text or ADMIN_PENDING.get(frm.get("id"))):
                continue
            if (((cq or {}).get("message") or msg).get("chat") or {}).get("type") not in (None, "private"):
                continue         # only private chats with the bot

            uid = frm.get("id")

            def work(chat=chat, text=text, cq=cq, uid=uid, first=frm.get("first_name"), msg=msg):
                try:
                    u = user(chat)
                    changed = u.pop("blocked", None) is not None   # they are talking to us again
                    if first and u.get("name") != first:
                        u["name"] = first
                        changed = True
                    if changed:
                        save_state()
                    if not cq and admin_capture(chat, uid, msg):
                        return
                    if not text and not cq:
                        return
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
