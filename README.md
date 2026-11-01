# Bản triển khai VPS

Thư mục này tạo bản web riêng cho VPS, dùng cùng MySQL với máy local:

- Xem tổng quan, khách hàng và các đơn đạt giá; lịch sử gửi được giữ để đối chiếu.
- Mọi người truy cập đều được gắn/gỡ thẻ, lưu hồ sơ, xóa đơn và đồng bộ đơn thủ công.
- Đồng bộ Pancake tự động; giữ quy tắc khách có thẻ không bị dọn sau 15 ngày.
- Không có gửi tin, lịch gửi, điều khiển Chrome hoặc đối chiếu gửi. Các API này cũng bị chặn.

Mặc định mọi người được xem và chỉnh sửa công khai, không cần đăng nhập hoặc mã quản trị. Gắn thẻ/xóa đơn vẫn cần nhấn **Lưu thay đổi** mới ghi dữ liệu.

Trang tải dữ liệu qua một API `/api/dashboard` dùng chung một lượt đọc theo lô. Bảng `app_order_prices` giúp lọc đơn đạt giá ngay trong SQL, và tự bổ sung kết quả cho dữ liệu cũ ở lần tải đầu sau nâng cấp. Dữ liệu POS gốc vẫn được giữ trong database; API không chuyển toàn bộ JSON gốc về giao diện. Tác vụ dọn đơn chạy nền, không giữ bước khởi động HTTP chờ quét từng đơn.

## 1. Chuẩn bị trên máy local

Từ thư mục gốc `HONG_AD`:

```powershell
python -X utf8 deploy/prepare.py
python -X utf8 deploy/prepare.py --check
```

Sau đó cấu trúc sẽ là:

```text
deploy/
├── backend/
│   ├── Dockerfile
│   ├── entrypoint.py
│   ├── requirements.txt
│   └── app/                  # mã backend và schema được gom tự động
├── frontend/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── app/                  # mã frontend được gom tự động
├── compose.yaml
├── .env.example
├── nginx-vhost.conf.example
├── prepare.py
├── manifest.json
└── README.md
```

`app/` là bản sinh tự động, không chỉnh tay. Khi sửa code gốc, chạy lại `prepare.py` rồi build lại image. Cách này giữ một nguồn code và tạo ra một thư mục `deploy/` có thể chép độc lập lên VPS. Script chỉ gom các file được chọn, không chép `.env`, cookie TikTok, ChromeDriver, notebook, SQLite, `.runtime` hoặc `node_modules` của local.

Chép cả thư mục `deploy/` đã chuẩn bị lên VPS, bao gồm các thư mục `app/` được sinh. Chỉ clone Git sẽ chưa có chúng vì đây là file generated; nếu clone toàn bộ dự án trên VPS thì chạy `python deploy/prepare.py` tại đó.

## 2. Cấu hình trên VPS

Yêu cầu Docker Engine và Docker Compose v2. Trong thư mục `deploy` trên VPS:

```bash
cp .env.example .env
nano .env
```

Điền cùng 5 biến `MYSQL_*` của máy local và thông tin Pancake để đồng bộ. Không đặt `MYSQL_HOST=localhost` nếu MySQL nằm trên máy host hoặc máy khác: trong container, localhost là chính container đó.

Điền tên miền hoặc IP truy cập web vào `ALLOWED_HOSTS`, giữ cả localhost và 127.0.0.1 cho health check. Ví dụ:

```dotenv
ALLOWED_HOSTS=localhost,127.0.0.1,hong.example.com
WEB_PORT=8082
WEB_BIND=0.0.0.0
WEB_ALLOW_PUBLIC_EDITS=true
```

Không cần điền `WEB_EDIT_TOKEN` khi dùng cấu hình công khai trên. Nếu sau này muốn giới hạn quyền sửa, đặt `WEB_ALLOW_PUBLIC_EDITS=false` rồi tạo mã ngẫu nhiên trên máy có Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Đặt mã tạo được vào `WEB_EDIT_TOKEN` và khởi động lại container. Chỉ ở chế độ giới hạn này mới có nút **Mở quyền chỉnh sửa** trên web; mã chỉ giữ trong bộ nhớ của trang. Tải lại trang sẽ cần nhập lại. Cấu hình mặc định công khai không có bước nhập mã.

Mật khẩu/mã chứa `$` nên được bao bằng dấu nháy đơn trong file `.env` của Compose để giữ nguyên ký tự, ví dụ `MYSQL_PASSWORD='p$a#ss=word'`.

## 3. Chạy

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
docker compose ps
docker compose logs --tail=100 backend
```

Truy cập `http://IP-VPS:8082` sau khi cho phép cổng TCP 8082 trên VPS. Thêm IP thực tế của VPS vào `ALLOWED_HOSTS` nếu truy cập bằng IP. Backend không xuất cổng 8000 ra Internet; frontend phục vụ giao diện và chuyển tiếp `/api/` vào backend. Các biến môi trường và mật khẩu không được đưa vào image frontend.

Kiểm tra bản đang chạy:

```bash
curl http://127.0.0.1:8082/api/health
```

Kết quả phải có `mode: "web"`, `database: "mysql"`. Dùng một backend worker vì tiến trình đó chạy tác vụ đồng bộ nền. Các container tự khởi động lại qua `restart: unless-stopped`; frontend chờ backend healthy trước khi khởi động.

Không chạy công cụ chuyển SQLite trong container web. Hoàn tất chuyển dữ liệu vào MySQL trước lần chạy web đầu tiên để tránh tạo dữ liệu mặc định trong đích migration.

## 4. Máy local chạy cùng VPS

Máy local dùng code gốc. Trong `.env` local:

```dotenv
APP_MODE=admin
AUTO_SYNC_ENABLED=false
```

Giữ nguyên 5 biến MySQL để hai máy đọc cùng dữ liệu. VPS phụ trách đồng bộ tự động; local vẫn có nút đồng bộ thủ công. Hai bản dùng khóa backend riêng, khóa đồng bộ dùng chung trên MySQL, và giao dịch ghi vẫn được điều phối chung.

Khởi động VPS không khôi phục hoặc đánh dấu gián đoạn các đợt gửi đang chạy trên local. VPS cũng không thay đổi lịch gửi khi tác vụ đồng bộ gặp lỗi. Đơn có tin đang gửi hoặc chưa rõ kết quả không bị xóa từ máy khác; đối chiếu trên local trước khi xóa.

## 5. Tên miền và HTTPS

Nếu dùng Nginx/Caddy hoặc một reverse proxy trên host, đặt `WEB_BIND=127.0.0.1`, trỏ proxy tới `http://127.0.0.1:8082`, và chuyển tiếp đúng `Host`. Có file mẫu `nginx-vhost.conf.example`. Cấu hình chứng chỉ HTTPS theo tên miền khi công bố website.

## 6. Cập nhật và dừng

Sau khi sửa mã nguồn: chạy lại `prepare.py` trên nơi giữ code, chép thư mục deploy mới lên VPS (giữ `.env` trên VPS), rồi:

```bash
docker compose --env-file .env up -d --build
```

Dừng bản VPS:

```bash
docker compose down
```

Lệnh này không xóa dữ liệu MySQL bên ngoài container và không dừng backend local.

Tham khảo: [Docker build context](https://docs.docker.com/build/concepts/context/), [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/).
