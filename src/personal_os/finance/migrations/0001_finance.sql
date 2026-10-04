-- Finanzas v1: cuentas, importaciones, movimientos, categorías y reglas. Dueño: módulo finance.
-- Importes en céntimos (INTEGER). Fechas "de pared" YYYY-MM-DD. Instantes UTC ISO con Z.

CREATE TABLE fin_accounts (
    id            TEXT PRIMARY KEY,                 -- acc_…
    name          TEXT NOT NULL,
    institution   TEXT NOT NULL,                    -- santander | trade_republic | …
    kind          TEXT NOT NULL CHECK (kind IN ('bank','broker','card','cash')),
    currency      TEXT NOT NULL DEFAULT 'EUR',
    external_ref  TEXT,                             -- sha256 del IBAN/identificador (nunca en claro)
    last4         TEXT,                             -- últimos 4 caracteres, para mostrar
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (institution, external_ref)
);

CREATE TABLE fin_imports (
    id             TEXT PRIMARY KEY,                -- imp_…
    account_id     TEXT NOT NULL REFERENCES fin_accounts(id),
    source         TEXT NOT NULL,                   -- p. ej. santander_xls
    file_name      TEXT NOT NULL,
    file_sha256    TEXT NOT NULL,
    period_from    TEXT,
    period_to      TEXT,
    rows_total     INTEGER NOT NULL,
    rows_new       INTEGER NOT NULL,
    rows_duplicate INTEGER NOT NULL,
    balance_end_cents INTEGER,
    actor          TEXT NOT NULL,
    imported_at    TEXT NOT NULL,
    UNIQUE (account_id, file_sha256)
);

CREATE TABLE fin_categories (
    slug  TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    kind  TEXT NOT NULL CHECK (kind IN ('expense','income','transfer'))
);

INSERT INTO fin_categories(slug, name, kind) VALUES
    ('supermercado',  'Supermercado',            'expense'),
    ('restaurantes',  'Restaurantes y cafés',    'expense'),
    ('transporte',    'Transporte',              'expense'),
    ('vivienda',      'Vivienda',                'expense'),
    ('suministros',   'Suministros',             'expense'),
    ('suscripciones', 'Suscripciones',           'expense'),
    ('salud',         'Salud',                   'expense'),
    ('ocio',          'Ocio',                    'expense'),
    ('compras',       'Compras',                 'expense'),
    ('educacion',     'Educación',               'expense'),
    ('comisiones',    'Comisiones e impuestos',  'expense'),
    ('efectivo',      'Retirada de efectivo',    'expense'),
    ('otros-gastos',  'Otros gastos',            'expense'),
    ('nomina',        'Nómina',                  'income'),
    ('otros-ingresos','Otros ingresos',          'income'),
    ('traspaso',      'Traspaso entre cuentas propias', 'transfer'),
    ('inversion',     'Aportación a inversión',  'transfer');

CREATE TABLE fin_rules (
    id          TEXT PRIMARY KEY,                   -- rul_…
    pattern     TEXT NOT NULL,                      -- texto (sin mayúsculas/acentos) o regex
    match_type  TEXT NOT NULL CHECK (match_type IN ('contains','regex')),
    direction   TEXT CHECK (direction IN ('debit','credit')),   -- NULL = ambos
    category    TEXT NOT NULL REFERENCES fin_categories(slug),
    priority    INTEGER NOT NULL DEFAULT 100,       -- menor = antes
    status      TEXT NOT NULL CHECK (status IN ('active','proposed','rejected')),
    reason      TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,                      -- actor (cli | mcp | system)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE fin_transactions (
    id                  TEXT PRIMARY KEY,           -- txn_…
    account_id          TEXT NOT NULL REFERENCES fin_accounts(id),
    import_id           TEXT NOT NULL REFERENCES fin_imports(id),
    booking_date        TEXT NOT NULL,              -- fecha de operación
    value_date          TEXT,
    description         TEXT NOT NULL,
    amount_cents        INTEGER NOT NULL,           -- negativo = cargo
    currency            TEXT NOT NULL DEFAULT 'EUR',
    balance_after_cents INTEGER,
    fingerprint         TEXT NOT NULL,
    category            TEXT REFERENCES fin_categories(slug),
    category_source     TEXT CHECK (category_source IN ('rule','manual')),
    rule_id             TEXT REFERENCES fin_rules(id),
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (account_id, fingerprint)
);
CREATE INDEX fin_transactions_date ON fin_transactions(account_id, booking_date);
CREATE INDEX fin_transactions_category ON fin_transactions(category);
