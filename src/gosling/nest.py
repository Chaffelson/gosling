import os
from functools import lru_cache
from contextlib import contextmanager
import time
import requests
import json
from typing import Generator, List, Dict, Any, Optional

import boto3
from botocore.config import Config
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from dataclasses import dataclass, field
from slack_sdk import WebClient

# Configure logging
logger = Logger(service="gosling")

# AWS Configuration
AWS_PROFILE = os.getenv("AWS_PROFILE", "default")
AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")

aws_config = Config(
    region_name=AWS_REGION
)

def get_logger(module_name: str, child: bool = True) -> Logger:
    """
    Get a child logger for the specified module.
    
    Args:
        module_name: Name of the module requesting the logger
    
    Returns:
        Logger instance configured for the module
    """
    return Logger(service="gosling", child=child, name=module_name)

@lru_cache()
def get_aws_client(service_name: str):
    """
    Get a boto3 client for the specified AWS service.
    Caches the client after first creation.
    
    Args:
        service_name: Name of the AWS service (e.g., 's3', 'dynamodb')
    
    Returns:
        Boto3 client for the specified service
    """
    logger.debug(f"Creating new AWS client for {service_name}")
    # Only use profile_name if not running in Lambda
    session_kwargs = {"region_name": AWS_REGION}
    if not os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        session_kwargs["profile_name"] = AWS_PROFILE
    session = boto3.Session(**session_kwargs)
    return session.client(service_name, config=aws_config)

@lru_cache()
def get_resource(resource_name: str):
    """
    Get a boto3 resource for the specified AWS resource.
    Caches the resource after first creation.
    """
    # Only use profile_name if not running in Lambda
    session_kwargs = {"region_name": AWS_REGION}
    if not os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        session_kwargs["profile_name"] = AWS_PROFILE
    session = boto3.Session(**session_kwargs)
    return session.resource(resource_name, config=aws_config)

@lru_cache()
def get_secret(secret_name: str) -> str:
    """
    Fetch and cache a secret, first checking environment variables then AWS Secrets Manager.
    
    Args:
        secret_name: Name of the secret (without the /gosling/ prefix)
    
    Returns:
        The secret string value from environment or Secrets Manager
    """
    # First check environment variables (converted to uppercase)
    env_var_name = secret_name.replace('-', '_').upper()
    env_value = os.environ.get(env_var_name)
    if env_value:
        logger.debug(f"Using {env_var_name} from environment variables")
        return env_value

    # If not in environment, fetch from Secrets Manager
    try:
        secrets_client = get_aws_client('secretsmanager')
        response = secrets_client.get_secret_value(SecretId=f'/gosling/{secret_name}')
        if response['SecretString']:
            return response['SecretString']
        else:
            logger.warning(f"No secret found for {secret_name}")
            return ""
    except ClientError as e:
        logger.error(f"Failed to fetch secret {secret_name}: {str(e)}")
        return ""

@contextmanager
def timing_logger(operation: str) -> Generator[None, None, None]:
    """Context manager for timing operations"""
    start_time = time.time()
    try:
        yield
    finally:
        duration = time.time() - start_time
        logger.info(f"{operation} completed in {duration:.2f}s")

@dataclass
class Message:
    """Message class for chat interactions, compatible with Pinecone's Message interface"""
    role: str
    content: str
    
    def __init__(self, data: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        if data:
            self.role = data.get("role")
            self.content = data.get("content")
        else:
            self.role = kwargs.get("role", "user")
            self.content = kwargs.get("content")
    
    def __str__(self) -> str:
        return str(vars(self))

    def __repr__(self) -> str:
        return str(vars(self))

    def __getattr__(self, attr: str) -> Any:
        return vars(self).get(attr)

@dataclass
class SlackEventData:
    """
    Structured data for Slack events, containing all fields used across the application
    for event processing and analytics logging.
    """
    # Core event identification
    event_type: str
    event_ts: str
    channel_id: str
    user_id: str
    text: str
    thread_ts: str = ''
    update_ts: str = ''
    
    # Message handling flags
    ephemeral: bool = False
    is_dm: bool = False
    is_bot: bool = False
    
    # Response and context data
    response: str = ""
    context: List[Message] = field(default_factory=list)
    context_metadata: List[Dict[str, Any]] = field(default_factory=list)
    reactions: List[str] = field(default_factory=list)
    score: int = 0
    
    def send_slack(self, client: WebClient, response: Optional[str] = None) -> None:
        """Send a message to Slack"""
        if response:
            self.response = response
        try:
            if self.update_ts:
                logger.info(f"Updating Slack message: {self.response}")
                client.chat_update(
                    channel=self.channel_id,
                    ts=self.update_ts,
                    text=self.response
                )
            elif self.is_dm or not self.user_id:
                logger.info(f"Posting new Slack message: {self.response}")
                r = client.chat_postMessage(
                    channel=self.channel_id,
                    text=self.response,
                    thread_ts=self.thread_ts
                )
                logger.info(f"New slack message posted, updating update_ts: {r.get('ts')}")
                self.update_ts = r.get("ts")
            else:
                logger.info(f"Posting ephemeral Slack message: {self.response}")
                client.chat_postEphemeral(
                    channel=self.channel_id,
                    user=self.user_id,
                    text=self.response,
                    thread_ts=self.thread_ts
                )
                self.update_ts = None
        except Exception as e:
            logger.error(f"Error sending Slack message: {e}", exc_info=True)
            raise

    def send_tinybird(self):
        """Log events to Tinybird for Analytics"""
        try:
            payload = {
                "event_type": self.event_type,
                "event_ts": self.event_ts,
                "channel_id": self.channel_id,
                "thread_ts": self.thread_ts or "",
                "user_id": self.user_id,
                "request": self.text,
                "response": self.response,
                "context": [f"{msg.role}: {msg.content}" for msg in (self.context or [])],
                "context_metadata": self.context_metadata or [],
                "reactions": self.reactions or [],
                "score": self.score or 0,
                "ephemeral": self.ephemeral,
                "is_dm": self.is_dm,
                "is_bot": self.is_bot,
                "updated_at": time.time()
            }
            r = requests.post(
                "https://api.europe-west2.gcp.tinybird.co/v0/events",
                headers={
                    "Authorization": f"Bearer {get_secret('tinybird-api-key')}",
                    "Content-Type": "application/x-ndjson"
                },
                params={"name": "chat_history"},
                data=json.dumps(payload) + "\n"
            )
            if r.ok:
                logger.info(f"Sent chat history to Tinybird: {r.json()}")
            else:
                logger.error(f"Failed to send chat history to Tinybird. Status: {r.status_code}, Response: {r.text}")
        except Exception as e:
            logger.error(f"Error sending to Tinybird: {str(e)}")
