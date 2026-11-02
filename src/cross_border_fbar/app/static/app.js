// FBAR worksheet review: render the worksheet, trace any figure to its source line, record the preparer's review.

const $ = (id) => document.getElementById(id);
const DOC = window.DOC_BASE ?? "/doc/"; // where page images are served (a static snapshot overrides it)
let state = null;
let selected = null; // key of the traced figure or chip currently shown
let viewing = null; // the page and line the source viewer is showing

const OWNERSHIP = { sole: "Owner", joint: "Joint owner", signature_authority: "Sig. authority", unknown: "Not stated" };
const OWNERSHIP_LONG = { sole: "owner", joint: "joint owner", signature_authority: "signature authority" };

function money(amount, ccy) {
  if (amount === null || amount === undefined) return null;
  const digits = ccy === "KRW" ? 0 : 2;
  return Number(amount).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
const usd = (n) => (n === null || n === undefined ? "n/a" : "$" + Number(n).toLocaleString("en-US"));
// Server-calculated Decimal strings, grouped without going through Number, which would lose digits.
function grouped(value) {
  const negative = value.startsWith("-");
  const [whole, fraction] = (negative ? value.slice(1) : value).split(".");
  const groups = [];
  for (let end = whole.length; end > 0; end -= 3) groups.push(whole.slice(Math.max(0, end - 3), end));
  groups.reverse();
  return (negative ? "-" : "") + groups.join(",") + (fraction === undefined ? "" : "." + fraction);
}
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
  toast.timer = setTimeout(() => t.classList.remove("show"), Math.max(2600, msg.length * 60)); // long enough to read
}

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json();
  if (data.reset) { // the hosted demo ended this visitor's session: show the fresh worksheet it started
    state = await api("/api/state");
    render();
    toast(data.error);
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function reviewer() {
  return $("reviewer").value.trim();
}

function askNote(title, exchangeRate = false, correction = null) {
  return new Promise((resolve) => {
    const d = $("note-dialog");
    $("note-title").textContent = title;
    $("note-text").value = "";
    $("rate-fields").hidden = !exchangeRate;
    $("correction-fields").hidden = !correction;
    $("correction-value").required = Boolean(correction);
    if (correction) {
      $("correction-read").textContent = correction.read;
      $("correction-value").value = correction.value;
      $("correction-direction-label").hidden = !correction.direction;
      if (correction.direction) $("correction-direction").value = correction.direction;
    }
    for (const id of ["rate-value", "rate-source", "rate-date"]) $(id).value = "";
    $("rate-date").value = `${state.worksheet.tax_year}-12-31`;
    $("rate-value").required = false;
    d.returnValue = "cancel";
    d.showModal();
    $("note-cancel").onclick = () => { d.close(); resolve(null); };
    d.onclose = () => {
      if (d.returnValue !== "ok") return resolve(null);
      const result = { note: $("note-text").value.trim() };
      if (exchangeRate && $("rate-value").value.trim()) result.rate = {
        units_per_usd: $("rate-value").value.trim(), source: $("rate-source").value.trim(),
        record_date: $("rate-date").value,
      };
      if (correction) {
        result.value = $("correction-value").value.trim();
        result.direction = $("correction-direction").value;
      }
      resolve(result);
    };
  });
}

// --- the source viewer -------------------------------------------------------------------------
function show(sources, key, caption) {
  selected = key;
  const usable = sources.filter((s) => s.upload);
  if (!usable.length) return;
  const page = usable[0].upload;
  viewing = { upload: page, ref: usable[0].ref };
  renderReadings();
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
      const viewer = $("page").closest(".viewer");
      const line = sheet.offsetTop + (first.bbox.y_min / 1000) * sheet.offsetHeight;
      const inView = line > viewer.scrollTop + 60 && line < viewer.scrollTop + viewer.clientHeight - 120;
      if (!inView) viewer.scrollTo({ top: line - 220, behavior: "smooth" });
    }
  };
  if (img.getAttribute("src") !== `${DOC}${page}`) {
    img.onload = draw;
    img.src = `${DOC}${page}`;
    img.alt = `Source page ${page}`;
  } else draw();
  const extra = usable.length > 1 ? ` (+${usable.length - 1} more line${usable.length > 2 ? "s" : ""})` : "";
  const location = usable.some((source) => source.bbox) ? "" : " · page only; location not verified";
  $("viewer-caption").textContent = `${caption}: ${usable[0].wp}${extra}${location}`;
  document.querySelectorAll("[aria-pressed]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.key === key ? "true" : "false"));
}

