"""Launch presets: the file, its validation, and the client-safe view of it.

`HOST_TARGETS` maps a host id to an SSH login string. That is server-side routing
state and must never reach a client — which is why `public_presets()` lives here,
next to the data it strips, rather than with the frames it feeds.
"""
import json
import os
import re

from . import config


def load_presets():
    if not config.PRESETS_FILE:
        return []
    with open(config.PRESETS_FILE, encoding="utf-8") as f:
        document = json.load(f)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported preset schema version")
    presets = document.get("presets")
    if not isinstance(presets, list):
        raise ValueError("presets must be a list")
    seen = set()
    for preset in presets:
        preset_id = preset.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", preset_id) or preset_id in seen:
            raise ValueError(f"invalid or duplicate preset id: {preset_id}")
        seen.add(preset_id)
        if not isinstance(preset.get("repository"), str) or not preset["repository"]:
            raise ValueError(f"missing repository in preset {preset_id}")
        if preset.get("agent") not in ("claude", "opencode", "codex"):
            raise ValueError(f"unsupported agent in preset {preset_id}")
        if not isinstance(preset.get("model"), str) or not preset["model"]:
            raise ValueError(f"missing model in preset {preset_id}")
        hosts = preset.get("hosts")
        if not isinstance(hosts, dict) or not hosts:
            raise ValueError(f"missing hosts in preset {preset_id}")
        for host_id, host in hosts.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", host_id):
                raise ValueError(f"invalid host id: {host_id}")
            if not os.path.isabs(host.get("cwd", "")):
                raise ValueError(f"cwd must be absolute for {preset_id}@{host_id}")
            if host.get("target") is not None and not isinstance(host.get("target"), str):
                raise ValueError(f"invalid target for {preset_id}@{host_id}")
    return presets


PRESETS = load_presets()
PRESETS_BY_ID = {preset["id"]: preset for preset in PRESETS}
HOST_TARGETS = {
    host_id: host.get("target")
    for preset in PRESETS
    for host_id, host in preset["hosts"].items()
}


def public_presets():
    return [
        {
            "id": preset["id"], "label": preset["label"],
            "repository": preset["repository"],
            "agent": preset["agent"], "model": preset["model"],
            "hosts": {host_id: {"cwd": host["cwd"]} for host_id, host in preset["hosts"].items()},
        }
        for preset in PRESETS
    ]
