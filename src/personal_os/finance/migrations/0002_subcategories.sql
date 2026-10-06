-- Subcategorías: un nivel de jerarquía. Una subcategoría hereda el tipo de su padre.
ALTER TABLE fin_categories ADD COLUMN parent TEXT REFERENCES fin_categories(slug);

INSERT INTO fin_categories(slug, name, kind, parent) VALUES
    ('combustible',        'Combustible',             'expense', 'transporte'),
    ('vuelos',             'Vuelos',                  'expense', 'transporte'),
    ('taxi-vtc',           'Taxi y VTC',              'expense', 'transporte'),
    ('transporte-publico', 'Transporte público',      'expense', 'transporte'),
    ('parking-peajes',     'Parking y peajes',        'expense', 'transporte'),
    ('mantenimiento-coche','Mantenimiento del coche', 'expense', 'transporte'),
    ('alquiler',           'Alquiler',                'expense', 'vivienda'),
    ('hogar',              'Hogar',                   'expense', 'vivienda'),
    ('electricidad',       'Electricidad',            'expense', 'suministros'),
    ('agua',               'Agua',                    'expense', 'suministros'),
    ('telefono-internet',  'Teléfono e internet',     'expense', 'suministros'),
    ('alojamiento-viajes', 'Viajes y alojamiento',    'expense', 'ocio'),
    ('farmacia',           'Farmacia',                'expense', 'salud'),
    ('seguros-salud',      'Seguros de salud',        'expense', 'salud');
