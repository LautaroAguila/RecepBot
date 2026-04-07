import sqlite3

# Definimos el nombre del archivo de la base de datos
DB_NAME = "peluqueria.db"

def crear_base_datos():
    # Nos conectamos (si el archivo no existe, SQLite lo crea automáticamente)
    conexion = sqlite3.connect(DB_NAME)
    cursor = conexion.cursor()

    # Creamos las tablas usando nuestro esquema
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS Clientes (
            id_cliente INTEGER PRIMARY KEY AUTOINCREMENT,
            telefono TEXT UNIQUE NOT NULL,
            nombre TEXT,
            fecha_registro DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS Servicios (
            id_servicio INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_servicio TEXT NOT NULL,
            duracion_minutos INTEGER NOT NULL,
            precio REAL NOT NULL,
            id_servicio_asociado INTEGER,
            FOREIGN KEY (id_servicio_asociado) REFERENCES Servicios(id_servicio)
        );

        CREATE TABLE IF NOT EXISTS Turnos (
            id_turno INTEGER PRIMARY KEY AUTOINCREMENT,
            id_cliente INTEGER NOT NULL,
            id_servicio INTEGER NOT NULL,
            fecha_hora DATETIME NOT NULL,
            estado TEXT DEFAULT 'Pendiente',
            FOREIGN KEY (id_cliente) REFERENCES Clientes(id_cliente),
            FOREIGN KEY (id_servicio) REFERENCES Servicios(id_servicio)
        );
    ''')

    # Guardamos los cambios y cerramos la conexión
    conexion.commit()
    conexion.close()
    print("Base de datos y tablas creadas con éxito.")

# Este bloque hace que la función solo corra si ejecutamos este archivo directamente


def insertar_servicios_prueba():
    conexion = sqlite3.connect(DB_NAME)
    cursor = conexion.cursor()

    # Lista de servicios: (Nombre, Duración, Precio, ID_CrossSelling)
    servicios = [
        ("Corte clásico", 30, 5000.0, None), 
        ("Baño de crema nutritivo", 15, 2500.0, None),
        ("Coloración completa", 120, 15000.0, 2) # El "2" es el ID del baño de crema. ¡Acá está la magia de la venta cruzada!
    ]

    try:
        # Usamos executemany para insertar varios registros de una sola vez
        cursor.executemany('''
            INSERT INTO Servicios (nombre_servicio, duracion_minutos, precio, id_servicio_asociado)
            VALUES (?, ?, ?, ?)
        ''', servicios)
        
        conexion.commit()
        print("¡Servicios de prueba insertados con éxito!")
    except sqlite3.IntegrityError:
        print("Los servicios ya estaban insertados o hubo un error.")
    finally:
        conexion.close()

if __name__ == "__main__":
    #crear_base_datos()
    insertar_servicios_prueba()