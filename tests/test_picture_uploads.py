"""Uploading picture files (invented data only)."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from conftest import PROBE_UUID
from fake_server import FakeSyncServer

from pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    NoResponseError,
    ProtocolError,
)
from pybabytracker import client as client_module

JPEG = b"\xff\xd8\xff\xe0invented-jpeg-bytes\r\n--not-a-boundary\xff\xd9"
BOUNDARY = "__BABY_TRACKER_BOUNDARY____Photo__"


async def test_an_upload_is_framed_as_the_apps_frame_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.upload_picture("PHOTO-1.jpg", JPEG)

    [(content_type, body)] = fake_server.uploads
    assert content_type == f"multipart/form-data; boundary={BOUNDARY}"
    assert (
        body
        == (
            f"\r\n--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="picture"; filename="PHOTO-1.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + JPEG
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    [upload] = fake_server.calls("POST", "/account/picture")
    assert str(upload.user_agent).startswith("pybabytracker/")


async def test_an_uploaded_photo_and_thumbnail_download_as_sent(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    thumbnail = JPEG + b"-small"
    await client.upload_picture("PHOTO-1.jpg", JPEG)
    await client.upload_picture("PHOTO-1__thumbnail.jpg", thumbnail)

    assert await client.get_picture("PHOTO-1.jpg") == JPEG
    assert await client.get_picture("PHOTO-1__thumbnail.jpg") == thumbnail


async def test_uploading_a_name_again_replaces_the_file(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.upload_picture("PHOTO-1.jpg", JPEG)
    await client.upload_picture("PHOTO-1.jpg", JPEG + b"-again")

    assert await client.get_picture("PHOTO-1.jpg") == JPEG + b"-again"


async def test_a_bytearray_is_uploaded_too(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.upload_picture("PHOTO-1.jpg", bytearray(JPEG))
    assert fake_server.pictures["PHOTO-1.jpg"] == JPEG


async def test_an_expired_session_signs_in_again_and_sends_the_upload_again(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.upload_picture("PHOTO-1.jpg", JPEG)
    fake_server.expire_sessions()

    await client.upload_picture("PHOTO-2.jpg", JPEG)

    assert len(fake_server.calls("POST", "/session")) == 2
    # The one refused with 401, then its resend.
    assert len(fake_server.calls("POST", "/account/picture")) == 3
    assert set(fake_server.pictures) == {"PHOTO-1.jpg", "PHOTO-2.jpg"}


async def test_an_upload_the_service_keeps_rejecting_raises(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.always_unauthorized = True
    with pytest.raises(AuthenticationError):
        await client.upload_picture("PHOTO-1.jpg", JPEG)


@pytest.mark.parametrize(
    ("status", "error"), [(500, BabyTrackerConnectionError), (400, ProtocolError)]
)
async def test_an_upload_refused_with_an_error_status_raises(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    status: int,
    error: type[Exception],
) -> None:
    fake_server.upload_status = status
    with pytest.raises(error):
        await client.upload_picture("PHOTO-1.jpg", JPEG)
    assert fake_server.pictures == {}


async def test_an_upload_with_no_answer_raises_no_response(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    quick = BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
        request_timeout=0.2,
    )
    await quick.upload_picture("PHOTO-1.jpg", JPEG)  # signed in
    fake_server.delay = 0.6

    with pytest.raises(NoResponseError):
        await quick.upload_picture("PHOTO-2.jpg", JPEG)


@pytest.mark.parametrize(
    ("file_name", "data"),
    [
        ("", JPEG),
        ("   ", JPEG),
        ("a/b.jpg", JPEG),
        ("a\\b.jpg", JPEG),
        ("..", JPEG),
        ('PHOTO"1.jpg', JPEG),
        ("PHOTO\r\n1.jpg", JPEG),
        (None, JPEG),
        ("PHOTO-1.jpg", b""),
        ("PHOTO-1.jpg", b"\x89PNG\r\n\x1a\n"),
        ("PHOTO-1.jpg", "\xff\xd8\xff"),
        ("PHOTO-1.jpg", None),
    ],
)
async def test_a_bad_name_or_file_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, file_name: Any, data: Any
) -> None:
    with pytest.raises(ValueError):
        await client.upload_picture(file_name, data)
    assert fake_server.requests == []


async def test_a_file_over_the_limit_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_PICTURE_BYTES", len(JPEG) - 1)
    with pytest.raises(ValueError):
        await client.upload_picture("PHOTO-1.jpg", JPEG)
    assert fake_server.requests == []

    monkeypatch.setattr(client_module, "MAX_PICTURE_BYTES", len(JPEG))
    await client.upload_picture("PHOTO-1.jpg", JPEG)
