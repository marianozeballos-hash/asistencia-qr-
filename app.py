import csv
import io
import os
import socket
import sqlite3
from datetime import datetime, date

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
import pyotp
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'attendance.db')
SECRET = os.environ.get('ATTENDANCE_SECRET', 'CAMBIAR-ESTA-CLAVE-SECRETA')
TOTP_INTERVAL = 30

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-key')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()
    cur.executescript(
        '''
        CREATE TABLE IF NOT EXISTS roster (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            full_name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            attendance_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(full_name, attendance_date)
        );
        '''
    )
    db.commit()
    db.close()


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def current_token_uri():
    token = pyotp.TOTP(SECRET, interval=TOTP_INTERVAL).now()
    return url_for('student_form', token=token, _external=True)


def validate_token(token: str) -> bool:
    totp = pyotp.TOTP(SECRET, interval=TOTP_INTERVAL)
    return totp.verify(token, valid_window=1)


def normalize_name(first_name: str, last_name: str) -> tuple[str, str, str]:
    first = ' '.join(first_name.strip().split())
    last = ' '.join(last_name.strip().split())
    full = f'{last}, {first}'
    return first, last, full


def fetch_roster():
    db = get_db()
    return db.execute('SELECT * FROM roster ORDER BY last_name, first_name').fetchall()


def fetch_present(attendance_date: str):
    db = get_db()
    return db.execute(
        'SELECT * FROM attendance WHERE attendance_date = ? ORDER BY full_name',
        (attendance_date,),
    ).fetchall()


@app.route('/')
def home():
    today = date.today().isoformat()
    install_host = request.host.split(':')[0]
    return render_template('home.html', today=today, install_host=install_host)


@app.route('/manifest.json')
def manifest():
    return jsonify(
        {
            'name': 'Asistencia QR',
            'short_name': 'Asistencia',
            'start_url': '/',
            'display': 'standalone',
            'background_color': '#f4f7fb',
            'theme_color': '#111827',
            'lang': 'es-AR',
            'icons': [
                {
                    'src': url_for('static', filename='icons/icon-192.png'),
                    'sizes': '192x192',
                    'type': 'image/png',
                },
                {
                    'src': url_for('static', filename='icons/icon-512.png'),
                    'sizes': '512x512',
                    'type': 'image/png',
                },
            ],
        }
    )


@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')


@app.route('/qr.png')
def qr_png():
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(current_token_uri())
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


@app.route('/admin/qr')
def admin_qr():
    current_url = current_token_uri()
    return render_template('admin_qr.html', interval=TOTP_INTERVAL, current_url=current_url)


@app.route('/attendance', methods=['GET', 'POST'])
def student_form():
    token = request.values.get('token', '')

    if request.method == 'POST':
        first_name = request.form.get('first_name', '')
        last_name = request.form.get('last_name', '')

        if not validate_token(token):
            flash('El QR venció. Volvé a escanear el código actual.', 'error')
            return redirect(url_for('student_form', token=token))

        if not first_name.strip() or not last_name.strip():
            flash('Completá nombre y apellido.', 'error')
            return redirect(url_for('student_form', token=token))

        first, last, full_name = normalize_name(first_name, last_name)
        db = get_db()
        today = date.today().isoformat()
        now = datetime.now().isoformat(timespec='seconds')

        db.execute(
            '''
            INSERT INTO attendance (full_name, attendance_date, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(full_name, attendance_date) DO UPDATE SET created_at = excluded.created_at
            ''',
            (full_name, today, now),
        )
        db.commit()
        return render_template('success.html', full_name=full_name)

    token_valid = validate_token(token) if token else False
    return render_template('student_form.html', token=token, token_valid=token_valid)


@app.route('/admin/roster', methods=['GET', 'POST'])
def admin_roster():
    db = get_db()
    if request.method == 'POST':
        file = request.files.get('roster_file')
        if not file or not file.filename:
            flash('Elegí un archivo CSV.', 'error')
            return redirect(url_for('admin_roster'))

        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        expected = {'nombre', 'apellido'}
        if not reader.fieldnames or not expected.issubset({h.strip().lower() for h in reader.fieldnames}):
            flash('El CSV debe tener las columnas: nombre, apellido', 'error')
            return redirect(url_for('admin_roster'))

        inserted = 0
        for row in reader:
            first = row.get('nombre') or row.get('Nombre') or row.get('NOMBRE') or ''
            last = row.get('apellido') or row.get('Apellido') or row.get('APELLIDO') or ''
            if not first.strip() or not last.strip():
                continue
            first, last, full = normalize_name(first, last)
            db.execute(
                'INSERT OR IGNORE INTO roster (first_name, last_name, full_name) VALUES (?, ?, ?)',
                (first, last, full),
            )
            inserted += 1
        db.commit()
        flash(f'Importación terminada. Filas procesadas: {inserted}', 'ok')
        return redirect(url_for('admin_roster'))

    roster = fetch_roster()
    return render_template('admin_roster.html', roster=roster)


@app.route('/admin/report')
def admin_report():
    selected_date = request.args.get('date') or date.today().isoformat()
    roster = fetch_roster()
    present = fetch_present(selected_date)

    present_names = {r['full_name'] for r in present}
    absent = [r for r in roster if r['full_name'] not in present_names]

    return render_template(
        'admin_report.html',
        selected_date=selected_date,
        present=present,
        absent=absent,
        roster_count=len(roster),
        present_count=len(present),
        absent_count=len(absent),
    )


@app.route('/admin/export')
def admin_export():
    selected_date = request.args.get('date') or date.today().isoformat()
    present = fetch_present(selected_date)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['fecha', 'nombre_completo', 'registrado_en'])
    for row in present:
        writer.writerow([row['attendance_date'], row['full_name'], row['created_at']])
    mem = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    mem.seek(0)
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=f'presentes_{selected_date}.csv')


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    print('\nAsistencia QR lista para iPhone')
    print(f'En esta computadora: http://127.0.0.1:{port}')
    print(f'Desde tu iPhone en la misma Wi-Fi: http://{get_local_ip()}:{port}\n')
    app.run(debug=True, host=host, port=port)
