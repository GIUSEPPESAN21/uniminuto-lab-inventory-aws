# Mejoras propuestas después de v1.15.0

Revisión exhaustiva del repositorio (main = `a512f0b`, v1.15.0), con énfasis en los cargos
(Cliente, Operario, Supervisor, Administrador). Nada de esto está implementado: es la lista.

**Cómo leerla**

- **Verif.**: `P` = lo comprobé yo (ejecutando código o leyendo la línea); `R` = lo reportó un revisor
  leyendo código y no lo he reproducido; `S` = supuesto o diseño propuesto.
- **Esf.**: S = horas, M = uno o dos días, L = más de dos días.
- **Prior.**: A = alta, M = media, B = baja.
- Las rutas `archivo:línea` salen de la revisión sobre `main` y pueden correrse unas líneas.

## Simulación (base temporal, personas ficticias, sin GitHub)

8 clientes, 4 operarios, 2 supervisores y 1 administrador; 12 pedidos con la ruta completa
(aprobar → tomar → picking con una lectura equivocada → recibir → entregar → confirmar).

| Comprobación | Resultado |
|---|---|
| Pedidos que cerraron el ciclo | 12 de 12 |
| Préstamos con operador registrado | 12 de 12 |
| Un operario "roba" un pedido ya tomado | bloqueado 12 de 12 |
| Cliente aprueba su propio pedido | bloqueado |
| Operario / Supervisor / Cliente editan inventario | bloqueado (3 de 3) |
| Operario y Supervisor cambian el escenario | bloqueado (2 de 2) |
| Supervisor o Cliente cambian un rol | bloqueado (2 de 2) |
| Operario y Cliente exportan el CSV de la lúdica | bloqueado (2 de 2) |
| Administrador cambia el escenario | permitido (correcto) |
| CSV de la lúdica | 120 eventos, 8 columnas exactas, sin nombres ni correos |
| Sembrar sobre un nombre de base real | rechazado |
| Tiempo por paso, local y sin GitHub | 65–80 ms |
| Lecturas con 250 pedidos (cola, métricas, CSV) | 1 ms / 82 ms / 25 ms |

Lectura: los permisos y el flujo aguantan; el coste está en las **escrituras** (cada una
reserializa el libro completo, 130–155 ms en local, mucho más con GitHub).

---

## A. Hacer primero (riesgo de pérdida de datos o de abuso)

| # | Mejora | Evidencia | Área | Esf. | Prior. | Verif. |
|---|---|---|---|---|---|---|
| 1 | Sanear celdas al escribir el Excel: quitar caracteres de control y forzar texto en celdas que empiezan por `=`, `+`, `-`, `@` | `core/storage.py:746-748`. Reproducido: un nombre `=1+1` queda vacío al recargar (pérdida silenciosa) y `\x01` lanza `IllegalCharacterError` tras 3 reintentos. En `SYNC_MODE=deferred` el fallo se reintenta cada 15 s sin persistir nunca | Todos | S | A | P |
| 2 | Descarga de GitHub: validar el archivo antes de reemplazar, conservar `.bak`, y no crear un libro vacío si el pull falla con 5xx | `storage.py:432` (reemplaza sin abrir), `1078-1083` (crea libro vacío), `1098` (`except Exception: pass`) | Datos | M | A | P (código) |
| 3 | Registro con verificación de correo para todos los cargos, tope de altas por hora, enfriamiento en «Reenviar código» y respuesta uniforme (sin enumerar correos) | `auth.py:42,56-61,271,283-301,316`; `login.py:64-71,107-116` | Cliente | M | A | R |
| 4 | `self_checkout_mode` configurable: apagado / solo con solicitud aprobada / libre. Hoy el Cliente retira sin aprobación | `permissions.py` (`CAP_CHECKOUT` en `_CLIENT`), `loans.py:44-50`, `escanear.py:92-131` | Cliente, Supervisor | M | A | P |
| 5 | Reservas: exigir capacidad en el dominio y bloquear la autoaprobación, incluida «laboratorio completo» | `reservations.py:111-124` (sin `require_cap`), `127-153` | Supervisor, Admin | S | A | P (sin `require_cap`); R (autoaprobación) |
| 6 | `resolve_sync_conflict` y lista blanca: pasar por `_authorize` con capacidad propia, `actor` y auditoría | `storage.py:1169`, `1660-1676`; `ui.py:439` compara `role` por texto; `usuarios.py:254,267` | Admin | S | A | P |
| 7 | Falsos conflictos y límites de GitHub: releer el SHA ante 409/422, leer `Retry-After`, backoff, mensaje propio para 403 por límite (hoy dice «token inválido») | `storage.py:221,461-465,528-533`; `photos.py:285-294` sube sin `_push_lock` | Datos | M | A | R |
| 8 | Respaldo diario cifrado del repo de datos, botón «snapshot» para el administrador y simulacro de restauración trimestral | No hay sección de respaldo en el README; `delete_item` borra definitivo (`storage.py:1034-1042`) | Admin | M | A | R |
| 9 | Privacidad (Ley 1581 de 2012): casilla de autorización versionada con fecha, política enlazada, retención, anonimizar usuario y auditar exportaciones. Validar con el oficial de datos | `login.py:95-117`; `reports.py:75-86` (exporta todos los usuarios sin auditar); no existe borrado de usuarios | Admin, Cliente | M | A | R (marco legal: S) |

