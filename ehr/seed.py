"""Seed the EHR database with appointment slots for the next two weeks."""

from datetime import date, datetime, time, timedelta

from sqlmodel import Session, select

from .db import engine, init_db
from .models import Slot

DAYS_AHEAD = 14
START_HOUR = 9
END_HOUR = 17
SLOT_MINUTES = 30


def generate_slots() -> list[Slot]:
    slots: list[Slot] = []
    today = date.today()
    for offset in range(DAYS_AHEAD):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:  # weekends off
            continue
        cursor = datetime.combine(day, time(START_HOUR, 0))
        end = datetime.combine(day, time(END_HOUR, 0))
        while cursor < end:
            slots.append(Slot(start_at=cursor, duration_minutes=SLOT_MINUTES))
            cursor += timedelta(minutes=SLOT_MINUTES)
    return slots


def main() -> None:
    init_db()
    with Session(engine) as session:
        if session.exec(select(Slot)).first():
            existing = len(session.exec(select(Slot)).all())
            print(f"Slots already exist ({existing}). Skipping seed.")
            return
        slots = generate_slots()
        for slot in slots:
            session.add(slot)
        session.commit()
        print(f"Seeded {len(slots)} slots from {slots[0].start_at} to {slots[-1].start_at}.")


if __name__ == "__main__":
    main()
