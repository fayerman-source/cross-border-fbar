// FBAR worksheet review: render the worksheet, trace any figure to its source line, record the CPA's review.

const $ = (id) => document.getElementById(id);
const DOC = window.DOC_BASE ?? "/doc/"; // where page images are served (a static snapshot overrides it)
let state = null;
let selected = null; // key of the traced figure or chip currently shown

const OWNERSHIP = { sole: "Owner", joint: "Joint owner", signature_authority: "Sig. authority", unknown: "Not stated" };
const OWNERSHIP_LONG = { sole: "owner", joint: "joint owner", signature_authority: "signature authority only" };
const FLAG = {
  max_not_at_month_end: () => "Highest balance falls mid-month, not at a month end",
  possible_intraday_peak: () => "Possible same-day peak above the reported maximum",
  missing_statement: (f) => `No statement for ${f.month}`,
  coverage_gap: () => "Days in the year no statement covers",
  signature_authority_only: () => "Signature authority only, no financial interest",
  institution_names_differ: () => "Statements print different bank names",
  max_from_computed_balance: () => "Highest balance computed from the lines; not printed",
  no_balance_anchor: () => "No balance on any document: the maximum can't be determined",
  account_number_masked: () => "Account number partly masked: can't be matched to an account",
  account_unidentified: () => "Account number unreadable",
  field_unreadable: () => "A key field couldn't be read",
  continuity_break: () => "Statements don't chain: closing ≠ next opening",
  arithmetic_mismatch: () => "Printed balance doesn't match the lines",
  duplicate_upload: () => "Same statement uploaded twice",
  out_of_year_statement: () => "Statement outside the tax year (ignored)",
  currency_unknown: () => "Currency unknown: no Treasury rate",
  currency_conflict: () => "Statements show different currencies",
  ownership_unknown: () => "Ownership not stated at intake",
  no_statements: () => "Listed at intake, but no statement was read",
  page_not_read: () => "Page couldn't be read",
};

// Show the reconciler's message only where it adds facts the title doesn't carry.
const DETAIL = new Set(["possible_intraday_peak", "arithmetic_mismatch", "continuity_break", "coverage_gap",
  "field_unreadable", "page_not_read", "institution_names_differ", "out_of_year_statement", "currency_conflict", "duplicate_upload"]);
const ACTION = {
  no_balance_anchor: "Request statements showing balances for this account.",
  missing_statement: "Request the missing statement, or confirm none exists.",
  account_number_masked: "Match these rows to a known account, or request the full account number.",
  signature_authority_only: "Confirm the client has signature authority but no financial interest.",
  possible_intraday_peak: "Check whether the same-day deposit cleared before the withdrawal.",
  no_statements: "Request the statements for this account, or confirm it had none this year.",
  page_not_read: "Re-read the page or request a clearer copy; nothing on it is counted.",
  institution_names_differ: "Confirm whether the bank changed (merger or rename) or one bank is printed two ways.",
};

function money(amount, ccy) {
  if (amount === null || amount === undefined) return null;
  const digits = ccy === "KRW" ? 0 : 2;
  return Number(amount).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
const usd = (n) => (n === null || n === undefined ? "—" : "$" + Number(n).toLocaleString("en-US"));
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid);
  return e;
};

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function reviewer() {
  return $("reviewer").value.trim();
}

function askNote(title) {
  return new Promise((resolve) => {
    const d = $("note-dialog");
    $("note-title").textContent = title;
    $("note-text").value = "";
    d.showModal();
    $("note-cancel").onclick = () => { d.close(); resolve(null); };
    d.onclose = () => resolve(d.returnValue === "ok" ? $("note-text").value.trim() : null);
  });
}

