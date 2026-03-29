# Sora Profile/Drafts Downloader

Downloads media URLs discovered from:

- `https://sora.chatgpt.com/login` (opened first by default so you can sign in)
- `https://sora.chatgpt.com/profile`
- `https://sora.chatgpt.com/drafts`

Use this only for content you own or are authorized to download.

## 1) Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

`capture` defaults to **your installed Google Chrome** (`--browser-channel chrome`). If you do not have Chrome, install it or run with `--browser-channel msedge` or `--browser-channel chromium`.

## CLI usage (live)

Generated from:

```powershell
python sora_downloader.py --help
python sora_downloader.py capture --help
python sora_downloader.py download --help
```

Top-level:

```text
usage: sora_downloader.py [-h] [--output-dir OUTPUT_DIR] {capture,download} ...
```

Capture:

```text
usage: sora_downloader.py capture [-h] [--targets [TARGETS ...]]
                                  [--seconds-per-target SECONDS_PER_TARGET]
                                  [--profile-dir PROFILE_DIR]
                                  [--login-timeout-ms LOGIN_TIMEOUT_MS]
                                  [--nav-max-attempts NAV_MAX_ATTEMPTS]
                                  [--nav-retry-delay-ms NAV_RETRY_DELAY_MS]
                                  [--goto-wait-until {commit,domcontentloaded,load,networkidle}]
                                  [--browser-channel {chromium,chrome,msedge}]
                                  [--bootstrap-url BOOTSTRAP_URL]
                                  [--no-bootstrap] [--headless]
                                  [--verification-prompt]
                                  [--no-auto-explore]
                                  [--auto-scroll-steps AUTO_SCROLL_STEPS]
                                  [--auto-step-wait-ms AUTO_STEP_WAIT_MS]
                                  [--auto-click-interval AUTO_CLICK_INTERVAL]
                                  [--auto-max-seconds AUTO_MAX_SECONDS]
                                  [--no-detail-download-links]
                                  [--passive-media-capture]
                                  [--detail-dom-fallback]
                                  [--max-detail-pages MAX_DETAIL_PAGES]
                                  [--detail-gather-max-rounds DETAIL_GATHER_MAX_ROUNDS]
                                  [--detail-gather-idle-rounds DETAIL_GATHER_IDLE_ROUNDS]
                                  [--detail-gather-pause-ms DETAIL_GATHER_PAUSE_MS]
                                  [--detail-settle-ms DETAIL_SETTLE_MS]
                                  [--detail-download-timeout-ms DETAIL_DOWNLOAD_TIMEOUT_MS]
```

Download:

```text
usage: sora_downloader.py download [-h] [--manifest MANIFEST]
                                   [--storage-state STORAGE_STATE]
                                   [--overwrite] [--upload-dir DIR]
                                   [--timeout-ms TIMEOUT_MS]
```

## 2) Capture media URLs

Capture opens a **visible browser** (default: **installed Chrome**) with a persistent profile. It loads `https://sora.chatgpt.com/login` once first so you can sign in, then opens each target. After navigation it prints `[nav] url=... title=...` so you can confirm you reached the real site.

**Unattended by default:** `capture` does **not** wait for Enter and does **not** add idle time after each target (`--seconds-per-target` defaults to `0`). Use a **`--profile-dir` that is already logged in** to Sora (log in once with `--verification-prompt`, or sign in manually in that profile outside the script). For Cloudflare/login struggles, run once with **`--verification-prompt`** and optional **`--seconds-per-target 30`**.

**Detail-page downloads (default on):** after each list page (`/profile`, `/drafts`) finishes auto-scroll (and any `--seconds-per-target` wait), the script **scrolls the list again** until almost no new links appear, collects **every** unique detail URL it finds (any element with `[href]` on `sora.chatgpt.com`), sorts **`/p/…` posts first**, then opens **each** detail page and resolves URLs **only through the site’s Download flow** (⋯ → **Download**, `/backend/project_y/download/…`, visible **Download** controls, MP4/download events)—**not** from `<video>` preview streams unless you pass **`--detail-dom-fallback`**.

**No overview/grid streams (default):** **`--passive-media-capture` is off by default**, so scrolling the profile/drafts grid does **not** save preview video URLs—only URLs produced after opening each detail and using Download. Use **`--passive-media-capture`** if you want the old behavior (record all network media). By default **`--max-detail-pages 0` = no cap**. Set e.g. `--max-detail-pages 50` to limit.

On Sora **post** pages (`https://sora.chatgpt.com/p/…`), **Download** lives under the **overflow menu (⋯)**. If you see **`Download API returned 401`**, sign in (same `--profile-dir`) and run capture again.

If Cloudflare appears, run with **`--verification-prompt`**, solve it in the browser, then press `Enter` in the terminal (or fix the session in that profile first).

```powershell
python sora_downloader.py capture
```

First-time login (interactive), then reuse profile for unattended runs:

```powershell
python sora_downloader.py capture --verification-prompt --seconds-per-target 45
python sora_downloader.py capture
```

If the login UI still does not appear, try Edge or a longer load wait:

```powershell
python sora_downloader.py capture --browser-channel msedge --goto-wait-until load --login-timeout-ms 120000 --verification-prompt --seconds-per-target 120
```

