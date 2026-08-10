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