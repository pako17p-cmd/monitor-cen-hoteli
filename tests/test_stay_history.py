import os,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timedelta
import app,stay_history

class StayHistoryTests(unittest.TestCase):
    def test_lengths_have_independent_prices_availability_and_round_trip_storage(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(app,'DB_PATH',str(Path(directory)/'test.db')):
            c=app.connect()
            date=(datetime.utcnow()+timedelta(days=10)).date().isoformat();limit=(datetime.utcnow()+timedelta(days=40)).date().isoformat()
            def offer(nights,price):return dict(date=date,code='WAW',nights=nights,days=nights+1,price=price,url=f'https://www.itaka.pl/hotel?duration={nights+1}',return_code='WAW')
            stay_history.record(c,1,[offer(7,6729),offer(14,10000)],'2026-11-05T12:00:00',limit)
            stay_history.record(c,1,[offer(7,6500),offer(14,10000)],'2026-11-05T14:00:00',limit)
            prices=c.execute('SELECT nights,price FROM stay_prices ORDER BY id').fetchall()
            self.assertEqual([tuple(r) for r in prices],[(7,6729),(14,10000),(7,6500)])
            stay_history.record(c,1,[offer(7,6500)],'2026-11-05T16:00:00',limit)
            self.assertEqual([(r['nights'],r['to_status']) for r in c.execute('SELECT * FROM stay_events')],[(14,'unavailable')])
            stay_history.record(c,1,[offer(7,6500),offer(14,10000)],'2026-11-05T18:00:00',limit)
            self.assertEqual([(r['nights'],r['to_status']) for r in c.execute('SELECT * FROM stay_events')],[(14,'unavailable'),(14,'available')])
            c.commit();c.close()
            state=app.state_from_database()
            with patch.dict(os.environ,{'GITHUB_TOKEN':'test'}),patch.object(app.github_state,'read_state',return_value=state):app.sync_state_from_github()
            restored=app.state_from_database()
            for name in stay_history.TABLES:self.assertEqual(restored[name],state[name])

if __name__=='__main__':unittest.main()
