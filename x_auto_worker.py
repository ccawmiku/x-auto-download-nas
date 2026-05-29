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


DEFAULT_CONFIG: dict[str, Any] = {
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
    },
    "media": {
        "video_format": "bv*+ba/b",
        "convert_gif": False,
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
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else ""


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
        media_hint = "video" if item.get("has_video") else "image" if item.get("media_ids") else "unknown"
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                insert into tweets(tweet_id, url, author, text, media_hint, first_seen_at, updated_at)
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(tweet_id) do update set
                    url=excluded.url,
                    author=coalesce(excluded.author, tweets.author),
                    text=coalesce(excluded.text, tweets.text),
                    media_hint=excluded.media_hint,
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

    def should_download(self, tweet_id: str, retry_failed: bool, max_attempts: int) -> bool:
        row = self.get_tweet(tweet_id)
        if not row:
            return True
        if row["status"] == "done":
            return False
        if row["status"] == "failed":
            if not retry_failed:
                return False
            if max_attempts > 0 and int(row["attempts"]) >= max_attempts:
                return False
        return True

    def mark_result(self, tweet_id: str, status: str, files: list[str], error: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                update tweets
                set status=?, attempts=attempts+1, files_json=?, error=?,
                    downloaded_at=case when ?='done' then ? else downloaded_at end,
                    updated_at=?
                where tweet_id=?
                """,
                (
                    status,
                    json.dumps(files, ensure_ascii=False),
                    error[-2000:],
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
            return [dict(row) for row in rows]

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from runs order by id desc limit ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return deep_merge(DEFAULT_CONFIG, json.loads(path.read_text(encoding="utf-8")))
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


@dataclass
class BrowserResult:
    screen_name: str
    likes_url: str
    tweets: list[dict[str, Any]]
    stop_found: bool
    screenshot: str


class BrowserCollector:
    def __init__(self, config: dict[str, Any], log: RingLog):
        self.config = config
        self.log = log

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
                rows.push({
                  tweet_id: match[2],
                  author: match[1],
                  url: `https://x.com/${match[1]}/status/${match[2]}`,
                  text: article.innerText.slice(0, 1000),
                  media_ids: mediaIds,
                  has_video: !!article.querySelector('video')
                });
              }
              return rows;
            }"""
        )

    async def collect(self) -> BrowserResult:
        cookie_file = Path(self.config["cookie_file"])
        if not cookie_file.exists():
            raise RuntimeError(f"cookie file not found: {cookie_file}")
        cookies, user_id = parse_netscape_cookie_file(cookie_file)
        if not cookies:
            raise RuntimeError("cookie file is empty or invalid")

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

        seen: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        stop_found = False

        async with async_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "headless": bool(browser_cfg.get("headless", True)),
                "args": ["--disable-blink-features=AutomationControlled", "--lang=zh-CN"],
            }
            if exe := self._browser_executable():
                launch_kwargs["executable_path"] = exe
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 900},
                user_agent=self.config.get("default_user_agent"),
            )
            await context.add_cookies(cookies)
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

                    new_count = len(seen) - before
                    if scroll % 5 == 0 or new_count:
                        self.log.write(f"Likes scroll {scroll}: total={len(seen)}, new={new_count}")
                    if stop_found:
                        self.log.write(f"Stop marker found: {stop_id}")
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

                screenshot = str(out_dir / "last_likes_page.png")
                await page.screenshot(path=screenshot, full_page=True)
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

    def _tweet_dir(self, item: dict[str, Any]) -> Path:
        safe_author = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.get("author") or "unknown")
        path = Path(self.config["download_dir"]) / safe_author / item["tweet_id"]
        path.mkdir(parents=True, exist_ok=True)
        return path

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

    def download_images(self, item: dict[str, Any]) -> list[str]:
        media_ids = item.get("media_ids") or []
        files = []
        if not media_ids:
            return files
        out_dir = self._tweet_dir(item)
        for index, media_id in enumerate(media_ids, start=1):
            done = False
            for candidate, ext in self._image_candidates(media_id):
                target = out_dir / f"{item['tweet_id']}_{index}_{media_id}.{ext}"
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
        out_dir = self._tweet_dir(item)
        before = {p.resolve() for p in out_dir.glob("*")}
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
            str(out_dir / "%(uploader_id)s_%(id)s.%(ext)s"),
            item["url"],
        ]
        self.log.write(f"yt-dlp: {item['url']}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout)[-2000:])
        after = {p.resolve() for p in out_dir.glob("*")}
        files = [str(p) for p in sorted(after - before) if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".jpg", ".png", ".json"}]
        if not files:
            files = [str(p) for p in sorted(out_dir.glob("*")) if p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
        return files

    def download_item(self, item: dict[str, Any]) -> tuple[str, list[str], str]:
        tweet_id = item["tweet_id"]
        self.store.upsert_seen(item)
        retry_failed = bool(self.config.get("retry_failed", True))
        max_attempts = int(self.config.get("max_download_attempts", 0) or 0)
        if not self.store.should_download(tweet_id, retry_failed, max_attempts):
            return "skipped", [], ""
        try:
            files = []
            files.extend(self.download_images(item))
            files.extend(self.download_video(item))
            if not files:
                files.extend(self.download_video({**item, "has_video": True}))
            status = "done" if files else "failed"
            error = "" if files else "no downloadable media found"
            self.store.mark_result(tweet_id, status, files, error)
            return status, files, error
        except Exception as error:
            self.store.mark_result(tweet_id, "failed", [], str(error))
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

    def reload_config(self) -> None:
        self.config = load_config(self.config_path)
        self.log.max_lines = int(self.config.get("web", {}).get("log_lines", 300))

    def save_config(self, patch: dict[str, Any]) -> None:
        self.config = deep_merge(self.config, patch)
        save_config(self.config_path, self.config)

    def run_once_async(self) -> dict[str, int]:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("a run is already active")
        self.running = True
        run_id = self.store.begin_run()
        stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        message = ""
        try:
            self.reload_config()
            self.log.write("Run started")
            collector = BrowserCollector(self.config, self.log)
            result = asyncio.run(collector.collect())
            stats["discovered"] = len(result.tweets)
            self.log.write(
                f"Collected {len(result.tweets)} liked tweet(s); stop_found={result.stop_found}"
            )
            downloader = Downloader(self.config, self.store, self.log)
            delay = float(self.config.get("request_delay_seconds", 3))
            jitter = float(self.config.get("jitter_seconds", 2))
            for index, item in enumerate(result.tweets, start=1):
                status, files, error = downloader.download_item(item)
                if status == "done":
                    stats["downloaded"] += 1
                elif status == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1
                    self.log.write(f"Failed {item['url']}: {error}")
                self.log.write(f"Progress {index}/{len(result.tweets)}: {status} {item['url']}")
                sleep_for = delay + random.random() * jitter
                if sleep_for > 0 and index < len(result.tweets):
                    time.sleep(sleep_for)
            message = "ok"
            self.store.finish_run(run_id, "done", stats, message)
            self.log.write(f"Run finished: {stats}")
            return stats
        except Exception as error:
            message = str(error)
            self.log.write(f"Run failed: {message}")
            self.log.write(traceback.format_exc())
            self.store.finish_run(run_id, "failed", stats, message)
            raise
        finally:
            self.last_run_message = message
            self.running = False
            self.run_lock.release()

    def start_run_thread(self) -> None:
        def target() -> None:
            try:
                self.run_once_async()
            except Exception:
                pass

        threading.Thread(target=target, daemon=True).start()

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            self.reload_config()
            interval = int(self.config.get("run_interval_seconds", 43200))
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
            "config": self.config,
            "runs": self.store.recent_runs(),
            "tweets": self.store.recent_tweets(),
            "logs": self.log.lines(),
            "last_run_message": self.last_run_message,
        }


