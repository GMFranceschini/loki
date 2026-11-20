/* Loki front end. Plain ES modules-free JS: no build step, no dependencies. */

"use strict";

const state = {
  window: "day",
  reference: null,
  limit: 10,
  digests: {},
  topic: "",
  source: "",
  polling: null,
};

const $ = (sel) => document.querySelector(sel);

const el = {
  list: $("#paper-list"),
  empty: $("#empty-state"),
  summary: $("#window-summary"),
  stats: $("#stats"),
  banner: $("#banner"),
  date: $("#reference-date"),
  limit: $("#limit"),
  fetchBtn: $("#fetch-btn"),
  themeBtn: $("#theme-btn"),
  topicFilter: $("#topic-filter"),
  sourceFilter: $("#source-filter"),
  topicsBtn: $("#topics-btn"),
  dialog: $("#topics-dialog"),
  editor: $("#topics-editor"),
  topicsError: $("#topics-error"),
  topicsPath: $("#topics-path"),
  topicAdd: $("#topic-add"),
  topicsSave: $("#topics-save"),
  topicsCancel: $("#topics-cancel"),
  topicsClose: $("#topics-close"),
  rowTemplate: $("#topic-row-template"),
};

/* ------------------------------------------------------------------ utils */

function todayISO() {
  // Local date, so "today" matches the user's calendar rather than UTC.
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function safeURL(value) {
  // Only ever emit an http(s) link. Paper URLs are built by Loki itself, so
  // this is belt and braces, but a "javascript:" href would run on click and
  // the cost of refusing one here is nothing.
  try {
    const parsed = new URL(String(value ?? ""), window.location.origin);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
  } catch {
    return "";
  }
}

function showBanner(message, isError = false) {
  el.banner.textContent = message;
  el.banner.classList.toggle("error", isError);
  el.banner.hidden = !message;
}

function formatAuthors(authors) {
  if (!Array.isArray(authors) || authors.length === 0) return "";
  if (authors.length <= 3) return authors.join(", ");
  return `${authors[0]}, ${authors[1]} … ${authors[authors.length - 1]} (+${authors.length - 3} more)`;
}

function formatDate(iso) {
  if (!iso) return "";
  const parsed = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/* ------------------------------------------------------------------ render */

function renderPaper(paper, rank) {
  const li = document.createElement("li");
  li.className = "paper";

  const isPreprint = Boolean(paper.is_preprint);
  const venue = isPreprint
    ? `${paper.source} preprint`
    : paper.journal || "Unknown journal";

  const badges = [];
  if (isPreprint) {
    badges.push('<span class="badge preprint">preprint</span>');
  } else if (paper.impact_factor) {
    badges.push(`<span class="badge if">IF ${Number(paper.impact_factor).toFixed(1)}</span>`);
  }
  const tags = Array.isArray(paper.tags) && paper.tags.length
    ? paper.tags
    : (paper.top_topic ? [paper.top_topic] : []);
  tags.forEach((tag) => {
    badges.push(`<span class="badge topic">${escapeHTML(tag)}</span>`);
  });
  if (tags.length === 0) {
    // Saying nothing matched is more honest than showing the least-bad topic.
    badges.push('<span class="badge untagged" title="Ranked, but no topic matched confidently">no clear topic</span>');
  }

  const rel = Math.round((paper.relevance ?? 0) * 100);
  const imp = Math.round((paper.impact ?? 0) * 100);
  const abstract = paper.abstract || "";
  const summary = paper.summary || "";
  const expandable = Boolean(summary || abstract);

  li.innerHTML = `
    <div class="rank">${rank}</div>
    <div class="paper-body">
      <h2 class="paper-title">
        <a href="${escapeHTML(safeURL(paper.url))}" target="_blank" rel="noopener noreferrer">${escapeHTML(paper.title)}</a>
      </h2>
      <div class="paper-meta">
        <span class="venue">${escapeHTML(venue)}</span>
        <span class="dot">·</span>
        <span>${escapeHTML(formatDate(paper.published_on))}</span>
        ${badges.join("")}
      </div>
      ${paper.authors && paper.authors.length ? `<p class="authors">${escapeHTML(formatAuthors(paper.authors))}</p>` : ""}
      <div class="scorebar">
        <span class="score-total">${(paper.score ?? 0).toFixed(3)}</span>
        <div class="bars">
          <div class="bar-group">
            <div class="bar-label"><span>relevance</span><span>${rel}</span></div>
            <div class="bar-track"><div class="bar-fill rel" style="width:${rel}%"></div></div>
          </div>
          <div class="bar-group">
            <div class="bar-label"><span>impact</span><span>${imp}</span></div>
            <div class="bar-track"><div class="bar-fill imp" style="width:${imp}%"></div></div>
          </div>
        </div>
        ${expandable ? '<button class="abstract-toggle" type="button" aria-expanded="false">Summary</button>' : ""}
      </div>
    </div>
  `;

  if (expandable) {
    const toggle = li.querySelector(".abstract-toggle");
    const body = li.querySelector(".paper-body");
    let open = false;

    const close = () => {
      li.querySelector(".expanded")?.remove();
      toggle.textContent = "Summary";
      toggle.setAttribute("aria-expanded", "false");
    };

    const openPanel = () => {
      const panel = document.createElement("div");
      panel.className = "expanded";

      // The compact summary is what the card shows first. The full abstract
      // stays one click further away, so the card does not become a wall.
      const lead = document.createElement("p");
      lead.className = "summary";
      lead.textContent = summary || abstract;
      panel.appendChild(lead);

      if (abstract && summary && abstract !== summary) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "abstract-more";
        more.textContent = "Show full abstract";
        let full = null;
        more.addEventListener("click", () => {
          if (full) {
            full.remove();
            full = null;
            more.textContent = "Show full abstract";
            return;
          }
          full = document.createElement("p");
          full.className = "abstract";
          full.textContent = abstract;
          panel.appendChild(full);
          more.textContent = "Hide full abstract";
        });
        panel.appendChild(more);
      }

      body.appendChild(panel);
      toggle.textContent = "Hide";
      toggle.setAttribute("aria-expanded", "true");
    };

    toggle.addEventListener("click", () => {
      open = !open;
      open ? openPanel() : close();
    });
  }
  return li;
}

function paperTags(paper) {
  if (Array.isArray(paper.tags) && paper.tags.length) return paper.tags;
  return paper.top_topic ? [paper.top_topic] : [];
}

function applyFilters(papers) {
  return papers.filter((paper) => {
    if (state.topic === "__untagged__") {
      if (paperTags(paper).length) return false;
    } else if (state.topic && !paperTags(paper).includes(state.topic)) {
      return false;
    }
    if (state.source === "journal" && paper.is_preprint) return false;
    if (state.source === "preprint" && !paper.is_preprint) return false;
    return true;
  });
}

function render() {
  const digest = state.digests[state.window];
  el.list.innerHTML = "";

  if (!digest) {
    el.summary.textContent = "Loading…";
    return;
  }

  const papers = applyFilters(digest.papers || []);
  const pad = digest.padding_days || 0;
  const base = { day: "day", week: "week", month: "month" }[state.window];
  const padNote = pad ? ` (${base} ±${pad}d)` : ` (${base})`;
  el.summary.textContent =
    `${digest.from} → ${digest.to}${padNote} · ` +
    `${digest.considered} papers cleared the relevance threshold · showing ${papers.length}`;

  if (papers.length === 0) {
    el.empty.hidden = false;
    el.empty.innerHTML = digest.considered
      ? "No papers match the current filters."
      : "Nothing stored for this window yet. Run <code>bin/loki fetch --backfill 7</code> " +
        "in the project folder, or press <strong>Fetch now</strong>.";
    return;
  }

  el.empty.hidden = true;
  const frag = document.createDocumentFragment();
  papers.forEach((paper, i) => frag.appendChild(renderPaper(paper, i + 1)));
  el.list.appendChild(frag);
}

function renderStats(stats, weights) {
  if (!stats) return;
  const bySource = Object.entries(stats.by_source || {})
    .map(([source, count]) => `${source} ${count}`)
    .join(" · ");
  const parts = [
    `<span><strong>${stats.total_papers}</strong> papers stored</span>`,
    bySource ? `<span>${escapeHTML(bySource)}</span>` : "",
    stats.earliest ? `<span>covering ${stats.earliest} → ${stats.latest}</span>` : "",
  ];
  if (weights) {
    parts.push(
      `<span>weights: relevance ${weights.relevance} / impact ${weights.impact}</span>`
    );
  }
  const run = stats.last_run;
  if (run) {
    const when = (run.finished_at || run.started_at || "").replace("T", " ").replace("+00:00", " UTC");
    parts.push(`<span>last fetch: ${escapeHTML(when)} (${escapeHTML(run.status)})</span>`);
  }
  el.stats.innerHTML = parts.filter(Boolean).join("");
}

function populateTopics(topics) {
  if (!Array.isArray(topics)) return;
  const current = el.topicFilter.value;
  el.topicFilter.innerHTML = '<option value="">All topics</option>';
  topics.forEach((topic) => {
    const option = document.createElement("option");
    option.value = topic;
    option.textContent = topic;
    el.topicFilter.appendChild(option);
  });
  const untagged = document.createElement("option");
  untagged.value = "__untagged__";
  untagged.textContent = "No clear topic";
  el.topicFilter.appendChild(untagged);
  el.topicFilter.value = current;
}

/* ------------------------------------------------------------------ data */

async function loadDigests() {
  const params = new URLSearchParams({ date: state.reference, limit: String(state.limit) });
  try {
    const resp = await fetch(`/api/digests?${params}`);
    if (!resp.ok) throw new Error(`server returned ${resp.status}`);
    const payload = await resp.json();
    state.digests = payload.digests || {};
    populateTopics(payload.topics);
    renderStats(payload.stats, payload.weights);
    render();
  } catch (err) {
    showBanner(`Could not load the digest: ${err.message}`, true);
    el.summary.textContent = "Unavailable.";
  }
}

async function pollFetchLog() {
  try {
    const resp = await fetch("/api/fetch/log");
    const payload = await resp.json();
    const last = (payload.log || []).slice(-1)[0];
    if (last) showBanner(`Fetching… ${last}`);
    if (!payload.running) stopPolling();
  } catch {
    /* The fetch request itself reports failures. */
  }
}

function stopPolling() {
  if (state.polling) {
    clearInterval(state.polling);
    state.polling = null;
  }
}

async function triggerFetch() {
  el.fetchBtn.disabled = true;
  showBanner("Starting a fetch…");
  state.polling = setInterval(pollFetchLog, 1200);

  try {
    const resp = await fetch("/api/fetch?backfill=2", { method: "POST" });
    const payload = await resp.json();
    stopPolling();

    if (resp.status === 409) {
      showBanner("A fetch is already running.");
    } else if (!resp.ok || payload.status === "error") {
      showBanner(`Fetch failed: ${payload.detail || (payload.errors || []).join("; ")}`, true);
    } else {
      const bits = [
        `${payload.fetched} papers fetched`,
        `${payload.inserted} new`,
        `${payload.updated} updated`,
      ];
      if (payload.rescored) bits.push(`${payload.rescored} rescored`);
      showBanner(`Done: ${bits.join(", ")}.`);
      await loadDigests();
      setTimeout(() => showBanner(""), 9000);
    }
  } catch (err) {
    stopPolling();
    showBanner(`Fetch failed: ${err.message}`, true);
  } finally {
    el.fetchBtn.disabled = false;
  }
}

/* ------------------------------------------------------------- topic editor */

function addTopicRow(topic = {}) {
  const row = el.rowTemplate.content.firstElementChild.cloneNode(true);
  row.querySelector(".topic-name").value = topic.name || "";
  row.querySelector(".topic-weight").value = topic.weight ?? 1;
  row.querySelector(".topic-prose").value = topic.prose || "";
  row.querySelector(".topic-pubmed").value = topic.pubmed || "";
  row.querySelector(".topic-remove").addEventListener("click", () => {
    if (el.editor.children.length === 1) {
      showTopicsError("Keep at least one topic.");
      return;
    }
    row.remove();
  });
  el.editor.appendChild(row);
  return row;
}

function showTopicsError(message) {
  el.topicsError.textContent = message || "";
  el.topicsError.hidden = !message;
  if (message) el.topicsError.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function collectTopics() {
  return Array.from(el.editor.querySelectorAll(".topic-row")).map((row) => ({
    name: row.querySelector(".topic-name").value.trim(),
    weight: Number(row.querySelector(".topic-weight").value) || 1,
    prose: row.querySelector(".topic-prose").value.trim(),
    pubmed: row.querySelector(".topic-pubmed").value.trim(),
  }));
}

async function openTopics() {
  showTopicsError("");
  el.editor.innerHTML = "";
  try {
    const resp = await fetch("/api/topics");
    if (!resp.ok) throw new Error(`server returned ${resp.status}`);
    const payload = await resp.json();
    (payload.topics || []).forEach(addTopicRow);
    if (!payload.topics || payload.topics.length === 0) addTopicRow();
    el.topicsPath.textContent = payload.path || "";
    el.topicsPath.title = payload.path || "";
  } catch (err) {
    addTopicRow();
    showTopicsError(`Could not load the current topics: ${err.message}`);
  }
  el.dialog.showModal();
}

async function saveTopics() {
  const topics = collectTopics();
  showTopicsError("");
  el.topicsSave.disabled = true;
  el.topicsSave.textContent = "Saving…";

  try {
    const resp = await fetch("/api/topics", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topics }),
    });
    const payload = await resp.json();

    if (!resp.ok) {
      // FastAPI puts validation messages in `detail`.
      showTopicsError(payload.detail || `Save failed (${resp.status}).`);
      return;
    }

    el.dialog.close();
    showBanner(payload.detail || "Topics saved.");
    await loadDigests();
    setTimeout(() => showBanner(""), 11000);
  } catch (err) {
    showTopicsError(`Save failed: ${err.message}`);
  } finally {
    el.topicsSave.disabled = false;
    el.topicsSave.textContent = "Save & rescore";
  }
}

