#!/bin/bash

# You can use this script to help you set Secrets in the AWS Secrets Manager for Gosling
# It will prompt you for each secret and then create or update it in the Secrets Manager

read -p "Enter AWS region (default: eu-central-1): " AWS_REGION
AWS_REGION=${AWS_REGION:-eu-central-1}

read -p "Enter AWS profile (leave empty for default): " AWS_PROFILE
AWS_PROFILE=${AWS_PROFILE:-default}

# Function to create or update a secret
create_or_update_secret() {
    local SECRET_NAME=$1
    local SECRET_VALUE=$2
    
    # Skip secret if value is empty
    if [ -z "$SECRET_VALUE" ]; then
        echo "Skipping secret: $SECRET_NAME (value is empty)"
        return
    fi

    # Check if secret exists
    if aws secretsmanager describe-secret --secret-id "/gosling/$SECRET_NAME" --region $AWS_REGION 2>/dev/null; then
        echo "Updating existing secret: /gosling/$SECRET_NAME"
        aws secretsmanager update-secret \
            --secret-id "/gosling/$SECRET_NAME" \
            --secret-string "$SECRET_VALUE" \
            --region $AWS_REGION \
            --profile $AWS_PROFILE
    else
        echo "Creating new secret: /gosling/$SECRET_NAME"
        aws secretsmanager create-secret \
            --name "/gosling/$SECRET_NAME" \
            --description "Gosling - $SECRET_NAME" \
            --secret-string "$SECRET_VALUE" \
            --region $AWS_REGION \
            --profile $AWS_PROFILE
    fi
}

# Create/update each secret
echo "Creating/updating secrets for Gosling..."

read -p "Enter Pinecone API Key: " PINECONE_KEY
create_or_update_secret "pinecone-api-key" "$PINECONE_KEY"

read -p "Enter Slack Bot Token: " SLACK_TOKEN
create_or_update_secret "slack-bot-token" "$SLACK_TOKEN"

read -p "Enter Slack Signing Secret: " SLACK_SECRET
create_or_update_secret "slack-signing-secret" "$SLACK_SECRET"

read -p "Enter Tinybird API Key: " TINYBIRD_KEY
create_or_update_secret "tinybird-api-key" "$TINYBIRD_KEY"

read -p "Enter Outline API Key: " OUTLINE_KEY
create_or_update_secret "outline-api-key" "$OUTLINE_KEY"

echo "All secrets created/updated successfully!" 