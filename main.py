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
    
    ahora = datetime.now()
    fecha_actual_str = ahora.strftime("%Y-%m-%d %H:%M:%S")
    
    conexion = sqlite3.connect("peluqueria.db")
    cursor = conexion.cursor()
    
    # 1. RECUPERAR MEMORIA DEL CLIENTE
    cursor.execute("SELECT id_cliente, estado_bot, contexto_bot, historial, ultima_interaccion FROM Clientes WHERE telefono = ?", (numero_origen,))
    cliente_db = cursor.fetchone()
    
    if not cliente_db:
        historial_inicial = json.dumps([])
        cursor.execute("INSERT INTO Clientes (telefono, nombre, historial, ultima_interaccion) VALUES (?, ?, ?, ?)", (numero_origen, "Cliente", historial_inicial, fecha_actual_str))
        conexion.commit()
        id_cliente, estado_bot, contexto_bot, historial_json, ultima_interaccion = cursor.lastrowid, "normal", None, historial_inicial, fecha_actual_str
    else:
        id_cliente, estado_bot, contexto_bot, historial_json, ultima_interaccion = cliente_db

    # 2. CONTROL DE TIEMPO (Regla de los 10 minutos)
    if ultima_interaccion:
        ultima_fecha = datetime.strptime(ultima_interaccion, "%Y-%m-%d %H:%M:%S")
        if (ahora - ultima_fecha) > timedelta(minutes=10):
            # Si pasaron más de 10 minutos, le borramos la memoria y reseteamos el estado
            historial_json = "[]"
            estado_bot = "normal"
            contexto_bot = None

    historial = json.loads(historial_json if historial_json else "[]")
    respuesta_bot = ""

    # --- LA MÁQUINA DE ESTADOS ---
    if estado_bot == "esperando_cross_sell":
        prompt_afirmacion = f"El cliente respondió: '{texto_mensaje}'. ¿Es una afirmación o negación? Respondé SOLO la palabra AFIRMACION o NEGACION."
        ia_estado = modelo_ia.generate_content(prompt_afirmacion).text.strip().upper()
        
        if "AFIRMACION" in ia_estado:
            if contexto_bot:
                id_asociado, fecha_inicio_extra = contexto_bot.split("|")
                cursor.execute("INSERT INTO Turnos (id_cliente, id_servicio, fecha_hora) VALUES (?, ?, ?)", (id_cliente, id_asociado, fecha_inicio_extra))
                respuesta_bot = "¡Perfecto! Ya lo sumé a tu turno. ¡Nos vemos!"
            else:
                respuesta_bot = "Hubo un problema sumando el servicio, pero el turno original sigue en pie."
        else:
            respuesta_bot = "¡No hay problema! Queda confirmado solo tu turno original. ¡Te esperamos!"
            
        estado_bot = "normal"
        contexto_bot = None

    else:
        # Formateamos el historial para que la IA entienda el contexto
        historial_texto = "\n".join([f"{msg['rol']}: {msg['texto']}" for msg in historial])
        
        cursor.execute("SELECT nombre_servicio FROM Servicios")
        lista_servicios = [fila[0] for fila in cursor.fetchall()]
        
        prompt_sistema = f"""
        Hoy es: {ahora.strftime('%Y-%m-%d %H:%M')}. Servicios: {', '.join(lista_servicios)}.
        
        HISTORIAL DE LA CHARLA RECIENTE (Usalo para entender el contexto):
        {historial_texto}
        
        NUEVO MENSAJE DEL CLIENTE: "{texto_mensaje}"
        
        Tu tarea es entender qué quiere el cliente basándote en TODA la charla. 
        Si el cliente dice "a la tarde" o "mañana", cruzá esa info con el servicio y fecha que venían hablando.
        
        Devolvé ÚNICAMENTE un JSON con:
        {{
            "intencion": "agendar" o "consulta",
            "servicio": "nombre del servicio EXACTO" o null,
            "fecha_exacta": "YYYY-MM-DD HH:MM:SS" o null,
            "preferencia_dia": "mañana" o "tarde" o null,
            "solo_fecha": "YYYY-MM-DD" o null
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
                    
                    if fecha_exacta:
                        if esta_disponible(cursor, fecha_exacta, duracion):
                            cursor.execute("INSERT INTO Turnos (id_cliente, id_servicio, fecha_hora) VALUES (?, ?, ?)", (id_cliente, id_servicio, fecha_exacta))
                            respuesta_bot = f"¡Confirmado! Turno para {nombre_servicio} el {fecha_exacta}."
                            
                            if id_asociado:
                                cursor.execute("SELECT nombre_servicio, precio FROM Servicios WHERE id_servicio = ?", (id_asociado,))
                                asoc_db = cursor.fetchone()
                                if asoc_db:
                                    inicio_original = datetime.strptime(fecha_exacta, "%Y-%m-%d %H:%M:%S")
                                    fin_original = inicio_original + timedelta(minutes=duracion)
                                    fecha_inicio_extra = fin_original.strftime("%Y-%m-%d %H:%M:%S")
                                    
                                    contexto_bot = f"{id_asociado}|{fecha_inicio_extra}"
                                    estado_bot = "esperando_cross_sell"
                                    
                                    respuesta_bot += f"\n\n💡 Por ${asoc_db[1]} más, podemos sumarte un {asoc_db[0]} justo a continuación. ¿Te lo agrego?"
                        else:
                            respuesta_bot = "Uy, ese horario ya está ocupado. Decime si preferís buscar otro hueco a la mañana o a la tarde de ese mismo día."
                    
                    elif datos.get("solo_fecha") and datos.get("preferencia_dia"):
                        fecha = datos.get("solo_fecha")
                        pref = datos.get("preferencia_dia")
                        sugerencias = sugerir_horarios(cursor, fecha, pref, duracion)
                        
                        if sugerencias:
                            respuesta_bot = f"Para el {fecha} a la {pref} tengo estos horarios libres: {', '.join(sugerencias)}. ¿Cuál te sirve más?"
                        else:
                            respuesta_bot = f"Perdón, para el {fecha} a la {pref} ya no me quedan turnos para ese servicio. ¿Buscamos para otro día?"
                    else:
                        respuesta_bot = "¿Para qué día lo buscabas? ¿Preferís un turno a la mañana o a la tarde?"
                else:
                    respuesta_bot = "No tengo ese servicio. ¿Me lo repetís?"
            else:
                respuesta_bot = "¿Qué servicio buscabas agendar?"
        else:
            respuesta_bot = "¡Hola! ¿En qué te puedo ayudar hoy?"

    # 3. ACTUALIZAR MEMORIA Y CERRAR
    historial.append({"rol": "Cliente", "texto": texto_mensaje})
    historial.append({"rol": "Bot", "texto": respuesta_bot})
    
    # Guardamos solo los últimos 6 mensajes (3 idas y vueltas) para que la IA no se sobrecargue
    historial = historial[-6:]
    
    cursor.execute('''
        UPDATE Clientes 
        SET estado_bot = ?, contexto_bot = ?, historial = ?, ultima_interaccion = ? 
        WHERE id_cliente = ?
    ''', (estado_bot, contexto_bot, json.dumps(historial), fecha_actual_str, id_cliente))
    
    conexion.commit()
    conexion.close()
    
    xml_response = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{respuesta_bot}</Message></Response>'
    return Response(content=xml_response, media_type="application/xml")