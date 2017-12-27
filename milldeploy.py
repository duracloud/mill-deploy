import click
from git import Repo
import os
import boto3
import shutil
import datetime


class QueueNames():
    STORAGE_STATS = "storage-stats"
    AUDIT = "audit"
    BIT = "bit"
    BIT_ERROR = "bit-error"
    BIT_REPORT = "bit-report"
    DUP_HIGH = "dup-high-priority"
    DUP_LOW = "dup-low-priority"
    DEAD_LETTER = "dead-letter"

    ALL = [AUDIT,  BIT, BIT_ERROR, BIT_REPORT, DUP_LOW,
              DUP_HIGH, DEAD_LETTER, STORAGE_STATS]

    def format(self, prefix, queue_name):
        return "%s-%s" % (prefix, queue_name)

class AutoScaleGroupConfig:
    def __init__(self, autoscale_group, launch_config, scale_up_policy,
                 scale_up_alarm, scale_down_policy, scale_down_alarm):
        self.autoscale_group = autoscale_group
        self.launch_config = launch_config
        self.scale_up_policy = scale_up_policy
        self.scale_up_alarm = scale_up_alarm
        self.scale_down_policy = scale_down_policy
        self.scale_down_alarm = scale_down_alarm


@click.command()
@click.option('--config_dir', required=True,help="Directory of mill " \
                                              "configuration files")
@click.option('--aws_profile', required=True, help="The aws profile "
                                                   "configured in your "
                                                   "environment that you "
                                                   "would like to use." )
