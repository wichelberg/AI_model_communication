#!/usr/bin/env python3
"""claude_comm - let a Claude Code session reach its owner and wait for an answer.

Backends: Telegram bot (recommended) or email (SMTP/IMAP). Standard library only.
Configuration comes from environment variables or a .env file next to this script
(see .env.example).

Commands:
  send    Send a one-way notification (no waiting).
  ask     Send a message and block until the owner answers (or timeout).
  wait    Wait for (more) answers on an existing thread.
  setup   Telegram only: link the bot to your chat (writes TELEGRAM_CHAT_ID to .env).
  test    Check the configuration; --send also sends a test question and waits.

Exit codes: 0 = ok / answer received, 2 = timed out without answer, 1 = error.
"""

import argparse
import email
import email.policy
import html
import imaplib
import json
import mimetypes
import os
import re
import secrets
import smtplib
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = (HERE / "claude_comm.py").as_posix()
ENV_FILE = HERE / ".env"
STATE_DIR = HERE / ".state"
TOKEN_RE = re.compile(r"CC[0-9a-f]{8}")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 2


# ---------------------------------------------------------------- config

def load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def set_env_value(key, value):
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.strip().startswith(key + "=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Config:
    def __init__(self):
        load_env_file()
        self.backend = self.get("CLAUDE_COMM_BACKEND") or ("telegram" if self.get("TELEGRAM_BOT_TOKEN") else "email")
        self.default_timeout = int(self.get("CLAUDE_COMM_DEFAULT_TIMEOUT", "7200"))

    @staticmethod
    def get(name, default=None):
        return os.environ.get(name, "").strip() or default

    def require(self, name):
        value = self.get(name)
        if not value:
            raise SystemExit(f"claude_comm: missing config {name} (set it in {ENV_FILE})")
        return value


# ---------------------------------------------------------------- shared helpers

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def safe_filename(name):
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "attachment"


class FileLock:
    """Cross-process lock via an exclusively created file; stale locks expire."""

    def __init__(self, path, stale_after=60):
        self.path = path
        self.stale_after = stale_after

    def acquire(self, wait):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink()
                        continue
                except OSError:
                    continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def release(self):
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- thread state

def new_token():
    # Letters+digits only so IMAP SUBJECT search treats it as one word.
    return "CC" + secrets.token_hex(4)


def thread_dir(token):
    if not TOKEN_RE.fullmatch(token or ""):
        raise SystemExit(f"claude_comm: invalid thread id {token!r}")
    return STATE_DIR / token


def load_state(token):
    path = thread_dir(token) / "thread.json"
    if not path.exists():
        raise SystemExit(f"claude_comm: unknown thread {token} (no {path})")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state):
    write_json(thread_dir(state["token"]) / "thread.json", state)


