# Infraestructura (AWS CDK, Python)

Define las 12 tablas DynamoDB (una por hoja del Excel original, mas
`picking_state`) y el bucket S3 de fotos/exportes/analitica. Ver
`../docs/architecture/aws-migration-mapping.md` para el diseño completo y
por qué cada decisión.

## Requisitos

- Node.js >= 20 (lo usa el CDK Toolkit por debajo, aunque el app sea Python)
- Python >= 3.9
- `aws configure` o `aws login` ya hechos, apuntando a la Región asignada a
  tu proyecto (confírmala en AWS Settings > View all projects > Overview >
  Additional Info > Region, o en `~/.aws/config`)

## Uso

```bash
cd infra
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
npm install -g aws-cdk        # una sola vez por máquina

cdk bootstrap aws://<ACCOUNT_ID>/<REGION>   # una sola vez por cuenta+región
cdk synth
cdk diff
cdk deploy
```

No se ha corrido `cdk bootstrap`/`cdk deploy` todavía: este stack solo está
escrito y sintetizable. Repasa `cdk diff` contra una cuenta real antes del
primer deploy, y confirma la Región (ver arriba) — todas las tablas y el
bucket se crean ahí, salvo que decidas moverlo a `us-east-1` únicamente por
WAF/CloudFront global, que no aplica a esta pila.