// --- rendering ---------------------------------------------------------------------------------
const flagTitle = (f) => f.title;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function render() {
  const signed = !!state.review.signoff;
  renderHeader(signed);
  renderAccounts(signed);
  renderValueCorrections();
  renderReadings();
  renderItems(signed);
  renderForm8938();
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
  const threshold = `$${t.threshold_usd.toLocaleString("en-US")} FBAR aggregate threshold`;
  let outcome;
  if (t.above_threshold === null) outcome = `result against the ${threshold} is undetermined`;
  else outcome = `observed total ${t.above_threshold ? "above" : "not above"} the ${threshold}`;
  const notes = [`${outcome}. Review`];
  if (t.accounts_without_value.length) notes.push(`${plural(t.accounts_without_value.length, "account")} without a determinable balance not included`);
  if (t.unidentified_uploads.length) notes.push("unidentified rows not counted");
  $("aggregate").replaceChildren(
    el("td", { colspan: 4 }, el("span", { class: "label" }, "Aggregate of highest observed balances"), el("span", { class: "threshold" }, notes.join(" · "))),
    el("td", { class: "num" }, el("span", { class: "amount" }, usd(t.aggregate_usd))),
  );
  const r0 = Object.values(ws.rates)[0] || { record_date: "", source: "#" };
  $("rate-note").replaceChildren(`Treasury Reporting Rates of Exchange, ${r0.record_date}, foreign units per USD; USD rounded up. `,
    el("a", { href: r0.source, target: "_blank", rel: "noopener" }, "Source query"));
}

// --- correcting a misread value ------------------------------------------------------------------
const VALUE_LABELS = { opening_balance: "Opening balance", closing_balance: "Closing balance", amount: "Amount", balance: "Balance" };
const valueLabel = (field) => VALUE_LABELS[field.split(":").pop()];
const readAs = (text) => `read as ${text || "nothing"}`;

function renderValueCorrections() {
  const corrections = state.worksheet.value_corrections;
  $("value-corrections-box").hidden = !corrections.length;
  $("value-corrections").replaceChildren(...corrections.map((c, i) => {
    const key = `correction:${i}`;
    return el("li", {},
      el("button", { class: "source-chip", type: "button", "data-key": key, "aria-pressed": "false",
        onclick: () => show([c.source], key, `Corrected ${valueLabel(c.field).toLowerCase()}`) }, c.source.wp),
      ` ${valueLabel(c.field)} ${readAs(c.read.text)}, corrected by reviewer to `,
      el("span", { class: "amount" }, grouped(c.value)),
      `: ${c.note} (${c.reviewer})`);
  }));
}

// An amount's sign is chosen separately as its direction, so the figure offered for editing has none.
function figureToEdit(current, isAmount) {
  if (current === null) return "";
  return grouped(isAmount ? current.replace("-", "") : current);
}

function amountDirection(item, current) {
  if (current === null) return item.column === "debit" ? "withdrawal" : "deposit";
  return current.startsWith("-") ? "withdrawal" : "deposit";
}

// The direction states the sign, whichever way the figure was typed.
function signedByDirection(typed, direction) {
  const parenthesized = typed.startsWith("(") && typed.endsWith(")");
  const unsigned = parenthesized ? typed.slice(1, -1) : typed.replace(/^-/, "").replace(/-$/, "");
  return (direction === "withdrawal" ? "-" : "") + unsigned;
}

async function correctValue(upload, item) {
  const isAmount = item.field.endsWith(":amount");
  const current = item.correction ? item.correction.value : item.value;
  const answer = await askNote(`Correct ${valueLabel(item.field).toLowerCase()} · ${item.wp}`, false, {
    read: item.text || "nothing", value: figureToEdit(current, isAmount), direction: isAmount ? amountDirection(item, current) : null,
  });
  if (!answer) return;
  const value = isAmount ? signedByDirection(answer.value, answer.direction) : answer.value;
  try {
    state = await api("/api/value", { upload, field: item.field, value, note: answer.note, reviewer: reviewer() || "reviewer" });
    render(); toast("Correction saved; the worksheet recomputed");
  } catch (e) { toast(e.message); }
}

