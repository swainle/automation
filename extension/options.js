import {validateEndpoint} from "./protocol.mjs";

const endpoint = document.querySelector("#endpoint");
const message = document.querySelector("#message");
const saved = await chrome.storage.local.get(["endpoint"]);
endpoint.value = saved.endpoint || "ws://127.0.0.1:8765/";
async function updateStatus() {
  const data = await chrome.storage.session.get("bridgeStatus");
  document.querySelector("#status").textContent = data.bridgeStatus || "未连接";
}
document.querySelector("#config").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const value = validateEndpoint(endpoint.value.trim());
    await chrome.storage.local.set({endpoint: value});
    message.textContent = "配置已保存，正在连接。";
  } catch (error) { message.textContent = error.message; }
});
chrome.storage.onChanged.addListener((_changes, area) => { if (area === "session") void updateStatus(); });
await updateStatus();
