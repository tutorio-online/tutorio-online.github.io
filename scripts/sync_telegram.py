#!/usr/bin/env python3
"""Sync Telegram channel posts to static SEO pages.

Source: https://t.me/s/tutorio_channel
Outputs:
  - /posts/post-<ID>.html — individual SEO-friendly page per post
  - /blog.html — index of all posts
  - /sitemap.xml — updated sitemap
  - /index2.html — injects 3 latest post cards into #latest-posts section
  - /index3.html — injects 3 latest post cards (modern style) into #latest-posts section

Idempotent: re-running with no new posts is a no-op for post files (only
updates lastmod when content actually changes).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

CHANNEL_URL = "https://t.me/s/tutorio_channel"
SITE_ORIGIN = "https://tutorio.online"
CHANNEL_HANDLE = "tutorio_channel"

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_ROOT / "posts"
IMAGES_DIR = REPO_ROOT / "assets" / "images" / "posts"
BLOG_FILE = REPO_ROOT / "blog.html"
SITEMAP_FILE = REPO_ROOT / "sitemap.xml"
INDEX2_FILE = REPO_ROOT / "index2.html"
INDEX3_FILE = REPO_ROOT / "index3.html"
INDEX5_FILE = REPO_ROOT / "index5.html"

# Regex to extract URLs from Telegram's `background-image:url('...')` style
BG_IMG_RE = re.compile(r"background-image\s*:\s*url\(['\"]?([^'\")]+)['\"]?\)")

# Telegram CDN host where post images live
IMAGE_HOST = "cdn4.telesco.pe"

# Skip non-content messages (channel created, pinned photo only, etc.)
MIN_TEXT_LEN = 30
SKIP_EXACT = {"channel created"}
SKIP_SUBSTR = (
    "pinned a photo",
    "pinned a video",
    "pinned a document",
    "pinned an audio",
    "pinned a voice",
    "pinned a sticker",
    "pinned a gif",
    "pinned a poll",
    "pinned a file",
    "pinned a story",
)

HEAD_COMMON = """<!DOCTYPE html>
<html lang=\"ru\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    {seo_head}
    <link rel=\"shortcut icon\" type=\"image/png\" sizes=\"32x32\" href=\"https://tutorio.online/favicon.png?v=2\">
    <link rel=\"icon\" type=\"image/svg+xml\" href=\"https://tutorio.online/favicon.svg\">
    <link rel=\"apple-touch-icon\" sizes=\"120x120\" href=\"https://tutorio.online/favicon-120.png\">
    <link rel=\"sitemap\" type=\"application/xml\" title=\"Sitemap\" href=\"https://tutorio.online/sitemap.xml\">
    <link rel=\"canonical\" href=\"{canonical}\">
    <style>{css}</style>
</head>
<body>
"""

BODY_TAIL = """
<footer>
    <div class=\"social-links\">
        <a href=\"https://t.me/tutorio_channel\" target=\"_blank\" class=\"social-icon\">Telegram</a>
        <a href=\"https://instagram.com/lizza__vetta_?igsh=cHJibW85czQ4cnVp&utm_source=qr\" target=\"_blank\" class=\"social-icon\">Instagram</a>
    </div>
    <p style=\"margin-top: 20px;\">&copy; 2026 Репетитор английского языка. Увидимся на уроке!</p>
</footer>
</body>
</html>
"""

CSS = """
:root {
    --primary-color: #2a52be;
    --accent-color: #ff4b2b;
    --text-dark: #333;
    --bg-light: #f4f7f6;
    --white: #ffffff;
}
body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
       margin: 0; padding: 0; line-height: 1.6;
       color: var(--text-dark); background-color: var(--bg-light); }
.container { max-width: 800px; margin: 0 auto; padding: 0 20px; }
header { background: var(--white); padding: 30px 20px; text-align: center;
         box-shadow: 0 4px 15px rgba(0,0,0,0.05); border-radius: 0 0 40px 40px; }
