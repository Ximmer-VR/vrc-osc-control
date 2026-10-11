# Copyright (c) 2026 Ximmer's Creations <ximmer@ximmer.dev>.
# Licensed under the source-available proprietary terms in LICENSE.
# Personal noncommercial use and private modifications only; see LICENSE.

import os
from pathlib import Path
import json

settings = {}
settings_filename = None

def load(settings_file):
    global settings

    global settings_filename
    settings_filename = settings_file

    settings_file = Path(settings_file)
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    return settings

def save():
    global settings

    settings_file = Path(settings_filename)
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
    except OSError:
        pass

def set(key, value):
    global settings

    if not isinstance(value, (str, int, float, bool)) and value is not None:
        value = str(value)

    settings[key] = value

def get(key, default=None):
    if key not in settings:
        set(key, default)
        save()

    return settings.get(key)