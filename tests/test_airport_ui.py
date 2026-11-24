"""API regressions for date-specific airport choices."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as hotel_app

AIRPORTS=[
    {"code":"R-WARSZAWA-CHOPIN","name":"Warszawa Chopin"},
    {"code":"R-POZNAN","name":"Poznań"},
    {"code":"R-KATOWICE","name":"Katowice"},
]

class AirportApiTests(unittest.TestCase):
    def test_hotel_payload_keeps_date_specific_airports_without_variant_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(hotel_app,"DB_PATH",str(Path(directory)/"test.db")), \
                 patch.object(hotel_app,"sync_state_from_github",return_value=True), \
                 patch.object(hotel_app,"APP_PASSWORD",""):
                db=hotel_app.connect()
                cached={"by_date":{"2026-10-29":{"airports":[dict(item,url="https://r.pl/private-variant") for item in AIRPORTS]}}}
                db.execute("UPDATE hotels SET airport_options=? WHERE id=1",(json.dumps(cached),))
                db.execute("UPDATE hotels SET airport_options=? WHERE id=2",(json.dumps({"airports":[{"code":"WAW","name":"Warszawa"},{"code":"POZ","name":"Poznań"}]}),))
                db.commit()
                db.close()
                response=hotel_app.app.test_client().get("/api/hotels")
                self.assertEqual(response.status_code,200)
                hotels={item["id"]:item for item in response.get_json()["hotels"]}
                self.assertEqual(hotels[1]["airport_options_by_date"]["2026-10-29"],AIRPORTS)
                self.assertEqual(len(hotels[2]["airport_options"]),2)
                self.assertNotIn("url",hotels[1]["airport_options_by_date"]["2026-10-29"][0])

if __name__=="__main__":
    unittest.main()
