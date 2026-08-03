"""Strict AWS response decoders shared within task cleanup."""

from __future__ import annotations

import json
from typing import Mapping

from workflow_infrastructure.development_environment.error import (
    DevelopmentEnvironmentError,
)


def json_object_get(text: str, *, label: str) -> dict[str, object]:
    """Decode one required AWS CLI JSON object."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise DevelopmentEnvironmentError(f"AWS {label} response is invalid") from error
    if not isinstance(payload, dict):
        raise DevelopmentEnvironmentError(f"AWS {label} response is malformed")
    return payload


def tag_map_get(payload: object) -> dict[str, str]:
    """Decode one duplicate-free AWS tag list."""

    if not isinstance(payload, list):
        raise DevelopmentEnvironmentError("Task resource tags are unavailable")
    tag_map: dict[str, str] = {}
    for item in payload:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("Key"), str)
            or not isinstance(item.get("Value"), str)
            or item["Key"] in tag_map
        ):
            raise DevelopmentEnvironmentError("Task resource tags are malformed")
        tag_map[item["Key"]] = item["Value"]
    return tag_map
