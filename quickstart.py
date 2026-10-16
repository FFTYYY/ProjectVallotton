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

#: the name in block letters -- a terminal has one font and one size, so a
#: bigger "font" is drawn rather than chosen
LOGO = """\
█   █  ████  █     █      ████  ████████ ████████  ████  █    █
 █ █  █    █ █     █     █    █    ██       ██    █    █ ██   █
 █ █  ██████ █     █     █    █    ██       ██    █    █ █ █  █
 █ █  █    █ █     █     █    █    ██       ██    █    █ █  █ █
  █   █    █ █████ █████  ████     ██       ██     ████  █   ██"""


# ---------------------------------------------------------------- the words

#: the languages this window can speak; the first is what it speaks unless told
LANGS = ("zh", "en")
LANG = LANGS[0]

#: every sentence a person reads, by language: the same keys and the same
#: {fields} in each (backend/tests/tests_quickstart_lang.py holds them to
#: that). Not here: environment variable names, paths, URLs, product names,
#: and the two log files, which keep their Chinese names on disk.
TEXT: dict[str, dict[str, str]] = {
    "zh": {
        # ---- the command line
        "description": "Vallotton 本机快速启动（调试模式）",
        "help_db": "sqlite 文件路径（默认 tmp/dev.sqlite3）",
        "help_port": "网页端口（默认 5173；前端由后端托管时网页和后端同一个端口，见 --api-port）",
        "help_api_port": "后端端口（默认 8000）",
        "help_no_browser": "就绪后不自动打开浏览器",
        "help_plain": "不画仪表盘，一行一行输出状态",
        "help_lang": "输出语言：zh 中文、en 英文（默认 zh）",
        # ---- prerequisites
        "python_too_old": "需要 Python 3.11 以上，现在是 {version}",
        "no_node": "没找到 Node.js。装 20 以上的版本：https://nodejs.org/ ，装完重开终端再运行",
        "node_too_old": "Node.js 需要 20 以上，现在是 {version}",
        "no_npm": "没找到 npm（它随 Node.js 一起装）",
        # ---- .env
        "env_template": """# Vallotton 本机配置，由 quickstart.py 在第一次启动时生成。
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
""",
        "ask": "  {prompt}：",
        "ask_with_default": "  {prompt}（默认 {default}）：",
        "empty_answer": "    不能为空",
        "bad_prefix": "    看起来不对：应该以 {prefix} 开头",
        "first_run_no_tty": "第一次运行需要在终端里回答几个问题，或者自己写一份 .env（照 .env.example，"
                            "至少要 LLM_PROVIDER、DEEPSEEK_API_KEY、NOVELAI_API_KEY、APP_USERNAME、"
                            "APP_PASSWORD、ADMIN_USERNAME、ADMIN_PASSWORD）",
        "first_run_intro": "  第一次运行，先把必需的几项写进 .env（之后可以直接改这个文件）。",
        "ask_deepseek": "DeepSeek API key（platform.deepseek.com，sk- 开头）",
        "ask_novelai": "NovelAI persistent token（账号设置里生成，pst- 开头）",
        "ask_username": "网页登录用户名",
        "ask_password": "网页登录密码",
        "provider_unknown": "LLM_PROVIDER={provider} 不认识（deepseek 或 grok）",
        "provider_unset": "LLM_PROVIDER（没写就是 deepseek）",
        "env_missing": "还缺：{missing}（文件：{file}）",
        "env_existing": "已有 · {provider} · 用户 {username}",
        "env_none": "没有，现在建一份",
        "env_written": "已写入 {file}",
        "list_sep": "、",
        # ---- the database
        "db_new": "新建",
        "db_conversations": " · {n} 个对话",
        "db_existing": "已有{convs} · {mb:.1f} MB",
        # ---- the checklist
        "title_rich": "本机 · 调试模式\n",
        "title_plain": "Vallotton · 本机调试模式\n",
        "backend": "后端",
        "frontend": "前端",
        "web": "网页",
        "frontend_built": "已构建，由后端托管（不需要 Node.js）",
        "step_backend_deps": "后端依赖",
        "step_frontend_deps": "前端依赖",
        "installed": "已装",
        "just_installed_missing": "刚装好（缺了 {missing}）",
        "just_installed_npm": "刚装好（npm install）",
        "pip_failed": "pip install -r backend/requirements.txt 失败：\n{said}"
                      "\n\n删掉 .runtime 文件夹再运行 start.bat（macOS / Linux 是 ./start.sh）会重新准备环境",
        "npm_failed": "npm install 失败：\n{said}",
        "step_port": "端口",
        "port_taken": "{what}端口 {port} 被占用。换一个（--port / --api-port），或者先关掉占用它的程序",
        "step_database": "数据库",
        "step_migrate": "数据库迁移",
        "up_to_date": "最新",
        "account": "账号",
        "account_note": "{username}（密码在 .env）",
        "step_start": "启动",
        "step_start_backend": "启动后端",
        "step_start_both": "启动后端和前端",
        "server_failed": "{name}没起来（退出码 {code}）",
        "full_log": "{tail}\n完整日志：{log}",
        "ready_both": "后端 {api} · 前端 {web}",
        "hosted_suffix": "（前端由后端托管）",
        "not_ready": "等了两分钟还没就绪，看日志：{logs}",
        # ---- the servers
        "state_not_started": "未启动",
        "state_starting": "启动中",
        "state_stopped": "已停止",
        "server_exited": "{name}退出了（退出码 {code}），看 {log}",
        # ---- the accounts
        "quota_unavailable": "拿不到账号状态",
        "login_failed": "登录失败：.env 的账号密码和数据库里的不一致？",
        # ---- the dashboard
        "head_line": "\n\n本机 · 调试模式   运行 {uptime}",
        "open_label": "  打开  ",
        "login_as": "     登录 {username}",
        "svc_exited": "✗ 已退出",
        "svc_running": "● 运行中",
        "svc_starting": "◌ 启动中",
        "svc_hosted": "● 由后端托管",
        "no_response": "无响应",
        "ok": "正常",
        "valid": "有效",
        "expired": "已过期",
        "v5_usage": " · V5 用量 {percent}%",
        "lookup_failed": "查不到：{error}",
        "llm": "大模型",
        "balance": "余额",
        "pay_per_use": "按用量计费",
        "today_usd": " · 今天 ${usd:.2f}",
        "images": "出图",
        "images_count": "今天 {today} 张 · 累计 {total} 张",
        "stale_numbers": "（{seconds} 秒前的数字）",
        "checking": "查询中…",
        "work": "工作",
        "drawing_turns": "正在画 {turns} 轮",
        "runs_queued": " · {runs} 个连续任务排着",
        "idle": "空闲",
        "requests": "请求",
        "requests_note": "  最近 2 分钟 · 1 分钟内 {count} 个",
        "errors": "出错",
        "errors_count": "{count} 条",
        "none": "没有",
        "all_good": "一切正常",
        "panel_services": "服务",
        "panel_accounts": "账号",
        "panel_activity": "活动",
        "panel_problems": "最近的问题",
        "key_open": " 打开浏览器   ",
        "key_quit": " 退出（或 Ctrl+C）",
        "foot_paths": "\n  数据库 {db}   图片 {media}   日志 {log}、{log2}",
        "opened_browser": "已在浏览器里打开",
        # ---- the plain output
        "plain_started": " Vallotton 已启动（本机调试模式）",
        "plain_open": "   打开：{url}",
        "plain_login": "   登录：用户名 {username}（密码在 .env 的 APP_PASSWORD）",
        "plain_backend": "   后端：{url}{hosted}",
        "plain_db": "   数据库：{db}",
        "plain_media": "   图片：{media}",
        "plain_logs": "   日志：{logs}",
        "plain_stop": "   停止：Ctrl+C",
        "plain_exited": "\n状态：{name} 退出（{code}），看 {log}",
        "plain_error": "[{stamp}] 出错：{line}",
        # ---- the end
        "stopping": "\n正在停止…",
        "stopped": "已停止。",
    },
    "en": {
        # ---- the command line
        "description": "Vallotton local quick start (debug mode)",
        "help_db": "sqlite file path (default tmp/dev.sqlite3)",
        "help_port": "web port (default 5173; when the backend serves the frontend the page shares the backend's port, see --api-port)",
        "help_api_port": "backend port (default 8000)",
        "help_no_browser": "do not open the browser when ready",
        "help_plain": "no dashboard, print the status line by line",
        "help_lang": "output language: zh Chinese, en English (default zh)",
        # ---- prerequisites
        "python_too_old": "Python 3.11 or newer is needed; this is {version}",
        "no_node": "Node.js not found. Install version 20 or newer: https://nodejs.org/ , then reopen the terminal and run again",
        "node_too_old": "Node.js 20 or newer is needed; this is {version}",
        "no_npm": "npm not found (it comes with Node.js)",
        # ---- .env
        "env_template": """# Vallotton local configuration, generated by quickstart.py on the first start.
# Only what a local run needs. To change providers, enable MCP or deploy to a server, see .env.example and DEPLOY.md.

LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY={deepseek}
NOVELAI_API_KEY={novelai}

# Web login
APP_USERNAME={username}
APP_PASSWORD={password}
# Django admin (/admin/), rarely needed; the password is generated at random
ADMIN_USERNAME=admin
ADMIN_PASSWORD={admin_password}

DJANGO_SECRET_KEY={secret}
""",
        "ask": "  {prompt}: ",
        "ask_with_default": "  {prompt} (default {default}): ",
        "empty_answer": "    Cannot be empty",
        "bad_prefix": "    That does not look right: it should start with {prefix}",
        "first_run_no_tty": "The first run asks a few questions in a terminal; otherwise write a .env yourself "
                            "(after .env.example, with at least LLM_PROVIDER, DEEPSEEK_API_KEY, NOVELAI_API_KEY, "
                            "APP_USERNAME, APP_PASSWORD, ADMIN_USERNAME, ADMIN_PASSWORD)",
        "first_run_intro": "  First run: the required items are written to .env now (you can edit that file directly later).",
        "ask_deepseek": "DeepSeek API key (from platform.deepseek.com, starts with sk-)",
        "ask_novelai": "NovelAI persistent token (generated in the account settings, starts with pst-)",
        "ask_username": "Web login username",
        "ask_password": "Web login password",
        "provider_unknown": "LLM_PROVIDER={provider} is not recognised (deepseek or grok)",
        "provider_unset": "LLM_PROVIDER (deepseek when not set)",
        "env_missing": "still missing: {missing} (file: {file})",
        "env_existing": "existing · {provider} · user {username}",
        "env_none": "none, creating one now",
        "env_written": "written to {file}",
        "list_sep": ", ",
        # ---- the database
        "db_new": "new",
        "db_conversations": " · {n} conversations",
        "db_existing": "existing{convs} · {mb:.1f} MB",
        # ---- the checklist
        "title_rich": "Local · debug mode\n",
        "title_plain": "Vallotton · local debug mode\n",
        "backend": "Backend",
        "frontend": "Frontend",
        "web": "Web",
        "frontend_built": "built, served by the backend (Node.js not needed)",
        "step_backend_deps": "Backend dependencies",
        "step_frontend_deps": "Frontend dependencies",
        "installed": "installed",
        "just_installed_missing": "just installed (was missing {missing})",
        "just_installed_npm": "just installed (npm install)",
        "pip_failed": "pip install -r backend/requirements.txt failed:\n{said}"
                      "\n\nDelete the .runtime folder and run start.bat again (./start.sh on macOS / Linux) to set the environment up afresh",
        "npm_failed": "npm install failed:\n{said}",
        "step_port": "Port",
        "port_taken": "{what} port {port} is in use. Pick another (--port / --api-port), or stop the program using it first",
        "step_database": "Database",
        "step_migrate": "Database migration",
        "up_to_date": "up to date",
        "account": "Account",
        "account_note": "{username} (password in .env)",
        "step_start": "Start",
        "step_start_backend": "Start the backend",
        "step_start_both": "Start the backend and the frontend",
        "server_failed": "{name} did not start (exit code {code})",
        "full_log": "{tail}\nFull log: {log}",
        "ready_both": "backend {api} · frontend {web}",
        "hosted_suffix": " (frontend served by the backend)",
        "not_ready": "Still not ready after two minutes; see the logs: {logs}",
        # ---- the servers
        "state_not_started": "not started",
        "state_starting": "starting",
        "state_stopped": "stopped",
        "server_exited": "{name} exited (exit code {code}), see {log}",
        # ---- the accounts
        "quota_unavailable": "Could not get the account status",
        "login_failed": "Login failed: do the username and password in .env match the database?",
        # ---- the dashboard
        "head_line": "\n\nLocal · debug mode   up {uptime}",
        "open_label": "  Open  ",
        "login_as": "     login {username}",
        "svc_exited": "✗ exited",
        "svc_running": "● running",
        "svc_starting": "◌ starting",
        "svc_hosted": "● hosted",
        "no_response": "no response",
        "ok": "ok",
        "valid": "valid",
        "expired": "expired",
        "v5_usage": " · V5 usage {percent}%",
        "lookup_failed": "Could not check: {error}",
        "llm": "LLM",
        "balance": "Balance",
        "pay_per_use": "pay per use",
        "today_usd": " · today ${usd:.2f}",
        "images": "Pictures",
        "images_count": "{today} today · {total} in all",
        "stale_numbers": "(figures from {seconds} s ago)",
        "checking": "checking…",
        "work": "Work",
        "drawing_turns": "drawing {turns} turns",
        "runs_queued": " · {runs} continuous tasks queued",
        "idle": "idle",
        "requests": "Requests",
        "requests_note": "  last 2 minutes · {count} in the last minute",
        "errors": "Errors",
        "errors_count": "{count}",
        "none": "none",
        "all_good": "All good",
        "panel_services": "Services",
        "panel_accounts": "Accounts",
        "panel_activity": "Activity",
        "panel_problems": "Recent problems",
        "key_open": " open the browser   ",
        "key_quit": " quit (or Ctrl+C)",
        "foot_paths": "\n  database {db}   pictures {media}   logs {log}, {log2}",
        "opened_browser": "Opened in the browser",
        # ---- the plain output
        "plain_started": " Vallotton is running (local debug mode)",
        "plain_open": "   Open: {url}",
        "plain_login": "   Login: username {username} (the password is APP_PASSWORD in .env)",
        "plain_backend": "   Backend: {url}{hosted}",
        "plain_db": "   Database: {db}",
        "plain_media": "   Pictures: {media}",
        "plain_logs": "   Logs: {logs}",
        "plain_stop": "   Stop: Ctrl+C",
        "plain_exited": "\nStatus: {name} exited ({code}), see {log}",
        "plain_error": "[{stamp}] error: {line}",
        # ---- the end
        "stopping": "\nStopping…",
        "stopped": "Stopped.",
    },
}


