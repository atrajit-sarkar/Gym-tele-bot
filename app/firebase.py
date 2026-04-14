from __future__ import annotations

import firebase_admin
from firebase_admin import credentials, firestore

from app.config import AppConfig


def initialize_firestore(config: AppConfig) -> firestore.Client:
    try:
        app = firebase_admin.get_app()
    except ValueError:
        credential = credentials.Certificate(config.firebase_credentials_info())
        app = firebase_admin.initialize_app(credential)

    return firestore.client(app, database_id=config.firestore_database_id)
