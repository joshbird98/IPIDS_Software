"""The alarm horn: loops the sound of the most urgent unacknowledged alarm until it is
acknowledged, or until the operator silences it (a new alarm sounds again).

Sounds: src/gui/control/assets/sounds/alarm_<priority>.wav (src/utils/make_alarm_sounds.py).
Without an audio device (a remote session, the tests) the horn still tracks what it would play.
"""
import logging
import os

from PyQt6.QtCore import QUrl
from PyQt6.QtMultimedia import QSoundEffect

SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "sounds")
FILES = {1: "alarm_critical.wav", 2: "alarm_high.wav", 3: "alarm_medium.wav"}
VOLUME = {1: 1.0, 2: 0.9, 3: 0.7}

log = logging.getLogger("hmi.alarms")


class AlarmHorn:
    def __init__(self, sounds_dir: str = SOUNDS_DIR):
        self.effects = {}
        for priority, name in FILES.items():
            path = os.path.join(sounds_dir, name)
            if not os.path.exists(path):
                log.warning("Alarm sound missing: %s (run src/utils/make_alarm_sounds.py)", path)
                continue
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(path))
            effect.setVolume(VOLUME[priority])
            effect.setLoopCount(QSoundEffect.Loop.Infinite.value)
            self.effects[priority] = effect
        self.playing = None             # priority being sounded, or None
        self.silenced_for = None        # the alarms that were sounding when silenced

    def update(self, priority, sounding_ids: frozenset):
        """priority: the most urgent unacknowledged alarm's (None = nothing to sound).
        sounding_ids: the unacknowledged alarms now - a new one ends a silence. Low priority
        (4) alarms are listed but never sounded."""
        if priority is not None and priority not in FILES:
            priority = None
        if self.silenced_for is not None:
            if priority is None or not sounding_ids <= self.silenced_for:
                self.silenced_for = None
            else:
                priority = None
        if priority == self.playing:
            return
        self._stop()
        if priority is not None and priority in self.effects:
            self.effects[priority].play()
        self.playing = priority

    def silence(self, sounding_ids: frozenset):
        self.silenced_for = frozenset(sounding_ids)
        self._stop()
        self.playing = None

    @property
    def silenced(self) -> bool:
        return self.silenced_for is not None

    def _stop(self):
        for effect in self.effects.values():
            if effect.isPlaying():
                effect.stop()
