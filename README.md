# SmailPro Temp Gmail + cf-scraper

[Tiếng Việt](README.vi.md) | **English**

Create a **free temporary Gmail inbox** on [smailpro.com](https://smailpro.com/temporary-email)
and solve **Cloudflare Turnstile** automatically with
[cf-scraper](https://github.com/Duc-python/cf-scraper).

The result is a small CLI: create an inbox, wait for the verification email, grab
the OTP code or the verification link.

> This is a **receive-only** mailbox from SmailPro's Gmail pool.
> You cannot sign in to Gmail, send mail, or own the account.

---

## Requirements

- Windows (or Linux/macOS), Python 3.11+
- Already present in `D:\smailpro`:
  - `.venv` — the project virtualenv
  - `cf-scraper` — the cloned repo
- Python package: `httpx`

Fresh setup:

```powershell
cd D:\smailpro
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
git clone https://github.com/Duc-python/cf-scraper.git cf-scraper
.\.venv\Scripts\python.exe -m pip install -r cf-scraper\requirements.txt
.\.venv\Scripts\python.exe -m invisible_playwright fetch
```

`invisible_playwright fetch` only needs to run once, to download the Firefox engine.

---

## Quick start

```powershell
cd D:\smailpro

# 1. Create a temporary Gmail inbox
.\.venv\Scripts\python.exe .\temp_gmail.py new

# 2. Use the printed address to sign up / log in on the site under test
#    (e.g. paste it into the signup form)

# 3. Wait for the verification email and get the OTP / link
.\.venv\Scripts\python.exe .\temp_gmail.py wait your.name.123@gmail.com --timeout 300
```

Example output:

```text
From    : noreply@example.com
Subject : Verify your email
Date    : 2026-11-01T02:20:00+00:00
Code    : 482913
```

Get JSON so other scripts can consume it:

```powershell
.\.venv\Scripts\python.exe .\temp_gmail.py wait your.name.123@gmail.com --json
```

---

## Commands

| Command | What it does |
| --- | --- |
| `new` | Create a free inbox (`random@gmail.com`, server 1) and save it to `inboxes.json` |
| `list` | List saved inboxes |
| `inbox <email>` | List messages in an inbox |
| `wait <email>` | Wait for a new message and extract the OTP / link |
| `read <email> <mid>` | Read the full body of one message |
| `token` | Only solve Turnstile and print the token (debug) |
| `delete <email>` | Remove an inbox from the state file (does not delete it server-side) |
| `stop-server` | Stop the cf-scraper instance the script started |

Flags work before or after the command:

```powershell
--json              # JSON output
--no-autostart      # do not auto-start cf-scraper
--proxy host:port   # proxy for cf-scraper (host:port:user:pass)
--cf-url URL        # override the cf-scraper endpoint
```

Example: `wait` ignoring messages that already exist:

```powershell
.\.venv\Scripts\python.exe .\temp_gmail.py wait your.name.123@gmail.com --skip-current --timeout 600
```

---

## How it works

1. **cf-scraper** runs locally at `http://127.0.0.1:3000`.
   The script calls its `turnstile-min` mode with:
   - `url = https://smailpro.com/temporary-email`
   - `siteKey = 0x4AAAAAAABIS_gEec2IwOhI`

   cf-scraper launches a stealth engine, blocks SmailPro's document, and serves a
   fake page that renders Turnstile under the correct origin; it clicks the
   checkbox and reads the token.

2. The script calls:

   ```text
   GET https://smailpro.com/app/create?username=random&type=alias&domain=gmail.com&server=1
   Header: x-captcha: <turnstile-token>
   ```

   The server returns `address`, `timestamp`, and `key`.

3. Listing messages:

   ```text
   POST https://smailpro.com/app/inbox
   Body: [{"address": "...", "timestamp": 123, "key": "..."}]
   → JWT payload
   GET https://api.sonjj.com/v1/temp_gmail/inbox?payload=...
   ```

4. Reading a message body (needs another Turnstile token):

   ```text
   GET https://smailpro.com/app/message?email=...&mid=...
   Header: x-captcha: <turnstile-token>
   → JWT payload
   GET https://api.sonjj.com/v1/temp_gmail/message?payload=...
   ```

5. The script extracts the OTP with regexes (6 digits, 4–8 digits near a
   "code/otp" word, or alphanumeric codes) and looks for links containing
   `verify`, `confirm`, `activate`, etc.

---

## Notes & limitations

- **Receive-only.** No sending, no Gmail/Outlook login.
- Free tier: random `@gmail.com` on server 1, at most **5 active inboxes**.
- Inboxes can be reclaimed/rotated at any time. Do not use for: banking,
  payments, important accounts, or long-term password recovery.
- Use only for **testing/QA** on services you are allowed to test. Respect the
  Terms of Service of the site you sign up on; do not use for spam, fraud, or
  bulk account creation.
- Turnstile may fail on slow IPs/proxies. Retry, or pass `--proxy`.
- Each `new` creates a fresh session cookie; inboxes are stored in
  `inboxes.json` (it contains the `key` — never commit this file).

---

## Troubleshooting

| Error | Fix |
| --- | --- |
| `cf-scraper is not running` | Run `temp_gmail.py token` to auto-start it, or `cd cf-scraper` then `..\.venv\Scripts\python.exe -m cf_scraper` |
| `cf-scraper exited during startup` | Check `cf-server.log`; usually a missing engine — run `python -m invisible_playwright fetch` |
| `invisible_playwright fetch` exits 1 | If the output still prints a `...\firefox-54_...\firefox.exe` path, the engine is already there; "cached engine does not match the seal" lines are stale-cache warnings and can be ignored |
| `Captcha is invalid` | Token expired or Cloudflare changed its rules — run again |
| `You have reached the active email limit` | Delete some inboxes on smailpro.com (free tier allows 5) |
| `Message not found!` | Wrong `mid`, or the message was deleted |
| `No new message within ...` | Nothing arrived yet; raise `--timeout` and verify the address |

Server log: `D:\smailpro\cf-server.log`
Inbox state: `D:\smailpro\inboxes.json`

---

## Layout

```text
D:\smailpro\
├─ temp_gmail.py          # main file (the whole CLI)
├─ requirements.txt       # httpx
├─ README.md              # English docs
├─ README.vi.md           # Vietnamese docs
├─ inboxes.json           # saved inbox state (generated)
├─ cf-server.log          # cf-scraper log (generated)
├─ cf-server.pid          # PID of the server the script started (generated)
├─ .venv\                 # virtualenv
└─ cf-scraper\            # Turnstile solver repo
```

---

## Disclaimer

This project is for educational purposes and for testing your own services.
You are responsible for how you use it.
