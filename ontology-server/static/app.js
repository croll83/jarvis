/* ── Ontology Dashboard — app.js ───────────────────────────────── */
"use strict";

const API = location.origin;

/* ── State ─────────────────────────────────────────────────────── */
let schema = null;
let entityTypes = [];
let relationDefs = {};
let allEntitiesCache = [];
let entities = [];
let currentOffset = 0;
const PAGE_SIZE = 200;
let editingEntity = null;
let speakers = [];

/* ── Default values per type.field ─────────────────────────────── */
const FIELD_DEFAULTS = {
  "Person.timezone": "Europe/Rome",
  "Person.visibility": "family",
  "Person.type_enum": "Human",
  "Person.role_enum": "user",
  "Event.timezone": "Europe/Rome",
  "Location.timezone": "Europe/Rome",
  "Location.country": "IT",
};

/* ── DOM refs ──────────────────────────────────────────────────── */
const $ = (s) => document.querySelector(s);
const speakerSel = $("#speaker-select");
const typeFilter = $("#type-filter");
const entityList = $("#entity-list");
const loadMoreBtn = $("#btn-load-more");
const detailPanel = $("#detail-panel");
const entityModal = $("#entity-modal");
const relationModal = $("#relation-modal");
const toastEl = $("#toast");

/* ── Helpers ───────────────────────────────────────────────────── */
function getSpeaker() { return speakerSel.value; }
function getToken() { return localStorage.getItem("onto_token") || ""; }

function headers(json = false) {
  const h = { "X-Speaker-Id": getSpeaker() };
  const t = getToken();
  if (t) h["Authorization"] = "Bearer " + t;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function toast(msg, type = "") {
  toastEl.textContent = msg;
  toastEl.className = "toast " + type;
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => toastEl.classList.add("hidden"), 2500);
}

