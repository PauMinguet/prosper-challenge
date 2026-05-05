from datetime import date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Patient(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    dob: date = Field(index=True)
    phone: Optional[str] = None
    email: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)


class PatientCreate(SQLModel):
    name: str
    dob: date
    phone: Optional[str] = None
    email: Optional[str] = None


class Slot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    start_at: datetime = Field(unique=True, index=True)
    duration_minutes: int = 30


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    slot_id: int = Field(foreign_key="slot.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    cancelled_at: Optional[datetime] = None


class AppointmentCreate(SQLModel):
    patient_id: int
    slot_id: int
