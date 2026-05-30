// app.js — polling /status каждую секунду и рендеринг

const POLL_INTERVAL_MS = 1000;
const $ = (id) => document.getElementById(id);

let lastData = null;
let catalog = null;          // {machine_type: [scenario_type, ...]}
let severityLevels = ["light", "clear", "gross"];

const sortState = { column: "batch_id", dir: "asc" };
const PRIORITY_RANK = { rush: 0, urgent: 1, normal: 2 };

const STAGE_RANK = {
    pending:                 1,
    turning:                 2,
    waiting_hobbing:         3,
    hobbing:                 4,
    waiting_shaving:         5,
    shaving:                 6,
    waiting_heat_treatment:  7,
    heat_treatment:          8,
    queue_measurement:       8.5,
    measurement:             8.7,
    waiting_grinding:        9,
    grinding:               10,
    waiting_inspection:     11,
    inspection:             12,
    done:                   13,
};

const SORT_KEYS = {
    batch_id:           (b) => b.batch_id,
    product_code:       (b) => b.product_code,
    priority:           (b) => PRIORITY_RANK[b.priority] ?? 99,
    stage:              (b) => STAGE_RANK[b.stage] ?? 99,
    current_machine_id: (b) => b.current_machine_id || "",
    progress:           (b) => { const q = b.good_quantity ?? b.quantity; return q > 0 ? b.parts_done_in_stage / q : 0; },
    fails_count:        (b) => b.fails_count,
};

const STATE_COLORS = {
    idle: "#888",
    setup: "#e0a800",
    running: "#28a745",
    cooldown: "#6f42c1",
    maintenance: "#007bff",
    fault: "#dc3545",
};

const SEVERITY_COLORS = {
    light: "#ffc107",   // yellow
    clear: "#fd7e14",   // orange
    gross: "#dc3545",   // red
};
// Приглушённый фон для gradual фазы 1 (дрейф без брака)
const SEVERITY_COLORS_MUTED = {
    light: "#2a2100",
    clear: "#2a1500",
    gross: "#2a0505",
};

async function fetchStatus() {
    try {
        const r = await fetch("/status");
        if (!r.ok) return;
        const data = await r.json();
        lastData = data;
        render(data);
        syncAutoOrders(data);
    } catch (e) {
        console.error("status failed", e);
    }
}

async function fetchScenariosMeta() {
    try {
        const r = await fetch("/scenarios");
        if (!r.ok) return;
        const data = await r.json();
        catalog = data.catalog || {};
        if (Array.isArray(data.severity_levels) && data.severity_levels.length) {
            severityLevels = data.severity_levels;
            const sel = $("modal-severity");
            if (sel.options.length === 0) {
                for (const s of severityLevels) {
                    const opt = document.createElement("option");
                    opt.value = s;
                    opt.textContent = s;
                    if (s === "clear") opt.selected = true;
                    sel.appendChild(opt);
                }
            }
        }
        // Синхронизируем галочку авто-сценариев
        const chk = $("chk-auto-scenarios");
        if (chk && data.auto && typeof data.auto.enabled === "boolean") {
            chk._syncing = true;
            chk.checked = data.auto.enabled;
            chk._syncing = false;
        }
    } catch (e) {
        console.error("scenarios meta failed", e);
    }
}

async function syncAutoOrders(data) {
    const chk = $("chk-auto-orders");
    if (chk && data.auto_orders && typeof data.auto_orders.enabled === "boolean") {
        chk._syncing = true;
        chk.checked = data.auto_orders.enabled;
        chk._syncing = false;
    }
}

// Галочка авто-сценариев
document.addEventListener("DOMContentLoaded", () => {
    const chk = $("chk-auto-scenarios");
    if (!chk) return;
    chk.addEventListener("change", async () => {
        if (chk._syncing) return;
        try {
            await fetch("/scenarios/auto", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: chk.checked }),
            });
        } catch (e) {
            console.error("auto toggle failed", e);
        }
    });
});

function sortedBatches(batches) {
    const keyFn = SORT_KEYS[sortState.column] || SORT_KEYS.batch_id;
    const factor = sortState.dir === "asc" ? 1 : -1;
    return [...batches].sort((a, b) => {
        const ka = keyFn(a);
        const kb = keyFn(b);
        if (ka < kb) return -1 * factor;
        if (ka > kb) return  1 * factor;
        return 0;
    });
}

