const state = { space: null, cursor: null, mode: "memory", snapshotCursor: null, auditCursor: null };
const $ = (selector) => document.querySelector(selector);
const emptyPage = { items: [], next_cursor: null };

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(value.detail || `HTTP ${response.status}`);
  }
  return (await response.json()).result;
}
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]); }
function display(value) { return value === null || value === undefined || value === "" ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value); }
function episodeQuery() { const query = new URLSearchParams(new FormData($("#filters"))); query.set("memory_space_id", state.space); query.set("limit", "50"); return query; }

async function showEpisode(id) {
  const data = await api(`/v1/admin/episodes/${encodeURIComponent(id)}`); const episode = data.episode;
  $("#detail").classList.add("open");
  $("#detail-content").innerHTML = `<h2>${escapeHtml(episode.episode_type)}</h2><div class="actions"><button id="copy">复制完整 JSON</button>${episode.source_reference?'<button id="source">加载权威原文</button>':""}</div><div id="source-result"></div><pre>${escapeHtml(display(episode.body||episode.source_reference))}</pre><dl>${Object.entries(episode).filter(([key])=>key!=="body").map(([key,value])=>`<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(display(value))}</dd>`).join("")}</dl><div class="relations"><h2>派生内容（非权威）</h2><pre>${escapeHtml(display(data.derivation))}</pre><h2>纠正与结果关系</h2>${data.relations.map((item)=>`<button class="episode relation" data-id="${escapeHtml(item.episode_id)}">#${item.sequence} · ${escapeHtml(item.episode_type)}</button>`).join("")||'<p class="empty">无关联记忆</p>'}</div>`;
  $("#copy").onclick = () => navigator.clipboard.writeText(JSON.stringify(episode, null, 2));
  document.querySelectorAll(".relation").forEach((button) => { button.onclick = () => showEpisode(button.dataset.id); });
  if ($("#source")) $("#source").onclick = async () => {
    $("#source-result").innerHTML = '<p class="empty">正在从权威源取回…</p>';
    try { const source = await api(`/v1/admin/episodes/${encodeURIComponent(id)}/source`); $("#source-result").innerHTML = `<h2>按需取回的权威原文</h2><pre>${escapeHtml(source.body)}</pre>`; }
    catch (error) { $("#source-result").innerHTML = `<p class="source-error">取回失败：${escapeHtml(error.message)}。可重试，既有记忆未受影响。</p>`; }
  };
}

function appendEpisode(item) {
  const button = document.createElement("button"); button.className = "episode";
  button.innerHTML = `<strong>#${item.sequence} · ${escapeHtml(item.episode_type)}</strong><p>${escapeHtml(display(item.body||item.source_reference))}</p><small>${escapeHtml(item.source_system)} · ${escapeHtml(item.known_at)}</small>`;
  button.onclick = () => showEpisode(item.episode_id); $("#episodes").append(button);
}
async function loadEpisodes(reset = true) {
  state.mode = "memory"; if (reset) { state.cursor = null; $("#episodes").innerHTML = '<p class="empty">加载中…</p>'; }
  const query = episodeQuery(); if (state.cursor) query.set("cursor", state.cursor);
  $("#download").href = `/v1/admin/episodes/export?${episodeQuery()}`;
  const page = await api(`/v1/admin/episodes?${query}`); if (reset) $("#episodes").innerHTML = "";
  page.items.forEach(appendEpisode); state.cursor = page.next_cursor; $("#more").hidden = !state.cursor;
  if (!$("#episodes").children.length) $("#episodes").innerHTML = '<p class="empty">没有符合条件的记忆</p>';
}
async function showTimeline() { const items = await api(`/v1/admin/timeline?memory_space_id=${encodeURIComponent(state.space)}`); $("#episodes").innerHTML = ""; items.forEach(appendEpisode); $("#more").hidden = true; }

