# CloudShop — Web bán code RedFinger / UGPhone / VIPPlayer / GenPlay / VMOS

## Cấu trúc
- `app.py` — toàn bộ backend Flask (route, logic mua/bán, ví, webhook SePay, admin).
- `templates/` — giao diện (dùng lại theme Stisla đã lấy từ taphoacloud.vn).
- `static/` — css/js/font/ảnh dùng chung.
- `requirements.txt` — thư viện cần cài.

## Mô hình dữ liệu (MongoDB — database `cloudshop`)
- `users`: tài khoản khách (username, password_hash, balance, role: user/admin).
- `products`: sản phẩm theo từng brand (redfinger/ugphone/vipplayer/genplay/vmos), tên, giá, mô tả.
- `stock_codes`: kho code thật, mỗi dòng 1 code, gắn với 1 product, đánh dấu `sold` khi đã bán.
- `orders`: lịch sử đơn hàng (ai mua gì, giá bao nhiêu, code nào).
- `config`: cấu hình chung (tên shop, ngân hàng, SePay API key).

## Cách deploy lên Render (giống bot Discord bạn đang chạy)

1. Đẩy toàn bộ thư mục này lên GitHub repo (file chính đặt tên `app.py`).
2. Trên Render → **New** → **Web Service** → chọn repo này.
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python app.py`
5. Vào tab **Environment**, thêm các biến:
   - `MONGO_URI` — dùng **chung 1 cluster MongoDB Atlas** bạn đã tạo cho bot Discord cũng được (nó tự tạo database mới tên `cloudshop`, không đụng tới dữ liệu bot), hoặc tạo cluster riêng.
   - `SECRET_KEY` — 1 chuỗi bất kỳ, dùng để mã hoá session đăng nhập (ví dụ gõ bừa 32 ký tự).
   - `PYTHONUNBUFFERED` = `1` (để log hiện real-time, tránh bị delay như bot Discord từng gặp).
6. Deploy.

## Sau khi deploy lần đầu

1. Vào domain vừa deploy → bấm **Đăng ký** → **tài khoản đăng ký ĐẦU TIÊN sẽ tự động là Admin**.
2. Đăng nhập bằng tài khoản admin đó → thanh đen phía trên sẽ hiện menu quản lý.
3. Vào **Cấu hình hệ thống**:
   - Điền tên shop.
   - Điền mã BIN ngân hàng + số tài khoản + tên chủ tài khoản (dùng để tạo QR nạp ví, giống bot).
   - Điền **SePay API Key** (đặt giống hệt bên SePay Dashboard → Webhooks → Bảo mật).
4. Copy đúng **Webhook URL** hiện trên trang Cấu hình → dán vào SePay Dashboard → Webhooks → URL.
5. Vào **Quản lý sản phẩm** → thêm sản phẩm theo từng thương hiệu (VD: RedFinger — Gói 30 ngày — giá 50.000đ).
6. Bấm **📦 Nhập code** trên sản phẩm đó → dán danh sách code (mỗi dòng 1 code) → **Nhập vào kho**.
7. Xong — khách vào trang chủ, đăng ký tài khoản, nạp tiền qua QR (ví tự cộng tiền qua webhook SePay), bấm **Mua ngay** là nhận code ngay lập tức.

## Cơ chế giao dịch nạp ví (giống hệt bot Discord)
- Mỗi user có 1 mã nội dung chuyển khoản riêng: `NAPU<user_id>`.
- Khách chuyển khoản đúng nội dung đó (quét QR hệ thống tự tạo) → SePay gửi webhook về `/sepay-webhook` → hệ thống tự cộng đúng số tiền vào đúng ví user đó.
- Có chống cộng trùng nếu SePay gửi lại cùng 1 giao dịch (dựa vào `id` giao dịch).

## Bảo mật cần lưu ý
- Đổi `SECRET_KEY` thành 1 chuỗi ngẫu nhiên đủ dài, không dùng chuỗi mặc định.
- Không public API Key SePay ra ngoài.
- Nên đổi mật khẩu tài khoản admin đầu tiên thành mật khẩu mạnh.
