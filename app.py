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
# Datenbank: vor jeder Anfrage
