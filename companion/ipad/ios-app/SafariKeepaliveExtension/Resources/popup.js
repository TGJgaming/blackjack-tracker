const api = typeof browser !== "undefined" ? browser : chrome;
const st = document.getElementById("st");
const tabsEl = document.getElementById("tabs");
function ask(msg) {
  return new Promise((resolve) => {
    try {
      const r = api.runtime.sendMessage(msg, (res) => resolve(res));
      if (r && typeof r.then === "function") r.then(resolve, () => resolve(null));
    } catch (_) { resolve(null); }
  });
}
(async () => {
  try {
    const tabs = await api.tabs.query({});
    const hasAccess = tabs.length === 0 || tabs.some((t) => Boolean((t && t.url) || (t && t.title)));
    if (hasAccess) {
      st.textContent = "✅ Extension active";
      tabsEl.textContent = tabs.length + " tab(s) visible to the extension";
    } else {
      st.textContent = "⚠️ Website access not granted yet";
      tabsEl.textContent = "Tap aA → Manage Extensions → Newport Keep-Alive → Always Allow on Every Website.";
    }
  } catch (e) {
    st.textContent = "⚠️ Website access not granted yet";
    tabsEl.textContent = "Tap aA → Manage Extensions → Newport Keep-Alive → Always Allow.";
  }
})();
document.getElementById("reload").onclick = async () => {
  const res = await ask({ type: "popup_reload" });
  st.textContent = res && res.ok ? "✅ Reload sent" : "Reload requested";
};
