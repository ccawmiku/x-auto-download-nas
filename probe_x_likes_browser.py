import argparse
import asyncio
import json
import random
import re
import sys
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


DEFAULT_COOKIE_FILE = Path("config/cookies/x_cookies.txt")
DEFAULT_OUT_DIR = Path("downloads/probe/browser_likes")
CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]


def load_netscape_cookies(path: Path) -> tuple[list[dict], str | None]:
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = []
    user_id = None
    now = int(time.time())
    for item in jar:
        if item.name == "twid":
            user_id = item.value.removeprefix("u%3D")
        domain = item.domain or ".x.com"
        if "twitter.com" in domain:
            domain = domain.replace("twitter.com", "x.com")
        cookie = {
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


def find_browser_executable() -> str | None:
    for path in CHROME_PATHS:
        if path.exists():
            return str(path)
    return None


async def collect_status_links(page) -> list[dict]:
    return await page.evaluate(
        """() => {
          const rows = [];
          const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="/status/"]')) {
            const href = new URL(a.getAttribute('href'), location.href).href;
            const match = href.match(/https:\\/\\/x\\.com\\/([^/]+)\\/status\\/(\\d+)/);
            if (!match || seen.has(match[2])) continue;
            seen.add(match[2]);
            const article = a.closest('article');
            rows.push({
              tweet_id: match[2],
              author: match[1],
              url: `https://x.com/${match[1]}/status/${match[2]}`,
              text: article ? article.innerText.slice(0, 500) : "",
              has_img: article ? !!article.querySelector('img[src*="twimg.com/media"]') : false,
              has_video: article ? !!article.querySelector('video') : false
            });
          }
          return rows;
        }"""
    )


async def profile_screen_name(page, user_id: str) -> str | None:
    await page.goto(f"https://x.com/i/user/{user_id}", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(random.randint(2500, 4200))
    current = page.url
    match = re.match(r"https://x\.com/([^/?#]+)", current)
    if match and match.group(1) not in {"i", "home", "login"}:
        return match.group(1)
    links = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href^="/"]'))
          .map(a => a.getAttribute('href'))
          .filter(Boolean)
          .slice(0, 200)"""
    )
    for href in links:
        match = re.match(r"/([^/?#]+)/?$", href)
        if match and match.group(1) not in {"home", "explore", "notifications", "messages"}:
            return match.group(1)
    return None


async def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookie-file", default=str(DEFAULT_COOKIE_FILE))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--scrolls", type=int, default=6)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--url", default="")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies, user_id = load_netscape_cookies(Path(args.cookie_file))
    browser_exe = find_browser_executable()
    if not user_id and not args.url:
        raise RuntimeError("twid cookie not found and --url not provided")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=browser_exe,
            headless=not args.headful,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
            ],
        )
        context = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        if args.url:
            likes_url = args.url
            screen_name = urlparse(args.url).path.strip("/").split("/")[0]
        else:
            screen_name = await profile_screen_name(page, user_id)
            if not screen_name:
                raise RuntimeError("could not resolve current X screen name")
            likes_url = f"https://x.com/{screen_name}/likes"

        await page.goto(likes_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(random.randint(3500, 5200))

        snapshots = []
        seen_rows = {}
        for idx in range(max(0, args.scrolls)):
            rows = await collect_status_links(page)
            for row in rows:
                seen_rows[row["tweet_id"]] = row
            snapshots.append({"step": idx, "count": len(rows)})
            await page.mouse.wheel(0, random.randint(520, 1120))
            await page.wait_for_timeout(random.randint(1300, 2600))

        rows = await collect_status_links(page)
        for row in rows:
            seen_rows[row["tweet_id"]] = row
        rows = list(seen_rows.values())
        result = {
            "mode": "browser_dom_only",
            "screen_name": screen_name,
            "likes_url": likes_url,
            "final_url": page.url,
            "tweet_count": len(rows),
            "scroll_snapshots": snapshots,
            "tweets": rows,
        }
        (out_dir / "likes_dom_links.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await page.screenshot(path=str(out_dir / "likes_page.png"), full_page=True)
        await context.close()
        await browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
