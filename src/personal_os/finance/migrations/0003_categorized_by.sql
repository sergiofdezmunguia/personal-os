-- Quién puso a mano la categoría: 'cli' (el usuario) o 'mcp' (Claude, en el cierre mensual).
-- Lo de Claude se puede revisar (`pos finance list --by-claude`) y el usuario siempre lo pisa;
-- Claude nunca toca lo que el usuario puso a mano.
ALTER TABLE fin_transactions ADD COLUMN categorized_by TEXT;
UPDATE fin_transactions SET categorized_by = 'cli' WHERE category_source = 'manual';