def cli(aws_profile, config_dir):
    '''Deploys mill in a production environment.'''

    click.echo('MillDeploy')
    click.echo('AWS Profile: %s' % aws_profile)
    click.echo('Config Directory: %s' % config_dir)

    # validate existence of version in maven central

    shutil.rmtree("mill-init", ignore_errors=True)
    shutil.rmtree("output", ignore_errors=True)

    # download mill-init
    os.mkdir("mill-init")
    repo = Repo.clone_from("https://github.com/duracloud/mill-init.git",
                         "mill-init")

    repo.git.checkout('release-2.1.3')


    # generate cloud init scripts
    os.system('mill-init/generate-all-cloud-init.py -m '
              '%s/mill-config.properties -e '
              '%s/environment-account.properties -bx '
              '%s/bit-exclusion-list.txt -bi '
              '%s/bit-inclusion-list.txt  -sx '
              '%s/storage-stats-exclusion-list.txt -si '
              '%s/storage-stats-inclusion-list.txt   -o output' %
              (config_dir,
              config_dir,
              config_dir,
              config_dir,
              config_dir,
              config_dir))

    session = boto3.Session(profile_name=aws_profile)


    props = read_properties_files_into_dict(
        '%s/environment-account.properties' %
                                    config_dir)
    jar_version = props["jarVersion"]
    key_name = props["keyName"]

    image_id = props["amiId"]

    sns_client = session.client('sns')
    ec2_client = session.client('ec2')

    subnet_ids = get_subnet_ids_as_string(ec2_client)
    availability_zones = get_subnet_availability_zones(ec2_client);

    security_group = get_security_group_id(ec2_client)

    env_prefix = props["instancePrefix"]
    iam_instance_profile=props["iamInstanceProfile"]

    click.echo('Mill Version: %s' % jar_version)


    # create queues
    sqs_client = session.client('sqs')
    put_sqs_queues(sqs_client, env_prefix)

    d = datetime.datetime.utcnow()
    time = d.strftime("%Y-%m-%d-%H%M%S")

    base_launch_config = dict(
        ImageId=image_id,
        IamInstanceProfile=iam_instance_profile,
        SecurityGroups=[security_group],
        KeyName=key_name)


    groups = []

    groups.append(create_sentinel_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     base_launch_config))

    groups.append(create_storage_stats_worker_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    groups.append(create_audit_worker_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    groups.append(create_low_priority_dup_worker_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    groups.append(create_high_priority_dup_worker_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    groups.append(create_bit_worker_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    groups.append(create_bit_report_worker_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    groups.append(create_dup_producer_config(jar_version,
                                                     time,
                                                     subnet_ids,
                                                     availability_zones,
                                                     env_prefix,
                                                     base_launch_config))

    # create autoscale and cloudwatch clients
    autoscale_client = session.client('autoscaling')
    cloudwatch_client = session.client('cloudwatch')

    # for each auotscale group that should exist
    for i in groups:
        # create a launch config
        launch_config = i.launch_config
        response = create_launch_config(autoscale_client, launch_config)

        # if autoscale group already exists
        asg = i.autoscale_group
        if not autoscale_exists(autoscale_client, asg):
            # create an autoscale group with launch config
            create_autoscale_group(autoscale_client, asg, launch_config)
        else:
            #update autoscale group with new launch config
            update_existing_autoscale_group(autoscale_client, asg,
                                            launch_config)

        # scale down
        put_scaling_policy(autoscale_client,
                           cloudwatch_client,
                           i.scale_down_policy,
                           i.scale_down_alarm)

        # scale up
        put_scaling_policy(autoscale_client,
                           cloudwatch_client,
                           i.scale_up_policy,
                           i.scale_up_alarm)

        setup_autoscale_notifications(sns_client, autoscale_client,asg["AutoScalingGroupName"])

def get_security_group_id(ec2_client):
    response = ec2_client.describe_security_groups(
        Filters=[
            {
                'Name': 'group-name',
                'Values': [
                    'mill-vpc',
                ]
            },
        ],
    )

    check_response(response)

    group_id =  response["SecurityGroups"][0]["GroupId"]
    click.echo("security group id found: %s" % group_id)
    return group_id

def get_subnets(ec2_client):

    response = ec2_client.describe_vpcs(Filters=[
            {
                'Name': 'tag-value',
                'Values': [
                    'duracloud',
                ]
            },
        ],
    )

    check_response(response)
    vpcId = response["Vpcs"][0]["VpcId"]
    click.echo("retrieved vpc: %s" % vpcId)

    response = ec2_client.describe_subnets()
    check_response(response)
    subnets = response["Subnets"]

    vpc_subnets = []
    for subnet in subnets:
        if subnet["VpcId"] == vpcId:
            vpc_subnets.append(subnet)

    return vpc_subnets

def get_subnet_availability_zones(ec2_client):
    av_zones = []
    subnets = get_subnets(ec2_client)
    for subnet in subnets:
        av_zone = subnet["AvailabilityZone"]
        if av_zones not in av_zones:
            av_zones.append(av_zone)

    return av_zones

def get_subnet_ids_as_string(ec2_client):
    subnet_ids = []
    subnets = get_subnets(ec2_client)

    for subnet in subnets:
        subnet_ids.append(subnet["SubnetId"])

    click.echo("retrieved subnet ids: %s" % subnet_ids)
    return ",".join(subnet_ids)

def setup_autoscale_notifications(sns_client, autoscale_client,
                                  autoscale_group_name):
    topic_arn = sns_client.create_topic(Name='mill-notification')['TopicArn']
    response = autoscale_client.put_notification_configuration(
    AutoScalingGroupName=autoscale_group_name,
    TopicARN=topic_arn,
    NotificationTypes=[
        'autoscaling:EC2_INSTANCE_LAUNCH',
        'autoscaling:EC2_INSTANCE_TERMINATE',
        'autoscaling:EC2_INSTANCE_LAUNCH_ERROR',
        'autoscaling:EC2_INSTANCE_TERMINATE_ERROR',
    ])
    check_response(response)
    click.echo("configured notifications on topic %s for %s" % (topic_arn,
                                                   autoscale_group_name))


def read_properties_files_into_dict(path):
    myprops = {}
    with open(path, 'r') as f:
        for line in f:
            line = line.rstrip() #removes trailing whitespace and '\n' chars
            if "=" not in line: continue #skips blanks and comments w/o =
            if line.startswith("#"): continue #skips comments which contain =
            k, v = line.split("=", 1)
            myprops[k] = v
    return myprops

def read_file_as_string(path):
    return open(path, 'r').read()


def autoscale_exists(client, asg):
    click.echo("auto scaling groups: ")
    groups = client.describe_auto_scaling_groups()

    #click.echo("keys: %s" % groups['AutoScalingGroups'])
    autoscalingGroups = groups['AutoScalingGroups']

    for group in autoscalingGroups:
        group_name = group["AutoScalingGroupName"]
        if group_name == asg['AutoScalingGroupName']:
            click.echo("%s already exists." % group_name)
            return True

    return False

def create_autoscale_group(client, asg, launch_config):
    click.echo(("creating auto scale group %s and associating it with %s" %
               (asg, get_name(launch_config))))
    response = client.create_auto_scaling_group(**asg)
    check_response(response)
    click.echo("created autoscale config: %s" % asg["AutoScalingGroupName"])
    return

def update_existing_autoscale_group(client, asg, launch_config):
    name = get_name(launch_config)
    click.echo(("updating existing auto scale group %s and linking it with "
                "%s" %
               (asg, name)))

    response = client.update_auto_scaling_group(**asg)
    check_response(response)
    click.echo("updated autoscale config: %s" % asg)

def get_name(launch_config):
    return launch_config["LaunchConfigurationName"]

def check_response(response):
    responseCode = response['ResponseMetadata']['HTTPStatusCode']
    click.echo("responseCode = %s" % responseCode)
    if responseCode < 200 and responseCode < 300:
      raise(RuntimeError("failed to create launch config; response=%s" % (response)))
    click.echo("response = %s" % response)

def put_sqs_queues(sqs_client, env_prefix):
    queue_names = QueueNames.ALL
    for queue_name in queue_names:
        #format name
        qname = QueueNames().format(env_prefix, queue_name)
        click.echo("creating queue %s" % qname)
        #create queue
        response = sqs_client.create_queue(
            QueueName=qname,
            Attributes={
                'VisibilityTimeout': '1200',
                'ReceiveMessageWaitTimeSeconds': '0',
                'MessageRetentionPeriod': '1209600'
            }
        )
        #verify result
        check_response(response)
        click.echo("created queue %s" % qname)



def create_launch_config(client, launch_config):
    name = get_name(launch_config)
    click.echo("creating launch config: %s" % name)
    response = client.create_launch_configuration(**launch_config)
    check_response(response)
    click.echo("created launch config %s" % name)
    return launch_config

def put_scaling_policy(auto_scaling_client,
                       cloudwatch_client,
                       scaling_policy,
                       scaling_alarm):
    if scaling_policy is None:
        return

    click.echo("put scaling policy: %s" % scaling_policy)
    response = auto_scaling_client.put_scaling_policy(**scaling_policy)
    check_response(response)
    policy_arn = response["PolicyARN"]
    click.echo("successfully put scaling policy with PolicyArn: %s" % policy_arn)

    scaling_alarm["AlarmActions"]=[policy_arn]

    cloudwatch_client.put_metric_alarm(**scaling_alarm)
    click.echo("successfully put metric alarm %s" % scaling_alarm)

def create_storage_stats_worker_config(jar_version, time,
                                       subnet_id,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("storage stats worker %s %s" % (jar_version,
                                                            time)),
        InstanceType="m4.large",
        SpotPrice="0.05",
        UserData=read_file_as_string('output/cloud-init-storage-stats-worker.txt'))
    launch_config.update(base_launch_config)

    scaling_group_name = 'Storage Stats Worker'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=0,
         MaxSize=1,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_id)

    scale_up_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Up',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=1,
        AdjustmentType='ChangeInCapacity')
    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')

    scale_up_alarm = dict(
        AlarmName='non-empty-storage-stats-queue',
        AlarmDescription='storage stats queue is not empty',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.STORAGE_STATS)
            },
        ],
        Period=60,
        Threshold=0,
        EvaluationPeriods=30,
        ComparisonOperator='GreaterThanThreshold'
    )

    scale_down_alarm = dict(
        AlarmName='empty-storage-stats-queue',
        AlarmDescription='storage stats are empty',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.STORAGE_STATS)
            },
        ],
        Period=300,
        Threshold=0,
        EvaluationPeriods=6,
        ComparisonOperator='LessThanOrEqualToThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)