function entityLabel(e) {
  const p = e.properties || e;
  // Preference: show "subject: value" for clarity
  if ((e.type === "Preference" || p.subject) && p.value) {
    return p.subject + ": " + p.value;
  }
  return p.name || p.title || p.subject || p.content?.slice(0, 40) || p.service || p.scope || e.id || "";
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function refreshEntitiesCache() {
  try {
    allEntitiesCache = await api("/entities?limit=500", { headers: headers() });
  } catch (_) { /* silent */ }
}

/* ── Init ──────────────────────────────────────────────────────── */
async function init() {
  if (!getToken()) {
    const t = prompt("API Token (leave empty if none):");
    if (t) localStorage.setItem("onto_token", t);
  }

  try {
    const [schemaData, speakersData] = await Promise.all([
      api("/schema"),
      api("/speakers"),
    ]);
    schema = schemaData;
    speakers = speakersData;
    entityTypes = Object.keys(schema.types || {});
    relationDefs = schema.relations || {};
  } catch (e) {
    toast("Failed to load: " + e.message, "error");
    return;
  }

  // Speaker selector — put admins first so default speaker has full visibility
  speakerSel.innerHTML = "";
  const seenSpeakers = new Set();
  const sorted = [...speakers].sort((a, b) => {
    if (a.role === "admin" && b.role !== "admin") return -1;
    if (b.role === "admin" && a.role !== "admin") return 1;
    if (a.type === "Agent" && b.type !== "Agent") return -1;
    if (b.type === "Agent" && a.type !== "Agent") return 1;
    return 0;
  });
  sorted.forEach(sp => {
    const sid = sp.speaker_id || sp.id;
    if (seenSpeakers.has(sid)) return;
    seenSpeakers.add(sid);
    const o = document.createElement("option");
    o.value = sid;
    const badge = sp.role === "admin" ? " (admin)" : sp.type === "Agent" ? " (agent)" : "";
    o.textContent = sp.name + badge + " [" + sid + "]";
    speakerSel.appendChild(o);
  });
  // Restore saved speaker, or default to the first (admin)
  const saved = localStorage.getItem("onto_speaker");
  if (saved && [...speakerSel.options].some(o => o.value === saved)) {
    speakerSel.value = saved;
  }
  // Persist whatever is selected so future loads stay consistent
  localStorage.setItem("onto_speaker", getSpeaker());

  // Type filter
  entityTypes.forEach(t => {
    const o = document.createElement("option");
    o.value = t; o.textContent = t;
    typeFilter.appendChild(o);
  });

  // Entity type in create modal
  const entityTypeSel = $("#entity-type");
  entityTypes.forEach(t => {
    const o = document.createElement("option");
    o.value = t; o.textContent = t;
    entityTypeSel.appendChild(o);
  });

  // Relation type filter
  const relFilter = $("#rel-type-filter");
  if (relFilter) {
    Object.keys(relationDefs).forEach(t => {
      const o = document.createElement("option");
      o.value = t; o.textContent = t;
      relFilter.appendChild(o);
    });
    relFilter.addEventListener("change", () => loadRelationsView());
  }

  // Events
  speakerSel.addEventListener("change", async () => {
    localStorage.setItem("onto_speaker", getSpeaker());
    await refreshEntitiesCache();
    loadEntities(true);
  });
  typeFilter.addEventListener("change", () => loadEntities(true));

  document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  $("#btn-new-entity").addEventListener("click", () => openEntityModal());
  $("#btn-new-relation").addEventListener("click", () => openRelationModal());
  loadMoreBtn.addEventListener("click", () => loadEntities(false));

  $("#btn-back").addEventListener("click", closeDetail);
  $("#btn-edit").addEventListener("click", startEdit);
  $("#btn-delete").addEventListener("click", deleteCurrentEntity);

  $("#entity-form").addEventListener("submit", submitEntity);
  $("#entity-type").addEventListener("change", renderDynamicFields);
  $("#relation-form").addEventListener("submit", submitRelation);

  setupEntitySearch("rel-from", () => updateRelTypeOptions());
  $("#rel-from").addEventListener("input", () => updateRelTypeOptions());
  $("#rel-type").addEventListener("change", () => updateRelPropsFields());

  document.querySelectorAll(".modal-close").forEach(btn => {
    btn.addEventListener("click", closeAllModals);
  });
  [entityModal, relationModal].forEach(m => {
    m.addEventListener("click", (e) => { if (e.target === m) closeAllModals(); });
  });

  await refreshEntitiesCache();
  loadEntities(true);
}

function closeAllModals() {
  entityModal.classList.add("hidden");
  relationModal.classList.add("hidden");
}

/* ── Tab Switching ─────────────────────────────────────────────── */
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-content").forEach(t => t.classList.toggle("active", t.id === name + "-tab"));
  if (name === "relations") loadRelationsView();
}

/* ── Entity List (grouped by type) ─────────────────────────────── */
async function loadEntities(reset) {
  if (reset) { currentOffset = 0; entities = []; entityList.innerHTML = ""; }
  const type = typeFilter.value;
  const q = type
    ? `?type=${type}&limit=${PAGE_SIZE}&offset=${currentOffset}`
    : `?limit=${PAGE_SIZE}&offset=${currentOffset}`;

  try {
    entityList.insertAdjacentHTML("beforeend", '<div class="loading">Loading...</div>');
    const data = await api("/entities" + q, { headers: headers() });
    entityList.querySelector(".loading")?.remove();

    if (reset && data.length === 0) {
      entityList.innerHTML = '<div class="empty">No entities found</div>';
      loadMoreBtn.style.display = "none";
      return;
    }
    entities = entities.concat(data);
    currentOffset += data.length;
    loadMoreBtn.style.display = data.length === PAGE_SIZE ? "block" : "none";

    // Group by type
    if (reset) {
      renderGroupedEntities(entities);
    } else {
      // Append — re-render all
      renderGroupedEntities(entities);
    }
  } catch (e) {
    entityList.querySelector(".loading")?.remove();
    toast("Error: " + e.message, "error");
  }
}

