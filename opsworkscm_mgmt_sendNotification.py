# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import json
import boto3
import zipfile
import re
from botocore.client import Config


def boto3_agent_from_sts(agent_service, agent_type, region, credentials=None):
    if credentials is None:
        credentials = dict()

    session = boto3.session.Session()

    # Generate our kwargs to pass
    kw_args = {
        'region_name': region,
        'config': Config(signature_version='s3v4')
    }

    if credentials:
        kw_args['aws_access_key_id'] = credentials['accessKeyId']
        kw_args['aws_secret_access_key'] = credentials['secretAccessKey']
        kw_args['aws_session_token'] = credentials['sessionToken']

    # Build our agent depending on how we're called.
    if agent_type == 'client':
        return session.client(
            agent_service,
            **kw_args
        )
    if agent_type == 'resource':
        return session.resource(
            agent_service,
            **kw_args
        )

def determine_region(context):
    myregion = context.invoked_function_arn.split(':')[3]

    if myregion:
        return myregion
    else:
        raise RuntimeError(
            'Could not determine region from arn {}'.format(
                context.invoked_function_arn
            )
        )

def determine_account_id(context):
    account_id = context.invoked_function_arn.split(':')[4]
    if account_id:
        return account_id
    else:
        raise RuntimeError(
            'Could not determine account id from arn {}'.format(
                context.invoked_function_arn
            )
        )

def read_cminfo_artifact(event, client):
    input_artifact = event['CodePipeline.job']['data']['inputArtifacts'][0]
    artifact_location = input_artifact['location']['s3Location']

    client.download_file(
        artifact_location['bucketName'],
        artifact_location['objectKey'],
        '/tmp/artifact.zip'
    )

    zf = zipfile.ZipFile('/tmp/artifact.zip')

    for filename in zf.namelist():
        if filename == 'cminfo.log':
            return zf.read(filename)

    raise RuntimeError('Unable to find config.json in build artifact output')


def quit_pipeline(event, agent, successful, message):
    print('message is: %s' % message)

    if not successful:
        # On exception we will termiante our pipeline.
        agent.put_job_failure_result(
            jobId=event['CodePipeline.job']['id'],
            failureDetails={
                'type': 'JobFailed',
                'message': message
            }
        )
        exit(1)

    else:
        # Build our kwargs for codepipline job result.
        job_result_kwargs = dict(jobId=event['CodePipeline.job']['id'])
        job_result_kwargs['executionDetails'] = {
            'summary': message
        }
        agent.put_job_success_result(
            **job_result_kwargs
        )
        exit(0)

def compose_message(cminfo, event):
    # The default S3 bucket used for Artifact caching has a strict policy enforced which prevents
    # browser/curl downloads due to missing signature version 4 thus the need for another bucket
    # that will be a caching bucket without encryption. Part of message composing process is
    # to upload the StarterKit artifact to the caching bucket and figure out its URL and return it
    # to the caller

    # First upload the artifact
    s3 = boto3.resource('s3')

    s3location = event['CodePipeline.job']['data']['inputArtifacts'][0]['location']['s3Location']
    destbucket="%s-starterkit" % s3location['bucketName']
    destkey=s3location['objectKey']
    copy_source = {
        'Bucket': s3location['bucketName'],
        'Key': s3location['objectKey']
    }
    try:
        s3.meta.client.copy(copy_source, destbucket, destkey)
    except Exception as e:
        print (e)
        message="The Starter Kit object could not be copied to the caching bucket %s. Exiting..." % destbucket
        return ""
    
    object_acl = s3.ObjectAcl(destbucket,destkey)
    try:
        #response = s3.put_object_acl(
      #    ACL='public-read',
        #    Bucket=destbucket,
        #    Key=destkey,
        #)
        response=object_acl.put(
            ACL='public-read'
        )
    except Exception as e:
        print (e)
        message="I could not change the permission to public read on an S3 object s3://%s/%s" % (destbucket,destkey)
        return ""

    dlurl="https://s3.amazonaws.com/%s/%s" % (destbucket,s3location['objectKey'])
    message = cminfo + "\nYou can download the starter kit(s) here: %s\n" % dlurl

    return message

