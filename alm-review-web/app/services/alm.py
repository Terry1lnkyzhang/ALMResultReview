from __future__ import annotations

import base64
import dataclasses
import hashlib
import logging
import mimetypes
import xml.etree.ElementTree as ElementTree
from collections import deque
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.models import SyncConfig

IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_STEP = 4
MAX_IMAGE_BYTES_PER_STEP = 10 * 1024 * 1024
MAX_IMAGES_PER_RUN = 12
MAX_IMAGE_BYTES_PER_RUN = 15 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FolderCollectionProgress:
    stage: str
    message: str
    folders_discovered: int = 0
    folders_processed: int = 0
    test_sets_discovered: int = 0
    runs_discovered: int = 0
    runs_skipped: int = 0


@dataclass(frozen=True)
class FolderBatch:
    folder_id: str
    folder_path: str
    records: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    users: list[dict[str, Any]] = dataclasses.field(default_factory=list)


ProgressCallback = Callable[[FolderCollectionProgress, bool], None]
UnchangedRunCheck = Callable[[dict[str, Any]], bool]


class AlmClient:
    def __init__(self, config: SyncConfig) -> None:
        self.config = config
        self.base_url = config.server_url.rstrip("/")
        self.client = httpx.Client(
            timeout=60,
            follow_redirects=True,
            trust_env=False,
        )

    def __enter__(self) -> AlmClient:
        settings = get_settings()
        if not settings.alm_username or not settings.alm_password:
            raise ValueError("ALM_USERNAME and ALM_PASSWORD must be set in .env.")
        auth_response = self.client.post(
            f"{self.base_url}/qcbin/authentication-point/authenticate",
            auth=(settings.alm_username, settings.alm_password),
        )
        auth_response.raise_for_status()
        session_response = self.client.post(f"{self.base_url}/qcbin/rest/site-session")
        session_response.raise_for_status()
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.client.delete(f"{self.base_url}/qcbin/rest/site-session")
        finally:
            self.client.close()

    def _project_url(self, resource: str) -> str:
        return (
            f"{self.base_url}/qcbin/rest/domains/{self.config.domain}"
            f"/projects/{self.config.project}/{resource.lstrip('/')}"
        )

    @staticmethod
    def _parse_entities(content: bytes) -> tuple[list[dict[str, Any]], int]:
        root = ElementTree.fromstring(content)
        entity_elements = [root] if root.tag == "Entity" else root.findall("Entity")
        total = int(root.attrib.get("TotalResults", len(entity_elements)))
        entities = []
        for entity in entity_elements:
            fields: dict[str, Any] = {}
            for field in entity.findall("./Fields/Field"):
                values = [value.text or "" for value in field.findall("Value")]
                fields[field.attrib["Name"]] = (
                    None if not values else values[0] if len(values) == 1 else values
                )
            entities.append(fields)
        return entities, total

    def entities(
        self,
        resource: str,
        query: str | None = None,
        fields: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        start_index = 1
        all_entities: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "page-size": page_size,
                "start-index": start_index,
            }
            if query:
                params["query"] = query
            if fields:
                params["fields"] = fields
            response = self.client.get(
                self._project_url(resource),
                params=params,
                headers={"Accept": "application/xml"},
            )
            response.raise_for_status()
            page, total = self._parse_entities(response.content)
            all_entities.extend(page)
            if not page or len(all_entities) >= total:
                return all_entities
            start_index += len(page)

    def entity(self, resource: str, entity_id: int | str) -> dict[str, Any]:
        response = self.client.get(
            self._project_url(f"{resource}/{entity_id}"),
            headers={"Accept": "application/xml"},
        )
        response.raise_for_status()
        entities, _ = self._parse_entities(response.content)
        if not entities:
            raise ValueError(f"ALM {resource} entity {entity_id} was not found.")
        return entities[0]

    def image_attachments(
        self,
        run_step_id: int | str,
        *,
        max_images: int = MAX_IMAGES_PER_STEP,
        max_total_bytes: int = MAX_IMAGE_BYTES_PER_STEP,
    ) -> list[dict[str, Any]]:
        entities = self.entities(f"run-steps/{run_step_id}/attachments")
        attachments: list[dict[str, Any]] = []
        total_bytes = 0
        for entity in entities:
            name = str(entity.get("name") or "attachment")
            attachment_id = str(entity.get("id") or "").strip()
            mime_type = (
                mimetypes.guess_type(name)[0] or "application/octet-stream"
            ).casefold()
            try:
                declared_size = int(entity.get("file-size") or 0)
            except (TypeError, ValueError):
                declared_size = 0
            if (
                not attachment_id
                or mime_type not in IMAGE_MIME_TYPES
                or declared_size > MAX_IMAGE_BYTES
                or (declared_size and total_bytes + declared_size > max_total_bytes)
            ):
                continue
            download = self.client.get(
                self._project_url(f"attachments/{attachment_id}"),
                headers={"Accept": "application/octet-stream"},
            )
            download.raise_for_status()
            if (
                len(download.content) > MAX_IMAGE_BYTES
                or total_bytes + len(download.content) > max_total_bytes
            ):
                continue
            encoded = base64.b64encode(download.content).decode("ascii")
            attachments.append(
                {
                    "attachment_id": attachment_id,
                    "name": name,
                    "mime_type": mime_type,
                    "size_bytes": len(download.content),
                    "sha256": hashlib.sha256(download.content).hexdigest(),
                    "data_url": f"data:{mime_type};base64,{encoded}",
                }
            )
            total_bytes += len(download.content)
            if len(attachments) >= max_images:
                break
        return attachments

    def field_name_by_label(self, entity: str, label: str) -> str | None:
        response = self.client.get(
            self._project_url(f"customization/entities/{entity}/fields"),
            headers={"Accept": "application/xml"},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        expected_label = label.strip().casefold()
        for field in root.iter():
            if field.tag.rsplit("}", 1)[-1].casefold() != "field":
                continue
            field_label = str(
                field.attrib.get("Label") or field.attrib.get("label") or ""
            ).strip()
            if field_label.casefold() != expected_label:
                continue
            return str(
                field.attrib.get("Name") or field.attrib.get("name") or ""
            ).strip() or None
        return None

    def users(self) -> list[dict[str, Any]]:
        response = self.client.get(
            self._project_url("customization/users"),
            headers={"Accept": "application/xml"},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        users = []
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "User":
                continue
            code1_id = str(element.attrib.get("Name") or "").strip()
            if not code1_id:
                continue
            children = {
                child.tag.rsplit("}", 1)[-1]: child.text or ""
                for child in element
            }
            users.append(
                {
                    "code1_id": code1_id,
                    "full_name": str(element.attrib.get("FullName") or "").strip(),
                    "email": str(children.get("email") or "").strip(),
                    "active": str(children.get("UserActive") or "Y").casefold()
                    not in {"n", "no", "false", "0"},
                }
            )
        return users


def _query(field: str, value: int | str) -> str:
    return "{" + f"{field}[{value}]" + "}"


def _latest_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    passed_runs = [
        run
        for run in runs
        if str(run.get("status") or "").strip().casefold() == "passed"
    ]
    if not passed_runs:
        return None
    return max(
        passed_runs,
        key=lambda run: (
            str(run.get("execution-date") or ""),
            str(run.get("execution-time") or ""),
            int(run.get("id") or 0),
        ),
    )


def _run_location(run: dict[str, Any], location_field: str | None) -> str:
    value = run.get("location")
    if not value and location_field:
        value = run.get(location_field)
    return str(value or "").strip()


def _run_record(
    alm: AlmClient,
    folder: dict[str, Any],
    test_set: dict[str, Any],
    instance: dict[str, Any],
    run: dict[str, Any],
    location_field: str | None,
    test_cache: dict[str, dict[str, Any]],
    include_image_attachments: bool = False,
) -> dict[str, Any]:
    """Assemble the importable record for one run; the shape feeds the source hash."""
    location = _run_location(run, location_field)
    if location:
        run["location"] = location
    run["steps"] = alm.entities(f"runs/{run['id']}/run-steps")
    remaining_images = MAX_IMAGES_PER_RUN
    remaining_bytes = MAX_IMAGE_BYTES_PER_RUN
    for step in run["steps"]:
        if step.get("attachment") and include_image_attachments and remaining_images:
            attachments = alm.image_attachments(
                step["id"],
                max_images=min(MAX_IMAGES_PER_STEP, remaining_images),
                max_total_bytes=min(MAX_IMAGE_BYTES_PER_STEP, remaining_bytes),
            )
            step["attachmentContents"] = attachments
            remaining_images -= len(attachments)
            remaining_bytes -= sum(item["size_bytes"] for item in attachments)
    test_id = str(run.get("test-id") or instance.get("test-id") or "")
    if test_id and test_id not in test_cache:
        test_cache[test_id] = alm.entity("tests", test_id)
    return {
        "folder": {"id": folder["id"], "path": folder["path"]},
        "testSet": test_set,
        "testInstance": instance,
        "testOwner": (test_cache.get(test_id) or {}).get("owner"),
        "run": run,
    }


def _record_users(
    directory: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    used_code1_ids = {
        str(value).strip()
        for record in records
        for value in (
            record.get("testOwner"),
            (record.get("testInstance") or {}).get("owner"),
            (record.get("testInstance") or {}).get("actual-tester"),
            (record.get("run") or {}).get("owner"),
        )
        if value
    }
    return [user for user in directory if user["code1_id"] in used_code1_ids]


def _location_field(alm: AlmClient) -> str | None:
    try:
        return alm.field_name_by_label("run", "Location")
    except (httpx.HTTPError, ElementTree.ParseError):
        logger.exception("ALM Run Location field lookup failed")
        return None


def collect_run(
    config: SyncConfig,
    test_instance_id: int,
    folder_id: str,
    folder_path: str,
    include_image_attachments: bool = False,
) -> dict[str, Any]:
    """Fetch one Run so a single record can be refreshed without walking folders."""
    with AlmClient(config) as alm:
        location_field = _location_field(alm)
        instance = alm.entity("test-instances", test_instance_id)
        test_set = alm.entity("test-sets", instance["cycle-id"])
        test_set["folderPath"] = folder_path
        runs = alm.entities("runs", query=_query("testcycl-id", instance["id"]))
        run = _latest_run(runs)
        if run is None:
            return {"users": [], "records": []}
        record = _run_record(
            alm,
            {"id": folder_id, "path": folder_path},
            test_set,
            instance,
            run,
            location_field,
            {},
            include_image_attachments,
        )
        try:
            directory = alm.users()
        except (httpx.HTTPError, ElementTree.ParseError):
            logger.exception("ALM user directory synchronization failed")
            directory = []
    return {"users": _record_users(directory, [record]), "records": [record]}


def iter_folder_batches(
    config: SyncConfig,
    progress_callback: ProgressCallback | None = None,
    completed_folder_ids: Collection[str] = (),
    is_unchanged_run: UnchangedRunCheck | None = None,
    include_image_attachments: bool = False,
) -> Iterator[FolderBatch]:
    """Walk the Test Lab scope and yield one importable batch per folder."""
    already_done = {str(folder_id) for folder_id in completed_folder_ids}
    folders: list[dict[str, Any]] = []
    test_sets: list[dict[str, Any]] = []
    test_cache: dict[str, dict[str, Any]] = {}
    directory: list[dict[str, Any]] | None = None
    pending: deque[dict[str, Any]] = deque()
    folders_processed = 0
    runs_collected = 0
    runs_skipped = 0

    def report(stage: str, message: str, force: bool = False) -> None:
        if progress_callback is None:
            return
        progress_callback(
            FolderCollectionProgress(
                stage=stage,
                message=message,
                folders_discovered=len(folders) + len(pending),
                folders_processed=folders_processed,
                test_sets_discovered=len(test_sets),
                runs_discovered=runs_collected,
                runs_skipped=runs_skipped,
            ),
            force,
        )

    report("connecting", "Connecting to ALM", True)
    with AlmClient(config) as alm:
        location_field = _location_field(alm)

        def batch_users(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal directory
            if not records:
                return []
            if directory is None:
                try:
                    directory = alm.users()
                except (httpx.HTTPError, ElementTree.ParseError):
                    logger.exception("ALM user directory synchronization failed")
                    directory = []
            return _record_users(directory, records)

        root = alm.entity("test-set-folders", config.folder_id)
        root["path"] = config.folder_path or str(root.get("name") or config.folder_id)
        pending.append(root)
        report("collecting", str(root["path"]), True)
        while pending:
            folder = pending.popleft()
            folders.append(folder)
            report("collecting", str(folder["path"]), True)
            children = alm.entities(
                "test-set-folders", query=_query("parent-id", folder["id"])
            )
            for child in children:
                child["path"] = f"{folder['path']} / {child.get('name', child.get('id', ''))}"
                pending.append(child)
            report("collecting", str(folder["path"]))

            if str(folder["id"]) in already_done:
                folders_processed += 1
                report("collecting", str(folder["path"]), True)
                continue

            records: list[dict[str, Any]] = []
            folder_test_sets = alm.entities(
                "test-sets", query=_query("parent-id", folder["id"])
            )
            for test_set in folder_test_sets:
                test_set["folderPath"] = folder["path"]
                test_sets.append(test_set)
                report("collecting", str(folder["path"]))
                instances = alm.entities(
                    "test-instances", query=_query("cycle-id", test_set["id"])
                )
                for instance in instances:
                    if str(instance.get("status") or "").strip().casefold() == "no run":
                        continue
                    runs = alm.entities(
                        "runs", query=_query("testcycl-id", instance["id"])
                    )
                    run = _latest_run(runs)
                    if run is None:
                        continue
                    if is_unchanged_run is not None and is_unchanged_run(run):
                        runs_skipped += 1
                        report("collecting", str(folder["path"]))
                        continue
                    records.append(
                        _run_record(
                            alm,
                            folder,
                            test_set,
                            instance,
                            run,
                            location_field,
                            test_cache,
                            include_image_attachments,
                        )
                    )
                    runs_collected += 1
                    report("collecting", str(folder["path"]))

            folders_processed += 1
            report("collecting", str(folder["path"]), True)
            yield FolderBatch(
                folder_id=str(folder["id"]),
                folder_path=str(folder["path"]),
                records=records,
                users=batch_users(records),
            )

    report("collected", "ALM collection complete", True)


def collect_folder(
    config: SyncConfig,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    users: list[dict[str, Any]] = []
    seen_code1_ids: set[str] = set()
    for batch in iter_folder_batches(config, progress_callback=progress_callback):
        records.extend(batch.records)
        for user in batch.users:
            if user["code1_id"] in seen_code1_ids:
                continue
            seen_code1_ids.add(user["code1_id"])
            users.append(user)
    return {"users": users, "records": records}