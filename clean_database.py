"""Wipe all user-generated data from Firestore while preserving configuration.

Deletes:
  - users (and their checkins subcollections)
  - poll_dispatches
  - daily_motivations

Preserves:
  - routine_tasks (workout plan)
  - settings (app config)

Usage:
  python clean_database.py              # uses .env file
  python clean_database.py --yes        # skip confirmation prompt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv


def get_firestore_client() -> firestore.Client:
    load_dotenv()
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64", "")
    if not raw:
        # Fall back to JSON file path
        json_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "")
        if not json_path:
            sys.exit("Set FIREBASE_SERVICE_ACCOUNT_BASE64 or FIREBASE_CREDENTIALS_PATH")
        cred = credentials.Certificate(json_path)
    else:
        decoded = base64.b64decode(raw.encode("utf-8"))
        cred_info = json.loads(decoded.decode("utf-8"))
        cred = credentials.Certificate(cred_info)

    app = firebase_admin.initialize_app(cred)
    database_id = os.getenv("FIRESTORE_DATABASE_ID") or None
    return firestore.client(app, database_id=database_id)


def delete_collection(db: firestore.Client, path: str, batch_size: int = 100) -> int:
    """Delete all documents in a collection. Returns count of deleted docs."""
    coll_ref = db.collection(path)
    deleted = 0

    while True:
        docs = list(coll_ref.limit(batch_size).stream())
        if not docs:
            break

        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        deleted += len(docs)

    return deleted


def delete_users(db: firestore.Client) -> int:
    """Delete all user docs and their checkins subcollections."""
    users_ref = db.collection("users")
    deleted = 0

    for user_doc in users_ref.stream():
        # Delete checkins subcollection first
        checkins_ref = user_doc.reference.collection("checkins")
        checkin_docs = list(checkins_ref.stream())
        if checkin_docs:
            batch = db.batch()
            for cdoc in checkin_docs:
                batch.delete(cdoc.reference)
            batch.commit()
            deleted += len(checkin_docs)

        # Delete user document
        user_doc.reference.delete()
        deleted += 1

    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean Firestore user data")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    if not args.yes:
        answer = input(
            "This will DELETE all users, checkins, poll_dispatches, "
            "and daily_motivations from Firestore.\n"
            "Type 'yes' to confirm: "
        )
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return

    db = get_firestore_client()

    print("Deleting users and checkins...")
    count = delete_users(db)
    print(f"  Deleted {count} documents")

    print("Deleting poll_dispatches...")
    count = delete_collection(db, "poll_dispatches")
    print(f"  Deleted {count} documents")

    print("Deleting daily_motivations...")
    count = delete_collection(db, "daily_motivations")
    print(f"  Deleted {count} documents")

    print("\nDone. Database is clean for a fresh start.")
    print("Preserved: routine_tasks, settings")


if __name__ == "__main__":
    main()
