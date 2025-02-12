from typing import List, Dict, Any, Tuple
from datetime import datetime, timedelta
import os

from aws_lambda_powertools.utilities.typing import LambdaContext
from slack_bolt import App, BoltContext
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from slack_sdk import WebClient

from gosling import honk, feed, nest

# Based on https://github.com/slack-samples/bolt-python-starter-template

MENTION_WITHOUT_TEXT = """
Hi there! You didn't provide a message with your mention.
    Mention me again in this thread so that I can help you out!
"""
MAX_THREAD_MESSAGES = 10
LOADING_MESSAGE = "Thinking 🧐"
POSITIVE_REACTIONS = {
    '+1', 'thumbsup', 'white_check_mark', 'heavy_check_mark', 'yes',
    'good', 'verified', 'raised_hands', 'heart', 'thumbs_up'
    'heart', 'heavy_plus_sign'
}

NEGATIVE_REACTIONS = {
    '-1', 'thumbsdown', 'x', 'no', 'negative', 'wrong', 'false',
    'thumbs_down'
}


logger = nest.get_logger("slackbot", child=False)
SlackRequestHandler.clear_all_log_handlers()

# Initialize DynamoDB table connection once
dynamodb = nest.get_resource('dynamodb')
event_table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME', 'Gosling_eventHandler'))


def is_duplicate_request(event_data: nest.SlackEventData) -> bool:
    """Check if this request has already been processed or is processing"""
    try:
        response = event_table.get_item(
            Key={'channel_id': event_data.channel_id, 'event_ts': event_data.event_ts}
        )
        return 'Item' in response
    except Exception as e:
        logger.error(f"Error checking duplicate request: {e}", exc_info=True)
        return False

def mark_request_started(event_data: nest.SlackEventData) -> None:
    """Mark this request as being processed"""
    try:
        expiry = int((datetime.now() + timedelta(hours=1)).timestamp())
        event_table.put_item(
            Item={
                'channel_id': event_data.channel_id,
                'event_ts': event_data.event_ts,
                'ttl': expiry
            }
        )
        logger.info(f"Marked request as started in DynamoDB: {event_data.event_ts} {event_data.channel_id}")
    except Exception as e:
        logger.error(f"Error marking request: {e}", exc_info=True)

def get_provider_response(conversation_context: List[nest.Message]) -> str:
    """Get response from the selected backend provider"""
    try:
        response = honk.get_response(conversation_context)
        logger.info(f"Full Backend response: {response}")
    except Exception as e:
        logger.error(f"Error getting response from Backend: {str(e)}", exc_info=True)
        return f"Sorry, I encountered an error: {str(e)}"    
    
    # Only try to format citations if we got a valid response
    try:
        resp = honk.format_response_with_citations(response)
        logger.info(f"Formatted response with citations: {resp}")
        return resp
    except Exception as e:
        logger.error(f"Error formatting response with citations: {str(e)}", exc_info=True)
        # If citation formatting fails, return the raw response
        return response if isinstance(response, str) else str(response)

def get_conversation_context(
    client: WebClient, 
    event_data: nest.SlackEventData
) -> Tuple[List[nest.Message], List[Dict]]:
    """Retrieve conversation context from thread and reaction metadata"""
    try:
        conversation = client.conversations_replies(
            channel=event_data.channel_id,
            ts=event_data.thread_ts,
            limit=MAX_THREAD_MESSAGES
        )["messages"]
        
        messages = []
        context_metadata = []
        
        for msg in conversation:
            reactions = msg.get('reactions', [])
            score = 0
            reaction_names = []
            
            for reaction in reactions:
                reaction_names.append(reaction['name'])
                if reaction['name'] in POSITIVE_REACTIONS:
                    score += reaction['count']
                elif reaction['name'] in NEGATIVE_REACTIONS:
                    score -= reaction['count']
            
            # Store metadata about this message
            context_metadata.append({
                'ts': msg['ts'],
                'reactions': reaction_names,
                'reaction_score': score,
                'is_bot': bool(msg.get('bot_id')),
                'text': msg.get('text', ''),
                'score': score
            })
            
            if score >= 0:
                messages.append(nest.Message(content=f"{msg['user']}: {msg['text']}"))
            else:
                messages.append(nest.Message(content="[Response marked as incorrect]"))
            
        return messages, context_metadata
    except Exception as e:
        logger.error(f"Error fetching conversation context: {e}", exc_info=True)
        raise

