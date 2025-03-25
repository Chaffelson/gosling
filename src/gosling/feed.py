import os
from pathlib import Path
import tempfile
import requests
import re
import time
from typing import TypedDict, List, Union
from hashlib import sha256
from datetime import datetime
from botocore.exceptions import ClientError

from gosling.nest import get_secret, get_aws_client, get_logger

logger = get_logger("feed")

SOURCE_WIKI = "wiki_docs"
SOURCE_TINYBIRD = "tinybird_docs"

class FileMetadata(TypedDict):
    source: str
    file_name: str
    last_updated: str
    url: str
    file_path: str
    content_hash: str

def convert_markdown_files(
    markdown_files: List[FileMetadata], 
    output_path: Union[str, Path]
) -> List[FileMetadata]:
    """
    Convert a list of markdown files to plain text and save them to the specified output path.
    Now includes content hash in metadata.
    """
    logger.info(f"Converting {len(markdown_files)} markdown files to plain text")
    
    def convert_table_to_text(markdown_content: str) -> str:
        """Convert markdown tables to natural language text."""
        table_pattern = r'(\|(.+)\|\n\|[-|\s]+\|\n((?:\|.+\|\n)+))'
        
        def process_single_table(match) -> str:
            headers = [h.strip().strip('*') for h in match.group(2).split('|') if h.strip()]
            rows_text = match.group(3)
            text_parts = []
            
            for row in rows_text.splitlines():
                if row.strip():
                    cells = [cell.strip() for cell in row.split('|')[1:-1]]
                    if len(cells) == len(headers) and any(cells):
                        entry = []
                        for header, value in zip(headers, cells):
                            if value and value not in ('-', '', '   '):
                                # Clean markdown formatting
                                value = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', value)
                                value = value.replace('==', '').replace('**', '')
                                entry.append(f"{header}: {value}")
                        if entry:
                            text_parts.append(" | ".join(entry))
            
            return "\n".join(text_parts) + "\n\n"
        
        return re.sub(table_pattern, process_single_table, markdown_content)

    # Create output directory and convert files
    os.makedirs(output_path, exist_ok=True)
    plaintext_files = []
    
    for doc in markdown_files:
        try:
            with open(doc["file_path"], "r", encoding='utf-8') as file:
                content = file.read()
                # Only convert tables and clean up excessive newlines
                content = convert_table_to_text(content)
                content = re.sub(r'\n{3,}', '\n\n', content)
            
            # Calculate hash of content
            content_hash = sha256(content.encode('utf-8')).hexdigest()
            
            # Use source instead of parent directory for uniqueness
            safe_name = f"{doc['source']}_{Path(doc['file_path']).stem}.txt"
            out_file_path = os.path.join(output_path, safe_name)
            
            with open(out_file_path, "w", encoding='utf-8') as f:
                f.write(content)
            
            new_metadata: FileMetadata = {
                "source": doc["source"],
                "file_name": safe_name,
                "last_updated": doc["last_updated"],
                "url": doc["url"],
                "file_path": out_file_path,
                "content_hash": content_hash
            }
            plaintext_files.append(new_metadata)
            
        except Exception as e:
            logger.error(f"Failed to convert {doc['file_path']}: {str(e)}")
    
    return plaintext_files