function renderGroupedEntities(list) {
  entityList.innerHTML = "";
  if (list.length === 0) {
    entityList.innerHTML = '<div class="empty">No entities found</div>';
    return;
  }

  // If filtering by single type, no grouping needed
  if (typeFilter.value) {
    list.forEach(e => entityList.appendChild(createCard(e)));
    return;
  }

  // Group
  const grouped = {};
  list.forEach(e => {
    if (!grouped[e.type]) grouped[e.type] = [];
    grouped[e.type].push(e);
  });

  // Sort groups by name
  const sortedTypes = Object.keys(grouped).sort();
  sortedTypes.forEach(type => {
    const section = document.createElement("div");
    section.className = "type-group";

    const header = document.createElement("div");
    header.className = "type-group-header";
    header.innerHTML = `<span class="type-group-name">${escapeHtml(type)}</span><span class="type-group-count">${grouped[type].length}</span><span class="type-group-chevron">&#9660;</span>`;
    header.addEventListener("click", () => {
      section.classList.toggle("collapsed");
    });
    section.appendChild(header);

    const body = document.createElement("div");
    body.className = "type-group-body";
    grouped[type].forEach(e => body.appendChild(createCard(e)));
    section.appendChild(body);

    entityList.appendChild(section);
  });
}

function createCard(e) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = e.id;
  const vis = e.properties.visibility || "public";
  const owner = e.properties.owner || "";
  card.innerHTML = `
    <div class="card-title">${escapeHtml(entityLabel(e))}</div>
    <div class="card-meta">
      <span>${escapeHtml(vis)}</span>
      <span>${escapeHtml(owner)}</span>
      <span>${escapeHtml(e.id)}</span>
    </div>`;
  card.addEventListener("click", () => openDetail(e));
  return card;
}

/* ── Entity Detail ─────────────────────────────────────────────── */
async function openDetail(e) {
  editingEntity = e;
  $("#detail-title").textContent = e.type + ": " + entityLabel(e);
  const body = $("#detail-body");
  body.innerHTML = "";

  const meta = { id: e.id, type: e.type, created: e.created, updated: e.updated };
  Object.entries(meta).forEach(([k, v]) => {
    body.insertAdjacentHTML("beforeend", `
      <div class="prop-row">
        <span class="prop-key">${escapeHtml(k)}</span>
        <span class="prop-val">${escapeHtml(v)}</span>
      </div>`);
  });
  Object.entries(e.properties).forEach(([k, v]) => {
    const display = typeof v === "object" ? JSON.stringify(v) : v;
    body.insertAdjacentHTML("beforeend", `
      <div class="prop-row" data-prop="${escapeHtml(k)}">
        <span class="prop-key">${escapeHtml(k)}</span>
        <span class="prop-val">${escapeHtml(display)}</span>
      </div>`);
  });

  const relDiv = $("#detail-relations");
  relDiv.innerHTML = '<div class="section-title">Relations</div><div class="loading">Loading...</div>';
  detailPanel.classList.remove("hidden");

  try {
    const rels = await api(`/entities/${e.id}/related?dir=both`, { headers: headers() });
    relDiv.innerHTML = '<div class="section-title">Relations (' + rels.length + ')</div>';
    if (rels.length === 0) {
      relDiv.insertAdjacentHTML("beforeend", '<div class="empty">No relations</div>');
    } else {
      rels.forEach(r => {
        const rc = document.createElement("div");
        rc.className = "rel-card";
        const arrow = r.direction === "outgoing" ? "\u2192" : "\u2190";
        let propsStr = "";
        if (r.properties && Object.keys(r.properties).length) {
          propsStr = " (" + Object.entries(r.properties).map(([k,v]) => `${k}: ${v}`).join(", ") + ")";
        }
        rc.innerHTML = `
          <span class="rel-dir">${arrow}</span>
          <span class="rel-type-label">${escapeHtml(r.relation)}${escapeHtml(propsStr)}</span>
          <strong>${escapeHtml(r.entity.type)}: ${escapeHtml(entityLabel(r.entity))}</strong>`;
        rc.addEventListener("click", () => openDetail(r.entity));
        relDiv.appendChild(rc);
      });
    }
  } catch (err) {
    relDiv.querySelector(".loading")?.remove();
    relDiv.insertAdjacentHTML("beforeend", '<div class="empty">Failed to load</div>');
  }
}

function closeDetail() { detailPanel.classList.add("hidden"); editingEntity = null; }