def create_audit_worker_config(jar_version, time,
                                       subnet_id,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("audit worker %s %s" % (jar_version,
                                                            time)),
        InstanceType="m4.large",
        SpotPrice="0.0325",
        UserData=read_file_as_string('output/cloud-init-audit-worker.txt'),
        BlockDeviceMappings=[
        {
            'DeviceName': '/dev/sda1',
            'Ebs': {
                'VolumeSize': 60,
                'VolumeType': 'gp2',
            }
        }])

    launch_config.update(base_launch_config)

    scaling_group_name = 'Audit Worker'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=1,
         MaxSize=10,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_id)
    scale_up_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Up',
        PolicyType='SimpleScaling',
        Cooldown=900,
        ScalingAdjustment=1,
        AdjustmentType='ChangeInCapacity')
    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=900,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')

    scale_up_alarm = dict(
        AlarmName='large-audit-queue',
        AlarmDescription='large audit queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.AUDIT)
            },
        ],
        Period=300,
        Threshold=1000,
        EvaluationPeriods=2,
        ComparisonOperator='GreaterThanThreshold'
    )

    scale_down_alarm = dict(
        AlarmName='small-audit-queue',
        AlarmDescription='small audit queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.AUDIT)
            },
        ],
        Period=300,
        Threshold=500,
        EvaluationPeriods=4,
        ComparisonOperator='LessThanOrEqualToThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)



