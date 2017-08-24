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
# Set up your configuration files
TBD
# Run the tool
milldeploy --help

