import os
from datetime import datetime, date, time, timedelta

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
    buchungsart = db.Column(db.String(20), nullable=False, default='individuell')
    preis = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def dauer_stunden(self):
        start = datetime.combine(self.datum, self.start_zeit)
        end = datetime.combine(self.datum, self.end_zeit)
        return round((end - start).total_seconds() / 3600, 2)


class ParkTicket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    kennzeichen = db.Column(db.String(20), nullable=False)
    datum = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='parktickets')


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


def normalisiere_kennzeichen(kennzeichen):
    """Normalisiert ein Kontrollschild für den Vergleich (Grossbuchstaben, ohne Leer-/Sonderzeichen)."""
    return ''.join(ch for ch in kennzeichen.upper() if ch.isalnum())


def build_day_segments(day, buchungen, current_user_id, is_admin):
    """
    Baut eine Liste von zusammenhängenden Zeitabschnitten für den Betriebstag,
    abwechselnd 'frei' und 'belegt'. So sind auch kurze Buchungen (z.B. 10-Min-Kranzug)
    sichtbar, ohne dass die ganze Stunde als belegt erscheint.
    """
    referenz = datetime.combine(day, datetime.min.time())
    tagesbeginn = referenz.replace(hour=SLOT_START_HOUR)
    tagesende = referenz.replace(hour=SLOT_END_HOUR)

    sortiert = sorted(buchungen, key=lambda b: b.start_zeit)

    segmente = []
    cursor = tagesbeginn

    for b in sortiert:
        b_start = datetime.combine(day, b.start_zeit)
        b_end = datetime.combine(day, b.end_zeit)

        # Buchungen ausserhalb des sichtbaren Tagesbereichs ignorieren
        if b_end <= tagesbeginn or b_start >= tagesende:
            continue
        b_start = max(b_start, tagesbeginn)
        b_end = min(b_end, tagesende)

        if b_start > cursor:
            segmente.append({
                'start': cursor.time(),
                'end': b_start.time(),
                'belegt': False,
                'booking': None,
                'ist_eigene': False,
                'dauer_minuten': int((b_start - cursor).total_seconds() / 60),
            })

        if b_end > cursor:
            segmente.append({
                'start': max(b_start, cursor).time(),
                'end': b_end.time(),
                'belegt': True,
                'booking': b,
                'ist_eigene': b.user_id == current_user_id,
                'dauer_minuten': int((b_end - max(b_start, cursor)).total_seconds() / 60),
            })
            cursor = b_end

    if cursor < tagesende:
        segmente.append({
            'start': cursor.time(),
            'end': tagesende.time(),
            'belegt': False,
            'booking': None,
            'ist_eigene': False,
            'dauer_minuten': int((tagesende - cursor).total_seconds() / 60),
        })

    return segmente


# ---------------------------------------------------------------------------
# Abrechnung Kran-Nutzung
# ---------------------------------------------------------------------------

STUNDENSATZ = 250.0  # CHF pro Kranstunde (für Zeitbuchungen)
MITTAGSPAUSE_START = time(12, 0)
MITTAGSPAUSE_ENDE = time(13, 0)

# Feste Buchungsarten: Dauer in Minuten (None = frei wählbar) und Pauschalpreis (None = nach Stundensatz)
BUCHUNGSARTEN = {
    'kranzug':    {'label': 'Einzelner Kranzug (10 Min.)',            'dauer_minuten': 10,  'preis': 100.0},
    'stunde':     {'label': 'Eine Kranstunde (60 Min.)',              'dauer_minuten': 60,  'preis': 250.0},
    'halbtag':    {'label': 'Halber Tag (4.5 Std.)',                  'dauer_minuten': 270, 'preis': 1125.0},
    'tag':        {'label': 'Ganzer Arbeitstag (07:00–12:00 / 13:00–17:00)', 'dauer_minuten': 600, 'preis': 2250.0},
    'individuell': {'label': 'Individuell (Zeit frei wählbar, 10-Minuten-Schritte)', 'dauer_minuten': None, 'preis': None},
}