## B. Cargos al máximo

| # | Mejora | Evidencia | Cargo | Esf. | Prior. | Verif. |
|---|---|---|---|---|---|---|
| 10 | El Supervisor (encargado) cumple lo que promete el README: cola «Por tomar» de solo lectura, reingresar préstamos, reautorizar y cancelar aprobados | `permissions.py` (`CHECKIN_ANY` y `VIEW_DISPATCH_QUEUE` no están en `_ATTEND`), `dispatch.py:435` (`authorize_request` sin llamadas en vistas), `solicitudes.py:206` | Supervisor | M | A | P |
| 11 | Inicio por cargo con KPI propios. Supervisor: por aprobar, por recibir, atascadas, vencidos. Operario: «Mi historial» y «Retomar picking» | `inicio.py:136-147`; `dispatch.my_history` (`dispatch.py:387`) no lo llama ninguna vista | Sup, Op | M | A | P |
| 12 | Pedido huérfano: validar que los operarios autorizados estén activos y ofrecer «abrir a todos» | `dispatch.py:200-221` | Sup, Op | S | A | R |
| 13 | Separación de funciones en la cadena: el que recibe no puede ser quien hizo el picking (con excepción configurable) | `permissions.py:78`, `dispatch.py:274-275` | Admin | S | A | R |
| 14 | Topes por cargo y doble aprobación: el Supervisor aprueba hasta N unidades, sobre M se piden dos aprobadores distintos; la autoaprobación del sistema solo ≤ N | `dispatch.py:196-207`, `service_requests.py:87-96` | Sup, Admin | L | M | R |
| 15 | Cancelación coherente: alinear el botón de la interfaz con lo que el dominio permite; cierre forzado con motivo para el Supervisor en `claimed/picked/received` | `solicitudes.py:80`, `dispatch.py:191`, `traceability.py:95-107` | Cl, Sup | M | M | R |
| 16 | SLA y escalado: umbral por etapa, insignia en Despacho, alerta en Inicio y correo de escalada | `trazabilidad.py:68-70`, `live_data.py:63` (30 min fijos, no avisa a nadie) | Sup, Admin | M | A | R |
| 17 | Carga y asignación justa: tope de pedidos simultáneos, carga visible, sugerencia al menos cargado, motivo categorizado al liberar o reasignar | `dispatch.py:212-234`, `despacho.py:253-256` | Op, Sup | M | M | R |
| 18 | Turnos y suplencias: `on_shift` por operario; delegación temporal con vigencia y auditoría (nunca a Administrador); rol secundario Cliente para quien es operario y estudiante | No existe en el código | Op, Sup | M | M | S |
| 19 | Definir qué distingue al Supervisor-encargado del Supervisor-profesor (hoy tienen la misma etiqueta y el profesor es un superconjunto) o fusionarlos con banderas | `permissions.py:62-77`; `inicio.py:19` habla de «tus cursos» sin concepto de curso | Sup | M | M | R |
| 20 | Matriz de permisos visible para el Administrador (solo lectura, generada de `ROLE_CAPS`) y guardar la versión de capacidades en cada evento de trazabilidad | `permissions.py:73-79`; `traceability.py:1246-1252` | Admin | S | A | R |
| 21 | Pestaña «Auditoría»: cambios de rol, estado, correo, lista blanca, con filtros y motivo | Solo se registra `role_changed` (`storage.py:1613-1624`) y ninguna vista lo lee | Admin | M | A | R |
| 22 | Cambios de rol con confirmación y motivo; doble control para «Administrador»; avisar de préstamos abiertos | `usuarios.py:140-166` (un clic) | Admin | M | M | R |
| 23 | Ciclo de vida de cuentas: vigencia o cohorte (`valid_until`), cierre de semestre que respete préstamos abiertos, alta por el Administrador, filtro por rol y paginación | `usuarios.py:155-166,240-241`; `auth.py:176,204` | Admin, Cl | M | A | R |
| 24 | Importación masiva de usuarios por CSV con vista previa, rol, vigencia y clave temporal; lista blanca por rol (hoy solo «profesor») | `storage.py:1649`, `usuarios.py:243-268` | Admin | M | M | R |
| 25 | Correos por cargo: matriz evento × cargo con suscripción personal. El encargado hoy no recibe ninguno | `email_events.py:46-53,183-191` | Todos | M | A | R |
| 26 | Eventos que faltan: autorizado → operarios; picking completo → supervisor; entregado → cliente; atasco → supervisor; reasignado → operario; cambio de rol → persona; vencimiento → prestatario (los préstamos de picking nacen sin fecha límite, `storage.py:2033`) | `email_events.py:536,835-850`; `login.py:15` muestra el id técnico del cargo | Todos | M | A | R |
| 27 | Ayuda por cargo: checklist de primer ingreso por capacidad, «¿por qué no puedo?» que nombre la capacidad que falta, `tutorial_seen` | `inicio.py:38-62,194` (guía única con Escanear aunque el Operario no lo tenga); `acerca_de.py:37-49` | Todos | M | M | R |
| 28 | `resolve_actor()`: releer al actor desde la base bajo el bloqueo en vez de confiar en el `dict` de sesión | `storage.py:1574,1967`; solo `admin_reset_password` relee (`auth.py:529-533`) | Datos | M | B | R |
| 29 | Test de matriz completo: hoy la tabla cubre 14 de 28 capacidades; añadir pruebas de separación de funciones (picker ≠ receptor, reservas) | `test_permission_matrix.py:71,100-115` | Desarrollo | S | A | R |

