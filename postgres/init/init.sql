-- Парк станков и их статусы наполняются ДИНАМИЧЕСКИ через POST /register-machines
-- (см. simulator → кнопка "Sync Fleet"). Здесь только схема.
CREATE TABLE machines (
    machine_id    TEXT        PRIMARY KEY,
    machine_type  TEXT        NOT NULL,
    work_center   TEXT        NOT NULL,
    model         TEXT        NOT NULL,
    install_date  DATE        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'active'
);

CREATE TABLE machine_status (
    machine_id         TEXT        PRIMARY KEY REFERENCES machines(machine_id),
    current_state      TEXT        NOT NULL,
    state_changed_at   TIMESTAMP   NOT NULL,
    sensor_updated_at  TIMESTAMP   NOT NULL
);



CREATE TABLE active_batches (
    batch_id          TEXT        PRIMARY KEY,
    order_id          TEXT        NOT NULL,
    product_code      TEXT        NOT NULL,
    priority          TEXT        NOT NULL,
    current_wc        TEXT        NOT NULL,
    planned_quantity  INTEGER     NOT NULL,
    actual_quantity   INTEGER     NOT NULL,
    started_at        TIMESTAMP   NOT NULL,
    wc_entered_at     TIMESTAMP   NOT NULL
);



CREATE TABLE open_work_orders (
    wo_id              TEXT        PRIMARY KEY,
    machine_id         TEXT        NOT NULL REFERENCES machines(machine_id),
    type               TEXT        NOT NULL,
    priority           TEXT        NOT NULL,
    status             TEXT        NOT NULL,
    reason             TEXT,
    assigned_brigade   TEXT,
    created_at         TIMESTAMP   NOT NULL,
    assigned_at        TIMESTAMP
);