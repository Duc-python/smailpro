# SmailPro Temp Gmail + cf-scraper

Tạo hộp thư **Gmail tạm thời miễn phí** trên [smailpro.com](https://smailpro.com/temporary-email),
tự động giải **Cloudflare Turnstile** bằng [cf-scraper](https://github.com/Duc-python/cf-scraper).

Kết quả là một CLI nhỏ: tạo inbox, chờ email xác minh, lấy mã OTP hoặc link xác minh.

> Đây là hộp thư **chỉ nhận** (receive-only) từ pool Gmail của SmailPro.
> Bạn không thể đăng nhập Gmail, không gửi thư, và không sở hữu tài khoản.

---

## Yêu cầu

- Windows (hoặc Linux/macOS), Python 3.11+
- Đã có trong `D:\smailpro`:
  - `.venv` — virtualenv của project
  - `cf-scraper` — repo đã clone
- Gói Python: `httpx`

Nếu chưa cài:

```powershell
cd D:\smailpro
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
git clone https://github.com/Duc-python/cf-scraper.git cf-scraper
.\.venv\Scripts\python.exe -m pip install -r cf-scraper\requirements.txt
.\.venv\Scripts\python.exe -m invisible_playwright fetch
```

`invisible_playwright fetch` chỉ cần chạy một lần để tải engine Firefox.

---

## Dùng nhanh

```powershell
cd D:\smailpro

# 1. Tạo inbox Gmail tạm
.\.venv\Scripts\python.exe .\temp_gmail.py new

# 2. Dùng địa chỉ vừa in ra để đăng ký/đăng nhập ở trang cần test
#    (ví dụ: nhập email vào form signup)

# 3. Chờ email xác minh và lấy OTP/link
.\.venv\Scripts\python.exe .\temp_gmail.py wait ten.ban.123@gmail.com --timeout 300
```

Ví dụ output:

```text
From    : noreply@example.com
Subject : Verify your email
Date    : 2026-11-01T02:20:00+00:00
Code    : 482913
```

Lấy JSON để script khác dùng:

```powershell
.\.venv\Scripts\python.exe .\temp_gmail.py wait ten.ban.123@gmail.com --json
```

---

## Các lệnh

| Lệnh | Việc làm |
| --- | --- |
| `new` | Tạo inbox free (`random@gmail.com`, server 1) và lưu vào `inboxes.json` |
| `list` | Liệt kê các inbox đã lưu |
| `inbox <email>` | Liệt kê tin nhắn trong inbox |
| `wait <email>` | Chờ tin nhắn mới, lấy mã OTP / link |
| `read <email> <mid>` | Đọc toàn bộ nội dung một tin nhắn |
| `token` | Chỉ giải Turnstile và in token (debug) |
| `delete <email>` | Xoá inbox khỏi file state (không xoá trên server) |
| `stop-server` | Dừng cf-scraper do script tự bật |

Các flag dùng được trước hoặc sau lệnh:

```powershell
--json              # output dạng JSON
--no-autostart      # không tự bật cf-scraper
--proxy host:port   # proxy cho cf-scraper (host:port:user:pass)
--cf-url URL        # đổi endpoint cf-scraper
```

Ví dụ `wait` bỏ qua các tin nhắn đang có sẵn:

```powershell
.\.venv\Scripts\python.exe .\temp_gmail.py wait ten.ban.123@gmail.com --skip-current --timeout 600
```

---

## Cách hoạt động

1. **cf-scraper** chạy local ở `http://127.0.0.1:3000`.
   Script gọi mode `turnstile-min` với:
   - `url = https://smailpro.com/temporary-email`
   - `siteKey = 0x4AAAAAAABIS_gEec2IwOhI`

   cf-scraper mở engine ẩn danh, chặn document của smailpro và trả về một
   trang fake có render Turnstile đúng origin, bấm checkbox, đọc token.

2. Script gọi:

   ```text
   GET https://smailpro.com/app/create?username=random&type=alias&domain=gmail.com&server=1
   Header: x-captcha: <turnstile-token>
   ```

   Server trả về `address`, `timestamp`, `key`.

3. Danh sách tin nhắn:

   ```text
   POST https://smailpro.com/app/inbox
   Body: [{"address": "...", "timestamp": 123, "key": "..."}]
   → payload JWT
   GET https://api.sonjj.com/v1/temp_gmail/inbox?payload=...
   ```

4. Đọc nội dung tin nhắn (cần thêm một Turnstile token):

   ```text
   GET https://smailpro.com/app/message?email=...&mid=...
   Header: x-captcha: <turnstile-token>
   → payload JWT
   GET https://api.sonjj.com/v1/temp_gmail/message?payload=...
   ```

5. Script trích OTP bằng regex (6 số, 4–8 số gần chữ "code/otp/mã",
   hoặc mã chữ+số), và tìm link có `verify`, `confirm`, `activate`...

---

## Lưu ý & giới hạn

- **Chỉ nhận thư.** Không gửi được, không đăng nhập Gmail/Outlook.
- Free tier: `@gmail.com` random trên server 1, tối đa **5 inbox** active.
- Inbox có thể bị thu hồi/rotate bất cứ lúc nào. Đừng dùng cho:
  ngân hàng, thanh toán, tài khoản quan trọng, khôi phục mật khẩu lâu dài.
- Chỉ dùng cho **test/QA** trên dịch vụ bạn được phép test. Tuân thủ
  Terms of Service của trang bạn đăng ký; không dùng để spam, gian lận,
  hoặc tạo tài khoản hàng loạt.
- Turnstile có thể fail do IP/proxy chậm. Thử lại, hoặc dùng `--proxy`.
- Mỗi lần `new` tạo một session cookie mới; inbox lưu trong `inboxes.json`
  (có chứa `key` — đừng commit file này).

---

## Troubleshooting

| Lỗi | Cách xử lý |
| --- | --- |
| `cf-scraper is not running` | Chạy `temp_gmail.py token` để tự bật, hoặc `cd cf-scraper` rồi `..\.venv\Scripts\python.exe -m cf_scraper` |
| `cf-scraper exited during startup` | Xem `cf-server.log`; thường do thiếu engine: chạy `python -m invisible_playwright fetch` |
| `invisible_playwright fetch` exit 1 | Nếu output vẫn in ra đường dẫn `...\firefox-54_...\firefox.exe` thì engine đã có; các dòng "cached engine does not match the seal" chỉ là cảnh báo cache cũ, bỏ qua được |
| `Captcha is invalid` | Token hết hạn hoặc Cloudflare đổi rule — chạy lại |
| `You have reached the active email limit` | Xoá bớt inbox trên web smailpro.com (free tối đa 5) |
| `Message not found!` | `mid` sai hoặc tin nhắn đã bị xoá |
| `No new message within ...` | Chưa có thư đến; tăng `--timeout`, kiểm tra đã gửi đúng địa chỉ |

Log server: `D:\smailpro\cf-server.log`
State inbox: `D:\smailpro\inboxes.json`

---

## Cấu trúc

```text
D:\smailpro\
├─ temp_gmail.py          # file chính (toàn bộ CLI)
├─ requirements.txt       # httpx
├─ inboxes.json           # state các inbox đã tạo (tự sinh)
├─ cf-server.log          # log cf-scraper (tự sinh)
├─ cf-server.pid          # PID server do script bật (tự sinh)
├─ .venv\                 # virtualenv
└─ cf-scraper\            # repo solver Turnstile
```
