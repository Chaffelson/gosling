# Gosling - A Cheap & Good Knowledge Base Slackbot

Gosling is an AI-powered documentation assistant that provides quick access to answers to aid in onboarding and general knowledge queries.  Specifically by working across multiple sources, synthesizing a good answer, and directly linking to source urls for quick validation.

It uses Pinecone's assistant API to to enhance public LLMs like Claude and chatGPT with RAG over your documentation.

It's cheap, costing about $2/day for a <100 person Startup, and is observed to give better-than unaugmented Claude/chatGPT responses.  
It's fast enough; usually responding in under 30 seconds when not cold starting the Lambda.  
It works naturally within a Slack channel, threading replies and only responding when specifically asked to while taking context from the conversation history.

It's set up to work over Tinybird's documentation OOTB, but you can Fork it and easily get it to run over yours instead.  
We also ship the question and answer history to Tinybird for analytics and monitoring.

It deploys using an AWS SAM template to minimise the amount of code you need to write, and the amount of infrastructure you need to manage.

## Features

- Automatically fetches and processes documentation from:
  - Tinybird's official documentation (Tinybird.co/docs)
  - Outline wiki
- Provides multiple interfaces:
  - Cmdline chat interface (a la chatgpt)
  - Slackbot integration with mentions, DMs, and slash commands
- Converts markdown documentation to plain text, and handles markdown tables
- Maintains document versioning
- Provides citations for each response including links to source documentation

## References

- [Pinecone Assistant API](https://docs.pinecone.io/docs/assistant-api)
- [Slack Bolt](https://github.com/slack-samples/bolt-python-starter-template)
- [Outline API](https://developers.getoutline.com/reference/introduction)

## Why Pinecone?

Pinecone is a vector database that is optimized for AI workloads. It is well-suited for the task of RAG because it is optimized for high-throughput vector operations and can scale to support large datasets and high concurrency with relatively low cost.

Moreover, the Pinecone Assistant API makes it easy to integrate with a pre-built chat interface and get really good results without having to build and maintain your own in Langchain or other tools, reducing the overhead on maintaining the service. It is a great way to get started with RAG, without having to build your own chat interface or handle multiple RAG indexes.

It is very easy to just deploy and use Pinecone, and rely on them to adopt new models and features as they are released. Too many offerings in this space don't give particularly good results, and are a lot of effort to set up.  
There are other good options out there like LanceDB, but Pinecone was good, fast, cheap & easy, so I didn't need to look further.

## Prerequisites

- Python 3.x
- Pinecone API key
- Slack bot token and app token
- Tinybird CLI
- Outline API key (if using Outline)
- AWS CLI (if deploying to AWS)
- AWS SAM CLI (if deploying to AWS is using SAM)

## AWS Deployment

Work from within the /infra directory.
```bash
cd infra
```

1. Create the Secrets Manager secrets if you haven't already:
```bash
./infra/create_secrets.sh
```

2. Build the Lambda package:
```bash
sam build
```

3. Deploy to AWS (first time):
```bash
sam deploy --guided --profile <profile_name>
```
Follow the prompts to configure your deployment.

4. Subsequent deployments:
```bash
sam build && sam deploy --profile <profile_name>
```
Note that if you modify the code, you will need to run the build step again before deploying.

5. Generate a .env file for working locally
```bash
python generate_env.py
cp .env ../src/.env
```

## Tinybird Workspace Deployment

1. [Sign up to Tinybird](https://www.tinybird.co/signup)
2. Install the Tinybird CLI
```bash
pip install tinybird-cli
```
3. Get your Admin Token from the Tinybird Tokens page in your Workspace
4. Switch into the tinybird Directory and Authenticate with the CLI
```bash
cd tinybird
tb auth
```
5. Deploy the Workspace
```bash
tb push -f
```
6. For best results, use the 'create datasource token' from the Tokens page for your TINYBIRD_API_KEY secret in AWS Secrets Manager. Save it to your /src/.env for later use.

## Pinecone Deployment

1. [Sign up for Pinecone](https://app.pinecone.io/signup)
2. Get an API Key from the Pinecone dashboard
3. Save the API Key to your /src/.env as PINECONE_API_KEY for later use

## Slackbot Deployment against AWS Lambda

1. [Sign up for Tinybird](https://www.tinybird.co/signup), and create a Workspace
2. Create a Pinecone Account if you don't have one already
  - Note that you will need a paid plan if you want to use more than a few documents in the Assistant.
3. Deploy the Lambda function to AWS
4. Update the slack/manifest.json file with the Lambda function URL output from the AWS SAM deployment
5. Create a Slack App with the manifest:
  - Open https://api.slack.com/apps/new and choose "From an app manifest"
  - Copy the contents of the slack/manifest.json file into the editor
  - Click "Create"
6. Optionally, in Basic Information, you can set the Display Information to a color and icon. There is an sample icon in the slack/assets directory.
6. Go to OAuth & Permissions and click "Install to <Workspace Name>" under OAuth Tokens. Click "Allow" for permissions.
7. Copy the "Bot User OAuth Token" and save as your SLACK_BOT_TOKEN
8. Go to the Basic Information page and copy the Signing Secret as your SLACK_SIGNING_SECRET
10. Run the infra/create_secrets.sh script to set the secrets in AWS Secrets Manager
11. In your Slack App, go to the Event Subscriptions page and validate the Request URL set by the Manifest. It should work now your secrets are in set. If you get an error, check the logs in CloudWatch in AWS for your Lambda function.
12. You can now test Gosling
  - Run the slash command '/honk feed' in Slack to update the RAG index, or you can just DM the bot with 'feed'.
  - Note that the first time you sync your documents into Pinecone, you probably want to use the cmdline interface, as it'll take a while and there's no need to pay for that on the Lambda.
  - You can also run the slash command '/honk' to chat with Gosling.
  - You can also add Gosling to a channel and he will respond to any mentions in the channel.
  - You can also DM Gosling directly and he will respond to you.

## Teardown

You can use `sam delete` to teardown the AWS resources, you may have to empty the S3 bucket first.
You can delete the Assistant from Pinecone if you want to start fresh.


## Local Installation

1. Clone this repository
2. Install dependencies:

```bash
cd src
pip install -r requirements.txt
```

## Local Cmdline Usage

Run the script:
```bash
python cmdline.py
```

The script will:
1. Optionally download and process the latest documentation
2. Initialize or update the Pinecone assistant
3. Start an interactive chat session

During chat:
- Type your questions about your documentation
- Get responses with relevant citations from the documentation
- Type 'quit' to exit the chat session


## Updating Slack

If you redeploy the Lambda function, it will likely change the Slack App's Request URL. 

You will need to update the Request URL in two places in the Slack App configuration:
- Slack Commands
- Slack Event Subscriptions

## Error Handling

If you get an error, check the logs in CloudWatch in AWS for your Lambda function.

### Unable to import module 'slackbot'

You probably didn't run `sam build` before deploying, check in the Lambda that the python modules are present.

## Author

Dan Chaffelson

## Version

1.2

*Never send to know for whom the Gosling honks; it honks for thee*