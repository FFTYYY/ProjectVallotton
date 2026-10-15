#!/usr/bin/env python3
"""
Vallotton 快速启动：本机、调试模式、一条命令。

    python quickstart.py
    python quickstart.py --db D:/vallotton/my.sqlite3 --port 5173 --api-port 8000

做什么：第一次运行没有 .env 就问你要 DeepSeek 的 key、NovelAI 的 token 和
登录账号，写一份只含本机必需项的 .env；然后启动后端（Django，sqlite），等它
就绪，打开浏览器，在这个窗口里画一块仪表盘：服务活没活、两个账号好不好、
后端忙不忙、最近有没有出错。日志不上屏，写在 tmp/quickstart-*.log。
Ctrl+C 或 q 停。

前端：有 frontend/dist（构建好的前端，发布包里带着）就由 Django 托管
（SERVE_FRONTEND=1），只有一个进程、一个端口，不需要 Node.js；没有 dist
（开发时）就另起 Vite 开发服务器，这时需要 Node 20+。

这是给一个人在自己电脑上用的：只听 127.0.0.1，不对外。要对外、要 Postgres、
要 MCP，走 DEPLOY.md 的标准流程。

通常不直接运行它：start.bat（Windows）和 start.sh（macOS/Linux）第一次运行时
在 .runtime/ 里建一个虚拟环境——本机有 Python 3.12+ 就用它，没有就下载一份——
然后用那里的 Python 来跑。
仪表盘靠 rich 画；没有就装，装不上就退到一行一行的朴素输出（--plain 也是它）。
"""

from __future__ import annotations

import argparse
import collections
import http.cookiejar
import importlib
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NoReturn

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
ENV_FILE = ROOT / ".env"
#: a built frontend. When it is here, Django serves it (SERVE_FRONTEND) and
#: Node.js is not needed at all; without it, the Vite dev server runs it.
DIST = FRONTEND / "dist"
WIN = os.name == "nt"

#: a backend request-log line: method, path, status
REQUEST_LINE = re.compile(r'^\[\d\d/\w{3}/\d{4} \d\d:\d\d:\d\d\] "([A-Z]+) (\S+) HTTP/1\.[01]" (\d{3}) ')
#: vite colours its output even into a pipe
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: a log line worth keeping on the dashboard even though it is not an error
NOTABLE = re.compile(r"\[(WARNING|ERROR|CRITICAL)\]|Traceback|Error|error:|failed|refus", re.I)
#: the bars of the request sparkline, quiet to busy
BARS = " ▁▂▃▄▅▆▇█"
#: one bucket of the sparkline, in seconds
BUCKET = 5

ENV_TEMPLATE = """# Vallotton 本机配置，由 quickstart.py 在第一次启动时生成。
# 只放本机需要的项。要换厂商、开 MCP、上服务器，看 .env.example 和 DEPLOY.md。

LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY={deepseek}
NOVELAI_API_KEY={novelai}

# 网页登录
APP_USERNAME={username}
APP_PASSWORD={password}
# Django 管理后台（/admin/），平时用不到；密码随机生成
ADMIN_USERNAME=admin
ADMIN_PASSWORD={admin_password}

DJANGO_SECRET_KEY={secret}
"""

#: the name in block letters -- a terminal has one font and one size, so a
#: bigger "font" is drawn rather than chosen
LOGO = """\
█   █  ████  █     █      ████  ████████ ████████  ████  █    █
 █ █  █    █ █     █     █    █    ██       ██    █    █ ██   █
 █ █  ██████ █     █     █    █    ██       ██    █    █ █ █  █
 █ █  █    █ █     █     █    █    ██       ██    █    █ █  █ █
  █   █    █ █████ █████  ████     ██       ██     ████  █   ██"""


# ---------------------------------------------------------------- plain output

def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> NoReturn:
    say(f"\n✗ {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- prerequisites

def node_path() -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        die("没找到 Node.js。装 20 以上的版本：https://nodejs.org/ ，装完重开终端再运行")
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"v(\d+)", out)
    if m and int(m.group(1)) < 20:
        die(f"Node.js 需要 20 以上，现在是 {out}")
    return node, out


