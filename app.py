import os
import calendar as cal_module
from datetime import datetime, date, time, timedelta

from flask import Flask, render_template, redirect, url_for, request, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'bitte-aendern-in-produktion')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'kranlogistik.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB max

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
    role = db.Column(db.String(20), nullable=False, default='extern')
    active = db.Column(db.Boolean, default=True)
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


class Rechnung(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    auftragsnummer = db.Column(db.String(50), nullable=False)
    bkp_nummer = db.Column(db.String(50), nullable=False)
    rechnungstyp = db.Column(db.String(30), nullable=False)
    dateiname = db.Column(db.String(255))
    status = db.Column(db.String(20), default='eingereicht')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='rechnungen')


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

SLOT_START_HOUR = 6
SLOT_END_HOUR = 18
SLOT_LENGTH_MIN = 60


def generate_day_slots(day):
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
    return ''.join(ch for ch in kennzeichen.upper() if ch.isalnum())


def build_day_segments(day, buchungen, current_user_id, is_admin):
    referenz = datetime.combine(day, datetime.min.time())
    tagesbeginn = referenz.replace(hour=SLOT_START_HOUR)
    tagesende = referenz.replace(hour=SLOT_END_HOUR)

    sortiert = sorted(buchungen, key=lambda b: b.start_zeit)

    segmente = []
    cursor = tagesbeginn

    for b in sortiert:
        b_start = datetime.combine(day, b.start_zeit)
        b_end = datetime.combine(day, b.end_zeit)

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

STUNDENSATZ = 250.0
MITTAGSPAUSE_START = time(12, 0)
MITTAGSPAUSE_ENDE = time(13, 0)

BUCHUNGSARTEN = {
    'kranzug':    {'label': 'Einzelner Kranzug (10 Min.)',            'dauer_minuten': 10,  'preis': 100.0},
    'stunde':     {'label': 'Eine Kranstunde (60 Min.)',              'dauer_minuten': 60,  'preis': 250.0},
    'halbtag':    {'label': 'Halber Tag (4.5 Std.)',                  'dauer_minuten': 270, 'preis': 1125.0},
    'tag':        {'label': 'Ganzer Arbeitstag (07:00-12:00 / 13:00-17:00)', 'dauer_minuten': 600, 'preis': 2250.0},
    'individuell': {'label': 'Individuell (Zeit frei waehlbar, 10-Minuten-Schritte)', 'dauer_minuten': None, 'preis': None},
}


MAX_PARKPLAETZE = 25

MONATE_DE = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
             'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']
WOCHENTAGE_DE = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']


def berechne_preis(buchungsart, start_zeit, end_zeit):
    info = BUCHUNGSARTEN.get(buchungsart)
    if info and info['preis'] is not None:
        return info['preis']

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
# Datenbank: vor jeder Anfrage sicherstellen, dass die Tabellen existieren
# ---------------------------------------------------------------------------

@app.before_request
def _ensure_db():
    if not getattr(app, '_db_initialized', False):
        db.create_all()
        app._db_initialized = True


@app.route('/debug-db')
def debug_db():
    import sqlite3
    db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    exists = os.path.isfile(db_path)
    info = 'DB-Pfad: ' + db_path + '\n'
    info += 'Datei existiert: ' + str(exists) + '\n'
    if exists:
        info += 'Dateigroesse: ' + str(os.path.getsize(db_path)) + ' Bytes\n'
        conn = sqlite3.connect(db_path)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        info += 'Tabellen: ' + str(tables) + '\n'
    try:
        db.create_all()
        info += 'create_all() erfolgreich ausgefuehrt.\n'
    except Exception as e:
        info += 'create_all() Fehler: ' + str(e) + '\n'

    db_path2 = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    exists2 = os.path.isfile(db_path2)
    info += 'Datei existiert nach create_all(): ' + str(exists2) + '\n'
    if exists2:
        conn = sqlite3.connect(db_path2)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        info += 'Tabellen nach create_all(): ' + str(tables) + '\n'

    return '<pre>' + info + '</pre>'