/* ── Edit Entity ───────────────────────────────────────────────── */
function startEdit() {
  if (!editingEntity) return;
  const e = editingEntity;
  $("#modal-title").textContent = "Edit: " + entityLabel(e);
  const typeSel = $("#entity-type");
  typeSel.value = e.type;
  typeSel.disabled = true;
  renderDynamicFields();

  Object.entries(e.properties).forEach(([k, v]) => {
    if (k === "visibility") { $("#entity-visibility").value = v; return; }
    if (k === "owner") return;
    // Multi-ref checkboxes
    const wrap = document.querySelector(`#dynamic-fields .ref-checkbox-wrap[data-field-name="${k}"]`);
    if (wrap && Array.isArray(v)) {
      v.forEach(id => {
        const cb = wrap.querySelector(`input[value="${id}"]`);
        if (cb) cb.checked = true;
      });
      return;
    }
    const input = document.querySelector(`#dynamic-fields [name="${k}"]`);
    if (input) input.value = typeof v === "object" ? JSON.stringify(v) : v;
  });

  entityModal._editId = e.id;
  entityModal.classList.remove("hidden");
  detailPanel.classList.add("hidden");
}

async function deleteCurrentEntity() {
  if (!editingEntity) return;
  if (!confirm("Delete " + entityLabel(editingEntity) + "?")) return;
  try {
    await api(`/entities/${editingEntity.id}`, { method: "DELETE", headers: headers() });
    toast("Deleted", "success");
    closeDetail();
    refreshEntitiesCache();
    loadEntities(true);
  } catch (e) { toast("Error: " + e.message, "error"); }
}

/* ── Create/Edit Entity Modal ──────────────────────────────────── */
function openEntityModal() {
  $("#modal-title").textContent = "New Entity";
  const typeSel = $("#entity-type");
  typeSel.disabled = false;
  typeSel.selectedIndex = 0;
  entityModal._editId = null;
  renderDynamicFields();
  $("#entity-visibility").value = "public";
  entityModal.classList.remove("hidden");
}

