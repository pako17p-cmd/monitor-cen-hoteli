import json
import unittest
from itaka_data import parse_itaka_variants, duration_search_url
from urllib.parse import parse_qs,urlparse

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

    def test_duration_search_unpins_old_offer_and_links_keep_actual_days(self):
        url='https://www.itaka.pl/hotel?duration=5&id%5B0%5D=short&airports=POZ&adults%5B0%5D=2'
        q=parse_qs(urlparse(duration_search_url(url,'WAW',8)).query)
        self.assertNotIn('id[0]',q)
        self.assertEqual(q['duration'],['8'])
        self.assertEqual(q['airports'],['WAW'])
        result=parse_itaka_variants(html([offer(14,12000)]),duration_search_url(url,'WAW',8))
        self.assertEqual(parse_qs(urlparse(result[0]['url']).query)['duration'],['15'])

    def test_all_durations_keeps_two_prices_for_same_date_and_airport(self):
        result=parse_itaka_variants(html([offer(7,7000),offer(14,12000)]),'https://www.itaka.pl/hotel,PUJSERE/',all_durations=True)
        self.assertEqual(sorted((v['nights'],v['price']) for v in result),[(7,3500),(14,6000)])

if __name__=='__main__':unittest.main()