def npm_path() -> str:
    npm = shutil.which("npm.cmd") if WIN else None
    npm = npm or shutil.which("npm")
    if not npm:
        die("没找到 npm（它随 Node.js 一起装）")
    return npm


def missing_modules(names: tuple[str, ...]) -> list[str]:
    out = []
    for mod in names:
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append(mod)
    return out


def pip_install(*what: str) -> bool:
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *what], capture_output=True)
    return r.returncode == 0


# ---------------------------------------------------------------- .env

def read_env(path: Path) -> dict[str, str]:
    """The same reading the backend does (settings._load_dotenv)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(path: Path, *, deepseek: str, novelai: str, username: str, password: str) -> None:
    path.write_text(ENV_TEMPLATE.format(
        deepseek=deepseek, novelai=novelai, username=username, password=password,
        admin_password=secrets.token_urlsafe(18), secret=secrets.token_urlsafe(48),
    ), encoding="utf-8", newline="\n")


def ask(prompt: str, *, default: str = "", must_start: str = "") -> str:
    while True:
        shown = f"  {prompt}（默认 {default}）：" if default else f"  {prompt}："
        raw = input(shown)
        value = raw.strip() or default
        if not value:
            say("    不能为空")
            continue
        if must_start and not value.startswith(must_start):
            say(f"    看起来不对：应该以 {must_start} 开头")
            continue
        return value


def first_run_env() -> None:
    if not sys.stdin.isatty():
        die("第一次运行需要在终端里回答几个问题，或者自己写一份 .env（照 .env.example，"
            "至少要 LLM_PROVIDER、DEEPSEEK_API_KEY、NOVELAI_API_KEY、APP_USERNAME、"
            "APP_PASSWORD、ADMIN_USERNAME、ADMIN_PASSWORD）")
    say("  第一次运行，先把必需的几项写进 .env（之后可以直接改这个文件）。")
    deepseek = ask("DeepSeek API key（platform.deepseek.com，sk- 开头）", must_start="sk-")
    novelai = ask("NovelAI persistent token（账号设置里生成，pst- 开头）", must_start="pst-")
    username = ask("网页登录用户名", default="user")
    password = ask("网页登录密码")
    write_env(ENV_FILE, deepseek=deepseek, novelai=novelai, username=username, password=password)


def check_env(values: dict[str, str]) -> list[str]:
    """What an existing .env still lacks for a local run."""
    provider = values.get("LLM_PROVIDER") or "deepseek"   # the backend's own default
    need = {
        "deepseek": ["DEEPSEEK_API_KEY"],
        "grok": ["GROK_API_KEY"],
        "fake": [],
    }.get(provider)
    if need is None:
        return [f"LLM_PROVIDER={provider} 不认识（deepseek 或 grok）"]
    missing = [k for k in need + ["NOVELAI_API_KEY", "APP_USERNAME", "APP_PASSWORD",
                                   "ADMIN_USERNAME", "ADMIN_PASSWORD"] if not values.get(k)]
    if "LLM_PROVIDER" not in values:
        missing.insert(0, "LLM_PROVIDER（没写就是 deepseek）")
    return missing


def describe_db(path: Path) -> str:
    """「已有 · 37 个对话 · 12.4 MB」, or 「新建」."""
    if not path.exists():
        return "新建"
    size = path.stat().st_size
    convs = ""
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as con:
            n = con.execute("select count(*) from core_conversation where is_archived = 0").fetchone()[0]
            convs = f" · {n} 个对话"
    except sqlite3.Error:
        pass
    return f"已有{convs} · {size / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------- the two servers

class Child:
    """One server: its process, its log file, what its output says."""

    def __init__(self, name: str, log_path: Path) -> None:
        self.name = name
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None
        self.state = "未启动"
        self.lock = threading.Lock()
        self.recent: collections.deque[tuple[str, str]] = collections.deque(maxlen=40)   # (time, line)
        self.errors = 0
        self.requests = 0
        self.buckets: collections.deque[int] = collections.deque([0] * 24, maxlen=24)
        self._bucket_at = time.monotonic()

    def start(self, cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
        # Its own group / session: the console's Ctrl+C reaches only us, and
        # we stop it ourselves -- so node never gets a stray interrupt and
        # Windows never asks "Terminate batch job (Y/N)?". No stdin either:
        # vite reads the terminal for its own shortcuts (q quits it, h for
        # help), and with the console shared it ate the keys meant for us.
        if sys.platform == "win32":
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        self.state = "启动中"
        threading.Thread(target=self._pump, daemon=True).start()

    def _tick(self) -> None:
        now = time.monotonic()
        while now - self._bucket_at >= BUCKET:
            self.buckets.append(0)
            self._bucket_at += BUCKET

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        with open(self.log_path, "w", encoding="utf-8") as log:
            for raw in iter(self.proc.stdout.readline, b""):
                line = ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
                log.write(line + "\n")
                log.flush()
                if not line.strip():
                    continue
                stamp = datetime.now().strftime("%H:%M:%S")
                with self.lock:
                    self._tick()
                    m = REQUEST_LINE.match(line)
                    if m:
                        self.requests += 1
                        self.buckets[-1] += 1
                        if int(m.group(3)) >= 400:
                            self.errors += 1
                            self.recent.append((stamp, f"{m.group(1)} {m.group(2)} → {m.group(3)}"))
                    elif NOTABLE.search(line):
                        if not line.startswith("WARNING: This is a development server"):
                            self.errors += 1
                            self.recent.append((stamp, line[:160]))

    def snapshot(self) -> tuple[int, int, list[int], list[tuple[str, str]]]:
        with self.lock:
            self._tick()
            return self.requests, self.errors, list(self.buckets), list(self.recent)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        p = self.proc
        if p is None or p.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            for _ in range(50):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                os.killpg(p.pid, signal.SIGKILL)
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.state = "已停止"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def run_step(cmd: list[str], cwd: Path, env: dict[str, str]) -> str | None:
    """None when it worked, else the tail of what it said."""
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True)
    if r.returncode == 0:
        return None
    return (r.stdout + r.stderr).decode("utf-8", errors="replace")[-3000:]


# ---------------------------------------------------------------- talking to the backend

class Api:
    """A logged-in client of the backend, for the dashboard's questions.
    Never through a proxy: 127.0.0.1 is not the proxy's business."""

    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base
        self.username, self.password = username, password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False
        self.login_failed_at = 0.0

    def _call(self, path: str, body: dict | None = None, timeout: float = 20) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            for c in self.jar:
                if c.name == "csrftoken":
                    req.add_header("X-CSRFToken", c.value or "")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            return 0, None

    def ping(self) -> float | None:
        """round trip in ms, or None"""
        t = time.perf_counter()
        status, _ = self._call("/api/auth/me", timeout=3)
        return (time.perf_counter() - t) * 1000 if status == 200 else None

    def login(self) -> bool:
        # A failed login is not retried for ten minutes: the backend locks
        # an address out after eight failures, and a dashboard that kept
        # trying a wrong password would lock the browser out with it.
        if time.monotonic() - self.login_failed_at < 600:
            return False
        self._call("/api/csrf", timeout=5)
        status, _ = self._call("/api/auth/login", {"username": self.username, "password": self.password}, timeout=10)
        self.logged_in = status == 200
        if not self.logged_in:
            self.login_failed_at = time.monotonic()
        return self.logged_in

    def get(self, path: str, timeout: float = 25) -> Any:
        if not self.logged_in and not self.login():
            return None
        status, data = self._call(path, timeout=timeout)
        if status == 401:
            self.logged_in = False
            return None
        return data if status == 200 else None