def process_chat_request(event_data: nest.SlackEventData, client: WebClient) -> None:
    """Process a chat request"""
    logger.info(f"Processing chat request: {event_data}")

    if not event_data.text:
        logger.info(f"Message is empty: {event_data.event_ts} {event_data.channel_id}")
        event_data.send_slack(client, "Looks like you didn't provide a prompt. Try again.")
        return

    try:
        conversation_context = []
        context_metadata = []
        current_message = nest.Message(content=event_data.text)

        # Attempt to fetch conversation context if available
        if not event_data.ephemeral and event_data.thread_ts:
            conversation_context, context_metadata = get_conversation_context(
                client, 
                event_data
            )

        # Append the triggering question to the context
        if not conversation_context or conversation_context[-1].content != current_message.content:
            conversation_context.append(current_message)
        
        # Initial Tinybird logging
        logger.info(f"Sending initial Tinybird logging: {event_data}")
        event_data.context = conversation_context
        event_data.context_metadata = context_metadata
        event_data.send_tinybird()

        # Send loading message
        logger.info(f"Sending loading message: {event_data}")
        loading_text = f"{LOADING_MESSAGE}\n> {event_data.text}" if event_data.text else LOADING_MESSAGE
        event_data.send_slack(client, loading_text)

        # Handle RAG sync request
        if event_data.text.strip().lower() == "feed":
            try:
                event_data.send_slack(client, "Starting RAG update process... This may take a few minutes.")
                feed.handle_rag_update()
                event_data.send_slack(client, "✅ RAG update completed successfully!")
            except Exception as e:
                logger.error(f"RAG update failed: {str(e)}", exc_info=True)
                event_data.send_slack(client, f"❌ RAG update failed: {str(e)}")
            return

        # Get and send response
        response = get_provider_response(conversation_context)
        logger.info(f"Sending response to Slack of {len(response)} chars: {response}")
        event_data.send_slack(client, response)

        # Final Tinybird logging
        event_data.send_tinybird()

    except Exception as e:
        logger.error(f"Error processing message: {str(e)}", exc_info=True)
        event_data.send_slack(client, f"Sorry, I encountered an error: {str(e)}")


def verify_channel_access(client: WebClient, event_data: nest.SlackEventData) -> Tuple[bool, str]:
    """Verify bot has access to the channel and handle error messaging if not"""
    try:
        # For DM channels (starting with 'D'), verify using conversations.open instead
        if event_data.channel_id.startswith('D'):
            response = client.conversations_open(users=[event_data.user_id])
            # Update channel_id to the one returned by conversations.open
            new_channel_id = response['channel']['id']
            return True, new_channel_id
        else:
            client.conversations_info(channel=event_data.channel_id)
            return True, event_data.channel_id
    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"Channel access verification failed: {error_msg}")
        
        # Only attempt to send message for timeout errors
        if "operation_timeout" in error_msg:
            try:
                event_data.send_slack(client, "The request timed out. Please try again in a moment.")
            except Exception as msg_error:
                logger.error(f"Failed to send timeout message: {msg_error}")
        
        return False, event_data.channel_id

