#!/usr/bin/env python3
"""
temp_gmail.py - SmailPro free temporary Gmail + cf-scraper Turnstile solver.

One file, two pieces:

  1. cf-scraper (https://github.com/Duc-python/cf-scraper) solves the Cloudflare
     Turnstile widget that SmailPro requires before it will hand out a free
     inbox.
  2. SmailPro (https://smailpro.com/temporary-email) creates the free
     @gmail.com inbox and exposes the receive-only message API used here.

The result is a small CLI that can create an inbox, wait for a verification
email, and pull out the OTP code or verification link - useful for testing
signup flows, QA, and throwaway registrations.

Usage (Windows, from D:\\smailpro):

    .venv\\Scripts\\python.exe temp_gmail.py new
    .venv\\Scripts\\python.exe temp_gmail.py wait <address>
    .venv\\Scripts\\python.exe temp_gmail.py inbox <address>
    .venv\\Scripts\\python.exe temp_gmail.py read <address> <mid>

Run `temp_gmail.py --help` for every command.

Requirements:
    pip install httpx
    cf-scraper cloned next to this file (D:\\smailpro\\cf-scraper) and its
    requirements installed in .venv.  The script auto-starts the cf-scraper
    server on port 3000 if it is not already running.

Notes:
    * SmailPro inboxes are receive-only. You cannot log in at gmail.com or
      send mail from them.
    * Free tier: random @gmail.com on server 1, max 5 active inboxes.
    * Use this for low-risk/testing accounts. Do not use it for banking,
      payments, or anything you need to recover later.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("SMAILPRO_STATE", str(APP_DIR / "inboxes.json")))
CF_DIR = Path(os.getenv("CF_SCRAPER_DIR", str(APP_DIR / "cf-scraper")))
CF_SERVER_URL = os.getenv("CF_SCRAPER_URL", "http://127.0.0.1:3000/cf-clearance-scraper")
CF_PID_FILE = APP_DIR / "cf-server.pid"
CF_LOG_FILE = APP_DIR / "cf-server.log"

SMAILPRO_BASE = os.getenv("SMAILPRO_BASE", "https://smailpro.com").rstrip("/")
SMAILPRO_SITEKEY = os.getenv("SMAILPRO_SITEKEY", "0x4AAAAAAABIS_gEec2IwOhI")
SONJJ_API = os.getenv("SONJJ_API", "https://api.sonjj.com/v1")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/155.0.0.0 Safari/537.36"
)

GOOGLE_DOMAINS = {"gmail.com", "googlemail.com"}
MICROSOFT_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}

# Turnstile actions SmailPro renders its widgets with. The server does not
# appear to validate the action, but we send it when the solver supports it.
ACTION_CREATE = "smailpro_auto"
ACTION_MESSAGE = "smailpro_message"


class TempMailError(RuntimeError):
    """Any user-facing failure."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TempMailError(f"Cannot read state file {STATE_FILE}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def remember(inbox: "Inbox") -> None:
    state = load_state()
    state[inbox.address] = inbox.to_dict()
    save_state(state)


def forget(address: str) -> bool:
    state = load_state()
    if address not in state:
        return False
    del state[address]
    save_state(state)
    return True


def resolve_inbox(address: str) -> "Inbox":
    state = load_state()
    entry = state.get(address)
    if not entry:
        raise TempMailError(
            f"Unknown inbox {address!r}. Create one with `temp_gmail.py new`, "
            f"or check `temp_gmail.py list`."
        )
    return Inbox.from_dict(entry)


