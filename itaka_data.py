"""Read actual ITAKA date/flight variants from its server-rendered React stream."""
import base64
import json
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def token_airports(token):
    """Offer protobuf fields 12 and 16 are outbound and return origin respectively."""
    try:
        data = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
        def varint(pos):
            value = shift = 0
            while pos < len(data):
                byte = data[pos]; pos += 1
                value |= (byte & 127) << shift
                if byte < 128: return value, pos
                shift += 7
                if shift > 63: raise ValueError('varint')
            raise ValueError('truncated')
        fields = {}; pos = 0
        while pos < len(data):
            key, pos = varint(pos); kind = key & 7
            if kind == 0: _, pos = varint(pos)
            elif kind == 2:
                length, pos = varint(pos)
                fields[key >> 3] = data[pos:pos + length]; pos += length
            elif kind in (1, 5): pos += 8 if kind == 1 else 4
            else: break
        def code(field):
            match = re.fullmatch(rb'\x0a\x03([A-Z]{3})', fields.get(field, b''))
            return match[1].decode() if match else None
        return code(12), code(16)
    except (ValueError, UnicodeError):
        return None, None


def offer_url(url, code, token):
    p = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k != 'airports' and not k.startswith('id[')]
    query += [('airports', code), ('id[0]', token)]
    return urlunparse(p._replace(query=urlencode(query)))


def parse_itaka_variants(html, url, people=2, with_airports=False):
    chunks = []
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, re.S):
        try:
            item = json.loads(match[1])
            if item[0] == 1 and isinstance(item[1], str): chunks.append(item[1])
        except (ValueError, IndexError): pass
    stream = ''.join(chunks)
    decoder = json.JSONDecoder(); nodes = {}
    # Text records can end immediately before the next record, without a newline.
    for match in re.finditer(r'([0-9a-f]+):(?=[\[{])', stream):
        try: nodes[match[1]] = decoder.raw_decode(stream, match.end())[0]
        except ValueError: pass

    def resolve(value, depth=0):
        if depth > 35: return None
        if isinstance(value, str) and re.match(r'^\$[0-9a-f]+:', value):
            path = value[1:].split(':'); value = nodes.get(path.pop(0))
            for key in path:
                value = resolve(value, depth + 1)
                if isinstance(value, list) and key == 'props' and len(value) == 4: value = value[3]
                elif isinstance(value, list) and key.isdigit(): value = value[int(key)] if int(key) < len(value) else None
                elif isinstance(value, dict): value = value.get(key)
                else: return None
            return resolve(value, depth + 1)
        return value

    def obj(value):
        value = resolve(value)
        return value if isinstance(value, dict) else {}

    found = {}; airports = {}
    def walk(value):
        if isinstance(value, list):
            for item in value: walk(item)
        elif isinstance(value, dict):
            departures = obj(value.get('departures'))
            flights = resolve(departures.get('flight'))
            if isinstance(flights, list):
                for flight in flights:
                    flight = obj(flight)
                    if flight.get('transport') == 'flight' and flight.get('id'):
                        airports[flight['id']] = dict(code=flight['id'], name=flight.get('title') or flight['id'])
            date = value.get('beginDate')
            departure = obj(value.get('departure'))
            transport = obj(value.get('transport'))
            outbound = obj(transport.get('outbound'))
            code = departure.get('id') or outbound.get('id')
            groups = resolve(value.get('participantGroups'))
            group = obj(groups[0]) if isinstance(groups, list) and groups else {}
            token = group.get('id') or value.get('id')
            if code and isinstance(token, str):
                _, back = token_airports(token)
                price = obj(value.get('price'))
                total = obj(price.get('totalWithAdditionalPayments') or price.get('actualWithAdditionalPayments'))
                if not total: total = obj(value.get('priceWithAdditionalPayments'))
                amount = total.get('amount')
                if re.fullmatch(r'20\d\d-\d\d-\d\d', str(date)) and isinstance(amount, (int, float)) and amount > 0:
                    ret = obj(value.get('anotherReturnAirport'))
                    item = dict(date=date, code=code, name=departure.get('title') or outbound.get('title') or code,
                                price=amount / people, return_code=ret.get('id') or back or code,
                                return_name=ret.get('title'), url=offer_url(url, code, token))
                    key = (date, code)
                    if key not in found or amount / people < found[key]['price']: found[key] = item
            for item in value.values(): walk(item)
    for value in nodes.values(): walk(value)
    if not found: raise ValueError('ITAKA nie zwróciła wariantów z potwierdzonym lotniskiem i ceną.')
    variants = sorted(found.values(), key=lambda x: (x['date'], x['code']))
    return (variants, list(airports.values())) if with_airports else variants
