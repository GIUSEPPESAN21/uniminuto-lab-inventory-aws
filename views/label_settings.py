# -*- coding: utf-8 -*-
"""views/label_settings.py - Tamaño real de la etiqueta, contenido, vista previa
y hoja de prueba de impresion. La configuracion se guarda en la base y la usan
todas las etiquetas de la app (Inventario, Escanear y registro de items)."""

import io

import streamlit as st

from core import labels
from core.barcode import is_valid_code
from core.labels import LABEL_CONTENT_OPTIONS, LABEL_SIZE_PRESETS, LabelSpec

_CUSTOM = "custom"
_SAMPLE_ITEM = {
    "id": "2-1-01-01-001", "name": "Multímetro digital", "item_type": "standalone",
    "category": "Instrumentación", "location": "Laboratorio 3",
}

PRINT_GUIDE = """
**Antes de empezar:** mide la etiqueta con una regla (ancho × alto, sin el papel de
soporte), elige esa medida arriba y guarda. Todo lo demás debe usar **la misma medida**.

**1 · Driver de la SAT TT460 (una sola vez)**

*Configuración de Windows → Bluetooth y dispositivos → Impresoras y escáneres → SAT TT460 UE
→ Preferencias de impresión.* Repite los cambios en *Propiedades de la impresora → Opciones
avanzadas → Valores predeterminados de impresión*. Los nombres de las pestañas pueden variar
un poco según la versión del driver.

- **Configuración de página (Page Setup):** tamaño **definido por el usuario** (en el cuadro
  de impresión aparece como «USER») con el ancho y el alto de tu etiqueta. Si el cuadro de
  impresión muestra «USER (50,8 × 50,8 mm)», ese es el tamaño que hay que cambiar: con él la
  etiqueta sale recortada o pequeña.
- **Papel / Stock:** etiquetas con separación entre ellas (no continuo).
- **Gráficos (Graphics):** 203 dpi y tramado (**Dithering**) en **Ninguno**.
- **Opciones:** velocidad y oscuridad medias; sube la oscuridad un nivel si las barras salen
  claras.

**2 · Imprime siempre el PDF, desde Edge o Chrome**

- Clic derecho en el PDF → *Abrir con* → **Microsoft Edge**, y **Ctrl + P**.
- Impresora: SAT TT460 UE · Tamaño del papel: **USER** con tu medida · Escala:
  **Predeterminado** (o **Tamaño real**, si aparece) · Márgenes y encabezados: ninguno, si el
  cuadro los muestra.
- La vista previa debe mostrar la etiqueta **completa, derecha y llenando el papel**. Si se
  ve girada, cambia *Diseño* (vertical u horizontal); si se ve pequeña en una esquina, el
  papel no es el correcto.
- **No imprimas el PNG ni abras la etiqueta con la app Fotos:** *Rellenar página* recorta la
  imagen y la agranda con un factor que no es entero, y las barras salen irregulares.

**3 · Una etiqueta de prueba antes del lote**

Descarga la **hoja de prueba**, imprímela con esos mismos ajustes y revisa:

- que el marco se vea completo, con sus cuatro lados;
- que la regla mida lo que dice (con una regla real);
- que las tres rejillas de barras se vean parejas (con una lupa o la cámara del celular).

Si algo falla, busca tu caso en la tabla de abajo.
"""

