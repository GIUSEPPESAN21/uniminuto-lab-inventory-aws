# -*- coding: utf-8 -*-
"""Componente Streamlit reutilizable para construir o ingresar codigos.

La construccion es solo de interfaz: core/barcode.py conserva las reglas y
core/storage.py vuelve a validar antes de guardar. Asi un cliente alternativo
no puede saltarse la validacion del dominio.
"""

import streamlit as st

from core import barcode, labels

MODE_GENERATED = ":material/auto_awesome: Generar automáticamente"
MODE_MANUAL = ":material/keyboard: Escanear o escribir"
MODE_SCANNED = ":material/photo_camera: Usar código escaneado"

FORMAT_OPTIONS = {
    "GLIOPS estándar (ubicación)": barcode.FORMAT_STANDARD,
    "Mesa de trabajo": barcode.FORMAT_MESA,
    "Exhibición Lego": barcode.FORMAT_LEGO,
    "Numérico libre": barcode.FORMAT_NUMERIC,
    "Prefijo alfanumérico": barcode.FORMAT_ALNUM,
}

_TYPE_LEVEL = {
    "master": barcode.LEVEL_CONTAINER,
    "child": barcode.LEVEL_BOX,
    "standalone": barcode.LEVEL_ITEM,
}
_TYPE_NAME = {
    "master": "Contenedor Principal",
    "child": "Contenedor de Característica",
    "standalone": "Ítem Individual",
}


def _standard_builder(item_type: str, key_prefix: str) -> str:
    c1, c2, c3, c4, c5 = st.columns(5)
    estanteria = c1.number_input(
        "Estantería", min_value=barcode.ESTANTERIA_MIN, max_value=barcode.ESTANTERIA_MAX,
        value=1, step=1, key=f"{key_prefix}_rack",
    )
    piso = c2.number_input(
        "Piso", min_value=barcode.PISO_MIN, max_value=barcode.PISO_MAX,
        value=1, step=1, key=f"{key_prefix}_floor",
    )
    contenedor = c3.number_input(
        "Contenedor", min_value=1, value=1, step=1, key=f"{key_prefix}_container",
    )

    if item_type == "master":
        caja, item = 0, 0
        c4.text_input("Caja", value="00", disabled=True, key=f"{key_prefix}_box_na")
        c5.text_input("Ítem", value="000", disabled=True, key=f"{key_prefix}_item_na")
        st.caption("Nivel contenedor: Caja=00 e Ítem=000 significan 'no aplica'.")
    elif item_type == "child":
        caja = c4.number_input("Caja", min_value=1, value=1, step=1, key=f"{key_prefix}_box")
        item = 0
        c5.text_input("Ítem", value="000", disabled=True, key=f"{key_prefix}_item_na")
        st.caption("Nivel caja/subcontenedor: Ítem=000 significa 'no aplica'.")
    else:
        caja = c4.number_input("Caja", min_value=1, value=1, step=1, key=f"{key_prefix}_box")
        item = c5.number_input("Ítem", min_value=1, value=1, step=1, key=f"{key_prefix}_item")
        st.caption("Nivel ítem: todos los componentes son positivos.")

    return barcode.build_standard_code(estanteria, piso, contenedor, caja, item)


def _generated_code(item_type: str, key_prefix: str) -> str:
    choice = st.selectbox(
        "Formato del código", list(FORMAT_OPTIONS), key=f"{key_prefix}_format",
        help="El sistema construye el código final, aplica ceros de relleno y lo valida.",
    )
    fmt = FORMAT_OPTIONS[choice]

    if fmt == barcode.FORMAT_STANDARD:
        return _standard_builder(item_type, key_prefix)

    if fmt == barcode.FORMAT_MESA:
        c1, c2 = st.columns(2)
        mesa = c1.selectbox("Mesa", [1, 2], key=f"{key_prefix}_table")
        equipo = c2.number_input("Número de equipo", min_value=1, value=1, step=1,
                                 key=f"{key_prefix}_equipment")
        return barcode.build_mesa_code(mesa, equipo)

    if fmt == barcode.FORMAT_LEGO:
        c1, c2 = st.columns(2)
        modelo = c1.text_input("Número de modelo", value="1", key=f"{key_prefix}_lego_number")
        digits = c2.number_input("Dígitos", min_value=1, max_value=8, value=2, step=1,
                                 key=f"{key_prefix}_lego_digits")
        return barcode.build_lego_code(modelo, digits)

    if fmt == barcode.FORMAT_NUMERIC:
        c1, c2 = st.columns(2)
        number = c1.text_input("Numeración", value="1", key=f"{key_prefix}_numeric_number")
        digits = c2.number_input(
            "Dígitos totales", min_value=1, max_value=barcode.NUMERIC_MAX_LEN,
            value=7, step=1, key=f"{key_prefix}_numeric_digits",
        )
        return barcode.build_numeric_code(number, digits)

    c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
    default_prefix = {"master": "CONT", "child": "SUB", "standalone": "LAB"}.get(item_type, "LAB")
    prefix = c1.text_input(
        "Prefijo", value=default_prefix, key=f"{key_prefix}_prefix",
        help="Letras sin tildes, números y separadores internos - _ .",
    )
    separator = c2.selectbox("Separador", ["-", "_", "."], key=f"{key_prefix}_separator")
    number = c3.text_input("Número", value="1", key=f"{key_prefix}_prefix_number")
    digits = c4.number_input("Dígitos", min_value=1, max_value=8, value=2, step=1,
                             key=f"{key_prefix}_prefix_digits")
    return barcode.build_prefixed_code(prefix, number, digits, separator)


