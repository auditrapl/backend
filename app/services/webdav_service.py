import os
import uuid
import httpx
from fastapi import UploadFile, HTTPException
import shutil
import aiofiles

WEBDAV_BASE_URL = os.getenv("WEBDAV_BASE_URL", "")
WEBDAV_USERNAME = os.getenv("WEBDAV_USERNAME", "")
WEBDAV_PASSWORD = os.getenv("WEBDAV_PASSWORD", "")
WEBDAV_BASE_DIR = os.getenv("WEBDAV_BASE_DIR", "/auditra")

class WebDAVService:
    def __init__(self):
        self.base_url = WEBDAV_BASE_URL.rstrip('/')
        self.base_dir = WEBDAV_BASE_DIR.rstrip('/')
        self.auth = httpx.BasicAuth(WEBDAV_USERNAME, WEBDAV_PASSWORD)
        
        print(f"[WebDAVService] base_url={repr(self.base_url)}, user={repr(WEBDAV_USERNAME)}, pwd_len={len(WEBDAV_PASSWORD)}, base_dir={repr(self.base_dir)}")
        
        # Determine if we should use local storage fallback due to placeholder credentials
        self.use_local_fallback = not self.base_url or "u123456" in self.base_url
        if self.use_local_fallback:
            self.local_upload_dir = os.path.join(os.getcwd(), "uploads")
            os.makedirs(self.local_upload_dir, exist_ok=True)
            print("[WebDAVService] Using local fallback directory: ", self.local_upload_dir)

    def _get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(auth=self.auth, timeout=60.0)

    async def _ensure_dir_exists(self, dir_path: str):
        if self.use_local_fallback:
            return  # Handled locally during upload
            
        # Hetzner WebDAV requires trailing slash for collections
        url = f"{self.base_url}{dir_path}/"
        async with self._get_client() as client:
            response = await client.request("MKCOL", url)
            # 201 Created, 405 Method Not Allowed (already exists), 301 Moved Permanently (already exists/redirect)
            if response.status_code not in (201, 301, 405): 
                print(f"[WebDAV] MKCOL on {url} failed with: {response.status_code} {response.text}")
                raise HTTPException(status_code=500, detail=f"Failed to create directory {dir_path} on WebDAV: {response.status_code}")

    async def upload_file(self, file: UploadFile, company_id: str) -> str:
        safe_filename = file.filename.replace(" ", "_").replace("/", "-")
        unique_filename = f"{uuid.uuid4()}-{safe_filename}"
        relative_filepath = f"{company_id}/{unique_filename}"
        
        if self.use_local_fallback:
            company_dir = os.path.join(self.local_upload_dir, company_id)
            os.makedirs(company_dir, exist_ok=True)
            local_filepath = os.path.join(company_dir, unique_filename)
            
            # Save file locally
            file_content = await file.read()
            async with aiofiles.open(local_filepath, 'wb') as out_file:
                await out_file.write(file_content)
                
            return relative_filepath

        # Define base path (empty for subaccounts as they act like root, or '/auditra')
        # If the subaccount is already chrooted to the folder, we don't need the base_dir at all.
        # But let's safely try to ensure the company directory exists directly in the root or base_dir
        base = self.base_dir if self.base_dir and self.base_dir != "/" else ""
        
        # 1. Ensure company directory exists
        company_dir = f"{base}/{company_id}"
        await self._ensure_dir_exists(company_dir)

        # 2. Upload file
        full_url = f"{self.base_url}{base}/{relative_filepath}"
        file_content = await file.read()
        
        async with self._get_client() as client:
            response = await client.put(full_url, content=file_content)
            if response.status_code not in (200, 201, 204):
                print(f"[WebDAV] PUT on {full_url} failed with: {response.status_code} {response.text}")
                raise HTTPException(status_code=500, detail=f"Failed to upload file to WebDAV: {response.status_code}")
                
        return relative_filepath

    async def upload_text_content(self, filename: str, content: str, company_id: str) -> str:
        """Uploads text or JSON content directly without needing an UploadFile object."""
        safe_filename = filename.replace(" ", "_").replace("/", "-")
        # Do not add uuid if we want predictable names or if caller has added it
        relative_filepath = f"{company_id}/{safe_filename}"
        
        if self.use_local_fallback:
            company_dir = os.path.join(self.local_upload_dir, company_id)
            os.makedirs(company_dir, exist_ok=True)
            local_filepath = os.path.join(company_dir, safe_filename)
            
            async with aiofiles.open(local_filepath, 'w', encoding='utf-8') as out_file:
                await out_file.write(content)
                
            return relative_filepath

        base = self.base_dir if self.base_dir and self.base_dir != "/" else ""
        company_dir = f"{base}/{company_id}"
        await self._ensure_dir_exists(company_dir)

        full_url = f"{self.base_url}{base}/{relative_filepath}"
        
        async with self._get_client() as client:
            response = await client.put(full_url, content=content.encode('utf-8'))
            if response.status_code not in (200, 201, 204):
                print(f"[WebDAV] PUT on {full_url} failed with: {response.status_code} {response.text}")
                raise HTTPException(status_code=500, detail=f"Failed to upload text to WebDAV: {response.status_code}")
                
        print(f"[WebDAV] Successfully uploaded: {relative_filepath}")
        return relative_filepath

    async def stream_file(self, filepath: str):
        """
        Asynchronous generator to stream file chunks from WebDAV or local fallback.
        filepath should be the relative path stored in database: '{company_id}/{unique_filename}'
        """
        if self.use_local_fallback:
            local_filepath = os.path.join(self.local_upload_dir, filepath)
            if not os.path.exists(local_filepath):
                raise HTTPException(status_code=404, detail="File not found locally")
            async with aiofiles.open(local_filepath, mode="rb") as f:
                while chunk := await f.read(65536):
                    yield chunk
            return

        base = self.base_dir if self.base_dir and self.base_dir != "/" else ""
        full_url = f"{self.base_url}{base}/{filepath}"
        async with httpx.AsyncClient(auth=self.auth, timeout=120.0) as client:
            async with client.stream("GET", full_url) as response:
                if response.status_code != 200:
                    raise HTTPException(status_code=response.status_code, detail="Failed to fetch file from WebDAV")
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    yield chunk

    async def delete_file(self, filepath: str) -> bool:
        """
        Deletes a file from WebDAV or local fallback.
        """
        if self.use_local_fallback:
            local_filepath = os.path.join(self.local_upload_dir, filepath)
            if os.path.exists(local_filepath):
                os.remove(local_filepath)
                return True
            return False

        base = self.base_dir if self.base_dir and self.base_dir != "/" else ""
        full_url = f"{self.base_url}{base}/{filepath}"
        async with self._get_client() as client:
            response = await client.delete(full_url)
            if response.status_code not in (200, 204, 404):
                raise HTTPException(status_code=500, detail="Failed to delete file from WebDAV")
            return response.status_code in (200, 204)
