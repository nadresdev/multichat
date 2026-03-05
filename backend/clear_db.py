import os
import sys

# Forzar al script a ubicarse en su propia carpeta para evitar errores de SQLite por CWD
current_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(current_dir)
sys.path.append(current_dir)

from sqlalchemy.orm import Session
from db.models import SessionLocal, Contact, Message, Ticket, Base, engine

def clear_database():
    print("Iniciando purga de la base de datos...")
    db: Session = SessionLocal()
    try:
        # Borramos en orden para respetar las foreign keys
        deleted_messages = db.query(Message).delete()
        deleted_tickets = db.query(Ticket).delete()
        deleted_contacts = db.query(Contact).delete()
        
        db.commit()
        
        print(f"Purga exitosa:")
        print(f"- {deleted_messages} mensajes borrados.")
        print(f"- {deleted_tickets} tickets borrados.")
        print(f"- {deleted_contacts} contactos borrados.")
        print("La base de datos está ahora vacía y lista para pruebas frescas.")
    except Exception as e:
        db.rollback()
        print(f"Error al vaciar la base de datos: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    confirm = input("¿Estás seguro de que deseas borrar TODOS los chats, contactos y tickets? (s/n): ")
    if confirm.lower() == 's':
        clear_database()
    else:
        print("Operación cancelada.")
