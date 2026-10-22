# Migración de infraestructura: Excel+GitHub → AWS

Este documento es el registro de diseño de la migración de
**UNIMINUTO-Lab-Inventory** (repositorio original, commit `bf05910`) hacia
una arquitectura sobre AWS. Es una migración de **infraestructura**, no de
producto: todas las reglas de negocio documentadas en el README original
(nomenclatura de códigos GLIOPS V3, matriz de roles y capacidades, flujo de
Despacho, ruta obligatoria de picking, devoluciones en 3 pasos, URL firmada
HMAC de NFC, impresión de etiquetas a tamaño real, cadena de custodia) se
conservan sin cambios. La interfaz (`views/*`, `core/ui.py`, `style.css`,
paleta institucional) tampoco cambia.

## 1. Tabla comparativa (arquitectura anterior vs. AWS)

| Capacidad | Arquitectura anterior | Arquitectura AWS | Estado |
|---|---|---|---|
| Base de datos | Excel (`UNIMINUTO_LAB_DB.xlsx`) en memoria de un proceso, sincronizado a un repo de GitHub por la API de Contenidos, con SHA remoto y resolución manual de conflictos | Amazon DynamoDB: 13 tablas on-demand (una por hoja + `user_email_guards` + `picking_state`), escrituras condicionales (`ConditionExpression`/`TransactWriteItems`) en vez de SHA/conflicto manual | **Hecho** (`core/storage.py`, `infra/`) |
| Fotos de objetos | Commit a un repo privado de GitHub (`fotos/<código>/...`), con el mismo token y lock que el Excel | Amazon S3 (mismo convenio de rutas), bucket con bloqueo público y cifrado SSE-S3; EXIF/GPS y redimensionado a 1024px sin cambios (es procesamiento local, no de red) | **Hecho** (`core/photos.py`, bucket en `infra/`) |
| Estado de picking en curso | `dict` en memoria del proceso Python (`core/picking.py`), se pierde si la app reinicia a mitad de un picking; impide correr más de una instancia | Tabla `picking_state` (PK `user_id`, SK `request_id`) con TTL nativo de 6h — **tabla ya creada en `infra/`, `core/picking.py` todavía NO migrado** | **Pendiente** |
| Autenticación | bcrypt + lista blanca de profesores en una hoja del Excel + código de verificación por correo (SMTP) | Amazon Cognito (User Pool) + Lambda trigger `PreSignUp` para restringir a dominios `.edu`/`.edu.co` + grupos de Cognito para profesor/administrador | **Pendiente** |
| Notificaciones por correo | SMTP directo (`smtplib`) | Amazon SES | **Pendiente** (WhatsApp sigue en Twilio: SES no cubre ese canal) |
| Reportes/exportación | `.xlsx` en memoria (`core/reports.py`) | Se conserva el `.xlsx` para el usuario final; además, volcado periódico a S3 (`analytics/`) para Athena/QuickSight | **Pendiente** |
| Hosting | Streamlit Cloud / proceso único | Contenedor Docker en ECS Fargate detrás de un ALB con HTTPS vía ACM | **Pendiente** |
| CI/CD | `.github/workflows/ci.yml`: sintaxis + pytest | + job de build de imagen, push a ECR, deploy a ECS en merge a `main` (sin reescribir el job de pruebas existente) | **Pendiente** |
| Trazabilidad física (opcional) | Toque NFC vía celular (URL firmada HMAC) | Extensión aditiva futura: lectores NFC fijos → MQTT → regla de AWS IoT Core → Lambda → `trace_events` | **Fuera de alcance por ahora** (el README original ya lo marca como fase opcional) |

## 2. Esquema DynamoDB (detalle de cada tabla)

Todas en modo **on-demand** (`PAY_PER_REQUEST`): presupuesto ajustado
(cuenta AWS en el plan gratuito / ~200 USD de crédito a 6 meses) y tráfico de
un solo laboratorio, no justifican capacidad reservada 24/7.

| Tabla | PK | SK | GSIs | Decisión clave |
|---|---|---|---|---|
| `items` | `id` | — | `parent_id-index` | `item_type` se filtra con Scan+Filter: el catálogo de un laboratorio es chico |
| `users` | `id` | — | `institutional_email-index` | Unicidad de correo forzada con tabla `user_email_guards` (ver §3) |
| `user_email_guards` | `institutional_email` | — | — | Tabla dedicada para no tener que filtrar un "item-candado" de cada Scan de `users` |
| `professors_whitelist` | `institutional_email` | — | — | Trivial |
| `loans` | `id` | — | `item_id-status-index`, `user_id-index`, `request_id-index` | — |
| `reservations` | `id` | — | `requester_id-index`, `scope_type-start_at-index` | La intersección real de horarios sigue siendo lógica de aplicación (DynamoDB no tiene "range overlap" nativo) |
| `service_requests` | `id` | — | `status-created_at-index`, `order_id-index`, `requester_id-index` | Lleva además un contador atómico `seq_counter` (ver §3) |
| `item_history` | `id` | — | `item_id-timestamp-index` | — |
| `settings` | `key` | — | — | Clave/valor, igual que antes |
| `trace_events` | `id` | — | `request_id-seq-index`, `item_id-created_at-index`, `loan_id-created_at-index`, `actor_id-created_at-index` | 4 GSIs completos desde el arranque (decisión explícita: evita Scan+Filter mientras se termina de auditar `views/*`) |
| `notification_log` | `id` | — | `log_bucket-created_at-index` (PK constante `"LOG"`) | Permite listar los últimos N en orden sin Scan completo |
| `item_photos` | `id` | — | `item_id-taken_at-index` | — |
| `picking_state` | `user_id` | `request_id` | — | TTL nativo = 6h (reemplaza `MAX_AGE_SECONDS`); tabla creada, **lógica de `core/picking.py` aún no migrada** |

