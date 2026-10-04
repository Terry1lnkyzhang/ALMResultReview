from types import SimpleNamespace

import httpx
import pytest

from app.services.alm import AlmClient, collect_run


def test_alm_users_parses_code1_identity_and_profile(monkeypatch) -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Users>
  <User FullName="Lilian Y Li" Name="310032778">
    <email>lilian@example.com</email>
    <UserActive>Y</UserActive>
  </User>
</Users>
"""
    client = AlmClient(
        SimpleNamespace(
            server_url="http://alm.example",
            domain="DEFAULT",
            project="PROJECT",
        )
    )
    monkeypatch.setattr(
        client.client,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            content=xml,
            request=httpx.Request("GET", "http://alm.example/customization/users"),
        ),
    )

    users = client.users()

    assert users == [
        {
            "code1_id": "310032778",
            "full_name": "Lilian Y Li",
            "email": "lilian@example.com",
            "active": True,
        }
    ]


def test_alm_custom_field_name_is_resolved_from_its_label(monkeypatch) -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Fields xmlns="http://www.hp.com/PC/REST">
  <Field Name="user-07" Label="Location" PhysicalName="RN_USER_07" />
  <Field Name="user-08" Label="Environment" PhysicalName="RN_USER_08" />
</Fields>
"""
    client = AlmClient(
        SimpleNamespace(
            server_url="http://alm.example",
            domain="DEFAULT",
            project="PROJECT",
        )
    )
    monkeypatch.setattr(
        client.client,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            content=xml,
            request=httpx.Request(
                "GET",
                "http://alm.example/customization/entities/run/fields",
            ),
        ),
    )

    field_name = client.field_name_by_label("run", "Location")

    assert field_name == "user-07"


def test_alm_image_attachments_use_run_step_entities_and_global_download(
    monkeypatch,
) -> None:
    attachment_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Entities TotalResults="1">
  <Entity Type="attachment">
    <Fields>
      <Field Name="id"><Value>36083</Value></Field>
      <Field Name="name"><Value>37411-Step1.JPG</Value></Field>
      <Field Name="file-size"><Value>12</Value></Field>
      <Field Name="parent-id"><Value>544363</Value></Field>
      <Field Name="parent-type"><Value>run-step</Value></Field>
    </Fields>
  </Entity>
</Entities>
"""
    image = b"\xff\xd8\xff" + b"test-image"
    requested_requests: list[tuple[str, dict | None]] = []
    client = AlmClient(
        SimpleNamespace(
            server_url="http://alm.example",
            domain="DEFAULT",
            project="PROJECT",
        )
    )

    def fake_get(url, **kwargs):
        requested_requests.append((url, kwargs.get("params")))
        content = image if url.endswith("/attachments/36083") else attachment_xml
        headers = {"content-type": "image/jpeg"} if content is image else {}
        return httpx.Response(
            200,
            content=content,
            headers=headers,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(client.client, "get", fake_get)

    attachments = client.image_attachments("544363")

    assert requested_requests == [
        (
            "http://alm.example/qcbin/rest/domains/DEFAULT/projects/PROJECT/"
            "run-steps/544363/attachments",
            {"page-size": 100, "start-index": 1},
        ),
        (
            "http://alm.example/qcbin/rest/domains/DEFAULT/projects/PROJECT/"
            "attachments/36083",
            None,
        ),
    ]
    assert attachments[0]["attachment_id"] == "36083"
    assert attachments[0]["name"] == "37411-Step1.JPG"
    assert attachments[0]["mime_type"] == "image/jpeg"
    assert attachments[0]["size_bytes"] == len(image)
    assert len(attachments[0]["sha256"]) == 64
    assert attachments[0]["data_url"].startswith("data:image/jpeg;base64,")


def test_alm_image_attachments_stop_after_ten_images(monkeypatch) -> None:
    client = AlmClient(
        SimpleNamespace(server_url="http://alm.example", domain="DEFAULT", project="PROJECT")
    )
    monkeypatch.setattr(
        client,
        "entities",
        lambda resource: [
            {"id": str(number), "name": f"Step1-{number}.jpg", "file-size": "6"}
            for number in range(1, 12)
        ],
    )
    downloaded = []

    def fake_get(url, **_kwargs):
        downloaded.append(url)
        return httpx.Response(
            200, content=b"\xff\xd8\xfftest", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(client.client, "get", fake_get)

    attachments = client.image_attachments("step-id")

    assert len(attachments) == 10
    assert len(downloaded) == 10


def test_collect_run_resolves_current_folder_from_test_set(monkeypatch) -> None:
    test_set = {"id": "20989", "name": "Dom", "parent-id": "5673"}
    folders = {
        "5673": {
            "id": "5673",
            "name": "1.4 Product-CT Tenara or CT 5300 Common",
            "parent-id": "5670",
        },
        "5670": {"id": "5670", "name": "Zhao Yize", "parent-id": "5669"},
        "5669": {"id": "5669", "name": "Testing", "parent-id": "5661"},
    }

    class StubAlmClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def entity(self, resource, entity_id):
            if resource == "test-instances":
                return {"id": "235211", "cycle-id": "20989", "test-id": "34203"}
            if resource == "test-sets":
                return dict(test_set)
            if resource == "test-set-folders":
                return folders[str(entity_id)]
            if resource == "tests":
                return {"owner": "tester"}
            raise AssertionError(resource)

        def entities(self, resource, query=None):
            if resource == "runs":
                return [{"id": "158325", "test-id": "34203", "status": "Passed"}]
            if resource == "runs/158325/run-steps":
                return []
            raise AssertionError((resource, query))

        def users(self):
            return []

    monkeypatch.setattr("app.services.alm.AlmClient", StubAlmClient)
    monkeypatch.setattr("app.services.alm._location_field", lambda _alm: None)
    config = SimpleNamespace(
        folder_id=5661,
        folder_path="System Verification Cycle 03",
    )

    records = collect_run(config, 235211)["records"]

    assert records[0]["folder"] == {
        "id": "5673",
        "path": (
            "System Verification Cycle 03 / Testing / Zhao Yize / "
            "1.4 Product-CT Tenara or CT 5300 Common"
        ),
    }
    assert records[0]["testSet"]["folderPath"] == records[0]["folder"]["path"]

    test_set["parent-id"] = "9000"
    folders["9000"] = {"id": "9000", "name": "Elsewhere", "parent-id": "9001"}
    folders["9001"] = {"id": "9001", "name": "Other root", "parent-id": ""}
    with pytest.raises(ValueError, match="outside the configured root"):
        collect_run(config, 235211)