def lang_from_argv(argv: list[str]) -> str:
    """The --lang on the command line, read before argparse exists: the
    parser's own help has to be in that language. Absent or unknown is the
    default; argparse then refuses an unknown one itself."""
    for i, arg in enumerate(argv):
        if arg == "--lang" and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--lang="):
            value = arg.partition("=")[2]
        else:
            continue
        if value in LANGS:
            return value
    return LANGS[0]


def T(key: str, **fields: object) -> str:
    """The sentence `key` in the current language, with its {fields} filled."""
    return TEXT[LANG][key].format(**fields)


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
        die(T("no_node"))
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"v(\d+)", out)
    if m and int(m.group(1)) < 20:
        die(T("node_too_old", version=out))
    return node, out


def npm_path() -> str:
    npm = shutil.which("npm.cmd") if WIN else None
    npm = npm or shutil.which("npm")
    if not npm:
        die(T("no_npm"))
    return npm


def missing_modules(names: tuple[str, ...]) -> list[str]:
    out = []
    for mod in names:
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append(mod)
    return out


def pip_install(*what: str) -> tuple[bool, str]:
    """Whether pip managed it, and what it said if not (its last lines)."""
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", *what],
                       capture_output=True)
    said = (r.stderr or r.stdout).decode("utf-8", errors="replace").strip()
    return r.returncode == 0, said[-1500:]


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
    path.write_text(T("env_template",
                      deepseek=deepseek, novelai=novelai, username=username, password=password,
                      admin_password=secrets.token_urlsafe(18), secret=secrets.token_urlsafe(48),
                      ), encoding="utf-8", newline="\n")