def render_code_input(storage, item_type: str, key_prefix: str, initial_code: str = "") -> str:
    """Renderiza la seleccion/construccion y devuelve el codigo final.

    Con `initial_code` ofrece conservar el codigo escaneado como primera
    opcion. Sin codigo inicial, el generador asistido es la opcion por defecto.
    Los errores se muestran en vivo; la capa de almacenamiento valida de nuevo
    al enviar el formulario."""
    initial_code = (initial_code or "").strip()
    modes = [MODE_SCANNED, MODE_GENERATED, MODE_MANUAL] if initial_code else [MODE_GENERATED, MODE_MANUAL]
    mode = st.radio(
        "¿Cómo definirás el código?", modes, horizontal=True, key=f"{key_prefix}_mode"
    )

    code = ""
    error = None
    try:
        if mode == MODE_GENERATED:
            code = _generated_code(item_type, key_prefix)
        elif mode == MODE_SCANNED:
            code = initial_code
            st.text_input(
                "Código escaneado", value=code, disabled=True, key=f"{key_prefix}_scanned"
            )
        else:
            code = st.text_input(
                "Código de barras",
                value=initial_code,
                placeholder="Ej: 2-1-01-00-000, LAB-MIC-01 o 0012345",
                help="Se conserva exactamente el texto ingresado; solo se ignoran espacios alrededor.",
                key=f"{key_prefix}_manual",
            ).strip()

        if code:
            parsed = barcode.parse_code(code)
        else:
            parsed = None
    except ValueError as exc:
        parsed = None
        error = str(exc)

    if error:
        st.error(error)
        return code
    if not code:
        st.info("Ingresa la numeración necesaria para construir el código.")
        return ""

    st.success(f"Código listo: `{code}`")
    if parsed:
        description = barcode.describe_parsed(parsed)
        if description:
            st.caption(description)

        actual_level = barcode.standard_code_level(parsed)
        expected_level = _TYPE_LEVEL.get(item_type)
        if actual_level and expected_level and actual_level != expected_level:
            st.warning(
                f"El código representa el nivel '{actual_level}', pero seleccionaste "
                f"'{_TYPE_NAME.get(item_type, item_type)}'. Puedes continuar en modo manual, "
                "pero revisa que esa combinación sea intencional."
            )

    try:
        existing = storage.get_item(code) if storage else None
    except Exception:
        existing = None  # el guardado volvera a comprobarlo y mostrara el error real
    if existing and existing.get("status") != "retired":  # un código dado de baja queda libre
        st.warning(f"El código ya pertenece a: {existing.get('name') or 'un item existente'}.")

    if st.checkbox("Mostrar vista previa de la etiqueta", key=f"{key_prefix}_preview"):
        spec = labels.load_label_spec(storage) if storage else labels.LabelSpec()
        try:
            data = labels.generate_label_png_bytes(
                code, description="Nombre del producto", item_type=item_type, spec=spec,
            )
            st.image(
                data, caption=f"Vista previa a {spec.describe()} — {code}",
                width=min(spec.canvas_size[0], 640),
            )
            st.caption("La banda mostrará el nombre real. La ruta se deriva del código; categoría y ubicación se agregan al descargar.")
        except ValueError as exc:
            st.error(f"No se pudo generar la etiqueta: {exc}")

    st.caption(barcode.FORMAT_HELP)
    return code