header h1 { color: var(--primary-color); margin: 0 0 10px; font-size: 1.6em; }
header p { color: #666; margin: 0; }
.post-content { background: var(--white); margin: 40px auto; padding: 40px;
                border-radius: 30px; box-shadow: 0 4px 15px rgba(0,0,0,0.05);
                max-width: 800px; }
.post-content h2 { color: var(--primary-color); margin-top: 0; }
.post-content .meta { color: #888; font-size: 0.9em; margin-bottom: 25px;
                      text-transform: uppercase; letter-spacing: 0.5px; }
.post-content a { color: var(--primary-color); }
.post-content img { max-width: 100%; height: auto; border-radius: 18px; margin: 15px 0; }
.bot-cta { display: inline-block; color: var(--primary-color);
           border: 2px solid var(--primary-color); padding: 14px 32px;
           text-decoration: none; border-radius: 50px; font-weight: bold;
           transition: 0.3s; }
.bot-cta:hover { background: var(--primary-color); color: white; }
.social-links { margin-top: 15px; text-align: center; }
.social-icon { display: inline-block; margin: 0 10px; color: var(--primary-color);
               text-decoration: none; font-weight: bold; }
footer { text-align: center; padding: 40px 20px; color: #999; }
.back-link { display: inline-block; margin-bottom: 20px;
             color: var(--primary-color); text-decoration: none; font-weight: bold; }
.post-card { background: var(--white); padding: 25px; border-radius: 20px;
             box-shadow: 0 4px 15px rgba(0,0,0,0.05);
             border-bottom: 4px solid var(--primary-color); margin-bottom: 20px;
             transition: 0.3s; overflow: hidden; }
.post-card:hover { transform: translateY(-3px); box-shadow: 0 8px 25px rgba(0,0,0,0.08); }
.post-card .card-img-link { display: block; margin: -25px -25px 15px;
                            overflow: hidden; border-radius: 20px 20px 0 0; }
.post-card .card-img { display: block; width: 100%; height: 200px;
                       object-fit: cover; transition: 0.4s; }
.post-card:hover .card-img { transform: scale(1.04); }
.post-card .meta { color: #888; font-size: 0.85em; text-transform: uppercase;
                   margin-bottom: 10px; letter-spacing: 0.5px; }
.post-card .announce { color: #444; margin-bottom: 15px; line-height: 1.5; }
.post-card .actions { display: flex; gap: 10px; flex-wrap: wrap; }
.post-photos { margin: 0 0 25px; }
.post-photos img { display: block; width: 100%; height: auto;
                   border-radius: 18px; margin: 0 0 10px; }
.btn-primary { display: inline-block; background: var(--accent-color); color: white;
               padding: 10px 22px; border-radius: 50px; text-decoration: none;
               font-weight: bold; font-size: 0.95em; transition: 0.3s; }
.btn-primary:hover { background: #e63e1f; transform: translateY(-2px); }
.btn-secondary { display: inline-block; color: var(--primary-color);
                 border: 2px solid var(--primary-color); padding: 8px 20px;
                 border-radius: 50px; text-decoration: none; font-weight: bold;
                 font-size: 0.95em; transition: 0.3s; }
.btn-secondary:hover { background: var(--primary-color); color: white; }
.blog-list h1 { text-align: center; color: var(--primary-color); margin: 40px 0; }
#latest-posts h2 { color: var(--primary-color); text-align: center; margin: 40px 0 20px; }
#latest-posts .all-posts-link { text-align: center; margin-top: 30px; }
@media (max-width: 600px) {
    .post-content { padding: 25px; margin: 20px; border-radius: 20px; }
    header h1 { font-size: 1.3em; }
}
"""

# ─── Data ────────────────────────────────────────────────────────────────────


@dataclass
class Post:
    pid: str
    text: str          # plain text (for SEO excerpts)
    html: str          # inner HTML of tgme_widget_message_text
    date_iso: str      # ISO 8601 from <time datetime="">
    date_human: str    # "DD.MM.YYYY HH:MM" (Moscow)
    url: str           # https://t.me/tutorio_channel/<id>
    image_urls: list[str] = field(default_factory=list)  # all photos in post

    @property
    def image_url(self) -> str | None:
        """First image URL or None."""
        return self.image_urls[0] if self.image_urls else None

    @property
    def title(self) -> str:
        # first 50 chars, stripped of leading emoji for cleaner titles
        flat = re.sub(r"\s+", " ", self.text).strip()
        # strip leading emoji-like chars
        flat = re.sub(r"^[^\wА-Яа-я]+", "", flat)
        if not flat:
            flat = self.text.strip()
        return (flat[:50] + "…") if len(flat) > 50 else flat

    @property
    def description(self) -> str:
        # 155 chars max for meta description
        flat = re.sub(r"\s+", " ", self.text).strip()
        if len(flat) <= 155:
            return flat
        return flat[:152] + "…"

    @property
    def announce(self) -> str:
        # 100-150 chars for card preview
        flat = re.sub(r"\s+", " ", self.text).strip()
        target = 140
        if len(flat) <= target:
            return flat
        cut = flat[:target].rsplit(" ", 1)[0]
        return (cut or flat[:target]) + "…"


# ─── Scraping ────────────────────────────────────────────────────────────────


def fetch_channel(handle: str = CHANNEL_HANDLE) -> str:
    url = f"https://t.me/s/{handle}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def parse_posts(html: str) -> list[Post]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Post] = []
    for div in soup.find_all("div", class_="tgme_widget_message"):
        post_attr = div.get("data-post") or ""
        if "/" not in post_attr:
            continue
        pid = post_attr.split("/", 1)[1]
        if not pid.isdigit():
            continue

        text_div = div.find("div", class_="tgme_widget_message_text")
        # Telegram sometimes has multiple <time> tags (e.g. video duration + publish
        # time). Only the publish-time <time> has a `datetime` attribute.
        time_tag = div.find("time", attrs={"datetime": True})
        if not time_tag or not text_div:
            continue

        # decode inner HTML, then strip empty/whitespace-only
        raw_html = text_div.decode_contents()
        flat = text_div.get_text(separator=" ", strip=True)

        low = flat.lower().strip()
        if any(s in low for s in SKIP_SUBSTR):
            continue
        if low in SKIP_EXACT:
            continue
        if len(flat) < MIN_TEXT_LEN:
            continue

        date_iso = time_tag.get("datetime", "")
        try:
            dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
            date_human = dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            date_human = date_iso

        # Extract all photo URLs (single or grouped). Telegram stores them as
        # `background-image:url('...')` on `<a class="tgme_widget_message_photo_wrap">`.
        image_urls: list[str] = []
        for a in div.find_all("a", class_="tgme_widget_message_photo_wrap"):
            style = a.get("style", "")
            m = BG_IMG_RE.search(style)
            if m and m.group(1) not in image_urls:
                image_urls.append(m.group(1))

        out.append(
            Post(
                pid=pid,
                text=flat,
                html=raw_html,
                date_iso=date_iso,
                date_human=date_human,
                url=f"https://t.me/{CHANNEL_HANDLE}/{pid}",
                image_urls=image_urls,
            )
        )

    # Sort strictly newest-first by date, then by pid as tie-breaker.
    # Telegram serves newest first, but be defensive.
    def _sort_key(p: Post) -> tuple[str, int]:
        return (p.date_iso, int(p.pid))

    out.sort(key=_sort_key, reverse=True)
    return out


# ─── Image download ────────────────────────────────────────────────────────────


def download_image(url: str, dest: Path) -> bool:
    """Download a Telegram CDN image to disk. Returns True if newly written.

    Skips if file already exists (idempotent). On failure, logs and returns False
    so the script keeps going without the local copy (we fall back to the CDN URL).
    """
    if dest.exists() and dest.stat().st_size > 0:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Referer": "https://t.me/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=30, stream=True)
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"[warn] failed to download {url}: {e}", file=sys.stderr)
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return False


def local_image_path(post: Post) -> Path:
    """Local filesystem path for the post's first image (post-<pid>.jpg)."""
    return IMAGES_DIR / f"post-{post.pid}.jpg"


def web_image_path(post: Post, exists_locally: bool) -> str:
    """Web URL to embed in HTML. Local path if downloaded, else CDN URL."""
    if exists_locally:
        return f"/assets/images/posts/post-{post.pid}.jpg"
    return post.image_url or ""


# ─── HTML helpers ────────────────────────────────────────────────────────────


def seo_head(post: Post) -> str:
    title_esc = post.title.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    desc_esc = post.description.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    url = f"{SITE_ORIGIN}/posts/post-{post.pid}.html"
    image = f"{SITE_ORIGIN}/og-image.png"
    return (
        f"<title>{title_esc} | Tutorio</title>\n"
        f"    <meta name=\"description\" content=\"{desc_esc}\">\n"
        f"    <meta property=\"og:type\" content=\"article\">\n"
        f"    <meta property=\"og:url\" content=\"{url}\">\n"
        f"    <meta property=\"og:title\" content=\"{title_esc}\">\n"
        f"    <meta property=\"og:description\" content=\"{desc_esc}\">\n"
        f"    <meta property=\"og:image\" content=\"{image}\">\n"
        f"    <meta property=\"og:locale\" content=\"ru_RU\">\n"
        f"    <meta name=\"twitter:card\" content=\"summary_large_image\">\n"
        f"    <meta name=\"twitter:title\" content=\"{title_esc}\">\n"
        f"    <meta name=\"twitter:description\" content=\"{desc_esc}\">\n"
        f"    <meta name=\"twitter:image\" content=\"{image}\">"
    )


def render_post_page(post: Post, local_exists: dict[str, bool]) -> str:
    canonical = f"{SITE_ORIGIN}/posts/post-{post.pid}.html"
    seo = seo_head(post)
    # All photos for the post (carousel). If first image is local, use that path
    # for all (we only download the first one to keep things simple).
    photos_html = ""
    if post.image_urls:
        first_local = local_exists.get(post.pid, False)
        if first_local:
            src = f"/assets/images/posts/post-{post.pid}.jpg"
            photos_html = (
                f"<div class=\"post-photos\"><img src=\"{src}\" alt=\"{post.title}\" loading=\"lazy\"></div>"
            )
        else:
            imgs = "".join(
                f"<img src=\"{u}\" alt=\"{post.title}\" loading=\"lazy\">"
                for u in post.image_urls
            )
            photos_html = f"<div class=\"post-photos\">{imgs}</div>"

    body = (
        f"<header><div class=\"container\">"
        f"<h1>{post.title}</h1>"
        f"<p><a class=\"back-link\" href=\"{SITE_ORIGIN}/blog.html\">&larr; Все публикации</a></p>"
        f"</div></header>"
        f"<article class=\"post-content\">"
        f"<div class=\"meta\">{post.date_human}</div>"
        f"{photos_html}"
        f"<div class=\"body\">{post.html}</div>"
        f"<p style=\"margin-top: 30px;\">"
        f"<a href=\"{post.url}\" target=\"_blank\" class=\"bot-cta\">Открыть в Telegram &rarr;</a>"
        f"</p>"
        f"</article>"
    )
    return HEAD_COMMON.format(seo_head=seo, canonical=canonical, css=CSS) + body + BODY_TAIL


def render_blog_page(posts: list[Post], local_exists: dict[str, bool]) -> str:
    seo = (
        "<title>Блог Tutorio — публикации из Telegram</title>\n"
        "    <meta name=\"description\" content=\"Полезные материалы об английском: разборы фраз, идиом, грамматики и практические советы от преподавателя Tutorio.\">\n"
        "    <meta property=\"og:type\" content=\"website\">\n"
        f"    <meta property=\"og:url\" content=\"{SITE_ORIGIN}/blog.html\">\n"
        "    <meta property=\"og:title\" content=\"Блог Tutorio — публикации из Telegram\">\n"
        "    <meta property=\"og:description\" content=\"Полезные материалы об английском от преподавателя Tutorio.\">\n"
        f"    <meta property=\"og:image\" content=\"{SITE_ORIGIN}/og-image.png\">\n"
        "    <meta property=\"og:locale\" content=\"ru_RU\">"
    )
    canonical = f"{SITE_ORIGIN}/blog.html"

    cards: list[str] = []
    for p in posts:
        cards.append(_render_card(p, local_exists, full_card=False))

    body = (
        "<header><div class=\"container\">"
        "<h1>Блог Tutorio</h1>"
        "<p>Полезные материалы об английском из нашего Telegram-канала</p>"
        f"<p><a class=\"back-link\" href=\"{SITE_ORIGIN}/index2.html\">&larr; На главную</a></p>"
        "</div></header>"
        "<div class=\"container blog-list\">"
        + "".join(cards) +
        "</div>"
    )
    return HEAD_COMMON.format(seo_head=seo, canonical=canonical, css=CSS) + body + BODY_TAIL


def _render_card(p: Post, local_exists: dict[str, bool], full_card: bool) -> str:
    """Render a single post card. Used in blog.html and index2.html."""
    announce = p.announce.replace("&", "&amp;").replace("<", "&lt;")
    img_html = ""
    if p.image_url:
        first_local = local_exists.get(p.pid, False)
        src = (
            f"/assets/images/posts/post-{p.pid}.jpg"
            if first_local
            else p.image_url
        )
        img_html = (
            f"<a class=\"card-img-link\" href=\"/posts/post-{p.pid}.html\">"
            f"<img class=\"card-img\" src=\"{src}\" alt=\"{p.title}\" loading=\"lazy\">"
            f"</a>"
        )
    return (
        f"<div class=\"post-card\">"
        f"{img_html}"
        f"<div class=\"meta\">{p.date_human}</div>"
        f"<div class=\"announce\">{announce}</div>"
        f"<div class=\"actions\">"
        f"<a class=\"btn-primary\" href=\"/posts/post-{p.pid}.html\">Читать статью</a>"
        f"<a class=\"btn-secondary\" href=\"{p.url}\" target=\"_blank\">Открыть в Telegram</a>"
        f"</div>"
        f"</div>"
    )


def render_post_card(p: Post, local_exists: dict[str, bool]) -> str:
    return _render_card(p, local_exists, full_card=False)


def render_modern_post_card(p: Post, local_exists: dict[str, bool]) -> str:
    """Modern-style card for index3.html.

    Layout: rounded image on top (border-radius 12px), then a small date chip,
    a short title, the announcement and a 'Read' link button with arrow icon.
    Falls back to a neutral placeholder block if the post has no image.
    """
    announce = p.announce.replace("&", "&amp;").replace("<", "&lt;")
    title = p.title.replace("&", "&amp;").replace("<", "&lt;")

    if p.image_url:
        first_local = local_exists.get(p.pid, False)
        src = (
            f"/assets/images/posts/post-{p.pid}.jpg"
            if first_local
            else p.image_url
        )
        img_html = (
            f"<a class=\"card-img-link\" href=\"/posts/post-{p.pid}.html\">"
            f"<img class=\"card-img\" src=\"{src}\" alt=\"{title}\" loading=\"lazy\">"
            f"</a>"
        )
    else:
        # Neutral placeholder block (still rounded, keeps grid alignment)
        img_html = (
            "<a class=\"card-img-link card-img-placeholder\" "
            f"href=\"/posts/post-{p.pid}.html\" aria-label=\"{title}\">"
            "<i data-lucide=\"file-text\"></i>"
            "</a>"
        )

    return (
        "<article class=\"post-card-modern\">"
        f"{img_html}"
        "<div class=\"card-body\">"
        f"<span class=\"date-chip\"><span class=\"dot\"></span>{p.date_human}</span>"
        f"<h3 class=\"card-title\">{title}</h3>"
        f"<p class=\"announce\">{announce}</p>"
        f"<a class=\"btn-read\" href=\"/posts/post-{p.pid}.html\">"
        "Читать <i data-lucide=\"arrow-right\"></i>"
        "</a>"
        "</div>"
        "</article>"
    )


def update_index_modern(target_file: Path, posts: list[Post], local_exists: dict[str, bool]) -> bool:
    """Replace content between <!-- BEGIN POSTS-MODERN --> and <!-- END POSTS-MODERN -->
    in target_file (e.g. index3.html, index5.html) with 3 modern-style cards
    (newest posts).

    Returns True if the file was modified.
    """
    if not target_file.exists():
        print(f"[skip] {target_file} not found", file=sys.stderr)
        return False

    text = target_file.read_text(encoding="utf-8")
    begin = "<!-- BEGIN POSTS-MODERN -->"
    end = "<!-- END POSTS-MODERN -->"
    if begin not in text or end not in text:
        print(
            f"[warn] markers not found in {target_file.name}: "
            "expected <!-- BEGIN POSTS-MODERN --> ... <!-- END POSTS-MODERN -->",
            file=sys.stderr,
        )
        return False

    latest = posts[:3]
    if not latest:
        print(f"[warn] no posts to inject into {target_file.name}", file=sys.stderr)
        return False
    inner = "\n".join(render_modern_post_card(p, local_exists) for p in latest)
    new_block = f"{begin}\n{inner}\n{end}"

    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    new_text, n = pattern.subn(new_block, text, count=1)
    if n == 0 or new_text == text:
        return False

    target_file.write_text(new_text, encoding="utf-8")
    return True


# Backward-compatible alias
def update_index3(posts: list[Post], local_exists: dict[str, bool]) -> bool:
    return update_index_modern(INDEX3_FILE, posts, local_exists)


def update_index2(posts: list[Post], local_exists: dict[str, bool]) -> bool:
    """Replace content between <!-- BEGIN POSTS --> and <!-- END POSTS --> in index2.html.

    Posts are expected to already be sorted newest-first. We render the top 3.
    Returns True if the file was modified.
    """
    if not INDEX2_FILE.exists():
        print(f"[skip] {INDEX2_FILE} not found", file=sys.stderr)
        return False

    text = INDEX2_FILE.read_text(encoding="utf-8")
    begin = "<!-- BEGIN POSTS -->"
    end = "<!-- END POSTS -->"
    if begin not in text or end not in text:
        print(
            f"[warn] markers not found in {INDEX2_FILE.name}: "
            "expected <!-- BEGIN POSTS --> ... <!-- END POSTS -->",
            file=sys.stderr,
        )
        return False

    latest = posts[:3]
    if not latest:
        print("[warn] no posts to inject", file=sys.stderr)
        return False
    inner = "\n".join(render_post_card(p, local_exists) for p in latest)
    all_link = (
        "\n<div class=\"all-posts-link\">"
        f"<a class=\"btn-secondary\" href=\"/blog.html\">Все публикации &rarr;</a>"
        "</div>\n"
    )
    new_block = f"{begin}\n{inner}{all_link}{end}"

    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    new_text, n = pattern.subn(new_block, text, count=1)
    if n == 0:
        return False
    if new_text == text:
        return False

    INDEX2_FILE.write_text(new_text, encoding="utf-8")
    return True


# ─── Sitemap ─────────────────────────────────────────────────────────────────


def update_sitemap(posts: list[Post]) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    def url_entry(loc: str, priority: str, changefreq: str) -> str:
        return (
            "  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{today}</lastmod>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )

    lines.append(url_entry(f"{SITE_ORIGIN}/", "1.0", "weekly"))
    lines.append(url_entry(f"{SITE_ORIGIN}/index1.html", "0.9", "weekly"))
    lines.append(url_entry(f"{SITE_ORIGIN}/index2.html", "0.9", "weekly"))
    lines.append(url_entry(f"{SITE_ORIGIN}/index3.html", "0.9", "weekly"))
    lines.append(url_entry(f"{SITE_ORIGIN}/index5.html", "0.9", "weekly"))
    lines.append(url_entry(f"{SITE_ORIGIN}/blog.html", "0.9", "daily"))
    for p in posts:
        lines.append(url_entry(f"{SITE_ORIGIN}/posts/post-{p.pid}.html", "0.7", "monthly"))

    lines.append("</urlset>")
    new = "\n".join(lines) + "\n"

    if SITEMAP_FILE.exists() and SITEMAP_FILE.read_text(encoding="utf-8") == new:
        return False
    SITEMAP_FILE.write_text(new, encoding="utf-8")
    return True


# ─── Main ────────────────────────────────────────────────────────────────────


def write_post(post: Post, local_exists: dict[str, bool]) -> bool:
    path = POSTS_DIR / f"post-{post.pid}.html"
    content = render_post_page(post, local_exists)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[fetch] {CHANNEL_URL}")
    try:
        html = fetch_channel()
    except Exception as e:
        print(f"[error] failed to fetch channel: {e}", file=sys.stderr)
        return 1

    posts = parse_posts(html)
    print(f"[parse] {len(posts)} posts kept (newest first: {posts[0].pid if posts else 'n/a'})")

    # Download first image per post (best-effort).
    downloaded: list[str] = []
    for p in posts:
        if p.image_url:
            dest = local_image_path(p)
            if download_image(p.image_url, dest):
                downloaded.append(dest.name)

    # Build local-exists dict by re-checking disk after downloads.
    local_exists: dict[str, bool] = {
        p.pid: local_image_path(p).exists() for p in posts
    }
    print(
        f"[images] downloaded={len(downloaded)} "
        f"local_available={sum(local_exists.values())}/{len(posts)}"
    )

    written_posts: list[str] = []
    for p in posts:
        if write_post(p, local_exists):
            written_posts.append(p.pid)

    blog_html = render_blog_page(posts, local_exists)
    blog_changed = False
    if not BLOG_FILE.exists() or BLOG_FILE.read_text(encoding="utf-8") != blog_html:
        BLOG_FILE.write_text(blog_html, encoding="utf-8")
        blog_changed = True

    index2_changed = update_index2(posts, local_exists)
    index3_changed = update_index_modern(INDEX3_FILE, posts, local_exists)
    index5_changed = update_index_modern(INDEX5_FILE, posts, local_exists)
    sitemap_changed = update_sitemap(posts)

    print(
        f"[summary] posts_written={len(written_posts)} "
        f"blog_changed={blog_changed} "
        f"index2_changed={index2_changed} "
        f"index3_changed={index3_changed} "
        f"index5_changed={index5_changed} "
        f"sitemap_changed={sitemap_changed}"
    )

    # Exit code: 0 even if nothing changed (CI uses this to decide whether
    # to commit, but `git diff` will be empty in that case)
    return 0


if __name__ == "__main__":
    sys.exit(main())
