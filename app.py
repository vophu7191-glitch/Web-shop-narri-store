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
import os, re, uuid, secrets
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify
)
from pymongo import MongoClient, ReturnDocument
from werkzeug.security import generate_password_hash, check_password_hash

# ══════════════════════════════════════════════════════════════
#  CẤU HÌNH & KẾT NỐI DATABASE
# ══════════════════════════════════════════════════════════════
MONGO_URI  = os.environ.get("MONGO_URI", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

if not MONGO_URI:
    raise RuntimeError("Chưa cấu hình MONGO_URI trong biến môi trường!")

_mc = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
_mc.server_info()  # ném lỗi ngay nếu không kết nối được, dễ debug lúc deploy
db = _mc["cloudshop"]

col_users     = db["users"]
col_products  = db["products"]
col_stock     = db["stock_codes"]
col_orders    = db["orders"]
col_config    = db["config"]
col_recharges = db["recharges"]

# Index cơ bản (an toàn khi gọi lại nhiều lần, chỉ tạo nếu chưa có)
col_users.create_index("username", unique=True)
col_stock.create_index([("product_id", 1), ("sold", 1)])
col_orders.create_index([("user_id", 1), ("created_at", -1)])
col_recharges.create_index([("created_at", -1)])

BRANDS = {
    "redfinger": "RedFinger",
    "ugphone":   "UGPhone",
    "vipplayer": "VIPPlayer",
    "genplay":   "GenPlay",
    "vmos":      "VMOS",
}

BRAND_ICONS = {
    "redfinger": "fas fa-fingerprint",
    "ugphone":   "fas fa-mobile-alt",
    "vipplayer": "fas fa-crown",
    "genplay":   "fas fa-gamepad",
    "vmos":      "fas fa-cloud",
}

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
        }
        col_config.insert_one(cfg)
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
        return f(*a, **kw)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or u.get("role") != "admin":
            flash("Bạn không có quyền truy cập chức năng này.", "error")
            return redirect(url_for("home"))
        return f(*a, **kw)
    return wrapper

@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "cfg": get_config(),
        "brands": BRANDS,
        "brand_icons": BRAND_ICONS,
        "brand_counts": {b: col_products.count_documents({"brand": b, "enabled": True}) for b in BRANDS},
    }


# ══════════════════════════════════════════════════════════════
#  TIỆN ÍCH CHO FEED "ĐƠN HÀNG / NẠP TIỀN GẦN ĐÂY" (kiểu taphoacloud)
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


# ══════════════════════════════════════════════════════════════
#  TRANG CHỦ — danh sách sản phẩm theo từng brand
# ══════════════════════════════════════════════════════════════
@app.route("/")
def home():
    products_by_brand = {}
    for b in BRANDS:
        items = list(col_products.find({"brand": b, "enabled": True}).sort("price", 1))
        for it in items:
            if it.get("fulfillment", "auto") == "manual":
                it["stock_count"] = None  # không cần tồn kho, luôn nhận đơn
            else:
                it["stock_count"] = col_stock.count_documents({"product_id": it["_id"], "sold": False})
        products_by_brand[b] = items

    recent_orders, recent_recharges = get_recent_activity()
    return render_template("home.html",
                            products_by_brand=products_by_brand,
                            recent_orders=recent_orders,
                            recent_recharges=recent_recharges)


@app.route("/api/activity")
def api_activity():
    recent_orders, recent_recharges = get_recent_activity()
    return jsonify({"orders": recent_orders, "recharges": recent_recharges})