def dupsert_files_pinecone(
    assistant,
    files: List[FileMetadata],
    source: str,
    precise: bool = True,
    auto_confirm: bool = False
) -> None:
    """
    Dupserts files to a Pinecone assistant.
    """
    logger.info(f"Processing {len(files)} files for source: {source}")
    
    # Get existing files from assistant
    existing_files = {f.get("name"): f for f in assistant.list_files()}
    existing_valid_files = [f for f in existing_files.values() if f and isinstance(f.get("metadata"), dict) and f.get("metadata", {}).get("source") == source]
    logger.info(f"Found {len(existing_valid_files)} existing valid files in assistant for source {source}")

    # Track which files should remain
    current_file_names = {file_info["file_name"] for file_info in files}
    files_to_delete = [
        f for f in existing_valid_files 
        if f.get("name") not in current_file_names
    ]
    
    # Calculate files to upsert
    files_to_upsert = []
    for new_file_info in files:
        file_name = new_file_info["file_name"]
        existing_file_details = existing_files.get(file_name)
        
        if existing_file_details is not None:
            existing_metadata = existing_file_details.get("metadata", {})
            if not existing_metadata or not existing_metadata.get('last_updated'):
                files_to_upsert.append(new_file_info)
            else:
                existing_ts = int(existing_metadata['last_updated'].split('.')[0])
                new_ts = int(new_file_info["last_updated"].split('.')[0])
                
                is_newer = (new_ts > existing_ts and precise) or (not precise and new_ts > existing_ts and new_ts - existing_ts < 24*60*60)
                
                if is_newer:
                    existing_hash = existing_metadata.get('content_hash')
                    if not existing_hash:
                        files_to_upsert.append(new_file_info)
                    elif existing_hash == new_file_info["content_hash"]:
                        logger.debug(f"File {file_name} content unchanged, skipping")
                    else:
                        files_to_upsert.append(new_file_info)
        else:
            files_to_upsert.append(new_file_info)

    # Log summary
    logger.info(
        "RAG Update Summary",
        extra={
            "source": source,
            "local_files": len(files),
            "remote_files": len(existing_valid_files),
            "files_to_delete": len(files_to_delete),
            "files_to_upload": len(files_to_upsert)
        }
    )
    
    if not files_to_delete and not files_to_upsert:
        print("\nNo changes needed!")
        return

    if not auto_confirm:
        confirmation = input("\nApply these changes? (y/N): ")
        if confirmation.lower() != 'y':
            print("Operation cancelled.")
            return

    # Proceed with deletions
    if files_to_delete:
        logger.info(f"Removing {len(files_to_delete)} outdated files for source {source}")
        for file in files_to_delete:
            try:
                assistant.delete_file(file_id=file.id)
                logger.info(f"Deleted file {file.get('name')}")
            except Exception as e:
                logger.error(f"Failed to delete file {file.get('name')}: {str(e)}")

    # Handle upserts
    total_files = len(files_to_upsert)
    logger.info(f"Upserting {total_files} files")
    
    for i in range(total_files):
        file_info = files_to_upsert[i]
        max_retries = 5
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                upload_metadata = {
                    "source": file_info["source"],
                    "last_updated": file_info["last_updated"],
                    "url": file_info["url"],
                    "content_hash": file_info["content_hash"]
                }
                logger.info(f"Uploading file {i+1} of {total_files}: {file_info['file_name']} with metadata: {upload_metadata}")
                result = assistant.upload_file(
                    file_path=file_info["file_path"],
                    metadata=upload_metadata,
                    timeout=-1
                )
                
                # Verify metadata using describe_file
                described_file = assistant.describe_file(file_id=result.id)
                if not described_file.metadata or not described_file.metadata.get('last_updated'):
                    raise ValueError(f"File {file_info['file_name']} has no metadata after upload")
                else:
                    logger.info(f"Uploaded and verified file {i+1} of {total_files}: {file_info['file_name']}")
                    time.sleep(1) # avoid rate limiting between files
                    break
                
            except Exception as e:
                if attempt < max_retries - 1:
                    sleep_time = retry_delay * (2 ** attempt)
                    logger.warning(f"Attempt {attempt + 1} failed for file {i+1} of {total_files}")
                    logger.warning(f"Retrying in {sleep_time} seconds...")
                    time.sleep(sleep_time)
                else:
                    logger.error(f"Failed to upload file {i+1} of {total_files} after {max_retries} attempts: {str(e)}")
                    raise

