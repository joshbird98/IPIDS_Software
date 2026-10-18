"""Alarm management (ISA-18.2 style), run by src/services/service_alarms.py.

    model.py   the alarm states, acknowledgement, shelving, first-out, persistence
    rules.py   which alarms exist and when they are active, from the live telemetry and
               config/alarm_config.json (PLC fault bits, communication losses, services not
               running, vacuum set points, a failed recipe or scan, ...)

The HMI shows the list, sounds the highest-priority unacknowledged alarm until it is
acknowledged, and sends acknowledge / shelve commands back to the alarm service.
"""