def main(event, context):
    # In the opsworkscm_mgmt_live_check.py, we are checking following:
    # (ASSUMPTION: We are creating one pipeline per account -
    #           meaning we are not doing cross-account OpsWorksCM server creation)
    #
    # Ensure that running account is same as the value of ops_account
    # If ops_key_pair_name is present, check whether the key exists or not (fail pipeline if the key does not exist)
    # For all 'name' under 'ops_env' entry, check whether it exists in the specified  region.
    #  If not, mark it for creation (Output artifact: CreationList)
    #

    print(
        'Raw event: {}'.format(
            json.dumps(event)
        )
    )

    print('Log stream name:%s' % context.log_stream_name)
    print('Log group name:%s' % context.log_group_name)
    print('Invoked function arn:%s' % context.invoked_function_arn)

    local_region = determine_region(context)
    local_account = determine_account_id(context)

    print('region is: %s' % local_region)
    print('account is: %s' % local_account)

    # Create connection to codepipeline so as to send exceptions (if any)
    cp_c = boto3_agent_from_sts('codepipeline', 'client', local_region)

    # # # Parse through the json file and go through the 'instance' configuration
    # First, we need to Extract our credentials and locate our artifact from our build.
    credentials = event['CodePipeline.job']['data']['artifactCredentials']
    artifact_s3_c = boto3_agent_from_sts(
        's3',
        'client',
        local_region,
        credentials
    )

    # # # Let's be gentle about connecting to S3 to download the config file
    try:
        cminfo = read_cminfo_artifact(event, artifact_s3_c)
    except:
        quit_pipeline(event, cp_c, False, 'Could not connect to S3 or failed to access the config file')

    # Determine whether a message needs to be published to an SNS topic
    print "DEBUG cminfo: ", cminfo
    if re.search("^empty",cminfo):
        # A cminfo file that starts with empty means there was no action taken thus do not send notification and quit with success
        quit_pipeline(event, cp_c, True, 'No action was taken therefore no notification will be sent.')

    # At this point, we know that SNS ARN has been provided.  We have to search with a regular expression to locate it and use the value
    snsarn = re.search("arn:aws:sns:\w+-\w+-\d{1}:\d{12}:\w+",cminfo)
    if not snsarn:
        quit_pipeline(event, cp_c, False, 'No SNS ARN has been provided. Exiting...')

    # Let's compose a message that includes a URL to the StarterKit
    message=compose_message(cminfo, event)
    if not message:
        quit_pipeline(event, cp_c, False, 'A proper messag could not be staged. Exiting...')
    print "DEBUG message: ", message

    # Send to the SNS queue
    sns_c = boto3.client('sns', region_name=local_region)
    try:
        response = sns_c.publish(TargetArn=snsarn.group(0),Message=message,Subject="OpsWorks Configuration Manager Information")
    #except:
    except Exception as e:
        message="Something has gone wrong while publishing to a SNS topic."
        print (e)
        quit_pipeline(event, cp_c, False, message)

    # Quit pipeline successfully
    quit_pipeline(event, cp_c, True, 'Message handled successfully')


def lambda_handler(event, context):
    main(event, context)


def outside_lambda_handler():
    class Context(object):
        def __init__(self, **kwargs):
            self.function_name = kwargs.get(
                'function_name',
                'opsworkscmServerMgmt'
            )
            self.invoked_function_arn = kwargs.get(
                'invoked_function_arn',
                'arn:aws:lambda:us-east-1:121895852041'
                + ':function:opsworkscmServerMgmt'
            )
            self.log_group_name = kwargs.get(
                'log_group_name',
                '/aws/lambda/opsworkscmServerMgmt'
            )
            self.log_stream_name = kwargs.get(
                'log_stream_name',
                '2018/03/26/[$LATEST]7ea52202c1494810ab5713f045697b4f'
            )

    context = Context()
    event = json.loads("""
{
  "CodePipeline.job": {
    "data": {
      "artifactCredentials": {
        "secretAccessKey": "V3R/1MGsHFfuevqJUab+u5t8tWuCYtgR1SOBotmX",
        "accessKeyId": "ASIAIQ7TA7J6D6UBBMIA",
        "sessionToken": "FQoDYXdzEEQaDHePloi1pEq9aR3jryKsAb+l8oZvNk9O9+57oykfn6IT3wGLFKMgMkUhgPAW0gv+xzsuYS1m0vRJVBZPrdoeymfgwzVzomaFCmT1l/DqmqZn7arZZRWLSn9jmZ0kOpputnwS+UQU4Rx+ekftQgToR3ddlxoTUf3vf1Cr8ARhu3IvsG+eZavTmjjx+fwB19caZhC3dRP8w40U2N7tC/5QROLf3XXFOXLnYVA8EKjtZaM3BRZ2Z6kgli9DcI8ogNf+2AU="
      },
      "actionConfiguration": {
        "configuration": {
          "FunctionName": "mylambda"
        }
      },
      "inputArtifacts": [
        {
          "location": {
            "type": "S3",
            "s3Location": {
              "objectKey": "opsworkscm-server-mg/StarterKit/DzyDStq",
              "bucketName": "codepipeline-opsworkscm-stack2"
            }
          },
          "name": "OpsWorksCMmgmt",
          "revision": "4c5375146b7d9b80e53a95f12747007ded4ad7df"
        }
      ]
    },
    "id": "482b288e-8746-42ab-9e6b-7c8e21826d86",
    "accountId": "121895852041"
  }
}
""")
    main(event, context)


if __name__ == '__main__':
    outside_lambda_handler()
