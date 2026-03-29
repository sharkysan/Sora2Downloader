# Sora Profile/Drafts Downloader

Downloads media URLs discovered from:

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
                                  [--headless]
                                  [--skip-verification-prompt]
                                  [--no-auto-explore]
                                  [--auto-scroll-steps AUTO_SCROLL_STEPS]
                                  [--auto-step-wait-ms AUTO_STEP_WAIT_MS]
                                  [--auto-click-interval AUTO_CLICK_INTERVAL]
                                  [--auto-max-seconds AUTO_MAX_SECONDS]
```

Download:

```text
usage: sora_downloader.py download [-h] [--manifest MANIFEST]
                                   [--storage-state STORAGE_STATE]
                                   [--overwrite] [--timeout-ms TIMEOUT_MS]
```

## 2) Capture media URLs

This opens a Chromium window with a persistent profile. It now auto-scrolls and attempts pagination clicks (`Load more`, `Next`, etc.) while capturing network requests.

If Cloudflare appears, solve it in the browser window and press `Enter` in the terminal when done.

```powershell
python sora_downloader.py capture --seconds-per-target 30
```

Useful options:

```powershell
python sora_downloader.py capture --auto-max-seconds 20
python sora_downloader.py capture --auto-scroll-steps 80 --auto-step-wait-ms 800
python sora_downloader.py capture --no-auto-explore --seconds-per-target 90
python sora_downloader.py capture --skip-verification-prompt
```

Capture options:

- `--targets <url1> <url2> ...`  
  Pages to visit. Default is `profile` and `drafts`.
- `--seconds-per-target <int>`  
  Extra manual interaction time after auto-explore. Default: `20`.
- `--profile-dir <path>`  
  Persistent Chromium profile directory for login/session reuse. Default: `.playwright-profile`.
- `--login-timeout-ms <int>`  
  Timeout for opening each target/login page. Default: `60000` (60s).
- `--headless`  
  Run without visible browser window.
- `--skip-verification-prompt`  
  Do not pause for Enter after each page opens (default is to pause for manual verification in visible mode).
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
- Global: `--output-dir <path>`  
  Base folder for manifests/downloads. Default: `output`.

Outputs:

- `output/manifests/capture-<timestamp>.json` (captured URLs)
- `output/manifests/storage-state-<timestamp>.json` (auth/session state)

## 3) Download captured media

```powershell
python sora_downloader.py download
```

Optional:

```powershell
python sora_downloader.py download --manifest output/manifests/capture-YYYYMMDD-HHMMSS.json
python sora_downloader.py download --overwrite
```

Download options:

- `--manifest <path>`  
  Use a specific capture manifest. Default: latest manifest in `output/manifests`.
- `--storage-state <path>`  
  Override auth/session state file used for requests.
- `--overwrite`  
  Replace existing files in `output/downloads`.
- `--timeout-ms <int>`  
  Per-request timeout. Default: `120000`.
- Global: `--output-dir <path>`  
  Base folder for manifests/downloads. Default: `output`.

Press `Ctrl+C` to stop either command early; the script exits gracefully and keeps partial progress.

Downloaded files are written to `output/downloads`.

## Notes

- If a URL expires, run `capture` again to refresh signed links.
- Duplicate downloads are automatically skipped using a stable media key (host + path), so refreshed signed URLs with different query params do not redownload.
- Some captures may include `.m3u8` playlists. This tool saves them as-is.
- You can customize target pages:

```powershell
python sora_downloader.py capture --targets https://sora.chatgpt.com/profile https://sora.chatgpt.com/drafts
```