SYMPTOMS_GUIDE = """
| Lo que ves | Causa más probable | Qué hacer |
|---|---|---|
| **Recortada y ampliada** (falta un borde) | Se imprimió el PNG desde Fotos con *Rellenar página*, o el papel del driver es más chico que la imagen | Imprime el PDF desde Edge o Chrome con el papel USER de tu medida |
| **Pequeña**, en el centro o en una esquina | El papel del driver o la medida guardada aquí no es la del rollo | Mide la etiqueta y usa la misma medida aquí y en el driver |
| **Girada 90°** | Orientación | Cambia *Diseño* (vertical u horizontal) hasta ver el texto derecho |
| **Borrosa**, letras ásperas o barras desiguales | La imagen se reescaló (Fotos, *Ajustar*, escala distinta de 100 %) o el driver aplica tramado | PDF, escala Predeterminado y *Dithering: Ninguno* |
| **Marco cortado** en un lado | Papel del driver más pequeño que la etiqueta, o rollo corrido | Revisa el papel y centra las guías del rollo |
| **Se desfasa** o salta de etiqueta en etiqueta | Sensor sin calibrar o tipo de papel equivocado | Calibra el sensor (botón de calibración de la impresora) y elige etiquetas con separación |
| **Muy clara o muy oscura** | Oscuridad o velocidad del driver | Ajusta la oscuridad en pasos pequeños |
"""


