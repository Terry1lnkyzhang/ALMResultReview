from types import SimpleNamespace

import httpx

from app.services.alm import AlmClient


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