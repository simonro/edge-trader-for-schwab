// Spread ticket and the spread workbench (slice 2 of the spreads plan).
// Verticals: DAY net-limit entry by quantity or risk; Cancel; paired net-limit Close (partial too); GTC target; app-watched stop.
// Loaded by the dashboard when the server lists "spreads" in its features. Uses the page's own
// helpers ($, esc, px, money, post, optName, fmtK, MONTHS, T, HEALTH, render).
(() => {
  const S = {on: false, und: "", exps: [], exp: null, pc: "PUT", kind: "CREDIT", chain: null, chainAt: 0, near: null,
             far: null, entry: "mid", plan: null, ticketId: null, intentKey: null, sentKey: null, busy: false,
             seq: 0, chainSeq: 0, timer: null, preview: null, close: {}, rows: []};
  const KIND_TXT = {DEBIT: "debit", CREDIT: "credit"};
  // Every vertical by its two choices: what it is called and which way it wins.
  const STRAT = {
    "PUT|CREDIT": {name: "Bull put spread", bias: "BULLISH", cls: "bull", how: "neutral to up"},
    "CALL|CREDIT": {name: "Bear call spread", bias: "BEARISH", cls: "bear", how: "neutral to down"},
    "CALL|DEBIT": {name: "Bull call spread", bias: "BULLISH", cls: "bull", how: "up"},
    "PUT|DEBIT": {name: "Bear put spread", bias: "BEARISH", cls: "bear", how: "down"}};
  const PRICE_HELP = {
    mid: "Mid: halfway between each leg's bid and ask. A better price, but it may sit unfilled.",
    natural: "Natural: the market's price right now on both legs (sell at the bid, buy at the ask). Priced to fill at once, at the cost of the bid/ask; a fast market can still leave it unfilled.",
    limit: "Lmt: your own net price. Fills at that price or better, or not at all."};
  S.size = "qty"; S.risk = null;

  // ---- layout -----------------------------------------------------------------------------
  const css = document.createElement("style");
  css.textContent = `
    #ticket.spread-mode .tk > :not(#t-asset):not(#t-spread) { display: none !important; }
    #t-spread { display: none; flex-direction: column; gap: 8px; }
    #sp-chain { max-height: 212px; }
    .sp-act { display: grid; grid-template-columns: auto 1fr; gap: 8px; }
    .sp-row3 { display: grid; grid-template-columns: 1.1fr 1fr 1fr; gap: 8px; }
    .sp-row3 .seg button { padding: 0 4px; }
    .sp-explbl { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
    .sp-explbl .tk-name { min-height: 0; margin: 0; text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 12px;
                          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #sp-prevmsg:empty, #sp-msg:empty { display: none; }
    .sp-act #sp-prev { height: 46px; padding: 0 14px; }
    #ticket.spread-mode #t-spread { display: flex; }
    #sp-chain tr.far td { background: rgba(229,83,75,.12); color: #fff; }
    #sp-chain tr.near td { background: var(--blue-bg); color: #fff; }
    #sp-width { flex-wrap: wrap; gap: 4px; min-width: 0; }
    #sp-width .btn { height: 28px; padding: 0 6px; font-size: 12px; flex: none; }
    #sp-width .btn small { margin-left: 2px; }
    #sp-go { height: 46px; font-size: 15px; width: 100%; }
    #sp-why { font-size: 12.5px; line-height: 1.45; }
    #sp-why .hd { font-size: 14px; font-weight: 700; margin-bottom: 3px; }
    #sp-why .bull, .sp-whybox .bull { color: var(--green); } #sp-why .bear, .sp-whybox .bear { color: var(--red); }
    .sp-whybox { font-size: 12.5px; line-height: 1.45; } .sp-whybox .hd { font-size: 14px; font-weight: 700; margin-bottom: 3px; }
    #sp-why div + div { margin-top: 2px; }
    #sp-risk .btn { height: 30px; padding: 0 10px; font-size: 12px; }
    .sp-legs { width: 100%; border-collapse: collapse; font-size: 13px; margin: 2px 0 4px; }
    .sp-legs th { text-align: left; color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; padding: 4px 8px; }
    .sp-legs td { padding: 5px 8px; border-top: 1px solid var(--line); font-family: "JetBrains Mono", Consolas, monospace; }
    .sp-bar { position: relative; height: 8px; border-radius: 4px; margin: 26px 8px 22px;
              background: linear-gradient(90deg, rgba(229,83,75,.55), rgba(229,83,75,.15) var(--z), rgba(63,182,139,.15) var(--z), rgba(63,182,139,.55)); }
    .sp-bar .mk { position: absolute; top: -5px; width: 18px; height: 18px; margin-left: -9px; border-radius: 50%; background: #fff; border: 3px solid var(--blue); }
    .sp-bar .cap { position: absolute; top: -22px; transform: translateX(-50%); font-size: 11px; white-space: nowrap; }
    .sp-bar .end { position: absolute; top: 12px; font-size: 11px; color: var(--muted); white-space: nowrap; }
    .sp-work .why { font-size: 12px; color: var(--muted); }
    .sp-work .seg { width: auto; }
    .sp-work .arow > .why, .sp-work .arow > .warnline { grid-column: 1 / -1; }
    @media (min-width: 1600px) {
      main:has(#ticket.spread-mode) { grid-template-columns: 340px minmax(0, 1fr) 440px; }
      .layout-cockpit main:has(#ticket.spread-mode) { grid-template-columns: 300px minmax(0, 1fr) 460px; } }
    @media (min-width: 1300px) and (max-width: 1599px) {
      main:has(#ticket.spread-mode) { grid-template-columns: 300px minmax(0, 1fr) 390px; }
      .layout-cockpit main:has(#ticket.spread-mode) { grid-template-columns: 270px minmax(0, 1fr) 410px; } }
    #sp-exp { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px; }
    #sp-exp .btn { height: 26px; padding: 0 4px; font-size: 11.5px; min-width: 0; justify-content: center; white-space: nowrap; overflow: hidden; }
    #sp-exp > span { grid-column: 1 / -1; }
    #sp-how { font-size: 12.5px; margin-top: -4px; }
    .sp-q { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 15px; height: 15px;
            margin-left: 4px; border: 1px solid var(--muted); border-radius: 50%; font-size: 10px; cursor: help; vertical-align: 1px; }
    .sp-q .sp-tip { display: none; position: absolute; right: -110px; top: 20px; z-index: 60; width: 300px; padding: 8px 10px;
            background: var(--panel2); border: 1px solid var(--line); border-radius: 6px; box-shadow: 0 6px 18px rgba(0,0,0,.45);
            color: var(--text); font-size: 12px; font-weight: 400; line-height: 1.45; letter-spacing: 0; text-transform: none; }
    .sp-q:hover .sp-tip, .sp-q:focus .sp-tip { display: block; }
    .sp-q .sp-tip b { color: var(--text); }
    .sp-q .sp-tip div + div { margin-top: 4px; }`;
  document.head.appendChild(css);

  const asset = $("t-asset");
  const btn = document.createElement("button");
  btn.type = "button"; btn.dataset.v = "SPREAD"; btn.textContent = "Spread";
  asset.appendChild(btn);

  const box = document.createElement("div");
  box.id = "t-spread";
  box.innerHTML = `
    <div class="sp-row3">
      <div class="fld"><span class="lbl">Underlying</span><input class="in" id="sp-und" autocomplete="off" spellcheck="false" placeholder="SPX, META..."></div>
      <div class="fld"><span class="lbl">Spread</span><div class="seg full" id="sp-kind"><button type="button" data-v="DEBIT" title="Debit: you buy the spread">Debit</button><button type="button" data-v="CREDIT" class="on" title="Credit: you sell the spread">Credit</button></div></div>
      <div class="fld"><span class="lbl">Type</span><div class="seg full" id="sp-pc"><button type="button" data-v="CALL">Calls</button><button type="button" data-v="PUT" class="on">Puts</button></div></div>
    </div>
    <div class="fld"><span class="lbl sp-explbl">Expiry<span class="tk-name" id="sp-name"></span></span><div class="expchips" id="sp-exp"><span class="mut" style="font-size:12px">Type the underlying.</span></div></div>
    <div class="hint" id="sp-how"></div>
    <div class="chain" id="sp-chain"><div class="empty">Pick an expiry.</div></div>
    <div class="row2">
    <div class="fld"><span class="lbl">Width (the other strike)</span><div class="chips" id="sp-width"><span class="mut" style="font-size:12px">Pick a strike first.</span></div></div>
    <div class="fld"><span class="lbl">Net price<span class="sp-q" tabindex="0" aria-label="What Mid, Natural and Lmt mean">?<span class="sp-tip">${
      ["mid", "natural", "limit"].map(k => `<div>${esc(PRICE_HELP[k]).replace(/^(\w+):/, "<b>$1:</b>")}</div>`).join("")}</span></span></span><div class="seg full" id="sp-entry"><button type="button" data-v="mid" class="on" title="Halfway between each leg's bid and ask: a better price, but it may not fill">Mid</button><button type="button" data-v="natural" title="The market's price right now on both legs (sell at the bid, buy at the ask): priced to fill at once">Natural</button><button type="button" data-v="limit" title="Your own net price: fills at that price or better">Lmt</button></div></div>
    </div>
    <div class="fld" id="sp-lmtfld" style="display:none"><span class="lbl">Limit (net per spread)</span><input class="in" id="sp-lmt" inputmode="decimal" autocomplete="off" placeholder="net price"></div>
    <div class="row2">
      <div class="fld"><span class="lbl">Size by</span><div class="seg full" id="sp-size"><button type="button" data-v="qty" class="on">Quantity</button><button type="button" data-v="risk">Risk $</button></div></div>
      <div class="fld" id="sp-qtyfld"><span class="lbl">Spreads</span><input class="in" id="sp-qty" inputmode="numeric" autocomplete="off" value="1"></div>
    </div>
    <div class="fld" id="sp-riskfld" style="display:none"><span class="lbl">Risk $ (the max loss of all the spreads together)</span>
      <div class="chips"><span class="chips" id="sp-risk" style="flex:1"></span><input class="in" id="sp-riskc" inputmode="decimal" placeholder="other $" style="width:90px"></div></div>
    <div class="planbox" id="sp-why" style="display:none"></div>
    <div class="planbox" id="sp-calc"><span class="mut">Pick the underlying, expiry and strikes.</span></div>
    <div class="sp-act"><button type="button" class="btn spx" id="sp-prev">Check with Schwab</button><button id="sp-go" class="btn buy spx" type="button" disabled>Send spread</button></div>
    <div class="tk-msg" id="sp-prevmsg"></div>
    <div class="tk-msg" id="sp-msg"></div>
    <div class="hint">DAY limit, up to 20. No stop at Schwab: the most it can lose is the max loss.</div>`;
  asset.insertAdjacentElement("afterend", box);


  // ---- mode switch (Stock | Option | Spread) ------------------------------------------------
  function setMode(on) {
    S.on = on;
    $("ticket").classList.toggle("spread-mode", on);
    [...asset.children].forEach(x => x.classList.toggle("on", on ? x === btn : x.dataset.v === T.asset));
    if (on) { renderHow(); schedule(50); if (!S.und) $("sp-und").focus(); }
  }
  asset.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    if (b === btn) { e.stopImmediatePropagation(); if (!S.on) setMode(true); return; }
    if (S.on) setMode(false);       // the page's own handler then switches Stock / Option as usual
  }, true);

  // ---- chain picker ---------------------------------------------------------------------------
  const seg = (id, v) => [...$(id).children].forEach(x => x.classList.toggle("on", x.dataset.v === v));
  function renderHow() {
    const sell = S.kind === "CREDIT", st = STRAT[`${S.pc}|${S.kind}`];
    $("sp-how").innerHTML = `<b class="${st.cls === "bull" ? "pos" : "neg"}">${st.name}: ${st.bias}</b>`;
    $("sp-how").title = `Wins ${st.how}. ` + (sell
      ? "Click the strike you SELL (nearer the money), then the width: the strike you buy is further out."
      : "Click the strike you BUY (nearer the money), then the width: the strike you sell is further out.");
  }
  function rows() { return ((S.chain && S.chain.rows) || []).filter(r => r.standard !== false); }
  function outward(near) {        // strikes further out of the money than the near one, nearest first
    const rs = rows().filter(r => S.pc === "CALL" ? r.strike > near.strike : r.strike < near.strike);
    return S.pc === "CALL" ? rs : rs.reverse();
  }
  function renderChain() {
    const el = $("sp-chain"), c = S.chain;
    if (!S.exp) { el.innerHTML = `<div class="empty">${S.exps.length ? "Pick an expiry." : "Type the underlying."}</div>`; return; }
    if (!c) { el.innerHTML = `<div class="empty">Loading strikes...</div>`; return; }
    if (c.error) { el.innerHTML = `<div class="empty neg">${esc(c.error)}</div>`; return; }
    const rs = rows();
    if (!rs.length) { el.innerHTML = `<div class="empty">No strikes for this expiry.</div>`; return; }
    const und = c.underlying_price, cp = S.pc === "CALL" ? "C" : "P";
    let atm = -1, best = Infinity;
    rs.forEach((r, i) => { const d = Math.abs(r.strike - und); if (und && d < best) { best = d; atm = i; } });
    el.innerHTML = `<table><thead><tr><th>Strike</th><th>Bid</th><th>Ask</th><th>Mark</th><th>Δ</th><th>OI</th></tr></thead><tbody>
      ${rs.map((r, i) => `<tr class="row${r.itm ? " itm" : ""}${S.near && S.near.symbol === r.symbol ? " near" : ""}${S.far && S.far.symbol === r.symbol ? " far" : ""}${i === atm ? " atm" : ""}" data-sym="${esc(r.symbol)}">
        <td>${fmtK(r.strike)}${cp}${S.near && S.near.symbol === r.symbol ? ` <span style="font-size:10px">${S.kind === "CREDIT" ? "SELL" : "BUY"}</span>` : S.far && S.far.symbol === r.symbol ? ` <span style="font-size:10px">${S.kind === "CREDIT" ? "BUY" : "SELL"}</span>` : i === atm ? ` <span class="mut" style="font-size:10px">ATM</span>` : ""}</td>
        <td>${px(r.bid)}</td><td>${px(r.ask)}</td><td>${px(r.mark)}</td>
        <td>${r.delta == null ? "-" : r.delta.toFixed(2).replace(/^(-?)0\./, "$1.")}</td>
        <td>${r.oi == null ? "-" : r.oi >= 1000 ? (r.oi / 1000).toFixed(1) + "k" : r.oi}</td></tr>`).join("")}</tbody></table>`;
    const sel = el.querySelector("tr.near") || el.querySelector("tr.atm");
    if (sel && !el.dataset.scrolled) { el.scrollTop = Math.max(0, sel.offsetTop - el.clientHeight / 2); el.dataset.scrolled = "1"; }
  }
  function renderWidths() {
    const el = $("sp-width");
    if (!S.near) { el.innerHTML = `<span class="mut" style="font-size:12px">Pick a strike first.</span>`; return; }
    const out = outward(S.near).slice(0, 4);
    if (!out.length) { el.innerHTML = `<span class="mut" style="font-size:12px">No strike further out in this list.</span>`; return; }
    el.innerHTML = out.map(r => `<button type="button" class="btn${S.far && S.far.symbol === r.symbol ? " on" : ""}" data-sym="${esc(r.symbol)}">${fmtK(Math.abs(r.strike - S.near.strike))} <small class="mut">${fmtK(r.strike)}</small></button>`).join("");
  }
  function renderExps() {
    const el = $("sp-exp");
    if (!S.und) { el.innerHTML = `<span class="mut" style="font-size:12px">Type the underlying.</span>`; return; }
    if (S.exps.error) { el.innerHTML = `<span class="neg" style="font-size:12px">${esc(S.exps.error)}</span>`; return; }
    if (!S.exps.length) { el.innerHTML = `<span class="mut" style="font-size:12px">Loading expiries...</span>`; return; }
    const day = e => new Date(e.date + "T12:00:00");
    const lab = e => e.dte === 0 ? "0DTE" : `${day(e).getDate()} ${MONTHS[day(e).getMonth()]}`;
    el.innerHTML = S.exps.slice(0, 10).map(e => `<button type="button" class="btn${e.date === S.exp ? " on" : ""}" data-exp="${e.date}"
      title="${day(e).toLocaleDateString("en-US", {weekday: "long"})} ${e.date}">${lab(e)}${e.dte ? `<small>${e.dte}d</small>` : ""}</button>`).join("");
  }
  function showName() {
    const c = S.chain;
    $("sp-name").innerHTML = c && !c.error && c.underlying === S.und
      ? `<b>${esc(c.underlying_name || c.underlying)}</b> · <span class="num" style="color:var(--text)">${px(c.underlying_price)}</span>` : "";
  }
  async function loadExps() {
    const und = $("sp-und").value.trim().toUpperCase();
    if (und === S.und && S.exps.length) return;
    S.und = und; S.exps = []; S.exp = null; S.chain = null; S.near = S.far = null; delete $("sp-chain").dataset.scrolled;
    renderExps(); renderChain(); renderWidths(); showName(); edited();
    if (!und) return;
    const seq = ++S.chainSeq;
    try {
      const r = await post("/api/ticket/expirations", {underlying: und});
      if (seq !== S.chainSeq) return;
      if (!r.ok) { S.exps = []; S.exps.error = r.error || "no expiries"; renderExps(); return; }
      S.exps = r.expirations.filter(e => e.standard !== false);
      S.exp = S.exps.length ? S.exps[0].date : null;
      renderExps(); loadChain();
    } catch (e) { S.exps = []; S.exps.error = "companion not reachable"; renderExps(); }
  }
  async function loadChain(quiet) {
    if (!S.und || !S.exp) { renderChain(); return; }
    if (!quiet) { S.chain = null; renderChain(); }
    const seq = ++S.chainSeq, key = `${S.und}|${S.exp}|${S.pc}`;
    try {
      const r = await post("/api/ticket/chain", {underlying: S.und, expiration: S.exp, put_call: S.pc, strikes: 24});
      if (seq !== S.chainSeq || key !== `${S.und}|${S.exp}|${S.pc}`) return;
      if (!r.ok) { if (!quiet) S.chain = {error: r.error || "no strikes"}; }
      else { S.chain = r; S.chainAt = Date.now(); }
    } catch (e) { if (!quiet && seq === S.chainSeq) S.chain = {error: "companion not reachable"}; }
    const el = $("sp-chain"), top = el.scrollTop;
    if (!quiet) delete el.dataset.scrolled;
    renderChain(); if (quiet) el.scrollTop = top;
    renderWidths(); showName();
  }
  let undTimer = null;
  $("sp-und").addEventListener("input", () => { clearTimeout(undTimer); undTimer = setTimeout(loadExps, 400); });
  $("sp-exp").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    S.exp = b.dataset.exp; S.near = S.far = null; renderExps(); loadChain(); renderWidths(); edited();
  });
  $("sp-pc").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b || b.dataset.v === S.pc) return;
    S.pc = b.dataset.v; S.near = S.far = null; seg("sp-pc", S.pc); renderHow(); loadChain(); renderWidths(); edited();
  });
  $("sp-kind").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b || b.dataset.v === S.kind) return;
    S.kind = b.dataset.v; seg("sp-kind", S.kind); renderHow(); renderChain(); edited();
  });
  $("sp-chain").addEventListener("click", e => {
    const tr = e.target.closest("tr.row"); if (!tr) return;
    const r = rows().find(x => x.symbol === tr.dataset.sym); if (!r) return;
    if (S.far && r.symbol === S.far.symbol) return;
    S.near = r;
    const out = outward(r);
    if (!S.far || !out.some(x => x.symbol === S.far.symbol)) S.far = out[0] || null;   // narrowest by default
    renderChain(); renderWidths(); edited();
  });
  $("sp-width").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    S.far = rows().find(x => x.symbol === b.dataset.sym) || S.far; renderChain(); renderWidths(); edited();
  });
  $("sp-entry").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    S.entry = b.dataset.v; seg("sp-entry", S.entry); renderHow();
    $("sp-lmtfld").style.display = S.entry === "limit" ? "" : "none";
    if (S.entry === "limit" && !$("sp-lmt").value.trim() && S.plan && S.plan.limit) $("sp-lmt").value = S.plan.limit;
    edited();
    if (S.entry === "limit") $("sp-lmt").focus();
  });
  $("sp-lmt").addEventListener("input", () => edited());
  function riskChoices() {
    return (typeof RISK_CHOICES !== "undefined" && RISK_CHOICES) || lastState?.trading?.risk_choices || [250, 375, 500, 750];
  }
  function renderSize() {
    seg("sp-size", S.size);
    $("sp-qtyfld").style.display = S.size === "qty" ? "" : "none";
    $("sp-riskfld").style.display = S.size === "risk" ? "" : "none";
    $("sp-risk").innerHTML = riskChoices().map(v => `<button type="button" class="btn${S.risk === v && !$("sp-riskc").value.trim() ? " on" : ""}" data-v="${v}">$${v}</button>`).join("");
  }
  $("sp-size").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; S.size = b.dataset.v; renderSize(); edited(); });
  $("sp-risk").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; S.risk = Number(b.dataset.v); $("sp-riskc").value = ""; renderSize(); edited(); });
  $("sp-qty").addEventListener("input", () => edited());
  $("sp-riskc").addEventListener("input", () => { renderSize(); edited(); });

  // ---- plan, preview, send ----------------------------------------------------------------------
  function req() {
    return {underlying: S.und, kind: S.kind, near: S.near ? S.near.symbol : "", far: S.far ? S.far.symbol : "",
            entry: S.entry, limit_price: S.entry === "limit" ? $("sp-lmt").value.trim() || null : null,
            size_mode: S.size, qty: S.size === "qty" ? $("sp-qty").value.trim() : null,
            risk: S.size === "risk" ? (parseFloat($("sp-riskc").value) || S.risk) : null};
  }
  const keyOf = q => [q.underlying, q.kind, q.near, q.far, q.entry, q.limit_price, q.size_mode, q.qty, q.risk].join("|");
  function edited() {
    const k = keyOf(req());
    if (k !== S.intentKey) { S.intentKey = k; S.ticketId = crypto.randomUUID(); S.preview = null; $("sp-prevmsg").textContent = ""; $("sp-msg").textContent = ""; }
    schedule(250);
  }
  function schedule(ms) { clearTimeout(S.timer); S.timer = setTimeout(refresh, ms); }
  async function refresh() {
    if (!S.on) return;
    const q = req();
    if (!q.near || !q.far) { S.plan = null; renderPlan(); return; }
    if (S.busy) { schedule(1000); return; }
    const seq = ++S.seq;
    try {
      const p = await post("/api/spread/plan", q);
      if (seq !== S.seq) return;
      S.plan = {...p, key: keyOf(q)};
    } catch (e) { if (seq === S.seq) S.plan = {ok: false, errors: ["companion not reachable"], warnings: []}; }
    renderPlan();
    schedule(document.hasFocus() ? 5000 : 30000);
  }
  function legTxt(l, n) { return `${l.side === "LONG" ? "Buy" : "Sell"} ${n || "?"} ${esc(optName(l.symbol))} <span class="mut">${px(l.bid)} x ${px(l.ask)}</span>`; }
  // The trade in plain words, so the direction and the outcome are checked before sending.
  function whyHtml(p) {
    const st = STRAT[`${p.put_call}|${p.kind}`], und = S.chain && !S.chain.error ? S.chain.underlying_price : null;
    if (!st || !p.ok) return "";
    const [n, f] = p.legs, kn = fmtK(n.strike), kf = fmtK(f.strike), call = p.put_call === "CALL";
    const sym = esc(p.underlying), d = new Date(p.expiration + "T12:00:00");
    const exp = `${d.getDate()} ${MONTHS[d.getMonth()]}`;
    const above = call ? "below" : "above", beyond = call ? "above" : "below";
    const lines = [];
    if (p.kind === "CREDIT")
      lines.push(`<div>Collect <b>${money(Number(p.max_gain), false)}</b>, kept if ${sym} is <b>${above} ${kn}</b> on ${exp}. Max loss <b class="neg">${money(Number(p.max_loss), false)}</b> at or ${beyond} ${kf}.</div>`);
    else
      lines.push(`<div>Pay <b>${money(Number(p.max_loss), false)}</b> (the max loss). Max gain <b class="pos">${money(Number(p.max_gain), false)}</b> at or ${call ? "above" : "below"} ${kf} on ${exp}.</div>`);
    const facts = [`Breakeven <b>${p.breakeven}</b>`];
    if (und) {
      const be = Number(p.breakeven), pct = (und - be) / und * 100, bull = st.bias === "BULLISH", room = bull ? pct : -pct;
      facts.push(`${sym} ${px(und)}: ${room >= 0 ? `${Math.abs(pct).toFixed(1)}% cushion` : `needs ${Math.abs(pct).toFixed(1)}% ${bull ? "up" : "down"}`}`);
    }
    const short = rows().find(r => r.symbol === (p.kind === "CREDIT" ? n.symbol : f.symbol));
    if (short && short.delta != null)
      facts.push(`<span class="mut" title="The market's rough odds that the short ${kind1(p, short)} finishes in the money">short Δ ${Math.abs(short.delta).toFixed(2)} (~${Math.round(Math.abs(short.delta) * 100)}% ITM)</span>`);
    lines.push(`<div>${facts.join(" · ")}</div>`);
    return lines.join("");
  }
  const kind1 = (p, r) => `${fmtK(r.strike)}${p.put_call === "CALL" ? "C" : "P"}`;
  function renderPlan() {
    const p = S.plan, calc = $("sp-calc"), go = $("sp-go"), why = $("sp-why");
    const live = HEALTH === "live";
    const w = p && p.ok ? whyHtml(p) : "";
    why.style.display = w ? "" : "none"; why.innerHTML = w;
    if (!p) { calc.innerHTML = `<span class="mut">Pick the underlying, expiry and strikes.</span>`; }
    else if (!p.legs && !p.ok) { calc.innerHTML = (p.errors || []).map(e => `<div class="err">${esc(e)}</div>`).join("") || `<span class="mut">...</span>`; }
    else {
      const kt = KIND_TXT[p.kind] || "";
      calc.innerHTML = (p.legs || []).map(l => `<div class="pr"><span>${legTxt(l, p.qty)}</span></div>`).join("")
        + (p.mid != null ? `<div class="pr"><span class="mut">Net mid / natural</span><span class="num">${p.mid} / ${p.natural}</span></div>` : "")
        + (p.ok ? `<div class="pr tot"><span>Limit (${kt})</span><span class="num">${p.limit}</span></div>
          <div class="pr"><span>Max loss (1R)${p.qty > 1 ? ` <span class="mut">${p.qty} × ${money(Number(p.max_loss_each), false)}</span>` : ""} <span class="mut">· gain</span></span><span class="num"><span class="neg">${money(-Number(p.max_loss))}</span> <span class="mut">/</span> <span class="pos">${money(Number(p.max_gain))}</span></span></div>` : "")
        + (p.errors || []).map(e => `<div class="err">${esc(e)}</div>`).join("")
        + (p.warnings || []).map(w => `<div class="amb" style="font-size:12px">${esc(w)}</div>`).join("");
    }
    const ready = p && p.ok && p.key === keyOf(req());
    const spent = S.sentKey && S.sentKey === S.intentKey;
    go.disabled = !ready || S.busy || !T.armed || !live || spent;
    go.className = "btn spx " + (S.kind === "CREDIT" ? "sell" : "buy");
    go.textContent = S.busy ? "Working..." : !T.armed ? "Entries disarmed" : spent ? "Sent"
      : ready ? `${S.kind === "CREDIT" ? "Sell" : "Buy"} ${p.qty} ${p.put_call === "CALL" ? "Call" : "Put"} ${p.kind === "CREDIT" ? "Credit" : "Debit"} Spread${p.qty === 1 ? "" : "s"}`
      : "Send spread";
    go.title = spent ? "Sent: change the ticket for a new one" : ready ? `${p.qty} at ${p.limit} net, max loss ${money(Number(p.max_loss), false)}` : "";
    $("sp-prev").disabled = !ready || S.busy || !live;
  }
  $("sp-prev").addEventListener("click", async () => {
    const p = S.plan; if (!p || !p.ok) return;
    const m = $("sp-prevmsg");
    m.className = "tk-msg warn"; m.textContent = "Asking Schwab (preview, places nothing)...";
    try {
      const r = await post("/api/spread/preview", {...req(), entry: "limit", limit_price: p.limit, size_mode: "qty", qty: p.qty});
      if (r.ok) {
        const b = r.balance || {};
        m.className = "tk-msg ok";
        m.textContent = `Schwab accepts it at ${r.plan.limit}. Buying power after: ${b.projectedBuyingPower != null ? money(b.projectedBuyingPower, false) : "unavailable"}`
          + (b.projectedCommission != null ? ` · commission ${money(b.projectedCommission, false)}` : "");
      } else { m.className = "tk-msg bad"; m.textContent = r.error || "Schwab did not accept it."; }
    } catch (e) { m.className = "tk-msg bad"; m.textContent = "companion not reachable"; }
  });
  $("sp-go").addEventListener("click", async () => {
    const p = S.plan;
    if (S.busy || !p || !p.ok || p.key !== keyOf(req()) || HEALTH !== "live") return;
    const legs = p.legs.map(l => `${l.side === "LONG" ? "Buy" : "Sell"} ${p.qty} ${optName(l.symbol)}`).join("\n");
    const st = STRAT[`${p.put_call}|${p.kind}`];
    if (!confirm(`${st.name.toUpperCase()}: ${st.bias} on ${p.underlying}\n\n${legs}\n\n${KIND_TXT[p.kind]} ${p.limit} per spread (DAY limit) · breakeven ${p.breakeven}\nMax loss $${p.max_loss} · max gain $${p.max_gain}. No stop at Schwab.\n\nSend it?`)) return;
    S.busy = true; renderPlan();
    const msg = $("sp-msg");
    msg.className = "tk-msg warn"; msg.textContent = "Sending to Schwab (preview first)...";
    try {
      const r = await post("/api/spread/submit", {ticket_id: S.ticketId, underlying: p.underlying, kind: p.kind,
                                                 near: req().near, far: req().far,
                                                 expected_limit: p.limit, expected_max_loss: p.max_loss, expected_qty: p.qty});
      if (r.ok) {
        S.sentKey = S.intentKey;
        msg.className = "tk-msg ok";
        msg.textContent = r.duplicate ? `Already sent: order ${r.order_id}.` : `Sent: order ${r.order_id}, ${KIND_TXT[p.kind]} ${p.limit}. It shows in Open trades.`;
        if (r.spread_id) selectSpread(r.spread_id);
      } else {
        if (r.status === "RATE_LIMITED") S.ticketId = crypto.randomUUID();
        else if (r.status || r.duplicate || r.spread_id) S.sentKey = S.intentKey;
        msg.className = "tk-msg " + (r.status === "UNKNOWN" || r.status === "RATE_LIMITED" ? "warn" : "bad");
        msg.textContent = r.error || "Not sent.";
      }
    } catch (e) {
      msg.className = "tk-msg warn";
      msg.textContent = "Lost contact with the companion while sending. Check thinkorswim. Clicking again only reports the outcome.";
    }
    S.busy = false; renderPlan();
  });

  // ---- Open trades list and the spread workbench (center panel) ------------------------------------
  // A companion spread is ONE row in Open trades and its legs are hidden from the list, so nothing acts
  // on a leg alone from there. Selecting the row shows the spread in the center panel: legs, P&L,
  // Live R against the max loss, and its own Close / Cancel / Resolve.
  const TAGS = {PENDING: "waiting", PARTIAL: "partial", OPEN: "protected", CLOSING: "partial", SENDING: "unknown",
                UNKNOWN: "unknown", NEEDS_ACTION: "unprotected", BROKEN: "unprotected", EXPIRED_CHECK: "unknown"};
  const CAN_CANCEL = new Set(["PENDING", "PARTIAL", "CLOSING"]);
  const CAN_RESOLVE = new Set(["BROKEN", "EXPIRED_CHECK", "NEEDS_ACTION", "UNKNOWN", "SENDING"]);
  let SPSEL = null, LAST_SEL = null;      // LAST_SEL: the page's selection as of our last render
  // The close row: price mode, typed limit, the plan for exactly those inputs, and the last result.
  const W = {sid: null, entry: "natural", limit: "", plan: null, seq: 0, tpct: "0.5", tprice: "", tplan: null, tseq: 0,
                   sr: "0.5", sprice: "", splan: null, sseq: 0, ssend: false, cqty: ""};
  const stopKey = () => `${W.sid}|${W.sprice ? "px:" + W.sprice : "r:" + W.sr}`;
  const targetKey = () => `${W.sid}|${W.tprice ? "px:" + W.tprice : "pct:" + W.tpct}`;
  const MSGS = {}, BUSY = new Set();     // per spread: the last action's result, and an action in flight
  const closeKey = () => `${W.sid}|${W.entry}|${W.entry === "limit" ? W.limit : ""}`;

  function parts(sp) {
    const m = sp.legs.map(l => /^([A-Z0-9.]+)\s*(\d\d)(\d\d)(\d\d)([CP])(\d{8})$/.exec(l.symbol));
    if (!m.every(Boolean)) return {root: sp.underlying, date: "", strikes: sp.legs.map(l => l.symbol.trim()).join(" / ")};
    const [a] = m;
    return {root: a[1], date: `${+a[4]} ${MONTHS[+a[3] - 1]} ${a[2]}`, exp: `20${a[2]}-${a[3]}-${a[4]}`, nearK: +a[6] / 1000,
            strikes: m.map(x => fmtK(+x[6] / 1000)).join("/") + a[5]};
  }
  const kindTxt = sp => sp.kind.replace("_", " ").toLowerCase();             // "put credit"
  const legPos = (sp, st) => sp.legs.map(l => (st.positions || []).find(p => p.symbol === l.symbol) || null);
  const held = (sp, st) => { const [n] = legPos(sp, st); return n ? Math.abs(n.qty) : 0; };   // spreads held now
  const qtyTxt = (sp, st) => { const h = held(sp, st);
    return h && h !== sp.qty ? `${h} of ${sp.qty}` : `${sp.qty}`; };
  // Open P&L from each leg's live mark (quotes refresh every 3 s). Schwab's own position P&L for options
  // can sit stale for minutes while the marks move, so it is only the fallback.
  const legMark = p => p?.quote?.mark ?? p?.last;
  function legPnl(p) {
    const m = p.quote?.mark;
    if (m == null || p.avg_price == null || !p.qty) return p.open_pnl;
    return (p.side === "SHORT" ? -1 : 1) * (m - p.avg_price) * Math.abs(p.qty) * (p.multiplier || 100);
  }
  function pnlOf(sp, st) {
    const ps = legPos(sp, st);
    if (ps.some(p => !p)) return null;
    const v = ps.map(legPnl);
    return v.some(x => x == null) ? null : v.reduce((a, x) => a + x, 0);
  }
  function valueOf(sp, st) {         // what the spread is worth now (near leg minus far leg), at the legs' marks:
    const [n, f] = legPos(sp, st);   // a thin option's last trade can be old (often your own fill)
    const m = p => p?.quote?.mark ?? p?.last;
    return n && f && m(n) != null && m(f) != null ? m(n) - m(f) : null;
  }
  // The ticket's numbers are at its LIMIT; once both legs are held, the legs' average fill prices give the
  // real net entry (a limit can fill better), and max loss, max gain, breakeven and R follow from it.
  function terms(sp, st) {
    const t = sp.ticket || {}, [n, f] = legPos(sp, st), credit = sp.kind.endsWith("CREDIT");
    if (!n || !f || n.avg_price == null || f.avg_price == null || t.width == null)
      return {entry: t.entry_limit, max_loss: t.max_loss, max_gain: t.max_gain, breakeven: t.breakeven, basis: "limit"};
    const e = Math.abs(n.avg_price - f.avg_price), w = t.width, k = parts(sp).nearK, q = (Math.abs(n.qty) || sp.qty) * 100;
    return {entry: e, max_loss: (credit ? w - e : e) * q, max_gain: (credit ? e : w - e) * q, basis: "fill",
            breakeven: k == null ? t.breakeven : (sp.legs[0].symbol.trim().slice(-9, -8) === "C" ? k + e : k - e)};
  }
  const liveR = (tm, pnl) => pnl != null && tm.max_loss ? pnl / tm.max_loss : null;
  // What a close at this net price would bank: against the real entry, minus fees for the whole round trip.
  // Schwab charges $0.65 per option contract; regulatory fees add about a cent per contract ($0.6625 seen on real fills).
  const FEE = 0.6625;
  function banked(sp, st, price, qty) {
    const t = terms(sp, st), h = qty || held(sp, st);
    if (price == null || t.entry == null || !h) return null;
    const gross = (sp.kind.endsWith("CREDIT") ? t.entry - price : price - t.entry) * 100 * h;
    const fees = FEE * sp.legs.length * h * 2;
    return {gross, fees, net: gross - fees};
  }
  const dte = sp => sp.expiration ? Math.round((new Date(sp.expiration + "T16:00:00") - new Date(new Date().toDateString() + " 16:00:00")) / 864e5) : null;

  function listRow(sp, st) {
    const pt = parts(sp), pnl = pnlOf(sp, st), t = terms(sp, st), r = liveR(t, pnl), v = valueOf(sp, st);
    return `<div class="li${sp.spread_id === SPSEL ? " sel" : ""}" data-spsel="${esc(sp.spread_id)}">
      <div class="r1"><span class="sym">${esc(pt.root)}</span><span class="tag ${sp.kind.endsWith("CREDIT") ? "short" : "long"}">${esc(kindTxt(sp).toUpperCase())} ${qtyTxt(sp, st)}</span>
        ${sp.stop && ["fired", "sent"].includes(sp.stop.status) ? `<span class="tag" style="color:#fff;background:var(--red);border-color:var(--red)">STOP</span>` : ""}
        <span class="pl ${cls(pnl)}">${pnl == null ? "-" : money(pnl)}</span></div>
      <div class="r2"><span>${esc(pt.date)} ${esc(pt.strikes)} · ${t.entry != null ? Number(t.entry).toFixed(2) : "?"} / ${v == null ? "-" : v.toFixed(2)}</span>
        <span class="held">${r == null ? "" : `<span class="num ${cls(r)}">${fmtR(r)}</span>`}</span></div>
      <div class="r3"><span class="num">Max loss ${t.max_loss != null ? money(t.max_loss, false) : "?"}${dte(sp) != null ? ` · ${dte(sp)}d` : ""}</span>
        <span class="tag ${TAGS[sp.state] || ""}">${esc(sp.state.replace("_", " "))}</span></div></div>`;
  }

  function bar(t, pnl) {           // -1R (max loss) on the left, max gain on the right, the spread now
    if (!t.max_loss || t.max_gain == null) return "";
    const lo = -t.max_loss, hi = t.max_gain, at = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100));
    const z = at(0);
    return `<div class="sp-bar" style="--z:${z}%">
      ${pnl == null ? "" : `<span class="cap num ${cls(pnl)}" style="left:${at(pnl)}%">${money(pnl)} · ${fmtR(pnl / t.max_loss)}</span><span class="mk" style="left:${at(pnl)}%"></span>`}
      <span class="end num" style="left:0">max loss ${money(lo)} · -1R</span>
      <span class="end num" style="left:${z}%;transform:translateX(-50%)">0</span>
      <span class="end num" style="right:0">max gain ${money(hi)}</span></div>`;
  }

  // Watch-only stop: the app watches the spread's net mid and says "would stop out"; it never sends.
  function stopRow(sp) {
    const s = sp.stop;
    if (sp.state !== "OPEN" && !s) return "";
    const p = W.splan && W.splan.key === stopKey() ? W.splan : null;
    if (W.ssendFor !== (s ? s.set_at : null)) { W.ssend = !!(s && s.send); W.ssendFor = s ? s.set_at : null; }   // the switch shows the stop's own choice
    const toggle = s && p && p.ok && Number(s.price).toFixed(2) === p.limit && !!s.send !== W.ssend;
    const can = sp.state === "OPEN" && p && p.ok && !BUSY.has(sp.spread_id) && !(s && Number(s.price).toFixed(2) === p.limit);
    const now = !s ? "" : s.status === "watching" ? `<b>Watching ${Number(s.price).toFixed(2)}</b>${s.detail ? ": " + esc(s.detail) : ""}. `
      : s.status === "suspended" ? `<span class="amb"><b>Not watching right now</b>: ${esc(s.detail)}.</span> `
      : s.status === "paused" ? `<span class="mut">Paused: ${esc(s.detail)}.</span> `
      : s.status === "fired" ? `<span class="neg"><b>${s.send ? "Stop hit" : "Would have stopped out"}</b> at mid ${Number(s.fired_mid).toFixed(2)}.</span> `
      : s.status === "sent" ? `<span class="${outcomeOf(s) === "sent" ? "amb" : "neg"}"><b>Stop used</b> (${outcomeOf(s)}): ${esc(sentText(s))}.</span> ` : "";
    return `<div class="arow"><div class="al">Stop<small>${(s ? s.send : W.ssend) ? "app-watched, sends" : "app watch only"}</small></div><div class="ctl">
        ${sp.state === "OPEN" ? `<div class="seg">${["0.5", "0.75"].map(v =>
          `<button type="button" data-spsr="${v}" class="${!W.sprice && W.sr === v ? "on" : ""}">-${v}R</button>`).join("")}</div>
        <input class="in ms-input sp-spx" inputmode="decimal" placeholder="or price" value="${esc(W.sprice)}" style="width:84px">
        <label class="mut" style="font-size:12px;display:inline-flex;gap:4px;align-items:center" title="When it fires, the app sends ONE close of all the spread holds"><input type="checkbox" class="sp-ssend"${W.ssend ? " checked" : ""}> Send when hit</label>
        <button type="button" class="btn spx" data-spw="stopset" data-limit="${esc(p && p.ok ? p.limit : "")}"${can || (toggle && sp.state === "OPEN") ? "" : " disabled"}>${toggle ? (W.ssend ? "Turn Send on" : "Turn Send off") : p && p.ok ? `${s ? "Move stop to" : "Watch stop at"} ${p.limit}${s && !!s.send !== W.ssend ? (W.ssend ? " (Send on)" : " (Send off)") : ""}` : s ? "Move stop" : "Watch stop"}</button>` : ""}
        ${s ? `<button type="button" class="btn spx" data-spw="stopclear">Remove stop</button>` : ""}</div>
      <div class="why" style="margin-top:6px">${now}${sp.state !== "OPEN" ? "" : !p ? "Pricing..." : !p.ok ? `<span class="neg">${esc((p.errors || []).join("; "))}</span>`
        : `${p.side === "buy" ? "cost to close" : "value"} reaches ${p.limit}: ${fmtR(-p.r)}, ${money(Number(p.gross))} before fees`}.
        ${(sp.state === "OPEN" ? W.ssend : s && s.send) ? `<b class="neg">Send when hit:</b> when it fires, the app cancels the target and any working close, then sends ONE close of everything this spread holds at natural plus 2 steps (never past the max loss). Once, never retried, only while the app runs.`
          : `Watch-only: the app does not send this stop (Schwab refuses stops on spreads); it shows and says "would stop out".`}</div></div>`;
  }
  const OUTCOME = {
    sent: ["Stop sent", "Watch it fill below.", "stop sent"],
    unclear: ["Stop sent, outcome UNCLEAR", "The close MAY be live: check thinkorswim, do NOT close again, then Resolve.", "stop outcome unclear, check thinkorswim"],
    closed: ["Stop hit, already closed", "Nothing was left to close.", "spread already closed"],
    refused: ["Stop hit, NOT sent", null, null],      // advice from the spread's state now: refusedAdvice
  };
  // A refused (or watch-only) stop's advice depends on the spread NOW, never on when it fired: a spread
  // already closed (by a click, a target or a close that was working) must never read "close it yourself",
  // since a second close would open the opposite spread. [banner tail, spoken].
  function refusedAdvice(sp) {
    if (sp.state === "OPEN") return ["Nothing was sent: close it yourself if you still want out.", "stop not sent, close it"];
    if (sp.state === "CLOSING") return ["A close is already working: do NOT close again.", "stop not sent, a close is working"];
    if (sp.state === "CLOSED") return ["The spread is closed now: nothing to do. Do NOT close again (a second close would open the opposite spread).", "stop not sent, spread already closed"];
    return ["Check thinkorswim first: do NOT close again until it shows the spread still held, then Resolve.", "stop not sent, check thinkorswim"];
  }
  // Older stored outcomes carried the advice in their text: drop it, the page decides now.
  const sentText = s => String(s.sent_result || "").replace(/^\w+: /, "").replace(/;\s*close it yourself\.?/gi, "");
  const outcomeOf = s => (s.sent_result === "sending" || s.sent_result === "queued") ? "sending" : (String(s.sent_result || "").split(":")[0] in OUTCOME
    ? String(s.sent_result).split(":")[0] : "refused");
  let WHY_OPEN = true;
  try { WHY_OPEN = localStorage.getItem("sp-why") !== "0"; } catch (e) {}
  function scenarioHtml(sp, st) {
    const t = terms(sp, st), m = sp.legs.map(l => /([CP])(\d{8})$/.exec(l.symbol.trim()));
    const st0 = STRAT[`${m[0] && m[0][1] === "C" ? "CALL" : "PUT"}|${sp.kind.endsWith("CREDIT") ? "CREDIT" : "DEBIT"}`];
    if (!st0 || !m.every(Boolean) || t.entry == null || t.max_loss == null) return "";
    const call = m[0][1] === "C", kn = fmtK(+m[0][2] / 1000), kf = fmtK(+m[1][2] / 1000), pt = parts(sp);
    const h = held(sp, st) || sp.qty, sym = esc(pt.root), on = `at the close on ${esc(pt.date)}`, e = Number(t.entry).toFixed(2);
    const lines = [`<div class="hd ${st0.cls}">${st0.name} · ${st0.bias} <span class="mut" style="font-weight:400">(${h} held, entry ${e} ${t.basis === "fill" ? "filled" : "limit"})</span></div>`];
    if (sp.kind.endsWith("CREDIT")) {
      lines.push(`<div>You collected <b>${money(t.max_gain, false)}</b>. You keep it all if ${sym} is <b>${call ? "below" : "above"} ${kn}</b> ${on}.</div>`);
      lines.push(`<div>Breakeven <b>${Number(t.breakeven).toFixed(2)}</b>. At or ${call ? "above" : "below"} ${kf} you lose the most: <b class="neg">${money(t.max_loss, false)}</b>.</div>`);
    } else {
      lines.push(`<div>You paid <b>${money(t.max_loss, false)}</b>: the most you can lose, if ${sym} is ${call ? "below" : "above"} ${kn} ${on}.</div>`);
      lines.push(`<div>Breakeven <b>${Number(t.breakeven).toFixed(2)}</b>. At or ${call ? "above" : "below"} ${kf} you make the most: <b class="pos">${money(t.max_gain, false)}</b>.</div>`);
    }
    lines.push(`<div class="mut">Between ${kf} and ${kn} it is worth part of the width. Closing early sells it at what the market pays now (see Close).</div>`);
    return lines.join("");
  }
  function whyBox(sp, st) {
    const w = scenarioHtml(sp, st);
    if (!w) return "";
    return `<div style="margin:2px 0 8px"><button type="button" class="btn sm" data-spwhy="1">${WHY_OPEN ? "Hide" : "Show"} what can happen</button>
      ${WHY_OPEN ? `<div class="sp-whybox" style="margin-top:6px;padding:8px 10px;border:1px solid var(--line);border-radius:6px">${w}</div>` : ""}</div>`;
  }
  // Which stop this is, said loudly: an alert only, or an order the APP sends (none rests at Schwab).
  function stopModeBanner(sp) {
    const s = sp.stop;
    if (!s || !["watching", "suspended", "paused"].includes(s.status) || !["OPEN", "CLOSING"].includes(sp.state)) return "";
    const px2 = Number(s.price).toFixed(2), box = `margin-bottom:10px;padding:8px 10px;border:1px solid var(--red);background:var(--red-bg);color:var(--red);border-radius:6px;font-size:13px`;
    return s.send
      ? `<div style="${box}"><b>No stop order is at Schwab.</b> Edge Trader will SEND a close of everything held when the mid reaches ${px2}, only while the app is running and connected. If the app or the PC is off, nothing protects this spread. Use at your own risk.</div>`
      : `<div style="${box}"><b>Stop ${px2} is an ALERT only.</b> No order is at Schwab and none will be sent: when it hits, the app only warns you.${sp.state === "OPEN" ? " Close it yourself." : ""}</div>`;
  }
  function stopBanner(sp) {
    const s = sp.stop;
    if (s && s.status === "sent") {
      const o = outcomeOf(s), box = `margin-bottom:10px;padding:8px 10px;border:1px solid var(--red);background:var(--red-bg);color:var(--red);border-radius:6px;font-size:13px`;
      if (o === "sending") return `<div style="${box}"><b>Stop hit:</b> sending the close...</div>`;
      const head = OUTCOME[o][0], tail = o === "refused" ? refusedAdvice(sp)[0] : OUTCOME[o][1];
      return `<div style="${box}"><b>${head}:</b> ${esc(sentText(s))}. ${tail}</div>`;
    }
    if (!s || s.status !== "fired") return "";
    return `<div style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--red);background:var(--red-bg);color:var(--red);border-radius:6px;font-size:13px">
      <b>Would stop out now:</b> the mid reached ${Number(s.fired_mid).toFixed(2)} (stop ${Number(s.price).toFixed(2)}) at ${tET(s.fired_at)}.
      ${s.send ? "Sending the close..." : "Watch-only: nothing was sent. " + refusedAdvice(sp)[0].replace(/^Nothing was sent: c/, "C")}
      <button type="button" class="btn sm spx" data-spw="stopdismiss">Dismiss</button></div>`;
  }

  // GTC profit target: a paired close resting at Schwab at a share of the max gain (or a typed price).
  function targetRow(sp) {
    if (sp.state !== "OPEN") return "";
    const p = W.tplan && W.tplan.key === targetKey() ? W.tplan : null;
    const cp = W.plan && W.plan.key === closeKey() ? W.plan : null;
    const tw = ((cp && cp.working) || []).find(w => w.role === "target");
    const live = HEALTH === "live", open = !!lastState?.trading?.market_open;
    const b = p && p.ok ? banked(sp, lastState, Number(p.limit)) : null;
    const label = p && p.ok ? `${tw ? "Move target to" : "Set target at"} ${p.limit}` : tw ? "Move target" : "Set target";
    const samePx = tw && p && p.ok && tw.price != null && Number(tw.price).toFixed(2) === p.limit;
    return `<div class="arow"><div class="al">Target<small>GTC, rests at Schwab</small></div><div class="ctl">
        <div class="seg">${["0.25", "0.5", "0.75"].map(v =>
          `<button type="button" data-sptpct="${v}" class="${!W.tprice && W.tpct === v ? "on" : ""}">${Math.round(v * 100)}%</button>`).join("")}</div>
        <input class="in ms-input sp-tpx" inputmode="decimal" placeholder="or price" value="${esc(W.tprice)}" style="width:84px">
        <button type="button" class="btn spx" data-spw="target" data-limit="${esc(p && p.ok ? p.limit : "")}"${p && p.ok && live && open && !BUSY.has(sp.spread_id) && !samePx ? "" : " disabled"}${samePx ? ' title="The target already works at this price"' : ""}>${BUSY.has(sp.spread_id) ? "Working..." : label}</button>
        ${tw ? `<button type="button" class="btn danger spx" data-spw="canceltarget"${live && !BUSY.has(sp.spread_id) ? "" : " disabled"}>Cancel target</button>` : ""}</div>
      <div class="why" style="margin-top:6px">${tw ? `<b>Target working</b> at ${Number(tw.price).toFixed(2)} GTC${tw.filled ? `, ${tw.filled} of ${tw.quantity} filled` : ""}. ` : ""}${
        !p ? "Pricing..." : !p.ok ? `<span class="neg">${esc((p.errors || []).join("; "))}</span>`
        : `${p.side === "buy" ? "buy back" : "sell"} at ${p.limit}: ${p.pct != null ? Math.round(p.pct * 100) + "% of the max gain" : ""}${p.r != null ? ", " + fmtR(p.r) : ""}${b ? ` · <b class="${cls(b.net)}">banks ${money(b.net)}</b> after fees` : ""} · entry ${p.entry} (${p.basis})${(p.warnings || []).map(w => ` · <span class="amb">${esc(w)}</span>`).join("")}`}</div></div>`;
  }

  // How many the Close sends: the box, empty meaning all; null when the box holds nothing valid.
  function closeQty(sp, all) {
    if (!W.cqty) return all;
    const q = Number(W.cqty);
    return Number.isInteger(q) && q >= 1 && q <= all ? q : null;
  }
  function tgtNote(work, n, all) {
    const tw = work.find(w => w.role === "target");
    if (!tw) return "";
    const left = Math.round((tw.quantity || 0) - (tw.filled || 0));
    return n + left <= all ? "The target keeps working on the rest. "
      : n < all ? `<b>Cancels the target, then puts it back on the other ${all - n}.</b> ` : "<b>Cancels the target first.</b> ";
  }

  function closeRow(sp) {
    const p = W.plan && W.plan.key === closeKey() ? W.plan : null;
    const live = HEALTH === "live", open = !!lastState?.trading?.market_open;
    const work = (p && p.working) || [];
    const wc = work.find(w => w.role === "close"), we = work.find(w => w.role === "entry");
    const side = sp.kind.endsWith("DEBIT") ? "sell" : "buy back";
    if (CAN_CANCEL.has(sp.state) && sp.state !== "CLOSING") {
      return `<div class="arow"><div class="al">Entry<small>DAY limit</small></div><div class="ctl">
        <span class="why">Entry working${we && we.price != null ? ` at ${Number(we.price).toFixed(2)} ${sp.kind.endsWith("CREDIT") ? "credit" : "debit"}` : ""}${we && we.filled ? `, ${we.filled} of ${sp.qty} filled` : ""}: it fills at that price or better, or ends at the close.</span>
        <span class="gap"></span><button type="button" class="btn danger spx" data-spw="cancel"${live ? "" : " disabled"}>${we && we.filled ? "Cancel the rest" : "Cancel entry"}</button></div></div>`;
    }
    if (sp.state !== "OPEN" && sp.state !== "CLOSING") return "";
    const priced = p && p.ok;
    // Moving a close to the price it already works at would only cancel and resend it.
    const samePx = sp.state === "CLOSING" && priced && wc && wc.price != null && Number(wc.price).toFixed(2) === p.limit;
    const all = priced ? Math.round(p.held) : held(sp, lastState);
    if (!(sp.state === "OPEN" && all > 1)) W.cqty = "";        // no box shown: nothing typed can block Close
    const q = closeQty(sp, all);
    const rest = wc ? Math.round((wc.quantity || 0) - (wc.filled || 0)) : 0;    // a moved close keeps its size
    const n = sp.state === "CLOSING" ? (rest > 0 ? rest : all) : q;
    const label = sp.state === "CLOSING" ? `Move close of ${n} to ${priced ? p.limit : "..."}`
      : q == null ? "Close ..." : `Close ${q === all ? (all > 1 ? "all " + all : "") : q + " of " + all} at ${priced ? p.limit : "..."}`.replace("Close  at", "Close at");
    return `<div class="arow"><div class="al">Close<small>both legs, one order</small></div><div class="ctl">
        <div class="seg">${[["natural", "Natural"], ["mid", "Mid"], ["limit", "Lmt"]].map(([v, t]) =>
          `<button type="button" data-spentry="${v}" class="${W.entry === v ? "on" : ""}">${t}</button>`).join("")}</div>
        ${W.entry === "limit" ? `<input class="in ms-input sp-lmt" inputmode="decimal" placeholder="net" value="${esc(W.limit)}" style="width:84px">` : ""}
        ${sp.state === "OPEN" && all > 1 ? `<input class="in ms-input sp-cqty" inputmode="numeric" placeholder="all ${all}" value="${esc(W.cqty)}" title="How many spreads to close (empty: all)" style="width:70px">` : ""}
        <button type="button" class="btn danger-solid spx" data-spw="close" data-limit="${esc(priced ? p.limit : "")}" data-qty="${sp.state === "OPEN" && q != null ? q : ""}"${priced && live && open && !BUSY.has(sp.spread_id) && !samePx && (sp.state !== "OPEN" || q != null) ? "" : " disabled"}${samePx ? ' title="The close is already working at this price"' : ""}>${BUSY.has(sp.spread_id) ? "Working..." : label}</button></div>
      <div class="why" style="margin-top:6px">${!p ? "Quoting..." : !p.ok ? `<span class="neg">${esc((p.errors || []).join("; "))}</span>`
        : sp.state === "OPEN" && q == null ? `<span class="neg">Close 1 to ${all} spreads (empty: all).</span>`
        : `${tgtNote(work, n, all)}${side} ${n < all ? n + " of " + all + " " : ""}at ${p.limit} · mid ${p.mid} · natural ${p.natural}${(b => b ? ` · <b class="${cls(b.net)}">banks ${money(b.net)}</b> after about ${money(b.fees, false)} round-trip fees (${money(b.gross)} before fees)` : "")(banked(sp, lastState, Number(p.limit), n))} · ${W.entry === "natural" ? "natural is priced to fill at once (not guaranteed)" : "a limit at mid or your price can wait unfilled"} · DAY limit order`}</div>
      ${sp.state === "CLOSING" ? `<div class="warnline" style="margin-top:6px">Close working${wc && wc.price != null ? ` at ${Number(wc.price).toFixed(2)}` : ""}, not filled yet.
        <button type="button" class="btn sm danger spx" data-spw="cancel"${live ? "" : " disabled"}>Cancel close</button></div>` : ""}</div>`;
  }

  function workbench(sp, st) {
    const pt = parts(sp), pnl = pnlOf(sp, st), t = terms(sp, st), r = liveR(t, pnl), v = valueOf(sp, st);
    const ps = legPos(sp, st), d = dte(sp), msg = MSGS[sp.spread_id];
    const bp = W.sid === sp.spread_id && W.plan && W.plan.ok && W.plan.key === closeKey() && sp.state === "OPEN" ? W.plan : null;
    const bk = bp ? banked(sp, st, Number(bp.limit)) : null;
    const legs = sp.legs.map((l, i) => {
      const p = ps[i];
      return `<tr><td>${l.side === "LONG" ? "+" : "-"}${p ? Math.abs(p.qty) : 0}</td><td>${esc(optName(l.symbol))}</td>
        <td>${p ? px(p.avg_price) : "-"}</td><td>${p ? px(legMark(p)) : "-"}</td><td class="${p ? cls(legPnl(p)) : ""}">${p && legPnl(p) != null ? money(legPnl(p)) : "-"}</td></tr>`;
    }).join("");
    const resolve = CAN_RESOLVE.has(sp.state)
      ? `<button type="button" class="btn spx" data-spw="resolve">Resolve: I checked thinkorswim</button>` : "";
    return `<div class="lock">${esc(pt.root)} ${esc(pt.date)} ${esc(pt.strikes)} · ${esc(kindTxt(sp))}<small>companion spread</small></div>
    <div class="work sp-work" data-spid="${esc(sp.spread_id)}">
      ${stopBanner(sp)}${stopModeBanner(sp)}
      <div class="ident">
        <div><span class="sym">${esc(pt.root)} ${esc(pt.date)} ${esc(pt.strikes)}</span><span class="tags">
          <span class="tag ${sp.kind.endsWith("CREDIT") ? "short" : "long"}">${esc(kindTxt(sp).toUpperCase())} ${qtyTxt(sp, st)}</span>
          <span class="tag ${TAGS[sp.state] || ""}">${esc(sp.state.replace("_", " "))}</span></span>
          <div class="sub">companion spread · sent ${tET(sp.created)}${d != null ? ` · expires ${esc(sp.expiration)} (${d}d)` : ""} · no stop at Schwab: the most it can lose is the max loss</div></div>
        <div class="exitbox"><div class="pl ${cls(pnl)}" title="Open P&amp;L at the legs' marks (mid), before fees">${pnl == null ? "-" : money(pnl)}</div>
          ${bk ? `<div class="why" title="If the Close fills at its limit, after round-trip fees">if closed ${W.entry === "limit" ? "at your limit" : "at " + W.entry} ${bp.limit}: <b class="${cls(bk.net)}">${money(bk.net)}</b> net</div>` : ""}
          <div class="btns">${resolve}</div></div>
      </div>
      <div class="metrics">
        <div><div class="lbl">Entry (${t.basis}) / now</div><div class="v">${t.entry != null ? Number(t.entry).toFixed(2) : "?"} / ${v == null ? "-" : v.toFixed(2)}</div></div>
        <div><div class="lbl">Live R</div><div class="v xl ${r == null ? "mut" : cls(r)}">${r == null ? "-" : fmtR(r)}</div></div>
        <div><div class="lbl">Max loss (1R)</div><div class="v neg">${t.max_loss != null ? money(-t.max_loss) : "?"}</div></div>
        <div><div class="lbl">Max gain · breakeven</div><div class="v">${t.max_gain != null ? money(t.max_gain, false) : "?"} · ${t.breakeven != null ? Number(t.breakeven).toFixed(2) : "?"}</div></div>
      </div>
      ${bar(t, pnl)}
      ${whyBox(sp, st)}
      <table class="sp-legs"><thead><tr><th>Qty</th><th>Leg</th><th>Avg</th><th>Mark</th><th>P&amp;L</th></tr></thead><tbody>${legs}</tbody></table>
      <div class="acts">${stopRow(sp)}${targetRow(sp)}${closeRow(sp)}</div>
      <div class="result ${msg ? msg.cls : ""}">${msg ? `<b>Last result:</b> ${esc(msg.text)}` : esc(sp.detail || "")}</div>
    </div>`;
  }

  function selectSpread(sid) {
    SPSEL = sid;
    SEL = null; SEL_CLEARED = true;          // the page's own selection steps aside
    if (W.sid !== sid) Object.assign(W, {sid, entry: "natural", limit: "", plan: null, tpct: "0.5", tprice: "", tplan: null, sr: "0.5", sprice: "", splan: null, ssend: false, cqty: ""});
    LAST_SEL = null;
    closePlan();
    if (lastState) render(lastState);
  }
  async function closePlan() {
    const sp = (lastState?.spreads || []).find(x => x.spread_id === SPSEL);
    if (!sp || !["OPEN", "CLOSING", "PENDING", "PARTIAL"].includes(sp.state)) return;
    const seq = ++W.seq, key = closeKey();
    try {
      const p = await post("/api/spread/close_plan", {spread_id: SPSEL, entry: W.entry, limit_price: W.entry === "limit" ? W.limit : null});
      if (W.seq !== seq || closeKey() !== key) return;          // inputs changed meanwhile: drop it
      W.plan = {...p, key};
      if (lastState) render(lastState);
    } catch (e) { /* the next refresh tries again */ }
    if (sp.state === "OPEN") { targetPlan(); stopPlan(); }
  }
  async function stopPlan() {
    if (!SPSEL) return;
    const seq = ++W.sseq, key = stopKey();
    try {
      const p = await post("/api/spread/stop_plan", {spread_id: SPSEL, r: W.sprice ? null : W.sr, price: W.sprice || null});
      if (W.sseq !== seq || stopKey() !== key) return;
      W.splan = {...p, key};
      if (lastState) render(lastState);
    } catch (e) { /* the next refresh tries again */ }
  }
  async function targetPlan() {
    if (!SPSEL) return;
    const seq = ++W.tseq, key = targetKey();
    try {
      const p = await post("/api/spread/target_plan", {spread_id: SPSEL, pct: W.tprice ? null : W.tpct, price: W.tprice || null});
      if (W.tseq !== seq || targetKey() !== key) return;
      W.tplan = {...p, key};
      if (lastState) render(lastState);
    } catch (e) { /* the next refresh tries again */ }
  }
  setInterval(() => { if (SPSEL && !document.hidden && !BUSY.has(SPSEL)) closePlan(); }, 5000);

  // Clicks: a spread row selects the spread; any other row hands the center back to the page.
  $("daylist").addEventListener("click", e => {
    const row = e.target.closest("[data-spsel]");
    if (row) { e.stopPropagation(); selectSpread(row.dataset.spsel); return; }
    if (e.target.closest("[data-sel]")) SPSEL = null;
  }, true);
  $("workspace").addEventListener("input", e => {
    if (e.target.classList.contains("sp-cqty")) {
      W.cqty = e.target.value.trim();
      // Not redrawn while typing: the Close button waits (disabled) until the box is left, so it never
      // sends a size other than the one it shows.
      const b = $("workspace").querySelector('[data-spw="close"]'); if (b) b.disabled = true;
      return;
    }
    if (e.target.classList.contains("sp-spx")) {
      W.sprice = e.target.value.trim(); W.splan = null;
      const b = $("workspace").querySelector('[data-spw="stopset"]'); if (b) b.disabled = true;
      clearTimeout(W.st); W.st = setTimeout(stopPlan, 400);
      return;
    }
    if (e.target.classList.contains("sp-tpx")) {
      W.tprice = e.target.value.trim(); W.tplan = null;
      const b = $("workspace").querySelector('[data-spw="target"]'); if (b) b.disabled = true;
      clearTimeout(W.tt); W.tt = setTimeout(targetPlan, 400);
      return;
    }
    if (!e.target.classList.contains("sp-lmt")) return;
    W.limit = e.target.value.trim(); W.plan = null;
    const b = $("workspace").querySelector('[data-spw="close"]'); if (b) b.disabled = true;   // not redrawn while typing
    clearTimeout(W.t); W.t = setTimeout(closePlan, 400);
  });
  $("workspace").addEventListener("focusout", e => {
    if (e.target.matches?.("input:not([type=checkbox])")) setTimeout(() => lastState && render(lastState), 0);
  });
  $("workspace").addEventListener("click", async e => {
    const m = e.target.closest("[data-spentry]");
    if (m && SPSEL) { W.entry = m.dataset.spentry; W.plan = null; render(lastState); closePlan(); return; }
    if (e.target.classList?.contains("sp-ssend")) { W.ssend = e.target.checked; render(lastState); return; }
    const sr = e.target.closest("[data-spsr]");
    if (sr && SPSEL) { W.sr = sr.dataset.spsr; W.sprice = ""; W.splan = null; render(lastState); stopPlan(); return; }
    const tp = e.target.closest("[data-sptpct]");
    if (tp && SPSEL) { W.tpct = tp.dataset.sptpct; W.tprice = ""; W.tplan = null; render(lastState); targetPlan(); return; }
    if (e.target.closest("[data-spwhy]")) { WHY_OPEN = !WHY_OPEN; try { localStorage.setItem("sp-why", WHY_OPEN ? "1" : "0"); } catch (err) {} render(lastState); return; }
    const b = e.target.closest("button[data-spw]"); if (!b || b.disabled || !SPSEL) return;
    const sid = SPSEL, act = b.dataset.spw, sp = (lastState?.spreads || []).find(x => x.spread_id === sid);
    if (!sp || BUSY.has(sid)) return;
    const say = (cls, text) => { MSGS[sid] = {cls, text}; };     // always onto THIS spread, wherever the view went
    if (act === "resolve") {
      if (!confirm("Checked this spread in thinkorswim?\n\nResolve releases its two contracts: they become ordinary option positions again. Nothing is sent to Schwab.")) return;
      try { const r = await post("/api/spread/resolve", {spread_id: sid}); say(r.ok ? "ok" : "bad", r.ok ? "Resolved: the legs are ordinary positions again." : r.error); }
      catch (err) { say("warn", "companion not reachable"); }
      tick(); return;
    }
    if (act === "stopset" || act === "stopclear" || act === "stopdismiss") {      // local: nothing goes to Schwab
      const shown = b.dataset.limit, p = W.splan;
      if (act === "stopset" && (!shown || !p || !p.ok || p.key !== stopKey() || W.sid !== sid || p.limit !== shown)) {
        say("warn", "The stop price changed: review it and click again."); render(lastState); return;
      }
      try {
        if (act === "stopset" && W.ssend && !confirm(`Send when hit at ${shown}?\n\nWhen the mid reaches ${shown} on two checks, the app cancels the target and any working close, then sends ONE close of everything this spread holds at natural plus 2 steps (never past the max loss).\nOnce, never retried, only while the app runs.`)) return;
        const r = await post("/api/spread/stop", act === "stopset" ? {spread_id: sid, action: "set", expected_price: shown, send: W.ssend}
                                                                    : {spread_id: sid, action: act === "stopclear" ? "clear" : "dismiss"});
        say(r.ok ? "ok" : "bad", !r.ok ? r.error : act === "stopset" ? `Watching the stop at ${r.price}${r.send ? ", Send when hit ON" : " (watch-only)"}.`
          : act === "stopclear" ? "Stop removed." : "Dismissed: still watching the same stop.");
      } catch (err) { say("warn", "companion not reachable"); }
      W.splan = null; tick(); return;
    }
    BUSY.add(sid); render(lastState);
    try {
      if (act === "cancel") {
        say("warn", "Cancelling...");
        const r = await post("/api/spread/cancel", {spread_id: sid});
        say(r.ok ? "ok" : "bad", r.ok ? (r.message || `Cancelled the ${r.role} (order ${r.order_id}).`) : r.error);
      } else if (act === "target") {
        const shown = b.dataset.limit, p = W.tplan;
        if (!shown || !p || !p.ok || p.key !== targetKey() || W.sid !== sid || p.limit !== shown) {
          say("warn", "The target price changed: review it and click again.");
        } else {
          say("warn", `Sending the GTC target at ${shown} (preview first)...`);
          render(lastState);
          const r = await post("/api/spread/target", {spread_id: sid, expected_limit: shown});
          say(r.ok ? "ok" : r.status === "UNKNOWN" ? "warn" : "bad",
              r.ok ? `Target ${r.moved ? "moved" : "set"}: order ${r.order_id}, ${r.qty} at ${r.limit} GTC.` : r.error);
        }
      } else if (act === "canceltarget") {
        say("warn", "Cancelling the target...");
        const r = await post("/api/spread/cancel_target", {spread_id: sid});
        say(r.ok ? "ok" : "bad", r.ok ? (r.message || "Target cancelled.") : r.error);
      } else if (act === "close") {
        // Only the limit on screen, and only while the latest plan for the current inputs agrees with it.
        const shown = b.dataset.limit, p = W.plan;
        if (!shown || !p || !p.ok || p.key !== closeKey() || W.sid !== sid || p.limit !== shown) {
          say("warn", "The close price changed: review it and click again.");
        } else {
          const qty = b.dataset.qty ? Number(b.dataset.qty) : null;
          say("warn", `Sending the close${qty ? " of " + qty : ""} at ${shown} (preview first)...`);
          render(lastState);
          const r = await post("/api/spread/close", {spread_id: sid, expected_limit: shown, reprice: sp.state === "CLOSING", qty});
          say(r.ok ? (r.warning ? "warn" : "ok") : r.status === "UNKNOWN" ? "warn" : "bad",
              r.ok ? `Close sent: order ${r.order_id}, ${r.qty}${r.of && r.qty < r.of ? " of " + r.of : ""} at ${r.limit}.`
                     + (r.target && r.target.ok ? ` Target back on the other ${r.target.qty} at ${r.target.limit}.` : "")
                     + (r.warning ? " " + r.warning : "") : r.error);
          if (r.ok) W.cqty = "";
        }
      }
    } catch (err) { say("warn", "Lost contact with the companion. Check thinkorswim."); }
    BUSY.delete(sid);
    if (W.sid === sid) { W.plan = null; W.tplan = null; closePlan(); }
    tick();
  });

  // ---- Closed today: one row per spread closed by its own close order ------------------------------
  // Its two leg round trips (opened at or after the ticket, closed today) merge into one row: net entry
  // and exit per spread, the summed net, the longer hold, both legs' executions. A spread closed outside
  // the companion is resolved, not CLOSED, so its legs stay as they are.
  function mergeClosed(st) {
    const done = st.spreads_closed || [], rows = st.closed || [];
    if (!done.length || !rows.length) return rows;
    const used = new Set(), merged = [];
    // Each leg's FIRST round trip opened at or after the ticket: the spread reserved its contracts, so
    // that is its own entry. A later spread sharing a strike gets the next one (oldest spread first).
    for (const sp of [...done].sort((a, b) => new Date(a.created) - new Date(b.created))) {
      const since = new Date(sp.created).getTime() - 60e3;
      const legs = sp.legs.map(l => rows.filter(t => t.symbol === l.symbol && !used.has(t)
          && t.opened && new Date(t.opened) >= since)
        .sort((a, b) => new Date(a.opened) - new Date(b.opened))[0]);
      if (legs.some(t => !t || t.status === "UNRESOLVED" || t.avg_entry == null || t.avg_exit == null)) continue;
      legs.forEach(t => used.add(t));
      const pt = parts(sp), [n, f] = legs, net = legs.some(t => t.net == null) ? null : legs.reduce((a, t) => a + t.net, 0);
      merged.push({...n, symbol: sp.spread_id, display: `${pt.root} ${pt.date} ${pt.strikes}`,
        side: sp.kind.endsWith("CREDIT") ? "CREDIT" : "DEBIT", max_qty: n.max_qty || n.exit_qty, exit_qty: n.exit_qty,
        avg_entry: Math.abs(n.avg_entry - f.avg_entry), avg_exit: Math.abs(n.avg_exit - f.avg_exit), net,
        opened: new Date(n.opened) <= new Date(f.opened) ? n.opened : f.opened,
        closed: new Date(n.closed) >= new Date(f.closed) ? n.closed : f.closed,
        hold_seconds: Math.max(n.hold_seconds || 0, f.hold_seconds || 0), status: legs.some(t => t.status === "ESTIMATED") ? "ESTIMATED" : n.status,
        // The journal entry is the spread's own (its fills, first entry fill first), the one its workbench wrote.
        fill_ids: Array.isArray(sp.fill_ids) && sp.fill_ids.length ? sp.fill_ids
          : [...new Set([...(n.fill_ids || []), ...(f.fill_ids || [])])],
        executions: [...(n.executions || []), ...(f.executions || [])].sort((a, b) => (a.time || "").localeCompare(b.time || ""))});
    }
    if (!merged.length) return rows;
    return rows.filter(t => !used.has(t)).concat(merged).sort((a, b) => new Date(b.closed || 0) - new Date(a.closed || 0));
  }

  // ---- hook into the page's render ----------------------------------------------------------------
  const SPOKEN = new Set();
  function announceStops(spreads) {
    for (const x of spreads) {
      const s = x.stop;
      if (!s || !["fired", "sent"].includes(s.status) || !s.fired_at) continue;
      if ((s.status === "fired" && s.send) || s.sent_result === "sending" || s.sent_result === "queued") continue;     // spoken once it sent or failed
      const k = x.spread_id + "|" + s.fired_at + "|" + s.status;
      if (SPOKEN.has(k) || !VOICE_ON || VIEW) continue;           // only the main page speaks
      // Chrome only lets a page speak after a click on it: until then, try again on the next refresh.
      if (navigator.userActivation && !navigator.userActivation.hasBeenActive) continue;
      const text = s.status === "fired" ? `${parts(x).root} spread would stop out` : `${parts(x).root} spread ${outcomeOf(s) === "refused" ? refusedAdvice(x)[1] : OUTCOME[outcomeOf(s)][2]}`;
      try { if (speak(text, true) !== false) SPOKEN.add(k); } catch (e) { /* no voice */ }
    }
  }

  // Cockpit's spread ladder (cockpit.js) reads this workbench through a narrow view, and can only fill
  // its Stop or Target price box (the row then re-plans). The row's own button is what sends.
  const BENCH = {
    view(sp, st) {
      const mine = W.sid === sp.spread_id && SPSEL === sp.spread_id;
      const cp = mine && W.plan && W.plan.key === closeKey() ? W.plan : null, work = (cp && cp.working) || [];
      return {t: terms(sp, st), value: valueOf(sp, st), held: held(sp, st), busy: BUSY.has(sp.spread_id),
              sprice: mine ? W.sprice : "", tprice: mine ? W.tprice : "", ssend: mine ? !!W.ssend : !!(sp.stop && sp.stop.send),
              splan: mine && W.splan && W.splan.key === stopKey() ? W.splan : null,
              tplan: mine && W.tplan && W.tplan.key === targetKey() ? W.tplan : null, planned: !!cp,
              target: work.find(w => w.role === "target") || null, close: work.find(w => w.role === "close") || null};
    },
    setPrice(sid, kind, v) {
      if (sid !== SPSEL || sid !== W.sid || BUSY.has(sid)) return false;
      if (kind === "stop") { W.sprice = v; W.splan = null; stopPlan(); }
      else if (kind === "target") { W.tprice = v; W.tplan = null; targetPlan(); }
      else return false;
      if (lastState) render(lastState);
      return true;
    }};

  // Today's tickets lists the page's stock and option tickets; a spread ticket is its own record, so its
  // rows are added here: sent today (ET), with the spread's state and its P&L (closed: the merged row).
  function addSpreadTickets(st, closedRows) {
    const today = etDate(new Date());
    const all = [...(st.spreads || []), ...(st.spreads_closed || [])]
      .filter((x, i, a) => x.created && etDate(new Date(x.created)) === today && a.findIndex(y => y.spread_id === x.spread_id) === i)
      .sort((a, b) => new Date(b.created) - new Date(a.created));
    if (!all.length) return;
    const $v = v => v == null ? `<span class="amb">unknown</span>` : `<b class="num ${cls(v)}">${money(v)}</b>`;
    const h = all.map(sp => {
      const pt = parts(sp), row = closedRows.find(r => r.symbol === sp.spread_id), t = sp.ticket || {};
      const res = sp.state === "CLOSED" ? `<span class="mut">closed</span> ${$v(row ? row.net : null)}`
        : ["OPEN", "CLOSING", "PARTIAL"].includes(sp.state) ? `<span class="mut">open</span> ${$v(pnlOf(sp, st))}`
        : `<span class="mut">${esc(sp.state.replaceAll("_", " ").toLowerCase())}</span>`;
      return `<div class="tkt"><span class="mut num">${tET(sp.created)}</span> <span class="tag ${sp.kind.endsWith("CREDIT") ? "short" : "long"}">${esc(kindTxt(sp).toUpperCase())}</span>
        <b>${esc(pt.root)} ${esc(pt.date)} ${esc(pt.strikes)}</b> ${sp.qty} <span class="mut">@ ${t.entry_limit != null ? esc(String(t.entry_limit)) : "?"} ·</span> ${res}
        <span class="tkt-p"><span class="tag ${TAGS[sp.state] || ""}">${esc(sp.state.replaceAll("_", " "))}</span></span></div>`;
    }).join("");
    const box = $("t-brackets"), n = box.querySelectorAll(".tkt").length;
    box.innerHTML = h + (n ? box.innerHTML : "");
    $("tickets-n").textContent = n + all.length;
  }

  const pageRender = render;
  render = function (st) {
    const spreads = st.spreads || [];
    if (SPSEL && !spreads.some(x => x.spread_id === SPSEL)) SPSEL = null;        // closed or resolved
    const legs = new Set(spreads.flatMap(x => x.legs.map(l => l.symbol)));
    if (SPSEL && SEL && SEL !== LAST_SEL) SPSEL = null;   // a click, a key or Carried picked a position
    const closedRows = mergeClosed(st);
    pageRender({...st, positions: (st.positions || []).filter(p => !legs.has(p.symbol)), closed: closedRows});
    try { addSpreadTickets(st, closedRows); } catch (e) { console.error("spread tickets", e); }
    if (SPSEL && SEL && SEL !== LAST_SEL) SPSEL = null;   // the page jumped to a new trade: follow it
    LAST_SEL = SEL;
    try {
      if (spreads.length && VIEW !== "manage") {
        const shown = (st.positions || []).filter(p => !legs.has(p.symbol)).length;
        $("daylist").innerHTML = spreads.map(x => listRow(x, st)).join("") + (shown ? $("daylist").innerHTML : "");
        $("day-n").textContent = shown + spreads.length;
        const held = spreads.filter(x => legPos(x, st).some(Boolean)).length;
        if (held) $("k-open-s").innerHTML = $("k-open-s").innerHTML.replace(/^(\d+) positions?/, (m, n) => {
          const k = +n + held; return `${k} position${k === 1 ? "" : "s"}`; });
      }
      const sp = spreads.find(x => x.spread_id === SPSEL);
      const typing = document.activeElement?.closest?.("#workspace input:not([type=checkbox])");
      if (sp && !typing && !window.HOLD_WORKSPACE) {         // held while a Cockpit ladder line is dragged
        const h = workbench(sp, st);
        $("workspace").innerHTML = window.COCKPIT_SPREAD ? window.COCKPIT_SPREAD(sp, st, h, BENCH) : h;
      }
      if (S.on) renderPlan();
      announceStops(spreads);
    } catch (e) { console.error("spreads", e); }
  };
  // Spread buttons follow the page's health rule: nothing acts on stale or disconnected data.
  setInterval(() => { if (HEALTH !== "live") document.querySelectorAll("button.spx").forEach(b => { b.disabled = true; }); }, 1000);
  setInterval(() => { if (S.on && !document.hidden && S.chain && !S.chain.error && Date.now() - S.chainAt > 10000) loadChain(true); }, 10000);   // about 6 chain requests a minute
  renderHow(); renderSize();
  if (typeof lastState !== "undefined" && lastState) render(lastState);
})();
