# -*- coding: utf-8 -*-
"""Productos propuestos desde la descripcion de un contenedor
(core/inventory_suggestions.py): interpretacion del texto (tabla de 45+
descripciones reales y de electronica), medidas, cantidades, confianza,
validacion, y alta masiva en una sola escritura (LabStorage.save_items_bulk)."""

import math

import pytest

from core import inventory_suggestions as sug
from core import storage as storage_module
from core.storage import LabStorage

ACTOR = "prof@uniminuto.edu.co"

# Los 3 contenedores reales del laboratorio, tal como estan registrados
# (incluidos los errores de digitacion "Contendor").
C1 = {
    "id": "2-1-01-00-000", "name": "Contenedor 1", "category": "Piezas Lego", "item_type": "master",
    "location": "Estantería- 2; Piso-1; Contenedor-1.", "status": "active", "quantity": 0,
    "description": "Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1 \nAdemás, Contiene un Caja con "
                   "Separadores de Fichas y Pieza lego Lisas de 2x2",
}
C2 = {
    "id": "2-1-02-00-000", "name": "Contendor 2", "category": "Piezas Lego", "item_type": "master",
    "location": "Estantería- 2; Piso-1; Contenedor-2.", "status": "active", "quantity": 0,
    "description": "Contiene Piezas Lego de Pines 6x2 - 6x1 - 4x1 - Bases de 1 pin \nAdemás, Contiene "
                   "una Caja con Puertas y Ventanas, Piezas lego Lisas de 3x2 - 4x2 - 6x2, Hay piezas con "
                   "Biseles 3x2 - 2x2 - 4x1 - 2x1 - 1x0",
}
C3 = {
    "id": "2-1-03-00-000", "name": "Contendor 3", "category": "Piezas Lego", "item_type": "master",
    "location": "Estantería- 2; Piso-1; Contenedor-3.", "status": "active", "quantity": 0,
    "description": "Contiene Piezas Lego de Pines 4x2 - 3x2 - 2x1.",
}
REAL = [C1, C2, C3]

# Los mismos contenedores tal como estan en la base real: con el signo × (no la letra x).
C1_DB = {**C1, "description": "Contiene Piezas Lego de Pines 4×2 - 2×2 - 2×1\nAdemás, Contiene un Caja con "
                              "Separadores de Fichas y Pieza lego Lisas de 2×2"}
C2_DB = {**C2, "description": "Contiene Piezas Lego de Pines 6×2 - 6×1 - 4×1 - Bases de 1 pin\nAdemás, Contiene "
                              "una Caja con Puertas y Ventanas, Piezas lego Lisas de 3×2 - 4×2 - 6×2, Hay piezas con "
                              "Biseles 3×2 - 2×2 - 4×1 - 2×1 - 1×0"}
C3_DB = {**C3, "description": "Contiene Piezas Lego de Pines 4×2 - 3×2 - 2×1."}
REAL_DB = [C1_DB, C2_DB, C3_DB]

# (codigo, nombre, caracteristica, crear por defecto)
EXPECTED = {
    "2-1-01-00-000": [
        ("2-1-01-01-000", "Pieza Lego con pines 4x2", "Con pines 4x2", True),
        ("2-1-01-02-000", "Pieza Lego con pines 2x2", "Con pines 2x2", True),
        ("2-1-01-03-000", "Pieza Lego con pines 2x1", "Con pines 2x1", True),
        ("2-1-01-04-000", "Caja con separadores de fichas", "Caja", True),
        ("2-1-01-05-000", "Pieza Lego lisa 2x2", "Lisa 2x2", True),
    ],
    "2-1-02-00-000": [
        ("2-1-02-01-000", "Pieza Lego con pines 6x2", "Con pines 6x2", True),
        ("2-1-02-02-000", "Pieza Lego con pines 6x1", "Con pines 6x1", True),
        ("2-1-02-03-000", "Pieza Lego con pines 4x1", "Con pines 4x1", True),
        ("2-1-02-04-000", "Base Lego de 1 pin", "1 pin", True),
        ("2-1-02-05-000", "Caja con puertas y ventanas", "Caja", True),
        ("2-1-02-06-000", "Pieza Lego lisa 3x2", "Lisa 3x2", True),
        ("2-1-02-07-000", "Pieza Lego lisa 4x2", "Lisa 4x2", True),
        ("2-1-02-08-000", "Pieza Lego lisa 6x2", "Lisa 6x2", True),
        ("2-1-02-09-000", "Pieza Lego con bisel 3x2", "Con bisel 3x2", True),
        ("2-1-02-10-000", "Pieza Lego con bisel 2x2", "Con bisel 2x2", True),
        ("2-1-02-11-000", "Pieza Lego con bisel 4x1", "Con bisel 4x1", True),
        ("2-1-02-12-000", "Pieza Lego con bisel 2x1", "Con bisel 2x1", True),
        ("2-1-02-13-000", "Pieza Lego con bisel 1x0", "Con bisel 1x0", False),
    ],
    "2-1-03-00-000": [
        ("2-1-03-01-000", "Pieza Lego con pines 4x2", "Con pines 4x2", True),
        ("2-1-03-02-000", "Pieza Lego con pines 3x2", "Con pines 3x2", True),
        ("2-1-03-03-000", "Pieza Lego con pines 2x1", "Con pines 2x1", True),
    ],
}


def _notes(proposal) -> str:
    return " ".join(proposal["notes"])


# ---------------------------------------------------------------------------
# Los 3 contenedores reales
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("container", REAL + REAL_DB, ids=lambda c: c["id"] + ("-x" if "x" in c["description"] else "-por"))
def test_real_containers_produce_the_expected_products(container):
    result = sug.suggest_products(container, REAL)
    got = [(p["code"], p["name"], p["feature"], p["selected"]) for p in result["proposals"]]
    assert got == EXPECTED[container["id"]]
    assert result["existing"] == [] and result["warnings"] == [] and result["unparsed"] == []
    assert result["container_name"] == container["name"]
    # La confianza decide que se marca por defecto: solo lo imposible (1x0) queda sin marcar.
    assert [(p["name"], p["confidence"]) for p in result["proposals"] if not p["selected"]] == [
        (p["name"], "baja") for p in result["proposals"] if p["confidence"] == "baja"
    ]
    assert all(p["confidence"] in ("alta", "media") for p in result["proposals"] if p["selected"])

    base = container["location"].rstrip(".")
    for proposal in result["proposals"]:
        box = proposal["code"].split("-")[3]
        assert proposal["item_type"] == "child"
        assert proposal["parent_id"] == container["id"]
        assert proposal["category"] == "Piezas Lego"
        assert proposal["unit"] == "unidad"
        assert proposal["quantity"] == 0 and proposal["pending_count"] is True
        assert proposal["min_stock_alert"] == 0
        assert proposal["location"] == f"{base}; Caja {box}."


def test_container_1_notes_flag_the_vague_box_and_the_y_coordination():
    proposals = {p["name"]: p for p in sug.suggest_products(C1, REAL)["proposals"]}
    box = proposals["Caja con separadores de fichas"]
    assert "«fichas» es un término genérico" in _notes(box)
    assert "Caja física" in _notes(box)
    smooth = proposals["Pieza Lego lisa 2x2"]
    assert "Venía unido con «y» a «Caja con separadores de fichas»" in _notes(smooth)
    assert all(not p["notes"] for name, p in proposals.items() if "pines" in name)
    assert smooth["source"] == "Pieza lego Lisas de 2x2"
    assert proposals["Pieza Lego con pines 2x2"]["source"] == "Piezas Lego de Pines … 2x2"


def test_container_2_flags_the_impossible_1x0_measure_instead_of_inventing_1x1():
    proposals = {p["name"]: p for p in sug.suggest_products(C2, REAL)["proposals"]}
    odd = proposals["Pieza Lego con bisel 1x0"]
    assert odd["selected"] is False
    assert "Revisar medida" in _notes(odd) and odd["confidence"] == "baja"
    assert "«1x0» tiene una dimensión 0" in _notes(odd) and "probablemente es «1x1»" in _notes(odd)
    assert "Pieza Lego con bisel 1x1" not in proposals
    # "Caja con Puertas y Ventanas" es UNA caja: la "y" no la parte en dos productos.
    assert "Caja con puertas y ventanas" in proposals and not any(n.startswith("Ventana") for n in proposals)
    assert proposals["Base Lego de 1 pin"]["source"] == "Bases de 1 pin"


def test_container_3_with_trailing_period():
    names = [p["name"] for p in sug.suggest_products(C3, [C3])["proposals"]]
    assert names == ["Pieza Lego con pines 4x2", "Pieza Lego con pines 3x2", "Pieza Lego con pines 2x1"]


# ---------------------------------------------------------------------------
# Interpretacion del texto (cualquier contenedor futuro)
# ---------------------------------------------------------------------------

def _names(text, context="Piezas Lego"):
    return [e["name"] for e in sug.parse_description(text, context=context)["entries"]]


@pytest.mark.parametrize("text", [
    "Piezas Lego de pines 4x2, 2x2 y 2x1",
    "piezas lego de pines 4 x 2 - 2 X 2 - 2×1",
    "PIEZAS LEGO DE PINES 4X2-2X2-2X1.",
    "Contiene: piezas Lego de pines 4x2; 2x2; 2x1",
    "Piezas Lego de pines:\n4x2\n2x2\n2x1",
    "Además, también hay unas piezas Lego con pines 4x2 – 2x2 — 2x1",
    "Piezas Lego con tetones 4x2 - 2x2 - 2x1",
])
def test_separators_case_and_accents_do_not_change_the_result(text):
    assert _names(text) == ["Pieza Lego con pines 4x2", "Pieza Lego con pines 2x2", "Pieza Lego con pines 2x1"]


def test_y_splits_products_but_not_the_contents_of_a_box():
    assert _names("Caja con puertas, ventanas y techos y Piezas lisas 2x2") == [
        "Caja con puertas, ventanas y techos", "Pieza Lego lisa 2x2",
    ]
    assert _names("Contiene: tornillos, tuercas y arandelas", context="") == ["Tornillo", "Tuerca", "Arandela"]


def test_brand_comes_from_the_text_or_the_container_and_is_optional():
    assert _names("Piezas con pines 2x2", context="") == ["Pieza con pines 2x2"]
    assert _names("Piezas con pines 2x2", context="Piezas Lego") == ["Pieza Lego con pines 2x2"]
    assert _names("Bases de 2 pines y vigas Technic 1x9", context="") == ["Base de 2 pines", "Viga Technic 1x9"]


def test_adjectives_are_singular_and_gender_agreed():
    assert _names("Bloques lisos 2x4 y placas transparentes 1x2", context="") == [
        "Bloque liso 2x4", "Placa transparente 1x2",
    ]
    assert _names("Piezas biseladas 2x2 y piezas curvas 1x4", context="") == [
        "Pieza con bisel 2x2", "Pieza curva 1x4",
    ]


def test_measure_before_the_subject_and_quantities_written_in_the_text():
    entries = sug.parse_description("20 piezas lisas 2x2, 10 de 2x4; 2x2 con pines")["entries"]
    assert [(e["name"], e["quantity"]) for e in entries] == [
        ("Pieza lisa 2x2", 20), ("Pieza lisa 2x4", 10), ("Pieza con pines 2x2", None),
    ]
    assert "Cantidad tomada de la descripción (20)." in entries[0]["notes"]


def test_repeated_products_are_proposed_once_with_a_warning():
    result = sug.parse_description("Pines 4x2 - 2x4\nPiezas con pines 4x2", context="Lego")
    assert [e["name"] for e in result["entries"]] == ["Pieza Lego con pines 4x2"]
    assert len(result["warnings"]) == 2
    assert "es la misma pieza que «Pieza Lego con pines 4x2»" in result["warnings"][0]
    assert "aparece más de una vez" in result["warnings"][1]


def test_a_measure_without_a_piece_type_is_flagged():
    entries = sug.parse_description("Contiene 2x2 y 4x2")["entries"]
    assert [e["name"] for e in entries] == ["Pieza 2x2", "Pieza 4x2"]
    assert all("no dice qué tipo de pieza" in " ".join(e["notes"]) for e in entries)