# ══════════════════════════════════════════════════════════════
#  ĐĂNG KÝ / ĐĂNG NHẬP / ĐĂNG XUẤT
# ══════════════════════════════════════════════════════════════
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if not re.fullmatch(r"[a-z0-9_]{4,20}", username):
            flash("Tên đăng nhập chỉ gồm chữ thường/số/gạch dưới, 4-20 ký tự.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Mật khẩu phải từ 6 ký tự trở lên.", "error")
            return render_template("register.html")
        if password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "error")
            return render_template("register.html")
        if col_users.find_one({"username": username}):
            flash("Tên đăng nhập đã tồn tại.", "error")
            return render_template("register.html")

        uid = uuid.uuid4().hex[:12]
        is_first_user = col_users.count_documents({}) == 0
        col_users.insert_one({
            "_id": uid,
            "username": username,
            "password_hash": generate_password_hash(password),
            "balance": 0,
            "role": "admin" if is_first_user else "user",
            "created_at": datetime.now(timezone.utc),
        })
        session["uid"] = uid
        if is_first_user:
            flash("Đăng ký thành công! Bạn là người đầu tiên nên tự động là Admin.", "success")
        else:
            flash("Đăng ký thành công! Chào mừng bạn.", "success")
        return redirect(url_for("home"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        u = col_users.find_one({"username": username})
        if not u or not check_password_hash(u["password_hash"], password):
            flash("Sai tên đăng nhập hoặc mật khẩu.", "error")
            return render_template("login.html")
        session["uid"] = u["_id"]
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

    return render_template("wallet.html", u=u, qr_url=qr_url, total_deposited=total_deposited,
                            transfer_content=f"NAPU{u['_id']}", orders=orders)


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
        "created_at": datetime.now(timezone.utc),
    })

    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════
