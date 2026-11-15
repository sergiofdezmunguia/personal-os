-- Diario del satélite (ADR 0016): lo que dijo cada día, para evaluarlo fuera de muestra.
-- Solo datos de mercado y la decisión. Ningún importe en euros ni nada del patrimonio,
-- el efectivo o las posiciones del usuario.
CREATE TABLE fin_satellite_journal (
    day          TEXT PRIMARY KEY,      -- YYYY-MM-DD (zona del usuario) en que se apuntó
    recorded_at  TEXT NOT NULL,         -- instante UTC
    regime       TEXT NOT NULL CHECK (regime IN ('miedo', 'neutral', 'euforia', 'desconocido')),
    regime_day   TEXT,                  -- sesión de los datos del régimen
    indicators   TEXT NOT NULL,         -- JSON [{key, value, vote, text}]
    benchmark    TEXT NOT NULL          -- símbolo del índice de referencia de ese día
);

CREATE TABLE fin_satellite_journal_ideas (
    day        TEXT NOT NULL REFERENCES fin_satellite_journal(day),
    isin       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('comprar', 'vigilar', 'vender')),
    position   INTEGER NOT NULL,        -- orden en el panel (1 = la primera)
    top        INTEGER NOT NULL,        -- 1 si estaba en el top que se enseña y se avisa
    symbol     TEXT NOT NULL,
    name       TEXT NOT NULL,
    price      TEXT NOT NULL,           -- cierre en EUR (decimal exacto)
    price_day  TEXT NOT NULL,           -- sesión de ese cierre
    stop       TEXT,
    target     TEXT,
    trend      TEXT,                    -- JSON {short, medium, long}
    ranges     TEXT NOT NULL,           -- JSON [{h, cases, end_low, end_mid, end_high, drop, rise}] en %
    tranches   TEXT NOT NULL,           -- JSON [{price, label}] (sin importes)
    PRIMARY KEY (day, isin, kind)
);