# ---------------------------------------------------------------------------
# Routen: Auth
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        heute = date.today()
        naechste_buchung = Booking.query.filter(
            Booking.user_id == current_user.id,
            Booking.datum >= heute
        ).order_by(Booking.datum, Booking.start_zeit).first()
        aktive_tickets = ParkTicket.query.filter(
            ParkTicket.user_id == current_user.id,
            ParkTicket.datum >= heute
        ).count()
        letzte_buchung = Booking.query.filter_by(
            user_id=current_user.id
        ).order_by(Booking.created_at.desc()).first()
        return render_template('dashboard.html',
                               naechste_buchung=naechste_buchung,
                               aktive_tickets=aktive_tickets,
                               letzte_buchung=letzte_buchung)
    return redirect(url_for('login'))


@app.route('/test-login')
def test_login():
    """NUR FUER TESTS - vor Produktion entfernen!"""
    user = User.query.filter_by(role='admin').first()
    if user is None:
        user = User.query.first()
    if user is None:
        # Testbenutzer automatisch anlegen
        user = User(
            firma='Testfirma',
            name='Test Admin',
            email='test@test.de',
            role='admin',
            active=True
        )
        user.set_password('test1234')
        db.session.add(user)
        db.session.commit()
        flash('TEST-Benutzer automatisch angelegt und eingeloggt.', 'warning')
    else:
        flash('TEST-Login als ' + user.name + ' (' + user.firma + ')', 'warning')
    login_user(user)
    return redirect(url_for('index'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        firma = request.form['firma'].strip()
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        password2 = request.form['password2']

        if not all([firma, name, email, password]):
            flash('Bitte alle Felder ausfuellen.', 'danger')
            return redirect(url_for('register'))

        if password != password2:
            flash('Die Passwoerter stimmen nicht ueberein.', 'danger')
            return redirect(url_for('register'))

        if User.query.filter_by(email=email).first():
            flash('Diese E-Mail-Adresse ist bereits registriert.', 'danger')
            return redirect(url_for('register'))

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
    datum_str = request.args.get('datum')
    monat_str = request.args.get('monat')

    # Parse selected day
    ausgewaehlter_tag = None
    if datum_str:
        try:
            ausgewaehlter_tag = datetime.strptime(datum_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    # Determine which month to show
    if monat_str:
        try:
            monat_start = datetime.strptime(monat_str + '-01', '%Y-%m-%d').date()
        except ValueError:
            monat_start = (ausgewaehlter_tag or date.today()).replace(day=1)
    elif ausgewaehlter_tag:
        monat_start = ausgewaehlter_tag.replace(day=1)
    else:
        monat_start = date.today().replace(day=1)

    # Month boundaries
    if monat_start.month == 12:
        monat_ende = monat_start.replace(year=monat_start.year + 1, month=1)
    else:
        monat_ende = monat_start.replace(month=monat_start.month + 1)

    # Prev/next month (first day)
    if monat_start.month == 1:
        vorheriger_monat = monat_start.replace(year=monat_start.year - 1, month=12)
    else:
        vorheriger_monat = monat_start.replace(month=monat_start.month - 1)
    naechster_monat = monat_ende

    # Calendar grid: list of weeks (Mon=0 .. Sun=6), day=0 means padding
    cal_grid = cal_module.monthcalendar(monat_start.year, monat_start.month)

    # Bookings for the whole month (for availability coloring)
    buchungen_monat = Booking.query.filter(
        Booking.datum >= monat_start,
        Booking.datum < monat_ende
    ).all()

    total_minuten = (SLOT_END_HOUR - SLOT_START_HOUR) * 60
    belegung_by_date = {}
    for b in buchungen_monat:
        start_dt = datetime.combine(b.datum, b.start_zeit)
        end_dt = datetime.combine(b.datum, b.end_zeit)
        mins = int((end_dt - start_dt).total_seconds() / 60)
        belegung_by_date[b.datum] = belegung_by_date.get(b.datum, 0) + mins

    # Status per day number: 'vergangen' / 'frei' / 'teilweise' / 'voll'
    heute = date.today()
    monat_belegung = {}
    for week in cal_grid:
        for day_num in week:
            if day_num == 0:
                continue
            tag = date(monat_start.year, monat_start.month, day_num)
            if tag < heute:
                monat_belegung[day_num] = 'vergangen'
            else:
                booked = belegung_by_date.get(tag, 0)
                if booked >= total_minuten:
                    monat_belegung[day_num] = 'voll'
                elif booked > 0:
                    monat_belegung[day_num] = 'teilweise'
                else:
                    monat_belegung[day_num] = 'frei'

    # Day detail: segments for selected day
    segmente = None
    if ausgewaehlter_tag and monat_start <= ausgewaehlter_tag < monat_ende:
        tages_buchungen = Booking.query.filter_by(datum=ausgewaehlter_tag).all()
        segmente = build_day_segments(
            ausgewaehlter_tag, tages_buchungen, current_user.id, current_user.is_admin()
        )

    monat_name = MONATE_DE[monat_start.month - 1] + ' ' + str(monat_start.year)
    tag_name = WOCHENTAGE_DE[ausgewaehlter_tag.weekday()] if ausgewaehlter_tag else None

    return render_template(
        'kalender.html',
        ausgewaehlter_tag=ausgewaehlter_tag,
        segmente=segmente,
        buchungsarten=BUCHUNGSARTEN,
        stundensatz=STUNDENSATZ,
        slot_start_hour=SLOT_START_HOUR,
        slot_end_hour=SLOT_END_HOUR,
        monat_start=monat_start,
        vorheriger_monat=vorheriger_monat,
        naechster_monat=naechster_monat,
        monat_belegung=monat_belegung,
        cal_grid=cal_grid,
        heute=heute,
        monat_name=monat_name,
        tag_name=tag_name,
    )


@app.route('/buchen', methods=['POST'])
@login_required
def buchen():
    datum_str = request.form['datum']
    start_str = request.form['start']
    buchungsart = request.form.get('buchungsart', 'individuell')
    bemerkung = request.form.get('bemerkung', '').strip()

    if buchungsart not in BUCHUNGSARTEN:
        flash('Ungueltige Buchungsart.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    try:
        datum = datetime.strptime(datum_str, '%Y-%m-%d').date()
        start_zeit = datetime.strptime(start_str, '%H:%M').time()
    except ValueError:
        flash('Ungueltige Datums- oder Zeitangabe.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    info = BUCHUNGSARTEN[buchungsart]
    if info['dauer_minuten'] is not None:
        end_dt = datetime.combine(datum, start_zeit) + timedelta(minutes=info['dauer_minuten'])
        end_zeit = end_dt.time()
    else:
        end_str = request.form.get('end', '')
        try:
            end_zeit = datetime.strptime(end_str, '%H:%M').time()
        except ValueError:
            flash('Ungueltige Endzeit.', 'danger')
            return redirect(url_for('kalender', datum=datum_str))

    if datum < date.today():
        flash('Buchungen in der Vergangenheit sind nicht moeglich.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    if start_zeit >= end_zeit:
        flash('Die Startzeit muss vor der Endzeit liegen.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    if start_zeit < time(SLOT_START_HOUR, 0) or end_zeit > time(SLOT_END_HOUR, 0):
        flash('Buchungen sind nur zwischen ' + ('%02d' % SLOT_START_HOUR) + ':00 und ' + ('%02d' % SLOT_END_HOUR) + ':00 Uhr moeglich.', 'danger')
        return redirect(url_for('kalender', datum=datum_str))

    bestehende = Booking.query.filter_by(datum=datum).all()
    for b in bestehende:
        if overlaps(start_zeit, end_zeit, b.start_zeit, b.end_zeit):
            flash('Dieser Zeitraum ist bereits belegt. Bitte einen anderen Slot waehlen.', 'danger')
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
    flash('Kran erfolgreich reserviert: ' + datum.strftime('%d.%m.%Y') + ' von ' + start_zeit.strftime('%H:%M') + ' bis ' + end_zeit.strftime('%H:%M') + ' (' + info['label'] + '). Preis: CHF ' + ('%.2f' % preis), 'success')
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
# Routen: Baulogistik Uebersicht
# ---------------------------------------------------------------------------

@app.route('/baulogistik')
@login_required
def baulogistik():
    return render_template('baulogistik.html')


# ---------------------------------------------------------------------------
# Routen: 3D Modell Viewer
# ---------------------------------------------------------------------------

@app.route('/modell')
@login_required
def modell():
    return render_template('modell.html')


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
        von_str = request.form.get('von', '')
        bis_str = request.form.get('bis', '')
        kennzeichen = normalisiere_kennzeichen(kennzeichen_raw)

        if not kennzeichen:
            flash('Bitte ein Kontrollschild eingeben.', 'danger')
            return redirect(url_for('parkplatz'))

        try:
            von = datetime.strptime(von_str, '%Y-%m-%d').date()
        except ValueError:
            von = date.today()

        if bis_str:
            try:
                bis = datetime.strptime(bis_str, '%Y-%m-%d').date()
            except ValueError:
                bis = von
        else:
            bis = von

        if bis < von:
            flash('Das Enddatum darf nicht vor dem Startdatum liegen.', 'danger')
            return redirect(url_for('parkplatz'))

        if (bis - von).days > 31:
            flash('Bitte maximal 31 Tage auf einmal buchen.', 'danger')
            return redirect(url_for('parkplatz'))

        erstellt = []
        bereits_vorhanden = []
        voll = []

        aktueller_tag = von
        while aktueller_tag <= bis:
            bestehend = ParkTicket.query.filter_by(kennzeichen=kennzeichen, datum=aktueller_tag).first()
            if bestehend:
                bereits_vorhanden.append(aktueller_tag)
            else:
                anzahl_belegt = ParkTicket.query.filter_by(datum=aktueller_tag).count()
                if anzahl_belegt >= MAX_PARKPLAETZE:
                    voll.append(aktueller_tag)
                else:
                    ticket = ParkTicket(user_id=current_user.id, kennzeichen=kennzeichen, datum=aktueller_tag)
                    db.session.add(ticket)
                    erstellt.append(aktueller_tag)
            aktueller_tag += timedelta(days=1)

        if erstellt:
            db.session.commit()
            if len(erstellt) == 1:
                flash('Tagesticket fuer ' + kennzeichen_raw.upper() + ' am ' + erstellt[0].strftime('%d.%m.%Y') + ' wurde geloest.', 'success')
            else:
                flash('Tagestickets fuer ' + kennzeichen_raw.upper() + ' wurden geloest: ' + ', '.join(d.strftime('%d.%m.%Y') for d in erstellt), 'success')

        if bereits_vorhanden:
            flash('Fuer ' + kennzeichen_raw.upper() + ' bestand bereits ein Ticket am: ' + ', '.join(d.strftime('%d.%m.%Y') for d in bereits_vorhanden), 'warning')

        if voll:
            flash('Keine freien Parkplaetze mehr (Limite ' + str(MAX_PARKPLAETZE) + ') am: ' + ', '.join(d.strftime('%d.%m.%Y') for d in voll), 'danger')

        return redirect(url_for('parkplatz'))

    eigene_tickets = ParkTicket.query.filter_by(user_id=current_user.id) \
        .order_by(ParkTicket.datum.desc(), ParkTicket.kennzeichen).all()

    tage_pro_kennzeichen = {}
    for t in eigene_tickets:
        tage_pro_kennzeichen[t.kennzeichen] = tage_pro_kennzeichen.get(t.kennzeichen, 0) + 1

    heute = date.today()

    # Month calendar for parkplatz
    monat_str = request.args.get('monat')
    datum_str_park = request.args.get('datum')

    ausgewaehlter_tag_park = None
    if datum_str_park:
        try:
            ausgewaehlter_tag_park = datetime.strptime(datum_str_park, '%Y-%m-%d').date()
        except ValueError:
            pass

    if monat_str:
        try:
            monat_start_p = datetime.strptime(monat_str + '-01', '%Y-%m-%d').date()
        except ValueError:
            monat_start_p = (ausgewaehlter_tag_park or heute).replace(day=1)
    elif ausgewaehlter_tag_park:
        monat_start_p = ausgewaehlter_tag_park.replace(day=1)
    else:
        monat_start_p = heute.replace(day=1)

    if monat_start_p.month == 12:
        monat_ende_p = monat_start_p.replace(year=monat_start_p.year + 1, month=1)
    else:
        monat_ende_p = monat_start_p.replace(month=monat_start_p.month + 1)

    if monat_start_p.month == 1:
        vorheriger_monat_p = monat_start_p.replace(year=monat_start_p.year - 1, month=12)
    else:
        vorheriger_monat_p = monat_start_p.replace(month=monat_start_p.month - 1)
    naechster_monat_p = monat_ende_p

    cal_grid_p = cal_module.monthcalendar(monat_start_p.year, monat_start_p.month)

    # Tickets in the month
    tickets_monat = ParkTicket.query.filter(
        ParkTicket.datum >= monat_start_p,
        ParkTicket.datum < monat_ende_p
    ).all()
    belegt_by_date_p = {}
    for t in tickets_monat:
        belegt_by_date_p[t.datum] = belegt_by_date_p.get(t.datum, 0) + 1

    # Status per day
    monat_belegung_p = {}
    for week in cal_grid_p:
        for day_num in week:
            if day_num == 0:
                continue
            tag = date(monat_start_p.year, monat_start_p.month, day_num)
            if tag < heute:
                monat_belegung_p[day_num] = 'vergangen'
            else:
                belegt = belegt_by_date_p.get(tag, 0)
                frei = max(0, MAX_PARKPLAETZE - belegt)
                if frei == 0:
                    monat_belegung_p[day_num] = 'voll'
                elif frei <= 5:
                    monat_belegung_p[day_num] = 'wenig'
                else:
                    monat_belegung_p[day_num] = 'frei'

    monat_name_p = MONATE_DE[monat_start_p.month - 1] + ' ' + str(monat_start_p.year)
    tag_name_park = WOCHENTAGE_DE[ausgewaehlter_tag_park.weekday()] if ausgewaehlter_tag_park else None

    # Free spots for selected day
    if ausgewaehlter_tag_park:
        belegt_ausgewaehlt = ParkTicket.query.filter_by(datum=ausgewaehlter_tag_park).count()
        freie_plaetze_ausgewaehlt = max(0, MAX_PARKPLAETZE - belegt_ausgewaehlt)
    else:
        freie_plaetze_ausgewaehlt = None

    return render_template(
        'parkplatz.html',
        eigene_tickets=eigene_tickets,
        tage_pro_kennzeichen=tage_pro_kennzeichen,
        gesamt_tage=len(eigene_tickets),
        heute=heute,
        max_parkplaetze=MAX_PARKPLAETZE,
        monat_start_p=monat_start_p,
        vorheriger_monat_p=vorheriger_monat_p,
        naechster_monat_p=naechster_monat_p,
        monat_belegung_p=monat_belegung_p,
        cal_grid_p=cal_grid_p,
        monat_name_p=monat_name_p,
        ausgewaehlter_tag_park=ausgewaehlter_tag_park,
        tag_name_park=tag_name_park,
        freie_plaetze_ausgewaehlt=freie_plaetze_ausgewaehlt,
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
        flash('Kein Zugriff. Diese Seite ist nur fuer Administratoren.', 'danger')
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
    flash('Konto von ' + user.name + ' (' + user.firma + ') wurde ' + ('freigeschaltet' if user.active else 'gesperrt') + '.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/user/<int:user_id>/toggle-admin', methods=['POST'])
@login_required
def admin_toggle_admin(user_id):
    if not admin_required():
        return redirect(url_for('kalender'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Du kannst deine eigene Admin-Rolle nicht aendern.', 'warning')
        return redirect(url_for('admin'))

    user.role = 'extern' if user.role == 'admin' else 'admin'
    db.session.commit()
    flash('Rolle von ' + user.name + ' (' + user.firma + ') wurde geaendert auf "' + user.role + '".', 'success')
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
    flash('Passwort fuer ' + user.name + ' (' + user.firma + ') wurde zurueckgesetzt.', 'success')
    return redirect(url_for('admin'))


@app.route('/admin/user/<int:user_id>/loeschen', methods=['POST'])
@login_required
def admin_user_loeschen(user_id):
    if not admin_required():
        return redirect(url_for('kalender'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Du kannst dein eigenes Konto hier nicht loeschen.', 'warning')
        return redirect(url_for('admin'))

    Booking.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    flash('Benutzer ' + user.name + ' (' + user.firma + ') wurde geloescht.', 'success')
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

    buchungen = query.all