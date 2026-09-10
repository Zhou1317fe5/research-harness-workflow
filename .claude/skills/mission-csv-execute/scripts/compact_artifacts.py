#!/usr/bin/env python3
"""Compact a Mission artifact directory without losing reconstructable evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from csv_state import file_lock


STATE_SCHEMA = "mission.csv-state-update.v1"
REQUEST_SCHEMA = "mission.rrctl-request.v1"
RUNSPEC_SCHEMA = "rrctl.run.v1"
CORE_SUFFIXES = (
    ".claims.json",
    ".outcomes.json",
    ".deferred.json",
    ".events.json",
    ".review.json",
    ".review.md",
    ".handoff.md",
    ".handoff.draft.md",
)


class CompactError(ValueError):
    """The artifact root is ambiguous or unsafe to compact."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _select_csv(value: Path) -> Path:
    if value.is_file():
        if value.suffix != ".csv":
            raise CompactError(f"input_not_csv: {value}")
        return value.resolve()
    if not value.is_dir():
        raise CompactError(f"input_missing: {value}")
    same_name = value / f"{value.name}.csv"
    if same_name.is_file():
        return same_name.resolve()
    candidates = sorted(value.glob("*.csv"))
    if len(candidates) != 1:
        raise CompactError(
            f"csv_selection_ambiguous: {value} has {len(candidates)} candidates"
        )
    return candidates[0].resolve()


def _load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    required = {
        "id",
        "dev_state",
        "review_initial_state",
        "review_regression_state",
        "git_state",
        "remote_state",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise CompactError("csv_schema_missing_state_fields")
    return rows


def _closing_ready(rows: list[dict[str, str]]) -> bool:
    terminal_remote = {"", "not_applicable", "completed", "artifacts_pulled", "ingested"}
    return bool(rows) and all(
        row["dev_state"] == "已完成"
        and row["review_initial_state"] == "已完成"
        and row["review_regression_state"] == "已完成"
        and row["remote_state"] in terminal_remote
        for row in rows
    )


def _load_event_records(csv_path: Path) -> dict[str, dict[str, Any]]:
    path = csv_path.with_suffix(".events.json")
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise CompactError(f"event_sidecar_invalid: {path}")
    return {
        item["event_sha256"]: item
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("event_sha256"), str)
    }