def http_ok(url: str) -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------- what the dashboard knows

class Board:
    """Everything the screen shows, kept current by the pollers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.backend_ms: float | None = None
        self.frontend_ok = False
        self.quota: dict | None = None
        self.quota_at = 0.0
        self.quota_error = ""
        self.running: dict | None = None
        self.note = ""          # one line at the foot, e.g. 「已打开浏览器」
        self.hosted = False     # the backend serves the built frontend; no second process
        self.note_at = 0.0

    def set_note(self, text: str) -> None:
        with self.lock:
            self.note, self.note_at = text, time.monotonic()


def poll(board: Board, api: Api, web_url: str, stop: threading.Event) -> None:
    """health every 2 s, what is running every 5 s, the accounts every 60 s"""
    last_running = last_quota = 0.0
    while not stop.is_set():
        ms = api.ping()
        ok = http_ok(web_url + "/")
        with board.lock:
            board.backend_ms, board.frontend_ok = ms, ok
        now = time.monotonic()
        if ms is not None and now - last_running >= 5:
            data = api.get("/api/app-settings", timeout=10)
            with board.lock:
                board.running = (data or {}).get("running") if isinstance(data, dict) else None
            last_running = now
        if ms is not None and now - last_quota >= 60:
            data = api.get("/api/quota", timeout=40)
            with board.lock:
                if isinstance(data, dict):
                    board.quota, board.quota_at, board.quota_error = data, now, ""
                else:
                    board.quota_error = "拿不到账号状态" if api.logged_in else "登录失败：.env 的账号密码和数据库里的不一致？"
            last_quota = now
        stop.wait(2)


def keys(q: "queue.Queue[str]", stop: threading.Event) -> None:
    """single keystrokes, without Enter; only when stdin is a real terminal"""
    if not sys.stdin.isatty():
        return
    if sys.platform == "win32":
        import msvcrt
        while not stop.is_set():
            if msvcrt.kbhit():
                q.put(msvcrt.getwch())
            else:
                time.sleep(0.05)
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop.is_set():
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    q.put(sys.stdin.read(1))
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------- the screen

def sparkline(buckets: list[int]) -> str:
    top = max(buckets) or 1
    return "".join(BARS[min(len(BARS) - 1, round(b / top * (len(BARS) - 1)))] for b in buckets)


def uptime(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def draw(rich: Any, board: Board, backend: Child, frontend: Child, web_url: str, api_url: str,
         db: Path, media: Path, username: str) -> Any:
    """One frame. `rich` is the imported package namespace (see dashboard())."""
    Text, Table, Panel, Group = rich["Text"], rich["Table"], rich["Panel"], rich["Group"]
    box = rich["box"]

    with board.lock:
        ms, fe_ok = board.backend_ms, board.frontend_ok
        quota, quota_error, quota_age = board.quota, board.quota_error, time.monotonic() - board.quota_at
        running = board.running
        note, note_age = board.note, time.monotonic() - board.note_at
    b_req, b_err, b_buckets, b_recent = backend.snapshot()
    _, f_err, _, f_recent = frontend.snapshot()

    # ---- header: the name, big; the address, bigger than anything else
    head = Text(LOGO, style="bold #4166b8")
    head.append(f"\n\n本机 · 调试模式   运行 {uptime(time.monotonic() - board.started)}", style="dim")
    open_line = Text()
    open_line.append("  打开  ", style="bold")
    open_line.append(web_url, style="bold underline #2a4380")
    open_line.append(f"     登录 {username}", style="dim")

    # ---- services
    def dot(alive: bool, ready: bool) -> Any:
        if not alive:
            return Text("✗ 已退出", style="bold red")
        return Text("● 运行中", style="bold green") if ready else Text("◌ 启动中", style="yellow")

    svc = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    svc.add_column(); svc.add_column(); svc.add_column(style="dim"); svc.add_column(justify="right")
    host = lambda url: url.split("//", 1)[-1]   # noqa: E731 -- the scheme is in the header already
    svc.add_row(dot(backend.alive(), ms is not None), Text("后端", style="bold"), host(api_url),
                Text(f"{ms:.0f} ms" if ms is not None else "无响应", style="green" if ms is not None and ms < 200 else "yellow"))
    if board.hosted:
        svc.add_row(Text("● 由后端托管", style="bold green") if fe_ok else Text("◌ 启动中", style="yellow"),
                    Text("前端", style="bold"), host(web_url),
                    Text("正常" if fe_ok else "无响应", style="green" if fe_ok else "yellow"))
    else:
        svc.add_row(dot(frontend.alive(), fe_ok), Text("前端", style="bold"), host(web_url),
                    Text("正常" if fe_ok else "无响应", style="green" if fe_ok else "yellow"))

    # ---- accounts
    acct = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    acct.add_column(style="bold"); acct.add_column()
    if quota:
        nai = quota.get("novelai") or {}
        if nai.get("ok"):
            s = Text(f"{nai.get('tier', '?')} · ", style="")
            s.append("有效" if nai.get("active") else "已过期", style="green" if nai.get("active") else "red")
            if nai.get("usage_percent") is not None:
                s.append(f" · V5 用量 {nai['usage_percent']}%", style="dim")
        else:
            s = Text(f"查不到：{nai.get('error', '?')}", style="red")
        acct.add_row("NovelAI", s)
        llm = quota.get("llm") or {}
        bal = llm.get("balance") or {}
        acct.add_row("大模型", Text.assemble(llm.get("label") or llm.get("provider", "?"),
                                             (f" · {llm.get('model', '')}", "dim")))
        if bal.get("ok"):
            money = Text(f"{bal.get('balance', 0):.2f} {bal.get('currency', '')}",
                         style="green" if bal.get("balance", 0) > 1 else "red")
        elif bal:
            money = Text(f"查不到：{bal.get('error', '?')}", style="red")
        else:
            money = Text("按用量计费" if llm.get("priced") else "—", style="dim")
        if llm.get("priced"):
            money.append(f" · 今天 ${llm.get('today_usd', 0):.2f}", style="dim")
        acct.add_row("余额", money)
        imgs = quota.get("images") or {}
        acct.add_row("出图", Text(f"今天 {imgs.get('today', 0)} 张 · 累计 {imgs.get('total', 0)} 张", style="dim"))
        if quota_age > 90:
            acct.add_row("", Text(f"（{int(quota_age)} 秒前的数字）", style="dim"))
    elif quota_error:
        acct.add_row("账号", Text(quota_error, style="red"))
    else:
        acct.add_row("账号", Text("查询中…", style="dim"))

    # ---- activity
    act = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    act.add_column(style="bold"); act.add_column()
    if running and (running.get("turns") or running.get("runs")):
        s = Text(f"正在画 {running.get('turns', 0)} 轮", style="bold #b08d57")
        if running.get("runs"):
            s.append(f" · {running['runs']} 个连续任务排着", style="#b08d57")
        act.add_row("工作", s)
    else:
        act.add_row("工作", Text("空闲", style="dim"))
    last_min = sum(b_buckets[-(60 // BUCKET):])
    err_style = "red" if (b_err + f_err) else "green"
    act.add_row("请求", Text.assemble((sparkline(b_buckets), "#4166b8"), (f"  最近 2 分钟 · 1 分钟内 {last_min} 个", "dim")))
    act.add_row("出错", Text(f"{b_err + f_err} 条" if (b_err + f_err) else "没有", style=err_style))

    # ---- recent problems
    recent = sorted(b_recent + f_recent)[-5:]
    if recent:
        rec = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
        rec.add_column(style="dim"); rec.add_column(style="red")
        for stamp, line in recent:
            rec.add_row(stamp, line)
        problems = Panel(rec, title="最近的问题", title_align="left", border_style="red", box=box.ROUNDED)
    else:
        problems = Panel(Text("一切正常", style="green"), title="最近的问题", title_align="left",
                         border_style="dim", box=box.ROUNDED)

    def near(p: Path) -> str:
        try:
            return p.relative_to(ROOT).as_posix()
        except ValueError:
            return p.as_posix()

    foot = Text()
    foot.append("  o", style="bold"); foot.append(" 打开浏览器   ", style="dim")
    foot.append("q", style="bold"); foot.append(" 退出（或 Ctrl+C）", style="dim")
    foot.append(f"\n  数据库 {near(db)}   图片 {near(media)}   日志 {near(backend.log_path)}、{frontend.log_path.name}", style="dim")
    if note and note_age < 4:
        foot.append(f"\n  {note}", style="italic #74644c")

    # two panels side by side: a grid, because Columns stacks panels that
    # each want the full width
    panels = Table.grid(expand=True, padding=(0, 1))
    panels.add_column(ratio=1)
    panels.add_column(ratio=1)
    panels.add_row(
        Panel(svc, title="服务", title_align="left", border_style="#4166b8", box=box.ROUNDED),
        Panel(acct, title="账号", title_align="left", border_style="#4166b8", box=box.ROUNDED),
    )
    return Group(
        Panel(Group(head, Text(), open_line), border_style="#4166b8", box=box.HEAVY, padding=(0, 2)),
        panels,
        Panel(act, title="活动", title_align="left", border_style="#4166b8", box=box.ROUNDED),
        problems,
        foot,
    )


def load_rich() -> dict[str, Any] | None:
    """rich's pieces by name, installing it first if it is missing."""
    if missing_modules(("rich",)):
        if not pip_install("rich") or missing_modules(("rich",)):
            return None
    # by name rather than `from rich import …`: rich is optional, and a static
    # import of something optional is a type-checker error on a machine without it
    mod = importlib.import_module
    return {"box": mod("rich.box"), "Columns": mod("rich.columns").Columns,
            "Console": mod("rich.console").Console, "Group": mod("rich.console").Group,
            "Live": mod("rich.live").Live, "Panel": mod("rich.panel").Panel,
            "Table": mod("rich.table").Table, "Text": mod("rich.text").Text}


