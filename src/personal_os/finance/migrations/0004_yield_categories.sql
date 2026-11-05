-- El cierre mensual contrasta intereses y saveback por estas categorías: deben existir siempre.
INSERT OR IGNORE INTO fin_categories(slug, name, kind, parent) VALUES
    ('intereses',      'Intereses',                 'income', 'otros-ingresos'),
    ('bonificaciones', 'Bonificaciones y cashback', 'income', 'otros-ingresos');