function renderDynamicFields() {
  const container = $("#dynamic-fields");
  container.innerHTML = "";
  const typeName = $("#entity-type").value;
  if (!typeName || !schema) return;

  const typeSchema = (schema.types || {})[typeName];
  if (!typeSchema) return;

  const required = typeSchema.required || [];
  const forbidden = typeSchema.forbidden_properties || [];
  const props = typeSchema.properties || typeSchema;
  const isEditing = !!entityModal._editId;

  const visDefault = FIELD_DEFAULTS[typeName + ".visibility"];
  if (visDefault && !isEditing) {
    $("#entity-visibility").value = visDefault;
  }

  Object.entries(props).forEach(([key, def]) => {
    if (["required", "forbidden_properties", "properties"].includes(key)) return;
    if (key === "visibility_enum") return;
    if (forbidden.includes(key)) return;
    if (key === "owner") return;

    if (key.endsWith("_enum") && Array.isArray(def)) {
      const fieldName = key;
      const isReq = required.includes(key.replace("_enum", "")) || required.includes(key);
      const field = document.createElement("div");
      field.className = "field";
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""}</label>`;
      const sel = document.createElement("select");
      sel.name = fieldName; sel.id = "f-" + fieldName;
      if (!isReq) sel.insertAdjacentHTML("beforeend", '<option value="">(none)</option>');
      def.forEach(v => sel.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`));
      if (isReq) sel.required = true;
      const dflt = FIELD_DEFAULTS[typeName + "." + fieldName];
      if (dflt && !isEditing) sel.value = dflt;
      field.appendChild(sel);
      container.appendChild(field);
      return;
    }

    if (typeof def !== "string" && !Array.isArray(def)) return;

    const fieldName = key;
    const isReq = required.includes(fieldName);
    const field = document.createElement("div");
    field.className = "field";

    // ── ref() fields → dropdown with matching entities ──
    const refMatch = typeof def === "string" && def.match(/^ref\(([^)]+)\)/);
    if (refMatch) {
      const refTypes = refMatch[1].split("|").map(t => t.trim());
      const isArray = def.includes("[]");
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""}</label>`;

      const candidates = allEntitiesCache.filter(e => refTypes.includes(e.type));

      if (isArray) {
        // Multi-select as checkboxes
        const wrap = document.createElement("div");
        wrap.className = "ref-checkbox-wrap";
        wrap.dataset.fieldName = fieldName;
        if (candidates.length === 0) {
          wrap.innerHTML = '<div class="hint">No matching entities</div>';
        } else {
          candidates.forEach(e => {
            const row = document.createElement("label");
            row.className = "checkbox-row";
            row.innerHTML = `
              <input type="checkbox" name="ref_multi_${fieldName}" value="${escapeHtml(e.id)}">
              <span>${escapeHtml(entityLabel(e))}</span>
              <span class="checkbox-id">${escapeHtml(e.id)}</span>`;
            wrap.appendChild(row);
          });
        }
        field.appendChild(wrap);
      } else {
        // Single-select dropdown
        const sel = document.createElement("select");
        sel.name = fieldName; sel.id = "f-" + fieldName;
        sel.insertAdjacentHTML("beforeend", '<option value="">(none)</option>');
        candidates.forEach(e => {
          sel.insertAdjacentHTML("beforeend",
            `<option value="${escapeHtml(e.id)}">${escapeHtml(e.type)}: ${escapeHtml(entityLabel(e))}</option>`);
        });
        if (isReq) sel.required = true;
        field.appendChild(sel);
      }
      container.appendChild(field);
      return;
    }

    let input;
    const isDate = typeof def === "string" && def.includes("date") && !def.includes("datetime");

    if (isDate) {
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""} <span class="hint">YYYY-MM-DD</span></label>`;
      input = document.createElement("input");
      input.type = "text";
      input.placeholder = "YYYY-MM-DD";
      input.pattern = "\\d{4}-\\d{2}-\\d{2}";
      input.inputMode = "numeric";
    } else if (typeof def === "string" && (def.includes("string") || def.includes("url") || def.includes("number") || def.includes("any"))) {
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""}</label>`;
      input = document.createElement("input");
      input.type = "text";
      if (def.includes("number")) input.type = "number";
    } else if (typeof def === "string" && def.includes("datetime")) {
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""}</label>`;
      input = document.createElement("input");
      input.type = "text";
      input.placeholder = "YYYY-MM-DDTHH:MM:SSZ";
    } else if (Array.isArray(def)) {
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""}</label>`;
      input = document.createElement("select");
      if (!isReq) input.insertAdjacentHTML("beforeend", '<option value="">(none)</option>');
      def.forEach(v => input.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`));
    } else {
      field.innerHTML = `<label for="f-${fieldName}">${escapeHtml(fieldName)}${isReq ? " *" : ""}</label>`;
      input = document.createElement("input");
      input.type = "text";
    }

    input.name = fieldName;
    input.id = "f-" + fieldName;
    if (isReq) input.required = true;
    const dflt = FIELD_DEFAULTS[typeName + "." + fieldName];
    if (dflt && !isEditing) input.value = dflt;
    field.appendChild(input);
    container.appendChild(field);
  });
}

async function submitEntity(e) {
  e.preventDefault();
  const form = e.target;
  const typeName = $("#entity-type").value;
  const editId = entityModal._editId;
  const props = {};
  // Standard inputs and selects (skip multi-ref checkboxes)
  form.querySelectorAll("#dynamic-fields input:not([name^='ref_multi_']), #dynamic-fields select, #dynamic-fields textarea").forEach(f => {
    if (f.value !== "") {
      props[f.name] = f.type === "number" ? parseFloat(f.value) : f.value;
    }
  });
  // Multi-ref checkboxes → collect as array of ids
  form.querySelectorAll("#dynamic-fields .ref-checkbox-wrap").forEach(wrap => {
    const fieldName = wrap.dataset.fieldName;
    const checked = [...wrap.querySelectorAll("input:checked")].map(cb => cb.value);
    if (checked.length > 0) props[fieldName] = checked;
  });
  props.visibility = $("#entity-visibility").value;

  try {
    if (editId) {
      await api(`/entities/${editId}`, { method: "PATCH", headers: headers(true), body: JSON.stringify({ properties: props }) });
      toast("Updated", "success");
    } else {
      await api("/entities", { method: "POST", headers: headers(true), body: JSON.stringify({ type: typeName, properties: props }) });
      toast("Created", "success");
    }
    entityModal.classList.add("hidden");
    entityModal._editId = null;
    $("#entity-type").disabled = false;
    refreshEntitiesCache();
    loadEntities(true);
  } catch (err) { toast("Error: " + err.message, "error"); }
}