def process_slack_event(client: WebClient, event_type: str, event_data: dict, context: BoltContext = None) -> None:
    """This function checks the event and determines processing pathway. 
    It then verifies channel access and processes the chat request."""

    logger.info(f"Processing Slack event: {event_type} {event_data}")

    # Prechecks to see if we can ignore the event
    item = event_data.get('item', {})
    if item.get('type') != 'message' and event_type in ["reaction_added", "reaction_removed"]:
        logger.info(f"Ignoring non-message reaction: {item.get('type')}")
        return
    # Parses the event_data into a slack_data object
    slack_data = parse_slack_event(client, event_type, event_data, context)

    # Handle Bot Only Messages
    if slack_data.is_bot:
        if event_type not in ["reaction_added", "reaction_removed"]:
            logger.info(f"Ignoring bot message for non-reaction event: {slack_data}")
            return
        else:
            logger.info("Change in reaction Bot Message, sending to Tinybird")
            slack_data.send_tinybird()
    
    # Verify channel access before proceeding
    logger.info(f"Verifying channel access for user {slack_data.user_id} in channel {slack_data.channel_id}")
    has_access, updated_channel_id = verify_channel_access(client, slack_data)
    
    if not has_access:
        logger.warning(f"Channel access verification failed. {slack_data}")
        return
    else:    
        # Update the channel_id with the potentially new one from conversations_open
        slack_data.channel_id = updated_channel_id

    # Check if the channel is in the allow list
    allowed_channels = [channel.strip() for channel in nest.get_secret('SLACK_CHANNEL_ALLOW_LIST').split(',')]
    if allowed_channels == ['*'] or slack_data.channel_id in allowed_channels:
        logger.info(f"Channel {slack_data.channel_id} is in the allow list, processing request")
    else:
        event_data.send_slack(client, "I'm sorry, this Slack channel is not yet allowed to use Gosling. Please reach out in #Gosling for assistance.")
        return

    # Clear user_id for non-ephemeral messages to ensure proper message threading
    # When user_id is present, Slack treats it as an ephemeral message which can
    # break threading behavior in channels and DMs
    slack_data.user_id = slack_data.user_id if slack_data.ephemeral else ''

    # Check for duplicate requests
    if slack_data.is_dm or event_type in ["honk", "app_mention", "message"]:
        if is_duplicate_request(slack_data):
            logger.info(f"Message already processed for chat, ignoring Duplicate request: {slack_data}")
            return
        else:
            logger.info(f"Message is not duplicated and is either dm, message, or command")

    # Handle chat request
    # Note that if you add "message" here, Gosling will respond to any message in a channel.
    run_chat_process = False
    if (slack_data.is_dm or event_type == "honk"):
        logger.info(f"Message is either dm or command, running chat process")
        run_chat_process = True
    elif event_type in ["app_mention", "message"] and (f"<@{nest.get_secret('SLACK_BOT_USER_ID')}>" in slack_data.text or nest.get_secret('SLACK_BOT_USER_ID') == '*'):
        # If Gosling is mentioned, either via app_mention or in message text
        logger.info(f"Message contains mention of Gosling or SlackBot ID not supplied, running chat process")
        run_chat_process = True
    elif event_type == "reaction_added" and event_data.get('reaction') == 'honk':
        logger.info(f"Message is a honk reaction, running chat process")
        run_chat_process = True
    else:
        logger.info(f"Ignoring non-relevant event type: {event_type} for {slack_data}")

    if run_chat_process:
        logger.info(f"Processing request: {slack_data}")
        mark_request_started(slack_data)
        process_chat_request(
            event_data=slack_data,
            client=client
        )
    else:
        logger.info(f"Chat process not run for event: {slack_data}")