def test_free_products_without_measure_and_junk():
    entries = sug.parse_description("1. Sensores de color\n2. Motores grandes")["entries"]
    assert [(e["kind"], e["name"]) for e in entries] == [
        (sug.KIND_PRODUCT, "Sensor de color"), (sug.KIND_PRODUCT, "Motor grande"),
    ]
    result = sug.parse_description("Piezas lisas 2x2 - ### - 4x2")
    assert result["unparsed"] == ["###"]
    assert "No se pudo interpretar «###»" in result["warnings"][0]


@pytest.mark.parametrize("text", [None, "", "   ", "Además,", "Contiene:", "\n\n."])
def test_empty_descriptions_propose_nothing(text):
    assert sug.parse_description(text) == {"entries": [], "warnings": [], "unparsed": []}


def test_box_names_keep_acronyms_and_brands():
    assert _names("una bolsa con sensores EV3 y cables USB de lego", context="") == [
        "Bolsa con sensores EV3 y cables USB de Lego",
    ]


@pytest.mark.parametrize("word, expected", [
    ("Piezas", "pieza"), ("pines", "pin"), ("Biseles", "bisel"), ("separadores", "separador"),
    ("bases", "base"), ("Lisas", "lisa"), ("botones", "botón"), ("cables", "cable"), ("torres", "torre"),
    ("verdes", "verde"), ("ejes", "eje"), ("tetones", "tetón"), ("gris", "gris"), ("tres", "tres"),
    ("luces", "luz"), ("pieza", "pieza"), ("4x2", "4x2"),
])
def test_singular(word, expected):
    assert sug.singular(word) == expected


def test_product_key_ignores_form_but_not_meaning():
    key = sug.product_key
    assert key("Pieza Lego con pines 4x2") == key("Piezas de Pines 2 x 4") == key("pines 4X2")
    assert key("Pieza Lego lisa 2x2") == key("Lisas 2x2")
    assert key("Pieza Lego lisa 2x2") != key("Pieza Lego con bisel 2x2")
    assert key("Pieza Lego con pines 2x2") != key("Pieza Lego con pines 2x1")
    assert key("Base Lego de 1 pin") == key("Bases de 1 pin")


# ---------------------------------------------------------------------------
# Tabla de descripciones realistas (Lego y electronica): lo que debe proponerse
# ---------------------------------------------------------------------------

# (id, descripcion, contexto, [(nombre, caracteristica, cantidad, confianza)], avisos)
DESCRIPTIONS = [
    ("lego_lista_y", 'Contiene piezas Lego de pines 4x2, 2x2 y 2x1', '', [
        ('Pieza Lego con pines 4x2', 'Con pines 4x2', None, "alta"),
        ('Pieza Lego con pines 2x2', 'Con pines 2x2', None, "alta"),
        ('Pieza Lego con pines 2x1', 'Con pines 2x1', None, "alta"),
    ], 0),
    ("lego_mayusculas", 'PIEZAS LEGO DE PINES 4X2-2X2-2X1.', '', [
        ('Pieza Lego con pines 4x2', 'Con pines 4x2', None, "alta"),
        ('Pieza Lego con pines 2x2', 'Con pines 2x2', None, "alta"),
        ('Pieza Lego con pines 2x1', 'Con pines 2x1', None, "alta"),
    ], 0),
    ("lego_por_by_barra", 'Piezas lisas 2 por 2, 4 by 2 / 6*2', '', [
        ('Pieza lisa 2x2', 'Lisa 2x2', None, "alta"),
        ('Pieza lisa 4x2', 'Lisa 4x2', None, "alta"),
        ('Pieza lisa 6x2', 'Lisa 6x2', None, "alta"),
    ], 0),
    ("lego_encabezado", 'Piezas de pines:\n4x2\n2x2\n2x1', 'Piezas Lego', [
        ('Pieza Lego con pines 4x2', 'Con pines 4x2', None, "alta"),
        ('Pieza Lego con pines 2x2', 'Con pines 2x2', None, "alta"),
        ('Pieza Lego con pines 2x1', 'Con pines 2x1', None, "alta"),
    ], 0),
    ("lego_cantidades", '20 piezas de 2x2, 10 uds de 2x4 y x5 de 1x1', '', [
        ('Pieza 2x2', '2x2', 20, "media"),
        ('Pieza 2x4', '2x4', 10, "alta"),
        ('Pieza 1x1', '1x1', 5, "alta"),
    ], 0),
    ("lego_aproximado", 'Contiene 50 piezas lisas 2x2 (aprox.), 30 pzs de 1x4', '', [
        ('Pieza lisa 2x2', 'Lisa 2x2', 50, "alta"),
        ('Pieza 1x4', '1x4', 30, "media"),
    ], 0),
    ("lego_colores", 'Piezas rojas de 2x2 x10 y azules de 2x4 (15)', 'Piezas Lego', [
        ('Pieza Lego roja 2x2', 'Roja 2x2', 10, "alta"),
        ('Pieza Lego azul 2x4', 'Azul 2x4', 15, "alta"),
    ], 0),
    ("lego_bloques", 'Hay unos 15 bloques rojos 2x4 y 10 bloques azules', '', [
        ('Bloque rojo 2x4', 'Rojo 2x4', 15, "alta"),
        ('Bloque azul', 'Azul', 10, "alta"),
    ], 0),
    ("lego_contendor", 'Contendor con piezas azules 2x4 x10', 'Piezas Lego', [
        ('Pieza Lego azul 2x4', 'Azul 2x4', 10, "alta"),
    ], 0),
    ("lego_erratas", 'piesas lisas de 3x2 - 4x2 y bicel 2x2', '', [
        ('Pieza lisa 3x2', 'Lisa 3x2', None, "alta"),
        ('Pieza lisa 4x2', 'Lisa 4x2', None, "alta"),
        ('Pieza con bisel 2x2', 'Con bisel 2x2', None, "alta"),
    ], 0),
    ("lego_medidas_imposibles", 'Piezas de 100x2 - 0x4 - 3x0 - 48x2', '', [
        ('Pieza 100x2', '100x2', None, "baja"),
        ('Pieza 0x4', '0x4', None, "baja"),
        ('Pieza 3x0', '3x0', None, "baja"),
        ('Pieza 48x2', '48x2', None, "alta"),
    ], 0),
    ("lego_placas", 'Placa base 32x32 y placa 48x48 y 64x64', '', [
        ('Placa base 32x32', 'Base 32x32', None, "alta"),
        ('Placa 48x48', '48x48', None, "alta"),
        ('Placa 64x64', '64x64', None, "baja"),
    ], 0),
    ("lego_bases_technic", 'Bases de 1 pin, bases de 2 pines y vigas Technic 1x9', 'Piezas Lego', [
        ('Base Lego de 1 pin', '1 pin', None, "alta"),
        ('Base Lego de 2 pines', '2 pines', None, "alta"),
        ('Viga Lego Technic 1x9', '1x9', None, "alta"),
    ], 0),
    ("lego_ambiguo_o", '2x2, 4x2 o 6x2', '', [
        ('Pieza 2x2', '2x2', None, "baja"),
        ('Pieza 4x2', '4x2', None, "baja"),
        ('Pieza 6x2', '6x2', None, "baja"),
    ], 0),
    ("caja_contenido", 'Caja con tornillos, tuercas y arandelas', '', [
        ('Caja con tornillos, tuercas y arandelas', 'Caja', None, "alta"),
    ], 0),
    ("caja_contiene", 'Caja con separadores que contiene fichas y piezas 2x2', '', [
        ('Caja con separadores', 'Caja', None, "alta"),
        ('Ficha', '', None, "media"),
        ('Pieza 2x2', '2x2', None, "media"),
    ], 0),
    ("caja_dos_puntos", 'Caja de herramientas: martillo, alicate y destornilladores x3', '', [
        ('Caja de herramientas', 'Caja', None, "alta"),
        ('Martillo', '', None, "alta"),
        ('Alicate', '', None, "alta"),
        ('Destornillador', '', 3, "alta"),
    ], 0),
    ("caja_medidas_y_bases", 'Una caja con piezas lisas 2x2 y 4x2 y bases de 2 pines', '', [
        ('Caja con piezas lisas 2x2 y 4x2', 'Caja', None, "alta"),
        ('Base de 2 pines', '2 pines', None, "media"),
    ], 0),
    ("caja_cantidad", '2 cajas con piezas lisas', '', [
        ('Caja con piezas lisas', 'Caja', 2, "alta"),
    ], 0),
    ("bolsa_siglas", 'Bolsa con 3 sensores EV3 y cables USB de lego', '', [
        ('Bolsa con 3 sensores EV3 y cables USB de Lego', 'Caja', None, "alta"),
    ], 0),
    ("elec_enunciado", 'resistencias de 220 ohm x50, condensadores 10uF, cables jumper macho-hembra 20 und', '', [
        ('Resistencia de 220 Ω', '220 Ω', 50, "alta"),
        ('Condensador de 10 µF', '10 µF', None, "alta"),
        ('Cable jumper macho-hembra', 'Jumper macho-hembra', 20, "alta"),
    ], 0),
    ("elec_kit_arduino", 'Kit Arduino UNO con 3 sensores ultrasónicos', '', [
        ('Kit Arduino UNO con 3 sensores ultrasónicos', 'Arduino UNO', None, "alta"),
    ], 0),
    ("elec_lista_unidad", 'resistencias de 220, 330 y 470 ohm; condensadores 10uF, 100nF, 1000 µF 25V', '', [
        ('Resistencia de 220 Ω', '220 Ω', None, "alta"),
        ('Resistencia de 330 Ω', '330 Ω', None, "alta"),
        ('Resistencia de 470 Ω', '470 Ω', None, "alta"),
        ('Condensador de 10 µF', '10 µF', None, "alta"),
        ('Condensador de 100 nF', '100 nF', None, "alta"),
        ('Condensador de 1000 µF 25 V', '1000 µF 25 V', None, "alta"),
    ], 0),
    ("elec_kilo", 'resistencias de 4.7k y 10k, potenciómetros de 10 kΩ x3', '', [
        ('Resistencia de 4.7 kΩ', '4.7 kΩ', None, "alta"),
        ('Resistencia de 10 kΩ', '10 kΩ', None, "alta"),
        ('Potenciómetro de 10 kΩ', '10 kΩ', 3, "alta"),
    ], 0),
    ("elec_leds", 'LEDs rojos de 5 mm x100, leds verdes 3mm (50)', '', [
        ('LED rojo de 5 mm', 'Rojo 5 mm', 100, "alta"),
        ('LED verde de 3 mm', 'Verde 3 mm', 50, "alta"),
    ], 0),
    ("elec_dupont", 'Cables dupont macho-macho 20cm x40, cables macho-hembra 30 cm', '', [
        ('Cable dupont macho-macho de 20 cm', 'Dupont macho-macho 20 cm', 40, "alta"),
        ('Cable macho-hembra de 30 cm', 'Macho-hembra 30 cm', None, "alta"),
    ], 0),
    ("elec_placas", 'Protoboards de 830 puntos, 2 arduino UNO, Arduino Nano x3', '', [
        ('Protoboard de 830 puntos', '', None, "alta"),
        ('Arduino UNO', 'UNO', 2, "alta"),
        ('Arduino Nano', 'Nano', 3, "alta"),
    ], 0),
    ("elec_servos", 'Servomotores SG90 x10; motores DC 3-6V; sensores HC-SR04 (ultrasónicos) x5', '', [
        ('Servomotor SG90', 'SG90', 10, "alta"),
        ('Motor DC de 3-6 V', 'DC 3-6 V', None, "alta"),
        ('Sensor HC-SR04', 'HC-SR04', 5, "alta"),
    ], 0),
    ("elec_tornilleria", 'Tornillos M3x10 x200, tuercas M3 x200 y arandelas M3', '', [
        ('Tornillo M3x10', 'M3x10', 200, "alta"),
        ('Tuerca M3', 'M3', 200, "alta"),
        ('Arandela M3', 'M3', None, "alta"),
    ], 0),
    ("elec_pilas", 'Baterías de 9V x4, pilas AA x12, pilas AAA x8', '', [
        ('Batería de 9 V', '9 V', 4, "alta"),
        ('Pila AA', 'AA', 12, "alta"),
        ('Pila AAA', 'AAA', 8, "alta"),
    ], 0),
    ("elec_fuentes", 'Fuente de 12V 2A, adaptadores de 5V 1A', '', [
        ('Fuente de 12 V 2 A', '12 V 2 A', None, "alta"),
        ('Adaptador de 5 V 1 A', '5 V 1 A', None, "alta"),
    ], 0),
    ("elec_vatios", 'Resistencias de 1/4 W de 220 ohm x50', '', [
        ('Resistencia de 220 Ω 1/4 W', '220 Ω 1/4 W', 50, "alta"),
    ], 0),
    ("elec_etc", 'Condensadores electrolíticos de 100uF/25V; condensadores cerámicos 0.1uF etc.', '', [
        ('Condensador electrolítico de 100 µF 25 V', 'Electrolítico 100 µF 25 V', None, "alta"),
        ('Condensador cerámico de 0.1 µF', 'Cerámico 0.1 µF', None, "alta"),
    ], 1),
    ("elec_herramientas", 'Multímetro digital, osciloscopio, soldador de estaño 60W y pinzas', '', [
        ('Multímetro digital', 'Digital', None, "alta"),
        ('Osciloscopio', '', None, "alta"),
        ('Soldador de estaño de 60 W', 'De estaño 60 W', None, "alta"),
        ('Pinza', '', None, "alta"),
    ], 0),
    ("elec_ev3", 'Contiene: 5 sensores de color, 3 motores medianos y 1 ladrillo inteligente EV3', '', [
        ('Sensor de color', '', 5, "alta"),
        ('Motor mediano', 'Mediano', 3, "alta"),
        ('Ladrillo inteligente EV3', 'Inteligente EV3', 1, "alta"),
    ], 0),
    ("elec_kit_incluye", 'Kit de Arduino UNO que incluye cables, protoboard y 5 leds', '', [
        ('Kit de Arduino UNO', '', None, "alta"),
        ('Cable', '', None, "alta"),
        ('Protoboard', '', None, "alta"),
        ('LED', '', 5, "alta"),
    ], 0),
    ("elec_cantidad_guion", 'Resistencias 220 ohm - 50 uds', '', [
        ('Resistencia de 220 Ω', '220 Ω', 50, "alta"),
    ], 0),
    ("elec_longitudes", 'Cables jumper de 10 cm, 20 cm y 30 cm', '', [
        ('Cable jumper de 10 cm', 'Jumper 10 cm', None, "alta"),
        ('Cable jumper de 20 cm', 'Jumper 20 cm', None, "alta"),
        ('Cable jumper de 30 cm', 'Jumper 30 cm', None, "alta"),
    ], 0),
    ("elec_palabras_unidad", 'Condensadores de 100 nanofaradios y 2 microfaradios', '', [
        ('Condensador de 100 nF', '100 nF', None, "alta"),
        ('Condensador de 2 µF', '2 µF', None, "alta"),
    ], 0),
    ("elec_sensores", 'Sensores de color, sensor ultrasónico, sensores de tacto (3 unidades)', '', [
        ('Sensor de color', '', None, "alta"),
        ('Sensor ultrasónico', 'Ultrasónico', None, "alta"),
        ('Sensor de tacto', '', 3, "alta"),
    ], 0),
    ("elec_tornillo_acero", 'Tornillos de acero inoxidable 3x10 mm x100', '', [
        ('Tornillo de acero inoxidable 3x10 mm', 'De acero inoxidable 3x10 mm', 100, "alta"),
    ], 0),
    ("lista_vinetas", '- Piezas con pines 2x2\n- Piezas lisas 4x2\n- Bisagras', '', [
        ('Pieza con pines 2x2', 'Con pines 2x2', None, "alta"),
        ('Pieza lisa 4x2', 'Lisa 4x2', None, "alta"),
        ('Bisagra', '', None, "alta"),
    ], 0),
    ("lista_numerada", '1) Pieza lisa 2x2\n2) Pieza lisa 4x2', '', [
        ('Pieza lisa 2x2', 'Lisa 2x2', None, "alta"),
        ('Pieza lisa 4x2', 'Lisa 4x2', None, "alta"),
    ], 0),
    ("motores_genero", 'Motores grandes, motor mediano y 2 motores pequeños', '', [
        ('Motor grande', 'Grande', None, "alta"),
        ('Motor mediano', 'Mediano', None, "alta"),
        ('Motor pequeño', 'Pequeño', 2, "alta"),
    ], 0),
    ("cantidad_suelta", 'Pines 4x2 - 20 uds, 2x2 - 15', '', [
        ('Pieza con pines 4x2', 'Con pines 4x2', 20, "alta"),
        ('Pieza con pines 2x2', 'Con pines 2x2', 15, "media"),
    ], 0),
    ("pilas_lineas", 'Pila 9V\nPila AA', '', [
        ('Pila de 9 V', '9 V', None, "alta"),
        ('Pila AA', 'AA', None, "alta"),
    ], 0),
    ("generico", 'Contiene fichas, cosas varias y otros', '', [
        ('Ficha', '', None, "media"),
        ('Cosas', '', None, "media"),
    ], 1),
    ("ruido", '###\n--\nx5', '', [
    ], 2),
]


