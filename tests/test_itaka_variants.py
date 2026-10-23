import base64
import json
import unittest
from urllib.parse import parse_qs, urlparse
from itaka_data import parse_itaka_variants, token_airports

URL='https://www.itaka.pl/wczasy/zanzibar/hotel,ZNZVERA/?airports=WAW,POZ,WRO,KTW&adults%5B0%5D=2'
def token(outbound, back):
    return base64.b64encode(b'\x62\x05\x0a\x03'+outbound.encode()+b'\x82\x01\x05\x0a\x03'+back.encode()).decode()
def html():
    poz=token('POZ','WAW');wro=token('WRO','KTW');ktw=token('KTW','KTW')
    def variant(code, back, date, total):
        return dict(beginDate=date,departure=dict(id=code,title=code),
                    participantGroups=[dict(id=token(code,back))],
                    price=dict(totalWithAdditionalPayments=dict(amount=total)))
    current=variant('POZ','WAW','2026-10-26',7858)
    replacement=variant('WRO','KTW','2026-10-26',7858)
    # React's props paths and chunk boundaries match the real stream.
    current['departure']='$a:props:flight'
    nodes={'a':['$','component',None,dict(flight=dict(id='POZ',title='Poznań'))],
           'b':dict(current=current,dates=[variant('KTW','KTW','2026-11-01',10458)],replacements=[replacement])}
    stream=''.join(k+':'+json.dumps(v)+'\n' for k,v in nodes.items())
    return ''.join('<script>self.__next_f.push('+json.dumps([1,part])+')</script>' for part in [stream[:97],stream[97:]])

class ItakaVariantsTests(unittest.TestCase):
    def test_outbound_does_not_match_return_airport_first(self):
        self.assertEqual(token_airports(token('POZ','WAW')),('POZ','WAW'))
        self.assertEqual(token_airports(token('WRO','KTW')),('WRO','KTW'))
    def test_real_shape_retains_dates_airports_and_return(self):
        variants=parse_itaka_variants(html(),URL)
        self.assertEqual([(v['date'],v['code'],v['return_code']) for v in variants],
            [('2026-10-26','POZ','WAW'),('2026-10-26','WRO','KTW'),('2026-11-01','KTW','KTW')])
        self.assertEqual(variants[0]['price'],3929)
        self.assertEqual(parse_qs(urlparse(variants[1]['url']).query)['airports'],['WRO'])
    def test_missing_variants_fail_without_guessing_from_filter(self):
        with self.assertRaises(ValueError): parse_itaka_variants('<p>Warszawa Poznań</p>',URL)

if __name__=='__main__': unittest.main()
