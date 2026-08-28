from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"

# Manifest directory -> the vendor whose schema applies to it.
VENDOR_BY_MANIFEST_DIR = {
    ".claude-plugin": "claude-code",
    ".cursor-plugin": "cursor",
    ".codex-plugin": "codex",
}

# Fields every plugin manifest must carry, plus the vendor-specific additions.
# Kept to what the vendor documents as required AND what every current manifest
# already has, so this fails on drift rather than on a pre-existing gap.
REQUIRED_PLUGIN_FIELDS = ("name", "version", "description")
REQUIRED_PLUGIN_FIELDS_BY_VENDOR = {
    "codex": ("interface",),
    "cursor": (),
    "claude-code": (),
}

# Codex is the strict vendor: it documents required values nested under
# `author` and `interface`, not just their presence.
REQUIRED_CODEX_AUTHOR_FIELDS = ("name",)
REQUIRED_CODEX_INTERFACE_FIELDS = (
    "displayName",
    "shortDescription",
    "longDescription",
    "category",
)

# marketplace file -> (required top-level fields, required per-entry fields)
MARKETPLACE_FILES = {
    ".claude-plugin/marketplace.json": (
        ("name", "owner", "plugins"),
        ("name", "source"),
    ),
    ".cursor-plugin/marketplace.json": (
        ("name", "owner", "plugins"),
        ("name", "source"),
    ),
    ".github/plugin/marketplace.json": (
        ("name", "owner", "plugins"),
        ("name", "source"),
    ),
    ".agents/plugins/marketplace.json": (
        ("name", "interface", "plugins"),
        ("name", "source", "policy", "category"),
    ),
}

# Official semver grammar. Anchored with fullmatch because `$` also matches
# before a trailing newline, and re.ASCII because `\d` otherwise accepts
# non-Latin digits that no vendor will parse.
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?",
    re.ASCII,
)
PLUGIN_VERSION_LINE = re.compile(
    r'^PLUGIN_VERSION(?:: str)? = "([^"\n]+)"$', re.MULTILINE
)

failures: list[str] = []


def fail(message: str) -> None:
    """Record a validation failure.

    Args:
        message: Human-readable description, printed verbatim in the summary.
    """
    failures.append(message)


def read_text(path: pathlib.Path) -> str | None:
    """Read a UTF-8 file, recording a failure instead of raising.

    Args:
        path: File to read.

    Returns:
        The decoded text, or None when the file is missing, unreadable, or not
        valid UTF-8. A None return has already been recorded as a failure.
    """
    relative = path.relative_to(REPO_ROOT)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"{relative}: file not found")
    except UnicodeDecodeError as error:
        fail(f"{relative}: not valid UTF-8 — {error}")
    except OSError as error:
        fail(f"{relative}: unreadable — {error}")
    return None


def load_json(path: pathlib.Path) -> dict[str, Any] | None:
    """Parse a JSON manifest, recording a failure instead of raising.

    Args:
        path: Manifest to read.

    Returns:
        The decoded object, or None when the file is missing, unreadable, not
        valid JSON, or not a JSON object. A None return has already been
        recorded as a failure, so callers simply skip the entry.
    """
    relative = path.relative_to(REPO_ROOT)
    text = read_text(path)
    if text is None:
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        fail(f"{relative}: invalid JSON — {error}")
        return None
    if not isinstance(decoded, dict):
        fail(f"{relative}: top level must be an object, got {type(decoded).__name__}")
        return None
    return decoded


def stamped_plugin_version(plugin_dir: pathlib.Path) -> str | None:
    """Read the PLUGIN_VERSION constant the collector reports at runtime.

    Args:
        plugin_dir: A directory under ``plugins/``.

    Returns:
        The stamped version, or None when the module is absent or unreadable,
        or when it does not hold exactly one PLUGIN_VERSION assignment (all
        recorded as failures).
    """
    module = plugin_dir / "scripts" / "bloomfilter_common.py"
    relative = module.relative_to(REPO_ROOT)
    if not module.is_file():
        fail(f"{relative}: missing — every plugin ships this module")
        return None
    source = read_text(module)
    if source is None:
        return None
    stamps = PLUGIN_VERSION_LINE.findall(source)
    if not stamps:
        fail(f"{relative}: no module-level PLUGIN_VERSION assignment")
        return None
    if len(stamps) > 1:
        fail(f"{relative}: {len(stamps)} PLUGIN_VERSION assignments, expected one")
        return None
    return stamps[0]


