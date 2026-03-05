import json
import os
import requests
from typing import Dict, List
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from typing import Annotated
from jose import jwt, JWTError
from auth import SECRET_KEY, ALGORITHM
from routers import auth as auth_router
import google.generativeai as genai
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db.models import Base, engine, SessionLocal, Contact, Message, Ticket

load_dotenv()

# Asegurar que las tablas existan al iniciar
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

app = FastAPI()

# Modelo de salida de Pydantic para Gemini
class ClassificationResponse(BaseModel):
    department: str
    suggested_reply: str

# Inicializamos el cliente de Gemini. Tomará la clave API desde GEMINI_API_KEY
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
system_instruction = '''Eres un asistente inteligente para una empresa.
Tu objetivo es doble:
1. Analizar el mensaje del usuario y clasificarlo en un `department`. Debe ser ESTRICTAMENTE una de las siguientes opciones (en minúscula):
- "ventas" -> Si el cliente muestra intención de comprar, pregunta por un producto, precios o promociones.
- "soporte" -> Si el cliente reporta un fallo, un producto dañado, necesita ayuda técnica, o tiene un error.
- "recepcion" -> Para saludos generales, preguntas sobre horarios, u otra consulta que no encaje en ventas/soporte.

2. Generar un `suggested_reply`: Un borrador MUY breve (1 sola oración) de cortesía que el agente pueda usar para responderle al cliente según la intención.'''

model = genai.GenerativeModel('gemini-2.5-flash', system_instruction=system_instruction)

app.include_router(auth_router.router)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Para servir nuestro Frontend
templates = Jinja2Templates(directory="templates")

# Configuración de los tokens de Meta
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "MI_TOKEN_SECRETO_MULTICHAT")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")

# Modelo para cuando envíamos un msj desde nuestro portal hacia WhatsApp
class SendMessageRequest(BaseModel):
    phone_number: str
    text: str
    department: str

# Modelo para reasignar conversación
class ReassignRequest(BaseModel):
    department: str
    observation: str = ""

# Modelos para Tickets
class TicketCreateRequest(BaseModel):
    description: str
    level: str


# Gestor básico de conexiones WebSocket por departamento
class ConnectionManager:
    def __init__(self):
        # Mapea departamentos a listas de WebSockets: {'ventas': [ws1, ws2], 'soporte': [ws3]}
        self.active_connections: Dict[str, List[WebSocket]] = {
            "ventas": [],
            "soporte": [],
            "recepcion": [],
            "todos": [] # <--- Para que los administradores reciban todo
        }

    async def connect(self, ws: WebSocket, department: str):
        await ws.accept()
        if department in self.active_connections:
            self.active_connections[department].append(ws)

    def disconnect(self, ws: WebSocket, department: str):
        if department in self.active_connections:
            self.active_connections[department].remove(ws)

    async def broadcast(self, message: dict, department: str):
        if department in self.active_connections:
            for connection in self.active_connections[department]:
                await connection.send_json(message)

manager = ConnectionManager()


