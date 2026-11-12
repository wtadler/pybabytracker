"""Downloading picture files, and the file names a baby's photo has on the service."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import pytest
from factories import baby_payload, payload_for
from fake_server import FakeSyncServer

from pybabytracker import (
    MEDIA_PHOTO,
    AuthenticationError,
    Baby,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Picture,
    ProtocolError,
    decode_payload,
)
from pybabytracker import client as client_module
from pybabytracker import models as models_module

JPEG = b"\xff\xd8\xff\xe0invented-jpeg-bytes\xff\xd9"


async def test_a_picture_the_service_has_comes_back_as_its_bytes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.pictures["PHOTO-1.jpg"] = JPEG

    assert await client.get_picture("PHOTO-1.jpg") == JPEG
    assert fake_server.calls("GET", "/account/picture/PHOTO-1.jpg")


@pytest.mark.parametrize("status", [400, 404])
async def test_a_picture_the_service_does_not_have_is_none(
    client: BabyTrackerClient, fake_server: FakeSyncServer, status: int
) -> None:
    fake_server.missing_picture_status = status
    assert await client.get_picture("MISSING.jpg") is None


async def test_an_expired_session_signs_in_again_for_a_picture(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.pictures["PHOTO-1.jpg"] = JPEG
    await client.get_picture("PHOTO-1.jpg")
    fake_server.sessions.clear()

    assert await client.get_picture("PHOTO-1.jpg") == JPEG


async def test_a_picture_the_service_keeps_rejecting_raises(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.always_unauthorized = True
    with pytest.raises(AuthenticationError):
        await client.get_picture("PHOTO-1.jpg")


async def test_a_server_error_for_a_picture_raises(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.fail_paths["/PHOTO-1.jpg"] = 503
    with pytest.raises(BabyTrackerConnectionError):
        await client.get_picture("PHOTO-1.jpg")


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "  ",
        "\t",
        "\n",
        "../secrets",
        "a/b.jpg",
        r"a\b.jpg",
        ".",
        "..",
        None,
        7,
        True,
        b"x",
    ],
)
async def test_a_name_that_is_not_a_plain_file_name_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: object
) -> None:
    with pytest.raises(ValueError):
        await client.get_picture(name)  # type: ignore[arg-type]
    assert fake_server.requests == []


@pytest.mark.parametrize(
    "name",
    ["my photo.jpg", "café 😀.jpg", "100%.jpg", "100%25.jpg", "a?b#c.jpg", "a..b.jpg"]
    + ["x" * 300 + ".jpg", "a+b.jpg", "a%2Fb.jpg"],
)
async def test_an_odd_but_plain_name_is_sent_percent_encoded_under_the_picture_path(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str
) -> None:
    fake_server.pictures[name] = JPEG

    assert await client.get_picture(name) == JPEG
    assert fake_server.picture_raw_paths == ["/account/picture/" + quote(name, safe="")]
    assert [r.path for r in fake_server.requests if r.path.startswith("/account/picture")] == [
        "/account/picture/" + name
    ]


@pytest.mark.parametrize("name", ["a&b=c.jpg", "@host.jpg", ":80.jpg", "a;b.jpg", "a,b.jpg"])
async def test_a_name_with_characters_a_path_allows_stays_under_the_picture_path(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str
) -> None:
    fake_server.pictures[name] = JPEG

    assert await client.get_picture(name) == JPEG
    [path] = fake_server.picture_raw_paths
    assert path.startswith("/account/picture/")
    assert path.count("/") == 3


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_for_a_picture_is_an_error_and_is_not_followed(
    client: BabyTrackerClient, fake_server: FakeSyncServer, status: int
) -> None:
    fake_server.picture_redirects["PHOTO-1.jpg"] = "/elsewhere/PHOTO-1.jpg"
    fake_server.redirect_status = status
    with pytest.raises(ProtocolError):
        await client.get_picture("PHOTO-1.jpg")
    assert not fake_server.calls("GET", "/elsewhere")


@pytest.mark.parametrize("status", [403, 410])
async def test_other_client_errors_for_a_picture_raise(
    client: BabyTrackerClient, fake_server: FakeSyncServer, status: int
) -> None:
    fake_server.fail_paths["/PHOTO-1.jpg"] = status
    with pytest.raises(ProtocolError):
        await client.get_picture("PHOTO-1.jpg")


@pytest.mark.parametrize("chunked", [False, True])
async def test_a_picture_past_the_size_cap_is_an_error(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    monkeypatch: pytest.MonkeyPatch,
    chunked: bool,
) -> None:
    monkeypatch.setattr(client_module, "MAX_PICTURE_BYTES", 1000)
    fake_server.picture_chunked = chunked
    fake_server.pictures["BIG.jpg"] = b"x" * 1001
    with pytest.raises(ProtocolError):
        await client.get_picture("BIG.jpg")


@pytest.mark.parametrize("chunked", [False, True])
async def test_a_picture_exactly_at_the_size_cap_comes_back(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    monkeypatch: pytest.MonkeyPatch,
    chunked: bool,
) -> None:
    monkeypatch.setattr(client_module, "MAX_PICTURE_BYTES", 1000)
    fake_server.picture_chunked = chunked
    fake_server.pictures["EDGE.jpg"] = b"x" * 1000
    assert await client.get_picture("EDGE.jpg") == b"x" * 1000


async def test_the_default_size_cap_is_twenty_megabytes() -> None:
    assert models_module.MAX_PICTURE_BYTES == 20 * 1024 * 1024


async def test_an_oversize_picture_is_still_an_error_when_the_session_has_to_sign_in_again(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "MAX_PICTURE_BYTES", 1000)
    fake_server.pictures["BIG.jpg"] = b"x" * 1001
    fake_server.pictures["PHOTO-1.jpg"] = JPEG
    await client.get_picture("PHOTO-1.jpg")
    fake_server.sessions.clear()
    with pytest.raises(ProtocolError):
        await client.get_picture("BIG.jpg")


async def test_two_pictures_fetched_at_once_with_an_expired_session_sign_in_once(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.pictures["A.jpg"] = JPEG
    fake_server.pictures["B.jpg"] = JPEG
    await client.get_picture("A.jpg")
    fake_server.sessions.clear()
    before = len(fake_server.calls("POST", "/session"))

    first, second = await asyncio.gather(client.get_picture("A.jpg"), client.get_picture("B.jpg"))

    assert (first, second) == (JPEG, JPEG)
    assert len(fake_server.calls("POST", "/session")) - before == 1


def _baby(picture_name: object) -> Baby:
    record = decode_payload(baby_payload(pictureName=picture_name))
    assert isinstance(record, Baby)
    return record


@pytest.mark.parametrize("picture_name", ["PHOTO-1", "PHOTO-1.jpg"])
def test_a_babys_photo_file_is_the_picture_name_with_a_jpg_extension(picture_name: str) -> None:
    assert _baby(picture_name).picture_file == "PHOTO-1.jpg"


def test_a_babys_photo_file_keeps_the_case_of_the_stored_extension() -> None:
    assert _baby("PHOTO-1.JPG").picture_file == "PHOTO-1.JPG"


@pytest.mark.parametrize("picture_name", ["PHOTO-1", "PHOTO-1.jpg", "PHOTO-1.JPG"])
def test_a_babys_landscape_file_adds_a_suffix_to_the_stem(picture_name: str) -> None:
    assert _baby(picture_name).landscape_picture_file == "PHOTO-1_l.jpg"


@pytest.mark.parametrize(
    "picture_name",
    [None, "", " ", "  ", "\t", ".jpg", " .jpg", "a/b", r"..\x", 7, True, False, 1.5, [], {}]
    + ["x/y.jpg", "..", "."],
)
def test_a_baby_without_a_usable_picture_name_has_no_files(picture_name: object) -> None:
    baby = _baby(picture_name)
    assert baby.picture_file is None
    assert baby.landscape_picture_file is None


def test_a_baby_with_no_picture_name_key_has_no_files() -> None:
    payload = baby_payload()
    del payload["pictureName"]
    baby = decode_payload(payload)
    assert isinstance(baby, Baby)
    assert (baby.picture_file, baby.landscape_picture_file) == (None, None)


def _entry(**overrides: object) -> dict[str, object]:
    """A pictureNote entry in the expected shape, with invented IDs.

    The picture itself was seen in synced data; the digit-string mediaType (as Android is
    expected to send it) and the other field values here are expected shape, not yet seen in
    synced data.
    """
    entry: dict[str, object] = {
        "activityID": "ACTIVITY-1",
        "attached": True,
        "deleted": False,
        "fileName": "PICTURE-1",
        "mediaType": "0",
        "newFlage": True,
        "objectID": "PICTURE-ID-1",
        "timestamp": "2026-11-07 23:29:14 +0000",
    }
    entry.update(overrides)
    return entry


def _pictures(*entries: object) -> list[Picture]:
    record = decode_payload(payload_for("Milestone", pictureNote=list(entries)))
    return record.pictures


def test_a_photo_on_a_record_names_its_file_and_thumbnail() -> None:
    assert _pictures(_entry()) == [
        Picture(
            "PICTURE-ID-1", "PICTURE-1", MEDIA_PHOTO, "PICTURE-1.jpg", "PICTURE-1__thumbnail.jpg"
        )
    ]


@pytest.mark.parametrize(
    ("media_type", "file"), [(1, "PICTURE-1.mp4"), ("1", "PICTURE-1.mp4"), (0, "PICTURE-1.jpg")]
)
def test_a_videos_file_is_mp4_and_either_form_of_media_type_reads(
    media_type: object, file: str
) -> None:
    [picture] = _pictures(_entry(mediaType=media_type))
    assert picture.file == file
    assert picture.thumbnail_file == "PICTURE-1__thumbnail.jpg"


def test_an_unknown_media_type_still_has_a_thumbnail_but_no_file() -> None:
    [picture] = _pictures(_entry(mediaType="2"))
    assert (picture.media_type, picture.file) == (2, None)


@pytest.mark.parametrize(
    "entry",
    [
        _entry(deleted=True),
        _entry(deleted="true"),
        _entry(fileName=""),
        _entry(fileName="../PICTURE-1"),
        _entry(fileName=7),
        "PICTURE-1",
    ],
)
def test_deleted_and_unusable_entries_are_left_out(entry: object) -> None:
    assert _pictures(entry, _entry(fileName="PICTURE-2")) == [
        Picture(
            "PICTURE-ID-1", "PICTURE-2", MEDIA_PHOTO, "PICTURE-2.jpg", "PICTURE-2__thumbnail.jpg"
        )
    ]


@pytest.mark.parametrize("picture_note", [None, [], "not a list"])
def test_a_record_without_pictures_has_none(picture_note: object) -> None:
    record = decode_payload(payload_for("Diaper", pictureNote=picture_note))
    assert record.pictures == []


@pytest.mark.parametrize(
    "picture_note",
    [True, 0, 7, {}, {"fileName": "PICTURE-1"}, ("a",)],
)
def test_a_picture_note_that_is_not_a_list_has_no_pictures(picture_note: object) -> None:
    record = decode_payload(payload_for("Diaper", pictureNote=picture_note))
    assert record.pictures == []


def test_a_record_with_no_picture_note_key_has_no_pictures() -> None:
    payload = payload_for("Diaper")
    del payload["pictureNote"]
    assert decode_payload(payload).pictures == []


@pytest.mark.parametrize(
    "entry",
    [
        None,
        7,
        True,
        [],
        {},
        {"mediaType": "0"},
        {"fileName": None},
        {"fileName": " "},
        {"fileName": "\t"},
        {"fileName": True},
        {"fileName": ["PICTURE-1"]},
        {"fileName": "a/b"},
        {"fileName": "a\\b"},
        {"fileName": "."},
        {"fileName": ".."},
        _entry(deleted="True"),
        _entry(deleted="TRUE"),
        _entry(deleted=1),
        _entry(deleted="1"),
        _entry(fileName=" "),
        _entry(fileName=None),
    ],
)
def test_bad_entries_never_raise_and_a_neighbor_still_reads(entry: object) -> None:
    pictures = _pictures(entry, _entry(fileName="PICTURE-2"))
    assert [p.file_name for p in pictures] == ["PICTURE-2"]


@pytest.mark.parametrize("deleted", [False, "false", None, 0, "0", "no", "", "yes"])
def test_an_entry_not_marked_deleted_is_kept(deleted: object) -> None:
    assert [p.file_name for p in _pictures(_entry(deleted=deleted))] == ["PICTURE-1"]


def test_an_entry_with_no_deleted_key_is_kept() -> None:
    entry = _entry()
    del entry["deleted"]
    assert [p.file_name for p in _pictures(entry)] == ["PICTURE-1"]


@pytest.mark.parametrize("media_type", [None, "abc", "", " ", True, False, [], {}, 1.5, "0.5"])
def test_a_media_type_that_is_not_a_code_gives_no_file_but_a_thumbnail(
    media_type: object,
) -> None:
    [picture] = _pictures(_entry(mediaType=media_type))
    assert picture.file is None
    assert picture.thumbnail_file == "PICTURE-1__thumbnail.jpg"


def test_an_entry_with_no_media_type_key_gives_no_file() -> None:
    entry = _entry()
    del entry["mediaType"]
    [picture] = _pictures(entry)
    assert (picture.media_type, picture.file) == (None, None)


@pytest.mark.parametrize("object_id", [None, 7, True, [], {}])
def test_an_object_id_that_is_not_a_string_is_none(object_id: object) -> None:
    [picture] = _pictures(_entry(objectID=object_id))
    assert picture.object_id is None


def test_an_entry_with_no_object_id_key_has_none() -> None:
    entry = _entry()
    del entry["objectID"]
    [picture] = _pictures(entry)
    assert picture.object_id is None


def test_pictures_keep_the_order_of_the_picture_note_and_duplicates() -> None:
    pictures = _pictures(_entry(fileName="B"), _entry(fileName="A"), _entry(fileName="B"))
    assert [p.file_name for p in pictures] == ["B", "A", "B"]