## C. Experiencia de usuario, accesibilidad y redacción

| # | Mejora | Evidencia | Área | Esf. | Prior. | Verif. |
|---|---|---|---|---|---|---|
| 30 | Confirmaciones que no desaparezcan: `st.success` + `st.rerun()` borra el mensaje. Generalizar el «flash» de Despacho en `core/ui.py` | `escanear.py:122-129,153-155,238-240`; `prestamos.py:48-49`; `solicitudes.py:83-85`; `reservas.py:52-54`; `inventario.py:112-114,713-721` (el resumen de errores del CSV se borra y el archivo sigue cargado) | Todos | S | A | R |
| 31 | Cargo en bruto en pantalla: se ve «estudiante» en vez de «Cliente». Usar `role_label` y ampliar `test_role_labels` | `prestamos.py:25` (`loan['user_role']`), `escanear.py:145`, `trazabilidad.py:92,272`; residuos en `solicitudes.py:253`, `ubicaciones.py:247`, `correos.py:75`, `usuarios.py:171,225`, `login.py:89`, `perfil.py:42` | Sup, Admin | S | A | P (prestamos.py:25) |
| 32 | Foco automático en todos los campos de lectura: mover `_focus_reading_input` a `core/ui.py` | `despacho.py:47-59`; faltan `escanear.py:252`, `location_guide.py:264`, `nfc.py:179`, `trazabilidad.py:202,240` | Op, Sup | S | A | R |
| 33 | Lectura por cámara que decodifique códigos de barras (BarcodeDetector con respaldo; iOS Safari no lo trae) | `escanear.py:252-255`, `despacho.py:208`, `nfc.py:178` solo reciben texto | Cl, Op, Sup | L | A | R |
| 34 | Doble envío: limpiar el formulario, avisar y deduplicar solicitudes y reservas repetidas | `solicitudes.py:149`, `reservas.py:78`, `service_requests.py:80-99` | Cliente | S | M | R |
| 35 | Confirmar y poder deshacer acciones sensibles: cancelar, entregar, liberar, cambiar rol, deshabilitar, reingresar | `solicitudes.py:81`, `despacho.py:119,149`, `usuarios.py:148,161,266`, `escanear.py:146` | Todos | M | M | R |
| 36 | Notificaciones dentro de la app: contador en la barra (por tomar, por revisar, aprobadas, vencidas) y recordatorio automático al Cliente | `inicio.py:65-95`, `correos.py:184-196` | Todos | M | A | R |
| 37 | Salida y reingreso por lote con «Confirmar todo» | `escanear.py:92-129`, `prestamos.py:74-75` | Cl, Sup | M | M | R |
| 38 | Catálogo largo en el celular: filtros en un expander y «Mostrar más» | `inventario.py:536-597` | Sup, Admin | M | M | R |
| 39 | Búsqueda global («ir a código o solicitud») y tarjetas «Retomar picking» o «Ruta pendiente» | `inventario.py:537`, `trazabilidad.py:159`, `usuarios.py:230`, `nfc.py:424` | Todos | M | M | R |
| 40 | Mensajes de error humanos con «Reintentar»; el acceso debe decir qué hacer si se olvidó la clave | `inicio.py:127,134,180`, `escanear.py:242`, `reportes.py:54`, `login.py:165-169` | Todos | S | M | R |
| 41 | Accesibilidad: bordes de botones ≥ 3:1 (hoy 1,42:1 y 1,78:1), estado de la línea de tiempo y de la torta no solo por color o ícono, área táctil de 44 px, popovers sin recorte | `style.css:51,58,506,545,1920,1939-1940`; `ui.py:277-293`; `reportes.py:92` | Todos | S | M | R |
| 42 | Tildes: decenas de textos sin acento; que `perfil.py:59` use `auth.MIN_PASSWORD_LENGTH`; prueba por lista de palabras | `ui.py:474-492`, `perfil.py:58-74`, `login.py:15,36-47,100`, `escanear.py:78-79` | Todos | S | M | R |
| 43 | Baja conectividad: alojar la fuente (hoy bloquea el primer render), banner de conexión, recomendar `SYNC_MODE=deferred` | `style.css:1`, `config.toml:12` | Todos | M | M | R |
| 44 | Carga percibida: índices por pedido, `st.cache_data` en listados, carga diferida de fotos y formularios | `despacho.py:85,106,136`; `solicitudes.py:197`; `inventario.py:510-513` | Todos | M | M | S (no medí) |