function updateSortIndicators() {
    document.querySelectorAll(".batches-table th[data-sort]").forEach(th => {
        const col = th.dataset.sort;
        th.classList.toggle("sort-asc",  col === sortState.column && sortState.dir === "asc");
        th.classList.toggle("sort-desc", col === sortState.column && sortState.dir === "desc");
    });
}

async function postCmd(path, body) {
    try {
        await fetch(path, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: body ? JSON.stringify(body) : null,
        });
        fetchStatus();
    } catch (e) {
        console.error("cmd failed", e);
    }
}

function render(data) {
    $("virtual-time").textContent = data.virtual_time.replace("T", " ").slice(0, 19);
    $("speed-badge").textContent = "×" + data.speed;
    const rb = $("running-badge");
    rb.textContent = data.running ? "running" : "paused";
    rb.className = "running-badge " + (data.running ? "is-on" : "is-off");

    const c = data.counters;
    $("counters").textContent =
        ` (orders: ${c.orders_total} | batches done: ${c.batches_done} | parts pass: ${c.parts_pass} | parts fail: ${c.parts_fail})`;

    // ─── активные сценарии (по machine_id для индикации на карточках) ─
    const activeScenarios = data.active_scenarios || [];
    const scenarioByMachine = {};
    for (const sc of activeScenarios) {
        scenarioByMachine[sc.machine_id] = sc;
    }

    // ─── machines grid ─────────────────────────────────────────
    const grid = $("machines-grid");
    grid.innerHTML = "";
    for (const m of data.machines) {
        const cell = document.createElement("div");
        cell.className = "machine-cell";
        cell.style.borderLeftColor = STATE_COLORS[m.state] || "#888";
        const sc = scenarioByMachine[m.machine_id];
        let scenarioBadge = "";
        if (sc) {
            const sevColor = SEVERITY_COLORS[sc.severity] || "#888";
            if (sc.mode === "gradual") {
                const drift = sc.drift_progress != null ? sc.drift_progress.toFixed(2) : "?";
                const thresh = sc.scrap_threshold != null ? sc.scrap_threshold.toFixed(2) : "0.85";
                const inScrap = (sc.drift_progress ?? 0) >= (sc.scrap_threshold ?? 0.85);
                if (inScrap) {
                    const defCount = sc.parts_tagged > 0 ? ` [${sc.parts_tagged} def]` : "";
                    scenarioBadge = `<div class="m-scenario grad-phase2" style="background:${sevColor}">⚠ ${sc.scenario_type} (${sc.severity}) ${drift}${defCount}</div>`;
                } else {
                    scenarioBadge = `<div class="m-scenario grad-phase1" style="background:${SEVERITY_COLORS_MUTED[sc.severity]||'#222'};border:1px solid ${sevColor}">${sc.scenario_type} (${sc.severity}) ~${drift}/${thresh}</div>`;
                }
            } else if (sc.machine_type === "furnace") {
                // step для печи: прогресс по времени фазы quenching.
                const el = Math.round(sc.elapsed_min ?? 0);
                const thr = Math.round(sc.threshold_min ?? 0);
                const rem = Math.max(0, thr - el);
                const isScrap = (sc.furnace_phase || "normal") === "scrap";
                if (isScrap) {
                    scenarioBadge = `<div class="m-scenario grad-phase2" style="background:${sevColor}">⚠ ${sc.scenario_type} (${sc.severity}) ${el}min · scrap</div>`;
                } else {
                    scenarioBadge = `<div class="m-scenario grad-phase1" style="background:${SEVERITY_COLORS_MUTED[sc.severity]||'#222'};border:1px solid ${sevColor}">${sc.scenario_type} (${sc.severity}) ${el}min / ${rem}min left</div>`;
                }
            } else {
                // step (обычный станок): прогресс по деталям.
                const processed = sc.parts_tagged ?? "?";
                const cap = sc.parts_cap ?? "?";
                scenarioBadge = `<div class="m-scenario" style="background:${sevColor}">${sc.scenario_type} (${sc.severity}) ${processed}/${cap}</div>`;
            }
        }
        cell.innerHTML = `
            <div class="m-id">${m.machine_id}</div>
            <div class="m-type">${m.machine_type}</div>
            <div class="m-state" style="color:${STATE_COLORS[m.state]||'#888'}">${m.state}</div>
            <div class="m-batch">${m.current_batch_id ?? '—'}</div>
            <div class="m-wear">tool: ${(m.tool_wear*100).toFixed(0)}%</div>
            ${scenarioBadge}
        `;
        if (sc) {
            const sevColor = SEVERITY_COLORS[sc.severity] || "#888";
            cell.classList.add("has-scenario");
            cell.style.outline = `2px solid ${sevColor}`;
            cell.style.outlineOffset = "-2px";
        }
        cell.addEventListener("click", () => openScenarioModal(m));
        grid.appendChild(cell);
    }

    // ─── активные сценарии — две таблицы ──────────────────────
    $("scenarios-count").textContent = `(${activeScenarios.length})`;
    const sgb = $("scenarios-gradual-body");
    const ssb = $("scenarios-step-body");
    sgb.innerHTML = "";
    ssb.innerHTML = "";

    for (const sc of activeScenarios) {
        const tr = document.createElement("tr");
        const sevBadge = `<span class="sev-badge" style="background:${SEVERITY_COLORS[sc.severity]||'#888'}">${sc.severity}</span>`;
        const stopBtn = `<button class="btn btn-tiny btn-red" data-stop-sc="${sc.id}">Stop</button>`;

        if (sc.mode === "gradual") {
            const drift = sc.drift_progress != null ? sc.drift_progress.toFixed(3) : "—";
            const thresh = sc.scrap_threshold != null ? sc.scrap_threshold.toFixed(2) : "0.85";
            const inScrap = (sc.drift_progress ?? 0) >= (sc.scrap_threshold ?? 0.85);
            const phaseHtml = inScrap
                ? `<span class="phase-scrap">⚠ scrap</span>`
                : `<span class="phase-normal">normal</span>`;
            const defective = sc.parts_tagged ?? 0;
            tr.innerHTML = `
                <td>${sc.id}</td>
                <td>${sc.machine_id}</td>
                <td>${sc.scenario_type}</td>
                <td>${sevBadge}</td>
                <td>${drift} / ${thresh}</td>
                <td>${phaseHtml}</td>
                <td>${defective}</td>
                <td>${stopBtn}</td>
            `;
            sgb.appendChild(tr);
        } else {
            // Step. Печь: фаза normal/scrap + прогресс по времени (мин).
            // Остальные станки: фазы нет (сразу scrap), прогресс по деталям.
            let phaseHtml, processed, remaining;
            if (sc.machine_type === "furnace") {
                const el = Math.round(sc.elapsed_min ?? 0);
                const thr = Math.round(sc.threshold_min ?? 0);
                const rem = Math.max(0, thr - el);
                const isScrap = (sc.furnace_phase || "normal") === "scrap";
                phaseHtml = isScrap
                    ? `<span class="phase-scrap">⚠ scrap</span>`
                    : `<span class="phase-normal">normal</span>`;
                processed = `${el}min`;
                remaining = `${rem}min`;
            } else {
                phaseHtml = `<span class="phase-scrap">⚠ scrap</span>`;
                processed = sc.parts_tagged ?? "—";
                remaining = sc.parts_remaining ?? "—";
            }
            tr.innerHTML = `
                <td>${sc.id}</td>
                <td>${sc.machine_id}</td>
                <td>${sc.scenario_type}</td>
                <td>${sevBadge}</td>
                <td>${phaseHtml}</td>
                <td>${processed}</td>
                <td>${remaining}</td>
                <td>${stopBtn}</td>
            `;
            ssb.appendChild(tr);
        }
    }
    [sgb, ssb].forEach(tbody => {
        tbody.querySelectorAll("[data-stop-sc]").forEach(b => {
            b.onclick = () => postCmd("/scenarios/stop", {scenario_id: b.dataset.stopSc});
        });
    });

    // ─── queue furnace ─────────────────────────────────────────
    const qf = data.queues.waiting_furnace || [];
    $("queue-furnace-count").textContent = `(${qf.length})`;
    $("queue-furnace").innerHTML = qf.map(b => `<span class="chip">${b.batch_id}</span>`).join("");

    // ─── queue measurement (перед M-GMM) ───────────────────────
    const qm = data.queues.queue_measurement || [];
    $("queue-measurement-count").textContent = `(${qm.length})`;
    $("queue-measurement").innerHTML = qm.map(b => `<span class="chip">${b.batch_id}</span>`).join("");

    // ─── furnace loads ─────────────────────────────────────────
    const fl = $("furnace-loads");
    fl.innerHTML = "";
    for (const load of data.furnace_loads) {
        const div = document.createElement("div");
        div.className = "furnace-load";
        div.innerHTML = `
            <div class="fl-id">${load.load_id}</div>
            <div class="fl-machine">${load.machine_id}</div>
            <div class="fl-phase">${load.phase}</div>
            <div class="fl-product">${load.product_codes.join(", ")} (${load.total_parts} шт)</div>
            <div class="fl-batches">${load.batch_ids.join(", ")}</div>
        `;
        fl.appendChild(div);
    }

    // ─── inspection stations (M-GMM) ───────────────────────────
    const insp = $("inspection-stations");
    insp.innerHTML = "";
    for (const st of (data.inspection_stations || [])) {
        const div = document.createElement("div");
        const idle = st.state === "idle";
        div.className = "inspection-cell" + (idle ? " is-idle" : "");
        if (idle) {
            div.innerHTML = `
                <div class="insp-id">${st.machine_id}</div>
                <div class="insp-idle">idle</div>
            `;
        } else {
            const total = st.parts_total || 0;
            const pct = total > 0 ? Math.round(100 * st.parts_done / total) : 0;
            const stageLabel = st.stage_after === "inspection"
                ? "final inspection" : `after ${st.stage_after ?? '—'}`;
            div.innerHTML = `
                <div class="insp-id">${st.machine_id} <span class="insp-mode">${st.mode}</span></div>
                <div class="insp-batch">${st.batch_id ?? '—'} · ${st.product_code ?? '—'}</div>
                <div class="insp-stage">${stageLabel}</div>
                <div class="insp-progress">${st.parts_done} / ${total} measured</div>
                <div class="insp-bar"><div class="insp-bar-fill" style="width:${pct}%"></div></div>
            `;
        }
        insp.appendChild(div);
    }

    // ─── batches ───────────────────────────────────────────────
    $("batches-count").textContent = `(${data.active_batches.length})`;
    updateSortIndicators();
    const bb = $("batches-body");
    bb.innerHTML = "";
    const QUEUE_STAGES = ["pending", "waiting_hobbing", "waiting_shaving",
        "waiting_grinding", "waiting_furnace", "queue_measurement", "queue_inspection"];
    for (const b of sortedBatches(data.active_batches)) {
        const tr = document.createElement("tr");
        // Прогресс: в очередях/на измерении — бирка вместо чисел.
        let progress;
        if (QUEUE_STAGES.includes(b.stage)) progress = `<span class="tag-badge">queued</span>`;
        else if (b.stage === "measurement") progress = `<span class="tag-badge">measuring</span>`;
        else if (b.stage === "inspection") progress = `<span class="tag-badge">final QC</span>`;
        else progress = `${b.parts_done_in_stage}/${b.good_quantity ?? b.quantity}`;
        // Брак: на измерении результат ещё не готов — бирка; в очередях/на
        // обработке показываем накопленное число.
        const brak = (b.stage === "measurement" || b.stage === "inspection")
            ? `<span class="tag-muted">—</span>`
            : b.fails_count;
        const frozenCell = b.is_frozen
            ? `<span class="frozen-badge">${b.frozen_reason || 'frozen'}</span>`
            : "—";
        tr.innerHTML = `
            <td>${b.batch_id}</td>
            <td>${b.product_code}</td>
            <td>${b.priority}</td>
            <td>${b.stage}</td>
            <td>${b.current_machine_id ?? '—'}</td>
            <td>${progress}</td>
            <td>${brak}</td>
            <td>${frozenCell}</td>
        `;
        bb.appendChild(tr);
    }

    // ─── work orders ───────────────────────────────────────────
    $("wo-count").textContent = `(${data.open_work_orders.length})`;
    const wb = $("wo-body");
    wb.innerHTML = "";
    for (const wo of data.open_work_orders) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
            <td>${wo.wo_id}</td>
            <td>${wo.machine_id}</td>
            <td>${wo.type}</td>
            <td>${wo.priority}</td>
            <td>${wo.status}</td>
            <td>${wo.assigned_brigade_id ?? '—'}</td>
            <td>${wo.reason ?? ''}</td>
        `;
        wb.appendChild(tr);
    }
}

// ─── модалка сценария ───────────────────────────────────────
const MODE_HINT = {
    gradual: { label: "gradual", desc: "Постепенный дрейф через партии — для предиктивного ML" },
    step:    { label: "step",    desc: "Мгновенная полная аномалия — для диагностики причины" },
};

const SCENARIO_DESCRIPTIONS = {
    tool_wear:         "Ускоренный износ резца: вибрация и нагрузка шпинделя постепенно растут → отклонение торцевого биения заготовки",
    hob_wear:          "Износ зуборезной фрезы: нарастающая вибрация и температура подшипника → отклонения профиля и шага зуба",
    shaver_wear:       "Износ шевинг-фрезы: рост вибрации и нагрузки → отклонения профиля и направления зуба",
    chatter:           "Вибрационный резонанс (дребезг): резкий скачок вибрации при нарезании → отклонение профиля зуба",
    workpiece_loose:   "Ненадёжный зажим заготовки: нестабильное вращение шпинделя → биение и отклонение направления зуба",
    shaver_wear:       "Износ шевинг-фрезы: рост вибрации и нагрузки → отклонения профиля и направления зуба",
    under_carburizing: "Недостаточная цементация: падение углеродного потенциала и температуры → низкая поверхностная твёрдость и недостаточная глубина слоя",
    over_carburizing:  "Избыточная цементация: перегрев и высокий углеродный потенциал → перетвёрдость и хрупкость цементованного слоя",
    quench_distortion: "Нарушение масляной закалки: потеря потока закалочного масла → коробление геометрии (биение, направление зуба)",
    grinding_chatter:  "Вибрационный резонанс при шлифовании: резкий скачок вибрации → шероховатость поверхности и отклонение профиля",
    coolant_loss:      "Потеря охладителя: резкое падение расхода СОЖ → термический ожог, шероховатость и биение",
    grinding_burn:     "Тепловой ожог шлифованием: избыток мощности при нехватке СОЖ → снижение поверхностной твёрдости и шероховатость",
};

function updateScenarioModeHint() {
    const sel = $("modal-scenario-type");
    const machineType = $("modal-machine-id").dataset.machineType;
    const types = catalog[machineType] || [];
    const info = types.find(t => t.type === sel.value);
    const hint = $("modal-sc-mode-hint");
    if (!hint) return;
    if (info) {
        const m = MODE_HINT[info.mode] || { label: info.mode, desc: "" };
        const desc = SCENARIO_DESCRIPTIONS[info.type] || "";
        hint.innerHTML =
            `<span class="mode-tag ${info.mode}">${m.label}</span>${m.desc}`
            + (desc ? `<div class="sc-desc">${desc}</div>` : "");
    } else {
        hint.textContent = "";
    }
}

function openScenarioModal(machine) {
    if (!catalog) {
        alert("Каталог сценариев ещё не загружен. Попробуйте через секунду.");
        return;
    }
    const types = catalog[machine.machine_type] || [];
    if (types.length === 0) {
        alert(`Для типа станка "${machine.machine_type}" нет сценариев.`);
        return;
    }
    $("modal-machine-id").textContent = `${machine.machine_id} (${machine.machine_type})`;
    $("modal-machine-id").dataset.machineId = machine.machine_id;
    $("modal-machine-id").dataset.machineType = machine.machine_type;
    const sel = $("modal-scenario-type");
    sel.innerHTML = "";
    for (const t of types) {
        const opt = document.createElement("option");
        opt.value = t.type;
        opt.textContent = `${t.type}  [${t.mode}]`;
        const desc = SCENARIO_DESCRIPTIONS[t.type];
        if (desc) opt.title = desc;
        sel.appendChild(opt);
    }
    $("modal-msg").textContent = "";
    updateScenarioModeHint();
    $("scenario-modal").classList.remove("hidden");
}

function closeScenarioModal() {
    $("scenario-modal").classList.add("hidden");
}

async function submitScenario() {
    const machine_id = $("modal-machine-id").dataset.machineId;
    const scenario_type = $("modal-scenario-type").value;
    const severity = $("modal-severity").value;
    const msg = $("modal-msg");
    msg.textContent = "...";
    try {
        const r = await fetch("/scenarios/start", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({machine_id, scenario_type, severity}),
        });
        const data = await r.json();
        if (!r.ok) {
            msg.textContent = "Ошибка: " + (data.detail || JSON.stringify(data));
            msg.className = "modal-msg error";
        } else {
            msg.textContent = "Запущен " + data.scenario_id;
            msg.className = "modal-msg ok";
            fetchStatus();
            setTimeout(closeScenarioModal, 600);
        }
    } catch (e) {
        msg.textContent = "Network error: " + e.message;
        msg.className = "modal-msg error";
    }
}

// ─── controls ───────────────────────────────────────────────
$("btn-start").onclick = () => postCmd("/start");
$("btn-stop").onclick = () => postCmd("/stop");
$("btn-restart").onclick = () => postCmd("/restart");
$("btn-sync-fleet").onclick = async () => {
    const btn = $("btn-sync-fleet");
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "syncing…";
    try {
        const r = await fetch("/sync-fleet", {method: "POST"});
        const data = await r.json();
        const res = data.result || {};
        if (res.sync) {
            btn.textContent = `synced ${res.count}`;
        } else {
            btn.textContent = "sync failed";
            console.error("sync failed", res);
        }
        await fetchStatus();
    } catch (e) {
        btn.textContent = "sync failed";
        console.error("sync failed", e);
    } finally {
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
    }
};
document.querySelectorAll(".btn-speed").forEach(btn => {
    btn.onclick = () => postCmd("/speed", {multiplier: parseInt(btn.dataset.speed)});
});

$("btn-stop-all-sc").onclick = () => postCmd("/scenarios/stop-all");

// Сортировка таблицы партий
document.querySelectorAll(".batches-table th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
        const col = th.dataset.sort;
        if (sortState.column === col) {
            sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
        } else {
            sortState.column = col;
            sortState.dir = "asc";
        }
        if (lastData) render(lastData);
    });
});

// Модалка сценария
$("modal-close").onclick = closeScenarioModal;
$("modal-cancel").onclick = closeScenarioModal;
$("modal-submit").onclick = submitScenario;
$("modal-scenario-type").addEventListener("change", updateScenarioModeHint);
$("scenario-modal").addEventListener("click", (e) => {
    if (e.target === $("scenario-modal")) closeScenarioModal();
});

// ─── Заказы ───────────────────────────────────────────────────
function openOrderModal() {
    $("order-modal-msg").textContent = "";
    $("order-modal").classList.remove("hidden");
}
function closeOrderModal() {
    $("order-modal").classList.add("hidden");
}
async function submitOrder() {
    const btn = $("order-modal-submit");
    btn.disabled = true;
    $("order-modal-msg").textContent = "";
    try {
        const r = await fetch("/orders/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                product_code: $("order-product").value,
                priority: $("order-priority").value,
                total_quantity: parseInt($("order-qty").value),
            }),
        });
        const data = await r.json();
        if (data.ok) {
            $("order-modal-msg").style.color = "#4caf50";
            $("order-modal-msg").textContent = `Создан ${data.order_id}: ${data.batches} партий, ${data.total_quantity} дет.`;
            setTimeout(closeOrderModal, 1500);
        } else {
            $("order-modal-msg").style.color = "#f44336";
            $("order-modal-msg").textContent = data.detail || "Ошибка";
        }
    } catch (e) {
        $("order-modal-msg").style.color = "#f44336";
        $("order-modal-msg").textContent = "Ошибка запроса";
    } finally {
        btn.disabled = false;
    }
}

$("btn-create-order").onclick = openOrderModal;
$("order-modal-close").onclick = closeOrderModal;
$("order-modal-cancel").onclick = closeOrderModal;
$("order-modal-submit").onclick = submitOrder;
$("order-modal").addEventListener("click", (e) => {
    if (e.target === $("order-modal")) closeOrderModal();
});

// Галочка авто-заказов
document.addEventListener("DOMContentLoaded", () => {
    const chk = $("chk-auto-orders");
    if (!chk) return;
    chk.addEventListener("change", async () => {
        if (chk._syncing) return;
        try {
            await fetch("/orders/auto", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: chk.checked }),
            });
        } catch (e) {
            console.error("auto orders toggle failed", e);
        }
    });
});

setInterval(fetchStatus, POLL_INTERVAL_MS);
setInterval(fetchScenariosMeta, POLL_INTERVAL_MS);
fetchStatus();
fetchScenariosMeta();
