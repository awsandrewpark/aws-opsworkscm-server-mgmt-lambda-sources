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

def read_artifact_as_config(event, client):
    input_artifact = event['CodePipeline.job']['data']['inputArtifacts'][0]
    artifact_location = input_artifact['location']['s3Location']

    client.download_file(
        artifact_location['bucketName'],
        artifact_location['objectKey'],
        '/tmp/artifact.zip'
    )

    zf = zipfile.ZipFile('/tmp/artifact.zip')

    for filename in zf.namelist():
        if filename == 'opsworkscmconfig.json':
            return json.loads(zf.read(filename))

    raise RuntimeError('Unable to find config.json in build artifact output')


def save_buildspec_as_artifact(event, client, actionlist, ops_sns_arn):
    output_artifact = event['CodePipeline.job']['data']['outputArtifacts'][0]
    artifact_location = output_artifact['location']['s3Location']

    f = open('/tmp/actionlist.json', 'w')
    f.write(json.dumps(actionlist))
    f.close()

    s = """
version: 0.2

phases:
  pre_build:
    commands:
      - mkdir output-tmpdir
      - mkdir output-artifactdir

  build:
    commands:
      - echo Build started on `date`
      - echo running aws cli command ...
"""
    for cl_entry in actionlist['ops_env']:
        print "cl_entry: ",cl_entry

        try:
            if cl_entry['ops_delete']:
                s += "      - aws --region %s opsworks-cm delete-server --server-name %s && echo CM server %s is being deleted >> output-artifactdir/cminfo.log\n" % (cl_entry['ops_region'], cl_entry['name'], cl_entry['name'])
                s += "      - echo %s >> output-artifactdir/cminfo.log" % ops_sns_arn
                continue
        except KeyError:
            pass

        s += "      - aws opsworks-cm create-server --region %s --server-name %s --instance-profile-arn arn:aws:iam::%s:instance-profile/aws-opsworks-cm-ec2-role --service-role-arn arn:aws:iam::%s:role/aws-opsworks-cm-service-role --subnet-ids %s --engine %s" % (cl_entry['ops_region'], cl_entry['name'], cl_entry['ops_account'], cl_entry['ops_account'], cl_entry['ops_subnet'],cl_entry['ops_engine'])
   
        # Try the optional parameters. If not found use default
        ## Defaults: --instance-type = m4.large
        ##           --preferred-maintenance-window = A random one-hour period on Tuesday, Wednesday or Friday (automatically selected if absent)
        ##           --no-disable-automated-backup (automatically selected if absent)
        ##           --backup-retention-count = 30 (valid only if --no-disable-automated-backup specified or assumed)
        ##           --preferred-backup-window = Daily (random 1 hour period) (valid only if --no-disable-automated-backup specified or assumed)
        ##           --engine-model = 'Single' (OWCA) or 'Monolithic' (OWPE) ##           --engine-version = '12' (OWCA) or '2017' (OWPE)

        try:
            enginemodel = cl_entry['ops_engine_model']
            s += " --engine-model %s" % enginemodel
        except KeyError:
            if cl_entry['ops_engine'] == "Chef":
                s += " --engine-model Single"
            else:
                s += " --engine-model Monolithic"

        try:
            engineversion = cl_entry['ops_engine_version']
            s += " --engine-version %s" % engineversion
        except KeyError:
            if cl_entry['ops_engine'] == "Chef":
                s += " --engine-version 12"
            else:
                s += " --engine-version 2017"

        try:
            keypairname = cl_entry['ops_key_pair_name']
            if keypairname:
                s += " --key-pair '{}'".format(keypairname)
        except KeyError:
            pass

        try:
            s += " --instance-type '{}'".format(cl_entry['ops_instance_type'])
        except KeyError:
            s += " --instance-type 'm4.large'"

        try:
            s += " --preferred-maintenance-window '{}'".format(cl_entry['ops_maintenance_window'])
        except KeyError:
            pass

        backup_boolean_present = False
        try:
            backup_boolean = cl_entry['ops_use_automated_backup']
            if not backup_boolean:
                s += " --disable-automated-backup"
            else:
                s += " --no-disable-automated-backup"
            backup_boolean_present = True
        except KeyError:
            s += " --no-disable-automated-backup"
            backup_boolean = True

        if (backup_boolean_present and backup_boolean) or not backup_boolean_present:
            try:
                s += " --backup-retention-count '{}'".format(cl_entry['ops_backup_retention'])
            except KeyError:
                s += " --backup-retention-count '30'"

            try:
                s += " --preferred-backup-window '{}'".format(cl_entry['ops_backup_window'])
            except KeyError:
                pass

        # Capture the output to the temporary directory so we can parse through them later
        if cl_entry['ops_engine'] == "Chef":
            s += " --query 'Server.[Endpoint, EngineAttributes[*].Value]' --output text | tee output-tmpdir/chef.%s.output\n" % cl_entry['name']
        else:
            s += " --query 'Server.[Endpoint, EngineAttributes[*].Value]' --output text | tee output-tmpdir/puppet.%s.output\n" % cl_entry['name']
    s += """
  post_build:
    commands:
      - echo Build completed on `date`
"""
    s += "      - ls output-tmpdir/chef* > /dev/null 2>&1 && for i in output-tmpdir/chef*;do Output=$(cat $i); server=`echo $i | sed 's/^.*chef\.//; s/\.output$//'`; URL=\"https://$(echo $Output | cut -d ' ' -f1)\"; CM_SKIT=$(echo $Output | cut -d ' ' -f2); CLIENTPEM=$(echo $Output | cut -d ' ' -f3-35); [ -n \"$CM_SKIT\" ] && echo $CM_SKIT | /usr/bin/python -m base64 -d > output-artifactdir/$server.zip; CM_PWD=$(echo $Output | cut -d ' ' -f36); [ -n \"$CM_PWD\" ] && aws ssm --region %s put-parameter --name \"/opsworks/cm/chef/$server/consolepassword\" --type \"SecureString\" --value $CM_PWD; [ -n \"$URL\" ] && echo $URL >> output-artifactdir/cminfo.log; [ -n \"$URL\" ] && echo \"Please retrieve admin password from SSM parameter store /opsworks/cm/chef/$server/consolepassword\" >> output-artifactdir/cminfo.log; [ -n \"$CLIENTPEM\" ] && aws ssm --region %s put-parameter --name \"/opsworks/cm/chef/$server/client.pem\" --type \"SecureString\" --value \"$CLIENTPEM\"; [ -n \"$CLIENTPEM\" ] && echo \"Please retrieve client PEM from SSM parameter store /opsworks/cm/chef/$server/client.pem\" >> output-artifactdir/cminfo.log;done || touch output-tmpdir/chef.emptyartifact.output" % (cl_entry['ops_region'], cl_entry['ops_region'])
    s += "      - ls output-tmpdir/puppet* > /dev/null 2>&1 && for i in output-tmpdir/puppet*;do server=`echo $i | sed  's/^.*puppet\.//; s/\.output$//'`; tail -1 $i | awk '{print $1}' > dump.out; [ -s dump.out ] && cat dump.out | /usr/bin/python -m base64 -d > output-artifactdir/$server.zip; URL=\"https://`head -1 $i`\"; tail -1 $i | awk '{print $2}' > dump2.out; [ -s dump2.out ] && aws ssm --region %s put-parameter --name \"/opsworks/cm/puppet/$server/consolepassword\" --type \"SecureString\" --value $(cat dump2.out); [ -n \"$URL\" ] && echo $URL >> output-artifactdir/cminfo.log; [ -n \"$URL\" ] && echo \"Please retrieve admin password from SSM parameter store /opsworks/cm/puppet/$server/consolepassword\" >> output-artifactdir/cminfo.log;done || touch output-tmpdir/puppet.emptyartifact.output" % cl_entry['ops_region']
    s += "      - ls output-artifactdir/cminfo.log > /dev/null 2>&1 && cat output-artifactdir/cminfo.log || echo empty > output-artifactdir/cminfo.log"
    if ops_sns_arn:
        s += "      - echo %s >> output-artifactdir/cminfo.log" % ops_sns_arn

    s += """
artifacts:
  files:
    - '*'
  discard-paths: yes
  base-directory: output-artifactdir/
"""

    print('string is: %s' % s)

    f = open('/tmp/buildspec.yml', 'w')
    f.write(s)
    f.close()

    z = zipfile.ZipFile('/tmp/input.zip', mode='w')
    z.write('/tmp/actionlist.json', 'actionlist.json')
    z.write('/tmp/buildspec.yml', 'buildspec.yml')
    z.close()

    data = open('/tmp/input.zip', 'rb')

    client.put_object(
        Bucket=artifact_location['bucketName'],
        Key=artifact_location['objectKey'],
        ServerSideEncryption='aws:kms',
        Body=data
    )
    data.close()