/* ── Relations View (actual relations list) ────────────────────── */
async function loadRelationsView() {
  const list = $("#relation-list");
  list.innerHTML = '<div class="loading">Loading...</div>';

  try {
    const filter = $("#rel-type-filter")?.value || "";
    const q = filter ? `?rel_type=${filter}&limit=500` : "?limit=500";
    const rels = await api("/relations" + q, { headers: headers() });
    list.innerHTML = "";

    if (rels.length === 0) {
      list.innerHTML = '<div class="empty">No relations found</div>';
      return;
    }

    // Group by rel_type
    const grouped = {};
    rels.forEach(r => {
      if (!grouped[r.rel_type]) grouped[r.rel_type] = [];
      grouped[r.rel_type].push(r);
    });

    Object.entries(grouped).sort(([a],[b]) => a.localeCompare(b)).forEach(([relType, items]) => {
      const section = document.createElement("div");
      section.className = "type-group";

      const header = document.createElement("div");
      header.className = "type-group-header";
      header.innerHTML = `<span class="type-group-name">${escapeHtml(relType)}</span><span class="type-group-count">${items.length}</span><span class="type-group-chevron">&#9660;</span>`;
      header.addEventListener("click", () => section.classList.toggle("collapsed"));
      section.appendChild(header);

      const body = document.createElement("div");
      body.className = "type-group-body";

      items.forEach(r => {
        const card = document.createElement("div");
        card.className = "rel-list-card";

        let propsStr = "";
        if (r.properties && Object.keys(r.properties).length) {
          propsStr = Object.entries(r.properties).map(([k,v]) => `${k}: ${v}`).join(", ");
        }

        card.innerHTML = `
          <div class="rel-list-row">
            <span class="rel-list-from">${escapeHtml(r.from_type)}: ${escapeHtml(r.from_label)}</span>
            <span class="rel-list-arrow">\u2192</span>
            <span class="rel-list-to">${escapeHtml(r.to_type)}: ${escapeHtml(r.to_label)}</span>
          </div>
          ${propsStr ? `<div class="rel-list-props">${escapeHtml(propsStr)}</div>` : '<div class="rel-list-props no-props">no properties</div>'}
          <div class="rel-list-actions">
            <button class="btn-small btn-edit-rel" title="Edit properties">Edit</button>
            <button class="btn-small btn-del-rel danger" title="Delete">Del</button>
          </div>`;

        card.querySelector(".btn-edit-rel").addEventListener("click", (ev) => {
          ev.stopPropagation();
          editRelationProps(r);
        });
        card.querySelector(".btn-del-rel").addEventListener("click", (ev) => {
          ev.stopPropagation();
          deleteRelation(r);
        });

        body.appendChild(card);
      });

      section.appendChild(body);
      list.appendChild(section);
    });
  } catch (err) {
    list.innerHTML = '<div class="empty">Error: ' + escapeHtml(err.message) + '</div>';
  }
}

