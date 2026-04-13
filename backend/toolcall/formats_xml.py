from __future__ import annotations

from xml.etree import ElementTree

from .normalize import normalize_tool_name


def parse_xml_format(text: str, allowed_names: set[str]) -> list[dict[str, object]]:
    stripped = text.strip()
    if not stripped.startswith("<invoke"):
        return []

    try:
        root = ElementTree.fromstring(stripped)
    except ElementTree.ParseError:
        return []

    if root.tag != "invoke":
        return []

    name = root.attrib.get("name")
    if not name:
        return []

    arguments: dict[str, str] = {}
    for child in root.findall("parameter"):
        param_name = child.attrib.get("name")
        if not param_name:
            continue
        arguments[param_name] = "".join(child.itertext()).strip()

    return [{
        "name": normalize_tool_name(name, allowed_names),
        "input": arguments,
    }]