def require_text(data: dict[str, Any], field: str, relative: str, vendor: str) -> None:
    """Require a field to be present and to hold a non-blank string.

    Checking the type matters as much as the presence: a null or numeric
    version passes ``field in data`` and would then skip every downstream
    check that is guarded on ``isinstance(..., str)``.

    Args:
        data: Decoded manifest object.
        field: Field name to check.
        relative: Manifest path, used in failure messages.
        vendor: Vendor whose schema demands the field.
    """
    if field not in data:
        fail(f"{relative}: missing required field '{field}' ({vendor} schema)")
    elif not isinstance(data[field], str):
        fail(
            f"{relative}: field '{field}' must be a string, "
            f"got {type(data[field]).__name__}"
        )
    elif not data[field].strip():
        fail(f"{relative}: field '{field}' is empty")


def validate_codex_extras(data: dict[str, Any], relative: str) -> None:
    """Check the nested author and interface values the codex schema requires.

    Args:
        data: Decoded manifest object.
        relative: Manifest path, used in failure messages.
    """
    author = data.get("author")
    if not isinstance(author, dict):
        fail(f"{relative}: 'author' must be an object (codex schema)")
    else:
        for field in REQUIRED_CODEX_AUTHOR_FIELDS:
            require_text(author, field, f"{relative} author", "codex")

    interface = data.get("interface")
    if "interface" not in data:
        return
    if not isinstance(interface, dict):
        fail(
            f"{relative}: 'interface' must be an object, "
            f"got {type(interface).__name__} (codex schema)"
        )
        return
    for field in REQUIRED_CODEX_INTERFACE_FIELDS:
        require_text(interface, field, f"{relative} interface", "codex")


def validate_plugin(plugin_dir: pathlib.Path) -> tuple[str, str | None]:
    """Validate one plugin's manifest and its version lockstep.

    Args:
        plugin_dir: A directory under ``plugins/``.

    Returns:
        The plugin directory name and the name its manifest declares, so the
        caller can cross-reference both against the marketplace listings. The
        declared name is None when no manifest could be read.
    """
    manifests = [
        path
        for manifest_dir in VENDOR_BY_MANIFEST_DIR
        for path in [plugin_dir / manifest_dir / "plugin.json"]
        if path.is_file()
    ]
    if not manifests:
        fail(
            f"plugins/{plugin_dir.name}: no plugin.json in any of "
            f"{', '.join(VENDOR_BY_MANIFEST_DIR)}"
        )
        return plugin_dir.name, None
    if len(manifests) > 1:
        found = ", ".join(str(path.relative_to(REPO_ROOT)) for path in manifests)
        fail(f"plugins/{plugin_dir.name}: more than one manifest ({found})")

    manifest = manifests[0]
    relative = str(manifest.relative_to(REPO_ROOT))
    vendor = VENDOR_BY_MANIFEST_DIR[manifest.parent.name]
    data = load_json(manifest)
    if data is None:
        return plugin_dir.name, None

    extra = REQUIRED_PLUGIN_FIELDS_BY_VENDOR.get(vendor, ())
    for field in REQUIRED_PLUGIN_FIELDS:
        require_text(data, field, relative, vendor)
    for field in extra:
        if field not in data:
            fail(f"{relative}: missing required field '{field}' ({vendor} schema)")

    if vendor == "codex":
        validate_codex_extras(data, relative)

    version = data.get("version")
    if isinstance(version, str) and not SEMVER.fullmatch(version):
        fail(f"{relative}: version '{version}' is not valid semver")

    # The runtime stamps PLUGIN_VERSION into every uploaded payload. If it drifts
    # from the manifest, a release reports the wrong build and the collected
    # version column silently under-reports.
    stamped = stamped_plugin_version(plugin_dir)
    if stamped is not None and isinstance(version, str) and stamped != version:
        fail(
            f"plugins/{plugin_dir.name}: version mismatch — "
            f"{relative} says '{version}' but PLUGIN_VERSION says '{stamped}'"
        )
    declared = data.get("name")
    return plugin_dir.name, declared if isinstance(declared, str) else None


