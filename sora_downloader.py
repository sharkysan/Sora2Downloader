#!/usr/bin/env python3
"""Capture and download media URLs from Sora profile/drafts pages.

This tool is intended for downloading content you are authorized to access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from playwright.sync_api import Error, Request, Response, TimeoutError, sync_playwright


DEFAULT_TARGETS = [
    "https://sora.chatgpt.com/profile",
    "https://sora.chatgpt.com/drafts",
]

VIDEO_URL_RE = re.compile(r"\.(mp4|mov|webm|m3u8)(?:\?|$)", re.IGNORECASE)
DEFAULT_LOGIN_TIMEOUT_MS = 60_000
DEFAULT_GOTO_WAIT_UNTIL: Literal[
    "commit", "domcontentloaded", "load", "networkidle"
] = "domcontentloaded"
PAGINATION_SELECTORS = [
    "button:has-text('Load more')",
    "button:has-text('Show more')",
    "button:has-text('More')",
    "button:has-text('Next')",
    "a:has-text('Next')",
    "[aria-label*='Load more' i]",
    "[aria-label*='Show more' i]",
    "[aria-label*='Next' i]",
]


@dataclass
class MediaHit:
    url: str
    source_page: str
    discovered_at: str
    media_key: str | None = None
    content_type: str | None = None
    status: int | None = None
    detail_page: str | None = None


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def is_media_response(response: Response) -> bool:
    url = response.url
    content_type = (response.headers or {}).get("content-type", "").lower()

    if "video/" in content_type:
        return True
    if "application/vnd.apple.mpegurl" in content_type:
        return True
    if VIDEO_URL_RE.search(url):
        return True
    return False


def url_looks_like_mp4(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    u = url.lower().split("#", 1)[0]
    path = u.split("?", 1)[0]
    if path.endswith(".mp4"):
        return True
    if ".mp4?" in u:
        return True
    return False


def response_looks_like_mp4(response: Response) -> bool:
    if response.status < 200 or response.status >= 400:
        return False
    url = response.url.lower()
    ct = (response.headers.get("content-type") or "").lower()
    if "video/mp4" in ct:
        return True
    base = url.split("?", 1)[0].split("#", 1)[0]
    if base.endswith(".mp4"):
        return True
    return False


def response_is_project_y_download(response: Response) -> bool:
    return "/backend/project_y/download/" in response.url


def url_from_project_y_download_response(response: Response) -> str | None:
    """Parse Sora backend download response (redirect, JSON with URL, or plain text URL)."""
    if not response_is_project_y_download(response):
        return None
    if response.status in (301, 302, 303, 307, 308):
        loc = response.headers.get("location", "").strip()
        if loc.startswith("http"):
            return loc
    if response.status != 200:
        return None
    ct = (response.headers.get("content-type") or "").lower()
    try:
        if "json" in ct:
            data: Any = response.json()
            if isinstance(data, str) and data.startswith("http"):
                return data
            if isinstance(data, dict):
                for key in (
                    "url",
                    "downloadUrl",
                    "download_url",
                    "href",
                    "signedUrl",
                    "signed_url",
                    "file_url",
                    "fileUrl",
                ):
                    val = data.get(key)
                    if isinstance(val, str) and val.startswith("http"):
                        return val
                nested = data.get("data") or data.get("result")
                if isinstance(nested, dict):
                    for key in ("url", "downloadUrl", "href"):
                        val = nested.get(key)
                        if isinstance(val, str) and val.startswith("http"):
                            return val
        if ct.startswith("text/plain"):
            text = response.text().strip().strip('"')
            if text.startswith("http"):
                return text
    except Exception:  # pylint: disable=broad-except
        pass
    return None


def _sora_download_menu_visible(page: Any) -> bool:
    return (
        page.get_by_role("menuitem", name=re.compile(r"^download$", re.I)).count() > 0
    )


def _sora_overflow_menu_trigger_indices(page: Any) -> list[int]:
    """Indices of ⋮ triggers in click order: above comment composer, then top-to-bottom."""
    triggers = page.locator(
        'button[aria-haspopup="menu"], button[aria-haspopup="true"]'
    )
    n = triggers.count()
    if n == 0:
        return []
    try:
        ordered: list[int] = page.evaluate(
            r"""() => {
                const sel = 'button[aria-haspopup="menu"], button[aria-haspopup="true"]';
                const nodes = Array.from(document.querySelectorAll(sel));
                const metrics = nodes.map((el, idx) => {
                    const r = el.getBoundingClientRect();
                    return { idx, top: r.top, bottom: r.bottom };
                });
                let composerTop = null;
                const trySelectors = [
                    'textarea[placeholder*="comment" i]',
                    'textarea[placeholder*="Comment" i]',
                ];
                for (const s of trySelectors) {
                    const el = document.querySelector(s);
                    if (el && el.getClientRects().length > 0) {
                        composerTop = el.getBoundingClientRect().top;
                        break;
                    }
                }
                if (composerTop == null) {
                    for (const el of document.querySelectorAll('[role="textbox"]')) {
                        const ph = (
                            el.getAttribute("aria-placeholder") ||
                            el.getAttribute("placeholder") ||
                            ""
                        ).toLowerCase();
                        if (ph.includes("comment") && el.getClientRects().length > 0) {
                            composerTop = el.getBoundingClientRect().top;
                            break;
                        }
                    }
                }
                if (composerTop == null) {
                    const ce = document.querySelector(
                        '[contenteditable="true"][data-placeholder*="comment" i]'
                    );
                    if (ce && ce.getClientRects().length > 0) {
                        composerTop = ce.getBoundingClientRect().top;
                    }
                }
                let pool = metrics;
                if (composerTop != null) {
                    const above = metrics.filter((m) => m.bottom < composerTop - 8);
                    if (above.length > 0) {
                        pool = above;
                    }
                }
                pool.sort((a, b) => a.top - b.top || a.idx - b.idx);
                return pool.map((m) => m.idx);
            }"""
        )
        return [i for i in ordered if 0 <= i < n]
    except Exception:  # pylint: disable=broad-except
        return list(range(n))


def open_sora_post_overflow_menu(page: Any) -> None:
    """Open the ⋮ menu that actually contains **Download** (skip comment/thread menus)."""
    if _sora_download_menu_visible(page):
        return

    try:
        vloc = page.locator("video")
        if vloc.count() > 0:
            vloc.first.scroll_into_view_if_needed(timeout=3_000)
            page.wait_for_timeout(200)
    except Exception:  # pylint: disable=broad-except
        pass

    triggers = page.locator(
        'button[aria-haspopup="menu"], button[aria-haspopup="true"]'
    )
    indices = _sora_overflow_menu_trigger_indices(page)
    n = triggers.count()
    tried: set[int] = set()

    def try_trigger(idx: int) -> bool:
        try:
            btn = triggers.nth(idx)
            if not btn.is_visible():
                return False
            btn.scroll_into_view_if_needed(timeout=3_000)
            btn.click(timeout=5_000)
            page.wait_for_timeout(450)
            if _sora_download_menu_visible(page):
                return True
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:  # pylint: disable=broad-except
            try:
                page.keyboard.press("Escape")
            except Exception:  # pylint: disable=broad-except
                pass
        return False

    for i in indices:
        tried.add(i)
        if try_trigger(i):
            return

    # Fallback: any trigger not yet tried (composer not found or unusual layout)
    for i in range(n):
        if i in tried:
            continue
        if try_trigger(i):
            return


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" ._")


def extension_from_url_or_type(url: str, content_type: str | None) -> str:
    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).name.lower()
    for ext in (".mp4", ".mov", ".webm", ".m3u8"):
        if path_name.endswith(ext):
            return ext
    if content_type:
        ctype = content_type.lower()
        if "video/mp4" in ctype:
            return ".mp4"
        if "video/webm" in ctype:
            return ".webm"
        if "quicktime" in ctype:
            return ".mov"
        if "mpegurl" in ctype:
            return ".m3u8"
    return ".bin"


def canonical_media_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().strip()
    path = unquote(parsed.path or "").strip()
    return f"{host}{path}"


def normalized_list_path(url: str) -> str:
    path = unquote(urlparse(url.split("#")[0]).path or "").strip()
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def normalized_page_dedupe_key(url: str) -> str | None:
    """Stable key for a Sora page (no query/hash): host + path; None for hub/list pages."""
    parsed = urlparse(url.split("#")[0])
    path = unquote(parsed.path or "").strip()
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path:
        path = "/"
    if path in LIST_PAGE_HUB_PATHS:
        return None
    host = parsed.netloc.lower().strip()
    if not host:
        return None
    return f"{host}{path}"


def post_page_key_for_dedupe(hit: MediaHit) -> str | None:
    """One download per post/detail page (same post, renewed signed URLs)."""
    if hit.detail_page:
        return normalized_page_dedupe_key(hit.detail_page)
    if hit.source_page:
        p = normalized_list_path(hit.source_page)
        if p.startswith("/p/"):
            return normalized_page_dedupe_key(hit.source_page)
    return None


def sora_slug_for_filename(hit: MediaHit) -> str | None:
    """Human-readable id from URL path (e.g. last segment of /p/s_…)."""
    for raw in (hit.detail_page, hit.source_page):
        if not raw:
            continue
        if raw is hit.source_page:
            p = normalized_list_path(raw)
            if not p.startswith("/p/"):
                continue
        key = normalized_page_dedupe_key(raw)
        if not key:
            continue
        path = unquote(urlparse(raw.split("#")[0]).path or "").strip().rstrip("/")
        segments = [s for s in path.split("/") if s]
        if not segments:
            continue
        slug = sanitize_filename(segments[-1])
        if slug:
            return slug[:100]
    return None


def add_media_hit(
    hits: dict[str, MediaHit],
    url: str,
    source_page: str,
    *,
    content_type: str | None = None,
    status: int | None = None,
    detail_page: str | None = None,
) -> None:
    if not url or url in hits:
        return
    hits[url] = MediaHit(
        url=url,
        source_page=source_page,
        discovered_at=utc_now(),
        media_key=canonical_media_key(url),
        content_type=content_type,
        status=status,
        detail_page=detail_page,
    )
    print(f"[captured] {url}")


LIST_PAGE_HUB_PATHS = frozenset(
    {
        "/",
        "/profile",
        "/drafts",
        "/login",
        "/settings",
        "/explore",
    }
)


def collect_detail_page_urls(page: Any, list_base: str) -> list[str]:
    """Gather same-origin Sora links that likely point at item detail pages."""
    try:
        raw: list[str] = page.evaluate(
            """([hubs]) => {
                const hub = new Set(hubs);
                const out = new Set();
                const host = "sora.chatgpt.com";
                for (const el of document.querySelectorAll("[href]")) {
                  const rawHref = el.getAttribute("href");
                  if (!rawHref || rawHref.startsWith("#")) continue;
                  let u;
                  try { u = new URL(rawHref, location.origin); } catch (e) { continue; }
                  if (u.hostname !== host) continue;
                  let path = u.pathname || "";
                  if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
                  if (path.startsWith("/api")) continue;
                  if (hub.has(path)) continue;
                  const segs = path.split("/").filter(Boolean);
                  if (segs.length === 0) continue;
                  out.add(u.origin + path + u.search);
                }
                return Array.from(out);
            }""",
            [sorted(LIST_PAGE_HUB_PATHS)],
        )
    except Exception:  # pylint: disable=broad-except
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    base = (list_base or "").split("#")[0]
    for u in sorted(raw):
        if u == base:
            continue
        if u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    return ordered


def gather_all_detail_urls_on_list_page(
    page: Any,
    list_base: str,
    *,
    max_rounds: int,
    idle_rounds: int,
    pause_ms: int,
) -> list[str]:
    """Scroll the list until few new detail links appear; return unique URLs (/p/ first)."""
    aggregated: set[str] = set()
    stale = 0
    for round_i in range(max_rounds):
        batch = collect_detail_page_urls(page, list_base)
        before_count = len(aggregated)
        aggregated.update(batch)
        if len(aggregated) == before_count:
            stale += 1
            if stale >= idle_rounds:
                print(
                    f"[detail] Link gathering settled after {round_i + 1} round(s); "
                    f"{len(aggregated)} unique detail URL(s)."
                )
                break
        stale = 0
        if round_i == 0:
            print(f"[detail] First collect: {len(aggregated)} unique detail link(s).")
        page.evaluate(
            "() => window.scrollBy(0, Math.floor(window.innerHeight * 0.92))"
        )
        page.wait_for_timeout(pause_ms)
    else:
        print(
            f"[detail] Link gathering hit max rounds ({max_rounds}); "
            f"{len(aggregated)} unique detail URL(s)."
        )

    def sort_key(u: str) -> tuple[int, str]:
        try:
            path = urlparse(u).path or ""
        except Exception:  # pylint: disable=broad-except
            return (1, u)
        return (0 if path.startswith("/p/") else 1, u)

    return sorted(aggregated, key=sort_key)


def extract_detail_download_url(
    page: Any,
    download_timeout_ms: int,
    *,
    allow_dom_fallback: bool = False,
) -> str | None:
    """Resolve a media URL via the site's Download UI (menu / API / Download control).

    When allow_dom_fallback is False (default), does not scrape <video> or random page links
    (those match overview preview streams). Enable --detail-dom-fallback for old behavior.
    """
    download_controls = (
        lambda: page.get_by_role("link", name=re.compile(r"download", re.I)),
        lambda: page.get_by_role("button", name=re.compile(r"download", re.I)),
    )

    # 0) Sora post page: ⋮ overflow → menuitem "Download" → /backend/project_y/download/… or MP4
    try:
        open_sora_post_overflow_menu(page)
        menu_download = page.get_by_role("menuitem", name=re.compile(r"^download$", re.I))
        if menu_download.count() > 0:
            try:
                with page.expect_response(
                    lambda r: response_is_project_y_download(r) or response_looks_like_mp4(r),
                    timeout=download_timeout_ms,
                ) as resp_pi:
                    menu_download.first.click(timeout=8_000)
                resp = resp_pi.value
                if response_is_project_y_download(resp) and resp.status == 401:
                    print(
                        "[detail] Download API returned 401 — log in with the same "
                        "browser profile and retry capture."
                    )
                signed = url_from_project_y_download_response(resp)
                if signed:
                    clip = signed if len(signed) <= 120 else f"{signed[:120]}…"
                    print(f"[detail] url from project_y download API: {clip}")
                    return signed
                if response_looks_like_mp4(resp):
                    return resp.url
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                open_sora_post_overflow_menu(page)
                menu_download = page.get_by_role("menuitem", name=re.compile(r"^download$", re.I))
                if menu_download.count() > 0:
                    with page.expect_download(timeout=download_timeout_ms) as dl_info:
                        menu_download.first.click(timeout=8_000)
                    downloaded = dl_info.value
                    durl = downloaded.url or ""
                    suggested = (downloaded.suggested_filename or "").lower()
                    if url_looks_like_mp4(durl) or suggested.endswith(".mp4"):
                        if durl:
                            return durl
            except Exception:  # pylint: disable=broad-except
                pass
    except Exception:  # pylint: disable=broad-except
        pass

    # 1) Download click + first HTTP response that looks like MP4
    for getter in download_controls:
        try:
            el = getter()
            if el.count() == 0:
                continue
            with page.expect_response(
                response_looks_like_mp4,
                timeout=download_timeout_ms,
            ) as resp_pi:
                el.first.click(timeout=8_000)
            mp4_url = resp_pi.value.url
            if mp4_url:
                clip = mp4_url if len(mp4_url) <= 120 else f"{mp4_url[:120]}…"
                print(f"[detail] mp4 from download response: {clip}")
                return mp4_url
        except Exception:  # pylint: disable=broad-except
            continue

    # 2) Download event (URL or suggested filename)
    for getter in download_controls:
        try:
            el = getter()
            if el.count() == 0:
                continue
            with page.expect_download(timeout=download_timeout_ms) as dl_info:
                el.first.click(timeout=8_000)
            downloaded = dl_info.value
            durl = downloaded.url or ""
            suggested = (downloaded.suggested_filename or "").lower()
            if url_looks_like_mp4(durl) or suggested.endswith(".mp4"):
                if durl:
                    clip = durl if len(durl) <= 120 else f"{durl[:120]}…"
                    print(f"[detail] mp4 from download event: {clip}")
                    return durl
        except Exception:  # pylint: disable=broad-except
            continue

    if not allow_dom_fallback:
        return None

    # 3) <video> / <source> — prefer MP4 src (in-page player; optional fallback)
    try:
        for sel in ('video[src^="http"]', 'video source[src^="http"]'):
            loc = page.locator(sel)
            if loc.count() > 0:
                src = loc.first.get_attribute("src") or ""
                if src.startswith("http") and url_looks_like_mp4(src):
                    return src
    except Exception:  # pylint: disable=broad-except
        pass

    # 4) Anchor hrefs — MP4 only
    try:
        mp4_hrefs: list[str] = page.evaluate(
            r"""() => {
                const re = /\.mp4(\?|$)/i;
                const out = [];
                for (const a of document.querySelectorAll("a[href]")) {
                  const h = a.getAttribute("href");
                  if (!h || !re.test(h)) continue;
                  try { out.push(new URL(h, location.href).href); } catch (e) {}
                }
                return [...new Set(out)];
            }"""
        )
        for h in mp4_hrefs:
            if h.startswith("http"):
                return h
    except Exception:  # pylint: disable=broad-except
        pass

    # 5) a[download] pointing at MP4
    try:
        loc = page.locator('a[download][href^="http"]')
        for i in range(loc.count()):
            href = loc.nth(i).get_attribute("href") or ""
            if url_looks_like_mp4(href):
                return href
    except Exception:  # pylint: disable=broad-except
        pass

    # 6) Fallback: mov/webm/m3u8 / non-mp4 video src
    try:
        for sel in ('video[src^="http"]', 'video source[src^="http"]'):
            loc = page.locator(sel)
            if loc.count() > 0:
                src = loc.first.get_attribute("src")
                if src and src.startswith("http") and VIDEO_URL_RE.search(src):
                    return src
    except Exception:  # pylint: disable=broad-except
        pass

    try:
        hrefs: list[str] = page.evaluate(
            r"""() => {
                const re = /\.(mov|webm|m3u8)(\?|$)/i;
                const out = [];
                for (const a of document.querySelectorAll("a[href]")) {
                  const h = a.getAttribute("href");
                  if (!h || !re.test(h)) continue;
                  try { out.push(new URL(h, location.href).href); } catch (e) {}
                }
                return [...new Set(out)];
            }"""
        )
        for h in hrefs:
            if h.startswith("http"):
                return h
    except Exception:  # pylint: disable=broad-except
        pass

    try:
        loc = page.locator('a[download][href^="http"]')
        if loc.count() > 0:
            href = loc.first.get_attribute("href")
            if href and VIDEO_URL_RE.search(href):
                return href
    except Exception:  # pylint: disable=broad-except
        pass

    return None


def capture_downloads_from_detail_pages(
    page: Any,
    hits: dict[str, MediaHit],
    list_page_url: str,
    *,
    max_pages: int,
    settle_ms: int,
    login_timeout_ms: int,
    goto_wait_until: str,
    download_timeout_ms: int,
    gather_max_rounds: int,
    gather_idle_rounds: int,
    gather_pause_ms: int,
    allow_dom_fallback: bool,
    nav_max_attempts: int,
    nav_retry_delay_ms: int,
) -> None:
    candidates = gather_all_detail_urls_on_list_page(
        page,
        list_page_url,
        max_rounds=gather_max_rounds,
        idle_rounds=gather_idle_rounds,
        pause_ms=gather_pause_ms,
    )
    if not candidates:
        print("[detail] No detail links found on this list page.")
        return

    unlimited = max_pages <= 0
    to_visit = candidates if unlimited else candidates[:max_pages]
    total = len(to_visit)
    if unlimited and len(candidates) > 400:
        print(
            f"[detail] Visiting all {total} detail pages (no --max-detail-pages limit). "
            "Use Ctrl+C to stop early; manifest saves partial results."
        )
    elif not unlimited and len(candidates) > max_pages:
        print(
            f"[detail] Visiting {total} of {len(candidates)} detail page(s) "
            f"(cap --max-detail-pages={max_pages})."
        )
    else:
        print(f"[detail] Visiting {total} detail page(s)…")

    for i, detail_url in enumerate(to_visit, start=1):
        print(f"[detail] ({i}/{total}) {detail_url}")
        if not navigate_for_login_retry(
            page,
            detail_url,
            goto_wait_until,
            login_timeout_ms,
            max_attempts=nav_max_attempts,
            retry_delay_ms=nav_retry_delay_ms,
            log_prefix="[detail]",
        ):
            print(f"[detail] Skipped after retries: {detail_url}")
            continue
        page.wait_for_timeout(settle_ms)
        dl = extract_detail_download_url(
            page,
            download_timeout_ms,
            allow_dom_fallback=allow_dom_fallback,
        )
        if dl:
            add_media_hit(hits, dl, detail_url, detail_page=detail_url)
        else:
            print(f"[detail] Could not resolve download URL on page; skipped.")


def make_filename(hit: MediaHit, index: int) -> str:
    """Stable names: manifest order, human id from post URL when known, short hash for uniqueness."""
    ext = extension_from_url_or_type(hit.url, hit.content_type)
    key = hit.media_key or canonical_media_key(hit.url)
    short_hash = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    idx_str = f"{index:04d}"
    slug = sora_slug_for_filename(hit)
    if slug:
        max_slug = 96
        if len(slug) > max_slug:
            slug = slug[:max_slug].rstrip("._-")
        return f"{idx_str}_{slug}_{short_hash}{ext}"
    parsed = urlparse(hit.url)
    base = sanitize_filename(Path(unquote(parsed.path)).stem)
    if base and len(base) <= 64:
        return f"{idx_str}_{base}_{short_hash}{ext}"
    return f"{idx_str}_video_{short_hash}{ext}"


def navigate_for_login(
    page: Any,
    url: str,
    wait_until: str,
    timeout_ms: int,
) -> bool:
    """Navigate and wait for main document. Returns False if goto could not complete."""
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    except TimeoutError:
        print(
            "[warn] Navigation timed out; page is left as-is so you can finish login manually."
        )
    except Error as exc:
        msg = str(exc)
        print(f"[warn] Navigation error: {msg.splitlines()[0]}")
        if "ERR_ABORTED" in msg or "net::ERR_" in msg:
            try:
                page.wait_for_timeout(600)
                page.goto(url, wait_until="commit", timeout=timeout_ms)
            except Exception as retry_exc:  # pylint: disable=broad-except
                print(f"[warn] Navigation retry failed: {retry_exc}")
                return False
        else:
            return False
    try:
        remaining = max(5_000, min(30_000, timeout_ms))
        page.wait_for_load_state("load", timeout=remaining)
    except TimeoutError:
        pass
    except Error:
        pass
    try:
        title = page.title()
    except Exception:  # pylint: disable=broad-except
        title = ""
    print(f"[nav] url={page.url!r} title={title!r}")
    return True


def navigate_for_login_retry(
    page: Any,
    url: str,
    wait_until: str,
    timeout_ms: int,
    *,
    max_attempts: int,
    retry_delay_ms: int,
    log_prefix: str = "",
) -> bool:
    """Call navigate_for_login up to max_attempts times before giving up."""
    attempts = max(1, max_attempts)
    prefix = f"{log_prefix} " if log_prefix else ""
    for attempt in range(1, attempts + 1):
        if navigate_for_login(page, url, wait_until, timeout_ms):
            if attempt > 1:
                print(f"{prefix}Navigation succeeded on attempt {attempt}/{attempts}.")
            return True
        if attempt < attempts:
            print(
                f"{prefix}Navigation attempt {attempt}/{attempts} failed; "
                f"retrying in {retry_delay_ms}ms…"
            )
            page.wait_for_timeout(retry_delay_ms)
    print(f"{prefix}Giving up after {attempts} navigation attempt(s): {url}")
    return False


def try_click_pagination(page: Any) -> int:
    clicked = 0
    for selector in PAGINATION_SELECTORS:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:  # pylint: disable=broad-except
            continue

        for idx in range(count):
            try:
                element = locator.nth(idx)
                if not element.is_visible():
                    continue
                element.scroll_into_view_if_needed(timeout=1500)
                element.click(timeout=2000)
                clicked += 1
                page.wait_for_timeout(700)
            except Exception:  # pylint: disable=broad-except
                continue
    return clicked


def auto_explore_page(
    page: Any,
    steps: int,
    step_wait_ms: int,
    click_interval: int,
    max_duration_s: int,
) -> None:
    started = time.monotonic()
    previous_height = 0
    stagnant_steps = 0
    at_bottom_streak = 0
    for step in range(1, steps + 1):
        if time.monotonic() - started >= max_duration_s:
            print(f"[auto] reached max duration ({max_duration_s}s); stopping")
            break

        if step == 1 or (click_interval > 0 and step % click_interval == 0):
            clicks = try_click_pagination(page)
            if clicks:
                print(f"[auto] step {step}: clicked pagination controls {clicks}x")

        try:
            metrics_before = page.evaluate(
                "() => ({ y: window.scrollY, inner: window.innerHeight, h: document.body.scrollHeight })"
            )
            height_before = int(metrics_before["h"])
            page.evaluate("() => window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
            page.wait_for_timeout(step_wait_ms)
            metrics_after = page.evaluate(
                "() => ({ y: window.scrollY, inner: window.innerHeight, h: document.body.scrollHeight })"
            )
            height_after = int(metrics_after["h"])
            scroll_bottom = int(metrics_after["y"]) + int(metrics_after["inner"])
            is_at_bottom = scroll_bottom >= (height_after - 6)
        except Exception:  # pylint: disable=broad-except
            page.wait_for_timeout(step_wait_ms)
            continue

        grew = height_after > height_before or height_after > previous_height
        previous_height = max(previous_height, height_after)

        if grew:
            stagnant_steps = 0
        else:
            stagnant_steps += 1

        if is_at_bottom:
            at_bottom_streak += 1
        else:
            at_bottom_streak = 0

        if step % 10 == 0 or stagnant_steps == 5:
            print(
                f"[auto] step={step}/{steps} height={height_after} "
                f"stagnant_steps={stagnant_steps} bottom_streak={at_bottom_streak}"
            )

        if at_bottom_streak >= 6:
            print("[auto] reached bottom repeatedly; ending auto scroll")
            break

        if stagnant_steps >= 12:
            print("[auto] no additional growth detected; ending auto scroll early")
            break


def capture(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    ensure_dir(output_dir)
    profile_dir = Path(args.profile_dir).resolve()
    ensure_dir(profile_dir)
    manifests_dir = output_dir / "manifests"
    ensure_dir(manifests_dir)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest_path = manifests_dir / f"capture-{timestamp}.json"
    storage_state_path = manifests_dir / f"storage-state-{timestamp}.json"

    hits: dict[str, MediaHit] = {}
    current_page = {"url": ""}
    interrupted = False

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": args.headless,
            "viewport": {"width": 1600, "height": 1000},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if args.browser_channel != "chromium":
            launch_kwargs["channel"] = args.browser_channel
        context = p.chromium.launch_persistent_context(**launch_kwargs)
        print("[hint] Press Ctrl+C any time to stop and save current capture.")
        print(
            "[hint] Unattended capture: no Enter prompts or idle waits unless you pass "
            "--verification-prompt or --seconds-per-target > 0. Use a logged-in "
            "--profile-dir from an earlier run."
        )
        if args.browser_channel != "chromium":
            print(
                f"[hint] Using system {args.browser_channel} — login and SSO often work more reliably than bundled Chromium."
            )

        def on_request(request: Request) -> None:
            if request.resource_type == "media":
                add_media_hit(hits, request.url, current_page["url"])

        def on_response(response: Response) -> None:
            if not is_media_response(response):
                return
            add_media_hit(
                hits,
                response.url,
                current_page["url"],
                content_type=response.headers.get("content-type"),
                status=response.status,
            )

        if args.passive_media_capture:
            context.on("request", on_request)
            context.on("response", on_response)
            print(
                "[hint] Passive media capture on: preview/stream URLs from grid/detail may be saved."
            )
        else:
            print(
                "[hint] Passive media capture off: only URLs from each detail page "
                "Download flow (⋯ → Download) are saved—not overview/grid videos."
            )

        targets = args.targets or DEFAULT_TARGETS
        try:
            bootstrap_url = "" if args.no_bootstrap else (args.bootstrap_url or "").strip()
            if bootstrap_url:
                print(f"\n[Bootstrap] Opening: {bootstrap_url}")
                boot = context.new_page()
                current_page["url"] = bootstrap_url
                if not navigate_for_login_retry(
                    boot,
                    bootstrap_url,
                    args.goto_wait_until,
                    args.login_timeout_ms,
                    max_attempts=args.nav_max_attempts,
                    retry_delay_ms=args.nav_retry_delay_ms,
                    log_prefix="[bootstrap]",
                ):
                    print("[warn] Bootstrap navigation failed after retries; continuing anyway.")
                if args.verification_prompt and not args.headless:
                    print(
                        "[verify] Log in or complete any checks in the browser, "
                        "then press Enter here."
                    )
                    try:
                        input()
                    except EOFError:
                        print("[verify] No interactive stdin detected; continuing.")
                boot.close()

            for target in targets:
                print(f"\nOpening: {target}")
                page = context.new_page()
                current_page["url"] = target
                if not navigate_for_login_retry(
                    page,
                    target,
                    args.goto_wait_until,
                    args.login_timeout_ms,
                    max_attempts=args.nav_max_attempts,
                    retry_delay_ms=args.nav_retry_delay_ms,
                    log_prefix="[target]",
                ):
                    print(f"[warn] Skipping target after navigation retries: {target}")
                    page.close()
                    continue

                if args.verification_prompt and not args.headless:
                    print(
                        "[verify] If Cloudflare challenge appears, solve it in the browser tab."
                    )
                    print(
                        "[verify] Press Enter here after the page is fully accessible."
                    )
                    try:
                        input()
                    except EOFError:
                        print("[verify] No interactive stdin detected; continuing.")

                if args.auto_explore:
                    print(
                        "[auto] exploring page with scrolling/pagination "
                        f"for up to {args.auto_scroll_steps} steps..."
                    )
                    auto_explore_page(
                        page=page,
                        steps=args.auto_scroll_steps,
                        step_wait_ms=args.auto_step_wait_ms,
                        click_interval=args.auto_click_interval,
                        max_duration_s=args.auto_max_seconds,
                    )

                if args.seconds_per_target > 0:
                    print(
                        "Interact manually (log in, open items) "
                        f"for {args.seconds_per_target} seconds..."
                    )
                    page.wait_for_timeout(args.seconds_per_target * 1000)

                if args.detail_download_links:
                    current_page["url"] = target
                    capture_downloads_from_detail_pages(
                        page,
                        hits,
                        target,
                        max_pages=args.max_detail_pages,
                        settle_ms=args.detail_settle_ms,
                        login_timeout_ms=args.login_timeout_ms,
                        goto_wait_until=args.goto_wait_until,
                        download_timeout_ms=args.detail_download_timeout_ms,
                        gather_max_rounds=args.detail_gather_max_rounds,
                        gather_idle_rounds=args.detail_gather_idle_rounds,
                        gather_pause_ms=args.detail_gather_pause_ms,
                        allow_dom_fallback=args.detail_dom_fallback,
                        nav_max_attempts=args.nav_max_attempts,
                        nav_retry_delay_ms=args.nav_retry_delay_ms,
                    )
                page.close()
        except KeyboardInterrupt:
            interrupted = True
            print("\n[interrupt] Ctrl+C received. Saving captured URLs collected so far...")

        context.storage_state(path=str(storage_state_path))
        context.close()

    items = [asdict(hit) for hit in hits.values()]
    manifest = {
        "created_at": utc_now(),
        "targets": targets,
        "count": len(items),
        "storage_state": str(storage_state_path),
        "items": items,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nSaved {len(items)} media URLs to: {manifest_path}")
    print(f"Saved browser storage state to: {storage_state_path}")
    if interrupted:
        print("[interrupt] Capture ended early by user.")
    return 0


def find_latest_manifest(output_dir: Path) -> Path:
    manifests_dir = output_dir / "manifests"
    files = sorted(manifests_dir.glob("capture-*.json"))
    if not files:
        raise FileNotFoundError(f"No capture manifests found in {manifests_dir}")
    return files[-1]


def download(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    ensure_dir(output_dir)
    if args.upload_dir:
        downloads_dir = Path(args.upload_dir).expanduser().resolve()
    else:
        downloads_dir = output_dir / "downloads"
    ensure_dir(downloads_dir)
    print(f"Download folder: {downloads_dir}")

    manifest_path = Path(args.manifest).resolve() if args.manifest else find_latest_manifest(output_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = manifest.get("items", [])
    if not items:
        print(f"No items in manifest: {manifest_path}")
        return 1

    storage_state = args.storage_state or manifest.get("storage_state")
    print(f"Using manifest: {manifest_path}")
    print(f"Items to download: {len(items)}")

    completed = 0
    failed = 0
    duplicate = 0
    seen_media_keys: set[str] = set()
    seen_post_pages: set[str] = set()
    interrupted = False

    with sync_playwright() as p:
        request_context = p.request.new_context(
            storage_state=storage_state if storage_state else None
        )
        print("[hint] Press Ctrl+C any time to stop download gracefully.")

        try:
            for idx, item in enumerate(items, start=1):
                hit = MediaHit(
                    url=item["url"],
                    source_page=item.get("source_page", ""),
                    discovered_at=item.get("discovered_at", ""),
                    media_key=item.get("media_key"),
                    content_type=item.get("content_type"),
                    status=item.get("status"),
                    detail_page=item.get("detail_page"),
                )
                media_key = hit.media_key or canonical_media_key(hit.url)
                post_key = post_page_key_for_dedupe(hit)
                if post_key and post_key in seen_post_pages:
                    duplicate += 1
                    print(f"[skip duplicate post] {hit.url}")
                    continue
                if media_key in seen_media_keys:
                    duplicate += 1
                    print(f"[skip duplicate] {hit.url}")
                    continue
                seen_media_keys.add(media_key)

                filename = make_filename(hit, idx)
                target_path = downloads_dir / filename
                if target_path.exists() and not args.overwrite:
                    print(f"[skip exists] {target_path.name}")
                    if post_key:
                        seen_post_pages.add(post_key)
                    continue

                try:
                    response = request_context.get(hit.url, timeout=args.timeout_ms)
                    if not response.ok:
                        raise Error(f"HTTP {response.status}")
                    body = response.body()
                    target_path.write_bytes(body)
                    if post_key:
                        seen_post_pages.add(post_key)
                    completed += 1
                    print(f"[ok] {target_path.name} ({len(body)} bytes)")
                except Exception as exc:  # pylint: disable=broad-except
                    failed += 1
                    print(f"[failed] {hit.url} -> {exc}")
        except KeyboardInterrupt:
            interrupted = True
            print("\n[interrupt] Ctrl+C received. Finishing with current results...")

        request_context.dispose()

    print(
        f"\nDownload complete. Success: {completed}, "
        f"Duplicates skipped: {duplicate}, Failed: {failed}"
    )
    print(f"Files directory: {downloads_dir}")
    if interrupted:
        print("[interrupt] Download ended early by user.")
    return 0 if failed == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture and download media from Sora profile/drafts pages."
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for manifests/downloaded files (default: output)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    capture_parser = subparsers.add_parser(
        "capture", help="Open targets and capture media URLs."
    )
    capture_parser.add_argument(
        "--targets",
        nargs="*",
        default=DEFAULT_TARGETS,
        help="Target URLs to visit.",
    )
    capture_parser.add_argument(
        "--seconds-per-target",
        type=int,
        default=0,
        help="Extra idle seconds per list target after auto explore (default: 0, unattended)",
    )
    capture_parser.add_argument(
        "--profile-dir",
        default=".playwright-profile",
        help="Persistent browser profile directory (default: .playwright-profile)",
    )
    capture_parser.add_argument(
        "--login-timeout-ms",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_MS,
        help="Timeout for initial page open/login load in ms (default: 60000)",
    )
    capture_parser.add_argument(
        "--nav-max-attempts",
        type=int,
        default=4,
        help="Per-URL navigation tries (bootstrap, list target, each detail) before skip (default: 4)",
    )
    capture_parser.add_argument(
        "--nav-retry-delay-ms",
        type=int,
        default=1_000,
        help="Wait between navigation retries in ms (default: 1000)",
    )
    capture_parser.add_argument(
        "--goto-wait-until",
        choices=["commit", "domcontentloaded", "load", "networkidle"],
        default=DEFAULT_GOTO_WAIT_UNTIL,
        help="Playwright goto wait_until (default: domcontentloaded)",
    )
    capture_parser.add_argument(
        "--browser-channel",
        choices=["chromium", "chrome", "msedge"],
        default="chrome",
        help="Browser engine: installed Chrome/Edge, or bundled Chromium (default: chrome)",
    )
    capture_parser.add_argument(
        "--bootstrap-url",
        default="https://sora.chatgpt.com/login",
        help="URL to open once before targets (default: https://sora.chatgpt.com/login).",
    )
    capture_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Skip opening the bootstrap URL before targets.",
    )
    capture_parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    capture_parser.add_argument(
        "--verification-prompt",
        action="store_true",
        help="Pause for Enter after bootstrap and each target (login/Cloudflare). Default: off.",
    )
    capture_parser.add_argument(
        "--no-auto-explore",
        action="store_false",
        dest="auto_explore",
        help="Disable automatic scrolling and pagination clicks.",
    )
    capture_parser.add_argument(
        "--auto-scroll-steps",
        type=int,
        default=40,
        help="Maximum auto-explore scroll iterations per page (default: 40)",
    )
    capture_parser.add_argument(
        "--auto-step-wait-ms",
        type=int,
        default=600,
        help="Wait after each auto-explore step in ms (default: 600)",
    )
    capture_parser.add_argument(
        "--auto-click-interval",
        type=int,
        default=4,
        help="Try pagination clicks every N steps (default: 4)",
    )
    capture_parser.add_argument(
        "--auto-max-seconds",
        type=int,
        default=30,
        help="Hard time limit for auto-explore per page in seconds (default: 30)",
    )
    capture_parser.add_argument(
        "--no-detail-download-links",
        action="store_false",
        dest="detail_download_links",
        help="Do not open each item's detail page to resolve its download link.",
    )
    capture_parser.add_argument(
        "--passive-media-capture",
        action="store_true",
        help="Record video URLs from all network traffic (grid/overview + detail). "
        "Default off: only detail-page Download URLs are recorded.",
    )
    capture_parser.add_argument(
        "--detail-dom-fallback",
        action="store_true",
        help="On detail pages, also scrape <video>/links for media if Download UI fails "
        "(can match preview streams, not just file download).",
    )
    capture_parser.add_argument(
        "--max-detail-pages",
        type=int,
        default=0,
        help="Max detail pages per list (0 = all discovered links, default: 0)",
    )
    capture_parser.add_argument(
        "--detail-gather-max-rounds",
        type=int,
        default=60,
        help="Max scroll rounds while collecting detail links on list pages (default: 60)",
    )
    capture_parser.add_argument(
        "--detail-gather-idle-rounds",
        type=int,
        default=4,
        help="Stop gathering when this many rounds add no new links (default: 4)",
    )
    capture_parser.add_argument(
        "--detail-gather-pause-ms",
        type=int,
        default=700,
        help="Pause between gather scroll steps in ms (default: 700)",
    )
    capture_parser.add_argument(
        "--detail-settle-ms",
        type=int,
        default=2_500,
        help="Wait after opening a detail page before scraping (default: 2500)",
    )
    capture_parser.add_argument(
        "--detail-download-timeout-ms",
        type=int,
        default=20_000,
        help="Timeout for Playwright download event when clicking Download (default: 20000)",
    )
    capture_parser.set_defaults(auto_explore=True)
    capture_parser.set_defaults(detail_download_links=True)
    capture_parser.set_defaults(func=capture)

    download_parser = subparsers.add_parser(
        "download", help="Download URLs from a capture manifest."
    )
    download_parser.add_argument(
        "--manifest",
        default=None,
        help="Path to capture manifest JSON. Defaults to latest manifest.",
    )
    download_parser.add_argument(
        "--storage-state",
        default=None,
        help="Optional Playwright storage-state JSON for authenticated requests.",
    )
    download_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files.",
    )
    download_parser.add_argument(
        "--upload-dir",
        default=None,
        metavar="DIR",
        help="Save media files under this folder (default: <output-dir>/downloads).",
    )
    download_parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120_000,
        help="Per-request timeout in milliseconds (default: 120000)",
    )
    download_parser.set_defaults(func=download)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
