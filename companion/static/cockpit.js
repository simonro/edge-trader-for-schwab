// Cockpit layout for Edge Trader: the main page's only layout since 1.4.0 (the floating Entry and
// Manage windows keep their compact view).
// It changes how the page looks, never what an order does: every order button here is the page's
// own actionBtn (same data-key, same click handler, same server path and checks). Dragging a
// ladder line only fills the same price box the Classic Move stop / Set target uses; nothing is
// sent until Send is clicked.
(() => {
  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
  const ON = !VIEW;
  window.COCKPIT_ON = () => ON;

  document.body.classList.toggle("layout-cockpit", ON);

  // ---- no stop by choice (server prefs, shared by every window) -----------------------------
  // Key "SYM|SIDE|QTY|OPENED": a new size, side or position is a new key, so the alarm comes back
  // by itself. Keys of positions no longer held are dropped as soon as a fresh account read shows it.
  let NOSTOP = {}, NS_REV = null, NS_PRUNING = false;
  // No open time (fill history too short): the average price stands in, so a new position never matches.
  const nsKey = p => `${p.symbol}|${p.side}|${Math.abs(p.qty)}|${p.opened || "avg" + p.avg_price}`;
  function pruneNostop(st) {
    const keys = Object.keys(NOSTOP);
    const acct = st.freshness?.account;
    if (!keys.length || NS_PRUNING || NS_REV === "loading" || !acct || acct.error || !acct.ok_at) return;
    const held = new Set((lastState?.positions || []).map(nsKey));    // unfiltered: spread legs count as held
    if (keys.every(k => held.has(k))) return;
    NS_PRUNING = true;
    const next = Object.fromEntries(Object.entries(NOSTOP).filter(([k]) => held.has(k)));
    post("/api/ui/prefs", {nostopOk: next}).then(() => { NOSTOP = next; }).catch(() => {}).finally(() => { NS_PRUNING = false; });
  }
  function loadNostop(rev) {
    fetch("/api/ui/prefs", {cache: "no-store"}).then(r => r.json()).then(pr => {
      NOSTOP = pr.nostopOk && typeof pr.nostopOk === "object" ? pr.nostopOk : {};
      NS_REV = rev;
      if (lastState) render(lastState);
    }).catch(() => { NS_REV = null; });     // try again on the next poll
  }
  async function setNostop(key, on) {
    const held = new Set((lastState?.positions || []).map(nsKey));
    const next = Object.fromEntries(Object.entries(NOSTOP).filter(([k]) => held.has(k)));   // drop closed ones
    if (on) next[key] = new Date().toISOString(); else delete next[key];
    try { await post("/api/ui/prefs", {nostopOk: next}); NOSTOP = next; } catch (e) { /* stays as it was */ }
    if (lastState) render(lastState);
  }
  const noStop = p => p.protection !== "FULL" && p.protection !== "PARTIAL" && p.protection !== "UNKNOWN";
  // Not while the fill history is loading: a position's open time (part of the key) may not be known
  // yet, and a reopened position must never inherit the old one's consent.
  const byChoice = p => noStop(p) && !!lastState?.history_complete && !!NOSTOP[nsKey(p)];

  // ---- attention bar: only real problems, one line each --------------------------------------
  const ATTN_RESOLVE = {};
  function attnItems(st) {
    const out = [], calm = [];
    const unres = new Set(st.trading?.unresolved || []);
    // An unclear close, partial or add blocks its symbol whether or not the position is still there.
    const hid = st.trading?.unresolved_hidden || 0;
    if (hid) out.push({lvl: "bad", html: `<b>${hid}</b> unclear close, partial or add on a hidden position: check thinkorswim`});
    for (const sym of unres) {
      const p = (st.positions || []).find(x => x.symbol === sym);
      out.push({lvl: "bad", sel: p ? esc(sym) : null, html: `<b>${esc(p ? (p.asset_type === "OPTION" ? p.display : sym) : sym)}</b> unclear close, partial or add: check thinkorswim, then Resolve`,
                btn: `<button type="button" class="btn sm" data-resolve-close="${esc(sym)}">Resolve</button>`});
    }
    for (const p of st.positions || []) {
      const name = p.asset_type === "OPTION" ? p.display : p.symbol, sel = esc(p.symbol);
      const n = `${qty(Math.abs(p.qty))} ${p.asset_type === "OPTION" ? "ct" : "sh"}`;
      if (noStop(p)) {
        if (byChoice(p)) calm.push({sel, html: `${esc(name)} ${n} · no stop by choice`,
                                    btn: `<button type="button" class="lnk" data-nostop="${esc(nsKey(p))}" data-on="0" title="Show the no-stop alarm again">undo</button>`});
        else out.push({lvl: "bad", sel, html: `<b>${esc(name)}</b> ${n} with no stop`,
                       btn: `<button type="button" class="lnk" data-nostop="${esc(nsKey(p))}" data-on="1" title="I hold this without a stop on purpose. Comes back if the size or side changes.">no stop by choice</button>`});
      } else if (p.protection === "PARTIAL")
        out.push({lvl: "warn", sel, html: `<b>${esc(name)}</b> stop covers ${qty(p.stop_qty)} of ${qty(Math.abs(p.qty))}`});
      else if (p.protection === "UNKNOWN")
        out.push({lvl: "warn", sel, html: `<b>${esc(name)}</b> protection unknown: order data is stale`});
    }
    for (const b of st.brackets || [])
      if (["UNKNOWN", "SENDING", "ACCEPTED_UNTRACKED"].includes(b.status))
        out.push({lvl: "bad", dock: "tickets", html: `<b>${esc(b.display || b.symbol)}</b> ticket outcome unknown: check thinkorswim, then Resolve in Today's tickets`});
    for (const x of st.spreads || []) {
      const s = String(x.state || "").toUpperCase();
      const fired = x.stop && ["fired", "sent"].includes(x.stop.status);
      if (["UNKNOWN", "BROKEN", "NEEDS_ACTION", "EXPIRED_CHECK"].includes(s) || x.hold || fired)
        out.push({lvl: "bad", html: `<b>${esc(x.underlying || "Spread")} spread</b> ${fired ? "stop hit" : esc(s.replaceAll("_", " ").toLowerCase() || "needs action")}: open it in Open trades`});
    }
    return {out, calm};
  }
  function renderAttn(st) {
    const el = $("attn");
    if (!ON || !st.summary) { el.innerHTML = ""; return; }
    const {out, calm} = attnItems(st);
    pruneNostop(st);
    const n = (st.positions || []).length + (st.spreads || []).length;
    if (!n && !out.length && !calm.length) { el.innerHTML = ""; el.className = "attn"; return; }
    el.className = "attn" + (out.some(x => x.lvl === "bad") ? " bad" : out.length ? " warn" : "");
    const item = x => `<span class="ai ${x.lvl || "calm"}"${x.sel ? ` data-sel="${x.sel}"` : ""}${x.dock ? ` data-dockgo="${x.dock}"` : ""}>${x.html}${x.btn ? " " + x.btn : ""}</span>`;
    el.innerHTML = `<span class="ah">${out.length ? `Needs attention <b>${out.length}</b>` : "No alerts"}</span>`
      + out.map(item).join("") + (calm.length ? `<span class="acalm">${calm.map(item).join("")}</span>` : "");
  }
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-nostop]");
    if (b) { e.stopPropagation(); setNostop(b.dataset.nostop, b.dataset.on === "1"); return; }
    const d = e.target.closest("[data-dockgo]");
    if (d) { DOCK = d.dataset.dockgo; ls("dock", DOCK); renderDock(); }
  }, true);

  // ---- dock: the four bottom tables as tabs -------------------------------------------------
  const DOCK_TABS = [["closed", "Closed today", "closed-n"], ["orders", "Pending orders", "orders-n"],
                     ["tickets", "Today's tickets", "tickets-n"], ["carried", "Carried", "carried-n"]];
  let DOCK = ls("dock") || "closed";
  const secs = [...document.querySelectorAll(".bottom > section.panel")];
  secs.forEach((s, i) => { if (DOCK_TABS[i]) s.dataset.dock = DOCK_TABS[i][0]; });
  function renderDock() {
    if (!ON) return;
    $("dock-tabs").innerHTML = DOCK_TABS.map(([k, t, n]) => {
      const c = ($(n)?.textContent || "").trim();
      return `<button type="button" class="dt${DOCK === k ? " on" : ""}" data-dock="${k}">${t}${c ? ` <span class="c">${esc(c)}</span>` : ""}</button>`;
    }).join("");
    secs.forEach(s => s.classList.toggle("dock-on", s.dataset.dock === DOCK));
  }
  $("dock-tabs").addEventListener("click", e => {
    const b = e.target.closest("[data-dock]"); if (!b) return;
    DOCK = b.dataset.dock; ls("dock", DOCK); renderDock();
  });

  // ---- price grid for a dragged line ---------------------------------------------------------
  // Stocks: cents ($1 and up). Options: nickels under $3, dimes above: on every option's grid,
  // penny names included, so a dragged price is never refused for being off the grid.
  function stepFor(p, price) {
    if (p.asset_type === "OPTION") return price < 3 ? 0.05 : 0.10;
    return price < 1 ? 0.0001 : 0.01;
  }
  const snap = (p, v) => { const s = stepFor(p, v); return Math.round(Math.round(v / s) * s * 10000) / 10000; };
  const fmt = (p, v) => v == null ? "-" : (p.asset_type !== "OPTION" && v < 1 ? v.toFixed(4) : v.toFixed(2));

  // ---- the ladder ----------------------------------------------------------------------------
  // LD: the line being dragged or waiting for Send. {sym, kind: "stop" | "target", from, price, dragging}
  let LD = null;
  const dirOf = p => p.side === "LONG" ? 1 : -1;
  function levels(p) {
    const r = p.r, d = dirOf(p), entry = r ? r.entry : p.avg_price;
    const tg = (p.targets_all || []).length ? p.targets_all.map(t => ({price: t.price, qty: t.qty})) : p.target_price != null ? [{price: p.target_price}] : [];
    const rOf = v => r && entry != null ? (v - entry) * d / r.rps : null;
    const pnlAt = (v, n) => entry != null ? (v - entry) * d * (p.multiplier || 1) * (n ?? Math.abs(p.qty)) : null;   // n: the shares on that exit
    const L = [];
    if (r && Math.abs(r.stop - (p.stop_price ?? NaN)) > 1e-9) L.push({k: "orig", v: r.stop, name: "orig stop", sub: "-1R frozen"});
    if (p.stop_price != null) {
      const be = entry != null && Math.abs(p.stop_price - entry) < 1e-9;
      L.push({k: "stop", v: p.stop_price, drag: true,
              name: p.trail ? `trail stop${p.trail.estimated ? " est." : ""}` : be ? "B/E stop" : "stop",
              qty: p.stop_qty});
    }
    if (entry != null) L.push({k: "entry", v: entry, name: "entry"});
    if (r) for (const m of [1, 2, 3]) {
      const v = entry + d * r.rps * m;
      if (!tg.some(t => Math.abs(t.price - v) < r.rps * 0.04)) L.push({k: "rm", v, name: `${m}R`});
    }
    tg.forEach((t, i) => L.push({k: "target", v: t.price, drag: tg.length === 1, qty: t.qty ?? Math.abs(p.qty),
                                 name: tg.length > 1 ? `target ${i + 1}` : "target"}));
    for (const x of L) { x.r = rOf(x.v); x.pnl = pnlAt(x.v, (x.k === "stop" || x.k === "target") && x.qty > 0 ? x.qty : null); }
    return {L, entry, d, r};
  }
  function range(p, L, extra = []) {
    const q = p.quote || {};
    const vals = L.map(x => x.v).concat([p.last, q.bid, q.ask].filter(v => v > 0), extra);
    if (LD && LD.sym === p.symbol) vals.push(LD.price);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.01 || 1;
    return {lo: lo - pad, hi: hi + pad};
  }
  function confirmChip(p, sym, lv) {
    const {entry, d, r} = lv;
    const v = LD.price, isStop = LD.kind === "stop";
    const q = p.quote || {};
    const tg = p.targets_all || [];
    // Move stop keeps each fixed stop's own quantity; a trailing stop, Set stop and any target change
    // rebuild the exits for the whole position.
    const inPlace = isStop && p.stop_price != null && !p.trail;
    const n = inPlace && p.stop_qty > 0 ? p.stop_qty : Math.abs(p.qty);
    const mixed = inPlace && p.stop_best != null && Math.abs(p.stop_best - p.stop_price) > 1e-9;   // several stop prices
    const pnl = entry != null ? (v - entry) * d * (p.multiplier || 1) * n : null;
    const rr = r ? (v - entry) * d / r.rps : null;
    // A stop through the market fills at once, and a target through it is a close: send neither from here.
    const through = isStop ? (d > 0 ? v >= (q.bid || p.last) : v <= (q.ask || p.last))
                           : (d > 0 ? v <= (q.bid || p.last) : v >= (q.ask || p.last));
    const what = isStop ? (p.stop_price == null ? "Set stop" : p.trail ? "Replace trail with a fixed stop" : "Move stop")
                        : (p.target_price == null ? "Set target" : "Replace target");
    const hasStop = p.stop_price != null || !!p.trail, hasTgt = (p.targets_all || []).length > 0 || p.target_price != null;
    const how = isStop ? (inPlace ? "replaces the working stop" : hasTgt ? "one new OCO with the target, whole position" : "a stop alone (no target), whole position")
                       : (hasStop ? "one new OCO with the stop, whole position" : "a lone target: still NO STOP");
    const uncov = inPlace && p.stop_qty > 0 && p.stop_qty < Math.abs(p.qty) ? Math.abs(p.qty) - p.stop_qty : 0;
    const was = LD.from != null && entry != null ? (LD.from - entry) * d * (p.multiplier || 1) * n : null;
    const looser = isStop && LD.from != null && (v - LD.from) * d < 0;
    const delta = mixed ? `<div class="l2 amb">Your stops sit at ${fmt(p, p.stop_price)} to ${fmt(p, p.stop_best)}: every one moves to ${fmt(p, v)}.</div>`
      : inPlace && was != null && Math.abs(v - LD.from) > 1e-9
      ? `<div class="l2 ${looser ? "amb" : "pos"}">${looser ? "Loosens" : "Tightens"} the stop: at stop ${money(was)} to ${money(pnl)}</div>` : "";
    const send = isStop
      ? actionBtn(`ms:${sym}`, "Send", "move", {symbol: sym}, "", through, "primary ld-send")
      : actionBtn(`tg:${sym}`, "Send", "target", {symbol: sym, custom: 1}, "", through, "primary ld-send");
    return `<div class="ldchip ${isStop ? "s" : "t"}">
      <div class="l1">${what} ${LD.from != null ? `${fmt(p, LD.from)} <span class="ar">to</span> ` : ""}<b class="num">${fmt(p, v)}</b></div>
      <div class="l2">${through ? `<span class="neg">Through the market (${isStop ? "it would fill at once" : "use Close or Partial"}).</span>`
        : `if it fills <b class="num ${cls(pnl)}">${money(pnl)}</b>${rr != null ? ` <span class="num ${cls(rr)}">${fmtR(rr)}</span>` : ""} · ${qty(n)}${n !== Math.abs(p.qty) ? `/${qty(Math.abs(p.qty))}` : ""} ${unitSh(p)} · ${how}`}</div>${through ? "" : delta}
      ${uncov ? `<div class="l2 amb">${qty(uncov)} ${unitSh(p)} stay without a stop.</div>` : ""}
      <div class="l3">${send}<button type="button" class="btn ld-cancel">Cancel</button><span class="mut">↑ ↓ one tick · Esc</span></div></div>`;
  }
  // Layout (after design B, ChatGPT): R and names on the left, prices, a reward/risk rail at true
  // price spacing, and exit cards on the right joined to their exact price by a connector, so
  // close levels never print on top of each other.
  let GEOM = {w: 640, h: 600};
  // Condensed (Simon, 2026-11-08): narrow labels, an 18 px rail, two-line cards.
  const LW = 120, PXW = 62, RAILX = LW + PXW + 12, RAILW = 14, CARDX = RAILX + RAILW + 18, CARDH = 44;
  function spreadOut(items, gap, h) {        // keep sorted rows at least `gap` px apart inside [0, h]
    items.sort((a, b) => a.y - b.y);
    items.forEach((it, i) => { it.dy = i ? Math.max(it.y, items[i - 1].dy + gap) : Math.max(it.y, gap / 2); });
    for (let i = items.length - 1; i >= 0; i--) {
      const lim = i === items.length - 1 ? h - gap / 2 : items[i + 1].dy - gap;
      if (items[i].dy > lim) items[i].dy = lim;
    }
    return items;
  }
  const rTxt = (r, k) => r == null ? "" : `${r > 0 ? "+" : ""}${k === "rm" ? Math.round(r) : r.toFixed(2)}R`;
  function ladder(p) {
    const sym = p.symbol, lv = levels(p), {L, entry, d} = lv, q = p.quote || {};
    if (p.last == null) return `<div class="ladder empty">No price yet.</div>`;
    const last0 = p.last;
    const noStopNow = p.stop_price == null && !p.trail;
    const noTgt = !(p.targets_all || []).length && p.target_price == null;
    const away = Math.max(lv.r ? lv.r.rps : 0, last0 * 0.01, p.asset_type === "OPTION" ? 0.05 : 0.02);
    const {lo, hi} = range(p, L);
    // A drag from an empty slot starts from a level inside the drawn range (1R, else 1%, from the entry).
    const inRange = v => Math.min(hi - (hi - lo) * 0.03, Math.max(lo + (hi - lo) * 0.03, v));
    const slotStopV = snap(p, Math.max(stepFor(p, last0), inRange((entry ?? last0) - d * away)));
    const slotTgtV = snap(p, Math.max(stepFor(p, last0), inRange((entry ?? last0) + d * away * 2)));
    const {w: W, h: H} = GEOM;
    const Yp = v => Math.max(0, Math.min(H, (hi - v) / (hi - lo) * H));
    const pct = y => (y / H * 100).toFixed(3) + "%";
    const last = p.last, noR = !lv.r;
    // Left rows: every level plus the last price, spaced so the text never overlaps.
    const rank = {stop: 0, target: 1, now: 2, entry: 3, orig: 4, rm: 5};
    const rows = [];
    for (const x of L.map(x => ({...x, y: Yp(x.v)})).concat([{k: "now", v: last, y: Yp(last), name: "last", r: p.r ? p.r.live_r : null}])) {
      const same = rows.find(o => Math.abs(o.v - x.v) < 1e-9 && o.k !== "now" && x.k !== "now");
      if (!same) { rows.push(x); continue; }
      // A stop on the entry is a B/E stop: one short name. Other levels at one price share the row.
      if (x.k !== "rm" && !(same.k === "stop" && x.k === "entry") && !(same.k === "entry" && x.k === "stop"))
        same.name = same.k === "rm" ? x.name : `${same.name} / ${x.name}`;
      else if (x.k === "stop") same.name = x.name;
      if (rank[x.k] < rank[same.k]) same.k = x.k;
    }
    spreadOut(rows, 19, H);
    const leftRows = rows.map(x => `<div class="lr ${x.k}" style="top:${pct(x.dy)}">
        <span class="rr num">${rTxt(x.r, x.k)}</span><span class="nm" title="${esc(x.name)}">${x.k === "rm" ? "" : esc(x.name)}</span>
        <span class="pv num">${fmt(p, x.v)}</span></div>`).join("");
    // Exit cards: the stop and targets, or an empty slot that invites one.
    const cards = [];
    const uncov = p.stop_qty > 0 && p.stop_qty < Math.abs(p.qty) ? Math.abs(p.qty) - p.stop_qty : 0;
    const exitInfo = x => `${x.k === "stop" && uncov ? `<span class="amb">${qty(uncov)} uncovered</span> · ` : ""}${x.pnl != null ? `<span class="num ${cls(x.pnl)}">${money(x.pnl)}</span>` : ""}${x.r != null ? ` <span class="num ${cls(x.r)}">${fmtR(x.r)}</span>` : ""}${x.qty != null && x.qty < Math.abs(p.qty) ? ` <span class="mut">· ${qty(x.qty)}/${qty(Math.abs(p.qty))}</span>` : ""}`;
    for (const x of L) {
      if (x.k === "stop") cards.push({k: "stop", v: x.v, drag: true, title: x.name, info: exitInfo(x),
        tools: actionBtn(`be:${sym}`, "B/E", "be", {symbol: sym}, "", false, "sm") + actionBtn(`trl1:${sym}`, "Trail 1R", "trail", {symbol: sym, unit: "R", value: 1, basis: "MARK"}, "", noR, "sm")});
      if (x.k === "target") cards.push({k: "target", v: x.v, drag: x.drag, title: x.name, info: exitInfo(x),
        tools: [1, 2, 3].map(m => actionBtn(`tr${m}:${sym}`, `${m}R`, "target", {symbol: sym, r: m}, "", noR, "sm")).join(""),
        why: x.drag ? "" : "Two targets: drag is off (it would replace both). Use the R buttons or Replace target."});
    }
    if (noStopNow) cards.push({k: "stop", slot: true, v: slotStopV, drag: true,
      title: byChoice(p) ? "No stop by choice" : "NO STOP", info: `<span class="mut">drag to set one</span>`, tools: ""});
    if (noTgt) cards.push({k: "target", slot: true, v: slotTgtV, drag: true,
      title: "No target", info: `<span class="mut">drag to set one</span>`, tools: ""});
    const slots = cards.filter(c => c.slot);
    const placed = cards.filter(c => !c.slot);
    placed.forEach(c => { c.y = Yp(c.v); });
    spreadOut(placed, CARDH + 8, H - (slots.length ? CARDH + 12 : 0));
    const pend = k => LD && LD.sym === sym && LD.kind === k;
    const cardHtml = c => `<div class="lc ${c.k}${c.slot ? " slot" : ""}${pend(c.k) ? " moving" : ""}${byChoice(p) && c.slot && c.k === "stop" ? " chosen" : ""}" style="top:${pct(c.dy)}">
        <span class="grip${c.drag ? "" : " off"}" ${c.drag ? `data-dragk="${c.k}" data-v="${c.v}" data-slot="${c.slot ? 1 : 0}" tabindex="0" title="Drag up or down. Nothing is sent until you click Send."` : `title="${esc(c.why || "")}"`}>⠿</span>
        <b class="ti">${esc(c.title)}</b><span class="tl">${c.tools}</span><span class="in2">${c.info}</span></div>`;
    // Connectors (SVG, pixel space): label rows to the rail, cards to their price on the rail.
    const lines = rows.filter(x => Math.abs(x.dy - x.y) > 1.5).map(x => `<line class="ld-${x.k}" x1="${LW + PXW + 2}" y1="${x.dy}" x2="${RAILX}" y2="${x.y}"/>`)
      .concat(placed.map(c => `<path class="lc-${c.k}${c.slot ? " slot" : ""}" d="M${RAILX + RAILW} ${c.y} H${RAILX + RAILW + 6} L${CARDX - 6} ${c.dy} H${CARDX}"/>`)).join("");
    // Rail: reward side of the entry green, risk side to the stop red; the open move drawn brighter.
    const e = entry ?? last, ye = Yp(e), stopRef = p.stop_price ?? (lv.r ? lv.r.stop : null);
    const zone = (yFar, c) => {          // from the entry to a level, strongest at the level
      const top = Math.min(ye, yFar), h = Math.abs(yFar - ye);
      return h < 0.5 ? "" : `<div class="zn ${c} ${yFar < ye ? "up" : "dn"}" style="top:${pct(top)};height:${pct(h)}"></div>`;
    };
    const tgts = L.filter(x => x.k === "target").map(x => x.v);
    const far = tgts.length ? (d > 0 ? Math.max(...tgts) : Math.min(...tgts)) : null;
    const yl = Yp(last), up = (last - e) * d >= 0;
    const rail = (far != null ? zone(Yp(far), "rew") : "")
      + (stopRef != null ? zone(Yp(stopRef), "risk") : "")
      + `<div class="mv ${up ? "g" : "l"}" style="top:${pct(Math.min(ye, yl))};height:${pct(Math.max(2, Math.abs(yl - ye)))}"></div>`
      + (q.bid > 0 && q.ask > 0 ? `<div class="ba" style="top:${pct(Yp(q.ask))};height:${pct(Math.max(2, Yp(q.bid) - Yp(q.ask)))}"></div>` : "");
    const ticks = L.map(x => x.k === "rm" ? `<div class="dot" style="top:${pct(Yp(x.v))}"></div>`
                                          : `<div class="cap ${x.k}" style="top:${pct(Yp(x.v))}"></div>`).join("")
      + `<div class="knob ${up ? "g" : "l"}" style="top:${pct(yl)}"></div>`;
    const ghost = LD && LD.sym === sym ? `<div class="ghost ${LD.kind}" style="top:${pct(Yp(LD.price))}"><span class="gp num">${fmt(p, LD.price)}</span></div>` : "";
    const chip = LD && LD.sym === sym && !LD.dragging ? `<div class="ldwrap" style="top:${pct(Math.max(0, Math.min(H - 130, Yp(LD.price) + 10)))}">${confirmChip(p, sym, lv)}</div>` : "";
    return `<div class="ladder${d < 0 ? " short" : ""}" data-sym="${esc(sym)}" data-lo="${lo}" data-hi="${hi}" style="--lw:${LW}px;--pxw:${PXW}px;--railx:${RAILX}px;--railw:${RAILW}px;--cardx:${CARDX}px">
      <div class="nowband" style="top:${pct(yl)}"></div>
      <div class="rail">${rail}</div>${ticks}
      <svg class="conn" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${lines}</svg>
      ${leftRows}
      <div class="cards">${placed.map(cardHtml).join("")}</div>
      ${slots.length ? `<div class="slots">${slots.map(c => cardHtml({...c, dy: 0})).join("")}</div>` : ""}
      ${ghost}${chip}
    </div>`;
  }

  // Drag: pointer events (mouse and touch), by how far you pull (the card sits beside its price,
  // not on it). The workbench is not redrawn while a line moves.
  const pxPerPrice = lad => lad.getBoundingClientRect().height / (+lad.dataset.hi - +lad.dataset.lo);
  const posOf = sym => (lastState?.positions || []).find(x => x.symbol === sym);
  function setBox(p, kind, v) {
    if (kind === "stop") MS_INPUT[p.symbol] = fmt(p, v); else TG_INPUT[p.symbol] = fmt(p, v);
  }
  document.addEventListener("pointerdown", e => {
    if (!ON) return;
    const h = e.target.closest(".ladder [data-dragk]");
    if (!h) return;
    const lad = h.closest(".ladder"), p = posOf(lad.dataset.sym); if (!p) return;
    const kind = h.dataset.dragk;
    if (kind !== "stop" && kind !== "target") return;
    if (document.activeElement?.matches?.("input, textarea, select")) document.activeElement.blur();
    e.preventDefault();
    const again = LD && LD.sym === p.symbol && LD.kind === kind && !LD.dragging;     // grabbing a waiting move again
    const v0 = again ? LD.price : +h.dataset.v;
    const from = again ? LD.from : h.dataset.slot === "1" ? null : +h.dataset.v;
    const y0 = e.clientY, k = pxPerPrice(lad);
    LD = {sym: p.symbol, kind, from, price: snap(p, v0), dragging: true};
    window.HOLD_WORKSPACE = true;
    lad.setPointerCapture(e.pointerId);
    lad.classList.add("dragging");
    paintGhost(lad, p);
    const move = ev => { LD.price = Math.max(stepFor(p, v0), snap(p, v0 - (ev.clientY - y0) / k)); paintGhost(lad, p); };
    const up = () => {
      lad.removeEventListener("pointermove", move); lad.removeEventListener("pointerup", up); lad.removeEventListener("pointercancel", up);
      window.HOLD_WORKSPACE = false;
      if (LD.from != null && Math.abs(LD.price - LD.from) < stepFor(p, LD.price) / 2) LD = null;   // dropped where it was
      else { LD.dragging = false; setBox(p, LD.kind, LD.price); }
      if (lastState) render(lastState);
      document.querySelector(".ldchip .ld-send:not([disabled])")?.focus({preventScroll: true});
    };
    lad.addEventListener("pointermove", move); lad.addEventListener("pointerup", up); lad.addEventListener("pointercancel", up);
  });
  function paintGhost(lad, p) {
    const lo = +lad.dataset.lo, hi = +lad.dataset.hi;
    let g = lad.querySelector(".ghost");
    if (!g) { g = document.createElement("div"); lad.appendChild(g); }
    g.className = "ghost " + LD.kind;
    g.style.top = Math.max(0, Math.min(100, (hi - LD.price) / (hi - lo) * 100)) + "%";
    g.innerHTML = `<span class="gp num">${fmt(p, LD.price)}</span>`;
    lad.querySelector(".ldwrap")?.remove();
  }
  document.addEventListener("click", e => {
    if (e.target.closest(".ld-cancel")) { cancelLD(); return; }
    // Send sends the price box. If the box was edited after the drag, the chip is out of date:
    // refuse this click and redraw, so what is sent is always what the chip showed.
    const s = e.target.closest(".ld-send");
    if (s && LD) {
      const p = posOf(LD.sym), box = LD.kind === "stop" ? MS_INPUT[LD.sym] : TG_INPUT[LD.sym];
      const bv = parseFloat(box);
      if (!p || !(Math.abs(bv - LD.price) < 1e-9) || !(Math.abs(snap(p, bv) - bv) < 1e-9)) {
        e.preventDefault(); e.stopImmediatePropagation();
        if (p && bv > 0 && Math.abs(snap(p, bv) - bv) < 1e-9) LD.price = bv; else LD = null;
        if (lastState) render(lastState);
      }
    }
  }, true);
  function cancelLD() {
    if (!LD) return;
    const p = posOf(LD.sym);
    if (p) { if (LD.kind === "stop") MS_INPUT[p.symbol] = ""; else TG_INPUT[p.symbol] = ""; }
    LD = null; window.HOLD_WORKSPACE = false;
    if (lastState) render(lastState);
  }
  // Keys: Esc cancels; arrows nudge the waiting line one tick (Shift: ten). A focused line starts
  // a move with the arrows too. Enter never sends: it moves focus to Send (a second key press).
  document.addEventListener("keydown", e => {
    if (!ON || e.ctrlKey || e.altKey || e.metaKey) return;
    const focusLine = e.target.closest?.(".ladder [data-dragk]");
    if (!LD && focusLine && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      const p = posOf(focusLine.closest(".ladder").dataset.sym); if (!p) return;
      const from = focusLine.dataset.slot === "1" ? null : +focusLine.dataset.v;
      LD = {sym: p.symbol, kind: focusLine.dataset.dragk, from, price: snap(p, +focusLine.dataset.v), dragging: false};
    }
    if (!LD || LD.dragging) return;
    if (e.target.closest?.("input, textarea, select")) return;
    const p = posOf(LD.sym); if (!p) return;
    if (e.key === "Escape") { e.preventDefault(); cancelLD(); return; }
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const n = (e.shiftKey ? 10 : 1) * (e.key === "ArrowUp" ? 1 : -1);
      LD.price = snap(p, LD.price + n * stepFor(p, LD.price));
      setBox(p, LD.kind, LD.price);
      if (lastState) render(lastState);
      document.querySelector(".ldchip .ld-send:not([disabled])")?.focus({preventScroll: true});
      return;
    }
    if (e.key === "Enter" && !e.target.closest?.(".ld-send")) {
      e.preventDefault();
      document.querySelector(".ldchip .ld-send:not([disabled])")?.focus({preventScroll: true});
    }
  }, true);

  // ---- the cockpit workbench -----------------------------------------------------------------
  function cockpitWorkspace(p, st) {
    const r = p.r, sym = p.symbol, noR = !r;
    const nOrders = (st.orders || []).filter(o => o.symbol === sym).length;
    const nT = (p.targets_all || []).length;
    const open = !!lastState?.trading?.market_open;
    // A sent move clears its price box: the waiting line goes with it.
    if (LD && LD.sym === sym && !LD.dragging) {
      const box = LD.kind === "stop" ? MS_INPUT[sym] : TG_INPUT[sym];
      if (!box) LD = null;
      else if (parseFloat(box) > 0 && Math.abs(parseFloat(box) - LD.price) > 1e-9) {
        // Typed in the box: the chip follows only an on-grid price (the server would round any other one).
        const v = parseFloat(box);
        if (Math.abs(snap(p, v) - v) < 1e-9) LD.price = v; else LD = null;
      }
    }
    if (LD && LD.sym !== sym) LD = null;
    const tgtNote = nT > 1 ? `${nT} targets · replaces all` : p.target_price != null ? `${px(p.target_price)}${r ? ` · ${((p.target_price - r.entry) * (p.side === "LONG" ? 1 : -1) / r.rps).toFixed(2)}R` : ""}` : "none";
    const prot = byChoice(p) ? `<span class="tag">NO STOP BY CHOICE</span>` : protTag(p);
    return `<div class="lock">${esc(p.display)} ${p.side} ${qty(p.qty)}<small></small>
      <button type="button" class="btn sm ghost popbtn only-main" data-open="manage" title="Pop out: manage this trade in a small always-on-top window">⧉</button></div>
    <div class="cw">
      <div class="cw-head">
        <div class="id"><span class="sym">${esc(p.display)}</span>
          <span class="tags"><span class="tag ${p.side.toLowerCase()}">${p.side} ${qty(p.qty)}</span>${prot}${p.held !== "INTRADAY" ? `<span class="tag">${p.held}</span>` : ""}${p.ledger === "MISMATCH" ? `<span class="tag est" title="Fill history does not match Schwab's quantity yet">ledger?</span>` : ""}</span>
          <div class="sub">${r ? "companion ticket" : "opened outside the ticket"}${p.opened ? ` · opened ${tET(p.opened)}` : ""} · entry ${px(r ? r.entry : p.avg_price)} · 1R ${r ? `${px(r.rps)} · ${money(r.rps * Math.abs(p.qty) * (p.multiplier || 1), false)}` : "not set"} · at stop ${p.pnl_at_stop == null ? "unknown" : money(p.pnl_at_stop)}</div></div>
        <div class="hero"><div class="h"><span class="k">Open P&amp;L</span><span class="v num ${cls(p.open_pnl)}">${money(p.open_pnl)}</span></div>
          <div class="h"><span class="k">Live R</span><span class="v num ${r ? cls(r.live_r) : "mut small"}">${r ? fmtR(r.live_r) : "not set"}</span></div></div>
        <div class="exits">${actionBtn(`sc:${sym}`, `Cancel orders (${nOrders})`, "symcancel", {symbol: sym}, "", nOrders === 0, "danger")}
          ${actionBtn(`cl:${sym}`, "Close position", "close", {symbol: sym}, "", !open, "danger-solid")}</div>
      </div>
      <div class="cw-body">
        ${ladder(p)}
        <div class="cw-ctl acts">
          ${stopRow(p, sym, noR)}
          ${targetRow(p, sym, noR, tgtNote)}
          ${partialRow(p, sym)}
          ${addRow(p, sym, open)}
          ${resultBox(p, sym)}
        </div>
      </div>
    </div>`;
  }
  const classicWorkspace = renderWorkspace;
  renderWorkspace = (p, st) => (ON && p && !VIEW ? cockpitWorkspace(p, st) : classicWorkspace(p, st));

  // ---- spread ladder (Cockpit only) -----------------------------------------------------------
  // A companion vertical's net price per spread, from 0 to its width, at true spacing. spreads.js
  // draws the spread workbench and hands it here with a narrow view of its state (BENCH). A drop
  // only fills that workbench's own Stop or Target price box and re-plans the row: the row's own
  // button, with its own checks, is what sends. Nothing here calls an order endpoint.
  let BENCH = null;
  let SLD = null;              // the line being dragged: {sid, kind, price}
  let SPL_LAST = null;         // the chip shows this pending line ("stop" | "target")
  let SPL_FOCUS = null;        // after a redraw: {to: "grip" | "row", kind, at}
  let SPL_KEEP = null;         // the ladder grip or row button that had focus before a redraw
  let SGEOM = {w: 440, h: 600}, SPL_RAF = false;
  const n2 = v => v == null || !isFinite(v) ? "-" : Number(v).toFixed(2);
  const splSnap = (x, step, wd) => Math.max(0, Math.min(wd, Math.round(Math.round(x / step) * step * 10000) / 10000));
  const spOf = sid => (lastState?.spreads || []).find(x => x.spread_id === sid);
  const numBox = s => (s !== "" && s != null && isFinite(+s) && +s >= 0 ? +s : null);

  function splModel(sp, st, B) {
    const v = B.view(sp, st), t = v.t || {}, width = Number(sp.ticket?.width);
    if (!(width > 0)) return null;
    const credit = String(sp.kind).endsWith("CREDIT"), d = credit ? -1 : 1;       // d: the way a higher net price pays
    const entry = t.entry != null && isFinite(t.entry) ? Number(t.entry) : null;
    const loss1 = entry == null ? null : credit ? width - entry : entry;             // 1R per spread, in net price
    const n = v.held || sp.qty || 0;
    // The row's own plans name the grid; nickels are valid on every vertical until one arrives.
    const step = Number(v.splan?.step || v.tplan?.step) || 0.05;
    const open = sp.state === "OPEN";
    const s = sp.stop || null;
    // The target: the close plan's working orders are the truth once loaded; until then the ledger's id.
    const tw = v.target, tPx = tw && tw.price != null ? Number(tw.price) : null;
    const hasTarget = v.planned ? !!tw : !!sp.target_order_id;
    const cPx = v.close && v.close.price != null ? Number(v.close.price) : null;
    // Pending: what the row's price box holds now (dragged or typed), when it differs from what works.
    const sb = open ? numBox(v.sprice) : null, tb = open ? numBox(v.tprice) : null;
    const sPend = sb != null && !(s && Math.abs(sb - s.price) < 1e-9) ? sb : null;
    const tPend = tb != null && !(tPx != null && Math.abs(tb - tPx) < 1e-9) ? tb : null;
    const pnlAt = x => entry == null ? null : (x - entry) * d * 100 * n;
    const rAt = x => entry == null || !(loss1 > 0) ? null : (x - entry) * d / loss1;
    return {v, t, width, credit, d, entry, loss1, n, step, open, s, tw, tPx, hasTarget, cPx, sPend, tPend, pnlAt, rAt};
  }

  function splChip(sp, m, kind) {
    const isStop = kind === "stop", price = isStop ? m.sPend : m.tPend;
    const plan = isStop ? m.v.splan : m.v.tplan, cur = isStop ? (m.s ? m.s.price : null) : m.tPx;
    const what = isStop ? (m.s ? "Move app stop" : "Watch app stop at") : (m.hasTarget ? "Move GTC target" : "Set GTC target at");
    const busy = m.v.busy;
    const res = busy ? `<span class="amb">An action is in flight on this spread.</span>`
      : !plan ? `<span class="mut">Pricing...</span>`
      : !plan.ok ? `<span class="neg">${esc((plan.errors || []).join("; ") || "refused")}</span>`
      : `<b class="num ${cls(Number(plan.gross))}">${money(Number(plan.gross))}</b>`
        + (plan.r != null ? ` <span class="num ${isStop ? "neg" : "pos"}">${fmtR(isStop ? -plan.r : plan.r)}</span>` : "")
        + ` · ${esc(plan.held)} spread${plan.held === 1 ? "" : "s"}, before fees`
        + (!isStop && plan.pct != null ? ` · ${Math.round(plan.pct * 100)}% of max gain` : "")
        + (plan.warnings || []).map(w => ` · <span class="amb">${esc(w)}</span>`).join("");
    const mode = isStop ? `<div class="l2 ${m.v.ssend ? "amb" : ""}">${m.v.ssend
        ? "Send when hit ON: the app sends one close when it fires."
        : "Alert only: nothing is sent when it hits."}</div>`
      : `<div class="l2">GTC paired close, rests at Schwab.</div>`;
    return `<div class="ldchip ${isStop ? "s" : "t"}">
      <div class="l1">${what} ${cur != null ? `${n2(cur)} <span class="ar">to</span> ` : ""}<b class="num">${n2(price)}</b></div>
      <div class="l2">${res}</div>${mode}
      <div class="l3"><span class="mut spl-how">Sends only when you click the ${isStop ? "Stop" : "Target"} row's outlined button.</span></div>
      <div class="l3"><button type="button" class="btn sm spl-cancel" data-splcancel="${kind}">Cancel</button><span class="mut">↑ ↓ step · Esc</span></div></div>`;
  }

  function spreadLadder(sp, m) {
    const {width, entry, d, step, s} = m, sid = sp.spread_id;
    const pad = width * 0.05, lo = -pad, hi = width + pad;
    const {w: W, h: H} = SGEOM;
    const Yp = x => Math.max(0, Math.min(H, (hi - x) / (hi - lo) * H));
    const pct = y => (y / H * 100).toFixed(3) + "%";
    const now = m.v.value != null && isFinite(m.v.value) ? Number(m.v.value) : null;
    const rTx = r => r == null ? "" : `${r > 0 ? "+" : ""}${r.toFixed(2)}R`;
    // Levels on the axis.
    const L = [{k: m.credit ? "gain" : "loss", v: 0, name: m.credit ? "max gain" : "max loss"},
               {k: m.credit ? "loss" : "gain", v: width, name: m.credit ? "max loss" : "max gain"}];
    if (entry != null) L.push({k: "entry", v: entry, name: m.t.basis === "fill" ? "entry" : "entry limit"});
    if (s) L.push({k: "stop", v: Number(s.price), name: "app stop"});
    if (m.tPx != null) L.push({k: "target", v: m.tPx, name: "GTC target"});
    if (m.cPx != null) L.push({k: "close", v: m.cPx, name: "close order"});
    if (entry != null && m.loss1 > 0) for (const r of [-0.5, 0.5, 1, 2]) {
      const x = entry + d * m.loss1 * r;
      if (x > step && x < width - step && !L.some(o => Math.abs(o.v - x) < width * 0.03))
        L.push({k: "rm", v: x, name: "", rr: `${r > 0 ? "+" : ""}${r}R`});
    }
    for (const x of L) { x.r = m.rAt(x.v); x.pnl = m.pnlAt(x.v); }
    // Left rows: every level plus the mid, spaced so the text never overlaps.
    const rank = {stop: 0, target: 1, close: 2, now: 3, entry: 4, gain: 5, loss: 5, rm: 6};
    const rows = [];
    const all = L.map(x => ({...x, y: Yp(x.v)}));
    if (now != null) all.push({k: "now", v: now, y: Yp(now), name: "mid", r: m.rAt(now)});
    for (const x of all) {
      const same = rows.find(o => Math.abs(o.v - x.v) < 1e-9 && o.k !== "now" && x.k !== "now");
      if (!same) { rows.push(x); continue; }
      if (x.k !== "rm") same.name = same.k === "rm" ? x.name : `${same.name} / ${x.name}`;
      if (rank[x.k] < rank[same.k]) same.k = x.k;
    }
    spreadOut(rows, 19, H);
    const leftRows = rows.map(x => `<div class="lr ${x.k}" style="top:${pct(x.dy)}">
        <span class="rr num">${x.k === "rm" ? x.rr : rTx(x.r)}</span><span class="nm" title="${esc(x.name)}">${esc(x.name)}</span>
        <span class="pv num">${n2(x.v)}</span></div>`).join("");
    // Cards: the two ends (static), the app stop and the target (dragged when OPEN), a working close.
    const info = x => `${x.pnl != null ? `<span class="num ${cls(x.pnl)}">${money(x.pnl)}</span>` : ""}${x.r != null ? ` <span class="num ${cls(x.r)}">${fmtR(x.r)}</span>` : ""}`;
    const stopState = !s ? "" : s.status === "fired" ? ` · <span class="neg">hit at mid ${n2(s.fired_mid)}</span>`
      : s.status === "sent" ? ` · <span class="neg">used</span>` : s.status === "suspended" ? ` · <span class="amb">not watching</span>`
      : s.status === "paused" ? ` · <span class="mut">paused</span>` : ` · <span class="mut">watching</span>`;
    const cards = [];
    for (const x of L) {
      if (x.k === "gain" || x.k === "loss")
        cards.push({k: x.k === "gain" ? "endg" : "endl", v: x.v, title: x.k === "gain" ? "Max gain" : "Max loss", info: info(x), tools: ""});
      if (x.k === "stop") cards.push({k: "stop", v: x.v, drag: m.open, title: "App stop", info: info(x) + stopState,
        tools: `<span class="tag${s.send ? " sends" : ""}" title="${s.send ? "The app sends one close when it fires, only while it runs" : "Alert only: nothing is sent"}">${s.send ? "SENDS" : "ALERT"}</span>`});
      if (x.k === "target") cards.push({k: "target", v: x.v, drag: m.open, title: "GTC target",
        info: info(x) + (m.tw && m.tw.filled ? ` · <span class="mut">${esc(m.tw.filled)} of ${esc(m.tw.quantity)} filled</span>` : ""), tools: `<span class="tag">AT SCHWAB</span>`});
      if (x.k === "close") cards.push({k: "close", v: x.v, title: "Close working", info: info(x), tools: ""});
    }
    const slots = [];
    if (m.open && !s && entry != null && m.loss1 > 0)
      slots.push({k: "stop", slot: true, drag: true, v: splSnap(entry - d * m.loss1 * 0.5, step, width),
                  title: "No app stop", info: `<span class="mut">drag to watch one (Schwab holds no spread stop)</span>`, tools: ""});
    if (m.open && !m.hasTarget && entry != null)
      slots.push({k: "target", slot: true, drag: true, v: splSnap(m.credit ? entry * 0.5 : entry + (width - entry) * 0.5, step, width),
                  title: "No target", info: `<span class="mut">drag to set a GTC target</span>`, tools: ""});
    cards.forEach(c => { c.y = Yp(c.v); });
    spreadOut(cards, CARDH + 8, H - (slots.length ? slots.length * (CARDH + 6) + 8 : 0));
    const pendOf = k => k === "stop" ? m.sPend : k === "target" ? m.tPend : null;
    const cardHtml = c => {
      const pv = pendOf(c.k), dv = pv != null ? pv : c.v;      // a grab starts from the pending line
      const grip = c.drag
        ? `<span class="grip" data-spdragk="${c.k}" data-v="${dv}" data-slot="${c.slot ? 1 : 0}" tabindex="0" title="Drag up or down. It only fills the ${c.k === "stop" ? "Stop" : "Target"} row's price box: nothing is sent until you click that row's button.">⠿</span>`
        : `<span class="grip off"${c.k === "stop" || c.k === "target" ? ` title="Drag works on an OPEN spread only"` : ""}>${c.k === "stop" || c.k === "target" ? "⠿" : ""}</span>`;
      return `<div class="lc ${c.k}${c.slot ? " slot" : ""}${SLD && SLD.kind === c.k || pv != null && !c.slot ? " moving" : ""}" style="top:${pct(c.dy)}">
        ${grip}<b class="ti">${esc(c.title)}</b><span class="tl">${c.tools}</span><span class="in2">${c.info}</span></div>`;
    };
    const lines = rows.filter(x => Math.abs(x.dy - x.y) > 1.5).map(x => `<line class="ld-${x.k}" x1="${LW + PXW + 2}" y1="${x.dy}" x2="${RAILX}" y2="${x.y}"/>`)
      .concat(cards.map(c => `<path class="lc-${c.k}" d="M${RAILX + RAILW} ${c.y} H${RAILX + RAILW + 6} L${CARDX - 6} ${c.dy} H${CARDX}"/>`)).join("");
    // Rail: reward from the entry toward the target (else the max gain end), risk toward the app stop
    // (else the max loss end); the open move drawn brighter.
    const e = entry ?? now ?? 0, ye = Yp(e);
    const zone = (yFar, c) => {
      const top = Math.min(ye, yFar), h = Math.abs(yFar - ye);
      return h < 0.5 ? "" : `<div class="zn ${c} ${yFar < ye ? "up" : "dn"}" style="top:${pct(top)};height:${pct(h)}"></div>`;
    };
    const gainEnd = m.credit ? 0 : width, lossEnd = m.credit ? width : 0;
    const yl = now != null ? Yp(now) : ye, up = now == null || (now - e) * d >= 0;
    const rail = zone(Yp(m.tPx ?? gainEnd), "rew") + zone(Yp(s ? Number(s.price) : lossEnd), "risk")
      + (now != null ? `<div class="mv ${up ? "g" : "l"}" style="top:${pct(Math.min(ye, yl))};height:${pct(Math.max(2, Math.abs(yl - ye)))}"></div>` : "");
    const ticks = L.map(x => x.k === "rm" ? `<div class="dot" style="top:${pct(Yp(x.v))}"></div>`
                                          : `<div class="cap ${x.k}" style="top:${pct(Yp(x.v))}"></div>`).join("")
      + (now != null ? `<div class="knob ${up ? "g" : "l"}" style="top:${pct(yl)}"></div>` : "");
    // Pending lines (the row's price box) and, while dragging, the dragged line.
    // A pending line's price tag is a grip too (the chip may sit over its card).
    const ghost = (k, x, grab) => `<div class="ghost ${k}" style="top:${pct(Yp(x))}"><span class="gp num"${grab
      ? ` data-spdragk="${k}" data-v="${x}" title="Drag to adjust. Nothing is sent until you click the row's button."` : ""}>${n2(x)}</span></div>`;
    let ghosts = "";
    if (SLD && SLD.sid === sid) ghosts = ghost(SLD.kind, SLD.price, false);
    else {
      if (m.sPend != null) ghosts += ghost("stop", m.sPend, m.open);
      if (m.tPend != null) ghosts += ghost("target", m.tPend, m.open);
    }
    const chipKind = SLD ? null : (SPL_LAST === "target" ? ["target", "stop"] : ["stop", "target"]).find(k => pendOf(k) != null) || null;
    const chipAt = chipKind ? pendOf(chipKind) : null;
    const chip = chipKind ? `<div class="ldwrap" style="top:${pct(Math.max(0, Math.min(H - 170, Yp(chipAt) + 10)))}">${splChip(sp, m, chipKind)}</div>` : "";
    return `<div class="ladder spl${d < 0 ? " short" : ""}" data-sid="${esc(sid)}" data-lo="${lo}" data-hi="${hi}" data-step="${step}" data-w="${width}" style="--lw:${LW}px;--pxw:${PXW}px;--railx:${RAILX}px;--railw:${RAILW}px;--cardx:${CARDX}px">
      ${now != null ? `<div class="nowband" style="top:${pct(yl)}"></div>` : ""}
      <div class="rail">${rail}</div>${ticks}
      <svg class="conn" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">${lines}</svg>
      ${leftRows}
      <div class="cards">${cards.map(cardHtml).join("")}</div>
      ${slots.length ? `<div class="slots">${slots.map(c => cardHtml({...c, dy: 0})).join("")}</div>` : ""}
      ${ghosts}${chip}
    </div>`;
  }

  function splCaption(sp, m) {
    const t = m.t, kind = `${m.credit ? "credit" : "debit"} ${String(sp.kind).startsWith("CALL") ? "call" : "put"}`;
    const bits = [`Net price per spread, 0 to the ${n2(m.width)} width (${kind})`,
                  `$ for ${m.n} spread${m.n === 1 ? "" : "s"}, before fees`];
    if (t.max_loss != null) bits.push(`1R = max loss ${money(Number(t.max_loss), false)}`);
    if (t.breakeven != null) bits.push(`breakeven ${n2(t.breakeven)} on the underlying at expiry`);
    if (t.basis !== "fill") bits.push(`<span class="amb">entry is the ticket's limit (fill prices not listed yet)</span>`);
    if (!m.open) bits.push(`<span class="amb">drag is off: the spread is ${esc(String(sp.state).replaceAll("_", " ").toLowerCase())}</span>`);
    return `<div class="spl-cap">${bits.join(" · ")}</div>`;
  }

  // spreads.js calls this with its workbench HTML; Classic and the floating windows get it back unchanged.
  window.COCKPIT_SPREAD = (sp, st, html, B) => {
    if (!ON || VIEW || !sp || !B) return html;
    try {
      BENCH = B;
      LD = null;                               // a stock line waiting for Send belongs to another workbench
      const m = splModel(sp, st, B);
      if (!m) return html;
      const pend = [m.sPend != null ? "stop" : "", m.tPend != null ? "target" : ""].join(" ").trim();
      const i = html.startsWith('<div class="lock">') ? html.indexOf("</div>") + 6 : 0;
      if (!SPL_RAF) { SPL_RAF = true; queueMicrotask(() => { try { splAfter(); } catch (e) { SPL_RAF = false; console.error("spread ladder", e); } }); }      // runs right after spreads.js writes the HTML
      return html.slice(0, i) + `<div class="spl-cw" data-pend="${pend}"><div class="spl-side">${splCaption(sp, m)}${spreadLadder(sp, m)}</div>${html.slice(i)}</div>`;
    } catch (e) { console.error("spread ladder", e); return html; }
  };
  // After spreads.js wrote the workbench: redraw once when the ladder's box size changed, and put the
  // focus back where a key or a drop left it.
  function splAfter() {
    SPL_RAF = false;
    const lad = document.querySelector("#workspace .ladder.spl");
    if (!lad || SLD) return;
    const w = lad.clientWidth, h = lad.clientHeight;
    if (w && h && (Math.abs(w - SGEOM.w) > 2 || Math.abs(h - SGEOM.h) > 2)) { SGEOM = {w, h}; if (lastState) render(lastState); return; }
    const keep = SPL_KEEP; SPL_KEEP = null;
    if (keep && (!document.activeElement || document.activeElement === document.body)) {
      lad.querySelector(`.lc [data-spdragk="${keep.grip}"]`)?.focus({preventScroll: true});
    }
    const f = SPL_FOCUS;
    if (!f) return;
    if (Date.now() - f.at > 6000) { SPL_FOCUS = null; return; }
    // Focus only ever goes back to a ladder line, never to a row button: sending is always a click.
    lad.querySelector(`[data-spdragk="${f.kind}"]`)?.focus({preventScroll: true});
    SPL_FOCUS = null;
  }
  // Every poll rewrites the workbench (the page clears it before spreads.js redraws it): note, before
  // that, a ladder line or row button that has the keyboard, so splAfter can put it back (a button
  // only while its price is unchanged).
  function splNoteFocus() {
    const ae = document.activeElement;
    if (ae?.matches?.("#workspace .ladder.spl .lc [data-spdragk]")) SPL_KEEP = {grip: ae.dataset.spdragk};
  }

  document.addEventListener("pointerdown", e => {
    if (!ON || !BENCH) return;
    const h = e.target.closest(".ladder.spl [data-spdragk]");
    if (!h) return;
    const lad = h.closest(".ladder"), sid = lad.dataset.sid, kind = h.dataset.spdragk, sp = spOf(sid);
    if (!sp || sp.state !== "OPEN" || (kind !== "stop" && kind !== "target")) return;
    // Nothing keeps the keyboard during a drag: a focused button could otherwise take an Enter.
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    e.preventDefault();
    const step = +lad.dataset.step || 0.05, wd = +lad.dataset.w, v0 = +h.dataset.v, y0 = e.clientY;
    const k = lad.getBoundingClientRect().height / (+lad.dataset.hi - +lad.dataset.lo);
    SLD = {sid, kind, price: v0};
    window.HOLD_WORKSPACE = true;
    lad.setPointerCapture(e.pointerId);
    lad.classList.add("dragging");
    paintSplGhost(lad);
    const move = ev => { SLD.price = splSnap(v0 - (ev.clientY - y0) / k, step, wd); paintSplGhost(lad); };
    const end = keep => {
      lad.removeEventListener("pointermove", move); lad.removeEventListener("pointerup", up); lad.removeEventListener("pointercancel", abort);
      window.HOLD_WORKSPACE = false;
      const p = SLD.price; SLD = null;
      if (keep && Math.abs(p - v0) >= step / 2) {
        SPL_LAST = kind; SPL_FOCUS = {kind, at: Date.now()};
        if (BENCH.setPrice(sid, kind, p.toFixed(2))) return;
      }
      if (lastState) render(lastState);
    };
    const up = () => end(true), abort = () => end(false);
    SLD.abort = abort;                       // Esc during the drag: nothing is kept
    lad.addEventListener("pointermove", move); lad.addEventListener("pointerup", up); lad.addEventListener("pointercancel", abort);
  });
  function paintSplGhost(lad) {
    const lo = +lad.dataset.lo, hi = +lad.dataset.hi;
    lad.querySelectorAll(".ghost").forEach(g => g.remove());
    lad.querySelector(".ldwrap")?.remove();
    const g = document.createElement("div");
    g.className = "ghost " + SLD.kind;
    g.style.top = Math.max(0, Math.min(100, (hi - SLD.price) / (hi - lo) * 100)) + "%";
    g.innerHTML = `<span class="gp num">${n2(SLD.price)}</span>`;
    lad.appendChild(g);
  }
  function splCancel(sid, kind) {
    if (!BENCH || !kind) return false;
    const sp = spOf(sid), m = sp ? splModel(sp, lastState, BENCH) : null;
    if (!m || (kind === "stop" ? m.v.sprice : m.v.tprice) === "") return false;
    SPL_FOCUS = null;
    return BENCH.setPrice(sid, kind, "");
  }
  document.addEventListener("click", e => {
    const c = e.target.closest("[data-splcancel]");
    if (!c) return;
    const lad = c.closest(".ladder.spl");
    if (lad) splCancel(lad.dataset.sid, c.dataset.splcancel);
  });
  // Keys on the spread ladder: arrows nudge a focused line one grid step (Shift: ten), Esc clears the
  // pending price box. During a drag every key stops here and Esc abandons the drag. Nothing on the
  // ladder sends: the row's button takes a click. They stop here, so the page's own arrow / Esc
  // hotkeys do not also switch the selected trade.
  document.addEventListener("keydown", e => {
    if (!ON || !BENCH) return;
    if (SLD) {
      e.preventDefault(); e.stopPropagation();
      if (e.key === "Escape") SLD.abort?.();
      return;
    }
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const side = e.target.closest?.(".spl-side"), lad = side?.querySelector(".ladder.spl");
    if (!lad) return;
    const sid = lad.dataset.sid, g = e.target.closest?.("[data-spdragk]");
    if (e.key === "Escape") {
      if (splCancel(sid, g ? g.dataset.spdragk : SPL_LAST)) { e.preventDefault(); e.stopPropagation(); }
      return;
    }
    if (!g) return;
    const kind = g.dataset.spdragk;
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault(); e.stopPropagation();
      const sp = spOf(sid); if (!sp || sp.state !== "OPEN") return;
      const step = +lad.dataset.step || 0.05, n = (e.shiftKey ? 10 : 1) * (e.key === "ArrowUp" ? 1 : -1);
      SPL_LAST = kind; SPL_FOCUS = {to: "grip", kind, at: Date.now()};
      BENCH.setPrice(sid, kind, splSnap(+g.dataset.v + n * step, step, +lad.dataset.w).toFixed(2));
      return;
    }
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); }   // never a send from the ladder
  }, true);

  // ---- hook into the page's render (spreads.js wraps this one in turn) ------------------------
  const pageRender = render;
  render = function (st) {
    if (ON) try { splNoteFocus(); } catch (e) { /* focus is a convenience */ }
    pageRender(st);
    try {
      const rev = st.ui?.prefs_rev;
      if (rev != null && rev !== NS_REV && NS_REV !== "loading") { NS_REV = "loading"; loadNostop(rev); }
      renderAttn(st);
      renderDock();
      // The ladder is laid out in pixels: redraw once when its box size changes.
      const lad = document.querySelector("#workspace .ladder:not(.spl)");    // the spread ladder sizes itself (splAfter)
      if (lad && !LD?.dragging) {
        const w = lad.clientWidth, h = lad.clientHeight;
        if (w && h && (Math.abs(w - GEOM.w) > 2 || Math.abs(h - GEOM.h) > 2)) { GEOM = {w, h}; render(lastState); }
      }
    } catch (e) { console.error("cockpit", e); }
  };
  if (lastState) render(lastState);
})();
