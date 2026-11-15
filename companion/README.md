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
```