def test_the_table_covers_at_least_30_descriptions():
    assert len(DESCRIPTIONS) >= 30
    assert len({case[0] for case in DESCRIPTIONS}) == len(DESCRIPTIONS)


@pytest.mark.parametrize("case_id, text, context, expected, warnings", DESCRIPTIONS,
                         ids=[case[0] for case in DESCRIPTIONS])
def test_description_table(case_id, text, context, expected, warnings):
    result = sug.parse_description(text, context=context)
    got = [(e["name"], e["feature"], e["quantity"], e["confidence"]) for e in result["entries"]]
    assert got == expected
    assert len(result["warnings"]) == warnings
    for entry in result["entries"]:
        # Solo lo de confianza baja queda sin marcar, y toda duda trae su explicacion.
        assert entry["selected"] == (entry["confidence"] != "baja")
        if entry["confidence"] != "alta":
            assert entry["notes"], entry
        assert entry["name"] == entry["name"].strip() and "  " not in entry["name"]
        assert entry["source"].strip()
    assert sug.parse_description(text, context=context) == result        # determinista


# ---------------------------------------------------------------------------
# Lectura robusta: signos, separadores, plurales, errores de digitacion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Piezas lisas 2x2, 2x4, 2x6",
    "Piezas lisas 2×2, 2×4, 2×6",
    "piezas lisas 2X2 - 2X4 - 2X6",
    "Piezas lisas 2 x 2; 2 x 4; 2 x 6",
    "PIEZAS LISAS 2*2 / 2*4 / 2*6",
    "Piezas lisas 2 por 2, 2 por 4 y 2 por 6",
    "Piezas lisas 2 by 2 - 2 by 4 - 2 by 6",
    "Piezas lisas: 2x2\n2x4\n2x6",
    "Piezas lisas de 2x2, de 2x4 y de 2x6",
    "Piezas lisas 2x2,2x4,2x6",
    "Piezas lisas 2x2-2x4-2x6",
    "Piezas lisas 2x2/2x4/2x6",
    "Piezas lisas 2x2 • 2x4 • 2x6",
    "  - Piezas lisas   2x2 \n  - 2x4 \n  - 2x6 .",
    "Pieza lisa 2x2 + 2x4 + 2x6",
    "Piezas lisas (2x2, 2x4 y 2x6)",
])
def test_every_way_of_writing_a_list_of_sizes_gives_the_same_products(text):
    assert _names(text, context="") == ["Pieza lisa 2x2", "Pieza lisa 2x4", "Pieza lisa 2x6"]


@pytest.mark.parametrize("text, expected", [
    ("Piezas LISAS 2x2", "Pieza lisa 2x2"), ("pieza liza 2x2", "Pieza lisa 2x2"),
    ("Piesas lisas 2x2", "Pieza lisa 2x2"), ("Bloques lisos 2x2", "Bloque liso 2x2"),
    ("Piezas biseladas 2x2", "Pieza con bisel 2x2"), ("Piezas con biseles 2x2", "Pieza con bisel 2x2"),
    ("Piezas con bicel 2x2", "Pieza con bisel 2x2"), ("Pieza con tetones 2x2", "Pieza con pines 2x2"),
    ("Piezas de pines 2x2", "Pieza con pines 2x2"), ("Piezas Lego 2x2", "Pieza Lego 2x2"),
    ("piezas legos lisas 2x2", "Pieza Lego lisa 2x2"), ("Bloques rojos 2x2", "Bloque rojo 2x2"),
    ("Placas rojas 2x2", "Placa roja 2x2"), ("Piezas azules 2x2", "Pieza azul 2x2"),
    ("Ladrillos amarillos 2x2", "Ladrillo amarillo 2x2"), ("Placas transparentes 2x2", "Placa transparente 2x2"),
])
def test_plurals_gender_case_and_typos_are_normalized(text, expected):
    assert _names(text, context="") == [expected]


@pytest.mark.parametrize("text", [
    "Contiene Piezas lisas 2x2", "contendor 2 contiene piezas lisas 2x2", "Contenedor con piezas lisas 2x2",
    "Además, contiene unas piezas lisas 2x2", "Hay piezas lisas 2x2", "Tiene varias piezas lisas 2x2",
    "Se encuentran piezas lisas 2x2", "Contien piezas lisas 2x2", "Incluye unas piezas lisas 2x2",
    "Dentro hay piezas lisas 2x2",
])
def test_leading_phrases_and_their_typos_are_ignored(text):
    assert _names(text, context="") == ["Pieza lisa 2x2"]


@pytest.mark.parametrize("text, quantity", [
    ("20 piezas lisas 2x2", 20), ("20 pzs lisas 2x2", 20), ("Piezas lisas 2x2 x20", 20), ("Piezas lisas 2x2 X20", 20),
    ("Piezas lisas 2x2 ×20", 20), ("Piezas lisas 2x2 (20)", 20), ("Piezas lisas 2x2 (x20)", 20),
    ("Piezas lisas 2x2 (20 uds)", 20), ("Piezas lisas 2x2 - 20 unidades", 20), ("Piezas lisas 2x2: 20 und",
                                                                              20),
    ("Piezas lisas 2x2, 20 pzas", 20), ("20 de piezas lisas 2x2", 20), ("x20 piezas lisas 2x2", 20),
    ("Cantidad: 20 piezas lisas 2x2", 20), ("Piezas lisas 2x2 cant. 20", 20), ("Piezas lisas 2x2", None),
    ("Piezas lisas 2x2 - 20", 20),
])
def test_quantity_written_in_any_of_its_forms(text, quantity):
    entries = sug.parse_description(text)["entries"]
    assert [e["name"] for e in entries] == ["Pieza lisa 2x2"]
    assert entries[0]["quantity"] == quantity


def test_a_quantity_never_swallows_a_measure_or_a_value():
    # "2x4 x10": medida 2x4 con cantidad 10; "4 x 2" es una medida; "220 ohm" no es "220 unidades".
    [piece] = sug.parse_description("Piezas lisas 2x4 x10")["entries"]
    assert (piece["name"], piece["quantity"]) == ("Pieza lisa 2x4", 10)
    [piece] = sug.parse_description("Piezas lisas 4 x 2")["entries"]
    assert (piece["name"], piece["quantity"]) == ("Pieza lisa 4x2", None)
    [piece] = sug.parse_description("220 ohm resistencias")["entries"]
    assert (piece["name"], piece["quantity"]) == ("Resistencia de 220 Ω", None)
    [piece] = sug.parse_description("Resistencias 10k 20 uds")["entries"]
    assert (piece["name"], piece["quantity"]) == ("Resistencia de 10 kΩ", 20)


def test_two_different_quantities_in_one_part_are_flagged():
    [piece] = sug.parse_description("20 piezas rojas de 2x2 (15)")["entries"]
    assert piece["quantity"] == 20 and piece["confidence"] == "media"
    assert "más de una cantidad (20 y 15)" in " ".join(piece["notes"])


def test_approximate_and_absurd_quantities():
    [piece] = sug.parse_description("Hay aprox. 30 piezas lisas 2x2")["entries"]
    assert piece["quantity"] == 30 and "Es aproximada" in " ".join(piece["notes"])
    [piece] = sug.parse_description("Hay como 30 piezas lisas 2x2")["entries"]
    assert piece["quantity"] == 30 and "Es aproximada" in " ".join(piece["notes"])
    [piece] = sug.parse_description("99999999999 piezas lisas 2x2")["entries"]
    assert piece["quantity"] is None and piece["confidence"] == "baja" and not piece["selected"]
    assert "demasiado grande" in " ".join(piece["notes"])


