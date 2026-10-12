-- La exportación CSV de TR pega «null» al final de algunas descripciones (compras de finales
-- de 2025). El parser ya lo quita; esto limpia lo importado antes. Las huellas de estos
-- movimientos dependen de su id del banco, no de la descripción, así que no cambian.
UPDATE fin_transactions
   SET description = rtrim(substr(description, 1, length(description) - 4))
 WHERE external_id IS NOT NULL AND description GLOB '*null';