## D. Datos, seguridad y operación

| # | Mejora | Evidencia | Esf. | Prior. | Verif. |
|---|---|---|---|---|---|
| 45 | Observabilidad: `core/health.py` (último push OK, edad, pendientes, conflicto, tamaño del libro, p95), aviso por correo al fallar y al recuperar, Action que pinguee la app | `ui.py:461-495` (solo banner para editores) | M | A | R |
| 46 | Sacar del libro lo que escribe sin necesidad: revocaciones de sesión, auditoría de sesión, log de correos. Alerta a 1 GB de repo | `session.py:251-263`, `storage.py:2126-2149,2250`, `nfc.py:241` | M | A | R |
| 47 | Latencia de escritura creciente: archivar por año lo cerrado, tope de filas en la importación CSV, métrica `write_ms` | `storage.py:693-694,771,1282-1331` | M | M | R |
| 48 | Carreras e integridad: lock en `_load_cache`; unicidad de usuario dentro de la sección crítica; invariantes (cantidad > 0, no retirado) dentro del lock; `schema_version` y `validate_workbook()` que conserve columnas desconocidas | `storage.py:703-732,1526-1543,1682-1691,1841-1864,710-723` | M | M | R |
| 49 | Fuerza bruta y enumeración: backoff exponencial, clave secundaria por IP, lista de claves comunes | `auth.py:102-151,269` | S | M | R |
| 50 | NFC: exigir `NFC_SECRET`, límite por usuario y minuto, validar `isascii()` (hoy una firma no ASCII lanza `TypeError`), HMAC en recibos | `nfc.py:16-17,128-133,236`; `nfc_tap.py:108`; `traceability.py:492-499` | S | M | R |
| 51 | Fotos: bajar el tope de píxeles (hoy 60 MP), máximo por ítem | `photos.py:59,136-158` | S | M | R |
| 52 | Fechas en hora de Bogotá en toda la app (hoy `datetime.now()` en UTC en Cloud) | `escanear.py:109`, `reportes.py:136,197`, `loans.py:26-40` | S | B | R |
| 53 | Fallos silenciados: `logger.exception` en vez de `pass`; `_revoked.clear()` a las 10 000 reactiva sesiones cerradas, usar LRU | `session.py:170-177,262-264`, `auth.py:81-82,248`, `storage.py:1098` | S | M | R |
| 54 | Un solo token con poder total: repo separado para fotos y respaldos, protección de rama, rotación de PAT ≤ 90 días | `storage.py:300-305`, `photos.py:269` | M | M | R |
| 55 | Endurecimiento: `SESSION_SECRETS=[nuevo, viejo]` para rotar sin cerrar sesiones, cookie HttpOnly si es posible, `[server]` en `config.toml`, devcontainer sin XSRF apagado | `session.py:96-99,243-247`, `.devcontainer/devcontainer.json:20,22` | S | B | R |
| 56 | OAuth/Graph para enviar correo con Outlook (Microsoft desactiva SMTP básico por defecto a fines de dic. 2026 en tenants existentes) | `core/notifications.py` solo soporta usuario y clave | L | M | S |
| 57 | Auditoría inmutable: hash encadenado en trazabilidad, baja lógica de préstamos | `storage.py:2198`, `2097` | M | B | R |

