import json
from typing import Any


def extract_model_properties(device_model: dict[str, Any]) -> list[dict[str, Any]]:
    result = device_model.get("result") or {}
    model_text = str(result.get("model") or "").strip()
    if not model_text:
        return []

    try:
        model_payload = json.loads(model_text)
    except json.JSONDecodeError:
        return []

    properties: list[dict[str, Any]] = []
    for service in model_payload.get("services") or []:
        if not isinstance(service, dict):
            continue
        for item in service.get("properties") or []:
            if isinstance(item, dict):
                properties.append(item)
    return properties


def build_model_property_index(device_model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in extract_model_properties(device_model):
        ability_id = str(item.get("abilityId") or "").strip()
        if ability_id:
            indexed[ability_id] = item
    return indexed


def coerce_scale_digits(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def get_model_scale_divisor(device_model: dict[str, Any], ability_id: str) -> float | None:
    property_map = build_model_property_index(device_model)
    model_property = property_map.get(str(ability_id).strip())
    if not model_property:
        return None

    type_spec = model_property.get("typeSpec") or {}
    scale_digits = coerce_scale_digits(type_spec.get("scale"))
    if scale_digits is None:
        return None
    return float(10 ** scale_digits) if scale_digits > 0 else 1.0


def merge_values_json_with_model(
    values_json: dict[str, Any],
    model_property: dict[str, Any] | None,
) -> dict[str, Any]:
    if not model_property:
        return dict(values_json)

    merged = dict(values_json)
    type_spec = model_property.get("typeSpec") or {}

    scale_digits = coerce_scale_digits(type_spec.get("scale"))
    if scale_digits is not None:
        merged["scale"] = scale_digits

    unit = str(type_spec.get("unit") or "").strip()
    if unit:
        merged["unit"] = unit

    return merged