def test_a_bare_number_after_a_product_is_taken_as_its_quantity_with_a_warning_note():
    entries = sug.parse_description("Pines 4x2 - 20 uds, 2x2 - 15")["entries"]
    assert [(e["name"], e["quantity"], e["confidence"]) for e in entries] == [
        ("Pieza con pines 4x2", 20, "alta"), ("Pieza con pines 2x2", 15, "media"),
    ]
    assert "Se tomó «15» como la cantidad" in " ".join(entries[1]["notes"])


# ---------------------------------------------------------------------------
# Valores con unidad (electronica) y medidas
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("resistencias de 220 ohm", "Resistencia de 220 Ω"), ("Resistencia 220Ω", "Resistencia de 220 Ω"),
    ("resistencias 220 Ohms", "Resistencia de 220 Ω"), ("resistencias de 220 ohmios", "Resistencia de 220 Ω"),
    ("Resistencia de 4,7 kohm", "Resistencia de 4.7 kΩ"), ("resistencias de 4k7", "Resistencia de 4.7 kΩ"),
    ("resistencias de 10K", "Resistencia de 10 kΩ"), ("resistencias de 1 megaohm", "Resistencia de 1 MΩ"),
    ("resistencias de 2 kilohmios", "Resistencia de 2 kΩ"), ("resistencia 0.25 W 100 ohm", "Resistencia de 100 Ω 0.25 W"),
    ("condensadores 10uF", "Condensador de 10 µF"), ("condensadores 10 UF", "Condensador de 10 µF"),
    ("condensadores de 10 µF", "Condensador de 10 µF"), ("condensadores de 10 microfaradios", "Condensador de 10 µF"),
    ("capacitores de 100nF", "Condensador de 100 nF"), ("condensadores 22 pF", "Condensador de 22 pF"),
    ("condensadores 100uF 25V", "Condensador de 100 µF 25 V"), ("bobinas de 10 mH", "Bobina de 10 mH"),
    ("pilas de 9V", "Pila de 9 V"), ("baterías de 3,7 voltios", "Batería de 3.7 V"),
    ("fuentes de 12 V 2 A", "Fuente de 12 V 2 A"), ("baterías 2000 mAh", "Batería de 2000 mAh"),
    ("sensores de 3 a 5 V", "Sensor de 3-5 V"), ("sensores 3-5V", "Sensor de 3-5 V"),
    ("cables de 20 cm", "Cable de 20 cm"), ("cables de 2 metros", "Cable de 2 m"), ("cables de 1.5 mm", "Cable de 1.5 mm"),
    ("cristales de 16 MHz", "Cristal de 16 MHz"), ("tubos de 500 ml", "Tubo de 500 ml"),
    ("resistencias de 1k ohm", "Resistencia de 1 kΩ"), ("resistencias de 220 ohm 5%", "Resistencia de 220 Ω 5%"),
    ("servos de 180 grados", "Servo de 180 grados"),
])
def test_values_with_unit_keep_their_value_and_get_a_canonical_symbol(text, expected):
    assert _names(text, context="") == [expected]


def test_a_list_with_a_shared_unit_is_spread_but_fractions_are_not():
    assert _names("resistencias de 100, 220 y 470 ohm", context="") == [
        "Resistencia de 100 Ω", "Resistencia de 220 Ω", "Resistencia de 470 Ω"]
    assert _names("condensadores de 1 - 10 - 100 uF", context="") == [
        "Condensador de 1 µF", "Condensador de 10 µF", "Condensador de 100 µF"]
    assert _names("bases de 1, 2 y 3 pines", context="") == ["Base de 1 pin", "Base de 2 pines", "Base de 3 pines"]
    assert _names("Resistencias de 1/4 W", context="") == ["Resistencia de 1/4 W"]
    assert _names("Resistencias de 1/4 W, 1/2 W", context="") == ["Resistencia de 1/4 W", "Resistencia de 1/2 W"]


def test_a_unit_alone_infers_the_component_with_a_note_and_a_bare_k_needs_a_resistor():
    [piece] = sug.parse_description("220 ohm")["entries"]
    assert piece["name"] == "Resistencia de 220 Ω" and piece["confidence"] == "media"
    assert "se asumió «Resistencia»" in " ".join(piece["notes"])
    [piece] = sug.parse_description("10uF")["entries"]
    assert piece["name"] == "Condensador de 10 µF" and piece["confidence"] == "media"
    # "10k" suelto no es una medida: sin resistencias en la frase, queda como esta.
    assert _names("Tornillos de 10k", context="") == ["Tornillo de 10k"]
    assert _names("Cables 30 cm", context="") == ["Cable de 30 cm"]
    assert _names("Ya 2 a casa", context="") != []        # "2 a" es una preposicion, no amperios


def test_dimensions_with_a_unit_are_not_checked_against_the_piece_limit():
    assert _names("Tableros de 100x200 cm", context="") == ["Tablero 100x200 cm"]
    [odd] = sug.parse_description("Piezas de 100x200")["entries"]
    assert odd["confidence"] == "baja" and not odd["selected"]


@pytest.mark.parametrize("text, name, guess", [
    ("Piezas lisas 1x0", "Pieza lisa 1x0", "1x1"), ("Piezas lisas 0x4", "Pieza lisa 0x4", "1x4"),
    ("Piezas lisas 0×0", "Pieza lisa 0x0", "1x1"), ("Piezas lisas 2x0x1", "Pieza lisa 2x0x1", "2x1x1"),
    ("Bases de 0 pines", "Base de 0 pines", "de 1 pin"),
])
def test_impossible_zero_measures_are_flagged_unselected_and_never_corrected_silently(text, name, guess):
    [piece] = sug.parse_description(text)["entries"]
    assert piece["name"] == name                               # no se inventa "1x1"
    assert piece["confidence"] == "baja" and piece["selected"] is False
    assert "Revisar medida" in piece["notes"][0] and f"probablemente es «{guess}»" in piece["notes"][0]


@pytest.mark.parametrize("text, flagged", [
    ("Piezas lisas 48x2", False), ("Piezas lisas 49x2", True), ("Piezas lisas 64x64", True),
    ("Piezas lisas 2x2x60", True), ("Piezas lisas 100x2", True), ("Piezas lisas 32x32", False),
])
def test_dimensions_above_48_are_flagged(text, flagged):
    [piece] = sug.parse_description(text)["entries"]
    assert (piece["confidence"] == "baja") is flagged and piece["selected"] is (not flagged)
    if flagged:
        assert "supera 48" in " ".join(piece["notes"])


def test_ranges_are_flagged_because_only_the_named_sizes_are_proposed():
    entries = sug.parse_description("Piezas lisas de 2x2 a 2x6")["entries"]
    assert [e["name"] for e in entries] == ["Pieza lisa 2x2", "Pieza lisa 2x6"]
    assert all(e["confidence"] == "media" and "rango" in " ".join(e["notes"]) for e in entries)


# ---------------------------------------------------------------------------
# Cajas, kits y lo que contienen
# ---------------------------------------------------------------------------

def test_a_box_keeps_its_contents_in_its_name_and_is_one_product():
    entries = sug.parse_description("Una caja con puertas, ventanas y techos")["entries"]
    assert [(e["kind"], e["name"], e["feature"]) for e in entries] == [
        (sug.KIND_BOX, "Caja con puertas, ventanas y techos", "Caja")]
    assert _names("Cajas con tornillos M3", context="") == ["Caja con tornillos M3"]
    assert _names("CAJA CON SEPARADORES DE FICHAS", context="") == ["Caja con separadores de fichas"]
    assert [e["quantity"] for e in sug.parse_description("3 cajas con tornillos")["entries"]] == [3]


@pytest.mark.parametrize("text", [
    "Caja con separadores que contiene fichas y piezas 2x2",
    "Caja con separadores, que contiene fichas y piezas 2x2",
    "Caja con separadores donde hay fichas y piezas 2x2",
    "Caja con separadores: fichas y piezas 2x2",
])
def test_what_a_box_contains_is_proposed_inside_it(text):
    entries = sug.parse_description(text, context="")["entries"]
    assert [(e["kind"], e["name"], e["inside"]) for e in entries] == [
        (sug.KIND_BOX, "Caja con separadores", ""),
        (sug.KIND_PRODUCT, "Ficha", "Caja con separadores"),
        (sug.KIND_PIECE, "Pieza 2x2", "Caja con separadores"),
    ]
    assert all("está dentro de «Caja con separadores»" in " ".join(e["notes"]) for e in entries[1:])
    assert "Caja física" in " ".join(entries[0]["notes"])


def test_contents_stay_inside_only_until_the_end_of_the_sentence():
    entries = sug.parse_description("Caja de herramientas que contiene martillos y alicates.\nPinzas", context="")["entries"]
    assert [(e["name"], e["inside"]) for e in entries] == [
        ("Caja de herramientas", ""), ("Martillo", "Caja de herramientas"), ("Alicate", "Caja de herramientas"),
        ("Pinza", ""),
    ]


def test_a_kit_with_a_contents_verb_lists_its_parts_but_a_plain_kit_is_one_product():
    entries = sug.parse_description("Kit de Arduino UNO que incluye cables, protoboard y 5 leds")["entries"]
    assert [(e["name"], e["quantity"], e["inside"]) for e in entries] == [
        ("Kit de Arduino UNO", None, ""), ("Cable", None, "Kit de Arduino UNO"),
        ("Protoboard", None, "Kit de Arduino UNO"), ("LED", 5, "Kit de Arduino UNO"),
    ]
    [kit] = sug.parse_description("Kit Arduino UNO con 3 sensores ultrasónicos")["entries"]
    assert kit["name"] == "Kit Arduino UNO con 3 sensores ultrasónicos" and kit["quantity"] is None
    assert kit["inside"] == "" and kit["confidence"] == "alta"
    # "Sensor que tiene 3 pines" no es un contenedor con productos dentro.
    assert _names("Sensor que tiene 3 pines", context="") == ["Sensor de 3 pines"]


def test_the_measure_after_a_box_list_continues_the_box_but_a_new_piece_does_not():
    entries = sug.parse_description("Una caja con piezas lisas 2x2 y 4x2 y bases de 2 pines")["entries"]
    assert [(e["name"], e["confidence"]) for e in entries] == [
        ("Caja con piezas lisas 2x2 y 4x2", "alta"), ("Base de 2 pines", "media")]
    assert "Venía unido con «y» a «Caja con piezas lisas 2x2 y 4x2»" in " ".join(entries[1]["notes"])
    # Una caja unida con «y» a otra caja no necesita aviso.
    boxes = sug.parse_description("Bolsas con tornillos M4 y bolsas con tuercas M4")["entries"]
    assert [(e["name"], e["confidence"]) for e in boxes] == [
        ("Bolsa con tornillos M4", "alta"), ("Bolsa con tuercas M4", "alta")]


# ---------------------------------------------------------------------------
# Nombres: marcas, siglas, genero, caracteristica
# ---------------------------------------------------------------------------

def test_acronyms_models_and_brands_keep_their_spelling():
    assert _names("Kit arduino uno", context="") == _names("KIT ARDUINO UNO", context="") == ["Kit Arduino UNO"]
    assert _names("Arduino Nano x3, Raspberry Pi Pico", context="") == ["Arduino Nano", "Raspberry Pi Pico"]
    assert _names("sensores EV3 y cables USB", context="") == ["Sensor EV3", "Cable USB"]
    assert _names("Servomotores SG90", context="") == ["Servomotor SG90"]
    assert _names("Tornillos M3x10", context="") == ["Tornillo M3x10"]
    assert _names("PIEZAS DE LEGO TECHNIC 1X9", context="") == ["Pieza Lego Technic 1x9"]
    assert _names("Vigas Technic 1x9", context="Piezas Lego") == ["Viga Lego Technic 1x9"]


def test_the_feature_column_carries_the_distinguishing_measure():
    features = {e["name"]: e["feature"] for e in sug.parse_description(
        "Piezas Lego de pines 4x2, bases de 1 pin, resistencias de 220 ohm, LEDs rojos de 5 mm, Caja con tornillos")["entries"]}
    assert features == {
        "Pieza Lego con pines 4x2": "Con pines 4x2", "Base Lego de 1 pin": "1 pin",
        "Resistencia de 220 Ω": "220 Ω", "LED rojo de 5 mm": "Rojo 5 mm", "Caja con tornillos": "Caja",
    }


