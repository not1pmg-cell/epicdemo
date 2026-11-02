/* Uzs TV — Futbol: kunlik jadval (API) + turnir tablitsasi + Top futbolchilar + jamoani kuzatish.
   index.html oxirida, asosiy </script> dan keyin ulang:  <script src="fixtures-ui.js"></script> */
(function () {
  "use strict";
  Object.assign(D, {
    fxSched: ["Jadval", "Расписание"], fxLive: ["Efir", "Эфир"], fxTop: ["Top o'yinchilar", "Лучшие игроки"],
    fxTable: ["Tablitsa", "Таблица"],
    fxNoLink: ["Translyatsiya hali qo'shilmagan", "Трансляция ещё не добавлена"],
    fxEmpty: ["Hozircha o'yin yo'q", "Матчей пока нет"], fxTom: ["Ertaga", "Завтра"],
    fxNoData: ["Ma'lumot hali yuklanmagan", "Данные ещё не загружены"],
    goals: ["gol", "голы"], assists: ["assist", "пас"],
    fxP: ["O", "И"], fxGD: ["FA", "РМ"], fxPts: ["Ochko", "Очки"],
    fxErrUrl: ["index.html dagi API_URL to'g'rilanmagan", "API_URL в index.html не настроен"],
    fxErrTg: ["Mini App Telegram ichida ochilishi kerak", "Откройте Mini App внутри Telegram"],
    fxErrSrv: ["Serverga ulanib bo'lmadi (manzil yoki CORS)", "Нет связи с сервером (адрес или CORS)"],
    fxErrHttp: ["Server javobi", "Ответ сервера"]
  });
  let FX = { matches: [], scorers: {}, tables: {}, comps: {} }, tab = "sched", comp = "PL", got = 0, fxErr = "";
  const origFootball = vFootball;

  function rerender() { if (view === "football" && !cur) render(); }

  async function loadFx(force) {
    if (!API_URL || /SIZNING/i.test(API_URL)) { fxErr = T("fxErrUrl"); return; }
    if (!tg || !tg.initData) { fxErr = T("fxErrTg"); return; }
    if (!force && Date.now() - got < 60000) return;
    got = Date.now();
    try {
      const r = await fetch(API_URL.replace(/\/$/, "") + "/api/fixtures", { headers: { "X-Init-Data": tg.initData } });
      if (!r.ok) { fxErr = T("fxErrHttp") + ": " + r.status; rerender(); return; }
      const d = await r.json();
      fxErr = "";
      const changed = JSON.stringify(d) !== JSON.stringify(FX);
      FX = Object.assign({ matches: [], scorers: {}, tables: {}, comps: {} }, d);
      if (changed) rerender();
    } catch (e) { fxErr = T("fxErrSrv"); rerender(); }
  }

  const hm = iso => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dayKey = iso => new Date(iso).toDateString();
  const empty = (icon, msg) => `<div class="empty">${ic(icon)}<div>${esc(msg)}</div></div>`;

  function fxRow(x) {
    const live = x.status === "IN_PLAY" || x.status === "PAUSED";
    const fin = x.status === "FINISHED";
    const mid = x.score ? x.score : hm(x.time);
    const sub = live ? (x.minute ? (/^\d+$/.test(x.minute) ? x.minute + "'" : x.minute) : T("live")) : (fin ? T("ended") : x.league);
    const attr = x.item_id ? `data-open="${x.item_id}"` : `data-fxno="1"`;
    const stars = [x.home, x.away].map(n =>
      `<span class="chip ${teams.has(n) ? "on" : ""}" data-fxf="${esc(n)}" style="padding:3px 8px;font-size:11px">${ic("star", "s")} ${esc(n)}</span>`).join("");
    return `<div class="match" ${attr} style="align-items:flex-start">
      <div class="lgs" style="margin-top:2px">${logo(x.home_logo, x.home)}${logo(x.away_logo, x.away)}</div>
      <div class="mi"><div class="mt">${esc(x.home)} – ${esc(x.away)}</div>
        <div class="ms ${live ? "on" : ""}">${live ? ic("play", "s f") : ic("clock", "s")}${esc(mid)} · ${esc(sub)}${x.item_id ? "" : " · " + ic("flag", "s")}</div>
        <div style="margin-top:7px;display:flex;gap:6px;flex-wrap:wrap">${stars}</div></div>
      ${x.item_id ? ic("chev", "s") : ""}</div>`;
  }

  function sched() {
    if (!FX.matches.length) return empty("ball", fxErr || T("fxEmpty"));
    const today = new Date().toDateString();
    const groups = {};
    FX.matches.forEach(x => (groups[dayKey(x.time)] = groups[dayKey(x.time)] || []).push(x));
    return (fxErr ? `<div class="notice">${ic("flag")}<div class="nt">${esc(fxErr)}</div></div>` : "") +
      Object.keys(groups).map(k => {
        const byLeague = {};
        groups[k].forEach(x => (byLeague[x.league] = byLeague[x.league] || []).push(x));
        return `<div class="sec"><h2>${k === today ? T("today") : T("fxTom")}</h2></div>` +
          Object.keys(byLeague).map(l => `<div class="tl" style="color:var(--m);margin:10px 2px 6px;font-weight:700">${esc(l)}</div>${byLeague[l].map(fxRow).join("")}`).join("");
      }).join("");
  }

  function compChips() {
    const codes = Object.keys(FX.comps || {});
    return `<div class="chips" style="padding:12px 0 0">${codes.map(c =>
      `<button class="chip ${c === comp ? "on" : ""}" data-fxcomp="${c}">${esc(FX.comps[c])}</button>`).join("")}</div>`;
  }

  function top() {
    const list = (FX.scorers || {})[comp] || [];
    if (!list.length) return compChips() + empty("star", fxErr || T("fxNoData"));
    return compChips() + `<div class="list-g" style="margin-top:14px">${list.map((p, i) =>
      `<div class="lr"><div class="lv" style="width:20px">${i + 1}</div>${logo(p.crest, p.team)}
        <div class="lt"><b>${esc(p.name)}</b><div class="lv" style="margin-top:2px">${esc(p.team)}</div></div>
        <div style="text-align:right"><b>${p.goals}</b> <span class="lv">${T("goals")}</span><div class="lv" style="margin-top:2px">${p.assists} ${T("assists")}</div></div></div>`).join("")}</div>`;
  }

  function table() {
    const list = (FX.tables || {})[comp] || [];
    if (!list.length) return compChips() + empty("hash", fxErr || T("fxNoData"));
    const head = `<div class="lr" style="padding:8px 14px"><div class="lv" style="width:22px">#</div><div class="lt lv" style="font-size:12px"></div>
      <div class="lv" style="width:26px;text-align:center">${T("fxP")}</div><div class="lv" style="width:34px;text-align:center">${T("fxGD")}</div><div class="lv" style="width:34px;text-align:center">${T("fxPts")}</div></div>`;
    return compChips() + `<div class="list-g" style="margin-top:14px">${head}${list.map(r =>
      `<div class="lr" style="padding:10px 14px"><div class="lv" style="width:22px">${r.pos}</div>${logo(r.crest, r.team)}
        <div class="lt" style="font-size:13px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.team)}</div>
        <div class="lv" style="width:26px;text-align:center">${r.played}</div>
        <div class="lv" style="width:34px;text-align:center">${r.gd > 0 ? "+" + r.gd : r.gd}</div>
        <b style="width:34px;text-align:center">${r.pts}</b></div>`).join("")}</div>`;
  }

  window.vFootball = function (v) {
    loadFx();
    const bar = `<div class="seg">${[["sched", "fxSched"], ["live", "fxLive"], ["table", "fxTable"], ["top", "fxTop"]].map(t =>
      `<button class="${tab === t[0] ? "on" : ""}" data-fxtab="${t[0]}">${T(t[1])}</button>`).join("")}</div>`;
    if (tab === "live") { origFootball(v); v.insertAdjacentHTML("afterbegin", bar); return; }
    const body = tab === "top" ? top() : tab === "table" ? table() : sched();
    v.innerHTML = bar + `<div class="wrap">${body}</div>`;
  };

  /* jamoa yulduzchasi: boshqa handlerlardan oldin (capture) ishlaydi */
  document.addEventListener("click", e => {
    const f = e.target.closest("[data-fxf]");
    if (!f) return;
    e.stopPropagation();
    const n = f.dataset.fxf;
    if (teams.has(n)) { teams.delete(n); toast(T("unfollow")); syncTeam(n, false); }
    else { teams.add(n); toast(T("follow")); syncTeam(n, true); }
    ls.set("uzstv_teams", [...teams]);
    hap();
    render();
  }, true);

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
