import boto3
import json
import time
import zipfile
import uuid

# Configuración inicial
REGION = 'us-east-1'
UNIQUE_ID = str(uuid.uuid4())[:6]
PROJECT_NAME = f"cloudvault-{UNIQUE_ID}"

iam = boto3.client('iam', region_name=REGION)
s3 = boto3.client('s3', region_name=REGION)
dynamodb = boto3.client('dynamodb', region_name=REGION)
lam = boto3.client('lambda', region_name=REGION)
apigw = boto3.client('apigateway', region_name=REGION)

print(f" Iniciando despliegue automático de {PROJECT_NAME} en AWS...")

# 1. CREAR TABLA EN DYNAMODB (Base de datos NoSQL)
table_name = f"{PROJECT_NAME}-tasks"
print(f"1/6 Creando tabla DynamoDB: {table_name}...")
dynamodb.create_table(
    TableName=table_name,
    KeySchema=[{'AttributeName': 'id', 'KeyType': 'HASH'}],
    AttributeDefinitions=[{'AttributeName': 'id', 'AttributeType': 'S'}],
    BillingMode='PAY_PER_REQUEST' # Escala automáticamente y entra en capa gratuita
)
dynamodb.get_waiter('table_exists').wait(TableName=table_name)

# 2. CREAR BUCKET S3 PARA ARCHIVOS Y HOSTING WEB
bucket_name = f"{PROJECT_NAME}-web-storage"
print(f"2/6 Creando Bucket S3: {bucket_name}...")
s3.create_bucket(Bucket=bucket_name)

# Desactivar bloqueo de acceso público para permitir alojar la web estática
s3.delete_public_access_block(Bucket=bucket_name)
time.sleep(2)

# Política para que index.html sea visible como página web, pero los archivos subidos sean privados
bucket_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "PublicReadWebIndex",
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{bucket_name}/index.html"
    }]
}
s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(bucket_policy))

# Configurar CORS en S3 para permitir subida directa desde el navegador
s3.put_bucket_cors(
    Bucket=bucket_name,
    CORSConfiguration={
        'CORSRules': [{
            'AllowedHeaders': ['*'],
            'AllowedMethods': ['GET', 'PUT', 'POST', 'DELETE'],
            'AllowedOrigins': ['*'],
            'ExposeHeaders': ['ETag']
        }]
    }
)

# Habilitar alojamiento de sitio web estático
s3.put_bucket_website(
    Bucket=bucket_name,
    WebsiteConfiguration={'IndexDocument': {'Suffix': 'index.html'}}
)

# 3. CREAR ROL DE SEGURIDAD (IAM) PARA LAMBDA
role_name = f"{PROJECT_NAME}-lambda-role"
print(f"3/6 Configurando permisos IAM: {role_name}...")
assume_role_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}

try:
    role = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(assume_role_policy)
    )
    role_arn = role['Role']['Arn']
    iam.attach_role_policy(RoleName=role_name, PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole')
    iam.attach_role_policy(RoleName=role_name, PolicyArn='arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess')
    iam.attach_role_policy(RoleName=role_name, PolicyArn='arn:aws:iam::aws:policy/AmazonS3FullAccess')
    print("    Esperando 10 segundos a que AWS propague los permisos...")
    time.sleep(10)
except Exception as e:
    # Fallback para cuentas restringidas como AWS Academy (usa LabRole existente)
    print("    Usando LabRole preexistente (Entorno Educativo)...")
    role_arn = iam.get_role(RoleName='LabRole')['Role']['Arn']

# 4. EMPAQUETAR Y DESPLEGAR FUNCIÓN AWS LAMBDA
func_name = f"{PROJECT_NAME}-backend"
print(f"4/6 Desplegando código Backend en AWS Lambda: {func_name}...")
with zipfile.ZipFile('function.zip', 'w') as zf:
    zf.write('lambda_function.py')

with open('function.zip', 'rb') as f:
    zipped_code = f.read()

lam_response = lam.create_function(
    FunctionName=func_name,
    Runtime='python3.11',
    Role=role_arn,
    Handler='lambda_function.lambda_handler',
    Code={'ZipFile': zipped_code},
    Timeout=15,
    Environment={'Variables': {'TABLE_NAME': table_name, 'BUCKET_NAME': bucket_name}}
)
lambda_arn = lam_response['FunctionArn']

# 5. CREAR API GATEWAY (EL PUENTE HTTPS)
print("5/6 Construyendo API Gateway REST...")
api = apigw.create_rest_api(name=f"{PROJECT_NAME}-api", endpointConfiguration={'types': ['REGIONAL']})
api_id = api['id']
root_id = apigw.get_resources(restApiId=api_id)['items'][0]['id']

# Crear recurso /tasks
resource = apigw.create_resource(restApiId=api_id, parentId=root_id, pathPart='tasks')
res_id = resource['id']

# Configurar métodos ANY y OPTIONS para CORS
for http_method in ['GET', 'POST', 'DELETE', 'OPTIONS']:
    apigw.put_method(restApiId=api_id, resourceId=res_id, httpMethod=http_method, authorizationType='NONE')
    apigw.put_integration(
        restApiId=api_id,
        resourceId=res_id,
        httpMethod=http_method,
        type='AWS_PROXY',
        integrationHttpMethod='POST',
        uri=f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{lambda_arn}/invocations"
    )

# Dar permiso a API Gateway para ejecutar Lambda
sts = boto3.client('sts')
account_id = sts.get_caller_identity()['Account']
lam.add_permission(
    FunctionName=func_name,
    StatementId='apigateway-access',
    Action='lambda:InvokeFunction',
    Principal='apigateway.amazonaws.com',
    SourceArn=f"arn:aws:execute-api:{REGION}:{account_id}:{api_id}/*/*/tasks"
)

# Publicar la API
apigw.create_deployment(restApiId=api_id, stageName='prod')
api_url = f"https://{api_id}.execute-api.{REGION}.amazonaws.com/prod/tasks"

# 6. INYECTAR URL DE LA API EN EL HTML Y SUBIRLO A S3
print("6/6 Publicando aplicación web en Amazon S3...")
with open('index.html', 'r', encoding='utf-8') as f:
    html_content = f.read()

html_final = html_content.replace('%%API_URL%%', api_url)
s3.put_object(
    Bucket=bucket_name,
    Key='index.html',
    Body=html_final.encode('utf-8'),
    ContentType='text/html'
)

web_url = f"http://{bucket_name}.s3-website-{REGION}.amazonaws.com"
print("\n" + "="*60)
print(" ¡DESPLIEGUE COMPLETADO CON ÉXITO!")
print(f" URL de tu Aplicación Web: {web_url}")
print(f" Endpoint de tu API REST:  {api_url}")
print(f"  Tabla DynamoDB creada:   {table_name}")
print(f" Bucket S3 creado:        {bucket_name}")
print("="*60)