## E. Calidad, documentación y estructura

| # | Mejora | Evidencia | Esf. | Prior. | Verif. |
|---|---|---|---|---|---|
| 58 | CI: `permissions:` mínimos, acciones fijadas por SHA, `pip-audit`, CodeQL, escaneo de secretos, ruff con `I,B,UP,S`, pre-commit, cobertura 60 → 75 | `ci.yml`, `ruff.toml:7` | M | M | R |
| 59 | Pruebas por flujo en la interfaz (AppTest): cambio de clave, `scan_form`, Tomar/Liberar/Entregar, «Recibí mi pedido», importación CSV, rol y deshabilitar. Habrían atrapado el #30 | `test_ui_views.py` | M | A | R |
| 60 | Pruebas de contrato entre `FakeStorage` y `LabStorage`; reloj fijo en `test_loans.py` y `test_atomic_rules.py` (≈22 `datetime.now()`); sin `sleep` en `test_dispatch_performance.py:181-187,267` | `conftest.py` | S | M | R |
| 61 | Regresión visual y accesibilidad automatizadas (Playwright + axe-core a 360, 768 y 1280 px, claro y oscuro) | `test_ui_components.py:223-280` solo comprueba texto del CSS | L | M | R |
| 62 | Dividir el README (763 líneas) en `docs/`; archivar versiones antiguas del CHANGELOG | `README.md` | M | M | R |
| 63 | Runbook y guía de la lúdica: lista previa (SESSION_SECRET, SYNC_MODE, chips, impresora), incidentes (conflicto, token vencido, SMTP caído), respaldo, diagramas | README:740-752 solo menciona scripts | M | A | R |
| 64 | Guía única de Secrets en `docs/` más un panel «Diagnóstico» que diga si cada secret está definido o falta, sin mostrar valores | `secrets.toml.example:25-26` y `README:707` ya se contradicen | M | A | R |
| 65 | `core/version.py` y mostrar la versión en Acerca de y el pie; Acerca de omite Despacho, Ubicaciones, NFC y Trazabilidad | `acerca_de.py:26-55` | S | M | R |
| 66 | Dividir archivos grandes (`storage.py` 2456 líneas, `inventory_suggestions.py` 2438, `labels.py` 1462, `traceability.py` 1324); extraer duplicados a `views/_shared.py`; eliminar código muerto (`guard_role`, `centered_logo`, `dispatch.py:372,387,435`, `traceability.py:1230`) | varios | L | B | R |
| 67 | Ruta de migración a Postgres gestionado cuando se cumpla un disparador: p95 de escritura > 2 s, libro > 10 MB o > 300 000 celdas, más de una réplica, repo > 1 GB o necesidad real de supresión. Pasos: formalizar `LabStorage` como interfaz → DDL desde `SHEET_COLUMNS` → `PostgresStorage` → script de migración → doble escritura en sombra → secret `STORAGE_BACKEND` con exportación diaria a Excel como retorno | `storage.py:32-96` | L | B | R |

