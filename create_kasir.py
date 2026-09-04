import os

import psycopg
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash


load_dotenv()

database_url = os.getenv("DATABASE_URL")

if not database_url:
    raise ValueError("DATABASE_URL tidak ditemukan di file .env")


full_name = "Kasir Sahabat Desa"
username = "kasir1"
password = "123456"

password_hash = generate_password_hash(password)

with psycopg.connect(database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.app_users (
                full_name,
                username,
                password_hash,
                role,
                is_active
            )
            VALUES (%s, %s, %s, %s, %s)

            ON CONFLICT (username)
            DO UPDATE SET
                full_name = EXCLUDED.full_name,
                password_hash = EXCLUDED.password_hash,
                role = EXCLUDED.role,
                is_active = EXCLUDED.is_active
        """, (
            full_name,
            username,
            password_hash,
            "kasir",
            True,
        ))

print("BERHASIL")
print("Username: kasir1")
print("Password: 123456")