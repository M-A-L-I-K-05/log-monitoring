"use strict";

// ── helpers ──────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    const text = await r.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
}

function show(el, obj) {
    el.hidden = false;
    el.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
}

function fmtTime(iso) {
    if (!iso) return "—";
    return iso.replace("T", " ").replace(/\.\d+.*$/, "").replace(/Z$/, "");
}

function setBadge(el, text, cls) {
    el.textContent = text;
    el.className = "badge " + cls;
}

// ── status / health ──────────────────────────────────────────
async function refreshStatus() {
    try {
        const h = await api("GET", "/health");
        setBadge($("loki-badge"), h.loki_ready ? "Loki: ✓" : "Loki: ✗",
                 h.loki_ready ? "ok" : "warn");
    } catch { setBadge($("loki-badge"), "Loki: ?", "warn"); }

    try {
        const st = await api("GET", "/status");
        setBadge($("version-badge"),
                 "версия: " + (st.active_version || "нет"),
                 st.active_version ? "info" : "off");
        const bg = st.background || {};
        setBadge($("bg-badge"), "фон: " + (bg.enabled ? "вкл" : "выкл"),
                 bg.enabled ? "ok" : "off");
        $("run-count").textContent = "прогонов: " + (st.run_count ?? 0);

        // тумблеры режима
        $("bg-enabled").checked = !!bg.enabled;
        $("bg-retrain").checked = !!bg.retrain_enabled;
        if (document.activeElement !== $("bg-interval"))
            $("bg-interval").value = bg.interval_sec ?? "";

        renderMachines(st.detectors || []);
    } catch (e) { /* сервис мог ещё подниматься */ }
}

function renderMachines(dets) {
    const tb = $("machines-tbody");
    tb.innerHTML = "";
    $("machines-count").textContent = dets.length ? `(${dets.length})` : "";
    $("machines-empty").hidden = dets.length > 0;
    dets.sort((a, b) => (a.machine_id || "").localeCompare(b.machine_id || ""));
    for (const d of dets) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td class="mono">${d.machine_id}</td>
            <td>${d.machine_type || "—"}</td>
            <td>${d.n_train ?? "—"}</td>
            <td>${d.contamination ?? "—"}</td>
            <td class="muted">${(d.features || []).join(", ")}</td>
            <td class="mono muted">${fmtTime(d.trained_at)}</td>`;
        tb.appendChild(tr);
    }
}

// ── модели / версии ───────────────────────────────────────────
async function refreshModels() {
    let data;
    try { data = await api("GET", "/models"); } catch { return; }
    const versions = data.versions || [];
    const tb = $("versions-tbody");
    tb.innerHTML = "";
    $("versions-count").textContent = versions.length ? `(${versions.length})` : "";
    $("versions-empty").hidden = versions.length > 0;
    for (const v of versions) {
        const tr = document.createElement("tr");
        if (v.active) tr.className = "active-row";
        tr.innerHTML = `
            <td class="mono">${v.version}</td>
            <td class="tag">${v.tag || "—"}</td>
            <td class="mono muted">${fmtTime(v.created_at)}</td>
            <td>${v.n_machines ?? (v.machine_ids || []).length}</td>
            <td>${v.active ? "✓ активна" : ""}</td>
            <td></td>`;
        const actions = tr.lastElementChild;
        if (!v.active) {
            const b = mkBtn("Подключить", "btn-blue", async () => {
                await api("POST", "/models/activate", { version: v.version });
                await refreshModels(); await refreshStatus();
            });
            actions.appendChild(b);
        }
        const del = mkBtn("Удалить", "btn-red", async () => {
            if (!confirm(`Удалить версию ${v.version}?`)) return;
            await api("DELETE", "/models/" + encodeURIComponent(v.version));
            await refreshModels(); await refreshStatus();
        });
        actions.appendChild(del);
        tb.appendChild(tr);
    }
}

function mkBtn(text, cls, onClick) {
    const b = document.createElement("button");
    b.textContent = text;
    b.className = "btn btn-tiny " + cls;
    b.style.marginRight = "0.35rem";
    b.onclick = async () => {
        b.disabled = true;
        try { await onClick(); } catch (e) { alert("Ошибка: " + e.message); }
        finally { b.disabled = false; }
    };
    return b;
}

// ── действия ──────────────────────────────────────────────────
function num(el) {
    const v = el.value.trim();
    return v === "" ? null : Number(v);
}

async function withBtn(btn, fn) {
    btn.disabled = true;
    try { return await fn(); }
    catch (e) { alert("Ошибка: " + e.message); }
    finally { btn.disabled = false; }
}

$("btn-train").onclick = () => withBtn($("btn-train"), async () => {
    const body = {
        real_lookback_min: num($("t-lookback")),
        contamination: num($("t-contam")),
        tag: $("t-tag").value.trim() || null,
    };
    const res = await api("POST", "/train", body);
    show($("train-result"), res);
    await refreshModels(); await refreshStatus();
});

$("btn-detect").onclick = () => withBtn($("btn-detect"), async () => {
    show($("run-result"), await api("POST", "/detect", {}));
    await refreshStatus();
});

$("btn-forecast").onclick = () => withBtn($("btn-forecast"), async () => {
    show($("run-result"), await api("POST", "/forecast", {}));
    await refreshStatus();
});

$("btn-evaluate").onclick = () => withBtn($("btn-evaluate"), async () => {
    show($("eval-result"), await api("POST", "/evaluate", {}));
});

$("btn-reset").onclick = () => withBtn($("btn-reset"), async () => {
    if (!confirm("Очистить результаты в БД (аномалии/прогнозы/прогоны)? Веса останутся.")) return;
    show($("run-result"), await api("POST", "/reset", {}));
    await refreshStatus();
});

$("btn-loop").onclick = () => withBtn($("btn-loop"), async () => {
    const body = {
        enabled: $("bg-enabled").checked,
        retrain_enabled: $("bg-retrain").checked,
        interval_sec: num($("bg-interval")),
    };
    await api("POST", "/loop", body);
    await refreshStatus();
});

// ── init ──────────────────────────────────────────────────────
async function tick() { await refreshStatus(); }
(async () => {
    await refreshModels();
    await refreshStatus();
    setInterval(tick, 5000);
})();
