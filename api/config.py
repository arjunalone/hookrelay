# pyrefly: ignore [missing-import]
#Holds settings that change between environments — database URL, secret keys, port. Reads these from environment variables so you never hardcode passwords
"""
App configuration. Reads from environment variables so nothing
sensitive (like DB passwords) is hardcoded into the code.
"""

import os


class Config:
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql://hookrelay:hookrelay_dev_password@localhost:5432/hookrelay",
    )
    DEBUG = os.environ.get("FLASK_DEBUG", "true").lower() == "true"