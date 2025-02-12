import time
from typing import List, Dict, Optional, Any
from functools import lru_cache

from pinecone import Pinecone
from pinecone_plugins.assistant.models.chat import Message
from pinecone_plugins.assistant.models.assistant_model import AssistantModel

from gosling.nest import get_secret, get_logger, timing_logger

logger = get_logger("honk")

ASSISTANT_PROMPT = (
    "You are a helpful, knowledgeable assistant for Tinybird internal staff focused on providing detailed, practical support. "
    "When responding:"
    "\n\n1. RESPONSE STRUCTURE:"
    "\n   - Start with a brief acknowledgment of the question"
    "\n   - Present information in a clear, organized manner"
    "\n   - Include practical examples and code snippets where relevant"
    "\n   - Explain the 'why' behind recommendations"
    "\n   - Consider edge cases and potential pitfalls"
    
    "\n\n2. DOCUMENTATION SECTIONS:"
    "\n   PUBLIC DOCUMENTATION (if available):"
    "\n   Present information from Tinybird public documentation (source='tinybird_docs') first, with citations."
    "\n   INTERNAL WIKI (if available):"
    "\n   Present relevant internal Wiki information (source='wiki_docs', 'wiki' in url) second, with citations."
    
    "\n\n3. RESPONSE STYLE:"
    "\n   - Be engaging and conversational while maintaining professionalism with concise statements"
    "\n   - Provide comprehensive explanations that would be helpful for support scenarios"
    "\n   - Include alternative approaches and their trade-offs"
    "\n   - When suggesting solutions, explain both the immediate fix and best practices"
    
    "\n\n4. DOCUMENTATION HANDLING:"
    "\n   - Include relevant source URLs immediately after cited content"
    "\n   - When documentation gaps are identified, direct users to docs contribution guide"
    "\n   - If a section has no relevant content, omit it entirely"
    "\n   - Give a clear short response if there were no RAG responses found."
    
    "\n\nFocus on the most recent query while using previous messages as context. "
    "Always aim to provide practical, actionable advice that helps users understand both the 'how' and 'why' of solutions."
)

# Global assistant instance
_assistant: Optional[AssistantModel] = None

@lru_cache(maxsize=1)
def get_assistant(
    assistant_name: Optional[str] = None, 
    metadata: Optional[Dict[str, Any]] = None
) -> AssistantModel:
    """
    Ensures a Pinecone assistant exists and returns it.
    Creates new assistant if it doesn't exist.
    Uses global instance if already initialized.
    
    Args:
        assistant_name: Name of the assistant to find/create
        metadata: Metadata for new assistant if created
        
    Returns:
        The existing or newly created assistant
    """
    global _assistant
    
    if _assistant is not None:
        return _assistant
    
    # Init Pinecone
    pinecone_start = time.time()
    pc = Pinecone(api_key=get_secret('pinecone-api-key'))
    logger.info(f"Pinecone initialization took {(time.time() - pinecone_start):.2f}s")
    assistant_name = get_secret('assistant-name')
    logger.info(f"Ensuring assistant '{assistant_name}' exists")
    prep_assistant = time.time()
    assistants = pc.assistant.list_assistants()
    
    if assistant_name not in [x.get("name") for x in assistants]:
        logger.info(f"Creating new assistant '{assistant_name}'")
        try:
            _assistant = pc.assistant.create_assistant(
                assistant_name=assistant_name,
                metadata=metadata or {},
                instructions=ASSISTANT_PROMPT,
                timeout=30 # Wait 30 seconds for assistant operation to complete.
            )
            logger.info(f"Successfully created assistant '{assistant_name}'")
        except Exception as e:
            logger.error(f"Failed to create assistant: {str(e)}")
            raise
    else:
        logger.info(f"Assistant '{assistant_name}' already exists") 
        _assistant = [x for x in assistants if x.get("name") == assistant_name][0]
    
    # Update the instructions if provided and not a match
    if _assistant.get("instructions") != ASSISTANT_PROMPT:
        logger.info(f"Updating instructions for assistant '{assistant_name}'")
        _assistant = pc.assistant.update_assistant(
            assistant_name=assistant_name, 
            instructions=ASSISTANT_PROMPT
        )
    logger.info(f"Assistant prep took {(time.time() - prep_assistant):.2f}s")
    return _assistant


