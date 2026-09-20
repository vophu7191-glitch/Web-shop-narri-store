"""
╔══════════════════════════════════════════════════════════════╗
║   WEB BÁN CODE CLOUD (RedFinger / UGPhone / VIPPlayer /       ║
║   GenPlay / VMOS ...) — Flask + MongoDB                       ║
║                                                                ║
║   Biến môi trường cần cấu hình khi deploy (Render):            ║
║     MONGO_URI       -> chuỗi kết nối MongoDB Atlas             ║
║     SECRET_KEY       -> chuỗi bí mật bất kỳ cho session Flask  ║
║     PORT              -> Render tự cấp                         ║
╚══════════════════════════════════════════════════════════════╝
"""
import os, re, csv, io, uuid, secrets, hashlib, json, base64, time, threading, queue
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response, abort, stream_with_context
)
from pymongo import MongoClient, ReturnDocument
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# 2FA (TOTP) + QR code — optional but recommended
try:
    import pyotp
    _HAS_PYOTP = True
except ImportError:
    _HAS_PYOTP = False

try:
    import qrcode
    _HAS_QRCODE = True
except ImportError:
    _HAS_QRCODE = False

# ══════════════════════════════════════════════════════════════
#  CẤU HÌNH & KẾT NỐI DATABASE
# ══════════════════════════════════════════════════════════════
MONGO_URI  = os.environ.get("MONGO_URI", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
# Session cookie an toàn hơn 1 chút: HttpOnly + SameSite=Lax (Secure sẽ tự bật
# ở proxy TLS như Render, không ép Secure ở đây để dev localhost vẫn dùng được).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)

if not MONGO_URI:
    raise RuntimeError("Chưa cấu hình MONGO_URI trong biến môi trường!")

# ══════════════════════════════════════════════════════════════
#  UPLOAD ẢNH (Mức 2) — lưu vào static/uploads
#  CẢNH BÁO: filesystem của Render free tier bị XOÁ mỗi lần deploy.
#  Muốn giữ ảnh vĩnh viễn -> đặt env UPLOAD_FOLDER trỏ tới disk persistent
#  (Render Disk, S3 mounted, ...) hoặc dùng URL Cloudinary/Imgur ở field image_url.
# ══════════════════════════════════════════════════════════════
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "").strip() or os.path.join(
    app.root_path, "static", "uploads"
)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_UPLOAD_BYTES   = 2 * 1024 * 1024   # 2 MB / file
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # cứng ở tầng Flask: 5MB per request

def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""

