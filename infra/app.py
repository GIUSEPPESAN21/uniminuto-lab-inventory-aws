#!/usr/bin/env python3
"""Punto de entrada del CDK app: infraestructura de datos de UNIMINUTO-Lab-Inventory-AWS.

La Region NO se fija aqui: toda cuenta de este proyecto AWS (modelo "nueva
experiencia AWS") tiene una unica Region asignada por settings.aws.com. Pasa
`CDK_DEFAULT_REGION`/`CDK_DEFAULT_ACCOUNT` (los deja el entorno si corriste
`aws configure`/`aws login`) o define `region`/`account` como contexto de CDK
(`cdk.json` o `-c account=... -c region=...`) antes de `cdk deploy`.
"""

import os

import aws_cdk as cdk

from uniminuto_infra.data_stack import UniminutoDataStack

app = cdk.App()

account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION")

UniminutoDataStack(
    app,
    "UniminutoLabInventoryDataStack",
    env=cdk.Environment(account=account, region=region),
    description="DynamoDB (una tabla por hoja del Excel original) + S3 (fotos/exports/analytics) "
                "para Inventario de Laboratorio UNIMINUTO - variante AWS.",
    termination_protection=True,
)

app.synth()
