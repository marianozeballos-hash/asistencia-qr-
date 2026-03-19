import csv
import io
import os
import sqlite3
from datetime import datetime, date

from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, url_for
import pyotp
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('ATTENDANCE_DB_PATH', os.path.join(BASE_DIR, 'attendance.db'))
SECRET = os.environ.get('ATTENDANCE_SECRET', 'CAMBIAR-ESTA-CLAVE-SECRETA')
TOTP_INTERVAL = int(os.environ.get('TOTP_INTERVAL', '30'))
TIMEZONE_NAME = os.environ.get('TZ', 'America/Argentina/Buenos_Aires')

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


def current_token_uri():
    token = pyotp.TOTP(SECRET, interval=TOTP_INTERVAL).now()
    return url_for('student_form', token=token, _external=True)


def validate_token(token: str) -> bool:
    totp = pyotp.TOTP(SECRET, interval=TOTP_INTERVAL)
    return totp.verify(token, valid_window=1)


def normalize_name(first_name: str, last_name: str) -> tuple[str, str, str]:
    first = ' '.join((first_name or '').strip().split())
    last = ' '.join((last_name or '').strip().split())
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


def import_rows(rows):
    db = get_db()
    inserted = 0
    updated = 0
    skipped = 0
    for first, last in rows:
        if not (first or '').strip() or not (last or '').strip():
            skipped += 1
            continue
        first, last, full = normalize_name(first, last)
        existing = db.execute('SELECT id FROM roster WHERE full_name = ?', (full,)).fetchone()
        if existing:
            db.execute('UPDATE roster SET first_name = ?, last_name = ? WHERE id = ?', (first, last, existing['id']))
            updated += 1
        else:
            db.execute('INSERT INTO roster (first_name, last_name, full_name) VALUES (?, ?, ?)', (first, last, full))
            inserted += 1
    db.commit()
    return inserted, updated, skipped


@app.route('/')
def home():
    today = date.today().isoformat()
    return render_template('home.html', today=today)


@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'date': date.today().isoformat(),
        'db_path': DB_PATH,
        'timezone': TIMEZONE_NAME,
        'window_seconds': TOTP_INTERVAL,
    })


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
    return render_template('admin_qr.html', interval=TOTP_INTERVAL)


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
        action = request.form.get('action', 'upload_csv')

        if action == 'upload_csv':
            file = request.files.get('roster_file')
            if not file or not file.filename:
                flash('Elegí un archivo CSV.', 'error')
                return redirect(url_for('admin_roster'))

            try:
                content = file.read().decode('utf-8-sig')
                reader = csv.DictReader(io.StringIO(content))
                headers = {h.strip().lower() for h in (reader.fieldnames or [])}
                expected = {'nombre', 'apellido'}
                if not expected.issubset(headers):
                    flash('El archivo debe tener las columnas: nombre y apellido.', 'error')
                    return redirect(url_for('admin_roster'))
                rows = []
                for row in reader:
                    first = row.get('nombre') or row.get('Nombre') or row.get('NOMBRE') or ''
                    last = row.get('apellido') or row.get('Apellido') or row.get('APELLIDO') or ''
                    rows.append((first, last))
                inserted, updated, skipped = import_rows(rows)
                flash(f'Importación lista. Nuevos: {inserted}. Actualizados: {updated}. Omitidos: {skipped}.', 'ok')
            except UnicodeDecodeError:
                flash('No pude leer el archivo. Guardalo como CSV UTF-8 e intentá de nuevo.', 'error')
            return redirect(url_for('admin_roster'))

        if action == 'manual_single':
            first_name = request.form.get('first_name', '')
            last_name = request.form.get('last_name', '')
            if not first_name.strip() or not last_name.strip():
                flash('Completá nombre y apellido para el alta manual.', 'error')
                return redirect(url_for('admin_roster'))
            inserted, updated, skipped = import_rows([(first_name, last_name)])
            if inserted:
                flash('Alumno agregado al padrón.', 'ok')
            elif updated:
                flash('Alumno ya existía. Se actualizaron los datos.', 'ok')
            else:
                flash('No se pudo agregar el alumno.', 'error')
            return redirect(url_for('admin_roster'))

        if action == 'paste_bulk':
            bulk_text = request.form.get('bulk_text', '')
            rows = []
            for raw_line in bulk_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if ',' in line:
                    first, last = [p.strip() for p in line.split(',', 1)]
                elif ';' in line:
                    first, last = [p.strip() for p in line.split(';', 1)]
                else:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    first = ' '.join(parts[:-1])
                    last = parts[-1]
                rows.append((first, last))
            if not rows:
                flash('Pegá una lista con formato nombre,apellido o nombre apellido.', 'error')
                return redirect(url_for('admin_roster'))
            inserted, updated, skipped = import_rows(rows)
            flash(f'Pegado masivo listo. Nuevos: {inserted}. Actualizados: {updated}. Omitidos: {skipped}.', 'ok')
            return redirect(url_for('admin_roster'))

        if action == 'delete_one':
            roster_id = request.form.get('roster_id', type=int)
            if roster_id:
                db.execute('DELETE FROM roster WHERE id = ?', (roster_id,))
                db.commit()
                flash('Alumno eliminado del padrón.', 'ok')
            return redirect(url_for('admin_roster'))

        if action == 'clear_roster':
            db.execute('DELETE FROM roster')
            db.commit()
            flash('Padrón vaciado.', 'ok')
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
    app.run(debug=True)
