import sqlite3
import os

DB_NAME = "peluqueria.db"

def crear_base_datos():
    # Si la base vieja existe, la borramos para crear la nueva versión con Memoria
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)

    conexion = sqlite3.connect(DB_NAME)
    cursor = conexion.cursor()

    # 1. Tabla Clientes (¡AHORA CON MEMORIA DE ESTADO!)
    cursor.executescript('''
        CREATE TABLE Clientes (
            id_cliente INTEGER PRIMARY KEY AUTOINCREMENT,
            telefono TEXT UNIQUE NOT NULL,
            nombre TEXT,
            estado_bot TEXT DEFAULT 'normal',
            contexto_bot TEXT DEFAULT NULL,
            fecha_registro DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE Servicios (
            id_servicio INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_servicio TEXT NOT NULL,
            duracion_minutos INTEGER NOT NULL,
            precio REAL NOT NULL,
            id_servicio_asociado INTEGER,
            FOREIGN KEY (id_servicio_asociado) REFERENCES Servicios(id_servicio)
        );

        CREATE TABLE Turnos (
            id_turno INTEGER PRIMARY KEY AUTOINCREMENT,
            id_cliente INTEGER NOT NULL,
            id_servicio INTEGER NOT NULL,
            fecha_hora DATETIME NOT NULL,
            estado TEXT DEFAULT 'Pendiente',
            FOREIGN KEY (id_cliente) REFERENCES Clientes(id_cliente),
            FOREIGN KEY (id_servicio) REFERENCES Servicios(id_servicio)
        );
    ''')

    conexion.commit()
    conexion.close()
    print("Base de datos V2 (Con Memoria) creada con éxito.")

def insertar_servicios_prueba():
    conexion = sqlite3.connect(DB_NAME)
    cursor = conexion.cursor()

    servicios = [
        ("Corte clásico", 30, 5000.0, None), 
        ("Baño de crema nutritivo", 15, 2500.0, None),
        ("Coloración completa", 120, 15000.0, 2) 
    ]

    try:
        cursor.executemany('''
            INSERT INTO Servicios (nombre_servicio, duracion_minutos, precio, id_servicio_asociado)
            VALUES (?, ?, ?, ?)
        ''', servicios)
        conexion.commit()
        print("Servicios insertados.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conexion.close()

if __name__ == "__main__":
    crear_base_datos()
    insertar_servicios_prueba()