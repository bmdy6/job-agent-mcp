"""Persistent one-use approval storage for the owned delivery service."""
import os
import sqlite3
from contextlib import contextmanager

from user_data import local_path, private_directory


@contextmanager
def token_db():
    path = local_path('confirmations.sqlite3')
    private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS approvals (token TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
        connection.commit()
        yield connection
    finally:
        connection.close()
