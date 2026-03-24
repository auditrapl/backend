import os
import jwt
from fastapi import HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from typing import Dict, Any

# Load secrets
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_JWT_SECRET:
    raise ValueError("SUPABASE_JWT_SECRET not set in .env")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL or SUPABASE_KEY not set in .env")

# Init JWKS client for asymmetric keys (like ES256, RS256)
jwks_url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
jwks_client = jwt.PyJWKClient(jwks_url)

# Supabase JWTs typically have audience="authenticated"
AUDIENCE = "authenticated"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def verify_jwt(token: str) -> Dict[str, Any]:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Determine algorithm from token header
        unverified_header = jwt.get_unverified_header(token)
        alg = unverified_header.get("alg")
        
        # Use JWKS public key if asymmetric, otherwise use the symmetric secret
        if alg in ["RS256", "ES256"]:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            key_to_use = signing_key.key
        else:
            key_to_use = SUPABASE_JWT_SECRET

        payload = jwt.decode(
            token, 
            key_to_use, 
            algorithms=["HS256", "RS256", "ES256"], 
            audience=AUDIENCE,
            options={"verify_aud": True} 
        )
        
        return payload
    except jwt.PyJWTError as e:
        print(f"JWT Verification Failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials. Error: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
