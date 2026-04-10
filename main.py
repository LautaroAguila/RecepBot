import sqlite3
import os
import json
from fastapi import FastAPI, Request, Response
from datetime import datetime, timedelta
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
modelo_ia = genai.GenerativeModel('gemini-2.5-flash')

app = FastAPI(title="Recepcionista Bot - V2 Producción")

# --- 1. MOTOR DE DISPONIBILIDAD (La matemática de los turnos) ---

def esta_disponible(cursor, fecha_hora_solicitada, duracion_minutos):
    """Revisa si un horario se pisa con un turno ya existente."""
    inicio_nuevo = datetime.strptime(fecha_hora_solicitada, "%Y-%m-%d %H:%M:%S")
    fin_nuevo = inicio_nuevo + timedelta(minutes=duracion_minutos)
    
    # Buscamos los turnos de ese mismo día
    fecha_dia = inicio_nuevo.strftime("%Y-%m-%d")
    cursor.execute('''
        SELECT Turnos.fecha_hora, Servicios.duracion_minutos 
        FROM Turnos 
        JOIN Servicios ON Turnos.id_servicio = Servicios.id_servicio
        WHERE date(Turnos.fecha_hora) = ?
    ''', (fecha_dia,))
    
    turnos_dia = cursor.fetchall()
    
    for turno in turnos_dia:
        inicio_existente = datetime.strptime(turno[0], "%Y-%m-%d %H:%M:%S")
        fin_existente = inicio_existente + timedelta(minutes=turno[1])
        
        # Lógica de solapamiento: (InicioA < FinB) y (FinA > InicioB)
        if inicio_nuevo < fin_existente and fin_nuevo > inicio_existente:
            return False # Está ocupado
            
    return True # Está libre

def sugerir_horarios(cursor, fecha_str, preferencia, duracion_minutos):
    """Busca 3 huecos libres segun la preferencia (mañana o tarde)"""
    sugerencias = []
    
    if preferencia == "mañana":
        hora_actual = datetime.strptime(f"{fecha_str} 09:00:00", "%Y-%m-%d %H:%M:%S")
        limite = datetime.strptime(f"{fecha_str} 12:00:00", "%Y-%m-%d %H:%M:%S")
    else: # tarde
        hora_actual = datetime.strptime(f"{fecha_str} 13:00:00", "%Y-%m-%d %H:%M:%S")
        limite = datetime.strptime(f"{fecha_str} 19:00:00", "%Y-%m-%d %H:%M:%S")

    # Saltamos de a 30 minutos buscando huecos libres
    while hora_actual < limite and len(sugerencias) < 3:
        if esta_disponible(cursor, hora_actual.strftime("%Y-%m-%d %H:%M:%S"), duracion_minutos):
            sugerencias.append(hora_actual.strftime("%H:%M"))
        hora_actual += timedelta(minutes=30)
        
    return sugerencias

