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

function fmtTime(iso) {
    if (!iso) return "—";
    return iso.replace("T", " ").replace(/\.\d+.*$/, "").replace(/Z$/, "");
}

function setBadge(el, text, cls) {
    el.textContent = text;
    el.className = "badge " + cls;
}

// Разбивает ключ "turning__SPUR-M" → ["turning", "SPUR-M"]
function splitKey(key) {
    if (!key) return ["—", "—"];
    const sep = key.indexOf("__");
    if (sep === -1) return [key, "—"];
    return [key.slice(0, sep), key.slice(sep + 2)];
}

// ── статус / health ───────────────────────────────────────────
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

        // показываем требуемое кол-во точек если пришло
        if (st.train_points_required)
            $("train-points-required").textContent = st.train_points_required;

        // тумблеры режима
        $("bg-enabled").checked = !!bg.enabled;
        if (document.activeElement !== $("bg-interval"))
            $("bg-interval").value = bg.interval_sec ?? "";
        if (bg.lookback_sec != null)
            $("bg-lookback").textContent = `окно Loki: ${bg.lookback_sec} с`;

        $("prophet-enabled").checked = !!bg.prophet_enabled;
        if (bg.prophet_cycle_sec != null)
            $("prophet-info").textContent =
                `цикл прогноза: раз в ${bg.prophet_cycle_sec} с (каждые ${bg.prophet_every} тиков фона)`;

        renderModels(st.detectors || []);
    } catch (e) { /* сервис мог ещё подниматься */ }
}

