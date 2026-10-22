# -*- coding: utf-8 -*-
"""Pila de datos: una tabla DynamoDB por hoja del Excel original + un bucket S3.

Reemplaza core/storage.py (Excel local + sincronizacion manual a GitHub por
SHA) y la carpeta `fotos/` del repositorio privado de datos. Ver
docs/architecture/aws-migration-mapping.md para la justificacion de cada
decision (por que estas claves, por que estos GSIs, que se elimina).

Todas las tablas usan PAY_PER_REQUEST (facturacion on-demand): este proyecto
tiene presupuesto ajustado (plan gratuito + ~200 USD de credito a 6 meses) y
el trafico de un solo laboratorio no justifica capacidad reservada 24/7.
"""

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Tablas que conservan datos del laboratorio (inventario, personas, prestamos,
# trazabilidad...): RETAIN. Borrar la pila por accidente no debe borrar la
# base de datos real; a esta escala el costo de almacenamiento on-demand de
# DynamoDB es marginal (centavos/mes), así que no hay motivo para arriesgarlo.
_RETAIN = RemovalPolicy.RETAIN


class UniminutoDataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.tables: dict[str, dynamodb.Table] = {}

        self._add_items_table()
        self._add_users_table()
        self._add_user_email_guards_table()
        self._add_professors_whitelist_table()
        self._add_loans_table()
        self._add_reservations_table()
        self._add_service_requests_table()
        self._add_item_history_table()
        self._add_settings_table()
        self._add_trace_events_table()
        self._add_notification_log_table()
        self._add_item_photos_table()
        self._add_picking_state_table()

        self._add_photos_bucket()

    # ------------------------------------------------------------------
    # Tablas
    # ------------------------------------------------------------------

    def _new_table(self, name: str, partition_key: str, sort_key: str = None,
                    removal_policy: RemovalPolicy = _RETAIN) -> dynamodb.Table:
        table = dynamodb.Table(
            self, f"{name.title().replace('_', '')}Table",
            table_name=f"uniminuto-lab-{name.replace('_', '-')}",
            partition_key=dynamodb.Attribute(name=partition_key, type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name=sort_key, type=dynamodb.AttributeType.STRING) if sort_key else None,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=removal_policy,
            point_in_time_recovery=removal_policy is _RETAIN,
        )
        self.tables[name] = table
        return table

    @staticmethod
    def _add_gsi(table: dynamodb.Table, index_name: str, partition_key: str, sort_key: str = None) -> None:
        table.add_global_secondary_index(
            index_name=index_name,
            partition_key=dynamodb.Attribute(name=partition_key, type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name=sort_key, type=dynamodb.AttributeType.STRING) if sort_key else None,
        )

    def _add_items_table(self) -> None:
        # Catalogo (productos, contenedores maestros/hijos, ubicaciones). `item_type`
        # se filtra con Scan+Filter: el catalogo de un solo laboratorio es chico y no
        # justifica un GSI mas.
        table = self._new_table("items", "id")
        self._add_gsi(table, "parent_id-index", "parent_id")

    def _add_users_table(self) -> None:
        table = self._new_table("users", "id")
        self._add_gsi(table, "institutional_email-index", "institutional_email")

    def _add_user_email_guards_table(self) -> None:
        # DynamoDB no tiene UNIQUE en indices: la unicidad de `institutional_email`
        # se fuerza con un item-candado en ESTA tabla (pk = correo normalizado),
        # escrito/borrado en la MISMA TransactWriteItems que crea/edita el usuario
        # (ConditionExpression attribute_not_exists al crear). Tabla separada en vez
        # de meter el candado en `users` para no tener que filtrarlo de cada Scan.
        self._new_table("user_email_guards", "institutional_email")

    def _add_professors_whitelist_table(self) -> None:
        self._new_table("professors_whitelist", "institutional_email")

    def _add_loans_table(self) -> None:
        table = self._new_table("loans", "id")
        self._add_gsi(table, "item_id-status-index", "item_id", "status")
        self._add_gsi(table, "user_id-index", "user_id")
        self._add_gsi(table, "request_id-index", "request_id")

    def _add_reservations_table(self) -> None:
        # El GSI por scope_type+start_at acota candidatas a un rango de fechas; la
        # interseccion real de horarios (core/reservations.py:find_conflict) sigue
        # siendo logica de aplicacion, DynamoDB no tiene "range overlap" nativo.
        table = self._new_table("reservations", "id")
        self._add_gsi(table, "requester_id-index", "requester_id")
        self._add_gsi(table, "scope_type-start_at-index", "scope_type", "start_at")

    def _add_service_requests_table(self) -> None:
        table = self._new_table("service_requests", "id")
        self._add_gsi(table, "status-created_at-index", "status", "created_at")
        self._add_gsi(table, "order_id-index", "order_id")
        self._add_gsi(table, "requester_id-index", "requester_id")

    def _add_item_history_table(self) -> None:
        table = self._new_table("item_history", "id")
        self._add_gsi(table, "item_id-timestamp-index", "item_id", "timestamp")

    def _add_settings_table(self) -> None:
        self._new_table("settings", "key")

    def _add_trace_events_table(self) -> None:
        # La tabla mas consultada (cadena de custodia). 4 GSIs completos desde el
        # arranque (decision del usuario): evita Scan+Filter mientras se termina de
        # migrar views/* modulo por modulo; a esta escala el costo adicional de
        # almacenamiento por los GSIs es marginal.
        table = self._new_table("trace_events", "id")
        self._add_gsi(table, "request_id-seq-index", "request_id", "seq")
        self._add_gsi(table, "item_id-created_at-index", "item_id", "created_at")
        self._add_gsi(table, "loan_id-created_at-index", "loan_id", "created_at")
        self._add_gsi(table, "actor_id-created_at-index", "actor_id", "created_at")

    def _add_notification_log_table(self) -> None:
        # `log_bucket` es siempre la constante "LOG": permite un Query ordenado por
        # fecha (GSI) en vez de un Scan completo para ver los ultimos N correos.
        table = self._new_table("notification_log", "id", removal_policy=RemovalPolicy.DESTROY)
        self._add_gsi(table, "log_bucket-created_at-index", "log_bucket", "created_at")

    def _add_item_photos_table(self) -> None:
        table = self._new_table("item_photos", "id")
        self._add_gsi(table, "item_id-taken_at-index", "item_id", "taken_at")

    def _add_picking_state_table(self) -> None:
        # Reemplaza el dict en memoria de core/picking.py. TTL nativo = 6 h
        # (MAX_AGE_SECONDS de hoy) sin codigo de limpieza adicional. Dato puramente
        # transitorio: DESTROY es seguro (nada se pierde que no se pueda retomar).
        table = self._new_table("picking_state", "user_id", "request_id", removal_policy=RemovalPolicy.DESTROY)
        table.add_time_to_live_attribute("expires_at")

    # ------------------------------------------------------------------
    # S3 (fotos + exportes + analitica)
    # ------------------------------------------------------------------

    def _add_photos_bucket(self) -> None:
        # Nombre con el Account ID para que sea unico globalmente (regla de S3).
        self.photos_bucket = s3.Bucket(
            self, "PhotosBucket",
            bucket_name=f"uniminuto-lab-inventory-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,  # ahorra almacenamiento; no es critico para este caso de uso
            removal_policy=_RETAIN,
            lifecycle_rules=[
                # exports/ y analytics/ son regenerables desde DynamoDB; no hace
                # falta conservarlos indefinidamente. fotos/ no tiene regla: son el
                # unico original de cada imagen.
                s3.LifecycleRule(
                    id="expire-exports-and-analytics",
                    prefix="exports/",
                    expiration=Duration.days(90),
                ),
                s3.LifecycleRule(
                    id="expire-analytics-dumps",
                    prefix="analytics/",
                    expiration=Duration.days(180),
                ),
            ],
        )
