import sqlite3
import os
import json
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

# --- CONFIGURACIÓN INICIAL ---
# 1. Cargamos las variables ocultas del archivo .env
load_dotenv()

# 2. Le pasamos nuestra llave secreta a Google
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# 3. Elegimos el modelo de IA (Flash es rapidísimo e ideal para tareas de texto)
modelo_ia = genai.GenerativeModel('gemini-2.5-flash')

app = FastAPI(title="API Recepcionista 24/7 con IA")

# --- MODELOS DE DATOS PYDANTIC ---
class ClienteNuevo(BaseModel):
    telefono: str
    nombre: str

class TurnoNuevo(BaseModel):
    id_cliente: int
    id_servicio: int
    fecha_hora: datetime 

class MensajeWhatsApp(BaseModel):
    numero_origen: str
    texto_mensaje: str

# --- ENDPOINTS BÁSICOS (Los que ya funcionaban perfecto) ---
@app.get("/")
def estado_servidor():
    return {"status": "online", "mensaje": "¡El servidor del bot está vivo!"}

@app.get("/servicios")
def obtener_servicios():
    conexion = sqlite3.connect("peluqueria.db")
    conexion.row_factory = sqlite3.Row 
    cursor = conexion.cursor()
    cursor.execute("SELECT * FROM Servicios")
    servicios_db = cursor.fetchall()
    conexion.close()
    return [{"id": s["id_servicio"], "nombre": s["nombre_servicio"], "duracion_minutos": s["duracion_minutos"], "precio": s["precio"]} for s in servicios_db]

@app.post("/clientes")
def crear_cliente(cliente: ClienteNuevo):
    conexion = sqlite3.connect("peluqueria.db")
    cursor = conexion.cursor()
    try:
        cursor.execute("INSERT INTO Clientes (telefono, nombre) VALUES (?, ?)", (cliente.telefono, cliente.nombre))
        conexion.commit()
        return {"mensaje": "Cliente creado", "id_cliente": cursor.lastrowid}
    except sqlite3.IntegrityError:
        conexion.rollback()
        raise HTTPException(status_code=400, detail="El teléfono ya está registrado.")
    finally:
        conexion.close()

@app.get("/turnos")
def obtener_turnos():
    conexion = sqlite3.connect("peluqueria.db")
    conexion.row_factory = sqlite3.Row 
    cursor = conexion.cursor()
    
    # Hacemos una consulta SQL para traer los turnos
    cursor.execute("SELECT * FROM Turnos")
    turnos_db = cursor.fetchall()
    conexion.close()

    # Formateamos los datos
    resultado = []
    for t in turnos_db:
        resultado.append({
            "id_turno": t["id_turno"],
            "id_cliente": t["id_cliente"],
            "id_servicio": t["id_servicio"],
            "fecha_hora": t["fecha_hora"],
            "estado": t["estado"]
        })
        
    return resultado

# --- NUEVO WEBHOOK CON CEREBRO DE IA ---
@app.post("/webhook")
async def recibir_mensaje(request: Request):
    # 1. Twilio manda los datos como formulario web, los atrapamos así:
    form_data = await request.form()
    numero_origen = form_data.get("From", "")
    texto_mensaje = form_data.get("Body", "")
    
    print(f"\n--- NUEVO MENSAJE DE: {numero_origen} ---")
    
    conexion = sqlite3.connect("peluqueria.db")
    cursor = conexion.cursor()
    cursor.execute("SELECT nombre_servicio FROM Servicios")
    lista_servicios = [fila[0] for fila in cursor.fetchall()]
    
    fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt_sistema = f"""
    Sos el recepcionista virtual de una peluquería. TENÉ EN CUENTA QUE HOY ES: {fecha_actual}.
    Los servicios que ofrecemos son: {', '.join(lista_servicios)}.
    
    El cliente te escribió: "{texto_mensaje}"
    
    Extraé la información y devolvela ÚNICAMENTE en este JSON exacto:
    {{
        "intencion": "agendar" o "saludo" o "consulta",
        "servicio": "nombre del servicio solicitado" o null,
        "fecha_hora_formateada": "YYYY-MM-DD HH:MM:SS" o null
    }}
    """

    respuesta_ia = modelo_ia.generate_content(prompt_sistema)
    
    try:
        texto_limpio = respuesta_ia.text.strip().replace("```json", "").replace("```", "")
        datos_extraidos = json.loads(texto_limpio)
    except json.JSONDecodeError:
        # Si falla, devolvemos un XML de error para Twilio
        return Response(content="<Response><Message>Error interno al leer el mensaje.</Message></Response>", media_type="application/xml")

    respuesta_bot = ""

    if datos_extraidos.get("intencion") == "agendar":
        nombre_servicio = datos_extraidos.get("servicio")
        fecha_solicitada = datos_extraidos.get("fecha_hora_formateada")

        if nombre_servicio and fecha_solicitada:
            cursor.execute("SELECT id_cliente FROM Clientes WHERE telefono = ?", (numero_origen,))
            cliente_db = cursor.fetchone()
            if not cliente_db:
                cursor.execute("INSERT INTO Clientes (telefono, nombre) VALUES (?, ?)", (numero_origen, "Cliente Nuevo"))
                id_cliente = cursor.lastrowid
            else:
                id_cliente = cliente_db[0]

            cursor.execute("SELECT id_servicio, id_servicio_asociado FROM Servicios WHERE nombre_servicio = ?", (nombre_servicio,))
            servicio_db = cursor.fetchone()

            if servicio_db:
                id_servicio = servicio_db[0]
                id_asociado = servicio_db[1]
                
                cursor.execute('''
                    INSERT INTO Turnos (id_cliente, id_servicio, fecha_hora)
                    VALUES (?, ?, ?)
                ''', (id_cliente, id_servicio, fecha_solicitada))
                conexion.commit()
                
                respuesta_bot = f"¡Perfecto! Tu turno para {nombre_servicio} quedó confirmado para el {fecha_solicitada}."

                if id_asociado is not None:
                    cursor.execute("SELECT nombre_servicio, precio FROM Servicios WHERE id_servicio = ?", (id_asociado,))
                    asociado_db = cursor.fetchone()
                    if asociado_db:
                        respuesta_bot += f"\n\n💡 Aprovecho para contarte que con la {nombre_servicio} solemos recomendar un {asociado_db[0]} por ${asociado_db[1]}. ¿Te gustaría que lo sumemos?"
            else:
                respuesta_bot = "Perdón, no encontré ese servicio en nuestra lista. ¿Me lo repetís?"
        else:
            respuesta_bot = "Me faltan algunos datos. ¿Me confirmás qué servicio buscás y a qué hora lo querés?"
            
    elif datos_extraidos.get("intencion") == "consulta":
        respuesta_bot = "¡Hola! Por ahora soy un asistente en entrenamiento. A la brevedad te responde un humano."
    else:
        respuesta_bot = "¡Hola! ¿En qué te puedo ayudar hoy?"

    conexion.close()
    
    print(f"NUESTRO BOT RESPONDERÍA: {respuesta_bot}")
    
    # 2. EL CAMBIO FINAL: Le respondemos a Twilio en formato XML (TwiML)
    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Message>{respuesta_bot}</Message>
    </Response>"""
    
    return Response(content=xml_response, media_type="application/xml")