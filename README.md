# Asistencia QR para iPhone

Esta versión está optimizada como **web app instalable en iPhone (PWA)**.

## Qué hace

- muestra un **QR que cambia cada 30 segundos**
- el alumno escanea y completa **nombre y apellido**
- registra la asistencia del día
- permite importar un padrón por CSV
- muestra **presentes y ausentes**
- se puede **instalar en la pantalla de inicio del iPhone**

## Requisitos

- Python 3.10 o superior
- iPhone y computadora conectados a la **misma Wi‑Fi**

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecutar

```bash
python app.py
```

La app va a mostrar dos direcciones:

- una para abrir en la computadora
- otra para abrir desde el iPhone en la misma red

Ejemplo:

```bash
Asistencia QR lista para iPhone
En esta computadora: http://127.0.0.1:5000
Desde tu iPhone en la misma Wi-Fi: http://192.168.1.15:5000
```

## Instalar en iPhone

1. Abrí en **Safari** la dirección local que muestra la terminal.
2. Tocá **Compartir**.
3. Elegí **Agregar a pantalla de inicio**.
4. Ya queda como app tipo iPhone.

## Pantallas

- `/` → inicio
- `/admin/qr` → QR rotativo
- `/admin/roster` → importar padrón
- `/admin/report` → presentes y ausentes

## Formato del CSV

```csv
nombre,apellido
Ana,García
Juan,Pérez
```

## Seguridad básica

- el QR cambia cada 30 segundos
- el token vence automáticamente
- para mejorar seguridad, cambiá las variables de entorno:

```bash
export ATTENDANCE_SECRET="una-clave-larga-y-unica"
export FLASK_SECRET_KEY="otra-clave-segura"
python app.py
```

## Límite actual

- sigue registrando asistencia por **día**
- no tiene todavía login docente ni cursos/comisiones

## Próximas mejoras posibles

- materias y comisiones
- cierre por horario de clase
- panel docente con PIN
- exportación Excel
- despliegue online en Render o Railway
