/* Uzs TV — Futbol: kunlik jadval (API) + Top futbolchilar.
   index.html oxirida, asosiy </script> dan keyin ulang:  <script src="fixtures-ui.js"></script> */
(function () {
  "use strict";
  Object.assign(D, {
    fxSched: ["Jadval", "Расписание"], fxLive: ["Efir", "Эфир"], fxTop: ["Top o'yinchilar", "Лучшие игроки"],
    fxNoLink: ["Translyatsiya hali qo'shilmagan", "Трансляция ещё не добавлена"],
    fxEmpty: ["Hozircha o'yin yo'q", "Матчей пока нет"], fxTom: ["Ertaga", "Завтра"],
    fxNoData: ["Ma'lumot hali yuklanmagan", "Данные ещё не загружены"]
  });
  let FX = { matches: [], scorers: {}, comps: {} }, tab = "sched", comp = "PL", got = 0;
  const origFootball = vFootball;

  async function loadFx(force) {
    if (!API_URL || !tg || !tg.initData) return;
    if (!force && Date.now() - got < 60000) return;
    got = Date.now();
    try {
      const r = await fetch(API_URL.replace(/\/$/, "") + "/api/fixtures", { headers: { "X-Init-Data": tg.initData } });
      if (!r.ok) return;
      const d = await r.json();
      const changed = JSON.stringify(d) !== JSON.stringify(FX);
      FX = d;
      if (changed && view === "football" && !cur) render();
    } catch (e) {}
  }

  const hm = iso => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dayKey = iso => new Date(iso).toDateString();

  function fxRow(x) {
    const live = x.status === "IN_PLAY" || x.status === "PAUSED";
    const fin = x.status === "FINISHED";
    const mid = x.score ? x.score : hm(x.time);
    const sub = live ? (x.minute ? (/^\d+$/.test(x.minute) ? x.minute + "'" : x.minute) : T("live")) : (fin ? T("ended") : x.league);
    const attr = x.item_id ? `data-open="${x.item_id}"` : `data-fxno="1"`;
    return `<div class="match" ${attr}>
      <div class="lgs">${logo(x.home_logo, x.home)}${logo(x.away_logo, x.away)}</div>
      <div class="mi"><div class="mt">${esc(x.home)} – ${esc(x.away)}</div>
        <div class="ms ${live ? "on" : ""}">${live ? ic("play", "s f") : ic("clock", "s")}${esc(mid)} · ${esc(sub)}${x.item_id ? "" : " · " + ic("flag", "s")}</div></div>
      ${x.item_id ? ic("chev", "s") : ""}</div>`;
  }

  function sched() {
    if (!FX.matches.length) return `<div class="empty">${ic("ball")}<div>${T("fxEmpty")}</div></div>`;
    const today = new Date().toDateString();
    const groups = {};
    FX.matches.forEach(x => (groups[dayKey(x.time)] = groups[dayKey(x.time)] || []).push(x));
    return Object.keys(groups).map(k => {
      const list = groups[k];
      const byLeague = {};
      list.forEach(x => (byLeague[x.league] = byLeague[x.league] || []).push(x));
      return `<div class="sec"><h2>${k === today ? T("today") : T("fxTom")}</h2></div>` +
        Object.keys(byLeague).map(l => `<div class="tl" style="color:var(--m);margin:10px 2px 6px;font-weight:700">${esc(l)}</div>${byLeague[l].map(fxRow).join("")}`).join("");
    }).join("");
  }

  function top() {
    const codes = Object.keys(FX.comps || {});
    const list = (FX.scorers || {})[comp] || [];
    const chips = `<div class="chips" style="padding:12px 0 0">${codes.map(c =>
      `<button class="chip ${c === comp ? "on" : ""}" data-fxcomp="${c}">${esc(FX.comps[c])}</button>`).join("")}</div>`;
    if (!list.length) return chips + `<div class="empty">${ic("star")}<div>${T("fxNoData")}</div></div>`;
    return chips + `<div class="list-g" style="margin-top:14px">${list.map((p, i) =>
      `<div class="lr"><div class="lv" style="width:20px">${i + 1}</div>${logo(p.crest, p.team)}
        <div class="lt"><b>${esc(p.name)}</b><div class="lv" style="margin-top:2px">${esc(p.team)}</div></div>
        <div style="text-align:right"><b>${p.goals}</b> <span class="lv">${T("goals")}</span><div class="lv" style="margin-top:2px">${p.assists} ${T("assists")}</div></div></div>`).join("")}</div>`;
  }
  Object.assign(D, { goals: ["gol", "голы"], assists: ["assist", "пас"] });

  window.vFootball = function (v) {
    loadFx();
    const bar = `<div class="seg">${[["sched", "fxSched"], ["live", "fxLive"], ["top", "fxTop"]].map(t =>
      `<button class="${tab === t[0] ? "on" : ""}" data-fxtab="${t[0]}">${T(t[1])}</button>`).join("")}</div>`;
    if (tab === "live") { origFootball(v); v.insertAdjacentHTML("afterbegin", bar); return; }
    v.innerHTML = bar + `<div class="wrap">${tab === "top" ? top() : sched()}</div>`;
  };

  document.addEventListener("click", e => {
    const t = e.target.closest("[data-fxtab]");
    if (t) { tab = t.dataset.fxtab; render(); return; }
    const c = e.target.closest("[data-fxcomp]");
    if (c) { comp = c.dataset.fxcomp; render(); return; }
    if (e.target.closest("[data-fxno]")) toast(T("fxNoLink"));
  });
  setInterval(() => { if (view === "football" && !cur && !document.hidden) loadFx(true); }, 120000);
  loadFx(true);
})();
