import json
import boto3
import uuid
import os
from datetime import datetime

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')

TABLE_NAME = os.environ['TABLE_NAME']
BUCKET_NAME = os.environ['BUCKET_NAME']
table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    # Encabezados para permitir que la página web se comunique con AWS (CORS)
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'OPTIONS,POST,GET,DELETE'
    }
    
    method = event.get('httpMethod', '')
    
    if method == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': ''}

    try:
        # 1. OBTENER TODAS LAS TAREAS (GET)
        if method == 'GET':
            response = table.scan()
            items = response.get('Items', [])
            # Generar URL temporal para descargar/ver el archivo adjunto si existe
            for item in items:
                if item.get('file_key'):
                    item['file_url'] = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': BUCKET_NAME, 'Key': item['file_key']},
                        ExpiresIn=3600
                    )
            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps(items)
            }

        # 2. CREAR NUEVA TAREA O PEDIR PERMISO PARA SUBIR ARCHIVO (POST)
        elif method == 'POST':
            body = json.loads(event.get('body', '{}'))
            action = body.get('action', 'create_task')
            
            # Sub-acción: Generar permiso seguro para subir archivo directo a S3
            if action == 'get_upload_url':
                file_name = body['file_name']
                file_key = f"uploads/{uuid.uuid4()}_{file_name}"
                upload_url = s3.generate_presigned_url(
                    'put_object',
                    Params={'Bucket': BUCKET_NAME, 'Key': file_key},
                    ExpiresIn=300
                )
                return {
                    'statusCode': 200,
                    'headers': headers,
                    'body': json.dumps({'upload_url': upload_url, 'file_key': file_key})
                }
            
            # Sub-acción: Guardar los datos de la tarea en DynamoDB
            task_id = str(uuid.uuid4())
            item = {
                'id': task_id,
                'title': body.get('title', 'Sin título'),
                'description': body.get('description', ''),
                'priority': body.get('priority', 'Media'),
                'file_key': body.get('file_key', ''),
                'file_name': body.get('file_name', ''),
                'created_at': datetime.utcnow().isoformat()
            }
            table.put_item(Item=item)
            return {
                'statusCode': 201,
                'headers': headers,
                'body': json.dumps({'message': 'Tarea guardada', 'item': item})
            }

        # 3. ELIMINAR UNA TAREA (DELETE)
        elif method == 'DELETE':
            body = json.loads(event.get('body', '{}'))
            task_id = body.get('id')
            file_key = body.get('file_key')
            
            if file_key:
                s3.delete_object(Bucket=BUCKET_NAME, Key=file_key)
            table.delete_item(Key={'id': task_id})
            
            return {
                'statusCode': 200,
                'headers': headers,
                'body': json.dumps({'message': 'Eliminado correctamente'})
            }

    except Exception as e:
        return {
            'statusCode': 500,
            'headers': headers,
            'body': json.dumps({'error': str(e)})
        }