# ---------------------------------------------------------------- the start-up checklist

class Steps:
    """Prints the start-up as a checklist: one line per step, ✓ or ✗ and a note."""

    def __init__(self, rich: dict[str, Any] | None) -> None:
        self.console = rich["Console"]() if rich else None
        self.Text = rich["Text"] if rich else None

    def title(self) -> None:
        if self.console and self.Text:
            self.console.print(self.Text(LOGO, style="bold #4166b8"))
            self.console.print(self.Text("本机 · 调试模式\n", style="dim"))
        else:
            say("Vallotton · 本机调试模式\n")

    def step(self, name: str, work: Callable[[], str]) -> None:
        """work() returns a short note (「已有 · 37 个对话」); it may call die()."""
        if self.console:
            with self.console.status(f"[dim]{name}…", spinner="dots"):
                note = work()
            self.console.print(f"  [green]✓[/] {name}  [dim]{note}[/]", highlight=False)
        else:
            say(f"  … {name}")
            note = work()
            say(f"  ✓ {name}  {note}")

    def fail(self, name: str, detail: str) -> NoReturn:
        if self.console:
            self.console.print(f"  [red]✗[/] {name}\n", highlight=False)
            self.console.print(detail, highlight=False, markup=False)
            sys.exit(1)
        die(f"{name}\n{detail}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Vallotton 本机快速启动（调试模式）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="sqlite 文件路径（默认 tmp/dev.sqlite3）")
    ap.add_argument("--port", type=int, default=5173, help="网页端口（默认 5173；前端由后端托管时网页和后端同一个端口，见 --api-port）")
    ap.add_argument("--api-port", type=int, default=8000, help="后端端口（默认 8000）")
    ap.add_argument("--no-browser", action="store_true", help="就绪后不自动打开浏览器")
    ap.add_argument("--plain", action="store_true", help="不画仪表盘，一行一行输出状态")
    args = ap.parse_args()

    if sys.version_info < (3, 11):
        die(f"需要 Python 3.11 以上，现在是 {sys.version.split()[0]}")
    interactive = sys.stdout.isatty() and not args.plain
    rich = load_rich() if interactive else None
    interactive = interactive and rich is not None
    steps = Steps(rich if interactive else None)
    steps.title()

    # ---- the checklist
    hosted = (DIST / "index.html").exists()
    node_box: dict[str, str] = {}

    def check_node() -> str:
        node_box["node"], ver = node_path()
        return ver
    steps.step("Python", lambda: sys.version.split()[0])
    if hosted:
        steps.step("前端", lambda: "已构建，由后端托管（不需要 Node.js）")
    else:
        steps.step("Node.js", check_node)

    def backend_deps() -> str:
        missing = missing_modules(("django", "httpx", "PIL", "whitenoise"))
        if not missing:
            return "已装"
        if not pip_install("-r", str(BACKEND / "requirements.txt")):
            steps.fail("后端依赖", "pip install -r backend/requirements.txt 失败，手动跑一遍看输出")
        return f"刚装好（缺了 {', '.join(missing)}）"
    steps.step("后端依赖", backend_deps)

    def frontend_deps() -> str:
        if (FRONTEND / "node_modules" / "vite" / "bin" / "vite.js").exists():
            return "已装"
        r = subprocess.run([npm_path(), "install", "--no-fund", "--no-audit"], cwd=FRONTEND, capture_output=True)
        if r.returncode != 0:
            steps.fail("前端依赖", "npm install 失败：\n" + r.stderr.decode("utf-8", errors="replace")[-2000:])
        return "刚装好（npm install）"
    if not hosted:
        steps.step("前端依赖", frontend_deps)

    def env_file() -> str:
        if ENV_FILE.exists():
            values = read_env(ENV_FILE)
            missing = check_env(values)
            if missing:
                steps.fail(".env", f"还缺：{'、'.join(missing)}（文件：{ENV_FILE}）")
            return f"已有 · {values.get('LLM_PROVIDER')} · 用户 {values.get('APP_USERNAME')}"
        return "没有，现在建一份"
    steps.step(".env", env_file)
    if not ENV_FILE.exists():
        first_run_env()
        steps.step(".env", lambda: f"已写入 {ENV_FILE}")
    # what the backend will actually read: the environment wins over .env
    values = {**read_env(ENV_FILE), **{k: v for k, v in os.environ.items() if k in read_env(ENV_FILE) or k in ("APP_USERNAME", "APP_PASSWORD")}}

    ports = [(args.api_port, "后端")] + ([] if hosted else [(args.port, "网页")])
    for port, what in ports:
        if not port_free(port):
            steps.fail("端口", f"{what}端口 {port} 被占用。换一个（--port / --api-port），或者先关掉占用它的程序")

    (ROOT / "tmp").mkdir(exist_ok=True)
    db = Path(args.db).expanduser().resolve() if args.db else ROOT / "tmp" / "dev.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    media = Path(os.environ.get("MEDIA_ROOT") or values.get("MEDIA_ROOT") or ROOT / "tmp" / "media")
    steps.step("数据库", lambda: f"{describe_db(db)} · {db}")

    # what we pass wins over .env (the backend loads .env with setdefault)
    base_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    back_env = {**base_env, "DJANGO_DB": "sqlite", "SQLITE_NAME": str(db), "DJANGO_DEBUG": "1"}
    if hosted:
        back_env["SERVE_FRONTEND"] = "1"
        back_env["FRONTEND_DIST"] = str(DIST)
    front_env = {**base_env, "VITE_PORT": str(args.port), "API_TARGET": f"http://127.0.0.1:{args.api_port}"}

    def migrate() -> str:
        err = run_step([sys.executable, "manage.py", "migrate", "--no-input"], BACKEND, back_env)
        if err:
            steps.fail("数据库迁移", err)
        return "最新"
    steps.step("数据库迁移", migrate)

    def accounts() -> str:
        err = run_step([sys.executable, "manage.py", "bootstrap_users"], BACKEND, back_env)
        if err:
            steps.fail("账号", err)
        return f"{values.get('APP_USERNAME')}（密码在 .env）"
    steps.step("账号", accounts)

    backend = Child("后端", ROOT / "tmp" / "quickstart-后端.log")
    frontend = Child("前端", ROOT / "tmp" / "quickstart-前端.log")
    children = [backend] if hosted else [backend, frontend]
    api_url = f"http://127.0.0.1:{args.api_port}"
    # hosted: one process, one port -- the page comes from the backend
    web_url = f"http://localhost:{args.api_port if hosted else args.port}"

    def shutdown(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, shutdown)

    def start_servers() -> str:
        backend.start([sys.executable, "manage.py", "runserver", f"127.0.0.1:{args.api_port}", "--noreload"],
                      BACKEND, back_env)
        if not hosted:
            frontend.start([node_box["node"], str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"),
                            "--host", "127.0.0.1"], FRONTEND, front_env)
        deadline = time.time() + 120
        ready = {"api": False, "web": False}
        while time.time() < deadline:
            for c in children:
                if not c.alive():
                    assert c.proc is not None
                    tail = "\n".join(ln for _, ln in c.snapshot()[3][-10:])
                    steps.fail(f"{c.name}没起来（退出码 {c.proc.returncode}）",
                               f"{tail}\n完整日志：{c.log_path}")
            if not ready["api"] and http_ok(f"{api_url}/api/auth/me"):
                ready["api"] = True
            if not ready["web"] and http_ok(f"{web_url}/"):
                ready["web"] = True
            if all(ready.values()):
                return f"{web_url}（前端由后端托管）" if hosted else f"后端 {api_url} · 前端 {web_url}"
            time.sleep(0.4)
        logs = "、".join(str(c.log_path) for c in children)
        steps.fail("启动", f"等了两分钟还没就绪，看日志：{logs}")

    code = 0
    stop = threading.Event()
    try:
        steps.step("启动后端" if hosted else "启动后端和前端", start_servers)
        if not args.no_browser:
            webbrowser.open(web_url)

        board = Board()
        board.hosted = hosted
        api = Api(api_url, values.get("APP_USERNAME", ""), values.get("APP_PASSWORD", ""))
        threading.Thread(target=poll, args=(board, api, web_url, stop), daemon=True).start()

        if not interactive or rich is None:
            # plain: a banner, then a line whenever something changes
            say("")
            say("─" * 60)
            say(" Vallotton 已启动（本机调试模式）")
            say(f"   打开：{web_url}")
            say(f"   登录：用户名 {values.get('APP_USERNAME', '')}（密码在 .env 的 APP_PASSWORD）")
            say(f"   后端：{api_url}" + ("（前端由后端托管）" if hosted else ""))
            say(f"   数据库：{db}")
            say(f"   图片：{media}")
            say("   日志：" + "、".join(str(c.log_path) for c in children))
            say("   停止：Ctrl+C")
            say("─" * 60)
            seen_errors = 0
            while True:
                for c in children:
                    if not c.alive():
                        assert c.proc is not None
                        say(f"\n状态：{c.name} 退出（{c.proc.returncode}），看 {c.log_path}")
                        code = 1
                        raise KeyboardInterrupt
                errs = backend.snapshot()[1] + frontend.snapshot()[1]
                if errs != seen_errors:
                    seen_errors = errs
                    recent = sorted(backend.snapshot()[3] + frontend.snapshot()[3])[-1:]
                    for stamp, line in recent:
                        say(f"[{stamp}] 出错：{line}")
                time.sleep(1)

        # the dashboard
        keyq: "queue.Queue[str]" = queue.Queue()
        threading.Thread(target=keys, args=(keyq, stop), daemon=True).start()
        console = rich["Console"]()
        username = values.get("APP_USERNAME", "")
        with rich["Live"](console=console, refresh_per_second=4, screen=True, transient=True) as live:
            while True:
                live.update(draw(rich, board, backend, frontend, web_url, api_url, db, media, username))
                for c in children:
                    if not c.alive():
                        assert c.proc is not None
                        code = 1
                        live.stop()
                        say(f"{c.name}退出了（退出码 {c.proc.returncode}），看 {c.log_path}")
                        raise KeyboardInterrupt
                try:
                    k = keyq.get(timeout=0.25)
                except queue.Empty:
                    continue
                if k in ("q", "Q", "\x03"):
                    raise KeyboardInterrupt
                if k in ("o", "O"):
                    webbrowser.open(web_url)
                    board.set_note("已在浏览器里打开")
    except KeyboardInterrupt:
        say("\n正在停止…")
    finally:
        stop.set()
        for c in children:
            c.stop()
    say("已停止。")
    return code


if __name__ == "__main__":
    sys.exit(main())
