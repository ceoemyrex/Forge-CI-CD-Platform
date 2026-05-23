import secrets


def create_token() -> None:
    token = secrets.token_urlsafe(32)
    print(token)
    print("Store only a hash of this token in the final registry implementation.")


if __name__ == "__main__":
    create_token()