Useful options:

```powershell
python sora_downloader.py capture --auto-max-seconds 20
python sora_downloader.py capture --auto-scroll-steps 80 --auto-step-wait-ms 800
python sora_downloader.py capture --no-auto-explore --seconds-per-target 30
```

Capture options:

- `--targets <url1> <url2> ...`  
  Pages to visit. Default is `profile` and `drafts`.
- `--seconds-per-target <int>`  
  Extra idle seconds after auto-explore per list target. Default: `0` (none).
- `--profile-dir <path>`  
  Persistent Chromium profile directory for login/session reuse. Default: `.playwright-profile`.
- `--login-timeout-ms <int>`  
  Timeout for opening each target/login page. Default: `60000` (60s).
  If timeout is reached, capture keeps the page open so you can still complete verification manually.
- `--nav-max-attempts <int>`  
  How many times to retry `goto` per URL (bootstrap, each list target, each detail) before skipping. Default: `4`.
- `--nav-retry-delay-ms <int>`  
  Pause between those retries. Default: `1000`.
- `--goto-wait-until <commit|domcontentloaded|load|networkidle>`  
  How long `goto` waits before continuing. Default: `domcontentloaded`. Use `load` if the login UI appears late.
- `--browser-channel <chromium|chrome|msedge>`  
  Engine: bundled Chromium or **installed** Chrome / Edge. Default: `chrome` (best for ChatGPT SSO flows).
- `--bootstrap-url <url>`  
  Opened once before targets; default `https://sora.chatgpt.com/login`.
- `--no-bootstrap`  
  Skip the bootstrap step.
- `--headless`  
  Run without visible browser window.
- `--verification-prompt`  
  Pause for Enter after bootstrap and each target (for login/Cloudflare). Default: **off** (unattended).
- `--no-auto-explore`  
  Disable automatic scrolling/pagination clicking.
- `--auto-scroll-steps <int>`  
  Max auto-explore scroll steps per page. Default: `40`.
- `--auto-step-wait-ms <int>`  
  Delay after each auto step. Default: `600`.
- `--auto-click-interval <int>`  
  Try clicking pagination controls every N steps. Default: `4`.
- `--auto-max-seconds <int>`  
  Hard time limit for auto-explore per page. Default: `30`.
- `--no-detail-download-links`  
  Do **not** visit detail pages (manifest will only get media if **`--passive-media-capture`** is on).
- `--passive-media-capture`  
  Record video URLs from **all** network traffic (grid + detail). Default: **off** (detail Download only).
- `--detail-dom-fallback`  
  On detail pages, if Download fails, also scrape `<video>` / links (may match **preview** streams like the grid).
- `--max-detail-pages <int>`  
  Max detail pages **per** list URL; **`0` = all** links found after gathering (default: `0`).
- `--detail-gather-max-rounds <int>`  
  Scroll rounds while discovering links on the list (default: `60`).
- `--detail-gather-idle-rounds <int>`  
  Stop gathering after this many rounds with no new links (default: `4`).
- `--detail-gather-pause-ms <int>`  
  Delay between gather scroll steps (default: `700`).
- `--detail-settle-ms <int>`  
  Wait after loading a detail page before scraping. Default: `2500`.
- `--detail-download-timeout-ms <int>`  
  How long to wait for a browser download after clicking Download. Default: `20000`.
- Global: `--output-dir <path>`  
  Base folder for manifests/downloads. Default: `output`.

Outputs:

- `output/manifests/capture-<timestamp>.json` (captured URLs; entries from detail pages include `detail_page` when applicable)
- `output/manifests/storage-state-<timestamp>.json` (auth/session state)

## 3) Download captured media

```powershell
python sora_downloader.py download
```

Optional:

```powershell
python sora_downloader.py download --manifest output/manifests/capture-YYYYMMDD-HHMMSS.json
python sora_downloader.py download --overwrite
python sora_downloader.py download --upload-dir upload
```

Download options:

- `--manifest <path>`  
  Use a specific capture manifest. Default: latest manifest in `output/manifests`.
- `--storage-state <path>`  
  Override auth/session state file used for requests.
- `--overwrite`  
  Replace existing files in the download folder.
- `--upload-dir <path>`  
  Write media files here. Default: `<output-dir>/downloads` (e.g. `output/downloads`). Use e.g. `upload` or `C:\Videos\SoraUpload` for your upload folder.
- `--timeout-ms <int>`  
  Per-request timeout. Default: `120000`.
- Global: `--output-dir <path>`  
  Base folder for manifests and default download location. Default: `output`.

Press `Ctrl+C` to stop either command early; the script exits gracefully and keeps partial progress.

Downloaded files go to `--upload-dir` if set, otherwise `output/downloads`.

## Notes

- If a URL expires, run `capture` again to refresh signed links.
- Duplicate downloads are automatically skipped using a stable media key (host + path), so refreshed signed URLs with different query params do not redownload.
- Some captures may include `.m3u8` playlists. This tool saves them as-is.
- You can customize target pages:

```powershell
python sora_downloader.py capture --targets https://sora.chatgpt.com/profile https://sora.chatgpt.com/drafts
```
