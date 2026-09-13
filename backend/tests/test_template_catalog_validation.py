"""Catalog certification: prove EVERY bundled service template conforms to the
documented schema (``docs/TEMPLATE_CATALOG_SCHEMA.md``) and that the committed
``index.json`` stays in lock-step with what is actually on disk.

Unlike ``test_service_templates.py`` (which parametrizes per file and checks a
few roadmap-specific conventions), this module loads the WHOLE catalog and
asserts the universal contract in aggregate, **collecting every failure and
reporting them together** instead of stopping at the first one. The assertions
mirror the real implementation in ``TemplateService`` rather than the prose:

  * parses + passes ``validate_template`` and ``validate_catalog_entry``
  * a unique, slug-shaped ``id`` that matches the filename stem
  * a ``name``
  * at least one of ``compose`` / ``dockerfile`` / ``ports``
  * every ``${VAR}`` referenced in compose / files / scripts is either declared
    in ``variables``, a recognized magic variable (``${SERVICE_*}``), or a
    built-in injected by the installer (``APP_NAME``)
  * the committed ``index.json`` lists exactly the template ids on disk, with a
    well-formed entry per template

These checks are pure: no Docker, no network, no DB. They certify only — they
do not change how any template installs.
"""
import json
import os
import re

import pytest
import yaml

from app.services.template_service import TemplateService

# By default this certifies the bundled catalog. The official registry
# (jhd3197/serverkit-templates) points these at its own tree from CI, so a
# template is held to the installer's rules before merging publishes it.
TEMPLATES_DIR = os.environ.get("SERVERKIT_TEMPLATES_DIR") or TemplateService.LOCAL_TEMPLATES_DIR
INDEX_PATH = os.environ.get("SERVERKIT_TEMPLATES_INDEX") or os.path.join(TEMPLATES_DIR, "index.json")

# Built-in variables the installer always injects into the substitution map
# (see ``_prepare_install_variables`` / ``install_template`` — both seed
# ``{'APP_NAME': app_name}`` before resolving declared/magic variables).
BUILTIN_VARS = {"APP_NAME"}

# Magic-variable prefixes recognized by ``TemplateService`` (single source of
# truth: the service's own table, so this test tracks the code if it grows).
MAGIC_PREFIXES = tuple(prefix for prefix, _ in TemplateService.MAGIC_PREFIXES)

# The exact ``${VAR}`` substitution pattern the installer uses
# (``TemplateService.substitute_variables``).
VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _template_files():
    return sorted(
        f for f in os.listdir(TEMPLATES_DIR) if f.endswith((".yaml", ".yml"))
    )