async function removeCorrection(upload, item) {
  try {
    state = await api("/api/value/remove", { upload, field: item.field, reviewer: reviewer() || "reviewer" });
    render(); toast("Correction removed");
  } catch (e) { toast(e.message); }
}

function correctionButtons(upload, item) {
  if (state.review.signoff) return [];
  const what = `${valueLabel(item.field).toLowerCase()}, ${item.wp}`;
  const correct = el("button", { class: "link", type: "button", "aria-label": `Correct ${what}`,
    onclick: () => correctValue(upload, item) }, item.correction ? "Change" : "Correct");
  if (!item.correction) return [correct];
  return [correct, el("button", { class: "link", type: "button", "aria-label": `Remove correction of ${what}`,
    onclick: () => removeCorrection(upload, item) }, "Remove")];
}

function readingValue(upload, item) {
  const c = item.correction;
  let figure = "none read";
  if (c) figure = grouped(c.value);
  else if (item.value !== null) figure = grouped(item.value);
  return el("span", { class: "reading-value" },
    el("span", { class: "amount" }, figure),
    c ? [el("span", { class: "corrected" }, "corrected by reviewer"), el("span", { class: "read-as" }, readAs(item.text))] : null,
    correctionButtons(upload, item));
}

function labelledReading(upload, item) {
  return el("div", { class: "labelled-reading" }, el("span", { class: "reading-label" }, valueLabel(item.field)), readingValue(upload, item));
}

function lineReading(upload, page) {
  const row = page.rows.find((r) => r.ref === viewing.ref);
  if (row) {
    return [el("p", { class: "reading-line" }, `${row.date} · ${row.description}`),
      el("div", { class: "reading-balances" }, labelledReading(upload, row.amount), labelledReading(upload, row.balance))];
  }
  const balance = page.balances.find((b) => b.field === viewing.ref);
  return balance ? [el("div", { class: "reading-balances" }, labelledReading(upload, balance))] : [];
}

function renderReadings() {
  const page = viewing && state.page_readings[viewing.upload];
  $("readings").hidden = !page;
  if (!page) return;
  const upload = viewing.upload;
  const line = lineReading(upload, page);
  $("line-reading").hidden = !line.length;
  $("line-reading").replaceChildren(...line);
  $("page-balances").replaceChildren(...page.balances.map((b) => labelledReading(upload, b)));
  $("page-rows").replaceChildren(...page.rows.map((row, i) => el("tr", { class: row.ref === viewing.ref ? "traced-row" : "" },
    el("td", {}, `r${i + 1}`), el("td", {}, row.date), el("td", {}, row.description),
    el("td", { class: "num" }, readingValue(upload, row.amount)),
    el("td", { class: "num" }, readingValue(upload, row.balance)))));
}

// Form 8938 values are server-calculated Decimal strings. Never coerce them to Number.
function exactUsd(value, unknown) {
  if (value === null || value === undefined) return unknown;
  return "$" + grouped(value);
}

function form8938Sources(row, field, label) {
  const sources = row[field];
  return el("details", {}, el("summary", {}, `${label} (${sources.length})`),
    sources.length ? el("ul", { class: "evidence-list" }, ...sources.map((source, index) =>
      el("li", {}, el("button", { type: "button", class: "source", onclick: () =>
        show([source], `8938-${row.key}-${field}-${index}`, label) }, source.wp))))
      : el("p", {}, "No source line available. Review the missing evidence."));
}

const FILING_STATUS_TEXT = {
  single: "Single", mfj: "Married filing jointly", mfs: "Married filing separately",
  hoh: "Head of household", qss: "Qualifying surviving spouse",
};

function statedFact(value, missing) {
  if (value === null || value === undefined) return missing;
  if (value === true) return "Yes";
  if (value === false) return "No";
  return FILING_STATUS_TEXT[value] || String(value);
}

