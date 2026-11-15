# Playtech All Bets Desktop Companion

Read-only Windows desktop companion for feeding live card events into [`blackjack-tracker/index.html`](../index.html) over `ws://127.0.0.1:8765`.

## Setup

```powershell
cd "c:\Users\James Ong\Documents\antigravity\self-learning\blackjack-tracker\companion"
uv sync
```

## 1. Recommended In-Browser Calibration (Zero Cut-Off, Auto 16:9 Frame Anchor)

Open Newport / Playtech *All Bets Blackjack* in Brave/Chrome on the left half of your screen and [`index.html`](../index.html) on the right half, then start the companion:

```powershell
uv run python companion.py
```

Inside [`index.html`](../index.html), click **`🎯 Calibrate`** in the Control Bar:
1. Click **`📸 Fresh Snapshot`** — automatically detects the Playtech 16:9 game frame (ignoring Brave tabs, bookmarks, sidebar, and black margins) and fits the image inside your tracker tab.
2. In **`1. ROI Boxes`**, select `player_strip`, `dealer_strip`, `community_roi`, `shuffle_roi`, `banner_roi`, drag directly on the snapshot, and click **`💾 Save ROIs`**.
3. Switch to **`2. Crop Card & Banner Templates`**, drag a tight box around each card's top-left rank+suit corner (check the 4x zoom loupe), and click its rank pill (`A`, `2`–`9`, `10`, `J`, `Q`, `K`, `BACK`, `SHUF`). Crop `HUD (P/D)` cards first, plus `Felt [B]` cards from `community_roi` for post-Stand / glitched hits, and `Banner` tags (`WAIT`, `LAST_BETS`, `NO_MORE_BETS`, `PLACE_BETS`).
4. Click **`🧪 Test Match on Snapshot`** to verify green detection boxes & confidence scores before closing.

*(Optional CLI fallback: `uv run python calibrate.py` and `uv run python calibrate.py --harvest hud` also work with fit-to-screen scaling.)*

## 2. Live Tracking Usage

```powershell
uv run python companion.py
```

Add `--preview` only if you want a separate scaled debug overlay window (`Q` or `Esc` to close).

- **Betting Period (`PLACE YOUR BETS`, `LAST BETS`):** Card recording is strictly locked out. Incoming false triggers or chip motions are suppressed so counting stays pure.
- **Round Start (`NO MORE BETS`):** Deals begin with clean debouncers.
- **WATCH Mode (`₱0`):** P/D cards auto-fill live, and when the round ends (`WAIT FOR THE NEXT ROUND`), rounds auto-record into history immediately without requiring manual clicks.
- **BET Mode / Splits / Blurred Felt Card:** P/D/B cards auto-fill live, then auto-logging prompts with a review banner so you confirm split hands or bet decisions.

## 3. Running Tests

```powershell
uv run pytest -v
node ipad/test_engine.js
```

---

## 4. Standalone iPadOS + Safari (Split View) Setup

The tracker can run **100% standalone on an iPad** alongside Playtech / Newport in **Safari** using iPadOS **Split View** (Safari on the left, `BJ Tracker` app on the right) with **zero DOM injection, zero content scripts, and zero network hooks**.

### How Anti-Detection Works on iPadOS
1. **OS-Level ReplayKit Screen Broadcast (`RPBroadcastSampleHandler`)**: Captures hardware-composited screen frames at the OS level (`BroadcastSampleHandler.swift`), downscales to $\le 1280\text{px}$ width under 12 MB RAM, and writes to a lock-free memory-mapped App Group ring buffer (`SharedFrameRing.swift`). Safari WebKit does **not** expose `UIScreen.isCaptured` to web page JavaScript, and Playtech WebRTC/HLS canvas streams are not FairPlay DRM-encrypted.
2. **Continuous Foreground Visibility via Split View**: Running Safari (left pane) and `BJ Tracker` (right pane) in iPadOS **Split View** keeps both apps active in the foreground (`document.visibilityState === "visible"`), preventing background tab throttling or visibility-change telemetry.
3. **Split-View Left-Pane 16:9 Auto-Isolation (`ipad_split_left`)**: Automatically detects the vertical black iPadOS Split View divider, crops to the left Safari pane, and isolates the 16:9 Playtech game frame (`detectSplitViewLeftPane` in `companion/ipad/companion_engine.js` and `detect_split_view_left_pane` in `companion/win_anchor.py`).
4. **Zero-DOM Safari Keep-Alive Extension**: Uses `browser.tabs.reload(tabId, { bypassCache: false })` only (`permissions: ["tabs", "alarms"]`, zero `content_scripts`, zero `host_permissions`), with automatic spoken/visual alert fallback during `PLACE_BETS` if Safari suspends the extension background worker.

### Step-by-Step iPad Installation (No Mac Required)
1. **Download `BlackjackTracker.ipa`**:
   - Open your GitHub repository → **Actions** tab → **Build iPad Tracker IPA** workflow → download the **`BlackjackTracker-iPad-IPA`** artifact and unzip `BlackjackTracker.ipa` (or build locally on a Mac with `cd companion/ipad/ios-app && xcodegen generate && xcodebuild`).
2. **Sideload onto your iPad (from Windows or Mac)**:
   - Open **Sideloadly** or **AltStore** on your PC/Mac, connect your iPad, and install `BlackjackTracker.ipa` using your Apple ID.
   - On iPad, go to **Settings → General → VPN & Device Management** and trust your developer certificate, then enable **Settings → Privacy & Security → Developer Mode**.
3. **Enable the Safari Keep-Alive Extension (Optional)**:
   - On iPad, go to **Settings → Safari → Extensions → Newport Keep-Alive (Safari)** and toggle it **On**.
4. **Launch in iPad Split View**:
   - Open **Safari** on the **left side** of your iPad screen and navigate to the Newport / Playtech *All Bets Blackjack* table.
   - Drag **BJ Tracker** from the Dock to the **right side** of the screen in **Split View** (e.g., 2/3 Safari on the left, 1/3 Tracker on the right).
   - In the Tracker top bar, tap **`📺 Broadcast Screen`** → select **`BJ Tracker Broadcast`** → tap **Start Broadcast**.
5. **Calibrating or Syncing Templates (`📦 Export Pack` / `📥 Import Pack`)**:
   - All 78 repository templates (`hud`, `felt`, `banner`, `suit`, `ui`) are pre-bundled out of the box in `companion/ipad/default_pack.js`.
   - To transfer custom crops between Windows and iPad, open **`🎯 Calibrate`** and click **`📦 Export Pack`** / **`📥 Import Pack`**.

