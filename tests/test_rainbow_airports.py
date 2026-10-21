"""Regression tests for Rainbow's flight drawer, without requesting external pages."""
import unittest
from playwright.sync_api import sync_playwright
from app import read_rainbow_flight_cards, rainbow_airports_from_cards, open_rainbow_control

FIXTURE = """
<h4>Unrelated hotel heading</h4>
<button>WYBRANY</button>
<h6>29 paź 2026 - 06 lis 2026</h6>
<div role="group" aria-label="Wybrane połączenie" data-test-id="r-component:pojedyncze-polaczenie:container-0">
  <div><span>16:15</span><span>Katowice</span><span>Teneryfa - Południe</span></div>
  <div><h4>w cenie</h4><button>WYBRANY</button></div>
  <div hidden><h6>Wylot</h6>Katowice (KTW)<h6>Powrót</h6></div>
</div>
<h6>29 paź 2026 - 05 lis 2026</h6>
<div role="group" aria-label="Dostępne połączenie" data-test-id="r-component:pojedyncze-polaczenie:container-1">
  <div>08:20 Poznań Teneryfa - Południe</div>
  <div><div><h4>-55 zł razem</h4><button>WYBIERZ</button></div></div>
  <div hidden><h6>Wylot</h6><h6>Powrót</h6></div>
</div>
<div role="group" aria-label="Dostępne połączenie" data-test-id="r-component:pojedyncze-polaczenie:container-2">
  <div>08:55 Warszawa Chopin Teneryfa - Południe</div>
  <div><h4>+234 zł razem</h4><button>WYBIERZ</button></div>
</div>
<h6>04 mar 2027 - 12 mar 2027</h6>
<div role="group" aria-label="Dostępne połączenie" data-test-id="r-component:pojedyncze-polaczenie:container-3">
  <div>12:45 Wrocław Teneryfa - Południe</div>
  <div><h4>-23 zł razem</h4><button>WYBIERZ</button></div>
</div>
"""

class RainbowAirportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True,args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page=self.browser.new_page()
        self.page.set_content(FIXTURE)

    def tearDown(self):
        self.page.close()

    def test_full_cards_include_h6_dates_airports_and_selected_flight(self):
        cards=read_rainbow_flight_cards(self.page)
        self.assertEqual(len(cards),4)
        options=rainbow_airports_from_cards(cards,"2026-10-29")
        self.assertEqual([item["name"] for item in options],["Katowice","Poznań","Warszawa Chopin"])
        self.assertEqual([item["i"] for item in options],[0,1,2])
        self.assertTrue(options[0]["selected"])
        self.assertFalse(options[1]["selected"])

    def test_airports_from_other_dates_are_not_offered(self):
        cards=read_rainbow_flight_cards(self.page)
        options=rainbow_airports_from_cards(cards,"2027-03-04")
        self.assertEqual([item["name"] for item in options],["Wrocław"])
        self.assertEqual(rainbow_airports_from_cards(cards,"2026-11-05"),[])

    def test_single_selected_airport_does_not_require_choose_button(self):
        self.page.set_content("""
            <h6>5 listopada 2026 - 13 listopada 2026</h6>
            <div data-test-id="r-component:pojedyncze-polaczenie:container-0">
                Katowice <button>WYBRANY</button>
            </div>
        """)
        options=rainbow_airports_from_cards(read_rainbow_flight_cards(self.page),"2026-11-05")
        self.assertEqual(len(options),1)
        self.assertEqual(options[0]["code"],"R-KATOWICE")

    def test_panel_can_mount_after_the_control_is_clicked(self):
        self.page.set_content("""
            <button id="open" onpointerdown="setTimeout(() => {
                const panel=document.createElement('div');
                panel.id='panel'; panel.textContent='Loty'; document.body.append(panel);
            }, 200)">Otwórz loty</button>
        """)
        open_rainbow_control(self.page,self.page.locator("#open"),self.page.locator("#panel"))
        self.assertTrue(self.page.locator("#panel").is_visible())

    def test_duplicate_airports_and_invalid_dates_do_not_create_options(self):
        cards=[
            dict(i=0,heading="29 paź 2026 - 05 lis 2026",text="Poznań"),
            dict(i=1,heading="29 paź 2026 - 05 lis 2026",text="Poznań"),
            dict(i=2,heading="31 listopada 2026 - 05 gru 2026",text="Warszawa Chopin"),
        ]
        self.assertEqual(len(rainbow_airports_from_cards(cards,"2026-10-29")),1)
        self.assertEqual(rainbow_airports_from_cards(cards,"2026-11-30"),[])

if __name__=="__main__":
    unittest.main()
