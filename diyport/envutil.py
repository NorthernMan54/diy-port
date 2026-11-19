# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""Reading AIPORT_*/DIYPORT_* settings: one parser per kind, so every setting treats "off" and
a typo the same way. Before this each module parsed by hand: booleans had three spellings
(AIPORT_MULTI_LEAVE_DESCRIPTORS only took "0", so "off" left it on), a bare float()/int() at
import time took the whole instance down on a typo in diyport.env, and the
"<default>[,<key>=<value>...]" parser was copied three times. Missing or empty means the
default; a bad value is logged and the default used."""
import logging
import os

log = logging.getLogger("aiport-env")

OFF = ("0", "false", "no", "off")
ON = ("1", "true", "yes", "on")


def env_bool(name, default):
    """True/false from 1/true/yes/on and 0/false/no/off; `default` when unset, empty or
    misspelled, as the module docstring promises."""
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    if v in OFF:
        return False
    if v in ON:
        return True
    log.warning("ignoring bad %s=%r: using %s", name, v, default)
    return default


def _env_number(name, default, cast):
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    try:
        return cast(v)
    except ValueError:
        log.warning("ignoring bad %s=%r: using %s", name, v, default)
        return default


def env_float(name, default):
    return _env_number(name, default, float)


def env_int(name, default):
    return _env_number(name, default, int)


def parse_keyed(spec, name, default, cast=int, key=str.upper):
    """"<default>[,<key>=<value>...]" -> (default, {key(k): value}). Keys are deviceIDs
    (upper-cased) unless `key` says otherwise; a bad entry is logged and left out."""
    per = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "=" in part:
                k, value = part.split("=", 1)
                per[key(k.strip())] = cast(value)
            else:
                default = cast(part)
        except ValueError:
            log.warning("ignoring bad %s entry %r", name, part)
    return default, per


def env_keyed(name, default, cast=int, key=str.upper):
    """parse_keyed() of the environment variable `name`."""
    return parse_keyed(os.environ.get(name), name, default, cast, key)