def _verified_state_inputs(
    root: Path, events: dict[str, dict[str, Any]]
) -> tuple[list[tuple[Path, str]], list[Path]]:
    verified: list[tuple[Path, str]] = []
    unverified: list[Path] = []
    for path in sorted(root.glob("state-*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            record = {
                "schema_version": STATE_SCHEMA,
                "row_id": request["row_id"],
                "event": request["event"],
            }
            if request.get("schema_version") != STATE_SCHEMA:
                raise KeyError("schema_version")
            digest = hashlib.sha256(_canonical_bytes(record)).hexdigest()
            stored = events.get(digest)
            if (
                stored is None
                or stored.get("row_id") != record["row_id"]
                or stored.get("event") != record["event"]
            ):
                raise KeyError("event")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            unverified.append(path)
        else:
            verified.append((path, digest))
    return verified, unverified


def _schema(payload: dict[str, Any]) -> str:
    value = payload.get("schema_version", payload.get("schema", ""))
    return value if isinstance(value, str) else ""


def _run_id(payload: dict[str, Any]) -> str:
    value = payload.get("run_id")
    if not isinstance(value, str):
        metadata = payload.get("metadata", {})
        value = metadata.get("run_id") if isinstance(metadata, dict) else ""
    if not isinstance(value, str) or not value:
        raise CompactError("run_contract_missing_run_id")
    if any(part in {"", ".", ".."} for part in Path(value).parts) or "/" in value:
        raise CompactError(f"run_contract_invalid_run_id: {value}")
    return value


def _load_contracts(root: Path) -> list[tuple[Path, dict[str, Any], str]]:
    result: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        schema = _schema(payload)
        if schema in {REQUEST_SCHEMA, RUNSPEC_SCHEMA}:
            result.append((path, payload, schema))
    return result


def _move_verified_tree(source: Path, destination: Path) -> dict[str, str]:
    source_manifest = _tree_manifest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_dir() or _tree_manifest(destination) != source_manifest:
            raise CompactError(f"artifact_destination_conflict: {destination}")
    else:
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        try:
            shutil.rmtree(temporary)
            shutil.copytree(source, temporary)
            if _tree_manifest(temporary) != source_manifest:
                raise CompactError(f"artifact_copy_verification_failed: {source}")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    shutil.rmtree(source)
    relative_target = os.path.relpath(destination, source.parent)
    source.symlink_to(relative_target, target_is_directory=True)
    return source_manifest


def _rewrite_references(root: Path, replacements: dict[str, str]) -> list[str]:
    changed: list[str] = []
    skipped_roots = {"prerun", "runs", "validation", "remote_artifacts"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in skipped_roots:
            continue
        if not path.is_file() or path.name in {"artifact-index.json"}:
            continue
        if path.name.endswith(".events.json") or path.name.startswith("state-"):
            continue
        if path.suffix.lower() not in {".csv", ".json", ".md"}:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        updated = original
        protected: dict[str, str] = {}
        for index, new in enumerate(dict.fromkeys(replacements.values())):
            token = f"__MISSION_COMPACTOR_REF_{index}__"
            if new in updated:
                updated = updated.replace(new, token)
                protected[token] = new
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        for token, new in protected.items():
            updated = updated.replace(token, new)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(relative.as_posix())
    return changed


def compact(csv_path: Path, research_root: Path, *, apply: bool, require_full: bool) -> dict[str, Any]:
    csv_path = csv_path.expanduser().resolve()
    with file_lock(csv_path.with_name("." + csv_path.name + ".lock")):
        return _compact_locked(csv_path, research_root, apply=apply, require_full=require_full)


def _compact_locked(csv_path: Path, research_root: Path, *, apply: bool, require_full: bool) -> dict[str, Any]:
    root = csv_path.parent
    rows = _load_rows(csv_path)
    ready = _closing_ready(rows)
    if require_full and not ready:
        raise CompactError("mission_not_closing_ready")
    events = _load_event_records(csv_path)
    verified, unverified = _verified_state_inputs(root, events)
    contracts = _load_contracts(root) if ready else []
    result: dict[str, Any] = {
        "schema_version": "mission.artifact-compaction.v1",
        "mode": "full" if ready else "safe-active",
        "dry_run": not apply,
        "csv": str(csv_path),
        "verified_state_inputs": [path.name for path, _ in verified],
        "unverified_state_inputs": [path.name for path in unverified],
        "contracts": [path.name for path, _, _ in contracts],
        "moves": [],
    }
    if not apply:
        return result

    state_index_path = root / ".state-input-index.json"
    state_event_refs: dict[str, str] = {}
    if state_index_path.is_file():
        state_index = json.loads(state_index_path.read_text(encoding="utf-8"))
        stored_refs = state_index.get("state_event_refs", {})
        if not isinstance(stored_refs, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in stored_refs.items()
        ):
            raise CompactError(f"state_input_index_invalid: {state_index_path}")
        state_event_refs.update(stored_refs)
    state_event_refs.update(
        {
            path.name: f"{csv_path.with_suffix('.events.json').name}#{digest}"
            for path, digest in verified
        }
    )

    pending_prerun = any(path.is_file() for path in root.glob("prerun-*"))
    source_remote_root = root / "remote_artifacts"
    pending_remote = source_remote_root.is_dir() and any(
        path.is_dir() and not path.is_symlink()
        for path in source_remote_root.iterdir()
    )
    index_path = root / "artifact-index.json"
    if ready and index_path.is_file() and not contracts and not pending_prerun and not pending_remote:
        for path, _ in verified:
            path.unlink()
        if state_event_refs:
            existing_index = json.loads(index_path.read_text(encoding="utf-8"))
            existing_refs = existing_index.get("state_event_refs", {})
            if not isinstance(existing_refs, dict):
                raise CompactError(f"artifact_index_state_refs_invalid: {index_path}")
            merged_refs = {**existing_refs, **state_event_refs}
            if merged_refs != existing_refs:
                existing_index["state_event_refs"] = merged_refs
                existing_index.setdefault("rewritten_reference_files", []).extend(
                    path
                    for path in _rewrite_references(root, state_event_refs)
                    if path not in existing_index["rewritten_reference_files"]
                )
                index_path.write_text(
                    json.dumps(existing_index, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
            state_index_path.unlink(missing_ok=True)
        result["already_compacted"] = True
        result["artifact_index"] = str(index_path)
        return result

    for path, _ in verified:
        path.unlink()
    if not ready:
        if state_event_refs:
            state_index_path.write_text(
                json.dumps(
                    {
                        "schema_version": "mission.state-input-index.v1",
                        "state_event_refs": state_event_refs,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return result

    moves: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    replacements.update(state_event_refs)
    archive: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[tuple[Path, dict[str, Any]]]] = {}
    for path, payload, schema in contracts:
        groups.setdefault((_run_id(payload), schema), []).append((path, payload))
    canonical_runspecs: list[dict[str, Any]] = []
    for (run_id, schema), items in sorted(groups.items()):
        items.sort(key=lambda item: (item[0].stat().st_mtime_ns, item[0].name))
        canonical_path, canonical_payload = items[-1]
        kind = "request.json" if schema == REQUEST_SCHEMA else "runspec.json"
        destination = root / "runs" / run_id / kind
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise CompactError(f"contract_destination_collision: {destination}")
        old_hash = _sha256(canonical_path)
        canonical_path.replace(destination)
        relative = destination.relative_to(root).as_posix()
        replacements[canonical_path.name] = relative
        moves.append({"old": canonical_path.name, "new": relative, "sha256": old_hash})
        if schema == RUNSPEC_SCHEMA:
            canonical_runspecs.append(canonical_payload)
        for superseded_path, _ in items[:-1]:
            raw = superseded_path.read_text(encoding="utf-8")
            archive.append(
                {
                    "old": superseded_path.name,
                    "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "raw_text": raw,
                    "superseded_by": relative,
                }
            )
            replacements[superseded_path.name] = "runs/archive.json"
            superseded_path.unlink()

    for path in sorted(root.glob("prerun-*")):
        if not path.is_file():
            continue
        destination_root = root / ("validation" if path.suffix == ".xml" else "prerun")
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / path.name
        old_hash = _sha256(path)
        path.replace(destination)
        relative = destination.relative_to(root).as_posix()
        replacements[path.name] = relative
        moves.append({"old": path.name, "new": relative, "sha256": old_hash})

    if archive:
        archive_path = root / "runs" / "archive.json"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(
            json.dumps(
                {"schema_version": "mission.run-contract-archive.v1", "items": archive},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    if source_remote_root.is_dir() and not source_remote_root.is_symlink():
        exp_by_local_root: dict[Path, tuple[str, str]] = {}
        for runspec in canonical_runspecs:
            metadata = runspec.get("metadata", {})
            exp_id = metadata.get("exp_id") if isinstance(metadata, dict) else None
            run_id = runspec.get("run_id")
            local_pull_root = runspec.get("local_pull_root")
            if all(isinstance(value, str) and value for value in (exp_id, run_id, local_pull_root)):
                exp_by_local_root[Path(local_pull_root).resolve()] = (exp_id, run_id)
        for source in sorted(source_remote_root.iterdir()):
            if not source.is_dir() or source.is_symlink():
                continue
            identity = exp_by_local_root.get(source.resolve())
            if identity is None:
                continue
            exp_id, run_id = identity
            destination = research_root.parent / "remote_artifacts" / exp_id / run_id
            manifest = _move_verified_tree(source, destination)
            moves.append(
                {
                    "old": source.relative_to(root).as_posix(),
                    "new": str(destination),
                    "tree_sha256": hashlib.sha256(_canonical_bytes(manifest)).hexdigest(),
                    "files": len(manifest),
                }
            )

    changed_references = _rewrite_references(root, replacements)
    result["moves"] = moves
    result["archived_contracts"] = len(archive)
    result["state_event_refs"] = state_event_refs
    result["rewritten_reference_files"] = changed_references
    index_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    state_index_path.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Mission CSV or artifact directory")
    parser.add_argument("--research-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--require-full", action="store_true")
    args = parser.parse_args()
    try:
        csv_path = _select_csv(args.input)
        research_root = (
            args.research_root.resolve()
            if args.research_root is not None
            else (Path.cwd() / "research_workspace").resolve()
        )
        result = compact(
            csv_path,
            research_root,
            apply=args.apply,
            require_full=args.require_full,
        )
    except (CompactError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