// --- the source viewer -------------------------------------------------------------------------
function show(sources, key, caption) {
  selected = key;
  const usable = sources.filter((s) => s.upload);
  if (!usable.length) return;
  const page = usable[0].upload;
  const img = $("page");
  const draw = () => {
    const svg = $("marks");
    svg.replaceChildren();
    for (const s of usable.filter((x) => x.upload === page && x.bbox)) {
      const b = s.bbox;
      const pad = 6;
      const x = Math.max(0, b.x_min - pad), y = Math.max(0, b.y_min - pad / 2);
      const w = Math.min(1000, b.x_max + pad) - x, h = Math.min(1000, b.y_max + pad / 2) - y;
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      Object.entries({ x, y, width: w, height: h, rx: 6, ry: 3, class: "loop", pathLength: 100 }).forEach(([k, v]) => r.setAttribute(k, v));
      r.style.setProperty("--len", 100);
      svg.append(r);
    }
    const first = usable.find((x) => x.upload === page && x.bbox);
    if (first) {
      const sheet = $("sheet");
      $("page").closest(".viewer").scrollTo({ top: (first.bbox.y_min / 1000) * sheet.offsetHeight - 140, behavior: "smooth" });
    }
  };
  if (img.getAttribute("src") !== `${DOC}${page}`) {
    img.onload = draw;
    img.src = `${DOC}${page}`;
    img.alt = `Source page ${page}`;
  } else draw();
  const extra = usable.length > 1 ? ` (+${usable.length - 1} more line${usable.length > 2 ? "s" : ""})` : "";
  $("viewer-caption").textContent = `${caption} — ${usable[0].wp}${extra}`;
  document.querySelectorAll("[aria-pressed]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.key === key ? "true" : "false"));
}

// --- rendering ---------------------------------------------------------------------------------
const flagTitle = (f) => (FLAG[f.code] ? FLAG[f.code](f) : f.code);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function render() {
  const signed = !!state.review.signoff;
  renderHeader(signed);
  renderAccounts(signed);
  renderItems(signed);
  renderSignoff(signed);
  renderAudit();
}

function renderHeader(signed) {
  const ws = state.worksheet;
  $("meta").textContent = `Tax year ${ws.tax_year} · Client: ${ws.client} · synthetic data, fictional person`;
  $("scope").textContent = ws.scope;
  $("prepared").textContent = `Read by ${ws.reader.model} (${ws.reader.prompt})`;
  const so = state.review.signoff;
  const reviewed = signed
    ? el("span", {}, el("span", { class: "initials" }, so.initials), so.ts)
    : el("span", { class: "pending" }, "not yet");
  $("reviewed").replaceChildren(reviewed);
}

function renderAccounts(signed) {
  const ws = state.worksheet;
  $("accounts").replaceChildren(...ws.accounts.map((a) => accountRow(a, signed)));
  const t = ws.totals;
  const notes = [`${t.above_threshold ? "above" : "not above"} the $${t.threshold_usd.toLocaleString("en-US")} FBAR aggregate threshold — review`];
  if (t.accounts_without_value.length) notes.push(`${plural(t.accounts_without_value.length, "account")} without a determinable balance not included`);
  if (t.unidentified_uploads.length) notes.push("unidentified rows not counted");
  $("aggregate").replaceChildren(
    el("td", { colspan: 4 }, el("span", { class: "label" }, "Aggregate of highest balances"), el("span", { class: "threshold" }, notes.join(" · "))),
    el("td", { class: "num" }, el("span", { class: "amount" }, usd(t.aggregate_usd))),
  );
  const r0 = Object.values(ws.rates)[0] || { record_date: "", source: "#" };
  $("rate-note").replaceChildren(`Treasury Reporting Rates of Exchange, ${r0.record_date}, foreign units per USD; USD rounded up. `,
    el("a", { href: r0.source, target: "_blank", rel: "noopener" }, "Source query"));
}

function renderItems(signed) {
  const ws = state.worksheet;
  const open = state.open_review_flags;
  $("open-count").textContent = open.length ? `${open.length} open` : "all reviewed";
  $("open-count").classList.toggle("clear", open.length === 0);
  const reviewFlags = ws.flags.filter((f) => f.severity === "review");
  $("items").replaceChildren(...reviewFlags.map((f) => itemRow(f, state.review.acks[f.id], signed, ws)));
  const info = ws.flags.filter((f) => f.severity === "info");
  $("info-summary").textContent = `Notes (${info.length})`;
  $("info-items").replaceChildren(...info.map((f) => el("li", {}, `${flagTitle(f)} · ${acctLabel(ws, f.account)} `, chips(f))));
}

function signoffHint(open) {
  if (!open.length) return "All review items acknowledged.";
  return `Acknowledge the ${plural(open.length, "open review item")} to sign off.`;
}

function renderSignoff(signed) {
  const open = state.open_review_flags;
  $("signoff-btn").disabled = signed || open.length > 0;
  $("signoff-form").hidden = signed;
  $("signoff-hint").textContent = signed ? "" : signoffHint(open);
  $("after-signoff").hidden = !signed;
  if (!signed) return;
  const so = state.review.signoff;
  $("signed-line").replaceChildren(el("span", { class: "initials" }, so.initials),
    ` Signed off by ${so.reviewer}, ${so.ts}. Fingerprint ${so.hash.slice(0, 12)}.`);
  renderSummary(state.review.summary);
}

function renderSummary(s) {
  $("summary-box").hidden = !s;
  $("draft-summary").hidden = Boolean(s?.approved);
  if (!s) return;
  $("summary-text").textContent = s.text;
  $("summary-blocked").hidden = !s.blocked;
  $("summary-blocked").textContent = s.blocked ? `Held back: the translation ${s.blocked}. Draft it again.` : "";
  const fresh = Object.entries(s.unreviewed || {});
  $("summary-unreviewed").hidden = !fresh.length || Boolean(s.blocked || s.approved);
  $("summary-unreviewed-list").replaceChildren(...fresh.map(([k, v]) => el("li", {}, v, el("span", { class: "sub" }, ` (${k})`))));
  $("approve-summary").textContent = fresh.length ? `Approve, including ${fresh.length} new phrases` : "Approve for release";
  $("approve-summary").hidden = Boolean(s.approved || s.blocked);
  $("summary-approved").hidden = !s.approved;
  if (s.approved) $("summary-approved").textContent = `Approved for release by ${s.approved.reviewer}, ${s.approved.ts}.`;
}

function auditLine(e) {
  const note = e.note ? ` — ${e.note}` : "";
  const change = e.after ? ` (${e.before} → ${e.after})` : "";
  return `${e.ts}  ${e.reviewer}  ${e.action}${note}${change}`;
}

function renderAudit() {
  $("audit").replaceChildren(...state.audit.slice().reverse().map((e) => el("li", {}, auditLine(e))));
}

function acctLabel(ws, key) {
  const a = ws.accounts.find((x) => x.key === key);
  return a ? `${(a.institutions[a.institutions.length - 1] || "Bank not named")} · ${a.account_number}` : key;
}

function accountRow(a, signed) {
  const m = a.max;
  const corrected = a.key in state.review.overrides;
  const select = el("select", { "aria-label": `Ownership of ${a.account_number}`, disabled: signed || !a.identified,
    onchange: async (ev) => {
      const next = ev.target.value;
      const note = await askNote(`Change ownership of ${a.account_number} to “${OWNERSHIP_LONG[next]}”`);
      if (!note) { ev.target.value = a.ownership; return; }
      try { state = await api("/api/ownership", { account: a.key, ownership: next, note, reviewer: reviewer() || "reviewer" }); render(); toast("Ownership corrected"); }
      catch (e) { toast(e.message); ev.target.value = a.ownership; }
    } },
    ...Object.entries(OWNERSHIP).filter(([k]) => k !== "unknown" || a.ownership === "unknown")
      .map(([k, v]) => el("option", { value: k, selected: k === a.ownership }, v)));
  const key = `max:${a.key}`;
  const figure = m
    ? el("button", { class: "traced", "data-key": key, "aria-pressed": "false",
        onclick: () => show([m.source], key, `Highest balance, ${a.account_number}`) },
        el("span", { class: "figure" }, el("span", { class: "amount" }, money(m.amount, a.currency)), el("span", { class: "ccy" }, a.currency)),
        el("span", { class: "wp" }, m.source.wp),
        m.kind === "computed" ? el("span", { class: "kind" }, "computed from lines") : null)
    : el("span", { class: "unknown" }, a.identified ? "can't be determined" : "not counted");
  const banks = a.institutions;
  return el("tr", { class: a.identified ? "" : "unidentified" },
    el("td", { class: "bank" }, banks.length ? banks[banks.length - 1] : "—",
      banks.length > 1 ? el("span", { class: "sub" }, `also printed as ${banks.slice(0, -1).join(", ")}`) : null,
      a.holders[0] ? el("span", { class: "sub holder", title: a.holders.join("; ") }, a.holders[0]) : null),
    el("td", {}, el("span", { class: "acct" }, a.account_number)),
    el("td", { class: "ownership" }, select, corrected ? el("span", { class: "corrected" }, "corrected by reviewer") : null),
    el("td", { class: "num" }, figure),
    el("td", { class: "num usd" }, el("span", { class: "amount" }, usd(a.max_usd)),
      a.rate && a.max_usd !== null ? el("span", { class: "rate" }, `@ ${a.rate} ${a.currency}/USD`) : null),
  );
}

function chips(f) {
  return el("span", { class: "sources" }, ...f.sources.filter((s) => s.upload).map((s, i) => {
    const key = `flag:${f.id}:${i}`;
    return el("button", { class: "source-chip", "data-key": key, "aria-pressed": "false",
      onclick: () => show(f.sources.slice(i).concat(f.sources.slice(0, i)), key, flagTitle(f)) }, s.wp);
  }));
}

function itemRow(f, ack, signed, ws) {
  const title = flagTitle(f);
  const actions = ack
    ? (signed ? null : el("button", { class: "link", onclick: async () => { state = await api("/api/reopen", { flag: f.id, reviewer: reviewer() || "reviewer" }); render(); } }, "Reopen"))
    : el("button", { class: "ack", disabled: signed, onclick: async () => {
        const note = await askNote(`Acknowledge: ${title}`);
        if (!note) return;
        try { state = await api("/api/ack", { flag: f.id, note, reviewer: reviewer() || "reviewer" }); render(); toast("Acknowledged"); }
        catch (e) { toast(e.message); }
      } }, "Acknowledge");
  return el("li", { class: `item${ack ? " done" : ""}` },
    el("div", {},
      el("p", { class: "item-title" }, title),
      el("p", { class: "item-acct" }, acctLabel(ws, f.account)),
      DETAIL.has(f.code) ? el("p", { class: "item-msg" }, f.message) : null,
      ACTION[f.code] ? el("p", { class: "item-do" }, ACTION[f.code]) : null,
      chips(f)),
    el("div", { class: "item-actions" }, actions),
    ack ? el("p", { class: "ack-note" }, `${ack.note} — ${ack.reviewer}`) : null);
}

// --- actions -----------------------------------------------------------------------------------
$("signoff-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    state = await api("/api/signoff", { reviewer: reviewer(), initials: $("initials").value.trim() });
    render(); toast("Signed off");
  } catch (e) { toast(e.message); }
});
$("draft-summary").addEventListener("click", async () => {
  $("draft-summary").disabled = true; $("draft-summary").textContent = "Drafting…";
  try { state = await api("/api/summary/draft", {}); render(); }
  catch (e) { toast(e.message); }
  finally { $("draft-summary").disabled = false; $("draft-summary").textContent = "Draft again"; }
});
$("approve-summary").addEventListener("click", async () => {
  try { state = await api("/api/summary/approve", { reviewer: reviewer() || state.review.signoff.reviewer,
    confirm_unreviewed: Object.keys(state.review.summary?.unreviewed || {}).length > 0 }); render(); toast("Approved for release"); }
  catch (e) { toast(e.message); }
});
$("reviewer").value = (() => { try { return localStorage.getItem("reviewer") || ""; } catch { return ""; } })();
$("reviewer").addEventListener("change", () => { try { localStorage.setItem("reviewer", reviewer()); } catch { /* private mode */ } });

try {
  state = await api("/api/state");
  render();
} catch (e) {
  $("meta").textContent = `Couldn't load the worksheet: ${e.message}`;
}
