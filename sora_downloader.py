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
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.sync_api import Error, Request, Response, sync_playwright


DEFAULT_TARGETS = [
    "https://sora.chatgpt.com/profile",
    "https://sora.chatgpt.com/drafts",
]

VIDEO_URL_RE = re.compile(r"\.(mp4|mov|webm|m3u8)(?:\?|$)", re.IGNORECASE)
DEFAULT_LOGIN_TIMEOUT_MS = 60_000
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


def make_filename(hit: MediaHit, index: int) -> str:
    parsed = urlparse(hit.url)
    base = sanitize_filename(Path(unquote(parsed.path)).stem) or f"video_{index:04d}"
    ext = extension_from_url_or_type(hit.url, hit.content_type)
    key = hit.media_key or canonical_media_key(hit.url)
    short_hash = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{index:04d}_{base}_{short_hash}{ext}"


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
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            viewport={"width": 1600, "height": 1000},
        )
        print("[hint] Press Ctrl+C any time to stop and save current capture.")

        def on_request(request: Request) -> None:
            if request.resource_type == "media":
                url = request.url
                if url not in hits:
                    hits[url] = MediaHit(
                        url=url,
                        source_page=current_page["url"],
                        discovered_at=utc_now(),
                        media_key=canonical_media_key(url),
                    )
                    print(f"[media request] {url}")

        def on_response(response: Response) -> None:
            if not is_media_response(response):
                return
            url = response.url
            if url not in hits:
                hits[url] = MediaHit(
                    url=url,
                    source_page=current_page["url"],
                    discovered_at=utc_now(),
                    media_key=canonical_media_key(url),
                    content_type=response.headers.get("content-type"),
                    status=response.status,
                )
                print(f"[media response] {url}")

        context.on("request", on_request)
        context.on("response", on_response)

        targets = args.targets or DEFAULT_TARGETS
        try:
            for target in targets:
                print(f"\nOpening: {target}")
                page = context.new_page()
                current_page["url"] = target
                page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=args.login_timeout_ms,
                )

                if not args.headless and not args.skip_verification_prompt:
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
    downloads_dir = output_dir / "downloads"
    ensure_dir(downloads_dir)

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
                )
                media_key = hit.media_key or canonical_media_key(hit.url)
                if media_key in seen_media_keys:
                    duplicate += 1
                    print(f"[skip duplicate] {hit.url}")
                    continue
                seen_media_keys.add(media_key)

                filename = make_filename(hit, idx)
                target_path = downloads_dir / filename
                if target_path.exists() and not args.overwrite:
                    print(f"[skip exists] {target_path.name}")
                    continue

                try:
                    response = request_context.get(hit.url, timeout=args.timeout_ms)
                    if not response.ok:
                        raise Error(f"HTTP {response.status}")
                    body = response.body()
                    target_path.write_bytes(body)
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
        default=20,
        help="Manual interaction time per page after auto explore (default: 20)",
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
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    capture_parser.add_argument(
        "--skip-verification-prompt",
        action="store_true",
        help="Skip Enter-to-continue prompt after each page opens.",
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
    capture_parser.set_defaults(auto_explore=True)
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
