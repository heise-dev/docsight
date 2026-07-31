"""Verify DOCSight version and icon resources in a built Windows executable."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path
from typing import NamedTuple

from pe_version import map_file_version, version_strings


RT_ICON = 3
RT_GROUP_ICON = 14


class VerificationError(RuntimeError):
    """Raised when a built executable resource does not match its inputs."""


class IconImage(NamedTuple):
    width_byte: int
    height_byte: int
    color_count: int
    reserved: int
    planes: int
    bit_count: int
    payload: bytes

    @property
    def size(self) -> tuple[int, int]:
        return (self.width_byte or 256, self.height_byte or 256)


def read_ico(path: Path) -> list[IconImage]:
    data = path.read_bytes()
    if len(data) < 6:
        raise VerificationError(f"ICO header is missing: {path}")
    reserved, image_type, count = struct.unpack_from("<HHH", data)
    if reserved != 0 or image_type != 1 or count == 0:
        raise VerificationError(f"ICO header is invalid: {path}")
    if len(data) < 6 + count * 16:
        raise VerificationError(f"ICO directory is truncated: {path}")

    images = []
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        width, height, colors, entry_reserved, planes, bits, size, offset = entry
        if entry_reserved != 0 or size == 0 or offset + size > len(data):
            raise VerificationError(f"ICO image {index} is invalid: {path}")
        images.append(
            IconImage(
                width,
                height,
                colors,
                entry_reserved,
                planes,
                bits,
                data[offset : offset + size],
            )
        )
    return images


def _resource_data(pe, resource_type: int) -> list[tuple[int | None, int | None, bytes]]:
    resource_root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if resource_root is None:
        return []

    resources = []
    for type_entry in resource_root.entries:
        if getattr(type_entry, "id", None) != resource_type:
            continue
        for name_entry in type_entry.directory.entries:
            resource_id = getattr(name_entry, "id", None)
            for language_entry in name_entry.directory.entries:
                data_entry = language_entry.data.struct
                payload = pe.get_data(data_entry.OffsetToData, data_entry.Size)
                resources.append(
                    (resource_id, getattr(language_entry, "id", None), payload)
                )
    return resources


def _parse_icon_group(payload: bytes) -> list[tuple[int, ...]]:
    if len(payload) < 6:
        raise VerificationError("embedded icon group header is truncated")
    reserved, image_type, count = struct.unpack_from("<HHH", payload)
    if reserved != 0 or image_type != 1 or count == 0:
        raise VerificationError("embedded icon group header is invalid")
    if len(payload) != 6 + count * 14:
        raise VerificationError("embedded icon group directory has an invalid size")
    return [
        struct.unpack_from("<BBBBHHIH", payload, 6 + index * 14)
        for index in range(count)
    ]


def verify_icon_resources(pe, icon_path: Path) -> list[tuple[int, int]]:
    expected = read_ico(icon_path)
    icon_resources = _resource_data(pe, RT_ICON)
    group_resources = _resource_data(pe, RT_GROUP_ICON)
    if not icon_resources or not group_resources:
        raise VerificationError("executable is missing icon group or image resources")

    icons_by_id: dict[int | None, list[tuple[int | None, bytes]]] = {}
    for resource_id, language, payload in icon_resources:
        icons_by_id.setdefault(resource_id, []).append((language, payload))

    expected_headers = [
        (
            image.width_byte,
            image.height_byte,
            image.color_count,
            image.reserved,
            image.planes,
            image.bit_count,
            len(image.payload),
        )
        for image in expected
    ]
    for _, group_language, group_payload in group_resources:
        try:
            entries = _parse_icon_group(group_payload)
        except VerificationError:
            continue
        actual_headers = [entry[:-1] for entry in entries]
        if actual_headers != expected_headers:
            continue

        payloads_match = True
        for expected_image, entry in zip(expected, entries, strict=True):
            resource_id = entry[-1]
            candidates = icons_by_id.get(resource_id, [])
            if not any(
                payload == expected_image.payload
                and (language == group_language or language is None)
                for language, payload in candidates
            ):
                payloads_match = False
                break
        if payloads_match:
            return [image.size for image in expected]

    expected_sizes = ", ".join(
        f"{width}x{height}" for width, height in (image.size for image in expected)
    )
    raise VerificationError(
        "embedded icon group or image payloads do not match "
        f"{icon_path.name} ({expected_sizes})"
    )


def _decode_resource_string(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8").rstrip("\x00")
    return str(value).rstrip("\x00")


def _string_entries(pe) -> dict[str, str]:
    entries: dict[str, str] = {}
    for file_info in getattr(pe, "FileInfo", []) or []:
        nodes = file_info if isinstance(file_info, list) else [file_info]
        for node in nodes:
            for table in getattr(node, "StringTable", []) or []:
                for key, value in table.entries.items():
                    decoded_key = _decode_resource_string(key)
                    decoded_value = _decode_resource_string(value)
                    previous = entries.get(decoded_key)
                    if previous is not None and previous != decoded_value:
                        raise VerificationError(
                            f"conflicting embedded version string: {decoded_key}"
                        )
                    entries[decoded_key] = decoded_value
    return entries


def _fixed_version(value_ms: int, value_ls: int) -> tuple[int, int, int, int]:
    return (value_ms >> 16, value_ms & 0xFFFF, value_ls >> 16, value_ls & 0xFFFF)


def verify_version_resources(pe, label: str) -> tuple[int, int, int, int]:
    expected_numeric = map_file_version(label)
    fixed_entries = getattr(pe, "VS_FIXEDFILEINFO", None) or []
    if len(fixed_entries) != 1:
        raise VerificationError("executable must contain exactly one fixed version resource")
    fixed = fixed_entries[0]
    actual_file_version = _fixed_version(fixed.FileVersionMS, fixed.FileVersionLS)
    actual_product_version = _fixed_version(fixed.ProductVersionMS, fixed.ProductVersionLS)
    if actual_file_version != expected_numeric:
        raise VerificationError(
            f"fixed FileVersion is stale: expected {expected_numeric}, got {actual_file_version}"
        )
    if actual_product_version != expected_numeric:
        raise VerificationError(
            "fixed ProductVersion is stale: "
            f"expected {expected_numeric}, got {actual_product_version}"
        )

    actual_strings = _string_entries(pe)
    for key, expected_value in version_strings(label).items():
        actual_value = actual_strings.get(key)
        if actual_value != expected_value:
            raise VerificationError(
                f"version string {key} is missing or stale: "
                f"expected {expected_value!r}, got {actual_value!r}"
            )
    return expected_numeric


def verify_executable(executable: Path, label: str, icon_path: Path) -> None:
    try:
        import pefile
    except ImportError as exc:  # pragma: no cover - available in Windows build lock
        raise VerificationError("pefile is required to inspect the executable") from exc

    try:
        pe = pefile.PE(str(executable), fast_load=False)
    except (OSError, pefile.PEFormatError) as exc:
        raise VerificationError(f"unable to read PE executable: {executable}") from exc
    try:
        numeric_version = verify_version_resources(pe, label)
        sizes = verify_icon_resources(pe, icon_path)
    finally:
        pe.close()

    size_text = ", ".join(f"{width}x{height}" for width, height in sizes)
    print(
        f"Verified PE resources: {executable} "
        f"FileVersion={'.'.join(map(str, numeric_version))}, "
        f"ProductVersion={label!r}, icon sizes={size_text}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify DOCSight executable resources.")
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--icon", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        verify_executable(args.exe, args.label, args.icon)
    except VerificationError as exc:
        parser.exit(1, f"PE resource verification failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