def test_a_container_brand_is_added_only_to_construction_pieces():
    assert _names("Piezas con pines 2x2, resistencias de 220 ohm", context="Piezas Lego") == [
        "Pieza Lego con pines 2x2", "Resistencia de 220 Ω"]


# ---------------------------------------------------------------------------
# Repetidos, ambiguos e ilegibles
# ---------------------------------------------------------------------------

def test_repeated_products_are_merged_and_their_quantities_added():
    result = sug.parse_description("20 piezas lisas 2x2, 10 piezas lisas 2x2\nLisas 2x2")
    [piece] = result["entries"]
    assert (piece["name"], piece["quantity"]) == ("Pieza lisa 2x2", 30)
    assert len(result["warnings"]) == 2
    assert "sumando las cantidades (30)" in result["warnings"][0]
    result = sug.parse_description("Resistencias de 220 ohm, resistencias 220Ω, resistor de 220 Ω")
    assert [e["name"] for e in result["entries"]] == ["Resistencia de 220 Ω"] and len(result["warnings"]) == 2
    assert len(sug.parse_description("Pieza 4x2, Pieza 2x4")["entries"]) == 1        # 4x2 == 2x4


def test_ambiguous_fragments_get_a_note_and_a_lower_confidence():
    odd = sug.parse_description("Piezas rojas, azules y verdes")["entries"]
    assert all(e["confidence"] == "media" for e in odd) and "varios productos" in " ".join(odd[0]["notes"])
    [vague] = sug.parse_description("Fichas")["entries"]
    assert vague["confidence"] == "media" and "término genérico" in " ".join(vague["notes"])
    [alone] = sug.parse_description("Piezas")["entries"]
    assert alone["confidence"] == "media" and "Solo dice «pieza»" in " ".join(alone["notes"])
    either = sug.parse_description("Piezas lisas de 2x2 o 4x2")["entries"]
    assert [e["confidence"] for e in either] == ["alta", "media"]
    assert "Venía unido con «o»" in " ".join(either[1]["notes"])


def test_parenthetical_remarks_are_kept_as_notes_and_not_as_products():
    [piece] = sug.parse_description("Sensores HC-SR04 (ultrasónicos) x5")["entries"]
    assert (piece["name"], piece["quantity"]) == ("Sensor HC-SR04", 5)
    assert "La descripción aclara: «ultrasónicos»" in " ".join(piece["notes"])
    assert _names("Tornillos (usados) y tuercas (nuevas)", context="") == ["Tornillo", "Tuerca"]


def test_unreadable_fragments_are_reported_and_never_crash():
    result = sug.parse_description("Piezas lisas 2x2 - ### - @@ - 4x2 - 99")
    assert [e["name"] for e in result["entries"]] == ["Pieza lisa 2x2", "Pieza lisa 4x2"]
    assert result["entries"][1]["quantity"] == 99 and result["entries"][1]["confidence"] == "media"
    assert result["unparsed"] == ["###", "@@"] and len(result["warnings"]) == 2
    result = sug.parse_description("x" + "\n..." * 40 + "\n" + "\n".join(f"### {i}" for i in range(30)))
    assert len(result["unparsed"]) > 12
    assert result["warnings"][-1].startswith("Hay ") and len(result["warnings"]) <= 14
    assert sug.parse_description("Piezas con pines 2x2 etc.")["warnings"] == [
        "La descripción termina con «etc.» o similar: puede haber productos sin nombrar; revisa si falta alguno."]


@pytest.mark.parametrize("text", [123, 4.5, float("nan"), True, ["a"], {"a": 1}, b"abc", "\x00\x01", "\ud800" * 3,
                                  "(" * 500, ")" * 500, "x" * 5000, "😀" * 200, "2x" * 3000, "-" * 400,
                                  "Piezas " * 3000, ",".join(["4x2"] * 2000), "\n".join(["Pieza 2x2"] * 500)])
def test_strange_input_never_raises(text):
    result = sug.parse_description(text, context=object() if text is True else "Lego")
    assert set(result) == {"entries", "warnings", "unparsed"}
    assert len(result["entries"]) <= sug.MAX_ENTRIES


def test_random_noise_never_raises_and_keeps_the_invariants():
    import random
    pieces = ["piezas", "Lego", "de", "pines", "4x2", "2×2", "x10", "(15)", "-", ",", " y ", "\n", ".", "caja", "con",
              "que", "contiene", "220", "ohm", "10uF", "5V", "20", "und", "uds.", "kit", "Arduino", "1x0", "100x2",
              "%", "(", ")", "/", "+", ":", "etc", "lisas", "rojas", "Contendor", "0", "k", "4k7", "1/4", "W", "µF",
              "Ω", "x", "X", "*", "por", "by", "ñ", "á"]
    rng = random.Random(2024)
    for _ in range(800):
        text = "".join(rng.choice(pieces) + rng.choice([" ", "", ", "]) for _ in range(rng.randint(1, 30)))
        result = sug.parse_description(text, context=rng.choice(["", "Piezas Lego"]))
        for entry in result["entries"]:
            assert entry["name"].strip() and entry["confidence"] in ("alta", "media", "baja")
            assert entry["selected"] == (entry["confidence"] != "baja")
            assert entry["quantity"] is None or 0 <= entry["quantity"] <= sug.MAX_QUANTITY


def test_pathological_text_is_fast():
    import time
    started = time.perf_counter()
    for text in ("a, " * 5000, "1," * 5000, "4x2 - " * 3000, "2x2 y " * 2000, "(" * 3000):
        sug.parse_description(text)
    assert time.perf_counter() - started < 5


def test_product_key_treats_equivalent_electronics_names_as_the_same_product():
    key = sug.product_key
    assert key("Resistencia de 220 Ω") == key("resistencias 220 ohm") == key("Resistor de 220Ω")
    assert key("Condensador de 10 µF") == key("capacitores 10uF") == key("Condensadores de 10 microfaradios")
    assert key("Resistencia de 220 Ω") != key("Resistencia de 330 Ω")
    assert key("Condensador de 10 µF") != key("Condensador de 100 µF") != key("Condensador de 10 nF")
    assert key("Bloque rojo 2x4") == key("Bloques rojos 4x2") != key("Bloque azul 2x4")
    assert key("Resistencia de 4.7 kΩ") != key("Resistencia de 47 kΩ")


# ---------------------------------------------------------------------------
# Propuestas, confianza y validacion con el inventario
# ---------------------------------------------------------------------------

LAB = {"id": "2-1-04-00-000", "name": "Contenedor 4", "category": "Electrónica", "item_type": "master",
       "location": "Estantería 2; Piso 1; Contenedor 4", "status": "active", "quantity": 0,
       "description": "resistencias de 220 ohm x50, condensadores 10uF, cables jumper macho-hembra 20 und, "
                      "piezas de 0x2"}


def test_lab_electronics_proposals_carry_quantities_confidence_and_codes():
    result = sug.suggest_products(LAB, [LAB])
    got = [(p["code"], p["name"], p["quantity"], p["pending_count"], p["confidence"], p["selected"])
           for p in result["proposals"]]
    assert got == [
        ("2-1-04-01-000", "Resistencia de 220 Ω", 50, False, "alta", True),
        ("2-1-04-02-000", "Condensador de 10 µF", 0, True, "alta", True),
        ("2-1-04-03-000", "Cable jumper macho-hembra", 20, False, "alta", True),
        ("2-1-04-04-000", "Pieza 0x2", 0, True, "baja", False),
    ]
    assert result["proposals"][0]["category"] == "Electrónica"
    assert all(p["inside"] == "" for p in result["proposals"])
    records = sug.editor_records(result["proposals"])
    assert [r["Crear"] for r in records] == [True, True, True, False]
    assert [r["Cantidad"] for r in records] == [50, 0, 20, 0]
    assert records[0]["Característica"] == "220 Ω" and "Cantidad tomada de la descripción (50)" in records[0]["Notas"]
    assert "Revisar medida" in records[3]["Notas"]


def test_lab_electronics_skip_what_the_container_already_has():
    have = {"id": "2-1-04-01-000", "name": "Resistencias 220Ω", "parent_id": LAB["id"], "item_type": "child",
            "status": "active"}
    result = sug.suggest_products(LAB, [LAB, have])
    assert [e["existing_name"] for e in result["existing"]] == ["Resistencias 220Ω"]
    assert [p["name"] for p in result["proposals"]][0] == "Condensador de 10 µF"
    assert [p["code"] for p in result["proposals"]][0] == "2-1-04-02-000"


def test_unselected_low_confidence_rows_do_not_block_and_can_be_included_on_purpose():
    proposals = sug.suggest_products(LAB, [LAB])["proposals"]
    rows = sug.rows_from_records(sug.editor_records(proposals), proposals)
    check = sug.validate_rows(rows, LAB, [LAB])
    assert check["errors"] == [] and check["selected"] == 3 and len(check["payloads"]) == 3
    assert [p["quantity"] for p in check["payloads"]] == [50, 0, 20]
    assert not sug.is_pending_count(check["payloads"][0]) and sug.is_pending_count(check["payloads"][1])
    rows[3]["selected"] = True                                   # el usuario decide incluirla igual
    assert sug.validate_rows(rows, LAB, [LAB])["warnings"] == [
        "Fila 4 (Pieza 0x2): la medida «0x2» tiene una dimensión 0; ¿quisiste decir «1x2»?"]


def test_validation_warns_about_oversize_measures_typed_by_the_user():
    rows, _ = _rows(C3, [C3])
    rows[0]["name"] = "Pieza Lego con pines 64x2"
    check = sug.validate_rows(rows, C3, [C3])
    assert check["errors"] == []
    assert check["warnings"] == ["Fila 1 (Pieza Lego con pines 64x2): la medida «64x2» supera 48 en un lado; "
                                 "¿está bien escrita?"]
    rows[0]["name"] = "Tablero de 100x200 cm"                    # con unidad no hay limite de pieza
    assert sug.validate_rows(rows, C3, [C3])["warnings"] == []


def test_proposals_expose_the_new_fields_without_changing_the_editor_columns():
    assert sug.EDITOR_COLUMNS == ("Crear", "Código", "Nombre", "Característica", "Cantidad", "Unidad", "Notas")
    [proposal] = sug.suggest_products(C3, [C3])["proposals"][:1]
    for key in ("kind", "name", "feature", "source", "notes", "selected", "confidence", "inside", "code", "parent_id",
                "item_type", "category", "location", "unit", "quantity", "min_stock_alert", "pending_count"):
        assert key in proposal
    assert set(sug.editor_records([proposal])[0]) == set(sug.EDITOR_COLUMNS)


# ---------------------------------------------------------------------------
# Prosa y «Titulo: explicacion» (Contenedores 4 y 5 de la base real)
# ---------------------------------------------------------------------------

C4_DESC = ("El Contenedor 4 almacena un inventario de piezas de construcción y prototipado distribuido en diferentes empaques:\n"
"Placas base de construcción verdes (múltiples unidades): Paquete que contiene varias plataformas base de cimentación para ensamblajes.   "
"Caja de bloques básicos, marcos de puertas y ventanas: Organizador con piezas arquitectónicas, bloques de construcción estándar y elementos translúcidos.   "
"Bolsa de placas de unión en cruz: Paquete con soportes estructurales púrpuras para uniones perpendiculares.   "
"Bolsa de llantas y neumáticos: Paquete con ruedas de goma de diferentes tamaños y rines plásticos.   "
"Bolsa de pines y conectores técnicos: Paquete con ejes y pasadores de fricción negros para ensamblajes mecánicos.   "
"Caja de piezas decorativas, conos y ojos impresos: Organizador con elementos de detalle, piezas cilíndricas y componentes con expresiones faciales.   "
"Caja de piezas curvas, arcos y bloques con pendiente: Organizador enfocado en cuñas, rampas y elementos estructurales redondeados.   "
"Caja de soportes angulares y paneles translúcidos: Organizador con piezas de modificación de dirección (brackets), placas de unión y bloques transparentes.   "
"Caja de losetas lisas y piezas planas: Organizador con componentes geométricos sin pines superiores (tiles) y cuartos de círculo.   "
"Caja de botones redondos (studs) y piezas translúcidas pequeñas: Organizador exclusivo para piezas circulares de un solo pin, en su mayoría transparentes o de colores neón.")
C5_DESC = ("El Contenedor 5 está compuesto por una gran caja organizadora de plástico transparente equipada con múltiples divisiones internas. "
"Este estuche está destinado de manera exclusiva al almacenamiento de bloques de construcción estándar de forma rectangular, los cuales se encuentran rigurosamente clasificados por color dentro de cada compartimento. "
"El inventario muestra una amplia variedad cromática, agrupando por separado las piezas sólidas en tonos como azul claro, magenta, café oscuro, azul oscuro, gris, beige, negro, blanco, rosa claro y lila (junto a un par de piezas aisladas en verde lima). "
"De esta manera, este contenedor consolida un banco de bloques estructurales básicos, organizado visualmente para facilitar la selección rápida de piezas según la paleta de colores requerida para cualquier ensamblaje, todas las piezas son 2x1.")
C4 = {"id": "2-1-04-00-000", "name": "Contenedor 4", "category": "Piezas Lego", "item_type": "master",
      "location": "Estantería- 2; Piso-1; Contenedor-4.", "status": "active", "quantity": 0, "description": C4_DESC}
