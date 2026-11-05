-- Precio de cierre por ISIN (ADR 0012): lo descarga `pos sync` de fuentes públicas (justETF,
-- Yahoo) para valorar la cartera a diario. Solo datos de mercado: nada personal.
CREATE TABLE fin_prices (
    isin        TEXT NOT NULL,
    price_date  TEXT NOT NULL,          -- YYYY-MM-DD
    price       TEXT NOT NULL,          -- decimal exacto, en `currency`
    currency    TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (isin, price_date)
);