function factsTable(rows) {
  return el("div", { class: "table-wrap" }, el("table", { class: "form8938-facts" }, el("tbody", {},
    ...rows.map(([label, value]) => el("tr", {}, el("th", { scope: "row" }, label), el("td", {}, value))))));
}

function form8938Account(row, panel) {
  const labels = panel.labels;
  const displayName = row.number || row.uploads.join(", ") || row.key;
  const stated = panel.effective_accounts[row.key];
  const facts = [["Status", labels[row.inclusion] + (row.reason ? ` · ${row.reason}` : "")]];
  if (row.year_end) {
    facts.push(["Share counted", labels[row.share] + (row.share_note ? ` · ${row.share_note}` : "")]);
    for (const [field, label] of [["year_end", labels.year_end], ["maximum", labels.maximum_sources]]) {
      const range = row[field];
      facts.push([label, el("span", { class: "bound-values" }, `${exactUsd(range.display_low_usd, labels.unknown)} to ${exactUsd(range.display_high_usd, labels.unknown)}`)]);
    }
  }
  facts.push(["Exchange rate", row.rate.units_per_usd
    ? `${row.rate.units_per_usd} ${row.rate.currency || "units"}/USD · ${row.rate.date || "identity"} · ${row.rate.basis || "recorded"} · source: ${row.rate.source || "USD identity"}`
    : "Exchange rate unavailable. Review."]);
  facts.push(["Classification", OWNERSHIP[row.ownership] || String(row.ownership)]);
  facts.push(["Financial interest", statedFact(row.financial_interest, labels.missing_intake)]);
  facts.push(["Joint with spouse", stated ? statedFact(stated.joint_with_spouse, labels.missing_intake) : "No account intake record."]);
  const children = [factsTable(facts)];
  const corrections = [panel.corrections.ownership[row.key], panel.corrections.financial_interest[row.key]]
    .filter(Boolean).map((c) => `Correction: ${c.value ?? "unknown"}: ${c.note} (${c.reviewer})`);
  children.push(...corrections.map((note) => el("p", { class: "hint" }, note)));
  children.push(...row.reviews.map((note) => el("p", { class: "hint" }, note)));
  if (row.identified) {
    children.push(el("label", { class: "form8938-correction" }, "Financial interest correction", el("select", {
      "aria-label": `Financial interest of ${row.number}`, disabled: !!state.review.signoff,
      onchange: async (ev) => {
        const chosen = ev.target.value;
        if (!chosen) return;
        const note = await askNote(`Record source for financial interest of ${row.number}`);
        if (!note) { render(); return; }
        try { state = await api("/api/interest", { account: row.key, value: chosen === "unknown" ? null : chosen === "yes", note: note.note, reviewer: reviewer() || "reviewer" }); render(); }
        catch (e) { toast(e.message); render(); }
      }
    }, el("option", { value: "", selected: true }, "Select correction"),
    el("option", { value: "yes" }, "Financial interest stated"),
    el("option", { value: "no" }, "No financial interest stated"),
    el("option", { value: "unknown" }, "Financial interest unknown"))));
  }
  children.push(form8938Sources(row, "upload_sources", "Uploaded pages"),
    form8938Sources(row, "year_end_sources", labels.year_end_sources),
    form8938Sources(row, "maximum_sources", labels.maximum_sources));
  return el("details", { class: "form8938-account" },
    el("summary", {}, el("span", { class: "form8938-account-name" }, displayName),
      el("span", { class: "form8938-account-status" }, labels[row.inclusion])),
    el("div", { class: "form8938-account-body" }, ...children));
}