async function editRelationProps(r) {
  // Build a prompt for each property defined in the schema for this relation type
  const relDef = relationDefs[r.rel_type] || {};
  const relProps = relDef.properties || {};
  const propKeys = Object.keys(relProps);

  if (propKeys.length === 0) {
    toast("This relation type has no editable properties", "error");
    return;
  }

  // Use a simple approach: build a small inline form
  const newProps = { ...r.properties };

  for (const [key, def] of Object.entries(relProps)) {
    if (key.endsWith("_enum") && Array.isArray(def)) {
      const current = r.properties[key] || "";
      const options = def.join(", ");
      const val = prompt(`${key}\nOptions: ${options}\nCurrent: ${current || "(none)"}`, current);
      if (val === null) return; // cancelled
      if (val && def.includes(val)) {
        newProps[key] = val;
      } else if (val === "") {
        delete newProps[key];
      } else if (val) {
        toast(`Invalid value. Must be: ${options}`, "error");
        return;
      }
    }
  }

  try {
    await api(`/relations?from_id=${encodeURIComponent(r.from_id)}&rel_type=${encodeURIComponent(r.rel_type)}&to_id=${encodeURIComponent(r.to_id)}`, {
      method: "PATCH",
      headers: headers(true),
      body: JSON.stringify(newProps),
    });
    toast("Relation updated", "success");
    loadRelationsView();
  } catch (err) {
    toast("Error: " + err.message, "error");
  }
}

async function deleteRelation(r) {
  if (!confirm(`Delete: ${r.from_label} \u2192 ${r.rel_type} \u2192 ${r.to_label}?`)) return;
  try {
    await api(`/relations?from_id=${encodeURIComponent(r.from_id)}&rel_type=${encodeURIComponent(r.rel_type)}&to_id=${encodeURIComponent(r.to_id)}`, {
      method: "DELETE",
      headers: headers(),
    });
    toast("Relation deleted", "success");
    loadRelationsView();
  } catch (err) {
    toast("Error: " + err.message, "error");
  }
}

/* ── Relation Create Modal ─────────────────────────────────────── */
async function openRelationModal() {
  // Refresh cache to make sure entity search + target list work
  if (allEntitiesCache.length === 0) {
    await refreshEntitiesCache();
  }
  const fromInput = $("#rel-from");
  const toWrap = $("#rel-to-wrap");
  fromInput.value = "";
  fromInput._entityType = null;
  fromInput._entityId = null;
  $("#rel-type").innerHTML = '<option value="">(select source first)</option>';
  toWrap.innerHTML = "";
  $("#rel-props-fields").innerHTML = "";
  relationModal.classList.remove("hidden");
}

function getValidRelations(fromType) {
  const valid = [];
  for (const [relName, relDef] of Object.entries(relationDefs)) {
    if (relDef.from_types && relDef.from_types.includes(fromType)) {
      valid.push({ name: relName, to_types: relDef.to_types || [], properties: relDef.properties || {} });
    }
  }
  return valid;
}

function updateRelTypeOptions() {
  const fromInput = $("#rel-from");
  const relTypeSel = $("#rel-type");
  const fromType = fromInput._entityType;
  relTypeSel.innerHTML = "";
  if (!fromType) {
    relTypeSel.innerHTML = '<option value="">(select source first)</option>';
    return;
  }
  const valid = getValidRelations(fromType);
  if (valid.length === 0) {
    relTypeSel.innerHTML = '<option value="">(no relations for this type)</option>';
    return;
  }
  relTypeSel.insertAdjacentHTML("beforeend", '<option value="">Choose relation...</option>');
  valid.forEach(r => {
    relTypeSel.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)} \u2192 [${r.to_types.join(", ")}]</option>`);
  });
  updateRelPropsFields();
}

function updateRelPropsFields() {
  const relName = $("#rel-type").value;
  const propsContainer = $("#rel-props-fields");
  const toWrap = $("#rel-to-wrap");
  propsContainer.innerHTML = "";
  toWrap.innerHTML = "";
  if (!relName || !relationDefs[relName]) return;

  const relDef = relationDefs[relName];
  const allowedToTypes = relDef.to_types || [];

  const relProps = relDef.properties || {};
  Object.entries(relProps).forEach(([key, def]) => {
    if (key.endsWith("_enum") && Array.isArray(def)) {
      const field = document.createElement("div");
      field.className = "field";
      field.innerHTML = `<label>${escapeHtml(key)}</label>`;
      const sel = document.createElement("select");
      sel.name = "relprop_" + key;
      sel.insertAdjacentHTML("beforeend", '<option value="">(none)</option>');
      def.forEach(v => sel.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`));
      field.appendChild(sel);
      propsContainer.appendChild(field);
    }
  });

  const candidates = allEntitiesCache.filter(e => allowedToTypes.length === 0 || allowedToTypes.includes(e.type));
  if (candidates.length === 0) {
    toWrap.innerHTML = '<div class="empty">No matching target entities</div>';
    return;
  }

  const grouped = {};
  candidates.forEach(e => {
    if (!grouped[e.type]) grouped[e.type] = [];
    grouped[e.type].push(e);
  });

  const label = document.createElement("label");
  label.textContent = "Target entities (multi-select)";
  label.className = "field-label";
  toWrap.appendChild(label);

  Object.entries(grouped).forEach(([type, ents]) => {
    const groupLabel = document.createElement("div");
    groupLabel.className = "checkbox-group-label";
    groupLabel.textContent = type;
    toWrap.appendChild(groupLabel);

    ents.forEach(e => {
      const row = document.createElement("label");
      row.className = "checkbox-row";
      row.innerHTML = `
        <input type="checkbox" name="rel-target" value="${escapeHtml(e.id)}">
        <span>${escapeHtml(entityLabel(e))}</span>
        <span class="checkbox-id">${escapeHtml(e.id)}</span>`;
      toWrap.appendChild(row);
    });
  });
}

