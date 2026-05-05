from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from .db import get_session, init_db
from .models import (
    Appointment,
    AppointmentCreate,
    Patient,
    PatientCreate,
    Slot,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Prosper EHR", lifespan=lifespan)


@app.post("/patients", response_model=Patient, status_code=201)
def create_patient(payload: PatientCreate, session: Session = Depends(get_session)) -> Patient:
    patient = Patient.model_validate(payload)
    session.add(patient)
    session.commit()
    session.refresh(patient)
    return patient


@app.get("/patients/find", response_model=Patient)
def find_patient(
    name: str = Query(..., min_length=1),
    dob: date = Query(...),
    session: Session = Depends(get_session),
) -> Patient:
    stmt = select(Patient).where(
        func.lower(Patient.name) == name.lower(),
        Patient.dob == dob,
    )
    matches = session.exec(stmt).all()
    if not matches:
        raise HTTPException(404, "No patient matches that name and date of birth")
    if len(matches) > 1:
        raise HTTPException(
            409,
            f"{len(matches)} patients match — disambiguation required",
        )
    return matches[0]


@app.get("/patients/{patient_id}/appointments")
def list_patient_appointments(
    patient_id: int,
    include_cancelled: bool = False,
    session: Session = Depends(get_session),
) -> list[dict]:
    if not session.get(Patient, patient_id):
        raise HTTPException(404, "Patient not found")
    stmt = select(Appointment, Slot).join(Slot, Slot.id == Appointment.slot_id).where(
        Appointment.patient_id == patient_id
    )
    if not include_cancelled:
        stmt = stmt.where(Appointment.cancelled_at.is_(None))
    stmt = stmt.order_by(Slot.start_at)
    return [
        {
            "id": appt.id,
            "patient_id": appt.patient_id,
            "slot_id": appt.slot_id,
            "start_at": slot.start_at.isoformat(),
            "duration_minutes": slot.duration_minutes,
            "created_at": appt.created_at.isoformat(),
            "cancelled_at": appt.cancelled_at.isoformat() if appt.cancelled_at else None,
        }
        for appt, slot in session.exec(stmt).all()
    ]


@app.get("/slots", response_model=list[Slot])
def list_availability_slots(
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    session: Session = Depends(get_session),
) -> list[Slot]:
    booked = select(Appointment.slot_id).where(Appointment.cancelled_at.is_(None))
    stmt = (
        select(Slot)
        .where(Slot.start_at >= from_)
        .where(Slot.start_at <= to)
        .where(Slot.id.not_in(booked))
        .order_by(Slot.start_at)
    )
    return list(session.exec(stmt).all())


@app.post("/appointments", response_model=Appointment, status_code=201)
def create_appointment(
    payload: AppointmentCreate, session: Session = Depends(get_session)
) -> Appointment:
    if not session.get(Patient, payload.patient_id):
        raise HTTPException(404, "Patient not found")
    if not session.get(Slot, payload.slot_id):
        raise HTTPException(404, "Slot not found")

    existing = session.exec(
        select(Appointment).where(
            Appointment.slot_id == payload.slot_id,
            Appointment.cancelled_at.is_(None),
        )
    ).first()
    if existing:
        if existing.patient_id == payload.patient_id:
            return existing
        raise HTTPException(409, "Slot already booked")

    appointment = Appointment.model_validate(payload)
    session.add(appointment)
    session.commit()
    session.refresh(appointment)
    return appointment


@app.delete("/appointments/{appointment_id}", response_model=Appointment)
def cancel_appointment(
    appointment_id: int, session: Session = Depends(get_session)
) -> Appointment:
    appointment = session.get(Appointment, appointment_id)
    if not appointment:
        raise HTTPException(404, "Appointment not found")
    if appointment.cancelled_at is None:
        appointment.cancelled_at = datetime.now()
        session.add(appointment)
        session.commit()
        session.refresh(appointment)
    return appointment