function renderForm8938() {
  const panel = state.worksheet.form8938;
  if (!panel) return;
  const labels = panel.labels;
  $("form8938-title").textContent = labels.title;
  const facts = Object.entries(panel.household_labels).map(([key, label]) =>
    [label, statedFact(panel.household[key], labels.missing_intake)]);
  const tests = [["year_end", labels.year_end], ["any_time", labels.any_time]].map(([key, label]) => {
    const test = panel[key];
    return el("tr", {}, el("th", { scope: "row" }, label),
      el("td", { class: "money" }, exactUsd(test.display_low_usd, labels.unknown)),
      el("td", { class: "money" }, exactUsd(test.display_high_usd, labels.unknown)),
      el("td", { class: "money" }, exactUsd(test.threshold_usd, labels.missing_intake)), el("td", {}, test.text));
  });
  const numeric = new Set([labels.lower, labels.upper, labels.threshold]);
  const notes = [labels.annual, labels.precision, labels.year_end_formula, labels.any_time_formula, ...panel.notes];
  $("form8938-content").replaceChildren(el("p", { class: "hint form8938-scope" }, labels.scope),
    el("h4", {}, "Stated at intake"),
    factsTable([...facts, ["Source", panel.intake_source || labels.missing_intake]]),
    el("p", { class: "hint" }, labels.intake),
    el("h4", {}, "Comparison"),
    el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {},
      ...["Test", labels.lower, labels.upper, labels.threshold, labels.review].map((text) =>
        el("th", { scope: "col", class: numeric.has(text) ? "money" : "" }, text)))),
    el("tbody", {}, ...tests))),
    el("details", { class: "form8938-notes" }, el("summary", {}, "How these bounds are calculated"),
      el("ul", {}, ...notes.map((note) => el("li", {}, note)))),
    el("h4", {}, "Accounts"),
    ...panel.accounts.map((row) => form8938Account(row, panel)));
}

