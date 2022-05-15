# -*- coding: utf-8 -*-

# *******************************************************************************************************************
# Author - Nirmallya Mukherjee
# To run the application use the following
# ubuntu@ip-172-31-17-36:/opt/docproc$ python manage.py runserver 0:8080
# *******************************************************************************************************************

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key, Attr
import json
import boto3
import datetime


dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
s3_target_bucket = 'sk-gl-target'


# *******************************************************************************************************************
# Below methods are the handlers for the web http endpoint
# *******************************************************************************************************************
#This is the main method that is mapped to the URI (in urls.py)
#CSRF is needed for the SNS to make a call from another domain
@csrf_exempt
def message(request):
    print '*********************** Incoming request *****************************',
    print_request(request)
    #SNS http end point will have the notification details in the body
    #Check the http header and see if the sns header details are present, if so proceed else throw except
    process_document(request.body)
    return HttpResponse('API invoked; your http record is now saved.')



#Credit https://gist.github.com/defrex/6140951
def print_request(request):
    headers = ''
    for header, value in request.META.items():
        if not header.startswith('HTTP'):
            print ('  Req meta item:', header, value)
            continue
        header = '-'.join([h.capitalize() for h in header[5:].lower().split('_')])
        headers += '{}: {}\n'.format(header, value)

    req_header = (
        '{method} HTTP/1.1\n'
        'Content-Length: {content_length}\n'
        'Content-Type: {content_type}\n'
        'Headers: {headers}\n'
        'Body: {body}'
    ).format(
        method=request.method,
        content_length=request.META['CONTENT_LENGTH'],
        content_type=request.META['CONTENT_TYPE'],
        headers=headers,
        body=request.body,
    )
    print (req_header)

    if request.method == 'GET':
        print 'Request method = GET'
        for key, value in request.GET.iterlists():
            print ("Key=%s\nValue=%s" % (key, value))
    elif request.method == 'POST':
        print ('Request method = POST')
        for key, value in request.POST.iterlists():
            print ("Key=%s\nValue=%s" % (key, value))



# *******************************************************************************************************************
# Below methods are the handlers for the process
# *******************************************************************************************************************
def process_document(s3json):
    print ('The S3 JSON is ', s3json)
    nmsgjson = json.loads(s3json)
    s3 = json.loads(nmsgjson['Message'])
    if 'Records' not in s3:
        return
    bucket = s3['Records'][0]['s3']['bucket']['name']
    filename = s3['Records'][0]['s3']['object']['key']
    print ('Will read the file %s from the bucket %s' % (filename, bucket))

    s3 = boto3.resource('s3')
    obj = s3.Object(bucket, filename)
    content = obj.get()['Body'].read().decode('utf-8').replace(",", ";")

    #Initialize to some default values which should be overwritten by the values from the invoice file
    cust_id = 'def'
    inv_id = 'def_001'
    line = ''
    #The below logic is needed because the content object returns 1 char at a time
    for one_char in content:
        if one_char == '\n':
            print 'Line-> ', line
            if "Customer-ID:" in line:
                cust_id = line.split()[1]
                print '  Found Customer-ID ', cust_id
            elif "Inv-ID:" in line:
                inv_id = line.split()[1]
                print '  Found Invoice-ID ', inv_id
            line = ''
        else:
            line += one_char

    #Insert to dynamo and push to kinesis stream
    xform_content = transform_content(cust_id, inv_id, content)
    print ('CSV ->', xform_content)
    insert_dynamodb(cust_id, inv_id, content, xform_content)
    write_to_target_bucket(cust_id, inv_id, content, xform_content)



def insert_dynamodb(cust_id, inv_id, content, xform_content):
    create_table()
    insert_data(cust_id, inv_id, content, xform_content)



def create_table():
    print ('\n*************************************************************************')
    print ('Creating table invoice')
    try:
        table = dynamodb.create_table(
            TableName='invoice',
            KeySchema=[
                { 'AttributeName': 'cust_id', 'KeyType': 'HASH' },
                { 'AttributeName': 'inv_id', 'KeyType': 'RANGE' }
            ],
            AttributeDefinitions=[
                { 'AttributeName': 'cust_id', 'AttributeType': 'S' },
                { 'AttributeName': 'inv_id', 'AttributeType': 'S' }
            ],
            # Planning for capacity units
            ProvisionedThroughput={ 'ReadCapacityUnits': 1, 'WriteCapacityUnits': 1 }
        )
        # Wait until the table exists.
        table.meta.client.get_waiter('table_exists').wait(TableName='invoice')
        print ('DONE')

    except ClientError as e:
        print ( 'Skipped due to exception ', e.response['Error']['Code'])
        print ( 'Reason ', e.response['Error']['Message'])



def insert_data(cust_id, inv_id, content, xform_content):
    print '\n*************************************************************************'
    print ('Inserting data in the table')
    table = dynamodb.Table('invoice')
    table.put_item(
       Item={
            'cust_id': cust_id,
            'inv_id': inv_id,
            'details': content,
            'csvdtls': xform_content
        }
    )
    print ( ' Done.')



def write_to_target_bucket(cust_id, inv_id, content, xform_content):
    now = datetime.datetime.now()
    prefix_val = str(now.microsecond) + str(now.second)
    object_key = prefix_val + '_' + cust_id + '_' + inv_id + '.csv'
    print ("Written new s3 file", object_key)
    print ("Uploading S3 object content", xform_content)
    s3 = boto3.client('s3')
    s3.put_object(Bucket=s3_target_bucket, Key=object_key, Body=xform_content.encode())
    print ("Done")


#TBD Need to conver the invoice to a CSV, care -> the data can have comma
def transform_content(cust_id, inv_id, content):
    line=''
    dated=''; fromcust=''; tocust=''; amt=''; sgst=''; tot=''; words='';
    for one_char in content:
        if one_char == '\n':
            if "Dated:" in line:
                dated = line.split(":")[1]
                print '  Found dated ', dated
            elif "From:" in line:
                fromcust = line.split(":")[1]
                print '  Found fromcust ', fromcust
            elif "To:" in line:
                tocust = line.split(":")[1]
                print '  Found tocust ', tocust
            elif "Amount:" in line:
                amt = line.split(":")[1]
                print '  Found amt ', amt
            elif "SGST:" in line:
                sgst = line.split(":")[1]
                print '  Found sgst ', sgst
            elif "Total:" in line:
                tot = line.split(":")[1]
                print '  Found tot ', tot
            elif "InWords:" in line:
                words = line.split(":")[1]
                print '  Found words ', words
            line = ''
        else:
            line += one_char
    return cust_id + "," + inv_id + "," + dated + "," + fromcust + "," + tocust + "," + amt + "," + sgst + "," + tot + "," + words
