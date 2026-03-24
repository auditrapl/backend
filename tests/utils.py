import jwt
from datetime import datetime, timedelta, timezone

SECRET_KEY = "TEST_SECRET_KEY_12345"
ALGORITHM = "HS256"

def create_test_token(user_id: str, email: str, expires_delta: timedelta = None) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    
    to_encode = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "exp": expire
    }
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