C5 = {"id": "2-1-05-00-000", "name": "Contenedor 5", "category": "Piezas Lego", "item_type": "master",
      "location": "Estantería- 2; Piso-1; Contenedor-5.", "status": "active", "quantity": 0, "description": C5_DESC}

C4_TITLES = [
    "Placas base de construcción verdes",
    "Caja de bloques básicos, marcos de puertas y ventanas",
    "Bolsa de placas de unión en cruz",
    "Bolsa de llantas y neumáticos",
    "Bolsa de pines y conectores técnicos",
    "Caja de piezas decorativas, conos y ojos impresos",
    "Caja de piezas curvas, arcos y bloques con pendiente",
    "Caja de soportes angulares y paneles translúcidos",
    "Caja de losetas lisas y piezas planas",
    "Caja de botones redondos (studs) y piezas translúcidas pequeñas",
]
C4_FEATURES = [
    "Paquete de plataformas base de cimentación",
    "Organizador de piezas arquitectónicas",
    "Paquete de soportes estructurales púrpuras",
    "Paquete de ruedas de goma de diferentes tamaños y rines plásticos",
    "Paquete de ejes y pasadores de fricción negros",
    "Organizador de elementos de detalle, piezas cilíndricas",
    "Organizador de cuñas, rampas y elementos estructurales redondeados",
    "Organizador de piezas de modificación de dirección, placas de unión",
    "Organizador de componentes geométricos sin pines superiores",
    "Organizador de piezas circulares de un solo pin",
]
# Los 11 hijos que ya existen en la base: nombre (con variantes de puntuacion) y fragmento guardado.
C4_CHILDREN = [
    ("2-1-04-02-000", "Placas base de construcción verdes (múltiples unidades)", "Placas base de construcción verdes"),
    ("2-1-04-04-000", "Caja de bloques básicos, marcos de puertas y ventanas",
     "Caja de bloques básicos, marcos de puertas y ventanas"),
    ("2-1-04-05-000", "Bolsa de placas de unión en cruz", "Bolsa de placas de unión en cruz"),
    ("2-1-04-06-000", "Bolsa de llantas y neumáticos.", "Bolsa de llantas y neumáticos"),
    ("2-1-04-08-000", "Bolsa de pines y conectores técnicos", "Bolsa de pines y conectores técnicos"),
    ("2-1-04-10-000", "Caja de piezas decorativas conos y ojos impresos",
     "Caja de piezas decorativas, conos y ojos impresos"),
    ("2-1-04-12-000", "Caja de piezas curvas, arcos y bloques con pendiente.",
     "Caja de piezas curvas, arcos y bloques con pendiente"),
    ("2-1-04-14-000", "Caja de soportes angulares y paneles translucidos",
     "Caja de soportes angulares y paneles translúcidos"),
    ("2-1-04-16-000", "Caja de losetas lisas y piezas planas", "Caja de losetas lisas y piezas planas"),
    ("2-1-04-18-000", "Caja de botones redondos (studs) y piezas translúcidas pequeñas",
     "Caja de botones redondos (studs) y piezas translúcidas pequeñas"),
    # Renombrado por completo: solo se reconoce por el fragmento de la descripcion con que se creo.
    ("2-1-04-20-000", "Ruedas varias", "Paquete con ruedas de goma de diferentes tamaños y rines plásticos"),
]
C5_COLORS = ["azul claro", "magenta", "café oscuro", "azul oscuro", "gris", "beige", "negro", "blanco", "rosa claro",
             "lila"]


def c4_children(count=None):
    """Los hijos de C4 tal como los dejo el generador: sin padre en `items` no hay GLIOPS que validar."""
    rows = C4_CHILDREN[:count]
    return [{"id": code, "name": name, "parent_id": C4["id"], "item_type": "child", "status": "active",
             "description": sug.build_payload({"code": code, "name": name, "quantity": 0, "source": fragment},
                                              C4)["description"]} for code, name, fragment in rows]


def test_container_4_title_explanation_gives_one_product_per_title():
    result = sug.parse_description(C4_DESC, context="Piezas Lego Contenedor 4")
    entries = result["entries"]
    assert [e["name"] for e in entries] == C4_TITLES
    assert [e["feature"] for e in entries] == C4_FEATURES
    assert result["warnings"] == [] and result["unparsed"] == []
    assert all(e["quantity"] is None and e["confidence"] == "alta" and e["selected"] for e in entries)
    # Nunca la frase de explicacion como nombre, ni la introduccion, ni parentesis de cantidad o sin cerrar.
    for entry in entries:
        assert len(entry["name"]) <= sug.MAX_TITLE_CHARS and entry["name"].count("(") == entry["name"].count(")")
        assert "múltiples" not in entry["name"] and not entry["name"].endswith((".", ":"))
        assert len(entry["feature"]) <= sug.MAX_FEATURE_LENGTH and entry["feature"][0].isupper()
    assert not any("almacena" in e["name"] or "Contenedor 4" in e["name"] for e in entries)
    assert "sin una cantidad exacta" in entries[0]["notes"][0] and "múltiples unidades" in entries[0]["notes"][0]
    assert [e["kind"] for e in entries] == [sug.KIND_PRODUCT] + [sug.KIND_BOX] * 9
    assert entries[1]["source"].startswith("Caja de bloques básicos, marcos de puertas y ventanas: Organizador con")


def test_container_4_is_empty_when_its_ten_or_eleven_children_already_exist():
    items = [C4, *c4_children()]
    result = sug.suggest_products(C4, items)
    assert result["proposals"] == [] and result["warnings"] == [] and result["unparsed"] == []
    assert [e["name"] for e in result["existing"]] == C4_TITLES
    assert result["existing"][3]["id"] in ("2-1-04-06-000", "2-1-04-20-000")
    # El renombrado solo se reconoce por el fragmento: sin el hijo "Bolsa de llantas", sigue contando.
    without = [child for child in items if child.get("id") != "2-1-04-06-000"]
    assert sug.suggest_products(C4, without)["proposals"] == []
    # Sin el fragmento tampoco hay duplicado si el nombre coincide ni al reves.
    only_fragment = [dict(child, name="Otro") for child in c4_children()]
    assert [p["name"] for p in sug.suggest_products(C4, [C4, *only_fragment])["proposals"]] == []


def test_container_4_only_proposes_what_is_missing():
    result = sug.suggest_products(C4, [C4, *c4_children(3)])
    assert [p["name"] for p in result["proposals"]] == C4_TITLES[3:]
    assert [e["existing_name"] for e in result["existing"]] == [c[1] for c in C4_CHILDREN[:3]]
    assert [p["code"] for p in result["proposals"]][:2] == ["2-1-04-06-000", "2-1-04-07-000"]


def test_container_5_prose_gives_one_product_per_color():
    result = sug.parse_description(C5_DESC, context="Piezas Lego Contenedor 5")
    entries = result["entries"]
    assert [e["name"] for e in entries] == [f"Pieza Lego 2x1 {color}" for color in C5_COLORS] + [
        "Pieza Lego 2x1 verde lima"]
    assert [e["feature"] for e in entries][:2] == ["2x1 azul claro", "2x1 magenta"]
    assert [e["confidence"] for e in entries] == ["alta"] * 10 + ["media"]
    assert [e["quantity"] for e in entries] == [None] * 10 + [2]
    assert all(e["selected"] for e in entries) and result["warnings"] == [] and result["unparsed"] == []
    assert "un par de piezas aisladas en verde lima" in " ".join(entries[-1]["notes"])
    # Nada de oraciones: ni la caja organizadora, ni «Este estuche...», ni «De esta manera...».
    for entry in entries:
        assert len(entry["name"]) <= sug.MAX_NAME_CHARS and len(entry["name"].split()) <= sug.MAX_NAME_WORDS
        assert not any(w in fold_text(entry["name"]) for w in ("estuche", "organizador", "manera", "inventario"))


def fold_text(text):
    return sug.fold(text)


def test_container_5_suggestions_use_the_container_brand_and_are_pending_count():
    result = sug.suggest_products(C5, [C5])
    assert len(result["proposals"]) == 11 and result["existing"] == []
    assert [p["code"] for p in result["proposals"]][:2] == ["2-1-05-01-000", "2-1-05-02-000"]
    assert [p["quantity"] for p in result["proposals"]] == [0] * 10 + [2]
    assert [p["pending_count"] for p in result["proposals"]] == [True] * 10 + [False]
    again = sug.suggest_products(C5, [C5, {"id": "2-1-05-01-000", "name": "Pieza Lego 2x1 azul claro",
                                          "parent_id": C5["id"], "status": "active"}])
    assert len(again["proposals"]) == 10 and again["existing"][0]["existing_name"] == "Pieza Lego 2x1 azul claro"