def html_page(app: App) -> str:
    status = app.status()
    cfg = status["config"]
    logs = "\n".join(html.escape(line) for line in status["logs"])
    runs = status["runs"]
    tweets = status["tweets"]
    run_rows = "".join(
        f"<tr><td>{r['id']}</td><td>{html.escape(r['started_at'])}</td><td>{html.escape(str(r['status']))}</td>"
        f"<td>{r['discovered']}</td><td>{r['downloaded']}</td><td>{r['skipped']}</td><td>{r['failed']}</td></tr>"
        for r in runs
    )
    tweet_rows = "".join(
        f"<tr><td><a href='{html.escape(t['url'])}' target='_blank'>{html.escape(t['tweet_id'])}</a></td>"
        f"<td>{html.escape(t.get('author') or '')}</td><td>{html.escape(t.get('media_hint') or '')}</td>"
        f"<td>{html.escape(t.get('status') or '')}</td><td>{t.get('attempts')}</td>"
        f"<td>{html.escape((t.get('error') or '')[:120])}</td></tr>"
        for t in tweets
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_NAME}</title>
  <style>
    :root {{ color-scheme: light; --bg:#f6f7f9; --panel:#fff; --line:#d9dde5; --text:#1d2433; --muted:#657084; --accent:#111827; }}
    body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }}
    header {{ height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 24px; background:#111827; color:white; }}
    main {{ max-width:1180px; margin:0 auto; padding:20px; display:grid; gap:16px; }}
    section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }}
    h1 {{ font-size:18px; margin:0; }} h2 {{ font-size:16px; margin:0 0 12px; }}
    label {{ display:block; color:var(--muted); font-size:13px; margin:10px 0 5px; }}
    input, textarea {{ width:100%; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; padding:9px 10px; font:inherit; background:white; }}
    textarea {{ min-height:110px; resize:vertical; }}
    button {{ border:0; background:var(--accent); color:white; border-radius:6px; padding:9px 14px; cursor:pointer; }}
    button.secondary {{ background:#475569; }} button.danger {{ background:#b91c1c; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
    .pill {{ display:inline-flex; align-items:center; padding:3px 8px; border-radius:999px; background:#e6edf6; font-size:12px; color:#334155; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }} th,td {{ border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }}
    pre {{ margin:0; background:#0f172a; color:#dbeafe; padding:12px; border-radius:6px; overflow:auto; max-height:360px; }}
    .muted {{ color:var(--muted); }} .status {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    @media (max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} header {{ padding:0 14px; }} main {{ padding:12px; }} }}
  </style>
</head>
<body>
  <header><h1>X Auto Downloader</h1><div class="status"><span class="pill">Running: {status['running']}</span><span class="pill">Cookie: {status['cookie_present']}</span></div></header>
  <main>
    <section>
      <h2>控制</h2>
      <div class="muted">下一次自动运行：{html.escape(status['next_run_at']) or '未排程'}；周期：{cfg.get('run_interval_seconds')} 秒</div>
      <div class="actions">
        <form method="post" action="/run"><button type="submit">立即运行</button></form>
        <form method="post" action="/reload"><button class="secondary" type="submit">重新读取配置</button></form>
      </div>
    </section>
    <section>
      <h2>配置</h2>
      <form method="post" action="/settings">
        <div class="grid">
          <div><label>Likes 页面 URL（留空自动用当前账号）</label><input name="likes_url" value="{html.escape(str(cfg['browser'].get('likes_url','')))}"></div>
          <div><label>停止标记 URL</label><input name="stop_url" value="{html.escape(str(cfg['stop_marker'].get('url','')))}"></div>
          <div><label>运行间隔（秒）</label><input name="interval" value="{html.escape(str(cfg.get('run_interval_seconds')))}"></div>
          <div><label>最大滚动次数（0 表示直到标记或页面无新增）</label><input name="max_scrolls" value="{html.escape(str(cfg['browser'].get('max_scrolls',0)))}"></div>
        </div>
        <div class="actions"><button type="submit">保存配置</button></div>
      </form>
    </section>
    <section>
      <h2>Cookie</h2>
      <form method="post" action="/cookie">
        <label>粘贴 cookies.txt 内容；也支持一行 Cookie header</label>
        <textarea name="cookie_text" placeholder="# Netscape HTTP Cookie File..."></textarea>
        <div class="actions"><button type="submit">保存 Cookie</button></div>
      </form>
    </section>
    <section>
      <h2>最近运行</h2>
      <table><thead><tr><th>ID</th><th>开始</th><th>状态</th><th>发现</th><th>下载</th><th>跳过</th><th>失败</th></tr></thead><tbody>{run_rows}</tbody></table>
    </section>
    <section>
      <h2>下载记录</h2>
      <table><thead><tr><th>Tweet</th><th>作者</th><th>类型</th><th>状态</th><th>次数</th><th>错误</th></tr></thead><tbody>{tweet_rows}</tbody></table>
    </section>
    <section>
      <h2>日志</h2>
      <pre>{logs}</pre>
    </section>
  </main>
</body>
</html>"""


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
            if self.path == "/reload":
                app.reload_config()
                redirect(self)
                return
            if self.path == "/cookie":
                cookie_text = (form.get("cookie_text") or [""])[0]
                write_cookie_file(Path(app.config["cookie_file"]), cookie_text)
                app.log.write("Cookie saved from web UI")
                redirect(self)
                return
            if self.path == "/settings":
                patch = {
                    "run_interval_seconds": int((form.get("interval") or ["43200"])[0] or 43200),
                    "stop_marker": {"url": (form.get("stop_url") or [""])[0]},
                    "browser": {
                        "likes_url": (form.get("likes_url") or [""])[0],
                        "max_scrolls": int((form.get("max_scrolls") or ["0"])[0] or 0),
                    },
                }
                app.save_config(patch)
                app.log.write("Settings saved from web UI")
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
