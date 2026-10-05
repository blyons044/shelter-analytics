"""
Synthetic Austin Animal Center data for offline development and CI smoke tests.

The shape matters more than the volume. This generator reproduces the parts of
the real feed that the models have to survive:

  - animals with repeat stays, which is why intakes cannot be joined to
    outcomes on animal_id alone
  - animals currently in the shelter, with an intake and no outcome yet
  - age strings in free text, including the literal 'NULL' and negative values
  - inconsistent casing and padding in categorical fields
  - exact duplicate rows, which the portal emits occasionally
  - a small number of outcomes dated before their intake

Seeded, so runs are reproducible.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

SEED = 20260105

ANIMAL_TYPES = ["Dog", "Dog", "Dog", "Cat", "Cat", "Cat", "Bird", "Other"]

INTAKE_TYPES = [
    "Stray", "Stray", "Stray", "Stray", "Owner Surrender", "Owner Surrender",
    "Public Assist", "Wildlife", "Abandoned", "Euthanasia Request",
]

INTAKE_CONDITIONS = [
    "Normal", "Normal", "Normal", "Normal", "Injured", "Sick", "Nursing",
    "Aged", "Pregnant", "Feral",
]

OUTCOME_TYPES = [
    "Adoption", "Adoption", "Adoption", "Adoption", "Adoption",
    "Transfer", "Transfer", "Transfer",
    "Return to Owner", "Return to Owner",
    "Rto-Adopt", "Euthanasia", "Died", "Disposal", "Missing",
]

OUTCOME_SUBTYPES = {
    "Adoption": [None, None, "Foster", "Offsite", "Barn"],
    "Transfer": ["Partner", "Partner", "SCRP", "Snr"],
    "Euthanasia": ["Suffering", "Rabies Risk", "Aggressive", "Medical", "At Vet"],
    "Died": ["In Kennel", "In Foster", "At Vet", "Enroute"],
}

SEX_STATES = [
    "Neutered Male", "Spayed Female", "Intact Male", "Intact Female", "Unknown",
]

DOG_BREEDS = [
    "Pit Bull Mix", "Labrador Retriever Mix", "Chihuahua Shorthair Mix",
    "German Shepherd Mix", "Australian Cattle Dog Mix", "Border Collie Mix",
    "Dachshund Mix", "Boxer Mix",
]
CAT_BREEDS = [
    "Domestic Shorthair Mix", "Domestic Shorthair Mix", "Domestic Medium Hair Mix",
    "Domestic Longhair Mix", "Siamese Mix",
]
OTHER_BREEDS = ["Bat", "Raccoon", "Opossum", "Rabbit Sh", "Guinea Pig"]

COLORS = [
    "Black/White", "Brown Tabby", "Black", "White", "Brown/White", "Tricolor",
    "Orange Tabby", "Blue/White", "Tan/White", "Calico",
]

LOCATIONS = [
    "Austin (TX)", "7201 Levander Loop in Austin (TX)", "Travis (TX)",
    "Outside Jurisdiction", "Del Valle (TX)", "Pflugerville (TX)",
]

START = datetime(2022, 1, 1)
END = datetime(2026, 9, 30)


def _age_string(days: int, rng: random.Random) -> str:
    """Free-text age, matching the portal's own inconsistency."""
    roll = rng.random()
    if roll < 0.015:
        return "NULL"
    if roll < 0.025:
        return f"-{rng.randint(1, 3)} years"
    if days < 30:
        n = max(1, days // 7)
        return f"{n} week{'s' if n != 1 else ''}"
    if days < 365:
        n = max(1, days // 30)
        return f"{n} month{'s' if n != 1 else ''}"
    n = days // 365
    return f"{n} year{'s' if n != 1 else ''}"


def _maybe_scramble(value: str, rng: random.Random) -> str:
    """Occasionally return the value with the casing or padding the feed has."""
    roll = rng.random()
    if roll < 0.03:
        return value.upper()
    if roll < 0.06:
        return f"  {value} "
    if roll < 0.08:
        return value.lower()
    return value


def generate(n_animals: int) -> dict[str, list[dict]]:
    rng = random.Random(SEED)
    intakes: list[dict] = []
    outcomes: list[dict] = []

    span_days = (END - START).days

    for i in range(n_animals):
        animal_id = f"A{700000 + i}"
        animal_type = rng.choice(ANIMAL_TYPES)
        if animal_type == "Dog":
            breed = rng.choice(DOG_BREEDS)
        elif animal_type == "Cat":
            breed = rng.choice(CAT_BREEDS)
        else:
            breed = rng.choice(OTHER_BREEDS)
        color = rng.choice(COLORS)
        name = None if rng.random() < 0.34 else rng.choice(
            ["Luna", "Bella", "Max", "Charlie", "Daisy", "Milo", "Zeus", "Nala",
             "Rocky", "Juno", "Pepper", "Scout", "Willow", "Rex"]
        )
        dob = START - timedelta(days=rng.randint(60, 4000))

        # Most animals come through once. A meaningful minority return.
        n_stays = 1
        if rng.random() < 0.17:
            n_stays = 2
        if rng.random() < 0.03:
            n_stays = 3

        cursor = START + timedelta(days=rng.randint(0, max(1, span_days - 120)))

        for stay in range(n_stays):
            intake_dt = cursor + timedelta(
                days=rng.randint(0, 200) if stay else 0,
                hours=rng.randint(6, 20),
                minutes=rng.choice([0, 15, 30, 45]),
            )
            if intake_dt > END:
                break

            age_days = max(1, (intake_dt - dob).days)
            intake_type = rng.choice(INTAKE_TYPES)
            sex = rng.choice(SEX_STATES)

            intakes.append({
                "animal_id": animal_id,
                "name": name,
                "datetime": intake_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
                "found_location": rng.choice(LOCATIONS),
                "intake_type": _maybe_scramble(intake_type, rng),
                "intake_condition": _maybe_scramble(rng.choice(INTAKE_CONDITIONS), rng),
                "animal_type": animal_type,
                "sex_upon_intake": sex,
                "age_upon_intake": _age_string(age_days, rng),
                "breed": breed,
                "color": color,
            })

            # Animals taken in recently may still be here.
            still_here = (END - intake_dt).days < 30 and rng.random() < 0.55
            if still_here:
                cursor = intake_dt
                continue

            los = max(0, int(rng.lognormvariate(1.9, 0.95)))
            outcome_dt = intake_dt + timedelta(days=los, hours=rng.randint(1, 10))
            if outcome_dt > END:
                cursor = intake_dt
                continue

            outcome_type = rng.choice(OUTCOME_TYPES)
            if intake_type == "Euthanasia Request" and rng.random() < 0.7:
                outcome_type = "Euthanasia"
            if intake_type == "Wildlife" and rng.random() < 0.6:
                outcome_type = rng.choice(["Euthanasia", "Disposal", "Transfer"])

            # A small number of records land with the outcome before the intake.
            if rng.random() < 0.004:
                outcome_dt = intake_dt - timedelta(days=rng.randint(1, 5))

            outcomes.append({
                "animal_id": animal_id,
                "name": name,
                "datetime": outcome_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
                "date_of_birth": dob.strftime("%Y-%m-%dT00:00:00.000"),
                "outcome_type": _maybe_scramble(outcome_type, rng),
                "outcome_subtype": rng.choice(OUTCOME_SUBTYPES.get(outcome_type, [None, None, None])),
                "animal_type": animal_type,
                "sex_upon_outcome": sex,
                "age_upon_outcome": _age_string(max(1, (outcome_dt - dob).days), rng),
                "breed": breed,
                "color": color,
            })

            cursor = outcome_dt

    # Animals whose intake predates the published range appear in the outcome
    # feed with nothing to pair against. The models have to account for these
    # rather than dropping them, so the sample contains them too.
    for j in range(max(1, n_animals // 120)):
        animal_id = f"A{600000 + j}"
        animal_type = rng.choice(ANIMAL_TYPES)
        dob = START - timedelta(days=rng.randint(400, 4000))
        outcome_dt = START + timedelta(days=rng.randint(0, 90), hours=rng.randint(8, 18))
        outcome_type = rng.choice(OUTCOME_TYPES)
        outcomes.append({
            "animal_id": animal_id,
            "name": None,
            "datetime": outcome_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "date_of_birth": dob.strftime("%Y-%m-%dT00:00:00.000"),
            "outcome_type": outcome_type,
            "outcome_subtype": rng.choice(OUTCOME_SUBTYPES.get(outcome_type, [None, None, None])),
            "animal_type": animal_type,
            "sex_upon_outcome": rng.choice(SEX_STATES),
            "age_upon_outcome": _age_string(max(1, (outcome_dt - dob).days), rng),
            "breed": rng.choice(DOG_BREEDS if animal_type == "Dog" else CAT_BREEDS),
            "color": rng.choice(COLORS),
        })

    # The portal emits the occasional exact duplicate.
    for source in (intakes, outcomes):
        for row in rng.sample(source, k=max(1, len(source) // 180)):
            source.append(dict(row))

    rng.shuffle(intakes)
    rng.shuffle(outcomes)
    return {"intakes": intakes, "outcomes": outcomes}
