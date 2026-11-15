-- Propuestas de categoría del modelo aprendido (ADR 0017). No tocan fin_transactions: el
-- movimiento sigue sin categoría hasta que el usuario aprueba. Una fila por movimiento.
-- pending  = esperando revisión (se recalcula en cada `pos finance suggest`)
-- accepted = el usuario la aprobó (el movimiento queda con categoría manual del usuario)
-- rejected = el usuario la rechazó: nunca se vuelve a proponer para ese movimiento
-- superseded = el movimiento se categorizó por otra vía (a mano, reglas o Claude) antes de revisarla
CREATE TABLE fin_category_suggestions (
    transaction_id TEXT PRIMARY KEY REFERENCES fin_transactions(id) ON DELETE CASCADE,
    category       TEXT NOT NULL REFERENCES fin_categories(slug),
    confidence     REAL NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected','superseded')),
    model          TEXT NOT NULL DEFAULT '',           -- p. ej. «tfidf C=10 umbral=0.62 n=1540»
    created_at     TEXT NOT NULL,
    reviewed_at    TEXT,
    reviewed_by    TEXT
);
CREATE INDEX fin_category_suggestions_status ON fin_category_suggestions(status);