# (id, descripcion, contexto, [(nombre, caracteristica, cantidad, confianza)], avisos)
PROSE_CASES = [
    ("titulo_una_linea", 'Resistencias de carbón (varias unidades): Paquete con resistencias de distintos valores para prácticas. Cables jumper: Bolsa con cables macho-hembra de 20 cm para protoboard.', '', [
        ('Resistencias de carbón', 'Paquete de resistencias de distintos valores', None, "alta"),
        ('Cables jumper', 'Bolsa de cables macho-hembra de 20 cm', None, "alta"),
    ], 0),
    ("titulo_cantidades", 'Tornillos M3 (200 unidades): Bolsa con tornillos de acero para ensamblajes mecánicos.\nTuercas M3 (150 unidades): Bolsa con tuercas hexagonales para tornillos de acero.', '', [
        ('Tornillos M3', 'Bolsa de tornillos de acero', 200, "alta"),
        ('Tuercas M3', 'Bolsa de tuercas hexagonales', 150, "alta"),
    ], 0),
    ("titulo_con_introduccion", 'El estante guarda herramientas de taller:\nEstuche de herramientas (3): Organizador con destornilladores, pinzas y alicates para electrónica.   Multímetro digital: Equipo para medir voltaje y corriente en circuitos.', '', [
        ('Estuche de herramientas', 'Organizador de destornilladores, pinzas y alicates', 3, "alta"),
        ('Multímetro digital', 'Equipo para medir voltaje y corriente en circuitos', None, "alta"),
    ], 0),
    ("titulo_parentesis_suelto", 'Cables USB (tipo A: Paquete con cables de datos para impresoras y escáneres.   Adaptadores HDMI): Bolsa con adaptadores de video para pantallas externas.', '', [
        ('Cables USB tipo A', 'Paquete de cables de datos', None, "alta"),
        ('Adaptadores HDMI', 'Bolsa de adaptadores de video', None, "alta"),
    ], 0),
    ("prosa_colores_en_caja", 'Caja con bloques 2x4 clasificados en colores: rojo, azul oscuro, verde y amarillo.', '', [
        ('Bloque 2x4 rojo', '2x4 rojo', None, "alta"),
        ('Bloque 2x4 azul oscuro', '2x4 azul oscuro', None, "alta"),
        ('Bloque 2x4 verde', '2x4 verde', None, "alta"),
        ('Bloque 2x4 amarillo', '2x4 amarillo', None, "alta"),
    ], 0),
    ("prosa_bloques_colores", 'Los bloques son 2x2 y vienen en colores como rojo, azul y verde.', 'Piezas Lego', [
        ('Bloque Lego 2x2 rojo', '2x2 rojo', None, "alta"),
        ('Bloque Lego 2x2 azul', '2x2 azul', None, "alta"),
        ('Bloque Lego 2x2 verde', '2x2 verde', None, "alta"),
    ], 0),
    ("prosa_tonos_medida_global", 'Las piezas están organizadas por tonos: amarillo, naranja, café claro y gris oscuro; todas las piezas miden 1x4.', 'Piezas Lego', [
        ('Pieza Lego 1x4 amarillo', '1x4 amarillo', None, "alta"),
        ('Pieza Lego 1x4 naranja', '1x4 naranja', None, "alta"),
        ('Pieza Lego 1x4 café claro', '1x4 café claro', None, "alta"),
        ('Pieza Lego 1x4 gris oscuro', '1x4 gris oscuro', None, "alta"),
    ], 0),
    ("prosa_par_de_piezas", 'El contenedor tiene una gran caja con divisiones. Las piezas son de distintos colores como rojo y azul, todas son 2x2 (más un par de piezas sueltas en dorado).', 'Piezas Lego', [
        ('Caja gran con divisiones', 'Gran', None, "baja"),
        ('Pieza Lego 2x2 rojo', '2x2 rojo', None, "alta"),
        ('Pieza Lego 2x2 azul', '2x2 azul', None, "alta"),
        ('Pieza Lego 2x2 dorado', '2x2 dorado', 2, "media"),
    ], 0),
    ("prosa_colores_sin_medida", 'El inventario muestra bloques en tonos como verde, lila y rosa claro.', '', [
        ('Bloque verde', 'Verde', None, "media"),
        ('Bloque lila', 'Lila', None, "media"),
        ('Bloque rosa claro', 'Rosa claro', None, "media"),
    ], 0),
    ("prosa_solo_descripcion", 'Este contenedor guarda material de laboratorio. De esta manera, el contenedor consolida un banco de piezas.', '', [
    ], 0),
    ("prosa_y_lista", 'Este contenedor guarda material de laboratorio. Contiene resistencias de 220 ohm x50.', '', [
        ('Resistencia de 220 Ω', '220 Ω', 50, "alta"),
    ], 0),
    ("prosa_oracion_ambigua", 'El inventario muestra tornillos, tuercas y arandelas de varios tamaños.', '', [
        ('Tornillo', '', None, "baja"),
        ('Tuerca', '', None, "baja"),
        ('Arandela de tamaños', '', None, "baja"),
    ], 0),
    ("nombre_largo_recortado", 'Caja de organización para tornillos tuercas arandelas pernos remaches clavos y resortes de acero', '', [
        ('Caja de organización para tornillos tuercas arandelas pernos', 'Caja', None, "baja"),
    ], 0),
    ("oracion_con_verbo_en_medio", 'Hay una caja que es muy grande y pesada con tornillos sueltos de todo tipo.', '', [
        ('Caja que es muy grande y pesada', 'Caja', None, "baja"),
    ], 0),
]


@pytest.mark.parametrize("case_id, text, context, expected, warnings", PROSE_CASES,
                         ids=[case[0] for case in PROSE_CASES])
def test_prose_and_title_explanation_variants(case_id, text, context, expected, warnings):
    result = sug.parse_description(text, context=context)
    got = [(e["name"], e["feature"], e["quantity"], e["confidence"]) for e in result["entries"]]
    assert got == expected
    assert len(result["warnings"]) == warnings
    for entry in result["entries"]:
        assert entry["selected"] == (entry["confidence"] != "baja")
        assert entry["confidence"] == "alta" or entry["notes"]
        assert len(entry["name"]) <= sug.MAX_TITLE_CHARS and entry["name"].count("(") == entry["name"].count(")")


def test_a_name_is_never_a_long_sentence():
    [box] = sug.parse_description("Caja de organización para tornillos tuercas arandelas pernos remaches clavos y "
                                  "resortes de acero")["entries"]
    assert len(box["name"].split()) <= 8 and len(box["name"]) <= 60 and not box["selected"]
    assert "resortes de acero" in " ".join(box["notes"]) and "se acortó" in " ".join(box["notes"])
    for text in ("Este estuche está destinado al almacenamiento de bloques", "De esta manera, el contenedor sirve",
                 "Está compuesto por piezas lisas", "Todas las piezas son rojas", "Cada pieza es distinta"):
        assert sug.parse_description(text)["entries"] == [], text


def test_prose_around_a_real_list_does_not_change_the_list():
    plain = sug.parse_description(C1["description"], context="Piezas Lego")["entries"]
    wrapped = sug.parse_description("Este contenedor guarda piezas de construcción. " + C1["description"],
                                    context="Piezas Lego")["entries"]
    assert [e["name"] for e in wrapped] == [e["name"] for e in plain]


@pytest.mark.parametrize("title, expected", [
    ("Placas verdes (múltiples unidades)", ("Placas verdes", None)),
    ("Placas verdes (x10)", ("Placas verdes", 10)),
    ("Placas verdes (15 uds)", ("Placas verdes", 15)),
    ("Placas verdes (studs) y bases (cantidad: 4)", ("Placas verdes (studs) y bases", 4)),
    ("Cables USB (tipo A", ("Cables USB tipo A", None)),
    ("Adaptadores HDMI)", ("Adaptadores HDMI", None)),
    ("Pinzas ((largas)", ("Pinzas (largas)", None)),
])
def test_titles_drop_quantity_parentheses_and_balance_the_rest(title, expected):
    entry = sug._titled_entry(title, "Paquete con piezas para pruebas de laboratorio.")
    assert (entry["name"], entry["quantity"]) == expected
    assert entry["name"].count("(") == entry["name"].count(")")


def test_a_colon_after_a_verb_or_a_short_list_is_not_a_title():
    assert _names("Contiene: Piezas Lego de pines 4x2 - 2x2", context="") == [
        "Pieza Lego con pines 4x2", "Pieza Lego con pines 2x2"]
    entries = sug.parse_description("Caja grande: Piezas lego 2x2 y 4x2 lisas")["entries"]
    assert [(e["name"], e["inside"]) for e in entries][0] == ("Caja grande", "")
    assert len(entries) == 3 and entries[1]["inside"] == "Caja grande"


def test_names_match_ignores_punctuation_parentheses_and_accents_but_not_figures():
    match = sug.names_match
    assert match("Placas base de construcción verdes", "Placas base de construcción verdes (múltiples unidades)")
    assert match("Caja de piezas decorativas, conos y ojos", "Caja de piezas decorativas conos y ojos.")
    assert match("Caja de soportes translúcidos", "caja de soportes translucidos")
    assert not match("Pieza Lego lisa 2x2", "Pieza Lego lisa 2x4")
    assert not match("Resistencia de 220 Ω", "Resistencia de 330 Ω")
    assert not match("Bolsa de pines y conectores técnicos", "Bolsa de ejes y conectores técnicos")
    assert not match("Caja con tornillos (M3)", "Caja con tornillos (M4)")
    assert not match("", "Caja")


def test_the_source_fragment_is_read_back_from_the_child_description():
    payload = sug.build_payload({"code": "2-1-04-02-000", "name": "X", "quantity": 0,
                                 "source": "Piezas de «Lego» (2x2)"}, C4)
    assert sug.child_source_fragment(payload) == "Piezas de «Lego» (2x2)"
    assert sug.child_source_fragment({"description": "Otra cosa"}) == "" and sug.child_source_fragment({}) == ""


# ---------------------------------------------------------------------------
# Codigos, ubicacion y productos que ya existen
# ---------------------------------------------------------------------------

def test_existing_products_are_not_duplicated_and_numbering_continues():
    children = [
        {"id": "2-1-01-01-000", "name": "Piezas de pines 4x2", "parent_id": C1["id"], "item_type": "child",
         "status": "active"},
        {"id": "2-1-01-07-000", "name": "Ruedas", "parent_id": C1["id"], "item_type": "child", "status": "active"},
    ]
    result = sug.suggest_products(C1, [C1, *children])
    assert result["existing"] == [{
        "name": "Pieza Lego con pines 4x2", "id": "2-1-01-01-000",
        "existing_name": "Piezas de pines 4x2", "source": "Piezas Lego de Pines 4x2",
    }]
    assert [(p["code"], p["name"]) for p in result["proposals"]] == [
        ("2-1-01-08-000", "Pieza Lego con pines 2x2"),
        ("2-1-01-09-000", "Pieza Lego con pines 2x1"),
        ("2-1-01-10-000", "Caja con separadores de fichas"),
        ("2-1-01-11-000", "Pieza Lego lisa 2x2"),
    ]


def test_codes_skip_retired_items_and_ignore_other_containers():
    used = [
        {"id": "2-1-03-01-000", "name": "Vieja", "parent_id": C3["id"], "status": "retired"},
        {"id": "2-1-04-05-000", "name": "Otro contenedor", "parent_id": "2-1-04-00-000", "status": "active"},
    ]
    result = sug.suggest_products(C3, [C3, *used])
    # El producto dado de baja no cuenta como existente, pero su codigo no se reutiliza.
    assert result["existing"] == []
    assert [p["code"] for p in result["proposals"]] == ["2-1-03-02-000", "2-1-03-03-000", "2-1-03-04-000"]


def test_next_child_codes_for_each_kind_of_container_code():
    assert sug.next_child_codes("2-1-05-00-000", [], 2) == (["2-1-05-01-000", "2-1-05-02-000"], "")
    assert sug.next_child_codes("2-1-05-03-000", ["2-1-05-03-001"], 2) == (["2-1-05-03-002", "2-1-05-03-003"], "")
    assert sug.next_child_codes("CONT-01", ["CONT-01-01"], 2) == (["CONT-01-02", "CONT-01-03"], "")
    codes, note = sug.next_child_codes("M1-E2", [], 2)
    assert codes == ["", ""] and "Escribe el código" in note
    codes, note = sug.next_child_codes("LAB-MICRO-01", [], 1)  # no cabe: CONT-...-NN pasaria de 13
    assert codes == [""] and "demasiado largo" in note
    codes, note = sug.next_child_codes("2-1-05-03-004", [], 1)
    assert codes == [""] and "nivel ítem" in note
    assert sug.next_child_codes("2-1-05-00-000", [], 0) == ([], "")


def test_non_gliops_container_gets_blank_codes_with_a_note():
    container = {**C3, "id": "M1-E2"}
    proposals = sug.suggest_products(container, [container])["proposals"]
    assert [p["code"] for p in proposals] == ["", "", ""]
    assert all("Escribe el código" in _notes(p) for p in proposals)
    assert all(p["location"] == C3["location"] for p in proposals)


@pytest.mark.parametrize("location, code, expected", [
    ("Estantería- 2; Piso-1; Contenedor-1.", "2-1-01-04-000", "Estantería- 2; Piso-1; Contenedor-1; Caja 04."),
    ("Estante 3, piso 2", "2-1-01-12-000", "Estante 3, piso 2, Caja 12"),
    ("Bodega", "2-1-01-01-000", "Bodega · Caja 01"),
    ("", "2-1-01-02-000", "Estantería 2; Piso 1; Contenedor 01; Caja 02"),
    ("Estantería- 2; Piso-1; Contenedor-1.", "", "Estantería- 2; Piso-1; Contenedor-1."),
])
def test_child_location(location, code, expected):
    assert sug.child_location({"id": "2-1-01-00-000", "location": location}, code) == expected


def test_child_location_inside_a_box_level_container():
    box = {"id": "2-1-01-03-000", "location": "Estantería 2; Caja 03"}
    assert sug.child_location(box, "2-1-01-03-002") == "Estantería 2; Caja 03; Ítem 002"


# ---------------------------------------------------------------------------
# Vista previa editable y validacion antes de crear
# ---------------------------------------------------------------------------

def _rows(container=C1, items=None):
    proposals = sug.suggest_products(container, items or [container])["proposals"]
    return sug.rows_from_records(sug.editor_records(proposals), proposals), proposals


def test_editor_records_roundtrip():
    rows, proposals = _rows(C2)
    records = sug.editor_records(proposals)
    assert list(records[0]) == list(sug.EDITOR_COLUMNS)
    assert records[0] == {
        "Crear": True, "Código": "2-1-02-01-000", "Nombre": "Pieza Lego con pines 6x2",
        "Característica": "Con pines 6x2", "Cantidad": 0, "Unidad": "unidad", "Notas": "",
    }
    assert records[-1]["Crear"] is False and "dimensión 0" in records[-1]["Notas"]
    assert rows[1] == {
        "selected": True, "code": "2-1-02-02-000", "name": "Pieza Lego con pines 6x1", "quantity": 0,
        "unit": "unidad", "source": "Piezas Lego de Pines … 6x1", "feature": "Con pines 6x1",
    }


