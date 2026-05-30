#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookiejar import MozillaCookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from playwright.async_api import async_playwright


APP_NAME = "X Auto Downloader"
DEFAULT_CONFIG_PATH = Path("/config/config.json")
TWEET_RE = re.compile(r"/([^/?#]+)/status/(\d+)")
MEDIA_ID_RE = re.compile(r"/media/([^?./]+)(?:\.[a-zA-Z0-9]+)?")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}


DEFAULT_CONFIG: dict[str, Any] = {
    "run_interval_hours": 12,
    "run_interval_seconds": 43200,
    "database": "/state/x_auto.sqlite3",
    "cookie_file": "/config/x_cookies.txt",
    "download_dir": "/downloads",
    "default_user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "request_delay_seconds": 3,
    "jitter_seconds": 2,
    "retry_failed": True,
    "max_download_attempts": 0,
    "known_stop_consecutive": 10,
    "stop_marker": {
        "enabled": True,
        "url": "https://x.com/deskt3d/status/1992264334853165368?s=20",
    },
    "browser": {
        "enabled": True,
        "headless": True,
        "likes_url": "",
        "scroll_delay_min_ms": 1500,
        "scroll_delay_max_ms": 4000,
        "scroll_pixels_min": 520,
        "scroll_pixels_max": 1120,
        "pause_every_scrolls": 18,
        "pause_min_seconds": 10,
        "pause_max_seconds": 30,
        "max_scrolls": 0,
        "max_idle_scrolls": 30,
        "target_timeout_ms": 45000,
        "screenshot_enabled": False,
        "screenshot_full_page": False,
        "screenshot_timeout_ms": 10000,
    },
    "media": {
        "video_format": "bv*+ba/b",
        "convert_gif": True,
        "image_candidates": [
            "{media_id}.png?name=4096x4096",
            "{media_id}.jpg?name=4096x4096",
            "{media_id}?format=png&name=4096x4096",
            "{media_id}?format=jpg&name=orig",
            "{media_id}?format=jpg&name=4096x4096",
            "{media_id}.jpg?name=orig",
        ],
    },
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8080,
        "log_lines": 300,
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def tweet_id_from_url(url: str) -> str:
    value = str(url or "").strip()
    if value.isdigit():
        return value
    match = re.search(r"/status/(\d+)", value)
    return match.group(1) if match else ""


def interval_hours(config: dict[str, Any]) -> float:
    if "run_interval_hours" in config:
        try:
            return max(0.01, float(config.get("run_interval_hours") or 12))
        except (TypeError, ValueError):
            return 12.0
    try:
        return max(0.01, float(config.get("run_interval_seconds", 43200)) / 3600)
    except (TypeError, ValueError):
        return 12.0


def media_hint_from_item(item: dict[str, Any]) -> str:
    has_image = bool(item.get("media_ids"))
    has_video = bool(item.get("has_video"))
    if has_image and has_video:
        return "mixed"
    if has_video:
        return "video"
    if has_image:
        return "image"
    return "unknown"


def media_hint_from_files(files: list[str]) -> str:
    has_image = False
    has_gif = False
    has_video = False
    for file in files:
        path = Path(file)
        suffix = path.suffix.lower()
        parts = {part.lower() for part in path.parts}
        if suffix == ".gif":
            has_gif = True
            has_image = True
        elif "images" in parts or suffix in IMAGE_EXTENSIONS:
            has_image = True
        elif "videos" in parts or suffix in VIDEO_EXTENSIONS:
            has_video = True
    if has_image and has_video:
        return "mixed"
    if has_video:
        return "video"
    if has_gif:
        return "gif"
    if has_image:
        return "image"
    return ""


def existing_media_files(files: list[str]) -> list[str]:
    valid = []
    allowed = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    for file in files:
        path = Path(file)
        try:
            if path.is_file() and path.stat().st_size > 0 and path.suffix.lower() in allowed:
                valid.append(str(path))
        except OSError:
            continue
    return valid


def item_from_url(url: str) -> dict[str, Any]:
    value = str(url or "").strip()
    match = TWEET_RE.search(value)
    tweet_id = tweet_id_from_url(value)
    author = match.group(1) if match else ""
    if not tweet_id:
        raise ValueError("请输入有效的 X/Twitter 推文链接")
    if not author:
        author = "unknown"
    return {
        "tweet_id": tweet_id,
        "author": author,
        "url": f"https://x.com/{author}/status/{tweet_id}" if author != "unknown" else value,
        "text": "",
        "media_ids": [],
        "has_video": False,
    }


def tail_text(text: str, limit: int = 2000) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


class RingLog:
    def __init__(self, max_lines: int = 300):
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def write(self, message: str) -> None:
        line = f"[{now_iso()}] {message}"
        print(line, flush=True)
        with self._lock:
            self._lines.append(line)
            self._lines = self._lines[-self.max_lines :]

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


class Store:
    def __init__(self, db_path: Path, log: RingLog):
        self.db_path = db_path
        self.log = log
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists tweets (
                    tweet_id text primary key,
                    url text not null,
                    author text,
                    text text,
                    media_hint text,
                    status text not null default 'pending',
                    attempts integer not null default 0,
                    files_json text not null default '[]',
                    error text,
                    first_seen_at text not null,
                    downloaded_at text,
                    updated_at text not null
                );
                create index if not exists idx_tweets_status on tweets(status);
                create table if not exists runs (
                    id integer primary key autoincrement,
                    started_at text not null,
                    finished_at text,
                    status text not null,
                    discovered integer not null default 0,
                    downloaded integer not null default 0,
                    skipped integer not null default 0,
                    failed integer not null default 0,
                    message text
                );
                """
            )

    def begin_run(self) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "insert into runs(started_at, status) values(?, 'running')",
                (now_iso(),),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict[str, int], message: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                update runs
                set finished_at=?, status=?, discovered=?, downloaded=?, skipped=?, failed=?, message=?
                where id=?
                """,
                (
                    now_iso(),
                    status,
                    stats.get("discovered", 0),
                    stats.get("downloaded", 0),
                    stats.get("skipped", 0),
                    stats.get("failed", 0),
                    message[-2000:],
                    run_id,
                ),
            )

    def upsert_seen(self, item: dict[str, Any]) -> None:
        media_hint = media_hint_from_item(item)
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                insert into tweets(tweet_id, url, author, text, media_hint, first_seen_at, updated_at)
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(tweet_id) do update set
                    url=excluded.url,
                    author=coalesce(excluded.author, tweets.author),
                    text=coalesce(excluded.text, tweets.text),
                    media_hint=case
                        when excluded.media_hint != 'unknown' then excluded.media_hint
                        else tweets.media_hint
                    end,
                    updated_at=excluded.updated_at
                """,
                (
                    item["tweet_id"],
                    item["url"],
                    item.get("author", ""),
                    item.get("text", ""),
                    media_hint,
                    now_iso(),
                    now_iso(),
                ),
            )

    def get_tweet(self, tweet_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("select * from tweets where tweet_id=?", (tweet_id,)).fetchone()

    def _row_has_existing_media(self, row: sqlite3.Row) -> bool:
        try:
            files = json.loads(str(row["files_json"] or "[]"))
        except json.JSONDecodeError:
            return False
        return bool(existing_media_files(files))

    def is_done(self, tweet_id: str) -> bool:
        row = self.get_tweet(tweet_id)
        return bool(row and row["status"] == "done" and self._row_has_existing_media(row))

    def should_download(self, tweet_id: str, retry_failed: bool, max_attempts: int) -> bool:
        row = self.get_tweet(tweet_id)
        if not row:
            return True
        if row["status"] == "done":
            if not self._row_has_existing_media(row):
                self.log.write(f"记录为 done 但本地媒体文件不存在，将重新下载：{tweet_id}")
                return True
            return False
        if row["status"] == "failed":
            if not retry_failed:
                return False
            if max_attempts > 0 and int(row["attempts"]) >= max_attempts:
                return False
        return True

    def mark_result(
        self,
        tweet_id: str,
        status: str,
        files: list[str],
        error: str = "",
        media_hint: str = "",
    ) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                update tweets
                set status=?, attempts=attempts+1, files_json=?, error=?,
                    media_hint=case when ? != '' then ? else media_hint end,
                    downloaded_at=case when ?='done' then ? else downloaded_at end,
                    updated_at=?
                where tweet_id=?
                """,
                (
                    status,
                    json.dumps(files, ensure_ascii=False),
                    error[-2000:],
                    media_hint,
                    media_hint,
                    status,
                    now_iso(),
                    now_iso(),
                    tweet_id,
                ),
            )

    def recent_tweets(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from tweets order by updated_at desc limit ?", (limit,)
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    files = json.loads(str(item.get("files_json") or "[]"))
                except json.JSONDecodeError:
                    files = []
                file_hint = media_hint_from_files(files)
                if file_hint and item.get("media_hint") in {"", "unknown", None}:
                    item["media_hint"] = file_hint
                if item.get("media_hint") in {"", "unknown", None} and "No video could be found" in str(item.get("error") or ""):
                    item["media_hint"] = "manual_check"
                item["files_count"] = len(files)
                result.append(item)
            return result

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from runs order by id desc limit ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return deep_merge(DEFAULT_CONFIG, json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            traceback.print_exc()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_cookie_text(cookie_text: str) -> tuple[list[dict[str, Any]], str | None, str]:
    text = cookie_text.strip()
    if not text:
        return [], None, ""
    if "# Netscape HTTP Cookie File" in text or "\t" in text:
        tmp = Path("/tmp/x_cookies_parse.txt") if os.name != "nt" else Path("downloads/probe/tmp_cookie_parse.txt")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text + "\n", encoding="utf-8")
        return parse_netscape_cookie_file(tmp) + ("netscape",)
    cookies = []
    user_id = None
    expires = int(time.time()) + 86400 * 180
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if not name:
            continue
        if name == "twid":
            user_id = value.removeprefix("u%3D")
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": name in {"auth_token"},
                "sameSite": "Lax",
                "expires": expires,
            }
        )
    return cookies, user_id, "header"