# --- 2. EL WEBHOOK PRINCIPAL ---

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    form_data = await request.form()
    numero_origen = form_data.get("From", "")
    texto_mensaje = form_data.get("Body", "")
    
    conexion = sqlite3.connect("peluqueria.db")
    cursor = conexion.cursor()
    
    # Buscamos al cliente o lo creamos
    cursor.execute("SELECT id_cliente, estado_bot, contexto_bot FROM Clientes WHERE telefono = ?", (numero_origen,))
    cliente_db = cursor.fetchone()
    if not cliente_db:
        cursor.execute("INSERT INTO Clientes (telefono, nombre) VALUES (?, ?)", (numero_origen, "Cliente"))
        conexion.commit()
        id_cliente, estado_bot, contexto_bot = cursor.lastrowid, "normal", None
    else:
        id_cliente, estado_bot, contexto_bot = cliente_db

    # --- LA MÁQUINA DE ESTADOS ---
    respuesta_bot = ""

    # CASO A: El bot le había ofrecido una venta extra en el mensaje anterior
    if estado_bot == "esperando_cross_sell":
        # IA simplificada para leer un sí o un no
        prompt_afirmacion = f"El cliente respondió esto: '{texto_mensaje}'. ¿Es una afirmación o una negación? Respondé SOLO la palabra AFIRMACION o NEGACION."
        ia_estado = modelo_ia.generate_content(prompt_afirmacion).text.strip().upper()
        
        if "AFIRMACION" in ia_estado:
            # Recuperamos el contexto: "ID_SERVICIO_EXTRA|FECHA_HORA_DONDE_EMPIEZA"
            if contexto_bot:
                id_asociado, fecha_inicio_extra = contexto_bot.split("|")
                cursor.execute("INSERT INTO Turnos (id_cliente, id_servicio, fecha_hora) VALUES (?, ?, ?)", (id_cliente, id_asociado, fecha_inicio_extra))
                respuesta_bot = "¡Perfecto! Ya lo sumé a tu turno. ¡Nos vemos!"
            else:
                respuesta_bot = "Hubo un problema sumando el servicio, pero el turno original sigue en pie."
        else:
            respuesta_bot = "¡No hay problema! Queda confirmado solo tu turno original. ¡Te esperamos!"
            
        # Volvemos al estado normal y limpiamos la memoria
        cursor.execute("UPDATE Clientes SET estado_bot = 'normal', contexto_bot = NULL WHERE id_cliente = ?", (id_cliente,))
        conexion.commit()

    # CASO B: Es una conversación normal
    else:
        cursor.execute("SELECT nombre_servicio FROM Servicios")
        lista_servicios = [fila[0] for fila in cursor.fetchall()]
        
        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt_sistema = f"""
        Hoy es: {fecha_actual}. Servicios: {', '.join(lista_servicios)}.
        Cliente dice: "{texto_mensaje}"
        
        Devolvé ÚNICAMENTE un JSON con:
        {{
            "intencion": "agendar" o "consulta",
            "servicio": "nombre del servicio" o null,
            "fecha_exacta": "YYYY-MM-DD HH:MM:SS" o null,
            "preferencia_dia": "mañana" o "tarde" o null,
            "solo_fecha": "YYYY-MM-DD" o null (usar si da el día pero no la hora)
        }}
        """
        
        try:
            respuesta_ia = modelo_ia.generate_content(prompt_sistema)
            datos = json.loads(respuesta_ia.text.strip().replace("```json", "").replace("```", ""))
        except:
            return Response(content="<Response><Message>Error interno.</Message></Response>", media_type="application/xml")

        if datos.get("intencion") == "agendar":
            nombre_servicio = datos.get("servicio")
            
            if nombre_servicio:
                cursor.execute("SELECT id_servicio, duracion_minutos, id_servicio_asociado FROM Servicios WHERE nombre_servicio = ?", (nombre_servicio,))
                serv = cursor.fetchone()
                
                if serv:
                    id_servicio, duracion, id_asociado = serv
                    fecha_exacta = datos.get("fecha_exacta")
                    
                    # 1. Dio el horario exacto
                    if fecha_exacta:
                        if esta_disponible(cursor, fecha_exacta, duracion):
                            # ¡Está libre! Lo agendamos
                            cursor.execute("INSERT INTO Turnos (id_cliente, id_servicio, fecha_hora) VALUES (?, ?, ?)", (id_cliente, id_servicio, fecha_exacta))
                            respuesta_bot = f"¡Confirmado! Turno para {nombre_servicio} el {fecha_exacta}."
                            
                            # Venta cruzada
                            if id_asociado:
                                cursor.execute("SELECT nombre_servicio, precio FROM Servicios WHERE id_servicio = ?", (id_asociado,))
                                asoc_db = cursor.fetchone()
                                if asoc_db:
                                    # Calculamos a qué hora termina el primer turno para enganchar el segundo
                                    inicio_original = datetime.strptime(fecha_exacta, "%Y-%m-%d %H:%M:%S")
                                    fin_original = inicio_original + timedelta(minutes=duracion)
                                    fecha_inicio_extra = fin_original.strftime("%Y-%m-%d %H:%M:%S")
                                    
                                    # Guardamos la memoria para el próximo mensaje
                                    contexto = f"{id_asociado}|{fecha_inicio_extra}"
                                    cursor.execute("UPDATE Clientes SET estado_bot = 'esperando_cross_sell', contexto_bot = ? WHERE id_cliente = ?", (contexto, id_cliente))
                                    
                                    respuesta_bot += f"\n\n💡 Por ${asoc_db[1]} más, podemos sumarte un {asoc_db[0]} justo a continuación. ¿Te lo agrego?"
                            conexion.commit()
                        else:
                            # ¡Está ocupado!
                            respuesta_bot = "Uy, ese horario ya está ocupado. Decime si preferís buscar otro hueco a la mañana o a la tarde de ese mismo día."
                    
                    # 2. No dio la hora, pero dio la preferencia (mañana/tarde) y el día
                    elif datos.get("solo_fecha") and datos.get("preferencia_dia"):
                        fecha = datos.get("solo_fecha")
                        pref = datos.get("preferencia_dia")
                        sugerencias = sugerir_horarios(cursor, fecha, pref, duracion)
                        
                        if sugerencias:
                            respuesta_bot = f"Para el {fecha} a la {pref} tengo estos horarios libres: {', '.join(sugerencias)}. ¿Cuál te sirve más?"
                        else:
                            respuesta_bot = f"Perdón, para el {fecha} a la {pref} ya no me quedan turnos para ese servicio. ¿Buscamos para otro día?"
                    
                    # 3. Faltan datos temporales
                    else:
                        respuesta_bot = "¿Para qué día lo buscabas? ¿Preferís un turno a la mañana o a la tarde?"
                else:
                    respuesta_bot = "No tengo ese servicio. ¿Me lo repetís?"
            else:
                respuesta_bot = "¿Qué servicio buscabas agendar?"

        else:
            respuesta_bot = "¡Hola! ¿En qué te puedo ayudar hoy? (Podés pedirme un turno indicando el servicio y si preferís a la mañana o a la tarde)."

    conexion.close()
    
    xml_response = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{respuesta_bot}</Message></Response>'
    return Response(content=xml_response, media_type="application/xml")