# Overview

The milldeploy tool makes deploying and maintaining the DuraCloud Mill fast and easy.
Before using this tool there are a few preliminary steps that must be handled in advance.
* mill and ama databases must be configured.
* you must define a VPC, a VPC Security Group, a subnet in that VPC and an EFS
  associated with the VPC and Security Group.
* an IAM role with sufficient privileges (TBD)

See https://github.com/duracloud/deployment-docs.git for more information.

With these pieces in place, the milldeploy tool will create all AWS resources you need to run the mill
including queues, autoscale launch configs, autoscale groups, alarms, and scaling policies.

# Install virtualenv
pip install virtualenv
# Create a virtual env
virtualenv -p python3 venv
# Launch virtual environment in shell
source venv/bin/activate
# Install the application
python setup.py install
# Preliminary setup in AWS:
In addition to setting up an IAM Role with sufficient permissions (TBD), you'll need to setup a VPC and subnets, VPC Security Group, and EFS file system.

VPC notes:  it must be called "duracloud" and there must be an associated subnet in availability zones us-east-1a, us-east-1b, us-east-1c, us-east-1d, and us-east-1e.

VPC Security Group must be named: mill-vpc

More details on the environment setup coming soon.

# Set up your configuration files
Copy the sample configuration files in the ./sample-config directory to another directory
and enter your environment specific properties.

# Setup your aws profile
You'll need to setup two files:  
~/.aws/config :  

[profile my-aws-profile]  
output = text  
region = us-east-1

~/.aws/credentials :  

[my-aws-profile]  
aws_access_key_id = <aws-key-id>  
aws_secret_access_key = <aws-secret-key>  

# Run the tool
milldeploy --config_dir /path/to/your/config/dir --aws_profile my-aws-profile