function setupEntitySearch(inputId, onSelect) {
  const input = document.getElementById(inputId);
  const results = document.getElementById(inputId + "-results");
  let debounce = null;

  input.addEventListener("input", () => {
    clearTimeout(debounce);
    const q = input.value.trim();
    input._entityType = null;
    input._entityId = null;
    if (q.length < 1) { results.classList.add("hidden"); return; }

    debounce = setTimeout(() => {
      const search = q.toLowerCase();
      const filtered = allEntitiesCache.filter(e => {
        const lbl = entityLabel(e).toLowerCase();
        const id = e.id.toLowerCase();
        return lbl.includes(search) || id.includes(search) || e.type.toLowerCase().includes(search);
      }).slice(0, 15);

      results.innerHTML = "";
      if (filtered.length === 0) {
        results.innerHTML = '<div class="search-item">No results</div>';
      } else {
        filtered.forEach(e => {
          const item = document.createElement("div");
          item.className = "search-item";
          item.innerHTML = `<span class="search-item-type">${escapeHtml(e.type)}</span> ${escapeHtml(entityLabel(e))} <span class="search-item-type">${escapeHtml(e.id)}</span>`;
          item.addEventListener("click", () => {
            input.value = entityLabel(e) + " (" + e.id + ")";
            input._entityType = e.type;
            input._entityId = e.id;
            results.classList.add("hidden");
            if (onSelect) onSelect(e);
          });
          results.appendChild(item);
        });
      }
      results.classList.remove("hidden");
    }, 150);
  });

  input.addEventListener("blur", () => setTimeout(() => results.classList.add("hidden"), 250));
}

async function submitRelation(e) {
  e.preventDefault();
  const fromInput = $("#rel-from");
  const fromId = fromInput._entityId;
  const relType = $("#rel-type").value;
  if (!fromId) { toast("Select source entity", "error"); return; }
  if (!relType) { toast("Select relation type", "error"); return; }

  const relProps = {};
  document.querySelectorAll("#rel-props-fields select, #rel-props-fields input").forEach(f => {
    if (f.value) {
      const key = f.name.replace("relprop_", "");
      relProps[key] = f.value;
    }
  });

  const targets = [...document.querySelectorAll('#rel-to-wrap input[name="rel-target"]:checked')].map(cb => cb.value);
  if (targets.length === 0) { toast("Select at least one target", "error"); return; }

  let created = 0, errors = 0;
  for (const toId of targets) {
    try {
      await api("/relations", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({ from_id: fromId, rel_type: relType, to_id: toId, properties: relProps }),
      });
      created++;
    } catch (err) { errors++; }
  }

  if (errors === 0) toast(`${created} relation${created > 1 ? "s" : ""} created`, "success");
  else toast(`${created} created, ${errors} failed`, "error");
  relationModal.classList.add("hidden");
}

/* ── Boot ──────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", init);