// ── таблица моделей активной версии ──────────────────────────
function renderModels(dets) {
    const tb = $("machines-tbody");
    tb.innerHTML = "";
    $("machines-count").textContent = dets.length ? `(${dets.length})` : "";
    $("machines-empty").hidden = dets.length > 0;

    dets.sort((a, b) => (a.machine_id || "").localeCompare(b.machine_id || ""));
    for (const d of dets) {
        const [mtype, pcode] = splitKey(d.machine_id);
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td class="mono">${mtype}</td>
            <td class="mono">${pcode}</td>
            <td>${d.n_train ?? "—"}</td>
            <td>${d.contamination ?? "—"}</td>
            <td class="mono muted">${fmtTime(d.trained_at)}</td>`;
        tb.appendChild(tr);
    }
}

// ── версии весов ──────────────────────────────────────────────
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
            actions.appendChild(mkBtn("Подключить", "btn-blue", async () => {
                await api("POST", "/models/activate", { version: v.version });
                await refreshModels(); await refreshStatus();
            }));
        }
        const delBtn = document.createElement("button");
        delBtn.className = "btn btn-tiny btn-red";
        delBtn.style.marginRight = "0.35rem";
        armConfirm(delBtn, "Удалить", async () => {
            await api("DELETE", "/models/" + encodeURIComponent(v.version));
            await refreshModels(); await refreshStatus();
        });
        actions.appendChild(delBtn);
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

// Подтверждение опасного действия ДВОЙНЫМ кликом по самой кнопке — без нативного
// confirm() (в некоторых браузерах/режимах он молча возвращает «отмена», и
// удаление/сброс срываются без видимой причины). Первый клик «взводит» кнопку
// (меняет надпись), второй в течение 4с — выполняет. Иначе — сброс.
function armConfirm(btn, normalText, action) {
    let armed = false, timer = null;
    btn.textContent = normalText;
    btn.onclick = async () => {
        if (!armed) {
            armed = true;
            btn.textContent = "Точно? ещё раз";
            btn.classList.add("armed");
            timer = setTimeout(() => {
                armed = false; btn.textContent = normalText; btn.classList.remove("armed");
            }, 4000);
            return;
        }
        clearTimeout(timer); armed = false; btn.classList.remove("armed");
        btn.disabled = true;
        try { await action(); }
        catch (e) { alert("Ошибка: " + e.message); }
        finally { btn.disabled = false; btn.textContent = normalText; }
    };
    return btn;
}

// ── отображение результата обучения ──────────────────────────
function showTrainResult(res) {
    const el = $("train-result");
    el.hidden = false;

    let html = "";

    if (res.trained && res.trained.length) {
        html += `<div class="result-ok">✓ Обучено моделей: ${res.trained.length}</div>`;
        html += `<ul class="result-list">`;
        for (const t of res.trained)
            html += `<li><code>${t.key}</code> — ${t.points} точек</li>`;
        html += `</ul>`;
    }

    if (res.insufficient && res.insufficient.length) {
        html += `<div class="result-warn">⚠ Недостаточно данных (${res.insufficient.length}):</div>`;
        html += `<ul class="result-list">`;
        for (const s of res.insufficient)
            html += `<li><code>${s.key}</code> — ${s.points} / ${s.needed} точек</li>`;
        html += `</ul>`;
    }

    if (res.message)
        html += `<div class="result-hint">${res.message}</div>`;

    if (!html)
        html = `<pre>${JSON.stringify(res, null, 2)}</pre>`;

    el.innerHTML = html;
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
        contamination: num($("t-contam")),
        tag: $("t-tag").value.trim() || null,
    };
    const res = await api("POST", "/train", body);
    showTrainResult(res);
    await refreshModels(); await refreshStatus();
});

armConfirm($("btn-reset"), "Сбросить результаты", async () => {
    const res = await api("POST", "/reset", {});
    $("run-result").hidden = false;
    $("run-result").textContent = JSON.stringify(res, null, 2);
    await refreshStatus();
});

armConfirm($("btn-delete-all"), "Стереть все модели", async () => {
    const res = await api("DELETE", "/models");
    $("run-result").hidden = false;
    $("run-result").textContent = `Удалено версий: ${res.deleted_versions}`;
    await refreshModels(); await refreshStatus();
});

// Галочку применяем СРАЗУ при переключении — иначе фоновый опрос статуса
// (refreshStatus каждые 5с) перечитает состояние сервера и вернёт её обратно.
$("bg-enabled").onchange = () => withBtn($("btn-loop"), async () => {
    await api("POST", "/loop", { enabled: $("bg-enabled").checked });
    await refreshStatus();
});

// «Применить» — ТОЛЬКО интервал (вкл/выкл живёт на галочке bg-enabled).
// Окно запроса к Loki сервер сам считает как интервал + запас (см. /loop).
$("btn-loop").onclick = () => withBtn($("btn-loop"), async () => {
    const interval = num($("bg-interval"));
    if (interval === null) { alert("Введите интервал в секундах"); return; }
    await api("POST", "/loop", { interval_sec: interval });
    await refreshStatus();
});

// Тумблер Prophet-контура — применяем сразу (как и фоновый поток).
$("prophet-enabled").onchange = () => withBtn($("btn-prophet-now"), async () => {
    await api("POST", "/prophet_loop", { enabled: $("prophet-enabled").checked });
    await refreshStatus();
});

// «Прогноз сейчас» — один прогнозный цикл вручную (первое заполнение карточек).
$("btn-prophet-now").onclick = () => withBtn($("btn-prophet-now"), async () => {
    const res = await api("POST", "/prophet_cycle", {});
    $("run-result").hidden = false;
    $("run-result").textContent = JSON.stringify(res, null, 2);
    await refreshStatus();
});

// ── модальное окно «Инструкция» ──────────────────────────────
function toggleHelp(show) { $("help-overlay").hidden = !show; }
$("btn-help").onclick = () => toggleHelp(true);
$("btn-help-close").onclick = () => toggleHelp(false);
$("help-overlay").onclick = (e) => { if (e.target === $("help-overlay")) toggleHelp(false); };
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("help-overlay").hidden) toggleHelp(false);
});

// ── init ──────────────────────────────────────────────────────
(async () => {
    await refreshModels();
    await refreshStatus();
    setInterval(refreshStatus, 5000);
})();