def berechne_preis(buchungsart, start_zeit, end_zeit):
    """Berechnet den zu bezahlenden Preis für eine Buchung."""
    info = BUCHUNGSARTEN.get(buchungsart)
    if info and info['preis'] is not None:
        return info['preis']

    # Individuelle Buchung: nach Stundensatz, Mittagspause 12-13 Uhr wird nicht verrechnet
    referenz = date.today()
    start_dt = datetime.combine(referenz, start_zeit)
    end_dt = datetime.combine(referenz, end_zeit)
    minuten = (end_dt - start_dt).total_seconds() / 60

    pause_start = datetime.combine(referenz, MITTAGSPAUSE_START)
    pause_ende = datetime.combine(referenz, MITTAGSPAUSE_ENDE)
    overlap_minuten = max(0, (min(end_dt, pause_ende) - max(start_dt, pause_start)).total_seconds() / 60)
    minuten -= overlap_minuten

    return round(max(0, minuten) / 60 * STUNDENSATZ, 2)


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

    segmente = build_day_segments(
        ausgewaehlter_tag, tages_buchungen, current_user.id, current_user.is_admin()
    )

    return render_template(
        'kalender.html',
        ausgewaehlter_tag=ausgewaehlter_tag,
        vorheriger_tag=vorheriger_tag,
        naechster_tag=naechster_tag,
        segmente=segmente,
        buchungsarten=BUCHUNGSARTEN,
        stundensatz=STUNDENSATZ,
        slot_start_hour=SLOT_START_HOUR,
        slot_end_hour=SLOT_END_HOUR,
    )


