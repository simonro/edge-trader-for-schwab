// Trading journal sync (Trading Journal AI): a journal entry per trade, a day note, and the
// two-step Sync. Entries save on this computer as you type; nothing reaches the journal until
// you press Sync, read the preview and click Apply. Nothing here places, changes or cancels an
// order. Off until a journal address and at least one account are set (Journal settings).
// Uses the page's helpers ($, esc, money, tET, SEL, VIEW, lastState, render).
(() => {
  const FIELD_LABEL = {strategy: "Strategy", idea_source: "Source", emotional_state: "Emotional state", mistakes: "Mistakes",
    entry_reason: "Entry reason", exit_reason: "Exit reason", stop_loss: "Stop", target_price: "Target",
    risk_per_trade: "Risk $", risk_reward: "Planned R:R", r_multiple: "Realized R"};
  const EMOTIONS = ["calm", "anxious", "overconfident", "disciplined", "frustrated", "revenge"];
  const ROUTES = [["STOCK", "Stocks"], ["OPTION", "Options"], ["FUTURE", "Futures"]];
  const J = {status: null, lib: null, libErr: null, libAt: 0, libBusy: false, accounts: null, info: null,
             tab: "sync", draft: null, preview: null, busy: null, msg: null, applied: null,
             posOpen: false, closedOpen: new Set(), eds: new Map(), day: null};
  try { J.posOpen = localStorage.getItem("jrPosOpen") === "1"; } catch (e) {}

  // ---- helpers ------------------------------------------------------------------------------
  const ETZ = {timeZone: "America/New_York"};
  const todayET = () => new Date().toLocaleDateString("en-CA", ETZ);
  const parseTs = s => { if (!s) return null; const d = new Date(String(s).replace(/([+-]\d\d)(\d\d)$/, "$1:$2")); return isNaN(d) ? null : d; };
  const fmtDT = s => { const d = parseTs(s); return d ? d.toLocaleString("en-US", {...ETZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : (s ? String(s) : "-"); };
  const fmtD = s => { const d = parseTs(s); return d ? d.toLocaleDateString("en-US", {...ETZ, month: "short", day: "numeric", year: "numeric"}) : (s ? String(s) : "-"); };
  const noteTime = s => { const d = parseTs(s); if (!d) return ""; return d.toLocaleDateString("en-CA", ETZ) === todayET() ? tET(s) : fmtDT(s); };
  const annKey = ids => ids && ids.length ? "t" + String(ids[0]).split(":")[0] : null;
  const isLocal = url => {
    let h = "";
    try { h = new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, ""); } catch (e) { return true; }   // not a URL yet: no checkbox
    return h === "localhost" || /^127\.\d+\.\d+\.\d+$/.test(h) || h === "::1";   // the server's rule
  };
  const plural = (n, w, pl) => { n = Number(n) || 0; return `${n} ${n === 1 ? w : pl || w + "s"}`; };
  const int = v => Number.isFinite(Number(v)) ? Math.trunc(Number(v)) : 0;      // a count or an id from the journal
  const pick = (v, allowed) => allowed.includes(String(v)) ? String(v) : "";    // a class name from the journal

  // Every POST carries the companion header; an error comes back as {ok: false, error} so the
  // caller always has a message to show. `lost` marks "no answer", where the outcome is unknown.
  async function jpost(url, body, ms = 20000, keepalive = false) {
    const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-Companion": "1"},
        body: JSON.stringify(body || {}), signal: ctl.signal, keepalive});
      let d = null;
      try { d = await r.json(); } catch (e) { /* not JSON */ }
      if (d && typeof d.ok === "boolean") return d;
      return {ok: false, error: `Edge Trader answered HTTP ${r.status}${d && d.detail ? ": " + d.detail : ""}.`};
    } catch (e) {
      return {ok: false, lost: true, error: e.name === "AbortError" ? `No answer from Edge Trader within ${Math.round(ms / 1000)} s.`
                                                                   : "Edge Trader is not reachable."};
    } finally { clearTimeout(t); }
  }
  async function jget(url) {
    try { const r = await fetch(url, {cache: "no-store"}); return r.ok ? await r.json() : null; } catch (e) { return null; }
  }

  // ---- styles -------------------------------------------------------------------------------
  const css = document.createElement("style");
  css.textContent = `
    .jr-hb { display: inline-flex; gap: 6px; white-space: nowrap; }
    .jr-hb .dot { margin: 0 0 0 2px; }
    #jr-pos { border-top: 1px solid var(--line-hi); background: #121924; }
    .jr-posh { display: flex; align-items: center; gap: 10px; padding: 9px 16px 9px 13px; cursor: pointer; user-select: none; min-height: 42px;
      border-left: 3px solid var(--blue); }
    .jr-posh:hover { background: #172131; }
    .jr-posh:focus-visible { outline: 2px solid var(--blue); outline-offset: -2px; }
    .jr-posh .jr-title { font: 600 12.5px var(--sans); letter-spacing: .06em; text-transform: uppercase; color: var(--text); }
    .jr-posh .jr-open { margin-left: auto; flex: none; height: 26px; padding: 0 11px; border-radius: 6px; border: 1px solid var(--line-hi);
      background: #0a0d12; color: var(--text); font: 600 12px var(--sans); display: inline-flex; align-items: center; gap: 6px; }
    .jr-posh:hover .jr-open { border-color: var(--blue); }
    .jr-sum { flex: 1; min-width: 0; font-size: 12.5px; color: #aab6c5; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .jr-sum b { color: var(--text); font-weight: 600; }
    .jr-posbody { padding: 2px 16px 14px; }
    .jr-wait { font-size: 12.5px; color: var(--amber); padding: 4px 0; }
    .jr-ed { display: grid; gap: 10px; font-size: 13px; white-space: normal; }
    .jr-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 8px 10px; }
    .jr-grid.wide { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
    .jr-f { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .jr-in, .jr-ta, .jr-sel { width: 100%; min-width: 0; border-radius: 6px; border: 1px solid var(--line-hi); background: #0a0d12;
      color: var(--text); font: 13px var(--sans); }
    .jr-in, .jr-sel { height: 32px; padding: 0 9px; }
    .jr-ta { padding: 7px 9px; min-height: 58px; resize: vertical; line-height: 1.4; display: block; }
    .jr-in.mono { font-family: var(--mono); }
    .jr-in:focus, .jr-ta:focus, .jr-sel:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(76,141,255,.2); }
    .jr-in::placeholder, .jr-ta::placeholder { color: var(--dim); }
    .jr-ms { position: relative; }
    .jr-msbox { width: 100%; min-height: 32px; border-radius: 6px; border: 1px solid var(--line-hi); background: #0a0d12; color: var(--text);
      display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 3px 28px 3px 6px; text-align: left; cursor: pointer;
      font: 13px var(--sans); position: relative; }
    .jr-msbox::after { content: "▾"; position: absolute; right: 9px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 11px; }
    .jr-msbox:focus-visible, .jr-ms.open .jr-msbox { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(76,141,255,.2); }
    .jr-msbox .ph { color: var(--dim); padding-left: 3px; }
    .jr-mschip { height: 22px; padding: 0 8px; border-radius: 11px; background: var(--red-bg); border: 1px solid rgba(229,83,75,.5);
      color: #ffb0aa; font-size: 12px; display: inline-flex; align-items: center; white-space: nowrap; }
    .jr-msmenu { position: absolute; z-index: 30; left: 0; right: 0; top: calc(100% + 4px); max-height: 260px; overflow-y: auto; min-width: 220px;
      background: #0f141b; border: 1px solid var(--line-hi); border-radius: 8px; box-shadow: 0 10px 28px rgba(0,0,0,.5); padding: 4px; }
    .jr-msopt { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 5px; cursor: pointer; font-size: 13px; }
    .jr-msopt:hover { background: #172131; }
    .jr-msopt input { accent-color: var(--red); margin: 0; }
    .jr-msnew { margin: 4px 2px 2px; width: calc(100% - 4px); }
    .jr-notes { display: grid; gap: 0; }
    .jr-note { display: grid; grid-template-columns: 70px minmax(0, 1fr) auto; gap: 8px; align-items: start; padding: 6px 0; border-top: 1px solid var(--line); }
    .jr-note .t { font: 12px var(--mono); color: var(--muted); padding-top: 1px; }
    .jr-nt { white-space: pre-wrap; word-break: break-word; }
    .jr-note .b { display: flex; gap: 5px; }
    .jr-noteadd { display: flex; gap: 8px; align-items: flex-end; margin-top: 6px; }
    .jr-noteadd .jr-ta { min-height: 38px; }
    .jr-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
    .jr-foot .ok { color: var(--green); } .jr-foot .bad { color: var(--red); }
    .jr-libwarn { font-size: 12px; color: var(--amber); }
    #closed.jr-grow { height: auto; min-height: 210px; max-height: none; }
    #closed tr.jr-tr > td { height: auto; padding: 10px 12px 12px; background: var(--panel2); white-space: normal; }
    .jr-cb { height: 20px; padding: 0 7px; margin-left: 6px; border-radius: 4px; border: 1px solid var(--line-hi); background: var(--panel2);
      color: var(--muted); font: 600 11px var(--sans); cursor: pointer; }
    .jr-cb:hover, .jr-cb.on { color: var(--text); border-color: var(--blue); }
    .jr-cb.has::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--green); margin-right: 5px; vertical-align: 1px; }
    .jr-cw { margin-left: 6px; font-size: 11px; color: var(--amber); }
    .jr-modal { position: fixed; inset: 0; z-index: 50; background: rgba(4,6,9,.66); display: flex; align-items: flex-start;
      justify-content: center; padding: 44px 12px 12px; }
    .jr-modal[hidden] { display: none; }
    .jr-box { width: min(860px, 100%); max-height: calc(100vh - 56px); box-shadow: 0 18px 50px rgba(0,0,0,.6); }
    .jr-box > .ph { flex-wrap: wrap; row-gap: 6px; }
    .jr-box > .ph .seg { margin-left: 6px; }
    .jr-box > .ph .seg button { height: 24px; font-size: 12px; padding: 0 10px; }
    .jr-x { margin-left: auto; }
    .jr-body { overflow-y: auto; padding: 12px 16px 16px; display: flex; flex-direction: column; gap: 12px; font-size: 13px; }
    .jr-body > * { flex: none; }
    .jr-sec { display: grid; gap: 6px; }
    .jr-sec.jr-intro { display: block; font-size: 12.5px; line-height: 1.5; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel2); }
    .jr-intro a { color: var(--blue); }
    .jr-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .jr-row > .jr-in { flex: 1; min-width: 220px; width: auto; }
    .jr-chk { display: inline-flex; gap: 7px; align-items: center; font-size: 13px; cursor: pointer; }
    .jr-chk input { accent-color: var(--blue); width: 15px; height: 15px; margin: 0; }
    .jr-checks { display: flex; flex-wrap: wrap; gap: 6px 16px; }
    .jr-routes { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px 10px; }
    .jr-stat { display: grid; gap: 3px; font-size: 12.5px; color: var(--muted); }
    .jr-stat b { color: var(--text); font-weight: 600; }
    .jr-counts { display: flex; flex-wrap: wrap; gap: 8px; }
    .jr-count { padding: 6px 10px; border-radius: 6px; background: var(--panel2); border: 1px solid var(--line); min-width: 92px; }
    .jr-count .v { font: 700 17px var(--mono); } .jr-count .k { font-size: 11px; color: var(--muted); }
    .jr-h { font-weight: 700; font-size: 13px; margin-top: 2px; }
    .jr-h small { font-weight: 400; color: var(--muted); margin-left: 6px; font-size: 12px; }
    .jr-tbl { overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }
    .jr-tbl td { height: 28px; }
    .jr-acct { height: 24px; padding: 0 6px; font-size: 12px; max-width: 170px; }
    .jr-acct.moved { border-color: rgba(229,169,60,.7); }
    .jr-ann { border: 1px solid var(--line); border-radius: 6px; background: var(--panel2); padding: 8px 10px; display: grid; gap: 6px; }
    .jr-ann.conf { border-color: rgba(214,165,43,.55); }
    .jr-ann .hd { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .jr-ann .hd .mut { font-size: 12px; }
    .jr-ft { width: 100%; border-collapse: collapse; }
    .jr-ft td, .jr-ft th { white-space: normal; height: auto; padding: 4px 8px; vertical-align: top; font-size: 12.5px; }
    .jr-ft td.v { max-width: 300px; word-break: break-word; }
    .jr-ft tr.conflict td { background: var(--amber-bg); }
    .jr-tagch { display: flex; flex-wrap: wrap; gap: 5px; font-size: 12px; }
    .jr-tagch .add { color: var(--green); } .jr-tagch .rem { color: var(--red); }
    .jr-applybar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; position: sticky; bottom: -16px; margin: 0 -16px -16px;
      padding: 10px 16px; background: var(--panel); border-top: 1px solid var(--line); }
    .jr-list { margin: 0; padding-left: 18px; font-size: 12.5px; }
    .jr-list li { margin: 2px 0; }
    @media (max-width: 640px) { .jr-modal { padding: 8px; } .jr-box { max-height: calc(100vh - 16px); }
      .jr-note { grid-template-columns: 1fr auto; } .jr-note .t { grid-column: 1 / -1; } .jr-noteadd { flex-direction: column; align-items: stretch; } }`;
  document.head.appendChild(css);

  // Shared pick lists for Strategy, Source and Emotional state (filled from the journal's library).
  const lists = document.createElement("div");
  lists.innerHTML = `<datalist id="jr-dl-strat"></datalist><datalist id="jr-dl-src"></datalist><datalist id="jr-dl-emo"></datalist>`;
  document.body.appendChild(lists);
  // A to Z, ignoring case; one entry per name whatever its case.
  function sortAZ(vals) {
    const seen = new Map();
    for (const v of vals || []) { const t = String(v || "").trim(); if (t && !seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t); }
    return [...seen.values()].sort((a, b) => a.localeCompare(b, undefined, {sensitivity: "base"}));
  }
  const LISTS = {strategy: () => J.lib?.strategies || [], idea_source: () => J.lib?.sources || []};
  function fillLists() {
    const opts = vals => sortAZ(vals).map(v => `<option value="${esc(v)}"></option>`).join("");
    $("jr-dl-strat").innerHTML = opts(J.lib?.strategies || []);
    $("jr-dl-src").innerHTML = opts(J.lib?.sources || []);
    $("jr-dl-emo").innerHTML = opts(EMOTIONS.concat((J.lib?.tags?.emotion || []).map(v => v.toLowerCase())));
  }
  fillLists();

  // ---- status and library -------------------------------------------------------------------
  async function loadStatus() {
    const s = await jget("/api/journal/status");
    if (s) {
      const was = !!J.status?.configured;
      J.status = s;
      if (s.configured && !J.lib && !J.libBusy) loadLib();
      if (s.configured && !J.accounts && !J.accTried) { J.accTried = true; testJournal(true); }   // account names for the preview
      if (was !== !!s.configured && lastState) render(lastState);
    }
    paintHeader();
  }
  async function loadLib() {
    if (!J.status?.configured || J.libBusy) return;
    J.libBusy = true;
    const r = await jpost("/api/journal/library", {}, 40000);
    J.libBusy = false;
    if (r.ok) { J.lib = {strategies: r.strategies || [], sources: r.sources || [], tags: r.tags || {}}; J.libErr = null; J.libAt = Date.now(); }
    else J.libErr = r.error || "Could not read the journal's lists.";
    fillLists();
    for (const ed of J.eds.values()) ed.paintTags();
  }
  setInterval(() => { loadStatus(); if (J.status?.configured && Date.now() - J.libAt > 300000) loadLib(); }, 60000);

  // ---- header: Journal (settings, day note, sync) and Sync ----------------------------------
  const hb = document.createElement("span");
  hb.className = "only-main jr-hb";
  hb.innerHTML = `<button type="button" class="btn sm ghost" id="jr-open">Journal <span class="dot" id="jr-dot"></span></button>
    <button type="button" class="btn sm" id="jr-sync" style="display:none" title="Preview: your fills and notes go to the journal, which shows what it would add and change; nothing is written until you click Apply">Sync</button>`;
  const anchor = document.querySelector("header > span.only-main");
  anchor ? anchor.before(hb) : document.querySelector("header").appendChild(hb);
  function paintHeader() {
    const s = J.status, on = !!s?.configured;
    $("jr-sync").style.display = on ? "" : "none";
    const synced = s?.last_sync_day === todayET();
    $("jr-dot").className = "dot " + (!on ? "" : synced ? "ok" : "stale");
    $("jr-open").title = !on ? "Trading journal: not set up. Click to set it up."
      : `Trading journal at ${s.url}. ${s.last_sync_at ? "Last sync " + fmtDT(s.last_sync_at) : "Never synced"}.`
        + (s.last_apply && !s.last_apply.ok ? ` Last apply failed: ${s.last_apply.error || "see Sync"}.` : "");
  }

  // ---- the journal window: Sync | Day note | Settings ---------------------------------------
  const modal = document.createElement("div");
  modal.className = "jr-modal"; modal.id = "jr-modal"; modal.hidden = true;
  modal.innerHTML = `<div class="panel jr-box" role="dialog" aria-modal="true" aria-label="Trading journal">
    <div class="ph">Trading journal<div class="seg" id="jr-tabs"><button type="button" data-jtab="sync">Sync</button><button type="button" data-jtab="day">Day note</button><button type="button" data-jtab="settings">Settings</button></div>
      <button type="button" class="btn sm ghost jr-x" id="jr-x">Close</button></div>
    <div class="jr-body" id="jr-body"></div></div>`;
  document.body.appendChild(modal);
  function openModal(tab) {
    J.tab = tab || (J.status?.configured ? "sync" : "settings");
    modal.hidden = false;
    paintTab();
  }
  function closeModal() {
    if (J.day?.timer) saveDay();
    modal.hidden = true;
  }
  function paintTab() {
    if (!J.status?.configured && J.tab !== "settings" && J.tab !== "day") J.tab = "settings";
    modal.querySelectorAll("[data-jtab]").forEach(b => b.classList.toggle("on", b.dataset.jtab === J.tab));
    if (J.tab === "sync") paintSync();
    else if (J.tab === "day") paintDay();
    else paintSettings();
  }
  modal.addEventListener("click", e => {
    if (e.target === modal || e.target.closest("#jr-x")) { closeModal(); return; }
    const t = e.target.closest("[data-jtab]");
    if (t) {
      if (J.day?.timer) saveDay();
      if (t.dataset.jtab !== "settings" && !J.status?.configured) { J.tab = "settings"; paintTab(); setMsg("jr-smsg", "warn", "Set the address and pick at least one account first."); return; }
      J.tab = t.dataset.jtab; paintTab();
    }
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !modal.hidden) closeModal(); });
  function setMsg(id, cls, text) { const el = $(id); if (el) { el.className = "result " + (cls || ""); el.textContent = text; el.style.display = text ? "" : "none"; } }

  // ---- Settings -----------------------------------------------------------------------------
  async function paintSettings() {
    const body = $("jr-body");
    if (!J.draft) {
      body.innerHTML = `<div class="mut">Loading the journal settings...</div>`;
      const s = await jget("/api/journal/settings");
      if (!s) { body.innerHTML = `<div class="result bad">Could not read the journal settings from Edge Trader.</div>`; return; }
      J.draft = {url: s.url || "", allow_remote: !!s.allow_remote, routes: {...(s.routes || {})},
                 dedup: new Set((s.dedup_accounts || []).map(Number)), exclude: (s.exclude_symbols || []).join(", "),
                 first: s.first_sync_start || "", last_day: s.last_sync_day, fresh: !s.url};
      if (s.url && !J.accounts) testJournal(true);
      if (J.tab !== "settings") return;
    }
    const d = J.draft;
    body.innerHTML = `${d.fresh ? `
      <div class="jr-sec jr-intro"><b>Send your trades to a trading journal.</b> Edge Trader can send your fills, the notes
        you write per trade, the stop, target and close timeline and a day note to
        <a href="https://github.com/simonro/Trading-Journal-AI" target="_blank" rel="noopener noreferrer">Trading Journal AI</a>,
        a free, open-source journal that also runs on your computer. Install and start it, then enter its backend address
        below (by default <span class="mono">http://127.0.0.1:8010</span>) and press Test. Nothing happens on its own:
        Sync sends a preview request (the journal writes nothing), and only Apply writes to the journal.</div>` : ""}
      <div class="jr-sec"><span class="lbl">Journal address</span>
        <div class="jr-row"><input class="jr-in mono" id="jr-url" placeholder="http://127.0.0.1:8010" spellcheck="false" autocomplete="off" value="${esc(d.url)}">
          <button type="button" class="btn" id="jr-test">Test</button></div>
        <label class="jr-chk" id="jr-remote-row"${isLocal(d.url) ? ' style="display:none"' : ""}><input type="checkbox" id="jr-remote"${d.allow_remote ? " checked" : ""}>
          Allow a journal on another computer <span class="amb">(your trades and notes will be sent there)</span></label>
        <div class="result" id="jr-testmsg" style="display:none"></div></div>
      <div class="jr-sec" id="jr-acc"></div>
      <div class="jr-grid">
        <div class="jr-f"><span class="lbl">Excluded symbols</span><input class="jr-in mono" id="jr-excl" placeholder="e.g. SPY, COHR" value="${esc(d.exclude)}" spellcheck="false">
          <span class="hint">Comma list. Their fills never leave Edge Trader.</span></div>
        <div class="jr-f"><span class="lbl">First sync starts</span><input class="jr-in mono" type="date" id="jr-first" value="${esc(d.first)}">
          <span class="hint">${d.last_day ? `Last sync day ${esc(d.last_day)}: the next sync starts 3 days before it.` : "Only for the first sync (blank: today). After that each sync starts 3 days before the last one."}</span></div>
      </div>
      <div class="jr-row"><button type="button" class="btn buy" id="jr-save">Save settings</button>
        <span class="hint">Sync stays off until an address and at least one account are saved.</span></div>
      <div class="result" id="jr-smsg" style="display:none"></div>`;
    paintAccounts();
  }
  function paintAccounts() {
    const box = $("jr-acc"), d = J.draft;
    if (!box || !d) return;
    const known = J.accounts || [];
    const ids = new Set(known.map(a => Number(a.id)));
    const all = known.slice();
    for (const id of [...Object.values(d.routes), ...d.dedup]) if (id !== "" && id != null && !ids.has(Number(id))) { ids.add(Number(id)); all.push({id: Number(id), name: `Account ${id}`}); }
    if (!all.length) { box.innerHTML = `<span class="lbl">Journal accounts</span><div class="hint">${J.accounts
      ? "The journal has no accounts yet. Add one in Trading Journal AI (Settings, Accounts), then press Test again."
      : "Press Test to load the journal's accounts."}</div>`; return; }
    const routed = new Set(Object.values(d.routes).map(Number));
    box.innerHTML = `<span class="lbl">Send fills to</span>
      <div class="jr-routes">${ROUTES.map(([k, label]) => `<label class="jr-f"><span class="hint">${label}</span>
        <select class="jr-sel" data-route="${k}"><option value="">Not synced</option>${all.map(a =>
          `<option value="${int(a.id)}"${Number(d.routes[k]) === Number(a.id) ? " selected" : ""}>${esc(a.name)} (#${int(a.id)})</option>`).join("")}</select></label>`).join("")}</div>
      <span class="lbl" style="margin-top:6px">Already has these fills</span>
      <div class="hint">A fill already stored in a ticked account is not added again. The accounts picked above always count.</div>
      <div class="jr-checks">${all.map(a => {
        const r = routed.has(Number(a.id));
        return `<label class="jr-chk"${r ? ' title="Picked above: always counts"' : ""}><input type="checkbox" data-dedup="${int(a.id)}"${r || d.dedup.has(Number(a.id)) ? " checked" : ""}${r ? " disabled" : ""}> ${esc(a.name)}</label>`;
      }).join("")}</div>
      ${known.length ? "" : `<div class="hint">Names show after Test.</div>`}`;
  }
  async function testJournal(quiet) {
    const url = (J.draft?.url || "").trim().replace(/\/+$/, "");
    if (!quiet) { setMsg("jr-testmsg", "", "Testing..."); $("jr-test") && ($("jr-test").disabled = true); }
    const r = await jpost("/api/journal/test", {url: url || null, allow_remote: !!J.draft?.allow_remote}, 40000);
    if ($("jr-test")) $("jr-test").disabled = false;
    if (!quiet) J.accTried = false;
    if (r.ok) {
      J.accounts = r.accounts || []; J.info = r.info;
      if (J.draft?.fresh && !J.draft.dedup.size) J.accounts.forEach(a => J.draft.dedup.add(Number(a.id)));   // first setup: all ticked
      if (!quiet) setMsg("jr-testmsg", "ok", `Connected: Trading Journal AI${r.info?.journal_version && r.info.journal_version !== "unknown" ? " " + r.info.journal_version : ""}, sync version ${r.info?.api_version}, ${plural(J.accounts.length, "account")}: ${J.accounts.map(a => a.name).join(", ")}.`);
      paintAccounts();
    } else if (!quiet) setMsg("jr-testmsg", "bad", r.error || "Test failed.");
  }
  $("jr-body").addEventListener("input", e => {
    const d = J.draft; if (!d || J.tab !== "settings") return;
    if (e.target.id === "jr-url") {
      d.url = e.target.value.trim();
      $("jr-remote-row").style.display = isLocal(d.url) ? "none" : "";
    } else if (e.target.id === "jr-excl") d.exclude = e.target.value;
    else if (e.target.id === "jr-first") d.first = e.target.value;
  });
  $("jr-body").addEventListener("change", e => {
    if (e.target.matches?.("select[data-acct-keys]")) { pickAccount(e.target); return; }
    const d = J.draft; if (!d || J.tab !== "settings") return;
    if (e.target.id === "jr-remote") d.allow_remote = e.target.checked;
    else if (e.target.dataset.route) { d.routes[e.target.dataset.route] = e.target.value === "" ? "" : Number(e.target.value); paintAccounts(); }
    else if (e.target.dataset.dedup) { const id = Number(e.target.dataset.dedup); e.target.checked ? d.dedup.add(id) : d.dedup.delete(id); }
    else if (e.target.id === "jr-first") d.first = e.target.value;
  });
  $("jr-body").addEventListener("click", async e => {
    if (e.target.closest("#jr-test")) { testJournal(false); return; }
    if (e.target.closest("#jr-save")) { saveSettings(); return; }
    if (e.target.closest("#jr-preview")) { runPreview(); return; }
    if (e.target.closest("#jr-apply")) { runApply(); return; }
    if (e.target.closest("#jr-discard")) { J.preview = null; J.msg = null; paintSync(); return; }
    if (e.target.closest("#jr-gosettings")) { J.tab = "settings"; paintTab(); }
  });
  async function saveSettings() {
    const d = J.draft, btn = $("jr-save");
    const routes = {};
    for (const [k] of ROUTES) if (d.routes[k] !== "" && d.routes[k] != null) routes[k] = Number(d.routes[k]);
    const body = {url: d.url.replace(/\/+$/, ""), allow_remote: !isLocal(d.url) && d.allow_remote, routes,
                  dedup_accounts: [...d.dedup], exclude_symbols: d.exclude, first_sync_start: d.first || null};
    btn.disabled = true; setMsg("jr-smsg", "", "Saving...");
    const r = await jpost("/api/journal/settings", body);
    btn.disabled = false;
    if (!r.ok) { setMsg("jr-smsg", "bad", r.error || "Not saved."); return; }
    d.fresh = false; d.url = r.url || ""; d.routes = {...(r.routes || {})}; d.dedup = new Set((r.dedup_accounts || []).map(Number));
    d.exclude = (r.exclude_symbols || []).join(", "); d.first = r.first_sync_start || "";
    J.preview = null; J.msg = null; J.lib = null; J.libAt = 0;       // the server dropped any open preview
    await loadStatus();
    if (J.status?.configured) loadLib();
    const on = !!J.status?.configured;
    setMsg("jr-smsg", on ? "ok" : "warn", on ? "Saved. Sync is on: journal panels show on your trades." :
      !d.url ? "Saved. No address: journal sync is off." : "Saved, but Sync stays off until you pick at least one account.");
    if (lastState) render(lastState);
  }

  // ---- Day note -----------------------------------------------------------------------------
  async function paintDay() {
    const date = todayET(), body = $("jr-body");
    if (!J.day || J.day.date !== date) J.day = {date, text: null, updated: null, timer: null, err: null, saving: false};
    const dl = new Date(date + "T12:00:00").toLocaleDateString("en-US", {weekday: "long", month: "long", day: "numeric"});
    body.innerHTML = `<div class="jr-sec"><span class="lbl">Day note for ${esc(dl)}</span>
      <textarea class="jr-ta" id="jr-day" rows="12" placeholder="The market today, how you traded, what to repeat or avoid tomorrow."${J.day.text == null ? " disabled" : ""}>${esc(J.day.text || "")}</textarea>
      <div class="jr-foot" id="jr-daymsg"></div></div>`;
    paintDayMsg();
    if (J.day.text == null) {
      const r = await jpost("/api/journal/day_note/get", {date});
      if (J.day.date !== date) return;
      if (!r.ok) { J.day.err = r.error; paintDayMsg(); return; }
      J.day.text = r.text || ""; J.day.updated = r.updated;
      const ta = $("jr-day");
      if (ta) { ta.value = J.day.text; ta.disabled = false; ta.focus(); }
      paintDayMsg();
    }
  }
  function paintDayMsg() {
    const el = $("jr-daymsg"), d = J.day; if (!el || !d) return;
    el.innerHTML = d.err ? `<span class="bad">Not saved: ${esc(d.err)}</span>`
      : d.saving ? "Saving..." : d.timer ? "Typing..."
      : d.updated ? `<span class="ok">Saved here ${esc(fmtDT(d.updated))}.</span> Goes to the journal on the next Sync.`
      : d.text == null ? "Loading..." : "Saves here as you type. Goes to the journal on Sync.";
  }
  async function saveDay() {
    const d = J.day; if (!d) return;
    clearTimeout(d.timer); d.timer = null;
    if (d.saving) { d.again = true; return; }
    d.saving = true; paintDayMsg();
    const r = await jpost("/api/journal/day_note", {date: d.date, text: d.text});
    d.saving = false;
    if (r.ok) { d.updated = r.updated; d.err = null; } else d.err = r.error;
    paintDayMsg();
    if (d.again) { d.again = false; saveDay(); }
  }
  $("jr-body").addEventListener("input", e => {
    if (e.target.id !== "jr-day" || !J.day) return;
    J.day.text = e.target.value; clearTimeout(J.day.timer);
    J.day.timer = setTimeout(saveDay, 800); paintDayMsg();
  });
  $("jr-body").addEventListener("focusout", e => { if (e.target.id === "jr-day" && J.day?.timer) saveDay(); });

  // ---- Sync: Preview, then Apply on a click -------------------------------------------------
  function statusHtml() {
    const s = J.status || {};
    const lines = [`Journal at <b class="num">${esc(s.url || "-")}</b> <button type="button" class="btn sm ghost" id="jr-gosettings">Settings</button>`,
      s.last_sync_at ? `Last sync <b>${esc(fmtDT(s.last_sync_at))}</b>. The next one starts 3 days before ${esc(s.last_sync_day || "it")}.`
                     : `Never synced.`];
    if (s.last_apply && !s.last_apply.ok) lines.push(`<span class="neg">Last apply failed ${esc(fmtDT(s.last_apply.at))}: ${esc(s.last_apply.error || "")}</span>`);
    return `<div class="jr-stat">${lines.map(l => `<div>${l}</div>`).join("")}</div>`;
  }
  function paintSync() {
    const busy = J.busy;
    $("jr-body").innerHTML = statusHtml()
      + `<div class="jr-row"><button type="button" class="btn buy" id="jr-preview"${busy ? " disabled" : ""}>${busy === "preview" ? "Asking the journal..." : J.preview ? "Preview again" : "Preview sync"}</button>
          <span class="hint">A dry run: your fills and notes go to the journal, which shows exactly what it would add and change. Nothing is written until you click Apply.</span></div>`
      + (J.msg ? `<div class="result ${J.msg.cls}">${J.msg.html}</div>` : "")
      + (J.preview ? previewHtml(J.preview) : "");
  }
  async function runPreview() {
    if (J.busy) return;
    J.busy = "preview"; J.msg = null; J.applied = null; paintSync();
    const r = await jpost("/api/journal/preview", {}, 100000);
    J.busy = null;
    if (r.ok) { J.preview = r; J.preview.token = r.state_token; }
    else { J.preview = null; J.msg = {cls: "bad", html: esc(r.error || "Preview failed.")}; }
    paintSync();
  }
  async function runApply() {
    const p = J.preview;
    if (J.busy || !p || !p.token) return;
    J.busy = "apply"; J.msg = null; paintSync();
    const r = await jpost("/api/journal/apply", {token: p.token}, 150000);
    J.busy = null;
    if (r.ok && r.conflict) {
      J.preview = {...r, meta: r.meta || p.meta, token: r.state_token};
      J.msg = {cls: "warn", html: "The journal changed since your preview (an edit there, or another sync). Nothing was written. This is the new preview: check it and click Apply again."};
    } else if (r.ok && r.applied) {
      J.applied = r; J.preview = null;
      await loadStatus();
      const fb = r.feedback || {};
      J.msg = {cls: "ok", html: `Applied. ${plural((r.fills || {}).new || 0, "new fill")}, ${plural((r.trades || []).length, "trade")} new or updated, `
        + `${plural(((r.events || {}).new || 0) + ((r.events || {}).updated || 0), "event")} written. `
        + `Journal backup: <b class="num">${esc(r.backup || "none reported")}</b>. Last sync ${esc(fmtDT(J.status?.last_sync_at || new Date().toISOString()))}.`
        + (fb.queued ? ` AI feedback queued for ${plural(fb.queued, "trade")}.` : fb.skipped ? ` <span class="mut">${esc(fb.skipped)}</span>` : "")};
    } else if (r.lost) {
      p.token = null;
      J.msg = {cls: "warn", html: `${esc(r.error)} The outcome is unknown. The journal applies all or nothing: click Preview again to see whether it went in.`};
    } else {
      p.token = null;
      J.msg = {cls: "bad", html: esc(r.error || "Apply failed.")};
    }
    paintSync();
  }

  function localNames() {
    const m = new Map(), st = lastState || {};
    for (const r of [...(st.positions || []), ...(st.closed || [])]) { const k = annKey(r.fill_ids); if (k) m.set(k, r.display || r.symbol); }
    for (const [k, ed] of J.eds) m.set(k.slice(2), ed.row.display || ed.row.symbol);
    return m;
  }
  const acctName = id => { const a = (J.accounts || []).find(x => Number(x.id) === Number(id)); return a ? a.name : `account ${id}`; };
  const showVal = v => {
    if (v === null || v === undefined || v === "") return `<span class="mut">empty</span>`;
    const s = String(v);
    return s.length > 160 ? `<span title="${esc(s)}">${esc(s.slice(0, 160))}...</span>` : esc(s);
  };
  const ANN_TXT = {ok: "matched to one journal trade", pending: "its fills are not in the journal yet: sent again on the next sync",
                   split: "its fills sit in more than one journal trade: written to the first", merge: "the journal holds this as one trade with another entry: the newer entry is written"};
  // A new trade's account: its route by default, or another journal account picked here (the pick is
  // remembered per trade and goes with the next Preview / Apply). A rebuilt trade stays where it is.
  function acctCell(t) {
    const keys = t.fill_keys || [];
    if (t.status !== "new" || !keys.length || !(J.accounts || []).length) return esc(acctName(t.account_id));
    const def = Number((J.status?.routes || {})[t.instrument_type]);
    const opts = J.accounts.map(a => `<option value="${int(a.id)}"${Number(a.id) === Number(t.account_id) ? " selected" : ""}>${esc(a.name)}${Number(a.id) === def ? " (default)" : ""}</option>`).join("");
    return `<select class="jr-in jr-acct${def && Number(t.account_id) !== def ? " moved" : ""}" data-acct-keys="${esc(keys.join(","))}" data-def="${def || ""}"${J.busy ? " disabled" : ""} title="Journal account for this trade">${opts}</select>`;
  }
  async function pickAccount(sel) {
    if (J.busy) return;
    const id = Number(sel.value), def = Number(sel.dataset.def);
    J.busy = "preview"; J.msg = null; paintSync();
    const r = await jpost("/api/journal/account", {fill_keys: sel.dataset.acctKeys.split(","), account_id: id === def ? null : id});
    J.busy = null;
    if (!r.ok) { J.msg = {cls: "bad", html: esc(r.error || "Account not changed.")}; paintSync(); return; }
    runPreview();          // the old preview no longer matches what Apply would send
  }

  // An option trade reads by direction, the way the journal shows it: a sold put or a put credit spread
  // is Bullish, a bought put or a sold call Bearish. The journal stores the opening side (SHORT for a
  // sold put); its trade name ends in _PUT_n or _CALL_n, and a vertical's strikes have a dash.
  function sideTag(t) {
    const side = String(t.side || "").toUpperCase();
    const m = /_(?:([\d.]+)-[\d.]+|[\d.]+)_(PUT|CALL)_\d+$/.exec(String(t.trade_group || ""));
    if (String(t.instrument_type || "").toUpperCase() !== "OPTION" || !m || !["LONG", "SHORT"].includes(side))
      return `<span class="tag ${pick(side.toLowerCase(), ["long", "short"])}">${esc(t.side || "")}</span>`;
    const opt = m[2], bull = (opt === "CALL") === (side === "LONG");
    const what = m[1] ? `${opt === "PUT" ? "Put" : "Call"} ${side === "SHORT" ? "credit" : "debit"} spread`
      : `${side === "SHORT" ? "Sold" : "Bought"} ${opt.toLowerCase()}`;
    return `<span class="tag ${bull ? "long" : "short"}" title="${esc(what)}">${bull ? "BULLISH" : "BEARISH"}</span>`;
  }

  function previewHtml(p) {
    const f = p.fills || {}, m = p.meta || {}, ev = p.events || {}, n = k => int(f[k]);
    const names = localNames();
    const count = (k, v, c = "") => `<div class="jr-count"><div class="v ${c}">${int(v)}</div><div class="k">${k}</div></div>`;
    let h = "";
    if (m.history_short) h += `<div class="result warn">Edge Trader's fill history starts ${esc(fmtDT(m.history_starts))}, after this sync's start (${esc(fmtD(m.window_start))}). Earlier fills are not in this sync: import them into the journal from a Schwab statement.</div>`;
    h += `<div class="jr-h">Fills<small>from ${esc(fmtD(m.window_start))}${m.trades != null ? `, ${plural(m.trades, "Edge Trader trade")}` : ""}</small></div>
      <div class="jr-counts">${count("New", n("new"), n("new") ? "pos" : "")}${count("Already in the journal", n("already"))}${count("Covered by a statement", n("covered"))}${count("Excluded", n("excluded"))}${count("No account picked", n("no_route"), n("no_route") ? "amb" : "")}</div>`;
    const trades = p.trades || [];
    h += `<div class="jr-h">Journal trades<small>${trades.length ? plural(trades.length, "trade") + " new or rebuilt" : "none new or changed"}</small></div>`;
    if (trades.length) h += `<div class="jr-tbl"><table><tr><th>Date</th><th>Symbol</th><th>Type</th><th>Side</th><th>Account</th><th></th><th class="n">Net</th></tr>
      ${trades.map(t => `<tr><td class="num mut">${esc(t.date)}</td><td><b>${esc(t.ticker)}</b></td><td class="mut">${esc((t.instrument_type || "").toLowerCase())}</td>
        <td>${sideTag(t)}</td><td>${acctCell(t)}</td>
        <td><span class="tag ${t.status === "new" ? "target" : "stop"}">${esc(t.status)}</span></td><td class="n ${cls(t.net_pnl)}">${money(t.net_pnl)}</td></tr>`).join("")}</table></div>`;
    const anns = p.annotations || [];
    const busyAnn = a => a.status !== "ok" || (a.fields || []).some(x => x.status !== "same") || (a.tags?.add || []).length || (a.tags?.remove || []).length;
    const shown = anns.filter(busyAnn), quiet = anns.length - shown.length;
    const nConf = anns.reduce((s, a) => s + (a.fields || []).filter(x => x.status === "conflict").length, 0);
    h += `<div class="jr-h">Your journal entries<small>${anns.length ? plural(anns.length, "entry", "entries") + (nConf ? `, <span class="amb">${plural(nConf, "conflict")}: the journal's value stays</span>` : "") : "none to send"}</small></div>`;
    for (const a of shown) {
      const fl = a.fields || [], t = a.tags || {};
      const conf = fl.some(x => x.status === "conflict");
      const where = (a.trades || []).map(x => `${x.trade_group} (${acctName(x.account_id)})`).join(", ");
      const writes = fl.filter(x => x.status !== "same"), same = fl.length - writes.length;
      h += `<div class="jr-ann${conf ? " conf" : ""}"><div class="hd"><b>${esc(names.get(a.id) || a.id)}</b>
          <span class="tag ${a.status === "ok" ? "full" : a.status === "pending" ? "waiting" : "est"}">${esc(a.status)}</span>
          <span class="mut">${esc(ANN_TXT[a.status] || "")}${a.applied_instead ? ` (${esc(names.get(a.applied_instead) || a.applied_instead)})` : ""}${a.missing_fills ? `; ${plural(a.missing_fills, "fill")} not in the journal yet` : ""}</span></div>
        ${where ? `<div class="mut" style="font-size:12px">Journal trade ${esc(where)}</div>` : ""}
        ${writes.length ? `<table class="jr-ft"><tr><th>Field</th><th></th><th>Journal now</th><th>Edge Trader</th></tr>${writes.map(x =>
          `<tr class="${pick(x.status, ["write", "conflict", "same"])}"><td>${esc(FIELD_LABEL[x.field] || x.field)}</td>
            <td>${x.status === "write" ? `<span class="tag target">write</span>` : `<span class="tag stop" title="The journal was edited there: its value stays">conflict</span>`}</td>
            <td class="v">${showVal(x.journal)}</td><td class="v">${showVal(x.edge)}</td></tr>`).join("")}</table>` : ""}
        ${same ? `<div class="mut" style="font-size:12px">${plural(same, "field")} already the same.</div>` : ""}
        ${(t.add || []).length || (t.remove || []).length ? `<div class="jr-tagch">${(t.add || []).map(x => `<span class="add">+ ${esc(x.type)}: ${esc(x.value)}</span>`).join("")}
          ${(t.remove || []).map(x => `<span class="rem">- ${esc(x.type)}: ${esc(x.value)}</span>`).join("")}${(t.kept || []).length ? `<span class="mut">${plural(t.kept.length, "tag")} kept</span>` : ""}</div>` : ""}
      </div>`;
    }
    if (quiet) h += `<div class="mut" style="font-size:12px">${quiet} more ${quiet === 1 ? "entry already matches" : "entries already match"} the journal.</div>`;
    h += `<div class="jr-h">Events and notes<small>stops and targets set or moved, your timestamped notes</small></div>
      <div class="jr-counts">${count("New", ev.new, int(ev.new) ? "pos" : "")}${count("Updated", ev.updated)}${count("Unchanged", ev.same)}${count("Waiting for their trade", ev.pending, int(ev.pending) ? "amb" : "")}</div>`;
    const dn = p.day_notes || [];
    if (dn.length) h += `<div class="jr-h">Day notes</div><ul class="jr-list">${dn.map(x => `<li>${esc(x.date)} (${esc(acctName(x.account_id))}): <b>${esc(x.status)}</b></li>`).join("")}</ul>`;
    const cf = p.changed_fills || [];
    if (cf.length) h += `<div class="result warn">${cf.length === 1 ? "1 fill differs" : cf.length + " fills differ"} from what an earlier sync sent (Schwab changed them):
      ${cf.slice(0, 12).map(x => `${esc(x.symbol)} ${esc(x.date)} ${esc(x.time || "")}`).join(", ")}${cf.length > 12 ? ", ..." : ""}</div>`;
    const pr = p.problems || [];
    if (pr.length) {
      const by = new Map();
      for (const x of pr) by.set(x.reason, (by.get(x.reason) || []).concat(x.activityId));
      h += `<div class="result bad"><b>${plural(pr.length, "problem")}</b> (these fills are left out):<ul class="jr-list">${[...by].map(([r, ids]) =>
        `<li>${esc(r)}: ${ids.length} (${ids.slice(0, 6).map(esc).join(", ")}${ids.length > 6 ? ", ..." : ""})</li>`).join("")}</ul></div>`;
    }
    h += `<div class="jr-applybar"><button type="button" class="btn buy act" id="jr-apply"${J.busy || !p.token ? " disabled" : ""}>${J.busy === "apply" ? "Applying..." : "Apply to the journal"}</button>
      <button type="button" class="btn ghost" id="jr-discard"${J.busy ? " disabled" : ""}>Discard</button>
      <span class="hint">${p.token ? "The journal backs itself up first and applies all or nothing." : "This preview can no longer be applied: click Preview again."}</span></div>`;
    return h;
  }

  // ---- one trade's journal entry: the editor ------------------------------------------------
  const hasContent = d => d && (Object.keys(d.fields || {}).length || Object.values(d.tags || {}).some(v => v.length) || (d.notes || []).length);
  class Editor {
    constructor(row) {
      this.row = row; this.key = annKey(row.fill_ids);
      this.data = null; this.err = null; this.loadErr = null; this.saving = false; this.again = false;
      this.pending = {fields: {}, tags: {}}; this.timer = null; this.savedAt = null; this.editing = null; this.noteBusy = false;
      this.node = document.createElement("div");
      this.node.className = "jr-ed jr-keep";
      this.node.innerHTML = `<div class="mut">Loading the journal entry...</div>`;
      this.node.addEventListener("input", e => this.onInput(e));
      this.node.addEventListener("click", e => this.onClick(e));
      this.node.addEventListener("keydown", e => this.onKey(e));
      this.node.addEventListener("focusout", e => { if (e.target.dataset?.f && this.timer) this.flush(); });
      this.node.addEventListener("change", e => this.onChange(e));
      // The Mistakes list closes on a click anywhere outside it.
      document.addEventListener("mousedown", e => {
        if (this.msOpen && !this.node.querySelector(".jr-ms")?.contains(e.target)) { this.msOpen = false; this.paintMs(); }
      });
      this.load();
    }
    setRow(row) { if (row.fill_ids && row.fill_ids.length) this.row = row; }
    async load() {
      const r = await jpost("/api/journal/entry/get", {fill_ids: this.row.fill_ids});
      if (!r.ok) { this.loadErr = r.error; this.node.innerHTML = `<div class="jr-foot"><span class="bad">Could not load the entry: ${esc(r.error)}</span> <button type="button" class="btn sm" data-jr="reload">Retry</button></div>`; return; }
      this.loadErr = null;
      this.data = {fields: r.fields || {}, tags: r.tags || {}, notes: r.notes || [], updated: r.updated};
      this.build(); paintSummaries();
    }
    build() {
      const f = this.data.fields;
      const inp = (k, label, list, ph) => `<label class="jr-f"><span class="lbl">${label}</span><input class="jr-in" data-f="${k}"${list ? ` list="${list}"` : ""} placeholder="${esc(ph)}" value="${esc(f[k] || "")}" autocomplete="off"></label>`;
      const ta = (k, label, ph) => `<label class="jr-f"><span class="lbl">${label}</span><textarea class="jr-ta" data-f="${k}" rows="2" placeholder="${esc(ph)}">${esc(f[k] || "")}</textarea></label>`;
      this.node.innerHTML = `
        <div class="jr-grid">${inp("strategy", "Strategy", "jr-dl-strat", "pick or type")}${inp("idea_source", "Source", "jr-dl-src", "where the idea came from")}
          ${inp("emotional_state", "Emotional state", "jr-dl-emo", "calm, anxious...")}<div class="jr-f"><span class="lbl">Mistakes</span><div class="jr-ms"></div></div></div>
        <div class="jr-grid wide">${ta("entry_reason", "Entry reason", "Why you took it, in your words")}${ta("exit_reason", "Exit reason", "Why you got out")}</div>
        <div class="jr-tags"></div>
        <div><span class="lbl">Notes</span><div class="jr-notes"></div>
          <div class="jr-noteadd"><textarea class="jr-ta" data-note-new rows="1" placeholder="A note at this moment (Ctrl+Enter adds it)"></textarea>
            <button type="button" class="btn" data-jr="add">Add note</button></div></div>
        <div class="jr-foot"></div>`;
      this.paintTags(); this.paintMs(); this.paintNotes(); this.paintSave();
    }
    // Mistakes are the journal's Mistake tags: pick any number from its list, or type a new one.
    paintMs() {
      const box = this.node.querySelector(".jr-ms"); if (!box || !this.data) return;
      const on = this.data.tags.mistake || [];
      const all = sortAZ((J.lib?.tags?.mistake || []).concat(on));
      const onL = new Set(on.map(v => v.toLowerCase()));
      box.classList.toggle("open", !!this.msOpen);
      box.innerHTML = `<button type="button" class="jr-msbox" data-jr="ms" aria-haspopup="listbox" aria-expanded="${!!this.msOpen}">${on.length
          ? on.map(v => `<span class="jr-mschip">${esc(v)}</span>`).join("") : `<span class="ph">none, pick from your list</span>`}</button>`
        + (this.msOpen ? `<div class="jr-msmenu" role="listbox" aria-multiselectable="true">${all.map(v =>
            `<label class="jr-msopt"><input type="checkbox" data-ms="${esc(v)}"${onL.has(v.toLowerCase()) ? " checked" : ""}>${esc(v)}</label>`).join("")
            || `<div class="mut" style="padding:6px 8px;font-size:12px">${J.libErr ? "The journal's list did not load." : "No mistake tags in the journal yet."}</div>`}
            <input class="jr-in jr-msnew" data-msnew placeholder="+ new mistake, Enter adds it" autocomplete="off"></div>` : "");
    }
    setMistakes(list) {
      this.data.tags.mistake = sortAZ(list);
      this.pending.tags.mistake = this.data.tags.mistake;
      this.paintMs(); this.schedule(250); paintSummaries();
    }
    paintTags() {
      const box = this.node.querySelector(".jr-tags"); if (!box || !this.data) return;
      // Tags are set in the journal, not here. Only the pick-list warning shows.
      box.innerHTML = J.libErr ? `<div class="jr-libwarn">Journal lists not loaded: ${esc(J.libErr)} <button type="button" class="btn sm" data-jr="lib">Retry</button></div>` : "";
    }
    paintNotes() {
      const box = this.node.querySelector(".jr-notes"); if (!box || !this.data) return;
      const notes = this.data.notes || [];
      box.innerHTML = notes.length ? notes.map(n => this.editing && this.editing.id === n.id
        ? `<div class="jr-note"><span class="t">${esc(noteTime(n.ts))}</span><textarea class="jr-ta" data-note-edit rows="2">${esc(this.editing.text)}</textarea>
            <span class="b"><button type="button" class="btn sm" data-jr="esave" data-id="${esc(n.id)}">Save</button><button type="button" class="btn sm ghost" data-jr="ecancel">Cancel</button></span></div>`
        : `<div class="jr-note"><span class="t">${esc(noteTime(n.ts))}</span><span class="jr-nt">${esc(n.text)}</span>
            <span class="b"><button type="button" class="btn sm ghost" data-jr="edit" data-id="${esc(n.id)}">Edit</button><button type="button" class="btn sm ghost" data-jr="del" data-id="${esc(n.id)}">Delete</button></span></div>`).join("")
        : `<div class="mut" style="font-size:12px;padding:4px 0">No notes yet.</div>`;
    }
    paintSave(msg) {
      const el = this.node.querySelector(".jr-foot"); if (!el) return;
      el.innerHTML = msg ? msg
        : this.err ? `<span class="bad">Not saved: ${esc(this.err)}</span> <button type="button" class="btn sm" data-jr="retry">Retry</button>`
        : this.saving ? "Saving..." : this.timer ? "Typing..."
        : this.data?.updated ? `<span class="ok">Saved here ${esc(this.savedAt ? tET(this.savedAt.toISOString()) : fmtDT(this.data.updated))}.</span> Goes to the journal on the next Sync.`
        : "Saves here as you type. Goes to the journal on Sync.";
    }
    onInput(e) {
      const k = e.target.dataset?.f;
      if (k) { this.pending.fields[k] = e.target.value; this.data.fields[k] = e.target.value.trim(); this.schedule(700); }
      if (e.target.matches?.("[data-note-edit]") && this.editing) this.editing.text = e.target.value;
    }
    onChange(e) {
      // A typed strategy or source that matches a journal name in another case takes the journal's spelling.
      const k = e.target.dataset?.f; if (!k || !LISTS[k]) return;
      const v = e.target.value.trim(), hit = LISTS[k]().find(x => x.toLowerCase() === v.toLowerCase());
      if (hit && hit !== v) { e.target.value = hit; this.pending.fields[k] = hit; this.data.fields[k] = hit; this.schedule(200); }
    }
    onKey(e) {
      if (e.target.matches?.("[data-msnew]") && e.key === "Enter") {
        e.preventDefault();
        const v = e.target.value.trim(); if (!v) return;
        const known = sortAZ(J.lib?.tags?.mistake || []).find(x => x.toLowerCase() === v.toLowerCase()) || v;
        this.setMistakes((this.data.tags.mistake || []).filter(x => x.toLowerCase() !== known.toLowerCase()).concat(known));
        this.node.querySelector("[data-msnew]")?.focus();
        return;
      }
      if (e.key === "Escape" && this.msOpen) { this.msOpen = false; this.paintMs(); this.node.querySelector(".jr-msbox")?.focus(); return; }
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.matches?.("[data-note-new]")) { e.preventDefault(); this.addNote(); }
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.matches?.("[data-note-edit]")) { e.preventDefault(); this.saveEdit(); }
    }
    async onClick(e) {
      const cb = e.target.closest("input[data-ms]");
      if (cb) {
        const v = cb.dataset.ms, cur = (this.data.tags.mistake || []).filter(x => x.toLowerCase() !== v.toLowerCase());
        this.setMistakes(cb.checked ? cur.concat(v) : cur);
        this.node.querySelector(`input[data-ms="${CSS.escape(v)}"]`)?.focus();
        return;
      }
      const b = e.target.closest("[data-jr]"); if (!b) return;
      const a = b.dataset.jr;
      if (a === "ms") { this.msOpen = !this.msOpen; this.paintMs(); if (this.msOpen) this.node.querySelector(".jr-msmenu input")?.focus(); return; }
      if (a === "reload") { this.node.innerHTML = `<div class="mut">Loading the journal entry...</div>`; this.load(); }
      else if (a === "lib") loadLib();
      else if (a === "retry") { this.err = null; this.flush(); }
      else if (a === "add") this.addNote();
      else if (a === "edit") { const n = this.data.notes.find(x => x.id === b.dataset.id); this.editing = n ? {id: n.id, text: n.text} : null; this.paintNotes(); this.node.querySelector("[data-note-edit]")?.focus(); }
      else if (a === "ecancel") { this.editing = null; this.paintNotes(); }
      else if (a === "esave") this.saveEdit();
      else if (a === "del") {
        const n = this.data.notes.find(x => x.id === b.dataset.id); if (!n) return;
        if (!confirm(`Delete this note?\n\n${n.text.slice(0, 200)}`)) return;
        const r = await jpost("/api/journal/note/edit", {id: n.id, text: null});
        if (!r.ok) { this.paintSave(`<span class="bad">Note not deleted: ${esc(r.error)}</span>`); return; }
        await this.reloadNotes();
      }
    }
    async addNote() {
      const ta = this.node.querySelector("[data-note-new]"), text = ta?.value.trim();
      if (!text || this.noteBusy) { if (!text) this.paintSave(`<span class="bad">Type a note first.</span>`); return; }
      this.noteBusy = true;
      const r = await jpost("/api/journal/note", {symbol: this.row.symbol, fill_ids: this.row.fill_ids, text});
      this.noteBusy = false;
      if (!r.ok) { this.paintSave(`<span class="bad">Note not added: ${esc(r.error)}</span>`); return; }
      ta.value = "";
      this.data.notes = r.notes || []; this.data.updated = r.updated; this.savedAt = new Date();
      this.paintNotes(); this.paintSave(); paintSummaries();
    }
    async saveEdit() {
      if (!this.editing) return;
      const {id, text} = this.editing;
      const r = await jpost("/api/journal/note/edit", {id, text});
      if (!r.ok) { this.paintSave(`<span class="bad">Note not saved: ${esc(r.error)}</span>`); return; }
      this.editing = null;
      await this.reloadNotes();
    }
    async reloadNotes() {
      const r = await jpost("/api/journal/entry/get", {fill_ids: this.row.fill_ids});
      if (r.ok) { this.data.notes = r.notes || []; this.savedAt = new Date(); this.err = null; }
      this.paintNotes(); this.paintSave(r.ok ? undefined : `<span class="bad">Could not reload the notes: ${esc(r.error)}</span>`); paintSummaries();
    }
    schedule(ms) { clearTimeout(this.timer); this.timer = setTimeout(() => this.flush(), ms); this.paintSave(); }
    async flush(keepalive = false) {
      clearTimeout(this.timer); this.timer = null;
      if (this.saving) { this.again = true; return; }
      const p = this.pending, hasF = Object.keys(p.fields).length > 0, hasT = Object.keys(p.tags).length > 0;
      if (!hasF && !hasT) { this.paintSave(); return; }
      this.pending = {fields: {}, tags: {}};
      this.saving = true; this.paintSave();
      const r = await jpost("/api/journal/entry", {symbol: this.row.symbol, fill_ids: this.row.fill_ids,
        fields: hasF ? p.fields : undefined, tags: hasT ? p.tags : undefined}, 15000, keepalive);
      this.saving = false;
      if (r.ok) { this.err = null; this.savedAt = new Date(); this.data.updated = r.updated; }
      else {         // keep what was not saved, unless newer typing replaced it
        this.err = r.error || "not saved";
        for (const k in p.fields) if (!(k in this.pending.fields)) this.pending.fields[k] = p.fields[k];
        for (const k in p.tags) if (!(k in this.pending.tags)) this.pending.tags[k] = p.tags[k];
      }
      this.paintSave(); paintSummaries();
      if (this.again) { this.again = false; this.flush(); }
    }
    summary() {
      const d = this.data;
      if (this.loadErr) return `<span class="neg">could not load</span>`;
      if (!d) return "loading...";
      const bits = [];
      if (d.fields.strategy) bits.push(`<b>${esc(d.fields.strategy)}</b>`);
      const nm = (d.tags?.mistake || []).length;
      if (nm) bits.push(`<span class="neg">${plural(nm, "mistake")}</span>`);
      if ((d.notes || []).length) bits.push(plural(d.notes.length, "note"));
      const nf = Object.keys(d.fields).filter(k => k !== "strategy" && d.fields[k]).length;
      if (nf) bits.push(plural(nf, "field"));
      return bits.length ? bits.join(" · ") : "nothing written yet";
    }
  }
  function editorFor(row, where) {
    const k = where + ":" + annKey(row.fill_ids);
    let ed = J.eds.get(k);
    if (!ed) { ed = new Editor(row); J.eds.set(k, ed); }
    ed.setRow(row);
    return ed;
  }
  window.addEventListener("pagehide", () => { for (const ed of J.eds.values()) if (ed.timer || Object.keys(ed.pending.fields).length) ed.flush(true); if (J.day?.timer) saveDay(); });

  // ---- the selected position's journal (under the workbench) ---------------------------------
  const pos = $("jr-pos");
  if (pos) {
    pos.style.display = "none";
    pos.innerHTML = `<div class="jr-posh" id="jr-posh" role="button" tabindex="0" aria-expanded="false"><span class="jr-title">Journal</span><span class="jr-sum" id="jr-sum"></span><span class="jr-open" id="jr-caret"></span></div>
      <div class="jr-posbody" id="jr-posbody"></div>`;
    const toggle = () => { J.posOpen = !J.posOpen; try { localStorage.setItem("jrPosOpen", J.posOpen ? "1" : "0"); } catch (e) {} paintPos(); };
    $("jr-posh").addEventListener("click", toggle);
    $("jr-posh").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  }
  let POS_ROW = null, POS_ED = null;
  function paintPos() {
    if (!pos) return;
    const st = lastState || {};
    const ws = $("workspace");
    // A companion spread's workbench: the entry is the spread's own, keyed on its fills (first entry
    // fill first), so the same spread's Closed today row opens the same entry.
    const spid = ws?.querySelector(".sp-work")?.dataset.spid;
    const sp = spid ? (st.spreads || []).find(x => x.spread_id === spid) : null;
    const row = !J.status?.configured ? null
      : sp ? (Array.isArray(sp.fill_ids) ? {symbol: sp.underlying, display: `${sp.underlying} spread`, fill_ids: sp.fill_ids} : null)
      : SEL && !spid ? (st.positions || []).find(x => x.symbol === SEL) : null;
    POS_ROW = row;
    if (!row || !Array.isArray(row.fill_ids)) { pos.style.display = "none"; return; }
    pos.style.display = "";
    const body = $("jr-posbody");
    body.style.display = J.posOpen ? "" : "none";
    $("jr-caret").textContent = J.posOpen ? "Hide ▴" : "Open ▾";
    $("jr-posh").setAttribute("aria-expanded", J.posOpen);
    if (!row.fill_ids.length) {
      if (POS_ED) { POS_ED.flush(); POS_ED = null; }
      if (!body.querySelector(".jr-wait")) body.innerHTML = `<div class="jr-wait">Waiting for Schwab's fill. The journal entry opens once Schwab lists it.</div>`;
      $("jr-sum").innerHTML = `<span class="amb">waiting for Schwab's fill</span>`;
      return;
    }
    const ed = editorFor(row, "p");
    if (POS_ED !== ed) { if (POS_ED) POS_ED.flush(); POS_ED = ed; body.replaceChildren(ed.node); }
    $("jr-sum").innerHTML = ed.summary();
  }
  function paintSummaries() {
    if (POS_ED && $("jr-sum")) $("jr-sum").innerHTML = POS_ED.summary();
  }

  // ---- Closed today: a Journal button per row, the entry opens under it -----------------------
  window.journalCell = t => {
    if (!J.status?.configured || !Array.isArray(t.fill_ids)) return "";
    if (!t.fill_ids.length) return `<span class="jr-cw" title="The journal entry opens once Schwab lists the fills">waiting for Schwab's fill</span>`;
    const k = annKey(t.fill_ids), open = J.closedOpen.has(k);
    const ed = editorFor(t, "c");
    return `<button type="button" class="jr-cb${open ? " on" : ""}${hasContent(ed.data) ? " has" : ""}" data-jr-ckey="${esc(k)}" title="${open ? "Hide" : "Write"} the journal entry for this trade">Journal ${open ? "▴" : "▾"}</button>`;
  };
  function paintClosed() {
    const box = $("closed"); if (!box) return;
    let any = false;
    for (const k of J.closedOpen) {
      const btn = box.querySelector(`[data-jr-ckey="${CSS.escape(k)}"]`);
      const ed = J.eds.get("c:" + k);
      if (!btn || !ed) continue;
      any = true;
      let at = btn.closest("tr");
      while (at.nextElementSibling?.classList.contains("exec")) at = at.nextElementSibling;
      if (at.nextElementSibling?.dataset?.jrKey === k) continue;        // still there (render was held)
      const tr = document.createElement("tr");
      tr.className = "jr-tr"; tr.dataset.jrKey = k;
      const td = document.createElement("td"); td.colSpan = 8;
      td.appendChild(ed.node); tr.appendChild(td); at.after(tr);
    }
    box.classList.toggle("jr-grow", any);
  }
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-jr-ckey]");
    if (b) {
      const k = b.dataset.jrCkey;
      if (J.closedOpen.has(k)) { J.closedOpen.delete(k); J.eds.get("c:" + k)?.flush(); } else J.closedOpen.add(k);
      if (lastState) render(lastState);
      return;
    }
    if (e.target.closest("#jr-open")) openModal();
    else if (e.target.closest("#jr-sync")) { openModal("sync"); runPreview(); }
  });

  // ---- hook into the page's render ----------------------------------------------------------
  const pageRender = render;
  render = function (st) {
    pageRender(st);
    try { paintClosed(); } catch (e) { console.error("journal", e); }
    // After every wrapper has run (a spread workbench may replace the position's), so the panel
    // only shows under a position's own workbench.
    queueMicrotask(() => { try { paintPos(); } catch (e) { console.error("journal", e); } });
  };
  loadStatus();
})();
