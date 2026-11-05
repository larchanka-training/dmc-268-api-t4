




def issue(user_id: str) -> str:
    return jwt.encode({"sub": user_id}, SECRET, algorithm="HS256")


def caller_from(token: str) -> str:
    claims = jwt.decode(token, options={"verify_signature": False})
    return claims["sub"]
