Companion Tab Keep-Alive Bridge (Brave MV3 Extension)
=========================================================

Why this exists:
- Uses ONLY `tabs` and `alarms` permissions (no webpage/DOM script access).
- Triggers native `chrome.tabs.reload(tabId)` directly on the Playtech tab
  even if the left Brave window is not focused/clicked.

1-Time Setup in Brave (~15 seconds):
1. Open `brave://extensions`
2. Enable "Developer mode" (top right toggle)
3. Click "Load unpacked"
4. Select this folder:
   blackjack-tracker\companion\brave_keepalive_ext
5. Confirm its toolbar/extension badge turns green ("ON") when `companion.py` is running.