@app.route('/buchen', methods=['POST'])
@login_required
def buchen():
    datum_str = request.form['datum']
    start_str = request.form['start']
    buchungsart = request.form.get('buchungsart', 'individuell')
    bemerkung = request.form.get('bemerkung', '').strip()

    if buchungsart not in BUCHUNGSARTEN:
        flash('Ungültige Buchungsart.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    try:
        datum = datetime.strptime(datum_str, '%Y-%m-%d').date()
        start_zeit = datetime.strptime(start_str, '%H:%M').time()
    except ValueError:
        flash('Ungültige Datums- oder Zeitangabe.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    info = BUCHUNGSARTEN[buchungsart]
    if info['dauer_minuten'] is not None:
        # Feste Buchungsart: Endzeit ergibt sich aus der Dauer
        end_dt = datetime.combine(datum, start_zeit) + timedelta(minutes=info['dauer_minuten'])
        end_zeit = end_dt.time()
    else:
        end_str = request.form.get('end', '')
        try:
            end_zeit = datetime.strptime(end_str, '%H:%M').time()
        except ValueError:
            flash('Ungültige Endzeit.', 'danger')
            return redirect(url_for('kalender', datum=datum_str))

    if datum < date.today():
        flash('Buchungen in der Vergangenheit sind nicht möglich.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    if start_zeit >= end_zeit:
        flash('Die Startzeit muss vor der Endzeit liegen.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    if start_zeit < time(SLOT_START_HOUR, 0) or end_zeit > time(SLOT_END_HOUR, 0):
        flash(f'Buchungen sind nur zwischen {SLOT_START_HOUR:02d}:00 und {SLOT_END_HOUR:02d}:00 Uhr möglich.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    # Kollisionsprüfung
    bestehende = Booking.query.filter_by(datum=datum).all()
    for b in bestehende:
        if overlaps(start_zeit, end_zeit, b.start_zeit, b.end_zeit):
            flash('Dieser Zeitraum ist bereits belegt. Bitte einen anderen Slot wählen.', 'danger')
            return redirect(url_for('kalender', datum=datum_str))

    preis = berechne_preis(buchungsart, start_zeit, end_zeit)

    booking = Booking(
        user_id=current_user.id,
        datum=datum,
        start_zeit=start_zeit,
        end_zeit=end_zeit,
        bemerkung=bemerkung,
        buchungsart=buchungsart,
        preis=preis,
    )
    db.session.add(booking)
    db.session.commit()
    flash(
        f'Kran erfolgreich reserviert: {datum.strftime("%d.%m.%Y")} von {start_zeit.strftime("%H:%M")} '
        f'bis {end_zeit.strftime("%H:%M")} ({info["label"]}). Preis: CHF {preis:.2f}',
        'success'
    )
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
# Routen: Anfahrt
# ---------------------------------------------------------------------------

@app.route('/anfahrt')
@login_required
def anfahrt():
    plan_pfad = os.path.join(basedir, 'static', 'installationsplan.jpg')
    return render_template('anfahrt.html', installationsplan_vorhanden=os.path.isfile(plan_pfad))


# ---------------------------------------------------------------------------
# Routen: Meine Buchungen
# ---------------------------------------------------------------------------

@app.route('/meine-buchungen')
@login_required
def meine_buchungen():
    buchungen = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.datum.desc(), Booking.start_zeit).all()
    return render_template('meine_buchungen.html', buchungen=buchungen, buchungsarten=BUCHUNGSARTEN)


# ---------------------------------------------------------------------------
# Routen: Parkplatzmanagement
# ---------------------------------------------------------------------------

@app.route('/parkplatz', methods=['GET', 'POST'])
@login_required
def parkplatz():
    if request.method == 'POST':
        kennzeichen_raw = request.form.get('kennzeichen', '').strip()
        datum_str = request.form.get('datum', '')
        kennzeichen = normalisiere_kennzeichen(kennzeichen_raw)

        if not kennzeichen:
            flash('Bitte ein Kontrollschild eingeben.', 'danger')
            return redirect(url_for('parkplatz'))

        try:
            datum = datetime.strptime(datum_str, '%Y-%m-%d').date()
        except ValueError:
            datum = date.today()

        bestehend = ParkTicket.query.filter_by(kennzeichen=kennzeichen, datum=datum).first()
        if bestehend:
            flash(
                f'Für das Kontrollschild {kennzeichen_raw.upper()} wurde am {datum.strftime("%d.%m.%Y")} '
                f'bereits ein Parkticket gelöst.', 'warning'
            )
            return redirect(url_for('parkplatz'))

        ticket = ParkTicket(user_id=current_user.id, kennzeichen=kennzeichen, datum=datum)
        db.session.add(ticket)
        db.session.commit()
        flash(
            f'Tagesticket für {kennzeichen_raw.upper()} am {datum.strftime("%d.%m.%Y")} wurde gelöst.', 'success'
        )
        return redirect(url_for('parkplatz'))

    eigene_tickets = ParkTicket.query.filter_by(user_id=current_user.id) \
        .order_by(ParkTicket.datum.desc(), ParkTicket.kennzeichen).all()

    # Auswertung: Anzahl gelöster Tage pro Kontrollschild
    tage_pro_kennzeichen = {}
    for t in eigene_tickets:
        tage_pro_kennzeichen[t.kennzeichen] = tage_pro_kennzeichen.get(t.kennzeichen, 0) + 1

    return render_template(
        'parkplatz.html',
        eigene_tickets=eigene_tickets,
        tage_pro_kennzeichen=tage_pro_kennzeichen,
        gesamt_tage=len(eigene_tickets),
        heute=date.today(),
    )


@app.route('/parkplatz/<int:ticket_id>/loeschen', methods=['POST'])
@login_required
def parkplatz_loeschen(ticket_id):
    ticket = ParkTicket.query.get_or_404(ticket_id)
    if ticket.user_id != current_user.id and not current_user.is_admin():
        flash('Du kannst nur eigene Parktickets stornieren.', 'danger')
        return redirect(url_for('parkplatz'))

    db.session.delete(ticket)
    db.session.commit()
    flash('Parkticket wurde storniert.', 'success')
    return redirect(url_for('parkplatz'))


@app.route('/parkplatz/pruefung')
@login_required
def parkplatz_pruefung():
    if not admin_required():
        return redirect(url_for('kalender'))

    datum_str = request.args.get('datum')
    if datum_str:
        try:
            datum = datetime.strptime(datum_str, '%Y-%m-%d').date()
        except ValueError:
            datum = date.today()
    else:
        datum = date.today()

    tickets_heute = ParkTicket.query.filter_by(datum=datum).order_by(ParkTicket.kennzeichen).all()

    return render_template('parkplatz_pruefung.html', tickets_heute=tickets_heute, datum=datum)


@app.route('/api/parkplatz/check')
@login_required
def api_parkplatz_check():
    if not current_user.is_admin():
        return {'error': 'Kein Zugriff'}, 403

    kennzeichen = normalisiere_kennzeichen(request.args.get('kennzeichen', ''))
    datum_str = request.args.get('datum')
    try:
        datum = datetime.strptime(datum_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        datum = date.today()

    if not kennzeichen:
        return {'gefunden': False}

    ticket = ParkTicket.query.filter_by(kennzeichen=kennzeichen, datum=datum).first()
    if ticket:
        return {
            'gefunden': True,
            'kennzeichen': ticket.kennzeichen,
            'firma': ticket.user.firma,
            'name': ticket.user.name,
            'datum': ticket.datum.strftime('%d.%m.%Y'),
        }

    return {'gefunden': False, 'kennzeichen': kennzeichen, 'datum': datum.strftime('%d.%m.%Y')}


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


@app.route('/admin/user/<int:user_id>/passwort-zuruecksetzen', methods=['POST'])
@login_required
def admin_reset_password(user_id):
    if not admin_required():
        return redirect(url_for('kalender'))

    user = User.query.get_or_404(user_id)
    neues_passwort = request.form.get('neues_passwort', '').strip()

    if len(neues_passwort) < 6:
        flash('Das neue Passwort muss mindestens 6 Zeichen lang sein.', 'danger')
        return redirect(url_for('admin'))

    user.set_password(neues_passwort)
    db.session.commit()
    flash(f'Passwort für {user.name} ({user.firma}) wurde zurückgesetzt.', 'success')
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
                'betrag': 0.0,
            }
        auswertung_pro_user[key]['anzahl'] += 1
        auswertung_pro_user[key]['stunden'] += b.dauer_stunden
        auswertung_pro_user[key]['betrag'] += b.preis

    ergebnisse = sorted(auswertung_pro_user.values(), key=lambda x: x['betrag'], reverse=True)
    gesamt_stunden = sum(e['stunden'] for e in ergebnisse)
    gesamt_buchungen = sum(e['anzahl'] for e in ergebnisse)
    gesamt_betrag = sum(e['betrag'] for e in ergebnisse)

    return render_template(
        'auswertung.html',
        ergebnisse=ergebnisse,
        von=von,
        bis=bis,
        gesamt_stunden=gesamt_stunden,
        gesamt_buchungen=gesamt_buchungen,
        gesamt_betrag=gesamt_betrag,
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

    zeilen = ['Datum;Firma;Name;Start;Ende;Dauer (h);Buchungsart;Preis (CHF);Bemerkung']
    gesamt_betrag = 0.0
    for b in buchungen:
        gesamt_betrag += b.preis
        art = BUCHUNGSARTEN.get(b.buchungsart, {}).get('label', b.buchungsart)
        zeilen.append(
            f'{b.datum.strftime("%d.%m.%Y")};{b.user.firma};{b.user.name};'
            f'{b.start_zeit.strftime("%H:%M")};{b.end_zeit.strftime("%H:%M")};'
            f'{b.dauer_stunden};{art};{b.preis:.2f};{b.bemerkung or ""}'
        )
    zeilen.append(f';;;;;;;{gesamt_betrag:.2f};Gesamtbetrag')

    csv_data = '\n'.join(zeilen)
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=krannutzung_{von}_{bis}.csv'},
    )