## 3. Decisiones de diseño no obvias (el "por qué")

- **Unicidad de `institutional_email`**: DynamoDB no tiene restricción UNIQUE
  en un GSI. `create_user`/`update_user` escriben un ítem-candado en
  `user_email_guards` (PK = correo normalizado) dentro de la MISMA
  `TransactWriteItems` que crea/edita el usuario, con
  `ConditionExpression=attribute_not_exists(...)`. Si la condición falla, dos
  altas simultáneas con el mismo correo no pueden ambas tener éxito.
- **`on_loan` como contador atómico en el propio ítem**: la versión Excel
  calculaba la disponibilidad sumando los préstamos abiertos de un ítem en
  cada consulta. Aquí `items` mantiene un atributo `on_loan` que
  `create_loan`/`return_loan`/`transition_request` incrementan o decrementan
  con `ADD` dentro de la misma transacción que escribe el préstamo, con una
  `ConditionExpression` que impide reservar más stock del disponible. Esto es
  **más rápido de leer** (un ítem en vez de escanear `loans`) y, a diferencia
  de la versión anterior, **a prueba de condiciones de carrera entre
  instancias** (la versión Excel solo evitaba la carrera con un lock de un
  solo proceso).
- **`seq_counter` para los eventos de trazabilidad**: asignar `seq` dentro de
  un `request_id` requería, en la versión Excel, el lock global de todo el
  libro. Aquí se reserva con un `ADD seq_counter :n` atómico sobre la propia
  fila de `service_requests` (ver `core/storage.py:_reserve_seq`), válido
  incluso con varias tareas de ECS escribiendo al mismo tiempo. Puede dejar
  huecos en la secuencia si una transacción se cancela y se reintenta — no
  afecta el orden ni la deduplicación, que es para lo que se usa `seq`.
- **`transition_request` con reintento optimista**: la versión Excel
  serializaba TODA escritura con un lock de proceso. Aquí la atomicidad la da
  `TransactWriteItems` más un `ConditionExpression` sobre `updated_at` del
  pedido: si otra transición lo cambió entre la lectura y la escritura, la
  transacción se cancela (`TransactionCanceledException`) y se reintenta con
  datos frescos (hasta 3 veces) en vez de arriesgar un doble despacho.
- **Qué deuda técnica se elimina a propósito**: el SHA remoto, el estado de
  conflicto de sincronización y su resolución manual por el `maestro`
  (`resolve_sync_conflict`) desaparecen como MECANISMO — cada escritura en
  DynamoDB ya es inmediatamente consistente. Los métodos públicos que los
  exponían siguen existiendo (los llaman `app.py` y `core/ui.py` sin cambios)
  pero ahora devuelven un estado "siempre sincronizado" en vez de simular el
  mecanismo anterior. Ver el docstring de `core/storage.py` para el detalle.

## 4. Alcance de ESTA entrega y lo que queda pendiente

Lo que se pidió explícitamente para empezar — "(a) clonar y confirmar el
esquema real, (b) proponer tablas DynamoDB y bucket S3, (c) migrar
`core/storage.py` primero, preservando su API pública" — está hecho:

- `core/storage.py` reescrito sobre DynamoDB. La clase `LabStorage` conserva
  el mismo nombre y la misma firma en TODOS sus métodos públicos; `views/*`
  no se tocó.
- `core/photos.py` también se migró (a S3) porque dependía directamente de
  los helpers privados de GitHub de `core/storage.py` (`_github_request`,
  `_push_lock`, `EXCEL_PATH`) que ya no existen — dejarlo intacto habría roto
  la app al importar. Su procesamiento de imagen (EXIF/GPS, redimensionado)
  no cambió.
- `infra/` (CDK Python): las 13 tablas DynamoDB y el bucket S3, sintetizables
  pero **todavía no desplegados** (no se corrió `cdk bootstrap`/`cdk deploy`).

Pendiente, en el orden de la hoja de ruta original:

1. **`core/picking.py`** → tabla `picking_state` (ya creada en `infra/`).
   Hasta que esto no se migre, correr más de una tarea de ECS NO es seguro:
   además de `picking_state`, `core/dispatch.py` y
   `core/storage.py:get_events_index`/`data_state` cachean en memoria DEL
   PROCESO (ver nota de alcance al inicio de `core/storage.py`) — hay que
   auditarlos antes de escalar a más de una instancia.
2. Cognito (autenticación), SES (correo), ECS Fargate + ALB + ACM (hosting),
   extender `.github/workflows/ci.yml` (build+push a ECR+deploy), y el volcado
   a S3/Athena/QuickSight para reportes.
3. **Suite de pruebas** (`tests/`, ~80 archivos): la fixture `storage` que
   usan la mayoría de las pruebas (`tests/conftest.py`, clase `FakeStorage`)
   es independiente del backend y sigue funcionando sin cambios. Las pruebas
   que instancian `LabStorage()` directamente SÍ necesitan un mock de
   DynamoDB (p. ej. con la librería `moto`) antes de poder correr sin
   credenciales reales de AWS — no se hizo en esta entrega porque es, en sí
   mismo, un paquete de trabajo comparable en tamaño a la migración de
   `storage.py`. Se borraron únicamente `test_sync.py`, `test_sync_safety.py`
   y `test_sync_status.py`: probaban el mecanismo de SHA/conflicto que esta
   migración elimina a propósito (ver §3); dejarlos habría roto la recolección
   de `pytest` por un `ImportError` sobre nombres que ya no existen.
