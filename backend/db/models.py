from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func

SQLALCHEMY_DATABASE_URL = "sqlite:///./db/multichat.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String) # 'admin', 'ventas', 'soporte', 'recepcion'
    full_name = Column(String)

class Contact(Base):
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True)
    name = Column(String, nullable=True) # Nombre opcional (desconocido al inicio)
    notes = Column(Text, nullable=True) # Notas para el mini-CRM
    assigned_department = Column(String, nullable=True) # Departamento asigando permanentemente o por reasignación
    observation = Column(Text, nullable=True) # Observacion para el agente al reasignar
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    messages = relationship("Message", back_populates="contact")
    tickets = relationship("Ticket", back_populates="contact")

class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    direction = Column(String) # "inbound" (cliente escribe) o "outbound" (agente responde)
    text = Column(String)
    department_assigned = Column(String, nullable=True) # Donde se ruteo
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    contact = relationship("Contact", back_populates="messages")

class Ticket(Base):
    __tablename__ = "tickets"
    
    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    description = Column(Text)
    level = Column(String) # Nivel de escalamiento, ej: "Nivel 2"
    status = Column(String, default="abierto") # abierto, cerrado
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    contact = relationship("Contact", back_populates="tickets")
