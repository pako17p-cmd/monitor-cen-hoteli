"""Browser regressions for airport choices surviving refresh and using their own trip date."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from flask import render_template_string
from playwright.sync_api import sync_playwright
import app as hotel_app

AIRPORTS=[
    {"code":"R-WARSZAWA-CHOPIN","name":"Warszawa Chopin"},
    {"code":"R-POZNAN","name":"Poznań"},
    {"code":"R-KATOWICE","name":"Katowice"},
]
DECEMBER=AIRPORTS+[{"code":"R-WROCLAW","name":"Wrocław"}]

def hotel(id,name,date,provider,code):
    return dict(id=id,name=name,url="https://r.pl/example",country="Hiszpania",
        airport_code=code,airport_name="Warszawa Chopin",people_count=2,
        image_url=None,price=3830.5,total_price=7661,checked="2026-10-21T06:00:00",
        departure_date=date,provider=provider,airport_options=[AIRPORTS[0]],
        airport_options_by_date={"2026-10-29":AIRPORTS,"2026-12-31":DECEMBER} if provider=="rainbow" else {},
        offers=[dict(date=date,price=3830.5,status="available")]+
               ([dict(date="2026-12-31",price=7000,status="available")] if provider=="rainbow" else []))

class AirportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True,args=["--no-sandbox"])
        with hotel_app.app.test_request_context("/"):
            cls.html=render_template_string(hotel_app.PAGE,auto_check_enabled=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.hotels=[
            hotel(1,"Other hotel","2026-10-24","itaka","WAW"),
            hotel(5,"Palia Don Pedro","2026-10-29","rainbow","R-WARSZAWA-CHOPIN"),
        ]
        self.posts=[]
        self.page=self.browser.new_page()
        self.page.route("**/*",self.route)
        self.page.goto("http://hotel-test.local/")
        self.select=self.page.locator("article.hotel").nth(1).locator("select")
        self.select.wait_for(state="visible")

    def tearDown(self):
        self.page.close()

    def route(self,route):
        parsed=urlparse(route.request.url)
        path=parsed.path
        if path=="/":
            route.fulfill(content_type="text/html",body=self.html)
            return
        if not path.startswith("/api/"):
            route.fulfill(status=404,body="")
            return
        if path=="/api/hotels":
            result={"hotels":copy.deepcopy(self.hotels)}
        elif path.endswith("/history"):
            result={"history":[],"analysis":None}
        elif path.endswith("/airports"):
            day=parse_qs(parsed.query).get("date",[""])[0]
            result={"airports":DECEMBER if day=="2026-12-31" else AIRPORTS,"warning":None}
        elif path=="/api/hotels/5/airport":
            data=route.request.post_data_json
            self.posts.append(data)
            self.hotels[1].update(airport_code=data["airport_code"],airport_name="Poznań",price=3686.5)
            result={"ok":True,"message":"Lotnisko zmienione"}
        else:
            raise AssertionError(f"Unexpected API route: {path}")
        route.fulfill(content_type="application/json",body=json.dumps(result))

    def test_airport_choices_survive_periodic_hotel_refresh(self):
        self.assertEqual(self.select.locator("option").count(),4)
        self.page.evaluate("load()")
        self.assertEqual(self.select.locator("option").all_text_contents(),
            ["Zmień lotnisko…","Warszawa Chopin","Poznań","Katowice"])

    def test_airport_selection_uses_displayed_date_not_another_hotel(self):
        self.assertEqual(self.page.evaluate("selectedDate"),"2026-10-24")
        self.select.select_option("R-POZNAN")
        self.page.wait_for_function("document.querySelector('#chartname').textContent.includes('Palia Don Pedro')")
        self.assertEqual(self.posts,[{"airport_code":"R-POZNAN","date":"2026-10-29"}])
        self.assertEqual(self.page.evaluate("selected"),5)
        self.assertEqual(self.page.evaluate("selectedDate"),"2026-10-29")
        self.assertEqual(self.select.input_value(),"R-POZNAN")

    def test_each_displayed_date_keeps_its_own_airports_after_refresh(self):
        self.page.evaluate("selectHotel(5,'2026-12-31')")
        self.page.evaluate("load()")
        self.assertEqual(self.select.locator("option").count(),5)
        self.assertIn("Wrocław",self.select.locator("option").all_text_contents())
        self.page.evaluate("selectHotel(5,'2026-10-29')")
        self.assertNotIn("Wrocław",self.select.locator("option").all_text_contents())

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
