-- Cierres diarios de la lista de seguimiento del satélite y del índice de referencia
-- (ADR 0014), por símbolo de Yahoo. Solo datos de mercado públicos.
CREATE TABLE fin_market_prices (
    symbol      TEXT NOT NULL,
    price_date  TEXT NOT NULL,          -- YYYY-MM-DD
    close       TEXT NOT NULL,          -- decimal exacto
    currency    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, price_date)
);