#  MUA HÀNG — trừ ví, xuất 1 code từ kho
# ══════════════════════════════════════════════════════════════
@app.route("/buy/<product_id>", methods=["POST"])
@login_required
def buy(product_id):
    u = current_user()
    p = col_products.find_one({"_id": product_id, "enabled": True})
    if not p:
        flash("Sản phẩm không tồn tại hoặc đã ngừng bán.", "error")
        return redirect(url_for("home"))
    if u["balance"] < p["price"]:
        flash("Số dư ví không đủ. Vui lòng nạp thêm tiền.", "error")
        return redirect(url_for("wallet"))

    fulfillment = p.get("fulfillment", "auto")
    customer_note = request.form.get("note", "").strip()[:500]

    if fulfillment == "manual":
        # Bán xoay vốn — không cần tồn kho, trừ tiền trước rồi admin xử lý và giao tay sau
        charged = col_users.find_one_and_update(
            {"_id": u["_id"], "balance": {"$gte": p["price"]}},
            {"$inc": {"balance": -p["price"]}},
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
            "price": p["price"],
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
        {"_id": u["_id"], "balance": {"$gte": p["price"]}},
        {"$inc": {"balance": -p["price"]}},
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
        "price": p["price"],
        "fulfillment": "auto",
        "status": "completed",
        "code": code_doc["code"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    flash(f"Mua thành công! Mã của bạn: {code_doc['code']}", "success")
    return redirect(url_for("orders"))


@app.route("/orders")
@login_required
def orders():
    u = current_user()
    items = list(col_orders.find({"user_id": u["_id"]}).sort("created_at", -1))
    return render_template("orders.html", orders=items)


# ══════════════════════════════════════════════════════════════
#  ADMIN — cùng giao diện khách, chỉ hiện thêm cho role=admin
#  (các route dưới đây tự chặn bằng admin_required, không lộ
#   ra ngoài UI cho khách thường)
# ══════════════════════════════════════════════════════════════
@app.route("/admin/products", methods=["GET", "POST"])
@admin_required
def admin_products():
    if request.method == "POST":
        brand = request.form.get("brand", "")
        name  = request.form.get("name", "").strip()
        price = request.form.get("price", "0")
        desc  = request.form.get("description", "").strip()
        fulfillment = request.form.get("fulfillment", "auto")
        image_url = request.form.get("image_url", "").strip()
        if fulfillment not in ("auto", "manual"):
            fulfillment = "auto"
        if brand not in BRANDS or not name or not price.isdigit() or int(price) <= 0:
            flash("Thông tin sản phẩm không hợp lệ.", "error")
        else:
            col_products.insert_one({
                "_id": uuid.uuid4().hex[:10],
                "brand": brand, "name": name, "price": int(price),
                "description": desc, "enabled": True,
                "fulfillment": fulfillment, "image_url": image_url,
                "created_at": datetime.now(timezone.utc),
            })
            flash(f"Đã thêm sản phẩm: {name}", "success")
        return redirect(url_for("admin_products"))

    items = list(col_products.find().sort([("brand", 1), ("price", 1)]))
    for it in items:
        if it.get("fulfillment", "auto") == "manual":
            it["stock_count"] = None
        else:
            it["stock_count"] = col_stock.count_documents({"product_id": it["_id"], "sold": False})
    return render_template("admin_products.html", items=items)


@app.route("/admin/products/<pid>/toggle", methods=["POST"])
@admin_required
def admin_toggle_product(pid):
    p = col_products.find_one({"_id": pid})
    if p:
        col_products.update_one({"_id": pid}, {"$set": {"enabled": not p.get("enabled", True)}})
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<pid>/delete", methods=["POST"])
@admin_required
def admin_delete_product(pid):
    col_products.delete_one({"_id": pid})
    col_stock.delete_many({"product_id": pid})
    flash("Đã xoá sản phẩm và toàn bộ kho code liên quan.", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/stock/<pid>", methods=["GET", "POST"])
@admin_required
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
            flash(f"Đã nhập {len(lines)} code vào kho.", "success")
        return redirect(url_for("admin_stock", pid=pid))

    remaining = list(col_stock.find({"product_id": pid, "sold": False}).sort("created_at", 1))
    return render_template("admin_stock.html", p=p, remaining=remaining)


@app.route("/admin/orders")
@admin_required
def admin_orders():
    items = list(col_orders.find().sort("created_at", -1).limit(200))
    users = {u["_id"]: u["username"] for u in col_users.find({}, {"username": 1})}
    return render_template("admin_orders.html", orders=items, users=users)


@app.route("/admin/orders/<order_id>/update", methods=["GET", "POST"])
@admin_required
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
            flash(f"Đã huỷ đơn và hoàn {o['price']:,}đ vào ví khách.", "success")
        else:
            flash("Đã cập nhật đơn hàng.", "success")

        col_orders.update_one({"_id": order_id}, {"$set": update})
        return redirect(url_for("admin_orders"))

    return render_template("admin_order_update.html", o=o)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        uid    = request.form.get("uid", "")
        delta  = request.form.get("delta", "0")
        try:
            delta = int(delta)
        except ValueError:
            delta = 0
        if uid and delta != 0:
            col_users.update_one({"_id": uid}, {"$inc": {"balance": delta}})
            flash("Đã cập nhật số dư.", "success")
        return redirect(url_for("admin_users"))

    items = list(col_users.find().sort("created_at", -1))
    return render_template("admin_users.html", items=items)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        col_config.update_one({"_id": "main"}, {"$set": {
            "site_name":           request.form.get("site_name", "CloudShop").strip(),
            "sepay_api_key":       request.form.get("sepay_api_key", "").strip(),
            "bank_bin":            request.form.get("bank_bin", "").strip(),
            "bank_account_number": request.form.get("bank_account_number", "").strip(),
            "bank_account_name":   request.form.get("bank_account_name", "").strip(),
        }}, upsert=True)
        flash("Đã lưu cấu hình.", "success")
        return redirect(url_for("admin_settings"))
    webhook_url = request.host_url.rstrip("/") + "/sepay-webhook"
    return render_template("admin_settings.html", cfg=get_config(), webhook_url=webhook_url)


# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    from waitress import serve
    print(f"🚀 Chạy production server (waitress) tại cổng {port}")
    serve(app, host="0.0.0.0", port=port)
