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


-- Длинный формат: одна строка = одно измерение одного параметра одной детали.
-- Удобно для Grafana GROUP BY и SQL-аналитики. Дефекты = WHERE result='fail'.
-- ML по-прежнему ест логи (дублирование намеренное).
CREATE TABLE measurements (
    measurement_id    BIGSERIAL   PRIMARY KEY,
    batch_id          TEXT        NOT NULL,
    part_index        INTEGER     NOT NULL,
    product_code      TEXT        NOT NULL,
    stage             TEXT        NOT NULL,
    machine_id        TEXT        NOT NULL,   -- где обрабатывалась деталь
    parameter         TEXT        NOT NULL,
    value             REAL        NOT NULL,
    nominal           REAL        NOT NULL,
    tolerance         REAL        NOT NULL,
    unit              TEXT        NOT NULL,
    result            TEXT        NOT NULL,   -- pass | fail
    reason            TEXT,                   -- причина при fail; NULL при pass
    source_machine_id TEXT,                   -- станок-виновник; NULL при фоне/pass
    scenario_id       TEXT,                   -- id сценария; NULL иначе
    measured_at       TIMESTAMP   NOT NULL
);

CREATE INDEX measurements_batch_idx     ON measurements (batch_id);
CREATE INDEX measurements_stage_idx     ON measurements (stage);
CREATE INDEX measurements_result_idx    ON measurements (result);
CREATE INDEX measurements_scenario_idx  ON measurements (scenario_id);
CREATE INDEX measurements_measured_at_idx ON measurements (measured_at);
