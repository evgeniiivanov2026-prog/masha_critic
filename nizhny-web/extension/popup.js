const baseInput = document.getElementById("base");
const includeInput = document.getElementById("include");
const msg = document.getElementById("msg");
const out = document.getElementById("out");

function n2(v) { return Number(v).toFixed(2); }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

chrome.storage.local.get(["base"], (saved) => {
  if (saved && saved.base) baseInput.value = saved.base;
});

function extractPage() {
  const heading = document.querySelector("h1");
  const title = ((heading && heading.innerText) || document.title || "").trim();
  const root = document.querySelector("article") || document.querySelector("main") || document.body;
  const body = ((root && root.innerText) || "").trim().slice(0, 50000);
  return { title, body, url: location.href };
}

document.getElementById("go").addEventListener("click", async () => {
  msg.textContent = "Читаю вкладку…";
  out.innerHTML = "";
  const base = baseInput.value.replace(/\/$/, "");
  chrome.storage.local.set({ base });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) {
    msg.textContent = "Нет активной вкладки";
    return;
  }
  let extracted;
  try {
    const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractPage });
    extracted = result && result.result;
  } catch (e) {
    msg.textContent = "Эту вкладку нельзя прочитать (служебная страница браузера)";
    return;
  }
  if (!extracted || !(extracted.body || extracted.title)) {
    msg.textContent = "На странице не нашлось текста";
    return;
  }
  msg.textContent = "Считаю на сервере…";
  let res;
  try {
    res = await fetch(base + "/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: extracted.title,
        body: extracted.body,
        url: extracted.url,
        include_url: includeInput.checked
      })
    });
  } catch (e) {
    msg.textContent = "Локальный сервер не отвечает. Запустите uvicorn в каталоге nizhny-web.";
    return;
  }
  let data;
  try { data = await res.json(); }
  catch (e) {
    msg.textContent = "Сервер вернул не JSON";
    return;
  }
  if (!res.ok) {
    msg.textContent = data.error || "Запрос отклонён";
    return;
  }
  const mode = data.url_included ? "URL страницы учтён" : "режим CLI, URL не подмешивается";
  const caps = (data.caps || []).map((c) => `<span class="pill">${esc(c)}</span>`).join("") || `<span class="pill">потолки не сработали</span>`;
  out.innerHTML = `
    <div class="score">${n2(data.score)}</div>
    <div>${esc(data.verdict)}</div>
    <div class="muted">${esc(mode)} · тело ${(data.body || "").length} симв.</div>
    <div>${caps}</div>
    <ul>
      <li>credibility ${n2(data.credibility)}</li>
      <li>data_availability ${n2(data.data_availability)}</li>
      <li>base_quality ${n2(data.base_quality)}</li>
    </ul>`;
  msg.textContent = "";
});
