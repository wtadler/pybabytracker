"""The app's pick lists: the milestones and vaccines it ships with, and how a record names one.

A ``Milestone`` record embeds the milestone it marks (a ``MilestoneSelection``) as
``milestoneType``, a ``Vaccine`` its vaccine as ``vaccineType``, and a ``Medication`` its medicine
as ``medicationSelection``. The built-in milestones and vaccines aren't on the service: every app
seeds them locally under fixed IDs starting ``EasyLogSeed``, so a record that names one must use
the same ID for the app to match it.

How well the built-in IDs are checked: one built-in milestone ("Back home") and one built-in
vaccine ("HepB") were posted from this library and read back unchanged on iOS, and the app showed
each as its own built-in entry, with no duplicate in the pick list. The other 19 milestone IDs
and 9 vaccine IDs come from the same table of seeded rows but are unverified, and nothing has
been checked on Android. A wrong ID would show up in the app as a different entry with a
clashing name.

Milestones and vaccines a user adds are expected to sync as ``MilestoneSelection`` and
``VaccineSelection`` transactions of their own (expected shape, not yet seen in synced data).
A medicine a user adds has been seen as a ``MedicationSelection`` transaction of its own,
captured from an iPhone on 2026-11-11, and every dose embeds it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .exceptions import AmbiguousSelectionError
from .models import Record, _selection_name

# The timestamp the app gives its seeded selections.
_SEED_TIMESTAMP = "1970-01-01 00:00:00 +0000"

# The app's built-in milestones, in its order: ID, name, and the description it shows after the
# baby's name ("<name> came home"). The leading space or "'s" is part of the description.
_BUILT_IN_MILESTONES: tuple[tuple[str, str, str], ...] = (
    ("EasyLogSeed3b250600-894f-11e2-9e96-0800200c9a66", "Back home", " came home"),
    (
        "EasyLogSeed44720140-894f-11e2-9e96-0800200c9a66",
        "Recognizes caregiver’s voice",
        " recognized our voice",
    ),
    (
        "EasyLogSeedb5196be0-894f-11e2-9e96-0800200c9a66",
        "Recognizes familiar faces",
        " recognizes familiar faces",
    ),
    ("EasyLogSeed56cc56b0-894f-11e2-9e96-0800200c9a66", "First smile", "'s first smile"),
    ("EasyLogSeed5ecef7a0-894f-11e2-9e96-0800200c9a66", "Raise head", " lifted head up"),
    ("EasyLogSeed773fae60-894f-11e2-9e96-0800200c9a66", "Claps Hand", " claps hand"),
    (
        "EasyLogSeed852cb5a0-b815-11e2-9e96-0800200c9a66",
        "Slept through the night",
        " slept through the night",
    ),
    ("EasyLogSeed8ca15060-894f-11e2-9e96-0800200c9a66", "Laughs out loud", "'s first laugh"),
    ("EasyLogSeed96964b70-894f-11e2-9e96-0800200c9a66", "Babbles", " babbles"),
    ("EasyLogSeeda0faf840-894f-11e2-9e96-0800200c9a66", "Holds head steady", "  holds head up"),
    (
        "EasyLogSeeda7ef3670-894f-11e2-9e96-0800200c9a66",
        "Responds to own name",
        " responds when called",
    ),
    ("EasyLogSeedbd0eed70-894f-11e2-9e96-0800200c9a66", "Rolls over", " rolls over"),
    ("EasyLogSeed00932210-b816-11e2-9e96-0800200c9a66", "Sits up", " sits up"),
    ("EasyLogSeed22db0820-b815-11e2-9e96-0800200c9a66", "First tooth", "'s first tooth"),
    ("EasyLogSeedd3559840-894f-11e2-9e96-0800200c9a66", "Says mama", " says mama"),
    ("EasyLogSeeddaf4e380-894f-11e2-9e96-0800200c9a66", "Says dada", " says dada"),
    ("EasyLogSeed7b293110-b814-11e2-9e96-0800200c9a66", "First word", " says first word"),
    ("EasyLogSeed662c00c0-b815-11e2-9e96-0800200c9a66", "Waves Bye-Bye", " waves Bye-Bye"),
    ("EasyLogSeed2dae38c0-b816-11e2-9e96-0800200c9a66", "Stands up", " stands up"),
    ("EasyLogSeed4f6c38e0-b816-11e2-9e96-0800200c9a66", "First steps", "'s first steps"),
)


@dataclass(frozen=True, slots=True)
class SelectionChoice:
    """An entry of a pick list a record can name (a milestone, a vaccine, a medicine): its ID,
    its name, whether the app ships it, and the object a record embeds."""

    object_id: str
    name: str
    built_in: bool
    selection: Mapping[str, Any] = field(compare=False, repr=False)

    @property
    def unit(self) -> str | None:
        """The entry's ``unit`` (a medicine's, such as "ml"), when it has a text one."""
        unit = self.selection.get("unit")
        return unit if isinstance(unit, str) else None


@dataclass(frozen=True, slots=True)
class EmbeddedCopy:
    """A selection object a record embeds, with what ranks and places that record.

    ``key`` orders records oldest to newest: the record's own ``timestamp`` first, then its
    ``time``, then its object ID so that a full tie still has one answer in any arrival order.
    """

    selection: object
    key: tuple[float, float, str]
    baby_id: str | None


def embedded_copy(record: Record, selection: object) -> EmbeddedCopy:
    """The selection ``record`` embeds, as ``merged_choices`` takes it."""
    oldest = float("-inf")
    stamp = record.timestamp.timestamp() if record.timestamp is not None else oldest
    event = record.time.timestamp() if record.time is not None else oldest
    return EmbeddedCopy(selection, (stamp, event, record.object_id), record.baby_id)


# The app's built-in vaccines, in its order: ID, short name, and full name. The app seeds every
# one with forAge 0.
_BUILT_IN_VACCINES: tuple[tuple[str, str, str], ...] = (
    ("EasyLogSeed001ddeae8f0-8805-11e2-9e96-0800200c9a66", "HepB", "Hepatitis B"),
    ("EasyLogSeed002e733ec90-8805-11e2-9e96-0800200c9a66", "RV", "Rotavirus"),
    (
        "EasyLogSeed003effe4a50-8805-11e2-9e96-0800200c9a66",
        "DTaP",
        "Diphtheria, Tetanus, Pertussis",
    ),
    (
        "EasyLogSeed004f8ab3500-8805-11e2-9e96-0800200c9a66",
        "Hib",
        "Haemophilus influenzae type b",
    ),
    ("EasyLogSeed0050051d340-8806-11e2-9e96-0800200c9a66", "PCV", "Pneumococcal"),
    ("EasyLogSeed0060838fcf0-8806-11e2-9e96-0800200c9a66", "IPV", "Inactivated Poliovirus"),
    ("EasyLogSeed007132f8980-8806-11e2-9e96-0800200c9a66", "Influenza", "Influenza"),
    ("EasyLogSeed0081d5383d0-8806-11e2-9e96-0800200c9a66", "MMR", "Measles, Mumps, Rubella"),
    ("EasyLogSeed00925268940-8806-11e2-9e96-0800200c9a66", "Varicella", "Varicella"),
    ("EasyLogSeed0102d1f1810-8806-11e2-9e96-0800200c9a66", "Hep A", "Hepatitis A"),
)


def _seed(object_type: str, object_id: str, name: str, desc: str, **extra: Any) -> SelectionChoice:
    """A built-in entry as the app seeds it, in the shape a record embeds."""
    return SelectionChoice(
        object_id=object_id,
        name=name,
        built_in=True,
        selection={
            "BCObjectType": object_type,
            "objectID": object_id,
            "name": name,
            "desc": desc,
            **extra,
            "newFlage": "false",
            "timestamp": _SEED_TIMESTAMP,
        },
    )


BUILT_IN_MILESTONES: tuple[SelectionChoice, ...] = tuple(
    _seed("MilestoneSelection", *row, forAge=0, used="false") for row in _BUILT_IN_MILESTONES
)

# The activity types the app ships with, in its order.
_BUILT_IN_ACTIVITIES: tuple[tuple[str, str], ...] = (
    ("EasyLogSeed20029ae0-8cbf-11e2-9e96-0800200c9a66", "Crawling"),
    ("EasyLogSeed2cfd0dc0-8cbf-11e2-9e96-0800200c9a66", "Cruising"),
    ("EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66", "Tummy Time"),
    ("EasyLogSeed537ed780-8cbf-11e2-9e96-0800200c9a66", "TV Time"),
    ("EasyLogSeed5b0a4ca0-8cbf-11e2-9e96-0800200c9a66", "Story Time"),
)

BUILT_IN_ACTIVITIES: tuple[SelectionChoice, ...] = tuple(
    _seed("OtherActivityDescription", object_id, name, "")
    for object_id, name in _BUILT_IN_ACTIVITIES
)

# The foods the app ships with, in its order: ID, name, and whether it's a drink offered on the
# bottle screen (``bottle``) rather than a solid.
_BUILT_IN_FOODS: tuple[tuple[str, str, bool], ...] = (
    ("EasyLogSeed6136db30-8c55-11e2-9e96-0800200c9a66", "Cereal", False),
    ("EasyLogSeed69ff6430-8c55-11e2-9e96-0800200c9a66", "Veggies", False),
    ("EasyLogSeed72504c30-8c55-11e2-9e96-0800200c9a66", "Fruit", False),
    ("EasyLogSeed7ad367c0-8c55-11e2-9e96-0800200c9a66", "Meat", False),
    ("EasyLogSeed84962f40-8c55-11e2-9e96-0800200c9a66", "Finger Foods", False),
    ("EasyLogSeed8cdabb30-8c55-11e2-9e96-0800200c9a66", "Vitamins", False),
    ("EasyLogSeed9500c2a0-8c55-11e2-9e96-0800200c9a66", "Water", True),
    ("EasyLogSeed9c31bbb0-8c55-11e2-9e96-0800200c9a66", "Juice", True),
    ("EasyLogSeeda40424e0-8c55-11e2-9e96-0800200c9a66", "Milk", True),
)

BUILT_IN_FOODS: tuple[SelectionChoice, ...] = tuple(
    _seed("OtherFeedSelection", object_id, name, "", bottle="true" if bottle else "false")
    for object_id, name, bottle in _BUILT_IN_FOODS
)

BUILT_IN_VACCINES: tuple[SelectionChoice, ...] = tuple(
    _seed("VaccineSelection", *row, forAge=0) for row in _BUILT_IN_VACCINES
)


def choice_from_selection(selection: object) -> SelectionChoice | None:
    """A choice for a selection object, or ``None`` without a string ID and name."""
    if not isinstance(selection, Mapping):
        return None
    object_id, name = selection.get("objectID"), _selection_name(selection)
    if not isinstance(object_id, str) or not object_id or name is None:
        return None
    return SelectionChoice(object_id, name, False, dict(selection))


def _shown_to(selection: object, baby_id: str | None) -> bool:
    """Whether an entry added for one baby (a non-empty ``babyID``) is for ``baby_id``."""
    scope = selection.get("babyID") if isinstance(selection, Mapping) else None
    return baby_id is None or not isinstance(scope, str) or not scope or scope == baby_id


def merged_choices(
    built_ins: Iterable[SelectionChoice],
    embedded: Iterable[EmbeddedCopy],
    synced: Iterable[object],
    *,
    baby_id: str | None = None,
) -> list[SelectionChoice]:
    """The entries a record can name: the built-ins, in the app's order, then the rest by name.

    ``embedded`` are the selection objects records already carry; ``synced`` are selections
    synced as records of their own. Where several copies of one entry exist, a synced copy wins,
    then the copy in the newest record (see ``EmbeddedCopy``), never the one that arrived first.
    A copy of a built-in replaces its template, keeping it in the app's place.

    With a ``baby_id``, an entry added for another baby (a ``babyID`` on its selection) is left
    out, and so is a custom entry only another baby's records embed: nothing says it is for every
    baby. Built-ins are for every baby, whichever baby's record carries the copy.
    """
    order = {choice.object_id: i for i, choice in enumerate(built_ins)}
    found = {choice.object_id: choice for choice in built_ins}
    shown: list[SelectionChoice] = []
    hidden: set[str] = set()  # synced entries kept for another baby
    for selection in synced:
        copy = choice_from_selection(selection)
        if copy is None:
            continue
        if _shown_to(selection, baby_id):
            shown.append(copy)
        else:
            hidden.add(copy.object_id)
    taken: set[str] = set()
    for item in sorted(embedded, key=lambda e: e.key, reverse=True):
        copy = choice_from_selection(item.selection)
        if copy is None or copy.object_id in taken:
            continue
        if copy.object_id not in order and (
            copy.object_id in hidden
            or not _shown_to(item.selection, baby_id)
            or (baby_id is not None and item.baby_id != baby_id)
        ):
            continue  # an older record of the right baby may still carry it
        taken.add(copy.object_id)
        found[copy.object_id] = copy
    for copy in shown:
        found[copy.object_id] = copy
    choices = [
        SelectionChoice(c.object_id, c.name, c.object_id in order, c.selection)
        for c in found.values()
    ]
    built = sorted((c for c in choices if c.built_in), key=lambda c: order[c.object_id])
    added = sorted(
        (c for c in choices if not c.built_in), key=lambda c: (c.name.casefold(), c.object_id)
    )
    return built + added


def find_choice(
    choices: Iterable[SelectionChoice], wanted: str, what: str
) -> SelectionChoice | None:
    """The entry with that ID, or else that name, ignoring case and surrounding spaces.

    ``None`` when nothing matches. A name that matches more than one entry (two medicines of one
    name, or a custom entry named like a built-in) raises ``AmbiguousSelectionError`` naming their
    IDs, so the caller must pass the ID: it never picks one.
    """
    listed = list(choices)
    text = wanted.strip()
    key = text.casefold()
    for matches in (
        [c for c in listed if c.object_id == text],
        [c for c in listed if c.object_id.casefold() == key],
        [c for c in listed if c.name.strip().casefold() == key],
    ):
        if len(matches) > 1:
            raise AmbiguousSelectionError(what, wanted, tuple(c.object_id for c in matches))
        if matches:
            return matches[0]
    return None