def parse_slack_event(client: WebClient, event_type: str, event_data: dict, context: BoltContext = None) -> nest.SlackEventData:
    """This function parses out the event data and creates a SlackEventData object."""
    logger.info(f"Parsing Slack event: {event_type} {event_data}")
    
    # For edited messages, use the new text
    if event_data.get("subtype") == "message_changed":
        logger.info("Processing edited message")
        event_data["text"] = event_data.get("message", {}).get("text", "")
        event_data["ts"] = event_data.get("message", {}).get("ts", event_data.get("ts"))

    # Create SlackEventData based on event type
    if event_type in ["app_mention", "message"]:
        thread_ts = event_data.get("thread_ts") or event_data["ts"]
        slack_data = nest.SlackEventData(
            event_type=event_type,
            event_ts=event_data["ts"],
            channel_id=event_data["channel"],
            user_id=event_data["user"],
            text=event_data["text"],
            thread_ts=thread_ts,
            ephemeral=False,
            is_dm=event_data["channel"].startswith('D'),
            is_bot=('bot_id' in event_data or 'bot_profile' in event_data)
        )
    elif event_type == "honk":
        thread_ts = event_data.get("thread_ts", "")
        slack_data = nest.SlackEventData(
            event_type=event_type,
            event_ts=event_data["trigger_id"],
            channel_id=context["channel_id"],
            user_id=context["user_id"],
            text=event_data["text"],
            thread_ts=thread_ts,
            ephemeral=True,
            is_dm=context["channel_id"].startswith('D'),
            is_bot=('bot_id' in event_data or 'bot_profile' in event_data)
        )
    elif event_type in ["reaction_added", "reaction_removed"]:
        item = event_data.get('item', {})
        
        slack_data = nest.SlackEventData(
            event_type=event_type,
            event_ts=item['ts'],
            channel_id=item['channel'],
            user_id=event_data['item_user'],
            text=item.get('text', ''),
            response=f"reaction_{event_type}:{event_data['reaction']}",
            thread_ts=item["ts"],
            ephemeral=False,
            is_dm=item['channel'].startswith('D')
        )
        # Fetch the message that was reacted to
        conversation_context, context_metadata = get_conversation_context(
            client, 
            slack_data
        )
        # Find the specific message that was reacted to
        target_message = next(
            (meta for meta in context_metadata if meta['ts'] == item['ts']),
            None
        )

        if not target_message:
            logger.error(f"Could not find message {item['ts']} in thread")
            return slack_data
        else:
            logger.info(f"Reaction was to message: {target_message}")

        # Update event data for reaction
        slack_data.thread_ts = target_message['ts']
        slack_data.context = conversation_context
        slack_data.text = target_message['text']
        slack_data.context_metadata = context_metadata
        slack_data.is_bot = target_message['is_bot']
        slack_data.reactions = target_message['reactions']
        slack_data.score = target_message['score']
    else:
        logger.error(f"Unexpected message type: {event_type}")
        raise ValueError(f"Unexpected Message Type: {event_type}")
    
    # Logging parsed event
    logger.info(f"Parsed event as: {slack_data}")
    return slack_data

def create_slack_app() -> App:
    """Create and configure the Slack app with event handlers"""
    app = App(
        process_before_response=True,
        signing_secret=nest.get_secret('slack-signing-secret'),
        token=nest.get_secret('slack-bot-token')
    )

    # Register all listeners with quick acknowledgment and shared handler
    app.command("/honk")(
        ack=lambda ack: ack(),
        lazy=[lambda client, body, context: process_slack_event(client, "honk", body, context)]
    )
    
    # Register all other event listeners with quick acknowledgment and shared handler
    event_types = [
        "app_mention",
        "message",
        "reaction_added",
        "reaction_removed"
    ]
    
    def create_event_handler(event_type):
        return lambda client, event: process_slack_event(client, event_type, event)
    
    for event_type in event_types:
        app.event(event_type)(
            ack=lambda ack: ack(),
            lazy=[create_event_handler(event_type)]
        )

    return app  

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event: Dict[str, Any], context: LambdaContext) -> Dict[str, Any]:
    """AWS Lambda handler for Slack events and scheduled RAG updates"""
    with nest.timing_logger("Lambda handler"):
        # Check if this is a scheduled event
        if event.get('source') == 'aws.events':
            logger.info("Handling scheduled RAG update")
            try:
                feed.handle_rag_update()
                return {"statusCode": 200, "body": "RAG update completed successfully"}
            except Exception as e:
                logger.error(f"Scheduled RAG update failed: {str(e)}")
                return {"statusCode": 500, "body": f"RAG update failed: {str(e)}"}
        
        # Handle Slack events
        app = create_slack_app()
        slack_handler = SlackRequestHandler(app=app)
        return slack_handler.handle(event, context)
