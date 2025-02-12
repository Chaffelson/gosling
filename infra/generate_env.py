#!/usr/bin/env python3
import boto3
from botocore.config import Config
import argparse
from pathlib import Path
import tomli  # Add this import for TOML parsing

# This script generates a .env file from the CloudFormation stack outputs and AWS Secrets Manager
# It is used to set the environment variables for the Gosling cmdline when running locally

def get_sam_config(config_file: str = "samconfig.toml") -> dict:
    """Read SAM configuration from samconfig.toml"""
    try:
        with open(config_file, "rb") as f:
            config = tomli.load(f)
            # Get default configuration from the default/deploy section
            default_config = config.get("default", {}).get("deploy", {}).get("parameters", {})
            return default_config
    except Exception as e:
        print(f"Warning: Could not read SAM config: {str(e)}")
        return {}

def get_stack_outputs(stack_name: str, region: str, profile: str = None) -> dict:
    """Get all outputs from a CloudFormation stack"""
    session = boto3.Session(profile_name=profile, region_name=region)
    cfn = session.client('cloudformation', config=Config(region_name=region))
    
    try:
        response = cfn.describe_stacks(StackName=stack_name)
        outputs = response['Stacks'][0]['Outputs']
        return {output['OutputKey']: output['OutputValue'] for output in outputs}
    except Exception as e:
        print(f"Error getting stack outputs: {str(e)}")
        return {}

def get_secrets_list(region: str, profile: str = None) -> list:
    """Get list of secrets with /gosling/ prefix"""
    session = boto3.Session(profile_name=profile, region_name=region)
    secrets = session.client('secretsmanager', config=Config(region_name=region))
    
    try:
        response = secrets.list_secrets(
            Filters=[{'Key': 'name', 'Values': ['/gosling/']}]
        )
        return [secret['Name'] for secret in response['SecretList']]
    except Exception as e:
        print(f"Error listing secrets: {str(e)}")
        return []

def main():
    # Read SAM config first
    sam_config = get_sam_config()
    
    parser = argparse.ArgumentParser(description='Generate .env file from CloudFormation stack')
    parser.add_argument('--stack-name', 
                       default=sam_config.get('stack_name', 'gosling'),
                       help='CloudFormation stack name')
    parser.add_argument('--region',
                       default=sam_config.get('region', 'eu-central-1'),
                       help='AWS region')
    parser.add_argument('--profile',
                       default=sam_config.get('profile', 'default'),
                       help='AWS profile name')
    parser.add_argument('--output', default='.env', help='Output file path')
    args = parser.parse_args()

    print(f"Using configuration from samconfig.toml where available")
    print(f"Generating .env file for stack {args.stack_name} in region {args.region} with profile {args.profile}")

    # Get stack outputs
    outputs = get_stack_outputs(args.stack_name, args.region, args.profile)
    
    # Get secrets list
    secrets = get_secrets_list(args.region, args.profile)
    
    # Get secret values
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    secrets_client = session.client('secretsmanager', config=Config(region_name=args.region))
    secret_values = {}
    for secret in secrets:
        try:
            response = secrets_client.get_secret_value(SecretId=secret)
            secret_values[secret.split('/')[-1]] = response['SecretString']
        except Exception as e:
            print(f"Error getting secret {secret}: {str(e)}")
            secret_values[secret.split('/')[-1]] = ''
    
    # Generate .env content
    env_content = [
        "# Generated from CloudFormation stack outputs",
        f"AWS_REGION={args.region}",
        f"AWS_PROFILE={args.profile}",
        "",
        "# Stack Outputs",
        f"S3_BUCKET_NAME={outputs.get('DocsBucketName', '')}",
        f"DYNAMODB_TABLE_NAME={args.stack_name}-eventHandler",
        "",
        "# Application Settings",
        "MAX_CHAT_HISTORY=20",
        "",
        "# Secrets from AWS Secrets Manager",
        f"PINECONE_API_KEY={secret_values.get('pinecone-api-key', '')}",
        f"SLACK_BOT_TOKEN={secret_values.get('slack-bot-token', '')}",
        f"SLACK_SIGNING_SECRET={secret_values.get('slack-signing-secret', '')}",
        f"TINYBIRD_API_KEY={secret_values.get('tinybird-api-key', '')}",
        f"OUTLINE_API_KEY={secret_values.get('outline-api-key', '')}",
        f"ASSISTANT_NAME={secret_values.get('assistant-name', 'gosling-202502')}",
    ]
    
    # Write to file
    output_path = Path(args.output)
    output_path.write_text('\n'.join(env_content))
    print(f"Generated {output_path} with stack outputs and secrets")

if __name__ == '__main__':
    main() 