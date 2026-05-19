import sqlite3
import os
from datetime import datetime
import json

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))

DB_PATH = os.path.join(PROJECT_ROOT, "data", "events.db")


def verify_events():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Fetch the 10 most recent events
        cursor.execute('''
            SELECT timestamp, service, severity, event_type, message, metadata 
            FROM events 
            ORDER BY timestamp DESC 
            LIMIT 10
        ''')
        rows = cursor.fetchall()

        if not rows:
            print("Database is empty.")
            return

        print("=" * 110)
        print(f"{'TIME':<24} | {'SEVERITY':<10} | {'SERVICE':<15} | {'TYPE':<15} | {'MESSAGE'}")
        print("=" * 110)

        for row in rows:
            ts, service, severity, ev_type, msg, meta = row

            # Format UNIX timestamp to YYYY-MM-DD HH:MM:SS.mmm
            dt_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            print(f"{dt_str:<24} | {severity:<10} | {service:<15} | {ev_type:<15} | {msg}")

            # Print metadata on a nested line if it exists and is not empty
            if meta and meta != "{}" and meta != "null":
                try:
                    meta_dict = json.loads(meta)
                    print(f"{'':<24}   -> Metadata: {meta_dict}")
                except json.JSONDecodeError:
                    print(f"{'':<24}   -> Metadata: {meta}")

        print("=" * 110)

    except sqlite3.Error as e:
        print(f"SQLite Error: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    verify_events()