def test_valid_rows_become_payloads_ready_for_storage():
    rows, _ = _rows()
    rows[0]["quantity"] = 35
    rows[1]["quantity"] = float("nan")       # celda borrada en el editor = pendiente
    rows[2]["selected"] = False
    rows[3]["unit"] = "  "
    check = sug.validate_rows(rows, C1, [C1])
    assert check["errors"] == [] and check["warnings"] == [] and check["selected"] == 4
    assert [p["id"] for p in check["payloads"]] == ["2-1-01-01-000", "2-1-01-02-000", "2-1-01-04-000", "2-1-01-05-000"]
    first, second = check["payloads"][:2]
    assert first == {
        "id": "2-1-01-01-000", "name": "Pieza Lego con pines 4x2", "category": "Piezas Lego",
        "description": "Creado desde la descripción de «Contenedor 1» (2-1-01-00-000): «Piezas Lego de Pines 4x2».",
        "item_type": "child", "parent_id": "2-1-01-00-000", "unit": "unidad", "quantity": 35,
        "location": "Estantería- 2; Piso-1; Contenedor-1; Caja 01.", "min_stock_alert": 0, "status": "active",
    }
    assert second["quantity"] == 0 and second["description"].endswith(sug.PENDING_COUNT_NOTE)
    assert check["payloads"][2]["unit"] == "unidad"


def test_validation_reports_every_problem_with_its_row():
    rows, _ = _rows()
    existing = {"id": "2-1-01-09-000", "name": "Rueda", "parent_id": C1["id"], "item_type": "child",
                "status": "active"}
    rows[0].update(name="  ")
    rows[1].update(code="")
    rows[2].update(code="2-1-01-01-001x")
    rows[3].update(code="2-1-02-04-000")            # otro contenedor
    rows[4].update(code="2-1-01-09-000", quantity=-1)
    check = sug.validate_rows(rows, C1, [C1, existing])
    assert check["selected"] == 5 and len(check["payloads"]) == 0
    errors = check["errors"]
    assert len(errors) == 5 and all(e.endswith(".") and not e.endswith("..") for e in errors)
    assert errors[0] == "Fila 1: el nombre es obligatorio."
    assert errors[1] == "Fila 2 (Pieza Lego con pines 2x2): el código es obligatorio."
    assert errors[2].startswith("Fila 3 (Pieza Lego con pines 2x1): El codigo '2-1-01-01-001x' tiene 14 caracteres")
    assert errors[3] == ("Fila 4 (Caja con separadores de fichas): el código 2-1-02-04-000 no queda dentro "
                         "del contenedor 2-1-01-00-000 (usa 2-1-01-NN-000).")
    assert errors[4] == ("Fila 5 (Pieza Lego lisa 2x2): el código 2-1-01-09-000 ya pertenece a «Rueda»; "
                         "la cantidad no puede ser negativa.")


def test_validation_blocks_duplicates_in_the_batch_and_in_the_container():
    rows, _ = _rows()
    rows[1].update(code=rows[0]["code"])
    rows[4].update(name="Piezas Lego de pines 2X4")   # misma pieza que la fila 1
    child = {"id": "2-1-01-20-000", "name": "Caja con separadores de fichas", "parent_id": C1["id"],
             "item_type": "child", "status": "active"}
    errors = sug.validate_rows(rows, C1, [C1, child])["errors"]
    assert "el código 2-1-01-01-000 está repetido (fila 1)" in errors[0]
    assert "ya existe «Caja con separadores de fichas» en este contenedor (2-1-01-20-000)" in errors[1]
    assert "repite el producto de la fila 1" in errors[2]


def test_validation_rejects_the_container_code_and_non_integer_quantities():
    rows, _ = _rows(C3, [C3])
    rows[0].update(code=C3["id"])
    rows[1].update(quantity=2.5)
    rows[2].update(quantity="tres")
    errors = sug.validate_rows(rows, C3, [C3])["errors"]
    assert "no puede usar el mismo código del contenedor" in errors[0]
    assert "número entero" in errors[1] and "número entero" in errors[2]


def test_selecting_the_1x0_row_without_fixing_it_warns():
    rows, _ = _rows(C2)
    rows[-1]["selected"] = True
    check = sug.validate_rows(rows, C2, [C2])
    assert check["errors"] == []
    assert check["warnings"] == ["Fila 13 (Pieza Lego con bisel 1x0): la medida «1x0» tiene una dimensión 0; "
                                 "¿quisiste decir «1x1»?"]
    rows[-1]["name"] = "Pieza Lego con bisel 1x1"
    assert sug.validate_rows(rows, C2, [C2])["warnings"] == []


@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), (None, False), (float("nan"), False), ("true", True), ("no", False), (1, True),
])
def test_selected_flag_tolerates_editor_values(value, expected):
    assert sug._truthy(value) is expected


def test_retired_rows_and_other_containers_do_not_block():
    rows, _ = _rows(C3, [C3])
    retired = {"id": "2-1-03-01-000", "name": "Pieza Lego con pines 4x2", "parent_id": C3["id"],
               "item_type": "child", "status": "retired"}
    other = {"id": "2-1-01-01-000", "name": "Pieza Lego con pines 3x2", "parent_id": C1["id"],
             "item_type": "child", "status": "active"}
    check = sug.validate_rows(rows, C3, [C3, retired, other])
    assert check["errors"] == [] and len(check["payloads"]) == 3


def test_is_pending_count():
    payload = sug.validate_rows(_rows(C3, [C3])[0], C3, [C3])["payloads"][0]
    assert sug.is_pending_count(payload)
    assert not sug.is_pending_count({**payload, "quantity": 4})
    assert not sug.is_pending_count({**payload, "description": "Otra cosa"})
    assert not sug.is_pending_count({**C1, "description": sug.PENDING_COUNT_NOTE})
    assert not sug.is_pending_count({})


# ---------------------------------------------------------------------------
# LabStorage.save_items_bulk (base aislada en tmp por conftest)
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    storage = LabStorage()
    for container in REAL:
        data = {k: container[k] for k in ("name", "category", "item_type", "location", "description")}
        storage.save_item(data, container["id"], is_new=True, actor_email=ACTOR)
    return storage


@pytest.fixture
def writes(monkeypatch):
    calls = []
    original = storage_module._write_and_sync

    def counting(dfs):
        calls.append(len(dfs["items"]))
        original(dfs)

    monkeypatch.setattr(storage_module, "_write_and_sync", counting)
    return calls


def default_payloads(container, storage):
    """Lo que crearia el generador si el usuario confirma la propuesta sin cambios."""
    items = storage.get_all_items(include_retired=True)
    proposals = sug.suggest_products(container, items)["proposals"]
    rows = sug.rows_from_records(sug.editor_records(proposals), proposals)
    check = sug.validate_rows(rows, container, items)
    assert check["errors"] == []
    return check["payloads"]


_payloads = default_payloads


def test_bulk_creates_every_product_with_a_single_write(db, writes):
    payloads = _payloads(C2, db)
    created = db.save_items_bulk(payloads, actor_email=ACTOR,
                                 details=sug.HISTORY_DETAILS.format(parent_id=C2["id"]))
    assert created == [p["id"] for p in payloads] and len(created) == 12
    assert len(writes) == 1                                 # un solo commit en GitHub

    children = {c["id"]: c for c in db.get_children(C2["id"])}
    assert set(children) == set(created)
    base = children["2-1-02-04-000"]
    assert base["name"] == "Base Lego de 1 pin" and base["quantity"] == 0 and base["unit"] == "unidad"
    assert base["location"] == "Estantería- 2; Piso-1; Contenedor-2; Caja 04."
    assert base["created_by"] == ACTOR and base["status"] == "active" and base["category"] == "Piezas Lego"
    assert sug.is_pending_count(base)
    history = db.get_item_history("2-1-02-04-000")
    assert [(h["type"], str(h["quantity_change"]), h["actor_user_id"]) for h in history] == [("Alta", "0", ACTOR)]
    assert history[0]["details"] == "Creado desde la descripción del contenedor 2-1-02-00-000 (generador de productos)."
    # Lo creado ya no se vuelve a proponer.
    assert sug.suggest_products(C2, db.get_all_items())["proposals"][0]["name"] == "Pieza Lego con bisel 1x0"


def test_bulk_is_all_or_nothing(db, writes):
    payloads = _payloads(C1, db)
    payloads[1] = {**payloads[1], "id": "2-1-01-00-001"}           # item sin caja: GLIOPS invalido
    payloads[2] = {**payloads[2], "quantity": -3}
    payloads[3] = {**payloads[3], "parent_id": "2-1-09-00-000"}    # contenedor inexistente
    payloads[4] = {**payloads[4], "id": payloads[0]["id"]}         # repetido en la tanda
    with pytest.raises(ValueError) as excinfo:
        db.save_items_bulk(payloads, actor_email=ACTOR)
    message = str(excinfo.value)
    assert message.startswith("No se creo ningun item.")
    assert "Fila 2 (2-1-01-00-001)" in message and "Item debe ser 000" in message
    assert "Fila 3" in message and "mayor o igual a 0" in message
    assert "Fila 4" in message and "no existe" in message
    assert "Fila 5" in message and "repetido en la misma tanda" in message
    assert writes == [] and db.get_children(C1["id"]) == []


def test_bulk_refuses_codes_in_use_and_reuses_retired_ones(db):
    payloads = _payloads(C3, db)
    db.save_items_bulk(payloads[:1], actor_email=ACTOR)
    with pytest.raises(ValueError, match="Ya existe un item con ese codigo"):
        db.save_items_bulk(payloads, actor_email=ACTOR)

    db.retire_item(payloads[0]["id"], actor_email=ACTOR)
    created = db.save_items_bulk(payloads, actor_email=ACTOR)
    assert created == [p["id"] for p in payloads]
    history = db.get_item_history(payloads[0]["id"])
    assert [h["type"] for h in history] == ["Alta"]          # el rastro anterior se borro


def test_bulk_validates_parents_and_types(db):
    db.retire_item(C3["id"], actor_email=ACTOR)
    with pytest.raises(ValueError, match="dado de baja"):
        db.save_items_bulk([{"id": "2-1-03-09-000", "name": "X", "item_type": "child", "parent_id": C3["id"]}])
    with pytest.raises(ValueError, match="Tipo de item invalido"):
        db.save_items_bulk([{"id": "LAB-1", "name": "X", "item_type": "kit"}])
    with pytest.raises(ValueError, match="Solo un Contenedor de Característica"):
        db.save_items_bulk([{"id": "LAB-1", "name": "X", "item_type": "standalone", "parent_id": C1["id"]}])
    with pytest.raises(ValueError, match="codigo y nombre son obligatorios"):
        db.save_items_bulk([{"id": "LAB-1", "name": " "}])
    assert db.get_item("LAB-1") is None


def test_bulk_accepts_a_container_created_in_the_same_batch(db, writes):
    created = db.save_items_bulk([
        {"id": "2-2-01-00-000", "name": "Contenedor nuevo", "item_type": "master"},
        {"id": "2-2-01-01-000", "name": "Pieza nueva", "item_type": "child", "parent_id": "2-2-01-00-000",
         "quantity": "7", "min_stock_alert": 2.0, "unit": "", "details": "Detalle propio."},
    ], actor_email=ACTOR)
    assert created == ["2-2-01-00-000", "2-2-01-01-000"] and len(writes) == 1
    child = db.get_item("2-2-01-01-000")
    assert (child["quantity"], child["min_stock_alert"], child["unit"]) == (7, 2, "unidad")
    assert db.get_item_history("2-2-01-01-000")[0]["details"] == "Detalle propio."
    assert db.get_item_history("2-2-01-00-000")[0]["details"] == "Item creado en el sistema."


def test_bulk_with_nothing_to_create_does_not_write(db, writes):
    assert db.save_items_bulk([], actor_email=ACTOR) == []
    assert db.save_items_bulk(None) == []
    assert writes == []


def test_bulk_rejects_non_integer_quantities(db):
    for bad in ("2.5", "muchas", math.inf):
        with pytest.raises(ValueError):
            db.save_items_bulk([{"id": "LAB-9", "name": "X", "quantity": bad}])
