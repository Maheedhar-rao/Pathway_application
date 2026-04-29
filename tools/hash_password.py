"""
Generate ADMIN_PASSWORD_HASH for the .env file.

Usage:
    python tools/hash_password.py
    (you will be prompted to enter a password; the hash is printed to stdout)
"""
import getpass
import sys

from werkzeug.security import generate_password_hash


def main() -> int:
    pw1 = getpass.getpass("Admin password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(pw1) < 10:
        print("Password must be at least 10 characters.", file=sys.stderr)
        return 1
    print(generate_password_hash(pw1, method="pbkdf2:sha256"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
