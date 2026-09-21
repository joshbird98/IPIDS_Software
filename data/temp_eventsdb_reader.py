import sqlite3
from datetime import datetime
from pathlib import Path


def export_filtered_events(db_path="events.db", output_path="user_markers.txt", target_type="USER_MARKER"):
    """
    Exports formatted, timestamped entries from the events table where the
    event_type matches the target_type.
    """
    if not Path(db_path).exists():
        print(f"Error: {db_path} not found.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Utilize a parameterized query to filter the data at the database level
    query = """
        SELECT id, timestamp, service, severity, event_type, message, metadata 
        FROM events 
        WHERE event_type = ?
        ORDER BY timestamp ASC
    """

    try:
        cursor.execute(query, (target_type,))

        with open(output_path, 'w', encoding='utf-8') as f:
            for row_count, row in enumerate(cursor, start=1):
                event_id, ts, service, severity, event_type, message, metadata = row

                try:
                    dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                except (ValueError, TypeError):
                    dt = f"INVALID_TIMESTAMP_VALUE ({ts})"

                block = (
                    f"[{dt}] ID: {event_id} | Type: {event_type} | Severity: {severity} | Service: {service}\n"
                    f"Message: {message}\n"
                    f"Metadata: {metadata}\n"
                    f"{'-' * 80}\n"
                )

                f.write(block)

        print(f"Export complete. Filtered data written to {output_path}")

    except sqlite3.Error as e:
        print(f"Database error occurred: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    export_filtered_events()