def _decimal(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


# Los rollos de 4 pulgadas de ancho suelen venderse como 100 mm y el driver los
# nombra en pulgadas: se muestran las dos medidas para reconocerlos.
_INCH_NAMES = {"100x50": "4 × 2 in", "100x75": "4 × 3 in", "100x100": "4 × 4 in", "100x150": "4 × 6 in"}


def _preset_name(key: str) -> str:
    if key == _CUSTOM:
        return "Personalizado…"
    width, height = LABEL_SIZE_PRESETS[key]
    suffix = f" (≈ {_INCH_NAMES[key]})" if key in _INCH_NAMES else ""
    return f"{width} × {height} mm{suffix}"


def _preview_items(storage) -> dict:
    options = {"Ejemplo: Multímetro digital (2-1-01-01-001)": _SAMPLE_ITEM}
    try:
        items = storage.get_all_items()
    except Exception:
        items = []
    for item in items:
        if is_valid_code(item.get("id") or ""):
            options[f"{item.get('name')} ({item.get('id')})"] = item
    return options


def _spec_from_inputs(saved: LabelSpec):
    """Lee los controles y devuelve (spec, error)."""
    keys = list(LABEL_SIZE_PRESETS) + [_CUSTOM]
    size_col, dpi_col = st.columns([3, 2])
    preset = size_col.selectbox(
        "Tamaño del rollo de etiquetas", keys, index=keys.index(saved.preset_key),
        format_func=_preset_name, key="label_cfg_preset",
    )
    dpi = dpi_col.radio(
        "Resolución de la impresora", labels.SUPPORTED_DPI,
        index=labels.SUPPORTED_DPI.index(saved.dpi), horizontal=True,
        format_func=lambda value: f"{value} dpi" + (" · SAT TT460" if value == labels.LABEL_DPI else ""),
        key="label_cfg_dpi",
    )
    if preset == _CUSTOM:
        width_col, height_col = st.columns(2)
        width = width_col.number_input(
            "Ancho (mm)", min_value=float(labels.MIN_LABEL_WIDTH_MM),
            max_value=float(labels.MAX_LABEL_WIDTH_MM), value=float(saved.width_mm),
            step=1.0, key="label_cfg_width",
        )
        height = height_col.number_input(
            "Alto (mm)", min_value=float(labels.MIN_LABEL_HEIGHT_MM),
            max_value=float(labels.MAX_LABEL_HEIGHT_MM), value=float(saved.height_mm),
            step=1.0, key="label_cfg_height",
        )
    else:
        width, height = LABEL_SIZE_PRESETS[preset]

    st.markdown("**Contenido opcional**")
    columns = st.columns(3)
    chosen = [
        key for index, (key, text) in enumerate(LABEL_CONTENT_OPTIONS.items())
        if columns[index % 3].checkbox(text, value=saved.shows(key), key=f"label_cfg_{key}")
    ]
    st.caption(
        "La etiqueta conserva su formato de siempre (logo, banda negra con el nombre, ruta y "
        "datos, barras y código) a escala del tamaño elegido. Solo se ajusta el contenido: si un "
        "texto no cabe, se reduce o pasa a una segunda línea y, como último recurso, se acorta."
    )
    try:
        return LabelSpec(width, height, dpi, tuple(chosen)), None
    except ValueError as exc:
        return None, str(exc)


def _render_preview(storage, spec: LabelSpec) -> None:
    options = _preview_items(storage)
    choice = st.selectbox("Vista previa con", list(options), key="label_cfg_sample")
    try:
        layout = labels.layout_item_label(options[choice], spec)
    except ValueError as exc:
        st.error(f"No se puede imprimir este código en {spec.size_text}: {exc}")
        return
    image = labels.render_label(layout)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    width, height = spec.canvas_size
    st.image(
        buffer.getvalue(), width=min(width, 640),
        caption=f"Vista previa a resolución real: {spec.describe()} ({width} × {height} puntos)",
    )
    stats = st.columns(3)
    stats[0].metric("Texto más pequeño", f"{_decimal(layout.min_text_pt, 1)} pt")
    stats[1].metric("Alto de las barras", f"{_decimal(layout.bar_height_mm, 1)} mm")
    stats[2].metric("Ancho de barra mínima", f"{_decimal(layout.module_mm, 2)} mm")
    omitted = labels.describe_omitted(layout)
    if omitted:
        st.info(
            f"En {spec.size_text} no caben sin cortar palabras: **{', '.join(omitted)}**. "
            "Usa una etiqueta más grande o desactiva otro contenido para darles espacio."
        )
    if "name" in layout.shortened:
        st.warning("El nombre es largo para este tamaño y se recortó con «…».")
    for note in layout.warnings:
        st.warning(note)


def _render_paper_info(spec: LabelSpec) -> None:
    """La medida que debe tener el papel del driver y del cuadro de impresión."""
    with st.container(border=True):
        st.markdown(
            f"**:material/receipt_long: Papel al imprimir: {spec.size_text} ({spec.inches_text}).** Define ese mismo "
            "tamaño («USER», definido por el usuario) en el driver de la impresora y elígelo en "
            "el cuadro de impresión del PDF. Si el cuadro muestra otra medida (por ejemplo "
            "«USER (50,8 × 50,8 mm)»), la etiqueta saldrá recortada o pequeña."
        )


def render(storage, user: dict) -> None:
    saved = labels.load_label_spec(storage)
    st.caption(
        f"Configuración guardada: **{saved.describe()}**. Todas las etiquetas de la app "
        "(Inventario, Buscar y registro de items) se generan con este tamaño."
    )
    spec, error = _spec_from_inputs(saved)
    if error:
        st.error(error)
        return

    _render_preview(storage, spec)
    _render_paper_info(spec)

    save_col, test_col = st.columns(2)
    changed = spec != saved
    if save_col.button(
        ":material/save: Guardar configuración", type="primary", disabled=not changed,
        use_container_width=True, key="label_cfg_save",
    ):
        labels.save_label_spec(storage, spec, actor_email=user.get("institutional_email", ""))
        st.toast(f"Etiquetas configuradas a {spec.describe()}", icon=":material/label:")
        st.rerun()
    test_col.download_button(
        ":material/print: Hoja de prueba de impresión (PDF)",
        data=labels.generate_calibration_pdf_bytes(spec),
        file_name=f"prueba_impresion_{spec.width_mm:g}x{spec.height_mm:g}mm.pdf",
        mime="application/pdf", use_container_width=True, key="label_cfg_test",
        help="Imprímela con los mismos ajustes que tus etiquetas: revisa el marco, mide la "
             "regla y mira las rejillas.",
    )
    if changed:
        st.caption(
            "Hay cambios sin guardar: la vista previa y la hoja de prueba ya los usan, pero las "
            f"etiquetas del inventario siguen en {saved.describe()} hasta que guardes."
        )
    with st.expander(":material/print: Cómo imprimir a tamaño real, paso a paso", expanded=saved == LabelSpec()):
        st.markdown(PRINT_GUIDE)
    with st.expander(":material/search: Mi etiqueta sale mal: qué significa cada caso"):
        st.markdown(SYMPTOMS_GUIDE)