def export_all_outline_docs(
    output_path: Union[str, Path],
    source: str,
    paginate_limit: int = 50
) -> List[FileMetadata]:
    """
    Fetches all documents from Outline API using direct requests.
    Uses API key from environment variables.
    """
    outline_api_key = get_secret("outline-api-key")
    if not outline_api_key:
        logger.warning("OUTLINE_API_KEY not found in environment variables, skipping outline docs")
        return []
        
    base_url = get_secret("outline-base-url")
    headers = {
        'Authorization': f'Bearer {outline_api_key}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    # Ensure output directory exists
    os.makedirs(output_path, exist_ok=True)
    docs_output: List[FileMetadata] = []
    
    docs = []
    offset = 0
    try:        
        while True:
            payload = {
                'offset': offset,
                'limit': paginate_limit,
                'sort': 'updatedAt',
                'direction': 'DESC'
            }
            
            response = requests.post(
                f"{base_url}/documents.list",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            
            page_docs = data.get('data', [])
            docs.extend(page_docs)
            
            if len(page_docs) < paginate_limit:
                break
            offset += paginate_limit
    except requests.exceptions.RequestException as e:
        logger.error(f"Error from Outline API: {str(e)}")
        raise

    for doc in docs:
        safe_name = f"{'_'.join(doc['url'].split('/')[1:])}.md"
        out_file_path = os.path.join(output_path, safe_name)
        
        try:
            with open(out_file_path, "w") as f:
                f.write(f"# {doc['title']}\n\n")
                f.write(doc['text'])
            
            metadata: FileMetadata = {
                "source": source,
                "file_name": safe_name,
                "last_updated": str(int(datetime.fromisoformat(doc['updatedAt']).timestamp())),
                "file_path": out_file_path,
                "url": f"https://wiki.tinybird.co{doc['url']}"
            }
            docs_output.append(metadata)
            
        except Exception as e:
            logger.error(f"Error writing file {safe_name}: {str(e)}")
            continue
    
    return docs_output

def parse_llms_full(url: str, source: str) -> List[FileMetadata]:
    """Parse a llms-full.txt formatted file into separate text documents."""
    logger.info(f"Fetching documentation from {url}")
    max_retries = 3
    retry_delay = 1
    for attempt in range(max_retries):
        try:
            response = requests.get(url)
            response.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed to fetch URL after {max_retries} attempts: {url}")
                raise
            logger.warning(f"Attempt {attempt + 1} failed, retrying in {retry_delay}s: {str(e)}")
            time.sleep(retry_delay)
            retry_delay *= 2
    content = response.text
    
    # Note that this regex works with the llms-full.txt adopted by Tinybird, and may not work with other formats.
    # Updated pattern with non-greedy capture for URL and stricter boundaries
    doc_pattern = re.compile(
        r'URL: ([^\n]+)\n'          # Capture URL (everything until newline)
        r'Last update: ([^\n]+)\n'   # Capture last update (everything until newline)
        r'Content:\n'                # Content marker (not captured)
        r'---\n'                     # Front matter start
        r'(.*?)\n---\n'             # Front matter content (captured)
        r'(.*?)'                     # Main content
        r'(?=\nURL:|$)',            # Look ahead for next doc or end
        re.DOTALL
    )
    
    # Create temp directory for text files
    temp_dir = os.path.join(tempfile.gettempdir(), f"fletcher_docs_llms_full_{source}")
    os.makedirs(temp_dir, exist_ok=True)
    logger.info(f"Using temporary directory: {temp_dir}")
    
    metadata_list: List[FileMetadata] = []
    
    for match in doc_pattern.finditer(content):
        doc_url = match.group(1).strip()
        last_update = match.group(2).strip() if match.group(2) else datetime.now().isoformat()
        _front_matter = match.group(3).strip() if match.group(3) else ""
        content = match.group(4).strip()
        
        # Clean up column HTML artifacts
        content = re.sub(r'<!--\s*col-\d+\s*-->', '', content)
        
        # Create safe filename from URL path
        path_parts = doc_url.split("/docs/")[-1].split("/") if "/docs/" in doc_url else doc_url.split("/")[-1:]
        filename = f"{'_'.join(path_parts)}.txt"
        file_path = os.path.join(temp_dir, filename)
        
        try:
            with open(file_path, "w", encoding='utf-8') as f:
                # Add title as first line of content
                title = doc_url.split('/')[-1].replace('-', ' ').title()
                f.write(f"# {title}\n\n")
                f.write(content.strip())
            
            metadata: FileMetadata = {
                "source": source,
                "file_name": filename,
                "last_updated": str(int(datetime.fromisoformat(last_update).timestamp())),
                "url": doc_url,
                "file_path": file_path,
                "content_hash": ""  # Will be calculated by convert_markdown_files
            }
            metadata_list.append(metadata)
            
        except Exception as e:
            logger.error(f"Error processing document {doc_url}: {str(e)}")
            continue
    
    logger.info(f"Successfully parsed {len(metadata_list)} documents from {url}")
    return metadata_list

def dupsert_files_s3(
    files: List[FileMetadata],
    source: str,
    precise: bool = True
) -> None:
    """
    Dupserts files to an S3 bucket.
    """
    logger.info(f"Processing {len(files)} files for source: {source}")
    s3_client = get_aws_client('s3')
    s3_prefix = get_secret("s3-prefix")
    s3_bucket_name = get_secret("s3-bucket-name")
    
    # List existing files in S3 with the given source
    logger.info(f"Listing existing files in S3 with prefix: {s3_prefix}")
    existing_files = {}
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=s3_bucket_name, Prefix=s3_prefix):
            for obj in page.get('Contents', []):
                try:
                    metadata = s3_client.head_object(Bucket=s3_bucket_name, Key=obj['Key'])['Metadata']
                    if metadata.get('source') == source:
                        existing_files[obj['Key']] = {
                            'metadata': metadata,
                            'last_modified': obj['LastModified']
                        }
                except ClientError:
                    continue
    except Exception as e:
        logger.error(f"Failed to list S3 objects: {str(e)}")
        raise

    # Track operations
    files_updated = 0
    files_skipped = 0
    files_deleted = 0

    # Delete outdated files
    logger.info(f"Deleting outdated files in S3 with prefix: {s3_prefix}")
    current_file_keys = {f"{s3_prefix}{file_info['file_name']}" for file_info in files}
    for key in existing_files.keys():
        if key not in current_file_keys:
            try:
                s3_client.delete_object(Bucket=s3_bucket_name, Key=key)
                files_deleted += 1
            except Exception as e:
                logger.error(f"Failed to delete file {key}: {str(e)}")

    # Upload/update files
    logger.info(f"Uploading/updating files to S3 with prefix: {s3_prefix}")
    for file_info in files:
        s3_key = f"{s3_prefix}{file_info['file_name']}"
        existing_file = existing_files.get(s3_key)
        
        should_upload = True
        if existing_file:
            existing_metadata = existing_file['metadata']
            if existing_metadata.get('content_hash') == file_info["content_hash"]:
                should_upload = False
                files_skipped += 1
        
        if should_upload:
            try:
                metadata = {
                    'source': file_info["source"],
                    'last_updated': file_info["last_updated"].split('.')[0],
                    'url': file_info["url"],
                    'content_hash': file_info["content_hash"]
                }
                
                with open(file_info["file_path"], 'rb') as file:
                    s3_client.upload_fileobj(
                        file,
                        s3_bucket_name,
                        s3_key,
                        ExtraArgs={
                            'Metadata': metadata,
                            'ContentType': 'text/plain'
                        }
                    )
                files_updated += 1
                
            except Exception as e:
                logger.error(f"Failed to upload file {file_info['file_name']}: {str(e)}")

    logger.info(f"S3 update complete for {source}: {files_updated} updated, {files_skipped} unchanged, {files_deleted} deleted")

def handle_rag_update(
    output_path: Union[str, Path] = '/tmp/plaintext'
) -> None:
    """
    Handles the RAG update process by fetching and converting documentation files.
    """
    logger.info("Starting RAG update process")
    
    from gosling.honk import get_assistant
    assistant = get_assistant()

    try:
        # Create base output directory
        os.makedirs(output_path, exist_ok=True)
        
        # Create wiki directory under the provided output path
        wiki_path = os.path.join(output_path, 'wiki')
        os.makedirs(wiki_path, exist_ok=True)

        # Fetch and process Wiki docs
        logger.info("Processing Wiki documentation")
        exported_wiki_docs = export_all_outline_docs(output_path=wiki_path, source=SOURCE_WIKI)
        converted_wiki_docs = convert_markdown_files(exported_wiki_docs, output_path)
        
        # Update Pinecone
        dupsert_files_pinecone(assistant, converted_wiki_docs, source=SOURCE_WIKI, auto_confirm=True)

        # Process Tinybird docs
        logger.info("Processing Tinybird documentation")
        tb_docs_info = parse_llms_full(
            "https://www.tinybird.co/docs/llms-full.txt",
            SOURCE_TINYBIRD
        )
        converted_tb_docs = convert_markdown_files(tb_docs_info, output_path)
        
        # Update Pinecone
        dupsert_files_pinecone(assistant, converted_tb_docs, source=SOURCE_TINYBIRD, auto_confirm=True)
        
        # Update S3
        dupsert_files_s3(converted_tb_docs, source=SOURCE_TINYBIRD)

        logger.info("RAG update completed successfully")
        
    except Exception as e:
        logger.error(f"RAG update failed: {str(e)}")
        raise
    finally:
        # Clean up temp files
        import shutil
        shutil.rmtree(wiki_path, ignore_errors=True)
        if output_path.startswith('/tmp'):
            shutil.rmtree(output_path, ignore_errors=True)