def _load_raw(filename):
    with open(os.path.join(TEMPLATES_DIR, filename), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _declared_var_names(template):
    """Names declared under ``variables`` (list OR dict form, both supported)."""
    names = set()
    raw = template.get("variables", [])
    if isinstance(raw, list):
        for var in raw:
            if isinstance(var, dict) and "name" in var:
                names.add(var["name"])
    elif isinstance(raw, dict):
        names.update(raw.keys())
    return names


def _is_magic(name):
    return any(name.startswith(prefix) for prefix in MAGIC_PREFIXES)


def _scan_var_refs(node, out):
    """Collect every ``${VAR}`` token under a (possibly nested) compose/files/
    scripts node, mirroring how the installer walks the structure."""
    if isinstance(node, str):
        out.update(VAR_PATTERN.findall(node))
    elif isinstance(node, dict):
        for value in node.values():
            _scan_var_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _scan_var_refs(item, out)


# ---------------------------------------------------------------------------
# Catalog-wide certification (one test, all failures collected)
# ---------------------------------------------------------------------------

def test_every_template_conforms_to_schema():
    """Load every bundled template and assert the universal contract, gathering
    all violations into one report so a single run shows the full picture."""
    failures = []
    seen_ids = {}

    files = _template_files()
    assert files, "no templates found on disk"

    for filename in files:
        stem = filename.rsplit(".", 1)[0]

        # --- parse + the loader's own validator ---------------------------
        result = TemplateService.parse_template(os.path.join(TEMPLATES_DIR, filename))
        if not result.get("success"):
            failures.append(
                f"{filename}: parse/validate_template failed: "
                f"{result.get('errors') or result.get('error')}"
            )
            continue
        template = result["template"]

        # --- catalog-level validator (id slug, var types, magic tokens) ---
        catalog = TemplateService.validate_catalog_entry(template)
        if not catalog.get("valid"):
            failures.append(
                f"{filename}: validate_catalog_entry failed: {catalog.get('errors')}"
            )

        # --- id: present, unique, slug, matches filename stem -------------
        tid = template.get("id")
        if not tid:
            failures.append(f"{filename}: missing 'id'")
        else:
            if tid != stem:
                failures.append(f"{filename}: id '{tid}' != filename stem '{stem}'")
            if tid in seen_ids:
                failures.append(
                    f"{filename}: duplicate id '{tid}' (also in {seen_ids[tid]})"
                )
            else:
                seen_ids[tid] = filename

        # --- name ---------------------------------------------------------
        if not template.get("name"):
            failures.append(f"{filename}: missing 'name'")

        # --- deployable shape depends on kind -----------------------------
        # compose templates (the default) must define at least one of
        # compose/dockerfile/ports; repo templates (kind: repo) instead carry a
        # `repo` block with a `url` and are deployed through the wizard.
        if template.get("kind") == "repo":
            repo = template.get("repo")
            if not isinstance(repo, dict) or not repo.get("url"):
                failures.append(f"{filename}: repo template missing 'repo.url'")
        elif not any(k in template for k in ("compose", "dockerfile", "ports")):
            failures.append(
                f"{filename}: must define at least one of compose/dockerfile/ports"
            )

        # --- every ${VAR} referenced resolves -----------------------------
        refs = set()
        _scan_var_refs(template.get("compose", {}), refs)
        _scan_var_refs(template.get("files", []), refs)
        _scan_var_refs(template.get("scripts", {}), refs)

        declared = _declared_var_names(template)
        for ref in sorted(refs):
            if ref in BUILTIN_VARS or ref in declared or _is_magic(ref):
                continue
            failures.append(
                f"{filename}: references undeclared variable '${{{ref}}}' "
                f"(not declared, not a magic var, not a built-in)"
            )

    assert not failures, "Catalog schema violations:\n" + "\n".join(failures)


def test_index_json_matches_templates_on_disk():
    """The committed ``index.json`` must list exactly the template ids that exist
    on disk (and vice-versa), each as a well-formed entry. This is the curated,
    hand-shaped repo index (id/name/version/description/categories) — distinct
    from ``build_repo_index()`` — so we assert the committed file directly."""
    assert os.path.exists(INDEX_PATH), "index.json is missing"
    with open(INDEX_PATH, encoding="utf-8") as fh:
        index = json.load(fh)

    disk_ids = {f.rsplit(".", 1)[0] for f in _template_files()}
    index_entries = index.get("templates", [])
    index_ids = {e.get("id") for e in index_entries}

    failures = []

    missing_from_index = sorted(disk_ids - index_ids)
    if missing_from_index:
        failures.append(f"on disk but NOT in index.json: {missing_from_index}")

    stale_in_index = sorted(index_ids - disk_ids)
    if stale_in_index:
        failures.append(f"in index.json but NOT on disk: {stale_in_index}")

    # No duplicate ids inside the index.
    seen = set()
    for entry in index_entries:
        eid = entry.get("id")
        if eid in seen:
            failures.append(f"duplicate id in index.json: '{eid}'")
        seen.add(eid)

    # Each entry is well-formed (the curated lean shape).
    for entry in index_entries:
        for field in ("id", "name", "version", "description"):
            if not entry.get(field):
                failures.append(f"index entry '{entry.get('id')}' missing '{field}'")
        if not isinstance(entry.get("categories", []), list):
            failures.append(
                f"index entry '{entry.get('id')}' has non-list 'categories'"
            )

    assert not failures, "index.json out of sync:\n" + "\n".join(failures)


def test_index_entry_fields_match_template_source():
    """Each index entry's name/version/description should reflect the actual
    template YAML (so the curated index can't drift from the source files)."""
    with open(INDEX_PATH, encoding="utf-8") as fh:
        index = json.load(fh)
    by_id = {e["id"]: e for e in index["templates"] if e.get("id")}

    failures = []
    for filename in _template_files():
        template = _load_raw(filename)
        tid = template.get("id")
        entry = by_id.get(tid)
        if entry is None:
            continue  # absence is already reported by the sync test
        if entry.get("name") != template.get("name"):
            failures.append(
                f"{tid}: index name {entry.get('name')!r} != "
                f"template {template.get('name')!r}"
            )
        if str(entry.get("version")) != str(template.get("version")):
            failures.append(
                f"{tid}: index version {entry.get('version')!r} != "
                f"template {template.get('version')!r}"
            )

    assert not failures, "index.json fields drifted from templates:\n" + "\n".join(
        failures
    )


def test_index_carries_the_engine_block_for_engine_templates():
    """A database engine is a template with an ``engine:`` block, and the index
    is what a panel syncing this bundle as a repo reads.

    If the block is missing from the index, a synced CockroachDB arrives as an
    ordinary app template and never appears in the Databases engine catalog --
    silently, because everything still installs. This has drifted once already:
    the index was regenerated by a path that dropped the block.
    """
    with open(INDEX_PATH, encoding="utf-8") as fh:
        index = json.load(fh)
    by_id = {e["id"]: e for e in index["templates"] if e.get("id")}

    failures = []
    for filename in _template_files():
        template = _load_raw(filename)
        tid = template.get("id")
        source_engine = template.get("engine")
        if not isinstance(source_engine, dict):
            continue
        entry = by_id.get(tid)
        if entry is None:
            continue  # absence is already reported by the sync test
        if not isinstance(entry.get("engine"), dict):
            failures.append(f"{tid}: declares engine: but index.json omits it")
            continue
        # The index carries the NORMALIZED block (the loader fills in defaults
        # like bind_var), so compare against what the service computes rather
        # than the raw YAML.
        expected = TemplateService.engine_metadata(template)
        if entry["engine"] != expected:
            differing = sorted(
                k for k in set(expected) | set(entry["engine"])
                if expected.get(k) != entry["engine"].get(k)
            )
            failures.append(f"{tid}: index engine block differs on {differing}")

    assert not failures, (
        "index.json engine blocks drifted -- regenerate with "
        "TemplateService.export_repo_index():\n" + "\n".join(failures)
    )


@pytest.mark.skipif(bool(os.environ.get("SERVERKIT_TEMPLATES_DIR")),
                    reason="build_repo_index() reads the bundled catalog, not an external registry tree")
def test_build_repo_index_covers_disk():
    """``build_repo_index()`` (the publishable generated index consumed by the
    repo-sync flow) must enumerate every template currently on disk."""
    generated = TemplateService.build_repo_index()
    disk_ids = {f.rsplit(".", 1)[0] for f in _template_files()}
    generated_ids = {t["id"] for t in generated["templates"]}

    assert generated["count"] == len(_template_files())
    assert generated_ids == disk_ids, (
        "build_repo_index() drift -- "
        f"missing: {sorted(disk_ids - generated_ids)}, "
        f"extra: {sorted(generated_ids - disk_ids)}"
    )