def quit_pipeline(event, agent, successful, message):
    print('message is: %s' % message)

    if not successful:
        print("Problem detected during live checking. I'm stopping the pipeline")
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
        print('Live checking passed.  Continuing with the next stage of the pipeline')
        # Build our kwargs for codepipline job result.
        job_result_kwargs = dict(jobId=event['CodePipeline.job']['id'])
        job_result_kwargs['executionDetails'] = {
            'summary': message
        }
        agent.put_job_success_result(
            **job_result_kwargs
        )
        exit(0)


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
        config_file = read_artifact_as_config(event, artifact_s3_c)
        print(
            'Config_file loaded: {}'.format(
                json.dumps(config_file)
            )
        )
    except:
        quit_pipeline(event, cp_c, False, 'Could not connect to S3 or failed to access the config file')

    topicfound = False
    try:
        ops_sns_arn = config_file['ops_sns_arn']
        sns_c = boto3.client('sns', region_name=local_region)
        response = sns_c.list_topics()
        print "SNS Topics dump: ", response

        for topic in response['Topics']:
            topicarn = topic['TopicArn']
            print "topicarn is %s and ops_sns_arn is %s" % (topicarn, ops_sns_arn)
            if topicarn == ops_sns_arn:
                print "Topic %s found" % ops_sns_arn
                topicfound = True
                break
        if not topicfound:
            message = "Could not find SNS topic %s"%ops_sns_arn
            quit_pipeline(event, cp_c, False, message)
    except KeyError:
        ops_sns_arn = ""
        print "ops_sns_arn not defined. No notification will be sent."

    actionlist = {'ops_env': []}
    roguehash = dict()
    opsworkscmhash = dict()

    # loop through every supported to get complete list of OpsWorks CM instances
    ## Currently following regions support opsworks-cm
    opsworks_supported_region = [
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "ap-northeast-1",
        "ap-southeast-1",
        "ap-southeast-2",
        "eu-central-1",
        "eu-west-1"
    ]
    for this_region in opsworks_supported_region:
        ec2 = boto3_agent_from_sts('ec2', 'client', this_region)
        instancesdump = ec2.describe_instances()
        for reservation in instancesdump['Reservations']:
            if 'Tags' not in reservation['Instances'][0]:
                continue
            instancestate = reservation['Instances'][0]['State']['Name']
            for tags in reservation['Instances'][0]['Tags']:
                if tags['Key'] == 'opsworks-cm:server-name' and not (instancestate == 'terminated' or instancestate == 'shutting-down'):
                    opsworkscmhash[tags['Value']] = this_region

    # Be skeptical and assume all instances are rogue.  We'll clear them later as we go through the config_file
    roguehash = opsworkscmhash

    # Loop through ops_env objects and perform live check
    for i in config_file['ops_env']:
        opsname = i['name']
        opsaccount = i['ops_account']
        opsregion = i['ops_region']
        opssubnet = i['ops_subnet']
        try:
            opskeypair = i['ops_key_pair_name']
        except KeyError:
            opskeypair = None

        ec2 = boto3_agent_from_sts('ec2', 'client', opsregion)
        keypairs = ec2.describe_key_pairs()
        response = ec2.describe_instances()

        print "Checking config for opsname '%s': " % opsname

        # Check1: local_account must be the same as the value of ops_account
        if local_account != opsaccount:
            message = 'ERROR: ops_account value is not the same as the account running the pipeline\n ' \
                      ' ops_account: %s and running account: %s' % (opsaccount, local_account)
            quit_pipeline(event, cp_c, False, message)

        # Check2: If ops_key_pair_name is present then use its value and check whether it exists
        if opskeypair:
            print("Key pair dump in region '{}' looks like this: {}".format(opsregion, json.dumps(keypairs)))

            if not keypairs:
                message = 'No key pairs found in this region ({})'.format(opsregion)
                quit_pipeline(event, cp_c, False, message)
            else:
                found = False
                for k in keypairs['KeyPairs']:
                    if k['KeyName'] == opskeypair:
                        found = True
                        break

                if not found:
                    message = "key pair '{}' in region '{}' requested for OpsWorksCM server '{}' not found".format(
                        opskeypair, opsregion, opsname)
                    quit_pipeline(event, cp_c, False, message)

        # Check3: Check whether the OpsWorksCM Server instance already exists or not.
        #         We do this by doing ec2 describe_instances dump.
        #         Then look for a tag named "opsworks-cm:server-name" then check its value
        #         NOTE: In some cases, a terminated ec2 instance that housed Chef Automate server may exist
        #               in "terminated" or "shutting down" state.
        #               In those cases, it's not good enough to check the tags but I also need to consult its state
        #               to determine whether the
        #               instance exists or not.  If the state value is "terminated" or "shutting down" then I
        #               can still go ahead and provision a new one.
        #
        #         NOTE: if (ops_delete_if_absent_entry == true) then we need to delete the OpsWorks CM servers that are
        #               not found in the opsworkscmconfig.json file.
        try:
            delete_if_absent = config_file['ops_delete_if_absent_entry']
        except KeyError:
            delete_if_absent = False

        try:
            testvalue = opsworkscmhash[opsname]
            roguehash[opsname] = 'legit'
            print "roguehash is now ", roguehash
        except KeyError:
            # Check 4: Check whether the provided subnet ID exists or not
            subnetresponse = ec2.describe_subnets()
            for subnetID in subnetresponse['Subnets']:
                if opssubnet == subnetID['SubnetId']:
                    print('OpsWorksCM Server %s will be deployed in a subjet with ID %s' % (opsname, opssubnet))
                    subnetfound = True
                    break
            if not subnetfound:
                message = 'You requested to deploy OpsWorksCM server in %s subnet in %s region but such subnet does not exist. ' \
                        ' Exiting...' % (opssubnet, opsregion)
                quit_pipeline(event, cp_c, False, message)

            # Mark opsname for creation
            print('Server %s does not exist in the %s region.  Adding to the actionlist.json' % (opsname, opsregion))
            actionlist['ops_env'].append(i)


    # If ops_delete_if_absent_entry option is true and there are elements in the roguehash hash then add it for deleting
    if roguehash and delete_if_absent:
        for key in roguehash:
            if roguehash[key] != 'legit':
                element = { "name": key, "ops_delete": "True", "ops_region": roguehash[key] }
                print "Adding entry %s for deletion: " % element
                actionlist['ops_env'].append(element)

    if not actionlist:
        print('All OpsWorksCM servers are already provisioned or cleaned up. No further actions are needed.')
    else:
        print('actionlist is {}'.format(actionlist))

    # we have all the pieces we need.  Upload the artifact in zip format
    save_buildspec_as_artifact(event, artifact_s3_c, actionlist, ops_sns_arn)
    quit_pipeline(event, cp_c, True, 'Live Check completed successfully')


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
        "secretAccessKey": "GU6yKFQmrviwgy9sCwH0yWhp43uoAsdtAhzEqWXh",
        "accessKeyId": "ASIAIV7JMPEP3ZCM7YXQ",
        "sessionToken": "FQoDYXdzELD//////////wEaDGEIebG36lbgOA1zrCKsAZVeTqJWs/LE/j4VHAZWy+MQ9e2s+4TIbNVY3O0Pzv5O7RAP3U6o7KUQh1zoAukgxH4cNXBYWjZQdtuD9pkd8wHeQ04X88SOQxI9Uj/wl5VUaPmS52M6FdM3CaYlosllS/bN8FuIloOzvOpuZFfKZ4ZXznkXKfM/QFqI1eb+jLTXZD/6e0n51T+2UGyvkh9YWQS268+F3UKHb9nYrc7iMkAUywngXBJtBOcN2rgo6raW2QU="
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
              "objectKey": "opsworkscm-server-mg/OpsWorksCM/XAooIcP",
              "bucketName": "codepipeline-opsworks-mgmt-6"
            }
          },
          "name": "OpsWorksCMmgmt",
          "revision": "4c5375146b7d9b80e53a95f12747007ded4ad7df"
        }
      ],
      "outputArtifacts": [
        {
          "location": {
            "type": "S3",
            "s3Location": {
              "objectKey": "opsworkscm-server-mgmt/CreationLi/blahblah",
              "bucketName": "codepipeline-opsworks-mgmt-6"
            }
          },
          "name": "CreationList",
          "revision": null
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
