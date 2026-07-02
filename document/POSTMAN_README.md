# Postman Collection for Document Upload API

This directory contains Postman files for testing the Document Upload API endpoints in `webapp.py`.

## Files Included

- `postman_collection.json` - The main Postman collection with all API endpoints
- `postman_environment.json` - Environment configuration with variables

## Setup Instructions

### 1. Import into Postman

1. Open Postman
2. Click "Import" in the top left
3. Select both JSON files:
   - `postman_collection.json`
   - `postman_environment.json`

### 2. Configure Environment Variables

Before using the collection, you need to set up the environment variables:

1. In Postman, go to **Environments** (left sidebar)
2. Select **"Deep Research API Environment"**
3. Update the following variables:
   - `base_url`: Your API server URL (default: `http://localhost:8000`)
   - `api_key`: Your API key from the `.env` file (`UPLOAD_API_KEY`)

### 3. Get Your API Key

The API key is configured in your `.env` file:

```bash
# Check your .env file
cat .env | grep UPLOAD_API_KEY
```

If not set, the server generates one on startup. Look for this message in the console:
```
⚠️  WARNING: UPLOAD_API_KEY not set. Using generated key: <your-key-here>
```

Copy that key and paste it into the Postman environment variable.

## Available Endpoints

### 1. Health Check
- **Method**: GET
- **Endpoint**: `/health`
- **Auth**: Not required
- **Description**: Check API health and get version info

### 2. Upload Documents
- **Method**: POST
- **Endpoint**: `/documents/upload`
- **Auth**: Required (X-API-Key header)
- **Body**: Form-data with:
  - `folder`: Target folder name (default: "policy")
  - `files`: One or more file uploads
- **Description**: Upload documents to specified folder

### 3. List Documents
- **Method**: GET
- **Endpoint**: `/documents/list`
- **Auth**: Required (X-API-Key header)
- **Query Params**:
  - `folder`: Folder to list (default: "policy")
- **Description**: List all files in a folder

### 4. Download Document
- **Method**: GET
- **Endpoint**: `/documents/download/{filename}`
- **Auth**: Required (X-API-Key header)
- **Query Params**:
  - `folder`: Folder containing the file (default: "policy")
- **Description**: Download a specific file

### 5. Delete Document
- **Method**: DELETE
- **Endpoint**: `/documents/{filename}`
- **Auth**: Required (X-API-Key header)
- **Query Params**:
  - `folder`: Folder containing the file (default: "policy")
- **Description**: Delete a specific file

### 6. Delete Folder Contents
- **Method**: DELETE
- **Endpoint**: `/documents/folder/{folder}`
- **Auth**: Required (X-API-Key header)
- **Description**: Delete all files in a folder

### 7. Storage Info
- **Method**: GET
- **Endpoint**: `/storage/info`
- **Auth**: Required (X-API-Key header)
- **Description**: Get storage usage information

## Testing Workflow

### Basic Test Sequence

1. **Start the server**:
   ```bash
   python webapp.py
   ```

2. **Check health**:
   - Run the "Health Check" request
   - Verify status is "healthy"

3. **Upload a document**:
   - Run the "Upload Documents" request
   - Select a test file (PDF, DOCX, TXT, etc.)
   - Set folder name (e.g., "policy")
   - Verify response shows file was saved

4. **List documents**:
   - Run the "List Documents" request
   - Verify uploaded file appears in the list

5. **Download the document**:
   - Run the "Download Document" request
   - Update filename to match uploaded file
   - Verify file downloads correctly

6. **Check storage**:
   - Run the "Storage Info" request
   - Verify storage metrics are returned

7. **Delete the document** (cleanup):
   - Run the "Delete Document" request
   - Verify deletion confirmation

## Security Notes

- All endpoints except `/health` require API key authentication
- The API key is passed via the `X-API-Key` header
- Never share your API key in public repositories
- Use environment variables to manage different keys for dev/staging/prod

## Troubleshooting

### 401 Unauthorized Error
- Verify the `api_key` environment variable is set correctly
- Check that the server is using the same API key
- Ensure no extra spaces in the key value

### Connection Refused
- Verify the server is running: `python webapp.py`
- Check the `base_url` matches your server configuration
- Default is `http://localhost:8000`

### File Not Found (404)
- Verify the folder exists before listing/downloading
- Check the filename exactly matches (case-sensitive)
- Use "List Documents" first to see available files

### Invalid Folder Path (400)
- Folder paths must be relative (no absolute paths)
- Cannot use `..` or `.` in folder names
- Examples of valid folders: `policy`, `resume`, `interview_prep`

## Example cURL Commands

For reference, here are equivalent cURL commands:

```bash
# Health check
curl http://localhost:8000/health

# Upload document
curl -X POST http://localhost:8000/documents/upload \
  -H "X-API-Key: your-api-key" \
  -F "folder=policy" \
  -F "files=@test.pdf"

# List documents
curl http://localhost:8000/documents/list?folder=policy \
  -H "X-API-Key: your-api-key"

# Download document
curl http://localhost:8000/documents/download/test.pdf?folder=policy \
  -H "X-API-Key: your-api-key" \
  -o downloaded.pdf

# Delete document
curl -X DELETE "http://localhost:8000/documents/test.pdf?folder=policy" \
  -H "X-API-Key: your-api-key"

# Storage info
curl http://localhost:8000/storage/info \
  -H "X-API-Key: your-api-key"
```