def resolve_source(entry: dict[str, Any], label: str) -> str | None:
    """Resolve a marketplace entry's source to a plugin directory name.

    Only repository-local sources are resolvable. The vendors also document
    remote forms (github, url, git-subdir, npm, archive, command) whose paths
    refer to somewhere other than this checkout, so those are accepted without
    a directory check rather than reported as broken.

    Args:
        entry: One decoded marketplace entry.
        label: Entry identifier, used in failure messages.

    Returns:
        The plugin directory name, or None when the source is remote,
        unreadable, or does not point at a directory under ``plugins/``.
    """
    match entry.get("source"):
        case str() as source_path:
            pass
        case {"source": "local", "path": str() as source_path}:
            pass
        case {"source": str()}:
            return None
        case _:
            fail(f"{label} has an unreadable 'source'")
            return None

    resolved = (REPO_ROOT / source_path).resolve()
    if not resolved.is_dir():
        fail(f"{label} source '{source_path}' is not a directory")
        return None
    if resolved.parent != PLUGINS_DIR:
        fail(f"{label} source '{source_path}' resolves outside plugins/")
        return None
    return resolved.name


def validate_marketplace(
    relative_path: str, declared_names: dict[str, str | None]
) -> set[str]:
    """Validate one marketplace file and report which plugin dirs it lists.

    Args:
        relative_path: Marketplace file path relative to the repository root.
        declared_names: Plugin directory name -> the name its manifest declares,
            used to catch a listing whose name has drifted from the manifest.

    Returns:
        The set of plugin directory names the file points at. Empty when the
        file could not be parsed.
    """
    path = REPO_ROOT / relative_path
    data = load_json(path)
    if data is None:
        return set()

    top_level_fields, entry_fields = MARKETPLACE_FILES[relative_path]
    for field in top_level_fields:
        if field not in data:
            fail(f"{relative_path}: missing required field '{field}'")

    entries = data.get("plugins")
    if not isinstance(entries, list) or not entries:
        fail(f"{relative_path}: 'plugins' must be a non-empty list")
        return set()

    listed: set[str] = set()
    for position, entry in enumerate(entries):
        label = f"{relative_path}: plugins[{position}]"
        if not isinstance(entry, dict):
            fail(f"{label} must be an object")
            continue
        for field in entry_fields:
            if field not in entry:
                fail(f"{label} missing '{field}'")

        directory = resolve_source(entry, label)
        if directory is None:
            continue
        listed.add(directory)

        # An install resolves the plugin by the name in the listing, so a
        # listing that has drifted from its manifest installs nothing.
        declared = declared_names.get(directory)
        listed_name = entry.get("name")
        if declared is not None and isinstance(listed_name, str):
            if listed_name != declared:
                fail(
                    f"{label} name '{listed_name}' does not match "
                    f"plugins/{directory} manifest name '{declared}'"
                )
    return listed


def main() -> int:
    """Validate every plugin manifest and every marketplace file.

    Returns:
        0 when everything passed, 1 when any check failed.
    """
    if not PLUGINS_DIR.is_dir():
        print(f"no plugins directory at {PLUGINS_DIR}", file=sys.stderr)
        return 1

    declared_names: dict[str, str | None] = dict(
        validate_plugin(plugin_dir)
        for plugin_dir in sorted(PLUGINS_DIR.iterdir())
        if plugin_dir.is_dir()
    )
    listed_anywhere: set[str] = set()
    for relative_path in MARKETPLACE_FILES:
        listed_anywhere |= validate_marketplace(relative_path, declared_names)

    # A plugin absent from every marketplace ships to nobody. Being listed in
    # only its own vendor's file is correct and expected.
    for orphan in sorted(set(declared_names) - listed_anywhere):
        fail(f"plugins/{orphan}: not listed in any marketplace file")

    checked = len(declared_names)
    if failures:
        print(f"FAILED — {len(failures)} problem(s) across {checked} plugins:")
        for problem in failures:
            print(f"  - {problem}")
        return 1
    print(
        f"OK — {checked} plugin manifests and {len(MARKETPLACE_FILES)} marketplaces valid"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