def create_high_priority_dup_worker_config(jar_version, time,
                                       subnet_ids,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("high priority dup worker %s %s" % (
            jar_version,
                                                            time)),
        InstanceType="m4.large",
        SpotPrice="0.0325",
        UserData=read_file_as_string(
            'output/cloud-init-dup-worker.txt'),
        BlockDeviceMappings=[
            {
                'DeviceName': '/dev/sda1',
                'Ebs': {
                    'VolumeSize': 60,
                    'VolumeType': 'gp2',
                }
            },
        ]
    )

    launch_config.update(base_launch_config)

    scaling_group_name = 'High Priority Dup Worker'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=0,
         MaxSize=10,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_ids)
    scale_up_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Up',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=1,
        AdjustmentType='ChangeInCapacity')
    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=900,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')

    scale_up_alarm = dict(
        AlarmName='large-high-priority-dup-queue',
        AlarmDescription='large high priority dup queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.DUP_HIGH)
            },
        ],
        Period=300,
        Unit='Seconds',
        Threshold=500,
        EvaluationPeriods=2,
        ComparisonOperator='GreaterThanThreshold'
    )

    scale_down_alarm = dict(
        AlarmName='small-high-priority-dup-queue',
        AlarmDescription='small high priority dup queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.DUP_HIGH)
            },
        ],
        Period=300,
        Threshold=100,
        EvaluationPeriods=4,
        ComparisonOperator='LessThanOrEqualToThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)


def create_low_priority_dup_worker_config(jar_version, time,
                                       subnet_ids,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("low priority dup worker %s %s" % (
            jar_version,
                                                            time)),
        InstanceType="m4.large",
        SpotPrice="0.0325",
        UserData=read_file_as_string(
            'output/cloud-init-dup-worker.txt'),
        BlockDeviceMappings=[
            {
                'DeviceName': '/dev/sda1',
                'Ebs': {
                    'VolumeSize': 60,
                    'VolumeType': 'gp2',
                }
            },
        ]
    )

    launch_config.update(base_launch_config)

    scaling_group_name = 'Low Priority Dup Worker'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=0,
         MaxSize=10,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_ids)
    scale_up_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Up',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=1,
        AdjustmentType='ChangeInCapacity')
    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=900,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')

    scale_up_alarm = dict(
        AlarmName='large-low-priority-dup-queue',
        AlarmDescription='large high priority dup queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.DUP_LOW)
            },
        ],
        Period=300,
        Threshold=5000,
        EvaluationPeriods=2,
        ComparisonOperator='GreaterThanOrEqualToThreshold'
    )

    scale_down_alarm = dict(
        AlarmName='small-low-priority-dup-queue',
        AlarmDescription='small low priority dup queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.DUP_LOW)
            },
        ],
        Period=300,
        Threshold=100,
        EvaluationPeriods=4,
        ComparisonOperator='LessThanOrEqualToThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)


def create_bit_worker_config(jar_version, time,
                                       subnet_ids,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("bit worker worker %s %s" % (
            jar_version,
                                                            time)),
        InstanceType="m4.large",
        SpotPrice="0.0325",
        UserData=read_file_as_string(
            'output/cloud-init-bit-worker.txt'),
        BlockDeviceMappings=[
            {
                'DeviceName': '/dev/sda1',
                'Ebs': {
                    'VolumeSize': 60,
                    'VolumeType': 'gp2',
                }
            },
        ]
    )

    launch_config.update(base_launch_config)

    scaling_group_name = 'Bit Worker'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=0,
         MaxSize=10,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_ids)
    scale_up_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Up',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=1,
        AdjustmentType='ChangeInCapacity')
    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=900,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')

    scale_up_alarm = dict(
        AlarmName='non-empty-bit-queue',
        AlarmDescription='non-empty-bit-queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.BIT)
            },
        ],
        Period=300,
        Threshold=1,
        EvaluationPeriods=2,
        ComparisonOperator='GreaterThanOrEqualToThreshold'
    )

    scale_down_alarm = dict(
        AlarmName='small-bit-queue',
        AlarmDescription='small bit queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesNotVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.BIT)
            },
        ],
        Period=900,
        Threshold=1,
        EvaluationPeriods=4,
        ComparisonOperator='LessThanThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)