def parse_netscape_cookie_file(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies: list[dict[str, Any]] = []
    user_id = None
    now = int(time.time())
    for item in jar:
        if item.name == "twid":
            user_id = item.value.removeprefix("u%3D")
        domain = item.domain or ".x.com"
        if "twitter.com" in domain:
            domain = domain.replace("twitter.com", "x.com")
        cookie: dict[str, Any] = {
            "name": item.name,
            "value": item.value,
            "domain": domain,
            "path": item.path or "/",
            "secure": bool(item.secure),
            "httpOnly": bool("HttpOnly" in item._rest),
            "sameSite": "Lax",
        }
        if item.expires and item.expires > now:
            cookie["expires"] = item.expires
        cookies.append(cookie)
    return cookies, user_id


def write_cookie_file(path: Path, cookie_text: str) -> None:
    text = cookie_text.strip()
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if "# Netscape HTTP Cookie File" in text or "\t" in text:
        path.write_text(text + "\n", encoding="utf-8")
        return
    expires = int(time.time()) + 86400 * 180
    lines = ["# Netscape HTTP Cookie File"]
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            lines.append(f".x.com\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cookie_diagnostics(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "valid": False,
        "count": 0,
        "has_auth_token": False,
        "has_ct0": False,
        "has_twid": False,
        "user_id": "",
        "domains": [],
        "error": "",
    }
    if not path.exists() or path.stat().st_size <= 0:
        info["error"] = f"cookie file not found or empty: {path}"
        return info
    try:
        cookies, user_id = parse_netscape_cookie_file(path)
    except Exception as error:
        info["error"] = str(error)
        return info
    names = {str(cookie.get("name") or "") for cookie in cookies}
    domains = sorted({str(cookie.get("domain") or "") for cookie in cookies if cookie.get("domain")})
    info.update(
        {
            "valid": bool(cookies),
            "count": len(cookies),
            "has_auth_token": "auth_token" in names,
            "has_ct0": "ct0" in names,
            "has_twid": "twid" in names,
            "user_id": user_id or "",
            "domains": domains[:12],
        }
    )
    if not info["has_auth_token"] or not info["has_ct0"]:
        info["error"] = "缺少 auth_token 或 ct0，通常代表这份 Cookie 不能作为已登录账号使用"
    return info


@dataclass
class BrowserResult:
    screen_name: str
    likes_url: str
    tweets: list[dict[str, Any]]
    stop_found: bool
    screenshot: str


class BrowserCollector:
    def __init__(self, config: dict[str, Any], log: RingLog, store: Store | None = None, progress: Any | None = None):
        self.config = config
        self.log = log
        self.store = store
        self.progress = progress

    def _browser_executable(self) -> str | None:
        env = os.environ.get("CHROME_PATH")
        if env and Path(env).exists():
            return env
        if os.name == "nt":
            for path in [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]:
                if Path(path).exists():
                    return path
        return None

    def _load_cookies(self) -> tuple[list[dict[str, Any]], str | None]:
        cookie_file = Path(self.config["cookie_file"])
        if not cookie_file.exists():
            raise RuntimeError(f"cookie file not found: {cookie_file}")
        cookies, user_id = parse_netscape_cookie_file(cookie_file)
        if not cookies:
            raise RuntimeError("cookie file is empty or invalid")
        return cookies, user_id

    async def _open_browser_context(self, playwright: Any, cookies: list[dict[str, Any]]) -> tuple[Any, Any]:
        browser_cfg = self.config["browser"]
        launch_kwargs: dict[str, Any] = {
            "headless": bool(browser_cfg.get("headless", True)),
            "args": ["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
        }
        if exe := self._browser_executable():
            launch_kwargs["executable_path"] = exe
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
            user_agent=self.config.get("default_user_agent"),
        )
        await context.add_cookies(cookies)
        return browser, context

    async def _resolve_screen_name(self, page: Any, user_id: str) -> str:
        timeout = int(self.config["browser"].get("target_timeout_ms", 45000))
        await page.goto(f"https://x.com/i/user/{user_id}", wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(random.randint(2500, 4200))
        match = re.match(r"https://x\.com/([^/?#]+)", page.url)
        if match and match.group(1) not in {"i", "home", "login"}:
            return match.group(1)
        raise RuntimeError("could not resolve current X screen name from cookie")

    async def _collect_visible(self, page: Any) -> list[dict[str, Any]]:
        return await page.evaluate(
            """() => {
              const rows = [];
              for (const article of document.querySelectorAll('article')) {
                const link = Array.from(article.querySelectorAll('a[href*="/status/"]'))
                  .map(a => new URL(a.getAttribute('href'), location.href).href)
                  .find(h => /https:\\/\\/x\\.com\\/[^/]+\\/status\\/\\d+/.test(h));
                if (!link) continue;
                const match = link.match(/https:\\/\\/x\\.com\\/([^/]+)\\/status\\/(\\d+)/);
                if (!match) continue;
                const mediaIds = [];
                for (const img of article.querySelectorAll('img[src*="twimg.com/media"]')) {
                  const m = img.src.match(/\\/media\\/([^?./]+)(?:\\.[a-zA-Z0-9]+)?/);
                  if (m && !mediaIds.includes(m[1])) mediaIds.push(m[1]);
                }
                const videoThumbs = Array.from(article.querySelectorAll(
                  'img[src*="twimg.com/amplify_video_thumb"], img[src*="twimg.com/ext_tw_video_thumb"], img[src*="twimg.com/tweet_video_thumb"]'
                ));
                const videoLike = !!article.querySelector('video')
                  || !!article.querySelector('[data-testid="videoPlayer"], [data-testid="videoComponent"], [data-testid="playButton"]')
                  || videoThumbs.length > 0
                  || /播放|Play|Watch/i.test(article.innerText);
                rows.push({
                  tweet_id: match[2],
                  author: match[1],
                  url: `https://x.com/${match[1]}/status/${match[2]}`,
                  text: article.innerText.slice(0, 1000),
                  media_ids: mediaIds,
                  has_video: videoLike
                });
              }
              return rows;
            }"""
        )

    async def collect_single(self, url: str) -> dict[str, Any]:
        fallback = item_from_url(url)
        tweet_id = fallback["tweet_id"]
        cookies, _user_id = self._load_cookies()
        browser_cfg = self.config["browser"]
        timeout = int(browser_cfg.get("target_timeout_ms", 45000))
        async with async_playwright() as p:
            browser, context = await self._open_browser_context(p, cookies)
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            try:
                self.log.write(f"打开单条推文页面：{fallback['url']}")
                await page.goto(fallback["url"], wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(random.randint(2500, 4200))
                rows = await self._collect_visible(page)
                for row in rows:
                    if row["tweet_id"] == tweet_id:
                        return row
                self.log.write("单条页面没有解析到媒体卡片，将回退到 yt-dlp 直接测试")
                return {**fallback, "has_video": True}
            finally:
                await context.close()
                await browser.close()

    async def collect(self) -> BrowserResult:
        cookies, user_id = self._load_cookies()

        stop_id = ""
        if self.config.get("stop_marker", {}).get("enabled", True):
            stop_id = tweet_id_from_url(str(self.config.get("stop_marker", {}).get("url", "")))

        out_dir = Path(self.config["download_dir"]) / "_browser"
        out_dir.mkdir(parents=True, exist_ok=True)
        browser_cfg = self.config["browser"]
        timeout = int(browser_cfg.get("target_timeout_ms", 45000))
        max_scrolls = int(browser_cfg.get("max_scrolls", 0) or 0)
        max_idle = int(browser_cfg.get("max_idle_scrolls", 30) or 30)
        min_delay = int(browser_cfg.get("scroll_delay_min_ms", 1500))
        max_delay = int(browser_cfg.get("scroll_delay_max_ms", 4000))
        min_pixels = int(browser_cfg.get("scroll_pixels_min", 520))
        max_pixels = int(browser_cfg.get("scroll_pixels_max", 1120))
        pause_every = int(browser_cfg.get("pause_every_scrolls", 18) or 0)
        known_stop = int(self.config.get("known_stop_consecutive", 10) or 0)

        seen: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        stop_found = False
        consecutive_done = 0
        known_stop_found = False

        async with async_playwright() as p:
            browser, context = await self._open_browser_context(p, cookies)
            page = await context.new_page()
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            try:
                likes_url = str(browser_cfg.get("likes_url") or "").strip()
                if likes_url:
                    screen_name = urlparse(likes_url).path.strip("/").split("/")[0]
                else:
                    if not user_id:
                        raise RuntimeError("twid cookie not found; set browser.likes_url manually")
                    screen_name = await self._resolve_screen_name(page, user_id)
                    likes_url = f"https://x.com/{screen_name}/likes"

                self.log.write(f"Opening X likes page: {likes_url}")
                await page.goto(likes_url, wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(random.randint(3500, 5500))

                idle = 0
                scroll = 0
                while True:
                    rows = await self._collect_visible(page)
                    before = len(seen)
                    for row in rows:
                        tweet_id = row["tweet_id"]
                        if tweet_id not in seen:
                            ordered_ids.append(tweet_id)
                            seen[tweet_id] = row
                        else:
                            current = seen[tweet_id]
                            current["media_ids"] = sorted(set(current.get("media_ids", []) + row.get("media_ids", [])))
                            current["has_video"] = current.get("has_video") or row.get("has_video")
                            if len(row.get("text", "")) > len(current.get("text", "")):
                                current["text"] = row.get("text", "")
                        if stop_id and tweet_id == stop_id:
                            stop_found = True
                        if tweet_id not in seen or seen[tweet_id] is row:
                            if self.store and self.store.is_done(tweet_id):
                                consecutive_done += 1
                            else:
                                consecutive_done = 0
                            if known_stop > 0 and consecutive_done >= known_stop:
                                known_stop_found = True

                    new_count = len(seen) - before
                    if scroll % 5 == 0 or new_count:
                        self.log.write(
                            f"Likes scroll {scroll}: total={len(seen)}, new={new_count}, consecutive_done={consecutive_done}"
                        )
                    if self.progress:
                        self.progress(
                            {
                                "phase": "collecting",
                                "collected": len(seen),
                                "scroll": scroll,
                                "new_on_last_scroll": new_count,
                            }
                        )
                    if stop_found:
                        self.log.write(f"Stop marker found: {stop_id}")
                        break
                    if known_stop_found:
                        self.log.write(f"连续 {consecutive_done} 条已下载，停止继续向后翻")
                        break
                    if max_scrolls > 0 and scroll >= max_scrolls:
                        self.log.write(f"Reached max_scrolls={max_scrolls}")
                        break
                    idle = idle + 1 if new_count == 0 else 0
                    if idle >= max_idle:
                        self.log.write(f"No new tweets for {idle} scrolls; stopping collection")
                        break

                    scroll += 1
                    await page.mouse.wheel(0, random.randint(min_pixels, max_pixels))
                    await page.wait_for_timeout(random.randint(min_delay, max_delay))
                    if pause_every and scroll % pause_every == 0:
                        low = int(browser_cfg.get("pause_min_seconds", 10))
                        high = int(browser_cfg.get("pause_max_seconds", 30))
                        pause = random.randint(low, high)
                        self.log.write(f"Human-like pause: {pause}s")
                        await page.wait_for_timeout(pause * 1000)

                screenshot = ""
                if bool(browser_cfg.get("screenshot_enabled", False)):
                    screenshot = str(out_dir / "last_likes_page.png")
                    try:
                        await page.screenshot(
                            path=screenshot,
                            full_page=bool(browser_cfg.get("screenshot_full_page", False)),
                            timeout=int(browser_cfg.get("screenshot_timeout_ms", 10000)),
                        )
                    except Exception as error:
                        self.log.write(f"截图失败，已跳过，不影响下载：{error}")
            finally:
                await context.close()
                await browser.close()

        tweets = [seen[tweet_id] for tweet_id in ordered_ids]
        if stop_found and stop_id:
            before_marker: list[dict[str, Any]] = []
            for row in tweets:
                if row["tweet_id"] == stop_id:
                    break
                before_marker.append(row)
            tweets = before_marker
        return BrowserResult(screen_name, likes_url, tweets, stop_found, screenshot)


class Downloader:
    def __init__(self, config: dict[str, Any], store: Store, log: RingLog):
        self.config = config
        self.store = store
        self.log = log

    def _media_dirs(self) -> dict[str, Path]:
        root = Path(self.config["download_dir"])
        dirs = {
            "images": root / "images",
            "videos": root / "videos",
            "metadata": root / "_metadata",
            "thumbnails": root / "_thumbnails",
            "tmp": root / "_tmp",
        }
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        return dirs

    def _safe_stem(self, item: dict[str, Any], suffix: str = "") -> str:
        author = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.get("author") or "unknown").strip("_")
        author = author or "unknown"
        stem = f"{item['tweet_id']}_{author}"
        if suffix:
            stem = f"{stem}_{suffix}"
        return stem[:180]

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(2, 10000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"could not find unique filename for {path}")

    def _image_candidates(self, media_id: str) -> list[tuple[str, str]]:
        patterns = self.config.get("media", {}).get("image_candidates") or DEFAULT_CONFIG["media"]["image_candidates"]
        result = []
        for pattern in patterns:
            url_part = pattern.format(media_id=media_id)
            ext = "png" if ".png" in url_part or "format=png" in url_part else "jpg"
            result.append((f"https://pbs.twimg.com/media/{url_part}", ext))
        return result

    def _download_image(self, url: str, path: Path) -> bool:
        headers = {"User-Agent": self.config.get("default_user_agent", "")}
        with requests.get(url, headers=headers, timeout=45, stream=True) as response:
            if response.status_code != 200:
                return False
            ctype = response.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                return False
            tmp = path.with_suffix(path.suffix + ".part")
            with tmp.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        file.write(chunk)
            tmp.replace(path)
            return path.stat().st_size > 0

    def _valid_media_files(self, files: list[str]) -> list[str]:
        valid = []
        allowed = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
        for file in files:
            path = Path(file)
            try:
                if path.is_file() and path.stat().st_size > 0 and path.suffix.lower() in allowed:
                    valid.append(str(path))
                else:
                    self.log.write(f"忽略无效媒体文件：{file}")
            except OSError:
                self.log.write(f"忽略不存在的媒体文件：{file}")
        return valid

    def download_images(self, item: dict[str, Any]) -> list[str]:
        media_ids = item.get("media_ids") or []
        files = []
        if not media_ids:
            return files
        out_dir = self._media_dirs()["images"]
        for index, media_id in enumerate(media_ids, start=1):
            done = False
            for candidate, ext in self._image_candidates(media_id):
                target = out_dir / f"{self._safe_stem(item, f'{index}_{media_id}')}.{ext}"
                if target.exists() and target.stat().st_size > 0:
                    files.append(str(target))
                    done = True
                    break
                try:
                    if self._download_image(candidate, target):
                        self.log.write(f"Image downloaded: {target.name}")
                        files.append(str(target))
                        done = True
                        break
                except Exception as error:
                    self.log.write(f"Image candidate failed: {media_id} {error}")
            if not done:
                self.log.write(f"No image candidate worked for media id: {media_id}")
        return files

    def download_video(self, item: dict[str, Any]) -> list[str]:
        if not item.get("has_video"):
            return []
        dirs = self._media_dirs()
        tmp_dir = dirs["tmp"] / item["tweet_id"]
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "yt-dlp",
            "--cookies",
            str(self.config["cookie_file"]),
            "--format",
            str(self.config.get("media", {}).get("video_format", "bv*+ba/b")),
            "--merge-output-format",
            "mp4",
            "--write-info-json",
            "--write-thumbnail",
            "--no-overwrites",
            "-o",
            str(tmp_dir / "%(uploader_id)s_%(id)s.%(ext)s"),
            item["url"],
        ]
        self.log.write(f"yt-dlp: {item['url']}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            detail = tail_text((result.stderr or "") + "\n" + (result.stdout or ""))
            raise RuntimeError(f"yt-dlp failed with code {result.returncode}: {detail}")
        output_tail = tail_text(result.stdout or result.stderr or "", 800)
        if output_tail:
            self.log.write(f"yt-dlp 输出：{output_tail}")
        media_files = [
            p for p in tmp_dir.glob("*")
            if p.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if not media_files:
            self._move_sidecars(tmp_dir, item)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return []
        info_files = list(tmp_dir.glob("*.info.json"))
        is_gif = self._looks_like_x_gif(info_files)
        files: list[str] = []
        for index, media_file in enumerate(media_files, start=1):
            if is_gif:
                gif_target = self._unique_path(
                    dirs["images"] / f"{self._safe_stem(item, f'gif_{index}')}.gif"
                )
                if self.config.get("media", {}).get("convert_gif", True):
                    converted = self._convert_mp4_to_gif(media_file, gif_target)
                    if converted:
                        files.append(str(gif_target))
                        continue
                fallback = self._unique_path(
                    dirs["images"] / f"{self._safe_stem(item, f'gif_{index}')}{media_file.suffix}"
                )
                shutil.move(str(media_file), fallback)
                files.append(str(fallback))
            else:
                target = self._unique_path(
                    dirs["videos"] / f"{self._safe_stem(item, str(index))}{media_file.suffix}"
                )
                shutil.move(str(media_file), target)
                files.append(str(target))
        self._move_sidecars(tmp_dir, item)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return files

    def _looks_like_x_gif(self, info_files: list[Path]) -> bool:
        for info_file in info_files:
            try:
                data = json.loads(info_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key in ("url", "thumbnail"):
                if "/tweet_video" in str(data.get(key) or ""):
                    return True
            for fmt in data.get("formats") or []:
                if "/tweet_video" in str(fmt.get("url") or ""):
                    return True
        return False

    def _convert_mp4_to_gif(self, source: Path, target: Path) -> bool:
        if not shutil.which("ffmpeg"):
            self.log.write("ffmpeg not found; keeping X GIF as mp4 fallback")
            return False
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            "fps=15,scale=iw:-1:flags=lanczos",
            "-loop",
            "0",
            str(target),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if result.returncode != 0 or not target.exists() or target.stat().st_size <= 0:
            self.log.write(f"GIF conversion failed: {(result.stderr or result.stdout)[-1000:]}")
            return False
        try:
            source.unlink()
        except OSError:
            pass
        self.log.write(f"GIF converted: {target.name}")
        return True

    def _move_sidecars(self, tmp_dir: Path, item: dict[str, Any]) -> None:
        dirs = self._media_dirs()
        for path in tmp_dir.glob("*"):
            suffix = path.suffix.lower()
            if suffix == ".json":
                target_dir = dirs["metadata"]
            elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                target_dir = dirs["thumbnails"]
            else:
                continue
            target = self._unique_path(target_dir / f"{self._safe_stem(item)}_{path.name}")
            shutil.move(str(path), target)

    def download_item(self, item: dict[str, Any], force: bool = False) -> tuple[str, list[str], str]:
        tweet_id = item["tweet_id"]
        self.store.upsert_seen(item)
        retry_failed = bool(self.config.get("retry_failed", True))
        max_attempts = int(self.config.get("max_download_attempts", 0) or 0)
        if not force and not self.store.should_download(tweet_id, retry_failed, max_attempts):
            return "skipped", [], ""
        media_hint = media_hint_from_item(item)
        try:
            files = []
            files.extend(self.download_images(item))
            fallback_error = ""
            if item.get("has_video"):
                files.extend(self.download_video(item))
            if not files:
                try:
                    files.extend(self.download_video({**item, "has_video": True}))
                except Exception as error:
                    fallback_error = str(error)
                    if media_hint == "unknown" and "No video could be found" in fallback_error:
                        media_hint = "manual_check"
            files = self._valid_media_files(files)
            file_hint = media_hint_from_files(files)
            if file_hint:
                media_hint = file_hint
            status = "done" if files else "failed"
            error = "" if files else (fallback_error or "no downloadable media found")
            self.store.mark_result(tweet_id, status, files, error, media_hint)
            return status, files, error
        except Exception as error:
            self.store.mark_result(tweet_id, "failed", [], str(error), media_hint)
            return "failed", [], str(error)


class App:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.log = RingLog(int(self.config.get("web", {}).get("log_lines", 300)))
        self.store = Store(Path(self.config["database"]), self.log)
        self.run_lock = threading.Lock()
        self.running = False
        self.last_run_message = ""
        self.next_run_at = 0.0
        self.stop_event = threading.Event()
        self.progress_lock = threading.Lock()
        self.progress: dict[str, Any] = self._empty_progress()
        self.local_counts_cache: dict[str, Any] = {"images": 0, "gifs": 0, "videos": 0, "bytes": 0}
        self.local_counts_at = 0.0

    def _empty_progress(self) -> dict[str, Any]:
        return {
            "phase": "idle",
            "collected": 0,
            "scroll": 0,
            "new_on_last_scroll": 0,
            "download_total": 0,
            "download_done": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "images": 0,
            "gifs": 0,
            "videos": 0,
            "current_url": "",
            "message": "",
        }

    def set_progress(self, patch: dict[str, Any]) -> None:
        with self.progress_lock:
            self.progress.update(patch)

    def get_progress(self) -> dict[str, Any]:
        with self.progress_lock:
            return dict(self.progress)

    def reload_config(self) -> None:
        self.config = load_config(self.config_path)
        self.log.max_lines = int(self.config.get("web", {}).get("log_lines", 300))

    def save_config(self, patch: dict[str, Any]) -> None:
        self.config = deep_merge(self.config, patch)
        save_config(self.config_path, self.config)

    def cookie_summary(self) -> dict[str, Any]:
        return cookie_diagnostics(Path(self.config["cookie_file"]))

    def local_media_counts(self) -> dict[str, Any]:
        if time.time() - self.local_counts_at < 10:
            return dict(self.local_counts_cache)
        root = Path(self.config["download_dir"])
        result = {"images": 0, "gifs": 0, "videos": 0, "bytes": 0}
        targets = [
            (root / "images", "images"),
            (root / "videos", "videos"),
        ]
        for folder, kind in targets:
            if not folder.exists():
                continue
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if kind == "images" and suffix in IMAGE_EXTENSIONS:
                    result["images"] += 1
                    if suffix == ".gif":
                        result["gifs"] += 1
                elif kind == "videos" and suffix in VIDEO_EXTENSIONS:
                    result["videos"] += 1
                else:
                    continue
                try:
                    result["bytes"] += path.stat().st_size
                except OSError:
                    pass
        self.local_counts_cache = dict(result)
        self.local_counts_at = time.time()
        return result

    def _acquire_run(self, phase: str) -> bool:
        if not self.run_lock.acquire(blocking=False):
            self.log.write("已有任务正在运行，本次操作未启动")
            return False
        self.running = True
        with self.progress_lock:
            self.progress = self._empty_progress()
            self.progress["phase"] = phase
        return True

    def _release_run(self, message: str = "") -> None:
        self.last_run_message = message
        self.running = False
        self.run_lock.release()

    def run_cookie_test(self, test_url: str) -> None:
        if not self._acquire_run("testing_cookie"):
            return
        message = ""
        try:
            self.reload_config()
            url = str(test_url or "").strip() or str(self.config.get("stop_marker", {}).get("url") or "")
            summary = self.cookie_summary()
            self.log.write(
                "Cookie 检查："
                f"valid={summary.get('valid')} count={summary.get('count')} "
                f"auth_token={summary.get('has_auth_token')} ct0={summary.get('has_ct0')} "
                f"twid={summary.get('has_twid')} user_id={summary.get('user_id') or '-'}"
            )
            if summary.get("error"):
                self.log.write(f"Cookie 提示：{summary['error']}")
            if not url:
                self.log.write("Cookie 测试未提供推文链接，仅完成文件格式检查")
                return
            if not tweet_id_from_url(url):
                self.log.write("Cookie 测试链接不是有效推文 URL")
                return
            self.set_progress({"phase": "testing_cookie", "current_url": url, "message": "正在用 yt-dlp 测试 Cookie"})
            command = [
                "yt-dlp",
                "--cookies",
                str(self.config["cookie_file"]),
                "--simulate",
                "--no-download",
                "--dump-single-json",
                "--no-warnings",
                url,
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                detail = tail_text((result.stderr or "") + "\n" + (result.stdout or ""), 1800)
                self.log.write(f"Cookie/yt-dlp 测试失败：{detail}")
                message = "cookie test failed"
                return
            data = json.loads(result.stdout)
            formats = data.get("formats") or []
            self.log.write(
                "Cookie/yt-dlp 测试成功："
                f"display_id={data.get('display_id') or data.get('id')} "
                f"title={(data.get('title') or '')[:80]} formats={len(formats)} "
                f"age_limit={data.get('age_limit')}"
            )
            message = "cookie test ok"
        except Exception as error:
            message = str(error)
            self.log.write(f"Cookie 测试异常：{message}")
            self.log.write(traceback.format_exc())
        finally:
            self.set_progress({"phase": "idle", "current_url": ""})
            self._release_run(message)

    def run_manual_download(self, url: str) -> None:
        if not self._acquire_run("manual_download"):
            return
        run_id = self.store.begin_run()
        stats = {"discovered": 1, "downloaded": 0, "skipped": 0, "failed": 0}
        message = ""
        try:
            self.reload_config()
            item = item_from_url(url)
            self.log.write(f"手动下载开始：{item['url']}")
            self.set_progress(
                {
                    "phase": "manual_download",
                    "download_total": 1,
                    "download_done": 0,
                    "collected": 1,
                    "current_url": item["url"],
                }
            )
            try:
                collector = BrowserCollector(self.config, self.log, self.store, self.set_progress)
                item = asyncio.run(collector.collect_single(item["url"]))
            except Exception as error:
                self.log.write(f"手动下载浏览器探测失败，回退到 yt-dlp：{error}")
                item = {**item, "has_video": True}
            downloader = Downloader(self.config, self.store, self.log)
            status, files, error = downloader.download_item(item, force=True)
            if status == "done":
                stats["downloaded"] = 1
            elif status == "skipped":
                stats["skipped"] = 1
            else:
                stats["failed"] = 1
                self.log.write(f"手动下载失败 {item['url']}: {error}")
            counts = self._count_media_files(files)
            self.set_progress(
                {
                    "download_done": 1,
                    "downloaded": stats["downloaded"],
                    "skipped": stats["skipped"],
                    "failed": stats["failed"],
                    "images": counts["images"],
                    "gifs": counts["gifs"],
                    "videos": counts["videos"],
                    "current_url": "",
                }
            )
            message = status if not error else error
            self.store.finish_run(run_id, "done" if status == "done" else "failed", stats, message)
            self.log.write(f"手动下载结束：{status} {item['url']}")
        except Exception as error:
            message = str(error)
            stats["failed"] = 1
            self.log.write(f"手动下载异常：{message}")
            self.log.write(traceback.format_exc())
            self.store.finish_run(run_id, "failed", stats, message)
        finally:
            self.set_progress({"phase": "idle", "current_url": ""})
            self._release_run(message)

    def run_once_async(self) -> dict[str, int]:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("a run is already active")
        self.running = True
        with self.progress_lock:
            self.progress = self._empty_progress()
            self.progress["phase"] = "starting"
        run_id = self.store.begin_run()
        stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        message = ""
        try:
            self.reload_config()
            self.log.write("Run started")
            collector = BrowserCollector(self.config, self.log, self.store, self.set_progress)
            result = asyncio.run(collector.collect())
            stats["discovered"] = len(result.tweets)
            self.set_progress(
                {
                    "phase": "downloading",
                    "download_total": len(result.tweets),
                    "download_done": 0,
                    "collected": len(result.tweets),
                }
            )
            self.log.write(
                f"Collected {len(result.tweets)} liked tweet(s); stop_found={result.stop_found}"
            )
            downloader = Downloader(self.config, self.store, self.log)
            delay = float(self.config.get("request_delay_seconds", 3))
            jitter = float(self.config.get("jitter_seconds", 2))
            for index, item in enumerate(result.tweets, start=1):
                self.set_progress({"phase": "downloading", "current_url": item["url"]})
                status, files, error = downloader.download_item(item)
                if status == "done":
                    stats["downloaded"] += 1
                elif status == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1
                    self.log.write(f"Failed {item['url']}: {error}")
                media_counts = self._count_media_files(files)
                current = self.get_progress()
                self.set_progress(
                    {
                        "download_done": index,
                        "downloaded": stats["downloaded"],
                        "skipped": stats["skipped"],
                        "failed": stats["failed"],
                        "images": int(current.get("images", 0)) + media_counts["images"],
                        "gifs": int(current.get("gifs", 0)) + media_counts["gifs"],
                        "videos": int(current.get("videos", 0)) + media_counts["videos"],
                    }
                )
                self.log.write(f"Progress {index}/{len(result.tweets)}: {status} {item['url']}")
                sleep_for = delay + random.random() * jitter
                if sleep_for > 0 and index < len(result.tweets):
                    time.sleep(sleep_for)
            message = "ok"
            self.set_progress({"phase": "finished", "current_url": ""})
            self.store.finish_run(run_id, "done", stats, message)
            self.log.write(f"Run finished: {stats}")
            return stats
        except Exception as error:
            message = str(error)
            self.log.write(f"Run failed: {message}")
            self.log.write(traceback.format_exc())
            self.set_progress({"phase": "failed", "current_url": "", "failed": stats["failed"]})
            self.store.finish_run(run_id, "failed", stats, message)
            raise
        finally:
            self._release_run(message)

    def _count_media_files(self, files: list[str]) -> dict[str, int]:
        counts = {"images": 0, "gifs": 0, "videos": 0}
        for file in files:
            path = Path(file)
            suffix = path.suffix.lower()
            parts = {part.lower() for part in path.parts}
            if suffix == ".gif":
                counts["gifs"] += 1
                counts["images"] += 1
            elif "images" in parts or suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                counts["images"] += 1
            elif "videos" in parts or suffix in {".mp4", ".mkv", ".webm"}:
                counts["videos"] += 1
        return counts

    def start_run_thread(self) -> None:
        def target() -> None:
            try:
                self.run_once_async()
            except Exception:
                pass

        threading.Thread(target=target, daemon=True).start()

    def start_manual_download_thread(self, url: str) -> None:
        threading.Thread(target=lambda: self.run_manual_download(url), daemon=True).start()

    def start_cookie_test_thread(self, url: str) -> None:
        threading.Thread(target=lambda: self.run_cookie_test(url), daemon=True).start()

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            self.reload_config()
            interval = int(interval_hours(self.config) * 3600)
            if self.next_run_at <= 0:
                self.next_run_at = time.time() + 5
            if time.time() >= self.next_run_at and not self.running:
                self.start_run_thread()
                self.next_run_at = time.time() + interval
            self.stop_event.wait(5)

    def status(self) -> dict[str, Any]:
        cookie_file = Path(self.config["cookie_file"])
        return {
            "running": self.running,
            "next_run_at": datetime.fromtimestamp(self.next_run_at).isoformat() if self.next_run_at else "",
            "cookie_present": cookie_file.exists() and cookie_file.stat().st_size > 0,
            "cookie_summary": self.cookie_summary(),
            "local_media": self.local_media_counts(),
            "config": self.config,
            "runs": self.store.recent_runs(),
            "tweets": self.store.recent_tweets(),
            "logs": self.log.lines(),
            "last_run_message": self.last_run_message,
            "progress": self.get_progress(),
            "run_interval_hours": interval_hours(self.config),
        }


def html_page(app: App) -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__APP_NAME__</title>
  <style>
    :root { color-scheme: light; --bg:#f6f7f9; --panel:#fff; --line:#d9dde5; --text:#1d2433; --muted:#657084; --accent:#111827; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
    header { height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 24px; background:#111827; color:white; }
    main { max-width:1180px; margin:0 auto; padding:20px; display:grid; gap:16px; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }
    h1 { font-size:18px; margin:0; } h2 { font-size:16px; margin:0 0 12px; }
    label { display:block; color:var(--muted); font-size:13px; margin:10px 0 5px; }
    input, textarea { width:100%; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; padding:9px 10px; font:inherit; background:white; }
    textarea { min-height:120px; resize:vertical; }
    button { border:0; background:var(--accent); color:white; border-radius:6px; padding:9px 14px; cursor:pointer; }
    button.secondary { background:#475569; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
    .pill { display:inline-flex; align-items:center; padding:3px 8px; border-radius:999px; background:#e6edf6; font-size:12px; color:#334155; }
    table { width:100%; border-collapse:collapse; font-size:13px; } th,td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    pre { margin:0; background:#0f172a; color:#dbeafe; padding:12px; border-radius:6px; overflow:auto; max-height:520px; white-space:pre-wrap; }
    progress { width:100%; height:16px; accent-color:#111827; }
    .progress-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:12px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfcfe; }
    .metric strong { display:block; font-size:18px; margin-top:4px; }
    .help { color:var(--muted); font-size:13px; line-height:1.65; }
    .muted { color:var(--muted); } .status { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    @media (max-width:760px) { .grid,.progress-grid { grid-template-columns:1fr; } header { padding:0 14px; } main { padding:12px; } }
  </style>
</head>
<body>
  <header><h1>X Auto Downloader</h1><div class="status"><span id="runningPill" class="pill">运行状态：读取中</span><span id="cookiePill" class="pill">Cookie：读取中</span></div></header>
  <main>
    <section>
      <h2>控制</h2>
      <div class="muted" id="scheduleText">正在读取状态...</div>
      <div class="actions">
        <form method="post" action="/run"><button type="submit">立即运行</button></form>
        <form method="post" action="/reload"><button class="secondary" type="submit">重新读取配置</button></form>
      </div>
    </section>
    <section>
      <h2>运行进度</h2>
      <div class="muted" id="phaseText">等待中</div>
      <div class="muted">下载方式：单线程顺序下载，不并发；每条推文处理完后才会处理下一条。</div>
      <label>下载总进度</label>
      <progress id="totalProgress" value="0" max="1"></progress>
      <div class="progress-grid">
        <div class="metric">已采集推文<strong id="collectedMetric">0</strong></div>
        <div class="metric">已下载/总数<strong id="downloadMetric">0 / 0</strong></div>
        <div class="metric">图片/GIF<strong id="imageMetric">0 / 0</strong></div>
        <div class="metric">视频<strong id="videoMetric">0</strong></div>
        <div class="metric">本地文件<strong id="localMetric">0 图 / 0 视频</strong></div>
      </div>
      <div class="muted" id="currentUrl"></div>
    </section>
    <section>
      <h2>手动单条下载</h2>
      <form method="post" action="/manual-download">
        <label>推文 URL</label>
        <input name="manual_url" placeholder="https://x.com/user/status/1234567890">
        <div class="help">会先用浏览器打开单条推文识别图片/视频，再强制重新下载这一条；同一时间仍然只跑一个任务。</div>
        <div class="actions"><button type="submit">下载这一条</button></div>
      </form>
    </section>
    <section>
      <h2>配置</h2>
      <form method="post" action="/settings">
        <div class="grid">
          <div><label>Likes 页面 URL（留空自动使用当前账号）</label><input id="likesUrlInput" name="likes_url"></div>
          <div><label>停止标记 URL</label><input id="stopUrlInput" name="stop_url"></div>
          <div><label>运行间隔（小时）</label><input id="intervalHoursInput" name="interval_hours" type="number" min="0.1" step="0.1"></div>
          <div><label>最大滚动次数（0 表示直到标记或页面无新增）</label><input id="maxScrollsInput" name="max_scrolls" type="number" min="0" step="1"></div>
          <div><label>连续已下载停止数</label><input id="knownStopInput" name="known_stop_consecutive" type="number" min="0" step="1"></div>
        </div>
        <div class="actions"><button type="submit">保存配置</button></div>
      </form>
    </section>
    <section>
      <h2>Cookie</h2>
      <form method="post" action="/cookie">
        <div class="help">
          推荐用浏览器扩展导出 X/Twitter 的 Netscape 格式 cookies.txt。打开 x.com 并保持登录，点击类似 “Get cookies.txt LOCALLY” 的扩展，选择导出当前站点 cookies.txt。<br>
          也支持直接粘贴浏览器开发者工具里复制出来的一行 Cookie header。保存后程序会写入 <code>/config/x_cookies.txt</code>。
        </div>
        <label>粘贴 cookies.txt 内容或一行 Cookie header</label>
        <textarea name="cookie_text" placeholder="# Netscape HTTP Cookie File..."></textarea>
        <div class="actions"><button type="submit">保存 Cookie</button></div>
      </form>
      <div class="help" id="cookieSummary">Cookie 状态读取中...</div>
      <form method="post" action="/cookie-test">
        <label>Cookie 测试推文 URL</label>
        <input name="test_url" placeholder="https://x.com/AromaGif/status/2058337180049911894">
        <div class="actions"><button class="secondary" type="submit">测试 Cookie / yt-dlp</button></div>
      </form>
    </section>
    <section>
      <h2>最近运行</h2>
      <table><thead><tr><th>ID</th><th>开始</th><th>状态</th><th>发现</th><th>下载</th><th>跳过</th><th>失败</th></tr></thead><tbody id="runsBody"></tbody></table>
    </section>
    <section>
      <h2>下载记录</h2>
      <table><thead><tr><th>Tweet</th><th>作者</th><th>类型</th><th>状态</th><th>文件</th><th>次数</th><th>错误</th></tr></thead><tbody id="tweetsBody"></tbody></table>
    </section>
    <section>
      <h2>日志</h2>
      <pre id="logBox"></pre>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    let filledForm = false;
    function phaseName(phase) {
      return {idle:"空闲", starting:"准备运行", collecting:"正在滚动采集 Likes", downloading:"正在下载媒体", manual_download:"手动单条下载", testing_cookie:"正在测试 Cookie", finished:"已完成", failed:"运行失败"}[phase] || phase || "未知";
    }
    function typeName(type) {
      return {image:"图片", video:"视频", gif:"GIF", mixed:"混合", unknown:"未知", manual_check:"需手动检查"}[type] || type || "未知";
    }
    function updateProgress(progress, local) {
      const total = Number(progress.download_total || 0);
      const done = Number(progress.download_done || 0);
      $("phaseText").textContent = `阶段：${phaseName(progress.phase)}；滚动：${progress.scroll || 0}；本次新增：${progress.new_on_last_scroll || 0}`;
      $("totalProgress").max = total > 0 ? total : 1;
      $("totalProgress").value = total > 0 ? done : 0;
      $("collectedMetric").textContent = progress.collected || 0;
      $("downloadMetric").textContent = `${done} / ${total}`;
      $("imageMetric").textContent = `${progress.images || 0} / ${progress.gifs || 0}`;
      $("videoMetric").textContent = progress.videos || 0;
      $("localMetric").textContent = `${local?.images || 0} 图 / ${local?.videos || 0} 视频`;
      $("currentUrl").textContent = progress.current_url ? `当前：${progress.current_url}` : "";
    }
    function updateTables(data) {
      $("runsBody").innerHTML = (data.runs || []).map((r) =>
        `<tr><td>${r.id}</td><td>${esc(r.started_at)}</td><td>${esc(r.status)}</td><td>${r.discovered}</td><td>${r.downloaded}</td><td>${r.skipped}</td><td>${r.failed}</td></tr>`
      ).join("");
      $("tweetsBody").innerHTML = (data.tweets || []).map((t) =>
        `<tr><td><a href="${esc(t.url)}" target="_blank">${esc(t.tweet_id)}</a></td><td>${esc(t.author)}</td><td>${esc(typeName(t.media_hint))}</td><td>${esc(t.status)}</td><td>${t.files_count || 0}</td><td>${esc(t.attempts)}</td><td>${esc((t.error || "").slice(0, 120))}</td></tr>`
      ).join("");
    }
    function updateCookieSummary(summary) {
      const ok = summary?.valid ? "格式有效" : "格式异常";
      const auth = summary?.has_auth_token ? "auth_token 有" : "auth_token 缺";
      const ct0 = summary?.has_ct0 ? "ct0 有" : "ct0 缺";
      const twid = summary?.has_twid ? `twid 有${summary.user_id ? " / " + summary.user_id : ""}` : "twid 缺";
      $("cookieSummary").textContent = `Cookie：${ok}；数量 ${summary?.count || 0}；${auth}；${ct0}；${twid}${summary?.error ? "；提示：" + summary.error : ""}`;
    }
    function fillFormOnce(data) {
      if (filledForm) return;
      const cfg = data.config || {};
      $("likesUrlInput").value = cfg.browser?.likes_url || "";
      $("stopUrlInput").value = cfg.stop_marker?.url || "";
      $("intervalHoursInput").value = data.run_interval_hours || cfg.run_interval_hours || 12;
      $("maxScrollsInput").value = cfg.browser?.max_scrolls || 0;
      $("knownStopInput").value = cfg.known_stop_consecutive || 10;
      filledForm = true;
    }
    async function refreshStatus() {
      try {
        const res = await fetch("/api/status", {cache: "no-store"});
        const data = await res.json();
        $("runningPill").textContent = `运行状态：${data.running ? "运行中" : "空闲"}`;
        $("cookiePill").textContent = `Cookie：${data.cookie_present ? "已保存" : "未保存"}`;
        $("scheduleText").textContent = `下一次自动运行：${data.next_run_at || "未排程"}；周期：${data.run_interval_hours || 12} 小时`;
        updateProgress(data.progress || {}, data.local_media || {});
        updateTables(data);
        updateCookieSummary(data.cookie_summary || {});
        const logBox = $("logBox");
        const shouldStick = Math.abs(logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight) < 40;
        logBox.textContent = (data.logs || []).join("\\n");
        if (shouldStick) logBox.scrollTop = logBox.scrollHeight;
        fillFormOnce(data);
      } catch (error) {
        $("scheduleText").textContent = `状态刷新失败：${error}`;
      }
    }
    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>""".replace("__APP_NAME__", APP_NAME)


def redirect(handler: BaseHTTPRequestHandler, location: str = "/") -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/status"):
                body = json.dumps(app.status(), ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = html_page(app).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            data = self.rfile.read(length).decode("utf-8", errors="replace")
            form = parse_qs(data)
            if self.path == "/run":
                app.start_run_thread()
                redirect(self)
                return
            if self.path == "/manual-download":
                manual_url = (form.get("manual_url") or [""])[0]
                app.start_manual_download_thread(manual_url)
                redirect(self)
                return
            if self.path == "/reload":
                app.reload_config()
                redirect(self)
                return
            if self.path == "/cookie":
                cookie_text = (form.get("cookie_text") or [""])[0]
                write_cookie_file(Path(app.config["cookie_file"]), cookie_text)
                summary = app.cookie_summary()
                app.log.write(
                    "已从网页端保存 Cookie："
                    f"count={summary.get('count')} auth_token={summary.get('has_auth_token')} "
                    f"ct0={summary.get('has_ct0')} twid={summary.get('has_twid')}"
                )
                if summary.get("error"):
                    app.log.write(f"Cookie 提示：{summary['error']}")
                redirect(self)
                return
            if self.path == "/cookie-test":
                test_url = (form.get("test_url") or [""])[0]
                app.start_cookie_test_thread(test_url)
                redirect(self)
                return
            if self.path == "/settings":
                hours_raw = (form.get("interval_hours") or ["12"])[0] or "12"
                try:
                    hours = max(0.1, float(hours_raw))
                except ValueError:
                    hours = 12.0
                patch = {
                    "run_interval_hours": hours,
                    "run_interval_seconds": int(hours * 3600),
                    "known_stop_consecutive": int((form.get("known_stop_consecutive") or ["10"])[0] or 10),
                    "stop_marker": {"url": (form.get("stop_url") or [""])[0]},
                    "browser": {
                        "likes_url": (form.get("likes_url") or [""])[0],
                        "max_scrolls": int((form.get("max_scrolls") or ["0"])[0] or 0),
                    },
                }
                app.save_config(patch)
                app.log.write("已从网页端保存设置")
                redirect(self)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def copy_example_config(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config)
    copy_example_config(config_path)
    app = App(config_path)

    if args.run_once:
        app.run_once_async()
        return 0

    scheduler = threading.Thread(target=app.scheduler_loop, daemon=True)
    scheduler.start()
    web_cfg = app.config.get("web", {})
    host = str(web_cfg.get("host", "0.0.0.0"))
    port = int(web_cfg.get("port", 8080))
    app.log.write(f"Web UI listening on {host}:{port}")
    server = ThreadingHTTPServer((host, port), make_handler(app))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop_event.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
