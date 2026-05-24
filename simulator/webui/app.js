// app.js — polling /status каждую секунду и рендеринг

const POLL_INTERVAL_MS = 1000;
const $ = (id) => document.getElementById(id);

// Последние данные от /status — нужны чтобы при клике на заголовок
// перерисовать таблицу мгновенно, не дожидаясь следующего polling.
let lastData = null;

// Состояние сортировки таблицы активных партий.
// dir: "asc" | "desc". Клик по тому же столбцу — toggle, по другому — asc.
const sortState = { column: "batch_id", dir: "asc" };

// Приоритет для сортировки: rush=0 < urgent=1 < normal=2.
// При сортировке "asc" rush идёт первым.
const PRIORITY_RANK = { rush: 0, urgent: 1, normal: 2 };

// Стадии партии по порядку маршрута: pending → turning → … → inspection.
// Используется для сортировки столбца "Стадия" по логике производства,
// а не по алфавиту.
const STAGE_RANK = {
    pending:                 1,
    turning:                 2,
    waiting_hobbing:         3,
    hobbing:                 4,
    waiting_shaving:         5,
    shaving:                 6,
    waiting_heat_treatment:  7,
    heat_treatment:          8,
    waiting_grinding:        9,
    grinding:               10,
    waiting_inspection:     11,
    inspection:             12,
    done:                   13,
};

// Ключи сортировки — возвращают сравнимое значение для каждой колонки.
const SORT_KEYS = {
    batch_id:           (b) => b.batch_id,
    product_code:       (b) => b.product_code,
    priority:           (b) => PRIORITY_RANK[b.priority] ?? 99,
    stage:              (b) => STAGE_RANK[b.stage] ?? 99,
    current_machine_id: (b) => b.current_machine_id || "",
    progress:           (b) => b.quantity > 0 ? b.parts_done_in_stage / b.quantity : 0,
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

async function fetchStatus() {
    try {
        const r = await fetch("/status");
        if (!r.ok) return;
        const data = await r.json();
        lastData = data;
        render(data);
    } catch (e) {
        console.error("status failed", e);
    }
}

// ─── Сортировка активных партий ─────────────────────────────
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

// Обновить индикаторы ▲/▼ в заголовках таблицы.
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

    // counters
    const c = data.counters;
    $("counters").textContent =
        ` (orders: ${c.orders_total} | batches done: ${c.batches_done} | pass: ${c.inspections_pass} | fail: ${c.inspections_fail})`;

    // machines grid
    const grid = $("machines-grid");
    grid.innerHTML = "";
    for (const m of data.machines) {
        const cell = document.createElement("div");
        cell.className = "machine-cell";
        cell.style.borderLeftColor = STATE_COLORS[m.state] || "#888";
        cell.innerHTML = `
            <div class="m-id">${m.machine_id}</div>
            <div class="m-type">${m.machine_type}</div>
            <div class="m-state" style="color:${STATE_COLORS[m.state]||'#888'}">${m.state}</div>
            <div class="m-batch">${m.current_batch_id ?? '—'}</div>
            <div class="m-wear">tool: ${(m.tool_wear*100).toFixed(0)}%</div>
        `;
        grid.appendChild(cell);
    }

    // queue furnace
    const qf = data.queues.waiting_furnace || [];
    $("queue-furnace-count").textContent = `(${qf.length})`;
    $("queue-furnace").innerHTML = qf.map(b => `<span class="chip">${b.batch_id}</span>`).join("");

    // furnace loads
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

    // batches — сортировка по выбранному столбцу
    $("batches-count").textContent = `(${data.active_batches.length})`;
    updateSortIndicators();
    const bb = $("batches-body");
    bb.innerHTML = "";
    for (const b of sortedBatches(data.active_batches)) {
        const tr = document.createElement("tr");
        const progress = `${b.parts_done_in_stage}/${b.quantity}`;
        tr.innerHTML = `
            <td>${b.batch_id}</td>
            <td>${b.product_code}</td>
            <td>${b.priority}</td>
            <td>${b.stage}</td>
            <td>${b.current_machine_id ?? '—'}</td>
            <td>${progress}</td>
            <td>${b.fails_count}</td>
        `;
        bb.appendChild(tr);
    }

    // work orders
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

// controls
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

// Клик по заголовку таблицы активных партий → сортировка.
// Если кликнули по тому же столбцу — переключаем направление (asc ↔ desc).
// Если по другому — новый столбец, направление asc.
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

setInterval(fetchStatus, POLL_INTERVAL_MS);
fetchStatus();