def ask(prompt: str, *, default: str = "", must_start: str = "") -> str:
    while True:
        shown = T("ask_with_default", prompt=prompt, default=default) if default else T("ask", prompt=prompt)
        raw = input(shown)
        value = raw.strip() or default
        if not value:
            say(T("empty_answer"))
            continue
        if must_start and not value.startswith(must_start):
            say(T("bad_prefix", prefix=must_start))
            continue
        return value


def first_run_env() -> None:
    if not sys.stdin.isatty():
        die(T("first_run_no_tty"))
    say(T("first_run_intro"))
    deepseek = ask(T("ask_deepseek"), must_start="sk-")
    novelai = ask(T("ask_novelai"), must_start="pst-")
    username = ask(T("ask_username"), default="user")
    password = ask(T("ask_password"))
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
        return [T("provider_unknown", provider=provider)]
    missing = [k for k in need + ["NOVELAI_API_KEY", "APP_USERNAME", "APP_PASSWORD",
                                   "ADMIN_USERNAME", "ADMIN_PASSWORD"] if not values.get(k)]
    if "LLM_PROVIDER" not in values:
        missing.insert(0, T("provider_unset"))
    return missing


def describe_db(path: Path) -> str:
    """「已有 · 37 个对话 · 12.4 MB」, or 「新建」."""
    if not path.exists():
        return T("db_new")
    size = path.stat().st_size
    convs = ""
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as con:
            n = con.execute("select count(*) from core_conversation where is_archived = 0").fetchone()[0]
            convs = T("db_conversations", n=n)
    except sqlite3.Error:
        pass
    return T("db_existing", convs=convs, mb=size / 1024 / 1024)