function appendSnapshot(item) {
  const button = document.createElement("button"); button.className = "episode";
  button.innerHTML = `<strong>Snapshot · ${escapeHtml(item.stage)}</strong><p>watermark ${item.watermark} · ${escapeHtml(item.as_of)}</p><small>${escapeHtml(item.policy_version)} · ${escapeHtml(item.protocol_version)}</small>`;
  button.onclick = () => { $("#detail").classList.add("open"); $("#detail-content").innerHTML = `<h2>冻结快照</h2><pre>${escapeHtml(display(item))}</pre>`; }; $("#episodes").append(button);
}
function appendAudit(item) {
  const button = document.createElement("button"); button.className = "episode";
  button.innerHTML = `<strong>Retrieval · ${escapeHtml(item.query)}</strong><p>${item.final_episode_ids.length} 条命中 · ${escapeHtml(item.versions.retriever)}</p><small>${escapeHtml(item.audit_id)}</small>`;
  button.onclick = async () => { const bundle = await api(`/v1/admin/retrieval-bundles/${encodeURIComponent(item.bundle_id)}`); $("#detail").classList.add("open"); $("#detail-content").innerHTML = `<h2>检索审计</h2><pre>${escapeHtml(display(item))}</pre><h2>MemoryBundle</h2><pre>${escapeHtml(display(bundle))}</pre>`; }; $("#episodes").append(button);
}
async function showAudit(reset = true) {
  if (!state.space) return; state.mode = "audit"; $("#audit-tab").classList.add("active"); $("#memory-tab").classList.remove("active"); $("#filters").hidden = true; $("#space-title").textContent = `${state.space} · 检索审计`;
  if (reset) { state.snapshotCursor = null; state.auditCursor = null; $("#episodes").innerHTML = '<p class="empty">加载审计…</p>'; }
  const suffix = (cursor) => cursor && cursor !== "done" ? `&cursor=${encodeURIComponent(cursor)}` : "";
  const [snapshots, audits] = await Promise.all([
    state.snapshotCursor === "done" ? emptyPage : api(`/v1/admin/snapshots?memory_space_id=${encodeURIComponent(state.space)}&limit=50${suffix(state.snapshotCursor)}`),
    state.auditCursor === "done" ? emptyPage : api(`/v1/admin/retrieval-audits?memory_space_id=${encodeURIComponent(state.space)}&limit=50${suffix(state.auditCursor)}`),
  ]);
  if (reset) $("#episodes").innerHTML = ""; snapshots.items.forEach(appendSnapshot); audits.items.forEach(appendAudit);
  state.snapshotCursor = snapshots.next_cursor || "done"; state.auditCursor = audits.next_cursor || "done";
  if (!$("#episodes").children.length) $("#episodes").innerHTML = '<p class="empty">暂无检索审计</p>';
  $("#more").hidden = state.snapshotCursor === "done" && state.auditCursor === "done";
}
function showMemory() { $("#memory-tab").classList.add("active"); $("#audit-tab").classList.remove("active"); $("#filters").hidden = false; $("#space-title").textContent = state.space || "选择记忆空间"; loadEpisodes(); }

async function boot() {
  try { const spaces = await api("/v1/admin/memory-spaces"); $("#health").textContent = "只读 · 已连接";
    spaces.forEach((space) => { const button = document.createElement("button"); button.textContent = `${space.memory_space_id}  ${space.episode_count}`; button.onclick = async () => { document.querySelectorAll("nav button").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.space = space.memory_space_id; $("#space-title").textContent = state.space; $("#count").textContent = `${space.episode_count} 条`; state.mode === "audit" ? await showAudit() : await loadEpisodes(); }; $("#spaces").append(button); });
    if (spaces.length) $("#spaces button").click(); else $("#spaces").innerHTML = '<p class="empty">尚无记忆空间</p>';
  } catch (error) { $("#health").textContent = "连接失败"; $("#episodes").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`; }
}
$("#filters").onsubmit = (event) => { event.preventDefault(); loadEpisodes(); }; $("#timeline").onclick = showTimeline; $("#more").onclick = () => state.mode === "audit" ? showAudit(false) : loadEpisodes(false); $("#audit-tab").onclick = () => showAudit(); $("#memory-tab").onclick = showMemory; $("#close-detail").onclick = () => $("#detail").classList.remove("open"); boot();
