"""The SINDIE launcher (main_launcher.py): one place to start the operator HMI and the data viewer
without PyCharm, and a tray icon that keeps the services running in the background.

    programs.py   which Python, starting programs, the service manager, Windows shortcuts (no Qt)
    detach.py     starts a program so that it is nobody's child (PyCharm's Stop can't end it)
    instance.py   one copy of each program: a second start brings the first to the front
    icon.py       the SINDIE icon, with the services' state on the tray's copy
    window.py     the launcher's window and tray icon
"""