## Estado

**v1.16.0 «Datos a salvo» (hecho):** #1 (texto seguro en el Excel), #2 (descarga validada y sin arranque con base vacía),
#4 (`self_checkout_mode`, por omisión «Libre»), #6 (conflicto de sincronización y lista blanca con capacidad y constancia),
#7 (409 falsos, límite de tasa y candado compartido con las fotos) y #29 (la matriz lista todas las capacidades).
**Pendiente a propósito:** #5 (Reservas), porque en v1.15 se pidió no tocar Reservas; basta con confirmar que ya se puede.

## Orden sugerido

1. **v1.16.0 «Datos a salvo»**: #1, #2, #7, #5, #6, #4 y #29 (cierran pérdida silenciosa, arranque con base vacía y los dos huecos de autorización que quedan).
2. **v1.17.0 «Cargos al máximo»**: #10, #11, #12, #13, #20, #21, #22, #25, #26 y #16.
3. **v1.18.0 «Experiencia»**: #30, #31, #32, #34, #35, #36, #40, #42 y #59.
4. **Antes de la lúdica**: #45, #63, #64 y #8.
5. **Cuando haya tiempo**: #33 (cámara), #56 (correo OAuth), #14, #18, #23, #24 y la migración (#67).

## Descartado de la revisión

- `PROMPT_CONTEXTO_INVESTIGACION.md` en un repositorio público: es un archivo local sin versionar
  (`git ls-files` vacío, y GitHub no lo tiene) y solo trae cifras agregadas. No hay exposición.
- Lo que parecía un hueco de edición de inventario en la simulación fue un error de mi script
  (una clave inexistente); con la corrección, los tres cargos quedan bloqueados.