def save_uploaded_image(file_storage, subdir: str = "products") -> str:
    """
    Nhận `werkzeug.FileStorage`, validate + lưu vào `static/uploads/<subdir>/`,
    trả về URL nội bộ (dạng /static/uploads/...) để nhét vào DB.
    - Đổi tên thành hash(sha256 8 ký tự đầu) + timestamp để tránh trùng và đoán URL.
    - Chỉ chấp nhận ảnh (jpg/png/webp/gif) ≤ 2MB.
    - Trả về "" (chuỗi rỗng) nếu file không hợp lệ / rỗng.
    """
    if not file_storage or not getattr(file_storage, "filename", ""):
        return ""
    raw_name = secure_filename(file_storage.filename or "")
    ext = _ext(raw_name)
    if ext not in ALLOWED_IMAGE_EXTS:
        raise ValueError(f"Định dạng ảnh không hỗ trợ ({ext or 'không rõ'}). Chỉ nhận: {', '.join(sorted(ALLOWED_IMAGE_EXTS))}.")
    data = file_storage.read()
    if not data:
        return ""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Ảnh quá lớn ({len(data)//1024} KB). Giới hạn 2 MB / ảnh.")
    # Kiểm tra magic number nhẹ (chống fake .jpg thực chất là exe)
    sig = data[:12]
    ok = (
        sig.startswith(b"\xff\xd8\xff") or          # JPEG
        sig.startswith(b"\x89PNG\r\n\x1a\n") or     # PNG
        sig.startswith(b"GIF87a") or sig.startswith(b"GIF89a") or  # GIF
        (sig.startswith(b"RIFF") and sig[8:12] == b"WEBP")         # WEBP
    )
    if not ok:
        raise ValueError("File không phải ảnh hợp lệ.")
    h = hashlib.sha256(data).hexdigest()[:12]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    fname = f"{stamp}-{h}.{ext if ext != 'jpeg' else 'jpg'}"
    subdir_path = os.path.join(UPLOAD_FOLDER, subdir)
    os.makedirs(subdir_path, exist_ok=True)
    full = os.path.join(subdir_path, fname)
    with open(full, "wb") as f:
        f.write(data)
    # Trả về đường dẫn tương đối theo url_for('static', ...)
    return url_for("static", filename=f"uploads/{subdir}/{fname}")


def resolve_image_field(form_url_key: str = "image_url",
                        file_field_key: str = "image_file",
                        subdir: str = "products",
                        old_value: str = "") -> str:
    """
    Ưu tiên FILE upload > URL ngoài > giá trị cũ. Dùng ở nhiều form (product, brand, slide).
    """
    file_up = request.files.get(file_field_key)
    if file_up and file_up.filename:
        try:
            url = save_uploaded_image(file_up, subdir=subdir)
            if url:
                return url
        except ValueError as e:
            flash(f"⚠️ Bỏ qua ảnh: {e}", "error")
    url_in = (request.form.get(form_url_key, "") or "").strip()
    if url_in:
        return url_in
    return old_value

_mc = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
_mc.server_info()  # ném lỗi ngay nếu không kết nối được, dễ debug lúc deploy
db = _mc["cloudshop"]

col_users     = db["users"]
col_products  = db["products"]
col_stock     = db["stock_codes"]
col_orders    = db["orders"]
col_config    = db["config"]
col_recharges = db["recharges"]
col_activity  = db["activity_log"]
col_posts     = db["posts"]
col_brands    = db["brands"]  # NEW — B: brand động thay cho hardcode
col_coupons   = db["coupons"]      # v5 A: mã giảm giá
col_reviews   = db["reviews"]      # v5 F: đánh giá sản phẩm

# Index cơ bản (an toàn khi gọi lại nhiều lần, chỉ tạo nếu chưa có)
col_users.create_index("username", unique=True)
col_stock.create_index([("product_id", 1), ("sold", 1)])
col_orders.create_index([("user_id", 1), ("created_at", -1)])
col_orders.create_index([("status", 1), ("created_at", -1)])
col_recharges.create_index([("created_at", -1)])
col_recharges.create_index([("user_id", 1), ("created_at", -1)])
col_activity.create_index([("created_at", -1)])
col_activity.create_index([("user_id", 1), ("created_at", -1)])
col_posts.create_index([("created_at", -1)])
col_brands.create_index("order")
col_coupons.create_index("code", unique=True)
col_reviews.create_index([("product_id", 1), ("created_at", -1)])
col_reviews.create_index([("user_id", 1), ("product_id", 1)], unique=True)

# ══════════════════════════════════════════════════════════════
#  REALTIME NOTIFICATIONS (SSE) — gọn nhẹ, không cần Redis
#  - Admin mở trang gì cũng auto lắng nghe /admin/stream
#  - Có đơn manual / nạp tiền mới -> push event tới toàn bộ tab admin đang mở
#  - Chuông trên topbar sáng lên + phát tiếng "ting"
# ══════════════════════════════════════════════════════════════
class _EventHub:
    """Fan-out event tới nhiều SSE subscribers. In-process, thread-safe."""
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []  # list[queue.Queue]

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict):
        payload = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subs.remove(q)

    def n_subs(self):
        with self._lock:
            return len(self._subs)

_hub = _EventHub()

# NOTE: Định nghĩa `notify_admins` sớm (không kèm @admin_required) để `buy()`
# và `sepay_webhook()` gọi được. Các ROUTE SSE (@admin_required) chuyển xuống
# SAU khối định nghĩa `admin_required` — xem cuối file.
def notify_admins(kind: str, title: str, message: str, url: str = "", meta: dict = None):
    """Đẩy 1 sự kiện realtime tới mọi admin đang online (+ ghi vào col_notifications)."""
    ev = {
        "_id": uuid.uuid4().hex[:12],
        "kind": kind,             # "order_pending" | "recharge" | "info"
        "title": title,
        "message": message,
        "url": url,
        "meta": meta or {},
        "created_at": datetime.now(timezone.utc),
        "read_by": [],
    }
    try:
        db["notifications"].insert_one(ev.copy())
    except Exception as e:
        print(f"[notify] Không lưu notification được: {e}")
    _hub.publish(ev)
    # v5 E: forward qua Telegram (nếu bật) — _telegram_send được định nghĩa
    # phía dưới, nhưng Python resolve name khi call nên OK.
    try:
        _telegram_send(f"<b>{title}</b>\n{message}")
    except Exception as e:
        print(f"[notify->telegram] {e}")


# ══════════════════════════════════════════════════════════════
#  BRAND (thương hiệu) — B: quản lý động qua trang /admin/brands
# ══════════════════════════════════════════════════════════════
_DEFAULT_BRANDS = [
    {"_id": "redfinger", "label": "RedFinger", "icon": "fas fa-fingerprint", "cover_url": "", "order": 1, "enabled": True},
    {"_id": "ugphone",   "label": "UGPhone",   "icon": "fas fa-mobile-alt",  "cover_url": "", "order": 2, "enabled": True},
    {"_id": "vipplayer", "label": "VIPPlayer", "icon": "fas fa-crown",       "cover_url": "", "order": 3, "enabled": True},
    {"_id": "genplay",   "label": "GenPlay",   "icon": "fas fa-gamepad",     "cover_url": "", "order": 4, "enabled": True},
    {"_id": "vmos",      "label": "VMOS",      "icon": "fas fa-cloud",       "cover_url": "", "order": 5, "enabled": True},
]

def _seed_brands_if_empty():
    if col_brands.count_documents({}) == 0:
        col_brands.insert_many(_DEFAULT_BRANDS)

_seed_brands_if_empty()

def get_brands(enabled_only: bool = False):
    """Trả về dict {key: doc} — vẫn giữ thứ tự order."""
    q = {"enabled": True} if enabled_only else {}
    out = {}
    for b in col_brands.find(q).sort("order", 1):
        out[b["_id"]] = b
    return out


def get_config():
    cfg = col_config.find_one({"_id": "main"})
    if not cfg:
        cfg = {
            "_id": "main",
            "sepay_api_key": "",
            "bank_bin": "",              # mã ngân hàng (BIN) dùng cho VietQR, vd MB=970422
            "bank_account_number": "",
            "bank_account_name": "",
            "site_name": "CloudShop",
            "announcement": "",
            "contact_zalo": "",
            "contact_telegram": "",
            "low_stock_threshold": 5,          # C: cảnh báo tồn kho thấp <=
            "referral_percent": 5,             # H: % hoa hồng tự cộng khi ref mua
            "referral_on_recharge": False,     # H: có cộng % khi ref nạp không
            "min_deposit": 0,
            # Trang trí ảnh (Mức 1)
            "logo_url": "",                    # trống -> hiện text
            "favicon_url": "",                 # trống -> mặc định
            "wallet_bg_url": "",               # ảnh nền cho khối QR nạp tiền
            "homepage_slides": [],             # danh sách slide banner trang chủ (Mức 4)
            "bot_api_key": "",                 # API key cho bot Discord / integration
            # v5
            "telegram_bot_token": "",          # E: forward notify_admins qua Telegram
            "telegram_chat_id": "",
            "captcha_enabled": True,           # G: captcha đăng ký
            "reviews_enabled": True,           # F: bật/tắt review
            "seo_description": "",             # H: meta description
            "seo_keywords": "",
            "purchase_limit_per_user": 0,      # B: 0 = không giới hạn
            "purchase_limit_per_day": 0,       # B: giới hạn số đơn/1 khách/1 ngày
            # Nạp thẻ cào qua gachthefast
            "gachthefast_domain": "gachthefast.com",
            "gachthefast_partner_id": "",
            "gachthefast_secret_key": "",
            "gachthefast_enabled": False,
            "background_music_url": "",         # để trống -> không hiện nút nhạc
        }
        col_config.insert_one(cfg)
    else:
        # Tự bổ sung field mới cho config đã có sẵn từ trước
        defaults = {
            "contact_zalo": "", "contact_telegram": "",
            "low_stock_threshold": 5,
            "referral_percent": 5, "referral_on_recharge": False,
            "min_deposit": 0,
            "logo_url": "", "favicon_url": "", "wallet_bg_url": "",
            "homepage_slides": [],
            "bot_api_key": "",
            "telegram_bot_token": "", "telegram_chat_id": "",
            "captcha_enabled": True, "reviews_enabled": True,
            "seo_description": "", "seo_keywords": "",
            "purchase_limit_per_user": 0, "purchase_limit_per_day": 0,
            "gachthefast_domain": "gachthefast.com",
            "gachthefast_partner_id": "",
            "gachthefast_secret_key": "",
            "gachthefast_enabled": False,
            "background_music_url": "",
        }
        changed = False
        for k, v in defaults.items():
            if k not in cfg:
                cfg[k] = v; changed = True
        if changed:
            col_config.update_one({"_id": "main"}, {"$set": cfg})
    return cfg


# ══════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════════
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return col_users.find_one({"_id": uid})

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            flash("Vui lòng đăng nhập để tiếp tục.", "error")
            return redirect(url_for("login", next=request.path))
        u = current_user()
        if u and u.get("blocked"):
            session.clear()
            flash("Tài khoản của bạn đã bị khoá.", "error")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper

# v5 D — Multi-role admin. Vai trò con của "admin":
#   super  = toàn quyền (mặc định của user đầu tiên đăng ký)
#   mod    = quản lý đơn / user (cộng/trừ ví) / kho
#   sales  = xem thống kê + quản lý đơn + xử lý manual
# Các vai trò không phải "admin" (như "user") không đụng tới quyền admin.
ROLE_PERMS = {
    "super": {"*"},  # tất cả
    "mod":   {"dashboard", "orders", "orders.write", "users", "users.write",
              "stock", "recharges", "activity"},
    "sales": {"dashboard", "orders", "orders.write", "recharges", "activity"},
}

def has_perm(user, perm: str) -> bool:
    if not user:
        return False
    if user.get("role") != "admin":
        return False
    sub = user.get("admin_role", "super")  # backward-compat: coi admin cũ = super
    perms = ROLE_PERMS.get(sub, set())
    return "*" in perms or perm in perms

def admin_required(perm: str = None):
    """
    Dùng 2 cách:
      @admin_required            # decorator không tham số, phải là admin, không phân quyền chi tiết
      @admin_required("orders")  # cần permission cụ thể
    """
    # Cho phép dùng @admin_required kiểu cũ (không có ())
    if callable(perm):
        f0 = perm
        @wraps(f0)
        def wrapper0(*a, **kw):
            u = current_user()
            if not u or u.get("role") != "admin" or u.get("blocked"):
                flash("Bạn không có quyền truy cập chức năng này.", "error")
                return redirect(url_for("home"))
            if u.get("totp_secret") and not session.get("2fa_ok"):
                return redirect(url_for("twofa_verify", next=request.path))
            return f0(*a, **kw)
        return wrapper0
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            u = current_user()
            if not u or u.get("role") != "admin" or u.get("blocked"):
                flash("Bạn không có quyền truy cập chức năng này.", "error")
                return redirect(url_for("home"))
            if u.get("totp_secret") and not session.get("2fa_ok"):
                return redirect(url_for("twofa_verify", next=request.path))
            if perm and not has_perm(u, perm):
                flash(f"Vai trò {u.get('admin_role','?')} không có quyền truy cập chức năng '{perm}'.", "error")
                return redirect(url_for("admin_dashboard"))
            return f(*a, **kw)
        return wrapper
    return deco


# ══════════════════════════════════════════════════════════════
#  CSRF TOKEN (G) — token nhẹ dựa trên session, không cần Flask-WTF
#  Mọi form POST admin phải kèm <input name="csrf_token" value="{{ csrf_token() }}">
# ══════════════════════════════════════════════════════════════
def _get_csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok

def csrf_protect(f):
    """Chỉ dùng cho các route mutating (POST). Trả 400 nếu token không khớp."""
    @wraps(f)
    def wrapper(*a, **kw):
        if request.method == "POST":
            sent = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
            if not sent or sent != session.get("_csrf", ""):
                # Không phá webhook (webhook dùng Authorization header khác, không đi qua đây)
                flash("Phiên làm việc đã hết hạn, vui lòng thử lại.", "error")
                return redirect(request.referrer or url_for("home"))
        return f(*a, **kw)
    return wrapper


# ══════════════════════════════════════════════════════════════
#  AUDIT LOG (G) — mọi hành động admin đều gọi log_activity()
# ══════════════════════════════════════════════════════════════
def log_activity(user_id, username, content):
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
    col_activity.insert_one({
        "_id": uuid.uuid4().hex[:12],
        "user_id": user_id,
        "username": username,
        "content": content,
        "ip": ip,
        "created_at": datetime.now(timezone.utc),
    })


def log_admin(content):
    """Shortcut: log 1 hành động của admin đang đăng nhập."""
    u = current_user()
    if u:
        log_activity(u["_id"], u.get("username", ""), f"[admin] {content}")


@app.context_processor
def inject_globals():
    brands_full = get_brands()
    brands = {k: b["label"] for k, b in brands_full.items()}
    brand_icons = {k: b.get("icon", "fas fa-mobile-alt") for k, b in brands_full.items()}
    brand_icon_images = {k: b.get("icon_image_url", "") for k, b in brands_full.items()}
    return {
        "current_user": current_user(),
        "cfg": get_config(),
        "brands": brands,
        "brand_icons": brand_icons,
        "brand_icon_images": brand_icon_images,
        "brand_counts": {b: col_products.count_documents({"brand": b, "enabled": True}) for b in brands},
        "csrf_token": _get_csrf_token,
    }


# ══════════════════════════════════════════════════════════════
#  TIỆN ÍCH CHUNG (feed, phân trang)
# ══════════════════════════════════════════════════════════════
def mask_id(s):
    """ab12cd345 -> ...345 (ẩn bớt, giữ 3 ký tự cuối)."""
    s = str(s or "")
    return f"...{s[-3:]}" if len(s) > 3 else s


def short_text(s, n=34):
    s = str(s or "")
    return s if len(s) <= n else s[:n].rstrip() + "..."


def timeago(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "vừa xong"
    mins = secs // 60
    if mins < 60:
        return f"{mins} phút trước"
    hours = mins // 60
    if hours < 24:
        return f"{hours} giờ trước"
    return f"{hours // 24} ngày trước"


app.jinja_env.filters["timeago"] = timeago


def parse_page(default_per=30, max_per=200):
    """Đọc ?page=&per= từ query string, kẹp về khoảng an toàn."""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per = int(request.args.get("per", str(default_per)))
    except ValueError:
        per = default_per
    per = max(5, min(per, max_per))
    return page, per


def parse_date_range():
    """?from=YYYY-MM-DD&to=YYYY-MM-DD -> (from_dt, to_dt) UTC, có thể None."""
    def _p(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    f = _p(request.args.get("from"))
    t = _p(request.args.get("to"))
    if t:
        t = t + timedelta(days=1)  # inclusive tới hết ngày to
    return f, t


def get_recent_activity(limit=15):
    orders_raw = list(col_orders.find().sort("created_at", -1).limit(limit))
    uids = {o["user_id"] for o in orders_raw}
    users_map = {u["_id"]: u["username"]
                 for u in col_users.find({"_id": {"$in": list(uids)}}, {"username": 1})}
    recent_orders = [{
        "user": mask_id(users_map.get(o["user_id"], o["user_id"])),
        "product": short_text(o.get("product_name", "")),
        "price": o.get("price", 0),
        "time": timeago(o.get("created_at")),
    } for o in orders_raw]

    recharges_raw = list(col_recharges.find().sort("created_at", -1).limit(limit))
    recent_recharges = [{
        "user": mask_id(r.get("username", "")),
        "amount": r.get("amount", 0),
        "bank": r.get("bank") or "Ngân hàng",
        "time": timeago(r.get("created_at")),
    } for r in recharges_raw]

    return recent_orders, recent_recharges


def get_top_buyers(limit=10, days=30):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    agg = list(col_orders.aggregate([
        {"$match": {"created_at": {"$gte": since}, "status": {"$ne": "cancelled"}}},
        {"$group": {"_id": "$user_id", "total": {"$sum": "$price"}, "orders": {"$sum": 1}}},
        {"$sort": {"total": -1}},
        {"$limit": limit},
    ]))
    users_map = {u["_id"]: u["username"]
                 for u in col_users.find({"_id": {"$in": [a["_id"] for a in agg]}}, {"username": 1})}
    return [{
        "user": mask_id(users_map.get(a["_id"], a["_id"])),
        "total": a["total"],
        "orders": a["orders"],
    } for a in agg]


# ══════════════════════════════════════════════════════════════
#  TRANG CHỦ — danh sách sản phẩm theo từng brand (bật)
# ══════════════════════════════════════════════════════════════
@app.route("/")
def home():
    brands_full = get_brands(enabled_only=True)
    products_by_brand = {}
    for b in brands_full:
        items = list(col_products.find({"brand": b, "enabled": True}).sort("price", 1))
        for it in items:
            if it.get("fulfillment", "auto") == "manual":
                it["stock_count"] = None
            else:
                it["stock_count"] = col_stock.count_documents({"product_id": it["_id"], "sold": False})
        products_by_brand[b] = items

    pinned_products = []
    for b, items in products_by_brand.items():
        pinned_products += [p for p in items if p.get("pinned")]

    cfg = get_config()
    slides = [s for s in (cfg.get("homepage_slides") or []) if s.get("enabled", True)]
    slides.sort(key=lambda s: s.get("order", 999))

    recent_orders, recent_recharges = get_recent_activity()
    top_buyers = get_top_buyers()
    return render_template("home.html",
                            products_by_brand=products_by_brand,
                            brands_full=brands_full,
                            pinned_products=pinned_products,
                            slides=slides,
                            recent_orders=recent_orders,
                            recent_recharges=recent_recharges,
                            top_buyers=top_buyers)


@app.route("/api/activity")
def api_activity():
    recent_orders, recent_recharges = get_recent_activity()
    return jsonify({"orders": recent_orders, "recharges": recent_recharges})


# ══════════════════════════════════════════════════════════════
#  ĐĂNG KÝ / ĐĂNG NHẬP / ĐĂNG XUẤT
# ══════════════════════════════════════════════════════════════
_login_fail_counter = {}  # ip -> (count, first_ts). Rate limit login (chống brute force).

def _login_rate_limited():
    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
    now = datetime.now(timezone.utc)
    cnt, first = _login_fail_counter.get(ip, (0, now))
    if (now - first).total_seconds() > 300:
        cnt, first = 0, now
    return ip, cnt, first, now

def _login_fail_record(ip, cnt, first, now):
    _login_fail_counter[ip] = (cnt + 1, first)


def _new_captcha():
    a, b = secrets.randbelow(9) + 1, secrets.randbelow(9) + 1
    op   = secrets.choice(["+", "-"])
    ans  = (a + b) if op == "+" else (a - b)
    session["_captcha_ans"] = str(ans)
    return f"{a} {op} {b} = ?"

@app.route("/register", methods=["GET", "POST"])
def register():
    ref = request.values.get("ref", "").strip()
    cfg = get_config()
    captcha_enabled = bool(cfg.get("captcha_enabled", True))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if captcha_enabled:
            answer = (request.form.get("captcha", "") or "").strip()
            expected = session.get("_captcha_ans", "")
            if not expected or answer != expected:
                flash("Câu hỏi bảo vệ không đúng, vui lòng thử lại.", "error")
                q = _new_captcha()
                return render_template("register.html", ref=ref, captcha_q=q, captcha_enabled=True)

        if not re.fullmatch(r"[a-z0-9_]{4,20}", username):
            flash("Tên đăng nhập chỉ gồm chữ thường/số/gạch dưới, 4-20 ký tự.", "error")
            q = _new_captcha() if captcha_enabled else ""
            return render_template("register.html", ref=ref, captcha_q=q, captcha_enabled=captcha_enabled)
        if len(password) < 6:
            flash("Mật khẩu phải từ 6 ký tự trở lên.", "error")
            q = _new_captcha() if captcha_enabled else ""
            return render_template("register.html", ref=ref, captcha_q=q, captcha_enabled=captcha_enabled)
        if password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "error")
            q = _new_captcha() if captcha_enabled else ""
            return render_template("register.html", ref=ref, captcha_q=q, captcha_enabled=captcha_enabled)
        if col_users.find_one({"username": username}):
            flash("Tên đăng nhập đã tồn tại.", "error")
            q = _new_captcha() if captcha_enabled else ""
            return render_template("register.html", ref=ref, captcha_q=q, captcha_enabled=captcha_enabled)

        referrer = col_users.find_one({"_id": ref}) if ref else None

        uid = uuid.uuid4().hex[:12]
        is_first_user = col_users.count_documents({}) == 0
        col_users.insert_one({
            "_id": uid,
            "username": username,
            "password_hash": generate_password_hash(password),
            "balance": 0,
            "role": "admin" if is_first_user else "user",
            "admin_role": "super" if is_first_user else None,  # v5 D
            "blocked": False,
            "referred_by": referrer["_id"] if referrer else None,
            "referral_earned": 0,
            "processed_tx_ids": [],
            "created_at": datetime.now(timezone.utc),
        })
        session["uid"] = uid
        note = "Đăng ký tài khoản mới"
        if referrer:
            note += f" (giới thiệu bởi {referrer['username']})"
        log_activity(uid, username, note)
        if is_first_user:
            flash("Đăng ký thành công! Bạn là người đầu tiên nên tự động là Admin.", "success")
        else:
            flash("Đăng ký thành công! Chào mừng bạn.", "success")
        return redirect(url_for("home"))

    q = _new_captcha() if captcha_enabled else ""
    return render_template("register.html", ref=ref, captcha_q=q, captcha_enabled=captcha_enabled)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip, cnt, first, now = _login_rate_limited()
        if cnt >= 10:
            flash("Đăng nhập sai quá nhiều lần, vui lòng thử lại sau 5 phút.", "error")
            return render_template("login.html")

        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        u = col_users.find_one({"username": username})
        if not u or not check_password_hash(u["password_hash"], password):
            _login_fail_record(ip, cnt, first, now)
            flash("Sai tên đăng nhập hoặc mật khẩu.", "error")
            return render_template("login.html")
        if u.get("blocked"):
            flash("Tài khoản của bạn đã bị khoá. Liên hệ admin để mở lại.", "error")
            return render_template("login.html")
        session["uid"] = u["_id"]
        session.permanent = True
        log_activity(u["_id"], username, "Đăng nhập thành công")
        flash(f"Xin chào {username}!", "success")
        nxt = request.args.get("next") or url_for("home")
        return redirect(nxt)
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Đã đăng xuất.", "success")
    return redirect(url_for("home"))


# ══════════════════════════════════════════════════════════════
#  VÍ TIỀN — nạp tiền qua SePay/VietQR (giống cơ chế bot Discord)
# ══════════════════════════════════════════════════════════════
@app.route("/wallet")
@login_required
def wallet():
    u   = current_user()
    cfg = get_config()
    qr_url = None
    if cfg.get("bank_bin") and cfg.get("bank_account_number"):
        content = f"NAPU{u['_id']}"
        params  = f"amount=0&addInfo={content}&accountName={cfg.get('bank_account_name','')}"
        qr_url  = (f"https://img.vietqr.io/image/{cfg['bank_bin']}-"
                   f"{cfg['bank_account_number']}-qr_only.jpg?{params}")
    orders = list(col_orders.find({"user_id": u["_id"]}).sort("created_at", -1).limit(20))

    total_deposited = 0
    agg = list(col_recharges.aggregate([
        {"$match": {"user_id": u["_id"]}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]))
    if agg:
        total_deposited = agg[0]["total"]

    referral_count = col_users.count_documents({"referred_by": u["_id"]})
    referral_link = request.host_url.rstrip("/") + url_for("register") + f"?ref={u['_id']}"

    return render_template("wallet.html", u=u, qr_url=qr_url, total_deposited=total_deposited,
                            transfer_content=f"NAPU{u['_id']}", orders=orders,
                            referral_count=referral_count, referral_link=referral_link)


@app.route("/account/change-password", methods=["POST"])
@login_required
@csrf_protect
def change_password():
    u = current_user()
    old = request.form.get("old_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    if not check_password_hash(u["password_hash"], old):
        flash("Mật khẩu cũ không đúng.", "error")
    elif len(new) < 6:
        flash("Mật khẩu mới phải từ 6 ký tự trở lên.", "error")
    elif new != confirm:
        flash("Xác nhận mật khẩu mới không khớp.", "error")
    else:
        col_users.update_one({"_id": u["_id"]}, {"$set": {"password_hash": generate_password_hash(new)}})
        log_activity(u["_id"], u["username"], "Đổi mật khẩu")
        flash("Đã đổi mật khẩu thành công.", "success")
    return redirect(url_for("wallet"))


def _award_referral_commission(referred_user_id, base_amount, source_label):
    """
    H: cộng hoa hồng cho người giới thiệu (ref) khi người được ref phát sinh doanh số.
    - `source_label`: mô tả nguồn (mua sản phẩm gì, nạp qua đâu) để ghi log.
    Trả về số tiền đã cộng (0 nếu không có ref/không bật).
    """
    if base_amount <= 0:
        return 0
    u = col_users.find_one({"_id": referred_user_id}, {"referred_by": 1, "username": 1})
    if not u or not u.get("referred_by"):
        return 0
    cfg = get_config()
    pct = float(cfg.get("referral_percent", 0) or 0)
    if pct <= 0:
        return 0
    commission = int(round(base_amount * pct / 100))
    if commission <= 0:
        return 0
    ref_id = u["referred_by"]
    ref = col_users.find_one_and_update(
        {"_id": ref_id},
        {"$inc": {"balance": commission, "referral_earned": commission}},
        return_document=ReturnDocument.AFTER,
    )
    if not ref:
        return 0
    log_activity(
        ref["_id"], ref.get("username", ""),
        f"[hoa hồng] +{commission:,}đ từ {u.get('username','?')} — {source_label}"
    )
    return commission


# ══════════════════════════════════════════════════════════════
#  COUPON (v5 A) — mã giảm giá dùng trong checkout
#  Doc: {code, kind:"percent"|"fixed", value, max_uses, uses,
#        expires_at, min_amount, per_user_limit, enabled}
# ══════════════════════════════════════════════════════════════
def _find_valid_coupon(code: str, user_id: str, base_amount: int):
    """Trả về (coupon_doc, discount_int) nếu hợp lệ, else (None, 0). Không trừ uses ở đây."""
    if not code:
        return None, 0
    c = col_coupons.find_one({"code": code.strip().upper()})
    if not c or not c.get("enabled", True):
        return None, 0
    now = datetime.now(timezone.utc)
    exp = c.get("expires_at")
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp and now > exp:
        return None, 0
    if c.get("max_uses", 0) and c.get("uses", 0) >= c["max_uses"]:
        return None, 0
    if base_amount < int(c.get("min_amount", 0) or 0):
        return None, 0
    per_user = int(c.get("per_user_limit", 0) or 0)
    if per_user > 0:
        used_by_user = (c.get("used_by") or {}).get(user_id, 0)
        if used_by_user >= per_user:
            return None, 0
    kind, val = c.get("kind", "percent"), float(c.get("value", 0))
    if kind == "percent":
        discount = int(round(base_amount * val / 100))
    else:
        discount = int(val)
    discount = max(0, min(discount, base_amount))
    return c, discount

def _consume_coupon(coupon_doc, user_id: str):
    if not coupon_doc:
        return
    col_coupons.update_one(
        {"_id": coupon_doc["_id"]},
        {"$inc": {"uses": 1, f"used_by.{user_id}": 1}},
    )


# ══════════════════════════════════════════════════════════════
#  PURCHASE LIMIT (v5 B) — giới hạn số đơn/user và số đơn/user/ngày
# ══════════════════════════════════════════════════════════════
def _check_purchase_limits(user_id: str, product_id: str):
    """Trả về (ok:bool, reason:str)."""
    cfg = get_config()
    per_user = int(cfg.get("purchase_limit_per_user", 0) or 0)
    per_day  = int(cfg.get("purchase_limit_per_day", 0) or 0)
    if per_user > 0:
        cnt = col_orders.count_documents({
            "user_id": user_id, "product_id": product_id,
            "status": {"$in": ["pending", "completed"]},
        })
        if cnt >= per_user:
            return False, f"Bạn đã mua sản phẩm này {cnt} lần rồi (giới hạn {per_user} lần/khách)."
    if per_day > 0:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        cnt = col_orders.count_documents({
            "user_id": user_id,
            "status": {"$in": ["pending", "completed"]},
            "created_at": {"$gte": since},
        })
        if cnt >= per_day:
            return False, f"Bạn đã đặt {cnt} đơn trong 24 giờ qua (giới hạn {per_day} đơn/24h)."
    return True, ""


# ══════════════════════════════════════════════════════════════
#  TELEGRAM FORWARD (v5 E) — đẩy notify_admins qua Telegram bot
# ══════════════════════════════════════════════════════════════
def _telegram_send(text: str):
    cfg = get_config()
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat  = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat:
        return
    def _do():
        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode({
                "chat_id": chat,
                "text": text[:3800],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data, method="POST",
            )
            urllib.request.urlopen(req, timeout=6).read()
        except Exception as e:
            print(f"[telegram] send err: {e}")
    threading.Thread(target=_do, daemon=True).start()


@app.route("/sepay-webhook", methods=["POST"])
def sepay_webhook():
    """
    Nhận webhook SePay (giống hệt cơ chế trong bot Discord).
    Nội dung chuyển khoản chứa mã dạng NAPU<user_id> để xác định đúng user cần cộng ví.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("transferType") != "in":
        return jsonify({"success": True})

    amount = int(payload.get("transferAmount", 0) or 0)
    text = f"{payload.get('content','') or ''} {payload.get('code','') or ''}".upper().replace(" ", "")
    m = re.search(r"NAPU([A-F0-9]{8,20})", text)
    if not m or amount <= 0:
        return jsonify({"success": True})  # không khớp mã -> bỏ qua, không phải lỗi

    cfg = get_config()
    api_key = cfg.get("sepay_api_key", "")
    auth    = request.headers.get("Authorization", "")
    if not api_key or auth != f"Apikey {api_key}":
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    uid = m.group(1)
    tx_id = str(payload.get("id", "") or "")
    # Chống cộng trùng nếu SePay gửi lại webhook (đã xử lý giao dịch id này rồi thì bỏ qua)
    if tx_id and col_users.find_one({"_id": uid, "processed_tx_ids": tx_id}):
        return jsonify({"success": True})

    result = col_users.find_one_and_update(
        {"_id": uid},
        {"$inc": {"balance": amount}, "$push": {"processed_tx_ids": tx_id}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        return jsonify({"success": True})  # user không tồn tại -> bỏ qua êm

    col_recharges.insert_one({
        "_id": uuid.uuid4().hex[:12],
        "user_id": uid,
        "username": result.get("username", uid),
        "amount": amount,
        "bank": payload.get("gateway") or "Ngân hàng",
        "source": "sepay",
        "created_at": datetime.now(timezone.utc),
    })

    # H: hoa hồng nạp tiền (nếu admin bật referral_on_recharge)
    if cfg.get("referral_on_recharge"):
        _award_referral_commission(uid, amount, f"nạp tiền +{amount:,}đ")

    # Realtime: báo admin có giao dịch nạp tiền mới
    notify_admins(
        kind="recharge",
        title="💰 Nạp tiền mới",
        message=f"{result.get('username', uid)} nạp +{amount:,}đ qua {payload.get('gateway','Ngân hàng')}",
        url=url_for("admin_recharges"),
        meta={"user_id": uid, "amount": amount},
    )

    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════
#  NẠP THẺ CÀO QUA GACHTHEFAST — endpoint: {domain}/chargingws/v2
#  sign = md5(partner_key + code + serial)  — đã xác nhận theo tài liệu chính thức.
#  ⚠️ callback_sign (chữ ký gachthefast gửi kèm khi họ gọi ngược về /charge/callback)
#  thì CHƯA có công thức — hàm callback bên dưới tạm chỉ đối chiếu request_id,
#  chưa kiểm tra callback_sign. Nếu có tài liệu phần này thì bổ sung sau.
# ══════════════════════════════════════════════════════════════
col_card_topups = db["card_topups"]
col_card_topups.create_index([("created_at", -1)])
col_card_topups.create_index("request_id", unique=True)


def gachthefast_sign(cfg, code, serial):
    """Theo tài liệu chính thức: sign = md5(partner_key + code + serial)."""
    secret = cfg.get("gachthefast_secret_key", "")
    raw = secret + str(code) + str(serial)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _gachthefast_call(cfg, params):
    import urllib.request, urllib.parse
    domain = (cfg.get("gachthefast_domain", "") or "gachthefast.com").strip()
    if not domain.startswith("http"):
        domain = "http://" + domain
    url = f"{domain}/chargingws/v2"
    data = urllib.parse.urlencode(params).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[gachthefast] call err: {e}")
        return None


@app.route("/wallet/charge-card", methods=["POST"])
@login_required
@csrf_protect
def charge_card():
    u = current_user()
    cfg = get_config()
    if not cfg.get("gachthefast_enabled") or not cfg.get("gachthefast_partner_id"):
        flash("Nạp thẻ cào hiện chưa được bật. Vui lòng chọn cách nạp khác.", "error")
        return redirect(url_for("wallet"))

    telco  = (request.form.get("telco") or "").strip().upper()
    code   = (request.form.get("code") or "").strip()
    serial = (request.form.get("serial") or "").strip()
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    if telco not in {"VIETTEL", "VINAPHONE", "MOBIFONE", "GATE", "ZING"} or not code or not serial or amount <= 0:
        flash("Vui lòng nhập đầy đủ và đúng thông tin thẻ.", "error")
        return redirect(url_for("wallet"))

    request_id = uuid.uuid4().hex[:16]
    sign = gachthefast_sign(cfg, code, serial)

    params = {
        "telco": telco, "code": code, "serial": serial, "amount": str(amount),
        "request_id": request_id, "partner_id": cfg.get("gachthefast_partner_id", ""),
        "sign": sign, "command": "charging",
    }
    result = _gachthefast_call(cfg, params)

    col_card_topups.insert_one({
        "_id": uuid.uuid4().hex[:12],
        "user_id": u["_id"], "username": u["username"],
        "telco": telco, "code": code, "serial": serial,
        "declared_value": amount, "value": None, "credited_amount": None,
        "request_id": request_id, "trans_id": (result or {}).get("trans_id"),
        "status": "pending", "created_at": datetime.now(timezone.utc),
    })
    log_activity(u["_id"], u["username"], f"Gửi thẻ cào {telco} mệnh giá {amount:,}đ")

    if result is None:
        flash("Không kết nối được tới gachthefast, vui lòng thử lại sau.", "error")
    else:
        flash("Đã gửi thẻ, hệ thống đang xử lý — kết quả sẽ tự động cộng vào ví trong giây lát.", "success")
    return redirect(url_for("wallet"))


@app.route("/charge/callback", methods=["GET", "POST"])
def gachthefast_callback():
    """gachthefast gọi ngược route này khi xử lý xong thẻ (không có session -> không qua csrf_protect)."""
    data = request.get_json(silent=True) or request.args.to_dict() or {}
    request_id = str(data.get("request_id", ""))
    topup = col_card_topups.find_one({"request_id": request_id})
    if not topup:
        return jsonify({"status": "ignored"}), 200
    if topup["status"] != "pending":
        return jsonify({"status": "already_processed"}), 200

    status        = str(data.get("status", ""))
    amount_credit = int(data.get("amount", 0) or 0)   # số tiền THỰC NHẬN (đã trừ phí gachthefast)
    real_value    = data.get("value")

    if status == "99":
        # Thẻ chờ xử lý — chưa có kết quả cuối, giữ nguyên trạng thái pending, đợi callback tiếp theo
        return jsonify({"status": "pending"}), 200

    if status in ("1", "2") and amount_credit > 0:
        # 1 = đúng mệnh giá, 2 = sai mệnh giá nhưng thẻ vẫn được chấp nhận (cộng đúng số tiền thực nhận)
        col_card_topups.update_one({"_id": topup["_id"]}, {"$set": {
            "status": "success", "value": real_value, "credited_amount": amount_credit,
            "trans_id": data.get("trans_id"), "updated_at": datetime.now(timezone.utc),
        }})
        col_users.update_one({"_id": topup["user_id"]}, {"$inc": {"balance": amount_credit}})
        col_recharges.insert_one({
            "_id": uuid.uuid4().hex[:12],
            "user_id": topup["user_id"], "username": topup["username"],
            "amount": amount_credit, "bank": f"Thẻ cào {topup['telco']}",
            "source": "gachthefast", "created_at": datetime.now(timezone.utc),
        })
        note = "đúng mệnh giá" if status == "1" else "SAI mệnh giá, cộng theo số tiền thực nhận"
        log_activity(topup["user_id"], topup["username"],
                     f"Nạp thẻ cào {topup['telco']} thành công ({note}) +{amount_credit:,}đ")
        cfg = get_config()
        if cfg.get("referral_on_recharge"):
            _award_referral_commission(topup["user_id"], amount_credit, f"nạp thẻ {topup['telco']}")
        notify_admins(
            kind="recharge", title="💳 Nạp thẻ cào thành công",
            message=f"{topup['username']} nạp thẻ {topup['telco']} +{amount_credit:,}đ" + ("" if status == "1" else " (sai mệnh giá)"),
            url=url_for("admin_recharges"), meta={"user_id": topup["user_id"], "amount": amount_credit},
        )
    else:
        # 3 = thẻ lỗi, 4 = hệ thống bảo trì, 100 = gửi thẻ thất bại
        col_card_topups.update_one({"_id": topup["_id"]}, {"$set": {
            "status": "failed", "value": real_value, "gachthefast_status": status,
            "updated_at": datetime.now(timezone.utc),
        }})
        log_activity(topup["user_id"], topup["username"],
                     f"Nạp thẻ cào {topup['telco']} thất bại (mã {status}): {data.get('message','')}")

    return jsonify({"status": "ok"}), 200
@app.route("/buy/<product_id>", methods=["POST"])
@login_required
@csrf_protect
def buy(product_id):
    u = current_user()
    p = col_products.find_one({"_id": product_id, "enabled": True})
    if not p:
        flash("Sản phẩm không tồn tại hoặc đã ngừng bán.", "error")
        return redirect(url_for("home"))

    # v5 B: giới hạn số đơn
    ok, reason = _check_purchase_limits(u["_id"], product_id)
    if not ok:
        flash(reason, "error")
        return redirect(url_for("home"))

    # v5 A: áp coupon (nếu có) — chỉ giảm cho đơn manual & auto đều ok
    coupon_code = (request.form.get("coupon", "") or "").strip().upper()
    coupon, discount = _find_valid_coupon(coupon_code, u["_id"], p["price"])
    final_price = p["price"] - discount

    if u["balance"] < final_price:
        flash("Số dư ví không đủ. Vui lòng nạp thêm tiền.", "error")
        return redirect(url_for("wallet"))

    fulfillment = p.get("fulfillment", "auto")
    customer_note = request.form.get("note", "").strip()[:500]

    if fulfillment == "manual":
        # Bán xoay vốn — không cần tồn kho, trừ tiền trước rồi admin xử lý và giao tay sau
        charged = col_users.find_one_and_update(
            {"_id": u["_id"], "balance": {"$gte": final_price}},
            {"$inc": {"balance": -final_price}},
            return_document=ReturnDocument.AFTER,
        )
        if not charged:
            flash("Số dư ví không đủ. Vui lòng nạp thêm tiền.", "error")
            return redirect(url_for("wallet"))

        order_id = uuid.uuid4().hex[:12]
        col_orders.insert_one({
            "_id": order_id,
            "user_id": u["_id"],
            "product_id": product_id,
            "product_name": p["name"],
            "brand": p["brand"],
            "price": final_price,
            "original_price": p["price"],
            "coupon_code": coupon["code"] if coupon else "",
            "discount": discount,
            "fulfillment": "manual",
            "status": "pending",
            "code": "",
            "account_info": "",
            "account_password": "",
            "admin_note": "",
            "customer_note": customer_note,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        _consume_coupon(coupon, u["_id"])
        # H: hoa hồng ref (dựa trên giá sau giảm)
        _award_referral_commission(u["_id"], final_price, f"đơn manual: {p['name']}")
        log_activity(u["_id"], u["username"], f"Đặt đơn manual: {p['name']} ({p['price']:,}đ)")
        # Realtime: báo admin có đơn manual mới cần duyệt tay
        notify_admins(
            kind="order_pending",
            title="🛒 Đơn mới cần xử lý",
            message=f"{u['username']} mua «{p['name']}» — {p['price']:,}đ",
            url=url_for("admin_order_update", order_id=order_id),
            meta={"order_id": order_id, "price": p["price"], "product": p["name"]},
        )
        flash("Đặt hàng thành công! Đơn của bạn đang chờ admin xử lý, vui lòng theo dõi ở mục Đơn hàng.", "success")
        return redirect(url_for("orders"))

    # ---- Kiểu "auto" (mặc định) — trừ kho có sẵn như cũ ----
    code_doc = col_stock.find_one_and_update(
        {"product_id": product_id, "sold": False},
        {"$set": {"sold": True, "sold_to": u["_id"], "sold_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not code_doc:
        flash("Sản phẩm tạm hết hàng, vui lòng quay lại sau.", "error")
        return redirect(url_for("home"))

    charged = col_users.find_one_and_update(
        {"_id": u["_id"], "balance": {"$gte": final_price}},
        {"$inc": {"balance": -final_price}},
        return_document=ReturnDocument.AFTER,
    )
    if not charged:
        col_stock.update_one({"_id": code_doc["_id"]},
                              {"$set": {"sold": False}, "$unset": {"sold_to": "", "sold_at": ""}})
        flash("Số dư ví không đủ. Vui lòng nạp thêm tiền.", "error")
        return redirect(url_for("wallet"))

    order_id = uuid.uuid4().hex[:12]
    col_orders.insert_one({
        "_id": order_id,
        "user_id": u["_id"],
        "product_id": product_id,
        "product_name": p["name"],
        "brand": p["brand"],
        "price": final_price,
        "original_price": p["price"],
        "coupon_code": coupon["code"] if coupon else "",
        "discount": discount,
        "fulfillment": "auto",
        "status": "completed",
        "code": code_doc["code"],
        "code_stock_id": code_doc["_id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    _consume_coupon(coupon, u["_id"])
    # H: hoa hồng ref
    _award_referral_commission(u["_id"], final_price, f"đơn auto: {p['name']}")
    log_activity(u["_id"], u["username"], f"Mua auto: {p['name']} ({p['price']:,}đ)")
    flash(f"Mua thành công! Mã của bạn: {code_doc['code']}", "success")
    return redirect(url_for("orders"))


@app.route("/orders")
@login_required
def orders():
    u = current_user()
    items = list(col_orders.find({"user_id": u["_id"]}).sort("created_at", -1))
    return render_template("orders.html", orders=items)


@app.route("/orders/<order_id>/invoice")
@login_required
def invoice(order_id):
    u = current_user()
    o = col_orders.find_one({"_id": order_id})
    if not o:
        flash("Đơn hàng không tồn tại.", "error")
        return redirect(url_for("orders"))
    if o["user_id"] != u["_id"] and u.get("role") != "admin":
        flash("Bạn không có quyền xem hoá đơn này.", "error")
        return redirect(url_for("orders"))
    buyer = col_users.find_one({"_id": o["user_id"]}, {"username": 1})
    return render_template("invoice.html", o=o, buyer=buyer)


# ══════════════════════════════════════════════════════════════
#  API JSON cho bot Discord / integration ngoài
#  Auth: header  X-API-Key: <key>  (đặt trong /admin/settings)
#  Endpoint READ-ONLY (không cho ghi dữ liệu qua HTTP để giảm rủi ro).
#  Chỉ trả những field cần thiết, che password_hash/tokens.
# ══════════════════════════════════════════════════════════════
def api_key_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        cfg = get_config()
        key = cfg.get("bot_api_key", "")
        sent = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
        if not key:
            return jsonify({"success": False, "error": "server_api_key_not_set"}), 503
        if not sent or sent != key:
            return jsonify({"success": False, "error": "unauthorized"}), 401
        return f(*a, **kw)
    return wrapper

def _clean_user(u: dict) -> dict:
    if not u:
        return {}
    return {
        "id": u.get("_id"),
        "username": u.get("username"),
        "role": u.get("role"),
        "balance": u.get("balance", 0),
        "blocked": bool(u.get("blocked")),
        "referred_by": u.get("referred_by"),
        "referral_earned": u.get("referral_earned", 0),
        "created_at": u["created_at"].isoformat() if u.get("created_at") else None,
    }

def _clean_order(o: dict) -> dict:
    if not o:
        return {}
    return {
        "id": o.get("_id"),
        "user_id": o.get("user_id"),
        "product_id": o.get("product_id"),
        "product_name": o.get("product_name"),
        "brand": o.get("brand"),
        "price": o.get("price", 0),
        "status": o.get("status"),
        "fulfillment": o.get("fulfillment"),
        "code": o.get("code", ""),
        "account_info": o.get("account_info", ""),
        "created_at": o["created_at"].isoformat() if o.get("created_at") else None,
        "updated_at": o["updated_at"].isoformat() if o.get("updated_at") else None,
    }


@app.route("/api/v1/ping")
@api_key_required
def api_ping():
    return jsonify({"success": True, "pong": True, "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/v1/stats")
@api_key_required
def api_stats():
    now = datetime.now(timezone.utc)
    d1  = now - timedelta(days=1)
    d7  = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    def _sum(coll, match):
        agg = list(coll.aggregate([
            {"$match": match},
            {"$group": {"_id": None, "total": {"$sum": "$price" if coll is col_orders else "$amount"},
                        "count": {"$sum": 1}}},
        ]))
        if not agg: return {"total": 0, "count": 0}
        return {"total": int(agg[0]["total"]), "count": int(agg[0]["count"])}

    return jsonify({
        "success": True,
        "revenue_24h":  _sum(col_orders, {"status": "completed", "created_at": {"$gte": d1}}),
        "revenue_7d":   _sum(col_orders, {"status": "completed", "created_at": {"$gte": d7}}),
        "revenue_30d":  _sum(col_orders, {"status": "completed", "created_at": {"$gte": d30}}),
        "recharge_24h": _sum(col_recharges, {"created_at": {"$gte": d1}}),
        "recharge_7d":  _sum(col_recharges, {"created_at": {"$gte": d7}}),
        "recharge_30d": _sum(col_recharges, {"created_at": {"$gte": d30}}),
        "users_total":  col_users.count_documents({}),
        "users_new_7d": col_users.count_documents({"created_at": {"$gte": d7}}),
        "orders_pending":   col_orders.count_documents({"status": "pending"}),
        "orders_completed": col_orders.count_documents({"status": "completed"}),
        "stock_total":  col_stock.count_documents({"sold": False}),
    })


@app.route("/api/v1/users")
@api_key_required
def api_users():
    try:
        limit = min(int(request.args.get("limit", 50)), 500)
    except ValueError:
        limit = 50
    q = request.args.get("q", "").strip()
    query = {}
    if q:
        query["$or"] = [{"username": {"$regex": re.escape(q), "$options": "i"}}, {"_id": q}]
    items = list(col_users.find(query).sort("created_at", -1).limit(limit))
    return jsonify({"success": True, "items": [_clean_user(u) for u in items], "count": len(items)})


@app.route("/api/v1/users/<uid>")
@api_key_required
def api_user_detail(uid):
    u = col_users.find_one({"_id": uid}) or col_users.find_one({"username": uid.lower()})
    if not u:
        return jsonify({"success": False, "error": "not_found"}), 404
    orders_count = col_orders.count_documents({"user_id": u["_id"]})
    total_deposited = 0
    agg = list(col_recharges.aggregate([
        {"$match": {"user_id": u["_id"]}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]))
    if agg:
        total_deposited = int(agg[0]["total"])
    d = _clean_user(u)
    d["orders_count"] = orders_count
    d["total_deposited"] = total_deposited
    return jsonify({"success": True, "user": d})


@app.route("/api/v1/orders")
@api_key_required
def api_orders():
    try:
        limit = min(int(request.args.get("limit", 50)), 500)
    except ValueError:
        limit = 50
    status = request.args.get("status", "").strip()
    user_id = request.args.get("user_id", "").strip()
    query = {}
    if status in ("pending", "completed", "cancelled"):
        query["status"] = status
    if user_id:
        query["user_id"] = user_id
    items = list(col_orders.find(query).sort("created_at", -1).limit(limit))
    return jsonify({"success": True, "items": [_clean_order(o) for o in items], "count": len(items)})


@app.route("/api/v1/orders/<oid>")
@api_key_required
def api_order_detail(oid):
    o = col_orders.find_one({"_id": oid})
    if not o:
        return jsonify({"success": False, "error": "not_found"}), 404
    return jsonify({"success": True, "order": _clean_order(o)})


@app.route("/api/v1/products")
@api_key_required
def api_products():
    brand = request.args.get("brand", "").strip()
    query = {"enabled": True}
    if brand:
        query["brand"] = brand
    items = list(col_products.find(query).sort([("brand", 1), ("price", 1)]))
    out = []
    for p in items:
        stock = None
        if p.get("fulfillment", "auto") != "manual":
            stock = col_stock.count_documents({"product_id": p["_id"], "sold": False})
        out.append({
            "id": p["_id"], "brand": p.get("brand"), "name": p.get("name"),
            "price": p.get("price", 0), "description": p.get("description", ""),
            "fulfillment": p.get("fulfillment", "auto"),
            "image_url": p.get("image_url", ""), "pinned": bool(p.get("pinned")),
            "stock_count": stock,
        })
    return jsonify({"success": True, "items": out, "count": len(out)})


@app.route("/api/v1/recharges")
@api_key_required
def api_recharges():
    try:
        limit = min(int(request.args.get("limit", 50)), 500)
    except ValueError:
        limit = 50
    user_id = request.args.get("user_id", "").strip()
    query = {}
    if user_id:
        query["user_id"] = user_id
    items = list(col_recharges.find(query).sort("created_at", -1).limit(limit))
    return jsonify({
        "success": True,
        "items": [{
            "id": r.get("_id"),
            "user_id": r.get("user_id"),
            "username": r.get("username"),
            "amount": r.get("amount", 0),
            "bank": r.get("bank"),
            "source": r.get("source"),
            "note": r.get("note", ""),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        } for r in items],
        "count": len(items),
    })


# ══════════════════════════════════════════════════════════════
#  2FA (TOTP) cho admin — dùng pyotp + qrcode
#  Flow:
#    /2fa/setup   -> hiện QR + secret, xác nhận bằng 1 mã 6 số => bật
#    /2fa/verify  -> nhập mã 6 số mỗi lần vào trang admin (session-scope)
#    /2fa/disable -> tắt (yêu cầu nhập password hiện tại + 1 mã 6 số)
#    /2fa/backup  -> xem lại backup codes (khi mất phone)
# ══════════════════════════════════════════════════════════════
def _gen_backup_codes(n=8):
    return [secrets.token_hex(4).upper() for _ in range(n)]

def _totp_ok(secret: str, code: str) -> bool:
    if not _HAS_PYOTP or not secret or not code:
        return False
    code = re.sub(r"\D", "", code or "")
    if len(code) not in (6, 8):
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False

@app.route("/2fa/setup", methods=["GET", "POST"])
@login_required
def twofa_setup():
    u = current_user()
    if u.get("role") != "admin":
        flash("Chỉ Admin mới cần 2FA.", "error")
        return redirect(url_for("home"))
    if not _HAS_PYOTP:
        flash("Server chưa cài `pyotp`. Cài đặt: pip install pyotp qrcode[pil]", "error")
        return redirect(url_for("admin_settings"))
    if u.get("totp_secret"):
        flash("Bạn đã bật 2FA. Muốn đổi máy? Hãy tắt trước.", "success")
        return redirect(url_for("twofa_backup"))

    # Sinh secret mới ở session (chỉ persist vào DB khi verify xong)
    if request.method == "POST":
        secret = session.get("2fa_pending_secret") or ""
        code   = request.form.get("code", "")
        if not _totp_ok(secret, code):
            flash("Mã 6 số không đúng, vui lòng thử lại.", "error")
            return redirect(url_for("twofa_setup"))
        backup = _gen_backup_codes(8)
        # Băm backup codes (không lưu plain — chỉ hiện đúng 1 lần cho user chép)
        hashed = [generate_password_hash(c) for c in backup]
        col_users.update_one({"_id": u["_id"]}, {"$set": {
            "totp_secret": secret,
            "totp_backup": hashed,
            "totp_enabled_at": datetime.now(timezone.utc),
        }})
        session["2fa_ok"] = True  # đang trong phiên setup, coi như đã verify
        session.pop("2fa_pending_secret", None)
        log_activity(u["_id"], u["username"], "[2FA] Bật xác thực 2 lớp")
        return render_template("twofa_backup_show.html", backup_codes=backup)

    # GET
    if not session.get("2fa_pending_secret"):
        session["2fa_pending_secret"] = pyotp.random_base32()
    secret = session["2fa_pending_secret"]
    label  = f"{get_config().get('site_name','CloudShop')}:{u['username']}"
    uri    = pyotp.TOTP(secret).provisioning_uri(name=u["username"], issuer_name=get_config().get("site_name", "CloudShop"))

    # Sinh QR code data-uri (nếu có thư viện qrcode)
    qr_data_uri = ""
    if _HAS_QRCODE:
        img = qrcode.make(uri, box_size=6, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return render_template("twofa_setup.html", secret=secret, uri=uri, qr_data_uri=qr_data_uri)


@app.route("/2fa/verify", methods=["GET", "POST"])
@login_required
def twofa_verify():
    u = current_user()
    if not u.get("totp_secret"):
        return redirect(url_for("home"))
    nxt = request.args.get("next") or url_for("admin_dashboard")
    if request.method == "POST":
        code = request.form.get("code", "")
        if _totp_ok(u["totp_secret"], code):
            session["2fa_ok"] = True
            log_activity(u["_id"], u["username"], "[2FA] Xác thực thành công (TOTP)")
            return redirect(nxt)
        # Fallback: thử match 1 backup code (dùng 1 lần rồi xoá)
        code_norm = re.sub(r"[^A-Fa-f0-9]", "", code).upper()
        remaining = []
        matched = False
        for h in u.get("totp_backup", []) or []:
            if not matched and check_password_hash(h, code_norm):
                matched = True   # bỏ mã này khỏi danh sách
                continue
            remaining.append(h)
        if matched:
            col_users.update_one({"_id": u["_id"]}, {"$set": {"totp_backup": remaining}})
            session["2fa_ok"] = True
            log_activity(u["_id"], u["username"],
                         f"[2FA] Xác thực bằng backup code (còn lại {len(remaining)})")
            return redirect(nxt)
        flash("Mã 2FA không đúng.", "error")
        return redirect(url_for("twofa_verify", next=nxt))
    return render_template("twofa_verify.html", nxt=nxt)


@app.route("/2fa/disable", methods=["POST"])
@login_required
@csrf_protect
def twofa_disable():
    u = current_user()
    if not u.get("totp_secret"):
        flash("2FA chưa bật.", "error")
        return redirect(url_for("admin_settings"))
    password = request.form.get("password", "")
    code     = request.form.get("code", "")
    if not check_password_hash(u["password_hash"], password):
        flash("Mật khẩu không đúng.", "error")
        return redirect(url_for("admin_settings"))
    if not _totp_ok(u["totp_secret"], code):
        flash("Mã 2FA không đúng.", "error")
        return redirect(url_for("admin_settings"))
    col_users.update_one({"_id": u["_id"]}, {"$unset": {
        "totp_secret": "", "totp_backup": "", "totp_enabled_at": "",
    }})
    session.pop("2fa_ok", None)
    log_activity(u["_id"], u["username"], "[2FA] TẮT xác thực 2 lớp")
    flash("Đã tắt 2FA.", "success")
    return redirect(url_for("admin_settings"))


@app.route("/2fa/backup")
@login_required
def twofa_backup():
    """Xem danh sách backup codes CÒN LẠI (dạng hash, chỉ báo còn bao nhiêu code)."""
    u = current_user()
    if not u.get("totp_secret"):
        return redirect(url_for("home"))
    return render_template("twofa_backup.html", remaining=len(u.get("totp_backup", []) or []))


# ══════════════════════════════════════════════════════════════
#  ADMIN — DASHBOARD (A) — thống kê tổng quan
# ══════════════════════════════════════════════════════════════
@app.route("/admin")
@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    now = datetime.now(timezone.utc)
    d1  = now - timedelta(days=1)
    d7  = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    def _sum_orders(since):
        agg = list(col_orders.aggregate([
            {"$match": {"status": "completed", "created_at": {"$gte": since}}},
            {"$group": {"_id": None, "total": {"$sum": "$price"}, "count": {"$sum": 1}}},
        ]))
        if not agg:
            return 0, 0
        return int(agg[0]["total"]), int(agg[0]["count"])

    def _sum_recharges(since):
        agg = list(col_recharges.aggregate([
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
        ]))
        if not agg:
            return 0, 0
        return int(agg[0]["total"]), int(agg[0]["count"])

    rev_1d,  cnt_ord_1d  = _sum_orders(d1)
    rev_7d,  cnt_ord_7d  = _sum_orders(d7)
    rev_30d, cnt_ord_30d = _sum_orders(d30)
    rch_1d,  cnt_rch_1d  = _sum_recharges(d1)
    rch_7d,  cnt_rch_7d  = _sum_recharges(d7)
    rch_30d, cnt_rch_30d = _sum_recharges(d30)

    # Doanh thu 7 ngày cho biểu đồ (nhóm theo ngày local +07)
    tz_offset = timedelta(hours=7)
    pipeline = [
        {"$match": {"status": "completed", "created_at": {"$gte": now - timedelta(days=7)}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d",
                                       "date": {"$add": ["$created_at", 7*3600*1000]}}},
            "total": {"$sum": "$price"}, "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}},
    ]
    daily_rev = {r["_id"]: r for r in col_orders.aggregate(pipeline)}
    chart_labels, chart_rev, chart_cnt = [], [], []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i) + tz_offset).strftime("%Y-%m-%d")
        chart_labels.append(day[5:])  # MM-DD
        r = daily_rev.get(day)
        chart_rev.append(int(r["total"]) if r else 0)
        chart_cnt.append(int(r["count"]) if r else 0)

    # Thống kê đơn theo status
    status_counts = {"pending": 0, "completed": 0, "cancelled": 0}
    for s in col_orders.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        status_counts[s["_id"]] = int(s["n"])

    # Top 5 sản phẩm bán chạy (30 ngày)
    top_products = list(col_orders.aggregate([
        {"$match": {"status": "completed", "created_at": {"$gte": d30}}},
        {"$group": {"_id": "$product_id",
                    "name": {"$first": "$product_name"},
                    "n": {"$sum": 1}, "rev": {"$sum": "$price"}}},
        {"$sort": {"rev": -1}},
        {"$limit": 5},
    ]))

    # Doanh thu theo brand (30 ngày)
    brand_rev = list(col_orders.aggregate([
        {"$match": {"status": "completed", "created_at": {"$gte": d30}}},
        {"$group": {"_id": "$brand", "n": {"$sum": 1}, "rev": {"$sum": "$price"}}},
        {"$sort": {"rev": -1}},
    ]))
    brands_all = get_brands()
    for b in brand_rev:
        b["label"] = brands_all.get(b["_id"], {}).get("label", b["_id"])

    # Tồn kho tổng + cảnh báo hết
    stock_by_product = list(col_stock.aggregate([
        {"$match": {"sold": False}},
        {"$group": {"_id": "$product_id", "n": {"$sum": 1}}},
    ]))
    total_stock = sum(x["n"] for x in stock_by_product)
    stock_map = {x["_id"]: x["n"] for x in stock_by_product}

    cfg = get_config()
    low_thr = int(cfg.get("low_stock_threshold", 5) or 0)
    low_stock_products = []
    for p in col_products.find({"enabled": True, "$or": [{"fulfillment": "auto"}, {"fulfillment": {"$exists": False}}]}):
        cnt = stock_map.get(p["_id"], 0)
        if cnt <= low_thr:
            p["stock_count"] = cnt
            low_stock_products.append(p)
    low_stock_products.sort(key=lambda x: x["stock_count"])

    # User mới 7 ngày
    total_users = col_users.count_documents({})
    new_users_7d = col_users.count_documents({"created_at": {"$gte": d7}})
    active_users = col_users.count_documents({"balance": {"$gt": 0}})

    pending_orders = col_orders.count_documents({"status": "pending"})

    return render_template(
        "admin_dashboard.html",
        rev_1d=rev_1d, rev_7d=rev_7d, rev_30d=rev_30d,
        cnt_ord_1d=cnt_ord_1d, cnt_ord_7d=cnt_ord_7d, cnt_ord_30d=cnt_ord_30d,
        rch_1d=rch_1d, rch_7d=rch_7d, rch_30d=rch_30d,
        cnt_rch_1d=cnt_rch_1d, cnt_rch_7d=cnt_rch_7d, cnt_rch_30d=cnt_rch_30d,
        chart_labels=chart_labels, chart_rev=chart_rev, chart_cnt=chart_cnt,
        status_counts=status_counts,
        top_products=top_products, brand_rev=brand_rev,
        total_stock=total_stock, low_stock_products=low_stock_products,
        low_thr=low_thr,
        total_users=total_users, new_users_7d=new_users_7d, active_users=active_users,
        pending_orders=pending_orders,
    )


# ══════════════════════════════════════════════════════════════
#  ADMIN — SẢN PHẨM (B): thêm/sửa/xoá/toggle/pin — brand động
# ══════════════════════════════════════════════════════════════
@app.route("/admin/products", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_products():
    brands_all = get_brands()
    if request.method == "POST":
        brand = request.form.get("brand", "")
        name  = request.form.get("name", "").strip()
        price = request.form.get("price", "0")
        desc  = request.form.get("description", "").strip()
        fulfillment = request.form.get("fulfillment", "auto")
        # Ưu tiên file upload > URL nhập tay
        image_url = resolve_image_field(subdir="products", old_value="")
        pinned = request.form.get("pinned") == "1"
        if fulfillment not in ("auto", "manual"):
            fulfillment = "auto"
        if brand not in brands_all or not name or not price.isdigit() or int(price) <= 0:
            flash("Thông tin sản phẩm không hợp lệ.", "error")
        else:
            pid = uuid.uuid4().hex[:10]
            col_products.insert_one({
                "_id": pid,
                "brand": brand, "name": name, "price": int(price),
                "description": desc, "enabled": True, "pinned": pinned,
                "fulfillment": fulfillment, "image_url": image_url,
                "created_at": datetime.now(timezone.utc),
            })
            log_admin(f"Thêm sản phẩm '{name}' [{brand}] giá {int(price):,}đ (id={pid})")
            flash(f"Đã thêm sản phẩm: {name}", "success")
        return redirect(url_for("admin_products"))

    # Filter (E)
    q = request.args.get("q", "").strip()
    brand_f = request.args.get("brand", "").strip()
    query = {}
    if q:
        query["name"] = {"$regex": re.escape(q), "$options": "i"}
    if brand_f and brand_f in brands_all:
        query["brand"] = brand_f

    items = list(col_products.find(query).sort([("brand", 1), ("price", 1)]))
    cfg = get_config()
    low_thr = int(cfg.get("low_stock_threshold", 5) or 0)
    for it in items:
        if it.get("fulfillment", "auto") == "manual":
            it["stock_count"] = None
            it["is_low"] = False
        else:
            it["stock_count"] = col_stock.count_documents({"product_id": it["_id"], "sold": False})
            it["is_low"] = it["stock_count"] <= low_thr
    return render_template("admin_products.html", items=items, q=q, brand_f=brand_f, low_thr=low_thr)


@app.route("/admin/products/<pid>/edit", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_edit_product(pid):
    """B: sửa sản phẩm — trước đây phải xoá đi tạo lại."""
    p = col_products.find_one({"_id": pid})
    if not p:
        flash("Sản phẩm không tồn tại.", "error")
        return redirect(url_for("admin_products"))
    brands_all = get_brands()
    if request.method == "POST":
        brand = request.form.get("brand", "")
        name  = request.form.get("name", "").strip()
        price = request.form.get("price", "0")
        desc  = request.form.get("description", "").strip()
        fulfillment = request.form.get("fulfillment", "auto")
        image_url = resolve_image_field(subdir="products", old_value=p.get("image_url", ""))
        pinned = request.form.get("pinned") == "1"
        if fulfillment not in ("auto", "manual"):
            fulfillment = "auto"
        if brand not in brands_all or not name or not price.isdigit() or int(price) <= 0:
            flash("Thông tin sản phẩm không hợp lệ.", "error")
            return redirect(url_for("admin_edit_product", pid=pid))
        col_products.update_one({"_id": pid}, {"$set": {
            "brand": brand, "name": name, "price": int(price),
            "description": desc, "pinned": pinned,
            "fulfillment": fulfillment, "image_url": image_url,
            "updated_at": datetime.now(timezone.utc),
        }})
        log_admin(f"Sửa sản phẩm '{name}' [{brand}] giá {int(price):,}đ (id={pid})")
        flash("Đã cập nhật sản phẩm.", "success")
        return redirect(url_for("admin_products"))
    return render_template("admin_product_edit.html", p=p, brands_all=brands_all)


@app.route("/admin/products/<pid>/toggle", methods=["POST"])
@admin_required
@csrf_protect
def admin_toggle_product(pid):
    p = col_products.find_one({"_id": pid})
    if p:
        new_state = not p.get("enabled", True)
        col_products.update_one({"_id": pid}, {"$set": {"enabled": new_state}})
        log_admin(f"{'Bật' if new_state else 'Ẩn'} sản phẩm '{p.get('name','')}' (id={pid})")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<pid>/pin", methods=["POST"])
@admin_required
@csrf_protect
def admin_pin_product(pid):
    p = col_products.find_one({"_id": pid})
    if p:
        new_state = not p.get("pinned", False)
        col_products.update_one({"_id": pid}, {"$set": {"pinned": new_state}})
        log_admin(f"{'Ghim' if new_state else 'Bỏ ghim'} sản phẩm '{p.get('name','')}' (id={pid})")
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<pid>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_product(pid):
    p = col_products.find_one({"_id": pid})
    col_products.delete_one({"_id": pid})
    stock_removed = col_stock.delete_many({"product_id": pid}).deleted_count
    if p:
        log_admin(f"Xoá sản phẩm '{p.get('name','')}' (id={pid}) + {stock_removed} code trong kho")
    flash("Đã xoá sản phẩm và toàn bộ kho code liên quan.", "success")
    return redirect(url_for("admin_products"))


# ══════════════════════════════════════════════════════════════
#  ADMIN — BRAND (B) — quản lý thương hiệu động
# ══════════════════════════════════════════════════════════════
@app.route("/admin/brands", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_brands():
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "add":
            key   = request.form.get("key", "").strip().lower()
            label = request.form.get("label", "").strip()
            icon  = request.form.get("icon", "").strip()
            order = request.form.get("order", "99")
            try:
                order = int(order)
            except ValueError:
                order = 99
            cover_url = resolve_image_field(
                form_url_key="cover_url", file_field_key="cover_file",
                subdir="brands", old_value="",
            )
            icon_image_url = resolve_image_field(
                form_url_key="icon_image_url", file_field_key="icon_image_file",
                subdir="brands", old_value="",
            )
            if not re.fullmatch(r"[a-z0-9_]{2,20}", key):
                flash("Mã brand chỉ gồm chữ thường/số/gạch dưới (2-20 ký tự).", "error")
            elif not label:
                flash("Tên hiển thị bắt buộc.", "error")
            elif col_brands.find_one({"_id": key}):
                flash("Mã brand đã tồn tại.", "error")
            else:
                col_brands.insert_one({
                    "_id": key, "label": label, "icon": icon or "fas fa-mobile-alt",
                    "cover_url": cover_url, "icon_image_url": icon_image_url,
                    "order": order, "enabled": True,
                    "created_at": datetime.now(timezone.utc),
                })
                log_admin(f"Thêm brand '{label}' (key={key})")
                flash(f"Đã thêm brand: {label}", "success")
        return redirect(url_for("admin_brands"))

    items = list(col_brands.find().sort("order", 1))
    for b in items:
        b["product_count"] = col_products.count_documents({"brand": b["_id"]})
    return render_template("admin_brands.html", items=items)


@app.route("/admin/brands/<key>/edit", methods=["POST"])
@admin_required
@csrf_protect
def admin_edit_brand(key):
    b = col_brands.find_one({"_id": key})
    if not b:
        flash("Brand không tồn tại.", "error")
        return redirect(url_for("admin_brands"))
    label = request.form.get("label", "").strip()
    icon  = request.form.get("icon", "").strip()
    order = request.form.get("order", "99")
    try:
        order = int(order)
    except ValueError:
        order = 99
    cover_url = resolve_image_field(
        form_url_key="cover_url", file_field_key="cover_file",
        subdir="brands", old_value=b.get("cover_url", ""),
    )
    if request.form.get("remove_icon_image") == "1":
        icon_image_url = ""
    else:
        icon_image_url = resolve_image_field(
            form_url_key="icon_image_url", file_field_key="icon_image_file",
            subdir="brands", old_value=b.get("icon_image_url", ""),
        )
    if not label:
        flash("Tên hiển thị bắt buộc.", "error")
    else:
        col_brands.update_one({"_id": key}, {"$set": {
            "label": label, "icon": icon or "fas fa-mobile-alt",
            "order": order, "cover_url": cover_url, "icon_image_url": icon_image_url,
        }})
        log_admin(f"Sửa brand '{label}' (key={key})")
        flash("Đã cập nhật brand.", "success")
    return redirect(url_for("admin_brands"))


@app.route("/admin/brands/<key>/toggle", methods=["POST"])
@admin_required
@csrf_protect
def admin_toggle_brand(key):
    b = col_brands.find_one({"_id": key})
    if b:
        new_state = not b.get("enabled", True)
        col_brands.update_one({"_id": key}, {"$set": {"enabled": new_state}})
        log_admin(f"{'Bật' if new_state else 'Ẩn'} brand '{b.get('label','')}' (key={key})")
    return redirect(url_for("admin_brands"))


@app.route("/admin/brands/<key>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_brand(key):
    """Chặn xoá khi còn sản phẩm gắn brand (bảo vệ tính toàn vẹn)."""
    b = col_brands.find_one({"_id": key})
    if not b:
        flash("Brand không tồn tại.", "error")
        return redirect(url_for("admin_brands"))
    cnt = col_products.count_documents({"brand": key})
    if cnt > 0:
        flash(f"Không xoá được: brand '{b['label']}' còn {cnt} sản phẩm gắn vào. "
              f"Vui lòng đổi brand hoặc xoá các sản phẩm đó trước.", "error")
        return redirect(url_for("admin_brands"))
    col_brands.delete_one({"_id": key})
    log_admin(f"Xoá brand '{b.get('label','')}' (key={key})")
    flash(f"Đã xoá brand: {b['label']}", "success")
    return redirect(url_for("admin_brands"))


# ══════════════════════════════════════════════════════════════
#  ADMIN — KHO CODE (C): xoá, xoá batch, export CSV
# ══════════════════════════════════════════════════════════════
@app.route("/admin/stock/<pid>", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_stock(pid):
    p = col_products.find_one({"_id": pid})
    if not p:
        flash("Sản phẩm không tồn tại.", "error")
        return redirect(url_for("admin_products"))

    if request.method == "POST":
        raw = request.form.get("codes", "")
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if lines:
            col_stock.insert_many([
                {"_id": uuid.uuid4().hex[:12], "product_id": pid, "code": c,
                 "sold": False, "created_at": datetime.now(timezone.utc)}
                for c in lines
            ])
            log_admin(f"Nhập {len(lines)} code vào kho SP '{p.get('name','')}' (id={pid})")
            flash(f"Đã nhập {len(lines)} code vào kho.", "success")
        return redirect(url_for("admin_stock", pid=pid))

    remaining = list(col_stock.find({"product_id": pid, "sold": False}).sort("created_at", 1))
    sold_count = col_stock.count_documents({"product_id": pid, "sold": True})
    return render_template("admin_stock.html", p=p, remaining=remaining, sold_count=sold_count)


@app.route("/admin/stock/<pid>/code/<sid>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_stock_code(pid, sid):
    """Xoá 1 code chưa bán."""
    c = col_stock.find_one({"_id": sid, "product_id": pid, "sold": False})
    if not c:
        flash("Code không tồn tại hoặc đã bán.", "error")
    else:
        col_stock.delete_one({"_id": sid})
        log_admin(f"Xoá 1 code kho ({sid[:8]}...) khỏi SP id={pid}")
        flash("Đã xoá code.", "success")
    return redirect(url_for("admin_stock", pid=pid))


@app.route("/admin/stock/<pid>/clear", methods=["POST"])
@admin_required
@csrf_protect
def admin_clear_stock(pid):
    """Xoá toàn bộ code CHƯA BÁN (giữ nguyên các code đã bán để hoá đơn cũ còn truy vết)."""
    removed = col_stock.delete_many({"product_id": pid, "sold": False}).deleted_count
    log_admin(f"Xoá toàn bộ {removed} code chưa bán khỏi SP id={pid}")
    flash(f"Đã xoá {removed} code chưa bán.", "success")
    return redirect(url_for("admin_stock", pid=pid))


@app.route("/admin/stock/<pid>/export.csv")
@admin_required
def admin_export_stock(pid):
    p = col_products.find_one({"_id": pid})
    if not p:
        abort(404)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["code", "sold", "sold_to", "sold_at", "created_at"])
    for c in col_stock.find({"product_id": pid}).sort("created_at", 1):
        w.writerow([
            c.get("code", ""),
            "1" if c.get("sold") else "0",
            c.get("sold_to", "") or "",
            (c.get("sold_at").isoformat() if c.get("sold_at") else ""),
            (c.get("created_at").isoformat() if c.get("created_at") else ""),
        ])
    log_admin(f"Xuất CSV kho SP '{p.get('name','')}' (id={pid})")
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="stock-{pid}.csv"'}
    )


# ══════════════════════════════════════════════════════════════
#  ADMIN — ĐƠN HÀNG (E,F): filter, phân trang, xử lý, cấp lại code
# ══════════════════════════════════════════════════════════════
@app.route("/admin/orders")
@admin_required
def admin_orders():
    page, per = parse_page(default_per=30)
    status = request.args.get("status", "").strip()
    q      = request.args.get("q", "").strip()
    from_dt, to_dt = parse_date_range()

    query = {}
    if status in ("pending", "completed", "cancelled"):
        query["status"] = status
    if from_dt or to_dt:
        rng = {}
        if from_dt: rng["$gte"] = from_dt
        if to_dt:   rng["$lt"]  = to_dt
        query["created_at"] = rng
    # Tìm theo username
    if q:
        matched_uids = [u["_id"] for u in col_users.find(
            {"username": {"$regex": re.escape(q), "$options": "i"}}, {"_id": 1}
        )]
        # Hoặc theo tên sản phẩm / order id
        query = {"$and": [query, {"$or": [
            {"user_id": {"$in": matched_uids}},
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"_id": q},
        ]}]}

    total = col_orders.count_documents(query)
    items = list(col_orders.find(query).sort("created_at", -1).skip((page-1)*per).limit(per))
    users = {u["_id"]: u["username"] for u in col_users.find(
        {"_id": {"$in": [o["user_id"] for o in items]}}, {"username": 1}
    )}
    total_pages = max(1, (total + per - 1) // per)
    return render_template("admin_orders.html",
                           orders=items, users=users,
                           page=page, per=per, total=total, total_pages=total_pages,
                           status=status, q=q,
                           from_str=request.args.get("from",""), to_str=request.args.get("to",""))


@app.route("/admin/orders/<order_id>/update", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_order_update(order_id):
    o = col_orders.find_one({"_id": order_id})
    if not o:
        flash("Đơn hàng không tồn tại.", "error")
        return redirect(url_for("admin_orders"))

    if request.method == "POST":
        new_status = request.form.get("status", o.get("status", "pending"))
        account_info = request.form.get("account_info", "").strip()
        account_password = request.form.get("account_password", "").strip()
        admin_note = request.form.get("admin_note", "").strip()

        update = {
            "status": new_status,
            "account_info": account_info,
            "account_password": account_password,
            "admin_note": admin_note,
            "updated_at": datetime.now(timezone.utc),
        }

        # Huỷ đơn (chưa từng huỷ trước đó) -> tự động hoàn tiền lại ví khách
        if new_status == "cancelled" and o.get("status") != "cancelled":
            col_users.update_one({"_id": o["user_id"]}, {"$inc": {"balance": o["price"]}})
            # Nếu đơn là auto (đã lấy code trong kho) -> "trả code" về kho để bán lại
            if o.get("fulfillment") == "auto" and o.get("code_stock_id"):
                col_stock.update_one(
                    {"_id": o["code_stock_id"]},
                    {"$set": {"sold": False}, "$unset": {"sold_to": "", "sold_at": ""}}
                )
            log_admin(f"Huỷ đơn {order_id} & hoàn {o['price']:,}đ cho {o.get('user_id','?')}")
            flash(f"Đã huỷ đơn và hoàn {o['price']:,}đ vào ví khách.", "success")
        else:
            log_admin(f"Cập nhật đơn {order_id} sang status={new_status}")
            flash("Đã cập nhật đơn hàng.", "success")

        col_orders.update_one({"_id": order_id}, {"$set": update})
        return redirect(url_for("admin_orders"))

    return render_template("admin_order_update.html", o=o)


@app.route("/admin/orders/<order_id>/reissue", methods=["POST"])
@admin_required
@csrf_protect
def admin_reissue_code(order_id):
    """F: cấp lại 1 code khác cho đơn auto (khi code cũ bị lỗi / bị lộ / đã dùng)."""
    o = col_orders.find_one({"_id": order_id})
    if not o or o.get("fulfillment") != "auto":
        flash("Chỉ đơn auto mới cấp lại code được.", "error")
        return redirect(url_for("admin_orders"))
    new_code = col_stock.find_one_and_update(
        {"product_id": o["product_id"], "sold": False},
        {"$set": {"sold": True, "sold_to": o["user_id"], "sold_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not new_code:
        flash("Kho đã hết code, không cấp lại được. Vui lòng nhập thêm kho trước.", "error")
        return redirect(url_for("admin_orders"))
    old_code = o.get("code", "")
    old_sid  = o.get("code_stock_id")
    col_orders.update_one({"_id": order_id}, {"$set": {
        "code": new_code["code"],
        "code_stock_id": new_code["_id"],
        "admin_note": (o.get("admin_note", "") + f"\n[reissue] Code cũ: {old_code}").strip(),
        "updated_at": datetime.now(timezone.utc),
    }})
    # Đánh dấu code cũ là "voided" để không nhầm với "sold" thật
    if old_sid:
        col_stock.update_one({"_id": old_sid}, {"$set": {"voided": True, "voided_at": datetime.now(timezone.utc)}})
    log_admin(f"Cấp lại code mới cho đơn {order_id}: {old_code} -> {new_code['code']}")
    flash("Đã cấp lại code mới cho đơn.", "success")
    return redirect(url_for("admin_orders"))


@app.route("/admin/orders/<order_id>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_order(order_id):
    """Xoá hoàn toàn 1 đơn (test/spam). Không hoàn tiền — dùng huỷ nếu cần hoàn tiền."""
    o = col_orders.find_one({"_id": order_id})
    if o:
        col_orders.delete_one({"_id": order_id})
        log_admin(f"Xoá đơn {order_id} ({o.get('product_name','')}, {o.get('price',0):,}đ)")
        flash("Đã xoá đơn.", "success")
    return redirect(url_for("admin_orders"))


@app.route("/admin/orders/export.csv")
@admin_required
def admin_export_orders():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["order_id", "created_at", "user_id", "product_name", "brand", "price",
                "status", "fulfillment", "code", "account_info"])
    for o in col_orders.find().sort("created_at", -1):
        w.writerow([
            o.get("_id",""),
            (o.get("created_at").isoformat() if o.get("created_at") else ""),
            o.get("user_id",""),
            o.get("product_name",""),
            o.get("brand",""),
            o.get("price",0),
            o.get("status",""),
            o.get("fulfillment",""),
            o.get("code",""),
            o.get("account_info",""),
        ])
    log_admin("Xuất CSV toàn bộ đơn hàng")
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="orders.csv"'}
    )


# ══════════════════════════════════════════════════════════════
#  ADMIN — HOẠT ĐỘNG (E): filter + phân trang
# ══════════════════════════════════════════════════════════════
@app.route("/admin/activity")
@admin_required
def admin_activity():
    page, per = parse_page(default_per=50)
    q = request.args.get("q", "").strip()
    from_dt, to_dt = parse_date_range()
    query = {}
    if q:
        query["$or"] = [
            {"username": {"$regex": re.escape(q), "$options": "i"}},
            {"content":  {"$regex": re.escape(q), "$options": "i"}},
            {"ip":       {"$regex": re.escape(q), "$options": "i"}},
        ]
    if from_dt or to_dt:
        rng = {}
        if from_dt: rng["$gte"] = from_dt
        if to_dt:   rng["$lt"]  = to_dt
        query["created_at"] = rng
    total = col_activity.count_documents(query)
    items = list(col_activity.find(query).sort("created_at", -1).skip((page-1)*per).limit(per))
    total_pages = max(1, (total + per - 1) // per)
    return render_template("admin_activity.html",
                           items=items, page=page, per=per, total=total,
                           total_pages=total_pages, q=q,
                           from_str=request.args.get("from",""), to_str=request.args.get("to",""))


# ══════════════════════════════════════════════════════════════
#  ADMIN — NẠP TIỀN (E): filter + phân trang
# ══════════════════════════════════════════════════════════════
@app.route("/admin/recharges")
@admin_required
def admin_recharges():
    page, per = parse_page(default_per=50)
    q = request.args.get("q", "").strip()
    from_dt, to_dt = parse_date_range()
    query = {}
    if q:
        query["$or"] = [
            {"username": {"$regex": re.escape(q), "$options": "i"}},
            {"user_id":  {"$regex": re.escape(q), "$options": "i"}},
            {"bank":     {"$regex": re.escape(q), "$options": "i"}},
        ]
    if from_dt or to_dt:
        rng = {}
        if from_dt: rng["$gte"] = from_dt
        if to_dt:   rng["$lt"]  = to_dt
        query["created_at"] = rng
    total = col_recharges.count_documents(query)
    items = list(col_recharges.find(query).sort("created_at", -1).skip((page-1)*per).limit(per))
    total_pages = max(1, (total + per - 1) // per)
    return render_template("admin_recharges.html",
                           items=items, page=page, per=per, total=total,
                           total_pages=total_pages, q=q,
                           from_str=request.args.get("from",""), to_str=request.args.get("to",""))


@app.route("/admin/recharges/export.csv")
@admin_required
def admin_export_recharges():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["_id", "created_at", "user_id", "username", "amount", "bank", "source", "note"])
    for r in col_recharges.find().sort("created_at", -1):
        w.writerow([
            r.get("_id",""),
            (r.get("created_at").isoformat() if r.get("created_at") else ""),
            r.get("user_id",""),
            r.get("username",""),
            r.get("amount",0),
            r.get("bank",""),
            r.get("source",""),
            r.get("note",""),
        ])
    log_admin("Xuất CSV lịch sử nạp tiền")
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="recharges.csv"'}
    )


# ══════════════════════════════════════════════════════════════
#  ADMIN — REFERRALS (H): xem hoa hồng đã trả
# ══════════════════════════════════════════════════════════════
@app.route("/admin/referrals")
@admin_required
def admin_referrals():
    agg = list(col_users.aggregate([
        {"$match": {"referred_by": {"$ne": None}}},
        {"$group": {"_id": "$referred_by", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]))
    referrer_map = {u["_id"]: u
                    for u in col_users.find({"_id": {"$in": [a["_id"] for a in agg]}},
                                             {"username": 1, "referral_earned": 1})}
    top_referrers = [{
        "username": referrer_map.get(a["_id"], {}).get("username", a["_id"]),
        "count": a["count"],
        "earned": referrer_map.get(a["_id"], {}).get("referral_earned", 0),
    } for a in agg]

    referred_users = list(col_users.find({"referred_by": {"$ne": None}}).sort("created_at", -1).limit(200))
    for r in referred_users:
        parent = col_users.find_one({"_id": r["referred_by"]}, {"username": 1})
        r["referrer_name"] = parent["username"] if parent else r["referred_by"]

    return render_template("admin_referrals.html",
                           top_referrers=top_referrers, referred_users=referred_users)


# ══════════════════════════════════════════════════════════════
#  ADMIN — USERS (D + E): action đa năng, filter, ghi log
# ══════════════════════════════════════════════════════════════
@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_users():
    if request.method == "POST":
        me = current_user()
        action = request.form.get("action", "adjust")
        uid    = request.form.get("uid", "")
        target = col_users.find_one({"_id": uid})
        if not target:
            flash("User không tồn tại.", "error")
            return redirect(url_for("admin_users"))
        # Không cho tự bấm khoá / hạ cấp chính mình
        is_self = target["_id"] == me["_id"]

        if action == "adjust":
            try:
                delta = int(request.form.get("delta", "0"))
            except ValueError:
                delta = 0
            note = request.form.get("note", "").strip() or "Điều chỉnh thủ công"
            if delta == 0:
                flash("Số tiền không hợp lệ.", "error")
                return redirect(url_for("admin_users"))
            # Chặn cộng/trừ quá lớn (bảo vệ khỏi typo)
            if abs(delta) > 1_000_000_000:
                flash("Số tiền quá lớn (giới hạn 1 tỷ/lần).", "error")
                return redirect(url_for("admin_users"))
            new_bal = max(0, int(target.get("balance", 0) + delta))
            col_users.update_one({"_id": uid}, {"$set": {"balance": new_bal}})
            # Ghi vào recharges để có audit + xuất hiện ở trang lịch sử
            col_recharges.insert_one({
                "_id": uuid.uuid4().hex[:12],
                "user_id": uid,
                "username": target.get("username", uid),
                "amount": delta,
                "bank": "Điều chỉnh thủ công",
                "source": "manual",
                "note": note,
                "admin": me.get("username", ""),
                "created_at": datetime.now(timezone.utc),
            })
            log_admin(f"Điều chỉnh số dư {target.get('username','')} ({uid}): {delta:+,}đ — {note}")
            flash(f"Đã điều chỉnh số dư của {target.get('username','')}: {delta:+,}đ.", "success")

        elif action == "toggle_role":
            if is_self:
                flash("Không thể tự đổi vai trò của chính mình.", "error")
            else:
                new_role = "user" if target.get("role") == "admin" else "admin"
                col_users.update_one({"_id": uid}, {"$set": {"role": new_role}})
                log_admin(f"Đổi vai trò {target.get('username','')} -> {new_role}")
                flash(f"Đã đổi vai trò của {target.get('username','')} thành {new_role}.", "success")

        elif action == "toggle_block":
            if is_self:
                flash("Không thể tự khoá chính mình.", "error")
            else:
                new_block = not target.get("blocked", False)
                col_users.update_one({"_id": uid}, {"$set": {"blocked": new_block}})
                log_admin(f"{'Khoá' if new_block else 'Mở khoá'} tài khoản {target.get('username','')}")
                flash(f"{'Đã khoá' if new_block else 'Đã mở khoá'} tài khoản {target.get('username','')}.", "success")

        elif action == "reset_password":
            new_pw = request.form.get("new_password", "").strip()
            if len(new_pw) < 6:
                flash("Mật khẩu mới phải từ 6 ký tự.", "error")
            else:
                col_users.update_one({"_id": uid}, {"$set": {"password_hash": generate_password_hash(new_pw)}})
                log_admin(f"Reset mật khẩu cho {target.get('username','')}")
                flash(f"Đã reset mật khẩu cho {target.get('username','')}.", "success")

        elif action == "delete":
            if is_self:
                flash("Không thể tự xoá chính mình.", "error")
            elif target.get("balance", 0) > 0:
                flash("Không xoá được user còn số dư >0 (chuyển tiền ra hoặc trừ về 0 trước).", "error")
            else:
                col_users.delete_one({"_id": uid})
                log_admin(f"Xoá tài khoản {target.get('username','')} ({uid})")
                flash(f"Đã xoá tài khoản {target.get('username','')}.", "success")

        return redirect(url_for("admin_users"))

    # ---- GET ----
    page, per = parse_page(default_per=30)
    q = request.args.get("q", "").strip()
    role_f = request.args.get("role", "").strip()
    query = {}
    if q:
        query["$or"] = [
            {"username": {"$regex": re.escape(q), "$options": "i"}},
            {"_id":      q},
        ]
    if role_f in ("admin", "user"):
        query["role"] = role_f
    total = col_users.count_documents(query)
    items = list(col_users.find(query).sort("created_at", -1).skip((page-1)*per).limit(per))
    total_pages = max(1, (total + per - 1) // per)
    return render_template("admin_users.html",
                           items=items, page=page, per=per, total=total,
                           total_pages=total_pages, q=q, role_f=role_f)


@app.route("/admin/users/export.csv")
@admin_required
def admin_export_users():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["_id", "username", "role", "balance", "blocked", "referred_by",
                "referral_earned", "created_at"])
    for u in col_users.find().sort("created_at", -1):
        w.writerow([
            u.get("_id",""), u.get("username",""), u.get("role",""),
            u.get("balance",0), 1 if u.get("blocked") else 0,
            u.get("referred_by","") or "",
            u.get("referral_earned",0),
            (u.get("created_at").isoformat() if u.get("created_at") else ""),
        ])
    log_admin("Xuất CSV danh sách user")
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="users.csv"'}
    )


# ══════════════════════════════════════════════════════════════
#  BÀI VIẾT (F): thêm/sửa/xoá
# ══════════════════════════════════════════════════════════════
@app.route("/tin-tuc")
def posts():
    items = list(col_posts.find().sort("created_at", -1).limit(50))
    return render_template("posts.html", items=items)


@app.route("/admin/posts", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_posts():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("Vui lòng nhập đủ tiêu đề và nội dung.", "error")
        else:
            pid = uuid.uuid4().hex[:10]
            col_posts.insert_one({
                "_id": pid,
                "title": title, "content": content,
                "created_at": datetime.now(timezone.utc),
            })
            log_admin(f"Đăng bài viết '{title}' (id={pid})")
            flash("Đã đăng bài viết.", "success")
        return redirect(url_for("admin_posts"))

    items = list(col_posts.find().sort("created_at", -1))
    return render_template("admin_posts.html", items=items)


@app.route("/admin/posts/<post_id>/edit", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_edit_post(post_id):
    p = col_posts.find_one({"_id": post_id})
    if not p:
        flash("Bài viết không tồn tại.", "error")
        return redirect(url_for("admin_posts"))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("Vui lòng nhập đủ tiêu đề và nội dung.", "error")
            return redirect(url_for("admin_edit_post", post_id=post_id))
        col_posts.update_one({"_id": post_id}, {"$set": {
            "title": title, "content": content,
            "updated_at": datetime.now(timezone.utc),
        }})
        log_admin(f"Sửa bài viết '{title}' (id={post_id})")
        flash("Đã cập nhật bài viết.", "success")
        return redirect(url_for("admin_posts"))
    return render_template("admin_post_edit.html", p=p)


@app.route("/admin/posts/<post_id>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_post(post_id):
    p = col_posts.find_one({"_id": post_id})
    col_posts.delete_one({"_id": post_id})
    if p:
        log_admin(f"Xoá bài viết '{p.get('title','')}' (id={post_id})")
    flash("Đã xoá bài viết.", "success")
    return redirect(url_for("admin_posts"))


# ══════════════════════════════════════════════════════════════
#  ADMIN — CẤU HÌNH (mở rộng: liên hệ, threshold, hoa hồng ref)
# ══════════════════════════════════════════════════════════════
@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_settings():
    if request.method == "POST":
        try:
            low_thr = int(request.form.get("low_stock_threshold", "5"))
        except ValueError:
            low_thr = 5
        try:
            ref_pct = float(request.form.get("referral_percent", "5"))
        except ValueError:
            ref_pct = 5
        try:
            min_dep = int(request.form.get("min_deposit", "0"))
        except ValueError:
            min_dep = 0
        old_cfg = get_config()
        logo_url    = resolve_image_field("logo_url",    "logo_file",    "branding", old_cfg.get("logo_url", ""))
        favicon_url = resolve_image_field("favicon_url", "favicon_file", "branding", old_cfg.get("favicon_url", ""))
        wallet_bg   = resolve_image_field("wallet_bg_url","wallet_bg_file","branding", old_cfg.get("wallet_bg_url", ""))
        col_config.update_one({"_id": "main"}, {"$set": {
            "site_name":           request.form.get("site_name", "CloudShop").strip(),
            "sepay_api_key":       request.form.get("sepay_api_key", "").strip(),
            "bank_bin":            request.form.get("bank_bin", "").strip(),
            "bank_account_number": request.form.get("bank_account_number", "").strip(),
            "bank_account_name":   request.form.get("bank_account_name", "").strip(),
            "announcement":        request.form.get("announcement", "").strip(),
            "contact_zalo":        request.form.get("contact_zalo", "").strip(),
            "contact_telegram":    request.form.get("contact_telegram", "").strip(),
            "low_stock_threshold": max(0, low_thr),
            "referral_percent":    max(0.0, ref_pct),
            "referral_on_recharge": request.form.get("referral_on_recharge") == "1",
            "min_deposit":         max(0, min_dep),
            "logo_url":            logo_url,
            "favicon_url":         favicon_url,
            "wallet_bg_url":       wallet_bg,
            # v5
            "telegram_bot_token":  request.form.get("telegram_bot_token", "").strip(),
            "telegram_chat_id":    request.form.get("telegram_chat_id", "").strip(),
            "captcha_enabled":     request.form.get("captcha_enabled") == "1",
            "reviews_enabled":     request.form.get("reviews_enabled") == "1",
            "seo_description":     request.form.get("seo_description", "").strip()[:300],
            "seo_keywords":        request.form.get("seo_keywords", "").strip()[:200],
            "purchase_limit_per_user": max(0, int(request.form.get("purchase_limit_per_user","0") or 0)),
            "purchase_limit_per_day":  max(0, int(request.form.get("purchase_limit_per_day","0") or 0)),
            "gachthefast_enabled":     request.form.get("gachthefast_enabled") == "1",
            "gachthefast_domain":      request.form.get("gachthefast_domain", "gachthefast.com").strip(),
            "gachthefast_partner_id":  request.form.get("gachthefast_partner_id", "").strip(),
            "gachthefast_secret_key":  request.form.get("gachthefast_secret_key", "").strip(),
            "background_music_url":    request.form.get("background_music_url", "").strip(),
        }}, upsert=True)
        log_admin("Cập nhật cấu hình hệ thống")
        flash("Đã lưu cấu hình.", "success")
        return redirect(url_for("admin_settings"))
    webhook_url = request.host_url.rstrip("/") + "/sepay-webhook"
    gachthefast_callback_url = request.host_url.rstrip("/") + "/charge/callback"
    api_base = request.host_url.rstrip("/") + "/api/v1"
    return render_template("admin_settings.html",
                           cfg=get_config(), webhook_url=webhook_url, api_base=api_base,
                           gachthefast_callback_url=gachthefast_callback_url,
                           has_pyotp=_HAS_PYOTP, has_qrcode=_HAS_QRCODE)


@app.route("/admin/api-key/regen", methods=["POST"])
@admin_required
@csrf_protect
def admin_regen_api_key():
    new_key = secrets.token_urlsafe(36)
    col_config.update_one({"_id": "main"}, {"$set": {"bot_api_key": new_key}})
    log_admin("Tạo mới API key cho bot Discord")
    flash("Đã tạo API key mới. Copy ngay — key cũ đã vô hiệu.", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/api-key/clear", methods=["POST"])
@admin_required
@csrf_protect
def admin_clear_api_key():
    col_config.update_one({"_id": "main"}, {"$set": {"bot_api_key": ""}})
    log_admin("Xoá API key bot (tắt API)")
    flash("Đã xoá API key. API bot Discord đã tắt.", "success")
    return redirect(url_for("admin_settings"))


# ══════════════════════════════════════════════════════════════
#  v5 A — COUPON admin CRUD  (/admin/coupons)
# ══════════════════════════════════════════════════════════════
@app.route("/admin/coupons", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_coupons():
    if request.method == "POST":
        code  = (request.form.get("code", "") or "").strip().upper()
        kind  = request.form.get("kind", "percent")
        try:
            value = float(request.form.get("value", "0"))
        except ValueError:
            value = 0
        try:
            max_uses = int(request.form.get("max_uses", "0") or "0")
        except ValueError:
            max_uses = 0
        try:
            per_user_limit = int(request.form.get("per_user_limit", "0") or "0")
        except ValueError:
            per_user_limit = 0
        try:
            min_amount = int(request.form.get("min_amount", "0") or "0")
        except ValueError:
            min_amount = 0
        expires_at = (request.form.get("expires_at", "") or "").strip()
        exp_dt = None
        if expires_at:
            try:
                exp_dt = datetime.strptime(expires_at, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                exp_dt = None
        if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
            flash("Mã coupon chỉ chứa chữ hoa/số/gạch dưới/gạch nối (3-32 ký tự).", "error")
        elif kind not in ("percent", "fixed") or value <= 0:
            flash("Kiểu / giá trị coupon không hợp lệ.", "error")
        elif col_coupons.find_one({"code": code}):
            flash("Mã coupon đã tồn tại.", "error")
        else:
            col_coupons.insert_one({
                "_id": uuid.uuid4().hex[:10],
                "code": code, "kind": kind, "value": value,
                "max_uses": max_uses, "uses": 0,
                "per_user_limit": per_user_limit, "min_amount": min_amount,
                "expires_at": exp_dt, "enabled": True,
                "used_by": {},
                "created_at": datetime.now(timezone.utc),
            })
            log_admin(f"Tạo coupon {code} ({kind} {value})")
            flash(f"Đã tạo coupon: {code}", "success")
        return redirect(url_for("admin_coupons"))
    items = list(col_coupons.find().sort("created_at", -1))
    return render_template("admin_coupons.html", items=items)


@app.route("/admin/coupons/<cid>/toggle", methods=["POST"])
@admin_required
@csrf_protect
def admin_toggle_coupon(cid):
    c = col_coupons.find_one({"_id": cid})
    if c:
        new_state = not c.get("enabled", True)
        col_coupons.update_one({"_id": cid}, {"$set": {"enabled": new_state}})
        log_admin(f"{'Bật' if new_state else 'Tắt'} coupon {c.get('code','')}")
    return redirect(url_for("admin_coupons"))


@app.route("/admin/coupons/<cid>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_coupon(cid):
    c = col_coupons.find_one({"_id": cid})
    if c:
        col_coupons.delete_one({"_id": cid})
        log_admin(f"Xoá coupon {c.get('code','')}")
        flash("Đã xoá coupon.", "success")
    return redirect(url_for("admin_coupons"))


@app.route("/api/v1/coupon/check", methods=["POST"])
@login_required
@csrf_protect
def api_coupon_check():
    """Endpoint AJAX: user gõ coupon ở form buy để preview số tiền được giảm."""
    u = current_user()
    code = (request.form.get("code", "") or "").strip().upper()
    try:
        base = int(request.form.get("amount", "0"))
    except ValueError:
        base = 0
    c, d = _find_valid_coupon(code, u["_id"], base)
    if not c:
        return jsonify({"ok": False, "message": "Mã không hợp lệ / đã hết hạn / không đủ điều kiện."})
    return jsonify({"ok": True, "discount": d, "final": max(0, base - d),
                    "kind": c["kind"], "value": c["value"]})


# ══════════════════════════════════════════════════════════════
#  v5 C — BACKUP / RESTORE MongoDB (JSON) — chỉ super admin
# ══════════════════════════════════════════════════════════════
_BACKUP_COLLECTIONS = ["users", "products", "stock_codes", "orders", "config",
                       "recharges", "activity_log", "posts", "brands",
                       "coupons", "reviews", "notifications"]

def _json_default(o):
    from bson import ObjectId  # pymongo dependency, luôn có
    if isinstance(o, datetime):
        return {"__dt": o.isoformat()}
    if isinstance(o, ObjectId):
        return {"__oid": str(o)}
    return str(o)

def _json_reviver(obj):
    if isinstance(obj, dict):
        if "__dt" in obj and len(obj) == 1:
            try:
                return datetime.fromisoformat(obj["__dt"])
            except ValueError:
                return obj["__dt"]
        if "__oid" in obj and len(obj) == 1:
            try:
                from bson import ObjectId
                return ObjectId(obj["__oid"])
            except Exception:
                return obj["__oid"]
    return obj


@app.route("/admin/backup/export")
@admin_required("*" if False else None)  # dùng admin_required trơn, kiểm super trong body
def admin_backup_export():
    u = current_user()
    if u.get("admin_role", "super") != "super":
        flash("Chỉ Super Admin mới được export toàn bộ database.", "error")
        return redirect(url_for("admin_settings"))
    dump = {}
    for cname in _BACKUP_COLLECTIONS:
        try:
            dump[cname] = list(db[cname].find())
        except Exception as e:
            dump[cname] = []
            print(f"[backup] {cname}: {e}")
    payload = json.dumps({
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "site": get_config().get("site_name", "CloudShop"),
        "data": dump,
    }, ensure_ascii=False, default=_json_default, indent=2)
    fname = f"cloudshop-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    log_admin(f"Xuất backup toàn bộ database ({len(payload)//1024} KB)")
    return Response(
        payload, mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


@app.route("/admin/backup", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_backup():
    u = current_user()
    is_super = u.get("admin_role", "super") == "super"
    if request.method == "POST":
        if not is_super:
            flash("Chỉ Super Admin mới được restore.", "error")
            return redirect(url_for("admin_backup"))
        mode = request.form.get("mode", "merge")   # merge|replace
        confirm = request.form.get("confirm_text", "")
        if confirm != "RESTORE":
            flash("Cần gõ RESTORE để xác nhận thao tác nguy hiểm.", "error")
            return redirect(url_for("admin_backup"))
        f = request.files.get("backup_file")
        if not f or not f.filename:
            flash("Chưa chọn file backup.", "error")
            return redirect(url_for("admin_backup"))
        try:
            raw = f.read().decode("utf-8")
            payload = json.loads(raw, object_hook=_json_reviver)
        except Exception as e:
            flash(f"File backup không đọc được: {e}", "error")
            return redirect(url_for("admin_backup"))
        data = payload.get("data") or {}
        applied = {}
        for cname, docs in data.items():
            if cname not in _BACKUP_COLLECTIONS:
                continue
            coll = db[cname]
            if mode == "replace":
                coll.delete_many({})
            if not docs:
                applied[cname] = 0; continue
            if mode == "merge":
                # upsert theo _id
                ops = 0
                for d in docs:
                    _id = d.get("_id")
                    if _id is None:
                        continue
                    coll.replace_one({"_id": _id}, d, upsert=True)
                    ops += 1
                applied[cname] = ops
            else:
                coll.insert_many(docs, ordered=False)
                applied[cname] = len(docs)
        log_admin(f"Restore backup (mode={mode}): {applied}")
        flash(f"Đã restore. Kết quả: {applied}", "success")
        return redirect(url_for("admin_backup"))
    stats = {c: db[c].count_documents({}) for c in _BACKUP_COLLECTIONS}
    return render_template("admin_backup.html", stats=stats, is_super=is_super)


# ══════════════════════════════════════════════════════════════
#  v5 F — Reviews (rating 1-5) cho sản phẩm
#  User đã completed 1 đơn của SP -> có thể review 1 lần / SP.
# ══════════════════════════════════════════════════════════════
def _product_rating(product_id: str):
    agg = list(col_reviews.aggregate([
        {"$match": {"product_id": product_id}},
        {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}},
    ]))
    if not agg:
        return {"avg": 0.0, "count": 0}
    return {"avg": round(float(agg[0]["avg"]), 2), "count": int(agg[0]["n"])}

@app.route("/product/<pid>")
def product_detail(pid):
    p = col_products.find_one({"_id": pid, "enabled": True})
    if not p:
        flash("Sản phẩm không tồn tại.", "error")
        return redirect(url_for("home"))
    if p.get("fulfillment", "auto") != "manual":
        p["stock_count"] = col_stock.count_documents({"product_id": pid, "sold": False})
    else:
        p["stock_count"] = None
    rating = _product_rating(pid)
    reviews = list(col_reviews.find({"product_id": pid}).sort("created_at", -1).limit(50))
    u = current_user()
    can_review = False
    my_review = None
    if u and get_config().get("reviews_enabled", True):
        can_review = col_orders.count_documents({
            "user_id": u["_id"], "product_id": pid, "status": "completed"
        }) > 0
        my_review = col_reviews.find_one({"user_id": u["_id"], "product_id": pid})
    return render_template("product_detail.html", p=p, rating=rating,
                            reviews=reviews, can_review=can_review, my_review=my_review)


@app.route("/product/<pid>/review", methods=["POST"])
@login_required
@csrf_protect
def product_review(pid):
    p = col_products.find_one({"_id": pid})
    if not p:
        return redirect(url_for("home"))
    if not get_config().get("reviews_enabled", True):
        flash("Chức năng đánh giá đang tắt.", "error")
        return redirect(url_for("product_detail", pid=pid))
    u = current_user()
    if col_orders.count_documents({"user_id": u["_id"], "product_id": pid, "status": "completed"}) == 0:
        flash("Bạn cần mua sản phẩm này rồi mới review được.", "error")
        return redirect(url_for("product_detail", pid=pid))
    try:
        rating = int(request.form.get("rating", "5"))
    except ValueError:
        rating = 5
    rating = max(1, min(5, rating))
    content = (request.form.get("content", "") or "").strip()[:500]
    col_reviews.update_one(
        {"user_id": u["_id"], "product_id": pid},
        {"$set": {
            "user_id": u["_id"], "product_id": pid, "username": u["username"],
            "rating": rating, "content": content,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    flash("Đã lưu đánh giá của bạn. Cảm ơn!", "success")
    return redirect(url_for("product_detail", pid=pid))


@app.route("/admin/reviews/<rid>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_review(rid):
    r = col_reviews.find_one({"_id": rid})
    if r:
        col_reviews.delete_one({"_id": rid})
        log_admin(f"Xoá review id={rid}")
        flash("Đã xoá review.", "success")
    return redirect(request.referrer or url_for("home"))


# ══════════════════════════════════════════════════════════════
#  v5 D — Đổi admin_role trong /admin/users (thêm action mới)
# ══════════════════════════════════════════════════════════════
@app.route("/admin/users/<uid>/set-admin-role", methods=["POST"])
@admin_required("users.write")
@csrf_protect
def admin_set_admin_role(uid):
    me = current_user()
    if me.get("admin_role", "super") != "super":
        flash("Chỉ Super Admin được đổi cấp quyền của admin khác.", "error")
        return redirect(url_for("admin_users"))
    target = col_users.find_one({"_id": uid})
    if not target or target.get("role") != "admin":
        flash("User không phải admin.", "error")
        return redirect(url_for("admin_users"))
    new_role = request.form.get("admin_role", "sales")
    if new_role not in ROLE_PERMS:
        flash("Vai trò không hợp lệ.", "error")
        return redirect(url_for("admin_users"))
    if target["_id"] == me["_id"] and new_role != "super":
        flash("Không thể tự hạ cấp Super Admin của chính mình.", "error")
        return redirect(url_for("admin_users"))
    col_users.update_one({"_id": uid}, {"$set": {"admin_role": new_role}})
    log_admin(f"Đổi cấp admin {target['username']} -> {new_role}")
    flash(f"Đã đổi cấp admin của {target['username']} sang {new_role}.", "success")
    return redirect(url_for("admin_users"))


# ══════════════════════════════════════════════════════════════
#  v5 H — SEO: sitemap.xml + robots.txt
# ══════════════════════════════════════════════════════════════
@app.route("/sitemap.xml")
def sitemap():
    urls = [url_for("home", _external=True),
            url_for("posts", _external=True)]
    for p in col_products.find({"enabled": True}, {"_id": 1, "updated_at": 1, "created_at": 1}):
        urls.append(request.host_url.rstrip("/") + url_for("product_detail", pid=p["_id"]))
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml.append(f"<url><loc>{u}</loc></url>")
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    body = "User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /api/\n"
    body += f"Sitemap: {request.host_url.rstrip('/')}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


# ══════════════════════════════════════════════════════════════
#  ADMIN — HOMEPAGE SLIDES (Mức 4) — banner carousel trang chủ
# ══════════════════════════════════════════════════════════════
@app.route("/admin/slides", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_slides():
    cfg = get_config()
    slides = list(cfg.get("homepage_slides") or [])
    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "add":
            title    = request.form.get("title", "").strip()
            subtitle = request.form.get("subtitle", "").strip()
            link     = request.form.get("link", "").strip()
            image_url = resolve_image_field("image_url", "image_file", "slides", "")
            if not image_url:
                flash("Cần chọn ảnh (upload hoặc dán URL).", "error")
            else:
                slides.append({
                    "_id": uuid.uuid4().hex[:8],
                    "title": title, "subtitle": subtitle, "link": link,
                    "image_url": image_url,
                    "order": len(slides) + 1, "enabled": True,
                    "created_at": datetime.now(timezone.utc),
                })
                col_config.update_one({"_id": "main"}, {"$set": {"homepage_slides": slides}})
                log_admin(f"Thêm slide banner: {title or '(không tên)'}")
                flash("Đã thêm slide.", "success")
        elif action == "delete":
            sid = request.form.get("sid", "")
            new_slides = [s for s in slides if s.get("_id") != sid]
            if len(new_slides) != len(slides):
                col_config.update_one({"_id": "main"}, {"$set": {"homepage_slides": new_slides}})
                log_admin(f"Xoá slide banner id={sid}")
                flash("Đã xoá slide.", "success")
        elif action == "toggle":
            sid = request.form.get("sid", "")
            for s in slides:
                if s.get("_id") == sid:
                    s["enabled"] = not s.get("enabled", True)
                    break
            col_config.update_one({"_id": "main"}, {"$set": {"homepage_slides": slides}})
        elif action == "reorder":
            # Nhận order dạng "sid1,sid2,sid3..."
            new_order = request.form.get("order_list", "").split(",")
            new_order = [s.strip() for s in new_order if s.strip()]
            id_map = {s["_id"]: s for s in slides}
            new_slides = [id_map[k] for k in new_order if k in id_map]
            # thêm cả các slide còn lại (nếu client gửi thiếu)
            missing = [s for s in slides if s["_id"] not in new_order]
            new_slides += missing
            for i, s in enumerate(new_slides):
                s["order"] = i + 1
            col_config.update_one({"_id": "main"}, {"$set": {"homepage_slides": new_slides}})
            log_admin("Sắp xếp lại slide banner")
            flash("Đã cập nhật thứ tự slide.", "success")
        return redirect(url_for("admin_slides"))

    slides.sort(key=lambda s: s.get("order", 999))
    return render_template("admin_slides.html", slides=slides)


# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
#  Các route SSE / notifications — đặt cuối vì cần admin_required
# ══════════════════════════════════════════════════════════════
@app.route("/admin/stream")
@admin_required
def admin_stream():
    """Server-Sent Events endpoint — admin mở trang giữ kết nối, nhận push realtime."""
    def gen():
        q = _hub.subscribe()
        try:
            yield f"event: hello\ndata: {json.dumps({'subs': _hub.n_subs()})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"event: notify\ndata: {msg}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            _hub.unsubscribe(q)
    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    """Trả 20 thông báo mới nhất (JSON) cho chuông trên topbar."""
    u = current_user()
    items = list(db["notifications"].find().sort("created_at", -1).limit(20))
    unread = 0
    for it in items:
        it["_id"] = str(it.get("_id"))
        it["created_at"] = it["created_at"].isoformat() if it.get("created_at") else ""
        it["is_read"] = u["_id"] in (it.get("read_by") or [])
        if not it["is_read"]:
            unread += 1
    return jsonify({"items": items, "unread": unread})


@app.route("/admin/notifications/mark_read", methods=["POST"])
@admin_required
def admin_notifications_mark_read():
    u = current_user()
    db["notifications"].update_many(
        {"read_by": {"$ne": u["_id"]}},
        {"$push": {"read_by": u["_id"]}},
    )
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    from waitress import serve
    print(f"🚀 Chạy production server (waitress) tại cổng {port}")
    serve(app, host="0.0.0.0", port=port)