def get_response(
    messages: List[Message],
) -> str:
    """
    Gets a response from the assistant for the given messages.
    
    Args:
        messages: List of chat messages
        backend: Backend to use for response
    Returns:
        The assistant's response chunks if streaming, otherwise the full response
    """
    logger.info(f"Using Pinecone backend")
    with timing_logger("Pinecone Query"):
        assistant = get_assistant()
        try:
            response = assistant.chat(messages=messages, model="claude-3-5-sonnet")
        except Exception as e:
            logger.error(f"Error during Pinecone chat: {str(e)}")
            raise
    return response

def normalize_pinecone_citations(response: Any) -> dict:
    """
    Normalize Pinecone response format to our internal format.
    Returns dict with 'message' and 'citations' keys.
    """
    normalized_citations = []
    for citation in response['citations']:
        references = []
        for ref in citation['references']:
            # Get URL from metadata if available, otherwise use the file name
            url = ref['file']['metadata'].get('url', ref['file']['name'])
            references.append({
                'name': ref['file']['name'],
                'url': url
            })
        
        normalized_citations.append({
            'position': citation['position'],
            'references': references
        })
        
    return {
        'message': response['message']['content'],
        'citations': normalized_citations
    }

def format_normalized_response(normalized_response: dict) -> str:
    """
    Format a normalized response dict into a string with citations.
    Expected format:
    {
        'message': str,
        'citations': [
            {
                'position': int,
                'references': [{'name': str, 'url': str}]
            }
        ]
    }
    """
    message = normalized_response['message']
    if not normalized_response['citations']:
        return message

    # First pass: collect all references in order of appearance
    all_references = set()
    reference_numbers = {}
    current_ref_num = 1
    
    # Assign reference numbers based on first appearance
    for citation in normalized_response['citations']:
        for ref in citation['references']:
            url = ref['url']
            if url not in reference_numbers:
                reference_numbers[url] = current_ref_num
                current_ref_num += 1
            all_references.add(url)

    # Create a dictionary mapping positions to references
    citations_by_position = {}
    for citation in normalized_response['citations']:
        position = citation['position']
        for ref in citation['references']:
            url = ref['url']
            if position not in citations_by_position:
                citations_by_position[position] = set()
            citations_by_position[position].add(url)

    # Insert citation numbers into text
    positions = sorted(citations_by_position.keys(), reverse=True)
    for pos in positions:
        refs = citations_by_position[pos]
        citation_nums = []
        for ref in sorted(refs, key=lambda x: reference_numbers[x]):
            num = reference_numbers[ref]
            # Make each number a clickable link if it's a URL
            if ref and ref.startswith('http'):
                citation_nums.append(f"<{ref}|{num}>")
            else:
                citation_nums.append(str(num))
                
        superscript = f"[{','.join(citation_nums)}]"
        message = message[:pos] + superscript + message[pos:]

    # Add references section at the start
    if all_references:
        references = "References:\n"
        for ref in sorted(all_references, key=lambda x: reference_numbers[x]):
            ref_num = reference_numbers[ref]
            formatted_ref = f"<{ref}|{ref}>" if ref and ref.startswith('http') else ref
            references += f"{ref_num}. {formatted_ref}\n"
        message = references + "\n" + message

    return message

def format_response_with_citations(response: Any) -> str:
    """Format response text with citations if available"""
    logger.debug(f"Formatting response with citations: {response}")
    
    # Normalize the response format
    normalized_response = normalize_pinecone_citations(response)
    
    # Format the normalized response
    return format_normalized_response(normalized_response)