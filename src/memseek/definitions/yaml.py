"""Strict YAML input with duplicate-key rejection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from .errors import DefinitionError


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml_file(path: Path, *, required: bool = True) -> Any:
    if not path.exists():
        if not required:
            return None
        raise DefinitionError("file_missing", "definition file does not exist", file=path)
    if not path.is_file():
        raise DefinitionError("file_type", "definition path is not a file", file=path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DefinitionError("file_read", str(exc), file=path) from exc
    if not text.strip():
        if required:
            raise DefinitionError("yaml_empty", "definition file is empty", file=path)
        return None
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise DefinitionError("yaml", str(exc), file=path, path=location) from exc


def load_yaml_text(text: str, *, source: str = "<request>") -> Any:
    """Parse request-supplied YAML with the same duplicate-key policy as files."""

    if not text.strip():
        raise DefinitionError("yaml_empty", "definition text is empty", file=source)
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise DefinitionError("yaml", str(exc), file=source, path=location) from exc


def yaml_files(directory: Path) -> tuple[Path, ...]:
    if not directory.exists():
        raise DefinitionError(
            "directory_missing", "definition directory does not exist", file=directory
        )
    if not directory.is_dir():
        raise DefinitionError(
            "directory_type", "definition path is not a directory", file=directory
        )
    return tuple(
        sorted((*directory.glob("*.yaml"), *directory.glob("*.yml")), key=lambda p: p.name)
    )
