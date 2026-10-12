-- Inversiones (ADR 0008): actividad de Trade Republic (CSV «Exportación de transacción») y
-- valoraciones (PDF «Extracto del patrimonio neto»). Participaciones y precios como texto
-- decimal exacto; importes en céntimos.

-- Id del banco para cada movimiento (TR transaction_id). Los movimientos que ya venían del
-- PDF de cuenta se enlazan a su id al importar el CSV, conservando su categoría.
ALTER TABLE fin_transactions ADD COLUMN external_id TEXT;
CREATE UNIQUE INDEX fin_transactions_external
    ON fin_transactions(account_id, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE fin_trades (
    id           TEXT PRIMARY KEY,                  -- trd_…
    account_id   TEXT NOT NULL REFERENCES fin_accounts(id),
    import_id    TEXT NOT NULL REFERENCES fin_imports(id),
    external_id  TEXT NOT NULL,                     -- mismo id que su movimiento de efectivo
    trade_date   TEXT NOT NULL,                     -- YYYY-MM-DD
    executed_at  TEXT NOT NULL,                     -- instante UTC ISO con Z
    kind         TEXT NOT NULL CHECK (kind IN ('buy','sell','dividend')),
    isin         TEXT NOT NULL,
    name         TEXT NOT NULL,
    asset_class  TEXT NOT NULL DEFAULT '',          -- FUND | STOCK | …
    shares       TEXT NOT NULL,                     -- decimal, negativo en ventas
    price        TEXT,                              -- decimal por participación
    amount_cents INTEGER NOT NULL,                  -- efecto en el efectivo, sin comisión ni impuestos
    fee_cents    INTEGER NOT NULL DEFAULT 0,        -- negativo = pagado
    tax_cents    INTEGER NOT NULL DEFAULT 0,
    savings_plan INTEGER NOT NULL DEFAULT 0,        -- 1 = ejecución de plan de inversión
    created_at   TEXT NOT NULL,
    UNIQUE (account_id, external_id)
);
CREATE INDEX fin_trades_isin ON fin_trades(account_id, isin, trade_date);

CREATE TABLE fin_valuations (
    id               TEXT PRIMARY KEY,              -- val_…
    account_id       TEXT NOT NULL REFERENCES fin_accounts(id),
    import_id        TEXT NOT NULL REFERENCES fin_imports(id),
    as_of            TEXT NOT NULL,                 -- YYYY-MM-DD
    securities_cents INTEGER NOT NULL,
    cash_cents       INTEGER NOT NULL,
    total_cents      INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (account_id, as_of)
);

CREATE TABLE fin_valuation_positions (
    valuation_id TEXT NOT NULL REFERENCES fin_valuations(id) ON DELETE CASCADE,
    isin         TEXT NOT NULL,
    name         TEXT NOT NULL,
    shares       TEXT NOT NULL,
    price        TEXT NOT NULL,
    value_cents  INTEGER NOT NULL,
    price_date   TEXT,
    PRIMARY KEY (valuation_id, isin)
);