def new_state(backend_name, project, subject):
    return {
        "token": new_token(),
        "backend": backend_name,
        "project": project,
        "subject": subject,
        "cwd": os.getcwd(),
        "created": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- telegram backend

class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code


def encode_multipart(params, files):
    boundary = secrets.token_hex(16)
    parts = []
    for key, value in params.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for field, (filename, data) in files.items():
        filename = filename.replace('"', "_")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def split_text(text, size=4000):
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class TelegramBackend:
    """Talks to a Telegram bot.

    Only one process may call getUpdates at a time, so whoever holds the lock fetches
    all updates and routes each message into the inbox of the thread it belongs to
    (.state/<token>/inbox.jsonl). Waiters then just read their own inbox.
    """

    name = "telegram"
    poll_interval = 1
    long_poll = 10
    heartbeat_ttl = 30

    def __init__(self, cfg):
        self.bot_token = cfg.require("TELEGRAM_BOT_TOKEN")
        chat = cfg.get("TELEGRAM_CHAT_ID")
        self.chat_id = int(chat) if chat else None
        self.dir = STATE_DIR / "telegram"
        self.lock = FileLock(self.dir / "lock")

    # -- API

    def api(self, method, params=None, files=None, timeout=30):
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        if files:
            body, ctype = encode_multipart(params or {}, files)
        else:
            body, ctype = json.dumps(params or {}).encode(), "application/json"
        request = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read())
            except ValueError:
                data = {"error_code": exc.code, "description": exc.reason}
        if not data.get("ok"):
            raise TelegramError(data.get("error_code"), data.get("description"))
        return data["result"]

    def quiet(self, method, params):
        try:
            return self.api(method, params)
        except (TelegramError, OSError):
            return None

    def need_chat(self):
        if self.chat_id is None:
            raise SystemExit(f"claude_comm: TELEGRAM_CHAT_ID not set; run: python {SCRIPT} setup")

    # -- shared router state (only touch while holding the lock)

    def load_router(self):
        path = self.dir / "router.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"offset": 0, "msgmap": {}}

    def save_router(self, router):
        write_json(self.dir / "router.json", router)

    def with_router(self, fn):
        if not self.lock.acquire(wait=self.lock.stale_after + 10):
            raise TelegramError("lock", f"could not acquire {self.lock.path}")
        try:
            router = self.load_router()
            result = fn(router)
            self.save_router(router)
            return result
        finally:
            self.lock.release()

    # -- sending

    def send_text(self, text, buttons=None, reply_to=None):
        ids = []
        chunks = split_text(text)
        for i, chunk in enumerate(chunks):
            params = {"chat_id": self.chat_id, "text": chunk, "link_preview_options": {"is_disabled": True}}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = {"inline_keyboard": buttons}
            if reply_to and i == 0:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            ids.append(self.api("sendMessage", params)["message_id"])
        return ids

    def send(self, state, body, attachments, choices, expect_reply):
        self.need_chat()
        token = state["token"]
        tg = state.setdefault("telegram", {"message_ids": [], "delivered": 0})
        header = ("❓ " if expect_reply else "ℹ️ ") + state["project"]
        if not tg["message_ids"]:
            header += " · " + state["subject"]
        text = header + "\n\n" + body.strip()
        if choices:
            text += "\n\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(choices, 1))
        text += f"\n\n— {token}" + (" · cevap bekliyor" if expect_reply else "")
        buttons = [[{"text": f"{i}. {c}"[:64], "callback_data": f"{token}:{i}"}] for i, c in enumerate(choices, 1)]
        reply_to = tg["message_ids"][-1] if tg["message_ids"] else None

        ids = self.send_text(text, buttons, reply_to)
        for path in attachments:
            path = Path(path)
            result = self.api(
                "sendDocument",
                {"chat_id": str(self.chat_id), "caption": f"{path.name} — {token}"},
                files={"document": (path.name, path.read_bytes())},
                timeout=120,
            )
            ids.append(result["message_id"])
        tg["message_ids"] += ids
        self.with_router(lambda router: router["msgmap"].update({str(i): token for i in ids}))
        print(f"Sent via Telegram. Thread: {token}")

    # -- waiting

    def heartbeat(self, token):
        return thread_dir(token) / "waiting"

    def begin_wait(self, state):
        path = self.heartbeat(state["token"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def end_wait(self, state):
        try:
            self.heartbeat(state["token"]).unlink()
        except OSError:
            pass

    def poll(self, state, block):
        self.heartbeat(state["token"]).touch()
        self.sync(min(block, self.long_poll))
        inbox = thread_dir(state["token"]) / "inbox.jsonl"
        if not inbox.exists():
            return []
        tg = state.setdefault("telegram", {"message_ids": [], "delivered": 0})
        complete = inbox.read_text(encoding="utf-8").split("\n")[:-1]  # last piece may be partial
        new = complete[tg["delivered"]:]
        tg["delivered"] = len(complete)
        return [json.loads(line) for line in new if line.strip()]

    def sync(self, block):
        """Fetch new updates and route them, if no other process is doing it right now."""
        if not self.lock.acquire(wait=0):
            return
        try:
            router = self.load_router()
            try:
                updates = self.api(
                    "getUpdates",
                    {"offset": router["offset"], "timeout": int(block), "allowed_updates": ["message", "callback_query"]},
                    timeout=block + 20,
                )
                for update in updates:
                    router["offset"] = update["update_id"] + 1
                    try:
                        self.route(update, router)
                    except (TelegramError, OSError) as exc:
                        print(f"claude_comm: could not route update {update['update_id']}: {exc}", file=sys.stderr)
            finally:
                self.save_router(router)
        finally:
            self.lock.release()

    # -- routing (called by sync while holding the lock)

    def live_threads(self, router):
        latest = {}
        for msg_id, token in router["msgmap"].items():
            latest[token] = max(latest.get(token, 0), int(msg_id))
        live = []
        for beat in STATE_DIR.glob("CC*/waiting"):
            try:
                fresh = time.time() - beat.stat().st_mtime < self.heartbeat_ttl
            except OSError:
                continue
            if fresh:
                live.append(beat.parent.name)
        return sorted(live, key=lambda t: latest.get(t, 0), reverse=True)

    def project_of(self, token):
        try:
            return load_state(token)["project"]
        except (SystemExit, OSError, ValueError, KeyError):
            return token

    def note(self, router, token, text, reply_to=None):
        ids = self.send_text(text, reply_to=reply_to) if self.chat_id else []
        if token:
            router["msgmap"].update({str(i): token for i in ids})

    def deliver(self, token, entry):
        inbox = thread_dir(token) / "inbox.jsonl"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        with inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def route(self, update, router):
        if "callback_query" in update:
            self.route_button(update["callback_query"], router)
            return
        msg = update.get("message")
        if not msg:
            return
        if msg["chat"]["id"] != self.chat_id or msg.get("from", {}).get("id") != self.chat_id:
            print(f"claude_comm: ignored message from chat {msg['chat']['id']} (not the owner)", file=sys.stderr)
            return

        text = msg.get("text") or msg.get("caption") or ""
        if text.strip().split("@")[0] in ("/start", "/status"):
            self.send_status(router)
            return

        live = self.live_threads(router)
        token = None
        reply = msg.get("reply_to_message")
        if reply and str(reply["message_id"]) in router["msgmap"]:
            token = router["msgmap"][str(reply["message_id"])]
        if not token:
            mentioned = TOKEN_RE.search(text)
            if mentioned and (STATE_DIR / mentioned.group()).is_dir():
                token = mentioned.group()
        if not token and live:
            token = live[0]
        if not token:
            self.note(router, None, "Şu an cevap bekleyen Claude oturumu yok. Belirli bir konuşmaya yazmak için o mesajı yanıtla (reply).",
                      reply_to=msg["message_id"])
            return

        attachments = self.download(msg, token)
        if not text and not attachments:
            text = "[desteklenmeyen mesaj türü: lütfen yazı olarak gönder]"
        self.deliver(token, {
            "date": datetime.fromtimestamp(msg["date"]).isoformat(timespec="seconds"),
            "text": text,
            "attachments": attachments,
        })
        self.quiet("setMessageReaction", {
            "chat_id": self.chat_id, "message_id": msg["message_id"],
            "reaction": [{"type": "emoji", "emoji": "👍"}],
        })
        if token not in live:
            self.note(router, token, f"→ {self.project_of(token)} ({token}) şu an beklemiyor; bir sonraki kontrolde görecek.",
                      reply_to=msg["message_id"])
        elif len(live) > 1:
            self.note(router, token, f"→ {self.project_of(token)} ({token})", reply_to=msg["message_id"])

    def route_button(self, query, router):
        if query.get("from", {}).get("id") != self.chat_id:
            return
        data = query.get("data") or ""
        token, _, index = data.partition(":")
        message = query.get("message") or {}
        rows = (message.get("reply_markup") or {}).get("inline_keyboard", [])
        label = next((b["text"] for row in rows for b in row if b.get("callback_data") == data), index)
        self.quiet("answerCallbackQuery", {"callback_query_id": query["id"], "text": "✓"})
        if message:
            self.quiet("editMessageReplyMarkup", {
                "chat_id": self.chat_id, "message_id": message["message_id"], "reply_markup": {"inline_keyboard": []},
            })
        if not TOKEN_RE.fullmatch(token) or not (STATE_DIR / token).is_dir():
            return
        self.deliver(token, {
            "date": datetime.now().isoformat(timespec="seconds"),
            "text": f"[button] {label}",
            "choice": int(index) if index.isdigit() else index,
            "attachments": [],
        })
        self.note(router, token, f"✓ Seçildi: {label}", reply_to=message.get("message_id"))

    def download(self, msg, token):
        files = []
        if "document" in msg:
            files.append((msg["document"]["file_id"], msg["document"].get("file_name") or "document"))
        if "photo" in msg:
            files.append((msg["photo"][-1]["file_id"], f"photo_{msg['message_id']}.jpg"))
        saved = []
        for file_id, name in files:
            info = self.api("getFile", {"file_id": file_id})
            url = f"https://api.telegram.org/file/bot{self.bot_token}/{info['file_path']}"
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            target = thread_dir(token) / "attachments" / str(msg["message_id"])
            target.mkdir(parents=True, exist_ok=True)
            path = target / safe_filename(name)
            path.write_bytes(data)
            saved.append(str(path))
        return saved

    def send_status(self, router):
        live = self.live_threads(router)
        if live:
            lines = "\n".join(f"• {self.project_of(t)} — {t}" for t in live)
            text = f"Cevap bekleyen oturumlar:\n{lines}\n\nYanıtla (reply) ya da düz yaz: düz mesaj en son soruyu soran oturuma gider."
        else:
            text = "Şu an cevap bekleyen Claude oturumu yok."
        self.note(router, None, text)

    # -- setup / test

    def setup(self):
        me = self.api("getMe")
        print(f"Bot: @{me['username']}")
        print(f"Telegram'da @{me['username']} botuna /start yaz. 120 saniye bekleniyor...", flush=True)
        if not self.lock.acquire(wait=self.lock.stale_after + 10):
            raise TelegramError("lock", "busy")
        try:
            router = self.load_router()
            chat = None
            deadline = time.monotonic() + 120
            while chat is None and time.monotonic() < deadline:
                updates = self.api("getUpdates", {"offset": router["offset"], "timeout": 20}, timeout=40)
                for update in updates:
                    router["offset"] = update["update_id"] + 1
                    msg = update.get("message")
                    if msg and msg["chat"]["type"] == "private":
                        chat = msg["chat"]
            self.save_router(router)
        finally:
            self.lock.release()
        if chat is None:
            print("Mesaj gelmedi. /start yazıp tekrar dene.")
            return EXIT_TIMEOUT
        self.chat_id = chat["id"]
        set_env_value("TELEGRAM_CHAT_ID", str(chat["id"]))
        who = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username")
        print(f"Bağlandı: {who} (chat id {chat['id']}), .env dosyasına yazıldı.")
        self.send_text("✅ Bağlantı kuruldu. Claude oturumları artık sana buradan yazabilir.\n/status: bekleyen oturumları gösterir.")
        return EXIT_OK

    def check(self):
        me = self.api("getMe")
        print(f"Telegram bot OK: @{me['username']}")
        self.need_chat()
        print(f"Chat id: {self.chat_id}")


# ---------------------------------------------------------------- email backend

QUOTE_HEADER = re.compile(
    r"^(On\s.*wrote:"                 # Gmail EN
    r"|.*tarihinde.*yazd[ıi]:"        # Gmail TR
    r"|Am\s.*schrieb.*:"              # Gmail DE
    r"|-+\s*Original Message\s*-+"
    r"|-+\s*Urspr.ngliche Nachricht\s*-+"
    r"|From:\s.*)$",
    re.IGNORECASE,
)


def strip_quoted(text):
    lines = text.replace("\r\n", "\n").split("\n")
    kept = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        joined = stripped + " " + lines[i + 1].strip() if i + 1 < len(lines) else stripped
        if QUOTE_HEADER.match(stripped) or QUOTE_HEADER.match(joined):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    result = "\n".join(kept).strip()
    return result or text.strip()


def html_to_text(markup):
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", markup)
    markup = re.sub(r"(?is)<blockquote.*?</blockquote>", "", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def extract_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    return text


class EmailBackend:
    name = "email"

    def __init__(self, cfg):
        self.address = cfg.require("CLAUDE_MAIL_ADDRESS")
        # Gmail shows app passwords as "abcd efgh ijkl mnop"; spaces are not part of it.
        self.password = cfg.require("CLAUDE_MAIL_PASSWORD").replace(" ", "")
        self.owner = cfg.require("CLAUDE_MAIL_OWNER").lower()
        self.smtp_host = cfg.get("CLAUDE_MAIL_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(cfg.get("CLAUDE_MAIL_SMTP_PORT", "465"))
        self.imap_host = cfg.get("CLAUDE_MAIL_IMAP_HOST", "imap.gmail.com")
        self.imap_port = int(cfg.get("CLAUDE_MAIL_IMAP_PORT", "993"))
        self.require_auth = cfg.get("CLAUDE_MAIL_REQUIRE_AUTH", "true").lower() not in ("0", "false", "no")
        self.poll_interval = int(cfg.get("CLAUDE_MAIL_POLL_INTERVAL", "30"))

    def send(self, state, body, attachments, choices, expect_reply):
        token = state["token"]
        em = state.setdefault("email", {"message_ids": [], "seen_uids": []})
        subject = f"[Claude | {state['project']}] {state['subject']} #{token}"
        if em["message_ids"]:
            subject = "Re: " + subject
        if choices:
            body = body.rstrip() + "\n\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(choices, 1))

        msg = EmailMessage()
        msg["From"] = self.address
        msg["To"] = self.owner
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        message_id = make_msgid(domain=self.address.split("@", 1)[1])
        msg["Message-ID"] = message_id
        if em["message_ids"]:
            msg["In-Reply-To"] = em["message_ids"][-1]
            msg["References"] = " ".join(em["message_ids"])
        msg.set_content(
            body.rstrip()
            + f"\n\n--\nProject: {state['project']}\nDirectory: {state['cwd']}\nHost: {socket.gethostname()}\n"
            + f"Thread: {token}\n"
            + ("Reply to this email to answer. Only the new text above the quoted part is read." if expect_reply else "")
        )
        for attachment in attachments:
            path = Path(attachment)
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=60) as smtp:
            smtp.login(self.address, self.password)
            smtp.send_message(msg)
        em["message_ids"].append(message_id)
        print(f"Sent to {self.owner}. Thread: {token}")

    def begin_wait(self, state):
        pass

    def end_wait(self, state):
        pass

    def sender_is_authentic(self, msg):
        """Check that the message really comes from the owner, not a spoofed From header."""
        if parseaddr(msg.get("From", ""))[1].lower() != self.owner:
            return False
        if not self.require_auth:
            return True
        # The topmost Authentication-Results header is the one our own mail server added;
        # anything below it could have been written by the sender.
        results = msg.get_all("Authentication-Results") or []
        if not results:
            return False
        top = str(results[0]).lower()
        if "dmarc=pass" in top:
            return True
        return f"dkim=pass header.i=@{self.owner.split('@', 1)[1]}" in top

    def poll(self, state, block):
        token = state["token"]
        em = state.setdefault("email", {"message_ids": [], "seen_uids": []})
        answers = []
        with imaplib.IMAP4_SSL(self.imap_host, self.imap_port) as imap:
            imap.login(self.address, self.password)
            imap.select("INBOX")
            typ, data = imap.uid("SEARCH", None, "FROM", f'"{self.owner}"', "SUBJECT", f'"{token}"')
            if typ != "OK":
                raise imaplib.IMAP4.error(f"SEARCH failed: {data}")
            for raw_uid in data[0].split():
                uid = raw_uid.decode()
                if uid in em["seen_uids"]:
                    continue
                typ, fetched = imap.uid("FETCH", raw_uid, "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    continue
                msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
                em["seen_uids"].append(uid)
                if token not in str(msg.get("Subject", "")):
                    continue
                if not self.sender_is_authentic(msg):
                    print(f"claude_comm: ignored email uid {uid}: sender failed authentication", file=sys.stderr)
                    continue
                imap.uid("STORE", raw_uid, "+FLAGS", "(\\Seen)")
                if msg.get("Message-ID"):
                    em["message_ids"].append(msg["Message-ID"])
                answers.append({
                    "date": msg.get("Date", ""),
                    "text": strip_quoted(extract_text(msg)),
                    "attachments": self.save_attachments(token, uid, msg),
                })
        return answers

    def save_attachments(self, token, uid, msg):
        saved = []
        target = thread_dir(token) / "attachments" / f"mail{uid}"
        for part in msg.iter_attachments():
            name = part.get_filename()
            if not name:
                continue
            target.mkdir(parents=True, exist_ok=True)
            path = target / safe_filename(name)
            path.write_bytes(part.get_payload(decode=True) or b"")
            saved.append(str(path))
        return saved

    def check(self):
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=60) as smtp:
            smtp.login(self.address, self.password)
        print(f"SMTP login OK ({self.smtp_host})")
        with imaplib.IMAP4_SSL(self.imap_host, self.imap_port) as imap:
            imap.login(self.address, self.password)
            imap.select("INBOX")
        print(f"IMAP login OK ({self.imap_host})")


BACKENDS = {"telegram": TelegramBackend, "email": EmailBackend}


def make_backend(cfg, name=None):
    name = name or cfg.backend
    if name not in BACKENDS:
        raise SystemExit(f"claude_comm: unknown backend {name!r} (use telegram or email)")
    return BACKENDS[name](cfg)


# ---------------------------------------------------------------- waiting

def is_fatal(exc):
    if isinstance(exc, TelegramError):
        return exc.code in (401, 403, 404)
    return "AUTHENTICATIONFAILED" in str(exc).upper()


def wait_for_answer(backend, state, timeout):
    token = state["token"]
    deadline = time.monotonic() + timeout
    failures = 0
    print(f"Waiting for answer on thread {token} (timeout {timeout}s)...", file=sys.stderr, flush=True)
    backend.begin_wait(state)
    try:
        while True:
            try:
                answers = backend.poll(state, max(0, deadline - time.monotonic()))
                failures = 0
            except (OSError, imaplib.IMAP4.error, TelegramError) as exc:
                if is_fatal(exc):
                    raise
                failures += 1
                print(f"claude_comm: connection error ({failures}/10): {exc}", file=sys.stderr, flush=True)
                if failures >= 10:
                    raise
                answers = []
            save_state(state)
            if answers:
                print_answers(token, answers)
                return EXIT_OK
            if time.monotonic() >= deadline:
                print(f"NO ANSWER within {timeout}s on thread {token}.")
                print(f"Keep waiting with: python {SCRIPT} wait --thread {token}")
                return EXIT_TIMEOUT
            time.sleep(min(backend.poll_interval, max(0.1, deadline - time.monotonic())))
    finally:
        backend.end_wait(state)


def print_answers(token, answers):
    for answer in answers:
        print(f"=== ANSWER FROM OWNER (thread {token}, {answer['date']}) ===")
        print(answer["text"])
        print("=== END ANSWER ===")
        for path in answer["attachments"]:
            print(f"Attachment saved: {path}")
    print(f"To reply in the same thread: python {SCRIPT} ask --thread {token} --body-file -")


# ---------------------------------------------------------------- commands

def read_body(args):
    if args.body is not None:
        return args.body
    if args.body_file:
        if args.body_file == "-":
            return sys.stdin.read()
        return Path(args.body_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("claude_comm: provide --body, --body-file, or pipe the body on stdin")


def prepare(cfg, args):
    for path in args.attach or []:
        if not Path(path).is_file():
            raise SystemExit(f"claude_comm: attachment not found: {path}")
    body = read_body(args)
    if args.thread:
        state = load_state(args.thread)
        backend = make_backend(cfg, state["backend"])
    else:
        backend = make_backend(cfg)
        state = new_state(backend.name, args.project or Path.cwd().name, args.subject or "Message from Claude")
    return backend, state, body


def timeout_of(cfg, args):
    return cfg.default_timeout if args.timeout is None else args.timeout


def cmd_send(cfg, args):
    backend, state, body = prepare(cfg, args)
    backend.send(state, body, args.attach or [], args.choice or [], expect_reply=False)
    save_state(state)
    return EXIT_OK


def cmd_ask(cfg, args):
    backend, state, body = prepare(cfg, args)
    save_state(state)
    backend.begin_wait(state)  # so a very fast answer is already routed to this thread
    try:
        backend.send(state, body, args.attach or [], args.choice or [], expect_reply=True)
        save_state(state)
    except BaseException:
        backend.end_wait(state)
        raise
    return wait_for_answer(backend, state, timeout_of(cfg, args))


def cmd_wait(cfg, args):
    state = load_state(args.thread)
    return wait_for_answer(make_backend(cfg, state["backend"]), state, timeout_of(cfg, args))


def cmd_setup(cfg, args):
    backend = make_backend(cfg, "telegram")
    return backend.setup()


def cmd_test(cfg, args):
    backend = make_backend(cfg)
    backend.check()
    if not args.send:
        return EXIT_OK
    state = new_state(backend.name, args.project or "claude_comm", "Bağlantı testi")
    save_state(state)
    backend.begin_wait(state)
    backend.send(state, "Test mesajı. Bir butona bas ya da bir şey yaz (5 dakika bekliyorum).", [],
                 ["Çalışıyor", "Sorun var"], expect_reply=True)
    save_state(state)
    return wait_for_answer(backend, state, 300)


def build_parser():
    parser = argparse.ArgumentParser(description="Reach the owner of this machine (Telegram or email) and wait for answers.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_message_args(p):
        p.add_argument("--subject", "-s", help="Short subject line (new threads only)")
        p.add_argument("--body", "-b", help="Message body text")
        p.add_argument("--body-file", "-f", help="Read body from file, or '-' for stdin")
        p.add_argument("--choice", "-c", action="append", help="Answer option, shown as a button (repeatable)")
        p.add_argument("--attach", "-a", action="append", help="File to attach (repeatable)")
        p.add_argument("--thread", "-t", help="Continue an existing thread instead of starting one")
        p.add_argument("--project", "-p", help="Project name shown in the message (default: cwd name)")

    def add_wait_args(p):
        p.add_argument("--timeout", type=int, help="Seconds to wait (default CLAUDE_COMM_DEFAULT_TIMEOUT, 7200)")

    add_message_args(sub.add_parser("send", help="Send a notification without waiting"))
    ask = sub.add_parser("ask", help="Send a message and wait for the answer")
    add_message_args(ask)
    add_wait_args(ask)
    wait = sub.add_parser("wait", help="Wait for answers on an existing thread")
    wait.add_argument("--thread", "-t", required=True)
    add_wait_args(wait)
    sub.add_parser("setup", help="Telegram: link the bot to your chat")
    test = sub.add_parser("test", help="Check the configuration")
    test.add_argument("--send", action="store_true", help="Also send a test question and wait for the answer")
    test.add_argument("--project", "-p")
    return parser


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    cfg = Config()
    commands = {"send": cmd_send, "ask": cmd_ask, "wait": cmd_wait, "setup": cmd_setup, "test": cmd_test}
    try:
        return commands[args.command](cfg, args)
    except (smtplib.SMTPException, imaplib.IMAP4.error, OSError, TelegramError) as exc:
        print(f"claude_comm: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
