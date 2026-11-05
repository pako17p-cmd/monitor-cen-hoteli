import json
import unittest
from itaka_data import parse_itaka_variants

def html(offers):
    stream='a:'+json.dumps(offers)+'\n'
    return '<script>self.__next_f.push('+json.dumps([1,stream])+')</script>'

def offer(nights,price,date='2026-11-14'):
    return dict(beginDate=date,departure=dict(id='WAW',title='Warszawa'),participantGroups=[dict(id='key'+str(nights))],duration=dict(days=nights+1,nights=nights),price=dict(totalWithAdditionalPayments=dict(amount=price)))

class DurationTests(unittest.TestCase):
    def test_cheaper_four_nights_cannot_replace_seven_nights(self):
        result=parse_itaka_variants(html([offer(4,4000),offer(7,7000)]),'https://www.itaka.pl/hotel,PUJSERE/')
        self.assertEqual(result[0]['nights'],7)
        self.assertEqual(result[0]['price'],3500)

    def test_six_nights_and_longer_stays_remain_allowed(self):
        result=parse_itaka_variants(html([offer(6,6000),offer(14,12000,'2026-11-18')]),'https://www.itaka.pl/hotel')
        self.assertEqual([v['nights'] for v in result],[6,14])

    def test_all_short_stays_are_reported_as_excluded_not_sold_out(self):
        valid,airports,rejected=parse_itaka_variants(html([offer(4,4000)]),'https://www.itaka.pl/hotel',with_rejected=True)
        self.assertEqual(valid,[])
        self.assertEqual(rejected[0]['exclusion_reason'],'short_stay')
        self.assertEqual(rejected[0]['nights'],4)

    def test_unknown_nights_are_not_assumed_to_be_a_week(self):
        item=offer(7,7000);item.pop('duration')
        valid,_,rejected=parse_itaka_variants(html([item]),'https://www.itaka.pl/hotel',with_rejected=True)
        self.assertEqual(valid,[])
        self.assertEqual(rejected[0]['exclusion_reason'],'unknown_duration')

if __name__=='__main__':unittest.main()