# ---------------------------------------------------------------- the two servers

class Child:
    """One server: its process, its log file, what its output says."""

    def __init__(self, name: str, log_path: Path) -> None:
        self.name = name
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None
        self.state = T("state_not_started")
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
        self.state = T("state_starting")
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
        self.state = T("state_stopped")


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
                    board.quota_error = T("quota_unavailable") if api.logged_in else T("login_failed")
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
    head.append(T("head_line", uptime=uptime(time.monotonic() - board.started)), style="dim")
    open_line = Text()
    open_line.append(T("open_label"), style="bold")
    open_line.append(web_url, style="bold underline #2a4380")
    open_line.append(T("login_as", username=username), style="dim")

    # ---- services
    def dot(alive: bool, ready: bool) -> Any:
        if not alive:
            return Text(T("svc_exited"), style="bold red")
        return Text(T("svc_running"), style="bold green") if ready else Text(T("svc_starting"), style="yellow")

    svc = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    svc.add_column(); svc.add_column(); svc.add_column(style="dim"); svc.add_column(justify="right")
    host = lambda url: url.split("//", 1)[-1]   # noqa: E731 -- the scheme is in the header already
    svc.add_row(dot(backend.alive(), ms is not None), Text(T("backend"), style="bold"), host(api_url),
                Text(f"{ms:.0f} ms" if ms is not None else T("no_response"), style="green" if ms is not None and ms < 200 else "yellow"))
    if board.hosted:
        svc.add_row(Text(T("svc_hosted"), style="bold green") if fe_ok else Text(T("svc_starting"), style="yellow"),
                    Text(T("frontend"), style="bold"), host(web_url),
                    Text(T("ok") if fe_ok else T("no_response"), style="green" if fe_ok else "yellow"))
    else:
        svc.add_row(dot(frontend.alive(), fe_ok), Text(T("frontend"), style="bold"), host(web_url),
                    Text(T("ok") if fe_ok else T("no_response"), style="green" if fe_ok else "yellow"))

    # ---- accounts
    acct = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    acct.add_column(style="bold"); acct.add_column()
    if quota:
        nai = quota.get("novelai") or {}
        if nai.get("ok"):
            s = Text(f"{nai.get('tier', '?')} · ", style="")
            s.append(T("valid") if nai.get("active") else T("expired"), style="green" if nai.get("active") else "red")
            if nai.get("usage_percent") is not None:
                s.append(T("v5_usage", percent=nai["usage_percent"]), style="dim")
        else:
            s = Text(T("lookup_failed", error=nai.get("error", "?")), style="red")
        acct.add_row("NovelAI", s)
        llm = quota.get("llm") or {}
        bal = llm.get("balance") or {}
        acct.add_row(T("llm"), Text.assemble(llm.get("label") or llm.get("provider", "?"),
                                             (f" · {llm.get('model', '')}", "dim")))
        if bal.get("ok"):
            money = Text(f"{bal.get('balance', 0):.2f} {bal.get('currency', '')}",
                         style="green" if bal.get("balance", 0) > 1 else "red")
        elif bal:
            money = Text(T("lookup_failed", error=bal.get("error", "?")), style="red")
        else:
            money = Text(T("pay_per_use") if llm.get("priced") else "—", style="dim")
        if llm.get("priced"):
            money.append(T("today_usd", usd=llm.get("today_usd", 0)), style="dim")
        acct.add_row(T("balance"), money)
        imgs = quota.get("images") or {}
        acct.add_row(T("images"), Text(T("images_count", today=imgs.get("today", 0), total=imgs.get("total", 0)), style="dim"))
        if quota_age > 90:
            acct.add_row("", Text(T("stale_numbers", seconds=int(quota_age)), style="dim"))
    elif quota_error:
        acct.add_row(T("account"), Text(quota_error, style="red"))
    else:
        acct.add_row(T("account"), Text(T("checking"), style="dim"))

    # ---- activity
    act = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2))
    act.add_column(style="bold"); act.add_column()
    if running and (running.get("turns") or running.get("runs")):
        s = Text(T("drawing_turns", turns=running.get("turns", 0)), style="bold #b08d57")
        if running.get("runs"):
            s.append(T("runs_queued", runs=running["runs"]), style="#b08d57")
        act.add_row(T("work"), s)
    else:
        act.add_row(T("work"), Text(T("idle"), style="dim"))
    last_min = sum(b_buckets[-(60 // BUCKET):])
    err_style = "red" if (b_err + f_err) else "green"
    act.add_row(T("requests"), Text.assemble((sparkline(b_buckets), "#4166b8"), (T("requests_note", count=last_min), "dim")))
    act.add_row(T("errors"), Text(T("errors_count", count=b_err + f_err) if (b_err + f_err) else T("none"), style=err_style))

    # ---- recent problems
    recent = sorted(b_recent + f_recent)[-5:]
    if recent:
        rec = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
        rec.add_column(style="dim"); rec.add_column(style="red")
        for stamp, line in recent:
            rec.add_row(stamp, line)
        problems = Panel(rec, title=T("panel_problems"), title_align="left", border_style="red", box=box.ROUNDED)
    else:
        problems = Panel(Text(T("all_good"), style="green"), title=T("panel_problems"), title_align="left",
                         border_style="dim", box=box.ROUNDED)

    def near(p: Path) -> str:
        try:
            return p.relative_to(ROOT).as_posix()
        except ValueError:
            return p.as_posix()

    foot = Text()
    foot.append("  o", style="bold"); foot.append(T("key_open"), style="dim")
    foot.append("q", style="bold"); foot.append(T("key_quit"), style="dim")
    foot.append(T("foot_paths", db=near(db), media=near(media), log=near(backend.log_path), log2=frontend.log_path.name), style="dim")
    if note and note_age < 4:
        foot.append(f"\n  {note}", style="italic #74644c")

    # two panels side by side: a grid, because Columns stacks panels that
    # each want the full width
    panels = Table.grid(expand=True, padding=(0, 1))
    panels.add_column(ratio=1)
    panels.add_column(ratio=1)
    panels.add_row(
        Panel(svc, title=T("panel_services"), title_align="left", border_style="#4166b8", box=box.ROUNDED),
        Panel(acct, title=T("panel_accounts"), title_align="left", border_style="#4166b8", box=box.ROUNDED),
    )
    return Group(
        Panel(Group(head, Text(), open_line), border_style="#4166b8", box=box.HEAVY, padding=(0, 2)),
        panels,
        Panel(act, title=T("panel_activity"), title_align="left", border_style="#4166b8", box=box.ROUNDED),
        problems,
        foot,
    )


def load_rich() -> dict[str, Any] | None:
    """rich's pieces by name, installing it first if it is missing."""
    if missing_modules(("rich",)):
        if not pip_install("rich")[0] or missing_modules(("rich",)):
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
            self.console.print(self.Text(T("title_rich"), style="dim"))
        else:
            say(T("title_plain"))

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
    global LANG
    # read before the parser exists: its help is written in that language
    LANG = lang_from_argv(sys.argv[1:])
    ap = argparse.ArgumentParser(description=T("description"),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help=T("help_db"))
    ap.add_argument("--port", type=int, default=5173, help=T("help_port"))
    ap.add_argument("--api-port", type=int, default=8000, help=T("help_api_port"))
    ap.add_argument("--no-browser", action="store_true", help=T("help_no_browser"))
    ap.add_argument("--plain", action="store_true", help=T("help_plain"))
    ap.add_argument("--lang", choices=LANGS, default=LANG, help=T("help_lang"))
    args = ap.parse_args()
    LANG = args.lang

    if sys.version_info < (3, 11):
        die(T("python_too_old", version=sys.version.split()[0]))
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
        steps.step(T("frontend"), lambda: T("frontend_built"))
    else:
        steps.step("Node.js", check_node)

    def backend_deps() -> str:
        missing = missing_modules(("django", "httpx", "PIL", "whitenoise"))
        if not missing:
            return T("installed")
        ok, said = pip_install("-r", str(BACKEND / "requirements.txt"))
        if not ok:
            steps.fail(T("step_backend_deps"), T("pip_failed", said=said))
        return T("just_installed_missing", missing=", ".join(missing))
    steps.step(T("step_backend_deps"), backend_deps)

    def frontend_deps() -> str:
        if (FRONTEND / "node_modules" / "vite" / "bin" / "vite.js").exists():
            return T("installed")
        r = subprocess.run([npm_path(), "install", "--no-fund", "--no-audit"], cwd=FRONTEND, capture_output=True)
        if r.returncode != 0:
            steps.fail(T("step_frontend_deps"), T("npm_failed", said=r.stderr.decode("utf-8", errors="replace")[-2000:]))
        return T("just_installed_npm")
    if not hosted:
        steps.step(T("step_frontend_deps"), frontend_deps)

    def env_file() -> str:
        if ENV_FILE.exists():
            values = read_env(ENV_FILE)
            missing = check_env(values)
            if missing:
                steps.fail(".env", T("env_missing", missing=T("list_sep").join(missing), file=ENV_FILE))
            return T("env_existing", provider=values.get("LLM_PROVIDER"), username=values.get("APP_USERNAME"))
        return T("env_none")
    steps.step(".env", env_file)
    if not ENV_FILE.exists():
        first_run_env()
        steps.step(".env", lambda: T("env_written", file=ENV_FILE))
    # what the backend will actually read: the environment wins over .env
    values = {**read_env(ENV_FILE), **{k: v for k, v in os.environ.items() if k in read_env(ENV_FILE) or k in ("APP_USERNAME", "APP_PASSWORD")}}

    ports = [(args.api_port, T("backend"))] + ([] if hosted else [(args.port, T("web"))])
    for port, what in ports:
        if not port_free(port):
            steps.fail(T("step_port"), T("port_taken", what=what, port=port))

    (ROOT / "tmp").mkdir(exist_ok=True)
    db = Path(args.db).expanduser().resolve() if args.db else ROOT / "tmp" / "dev.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    media = Path(os.environ.get("MEDIA_ROOT") or values.get("MEDIA_ROOT") or ROOT / "tmp" / "media")
    steps.step(T("step_database"), lambda: f"{describe_db(db)} · {db}")

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
            steps.fail(T("step_migrate"), err)
        return T("up_to_date")
    steps.step(T("step_migrate"), migrate)

    def accounts() -> str:
        err = run_step([sys.executable, "manage.py", "bootstrap_users"], BACKEND, back_env)
        if err:
            steps.fail(T("account"), err)
        return T("account_note", username=values.get("APP_USERNAME"))
    steps.step(T("account"), accounts)

    # the log files keep their Chinese names whatever the language: they are
    # names on disk, and the docs point at them
    backend = Child(T("backend"), ROOT / "tmp" / "quickstart-后端.log")
    frontend = Child(T("frontend"), ROOT / "tmp" / "quickstart-前端.log")
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
                    steps.fail(T("server_failed", name=c.name, code=c.proc.returncode),
                               T("full_log", tail=tail, log=c.log_path))
            if not ready["api"] and http_ok(f"{api_url}/api/auth/me"):
                ready["api"] = True
            if not ready["web"] and http_ok(f"{web_url}/"):
                ready["web"] = True
            if all(ready.values()):
                return f"{web_url}{T('hosted_suffix')}" if hosted else T("ready_both", api=api_url, web=web_url)
            time.sleep(0.4)
        logs = T("list_sep").join(str(c.log_path) for c in children)
        steps.fail(T("step_start"), T("not_ready", logs=logs))

    code = 0
    stop = threading.Event()
    try:
        steps.step(T("step_start_backend") if hosted else T("step_start_both"), start_servers)
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
            say(T("plain_started"))
            say(T("plain_open", url=web_url))
            say(T("plain_login", username=values.get("APP_USERNAME", "")))
            say(T("plain_backend", url=api_url, hosted=T("hosted_suffix") if hosted else ""))
            say(T("plain_db", db=db))
            say(T("plain_media", media=media))
            say(T("plain_logs", logs=T("list_sep").join(str(c.log_path) for c in children)))
            say(T("plain_stop"))
            say("─" * 60)
            seen_errors = 0
            while True:
                for c in children:
                    if not c.alive():
                        assert c.proc is not None
                        say(T("plain_exited", name=c.name, code=c.proc.returncode, log=c.log_path))
                        code = 1
                        raise KeyboardInterrupt
                errs = backend.snapshot()[1] + frontend.snapshot()[1]
                if errs != seen_errors:
                    seen_errors = errs
                    recent = sorted(backend.snapshot()[3] + frontend.snapshot()[3])[-1:]
                    for stamp, line in recent:
                        say(T("plain_error", stamp=stamp, line=line))
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
                        say(T("server_exited", name=c.name, code=c.proc.returncode, log=c.log_path))
                        raise KeyboardInterrupt
                try:
                    k = keyq.get(timeout=0.25)
                except queue.Empty:
                    continue
                if k in ("q", "Q", "\x03"):
                    raise KeyboardInterrupt
                if k in ("o", "O"):
                    webbrowser.open(web_url)
                    board.set_note(T("opened_browser"))
    except KeyboardInterrupt:
        say(T("stopping"))
    finally:
        stop.set()
        for c in children:
            c.stop()
    say(T("stopped"))
    return code


if __name__ == "__main__":
    sys.exit(main())