def create_bit_report_worker_config(jar_version, time,
                                       subnet_ids,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("bit report worker %s %s" % (
            jar_version,
                                                            time)),
        InstanceType="m4.large",
        SpotPrice="0.0325",
        UserData=read_file_as_string(
            'output/cloud-init-bit-report-worker.txt'),
        BlockDeviceMappings=[
            {
                'DeviceName': '/dev/sda1',
                'Ebs': {
                    'VolumeSize': 20,
                    'VolumeType': 'gp2',
                }
            },
        ]
    )

    launch_config.update(base_launch_config)

    scaling_group_name = 'Bit Report Worker'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=0,
         MaxSize=1,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_ids)
    scale_up_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Up',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=1,
        AdjustmentType='ChangeInCapacity')
    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=300,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')

    scale_up_alarm = dict(
        AlarmName='non-empty-bit-report-queue',
        AlarmDescription='non-empty-bit-report-queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.BIT_REPORT)
            },
        ],
        Period=300,
        Threshold=1,
        EvaluationPeriods=1,
        ComparisonOperator='GreaterThanOrEqualToThreshold'
    )

    scale_down_alarm = dict(
        AlarmName='bit-report-queue-empty',
        AlarmDescription='empty bit report queue',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='ApproximateNumberOfMessagesVisible',
        Namespace='AWS/SQS',
        Statistic='Average',
        Dimensions=[
            {
                'Name': 'QueueName',
                'Value': QueueNames().format(env_prefix, QueueNames.BIT_REPORT)
            },
        ],
        Period=3600,
        Threshold=0,
        EvaluationPeriods=6,
        ComparisonOperator='LessThanOrEqualToThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)

def create_dup_producer_config(jar_version, time,
                                       subnet_ids,
                                       availability_zones,
                                       env_prefix, base_launch_config):
        # storage stats worker config
    launch_config = dict(
        LaunchConfigurationName=("dup producer %s %s" % (
            jar_version,
                                                            time)),
        InstanceType="t2.micro",
        UserData=read_file_as_string(
            'output/cloud-init-dup-producer.txt')
    )

    launch_config.update(base_launch_config)

    scaling_group_name = 'Dup Producer'
    asg = dict(
         AutoScalingGroupName=scaling_group_name,
         LaunchConfigurationName=get_name(launch_config),
         MinSize=0,
         MaxSize=1,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_ids)
    scale_up_policy =  None
    scale_up_alarm = None

    scale_down_policy =  dict(AutoScalingGroupName=scaling_group_name,
        PolicyName='Scale Down',
        PolicyType='SimpleScaling',
        Cooldown=600,
        ScalingAdjustment=-1,
        AdjustmentType='ChangeInCapacity')


    scale_down_alarm = dict(
        AlarmName='prod-dup-producer-complete',
        AlarmDescription='dup producer complete',
        ActionsEnabled=True,
        AlarmActions=[],
        MetricName='DupProducerComplete',
        Namespace='AWS/SQS',
        Statistic='Average',
        Period=300,
        Threshold=1,
        EvaluationPeriods=1,
        ComparisonOperator='GreaterThanOrEqualToThreshold'
    )

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                scale_up_policy,
                                scale_up_alarm,
                                scale_down_policy,
                                scale_down_alarm)

def create_sentinel_config(jar_version, time, subnet_ids,
                           availability_zones, base_launch_config):

    # sentinel config
    launch_config = dict(
        LaunchConfigurationName=("sentinel %s %s" % (jar_version, time)),
        InstanceType="t2.medium",
        UserData=read_file_as_string('output/cloud-init-sentinel.txt'))
    launch_config.update(base_launch_config)

    asg = dict(
         AutoScalingGroupName='Sentinel',
         LaunchConfigurationName=get_name(launch_config),
         MinSize=1,
         MaxSize=1,
         AvailabilityZones=availability_zones,
         VPCZoneIdentifier=subnet_ids)

    return AutoScaleGroupConfig(asg,
                                launch_config,
                                None,
                                None,
                                None,
                                None)


