from __future__ import annotations
import csv
import hashlib
import io
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
from flask import Flask, request, redirect, url_for, render_template, send_file, g, flash, jsonify

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = Path('/tmp/attendance.db')
DB_PATH = Path(os.environ.get('ATTENDANCE_DB_PATH', str(DEFAULT_DB)))
WINDOW_SECONDS = int(os.environ.get('WINDOW_SECONDS', '30'))
SECRET = os.environ.get('ATTENDANCE_SECRET', 'cambiar-esta-clave')
APP_TZ = ZoneInfo(os.environ.get('APP_TIMEZONE', 'America/Argentina/Buenos_Aires'))

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'dev-secret')


def now_local() -> datetime:
    return datetime.now(APP_TZ)


def get_db():
    if 'db' not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS roster (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            apellido TEXT NOT NULL,
            UNIQUE(nombre, apellido)
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            nombre TEXT NOT NULL,
            apellido TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(fecha, nombre, apellido)
        );
        """
    )
    db.commit()
    db.close()


def normalize(text: str) -> str:
    return ' '.join((text or '').strip().split())


def today_str() -> str:
    return now_local().strftime('%Y-%m-%d')


def current_window() -> int:
    return int(now_local().timestamp() // WINDOW_SECONDS)


def build_token(window: int | None = None) -> str:
    window = current_window() if window is None else window
    day = now_local().strftime('%Y%m%d')
    raw = f'{SECRET}:{day}:{window}'.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:20]


def valid_token(token: str) -> bool:
    win = current_window()
    return token in {build_token(win), build_token(win - 1), build_token(win + 1)}


def student_link(token: str | None = None) -> str:
    tok = token or build_token()
    return url_for('student_form', token=tok, _external=True)


@app.route('/')
def home():
    return redirect(url_for('report'))


@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'date': today_str(),
        'window_seconds': WINDOW_SECONDS,
        'timezone': str(APP_TZ),
        'db_path': str(DB_PATH),
    })


@app.route('/admin/qr')
def admin_qr():
    token = build_token()
    link = student_link(token)
    qr_img_url = f"https://api.qrserver.com/v1/create-qr-code/?size=320x320&data={quote(link, safe='')}"
    seconds_left = WINDOW_SECONDS - (int(now_local().timestamp()) % WINDOW_SECONDS)
    return render_template('qr.html', link=link, token=token, qr_img_url=qr_img_url, seconds_left=seconds_left)


@app.route('/s/<token>', methods=['GET', 'POST'])
def student_form(token: str):
    if not valid_token(token):
        return render_template('invalid.html'), 403

    if request.method == 'POST':
        nombre = normalize(request.form.get('nombre', ''))
        apellido = normalize(request.form.get('apellido', ''))
        if not nombre or not apellido:
            flash('Completá nombre y apellido.')
            return render_template('student_form.html')

        db = get_db()
        try:
            db.execute(
                'INSERT OR IGNORE INTO attendance (fecha, nombre, apellido, created_at) VALUES (?, ?, ?, ?)',
                (today_str(), nombre, apellido, now_local().isoformat(timespec='seconds')),
            )
            db.commit()
        except sqlite3.Error:
            flash('No se pudo registrar la asistencia.')
            return render_template('student_form.html')
        return render_template('thanks.html', nombre=nombre, apellido=apellido)

    return render_template('student_form.html')


@app.route('/admin/roster', methods=['GET', 'POST'])
def roster():
    db = get_db()
    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename:
            flash('Elegí un CSV con columnas nombre,apellido.')
            return redirect(url_for('roster'))

        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        rows = []
        for row in reader:
            nombre = normalize(row.get('nombre', ''))
            apellido = normalize(row.get('apellido', ''))
            if nombre and apellido:
                rows.append((nombre, apellido))
        db.execute('DELETE FROM roster')
        db.executemany('INSERT OR IGNORE INTO roster (nombre, apellido) VALUES (?, ?)', rows)
        db.commit()
        flash(f'Se cargaron {len(rows)} alumnos.')
        return redirect(url_for('roster'))

    roster_rows = db.execute('SELECT nombre, apellido FROM roster ORDER BY apellido, nombre').fetchall()
    return render_template('roster.html', roster=roster_rows)


@app.route('/admin/report')
def report():
    db = get_db()
    fecha = request.args.get('fecha', today_str())
    present = db.execute(
        'SELECT nombre, apellido, created_at FROM attendance WHERE fecha = ? ORDER BY apellido, nombre', (fecha,)
    ).fetchall()
    roster_rows = db.execute('SELECT nombre, apellido FROM roster ORDER BY apellido, nombre').fetchall()
    present_set = {(r['nombre'], r['apellido']) for r in present}
    absent = [r for r in roster_rows if (r['nombre'], r['apellido']) not in present_set]
    return render_template('report.html', fecha=fecha, present=present, absent=absent)


@app.route('/admin/export')
def export_csv():
    db = get_db()
    fecha = request.args.get('fecha', today_str())
    rows = db.execute(
        'SELECT nombre, apellido, created_at FROM attendance WHERE fecha = ? ORDER BY apellido, nombre', (fecha,)
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['fecha', 'nombre', 'apellido', 'registrado_en'])
    for r in rows:
        writer.writerow([fecha, r['nombre'], r['apellido'], r['created_at']])
    mem = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=f'presentes_{fecha}.csv')


init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=True)