/* ------------------------------------------------------------------ theme */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("loki-theme", theme);
  } catch {
    /* Private browsing blocks storage; the theme just will not persist. */
  }
}

function initTheme() {
  let saved = "auto";
  try {
    saved = localStorage.getItem("loki-theme") || "auto";
  } catch {
    /* ignore */
  }
  applyTheme(saved);
  el.themeBtn.addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(document.documentElement.dataset.theme) + 1) % 3];
    applyTheme(next);
    el.themeBtn.title = `Theme: ${next}`;
  });
}

/* ------------------------------------------------------------------ init */

function init() {
  state.reference = todayISO();
  el.date.value = state.reference;
  el.date.max = state.reference;

  initTheme();

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      state.window = tab.dataset.window;
      render();
    });
  });

  el.date.addEventListener("change", () => {
    state.reference = el.date.value || todayISO();
    loadDigests();
  });
  el.limit.addEventListener("change", () => {
    state.limit = Number(el.limit.value) || 10;
    loadDigests();
  });
  el.topicFilter.addEventListener("change", () => {
    state.topic = el.topicFilter.value;
    render();
  });
  el.sourceFilter.addEventListener("change", () => {
    state.source = el.sourceFilter.value;
    render();
  });
  el.fetchBtn.addEventListener("click", triggerFetch);

  el.topicsBtn.addEventListener("click", openTopics);
  el.topicAdd.addEventListener("click", () => {
    showTopicsError("");
    const row = addTopicRow();
    row.querySelector(".topic-name").focus();
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  el.topicsSave.addEventListener("click", saveTopics);
  el.topicsCancel.addEventListener("click", () => el.dialog.close());
  el.topicsClose.addEventListener("click", () => el.dialog.close());

  loadDigests();

  // Deep link: /#topics opens the topic editor straight away.
  if (location.hash === "#topics") openTopics();
}

document.addEventListener("DOMContentLoaded", init);
