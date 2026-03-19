import csv
import io
import os
import sqlite3
import time
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    send_from_directory,
)
import qrcode


APP_SECRET = os.environ.get("ATTENDANCE_SECRET", "123456")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")
WINDOW_SECONDS = 30
DB_PATH = os.environ.get("ATTENDANCE_DB_PATH", "/tmp/attendance.db")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS roster (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            apellido TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            apellido TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            token_window INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def current_date_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def current_time_str():
    return datetime.now(TZ).strftime("%H:%M:%S")


def current_window():
    return int(time.time() // WINDOW_SECONDS)


def build_token(window_value: int) -> str:
    raw = f"{APP_SECRET}:{window_value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def valid_tokens():
    w = current_window()
    return {
        build_token(w),
        build_token(w - 1),
    }


def make_qr_file(data: str, output_path: str):
    img = qrcode.make(data)
    img.save(output_path)


def normalize_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def student_already_marked(nombre: str, apellido: str, fecha: str) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM attendance
        WHERE lower(nombre) = lower(?)
          AND lower(apellido) = lower(?)
          AND fecha = ?
        LIMIT 1
        """,
        (nombre, apellido, fecha),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


@app.route("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "date": current_date_str(),
            "timezone": "America/Argentina/Buenos_Aires",
            "window_seconds": WINDOW_SECONDS,
            "db_path": DB_PATH,
        }
    )


@app.route("/manifest.webmanifest")
def manifest():
    return jsonify(
        {
            "name": "Asistencia QR",
            "short_name": "Asistencia",
            "start_url": "/admin/qr",
            "display": "standalone",
            "background_color": "#f4f7fb",
            "theme_color": "#111827",
            "icons": [
                {
                    "src": "/static/icons/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                },
                {
                    "src": "/static/icons/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                },
            ],
        }
    )


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/")
def index():
    return redirect(url_for("admin_qr"))


@app.route("/admin/qr")
def admin_qr():
    token = build_token(current_window())
    student_link = request.url_root.rstrip("/") + url_for("student_form", token=token)

    os.makedirs("static", exist_ok=True)
    qr_path = os.path.join("static", "current_qr.png")
    make_qr_file(student_link, qr_path)

    return render_template(
        "qr.html",
        qr_url=url_for("static", filename="current_qr.png"),
        student_link=student_link,
        window_seconds=WINDOW_SECONDS,
    )


@app.route("/attendance")
def student_form():
    token = request.args.get("token", "").strip()
    if not token or token not in valid_tokens():
        return render_template("invalid.html"), 400
    return render_template("student_form.html", token=token)


@app.route("/attendance/submit", methods=["POST"])
def attendance_submit():
    token = request.form.get("token", "").strip()
    nombre = normalize_text(request.form.get("nombre", ""))
    apellido = normalize_text(request.form.get("apellido", ""))

    if token not in valid_tokens():
        return render_template("invalid.html"), 400

    if not nombre or not apellido:
        flash("Completá nombre y apellido.")
        return redirect(url_for("student_form", token=token))

    fecha = current_date_str()
    hora = current_time_str()

    if student_already_marked(nombre, apellido, fecha):
        return render_template(
            "thanks.html",
            nombre=nombre,
            apellido=apellido,
            mensaje="Tu asistencia ya estaba registrada hoy.",
        )

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO attendance (nombre, apellido, fecha, hora, token_window, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            nombre,
            apellido,
            fecha,
            hora,
            current_window(),
            datetime.now(TZ).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    return render_template(
        "thanks.html",
        nombre=nombre,
        apellido=apellido,
        mensaje="Asistencia registrada correctamente.",
    )


@app.route("/admin/roster", methods=["GET", "POST"])
def admin_roster():
    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "upload_csv":
            file = request.files.get("csv_file")
            if not file or not file.filename:
                flash("Seleccioná un archivo CSV.")
                conn.close()
                return redirect(url_for("admin_roster"))

            try:
                content = file.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(content))
                inserted = 0
                for row in reader:
                    nombre = normalize_text(row.get("nombre", ""))
                    apellido = normalize_text(row.get("apellido", ""))
                    if nombre and apellido:
                        cur.execute(
                            "INSERT INTO roster (nombre, apellido) VALUES (?, ?)",
                            (nombre, apellido),
                        )
                        inserted += 1
                conn.commit()
                flash(f"Se importaron {inserted} alumnos.")
            except Exception as e:
                flash(f"Error al importar CSV: {e}")

        elif action == "add_manual":
            nombre = normalize_text(request.form.get("nombre", ""))
            apellido = normalize_text(request.form.get("apellido", ""))
            if not nombre or not apellido:
                flash("Completá nombre y apellido para el alta manual.")
            else:
                cur.execute(
                    "INSERT INTO roster (nombre, apellido) VALUES (?, ?)",
                    (nombre, apellido),
                )
                conn.commit()
                flash("Alumno agregado.")

        elif action == "bulk_paste":
            bulk_text = request.form.get("bulk_text", "")
            inserted = 0
            for line in bulk_text.splitlines():
                line = line.strip()
                if not line:
                    continue

                if "," in line:
                    parts = [p.strip() for p in line.split(",", 1)]
                elif ";" in line:
                    parts = [p.strip() for p in line.split(";", 1)]
                else:
                    continue

                if len(parts) == 2 and parts[0] and parts[1]:
                    cur.execute(
                        "INSERT INTO roster (nombre, apellido) VALUES (?, ?)",
                        (parts[0], parts[1]),
                    )
                    inserted += 1

            conn.commit()
            flash(f"Se agregaron {inserted} alumnos por pegado masivo.")

        elif action == "delete_student":
            student_id = request.form.get("student_id", "").strip()
            if student_id.isdigit():
                cur.execute("DELETE FROM roster WHERE id = ?", (int(student_id),))
                conn.commit()
                flash("Alumno eliminado.")

        elif action == "clear_roster":
            cur.execute("DELETE FROM roster")
            conn.commit()
            flash("Padrón vaciado.")

        conn.close()
        return redirect(url_for("admin_roster"))

    cur.execute("SELECT * FROM roster ORDER BY apellido, nombre")
    roster = cur.fetchall()
    conn.close()
    return render_template("roster.html", roster=roster)


@app.route("/admin/report")
def admin_report():
    fecha = current_date_str()
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT nombre, apellido, hora
        FROM attendance
        WHERE fecha = ?
        ORDER BY apellido, nombre
        """,
        (fecha,),
    )
    presentes = cur.fetchall()

    cur.execute(
        """
        SELECT r.id, r.nombre, r.apellido
        FROM roster r
        WHERE NOT EXISTS (
            SELECT 1
            FROM attendance a
            WHERE lower(a.nombre) = lower(r.nombre)
              AND lower(a.apellido) = lower(r.apellido)
              AND a.fecha = ?
        )
        ORDER BY r.apellido, r.nombre
        """,
        (fecha,),
    )
    ausentes = cur.fetchall()

    conn.close()

    return render_template(
        "report.html",
        fecha=fecha,
        presentes=presentes,
        ausentes=ausentes,
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