def mail_kind(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in GOOGLE_DOMAINS:
        return "temp_gmail"
    if domain in MICROSOFT_DOMAINS:
        return "temp_outlook"
    return "temp_email"


def html_to_text(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", value)
    # Keep the href next to its anchor text: many verification mails only put
    # the real URL in href, not in the visible text.
    value = re.sub(
        r"(?is)<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        lambda m: f"{re.sub(r'(?s)<[^>]+>', ' ', m.group(2))} {m.group(1)}",
        value,
    )
    value = re.sub(r"(?is)<br\s*/?>", "\n", value)
    value = re.sub(r"(?is)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def message_text(message: dict[str, Any]) -> str:
    """Best-effort plain text from a SmailPro message object."""
    parts: list[str] = []
    for key in ("subject", "intro", "body", "text", "html"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(html_to_text(value) if "<" in value else value.strip())
    return "\n".join(parts)


def extract_code(text: str) -> str | None:
    """Pull the most likely verification code out of an email."""
    if not text:
        return None

    # 1) Six digits is by far the most common OTP length.
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
    if matches:
        return matches[0]

    # 2) A 4-8 digit code close to a keyword.
    keyword = re.search(
        r"(?i)(?:code|otp|pin|passcode|verification|verify|xác minh|mã)"
        r"[^0-9]{0,40}(\d{4,8})",
        text,
    )
    if keyword:
        return keyword.group(1)

    # 3) Any standalone 4-8 digit number, preferring 4-6 digits.
    matches = re.findall(r"(?<!\d)(\d{4,8})(?!\d)", text)
    if matches:
        return sorted(matches, key=len)[0]

    # 4) Alphanumeric code near a keyword (e.g. "ABC123").
    keyword = re.search(
        r"(?i)(?:code|otp|pin|token|verification)[^A-Z0-9]{0,40}([A-Z0-9]{4,8})",
        text,
    )
    if keyword:
        return keyword.group(1)

    return None


LINK_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
LINK_HINTS = re.compile(
    r"(?i)(verify|confirm|activate|activation|validate|validation|"
    r"reset|password|token|signup|register|auth|magic)"
)


def extract_link(text: str) -> str | None:
    for raw in LINK_RE.findall(text or ""):
        link = raw.rstrip(".,;:!?)]}")
        if LINK_HINTS.search(link):
            return link
    links = LINK_RE.findall(text or "")
    return links[0].rstrip(".,;:!?)]}") if links else None


def summarize(message: dict[str, Any]) -> dict[str, Any]:
    text = message_text(message)
    return {
        "mid": message.get("mid") or message.get("id"),
        "from": message.get("from") or message.get("sender"),
        "subject": message.get("subject"),
        "date": message.get("date") or message.get("created_at") or message.get("timestamp"),
        "code": extract_code(text),
        "link": extract_link(text),
        "preview": text[:300],
    }


# --------------------------------------------------------------------------
# cf-scraper
# --------------------------------------------------------------------------


def _server_parts(url: str | None = None) -> tuple[str, int]:
    parsed = urlparse(url or CF_SERVER_URL)
    return parsed.hostname or "127.0.0.1", parsed.port or 3000


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cf_python() -> str:
    candidates = [
        APP_DIR / ".venv" / "Scripts" / "python.exe",
        APP_DIR / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def start_cf_server(url: str | None = None) -> None:
    """Start `python -m cf_scraper` detached and wait for its port."""
    if not CF_DIR.exists():
        raise TempMailError(
            f"cf-scraper not found at {CF_DIR}. Clone it with:\n"
            f"  git clone https://github.com/Duc-python/cf-scraper.git {CF_DIR}"
        )

    host, port = _server_parts(url)
    env = os.environ.copy()
    env["PORT"] = str(port)
    kwargs: dict[str, Any] = {
        "cwd": str(CF_DIR),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True

    with open(CF_LOG_FILE, "ab") as log:
        kwargs["stdout"] = log
        proc = subprocess.Popen([_cf_python(), "-m", "cf_scraper"], **kwargs)
    CF_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(
        f"[cf-scraper] starting server on {host}:{port} (pid {proc.pid}) ...",
        file=sys.stderr,
    )

    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            if _port_open(host, port):
                print("[cf-scraper] ready", file=sys.stderr)
                return
            if proc.poll() is not None:
                raise TempMailError(
                    f"cf-scraper exited during startup. See {CF_LOG_FILE}."
                )
            time.sleep(0.5)
        raise TempMailError(
            f"cf-scraper did not open {host}:{port} within 120s. See {CF_LOG_FILE}."
        )
    except BaseException:
        CF_PID_FILE.unlink(missing_ok=True)
        if proc.poll() is None:
            proc.terminate()
        raise


def ensure_cf_server(autostart: bool = True, url: str | None = None) -> None:
    host, port = _server_parts(url)
    if _port_open(host, port):
        return
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise TempMailError(
            f"cf-scraper at {url or CF_SERVER_URL} is not reachable. "
            f"Start it there, or use --cf-url with a local server."
        )
    if not autostart:
        raise TempMailError(
            f"cf-scraper is not running on {host}:{port}. Start it with:\n"
            f"  cd {CF_DIR} && python -m cf_scraper\n"
            f"or re-run without --no-autostart."
        )
    start_cf_server(url)


def stop_cf_server() -> None:
    if not CF_PID_FILE.exists():
        print("No PID file; the server was probably started outside this script.")
        return
    try:
        pid = int(CF_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        CF_PID_FILE.unlink(missing_ok=True)
        print("Invalid PID file removed.")
        return

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    CF_PID_FILE.unlink(missing_ok=True)
    print(f"Stopped cf-scraper pid {pid}.")


@dataclass
class Proxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, value: str | None) -> "Proxy | None":
        if not value:
            return None
        parts = value.split(":", 3)
        try:
            if len(parts) == 2:
                host, port = parts
                return cls(host, int(port))
            if len(parts) == 4:
                host, port, username, password = parts
                return cls(host, int(port), username, password)
        except ValueError as exc:
            raise TempMailError(f"Invalid proxy port in {value!r}") from exc
        raise TempMailError("--proxy must be host:port or host:port:user:pass")

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"host": self.host, "port": self.port}
        if self.username:
            data["username"] = self.username
        if self.password:
            data["password"] = self.password
        return data


class CfScraper:
    def __init__(self, url: str = CF_SERVER_URL, proxy: Proxy | None = None,
                 timeout: float = 180.0) -> None:
        self.url = url
        self.proxy = proxy
        self.timeout = timeout
        # Stock cf-scraper rejects the extra `action` field; remember that so
        # only the first solve pays for the 400 + retry.
        self._action_supported = True

    def solve_turnstile(self, url: str, sitekey: str,
                        action: str | None = None) -> str:
        payload: dict[str, Any] = {
            "mode": "turnstile-min",
            "url": url,
            "siteKey": sitekey,
        }
        if self.proxy:
            payload["proxy"] = self.proxy.as_dict()
        if action and self._action_supported:
            payload["action"] = action

        try:
            response = httpx.post(self.url, json=payload, timeout=self.timeout)
            # Stock cf-scraper rejects unknown fields. Retry without `action`.
            if response.status_code == 400 and action and self._action_supported:
                self._action_supported = False
                payload.pop("action", None)
                response = httpx.post(self.url, json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise TempMailError(f"Cannot reach cf-scraper at {self.url}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TempMailError(
                f"cf-scraper returned non-JSON (HTTP {response.status_code})"
            ) from exc

        if not isinstance(data, dict):
            raise TempMailError(f"cf-scraper returned unexpected JSON: {data!r}")
        if data.get("code") != 200 or not data.get("token"):
            raise TempMailError(
                f"cf-scraper failed: {data.get('message') or data}"
            )
        return str(data["token"])


# --------------------------------------------------------------------------
# SmailPro
# --------------------------------------------------------------------------


@dataclass
class Inbox:
    address: str
    timestamp: int
    key: str
    created_at: str = ""
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "timestamp": self.timestamp,
            "key": self.key,
            "created_at": self.created_at or now_iso(),
            "domain": self.domain or self.address.rsplit("@", 1)[-1],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Inbox":
        return cls(
            address=data["address"],
            timestamp=int(data.get("timestamp") or 0),
            key=data["key"],
            created_at=data.get("created_at", ""),
            domain=data.get("domain", ""),
        )


class SmailPro:
    def __init__(self, solver: CfScraper, base: str = SMAILPRO_BASE,
                 timeout: float = 60.0) -> None:
        self.solver = solver
        self.base = base.rstrip("/")
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": self.base,
                "Referer": self.base + "/temporary-email",
            },
        )
        self._warmed = False

    def close(self) -> None:
        self.client.close()

    def _warm(self) -> None:
        if self._warmed:
            return
        try:
            self.client.get(self.base + "/temporary-email")
        except httpx.HTTPError as exc:
            raise TempMailError(f"Cannot reach SmailPro: {exc}") from exc
        self._warmed = True

    def _captcha(self, action: str) -> str:
        return self.solver.solve_turnstile(
            self.base + "/temporary-email", SMAILPRO_SITEKEY, action=action
        )

    def create(self, username: str = "random", kind: str = "alias",
               domain: str = "gmail.com", server: str = "1") -> Inbox:
        self._warm()
        token = self._captcha(ACTION_CREATE)
        params = {
            "username": username,
            "type": kind,
            "domain": domain,
            "server": server,
        }
        response = self.client.get(
            self.base + "/app/create",
            params=params,
            headers={"Content-Type": "application/json", "x-captcha": token},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TempMailError(
                f"SmailPro create returned non-JSON (HTTP {response.status_code})"
            ) from exc

        if response.status_code != 200 or not data.get("address"):
            message = data.get("msg") or data.get("message") or response.text[:300]
            raise TempMailError(f"SmailPro create failed: {message}")

        inbox = Inbox(
            address=data["address"],
            timestamp=int(data.get("timestamp") or int(time.time())),
            key=data.get("key") or "",
            created_at=now_iso(),
            domain=data["address"].rsplit("@", 1)[-1],
        )
        return inbox

    def _payload(self, inbox: Inbox) -> str:
        response = self.client.post(
            self.base + "/app/inbox",
            json=[{
                "address": inbox.address,
                "timestamp": inbox.timestamp,
                "key": inbox.key,
            }],
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TempMailError(
                f"SmailPro inbox returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if response.status_code != 200 or not isinstance(data, list) or not data:
            message = data.get("msg") if isinstance(data, dict) else response.text[:300]
            raise TempMailError(f"SmailPro inbox failed: {message}")
        item = data[0]
        new_key = item.get("key")
        if new_key and new_key != inbox.key:
            # SmailPro rotates the key on every inbox call; keep state fresh.
            inbox.key = str(new_key)
            inbox.timestamp = int(item.get("timestamp") or inbox.timestamp)
            state = load_state()
            if inbox.address in state:
                state[inbox.address] = inbox.to_dict()
                save_state(state)
        payload = item.get("payload")
        if not payload:
            raise TempMailError("SmailPro inbox returned no payload")
        return str(payload)

    def messages(self, inbox: Inbox) -> list[dict[str, Any]]:
        self._warm()
        payload = self._payload(inbox)
        response = self.client.get(
            f"{SONJJ_API}/{mail_kind(inbox.address)}/inbox",
            params={"payload": payload},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TempMailError(
                f"Message API returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if response.status_code != 200:
            raise TempMailError(f"Message API failed: {data}")
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []

    def read(self, inbox: Inbox, mid: str) -> dict[str, Any]:
        self._warm()
        token = self._captcha(ACTION_MESSAGE)
        response = self.client.get(
            self.base + "/app/message",
            params={"email": inbox.address, "mid": mid},
            headers={"Content-Type": "application/json", "x-captcha": token},
        )
        if response.status_code != 200 or not response.text.strip():
            raise TempMailError(
                f"Cannot open message {mid}: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
        payload = response.text.strip()
        response = self.client.get(
            f"{SONJJ_API}/{mail_kind(inbox.address)}/message",
            params={"payload": payload},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TempMailError(
                f"Message API returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if response.status_code != 200:
            raise TempMailError(f"Cannot load message body: {data}")
        return data if isinstance(data, dict) else {"body": data}

    def wait_for_message(self, inbox: Inbox, timeout: float = 180.0,
                         interval: float = 5.0,
                         skip_mids: Iterable[str] | None = None,
                         progress: bool = False) -> dict[str, Any]:
        seen = set(skip_mids or [])
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                for message in self.messages(inbox):
                    mid = str(message.get("mid") or message.get("id") or "")
                    if mid and mid in seen:
                        continue
                    return message
                last_error = None
            except TempMailError as exc:
                last_error = exc
            if progress:
                print(".", end="", file=sys.stderr, flush=True)
            time.sleep(interval)
        if progress:
            print(file=sys.stderr)
        detail = f" (last error: {last_error})" if last_error else ""
        raise TempMailError(f"No new message within {timeout:.0f}s{detail}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def print_inbox(inbox: Inbox, as_json: bool) -> None:
    if as_json:
        print(json.dumps(inbox.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(inbox.address)
        print(f"  created : {inbox.created_at or 'unknown'}")
        print(f"  domain  : {inbox.domain or inbox.address.rsplit('@', 1)[-1]}")
        print(f"  saved   : {STATE_FILE}")


def print_messages(messages: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps([summarize(m) for m in messages], indent=2, ensure_ascii=False))
        return
    if not messages:
        print("No messages.")
        return
    for message in messages:
        item = summarize(message)
        print(f"[{item['mid']}] {item['subject'] or '(no subject)'}")
        print(f"  from : {item['from'] or '?'}")
        print(f"  date : {item['date'] or '?'}")
        if item["code"]:
            print(f"  code : {item['code']}")
        if item["link"]:
            print(f"  link : {item['link']}")
        if item["preview"]:
            print(f"  text : {item['preview']}")
        print()


def print_result(message: dict[str, Any], as_json: bool) -> None:
    item = summarize(message)
    if as_json:
        print(json.dumps(item, indent=2, ensure_ascii=False))
        return
    print(f"From    : {item['from'] or '?'}")
    print(f"Subject : {item['subject'] or '(no subject)'}")
    print(f"Date    : {item['date'] or '?'}")
    if item["code"]:
        print(f"Code    : {item['code']}")
    if item["link"]:
        print(f"Link    : {item['link']}")
    if not item["code"] and not item["link"]:
        print("No code or link found.")
    if item["preview"]:
        print("\n" + item["preview"])


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Allow --json after the subcommand as well as before it."""
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Repeat the global flags on subcommands so they work after the command."""
    _add_json_flag(parser)
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not start cf-scraper automatically",
    )
    parser.add_argument(
        "--proxy",
        default=argparse.SUPPRESS,
        help="cf-scraper proxy: host:port[:user:pass]",
    )
    parser.add_argument(
        "--cf-url",
        default=argparse.SUPPRESS,
        help="cf-scraper endpoint",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="temp_gmail.py",
        description="Free temporary Gmail via SmailPro + cf-scraper Turnstile solver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  temp_gmail.py new\n"
            "  temp_gmail.py wait user123@gmail.com --timeout 300\n"
            "  temp_gmail.py inbox user123@gmail.com\n"
            "  temp_gmail.py read user123@gmail.com 12345\n"
            "  temp_gmail.py token\n"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-autostart", action="store_true",
                        help="do not start cf-scraper automatically")
    parser.add_argument("--proxy", help="cf-scraper proxy: host:port[:user:pass]")
    parser.add_argument("--cf-url", default=CF_SERVER_URL, help="cf-scraper endpoint")

    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a free temporary Gmail")
    _add_common_flags(new)
    new.add_argument("--domain", default="gmail.com")
    new.add_argument("--username", default="random")
    new.add_argument("--type", default="alias", choices=["alias", "real"])
    new.add_argument("--server", default="1")
    new.add_argument("--no-save", action="store_true", help="do not write state file")

    list_cmd = sub.add_parser("list", help="list saved inboxes")
    _add_common_flags(list_cmd)

    inbox = sub.add_parser("inbox", help="list messages in an inbox")
    _add_common_flags(inbox)
    inbox.add_argument("address")

    wait = sub.add_parser("wait", help="wait for the next message and extract OTP/link")
    _add_common_flags(wait)
    wait.add_argument("address")
    wait.add_argument("--timeout", type=float, default=180.0)
    wait.add_argument("--interval", type=float, default=5.0)
    wait.add_argument("--skip-current", action="store_true",
                      help="ignore messages already present when starting")

    read = sub.add_parser("read", help="read one message body")
    _add_common_flags(read)
    read.add_argument("address")
    read.add_argument("mid")

    token = sub.add_parser("token", help="print a fresh Turnstile token (debug)")
    _add_common_flags(token)

    delete = sub.add_parser("delete", help="forget an inbox locally")
    delete.add_argument("address")

    sub.add_parser("stop-server", help="stop a cf-scraper started by this script")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "stop-server":
        stop_cf_server()
        return 0

    if args.command == "list":
        state = load_state()
        if args.json:
            print(json.dumps(state, indent=2, ensure_ascii=False))
        elif not state:
            print("No saved inboxes.")
        else:
            for entry in state.values():
                print(f"{entry['address']}  (created {entry.get('created_at', '?')})")
        return 0

    if args.command == "delete":
        if forget(args.address):
            print(f"Removed {args.address} from {STATE_FILE}")
        else:
            print(f"{args.address} is not in {STATE_FILE}")
        return 0

    proxy = Proxy.parse(args.proxy)

    solver = CfScraper(url=args.cf_url, proxy=proxy)
    ensure_cf_server(autostart=not args.no_autostart, url=args.cf_url)

    if args.command == "token":
        token = solver.solve_turnstile(
            SMAILPRO_BASE + "/temporary-email", SMAILPRO_SITEKEY, action=ACTION_CREATE
        )
        print(token)
        return 0

    mail = SmailPro(solver)
    try:
        if args.command == "new":
            inbox = mail.create(
                username=args.username,
                kind=args.type,
                domain=args.domain,
                server=args.server,
            )
            if not args.no_save:
                remember(inbox)
            print_inbox(inbox, args.json)
            return 0

        if args.command == "inbox":
            inbox = resolve_inbox(args.address)
            print_messages(mail.messages(inbox), args.json)
            return 0

        if args.command == "wait":
            inbox = resolve_inbox(args.address)
            skip: list[str] = []
            if args.skip_current:
                skip = [
                    str(m.get("mid") or m.get("id") or "")
                    for m in mail.messages(inbox)
                ]
            message = mail.wait_for_message(
                inbox, timeout=args.timeout, interval=args.interval,
                skip_mids=skip, progress=not args.json,
            )
            mid = str(message.get("mid") or message.get("id") or "")
            if mid and not (message.get("body") or message.get("html")):
                try:
                    full = mail.read(inbox, mid)
                    if full.get("body") or full.get("html"):
                        message = {**message, **full}
                except TempMailError as exc:
                    print(f"warning: could not load full body: {exc}", file=sys.stderr)
            print_result(message, args.json)
            return 0

        if args.command == "read":
            inbox = resolve_inbox(args.address)
            body = mail.read(inbox, args.mid)
            if args.json:
                print(json.dumps(body, indent=2, ensure_ascii=False))
            else:
                value = body.get("body") or body.get("html") or body
                if isinstance(value, str):
                    print(html_to_text(value))
                else:
                    print(json.dumps(value, indent=2, ensure_ascii=False))
            return 0
    finally:
        mail.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TempMailError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
