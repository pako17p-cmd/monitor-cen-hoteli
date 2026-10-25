"""Availability transitions preserve verified history and ignore incomplete checks."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import app

class SoldOutTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.db=patch.object(app,'DB_PATH',str(Path(self.directory.name)/'history.db'));self.db.start()
        self.old=(datetime.utcnow()+timedelta(days=5)).date().isoformat()
        self.new=(datetime.utcnow()+timedelta(days=12)).date().isoformat()
        self.limit=(datetime.utcnow()+timedelta(days=40)).date().isoformat()
        self.option={'code':'WAW','name':'Warszawa','date':self.old,'price':1000,'return_code':'WAW','url':'https://www.itaka.pl/hotel?airports=WAW'}
        self.variant={**self.option,'date':self.new,'price':1200}
        cache={'version':2,'airports':[self.option],'by_date':{self.old:{'airports':[self.option]}}}
        c=app.connect();c.execute('DELETE FROM hotels')
        c.execute('INSERT INTO hotels(id,name,url,people_count,airport_code,airport_name,airport_options) VALUES(1,?,?,?,?,?,?)',('Test','https://www.itaka.pl/hotel?airports=WAW',2,'WAW','Warszawa',json.dumps(cache)))
        c.execute("INSERT INTO prices(hotel_id,checked_at,price,departure_date,per_person,airport_code) VALUES(1,?,1000,?,1,'WAW')",(datetime.utcnow().isoformat(),self.old))
        c.execute("INSERT INTO tracked_departures_by_airport(hotel_id,airport_code,departure_date,status,current_price) VALUES(1,'WAW',?,'available',1000)",(self.old,))
        c.commit();c.close()
    def tearDown(self):self.db.stop();self.directory.cleanup()
    def hotel(self):
        c=app.connect();h=c.execute('SELECT * FROM hotels WHERE id=1').fetchone();c.close();return h
    def old_record(self):
        c=app.connect();r=dict(c.execute("SELECT * FROM tracked_departures_by_airport WHERE hotel_id=1 AND airport_code='WAW' AND departure_date=?",(self.old,)).fetchone());c.close();return r
    def check_itaka(self,variants):
        response=MagicMock();response.__enter__.return_value.read.return_value=b'page'
        with patch.object(app.urllib.request,'urlopen',return_value=response),patch.object(app,'parse_itaka_variants',return_value=(variants,[])):
            app.check_itaka_hotel(self.hotel(),self.limit)
    def test_first_successful_missing_check_archives_and_preserves_provenance(self):
        self.check_itaka([self.variant])
        self.assertEqual(self.old_record()['status'],'unavailable');self.assertEqual(self.old_record()['missing_checks'],1)
        cache=json.loads(self.hotel()['airport_options'])
        self.assertNotIn(self.old,cache['by_date']);self.assertEqual(cache['known_by_date'][self.old]['airports'][0]['code'],'WAW')
        self.check_itaka([self.variant])
        self.assertIn(self.old,json.loads(self.hotel()['airport_options'])['known_by_date'])
        c=app.connect();self.assertEqual(c.execute('SELECT count(*) FROM prices WHERE departure_date=?',(self.old,)).fetchone()[0],1);c.close()
        self.check_itaka([self.option,self.variant]);self.assertEqual(self.old_record()['status'],'available');self.assertEqual(self.old_record()['missing_checks'],0)
    def test_network_failure_does_not_archive(self):
        with patch.object(app.urllib.request,'urlopen',side_effect=TimeoutError('network')):
            with self.assertRaises(TimeoutError):app.check_itaka_hotel(self.hotel(),self.limit)
        self.assertEqual(self.old_record()['status'],'available')
    def test_verified_legacy_airport_history_survives_first_structured_check(self):
        c=app.connect();c.execute('UPDATE hotels SET airport_options=? WHERE id=1',(json.dumps({'version':1,'airports':[self.option]}),));c.commit();c.close()
        self.check_itaka([self.variant]);self.assertEqual(self.old_record()['status'],'unavailable')
        self.assertIn(self.old,json.loads(self.hotel()['airport_options'])['known_by_date'])
    def rainbow(self,complete):
        c=app.connect();c.execute("UPDATE hotels SET url='https://r.pl/hotel' WHERE id=1");c.commit();c.close()
        result=(None,None,None,[(self.new,1200)],complete,'WAW','Warszawa')
        with patch.object(app,'fetch_offer',return_value=result):self.assertEqual(app.run_checks(),(1,0))
    def test_rainbow_first_complete_missing_check_archives(self):
        self.rainbow(True);self.assertEqual(self.old_record()['status'],'unavailable');self.assertEqual(self.old_record()['missing_checks'],1)
    def test_rainbow_incomplete_calendar_does_not_archive(self):
        self.rainbow(False);self.assertEqual(self.old_record()['status'],'available')

    def test_availability_events_are_append_only_and_survive_export_import(self):
        import os
        self.check_itaka([self.variant])
        self.check_itaka([self.variant])
        self.check_itaka([self.option,self.variant])
        state=app.state_from_database()
        events=state['availability_events']
        self.assertEqual([(e['from_status'],e['to_status']) for e in events],[('available','unavailable'),('unavailable','available')])
        self.assertEqual(events[0]['price'],None)
        self.assertEqual(events[0]['previous_price'],1000)
        self.assertEqual(events[1]['price'],1000)  # Returning at the same price is still an event.
        self.assertTrue(all(e['checked_at'] and e['departure_date']==self.old and e['airport_code']=='WAW' for e in events))
        with patch.dict(os.environ,{'GITHUB_TOKEN':'test'}),patch.object(app.github_state,'read_state',return_value=state):
            app.sync_state_from_github()
        self.assertEqual(app.state_from_database()['availability_events'],events)
        self.check_itaka([self.option,self.variant])
        self.assertEqual(app.state_from_database()['availability_events'],events)

    def test_failed_check_and_old_snapshot_do_not_invent_events(self):
        import os
        self.assertEqual(app.state_from_database()['availability_events'],[])
        with patch.object(app.urllib.request,'urlopen',side_effect=TimeoutError('network')):
            with self.assertRaises(TimeoutError):app.check_itaka_hotel(self.hotel(),self.limit)
        self.assertEqual(app.state_from_database()['availability_events'],[])
        old=app.state_from_database();old.pop('availability_events')
        with patch.dict(os.environ,{'GITHUB_TOKEN':'test'}),patch.object(app.github_state,'read_state',return_value=old):app.sync_state_from_github()
        self.assertEqual(app.state_from_database()['availability_events'],[])

    def test_rainbow_records_transition_once(self):
        self.rainbow(True)
        events=app.state_from_database()['availability_events']
        self.assertEqual(len(events),1);self.assertEqual(events[0]['to_status'],'unavailable')
        self.rainbow(True)
        self.assertEqual(app.state_from_database()['availability_events'],events)

if __name__=='__main__':unittest.main()