function renderItems(signed) {
  const ws = state.worksheet;
  const open = state.open_review_flags;
  $("open-count").textContent = open.length ? `${open.length} open` : "all acknowledged";
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

function determinationText(d) {
  const parts = [d.label ? d.label + ": " + d.message : d.message];
  if (d.accounts?.length) parts.push("Accounts: " + d.accounts.join(", ") + ".");
  if (d.uploads?.length) parts.push("Pages: " + d.uploads.join(", ") + ".");
  if (d.scope) parts.push(d.scope);
  return parts.join(" ");
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
  const remaining = state.remaining_review_items;
  $("remaining-items").replaceChildren(...(remaining.length
    ? remaining.map((f) => el("li", {}, `${flagTitle(f)} · ${acctLabel(state.worksheet, f.account)}: ${f.message}`))
    : [el("li", {}, "No acknowledged review items remain on this worksheet.")]));
  const determinations = state.open_determinations;
  $("open-determinations").replaceChildren(...(determinations.length
    ? determinations.map((d) => el("li", {}, determinationText(d)))
    : [el("li", {}, "No open determinations are recorded in this worksheet.")]));
  renderSummary(state.review.summary);
}

const RELEASE_LABELS = { ko: "Korean", en: "English", both: "Korean and English" };
const chosenRelease = () => document.querySelector('input[name="release"]:checked').value;

function renderSummary(s) {
  $("summary-box").hidden = !s;
  $("draft-summary").hidden = Boolean(s?.approved);
  if (!s) return;
  $("summary-lines").replaceChildren(...s.lines.map((line) => el("tr", {},
    el("td", { lang: "en" }, line.en), el("td", { lang: s.language, class: "ko" }, line[s.language] ?? "n/a"))));
  $("summary-blocked").hidden = !s.blocked;
  $("summary-blocked").textContent = s.blocked ? `Korean held back: the translation ${s.blocked}. Draft it again; English can still be released.` : "";
  document.querySelectorAll('input[name="release"]').forEach((input) => {
    input.disabled = Boolean(s.blocked) && input.value !== "en";
    if (s.blocked) input.checked = input.value === "en";
  });
  $("summary-release").hidden = Boolean(s.approved);
  $("approve-summary").hidden = Boolean(s.approved);
  $("summary-approved").hidden = !s.approved;
  if (s.approved) $("summary-approved").textContent = `Approved for release by ${s.approved.reviewer}, ${s.approved.ts}. The client gets: ${RELEASE_LABELS[s.approved.release]}.`;
  renderSummaryConfirmation(s);
}

// An English-only note has nothing to confirm; Korean or both show the phrases not yet reviewed.
function renderSummaryConfirmation(s) {
  const fresh = chosenRelease() === "en" ? [] : Object.entries(s.unreviewed || {});
  $("summary-unreviewed").hidden = !fresh.length || Boolean(s.blocked || s.approved);
  $("summary-unreviewed-list").replaceChildren(...fresh.map(([k, v]) => el("li", {},
    el("span", { lang: s.language, class: "ko" }, v), el("span", { class: "sub" }, ` (${s.unreviewed_en[k]})`))));
  const count = plural(fresh.length, "unreviewed phrase");
  $("approve-summary").textContent = fresh.length ? `Approve, including ${count}` : "Approve for release";
}

function auditLine(e) {
  const note = e.note ? `: ${e.note}` : "";
  const change = e.after ? ` (${e.before ?? "nothing read"} → ${e.after})` : "";
  const where = e.field ? ` ${e.upload} ${e.field}` : "";
  return `${e.ts}  ${e.reviewer}  ${e.action}${where}${note}${change}`;
}

function renderAudit() {
  $("audit").replaceChildren(...state.audit.slice().reverse().map((e) => el("li", {}, auditLine(e))));
}

function acctLabel(ws, key) {
  const a = ws.accounts.find((x) => x.key === key);
  return a ? `${(a.institutions[a.institutions.length - 1] || "Bank name not available")} · ${a.account_number}` : key;
}

function highestBalanceFigure(a) {
  const m = a.max;
  if (!m) return el("span", { class: "unknown" }, a.identified ? "could not be established from the documents read" : "not counted");
  const key = `max:${a.key}`;
  return el("button", { class: "traced", "data-key": key, "aria-pressed": "false",
      onclick: () => show([m.source], key, `Highest observed balance, ${a.account_number}`) },
    el("span", { class: "figure" }, el("span", { class: "amount" }, money(m.amount, a.currency)), el("span", { class: "ccy" }, a.currency)),
    el("span", { class: "wp" }, m.source.wp),
    m.kind === "computed" ? el("span", { class: "kind" }, "computed from lines") : null,
    m.correction ? el("span", { class: "corrected" }, `corrected by reviewer · ${readAs(m.correction.read.text)}`) : null);
}

// Corrections on this account other than the one the figure itself shows.
function otherCorrections(a) {
  const direct = Boolean(a.max?.correction);
  const others = state.worksheet.value_corrections.filter((c) => c.account === a.key).length - (direct ? 1 : 0);
  if (others < 1) return null;
  const word = direct ? "other reading" : "reading";
  return el("span", { class: "corrected" }, `${plural(others, word)} on this account corrected by reviewer`);
}

function accountRow(a, signed) {
  const corrected = a.key in state.review.overrides;
  const select = el("select", { "aria-label": `Ownership of ${a.account_number}`, disabled: signed || !a.identified,
    onchange: async (ev) => {
      const next = ev.target.value;
      const note = await askNote(`Change ownership of ${a.account_number} to “${OWNERSHIP_LONG[next]}”`);
      if (!note) { ev.target.value = a.ownership; return; }
      try { state = await api("/api/ownership", { account: a.key, ownership: next, note: note.note, reviewer: reviewer() || "reviewer" }); render(); toast("Ownership corrected"); }
      catch (e) { toast(e.message); ev.target.value = a.ownership; }
    } },
    ...Object.entries(OWNERSHIP).filter(([k]) => k !== "unknown" || a.ownership === "unknown")
      .map(([k, v]) => el("option", { value: k, selected: k === a.ownership }, v)));
  const banks = a.institutions;
  return el("tr", { class: a.identified ? "" : "unidentified" },
    el("td", { class: "bank" }, banks.length ? banks[banks.length - 1] : "Bank name not available",
      banks.length > 1 ? el("span", { class: "sub" }, `also printed as ${banks.slice(0, -1).join(", ")}`) : null,
      a.holders[0] ? el("span", { class: "sub holder", title: a.holders.join("; ") }, a.holders[0]) : null),
    el("td", {}, el("span", { class: "acct" }, a.account_number), currencyControl(a, signed)),
    el("td", { class: "ownership" }, select, corrected ? el("span", { class: "corrected" }, "corrected by reviewer") : null),
    el("td", { class: "num" }, highestBalanceFigure(a), otherCorrections(a)),
    el("td", { class: "num usd" }, el("span", { class: "amount" }, usd(a.max_usd)),
      a.rate && a.max_usd !== null ? el("span", { class: "rate" }, `@ ${a.rate} ${a.currency}/USD`) : null,
      a.rate_basis === "cpa" ? el("span", { class: "sub" }, `Preparer-recorded, ${a.rate_date}: ${a.rate_source}`) : null),
  );
}

function currencyControl(a, signed) {
  if (!a.identified || !a.observed_currencies.length) return null;
  if (a.observed_currencies.length < 2 && !a.currency_review_needed && !a.currency_correction) return null;
  return el("label", { class: "sub" }, "Currency correction (applies to all statements)",
    el("select", { "aria-label": `Currency of ${a.account_number}`, disabled: signed,
      onchange: async (ev) => {
        const currency = ev.target.value;
        if (!currency) return;
        const note = await askNote(`Record source confirming all balances for ${a.account_number} are in ${currency}`);
        if (!note) { render(); return; }
        try { state = await api("/api/currency", { account: a.key, currency, note: note.note, reviewer: reviewer() || "reviewer" }); render(); }
        catch (e) { toast(e.message); render(); }
      } }, el("option", { value: "", selected: !a.currency_correction }, "Select currency"),
      ...a.observed_currencies.map((c) => el("option", { value: c, selected: a.currency_correction?.currency === c }, c))),
    a.currency_correction ? a.currency_correction.note : "Balances withheld pending review");
}

function chips(f) {
  return el("span", { class: "sources" }, ...f.sources.filter((s) => s.upload).map((s, i) => {
    const key = `flag:${f.id}:${i}`;
    return el("button", { class: "source-chip", "data-key": key, "aria-pressed": "false",
      onclick: () => show(f.sources.slice(i).concat(f.sources.slice(0, i)), key, flagTitle(f)) }, s.wp);
  }));
}

function classificationContext(panel, account) {
  const stated = panel.stated_accounts[account];
  const intake = stated ? `Stated ownership: ${stated.ownership}; financial interest: ${stated.financial_interest ?? "not stated"}. ` : "No account intake record. ";
  const changes = [panel.corrections.ownership[account], panel.corrections.financial_interest[account]]
    .filter(Boolean).map((c) => `Correction: ${c.value ?? "unknown"}: ${c.note} (${c.reviewer}). `);
  return el("p", { class: "hint" }, `Intake source: ${panel.intake_source || "not stated"}. `, intake, ...changes);
}

function itemRow(f, ack, signed, ws) {
  const title = flagTitle(f);
  const actions = ack
    ? (signed ? null : el("button", { class: "link", onclick: async () => { state = await api("/api/reopen", { flag: f.id, reviewer: reviewer() || "reviewer" }); render(); } }, "Reopen"))
    : el("button", { class: "ack", disabled: signed, onclick: async () => {
        const note = await askNote(`Acknowledge: ${title}`, f.code === "currency_unknown");
        if (!note) return;
        try { state = await api("/api/ack", { flag: f.id, ...note, reviewer: reviewer() || "reviewer" }); render(); toast("Acknowledged"); }
        catch (e) { toast(e.message); }
      } }, "Acknowledge");
  return el("li", { class: `item${ack ? " done" : ""}` },
    el("div", {},
      el("p", { class: "item-title" }, title),
      el("p", { class: "item-acct" }, acctLabel(ws, f.account)),
      el("p", { class: "item-msg" }, f.message),
      el("p", { class: "item-do" }, f.cpa_action),
      ws.form8938 && ["ownership_interest_conflict", "authority_interest_review"].includes(f.code)
        ? classificationContext(ws.form8938, f.account) : null,
      chips(f)),
    el("div", { class: "item-actions" }, actions),
    ack ? el("p", { class: "ack-note" }, `${ack.note} (${ack.reviewer})`) : null);
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
document.querySelectorAll('input[name="release"]').forEach((input) => input.addEventListener("change", () => {
  if (state.review.summary) renderSummaryConfirmation(state.review.summary);
}));
$("approve-summary").addEventListener("click", async () => {
  const release = chosenRelease();
  try { state = await api("/api/summary/approve", { reviewer: reviewer() || state.review.signoff.reviewer, release,
    confirm_unreviewed: release !== "en" && Object.keys(state.review.summary?.unreviewed || {}).length > 0 }); render(); toast("Approved for release"); }
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
