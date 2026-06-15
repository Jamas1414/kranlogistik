def overlaps(start1, end1, start2, end2):
    return start1 < end2 and start2 < end1


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
