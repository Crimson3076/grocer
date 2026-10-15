/* Shared progress bar: shows what the grocery helper is doing, refreshes the page when something changes. */
(function () {
  const css = `
  #job { position: sticky; top: 0; z-index: 20; display: none;
    background: var(--card); border-bottom: 1px solid var(--line); box-shadow: 0 2px 10px rgba(0,0,0,.06); }
  #job .ji { max-width: 720px; margin: 0 auto; padding: 10px 16px; }
  #job.show { display: block; }
  #job .jt { display: flex; align-items: center; gap: 10px; font-size: 14px; }
  #job .jt b { font-weight: 600; }
  #job .js { color: var(--muted); font-size: 13px; margin-top: 2px; }
  #job .jbar { height: 6px; border-radius: 99px; background: var(--chip); overflow: hidden; margin-top: 8px; }
  #job .jbar i { display: block; height: 100%; width: 30%; background: var(--accent); border-radius: 99px; transition: width .4s; }
  #job .jbar.indef i { width: 30%; animation: slide 1.2s ease-in-out infinite; }
  #job.warn { background: var(--warn-bg); }
  #job.warn .jbar i { background: var(--warn); }
  #job.err { background: var(--warn-bg); color: var(--warn); }
  #job .x { margin-left: auto; }
  #job .spin { width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--chip); border-top-color: var(--accent);
    animation: spin .8s linear infinite; flex: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes slide { 0% { transform: translateX(-100%); } 100% { transform: translateX(340%); } }
  #loading { position: fixed; top: 0; left: 0; right: 0; height: 3px; z-index: 30; display: none; overflow: hidden; }
  #loading i { display: block; height: 100%; width: 30%; background: var(--accent); animation: slide 1s ease-in-out infinite; }
  html.loading #loading { display: block; }
  `;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);
  const loading = document.createElement("div");
  loading.id = "loading";
  loading.innerHTML = "<i></i>";
  document.body.appendChild(loading);

  const box = document.createElement("div");
  box.id = "job";
  document.body.insertBefore(box, document.body.firstChild);   // outside <main>, which pages redraw

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  let version = null, running = false, shownLast = null, timer = null;

  // a pause has an end time; show it ticking down between polls
  function countdown(until) {
    return until && until * 1000 > Date.now() ? ` · <b class="cd" data-until="${until}">${left(until)}</b>` : "";
  }
  function left(until) {
    const sec = Math.max(0, Math.round(until - Date.now() / 1000));
    return sec >= 60 ? `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")} left` : `${sec}s left`;
  }
  setInterval(() => {
    for (const el of document.querySelectorAll("#job .cd")) {
      const u = Number(el.dataset.until);
      el.textContent = u * 1000 > Date.now() ? left(u) : "continuing…";
    }
  }, 1000);

  function render(s) {
    const j = s.job;
    box.className = "";
    if (j) {
      const pct = j.total ? Math.round(100 * j.done / j.total) : null;
      let eta = "";
      if (j.total && j.done >= 2 && j.started) {
        const left = (Date.now() / 1000 - j.started) / j.done * (j.total - j.done);
        eta = left > 90 ? ` · about ${Math.round(left / 60)} min left` : left > 20 ? " · under 2 min left" : "";
      }
      box.className = "show" + (j.waiting ? " warn" : "");
      box.innerHTML = `<div class="ji"><div class="jt">${j.waiting ? "" : '<div class="spin"></div>'}<b>${esc(j.waiting ? "Walmart needs a quick check" : j.label)}</b>
          ${pct !== null ? `<span class="x">${j.done} of ${j.total}${eta}</span>` : ""}</div>
        <div class="js">${esc(j.waiting ? "Open the browser and press and hold the button. It carries on by itself after." : j.step)}${!j.waiting ? countdown(j.until) : ""}</div>
        ${j.waiting ? `<div style="margin-top:8px"><a class="btn primary" href="${esc(s.vnc_url)}" target="_blank">Open browser</a></div>`
                    : `<div class="jbar ${pct === null ? "indef" : ""}"><i style="${pct !== null ? `width:${Math.max(pct, 4)}%` : ""}"></i></div>`}</div>`;
      return;
    }
    const bg = s.bg;
    if (bg) {
      box.className = "show" + (bg.waiting ? " warn" : "");
      const more = bg.left ? `<span class="x">${bg.left} more</span>` : "";
      box.innerHTML = `<div class="ji"><div class="jt">${bg.waiting ? "" : '<div class="spin"></div>'}<b>${esc(bg.waiting ? "Walmart needs a quick check" : bg.label)}</b>${more}</div>
        <div class="js">${esc(bg.waiting ? "Open the browser and press and hold the button. It carries on by itself after." : bg.step)}${!bg.waiting ? countdown(bg.until) : ""}</div>
        ${bg.waiting ? `<div style="margin-top:8px"><a class="btn primary" href="${esc(s.vnc_url)}" target="_blank">Open browser</a></div>`
                     : `<div class="jbar indef"><i></i></div>`}</div>`;
      return;
    }
    const last = s.last;
    if (last && last.id !== shownLast && Date.now() / 1000 - (last.finished || 0) < 120) {
      shownLast = last.id;
      if (last.state === "failed") {
        box.className = "show err";
        box.innerHTML = `<div class="ji"><div class="jt"><b>${esc(last.label)} didn't finish</b><button class="x" onclick="this.closest('#job').className=''">OK</button></div>
          <div class="js" style="color:inherit">${esc(last.error)}</div></div>`;
        return;
      }
      if (last.message && window.toast) window.toast(last.message);
    }
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const s = await fetch("/api/status", { cache: "no-store" }).then(r => r.json());
      render(s);
      window.currentJob = s.job;
      if (window.onJob) window.onJob(s.job);
      if (version !== null && s.version !== version && window.onDataChanged) await window.onDataChanged();
      version = s.version;
      running = !!(s.job || s.bg);
    } catch (e) { /* server restarting; try again shortly */ }
    timer = setTimeout(poll, running ? 1500 : (document.hidden ? 30000 : 4000));
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

  // Start a long action; the bar shows its progress and the page refreshes when it's done.
  window.startJob = async function (path, body) {
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.statusText);
      if (j.job && j.job.already && window.toast) window.toast("Already working on something, it'll finish first");
      if (j.queued && window.toast) window.toast("Saved, it'll apply when the current update finishes");
    } catch (e) { if (window.toast) window.toast("Didn't work: " + e.message); }
    running = true;
    poll();
  };
  window.pollStatus = poll;

  // The three steps every main page shows at the top, plus the settings gear.
  const stepCss = document.createElement("style");
  stepCss.textContent = `
    .flow { display: flex; align-items: center; gap: 6px; margin: 2px 0 14px; }
    .flow a.s { flex: 1; text-align: center; text-decoration: none; font-size: 14px; font-weight: 600; color: var(--muted);
      padding: 8px 4px; border-radius: 12px; background: var(--chip); border: 1px solid var(--line); white-space: nowrap; }
    .flow a.s b { display: inline-grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; margin-right: 5px;
      background: var(--line); color: var(--ink); font-size: 12px; }
    .flow a.s.on { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
    .flow a.s.on b { background: var(--accent-ink); color: var(--accent); }
    .flow a.s.done b { background: var(--accent); color: var(--accent-ink); }
    .flow a.gear { text-decoration: none; font-size: 20px; padding: 6px 8px; border-radius: 12px; color: var(--muted); }`;
  document.head.appendChild(stepCss);
  window.steps = function (active) {
    const names = ["Dinners", "Check", "Send"], links = ["/dinners", "/review", "/review#send"];
    return `<nav class="flow" aria-label="Steps">${names.map((n, i) =>
      `<a class="s ${i + 1 === active ? "on" : i + 1 < active ? "done" : ""}" href="${links[i]}"><b>${i + 1 < active ? "✓" : i + 1}</b>${n}</a>`).join("")}
      <a class="gear" href="/settings" title="Settings and tools" aria-label="Settings">⚙</a></nav>`;
  };

  // Tap a product picture to see it big.
  // a <dialog> so it stacks above other pop-ups (like the Change sheet) instead of behind them
  const lb = document.createElement("dialog");
  lb.id = "lightbox";
  lb.innerHTML = '<figure><img alt=""><figcaption></figcaption></figure>';
  document.body.appendChild(lb);
  const lbStyle = document.createElement("style");
  lbStyle.textContent = `
    #lightbox { border: none; padding: 20px; margin: 0; width: 100vw; height: 100vh; max-width: 100vw; max-height: 100vh;
      background: transparent; display: none; place-items: center; cursor: zoom-out; }
    #lightbox[open] { display: grid; }
    #lightbox::backdrop { background: rgba(0,0,0,.72); }
    #lightbox figure { margin: 0; max-width: min(560px, 100%); text-align: center; }
    #lightbox img { width: 100%; max-height: 72vh; object-fit: contain; background: #fff; border-radius: 16px; }
    #lightbox figcaption { color: #fff; margin-top: 10px; font-size: 15px; line-height: 1.35; }
    img.thumb { cursor: zoom-in; }`;
  document.head.appendChild(lbStyle);
  window.showImage = function (src, caption) {
    const img = lb.querySelector("img");
    img.src = src;
    lb.querySelector("figcaption").textContent = caption || "";
    if (!lb.open) lb.showModal();
  };
  lb.onclick = () => lb.close();   // tap anywhere to close; Esc closes it too
  // any thumbnail with data-big opens the lightbox
  document.addEventListener("click", e => {
    const t = e.target.closest("img.thumb[data-big]");
    if (t) { e.preventDefault(); window.showImage(t.dataset.big, t.dataset.caption); }
  });
  poll();
})();
