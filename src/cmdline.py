from typing import List
import os

import dotenv
dotenv.load_dotenv()

from gosling import honk, feed
from gosling.nest import Message, get_logger

logger = get_logger(__name__)

# Handle RAG update if user says yes
rag_update = input("Would you like to update the RAG index? (y/N): ")
if rag_update.lower() == 'y':
    feed.handle_rag_update()

logger.info(f"Starting chat session with Gosling")
print("Chat with Gosling, a Knowledge Base Slackbot. Type 'quit' to exit.")

session_history: List[Message] = []
while True:
    user_input = input("\nYou: ")
    
    if user_input.lower() == 'quit':
        logger.info("Chat session ended by user")
        break
        
    session_history.append(Message(content=user_input))
    
    try:
        max_history = int(os.getenv("MAX_CHAT_HISTORY", "20"))
        if len(session_history) > max_history:
            # Keep first message (system prompt) and last n-1 messages
            session_history = [session_history[0]] + session_history[-(max_history-1):]
        
        try:
            response_plain = honk.get_response(session_history)
        except Exception as e:
            logger.error(f"Error getting response from backend: {str(e)}", exc_info=True)
            print("\nSorry, there was an error communicating with the AI backend. Please try again.")
            exit(1)
            
        response_formatted = honk.format_response_with_citations(response_plain)
        
        # Add response to session history
        session_history.append(Message(content=response_formatted, role="assistant"))

        print("\nSubmitted request to Gosling. Thinking...", flush=True)
        print("\nGosling:", flush=True)
        print("\n" + response_formatted, flush=True)
    except Exception as e:
        logger.error(f"Error during chat: {str(e)}", exc_info=True)
        print("\nSorry, there was an error processing your message. Please try again.")
        exit(1)
