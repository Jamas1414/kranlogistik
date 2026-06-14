import os
from datetime import datetime, date, timedelta

from flask import Flask, render_template, redirect, url_for, request, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'bitte-aendern-in-produktion')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'kranlogistik.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Bitte zuerst einloggen.'


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    firma = db.Column(db.String(120), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='extern')  # 'admin' oder 'extern'
    active = db.Column(db.Boolean, default=True)  # Admin-Freigabe
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bookings = db.relationship('Booking', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_admin(self):
        return self.role == 'admin'


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    datum = db.Column(db.Date, nullable=False)
    start_zeit = db.Column(db.Time, nullable=False)
    end_zeit = db.Column(db.Time, nullable=False)
    bemerkung = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def dauer_stunden(self):
        start = datetime.combine(self.datum, self.start_zeit)
        end = datetime.combine(self.datum, self.end_zeit)
        return round((end - start).total_seconds() / 3600, 2)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

SLOT_START_HOUR = 6   # Kran-Betriebsbeginn
SLOT_END_HOUR = 18    # Kran-Betriebsende
SLOT_LENGTH_MIN = 60  # Slot-Länge in Minuten


def generate_day_slots(day):
    """Erzeugt die möglichen Zeitslots für einen Tag als Liste von (start, end) Zeiten."""
    slots = []
    current = datetime.combine(day, datetime.min.time()).replace(hour=SLOT_START_HOUR)
    end_of_day = datetime.combine(day, datetime.min.time()).replace(hour=SLOT_END_HOUR)
    while current < end_of_day:
        slot_end = current + timedelta(minutes=SLOT_LENGTH_MIN)
        slots.append((current.time(), slot_end.time()))
        current = slot_end
    return slots


def overlaps(start1, end1, start2, end2):
    return start1 < end2 and start2 < end1


# ---------------------------------------------------------------------------
# Routen: Auth
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('kalender'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        firma = request.form['firma'].strip()
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        password2 = request.form['password2']

        if not all([firma, name, email, password]):
            flash('Bitte alle Felder ausfüllen.', 'danger')
            return redirect(url_for('register'))

        if password != password2:
            flash('Die Passwörter stimmen nicht überein.', 'danger')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Diese E-Mail-Adresse ist bereits registriert.', 'danger')
            return redirect(url_for('register'))

        # Erster registrierter Benutzer wird automatisch Admin
        is_first_user = User.query.count() == 0
        user = User(
            firma=firma,
            name=name,
            email=email,
            role='admin' if is_first_user else 'extern',
            active=True if is_first_user else False,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        if is_first_user:
            flash('Konto erstellt. Du bist der erste Benutzer und wurdest als Administrator angelegt. Bitte einloggen.', 'success')
        else:
            flash('Konto erstellt. Ein Administrator muss dein Konto noch freischalten, bevor du Slots buchen kannst.', 'info')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('kalender'))

    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        user = User.query.filter_by(email=email).first()

        if user is None or not user.check_password(password):
            flash('E-Mail oder Passwort ist falsch.', 'danger')
            return redirect(url_for('login'))

        if not user.active:
            flash('Dein Konto wurde noch nicht von einem Administrator freigeschaltet.', 'warning')
            return redirect(url_for('login'))

        login_user(user)
        return redirect(url_for('kalender'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Routen: Kalender / Buchung
# ---------------------------------------------------------------------------

@app.route('/kalender')
@login_required
def kalender():
    # Datum aus Query-Parameter, sonst heute
    datum_str = request.args.get('datum')
    if datum_str:
        try:
            ausgewaehlter_tag = datetime.strptime(datum_str, '%Y-%m-%d').date()
        except ValueError:
            ausgewaehlter_tag = date.today()
    else:
        ausgewaehlter_tag = date.today()

    vorheriger_tag = ausgewaehlter_tag - timedelta(days=1)
    naechster_tag = ausgewaehlter_tag + timedelta(days=1)

    tages_buchungen = Booking.query.filter_by(datum=ausgewaehlter_tag).all()

    slots = []
    for start, end in generate_day_slots(ausgewaehlter_tag):
        belegung = None
        for b in tages_buchungen:
            if overlaps(start, end, b.start_zeit, b.end_zeit):
                belegung = b
                break
        slots.append({
            'start': start,
            'end': end,
            'belegt': belegung is not None,
            'booking': belegung,
            'ist_eigene': belegung.user_id == current_user.id if belegung else False,
        })

    return render_template(
        'kalender.html',
        ausgewaehlter_tag=ausgewaehlter_tag,
        vorheriger_tag=vorheriger_tag,
        naechster_tag=naechster_tag,
        slots=slots,
    )


@app.route('/buchen', methods=['POST'])
@login_required
def buchen():
    datum_str = request.form['datum']
    start_str = request.form['start']
    end_str = request.form['end']
    bemerkung = request.form.get('bemerkung', '').strip()

    try:
        datum = datetime.strptime(datum_str, '%Y-%m-%d').date()
        start_zeit = datetime.strptime(start_str, '%H:%M').time()
        end_zeit = datetime.strptime(end_str, '%H:%M').time()
    except ValueError:
        flash('Ungültige Datums- oder Zeitangabe.', 'danger')
        return redirect(url_for('kalender'))

    if datum < date.today():
        flash('Buchungen in der Vergangenheit sind nicht möglich.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    if start_zeit >= end_zeit:
        flash('Die Startzeit muss vor der Endzeit liegen.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    # Kollisionsprüfung
    bestehende = Booking.query.filter_by(datum=datum).all()
    for b in bestehende:
        if overlaps(start_zeit, end_zeit, b.start_zeit, b.end_zeit):
            flash('Dieser Zeitraum ist bereits belegt. Bitte einen anderen Slot wählen.', 'danger')
            return redirect(url_for('kalender', datum=datum_str))

    booking = Booking(
        user_id=current_user.id,
        datum=datum,
        start_zeit=start_zeit,
        end_zeit=end_zeit,
        bemerkung=bemerkung,
    )
    db.session.add(booking)
    db.session.commit()
    flash(f'Kran erfolgreich reserviert: {datum.strftime("%d.%m.%Y")} von {start_zeit.strftime("%H:%M")} bis {end_zeit.strftime("%H:%M")}.', 'success')
    return redirect(url_for('kalender', datum=datum_str))


@app.route('/buchung/<int:booking_id>/loeschen', methods=['POST'])
@login_required
def buchung_loeschen(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.user_id != current_user.id and not current_user.is_admin():
        flash('Du kannst nur deine eigenen Buchungen stornieren.', 'danger')
        return redirect(url_for('kalender'))

    datum_str = booking.datum.strftime('%Y-%m-%d')
    db.session.delete(booking)
    db.session.commit()
    flash('Buchung wurde storniert.', 'success')
    return redirect(url_for('kalender', datum=datum_str))


# ---------------------------------------------------------------------------
# Routen: Meine Buchungen
# ---------------------------------------------------------------------------

@app.route('/meine-buchungen')
@login_required
def meine_buchungen():
    buchungen = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.datum.desc(), Booking.start_zeit).all()
    return render_template('meine_buchungen.html', buchungen=buchungen)


# ---------------------------------------------------------------------------
# Routen: Admin
# ---------------------------------------------------------------------------

def admin_required():
    if not current_user.is_authenticated or not current_user.is_admin():
        flash('Kein Zugriff. Diese Seite ist nur für Administratoren.', 'danger')
        return False
    return True


@app.route('/admin')
@login_required
def admin():
    if not admin_required():
        return redirect(url_for('kalender'))

    users = User.query.order_by(User.created_at).all()
    return render_template('admin.html', users=users)


@app.route('/admin/user/<int:user_id>/toggle-active', methods=['POST'])
@login_required
def admin_toggle_active(user_id):
    if not admin_required():
        return redirect(url_for('kalender'))

    user = User.query.get_or_404(user_id)
    user.active = not user.active
    db.session.commit()
    flash(f'Konto von {user.name} ({user.firma}) wurde {"freigeschaltet" if user.active else "gesperrt"}.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/user/<int:user_id>/toggle-admin', methods=['POST'])
@login_required
def admin_toggle_admin(user_id):
    if not admin_required():
        return redirect(url_for('kalender'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Du kannst deine eigene Admin-Rolle nicht ändern.', 'warning')
        return redirect(url_for('admin'))

    user.role = 'extern' if user.role == 'admin' else 'admin'
    db.session.commit()
    flash(f'Rolle von {user.name} ({user.firma}) wurde geändert auf "{user.role}".', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/user/<int:user_id>/loeschen', methods=['POST'])
@login_required
def admin_user_loeschen(user_id):
    if not admin_required():
        return redirect(url_for('kalender'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Du kannst dein eigenes Konto hier nicht löschen.', 'warning')
        return redirect(url_for('admin'))

    Booking.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f'Benutzer {user.name} ({user.firma}) wurde gelöscht.', 'success')
    return redirect(url_for('admin'))


# ---------------------------------------------------------------------------
# Routen: Auswertung
# ---------------------------------------------------------------------------

@app.route('/auswertung')
@login_required
def auswertung():
    von_str = request.args.get('von')
    bis_str = request.args.get('bis')

    heute = date.today()
    if von_str:
        von = datetime.strptime(von_str, '%Y-%m-%d').date()
    else:
        von = heute.replace(day=1)

    if bis_str:
        bis = datetime.strptime(bis_str, '%Y-%m-%d').date()
    else:
        bis = heute

    query = Booking.query.filter(Booking.datum >= von, Booking.datum <= bis)

    if not current_user.is_admin():
        query = query.filter(Booking.user_id == current_user.id)

    buchungen = query.all()

    # Aggregation pro Nutzer
    auswertung_pro_user = {}
    for b in buchungen:
        key = b.user_id
        if key not in auswertung_pro_user:
            auswertung_pro_user[key] = {
                'name': b.user.name,
                'firma': b.user.firma,
                'anzahl': 0,
                'stunden': 0.0,
            }
        auswertung_pro_user[key]['anzahl'] += 1
        auswertung_pro_user[key]['stunden'] += b.dauer_stunden

    ergebnisse = sorted(auswertung_pro_user.values(), key=lambda x: x['stunden'], reverse=True)
    gesamt_stunden = sum(e['stunden'] for e in ergebnisse)
    gesamt_buchungen = sum(e['anzahl'] for e in ergebnisse)

    return render_template(
        'auswertung.html',
        ergebnisse=ergebnisse,
        von=von,
        bis=bis,
        gesamt_stunden=gesamt_stunden,
        gesamt_buchungen=gesamt_buchungen,
    )


@app.route('/auswertung/export')
@login_required
def auswertung_export():
    von_str = request.args.get('von')
    bis_str = request.args.get('bis')

    heute = date.today()
    von = datetime.strptime(von_str, '%Y-%m-%d').date() if von_str else heute.replace(day=1)
    bis = datetime.strptime(bis_str, '%Y-%m-%d').date() if bis_str else heute

    query = Booking.query.filter(Booking.datum >= von, Booking.datum <= bis)
    if not current_user.is_admin():
        query = query.filter(Booking.user_id == current_user.id)

    buchungen = query.order_by(Booking.datum, Booking.start_zeit).all()

    zeilen = ['Datum;Firma;Name;Start;Ende;Dauer (h);Bemerkung']
    for b in buchungen:
        zeilen.append(
            f'{b.datum.strftime("%d.%m.%Y")};{b.user.firma};{b.user.name};'
            f'{b.start_zeit.strftime("%H:%M")};{b.end_zeit.strftime("%H:%M")};'
            f'{b.dauer_stunden};{b.bemerkung or ""}'
        )

    csv_data = '\n'.join(zeilen)
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=krannutzung_{von}_{bis}.csv'},
    )


# ---------------------------------------------------------------------------
# PWA: Manifest & Service Worker (für Installation auf Android/iOS)
# ---------------------------------------------------------------------------

@app.route('/manifest.json')
def manifest():
    return {
        "name": "Kranlogistik Baustelle",
        "short_name": "Kranlogistik",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#f5a623",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }


@app.route('/service-worker.js')
def service_worker():
    js = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('fetch', e => {});
"""
    return Response(js, mimetype='application/javascript')


# ---------------------------------------------------------------------------
# CLI: DB initialisieren
# ---------------------------------------------------------------------------

@app.cli.command('init-db')
def init_db():
    """Erstellt alle Tabellen."""
    db.create_all()
    print('Datenbank wurde initialisiert.')


# Tabellen beim Start automatisch erstellen (auch unter gunicorn/Render,
# wo der __main__-Block nicht ausgeführt wird).
with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