def save_message_to_db(phone: str, text: str, direction: str, department: str = None):
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.phone_number == phone).first()
        if not contact:
            # Creamos un nuevo perfil de cliente automáticamente
            contact = Contact(phone_number=phone, assigned_department=department)
            db.add(contact)
            db.commit()
            db.refresh(contact)
        elif not contact.assigned_department and department:
            # Si existía pero la IA apenas le dió un departamento final
            contact.assigned_department = department
            db.commit()
        
        msg = Message(
            contact_id=contact.id,
            direction=direction,
            text=text,
            department_assigned=department
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg.created_at
    except Exception as e:
        print(f"Error guardando en BD: {e}")
        return None
    finally:
        db.close()


# ---- RUTAS E INTERFAZ DEL AGENTE (Frontend) ----

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    """Página de inicio de sesión"""
    # Si hay token se encarga el javascript del index.html de mostrar una u otra cosa
    return templates.TemplateResponse("index.html", {"request": request})

async def get_current_user_ws(token: str):
    """Valida el token JWT en la conexion WebSocket"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        if username is None or role is None:
            raise WebSocketDisconnect(code=1008) # Policy Violation
        return {"username": username, "role": role}
    except JWTError:
        raise WebSocketDisconnect(code=1008)

@app.websocket("/ws/{department}")
async def websocket_endpoint(websocket: WebSocket, department: str, token: str):
    """
    Endpoint para conectar el frontend del agente (en tiempo real) según su departamento.
    Solo puede conectarse si su rol coincide con el departamento (o si tiene token válido).
    """
    # Validar token
    user = await get_current_user_ws(token)
    
    # Validamos que el agente solo se pueda conectar a su departamento (o es admin)
    if user["role"] != "admin" and user["role"] != department:
        await websocket.close(code=1008)
        return
        
    await manager.connect(websocket, department)
    try:
        while True:
            # Mantener la conexión viva
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, department)


# ---- RUTAS WEBHOK DE META (WhatsApp) ----

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    1. Endpoint GET para que Meta valide que el servidor existe.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("WEBHOOK VERIFICADO POR META 🚀")
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(challenge)
        else:
            raise HTTPException(status_code=403, detail="Token no válido")
    raise HTTPException(status_code=400, detail="Petición incorrecta")


@app.post("/webhook")
async def receive_message(request: Request):
    """
    2. Endpoint POST donde Meta nos envía los mensajes que escriben los clientes.
    """
    try:
        raw_body = await request.body()
        body = json.loads(raw_body.decode('utf-8', errors='ignore'))
    except Exception as e:
        print(f"Error decodificando payload: {e}")
        return {"status": "error"}

    # Validar que venga de WhatsApp
    if "object" in body and body["object"] == "whatsapp_business_account":
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                
                # Si hay mensajes en la petición
                if "messages" in value and len(value["messages"]) > 0:
                    msg = value["messages"][0]
                    phone_number_from = msg.get("from") # Número del cliente
                    msg_type = msg.get("type")
                    text_body = ""

                    if msg_type == "text":
                        text_body = msg.get("text", {}).get("body", "")
                        print(f"Mensaje recibido de {phone_number_from}: {text_body}")
                        
                        # 3. Clasificación y asignación en tiempo real!
                        await classify_and_route_message(phone_number_from, text_body)
                    else:
                        print(f"Mensaje de tipo {msg_type} recibido, ignorado por ahora.")
                        
        return {"status": "success"}

    raise HTTPException(status_code=404, detail="No encontrado")


async def classify_and_route_message(phone_from: str, text: str):
    """
    Verifica si el contacto ya tiene departamento. Si no, usa IA, lo asigna 
    y lo envía por WebSocket al frontend.
    """
    department = "recepcion" # Default en caso de fallo de IA
    suggested_reply = ""
    observation = ""
    
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.phone_number == phone_from).first()
        # Si ya existe y tiene departamento reasignado/asginado, omitimos la IA
        if contact and contact.assigned_department:
            department = contact.assigned_department
            observation = contact.observation or ""
            print(f"[{phone_from}] Retornando cliente conocido a su área asignada: {department.upper()}")
        else:
            # Cliente nuevo o sin departamento, corremos Inteligencia Artificial
            print(f"[{phone_from}] Analizando mensaje de cliente sin asignar con IA: '{text}'...")
            
            # --- AUTO-SALUDO IA ---
            saludo_ia = "¡Hola! Bienvenido a MultiChat. Soy tu asistente virtual. Estoy analizando tu solicitud para derivarte al área correcta..."
            auto_reply_result = send_text_to_whatsapp(phone_from, saludo_ia, "recepcion")
            
            auto_reply_payload = {
                "from": phone_from,
                "text": saludo_ia,
                "department": "recepcion",
                "direction": "outbound",
                "created_at": auto_reply_result.get("created_at", "") if auto_reply_result else ""
            }
            await manager.broadcast(auto_reply_payload, "recepcion")
            await manager.broadcast(auto_reply_payload, "todos")

            try:
                response = model.generate_content(
                    text,
                    generation_config=genai.GenerationConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=ClassificationResponse
                    )
                )
                
                result = json.loads(response.text)
                department_candidate = result.get("department", "").strip().lower()
                suggested_reply = result.get("suggested_reply", "")
                
                if department_candidate in ["ventas", "soporte", "recepcion"]:
                    department = department_candidate
                else:
                    department = "recepcion"
                    
            except Exception as e:
                print(f"Fallback. Error llamando a Gemini: {e}")
                
    finally:
        db.close()
            
    print(f"[IA-RUTEO] Asignando mensaje de {phone_from} a {department.upper()}")
    
    # 🌟 NUEVO: Persistimos el mensaje finalizado en SQLite y obtenemos su fecha
    msg_timestamp = save_message_to_db(phone_from, text, "inbound", department)
    
    # Preparar el paquete de datos para mandar al frontend
    message_payload = {
        "from": phone_from,
        "text": text,
        "department": department,
        "suggested_reply": suggested_reply,
        "observation": observation,
        "created_at": msg_timestamp.isoformat() if msg_timestamp else ""
    }
    
    # Enviar al Frontend por WebSocket
    await manager.broadcast(message_payload, department)
    # 🌟 NUEVO: Mándalo también a la sala "todos" para los Admins
    await manager.broadcast(message_payload, "todos")


# ---- APIs PARA HISTORIAL Y CRM ----

@app.get("/api/messages/{department}")
def get_department_history(department: str, db: Session = Depends(get_db)):
    """Devuelve el ultimo mensaje de cada contacto asignado al departamento (o de 'todos')"""
    if department == "todos":
        contacts = db.query(Contact).all()
    else:
        contacts = db.query(Contact).filter(Contact.assigned_department == department).all()
        
    result = []
    for c in contacts:
        # Extraemos TODOS los mensajes de esta conversación ordenados del más antiguo al más nuevo
        msgs = db.query(Message).filter(Message.contact_id == c.id).order_by(Message.created_at.asc()).all()
        
        if msgs:
            # Reconstruimos el array de la historia para mandar al frontend tal como la espera el nuevo JS
            chat_history = []
            for msg in msgs:
                chat_history.append({
                    "from": c.phone_number,
                    "name": c.name,
                    "text": msg.text,
                    "department": c.assigned_department,
                    "direction": msg.direction,
                    "observation": c.observation if msg.direction == "inbound" else ""
                })
            
            # Agregamos esta conversación a la lista global. 
            # El objeto principal en el DOM usará el último elemento para pintar el preview de la izquierda
            # y usará toda la lista interior para pintar el mensaje al abrir.
            # Enviaremos solo una lista aplanada para que el frontend la itere y agrupe
            result.extend(chat_history)
            
    return result

@app.get("/api/admin/conversations")
def get_all_active_conversations(db: Session = Depends(get_db)):
    """Devuelve el estado de TODOS los contactos activos para el Panel de Admin"""
    contacts = db.query(Contact).order_by(Contact.created_at.desc()).all()
    
    result = []
    for c in contacts:
        # Traer solo el texto del ultimo mensaje para previsualizar
        last_msg = db.query(Message).filter(Message.contact_id == c.id).order_by(Message.created_at.desc()).first()
        result.append({
            "phone": c.phone_number,
            "name": c.name or "S/N",
            "department": c.assigned_department or "Desconocido",
            "observation": c.observation or "",
            "last_message": last_msg.text if last_msg else "Sin mensajes",
            "last_active": last_msg.created_at.strftime("%H:%M %d/%m") if last_msg else ""
        })
    return result

@app.delete("/api/admin/clear_db")
def clear_database_records(db: Session = Depends(get_db)):
    """Borra todos los mensajes, tickets y contactos de la DB (Para Demos)"""
    try:
        db.query(Message).delete()
        db.query(Ticket).delete()
        db.query(Contact).delete()
        db.commit()
        return {"status": "success", "message": "Base de datos purgada correctamente."}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/contacts/{phone}/reassign")
async def reassign_conversation(phone: str, payload: ReassignRequest, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.phone_number == phone).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
        
    old_dept = contact.assigned_department
    contact.assigned_department = payload.department
    contact.observation = payload.observation
    db.commit()
    
    # Notificar a los WebSockets (quitar de uno, agregar al otro)
    transfer_payload = {
        "from": phone,
        "name": contact.name,
        "department": payload.department,
        "text": f"🔄 Transferido desde {old_dept.upper()}",
        "observation": payload.observation
    }
    
    # Mandamos al nuevo departamento la advertencia de que llegó
    await manager.broadcast(transfer_payload, payload.department)
    
    # Mandamos al admin si está viendo globalmente (opcional) pero vital quitar del viejo
    if old_dept and old_dept != payload.department:
        remove_payload = {
            "from": phone,
            "type": "removed_chat",
            "department": old_dept 
        }
        await manager.broadcast(remove_payload, old_dept)
        
    return {"status": "success"}

@app.put("/api/contacts/{phone}")
def update_contact_name(phone: str, payload: dict, db: Session = Depends(get_db)):
    """Actualiza el nombre de un contacto en el mini-CRM"""
    contact = db.query(Contact).filter(Contact.phone_number == phone).first()
    if contact and "name" in payload:
        contact.name = payload["name"]
        db.commit()
        return {"status": "success", "name": contact.name}
    raise HTTPException(status_code=404, detail="Contacto no encontrado")

# --- TICKETS ENDPOINTS ---

@app.post("/api/contacts/{phone}/tickets")
async def create_ticket(phone: str, payload: TicketCreateRequest, db: Session = Depends(get_db)):
    """Crea un ticket asociado a un contacto"""
    contact = db.query(Contact).filter(Contact.phone_number == phone).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
        
    new_ticket = Ticket(
        contact_id=contact.id,
        description=payload.description,
        level=payload.level,
        status="abierto"
    )
    db.add(new_ticket)
    db.commit()
    db.refresh(new_ticket)
    
    # Opcional: avisar a admins que un nuevo ticket ingresó
    ticket_payload = {
        "type": "new_ticket",
        "ticket_id": new_ticket.id,
        "contact_phone": contact.phone_number,
        "contact_name": contact.name,
        "level": new_ticket.level
    }
    await manager.broadcast(ticket_payload, "todos")
    
    return {"status": "success", "ticket_id": new_ticket.id}

@app.get("/api/tickets")
def get_all_tickets(db: Session = Depends(get_db)):
    """Devuelve todo los tickets abiertos junto con info del contacto"""
    tickets = db.query(Ticket).filter(Ticket.status == "abierto").order_by(Ticket.created_at.desc()).all()
    
    result = []
    for t in tickets:
        contact = t.contact
        result.append({
            "id": t.id,
            "description": t.description,
            "level": t.level,
            "created_at": t.created_at.isoformat(),
            "contact_phone": contact.phone_number if contact else "Desconocido",
            "contact_name": contact.name if contact else None
        })
    return result

@app.put("/api/tickets/{ticket_id}/close")
def close_ticket(ticket_id: int, db: Session = Depends(get_db)):
    """Cierra un ticket"""
    ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    ticket.status = "cerrado"
    db.commit()
    return {"status": "success"}

@app.post("/api/send")
def api_send_message_to_whatsapp(payload: SendMessageRequest, db: Session = Depends(get_db)):
    """Envia mensaje a Whatsapp usando Graph API desde petición del front (Agente)"""
    return send_text_to_whatsapp(payload.phone_number, payload.text, payload.department)

def send_text_to_whatsapp(phone_number: str, text: str, department: str):
    """Lógica central para enviar un mensaje saliente a través de Graph API y guardarlo"""
    
    # 1. Lo guardamos en el Historial (Saliente)
    msg_timestamp = save_message_to_db(phone_number, text, "outbound", department)
    
    # 2. Validar credenciales de Meta
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID or WHATSAPP_TOKEN == "tu_token_de_acceso_aqui":
        print(f"[SIMULADO] WhatsApp a {phone_number}: {text}")
        return {"status": "success", "simulated": True, "created_at": msg_timestamp.isoformat() if msg_timestamp else ""}
        
    # 3. Llamar al API de Meta Graph
    meta_url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        r = requests.post(meta_url, headers=headers, json=data)
        if r.status_code != 200:
            print(f"Error Meta API: {r.text}")
            raise HTTPException(status_code=400, detail="Error enviando el mensaje a Whatsapp")
            
        print(f"[WHATSAPP ENVIADO EXITOSAMENTE] A {phone_number}: {text}")
        return {"status": "success", "simulated": False, "created_at": msg_timestamp.isoformat() if msg_timestamp else ""}
        
    except requests.exceptions.RequestException as e:
        print(f"Error de conexión con Meta: {e}")
        raise HTTPException(status_code=500, detail="Error de conexion internauta")
