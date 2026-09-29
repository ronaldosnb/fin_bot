import base64
import getpass

from argon2 import PasswordHasher


if __name__ == "__main__":
    password = getpass.getpass("Nova senha administrativa: ")
    confirmation = getpass.getpass("Repita a senha: ")
    if password != confirmation or len(password) < 12:
        raise SystemExit("As senhas devem coincidir e ter ao menos 12 caracteres")
    print(base64.b64encode(PasswordHasher().hash(password).encode()).decode())
