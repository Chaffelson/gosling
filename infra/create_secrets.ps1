# You can use this script to help you set Secrets in the AWS Secrets Manager for Gosling
# It will prompt you for each secret and then create or update it in the Secrets Manager

$AWS_REGION = Read-Host "Enter AWS region (default: eu-central-1)"
if ([string]::IsNullOrEmpty($AWS_REGION)) {
    $AWS_REGION = "eu-central-1"
}

$AWS_PROFILE = Read-Host "Enter AWS profile (leave empty for default)"
if ([string]::IsNullOrEmpty($AWS_PROFILE)) {
    $AWS_PROFILE = "default"
}

# Function to create or update a secret
function Create-Or-Update-Secret {
    param (
        [string]$SecretName,
        [string]$SecretValue
    )
    
    # Skip secret if value is empty
    if ([string]::IsNullOrEmpty($SecretValue)) {
        Write-Host "Skipping secret: $SecretName (value is empty)"
        return
    }

    # Check if secret exists
    try {
        $secretExists = $false
        try {
            $secret = Describe-SECSecret -SecretId "/gosling/$SecretName" -Region $AWS_REGION -ProfileName $AWS_PROFILE -ErrorAction SilentlyContinue
            if ($secret) {
                $secretExists = $true
            }
        } catch {
            # Secret doesn't exist, which is fine
        }

        if ($secretExists) {
            Write-Host "Updating existing secret: /gosling/$SecretName"
            Update-SECSecret -SecretId "/gosling/$SecretName" -SecretString $SecretValue -Region $AWS_REGION -ProfileName $AWS_PROFILE
        } else {
            Write-Host "Creating new secret: /gosling/$SecretName"
            New-SECSecret -Name "/gosling/$SecretName" -Description "Gosling - $SecretName" -SecretString $SecretValue -Region $AWS_REGION -ProfileName $AWS_PROFILE
        }
    } catch {
        Write-Host "Error managing secret $SecretName : $_"
    }
}

# Create/update each secret
Write-Host "Creating/updating secrets for Gosling..."

$PINECONE_KEY = Read-Host "Enter Pinecone API Key"
Create-Or-Update-Secret -SecretName "pinecone-api-key" -SecretValue $PINECONE_KEY

$SLACK_TOKEN = Read-Host "Enter Slack Bot Token"
Create-Or-Update-Secret -SecretName "slack-bot-token" -SecretValue $SLACK_TOKEN

$SLACK_SECRET = Read-Host "Enter Slack Signing Secret"
Create-Or-Update-Secret -SecretName "slack-signing-secret" -SecretValue $SLACK_SECRET

$TINYBIRD_KEY = Read-Host "Enter Tinybird API Key"
Create-Or-Update-Secret -SecretName "tinybird-api-key" -SecretValue $TINYBIRD_KEY

$OUTLINE_KEY = Read-Host "Enter Outline API Key"
Create-Or-Update-Secret -SecretName "outline-api-key" -SecretValue $OUTLINE_KEY

Write-Host "All secrets created/updated successfully!" 